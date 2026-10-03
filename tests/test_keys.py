"""awb/tcp/keys.py: the key service holds the keys and secrets of the test tenants and signs for the sessions.

The service runs on short socket paths under /tmp against the stand-in gateway of tests/tcp_fake.py, which checks
every signature and, for this test, echoes what a write sent. Every key and secret here is invented.
"""
from __future__ import annotations

import base64
import contextlib
import json
import shutil
import tempfile
from pathlib import Path

import pytest

from awb import cli, config, jobs
from awb.tcp import keys
from tests.tcp_fake import FakeGateway

AK, SK = "AKFAKE", "sk-fake-secret"
LAB_AK, LAB_SK = "AKLABFAKE", "sk-lab-fake-secret"
ADMIN_PW = "-".join(("Invented", "Pass", "4711"))      # built from pieces: an invented value
STORE = {
    "test-1/ak": AK, "test-1/sk": SK,
    "test-1/lab/ak": LAB_AK, "test-1/lab/sk": LAB_SK,
    "test-1/secret/ecs-admin": ADMIN_PW,
    "test-2/ak": "AKOTHER", "test-2/sk": "sk-other-secret",
}
PROJECT = "tcp-ab2c"


class EchoGateway(FakeGateway):
    """The stand-in gateway, with a write path that echoes the body it got, the way an API may echo a field."""

    def route(self, method, raw_path):
        path = raw_path.partition("?")[0]
        if method == "POST" and path == "/v1/%s/cloudservers" % self.PROJECT:
            sent = json.loads(self.bodies[-1] or b"{}")
            pw = json.dumps(sent).split('"admin_pass": "')[1].split('"')[0] if "admin_pass" in json.dumps(sent) else ""
            return 200, {"server": sent, "user_data_seen": base64.b64encode(pw.encode()).decode()}
        if method == "DELETE" and path.startswith("/v1/%s/cloudservers/" % self.PROJECT):
            return 204, ""
        return super().route(method, raw_path)


@contextlib.contextmanager
def short_dir():
    d = tempfile.mkdtemp(prefix="awbk", dir="/tmp")
    try:
        yield Path(d)
    finally:
        shutil.rmtree(d, ignore_errors=True)


@pytest.fixture(autouse=True)
def no_waiting(monkeypatch):
    monkeypatch.setattr(jobs.time, "sleep", lambda s: None)


@pytest.fixture(autouse=True)
def calls_logged_here(monkeypatch):
    """The call log of the service. A test that plays the work user makes config.paths() ignore the test folders,
    so the log would land in the real home: it is kept in a list instead."""
    from awb.tcp import throttle

    logged: list = []
    monkeypatch.setattr(throttle, "log_call", lambda *a, **k: logged.append(a))
    return logged


@pytest.fixture
def gw():
    with EchoGateway(AK, SK) as g:
        yield g


@pytest.fixture
def svc(gw, tmp_path, monkeypatch):
    with short_dir() as d:
        s = keys.Service(d / "admin.sock", d / "call.sock", endpoint=gw.base, log_dir=tmp_path / "log")
        s._registered = lambda: {"test-1": ("eu-de",), "test-2": ("eu-de", "eu-nl")}
        s._active_project = lambda code: code == PROJECT
        s.start()
        monkeypatch.setenv(keys.CALL_SOCKET_ENV, str(s.call_path))
        monkeypatch.setenv(keys.ADMIN_SOCKET_ENV, str(s.admin_path))
        try:
            yield s
        finally:
            s.stop()


def load(svc):
    return keys.unlock(svc.admin_path, sorted(STORE), reader=lambda e: STORE[e])


def call(svc, **req):
    req.setdefault("op", "call")
    req.setdefault("tenant", "test-1")
    return keys.request(svc.call_path, req)


def lab_gateway(gw):
    """The lab key signs: the gateway checks the lab key's signature."""
    gw.ak, gw.sk = LAB_AK, LAB_SK


# --------------------------------------------------------------------------- the password store


def test_the_layout_of_the_password_store():
    t = keys.collect(sorted(STORE), reader=lambda e: STORE[e])
    assert sorted(t) == ["test-1", "test-2"]
    assert t["test-1"]["roles"]["read"] == {"ak": AK, "sk": SK}
    assert t["test-1"]["roles"]["lab"] == {"ak": LAB_AK, "sk": LAB_SK}
    assert t["test-1"]["secrets"] == {"ecs-admin": ADMIN_PW}
    assert t["test-2"]["roles"] == {"read": {"ak": "AKOTHER", "sk": "sk-other-secret"}}


@pytest.mark.parametrize("entries", [["Test/ak"], ["test-1/other/ak"], ["test-1/secret/Bad Name"], ["test-1/ak"],
                                     ["test-1/lab/ak"], ["test-1/x/y/z"]])
def test_an_entry_outside_the_layout_is_refused_without_its_name(entries):
    with pytest.raises(keys.KeysError) as err:
        keys.collect(entries, reader=lambda e: "value-of-entry")
    assert "value-of-entry" not in str(err.value) and "Bad Name" not in str(err.value)


def test_entries_come_from_the_file_names_under_awb_tenant(tmp_path):
    base = tmp_path / "awb" / "tenant" / "test-1"
    (base / "lab").mkdir(parents=True)
    for f in ("ak.gpg", "sk.gpg", "lab/ak.gpg", "lab/sk.gpg"):
        (base / f).write_bytes(b"x")
    (tmp_path / "awb" / "cloudflare.gpg").write_bytes(b"x")
    (tmp_path / "other.gpg").write_bytes(b"x")
    assert keys.store_entries(tmp_path) == ["test-1/ak", "test-1/lab/ak", "test-1/lab/sk", "test-1/sk"]


# --------------------------------------------------------------------------- the service


def test_locked_until_the_owner_loads_and_the_status_shows_names_only(svc, capsys):
    assert call(svc, method="GET", service="vpc", path="/v1/{project_id}/vpcs")["error"].startswith(
        "the key service is locked")
    summary = load(svc)
    assert summary["test-1"] == {"roles": ["lab", "read"], "secrets": ["ecs-admin"]}
    assert cli.main(["keys", "status"]) == 0
    out = capsys.readouterr().out
    assert "test-1" in out and "ecs-admin" in out and "eu-de" in out
    for value in STORE.values():
        assert value not in out
    assert keys.request(svc.admin_path, {"op": "lock"})["ok"]
    assert call(svc, method="GET", service="vpc", path="/v1/x")["error"].startswith("the key service is locked")


def test_a_read_call_is_signed_with_the_tenant_key(svc, gw):
    load(svc)
    a = call(svc, method="GET", service="vpc", path="/v1/{project_id}/vpcs", query={"limit": "3"})
    assert a["ok"] and a["status"] == 200 and len(a["data"]["vpcs"]) == 3


@pytest.mark.parametrize("req, error", [
    ({"method": "POST", "service": "ecs", "path": "/v1/{project_id}/cloudservers"}, "the read key allows GET, HEAD only"),
    ({"tenant": "test-9", "method": "GET", "service": "vpc", "path": "/v1/x"}, "no tenant of that alias"),
    ({"tenant": "test-2", "role": "lab", "method": "GET", "service": "vpc", "path": "/v1/x"}, "has no lab key"),
    ({"method": "GET", "service": "vpc", "path": "/v1/x", "region": "eu-ch2"}, "not one of the tenant's"),
    ({"method": "GET", "service": "vpc", "path": "/v1/../x"}, "a path is plain"),
    ({"method": "GET", "service": "https://evil", "path": "/v1/x"}, "a service name reads like"),
    ({"role": "admin", "method": "GET", "service": "vpc", "path": "/v1/x"}, "the role is read or lab"),
])
def test_calls_the_service_will_not_make(svc, req, error):
    load(svc)
    a = call(svc, **req)
    assert not a["ok"] and error in a["error"]


def test_a_write_needs_the_lab_key_and_an_active_project(svc, gw):
    load(svc)
    lab_gateway(gw)
    body = {"server": {"name": "lab-1", "admin_pass": "{{secret:ecs-admin}}"}}
    a = call(svc, role="lab", method="POST", service="ecs", path="/v1/{project_id}/cloudservers", body=body)
    assert "a write needs the code of an active project" in a["error"]
    a = call(svc, role="lab", method="POST", service="ecs", path="/v1/{project_id}/cloudservers", body=body,
             project="tcp-zzzz")
    assert "a write needs the code of an active project" in a["error"]
    a = call(svc, role="lab", method="POST", service="ecs", path="/v1/{project_id}/cloudservers", body=body,
             project=PROJECT)
    assert a["ok"] and a["status"] == 200, a
    # the gateway got the real password and echoed it back plainly and in base64: the answer carries neither
    assert ADMIN_PW.encode() in gw.bodies[-1]
    text = json.dumps(a)
    assert ADMIN_PW not in text and base64.b64encode(ADMIN_PW.encode()).decode() not in text
    assert a["data"]["server"]["server"]["admin_pass"] == keys.MASK
    assert a["data"]["user_data_seen"] == keys.MASK


@pytest.mark.parametrize("body, error", [
    ({"server": {"user_data": "{{secret:ecs-admin}}"}}, "may only fill a password field"),
    ({"server": {"admin_pass": "x{{secret:ecs-admin}}"}}, "must be the whole value"),
    ({"server": {"admin_pass": "{{secret:missing}}"}}, "no secret of that name"),
    ({"server": {"metadata": {"note": "{{secret:ecs-admin}}"}}}, "may only fill a password field"),
])
def test_a_secret_fills_a_password_field_and_nothing_else(svc, gw, body, error):
    load(svc)
    lab_gateway(gw)
    n = len(gw.calls)
    a = call(svc, role="lab", method="POST", service="ecs", path="/v1/{project_id}/cloudservers", body=body,
             project=PROJECT)
    assert not a["ok"] and error in a["error"]
    assert len(gw.calls) == n          # refused before anything was sent


def test_a_key_the_platform_echoes_is_taken_out(svc, gw):
    load(svc)
    gw.echo_key = True
    gw.sk = "another"                # the signature fails and the error names the key it got
    a = call(svc, method="GET", service="iam", path="/v3/projects")
    assert a["status"] == 401 and AK not in json.dumps(a) and keys.MASK in json.dumps(a)


def test_every_call_is_logged_without_a_path_or_a_value(svc, gw, tmp_path):
    load(svc)
    call(svc, method="GET", service="vpc", path="/v1/{project_id}/vpcs")
    lab_gateway(gw)
    call(svc, role="lab", method="DELETE", service="ecs", path="/v1/{project_id}/cloudservers/srv-1",
         project=PROJECT)
    lines = (next((tmp_path / "log").glob("cloud-*.tsv"))).read_text().splitlines()
    assert [l.split("\t")[2:] for l in lines] == [["test-1", "read", "GET", "vpc", "eu-de", "200", "-"],
                                                  ["test-1", "lab", "DELETE", "ecs", "eu-de", "204", PROJECT]]
    text = "\n".join(lines)
    assert "srv-1" not in text and "cloudservers" not in text
    for value in STORE.values():
        assert value not in text


def test_the_rate_limit_per_user(svc, monkeypatch):
    load(svc)
    monkeypatch.setattr(keys, "RATE_LIMIT", 3)
    answers = [keys.request(svc.call_path, {"op": "ping"}) for _ in range(5)]
    assert [a.get("error") for a in answers][-1] == "rate"


# --------------------------------------------------------------------------- the commands


def test_awb_cloud_call_for_the_work_user(svc, gw, tmp_path, monkeypatch, capsys):
    load(svc)
    monkeypatch.setattr(config, "is_work_user", lambda: True)
    assert cli.main(["cloud", "call", "GET", "vpc", "/v1/{project_id}/vpcs", "--tenant", "test-1",
                     "--query", "limit=2"]) == 0
    out = capsys.readouterr().out
    assert len(json.loads(out)["vpcs"]) == 2
    # the owner's direct key path stays closed to the work user
    assert cli.main(["cloud", "get", "vpc", "/v1/{project_id}/vpcs"]) == 2
    assert "no cloud key" in capsys.readouterr().err


def test_awb_cloud_call_takes_the_project_of_the_working_folder_and_a_body_file(svc, gw, tmp_path, monkeypatch,
                                                                               capsys):
    load(svc)
    lab_gateway(gw)
    proj = tmp_path / PROJECT
    proj.mkdir()
    (proj / "SCOPE.md").write_text("# Scope\n\n- code: %s\n" % PROJECT, encoding="utf-8")
    (proj / "server.json").write_text(json.dumps({"server": {"admin_pass": "{{secret:ecs-admin}}"}}),
                                      encoding="utf-8")
    monkeypatch.chdir(proj)
    assert cli.main(["cloud", "call", "POST", "ecs", "/v1/{project_id}/cloudservers", "--tenant", "test-1",
                     "--role", "lab", "--body", "server.json"]) == 0
    out = capsys.readouterr().out
    assert ADMIN_PW not in out and keys.MASK in out


def test_the_owner_commands_are_refused_to_the_work_user(monkeypatch, capsys):
    monkeypatch.setattr(config, "is_work_user", lambda: True)
    for cmd in ("unlock", "lock", "serve"):
        assert cli.main(["keys", cmd]) == 2


def test_no_service_is_a_plain_error(monkeypatch, capsys):
    monkeypatch.setenv(keys.CALL_SOCKET_ENV, "/tmp/awb-no-such-dir/cloud.sock")
    assert cli.main(["keys", "status"]) == 2
    assert "the key service is not running" in capsys.readouterr().err


def test_a_session_reaches_a_tenant_only_from_a_project_of_the_kind_project(svc, gw):
    from types import SimpleNamespace

    load(svc)
    rows = {"tcp-ab2c": SimpleNamespace(code="tcp-ab2c", kind="project"),
            "tcp-qu3r": SimpleNamespace(code="tcp-qu3r", kind="query"),
            "tcp-1ab4": SimpleNamespace(code="tcp-1ab4", kind="lab")}
    svc._project_row = lambda code: rows.get(code)
    session = __import__("os").getuid() + 1          # any uid but the service's own is a session
    req = {"tenant": "test-1", "method": "GET", "service": "vpc", "path": "/v1/{project_id}/vpcs"}
    with pytest.raises(keys.Refused, match="is a query"):
        svc._call(dict(req, project="tcp-qu3r"), session)
    with pytest.raises(keys.Refused, match="folder of an active project"):
        svc._call(dict(req), session)
    assert svc._call(dict(req, project="tcp-ab2c"), session)["status"] == 200
    assert svc._call(dict(req, project="tcp-1ab4"), session)["status"] == 200      # a lab of before counts
    assert svc._call(dict(req), __import__("os").getuid())["status"] == 200       # the owner needs no project
