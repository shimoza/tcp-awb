"""Task RT: read-only tenants of a customer or partner (awb/tcp/readonly.py, the key service of awb/tcp/keys.py).

A stand-in IAM and API on 127.0.0.1 hands out a token for a password login and checks it on every call; the
stand-in gateway of tests/tcp_fake.py checks the signature of a read key. Every login, password, key, token and
domain id here is invented.
"""
from __future__ import annotations

import json
import os
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace

import pytest

from awb import config
from awb.tcp import keys, readonly, tenants, xchg
from tests.tcp_fake import FakeGateway
from tests.test_keys import (AK, SK, LAB_AK, LAB_SK, PROJECT, calls_logged_here, no_waiting,  # noqa: F401
                             short_dir)

ALIAS = "cust-1"
KEY_ALIAS = "cust-2"
DOMAIN_ID = "d" * 32
PROJECT_ID = "e" * 32
LOGIN_USER = "reader-of-" + "fixture"
LOGIN_DOMAIN = "account-" + "fixture"
LOGIN_PW = "-".join(("Invented", "Login", "0815"))        # built from pieces: an invented value
TOKEN_PREFIX = "tok" + "-invented-"
STORE = {
    "%s/login/domain" % ALIAS: LOGIN_DOMAIN, "%s/login/user" % ALIAS: LOGIN_USER,
    "%s/login/password" % ALIAS: LOGIN_PW,
    "%s/ak" % KEY_ALIAS: AK, "%s/sk" % KEY_ALIAS: SK,
    "test-1/ak": "AKTEST", "test-1/sk": "sk-test-secret", "test-1/lab/ak": LAB_AK, "test-1/lab/sk": LAB_SK,
}
SETTINGS = {"readonly": "%s:login:%s:eu-de, %s:key:%s:eu-de" % (ALIAS, DOMAIN_ID, KEY_ALIAS, DOMAIN_ID)}
WRITES = ("POST", "PUT", "PATCH", "DELETE")
OTHER = "tcp-zz99"


class FakeIamApi:
    """IAM and one API on one address: POST /v3/auth/tokens gives a token for the invented login, every other path
    answers 200 only to the newest token (401 otherwise). `mode`: None, "mfa", "console", "wrong"; `life` the
    seconds a token lives; `echo` puts the token into the answer, the way a careless API might."""

    def __init__(self):
        self.logins = 0
        self.calls: list[tuple[str, str, str]] = []
        self.mode: str | None = None
        self.life = 86400
        self.echo = False
        self.current = ""
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _send(self, status, body, headers=None):
                raw = json.dumps(body).encode()
                self.send_response(status)
                for k, v in (headers or {}).items():
                    self.send_header(k, v)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def _any(self, method):
                path = self.path.partition("?")[0]
                length = int(self.headers.get("Content-Length") or 0)
                body = self.rfile.read(length) if length else b""
                if method == "POST" and path == "/v3/auth/tokens":
                    return self._login(body)
                fake.calls.append((method, path, self.headers.get("X-Auth-Token") or ""))
                if not fake.current or self.headers.get("X-Auth-Token") != fake.current:
                    return self._send(401, {"error": {"code": "IAM.0067", "message": "Invalid token."}})
                data = {"servers": [{"id": "s-1", "name": "web-1"}]}
                if fake.echo:
                    data["seen"] = fake.current
                return self._send(200, data)

            def _login(self, body):
                fake.logins += 1
                req = json.loads(body)
                user = req["auth"]["identity"]["password"]["user"]
                scope = req["auth"]["scope"]["project"]
                if fake.mode == "mfa":
                    return self._send(401, {"error": {"code": "IAM.0001", "message":
                                                      "The user has enabled login protection: MFA verification "
                                                      "required"}})
                if fake.mode == "console":
                    return self._send(403, {"error": {"code": "IAM.0081", "message": "This user only supports "
                                                      "console access, not programmatic access."}})
                if (fake.mode == "wrong" or user["password"] != LOGIN_PW or user["name"] != LOGIN_USER
                        or user["domain"]["name"] != LOGIN_DOMAIN):
                    return self._send(401, {"error": {"code": "IAM.0062", "message": "Incorrect password."}})
                fake.current = "%s%04d" % (TOKEN_PREFIX, fake.logins)
                import datetime

                expires = (datetime.datetime.now(datetime.timezone.utc)
                           + datetime.timedelta(seconds=fake.life)).strftime("%Y-%m-%dT%H:%M:%S.000000Z")
                tok = {"expires_at": expires, "methods": ["password"],
                       "project": {"id": PROJECT_ID, "name": scope["name"], "domain": {"id": DOMAIN_ID}},
                       "user": {"name": LOGIN_USER}}
                return self._send(201, {"token": tok}, {"X-Subject-Token": fake.current})

            def do_GET(self):
                self._any("GET")

            def do_HEAD(self):
                self._any("HEAD")

            def do_POST(self):
                self._any("POST")

            def do_PUT(self):
                self._any("PUT")

            def do_DELETE(self):
                self._any("DELETE")

            def do_PATCH(self):
                self._any("PATCH")

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *exc):
        self.server.shutdown()
        self.server.server_close()

    @property
    def base(self) -> str:
        return "http://127.0.0.1:%d" % self.server.server_address[1]


def _service(base: str, log_dir: Path, vault: Path):
    s = keys.Service(Path("/nonexistent/a"), Path("/nonexistent/c"), endpoint=base, log_dir=log_dir,
                     paths_fn=lambda: SimpleNamespace(vault=vault))
    s._registered = lambda: {"test-1": ("eu-de",)}
    s._active_project = lambda code: code in (PROJECT, OTHER)
    return s


@pytest.fixture
def iam():
    with FakeIamApi() as f:
        yield f


@pytest.fixture
def svc(iam, tmp_path):
    """A key service on its sockets with the invented store and the readonly line loaded."""
    with short_dir() as d:
        s = _service(iam.base, tmp_path / "log", tmp_path / "vault")
        s.admin_path, s.call_path = d / "admin.sock", d / "call.sock"
        s.start()
        try:
            keys.unlock(s.admin_path, sorted(STORE), reader=lambda e: STORE[e], settings=dict(SETTINGS))
            yield s
        finally:
            s.stop()


def get(svc, alias=ALIAS, **req):
    req = dict({"op": "call", "tenant": alias, "method": "GET", "service": "ecs",
                "path": "/v1/{project_id}/cloudservers"}, **req)
    return keys.request(svc.call_path, req)


@pytest.fixture
def session(svc, monkeypatch):
    """The service as a session of the work user sees it: two granted projects, the read-only tenant granted to
    PROJECT only."""
    rows = {c: SimpleNamespace(code=c, kind="project", state="active", customer=None) for c in (PROJECT, OTHER)}
    svc._project_row = lambda code: rows.get(code)
    svc._granted = lambda code: code in rows
    svc._console_uid = lambda: None
    monkeypatch.setattr(config, "is_work_user", lambda: False)
    readonly.grant(svc.paths_fn(), ALIAS, PROJECT)
    readonly.grant(svc.paths_fn(), KEY_ALIAS, PROJECT)
    return svc, os.getuid() + 1


# --------------------------------------------------------------------------- the line of keys.conf


def test_rt_the_readonly_line_parses_and_a_broken_one_is_refused(tmp_path):
    entries = readonly.parse(SETTINGS["readonly"])
    assert entries == {ALIAS: {"auth": "login", "domain_id": DOMAIN_ID, "regions": ("eu-de",)},
                       KEY_ALIAS: {"auth": "key", "domain_id": DOMAIN_ID, "regions": ("eu-de",)}}
    assert readonly.parse("%s:key:%s:eu-de+eu-nl" % (ALIAS, DOMAIN_ID))[ALIAS]["regions"] == ("eu-de", "eu-nl")
    for bad in ("%s:lab:%s:eu-de" % (ALIAS, DOMAIN_ID), "%s:key:short:eu-de" % ALIAS, "%s:key:%s" % (ALIAS, DOMAIN_ID),
                "Cust:key:%s:eu-de" % DOMAIN_ID, "%s:key:%s:eu-de, %s:login:%s:eu-de" % (ALIAS, DOMAIN_ID, ALIAS,
                                                                                         DOMAIN_ID)):
        assert readonly.parse(bad) is None, bad
    conf = tmp_path / "keys.conf"
    conf.write_text("readonly = %s:write:%s:eu-de\n" % (ALIAS, DOMAIN_ID))
    with pytest.raises(xchg.XchgError, match="line 1 of keys.conf: readonly is") as err:
        xchg.read_settings(conf)
    assert DOMAIN_ID not in str(err.value)


def test_rt_the_store_layout_takes_a_login_and_refuses_half_of_one():
    held = keys.collect(sorted(STORE), reader=lambda e: STORE[e])
    assert held[ALIAS]["login"] == {"domain": LOGIN_DOMAIN, "user": LOGIN_USER, "password": LOGIN_PW}
    with pytest.raises(keys.KeysError, match="the login of %s lacks" % ALIAS) as err:
        keys.collect(["%s/login/user" % ALIAS], reader=lambda e: STORE[e])
    assert LOGIN_USER not in str(err.value)


# --------------------------------------------------------------------------- the token flow


def test_rt_a_login_gets_a_token_once_and_signs_with_it(svc, iam):
    a = get(svc)
    assert a["ok"] and a["status"] == 200, a
    assert iam.logins == 1 and iam.calls[-1][:2] == ("GET", "/v1/%s/cloudservers" % PROJECT_ID)
    assert iam.calls[-1][2].startswith(TOKEN_PREFIX)                      # X-Auth-Token, no signature
    assert get(svc, method="HEAD")["status"] == 200
    assert iam.logins == 1                                                # the token in memory is used again


def test_rt_a_token_close_to_its_expiry_is_renewed(svc, iam):
    iam.life = int(readonly.RENEW_MARGIN) - 60                            # lives less than the margin
    assert get(svc)["status"] == 200 and get(svc)["status"] == 200
    assert iam.logins == 2
    iam.life = 86400
    assert get(svc)["status"] == 200 and get(svc)["status"] == 200
    assert iam.logins == 3


def test_rt_the_token_cache_renews_on_its_clock_and_forgets_on_lock():
    now = [1000.0]
    got = []

    def fetch(login, domain_id, region, endpoint=None):
        got.append(region)
        return "tok-%d" % len(got), PROJECT_ID, 3600.0

    t = readonly.Tokens(fetch=fetch, clock=lambda: now[0])
    assert t.get(ALIAS, "eu-de", {}, DOMAIN_ID) == ("tok-1", PROJECT_ID)
    now[0] += 3600 - readonly.RENEW_MARGIN - 1
    assert t.get(ALIAS, "eu-de", {}, DOMAIN_ID)[0] == "tok-1"
    now[0] += 2
    assert t.get(ALIAS, "eu-de", {}, DOMAIN_ID)[0] == "tok-2"
    t.clear()
    assert t.values() == []


def test_rt_a_token_iam_took_back_is_replaced_by_one_new_login(svc, iam):
    assert get(svc)["status"] == 200
    iam.current = "revoked"                                               # a change of the user: IAM takes it back
    a = get(svc)
    assert a["status"] == 200 and iam.logins == 2


def test_rt_a_lock_forgets_the_tokens(svc, iam):
    assert get(svc)["status"] == 200 and svc.tokens.values()
    keys.request(svc.admin_path, {"op": "lock"})
    assert svc.tokens.values() == []


def test_rt_mfa_is_refused_with_one_line_and_no_code_is_asked(svc, iam):
    iam.mode = "mfa"
    a = get(svc)
    assert a == {"ok": False, "error": readonly.MFA_LINE}
    assert "passcode" not in json.dumps(a) and iam.calls == []


def test_rt_a_console_only_login_and_a_wrong_password_say_so_without_a_value(svc, iam):
    iam.mode = "console"
    assert get(svc)["error"] == readonly.CONSOLE_ONLY_LINE
    iam.mode = "wrong"
    a = get(svc)
    assert a == {"ok": False, "error": "the login was refused by IAM (HTTP 401 IAM.0062)"}
    for value in (LOGIN_PW, LOGIN_USER, LOGIN_DOMAIN):
        assert value not in json.dumps(a)


def test_rt_obs_needs_a_key_on_a_login_only_tenant(svc, iam):
    a = get(svc, service="obs", path="/")
    assert a == {"ok": False, "error": readonly.OBS_LINE % ALIAS} and iam.logins == 0


# --------------------------------------------------------------------------- GET and HEAD only


def test_rt_every_other_method_is_refused_for_every_role_and_caller(session, iam):
    svc, uid = session
    for alias in (ALIAS, KEY_ALIAS):
        for method in WRITES:
            for role in ("read", "lab"):
                req = {"tenant": alias, "role": role, "method": method, "service": "ecs",
                       "path": "/v1/{project_id}/cloudservers", "project": PROJECT}
                for who, caller in ((os.getuid(), keys._NOT_ASKED), (uid, PROJECT)):
                    with pytest.raises(keys.Refused, match="^%s$" % (readonly.METHOD_LINE % alias)):
                        svc._call(dict(req), who, caller)
        assert get(svc, alias, method="POST", role="lab", project=PROJECT)["error"] == readonly.METHOD_LINE % alias
    assert iam.logins == 0 and iam.calls == []


def test_rt_no_lab_role_no_body_no_lease_no_lab_key_no_secret(session, iam):
    svc, uid = session
    assert get(svc, role="lab")["error"] == readonly.ROLE_LINE % ALIAS
    assert get(svc, body={"password": "{{secret:x}}"})["error"] == readonly.BODY_LINE % ALIAS
    for alias in (ALIAS, KEY_ALIAS):
        with pytest.raises(keys.Refused, match=readonly.LEASE_LINE % alias):
            svc._lease({"tenant": alias, "minutes": 15, "project": PROJECT}, os.getuid())
        assert svc._lab_keys(alias) is None
    # what the store holds beside the read key is never loaded for a read-only tenant
    store = dict(STORE, **{"%s/lab/ak" % KEY_ALIAS: "AKLABX", "%s/lab/sk" % KEY_ALIAS: "sk-lab-x",
                           "%s/secret/root" % KEY_ALIAS: "invented-root"})
    keys.unlock(svc.admin_path, sorted(store), reader=lambda e: store[e], settings=dict(SETTINGS))
    assert svc.tenants[KEY_ALIAS]["roles"].keys() == {"read"} and svc.tenants[KEY_ALIAS]["secrets"] == {}
    assert iam.calls == []


def test_rt_a_read_key_tenant_reads_signed_and_refuses_writes():
    with FakeGateway(AK, SK) as gw:
        with short_dir() as d:
            s = _service(gw.base, d / "log", d / "vault")
            s.admin_path, s.call_path = d / "admin.sock", d / "call.sock"
            s.start()
            try:
                keys.unlock(s.admin_path, sorted(STORE), reader=lambda e: STORE[e], settings=dict(SETTINGS))
                a = get(s, KEY_ALIAS, service="iam", path="/v3/projects")
                assert a["ok"] and a["status"] == 200, a
                assert get(s, KEY_ALIAS, method="DELETE", path="/v3/projects")["error"] == \
                    readonly.METHOD_LINE % KEY_ALIAS
            finally:
                s.stop()


# --------------------------------------------------------------------------- the grant of a project


def test_rt_a_project_without_the_grant_of_the_tenant_is_refused(session, iam):
    svc, uid = session
    req = {"tenant": ALIAS, "method": "GET", "service": "ecs", "path": "/v1/{project_id}/cloudservers"}
    with pytest.raises(keys.Refused, match="^%s$" % (readonly.GRANT_LINE % (OTHER, ALIAS, ALIAS, OTHER))):
        svc._call(dict(req, project=OTHER), uid, OTHER)                  # a granted project, not this tenant
    with pytest.raises(keys.Refused, match="runs from the folder of an active project"):
        svc._call(dict(req, project=PROJECT), uid, OTHER)                # naming the granted one from another
    with pytest.raises(keys.Refused, match="holds no key token"):
        svc._call(dict(req, project=PROJECT), uid, None)                 # no key token
    svc._granted = lambda code: False
    with pytest.raises(keys.Refused, match="has no grant of the owner"):
        svc._call(dict(req, project=PROJECT), uid, PROJECT)
    assert iam.logins == 0
    svc._granted = lambda code: True
    a = svc._call(dict(req, project=PROJECT), uid, PROJECT)
    assert a["ok"] and a["status"] == 200


def test_rt_the_console_never_reads_a_readonly_tenant(session, iam):
    svc, uid = session
    svc._console_uid = lambda: uid
    with pytest.raises(keys.Refused, match=readonly.CONSOLE_LINE % ALIAS):
        svc._call({"tenant": ALIAS, "method": "GET", "service": "ecs", "path": "/v1/x"}, uid)
    assert iam.logins == 0


def test_rt_a_login_that_is_not_registered_is_never_used(svc, iam):
    keys.unlock(svc.admin_path, sorted(STORE), reader=lambda e: STORE[e], settings={})
    assert get(svc)["error"] == readonly.UNREGISTERED_LINE % ALIAS
    assert iam.logins == 0


def test_rt_grants_are_the_owners_and_revoke_takes_them(tmp_path, monkeypatch):
    p = SimpleNamespace(vault=tmp_path / "vault")
    monkeypatch.setattr(config, "is_work_user", lambda: False)
    assert readonly.grant(p, ALIAS, PROJECT) is True and readonly.grant(p, ALIAS, PROJECT) is False
    assert readonly.granted(p, ALIAS, PROJECT) and not readonly.granted(p, ALIAS, OTHER)
    assert (p.vault / readonly.GRANTS_NAME).stat().st_mode & 0o777 == 0o600
    assert readonly.revoke(p, ALIAS, PROJECT) == 1 and not readonly.granted(p, ALIAS, PROJECT)
    monkeypatch.setattr(config, "is_work_user", lambda: True)
    with pytest.raises(readonly.ReadonlyError, match="the owner's"):
        readonly.grant(p, ALIAS, PROJECT)


# --------------------------------------------------------------------------- never a value


def test_rt_the_token_and_the_password_are_in_no_log_line_and_no_answer(svc, iam, tmp_path, capsys,
                                                                       calls_logged_here):  # noqa: F811
    iam.echo = True
    a = get(svc)
    assert a["status"] == 200 and a["data"]["seen"] == keys.MASK
    iam.mode = "wrong"
    svc.tokens.clear()
    get(svc)
    issued = ["%s%04d" % (TOKEN_PREFIX, n) for n in range(1, iam.logins + 1)]
    logs = "".join(f.read_text() for f in (tmp_path / "log").glob("*.tsv"))
    assert "\tLOGIN\tiam\t" in logs and "\tGET\tecs\t" in logs
    keys._print_summary(keys.request(svc.call_path, {"op": "tenants"})["tenants"])
    out = capsys.readouterr().out
    for text in (logs, json.dumps(a), out, repr(calls_logged_here)):
        for value in issued + [LOGIN_PW, LOGIN_USER, LOGIN_DOMAIN, TOKEN_PREFIX, SK]:
            assert value not in text, value


def test_rt_the_listing_shows_the_kind_and_the_way_in_never_a_value(svc, capsys):
    t = keys.request(svc.call_path, {"op": "tenants"})["tenants"]
    assert t[ALIAS] == {"roles": [], "secrets": [], "kind": "readonly", "auth": "login", "regions": ["eu-de"]}
    assert t[KEY_ALIAS] == {"roles": ["read"], "secrets": [], "kind": "readonly", "auth": "key",
                            "regions": ["eu-de"]}
    assert "kind" not in t["test-1"]
    keys._print_summary(t)
    assert "kind: readonly (login)" in capsys.readouterr().out


# --------------------------------------------------------------------------- the owner's commands


class FakeStore:
    def __init__(self, names, root: Path):
        self.names = set(names)
        self.root = root

    def has(self, entry):
        return entry in self.names


def _names(*parts):
    return {"awb/tenant/%s" % p for p in parts}


def test_rt_add_readonly_checks_the_entries_by_name_and_writes_keys_conf(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "is_work_user", lambda: False)
    conf = tmp_path / "keys.conf"
    conf.write_text("bucket_tenant = test-1\nmax_mb = 50\n")
    store = FakeStore(_names("%s/login/domain" % ALIAS, "%s/login/user" % ALIAS), tmp_path / "store")
    with pytest.raises(readonly.ReadonlyError, match="login/password is missing: pass insert it first"):
        readonly.add(ALIAS, DOMAIN_ID, "login", [], store=store, settings_path=conf)
    store.names |= _names("%s/login/password" % ALIAS)
    with pytest.raises(readonly.ReadonlyError, match="ak and .*sk are missing"):
        readonly.add(ALIAS, DOMAIN_ID, "key", [], store=store, settings_path=conf)
    with pytest.raises(readonly.ReadonlyError, match="test tenant"):
        readonly.add(ALIAS, DOMAIN_ID, None, [], store=store, registered={ALIAS}, settings_path=conf)
    with pytest.raises(readonly.ReadonlyError, match="exchange buckets"):
        readonly.add("test-1", DOMAIN_ID, None, [], store=FakeStore(_names("test-1/ak", "test-1/sk"), tmp_path),
                     settings_path=conf)
    lines = readonly.add(ALIAS, DOMAIN_ID, None, [], store=store, settings_path=conf)
    assert lines == ["tenant %s: read-only, login, eu-de: registered in keys.conf" % ALIAS]
    settings = xchg.read_settings(conf)
    assert settings["max_mb"] == "50" and readonly.of_settings(settings)[ALIAS]["auth"] == "login"
    assert conf.stat().st_mode & 0o777 == 0o600
    store.names |= _names("%s/lab/ak" % ALIAS)
    with pytest.raises(readonly.ReadonlyError, match="no lab key and no secret"):
        readonly.add(ALIAS, DOMAIN_ID, "login", [], store=store, settings_path=conf)


def test_rt_the_tenant_commands_add_grant_and_remove(home, tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(config, "is_work_user", lambda: False)
    conf = tmp_path / "keys.conf"
    monkeypatch.setenv("AWB_KEYS_CONF", str(conf))
    monkeypatch.setenv("PASSWORD_STORE_DIR", str(tmp_path / "store"))
    for name in ("domain", "user", "password"):
        f = tmp_path / "store" / "awb" / "tenant" / KEY_ALIAS / "login" / (name + ".gpg")
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text("")
    unlocked = []
    monkeypatch.setattr(keys, "unlock", lambda: unlocked.append(1) or {
        KEY_ALIAS: {"roles": [], "secrets": [], "kind": "readonly", "auth": "login"}})
    assert tenants.main(["add-readonly", KEY_ALIAS, "--domain-id", DOMAIN_ID, "--login"]) == 0
    out = capsys.readouterr().out
    assert "key service: unlocked, %s carries login (read-only)" % KEY_ALIAS in out and unlocked
    assert DOMAIN_ID in conf.read_text() and DOMAIN_ID not in out
    assert tenants.main(["grant", KEY_ALIAS, PROJECT]) == 0
    assert readonly.granted(home, KEY_ALIAS, PROJECT)
    assert tenants.main(["grant", "test-9", PROJECT]) == 1
    assert "not a read-only tenant" in capsys.readouterr().err
    assert tenants.main(["revoke", KEY_ALIAS, PROJECT]) == 0 and not readonly.granted(home, KEY_ALIAS, PROJECT)
    tenants.main(["grant", KEY_ALIAS, PROJECT])
    assert tenants.main(["remove", KEY_ALIAS]) == 0
    out = capsys.readouterr().out
    assert "removed from keys.conf, 1 grant(s) taken" in out and "pass rm -r awb/tenant/%s" % KEY_ALIAS in out
    assert KEY_ALIAS not in conf.read_text() and not readonly.granted(home, KEY_ALIAS, PROJECT)
    assert tenants.main(["remove", KEY_ALIAS]) == 1
    monkeypatch.setattr(config, "is_work_user", lambda: True)
    assert tenants.main(["add-readonly", KEY_ALIAS, "--domain-id", DOMAIN_ID]) == 1
    assert "the owner's" in capsys.readouterr().err
