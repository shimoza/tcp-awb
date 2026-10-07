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


def test_the_lab_role_does_not_write_to_an_identity_service(svc, gw):
    load(svc)
    before = len(gw.calls)
    a = call(svc, role="lab", method="POST", service="iam", path="/v3.0/OS-CREDENTIAL/credentials",
             body={"credential": {"user_id": "x"}}, project=PROJECT)
    assert not a["ok"] and "does not write to iam" in a["error"]
    assert len(gw.calls) == before          # refused before anything is signed and sent
    # a read of iam stays allowed: the inventory and the project id resolution need it
    a = call(svc, role="read", method="GET", service="iam", path="/v3/projects")
    assert a["ok"] and a["status"] == 200, a


def test_a_crafted_project_cannot_add_a_line_to_the_log(svc, gw, tmp_path):
    load(svc)
    a = call(svc, method="GET", service="vpc", path="/v1/x",
             project="tcp-ab2c\nFAKE\t12\tread\tGET\tvpc\teu-de\t200\tinjected")
    assert a["ok"]
    log = next((tmp_path / "log").glob("*.tsv"))
    lines = [l for l in log.read_text(encoding="utf-8").splitlines() if l.strip()]
    assert len(lines) == 1, lines


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


def test_the_console_user_reads_a_tenant_without_a_project_and_nothing_more(svc, gw):
    """F2: the tenant inventory of the console runs as its own system user, which alone may read without a project."""
    load(svc)
    console = __import__("os").getuid() + 2
    session = __import__("os").getuid() + 1
    svc._console_uid = lambda: console
    req = {"tenant": "test-1", "method": "GET", "service": "vpc", "path": "/v1/{project_id}/vpcs"}
    assert svc._call(dict(req), console)["status"] == 200
    with pytest.raises(keys.Refused, match="read key only"):
        svc._call(dict(req, role="lab"), console)
    with pytest.raises(keys.Refused, match="allows GET, HEAD only"):
        svc._call(dict(req, method="POST", body={}), console)
    with pytest.raises(keys.Refused, match="folder of an active project"):
        svc._call(dict(req), session)
    svc._console_uid = lambda: None          # no such user on the host: nobody reads without a project
    with pytest.raises(keys.Refused, match="folder of an active project"):
        svc._call(dict(req), console)
    assert keys.CONSOLE_USER == "awb-console" and svc.console_user == keys.CONSOLE_USER


# --------------------------------------------------------------------------- T2: the hand-over of a reload
#
# The service before runs in threads of this process, the new one is a real child process (`python -I -m awb keys
# serve --takeover FD`) or a stand-in that tampers with what it holds. The child reads the tenants and projects of
# the test home like the service of a host does.

KEYS_STANDIN = r'''
import sys
from awb.tcp import keys
fd, mode = int(sys.argv[1]), sys.argv[2]
real = keys._hold

def hold(svc, line):
    t, s = line["tenants"], line["settings"]
    if mode == "summary":
        t = {a: {"roles": sorted(v["roles"]), "secrets": sorted(v["secrets"])} for a, v in t.items()}
    elif mode == "masked":
        t = {a: {"roles": {r: {k: keys.MASK for k in pair} for r, pair in v["roles"].items()},
                 "secrets": {n: keys.MASK for n in v["secrets"]}} for a, v in t.items()}
    elif mode == "empty-settings":
        s = {}
    real(svc, dict(line, tenants=t, settings=s))

keys._hold = hold
sys.exit(keys.main(["serve", "--takeover", str(fd)]))
'''
SETTINGS = {"region": "eu-de", "bucket_tenant": "test-1"}


@pytest.fixture
def hsvc(home, gw, monkeypatch, tmp_path):
    """A service of the test home: tenants and an active project registered on disk, so that the new process of a
    reload knows them too. Serves in a thread like `awb keys serve`; a new process a reload started is stopped at
    the end."""
    from awb import projects
    from awb.tcp import tenants

    tenants.add(home, "test-1", "file:/nonexistent/key", ["eu-de"])
    tenants.add(home, "test-2", "file:/nonexistent/key", ["eu-de", "eu-nl"])
    folder = str(home.projects_root / PROJECT)
    home.projects_register.parent.mkdir(parents=True, exist_ok=True)
    projects._save(home, [projects.Project(PROJECT, "project", projects.NO_CUSTOMER, "tcp", folder,
                                           projects.memory_key(folder), "active", "2026-10-07")])
    monkeypatch.setenv("AWB_CLOUD_ENDPOINT", gw.base)
    import threading

    with short_dir() as d:
        s = keys.Service(d / "admin.sock", d / "call.sock", endpoint=gw.base)
        s.start()
        t = threading.Thread(target=s.serve_forever, daemon=True)
        t.start()
        s.thread, s.children = t, []
        try:
            yield s
        finally:
            if not s.handed_over.is_set():
                s.stopping.set()
            t.join(timeout=90)
            for pid in s.children:
                _stop(pid)


def _stop(pid: int) -> None:
    import os
    import signal
    import time

    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    end = time.monotonic() + 20
    while Path("/proc/%d" % pid).exists() and time.monotonic() < end:
        try:
            os.waitpid(pid, os.WNOHANG)
        except ChildProcessError:
            pass
        time.sleep(0.05)
    assert not Path("/proc/%d" % pid).exists(), "the new key service did not stop"


def _reload(s) -> dict:
    answer = keys.request(s.admin_path, {"op": "reload"}, timeout=75)
    if answer.get("ok"):
        s.children.append(answer["pid"])
    return answer


def _keys_standin(monkeypatch, tmp_path, mode: str) -> None:
    import sys

    script = tmp_path / "keys_standin.py"
    script.write_text(KEYS_STANDIN, encoding="utf-8")
    monkeypatch.setattr(keys, "_takeover_command", lambda fd: [sys.executable, "-I", str(script), str(fd), mode])


def test_the_key_service_reload_keeps_the_key_values(hsvc, gw, monkeypatch, tmp_path, capfd):
    """Planted: a new service that lost or changed a key value (its calls are not signed with the fixture keys); a
    summary-shaped, a masked or an empty-settings child that the service before lets serve; the digest in an answer
    or a log line."""
    import os

    real_command = keys._takeover_command
    summary = keys.unlock(hsvc.admin_path, sorted(STORE), reader=lambda e: STORE[e], settings=SETTINGS)
    with hsvc.lock:
        digest = keys._digest(hsvc.tenants, hsvc.settings)
    answers = []
    for mode in ("summary", "masked", "empty-settings"):
        _keys_standin(monkeypatch, tmp_path, mode)
        a = _reload(hsvc)
        answers.append(a)
        assert a == {"ok": False, "error": "failed",
                     "detail": "the new key service did not come up (other values)"}, mode
        assert keys.request(hsvc.admin_path, {"op": "status"})["pid"] == os.getpid()
        assert not hsvc.handing_over.is_set()
    monkeypatch.setattr(keys, "_takeover_command", real_command)
    a = _reload(hsvc)
    answers.append(a)
    assert a["ok"] and a["tenants"] == summary and a["pid"] != os.getpid()
    hsvc.thread.join(timeout=90)
    assert not hsvc.thread.is_alive(), "the service before did not leave"
    status = keys.request(hsvc.admin_path, {"op": "status"})
    answers.append(status)
    assert status["pid"] == a["pid"] and status["tenants"] == summary
    r = call(hsvc, method="GET", service="vpc", path="/v1/{project_id}/vpcs", query={"limit": "3"})
    assert r["ok"] and r["status"] == 200 and len(r["data"]["vpcs"]) == 3, r
    lab_gateway(gw)
    body = {"server": {"name": "lab-1", "admin_pass": "{{secret:ecs-admin}}"}}
    r = call(hsvc, role="lab", method="POST", service="ecs", path="/v1/{project_id}/cloudservers", body=body,
             project=PROJECT)
    assert r["ok"] and r["status"] == 200, r
    assert ADMIN_PW.encode() in gw.bodies[-1]
    out, err = capfd.readouterr()
    logs = "".join(f.read_text() for f in (config.paths().vault / "log").glob("*.tsv"))
    for text in [json.dumps(x) for x in answers] + [out, err, logs]:
        assert digest not in text
        for value in STORE.values():
            assert value not in text


def test_the_key_service_leaves_through_the_hand_over_exit(hsvc, tmp_path):
    """Planted: an exit through stop() that removes the socket files the new service serves on; a SIGTERM to the
    service before that does; a stop that removes a socket file it did not bind."""
    import os
    import socket

    keys.unlock(hsvc.admin_path, sorted(STORE), reader=lambda e: STORE[e], settings=SETTINGS)
    ident = [os.lstat(f).st_ino for f in (hsvc.admin_path, hsvc.call_path)]
    a = _reload(hsvc)
    assert a["ok"]
    hsvc.thread.join(timeout=90)
    hsvc.stopping.set()          # what the signal handler does
    hsvc.stop()
    assert [os.lstat(f).st_ino for f in (hsvc.admin_path, hsvc.call_path)] == ident
    assert keys.request(hsvc.admin_path, {"op": "ping"})["ok"]
    assert keys.request(hsvc.call_path, {"op": "ping"})["state"] == "unlocked"
    _stop(a["pid"])
    hsvc.children.remove(a["pid"])
    assert not hsvc.admin_path.exists() and not hsvc.call_path.exists(), "the new service's clean stop"
    # a later clean stop removes only a file it bound itself
    with short_dir() as d:
        s = keys.Service(d / "admin.sock", d / "call.sock")
        s.start()
        s.call_path.unlink()
        other = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        other.bind(str(s.call_path))
        try:
            s.stop()
            assert s.call_path.exists() and not s.admin_path.exists()
        finally:
            other.close()


def test_the_key_service_is_not_dumpable_says_ready_and_answers_ops(tmp_path, monkeypatch):
    """Planted: a served key service whose /proc stays the owner's; no READY=1 after the bind; a ping or status
    without ops (reload among them) or release."""
    import os
    import signal
    import socket
    import subprocess
    import sys
    import time

    from awb import vault

    with short_dir() as d:
        note = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
        note.bind(str(d / "notify"))
        note.settimeout(20)
        env = dict(os.environ, NOTIFY_SOCKET=str(d / "notify"), AWB_KEYS_SOCKET=str(d / "call.sock"),
                   AWB_KEYS_ADMIN=str(d / "admin.sock"), PYTHONPATH=str(Path(__file__).resolve().parent.parent))
        with open(tmp_path / "serve.log", "wb") as out:
            proc = subprocess.Popen([sys.executable, "-m", "awb", "keys", "serve"], env=env, stdout=out, stderr=out)
        try:
            assert note.recv(64) == b"READY=1"
            assert (d / "admin.sock").exists() and (d / "call.sock").exists()
            assert os.stat("/proc/%d/status" % proc.pid).st_uid == 0
            for op in ("ping", "status"):
                a = keys.request(d / "admin.sock", {"op": op}, timeout=10)
                assert "reload" in a["ops"] and a["release"] == vault.release_path()
        finally:
            proc.send_signal(signal.SIGTERM)
            proc.wait(timeout=20)
            note.close()
        assert not (d / "admin.sock").exists()
