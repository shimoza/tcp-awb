"""UX14: password changes and freshness checks never spend an unrequested login."""
from __future__ import annotations

import getpass
import json
import os
from types import SimpleNamespace

import pytest

from awb import cli, config, vault
from awb.tcp import keys, labadmin, tenants, xchg
from tests import fixtures as fx

OLD = "-".join(("old", "invented", "4711"))
NEW = "-".join(("new", "invented", "4712"))
HINT = "the stored login may be stale: awb keys status"


@pytest.fixture
def rig(tmp_path, monkeypatch):
    root = tmp_path / "store"
    monkeypatch.setenv("PASSWORD_STORE_DIR", str(root))
    monkeypatch.setattr(config, "is_work_user", lambda: False)
    monkeypatch.setattr(vault, "in_assistant_session", lambda: False)
    monkeypatch.setattr(labadmin, "ALLOWED", (fx.TENANT_ONE, fx.TENANT_TWO))
    settings = {"lab_admin": "%s %s" % (fx.TENANT_ONE, fx.TENANT_TWO)}
    monkeypatch.setattr(xchg, "read_settings", lambda: settings)
    values, writes, reads, logins, requests = {}, [], [], [], []

    def put(entry, value):
        values[entry] = value
        path = root / (entry + ".gpg")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"encrypted stand-in")

    for alias in (fx.TENANT_ONE, fx.TENANT_TWO):
        for part, value in dict(domain=fx.CUSTOMER_DOMAIN, user=fx.LOG_USER, password=OLD).items():
            put("awb/admin/%s/%s" % (alias, part), value)
        for part, value in {"ak": "AK" + "FAKE", "sk": "sk-" + "fake", "secret/ecs-admin": OLD}.items():
            put("awb/tenant/%s/%s" % (alias, part), value)

    def run(argv, **kwargs):
        if argv[1] == "insert":
            assert argv[2] == "-m" and "-f" in argv
            assert NEW not in " ".join(argv)
            writes.append(argv[3])
            put(argv[3], kwargs["input"].removesuffix("\n"))
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        assert argv[1] == "show"
        reads.append(argv[2])
        return SimpleNamespace(returncode=0, stdout=values[argv[2]] + "\n", stderr="")

    store = tenants.Store(root, run=run)
    monkeypatch.setattr(tenants, "Store", lambda: store)
    monkeypatch.setattr(keys.subprocess, "run", run)
    svc = keys.Service(tmp_path / "admin.sock", tmp_path / "call.sock")

    def request(sock, req, **kwargs):
        requests.append(req["op"])
        if req["op"] == "tenants":
            return {"ok": True, "tenants": svc._summary()}
        return svc._admin(req, os.getuid())

    monkeypatch.setattr(keys, "request", request)

    def http(method, url, headers, data, **kwargs):
        assert method == "POST" and "/v3/auth/tokens" in url
        logins.append(json.loads(data)["auth"]["identity"]["password"]["user"]["password"])
        return 201, {"X-Subject-Token": "invented-token"}, b'{"token": {}}'

    monkeypatch.setattr(labadmin, "_http", http)
    keys.unlock()
    reads.clear()
    monkeypatch.setattr(getpass, "getpass", lambda prompt: NEW)
    monkeypatch.setattr("builtins.input", lambda prompt: "")
    return SimpleNamespace(root=root, svc=svc, values=values, writes=writes, reads=reads, logins=logins,
                           requests=requests)


def test_password_updates_store_and_running_service_for_one_alias(rig, capsys):
    other = rig.svc.tenants[fx.TENANT_TWO]
    for alias in (fx.TENANT_ONE, fx.TENANT_TWO):
        rig.svc.lab.get((alias, "domain@eu-de"), lambda: ("invented-token", {}, 3600))
    assert cli.main(["tenant", "password", fx.TENANT_ONE]) == 0
    entry = "awb/admin/%s/password" % fx.TENANT_ONE
    assert rig.values[entry] == NEW and rig.writes == [entry]
    assert rig.svc.tenants[fx.TENANT_ONE]["admin"]["password"] == NEW
    assert rig.svc.tenants[fx.TENANT_TWO] == other
    assert (fx.TENANT_ONE, "domain@eu-de") not in rig.svc.lab.tokens
    assert (fx.TENANT_TWO, "domain@eu-de") in rig.svc.lab.tokens
    assert not any(fx.TENANT_TWO in e for e in rig.reads)
    assert rig.logins == []
    assert keys.main(["status"]) == 0
    output = capsys.readouterr()
    assert "store newer than loaded" not in output.out
    assert NEW not in output.out + output.err and OLD not in output.out + output.err
    fx.assert_no_fixture_name(output.out + output.err)


@pytest.mark.parametrize("prefix, part, handle", [
    ("admin", "password", "admin/password"), ("admin", "user", "admin/user"),
    ("admin", "domain", "admin/domain"), ("tenant", "ak", "read/ak"),
    ("tenant", "sk", "read/sk"), ("tenant", "secret/ecs-admin", "secret-1"),
])
def test_status_detects_touched_store_without_decryption_or_login_and_command_clears_it(
        rig, capsys, prefix, part, handle):
    path = rig.root / ("awb/%s/%s/%s.gpg" % (prefix, fx.TENANT_ONE, part))
    stamp = path.stat().st_mtime_ns + 10_000_000_000
    os.utime(path, ns=(stamp, stamp))
    assert keys.main(["status"]) == 0
    output = capsys.readouterr().out
    assert fx.TENANT_ONE in output and handle + ": store newer than loaded" in output
    assert rig.reads == [] and rig.logins == []
    assert tenants.main(["password", fx.TENANT_ONE]) == 0
    capsys.readouterr()
    assert keys.main(["status"]) == 0
    assert "store newer than loaded" not in capsys.readouterr().out
    assert rig.logins == []


def test_optional_login_is_explicit_and_uses_new_loaded_password_once(rig, monkeypatch, capsys):
    prompts = []

    def confirm(prompt):
        prompts.append(prompt)
        assert rig.svc.tenants[fx.TENANT_ONE]["admin"]["password"] == NEW
        return "y"

    monkeypatch.setattr("builtins.input", confirm)
    assert tenants.main(["password", fx.TENANT_ONE]) == 0
    assert prompts == ["test with one login now? [y/N] "]
    assert rig.logins == [NEW]
    output = capsys.readouterr()
    fx.assert_no_fixture_name(output.out + output.err)
    assert NEW not in output.out + output.err


@pytest.mark.parametrize("failed_login", [True, False], ids=["login-401", "call-401"])
def test_lab_401_gives_stale_hint_and_never_attempts_a_second_login(rig, monkeypatch, failed_login):
    login_calls, api_calls = [], []

    def http(method, url, headers, data, **kwargs):
        if "/v3/auth/tokens" in url:
            login_calls.append(url)
            if not failed_login:
                return 201, {"X-Subject-Token": "invented-token"}, b'{"token": {}}'
        else:
            api_calls.append(url)
        return 401, {}, json.dumps({"error": {"message": fx.CUSTOMER_DOMAIN + OLD}}).encode()

    monkeypatch.setattr(labadmin, "_http", http)
    auth = labadmin._Auth(rig.svc, fx.TENANT_ONE, rig.svc.tenants[fx.TENANT_ONE]["admin"], "eu-de")
    error = None
    try:
        labadmin._send(rig.svc, auth, "run-1", "GET", "iam", "/v3/users", {}, None, {}, None, None, None)
    except labadmin.LabAdminError as exc:
        error = exc
    assert len(login_calls) == 1
    assert len(api_calls) == (0 if failed_login else 1)
    assert error is not None and HINT in str(error)
    assert OLD not in str(error)
    fx.assert_no_fixture_name(str(error))


@pytest.mark.parametrize("guard", ["work-user", "assistant-session"])
def test_password_is_owner_only_before_prompt_or_store_access(rig, monkeypatch, guard):
    assert tenants.main(["password", fx.TENANT_ONE]) == 0
    rig.writes.clear()
    rig.reads.clear()
    rig.requests.clear()
    if guard == "work-user":
        monkeypatch.setattr(config, "is_work_user", lambda: True)
    else:
        monkeypatch.setattr(vault, "in_assistant_session", lambda: True)
    monkeypatch.setattr(getpass, "getpass", lambda prompt: pytest.fail("password asked before owner check"))
    assert tenants.main(["password", fx.TENANT_ONE]) == 1
    assert rig.writes == rig.reads == rig.requests == rig.logins == []


def test_failed_load_preserves_store_and_never_offers_login(rig, monkeypatch, capsys):
    assert tenants.main(["password", fx.TENANT_ONE]) == 0
    capsys.readouterr()
    monkeypatch.setattr(getpass, "getpass", lambda prompt: OLD)

    def fail(**kwargs):
        raise keys.KeysError(OLD)

    monkeypatch.setattr(keys, "unlock", fail)
    monkeypatch.setattr("builtins.input", lambda prompt: pytest.fail("login offered after failed load"))
    assert tenants.main(["password", fx.TENANT_ONE]) == 1
    assert rig.values["awb/admin/%s/password" % fx.TENANT_ONE] == OLD
    assert rig.svc.tenants[fx.TENANT_ONE]["admin"]["password"] == NEW
    assert rig.logins == []
    output = capsys.readouterr()
    assert "stored but not loaded: awb keys unlock" in output.err
    assert OLD not in output.out + output.err


def test_optional_login_401_is_one_attempt_and_keeps_new_store_value(rig, monkeypatch, capsys):
    calls = []

    def http(*args, **kwargs):
        calls.append(args)
        return 401, {}, b""

    monkeypatch.setattr(labadmin, "_http", http)
    monkeypatch.setattr("builtins.input", lambda prompt: "y")
    assert tenants.main(["password", fx.TENANT_ONE]) == 1
    assert len(calls) == 1
    assert rig.values["awb/admin/%s/password" % fx.TENANT_ONE] == NEW
    assert rig.svc.tenants[fx.TENANT_ONE]["admin"]["password"] == NEW
    assert HINT in capsys.readouterr().err
