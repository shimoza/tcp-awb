"""tcp-bytg (2026-11-20): a transient answer of the vault daemon is tried again before the name check gives up,
`awb kb find` says why it gave up, and `awb dev ask` runs in a side session's worktree of a project.

The daemon is a stand-in (a function in place of the client's check_remote, or a socket in a short temporary
folder); no test reaches a host path."""
from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import tempfile
import threading
from contextlib import contextmanager
from pathlib import Path

import pytest

from awb import check, cli, config, kb, ownerwait, projects, register, vault_client
from tests import fixtures as fx

HIT = [{"start": 0, "length": 3, "cls": "url"}]


@pytest.fixture
def no_wait(monkeypatch):
    monkeypatch.setattr(check, "TRANSIENT_DELAYS", (0.0, 0.0, 0.0, 0.0))


def _daemon(monkeypatch, *answers):
    """A stand-in check_remote that raises or returns `answers` in turn (the last one for ever); the calls count."""
    calls = []

    def fake(text, sock, timeout=None):
        a = answers[min(len(calls), len(answers) - 1)]
        calls.append(text)
        if isinstance(a, BaseException):
            raise a
        return a

    monkeypatch.setattr(vault_client, "check_remote", fake)
    return calls


@pytest.mark.parametrize("transient", [vault_client.VaultRateLimited("rate limit of the vault daemon reached"),
                                       vault_client.VaultBusy(),
                                       vault_client.VaultDropped("the vault daemon closed the connection")])
def test_a_transient_answer_is_tried_again_and_then_checks(monkeypatch, no_wait, tmp_path, transient):
    calls = _daemon(monkeypatch, transient, transient, HIT)
    assert check.RemoteCheck(tmp_path / "c.sock").scan("abc") == HIT
    assert len(calls) == 3


def test_the_retry_is_bounded_and_says_the_rate_limit(monkeypatch, no_wait, tmp_path):
    calls = _daemon(monkeypatch, vault_client.VaultRateLimited("rate limit of the vault daemon reached"))
    with pytest.raises(check.CheckRateLimited) as err:
        check.RemoteCheck(tmp_path / "c.sock").scan("abc")
    assert len(calls) == 1 + len(check.TRANSIENT_DELAYS)
    assert "rate limit" in str(err.value) and "(5 tries)" in str(err.value) and "try again" in str(err.value)


def test_a_dropped_connection_or_a_reload_still_failing_is_said_as_such(monkeypatch, no_wait, tmp_path):
    _daemon(monkeypatch, vault_client.VaultDropped("the vault daemon closed the connection"))
    with pytest.raises(check.CheckUnavailable) as err:
        check.RemoteCheck(tmp_path / "c.sock").scan("abc")
    assert "closed the connection" in str(err.value) and "register" not in str(err.value)
    _daemon(monkeypatch, vault_client.VaultBusy())
    with pytest.raises(check.CheckUnavailable) as err:
        check.RemoteCheck(tmp_path / "c.sock").scan("abc")
    assert "reloading" in str(err.value) and not isinstance(err.value, check.CheckRateLimited)


@pytest.mark.parametrize("hard, word", [(vault_client.VaultLocked("vault locked"), "vault locked"),
                                         (vault_client.VaultUnavailable("no vault daemon"), "no vault daemon"),
                                         (vault_client.VaultUnavailable("the vault daemon refused this user"),
                                          "refused")])
def test_a_real_failure_is_not_tried_again(monkeypatch, tmp_path, hard, word):
    monkeypatch.setattr(check, "TRANSIENT_DELAYS", (30.0,))      # a retry would hang the test
    calls = _daemon(monkeypatch, hard, HIT)
    with pytest.raises(check.CheckUnavailable) as err:
        check.RemoteCheck(tmp_path / "c.sock").scan("abc")
    assert len(calls) == 1 and word in str(err.value)


def test_the_planted_check_without_retry_fails_the_first_test(monkeypatch, no_wait, tmp_path):
    """Planted failure: with no retry the test of the transient answer must fail, so it can fail."""
    monkeypatch.setattr(check, "TRANSIENT_DELAYS", ())
    _daemon(monkeypatch, vault_client.VaultRateLimited("rate limit of the vault daemon reached"), HIT)
    with pytest.raises(check.CheckRateLimited):
        check.RemoteCheck(tmp_path / "c.sock").scan("abc")


# --------------------------------------------------------------------------- the client: busy and dropped


@contextmanager
def _server(answer: bytes | None):
    """A one-shot daemon on a socket in a short folder: it answers `answer`, or closes at once when None."""
    d = Path(tempfile.mkdtemp(prefix="bytg-", dir="/tmp"))
    sock = d / "c.sock"
    srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    srv.bind(str(sock))
    srv.listen(4)

    def serve():
        try:
            conn, _ = srv.accept()
        except OSError:
            return
        with conn:
            if answer is not None:
                conn.recv(1 << 16)
                conn.sendall(answer)

    t = threading.Thread(target=serve, daemon=True)
    t.start()
    try:
        yield sock
    finally:
        srv.close()
        t.join(5)
        shutil.rmtree(d, ignore_errors=True)


def test_the_client_names_a_busy_answer_and_a_dropped_connection():
    with _server(json.dumps({"ok": False, "error": "busy"}).encode() + b"\n") as sock:
        with pytest.raises(vault_client.VaultBusy):
            vault_client.check_remote("abc", sock, timeout=5)
    with _server(None) as sock:
        with pytest.raises(vault_client.VaultDropped):
            vault_client.check_remote("abc", sock, timeout=5)
    with _server(json.dumps({"ok": False, "error": "rate"}).encode() + b"\n") as sock:
        with pytest.raises(vault_client.VaultRateLimited):
            vault_client.check_remote("abc", sock, timeout=5)
    assert issubclass(vault_client.VaultDropped, vault_client.VaultUnavailable)


# --------------------------------------------------------------------------- awb kb: the precise reason


def test_kb_says_the_daemon_reason_and_keeps_a_broken_register_an_error(home, monkeypatch, capsys):
    def rate(text, register_path):
        raise check.CheckRateLimited(check._reason(vault_client.VaultRateLimited("x"), 5))

    monkeypatch.setattr(check, "check_text", rate)
    with pytest.raises(kb.KBError) as err:
        kb._name_hits("a text", home.register)
    assert "rate limit" in str(err.value) and "register cannot be read" not in str(err.value)

    def broken(text, register_path):
        raise register.RegisterError("bad line")

    monkeypatch.setattr(check, "check_text", broken)
    with pytest.raises(kb.KBError) as err:
        kb._name_hits("a text", home.register)
    assert str(err.value) == "name check unavailable: the register cannot be read"
    assert kb._unavailable(check.CheckUnavailable("odd")) == "name check unavailable: the register cannot be read"


# --------------------------------------------------------------------------- awb dev ask in a side worktree


@pytest.fixture
def world(home, monkeypatch, tmp_path):
    pr = projects.spawn(home, "query", "check the docs", fx.CUSTOMER_CODE, home.register)
    monkeypatch.setattr(config, "paths", lambda: home)
    monkeypatch.setattr(config, "is_work_user", lambda: True)
    monkeypatch.setattr(ownerwait, "export_items", lambda *a: [])
    monkeypatch.setattr(ownerwait, "winlab_items", lambda: [])
    monkeypatch.setattr(ownerwait, "winlab_online_items", lambda: [])
    monkeypatch.setattr(ownerwait, "tenant_mode_items", lambda: [])
    monkeypatch.setattr(ownerwait, "queue_file", lambda: tmp_path / "none.tsv")
    return home, pr


def _worktree(root: Path, name: str = "testruns") -> Path:
    wt = root / ".side" / name
    subprocess.run(["git", "-C", str(root), "worktree", "add", "-q", "-b", "side/" + name, str(wt)],
                   check=True, capture_output=True)
    return wt


def test_dev_ask_in_a_side_worktree_writes_the_ask_of_its_project(world, monkeypatch, capsys):
    home, pr = world
    wt = _worktree(Path(pr.path))
    (wt / "notes").mkdir(exist_ok=True)
    monkeypatch.chdir(wt / "notes")
    assert cli.main(["dev", "ask", "missing docs filter"]) == 0
    path = sorted((home.shared / "notices/dev").glob("*.md"))[-1]
    assert path.name.endswith("-%s-1.md" % pr.code)
    assert "Project: %s" % pr.code in path.read_text()
    fx.assert_no_fixture_name(capsys.readouterr().out)


def test_dev_ask_refuses_a_side_folder_that_is_no_worktree_of_its_project(world, monkeypatch, tmp_path, capsys):
    home, pr = world
    root = Path(pr.path)
    fake = root / ".side" / "fake"
    fake.mkdir(parents=True)
    (fake / "SCOPE.md").write_text((root / "SCOPE.md").read_text())
    (fake / ".git").write_text("gitdir: %s/elsewhere/.git/worktrees/fake\n" % tmp_path)
    monkeypatch.chdir(fake)
    assert cli.main(["dev", "ask", "missing docs filter"]) != 0
    assert "registered project" in capsys.readouterr().err
    assert not list((home.shared / "notices/dev").glob("*.md")) if (home.shared / "notices/dev").is_dir() else True
