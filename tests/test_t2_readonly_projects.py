"""Task T2: a read-only tenant reads every project of its domain the login can see (awb/tcp/readonly.py, the key
service of awb/tcp/keys.py, awb tenant regions|projects, awb cloud call --scope).

A stand-in IAM and API on 127.0.0.1 hands out one token per project scope and answers a call only when the token is
scoped to the project of its path; GET /v3/auth/projects lists the projects the login sees: the region projects
eu-de and eu-nl, a sub-project, a project of another domain and one of a region the tenant does not read. Every
login, password, key, token and id here is invented.
"""
from __future__ import annotations

import json
import os
import threading
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace

import pytest

from awb import config
from awb.tcp import cloud, keys, readonly, tenants, xchg
from tests.tcp_fake import FakeGateway
from tests.test_keys import AK, SK, PROJECT, calls_logged_here, no_waiting, short_dir  # noqa: F401

ALIAS = "cust-1"
KEY_ALIAS = "cust-2"
OLD_ALIAS = "cust-3"
DOMAIN_ID = "d" * 32
OTHER_DOMAIN = "c" * 32
P_DE, P_NL, P_SUB, P_FOREIGN, P_CH = "e" * 32, "f" * 32, "a" * 32, "b" * 32, "9" * 32
SUB = "eu-de_" + "lab"
FOREIGN = "eu-de_" + "other"
LOGIN_USER = "reader-of-" + "fixture"
LOGIN_DOMAIN = "account-" + "fixture"
LOGIN_PW = "-".join(("Invented", "Login", "4242"))        # built from pieces: an invented value
TOKEN_PREFIX = "tok" + "-invented-"
PROJECTS = [{"id": P_DE, "name": "eu-de", "domain_id": DOMAIN_ID},
            {"id": P_NL, "name": "eu-nl", "domain_id": DOMAIN_ID},
            {"id": P_SUB, "name": SUB, "domain_id": DOMAIN_ID},
            {"id": P_FOREIGN, "name": FOREIGN, "domain_id": OTHER_DOMAIN},
            {"id": P_CH, "name": "eu-ch2", "domain_id": DOMAIN_ID}]
STORE = {"%s/login/domain" % a: LOGIN_DOMAIN for a in (ALIAS, OLD_ALIAS)}
STORE.update({"%s/login/user" % a: LOGIN_USER for a in (ALIAS, OLD_ALIAS)})
STORE.update({"%s/login/password" % a: LOGIN_PW for a in (ALIAS, OLD_ALIAS)})
STORE.update({"%s/ak" % KEY_ALIAS: AK, "%s/sk" % KEY_ALIAS: SK})
SETTINGS = {"readonly": "%s:login:%s:eu-de+eu-nl, %s:key:%s:eu-de+eu-nl, %s:login:%s:eu-de"
            % (ALIAS, DOMAIN_ID, KEY_ALIAS, DOMAIN_ID, OLD_ALIAS, DOMAIN_ID)}
OTHER = "tcp-zz99"


class FakeScopedIam:
    """IAM and one API on one address. POST /v3/auth/tokens scopes a token to the project of its name (with the
    domain) or of its id; GET /v3/auth/projects lists PROJECTS; GET /v1/<id>/cloudservers answers 200 only to a
    token scoped to <id>. `scopes` keeps what each login asked for."""

    def __init__(self):
        self.logins = 0
        self.scopes: list[dict] = []
        self.tokens: dict[str, str] = {}
        self.calls: list[tuple[str, str, str]] = []
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
                    return self._login(json.loads(body))
                token = self.headers.get("X-Auth-Token") or ""
                fake.calls.append((method, path, token))
                if token not in fake.tokens:
                    return self._send(401, {"error": {"code": "IAM.0067", "message": "Invalid token."}})
                if path == readonly.PROJECTS_PATH:
                    return self._send(200, {"projects": PROJECTS})
                parts = path.strip("/").split("/")
                if len(parts) == 3 and parts[0] == "v1" and parts[2] == "cloudservers":
                    if parts[1] != fake.tokens[token]:
                        return self._send(403, {"error_code": "Ecs.0001", "error_msg": "other project"})
                    return self._send(200, {"servers": [{"id": "s-" + parts[1][:2]}]})
                return self._send(404, {"error_msg": "no such path"})

            def _login(self, req):
                fake.logins += 1
                user = req["auth"]["identity"]["password"]["user"]
                scope = req["auth"]["scope"]["project"]
                fake.scopes.append(scope)
                if user["password"] != LOGIN_PW or user["name"] != LOGIN_USER:
                    return self._send(401, {"error": {"code": "IAM.0062", "message": "Incorrect password."}})
                if "id" in scope:
                    found = [p for p in PROJECTS if p["id"] == scope["id"]]
                else:
                    found = [p for p in PROJECTS if p["name"] == scope["name"]
                             and p["domain_id"] == scope["domain"]["id"]]
                if not found:
                    return self._send(401, {"error": {"code": "IAM.0002", "message": "No project."}})
                token = "%s%04d" % (TOKEN_PREFIX, fake.logins)
                fake.tokens[token] = found[0]["id"]
                tok = {"expires_at": "2099-01-01T00:00:00.000000Z", "methods": ["password"],
                       "project": {"id": found[0]["id"], "name": found[0]["name"],
                                   "domain": {"id": found[0]["domain_id"]}}}
                return self._send(201, {"token": tok}, {"X-Subject-Token": token})

            def do_GET(self):
                self._any("GET")

            def do_HEAD(self):
                self._any("HEAD")

            def do_POST(self):
                self._any("POST")

            def do_DELETE(self):
                self._any("DELETE")

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


def _service(base, log_dir, vault):
    s = keys.Service(keys.Path("/nonexistent/a"), keys.Path("/nonexistent/c"), endpoint=base, log_dir=log_dir,
                     paths_fn=lambda: SimpleNamespace(vault=vault))
    s._registered = lambda: {}
    s._active_project = lambda code: code in (PROJECT, OTHER)
    return s


@pytest.fixture
def iam():
    with FakeScopedIam() as f:
        yield f


@pytest.fixture
def svc(iam, tmp_path):
    with short_dir() as d:
        s = _service(iam.base, tmp_path / "log", tmp_path / "vault")
        s.admin_path, s.call_path = d / "admin.sock", d / "call.sock"
        s.start()
        try:
            keys.unlock(s.admin_path, sorted(STORE), reader=lambda e: STORE[e], settings=dict(SETTINGS))
            yield s
        finally:
            s.stop()


@pytest.fixture
def session(svc, monkeypatch):
    """The service as a session sees it: two granted projects, the read-only tenant granted to PROJECT only."""
    rows = {c: SimpleNamespace(code=c, kind="project", state="active", customer=None) for c in (PROJECT, OTHER)}
    svc._project_row = lambda code: rows.get(code)
    svc._granted = lambda code: code in rows
    svc._console_uid = lambda: None
    monkeypatch.setattr(config, "is_work_user", lambda: False)
    readonly.grant(svc.paths_fn(), ALIAS, PROJECT)
    return svc, os.getuid() + 1


def get(svc, alias=ALIAS, **req):
    req = dict({"op": "call", "tenant": alias, "method": "GET", "service": "ecs",
                "path": "/v1/{project_id}/cloudservers"}, **req)
    return keys.request(svc.call_path, req)


def _log_regions(tmp_path) -> list[str]:
    rows = [ln.split("\t") for f in (tmp_path / "log").glob("cloud-*.tsv") for ln in f.read_text().splitlines()]
    return [r[6] for r in rows if r[4] == "GET" and r[5] == "ecs"]


# --------------------------------------------------------------------------- regions


def test_t2_an_old_one_region_line_reads_and_add_readonly_writes_both_regions(tmp_path, monkeypatch, capsys):
    assert readonly.parse("%s:login:%s:eu-de" % (ALIAS, DOMAIN_ID))[ALIAS]["regions"] == ("eu-de",)
    monkeypatch.setattr(config, "is_work_user", lambda: False)
    conf = tmp_path / "keys.conf"
    monkeypatch.setenv("AWB_KEYS_CONF", str(conf))
    monkeypatch.setenv("PASSWORD_STORE_DIR", str(tmp_path / "store"))
    for alias in (ALIAS, KEY_ALIAS):
        for name in ("ak", "sk"):
            f = tmp_path / "store" / "awb" / "tenant" / alias / (name + ".gpg")
            f.parent.mkdir(parents=True, exist_ok=True)
            f.write_text("")
    monkeypatch.setattr(keys, "unlock", lambda: {})
    monkeypatch.setattr(tenants, "load", lambda p: [])
    assert tenants.main(["add-readonly", ALIAS, "--domain-id", DOMAIN_ID, "--key"]) == 0
    assert tenants.main(["add-readonly", KEY_ALIAS, "--domain-id", DOMAIN_ID, "--key", "--region", "eu-de"]) == 0
    held = readonly.of_settings(xchg.read_settings(conf))
    assert held[ALIAS]["regions"] == readonly.DEFAULT_REGIONS == ("eu-de", "eu-nl")
    assert held[KEY_ALIAS]["regions"] == ("eu-de",)
    assert "read-only, key, eu-de+eu-nl: registered" in capsys.readouterr().out


def test_t2_regions_changes_a_tenant_without_its_login(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(config, "is_work_user", lambda: False)
    conf = tmp_path / "keys.conf"
    conf.write_text("max_mb = 50\nreadonly = %s:login:%s:eu-de\n" % (ALIAS, DOMAIN_ID))
    monkeypatch.setenv("AWB_KEYS_CONF", str(conf))
    unlocked = []
    monkeypatch.setattr(keys, "unlock", lambda: unlocked.append(1) or {
        ALIAS: {"roles": [], "secrets": [], "kind": "readonly", "auth": "login"}})
    asked = []
    monkeypatch.setattr(readonly, "ask_login", lambda *a, **k: asked.append(1) or [])
    assert tenants.main(["regions", ALIAS, "--region", "eu-de", "--region", "eu-nl"]) == 0
    out = capsys.readouterr().out
    assert "tenant %s: regions eu-de+eu-nl, were eu-de: changed in keys.conf" % ALIAS in out
    assert "key service: unlocked" in out and unlocked == [1] and asked == []
    settings = xchg.read_settings(conf)
    assert settings["max_mb"] == "50"
    assert readonly.of_settings(settings)[ALIAS] == {"auth": "login", "domain_id": DOMAIN_ID,
                                                     "regions": ("eu-de", "eu-nl")}
    assert DOMAIN_ID not in out and conf.stat().st_mode & 0o777 == 0o600
    assert tenants.main(["regions", ALIAS, "--region", "eu-de", "--region", "eu-nl"]) == 0
    assert "already" in capsys.readouterr().out and unlocked == [1]
    assert tenants.main(["regions", "cust-9", "--region", "eu-de"]) == 1
    assert "not a read-only tenant" in capsys.readouterr().err
    assert tenants.main(["regions", ALIAS, "--region", "Frankfurt"]) == 1
    monkeypatch.setattr(config, "is_work_user", lambda: True)
    assert tenants.main(["regions", ALIAS, "--region", "eu-de"]) == 1
    assert "the owner's" in capsys.readouterr().err


# --------------------------------------------------------------------------- projects


def test_t2_a_login_reads_both_regions_and_a_sub_project_with_a_token_each(svc, iam, tmp_path):
    for scope, pid in (("eu-de", P_DE), ("eu-nl", P_NL), (SUB, P_SUB), (P_SUB, P_SUB)):
        a = get(svc, scope=scope)
        assert a["ok"] and a["status"] == 200, (scope, a)
        assert a["data"]["servers"] == [{"id": "s-" + pid[:2]}]
        assert iam.calls[-1][:2] == ("GET", "/v1/%s/cloudservers" % pid)
    # one listing token (the first region by name), then one token per project by id, held and used again
    assert iam.scopes[0] == {"name": "eu-de", "domain": {"id": DOMAIN_ID}}
    assert iam.scopes[1:] == [{"id": P_DE}, {"id": P_NL}, {"id": P_SUB}]
    assert [c[1] for c in iam.calls].count(readonly.PROJECTS_PATH) == 1
    assert _log_regions(tmp_path) == ["eu-de", "eu-nl", "eu-de", "eu-de"]
    # no scope: the region project by name as before, in the region the call names
    assert get(svc, region="eu-nl")["status"] == 200
    assert iam.calls[-1][1] == "/v1/%s/cloudservers" % P_NL


def test_t2_a_project_of_another_domain_is_refused(svc, iam):
    for scope in (FOREIGN, P_FOREIGN):
        assert get(svc, scope=scope) == {"ok": False, "error": readonly.OTHER_DOMAIN_LINE % (scope, ALIAS)}
    assert get(svc, scope="eu-de_" + "unknown")["error"] == readonly.UNKNOWN_PROJECT_LINE % (
        "eu-de_" + "unknown", ALIAS, ALIAS)
    assert {"id": P_FOREIGN} not in iam.scopes
    assert all(not c[1].startswith("/v1/") for c in iam.calls)


def test_t2_a_region_not_configured_is_refused(svc, iam):
    assert get(svc, scope="eu-ch2")["error"] == readonly.REGION_LINE % ("eu-ch2", "eu-ch2", ALIAS, "eu-de+eu-nl",
                                                                         ALIAS)
    # the old line of one region: eu-nl is not one of its regions, by name, by scope or by region
    assert get(svc, OLD_ALIAS, scope="eu-nl")["error"] == readonly.REGION_LINE % ("eu-nl", "eu-nl", OLD_ALIAS,
                                                                                   "eu-de", OLD_ALIAS)
    assert get(svc, OLD_ALIAS, region="eu-nl")["error"] == "the region is not one of the tenant's"
    assert get(svc, scope=SUB, region="eu-nl")["error"] == "refused: the project %s is in eu-de, not in eu-nl" % SUB
    assert {"id": P_CH} not in iam.scopes and {"id": P_NL} not in iam.scopes
    # the old line still reads its one region, with and without a scope
    assert get(svc, OLD_ALIAS)["status"] == 200 and get(svc, OLD_ALIAS, scope=SUB)["status"] == 200


def test_t2_a_write_is_refused_with_a_scope_and_a_scope_is_plain(session, iam):
    svc, uid = session
    for method in ("POST", "PUT", "PATCH", "DELETE"):
        for alias in (ALIAS, KEY_ALIAS):
            assert get(svc, alias, method=method, scope=SUB, project=PROJECT)["error"] == \
                readonly.METHOD_LINE % alias
            with pytest.raises(keys.Refused, match="^%s$" % (readonly.METHOD_LINE % alias)):
                svc._call({"tenant": alias, "method": method, "service": "ecs", "path": "/v1/x", "scope": SUB,
                           "project": PROJECT}, uid, PROJECT)
    for bad in ("../eu-de", "eu-de/x", "EU-DE", "", "eu-de_" + "x" * 80, 7):
        assert get(svc, scope=bad)["error"] == readonly.SCOPE_LINE, bad
    assert get(svc, service="obs", path="/", scope="eu-de", tenant=KEY_ALIAS)["error"].startswith(
        "refused: an OBS call names no project")
    assert iam.logins == 0 and iam.calls == []


def test_t2_the_projects_op_lists_what_a_granted_session_reads(session, iam, monkeypatch, capsys):
    svc, uid = session
    want = [{"name": "eu-de", "id": P_DE, "region": "eu-de"}, {"name": SUB, "id": P_SUB, "region": "eu-de"},
            {"name": "eu-nl", "id": P_NL, "region": "eu-nl"}]
    a = svc._readonly_projects({"tenant": ALIAS, "project": PROJECT}, uid, PROJECT)
    assert a == {"ok": True, "status": 200, "tenant": ALIAS, "regions": ["eu-de", "eu-nl"], "projects": want,
                 "other": 2}
    assert not any(t in json.dumps(a) for t in iam.tokens)
    # the grant check is the one of a call: another granted project, the console, a test tenant
    with pytest.raises(keys.Refused, match="^%s$" % (readonly.GRANT_LINE % (OTHER, ALIAS, ALIAS, OTHER))):
        svc._readonly_projects({"tenant": ALIAS, "project": OTHER}, uid, OTHER)
    with pytest.raises(keys.Refused, match="runs from the folder of an active project"):
        svc._readonly_projects({"tenant": ALIAS, "project": PROJECT}, uid, OTHER)
    svc._console_uid = lambda: uid
    with pytest.raises(keys.Refused, match=readonly.CONSOLE_LINE % ALIAS):
        svc._readonly_projects({"tenant": ALIAS, "project": PROJECT}, uid, PROJECT)
    assert keys.request(svc.call_path, {"op": "projects", "tenant": "cust-9"})["ok"] is False
    # the owner's own call over the socket, and the command line of a session
    assert keys.request(svc.call_path, {"op": "projects", "tenant": ALIAS})["projects"] == want
    sent = []
    monkeypatch.setattr(keys, "request", lambda sock, req, **k: sent.append(req) or dict(a))
    assert tenants.main(["projects", ALIAS, "--project", PROJECT]) == 0
    out = capsys.readouterr().out
    assert sent == [{"op": "projects", "tenant": ALIAS, "project": PROJECT}]
    assert "%s: 3 project(s) in eu-de+eu-nl" % ALIAS in out and SUB in out and "2 more the login sees" in out
    assert "--scope PROJECT" in out


def test_t2_cloud_call_sends_the_scope(monkeypatch, capsys):
    sent = []
    monkeypatch.setattr(keys, "request", lambda sock, req, **k: sent.append(req) or {"ok": True, "status": 200,
                                                                                     "data": {"servers": []}})
    assert cloud.main(["call", "GET", "ecs", "/v1/{project_id}/cloudservers", "--tenant", ALIAS, "--scope", SUB,
                       "--project", PROJECT]) == 0
    assert sent[0]["scope"] == SUB and sent[0]["method"] == "GET"
    assert cloud.main(["call", "GET", "ecs", "/v1/x", "--tenant", ALIAS, "--project", PROJECT]) == 0
    assert "scope" not in sent[1]


class ScopedGateway(FakeGateway):
    """The stand-in gateway of a read key: the project list of IAM and the servers of each project by its id."""

    def route(self, method, raw_path):
        path = raw_path.partition("?")[0]
        if method != "GET":
            return 405, {"error_msg": "read only"}
        if path == readonly.PROJECTS_PATH:
            return 200, {"projects": PROJECTS}
        parts = path.strip("/").split("/")
        if len(parts) == 3 and parts[0] == "v1" and parts[2] == "cloudservers":
            return 200, {"servers": [{"id": "s-" + parts[1][:2]}]}
        return super().route(method, raw_path)


def test_t2_a_read_key_reads_a_sub_project_by_its_id_signed():
    with ScopedGateway(AK, SK) as gw:
        with short_dir() as d:
            s = _service(gw.base, d / "log", d / "vault")
            s.admin_path, s.call_path = d / "admin.sock", d / "call.sock"
            s.start()
            try:
                keys.unlock(s.admin_path, sorted(STORE), reader=lambda e: STORE[e], settings=dict(SETTINGS))
                a = get(s, KEY_ALIAS, scope=SUB)
                assert a["ok"] and a["status"] == 200 and a["data"]["servers"] == [{"id": "s-" + P_SUB[:2]}], a
                assert gw.calls[-1] == ("GET", "/v1/%s/cloudservers" % P_SUB)
                assert get(s, KEY_ALIAS, scope="eu-nl")["status"] == 200
                assert gw.calls[-1] == ("GET", "/v1/%s/cloudservers" % P_NL)
                assert get(s, KEY_ALIAS, scope=FOREIGN)["error"] == readonly.OTHER_DOMAIN_LINE % (FOREIGN, KEY_ALIAS)
                assert [c[1] for c in gw.calls].count(readonly.PROJECTS_PATH) == 1
                assert get(s, KEY_ALIAS, method="DELETE", scope=SUB)["error"] == readonly.METHOD_LINE % KEY_ALIAS
            finally:
                s.stop()


def test_t2_the_endpoints_follow_the_region_of_the_project():
    c = cloud.Client(keys.obs.Keys("", ""), "eu-nl", token="t", project_id=P_NL)
    assert c.base("ecs") == "https://ecs.eu-nl.otc.t-systems.com"
    assert readonly.iam_base("eu-nl") == "https://iam.eu-nl.otc.t-systems.com"
    assert readonly.region_of(SUB) == "eu-de" and readonly.region_of("eu-nl") == "eu-nl"
    assert readonly.region_of("MOS") is None and readonly.region_of("eu-de/x") is None
    assert urllib.parse.urlsplit(c.url("ecs", "/v1/%s/cloudservers" % P_NL)).netloc == "ecs.eu-nl.otc.t-systems.com"


def test_t2_a_token_per_project_and_a_lock_forgets_the_project_list():
    got = []

    def fetch(login, domain_id, region, endpoint=None, project_id=None):
        got.append((region, project_id))
        return "tok-%d" % len(got), project_id or P_DE, 3600.0

    t = readonly.Tokens(fetch=fetch, clock=lambda: 1000.0)
    assert t.get(ALIAS, "eu-de", {}, DOMAIN_ID) == ("tok-1", P_DE)
    assert t.get(ALIAS, "eu-de", {}, DOMAIN_ID, project_id=P_SUB) == ("tok-2", P_SUB)
    assert t.get(ALIAS, "eu-de", {}, DOMAIN_ID, project_id=P_SUB) == ("tok-2", P_SUB)
    t.drop(ALIAS, P_SUB)
    assert t.get(ALIAS, "eu-de", {}, DOMAIN_ID)[0] == "tok-1"
    assert t.get(ALIAS, "eu-de", {}, DOMAIN_ID, project_id=P_SUB)[0] == "tok-3"
    t.keep_listing(ALIAS, [{"id": P_DE}])
    assert t.listing(ALIAS) == [{"id": P_DE}]
    t.clear()
    assert t.listing(ALIAS) is None and t.values() == []
