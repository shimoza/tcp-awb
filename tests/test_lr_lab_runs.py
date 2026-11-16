"""Task LR: lab runs with the admin logins of the two lab tenants (awb/tcp/labadmin.py in the key service,
awb/labclient.py for the session, `awb lab run start|end|status`, the owner status).

A stand-in IAM and API on 127.0.0.1 takes a password login (domain or project scope), hands out temporary keys,
keeps the resources a call makes and deletes them again. Every login, password, token, key and id here is invented
and built from pieces at run time.
"""
from __future__ import annotations

import json
import os
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace

import pytest

from awb import config, labclient, labsecrets, ownerwait, projects, rulesync
from awb.tcp import keys, labadmin, xchg
from tests.test_keys import calls_logged_here, no_waiting, short_dir  # noqa: F401

LAB = "test-10491"
LAB2 = "test-10497"
NOT_LAB = "test-10498"
CUSTOMER = "cust-1"
PROJECT = "tcp-ab2c"
OTHER = "tcp-zz99"
DOMAIN_ID = "d" * 32
P_DE = "e" * 32
P_SUB = "a" * 32
USER = "lab-admin-" + "fixture"
DOMAIN = "lab-account-" + "fixture"
PASSWORD = "-".join(("Invented", "Admin", "4711"))
LOGIN = {"domain": DOMAIN, "user": USER, "password": PASSWORD}
ADMIN_STORE = {"%s/%s" % (a, k): v for a in (LAB, LAB2) for k, v in LOGIN.items()}


def _long(prefix: str, n: int) -> str:
    return prefix + "".join("abcdefghij"[(i * 7) % 10] for i in range(n))


class FakeLab:
    """IAM, STS, VPC, ECS and OBS on one address. `deleted` keeps the order of the deletes, `fail` paths answer a
    delete with 409, `seen_tokens` the X-Auth-Token of every call."""

    def __init__(self):
        self.n = 0
        self.tokens: dict[str, dict] = {}
        self.creds: dict[str, tuple[str, str]] = {}
        self.store: dict[str, dict] = {}
        self.deleted: list[str] = []
        self.fail: set[str] = set()
        self.seen_tokens: list[str] = []
        self.obs_auth: list[tuple[str, str]] = []
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _send(self, status, body=None, headers=None):
                raw = json.dumps(body).encode() if body is not None else b""
                self.send_response(status)
                for k, v in (headers or {}).items():
                    self.send_header(k, v)
                if body is not None:
                    self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                if self.command != "HEAD":
                    self.wfile.write(raw)

            def _token(self, scope=None):
                fake.n += 1
                token = "MII%05d" % fake.n + _long("", 89)
                fake.tokens[token] = scope or {"project": P_DE}
                return token

            def _any(self, method):
                path, _, query = self.path.partition("?")
                path = path.rstrip("/") or "/"
                length = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(length) if length else b""
                body = json.loads(raw) if raw and raw[:1] in b"{[" else None
                if method == "POST" and path == "/v3/auth/tokens":
                    return self._login(body)
                auth = self.headers.get("Authorization") or ""
                if auth.startswith("OBS "):
                    ak = auth[4:].partition(":")[0]
                    st = self.headers.get("x-obs-security-token") or ""
                    fake.obs_auth.append((ak, st))
                    if fake.creds.get(ak, ("", ""))[1] != st:
                        return self._send(403, {"code": "AccessDenied"})
                else:
                    token = self.headers.get("X-Auth-Token") or ""
                    fake.seen_tokens.append(token)
                    if token not in fake.tokens:
                        return self._send(401, {"error": {"code": "IAM.0067", "message": "Invalid token."}})
                if method == "POST" and path == "/v3.0/OS-CREDENTIAL/securitytokens":
                    fake.n += 1
                    ak = "TMPAK" + "%015d" % fake.n
                    sk = _long("tmpsk", 35)
                    st = _long("gQ", 300)
                    fake.creds[ak] = (sk, st)
                    return self._send(201, {"credential": {"access": ak, "secret": sk, "securitytoken": st,
                                                           "expires_at": "2099-01-01T00:00:00.000000Z"}})
                if path == "/v3/echo":
                    # a stand-in answer that carries the admin login and its token back, the worst case
                    return self._send(200, {"user": {"name": USER, "password": PASSWORD, "domain": DOMAIN},
                                            "token": self.headers.get("X-Auth-Token")})
                if path == "/v3/issue":
                    token = self._token({"project": P_DE})
                    return self._send(201, {"token": {"methods": ["token"]}}, {"X-Subject-Token": token})
                if path == "/v3/whoami":
                    return self._send(200, {"token": "seen"})
                if method == "POST":
                    fake.n += 1
                    kind = path.rstrip("/").split("/")[-1]
                    rid = "%032x" % (0xabc000 + fake.n)
                    if kind == "cloudservers":
                        fake.store[path + "/" + rid] = {}
                        return self._send(200, {"job_id": "job-1", "serverIds": [rid]})
                    if kind == "delete":
                        for s in (body or {}).get("servers", []):
                            p = path.rsplit("/", 1)[0] + "/" + s["id"]
                            fake.store.pop(p, None)
                            fake.deleted.append(p)
                        return self._send(200, {"job_id": "job-2"})
                    if kind == "action":
                        return self._send(202, {"id": rid})
                    fake.store[path + "/" + rid] = {"id": rid}
                    return self._send(201, {kind.rstrip("s"): {"id": rid, "name": "lr-%d" % fake.n}})
                if method in ("GET", "HEAD"):
                    if path in fake.store:
                        return self._send(200, fake.store[path])
                    return self._send(404, {"error_msg": "not found"})
                if method in ("PUT", "PATCH"):
                    fake.store[path] = {"put": True}
                    return self._send(201 if method == "PUT" else 200, {"ok": True})
                if method == "DELETE":
                    if path in fake.fail:
                        return self._send(409, {"error": {"code": "IAM.0099", "message": "in use"}})
                    if path not in fake.store:
                        return self._send(404, {"error_msg": "not found"})
                    fake.store.pop(path)
                    fake.deleted.append(path)
                    return self._send(204)
                return self._send(405, {"error_msg": "no"})

            def _login(self, req):
                user = req["auth"]["identity"]["password"]["user"]
                if user["password"] != PASSWORD or user["name"] != USER or user["domain"]["name"] != DOMAIN:
                    return self._send(401, {"error": {"code": "IAM.0062", "message": "Incorrect password."}})
                scope = req["auth"]["scope"]
                if "domain" in scope:
                    body = {"expires_at": "2099-01-01T00:00:00.000000Z", "domain": {"id": DOMAIN_ID, "name": DOMAIN}}
                else:
                    pid = scope["project"].get("id") or P_DE
                    body = {"expires_at": "2099-01-01T00:00:00.000000Z",
                            "project": {"id": pid, "name": "eu-de", "domain": {"id": DOMAIN_ID}}}
                token = self._token(scope)
                return self._send(201, {"token": body}, {"X-Subject-Token": token})

            def do_GET(self):
                self._any("GET")

            def do_HEAD(self):
                self._any("HEAD")

            def do_POST(self):
                self._any("POST")

            def do_PUT(self):
                self._any("PUT")

            def do_PATCH(self):
                self._any("PATCH")

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


SETTINGS = {"lab_admin": "%s %s" % (LAB, LAB2), "readonly": "%s:key:%s:eu-de" % (CUSTOMER, DOMAIN_ID)}
TENANT_STORE = {"%s/ak" % CUSTOMER: "AKREADFAKE", "%s/sk" % CUSTOMER: "sk-read-fake",
                "%s/ak" % NOT_LAB: "AKOTHERFAKE", "%s/sk" % NOT_LAB: "sk-other-fake"}


@pytest.fixture
def fake():
    with FakeLab() as f:
        yield f


@pytest.fixture
def svc(fake, tmp_path, monkeypatch):
    with short_dir() as d:
        s = keys.Service(d / "admin.sock", d / "call.sock", endpoint=fake.base, log_dir=tmp_path / "log",
                         paths_fn=lambda: SimpleNamespace(vault=tmp_path / "vault"))
        s._registered = lambda: {LAB: ("eu-de",), LAB2: ("eu-de",), NOT_LAB: ("eu-de",)}
        s._active_project = lambda code: code in (PROJECT, OTHER)
        s.start()
        monkeypatch.setenv(keys.CALL_SOCKET_ENV, str(s.call_path))
        monkeypatch.setenv(keys.ADMIN_SOCKET_ENV, str(s.admin_path))
        monkeypatch.setattr(labadmin, "END_PAUSE", 0.0)
        try:
            keys.unlock(s.admin_path, sorted(TENANT_STORE), reader=lambda e: TENANT_STORE[e],
                        settings=dict(SETTINGS), admin_reader=lambda e: ADMIN_STORE[e])
            yield s
        finally:
            s.stop()


@pytest.fixture
def session(svc, monkeypatch):
    """The service as a session of PROJECT sees it: the project granted the kind project."""
    rows = {c: SimpleNamespace(code=c, kind="project", state="active", customer=None) for c in (PROJECT, OTHER)}
    svc._project_row = lambda code: rows.get(code)
    svc._granted = lambda code: code in rows
    svc._console_uid = lambda: None
    return svc, os.getuid() + 1


def run_op(svc, uid, action, tenant=LAB, project=PROJECT, **extra):
    return svc._call_socket(dict({"op": "lab_run", "action": action, "tenant": tenant, "project": project}, **extra),
                            uid, caller=project)


def lab_call(svc, uid, method, service, path, run, tenant=LAB, project=PROJECT, **extra):
    req = dict({"op": "lab_call", "tenant": tenant, "project": project, "run": run, "method": method,
                "service": service, "path": path}, **extra)
    return svc._call_socket(req, uid, caller=project)


def _everything(svc, tmp_path) -> str:
    """Every file the service wrote: the run records and the call log."""
    return "\n".join(f.read_text(encoding="utf-8") for f in tmp_path.rglob("*") if f.is_file())


# --------------------------------------------------------------------------- the line of keys.conf and the unlock


def test_lr_the_lab_admin_line_names_the_two_lab_tenants_only(tmp_path):
    assert labadmin.parse("%s %s" % (LAB, LAB2)) == [LAB, LAB2]
    assert labadmin.parse("%s, %s" % (LAB2, LAB)) == [LAB2, LAB]
    for bad in (NOT_LAB, "%s %s" % (LAB, NOT_LAB), CUSTOMER, "%s %s" % (LAB, LAB)):
        assert labadmin.parse(bad) is None
        conf = tmp_path / "keys.conf"
        conf.write_text("bucket_tenant = test-1\nlab_admin = %s\n" % bad, encoding="utf-8")
        with pytest.raises(xchg.XchgError, match="line 2"):
            xchg.read_settings(conf)
    conf.write_text("lab_admin = %s %s\n" % (LAB, LAB2), encoding="utf-8")
    assert xchg.read_settings(conf)["lab_admin"] == "%s %s" % (LAB, LAB2)


def test_lr_the_unlock_loads_the_admin_logins_and_no_value_leaves_the_service(svc):
    status = keys.request(svc.admin_path, {"op": "status"})
    assert status["tenants"][LAB]["admin"] is True and status["tenants"][LAB2]["admin"] is True
    assert "admin" not in status["tenants"][NOT_LAB] and "admin" not in status["tenants"][CUSTOMER]
    seen = json.dumps(status) + json.dumps(keys.request(svc.call_path, {"op": "tenants"}))
    for value in (PASSWORD, USER, DOMAIN):
        assert value not in seen
    asked: list[str] = []

    def reader(entry):
        asked.append(entry)
        return ADMIN_STORE[entry]

    keys.unlock(svc.admin_path, sorted(TENANT_STORE), reader=lambda e: TENANT_STORE[e],
                settings={"lab_admin": LAB}, admin_reader=reader)
    assert asked == ["%s/domain" % LAB, "%s/user" % LAB, "%s/password" % LAB]
    with pytest.raises(keys.KeysError, match="awb/admin/%s/user" % LAB2):
        keys.unlock(svc.admin_path, sorted(TENANT_STORE), reader=lambda e: TENANT_STORE[e],
                    settings={"lab_admin": LAB2}, admin_reader=lambda e: {"%s/domain" % LAB2: DOMAIN}[e])


def test_lr_the_admin_login_comes_from_awb_admin_of_pass(monkeypatch):
    seen = []

    def run(args, **kw):
        import subprocess

        seen.append(args)
        return subprocess.CompletedProcess(args, 0, "value\n", "")

    monkeypatch.setattr(keys.subprocess, "run", run)
    assert keys.pass_reader("%s/user" % LAB, labadmin.ADMIN_PREFIX) == "value"
    assert seen[-1][:3] == ["pass", "show", "awb/admin/%s/user" % LAB]


# --------------------------------------------------------------------------- refusals


def test_lr_a_call_without_an_open_run_is_refused(session, fake):
    svc, uid = session
    a = lab_call(svc, uid, "GET", "iam", "/v3/users", None)
    assert not a["ok"] and a["error"] == labadmin.NO_RUN_LINE % (PROJECT, LAB, LAB)
    started = run_op(svc, uid, "start")
    assert started["ok"]
    wrong = lab_call(svc, uid, "GET", "iam", "/v3/users", "run-20260101-000000-abcd")
    assert not wrong["ok"] and "no open lab run" in wrong["error"]
    other = lab_call(svc, uid, "GET", "iam", "/v3/users", started["run"], tenant=LAB2)
    assert not other["ok"] and "no open lab run" in other["error"]
    again = run_op(svc, uid, "start")
    assert not again["ok"] and started["run"] in again["error"]
    assert fake.seen_tokens == []          # nothing reached the platform


def test_lr_test_10498_and_a_customer_tenant_are_refused(session, fake):
    svc, uid = session
    for tenant in (NOT_LAB, CUSTOMER, "test-1"):
        for answer in (run_op(svc, uid, "start", tenant=tenant),
                       lab_call(svc, uid, "POST", "vpc", "/v1/{project_id}/vpcs", "x", tenant=tenant, body={})):
            # TW: a customer tenant in read-only mode is refused with the line that names the write mode
            first = ("refused: %s is a read-only tenant" % tenant if tenant == CUSTOMER
                     else "refused: %s is no lab tenant" % tenant)
            assert not answer["ok"] and answer["error"].startswith(first), answer
    # the ordinary call keeps its rules: an IAM write is refused, a lab write without lab_writes too
    plain = svc._call_socket({"op": "call", "tenant": NOT_LAB, "role": "read", "method": "POST", "service": "iam",
                              "path": "/v3/users", "project": PROJECT}, uid, caller=PROJECT)
    assert not plain["ok"]
    assert fake.seen_tokens == []


def test_lr_a_tenant_not_on_the_line_is_refused_and_another_project_is_refused(session):
    svc, uid = session
    keys.unlock(svc.admin_path, sorted(TENANT_STORE), reader=lambda e: TENANT_STORE[e], settings={"lab_admin": LAB},
                admin_reader=lambda e: ADMIN_STORE[e])
    a = run_op(svc, uid, "start", tenant=LAB2)
    assert not a["ok"] and "no lab_admin line for %s" % LAB2 in a["error"]
    b = svc._call_socket({"op": "lab_run", "action": "start", "tenant": LAB, "project": OTHER}, uid, caller=PROJECT)
    assert not b["ok"] and "folder of an active project" in b["error"]
    svc._console_uid = lambda: uid
    c = run_op(svc, uid, "start")
    assert not c["ok"] and "console" in c["error"]


# --------------------------------------------------------------------------- a run: creates and the end


def test_lr_a_run_records_its_creates_and_the_end_removes_them_newest_first(session, fake):
    svc, uid = session
    run = run_op(svc, uid, "start", note="wif scopes")["run"]
    user = lab_call(svc, uid, "POST", "iam", "/v3/users", run, body={"user": {"name": "lr-user"}})
    group = lab_call(svc, uid, "POST", "iam", "/v3/groups", run, body={"group": {"name": "lr-group"}})
    vpc = lab_call(svc, uid, "POST", "vpc", "/v1/{project_id}/vpcs", run, body={"vpc": {"name": "lr"}})
    idp = lab_call(svc, uid, "PUT", "iam", "/v3/OS-FEDERATION/identity_providers/lr-idp", run, body={"x": 1})
    again = lab_call(svc, uid, "PUT", "iam", "/v3/OS-FEDERATION/identity_providers/lr-idp", run, body={"x": 2})
    ecs = lab_call(svc, uid, "POST", "ecs", "/v1/{project_id}/cloudservers", run, body={"server": {}})
    action = lab_call(svc, uid, "POST", "ecs", "/v1/{project_id}/cloudservers/x1/action", run, body={"reboot": {}})
    token = lab_call(svc, uid, "POST", "iam", "/v3.0/OS-CREDENTIAL/securitytokens", run, body={"auth": {}})
    assert [a["created"] for a in (user, group, vpc, idp, again, ecs, action, token)] == [
        ["iam users"], ["iam groups"], ["vpc vpcs"], ["iam identity_providers"], [], ["ecs cloudservers"], [], []]
    assert vpc["path"] == "/v1/%s/vpcs" % P_DE          # {project_id} filled in by the service
    uid_path = "/v3/users/" + user["data"]["user"]["id"]
    removed = lab_call(svc, uid, "DELETE", "iam", "/v3/groups/" + group["data"]["group"]["id"], run)
    assert removed["status"] == 204
    status = run_op(svc, uid, "status")["runs"]
    assert [r["run"] for r in status] == [run]
    assert [c["label"] for c in status[0]["left"]] == ["iam users", "vpc vpcs", "iam identity_providers",
                                                       "ecs cloudservers"]
    fake.deleted.clear()
    ended = run_op(svc, uid, "end")
    assert ended["ok"] and ended["created"] == 5 and ended["failed"] == [], ended
    servers = "/v1/%s/cloudservers/%s" % (P_DE, ecs["data"]["serverIds"][0])
    assert fake.deleted == [servers, "/v3/OS-FEDERATION/identity_providers/lr-idp",
                            "/v1/%s/vpcs/%s" % (P_DE, vpc["data"]["vpc"]["id"]), uid_path]
    assert run_op(svc, uid, "status")["runs"] == []
    assert not lab_call(svc, uid, "GET", "iam", "/v3/users", run)["ok"]   # the run is closed
    record = json.loads((labadmin.runs_dir(svc.paths_fn()) / ("%s.json" % run)).read_text())
    assert record["state"] == "ended" and record["calls"] == 9 and record["note"] == "wif scopes"
    assert oct(os.stat(labadmin.runs_dir(svc.paths_fn()) / ("%s.json" % run)).st_mode & 0o777) == "0o600"


def test_lr_a_failed_delete_stays_and_a_second_end_tries_it_again(session, fake):
    svc, uid = session
    run = run_op(svc, uid, "start")["run"]
    user = lab_call(svc, uid, "POST", "iam", "/v3/users", run, body={"user": {}})
    path = "/v3/users/" + user["data"]["user"]["id"]
    fake.fail.add(path)
    ended = run_op(svc, uid, "end")
    assert ended["ok"] and len(ended["failed"]) == 1 and ended["failed"][0]["error"] == "HTTP 409 IAM.0099"
    assert ended["failed"][0]["delete"] == "DELETE iam %s" % path
    fake.fail.clear()
    again = run_op(svc, uid, "end")
    assert again["ok"] and again["failed"] == [] and [c["label"] for c in again["removed"]] == ["iam users"]
    assert fake.deleted == [path]


# --------------------------------------------------------------------------- secrets never reach the session


def test_lr_no_password_login_or_whole_admin_token_reaches_the_answer_or_a_file(session, fake, tmp_path):
    svc, uid = session
    run = run_op(svc, uid, "start")["run"]
    echo = lab_call(svc, uid, "GET", "iam", "/v3/echo", run)
    cred = lab_call(svc, uid, "POST", "iam", "/v3.0/OS-CREDENTIAL/securitytokens", run, body={"auth": {}})
    issued = lab_call(svc, uid, "POST", "iam", "/v3/issue", run, body={})
    text = json.dumps([echo, cred, issued])
    for value in (PASSWORD, USER, DOMAIN):
        assert value not in text
    for token in list(fake.tokens) + [v for pair in fake.creds.values() for v in pair] + list(fake.creds):
        assert token not in text, "a whole token or key reached the answer"
    assert echo["data"]["user"] == {"name": labsecrets.LOGIN, "password": labsecrets.SECRET,
                                    "domain": labsecrets.LOGIN}
    assert echo["data"]["token"].endswith("...(97 chars)") and echo["data"]["token"].startswith("MII")
    access = cred["data"]["credential"]["access"]
    assert access == labsecrets.short(next(iter(fake.creds)))
    assert cred["data"]["credential"]["securitytoken"].endswith("...(302 chars)")
    assert issued["headers"]["X-Subject-Token"].endswith("...(97 chars)")
    run_op(svc, uid, "end")
    written = _everything(svc, tmp_path)
    for value in [PASSWORD, USER, DOMAIN] + list(fake.tokens):
        assert value not in written


def test_lr_a_held_token_or_key_of_the_run_works_in_its_shortened_form(session, fake):
    svc, uid = session
    run = run_op(svc, uid, "start")["run"]
    issued = lab_call(svc, uid, "POST", "iam", "/v3/issue", run, body={})
    shown = issued["headers"]["X-Subject-Token"]
    whole = [t for t in fake.tokens if labsecrets.short(t) == shown][0]
    used = lab_call(svc, uid, "GET", "iam", "/v3/whoami", run, headers={"X-Auth-Token": shown})
    assert used["status"] == 200 and fake.seen_tokens[-1] == whole
    forged = lab_call(svc, uid, "GET", "iam", "/v3/whoami", run, headers={"X-Auth-Token": "MIIabcde...(97 chars)"})
    assert not forged["ok"] and "X-Auth-Token takes only a token" in forged["error"]
    blocked = lab_call(svc, uid, "GET", "iam", "/v3/whoami", run, headers={"Authorization": "x"})
    assert not blocked["ok"]
    cred = lab_call(svc, uid, "POST", "iam", "/v3.0/OS-CREDENTIAL/securitytokens", run, body={"auth": {}})
    access = cred["data"]["credential"]["access"]
    bucket = lab_call(svc, uid, "PUT", "obs", "/%s-lr-1" % PROJECT, run, credential=access)
    whole_ak = [a for a in fake.creds if labsecrets.short(a) == access][0]
    assert bucket["status"] == 201 and fake.obs_auth[-1] == (whole_ak, fake.creds[whole_ak][1])
    assert bucket["created"] == []          # a held key's write is the session's own: not probed, not recorded


def test_lr_obs_goes_with_a_temporary_key_of_the_admin_token(session, fake):
    svc, uid = session
    run = run_op(svc, uid, "start")["run"]
    put = lab_call(svc, uid, "PUT", "obs", "/%s-lr-2" % PROJECT, run)
    assert put["status"] == 201 and put["created"] == ["obs bucket"]
    ak, st = fake.obs_auth[-1]
    assert ak.startswith("TMPAK") and st == fake.creds[ak][1]
    ended = run_op(svc, uid, "end")
    assert [c["label"] for c in ended["removed"]] == ["obs bucket"] and fake.deleted == ["/%s-lr-2" % PROJECT]


# --------------------------------------------------------------------------- the owner status


def test_lr_an_old_open_run_and_a_failed_delete_show_in_the_owner_status(home):
    now = time.time()
    old = {"id": "run-20261028-060000-abcd", "project": PROJECT, "tenant": LAB, "state": "open", "note": "",
           "started": "2026-10-28T06:00:00Z", "started_ts": now - 3 * 3600, "calls": 4,
           "created": [{"n": 1, "label": "vpc vpcs", "name": "lr", "state": "live",
                        "delete": {"method": "DELETE", "service": "vpc", "path": "/v1/x/vpcs/y"}}]}
    young = dict(old, id="run-20261028-085000-efgh", project=OTHER, started_ts=now - 600)
    failed = dict(old, id="run-20261027-080000-ijkm", tenant=LAB2, state="ended",
                  created=[dict(old["created"][0], label="iam users", state="failed", error="HTTP 409")])
    clean = dict(failed, id="run-20261027-090000-npqr", created=[dict(failed["created"][0], state="removed")])
    for r in (old, young, failed, clean):
        labadmin.save(home, r)
    items = ownerwait.lab_run_items(home, now)
    assert [(i.label, i.text, i.where, i.block) for i in items] == [
        (PROJECT, "lab run on %s ended with 1 failed delete: iam users" % LAB2, ownerwait.WHERE_SESSION,
         "awb lab run end --tenant %s" % LAB2),
        (PROJECT, "lab run on %s open 3 h, 1 left: vpc vpcs" % LAB, ownerwait.WHERE_SESSION,
         "awb lab run end --tenant %s" % LAB)]
    listed, _parked = ownerwait.collect(home, queue=home.vault / "none", runs=home.vault / "none")
    assert sum(1 for i in listed if i.text.startswith("lab run on")) == 2
    text = "\n".join(ownerwait.render(listed, 0))
    assert "/v1/x/vpcs/y" not in text


# --------------------------------------------------------------------------- the client and the command line


def test_lr_labclient_and_the_commands_work_through_the_socket(svc, fake, tmp_path, monkeypatch, capsys):
    folder = tmp_path / "projects" / PROJECT
    folder.mkdir(parents=True)
    (folder / "SCOPE.md").write_text("# %s\n\n- code: %s\n" % (PROJECT, PROJECT), encoding="utf-8")
    monkeypatch.chdir(folder)
    monkeypatch.setattr(config, "is_work_user", lambda: False)
    assert labclient.run_main(["start", "--tenant", LAB, "--note", "scopes"]) == 0
    run = (folder / "notes" / "lab-runs" / ("%s.open" % LAB)).read_text().strip()
    assert labadmin.RUN_RE.match(run)
    status, headers, body = labclient.call("POST", "iam", "/v3/users", body={"user": {"name": "lr-cli",
                                                                                       "password": "Pw-" + "x" * 12}})
    assert status == 201 and body["user"]["id"] and isinstance(headers, dict)
    labclient.call("GET", "iam", "/v3/echo")
    with pytest.raises(labclient.LabClientError, match="no lab tenant"):
        labclient.call("GET", "iam", "/v3/users", tenant=NOT_LAB, run=run)
    assert labclient.run_main(["status"]) == 0
    assert "1 left" in capsys.readouterr().out
    assert labclient.run_main(["end"]) == 0
    out = capsys.readouterr().out
    assert "created 1, removed 1, failed 0" in out
    assert not (folder / "notes" / "lab-runs" / ("%s.open" % LAB)).exists()
    log = (folder / "notes" / "lab-runs" / ("%s.log" % run)).read_text()
    assert "POST iam /v3/users" in log and "recorded: iam users" in log and "end of run" in log
    assert "no lab tenant" in log
    for value in [PASSWORD, USER, DOMAIN, "Pw-" + "x" * 12] + list(fake.tokens):
        assert value not in log
    with pytest.raises(labclient.LabClientError, match="no lab run is open"):
        labclient.call("GET", "iam", "/v3/users")
    from awb.tcp import lab

    assert lab.main(["run", "status"]) == 0


# --------------------------------------------------------------------------- the rules


def test_lr_the_work_rules_carry_the_lab_run_rule_and_sync_adds_it(tmp_path):
    text = projects._claude(PROJECT, "project", projects.NO_CUSTOMER)
    assert projects.LAB_RUN_RULE in text and len(text.splitlines()) <= projects.MAX_CLAUDE_LINES
    assert projects.LAB_RUN_RULE not in projects._claude(PROJECT, "query", projects.NO_CUSTOMER)
    old = text.replace(" " + projects.LAB_RUN_RULE, "")
    (tmp_path / "CLAUDE.md").write_text(old, encoding="utf-8")
    assert rulesync.sync_project(tmp_path) == ["CLAUDE.md"]
    assert (tmp_path / "CLAUDE.md").read_text(encoding="utf-8") == text
    work = (Path(__file__).resolve().parent.parent / "seal" / "work-claude" / "CLAUDE.md").read_text(encoding="utf-8")
    flat = " ".join(work.split())
    assert "A lab run (test-10491, test-10497) starts only on his word" in flat
    assert "awb lab run start, scripts call through awb.labclient, awb lab run end" in flat
    assert "Never ask him for logins." in flat


# --------------------------------------------------------------------------- client hardening regressions

_HARD_RUN = "run-20261116-010203-abcd"
_HARD_NEXT = "run-20261116-010204-efgh"


def _hard_project(tmp_path, monkeypatch):
    root = tmp_path / PROJECT
    root.mkdir()
    (root / "SCOPE.md").write_text("- code: %s\n" % PROJECT)
    logs = root / "notes" / "lab-runs"
    logs.mkdir(parents=True)
    (logs / (LAB + ".open")).write_text(_HARD_RUN + "\n")
    monkeypatch.chdir(root)
    monkeypatch.setattr(config, "is_work_user", lambda: False)
    return root, logs


def _hard_kept(rid="resource-a", **values):
    return dict({"id": rid, "label": "vpc rules", "region": "eu-de", "reason": "retain for testing"}, **values)


class _HardSocket:
    def __init__(self, chunks):
        self.chunks = iter(chunks)
        self.closed = False
        self.timeouts = []

    def settimeout(self, value):
        self.timeouts.append(value)

    def connect(self, where):
        pass

    def sendall(self, value):
        pass

    def recv(self, size):
        return next(self.chunks, b"")

    def close(self):
        self.closed = True


def _hard_socket(monkeypatch, chunks):
    sock = _HardSocket(chunks)
    monkeypatch.setattr(labclient.socket, "socket", lambda *a: sock)
    return sock


def test_lr_hard_identifiers_cannot_escape_or_inject_lines(tmp_path, monkeypatch):
    root, logs = _hard_project(tmp_path, monkeypatch)
    seen = []
    monkeypatch.setattr(labclient, "_request", lambda *a: seen.append(a) or {"ok": True, "status": 200})
    for value in ("../../escape", "/tmp/escape", _HARD_RUN + "\n", "", "a\x00b"):
        with pytest.raises(labclient.LabClientError):
            labclient.call("GET", "iam", "/v3/users", run=value)
        with pytest.raises(labclient.LabClientError):
            labclient.log(root, value, ["safe"])
    for tenant in ("../escape", LAB + "\n", "a/b"):
        with pytest.raises(labclient.LabClientError):
            labclient._start(root, PROJECT, tenant, "")
    assert not seen
    assert not list(tmp_path.glob("*.log"))
    assert not list(logs.glob("*.log"))


def test_lr_hard_service_run_is_validated_before_publication(tmp_path, monkeypatch, capsys):
    root, logs = _hard_project(tmp_path, monkeypatch)
    monkeypatch.setattr(labclient, "_request", lambda *a: {"ok": True, "run": "../../escape"})
    assert labclient.run_main(["start", "--tenant", LAB]) == 2
    assert (logs / (LAB + ".open")).read_text() == _HARD_RUN + "\n"
    assert "../../escape" not in capsys.readouterr().err
    assert not (root / "escape.log").exists()


def test_lr_hard_markers_reject_links_special_files_and_bad_bytes(tmp_path, monkeypatch):
    root, logs = _hard_project(tmp_path, monkeypatch)
    marker = logs / (LAB + ".open")
    target = tmp_path / "outside"
    target.write_text(_HARD_RUN + "\n")
    marker.unlink()
    marker.symlink_to(target)
    with pytest.raises(labclient.LabClientError):
        labclient._open_runs(root)
    marker.unlink()
    marker.mkdir()
    with pytest.raises(labclient.LabClientError):
        labclient._open_runs(root)
    marker.rmdir()
    for raw in (b"\xff", b"x" * 4096, b"", (_HARD_RUN + "\n\n").encode()):
        marker.write_bytes(raw)
        with pytest.raises(labclient.LabClientError):
            labclient._open_runs(root)


def test_lr_hard_unreadable_marker_fails_closed(tmp_path, monkeypatch):
    root, logs = _hard_project(tmp_path, monkeypatch)
    marker = logs / (LAB + ".open")
    marker.chmod(0)
    try:
        with pytest.raises(labclient.LabClientError):
            labclient._open_runs(root)
    finally:
        marker.chmod(0o600)


def test_lr_hard_log_refuses_linked_ancestors_and_files(tmp_path, monkeypatch):
    root, logs = _hard_project(tmp_path, monkeypatch)
    target = tmp_path / "outside"
    target.write_text("unchanged")
    path = logs / (_HARD_RUN + ".log")
    path.symlink_to(target)
    with pytest.raises(labclient.LabClientError):
        labclient.log(root, _HARD_RUN, ["safe"])
    assert target.read_text() == "unchanged"
    path.unlink()
    os.link(target, path)
    with pytest.raises(labclient.LabClientError):
        labclient.log(root, _HARD_RUN, ["safe"])
    path.unlink()
    saved = root / "saved-notes"
    (root / "notes").rename(saved)
    (root / "notes").symlink_to(saved, target_is_directory=True)
    with pytest.raises(labclient.LabClientError):
        labclient.log(root, _HARD_RUN, ["safe"])
    assert not (saved / "lab-runs" / path.name).exists()


def test_lr_hard_log_pins_directory_during_swap(tmp_path, monkeypatch):
    import builtins

    root, logs = _hard_project(tmp_path, monkeypatch)
    outside = tmp_path / "outside"
    outside.mkdir()
    moved = root / "saved"
    real_open = os.open
    real_builtin_open = builtins.open
    swapped = False

    def swap(path, flags, *args, **kwargs):
        nonlocal swapped
        if str(path).endswith(_HARD_RUN + ".log") and not swapped:
            swapped = True
            logs.rename(moved)
            logs.symlink_to(outside, target_is_directory=True)
        return real_open(path, flags, *args, **kwargs)

    def open_path(path, *args, **kwargs):
        nonlocal swapped
        if str(path).endswith(_HARD_RUN + ".log") and not swapped:
            swapped = True
            logs.rename(moved)
            logs.symlink_to(outside, target_is_directory=True)
        return real_builtin_open(path, *args, **kwargs)

    monkeypatch.setattr(os, "open", swap)
    monkeypatch.setattr(builtins, "open", open_path)
    # Refusal is safe too, but a successful append must use the already opened directory.
    try:
        labclient.log(root, _HARD_RUN, ["safe"])
    except labclient.LabClientError:
        pass
    assert swapped
    assert not list(outside.iterdir())


def test_lr_hard_scope_refuses_link_unreadable_and_oversized_files(tmp_path, monkeypatch):
    root, _ = _hard_project(tmp_path, monkeypatch)
    scope = root / "SCOPE.md"
    target = tmp_path / "outside"
    target.write_text("- code: %s\n" % PROJECT)
    scope.unlink()
    scope.symlink_to(target)
    with pytest.raises(labclient.LabClientError):
        labclient._code(root)
    scope.unlink()
    scope.write_bytes(b"\xff")
    with pytest.raises(labclient.LabClientError):
        labclient._code(root)
    scope.write_text("x" * (1024 * 1024 + 1))
    with pytest.raises(labclient.LabClientError):
        labclient._code(root)
    scope.write_text("- code: %s\n" % PROJECT)
    scope.chmod(0)
    try:
        with pytest.raises(labclient.LabClientError):
            labclient._code(root)
    finally:
        scope.chmod(0o600)


def test_lr_hard_project_fallback_never_echoes_folder_names(tmp_path, monkeypatch, capsys):
    from tests import fixtures

    root = tmp_path / fixtures.PERSON_FORMS[0]
    root.mkdir()
    (root / "SCOPE.md").write_text("# no project code\n")
    monkeypatch.chdir(root)
    seen = []
    monkeypatch.setattr(labclient, "_request", lambda *a: seen.append(a) or {"ok": True})
    assert labclient.run_main(["status"]) == 2
    assert not seen
    assert root.name not in str(capsys.readouterr())


def test_lr_hard_resources_refuse_links_and_do_not_touch_old_temp(tmp_path, monkeypatch):
    root, _ = _hard_project(tmp_path, monkeypatch)
    outside = tmp_path / "outside"
    outside.write_text("untouched\n")
    resources = root / "RESOURCES.md"
    resources.symlink_to(outside)
    with pytest.raises(labclient.LabClientError):
        labclient.keep_row(root, _HARD_RUN, LAB, _hard_kept())
    assert outside.read_text() == "untouched\n"
    resources.unlink()
    (root / ".RESOURCES.md.tmp").symlink_to(outside)
    assert labclient.keep_row(root, _HARD_RUN, LAB, _hard_kept()) == "rule-1"
    assert outside.read_text() == "untouched\n"
    assert (root / ".RESOURCES.md.tmp").is_symlink()


def test_lr_hard_kept_cells_cannot_forge_resource_rows(tmp_path, monkeypatch):
    root, _ = _hard_project(tmp_path, monkeypatch)
    row = _hard_kept("id|forged", label="vpc rules\n| forged |", region="eu-de|bad",
                     reason="retain\n| injected | id | bad | - | - | - | live | note |")
    labclient.keep_row(root, _HARD_RUN, LAB, row)
    text = (root / "RESOURCES.md").read_text()
    kept = [line for line in text.splitlines() if "kept from lab run" in line]
    assert len(kept) == 1 and kept[0].count("|") == 9
    assert not any(line.startswith("| injected |") for line in text.splitlines())
    # A blank label is legal fallback data, never an IndexError.
    assert labclient.keep_row(root, _HARD_RUN, LAB, _hard_kept("id-two", label="   "))


def test_lr_hard_resource_dedup_uses_id_column_only(tmp_path, monkeypatch):
    root, _ = _hard_project(tmp_path, monkeypatch)
    (root / "RESOURCES.md").write_text(
        "| rule-1 | older-id | vpc rules | eu-de | - | - | live | resource-a |\n")
    assert labclient.keep_row(root, _HARD_RUN, LAB, _hard_kept()) == "rule-2"
    text = (root / "RESOURCES.md").read_text()
    assert "| rule-2 | resource-a |" in text
    assert labclient.keep_row(root, _HARD_RUN, LAB, _hard_kept()) == "rule-2"
    assert (root / "RESOURCES.md").read_text() == text


def test_lr_hard_concurrent_keeps_preserve_both_rows(tmp_path, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor

    root, _ = _hard_project(tmp_path, monkeypatch)
    (root / "RESOURCES.md").write_text("# resources\n")
    first = threading.Event()
    second = threading.Event()
    published = threading.Event()
    release = threading.Event()
    real_replace = os.replace
    count = 0
    guard = threading.Lock()

    def replace(*args, **kwargs):
        nonlocal count
        with guard:
            count += 1
            n = count
        if n == 1:
            first.set()
            assert release.wait(5)
        else:
            published.set()
        return real_replace(*args, **kwargs)

    def other():
        second.set()
        return labclient.keep_row(root, _HARD_RUN, LAB2, _hard_kept("resource-b"))

    monkeypatch.setattr(os, "replace", replace)
    with ThreadPoolExecutor(2) as pool:
        a = pool.submit(labclient.keep_row, root, _HARD_RUN, LAB, _hard_kept())
        assert first.wait(5)
        b = pool.submit(other)
        assert second.wait(5)
        # The first writer holds publication; a second writer must not publish stale contents.
        overlapped = published.wait(0.2)
        release.set()
        handles = {a.result(timeout=5), b.result(timeout=5)}
    text = (root / "RESOURCES.md").read_text()
    assert not overlapped
    assert handles == {"rule-1", "rule-2"}
    assert "resource-a" in text and "resource-b" in text


def test_lr_hard_failed_resource_publish_preserves_original_and_cleans_temp(tmp_path, monkeypatch):
    root, _ = _hard_project(tmp_path, monkeypatch)
    path = root / "RESOURCES.md"
    path.write_text("# original\n")
    private = "pass" + "word-value"

    def fail(*args, **kwargs):
        raise OSError(private)

    monkeypatch.setattr(os, "replace", fail)
    with pytest.raises(labclient.LabClientError) as exc:
        labclient.keep_row(root, _HARD_RUN, LAB, _hard_kept())
    assert private not in str(exc.value)
    assert path.read_text() == "# original\n"
    assert not list(root.glob(".lab-*"))
    assert not (root / ".RESOURCES.md.tmp").exists()


def test_lr_hard_end_preserves_a_newer_marker(tmp_path, monkeypatch):
    root, logs = _hard_project(tmp_path, monkeypatch)

    def request(*args):
        (logs / (LAB + ".open")).write_text(_HARD_NEXT + "\n")
        return {"ok": True, "run": _HARD_RUN, "created": 0}

    monkeypatch.setattr(labclient, "_request", request)
    assert labclient._end(root, PROJECT, LAB) == 0
    assert (logs / (LAB + ".open")).read_text() == _HARD_NEXT + "\n"


def test_lr_hard_start_end_are_serialized(tmp_path, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor

    root, logs = _hard_project(tmp_path, monkeypatch)
    in_end = threading.Event()
    release = threading.Event()
    attempted = threading.Event()
    in_start = threading.Event()

    def request(obj, *args):
        if obj["action"] == "end":
            in_end.set()
            assert release.wait(5)
            return {"ok": True, "run": _HARD_RUN, "created": 0}
        in_start.set()
        return {"ok": True, "run": _HARD_NEXT}

    def start():
        attempted.set()
        return labclient._start(root, PROJECT, LAB, "")

    monkeypatch.setattr(labclient, "_request", request)
    with ThreadPoolExecutor(2) as pool:
        a = pool.submit(labclient._end, root, PROJECT, LAB)
        assert in_end.wait(5)
        b = pool.submit(start)
        assert attempted.wait(5)
        overlapped = in_start.wait(0.1)
        release.set()
        assert a.result(timeout=5) == b.result(timeout=5) == 0
    assert not overlapped
    assert (logs / (LAB + ".open")).read_text() == _HARD_NEXT + "\n"


def test_lr_hard_service_errors_never_echo_values(tmp_path, monkeypatch):
    root, logs = _hard_project(tmp_path, monkeypatch)
    private = "pass" + "word-value"
    monkeypatch.setattr(labclient, "_request", lambda *a: {"ok": False, "error": "rejected " + private})
    operations = [lambda: labclient.call("GET", "iam", "/v3/users"),
                  lambda: labclient._start(root, PROJECT, LAB, ""),
                  lambda: labclient._keep(root, PROJECT, LAB, "1", "retain"),
                  lambda: labclient._end(root, PROJECT, LAB),
                  lambda: labclient._status(PROJECT, root),
                  lambda: labclient.obs_status(), lambda: labclient.obs_abort("job-1")]
    for operation in operations:
        with pytest.raises(labclient.LabClientError) as exc:
            operation()
        assert private not in str(exc.value)
    assert private not in "".join(f.read_text() for f in logs.glob("*.log"))


def test_lr_hard_logs_rows_and_cli_withhold_names_and_secrets(home, tmp_path, monkeypatch, capsys):
    from tests import fixtures

    root, logs = _hard_project(tmp_path, monkeypatch)
    name = fixtures.PERSON_FORMS[0]
    secret = "invented-" + "pass" + "word-4711"
    text = name + " " + "pass" + "word: " + secret
    labclient.log(root, _HARD_RUN, [text])
    labclient.keep_row(root, _HARD_RUN, LAB, _hard_kept(reason=text))
    monkeypatch.setattr(labclient, "_request", lambda *a: {"ok": True, "runs": [
        {"run": _HARD_RUN, "project": PROJECT, "tenant": LAB, "state": "open", "left": [
            {"state": "live", "label": "vpc rules", "name": text}]}]})
    assert labclient.run_main(["status"]) == 0
    written = (logs / (_HARD_RUN + ".log")).read_text() + (root / "RESOURCES.md").read_text()
    written += str(capsys.readouterr())
    assert name not in written and secret not in written
    assert "withheld" in written or "secret" in written


def test_lr_hard_failed_name_check_withholds_text(home, tmp_path, monkeypatch):
    from awb import check

    root, logs = _hard_project(tmp_path, monkeypatch)
    private = "unverified-" + "value"

    def unavailable(*args):
        raise check.CheckUnavailable(private)

    monkeypatch.setattr(check, "check_text", unavailable)
    labclient.log(root, _HARD_RUN, [private])
    assert private not in (logs / (_HARD_RUN + ".log")).read_text()


def test_lr_hard_cli_controls_and_malformed_status_are_safe(tmp_path, monkeypatch, capsys):
    _hard_project(tmp_path, monkeypatch)
    monkeypatch.setattr(labclient, "_request", lambda *a: {"ok": True, "runs": [
        {"run": _HARD_RUN, "project": PROJECT, "tenant": LAB, "state": "open\x1b[2J"}]})
    assert labclient.run_main(["status"]) == 0
    assert "\x1b" not in capsys.readouterr().out
    monkeypatch.setattr(labclient, "_request", lambda *a: {"ok": True, "runs": [None]})
    assert labclient.run_main(["status"]) == 2
    assert "Traceback" not in capsys.readouterr().err


def test_lr_hard_wire_requires_boolean_ok_and_complete_json_line(monkeypatch):
    for raw in (b'{"ok":"false"}\n', b'{"ok":1}\n', b'{"ok":true}', b'{}\n', b'[]\n', b'\xff\n'):
        sock = _hard_socket(monkeypatch, [raw])
        with pytest.raises(labclient.LabClientError):
            labclient._request({"op": "lab_run"})
        assert sock.closed


def test_lr_hard_wire_bounds_depth_and_hides_decode_errors(monkeypatch):
    private = "pass" + "word-value"
    sock = _hard_socket(monkeypatch, [('{"ok":true,"data":' + "[" * 10000 + '"' + private + '"' + "]" * 10000 + "}\n").encode()])
    with pytest.raises(labclient.LabClientError) as exc:
        labclient._request({"op": "lab_run"})
    assert private not in str(exc.value) and sock.closed


def test_lr_hard_wire_deadline_covers_all_chunks(monkeypatch):
    sock = _hard_socket(monkeypatch, [b'{"ok":', b'true}\n'])
    ticks = iter([0.0, 0.5, 2.0])
    monkeypatch.setattr(labclient, "TIMEOUT", 1.0)
    monkeypatch.setattr(time, "monotonic", lambda: next(ticks))
    with pytest.raises(labclient.LabClientError):
        labclient._request({"op": "lab_run"})
    assert sock.closed


def test_lr_hard_obs_url_rejects_controls_and_trailing_newline():
    for url in ("obs://bucket/key\n", "obs://bucket/key\x00tail", "obs://buck\net/key", "obs://bucket/key\rtail"):
        with pytest.raises(labclient.LabClientError):
            labclient._object(url)
    assert labclient._object("obs://bucket/a/b c") == ("bucket", "a/b c")


def test_lr_hard_transfer_refuses_links_and_nonregular_files(tmp_path, monkeypatch):
    root, logs = _hard_project(tmp_path, monkeypatch)
    target = tmp_path / "outside"
    target.write_text("untouched")
    link = root / "linked"
    link.symlink_to(target)
    seen = []
    monkeypatch.setattr(labclient, "_request", lambda *a: seen.append(a) or {"ok": True, "job": "job-1"})
    for path in (link, root):
        with pytest.raises(labclient.LabClientError):
            labclient.obs_put(path, "obs://bucket/key")
    link.unlink()
    os.mkfifo(link)
    # Keep a reader/writer open so the old implementation cannot hang on the FIFO.
    hold = os.open(link, os.O_RDWR | os.O_NONBLOCK)
    try:
        with pytest.raises(labclient.LabClientError):
            labclient.obs_put(link, "obs://bucket/key")
        with pytest.raises(labclient.LabClientError):
            labclient.obs_get("obs://bucket/key", link)
    finally:
        os.close(hold)
    link.unlink()
    path = logs / (_HARD_RUN + ".log")
    path.symlink_to(target)
    with pytest.raises(labclient.LabClientError):
        labclient.obs_put(target, "obs://bucket/key")
    assert not seen and target.read_text() == "untouched"


def test_lr_hard_transfer_pins_parent_during_swap(tmp_path, monkeypatch):
    root, _ = _hard_project(tmp_path, monkeypatch)
    dest = root / "downloads"
    dest.mkdir()
    saved = root / "saved"
    outside = tmp_path / "outside"
    outside.mkdir()
    target = dest / "image"
    real_open = os.open
    swapped = False

    def swap(path, flags, *args, **kwargs):
        nonlocal swapped
        if str(path).endswith("image") and not swapped:
            swapped = True
            dest.rename(saved)
            dest.symlink_to(outside, target_is_directory=True)
        return real_open(path, flags, *args, **kwargs)

    def request(obj, root, fds):
        os.write(fds[0], b"download")
        return {"ok": True, "job": "job-1"}

    monkeypatch.setattr(os, "open", swap)
    monkeypatch.setattr(labclient, "_request", request)
    labclient.obs_get("obs://bucket/key", target)
    assert swapped and not list(outside.iterdir())
    assert (saved / "image").read_bytes() == b"download"


def test_lr_hard_transfer_refusal_does_not_unlink_replacement(tmp_path, monkeypatch):
    root, _ = _hard_project(tmp_path, monkeypatch)
    target = root / "download"

    def request(*args):
        target.unlink()
        target.write_text("another writer")
        return {"ok": False, "error": "refused"}

    monkeypatch.setattr(labclient, "_request", request)
    with pytest.raises(labclient.LabClientError):
        labclient.obs_get("obs://bucket/key", target)
    assert target.read_text() == "another writer"


def test_lr_hard_missing_and_unreadable_transfer_errors_hide_paths(tmp_path, monkeypatch):
    root, _ = _hard_project(tmp_path, monkeypatch)
    private = "pass" + "word-value"
    target = root / private
    with pytest.raises(labclient.LabClientError) as exc:
        labclient.obs_put(target, "obs://bucket/key")
    assert private not in str(exc.value)
    target.write_text("content")
    target.chmod(0)
    try:
        with pytest.raises(labclient.LabClientError) as exc:
            labclient.obs_put(target, "obs://bucket/key")
        assert private not in str(exc.value)
    finally:
        target.chmod(0o600)


def test_lr_hard_marker_publication_is_atomic(tmp_path, monkeypatch):
    root, logs = _hard_project(tmp_path, monkeypatch)
    monkeypatch.setattr(labclient, "_request", lambda *a: {"ok": True, "run": _HARD_NEXT})
    real_replace = os.replace
    snapshots = []

    def replace(*args, **kwargs):
        snapshots.append((logs / (LAB + ".open")).read_text())
        return real_replace(*args, **kwargs)

    monkeypatch.setattr(os, "replace", replace)
    assert labclient._start(root, PROJECT, LAB, "") == 0
    assert snapshots == [_HARD_RUN + "\n"]
    assert (logs / (LAB + ".open")).read_text() == _HARD_NEXT + "\n"


def test_lr_hard_wire_rejects_duplicate_keys_and_nonfinite_numbers(monkeypatch):
    for raw in (b'{"ok":false,"ok":true}\n', b'{"ok":true,"size":NaN}\n',
                b'{"ok":true,"size":Infinity}\n', b'{"ok":true,"size":1e999}\n'):
        sock = _hard_socket(monkeypatch, [raw])
        with pytest.raises(labclient.LabClientError):
            labclient._request({"op": "lab_obs"})
        assert sock.closed


def test_lr_hard_stored_errors_never_echo_bare_values(tmp_path, monkeypatch, capsys):
    root, _ = _hard_project(tmp_path, monkeypatch)
    private = "pass" + "word-value"
    item = {"state": "failed", "label": "vpc rules", "error": "rejected " + private}
    monkeypatch.setattr(labclient, "_request", lambda *a: {"ok": True, "runs": [
        {"run": _HARD_RUN, "project": PROJECT, "tenant": LAB, "state": "ended", "left": [item]}]})
    assert labclient.run_main(["status"]) == 0
    assert private not in str(capsys.readouterr())
    monkeypatch.setattr(labclient, "_request", lambda *a: {"ok": True, "run": _HARD_RUN, "failed": [item]})
    assert labclient._end(root, PROJECT, LAB) == 4
    assert private not in str(capsys.readouterr())
    monkeypatch.setattr(labclient, "_request", lambda *a: {"ok": True, "jobs": [
        {"id": "job-1", "action": "put", "bucket": "bucket", "key": "key", "state": "failed", "error": private}]})
    assert labclient.obs_main(["status"]) == 0
    assert private not in str(capsys.readouterr())


def test_lr_hard_obs_malformed_rows_return_safe_refusal(tmp_path, monkeypatch, capsys):
    _hard_project(tmp_path, monkeypatch)
    private = "pass" + "word-value"
    for row in (None, {}, {"size": private}, {"id": "job-1", "action": "get", "size": float("inf")}):
        monkeypatch.setattr(labclient, "_request", lambda *a: {"ok": True, "jobs": [row]})
        assert labclient.obs_main(["status"]) == 2
        output = str(capsys.readouterr())
        assert "Traceback" not in output and private not in output


def test_lr_hard_malformed_end_does_not_close_local_run(tmp_path, monkeypatch):
    root, logs = _hard_project(tmp_path, monkeypatch)
    for failed in ([{}], [None], "invalid"):
        monkeypatch.setattr(labclient, "_request", lambda *a: {"ok": True, "run": _HARD_RUN, "failed": failed})
        with pytest.raises(labclient.LabClientError):
            labclient._end(root, PROJECT, LAB)
        assert (logs / (LAB + ".open")).read_text() == _HARD_RUN + "\n"
        assert not (logs / (_HARD_RUN + ".log")).exists()


def test_lr_hard_download_destination_boundary_and_upload_descriptors(tmp_path, monkeypatch):
    root, _ = _hard_project(tmp_path, monkeypatch)
    scratch = tmp_path / "scratch-0"
    scratch.mkdir()
    monkeypatch.setattr(labclient, "SCRATCH", str(tmp_path / "scratch-%d"))
    monkeypatch.setattr(os, "getuid", lambda: 0)
    fds_seen = []

    def request(obj, project, fds):
        assert "path" not in obj and "file" not in obj
        fds_seen.extend(fds)
        if obj["action"] == "put":
            assert os.read(fds[0], 20) == b"source"
        else:
            os.write(fds[0], b"received")
        return {"ok": True, "job": "job-1"}

    monkeypatch.setattr(labclient, "_request", request)
    outside = tmp_path / "outside"
    outside.write_bytes(b"source")
    labclient.obs_put(outside, "obs://bucket/key")
    for folder in (root, scratch):
        target = folder / "download"
        labclient.obs_get("obs://bucket/key", target)
        assert target.read_bytes() == b"received"
    sibling = tmp_path / (PROJECT + "-other")
    sibling.mkdir()
    for target in (outside, sibling / "download", root / ".." / "escape", scratch / ".." / "escape"):
        with pytest.raises(labclient.LabClientError):
            labclient.obs_get("obs://bucket/key", target)
    assert outside.read_bytes() == b"source"
    for fd in fds_seen:
        with pytest.raises(OSError):
            os.fstat(fd)


def test_lr_hard_missing_parent_diagnostic_is_screened(home, tmp_path, monkeypatch):
    from tests import fixtures

    root, _ = _hard_project(tmp_path, monkeypatch)
    private = "invented-" + "value-4711"
    for name in (fixtures.PERSON_FORMS[0], "pass" + "word=" + private):
        with pytest.raises(labclient.LabClientError) as exc:
            labclient.obs_fetch("https://example.invalid/file", root / name / "image")
        assert name not in str(exc.value) and private not in str(exc.value)
    with pytest.raises(labclient.LabClientError, match=r"no such folder: .*/missing \(make it first, then fetch"):
        labclient.obs_fetch("https://example.invalid/file", root / "missing" / "image")


def test_lr_hard_unknown_job_diagnostic_never_echoes_argument(tmp_path, monkeypatch, capsys):
    _hard_project(tmp_path, monkeypatch)
    private = "pass" + "word-value"
    monkeypatch.setattr(labclient, "_request", lambda *a: {"ok": True, "jobs": []})
    assert labclient.obs_main(["status", private]) == 0
    assert private not in str(capsys.readouterr())
    with pytest.raises(labclient.LabClientError) as exc:
        labclient.obs_wait(private)
    assert private not in str(exc.value)


def test_lr_hard_explicit_project_and_marker_tenant_are_validated(tmp_path, monkeypatch, capsys):
    root, _ = _hard_project(tmp_path, monkeypatch)
    seen = []
    monkeypatch.setattr(labclient, "_request", lambda *a: seen.append(a) or {"ok": True, "runs": []})
    for project in ("../outside", PROJECT + "\n", "tcp-TOOLONG"):
        assert labclient.run_main(["status", "--project", project]) == 2
        assert project not in str(capsys.readouterr())
    for tenant in ("../outside", LAB + "\n", ""):
        with pytest.raises(labclient.LabClientError):
            labclient._marker(root, tenant)
    assert not seen


def test_lr_hard_pick_errors_withhold_tenant_names(tmp_path, monkeypatch):
    root, logs = _hard_project(tmp_path, monkeypatch)
    tenant = "private-" + "tenant"
    (logs / (tenant + ".open")).write_text(_HARD_NEXT + "\n")
    with pytest.raises(labclient.LabClientError) as exc:
        labclient._pick(root, None, None)
    assert tenant not in str(exc.value) and LAB not in str(exc.value)
    (logs / (tenant + ".open")).unlink()
    with pytest.raises(labclient.LabClientError) as exc:
        labclient._pick(root, tenant, None)
    assert tenant not in str(exc.value)


def test_lr_hard_start_checks_existing_marker_before_request(tmp_path, monkeypatch):
    root, logs = _hard_project(tmp_path, monkeypatch)
    marker = logs / (LAB + ".open")
    marker.write_text("invalid\n")
    seen = []
    monkeypatch.setattr(labclient, "_request", lambda *a: seen.append(a) or {"ok": True, "run": _HARD_NEXT})
    with pytest.raises(labclient.LabClientError):
        labclient._start(root, PROJECT, LAB, "")
    assert not seen and marker.read_text() == "invalid\n"


def test_lr_hard_wire_refuses_nonfinite_request_before_sending(monkeypatch):
    for number in (float("nan"), float("inf"), -float("inf")):
        sock = _hard_socket(monkeypatch, [b'{"ok":true}\n'])
        sent = []
        monkeypatch.setattr(sock, "sendall", sent.append)
        with pytest.raises(labclient.LabClientError):
            labclient._request({"op": "lab_call", "body": {"value": number}})
        assert not sent and sock.closed


def test_lr_hard_screening_handles_names_and_secrets_independently(home, tmp_path, monkeypatch, capsys):
    from tests import fixtures

    root, logs = _hard_project(tmp_path, monkeypatch)
    secret = "invented-" + "pass" + "word-4711"
    for value, text in ((fixtures.PERSON_FORMS[0], fixtures.PERSON_FORMS[0]),
                        (secret, "pass" + "word: " + secret)):
        labclient.log(root, _HARD_RUN, [text])
        labclient.keep_row(root, _HARD_RUN, LAB, _hard_kept(reason=text))
        monkeypatch.setattr(labclient, "_request", lambda *a: {"ok": True, "job": {"id": "job-1", "state": text}})
        assert labclient.obs_main(["abort", "job-1"]) == 0
        written = (logs / (_HARD_RUN + ".log")).read_text() + (root / "RESOURCES.md").read_text()
        assert value not in written + str(capsys.readouterr())
        (root / "RESOURCES.md").unlink()


def test_lr_hard_screening_fails_closed_for_work_user_and_checker_errors(tmp_path, monkeypatch):
    from awb import check

    root, logs = _hard_project(tmp_path, monkeypatch)
    private = "unverified-" + "value"
    assert not config.paths().register.parent.exists()
    monkeypatch.setattr(config, "is_work_user", lambda: True)
    for error in (check.CheckUnavailable(private), RuntimeError(private)):
        def unavailable(*args):
            raise error

        monkeypatch.setattr(check, "check_text", unavailable)
        labclient.log(root, _HARD_RUN, [private])
        assert private not in (logs / (_HARD_RUN + ".log")).read_text()


def test_lr_hard_screening_withholds_unicode_controls_and_invalid_text(tmp_path, monkeypatch):
    root, logs = _hard_project(tmp_path, monkeypatch)
    for control in ("\x00", "\x7f", "\x85", "\u202e", "\u2066", "\ud800"):
        labclient.log(root, _HARD_RUN, ["unsafe" + control + "text"])
    assert (logs / (_HARD_RUN + ".log")).read_text() == "[withheld]\n\n" * 6


def test_lr_hard_resource_placeholders_and_huge_handles_do_not_deduplicate(tmp_path, monkeypatch):
    root, _ = _hard_project(tmp_path, monkeypatch)
    path = root / "RESOURCES.md"
    for rid in ("-", "[withheld]", "[secret]"):
        path.write_text("| rule-7 | %s | vpc rules | eu-de | - | - | live | old |\n" % rid)
        assert labclient.keep_row(root, _HARD_RUN, LAB, _hard_kept(rid)) == "rule-8"
        assert len(path.read_text().splitlines()) == 2
    path.write_text("| rule-%s | old | vpc rules | eu-de | - | - | live | old |\n" % ("9" * 5000))
    assert labclient.keep_row(root, _HARD_RUN, LAB, _hard_kept()) == "rule-1"


def test_lr_hard_log_blocks_are_serialized(tmp_path, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor

    root, logs = _hard_project(tmp_path, monkeypatch)
    first_half = threading.Event()
    attempted = threading.Event()
    second_write = threading.Event()
    release = threading.Event()
    real_fdopen = os.fdopen

    class SplitWriter:
        def __init__(self, fd, *args, **kwargs):
            self.file = real_fdopen(fd, *args, **kwargs)

        def __enter__(self):
            return self

        def __exit__(self, *args):
            self.file.close()

        def fileno(self):
            return self.file.fileno()

        def write(self, text):
            if text.startswith("first"):
                self.file.write(text[:3])
                self.file.flush()
                first_half.set()
                assert release.wait(5)
                self.file.write(text[3:])
            else:
                second_write.set()
                self.file.write(text)

    def other():
        attempted.set()
        labclient.log(root, _HARD_RUN, ["second"])

    monkeypatch.setattr(os, "fdopen", SplitWriter)
    with ThreadPoolExecutor(2) as pool:
        a = pool.submit(labclient.log, root, _HARD_RUN, ["first"])
        try:
            assert first_half.wait(5)
            b = pool.submit(other)
            assert attempted.wait(5)
            overlapped = second_write.wait(0.2)
        finally:
            release.set()
        a.result(timeout=5)
        b.result(timeout=5)
    assert not overlapped
    assert (logs / (_HARD_RUN + ".log")).read_text() == "first\n\nsecond\n\n"


def test_lr_hard_end_rejects_empty_nonlist_resource_rows(tmp_path, monkeypatch):
    root, logs = _hard_project(tmp_path, monkeypatch)
    marker = logs / (LAB + ".open")
    for key in ("failed", "removed", "kept"):
        for value in ({}, "", 0, False):
            answer = {"ok": True, "run": _HARD_RUN, key: value}
            monkeypatch.setattr(labclient, "_request", lambda *a: answer)
            with pytest.raises(labclient.LabClientError):
                labclient._end(root, PROJECT, LAB)
            assert marker.read_text() == _HARD_RUN + "\n"
            assert not (logs / (_HARD_RUN + ".log")).exists()
