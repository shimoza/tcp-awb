"""T6: `awb web publish` installs a checked page with a backup in one step (a temporary web folder, queue and
source through AWB_WEB_ROOT, AWB_UI_QUEUE and AWB_UI_SOURCE)."""
from __future__ import annotations

import os
import pwd
import sqlite3
import stat
from pathlib import Path

import pytest

from awb import config, gate
from awb.tcp.web import publish
from tests import fixtures

PAGE = "<!doctype html><html><head><title>AWB</title></head><body><p>%s</p><script>let x=1</script></body></html>\n"


def _page(body: str = "projects") -> bytes:
    return (PAGE % body).encode("utf-8")


@pytest.fixture
def web(tmp_path, monkeypatch, home):
    """A web folder with a page, an empty queue and the applied source; the fixture register checks names."""
    root = tmp_path / "srv"
    root.mkdir()
    (root / "index.html").write_bytes(_page("old"))
    queue = tmp_path / "queue"
    (queue / "runs").mkdir(parents=True)
    con = sqlite3.connect(queue / "queue.sqlite3")
    con.execute("CREATE TABLE jobs (id TEXT PRIMARY KEY, text TEXT NOT NULL, hash TEXT NOT NULL, state TEXT NOT "
                "NULL, created TEXT NOT NULL, started TEXT, finished TEXT, detail TEXT NOT NULL DEFAULT '', "
                "apply_changes INTEGER NOT NULL DEFAULT 1)")
    con.commit()
    con.close()
    monkeypatch.setenv("AWB_WEB_ROOT", str(root))
    monkeypatch.setenv("AWB_UI_QUEUE", str(queue))
    monkeypatch.setenv("AWB_UI_SOURCE", str(tmp_path / "source.html"))
    return root


def _run(job_id: str, page: bytes, state: str = "completed", validation: str = "PASS: all checks",
         applies: bool = True) -> None:
    queue = Path(os.environ["AWB_UI_QUEUE"])
    run = queue / "runs" / job_id
    (run / "workspace").mkdir(parents=True)
    (run / "workspace" / "architect-workbench.html").write_bytes(page)
    (run / "validation.txt").write_text(validation + "\n", encoding="utf-8")
    if applies and state == "completed":
        Path(os.environ["AWB_UI_SOURCE"]).write_bytes(page)
    con = sqlite3.connect(queue / "queue.sqlite3")
    con.execute("INSERT INTO jobs(id,text,hash,state,created,apply_changes) VALUES(?,?,?,?,?,?)",
                (job_id, "task", "h", state, "2026-10-07T10:00:00+00:00", int(applies)))
    con.commit()
    con.close()


def test_a_completed_run_with_pass_is_installed_with_a_backup_named_by_its_id(web, capsys):
    _run("ui-20261007-001", _page("new"))
    assert publish.main(["publish", "--from-queue", "ui-20261007-001"]) == 0
    assert (web / "index.html").read_bytes() == _page("new")
    assert (web / "index.html.before-ui-20261007-001").read_bytes() == _page("old")
    assert stat.S_IMODE((web / "index.html").stat().st_mode) == 0o644
    out = capsys.readouterr().out
    assert "index.html.before-ui-20261007-001" in out and str(len(_page("new"))) in out


def test_a_candidate_only_run_in_state_ready_installs_its_candidate(web):
    _run("ui-20261007-002", _page("candidate"), state="ready", applies=False)
    assert publish.main(["publish", "--from-queue", "ui-20261007-002"]) == 0
    assert (web / "index.html").read_bytes() == _page("candidate")


def test_a_run_whose_validation_is_not_pass_is_refused(web, capsys):
    _run("ui-20261007-003", _page("new"), validation="FAIL: overflow at 390px")
    assert publish.main(["publish", "--from-queue", "ui-20261007-003"]) == 1
    assert "not PASS" in capsys.readouterr().err
    assert (web / "index.html").read_bytes() == _page("old")
    assert publish.backups(web) == []


def test_a_run_that_is_not_completed_is_refused(web, capsys):
    _run("ui-20261007-004", _page("new"), state="failed")
    assert publish.main(["publish", "--from-queue", "ui-20261007-004"]) == 1
    assert "failed" in capsys.readouterr().err
    assert (web / "index.html").read_bytes() == _page("old")


@pytest.mark.parametrize("cls", ["secret", "token", "private-key"])
def test_a_planted_secret_is_refused_with_its_class_only(web, tmp_path, capsys, cls):
    planted = gate._selftest_cases()[cls].decode("utf-8")
    _run("ui-20261007-005", _page(planted))
    assert publish.main(["publish", "--from-queue", "ui-20261007-005"]) == 1
    err = capsys.readouterr().err
    assert "%s: 1 line" % cls in err
    assert planted.splitlines()[1] not in err
    assert (web / "index.html").read_bytes() == _page("old")


def test_a_planted_registered_form_is_refused_with_the_class_only(web, tmp_path, capsys):
    form = fixtures.CUSTOMER_FORMS[0]
    page = tmp_path / "candidate.html"
    page.write_bytes(_page("offer for %s" % form))
    assert publish.main(["publish", str(page)]) == 1
    captured = capsys.readouterr()
    assert "name: 1 line" in captured.err
    for text in (captured.err, captured.out):
        for f in fixtures.ALL_REGISTERED:
            assert f not in text
    assert (web / "index.html").read_bytes() == _page("old")


def test_an_external_script_or_a_second_title_is_refused(web, tmp_path, capsys):
    page = tmp_path / "candidate.html"
    page.write_bytes(_page('</p><script src="https://cdn.example/x.js"></script><p>'))
    assert publish.main(["publish", str(page)]) == 1
    assert "external script" in capsys.readouterr().err
    page.write_bytes(_page("</p><title>again</title><p>"))
    assert publish.main(["publish", str(page)]) == 1
    assert "one <title>" in capsys.readouterr().err
    assert (web / "index.html").read_bytes() == _page("old")


def test_a_file_is_installed_with_a_dated_backup(web, tmp_path):
    page = tmp_path / "candidate.html"
    page.write_bytes(_page("from a file"))
    assert publish.main(["publish", str(page)]) == 0
    assert (web / "index.html").read_bytes() == _page("from a file")
    [backup] = publish.backups(web)
    assert backup.name.startswith("index.html.before-2") and backup.read_bytes() == _page("old")


def test_dry_run_installs_nothing(web, capsys):
    _run("ui-20261007-006", _page("new"))
    assert publish.main(["publish", "--from-queue", "ui-20261007-006", "--dry-run"]) == 0
    assert "index.html.before-ui-20261007-006" in capsys.readouterr().out
    assert (web / "index.html").read_bytes() == _page("old")
    assert publish.backups(web) == []


def test_rollback_restores_the_last_backup(web, tmp_path):
    _run("ui-20261007-007", _page("new"))
    assert publish.main(["publish", "--from-queue", "ui-20261007-007"]) == 0
    assert publish.main(["publish", "--rollback"]) == 0
    assert (web / "index.html").read_bytes() == _page("old")
    assert publish.main(["publish", "--rollback", "index.html.before-ui-20261007-007"]) == 0
    assert (web / "index.html").read_bytes() == _page("old")
    assert publish.main(["publish", "--rollback", "../index.html"]) == 1


def test_refused_for_the_work_user(web, monkeypatch, capsys):
    _run("ui-20261007-008", _page("new"))
    monkeypatch.setattr(config, "is_work_user", lambda: True)
    assert publish.main(["publish", "--from-queue", "ui-20261007-008"]) == 2
    assert "work user" in capsys.readouterr().err
    assert (web / "index.html").read_bytes() == _page("old")


def test_the_real_web_folder_needs_sudo(monkeypatch, tmp_path, capsys):
    monkeypatch.delenv("AWB_WEB_ROOT", raising=False)
    monkeypatch.setattr(publish, "SRV", tmp_path)
    page = tmp_path / "candidate.html"
    page.write_bytes(_page())
    host = publish.Host(geteuid=lambda: 1000, environ={})
    assert publish.main(["publish", str(page)], host) == 1
    assert "sudo" in capsys.readouterr().err
    assert not (tmp_path / "index.html").exists()


def test_under_sudo_the_page_is_read_and_checked_as_the_owner(web, tmp_path, monkeypatch):
    """Root starts `awb web check --emit` dropped to SUDO_USER and installs the bytes it hands back as root."""
    me = pwd.getpwuid(os.getuid())
    calls, owned = [], []

    class Done:
        returncode, stdout, stderr = 0, _page("checked by the owner"), b""

    def runner(cmd, **kw):
        calls.append((cmd, kw))
        return Done()

    host = publish.Host(geteuid=lambda: 0, runner=runner, environ={"SUDO_USER": me.pw_name, "AWB_WEB_ROOT": str(web)},
                        chown=lambda path, uid, gid: owned.append((Path(path).name, uid, gid)))
    assert publish.main(["publish", "--from-queue", "ui-20261007-009"], host) == 0
    [(cmd, kw)] = calls
    assert cmd[1:] == ["web", "check", "--emit", "--from-queue", "ui-20261007-009"]
    assert kw["user"] == me.pw_uid and kw["group"] == me.pw_gid and kw["env"]["HOME"] == me.pw_dir
    assert "AWB_UI_QUEUE" not in kw["env"]
    assert (web / "index.html").read_bytes() == _page("checked by the owner")
    assert ("index.html.before-ui-20261007-009", 0, 0) in owned and len(owned) == 2


def test_under_sudo_a_refusal_of_the_owner_check_installs_nothing(web, monkeypatch, capsys):
    me = pwd.getpwuid(os.getuid())

    class Refused:
        returncode, stdout, stderr = 1, b"", b"awb web: the page is refused: secret: 1 line\n"

    host = publish.Host(geteuid=lambda: 0, runner=lambda cmd, **kw: Refused(), environ={"SUDO_USER": me.pw_name, "AWB_WEB_ROOT": str(web)},
                        chown=lambda *a: None)
    assert publish.main(["publish", "--from-queue", "ui-20261007-010"], host) == 1
    assert "secret: 1 line" in capsys.readouterr().err
    assert (web / "index.html").read_bytes() == _page("old")


def test_status_names_the_page_and_the_last_backup(web, monkeypatch, capsys):
    monkeypatch.setattr(publish, "GATEWAY_UNIT", web / "no-unit")
    monkeypatch.setattr(publish, "_ask", lambda url, host_header=None: "no answer")
    _run("ui-20261007-011", _page("new"))
    assert publish.main(["publish", "--from-queue", "ui-20261007-011"]) == 0
    capsys.readouterr()
    assert publish.main(["status"]) == 0
    out = capsys.readouterr().out
    assert "page: %d bytes" % len(_page("new")) in out
    assert "last backup: index.html.before-ui-20261007-011" in out
    assert "gateway: not installed" in out and "health: no answer" in out


# --------------------------------------------------------------------------- T9 step 3: Publish from the owner host

import argparse
import json
import socket
import threading
import time

from awb.tcp.web import gateway, owner_actions


@pytest.fixture
def state(tmp_path, monkeypatch):
    folder = tmp_path / "publish-state"
    monkeypatch.setenv("AWB_WEB_PUBLISH_STATE", str(folder))
    return folder


def test_newest_takes_only_the_newest_pass_run_newer_than_the_last_and_other_bytes(web, state, capsys):
    _run("ui-20261008-001", _page("first"))
    _run("ui-20261008-002", _page("second"))
    _run("ui-20261008-003", _page("third"), state="ready", validation="FAIL: contrast", applies=False)
    for run_id, why in (("ui-20261008-003", "only the newest PASS run"), ("ui-20261008-001", "only the newest PASS run"),
                        ("../etc", "a form the queue never gives")):
        assert publish.main(["publish", "--from-queue", run_id, "--newest"]) == 1
        assert why in capsys.readouterr().err
    assert (web / "index.html").read_bytes() == _page("old") and publish.backups(web) == []
    assert publish.main(["publish", "--from-queue", "ui-20261008-002", "--newest"]) == 0
    assert (web / "index.html").read_bytes() == _page("second")
    assert (state / "last-run").read_text() == "ui-20261008-002\n"
    capsys.readouterr()
    assert publish.main(["publish", "--from-queue", "ui-20261008-002", "--newest"]) == 1
    assert "not newer than the last published one" in capsys.readouterr().err
    _run("ui-20261008-004", _page("second"))
    assert publish.main(["publish", "--from-queue", "ui-20261008-004", "--newest"]) == 1
    assert "the same as the live page" in capsys.readouterr().err
    assert [p.name for p in publish.backups(web)] == ["index.html.before-ui-20261008-002"]


def test_newest_keeps_the_newest_ten_backups(web, state):
    for i in range(12):
        (web / ("index.html.before-old-%02d" % i)).write_bytes(b"x")
        os.utime(web / ("index.html.before-old-%02d" % i), (1000 + i, 1000 + i))
    _run("ui-20261008-005", _page("fifth"))
    assert publish.main(["publish", "--from-queue", "ui-20261008-005", "--newest"]) == 0
    names = [p.name for p in publish.backups(web)]
    assert len(names) == 10 and names[-1] == "index.html.before-ui-20261008-005"
    assert "index.html.before-old-00" not in names and "index.html.before-old-11" in names


def _publish_unit(web, run_line, monkeypatch, tmp_path):
    """The root unit's path as a plain user: one connection on a listening socket, the answer line back."""
    monkeypatch.setattr(publish.config, "host_conf", lambda: {"owner": "someone"})
    listener = gateway.unix_listener(str(tmp_path / "publish.sock"))
    answer = []

    def client():
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.connect(str(tmp_path / "publish.sock"))
        s.sendall(run_line)
        answer.append(s.recv(1024))
        s.close()

    t = threading.Thread(target=client)
    t.start()
    a = argparse.Namespace(root=None, from_socket=True)
    assert publish._from_socket(a, publish.Host(geteuid=lambda: 1000), sock=listener) == 0
    t.join(5)
    listener.close()
    (tmp_path / "publish.sock").unlink()
    return answer[0].decode()


def test_the_root_unit_reads_one_run_id_and_answers_one_line(web, state, monkeypatch, tmp_path):
    _run("ui-20261008-006", _page("sixth"))
    assert _publish_unit(web, b"ui-20261008-006\n", monkeypatch, tmp_path) == \
        "published index.html.before-ui-20261008-006\n"
    assert (web / "index.html").read_bytes() == _page("sixth")
    assert _publish_unit(web, b"ui-20261008-006\n", monkeypatch, tmp_path) == \
        "refused the run is not newer than the last published one\n"
    assert _publish_unit(web, b"../../srv/x\n", monkeypatch, tmp_path).startswith("refused the run id has a form")
    with pytest.raises(publish.Refused, match="runs as root"):
        publish._from_socket(argparse.Namespace(root=None), publish.Host(geteuid=lambda: 1000))


class FakeUnit:
    """The root unit's socket as owner-actions sees it: records what arrives, answers a fixed line."""

    def __init__(self, path, line):
        self.got, self.line = [], line
        self.sock = gateway.unix_listener(str(path))
        threading.Thread(target=self.serve, daemon=True).start()

    def serve(self):
        while True:
            try:
                conn, _ = self.sock.accept()
            except OSError:
                return
            self.got.append(conn.recv(1024))
            conn.sendall(self.line)
            conn.close()


def _owner_post(server, body, level="owner"):
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.settimeout(5)
    s.connect(server.server_address)
    raw = json.dumps(body).encode()
    s.sendall(b"POST /api/owner/publish HTTP/1.0\r\nX-AWB-Level: " + level.encode() + b"\r\nContent-Type: "
              b"application/json\r\nContent-Length: " + str(len(raw)).encode() + b"\r\n\r\n" + raw)
    data = b""
    while chunk := s.recv(65536):
        data += chunk
    s.close()
    head, _, payload = data.partition(b"\r\n\r\n")
    return int(head.split()[1]), json.loads(payload)


def test_owner_actions_checks_the_code_itself_and_hands_on_the_run_id_alone(tmp_path):
    """A fake gateway that skips the code check is refused by owner-actions: a wrong, an old or a used code never
    reaches the root unit; a fresh code hands on the run id and nothing else."""
    secret = "JBSWY3DPEHPK3PXPJBSWY3DPEHPK3PXP"
    secrets = tmp_path / "owner-publish.json"
    secrets.write_text(json.dumps({"owner1": secret}))
    unit = FakeUnit(tmp_path / "unit.sock", b"published index.html.before-ui-20261008-007\n")
    server = owner_actions.Server(gateway.unix_listener(str(tmp_path / "actions.sock")), os.getuid(),
                                  tmp_path / "status.json", tmp_path / "log", secrets=secrets,
                                  publish_socket=str(tmp_path / "unit.sock"))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        now = time.time()
        code = gateway.totp_code(secret, int(now // 30))
        old = gateway.totp_code(secret, int(now // 30) - 1)
        wrong = "000000" if code != "000000" else "111111"
        for body in ({"run_id": "ui-20261008-007", "code": wrong}, {"run_id": "ui-20261008-007", "code": old}):
            assert _owner_post(server, body) == (403, {"error": owner_actions.CODE_REFUSED})
        assert _owner_post(server, {"run_id": "ui-20261008-007", "code": code}, level="reader")[0] == 403
        assert _owner_post(server, {"run_id": "../x", "code": code})[0] == 400
        assert _owner_post(server, {"run_id": "ui-20261008-007", "code": code, "more": 1})[0] == 400
        assert unit.got == []
        assert _owner_post(server, {"run_id": "ui-20261008-007", "code": code}) == (
            200, {"published": True, "backup": "index.html.before-ui-20261008-007"})
        assert unit.got == [b"ui-20261008-007\n"]
        assert _owner_post(server, {"run_id": "ui-20261008-007", "code": code}) == (403, {"error":
                                                                                         owner_actions.CODE_REFUSED})
        unit.line = b"refused only the newest PASS run can be published\n"
        server.used.clear()
        assert _owner_post(server, {"run_id": "ui-20261008-007", "code": code}) == (
            409, {"error": "Publish refused: only the newest PASS run can be published."})
        server.used.clear()
        server.publish_socket = str(tmp_path / "missing.sock")
        assert _owner_post(server, {"run_id": "ui-20261008-007", "code": code}) == (503, {"error":
                                                                                         owner_actions.PUBLISH_DOWN})
    finally:
        server.shutdown()
        server.server_close()
        unit.sock.close()


def test_awb_web_user_keeps_the_copy_of_the_owner_secrets_in_step(tmp_path, monkeypatch):
    from awb.tcp.web import users

    monkeypatch.delenv("CLAUDECODE", raising=False)
    monkeypatch.delenv("CLAUDE_CODE_ENTRYPOINT", raising=False)
    monkeypatch.setattr(users.shutil, "which", lambda name: None)
    env = {"AWB_WEB_USERS": str(tmp_path / "users.json")}
    copy = tmp_path / "owner-publish.json"
    users.main(argparse.Namespace(action="add", login="reader1", level=None), env, lambda: 1000, lambda line: None)
    assert not copy.exists()
    users.main(argparse.Namespace(action="add", login="owner1", level="owner"), env, lambda: 1000, lambda line: None)
    first = json.loads(copy.read_text())
    assert list(first) == ["owner1"] and stat.S_IMODE(copy.stat().st_mode) == 0o600
    users.main(argparse.Namespace(action="reset", login="owner1", level=None), env, lambda: 1000, lambda line: None)
    assert json.loads(copy.read_text())["owner1"] != first["owner1"]
    users.main(argparse.Namespace(action="remove", login="owner1", level=None), env, lambda: 1000, lambda line: None)
    assert json.loads(copy.read_text()) == {}
