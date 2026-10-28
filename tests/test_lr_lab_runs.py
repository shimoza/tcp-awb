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
            assert not answer["ok"] and answer["error"].startswith("refused: %s is no lab tenant" % tenant), answer
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
