"""Task TW (B-031): one switch per customer tenant between read-only and write (awb tenant mode, the tenant_write line
of keys.conf, awb/tenantmode.py; the key service of awb/tcp/keys.py, the lab runs of awb/tcp/labadmin.py and the lab
obs jobs of awb/tcp/labobs.py on such a tenant; awb owner status and awb tenant list).

No socket to 127.0.0.1: the API of a call is a stand-in of cloud.Client.request that records which key signed it, and
IAM and OBS of a lab run are the in-memory FakeObsLab of tests/lab_fake.py. The key service listens on Unix sockets of
a short folder. Every alias, login, password and key here is invented and built from pieces at run time.
"""
from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from awb import config, ownerwait, tenantmode
from awb.tcp import cloud, keys, labadmin, labobs, readonly, tenants, xchg
from tests.lab_fake import FakeObsLab
from tests.test_keys import short_dir

KEYED = "cust-1"           # a read key and a write key
LOGGED = "cust-2"          # a read key and a write login
BARE = "cust-3"            # a read key only
DOMAIN_ID = "d" * 32
PROJECT = "tcp-ab2c"
OTHER = "tcp-zz99"
BUCKET = PROJECT + "-lab-1"
SESSION = os.getuid() + 1
READ_AK = "AKREAD" + "INVENTED" + "ONE"
WRITE_AK = "AKWRITE" + "INVENTED" + "ONE"
WRITE_SK = "sk-" + "write-" + "invented-one"
WRITE_LOGIN = {"domain": "write-account-" + "fixture", "user": "writer-of-" + "fixture",
               "password": "-".join(("Invented", "Write", "4712"))}
STORE = {"%s/ak" % a: READ_AK + a[-1] for a in (KEYED, LOGGED, BARE)}
STORE.update({"%s/sk" % a: "sk-read-" + a for a in (KEYED, LOGGED, BARE)})
STORE.update({"%s/write/ak" % KEYED: WRITE_AK, "%s/write/sk" % KEYED: WRITE_SK})
STORE.update({"%s/write/login/%s" % (LOGGED, k): v for k, v in WRITE_LOGIN.items()})
READONLY = ", ".join("%s:key:%s:eu-de" % (a, DOMAIN_ID) for a in (KEYED, LOGGED, BARE))
NOTE = "mail OK of 2026-11-02"
WRITE_LINE = "%s:2026-11-03:%s, %s:2026-11-03:%s, %s:2026-11-03:" % (KEYED, NOTE, LOGGED, NOTE, BARE)
PART = 256 * 1024


class Api:
    """The stand-in of cloud.Client.request: records the key or token and the call, answers 200 (201 for a POST)."""

    def __init__(self):
        self.calls: list[dict] = []

    def method(self):
        api = self

        def request(client, method, service, path, *, query=None, body=None, headers=None):
            api.calls.append({"ak": client.keys.ak, "token": client.token, "method": method, "service": service,
                              "path": path, "body": body})
            return cloud.Response(201 if method == "POST" else 200, {"done": True, "echo": client.keys.sk}, "", 10)

        return request


def _service(d: Path, tmp: Path, name: str, settings: dict) -> keys.Service:
    svc = keys.Service(d / ("admin-%s.sock" % name), d / ("call-%s.sock" % name), endpoint="https://lab.invalid",
                       log_dir=tmp / "log",
                       paths_fn=lambda: SimpleNamespace(vault=tmp / "vault", projects_root=tmp / "projects"))
    svc._registered = lambda: {}
    svc._active_project = lambda code: code in (PROJECT, OTHER)
    rows = {c: SimpleNamespace(code=c, kind="project", state="active", customer=None) for c in (PROJECT, OTHER)}
    svc._project_row = lambda code: rows.get(code)
    svc._granted = lambda code: code in rows
    svc._console_uid = lambda: None
    svc.start()
    unlock(svc, settings)
    return svc


def unlock(svc, settings: dict) -> dict:
    return keys.unlock(svc.admin_path, sorted(STORE), reader=lambda e: STORE[e], settings=settings)


@pytest.fixture
def world(tmp_path, monkeypatch):
    fake = FakeObsLab(WRITE_LOGIN)
    api = Api()
    monkeypatch.setattr(cloud.Client, "request", api.method())
    monkeypatch.setattr(labadmin, "_http", fake.http)
    monkeypatch.setattr(labobs, "_stream", fake.stream)
    monkeypatch.setattr(labobs, "PAUSES", (0.0, 0.0, 0.0, 0.0))
    monkeypatch.setattr(labobs, "PART_SIZE", PART)
    monkeypatch.setattr(labobs, "STOP_WAIT", 5.0)
    monkeypatch.setattr(labadmin, "END_PAUSE", 0.0)
    monkeypatch.setattr(config, "is_work_user", lambda: False)
    p = SimpleNamespace(vault=tmp_path / "vault", projects_root=tmp_path / "projects")
    for alias in (KEYED, LOGGED, BARE):
        readonly.grant(p, alias, PROJECT)
    folder = tmp_path / "projects" / PROJECT
    (folder / "notes" / "lab-runs").mkdir(parents=True)
    with short_dir() as d:
        w = SimpleNamespace(fake=fake, api=api, folder=folder, tmp=tmp_path, p=p, services=[])

        def service(name="a", settings=None):
            s = _service(d, tmp_path, name, {"readonly": READONLY, "tenant_write": WRITE_LINE}
                         if settings is None else settings)
            # the IAM of a login in a call: a token named after the user, never a socket
            s.tokens.fetch = lambda login, domain_id, region, endpoint=None, project_id=None: (
                "tok-" + login["user"], "e" * 32, 86400.0)
            w.services.append(s)
            return s

        w.service = service
        w.svc = service()
        try:
            yield w
        finally:
            for s in w.services:
                s.stop()


def call(w, method, path="/v1/{project_id}/vpcs", *, tenant=KEYED, project=PROJECT, service="vpc", body=None,
         uid=SESSION, svc=None, **extra) -> dict:
    req = dict({"op": "call", "tenant": tenant, "method": method, "service": service, "path": path,
                "project": project, "body": body}, **extra)
    return (svc or w.svc)._call_socket(req, uid, caller=project if uid != os.getuid() else keys._NOT_ASKED)


def lab(w, op, *, tenant=LOGGED, project=PROJECT, **extra) -> dict:
    return w.svc._call_socket(dict({"op": op, "tenant": tenant, "project": project}, **extra), SESSION,
                              caller=project)


def lab_obs(w, action, run, file, *, tenant=LOGGED, key="images/disk.vhdx") -> dict:
    fds = [os.open(file, os.O_RDONLY),
           os.open(w.folder / "notes" / "lab-runs" / ("%s.log" % run), os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)]
    try:
        return w.svc._call_socket({"op": "lab_obs", "action": action, "tenant": tenant, "project": PROJECT, "run": run,
                                   "bucket": BUCKET, "key": key}, SESSION, caller=PROJECT, fds=fds)
    finally:
        for fd in fds:
            os.close(fd)


def job_record(w, job: str) -> dict:
    return json.loads((labobs.jobs_dir(w.p) / ("%s.json" % job)).read_text(encoding="utf-8"))


def a_file(w, n=PART * 4 + 99) -> Path:
    path = w.folder / "disk.vhdx"
    path.write_bytes(os.urandom(n))
    return path


def start_run(w, tenant=LOGGED) -> str:
    a = lab(w, "lab_run", tenant=tenant, action="start")
    assert a["ok"], a
    return a["run"]


def log_text(w) -> str:
    return "".join(f.read_text(encoding="utf-8") for f in (w.tmp / "log").glob("cloud-*.tsv"))


def read_only(w, alias=KEYED) -> dict:
    return keys.request(w.svc.admin_path, {"op": "tenant_readonly", "tenant": alias})


class Store:
    def __init__(self, names):
        self.names = set(names)

    def has(self, entry: str) -> bool:
        return entry in self.names


@pytest.fixture
def owner_conf(tmp_path, monkeypatch):
    """keys.conf of the owner with the three customer tenants, none in write mode; no assistant session."""
    conf = tmp_path / "keys.conf"
    conf.write_text("readonly = %s\n" % READONLY, encoding="utf-8")
    monkeypatch.setenv("AWB_KEYS_CONF", str(conf))
    monkeypatch.delenv("CLAUDECODE", raising=False)
    monkeypatch.delenv("CLAUDE_CODE_ENTRYPOINT", raising=False)
    monkeypatch.setattr(config, "is_work_user", lambda: False)
    return conf


WRITE_STORE = Store(["awb/tenant/%s/%s" % (KEYED, k) for k in ("ak", "sk", "write/ak", "write/sk")])


# --------------------------------------------------------------------------- read-only stays as it was


def test_tw_a_read_only_tenant_refuses_every_write_as_before(world):
    w = world
    svc = w.service("ro", {"readonly": READONLY})
    for method in ("POST", "PUT", "PATCH", "DELETE"):
        a = call(w, method, svc=svc, body={"vpc": {"name": "x"}} if method != "DELETE" else None)
        assert not a["ok"] and a["error"] == readonly.METHOD_LINE % KEYED, (method, a)
    a = call(w, "GET", svc=svc, body={"x": 1})
    assert a["error"] == readonly.BODY_LINE % KEYED
    assert not svc._call_socket({"op": "lab_run", "action": "start", "tenant": LOGGED, "project": PROJECT}, SESSION,
                                caller=PROJECT)["ok"]
    a = call(w, "GET", svc=svc)
    assert a["ok"] and a["status"] == 200
    assert [(c["method"], c["ak"]) for c in w.api.calls] == [("GET", STORE["%s/ak" % KEYED])]


def test_tw_a_lab_run_on_a_read_only_tenant_is_refused_with_the_mode_line(world):
    w = world
    svc = w.service("ro", {"readonly": READONLY})
    a = svc._call_socket({"op": "lab_run", "action": "start", "tenant": LOGGED, "project": PROJECT}, SESSION,
                         caller=PROJECT)
    assert a == {"ok": False, "error": readonly.WRITE_ONLY_LINE % (LOGGED, LOGGED)}


def test_tw_the_write_credential_is_held_only_in_write_mode(world):
    w = world
    svc = w.service("ro", {"readonly": READONLY, "tenant_write": "%s:2026-11-03:" % LOGGED})
    with svc.lock:
        assert set(svc.tenants[KEYED]["roles"]) == {"read"}
        assert svc.tenants[LOGGED]["write_login"] == WRITE_LOGIN
    status = keys.request(svc.admin_path, {"op": "status"})["tenants"]
    assert status[KEYED] == {"roles": ["read"], "secrets": [], "kind": "readonly", "auth": "key"}
    assert status[LOGGED]["mode"] == "write" and status[LOGGED]["write"] == "login"


def test_tw_a_lease_stays_refused_in_write_mode(world):
    a = world.svc._call_socket({"op": "lease", "tenant": KEYED, "project": PROJECT, "minutes": 15}, SESSION,
                               caller=PROJECT)
    assert a == {"ok": False, "error": readonly.LEASE_LINE % KEYED}


# --------------------------------------------------------------------------- write mode


def test_tw_write_mode_signs_a_put_and_a_post_of_a_granted_project_with_the_write_key(world):
    w = world
    a = call(w, "POST", body={"vpc": {"name": "tcp-ab2c-vpc"}})
    assert a["ok"] and a["status"] == 201, a
    b = call(w, "PUT", "/v1/{project_id}/vpcs/abc", body={"vpc": {"name": "tcp-ab2c-vpc-2"}})
    assert b["ok"] and b["status"] == 200, b
    c = call(w, "GET")
    assert c["ok"]
    assert [(x["method"], x["ak"], x["body"]) for x in w.api.calls] == [
        ("POST", WRITE_AK, {"vpc": {"name": "tcp-ab2c-vpc"}}), ("PUT", WRITE_AK, {"vpc": {"name": "tcp-ab2c-vpc-2"}}),
        ("GET", WRITE_AK, None)]
    assert WRITE_SK not in json.dumps([a, b, c]) and a["data"]["echo"] == keys.MASK


def test_tw_write_mode_refuses_a_project_without_the_grant_of_the_tenant(world):
    w = world
    a = call(w, "PUT", "/v1/{project_id}/vpcs/abc", project=OTHER, body={"vpc": {}})
    assert a == {"ok": False, "error": readonly.GRANT_LINE % (OTHER, KEYED, KEYED, OTHER)}
    b = call(w, "POST", project=OTHER, body={"vpc": {}})
    assert not b["ok"] and b["error"] == readonly.GRANT_LINE % (OTHER, KEYED, KEYED, OTHER)
    c = lab(w, "lab_run", tenant=LOGGED, project=OTHER, action="start")
    assert c == {"ok": False, "error": readonly.GRANT_LINE % (OTHER, LOGGED, LOGGED, OTHER)}
    assert w.api.calls == [] and labadmin.load_runs(w.p) == []


def test_tw_write_mode_keeps_iam_and_the_obs_buckets_of_other_projects_out(world):
    w = world
    a = call(w, "POST", "/v3/users", service="iam", body={"user": {}})
    assert not a["ok"] and "does not write to iam" in a["error"]
    b = call(w, "PUT", "/" + OTHER + "-data/x", service="obs")
    assert not b["ok"] and "named after the project" in b["error"]
    owner = call(w, "PUT", "/" + OTHER + "-data/x", service="obs", uid=os.getuid())
    assert owner == {"ok": False, "error": "an OBS write reaches only a bucket named after the project (%s-...)"
                     % PROJECT}
    c = call(w, "PUT", "/" + BUCKET + "/x", service="obs")
    assert c["ok"], c
    assert [(x["method"], x["service"], x["ak"]) for x in w.api.calls] == [("PUT", "obs", WRITE_AK)]


def test_tw_every_write_is_logged_without_a_key(world):
    w = world
    assert call(w, "POST", body={"vpc": {"name": "x"}})["ok"]
    text = log_text(w)
    row = [ln.split("\t") for ln in text.splitlines() if "\tPOST\t" in ln][0]
    assert row[2:9] == [KEYED, "write", "POST", "vpc", "eu-de", "201", PROJECT]
    assert WRITE_AK not in text and WRITE_SK not in text and WRITE_LOGIN["password"] not in text


def test_tw_write_mode_without_the_write_credential_is_refused_with_one_line(world):
    w = world
    a = call(w, "PUT", "/v1/{project_id}/vpcs/abc", tenant=BARE, body={"vpc": {}})
    assert not a["ok"] and a["error"].startswith(readonly.WRITE_CRED_LINE % (BARE, BARE)), a
    assert "awb/tenant/%s/write/ak" % BARE in a["error"]
    assert w.api.calls == []
    b = lab(w, "lab_run", tenant=BARE, action="start")
    assert not b["ok"] and b["error"].startswith(readonly.WRITE_CRED_LINE % (BARE, BARE))


# --------------------------------------------------------------------------- the owner's command


def test_tw_the_mode_command_records_the_day_and_the_note_and_switches_back(owner_conf):
    lines = readonly.set_mode(KEYED, "write", NOTE, store=WRITE_STORE, today="2026-11-03")
    assert lines == ["tenant %s: write since 2026-11-03 with the write key (%s): switched in keys.conf" % (KEYED, NOTE)]
    assert xchg.read_settings()["tenant_write"] == "%s:2026-11-03:%s" % (KEYED, NOTE)
    assert tenantmode.read() == {KEYED: {"since": "2026-11-03", "note": NOTE}}
    assert readonly.set_mode(KEYED, "readonly") == ["tenant %s: read-only: changed in keys.conf" % KEYED]
    assert "tenant_write" not in xchg.read_settings() and tenantmode.read() == {}
    assert "readonly = %s" % READONLY in owner_conf.read_text(encoding="utf-8")
    assert readonly.set_mode(KEYED, "readonly") == ["tenant %s: read-only already" % KEYED]
    with pytest.raises(readonly.ReadonlyError, match="only those have a mode"):
        readonly.set_mode("test-1", "write", store=WRITE_STORE)
    with pytest.raises(readonly.ReadonlyError, match="a note is"):
        readonly.set_mode(KEYED, "write", "a note, with a comma", store=WRITE_STORE)


def test_tw_the_mode_command_refuses_write_without_the_write_credential_in_pass(owner_conf):
    with pytest.raises(readonly.ReadonlyError) as err:
        readonly.set_mode(BARE, "write", store=Store(["awb/tenant/%s/ak" % BARE, "awb/tenant/%s/sk" % BARE]))
    assert str(err.value) == readonly.WRITE_CRED_LINE % (BARE, BARE)
    with pytest.raises(readonly.ReadonlyError) as err:
        readonly.set_mode(BARE, "write", store=Store(["awb/tenant/%s/write/login/user" % BARE]))
    entry = "awb/tenant/%s/write/login/" % BARE
    assert str(err.value).startswith("refused: %s has no whole write credential" % BARE)
    assert str(err.value).endswith(" %sdomain, %spassword is missing" % (entry, entry))
    assert tenantmode.read() == {}


@pytest.mark.parametrize("who", ["work user", "session"])
def test_tw_the_mode_command_is_refused_for_the_work_user_and_in_a_session(owner_conf, monkeypatch, capsys, who):
    if who == "work user":
        monkeypatch.setattr(config, "is_work_user", lambda: True)
    else:
        monkeypatch.setenv("CLAUDECODE", "1")
    for mode in ("write", "readonly"):
        with pytest.raises(readonly.ReadonlyError) as err:
            readonly.set_mode(KEYED, mode, store=WRITE_STORE)
        assert str(err.value) == readonly.MODE_OWNER_LINE
    assert tenants.main(["mode", KEYED, "write", "--note", NOTE]) == 1
    assert readonly.MODE_OWNER_LINE in capsys.readouterr().err
    assert tenantmode.read() == {}


# --------------------------------------------------------------------------- the switch back


def test_tw_the_switch_back_refuses_the_next_write_at_once(world):
    w = world
    assert call(w, "PUT", "/v1/{project_id}/vpcs/abc", body={"vpc": {}})["ok"]
    a = read_only(w)
    assert a == {"ok": True, "tenant": KEYED, "was": "write", "aborted": []}
    b = call(w, "PUT", "/v1/{project_id}/vpcs/abc", body={"vpc": {}})
    assert b == {"ok": False, "error": readonly.METHOD_LINE % KEYED}
    with w.svc.lock:
        assert "write" not in w.svc.tenants[KEYED]["roles"]
    assert call(w, "GET")["ok"]
    assert [(x["method"], x["ak"]) for x in w.api.calls] == [("PUT", WRITE_AK), ("GET", STORE["%s/ak" % KEYED])]
    assert call(w, "POST", tenant=LOGGED, body={"vpc": {}}, service="vpc")["ok"]   # the other tenant stays in write
    assert w.api.calls[-1]["token"] == "tok-" + WRITE_LOGIN["user"]


def test_tw_the_switch_back_aborts_an_open_lab_obs_job_with_a_clear_state(world):
    w = world
    run = start_run(w)
    assert lab(w, "lab_call", run=run, method="PUT", service="obs", path="/" + BUCKET)["ok"]
    release = threading.Event()
    w.fake.hold = {2: release}
    a = lab_obs(w, "put", run, a_file(w))
    assert a["ok"], a
    assert w.fake.holding.wait(10)
    stopper = threading.Thread(target=lambda: w.result.append(read_only(w, LOGGED)))
    w.result = []
    stopper.start()
    while not w.svc.obs_jobs.live[a["job"]].stop.is_set():
        time.sleep(0.01)
    release.set()
    stopper.join(30)
    assert w.result == [{"ok": True, "tenant": LOGGED, "was": "write", "aborted": [a["job"]]}]
    live = w.svc.obs_jobs.live.get(a["job"])
    if live is not None:
        live.thread.join(30)
    job = job_record(w, a["job"])
    assert job["state"] == "aborted" and job["error"] == labobs.READONLY_LINE % (LOGGED, LOGGED), job
    b = lab(w, "lab_call", run=run, method="PUT", service="obs", path="/" + BUCKET + "/x", body="x")
    assert b == {"ok": False, "error": readonly.WRITE_ONLY_LINE % (LOGGED, LOGGED)}
    status = lab(w, "lab_obs", action="status")
    assert [(r["id"], r["state"]) for r in status["jobs"]] == [(a["job"], "aborted")]


def test_tw_an_unlock_without_the_write_mode_aborts_the_jobs_too(world):
    w = world
    run = start_run(w)
    assert lab(w, "lab_call", run=run, method="PUT", service="obs", path="/" + BUCKET)["ok"]
    release = threading.Event()
    w.fake.hold = {2: release}
    a = lab_obs(w, "put", run, a_file(w))
    assert w.fake.holding.wait(10)
    locker = threading.Thread(target=lambda: unlock(w.svc, {"readonly": READONLY,
                                                           "tenant_write": "%s:2026-11-03:" % KEYED}))
    locker.start()
    while not w.svc.obs_jobs.live[a["job"]].stop.is_set():
        time.sleep(0.01)
    release.set()
    locker.join(30)
    live = w.svc.obs_jobs.live.get(a["job"])
    if live is not None:
        live.thread.join(30)
    assert job_record(w, a["job"])["state"] == "aborted"
    with w.svc.lock:
        assert "write_login" not in w.svc.tenants[LOGGED]


# --------------------------------------------------------------------------- lab runs on a customer tenant


def test_tw_lab_obs_works_on_a_customer_tenant_in_write_mode_with_the_run_log(world):
    w = world
    run = start_run(w)
    assert lab(w, "lab_call", run=run, method="PUT", service="obs", path="/" + BUCKET)["created"] == ["obs bucket"]
    path = a_file(w)
    a = lab_obs(w, "put", run, path)
    assert a["ok"], a
    w.svc.obs_jobs.live[a["job"]].thread.join(30) if a["job"] in w.svc.obs_jobs.live else None
    job = job_record(w, a["job"])
    assert job["state"] == "done" and job["tenant"] == LOGGED, job
    assert w.fake.buckets[BUCKET]["objects"]["images/disk.vhdx"]["data"] == path.read_bytes()
    log = (w.folder / "notes" / "lab-runs" / ("%s.log" % run)).read_text(encoding="utf-8")
    assert "obs job %s: put done" % a["job"] in log
    for ak, (sk, st) in w.fake.creds.items():
        assert ak not in log and sk not in log and st not in log
    assert WRITE_LOGIN["password"] not in log and WRITE_LOGIN["password"] not in log_text(w)


def test_tw_the_run_end_on_a_customer_tenant_deletes_only_what_the_run_made(world):
    w = world
    data = PROJECT + "-data"
    w.fake.buckets[data] = {"objects": {"old.txt": {"data": b"theirs", "meta": {}, "etag": '"o"'}}, "uploads": {}}
    run = start_run(w)
    a = lab(w, "lab_call", run=run, method="PUT", service="obs", path="/%s/old.txt" % data, body="ours")
    assert a["ok"] and a["created"] == [], a
    assert lab(w, "lab_call", run=run, method="PUT", service="obs", path="/%s/new.txt" % data,
               body="new")["created"] == ["obs object"]
    assert lab(w, "lab_call", run=run, method="PUT", service="obs", path="/" + BUCKET)["created"] == ["obs bucket"]
    end = lab(w, "lab_run", action="end")
    assert end["ok"] and end["failed"] == [], end
    assert sorted(r["label"] for r in end["removed"]) == ["obs bucket", "obs object"]
    assert BUCKET not in w.fake.buckets and set(w.fake.buckets[data]["objects"]) == {"old.txt"}
    assert [m for m, path, _q in w.fake.calls if m == "DELETE"] == ["DELETE", "DELETE"]


def test_tw_on_a_customer_tenant_a_put_outside_obs_and_a_server_delete_keep_what_was_there():
    put = labadmin.creates("PUT", "vpc", "/v1/p/vpcs/abc", {}, 200, {}, "eu-de", absent=True, customer=True)
    assert put == []
    assert labadmin.creates("PUT", "vpc", "/v1/p/vpcs/abc", {}, 200, {}, "eu-de", absent=True) != []
    assert labadmin.creates("PUT", "obs", "/tcp-ab2c-x", {}, 200, None, "eu-de", absent=True,
                            customer=True)[0]["label"] == "obs bucket"
    server = labadmin.creates("POST", "ecs", "/v1/p/cloudservers", {}, 200, {"serverIds": ["srv-1"]}, "eu-de",
                              customer=True)
    assert server[0]["delete"]["body"] == {"servers": [{"id": "srv-1"}], "delete_publicip": False,
                                           "delete_volume": False}
    lab_server = labadmin.creates("POST", "ecs", "/v1/p/cloudservers", {}, 200, {"serverIds": ["srv-1"]}, "eu-de")
    assert lab_server[0]["delete"]["body"]["delete_volume"] is True


def test_tw_a_lab_call_with_a_write_key_is_signed_with_it(world):
    w = world
    w.fake.creds[WRITE_AK] = (WRITE_SK, None)
    run = start_run(w, KEYED)
    a = lab(w, "lab_call", tenant=KEYED, run=run, method="PUT", service="obs", path="/" + BUCKET)
    assert a["ok"] and a["status"] == 200 and a["created"] == ["obs bucket"], a
    assert w.fake.aks[-1] == WRITE_AK
    assert WRITE_SK not in json.dumps(a)
    end = lab(w, "lab_run", tenant=KEYED, action="end")
    assert end["ok"] and end["failed"] == [] and BUCKET not in w.fake.buckets


# --------------------------------------------------------------------------- the owner sees it


def test_tw_owner_status_lists_a_tenant_in_write_mode_until_it_is_switched_back(owner_conf):
    assert ownerwait.tenant_mode_items() == []
    readonly.set_mode(KEYED, "write", NOTE, store=WRITE_STORE, today="2026-11-03")
    items = ownerwait.tenant_mode_items()
    assert [(i.label, i.text, i.where, i.block) for i in items] == [
        (KEYED, "customer tenant in write mode since 2026-11-03: %s" % NOTE, ownerwait.WHERE_TERMINAL,
         "awb tenant mode %s readonly" % KEYED)]
    text = "\n".join(ownerwait.render(items, 0))
    assert "%s  customer tenant in write mode since 2026-11-03: %s  [his terminal]" % (KEYED, NOTE) in text
    readonly.set_mode(KEYED, "readonly")
    assert ownerwait.tenant_mode_items() == []


def test_tw_tenant_list_shows_the_mode_of_each_customer_tenant(owner_conf, tmp_path):
    p = SimpleNamespace(vault=tmp_path / "vault")
    readonly.grant(p, KEYED, PROJECT)
    readonly.set_mode(KEYED, "write", NOTE, store=WRITE_STORE, today="2026-11-03")
    lines = tenants.customer_lines(p)
    assert lines[0] == "" and lines[1].split() == ["customer", "tenant", "read", "regions", "mode", "since", "note",
                                                   "granted"]
    rows = {ln.split()[0]: ln.split() for ln in lines[2:]}
    assert rows[KEYED] == [KEYED, "key", "eu-de", "write", "2026-11-03"] + NOTE.split() + [PROJECT]
    assert rows[BARE] == [BARE, "key", "eu-de", "readonly", "-", "-", "-"]


def test_tw_the_tenant_write_line_of_keys_conf_is_checked(tmp_path):
    conf = tmp_path / "keys.conf"
    conf.write_text("tenant_write = %s:2026-11-03:%s\n" % (KEYED, NOTE), encoding="utf-8")
    assert xchg.read_settings(conf)["tenant_write"] == "%s:2026-11-03:%s" % (KEYED, NOTE)
    for bad in ("%s:yesterday:ok" % KEYED, "%s" % KEYED, "%s:2026-11-03:a=b" % KEYED,
                "%s:2026-11-03:, %s:2026-11-04:" % (KEYED, KEYED)):
        conf.write_text("tenant_write = %s\n" % bad, encoding="utf-8")
        with pytest.raises(xchg.XchgError, match="tenant_write"):
            xchg.read_settings(conf)


def test_tw_remove_of_a_customer_tenant_drops_its_write_mode(owner_conf, tmp_path):
    p = SimpleNamespace(vault=tmp_path / "vault")
    readonly.set_mode(KEYED, "write", NOTE, store=WRITE_STORE, today="2026-11-03")
    readonly.remove(p, KEYED)
    assert tenantmode.read() == {} and KEYED not in readonly.of_settings(xchg.read_settings())
