"""UX13: source-only owner hand-over and per-session compaction receipts."""
from __future__ import annotations

import io
import json
import os
from pathlib import Path

import pytest

from awb import build, cli, config, handover, hooks, ownerwait, projects, workqueue
from tests import fixtures as fx
from tests.test_build_queue import BLOCK, queue as build_queue
from tests.test_deploy import git


@pytest.fixture
def desk(home, tmp_path, monkeypatch):
    root = tmp_path / "repo"
    (root / "awb").mkdir(parents=True)
    (root / "awb/cli.py").write_text("")
    git(root, "init", "-q")
    git(root, "-c", "user.name=fixture", "-c", "user.email=fixture@example.invalid",
        "commit", "--allow-empty", "-qm", "UX13 initial")
    monkeypatch.chdir(root)
    monkeypatch.setattr(config, "paths", lambda: home)
    monkeypatch.setattr(config, "is_work_user", lambda: False)
    monkeypatch.setattr(config, "is_build_user", lambda: False)
    monkeypatch.setattr(hooks, "for_this_user", lambda: False)
    monkeypatch.setattr(hooks, "_sealed", lambda: False)
    monkeypatch.setenv("AWB_WORK_QUEUE", str(root / "queue.tsv"))
    monkeypatch.setenv("AWB_BUILD_QUEUE", str(root / "build/queue"))
    monkeypatch.setenv("AWB_BUILD_RUNS", str(root / "runs"))
    monkeypatch.setattr(projects, "NOTE_JOURNAL", root / "DEPLOYED")
    # the child process of _build_lines runs as the real user: as the build user (the runner) `awb build` refuses
    # it, so the test reads the same command in this process, where the owner check above holds
    def build_lines():
        import contextlib

        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            assert cli.main(["build", "list"]) == 0
        return buf.getvalue().splitlines()

    monkeypatch.setattr(handover, "_build_lines", build_lines)
    return root, home


def written(root):
    return (root / "build/HANDOVER.md").read_text()


def run_hook(name, root, monkeypatch, capsys, sid="session-13"):
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(
        dict(cwd=str(root), session_id=sid, prompt="continue"))))
    code = hooks.main([name])
    out, err = capsys.readouterr()
    assert code == 0, err
    return out


def test_every_open_queue_row_and_no_done_row(desk, capsys):
    root, _ = desk
    path = workqueue.queue_file()
    rows = [workqueue._new("Q-%04d" % n, "review %s" % who, "check result", who)
            for n, who in enumerate(("owner", "desk", "development", "session:S13"), 1)]
    workqueue._write(path, rows + [dict(workqueue._new("Q-0005", "closed item", "none", "owner"),
                                      done=workqueue.now().isoformat())])
    workqueue._write(workqueue.archive_file(path), [dict(rows[-1], done=workqueue.now().isoformat())])
    assert cli.main(["handover", "write"]) == 0
    text = written(root)
    for row in rows[:-1]:
        assert row["id"] in text and row["what"] in text and row["next"] in text
    assert "Q-0004" not in text and "Q-0005" not in text
    assert cli.main(["check", "build/HANDOVER.md"]) == 0
    fx.assert_no_fixture_name(text + capsys.readouterr().out)


def test_legacy_waiting_rows_without_migrating(desk):
    root, _ = desk
    legacy = root / "QUEUE.md"
    original = ("| ID | What | State | Next |\n| --- | --- | --- | --- |\n"
                "| UX13 | keep thread | waiting on owner | review result |\n"
                "| UX14 | closed item | done | none |\n"
                "| UX15 | in progress | running | check result |\n")
    legacy.write_text(original)
    assert cli.main(["handover", "write"]) == 0
    text = written(root).split("## Git log", 1)[0]
    assert "UX13" in text and "keep thread" in text and "review result" in text
    assert "UX14" not in text and "UX15" not in text
    assert legacy.read_text() == original and not (root / "queue.tsv").exists()


def test_git_log_advances_only_after_a_successful_write(desk):
    root, _ = desk
    assert cli.main(["handover", "write"]) == 0
    assert "UX13 initial" in written(root)
    git(root, "-c", "user.name=fixture", "-c", "user.email=fixture@example.invalid",
        "commit", "--allow-empty", "-qm", "UX13 next")
    assert cli.main(["handover", "write"]) == 0
    assert "UX13 next" in written(root) and "UX13 initial" not in written(root)
    assert cli.main(["handover", "write"]) == 0
    assert "UX13 next" not in written(root)


def test_build_open_tasks_held_notes_asks_and_default_status(desk, monkeypatch):
    root, home = desk
    qdir = build.queue_dir()
    qdir.mkdir(parents=True)
    build_queue(qdir, BLOCK + BLOCK.replace("B-012", "B-013"), ("B-012.md", "B-013.md"))
    run = build.runs_dir() / "B-013/1"
    run.mkdir(parents=True)
    build.write_state(run, "done")
    notices = home.shared / "notices"
    notices.mkdir(exist_ok=True)
    for n, after in ((1, "a" * 40), (2, "b" * 40)):
        handle = "held-%016x" % n
        (notices / (handle + ".json")).write_text(json.dumps(
            dict(project="tcp-q7m4", file=handle + ".md", after=after)))
    (root / "DEPLOYED").write_text(json.dumps(dict(state="done", target="b" * 40, commits=["b" * 40])))
    asks = notices / "dev"
    asks.mkdir()
    for n, state in ((1, "open"), (2, "closed")):
        data = dict(project="tcp-q7m4", title="review request %d" % n, state=state,
                    blocks=True, created="2026-11-11T12:00:00+00:00")
        (asks / ("2026-11-11-tcp-q7m4-%d.md" % n)).write_text("<!-- %s -->\nrequest\n" % json.dumps(data))
    calls = []
    def collect(p=None, **kwargs):
        calls.append(kwargs)
        return [ownerwait.Item("tcp-q7m4", "review decision", "project session")], 0
    monkeypatch.setattr(ownerwait, "collect", collect)
    assert cli.main(["handover", "write"]) == 0
    text = written(root)
    assert "B-012" in text and "B-013" not in text
    assert "held-0000000000000001.md" in text
    assert "held-0000000000000002.md" not in text
    assert "review request 1" in text and "review request 2" not in text
    assert "review decision" in text and calls == [{}]


def test_unsafe_source_is_refused_without_replacing_previous_handover(desk, capsys, monkeypatch):
    root, _ = desk
    assert cli.main(["handover", "write"]) == 0
    before = written(root)
    for unsafe in (fx.CUSTOMER_FORMS[0], "pass" + "word=secret-value-ux13"):
        monkeypatch.setattr(ownerwait, "collect", lambda p: (
            [ownerwait.Item("tcp-q7m4", unsafe, "project session")], 0))
        assert cli.main(["handover", "write"]) != 0
        assert written(root) == before
        out, err = capsys.readouterr()
        assert unsafe not in out + err
        fx.assert_no_fixture_name(out + err)


def test_locked_check_keeps_previous_file(desk, monkeypatch):
    from awb import ledger
    root, _ = desk
    assert cli.main(["handover", "write"]) == 0
    before = written(root)
    def locked(*args, **kwargs):
        raise ledger.LedgerError("unavailable")
    monkeypatch.setattr(ledger, "ensure_checkable", locked)
    assert cli.main(["handover", "write"]) != 0
    assert written(root) == before


def test_compaction_writes_and_only_second_prompt_notifies_once(desk, monkeypatch, capsys):
    root, _ = desk
    for n in range(1, 4):
        run_hook("pre-compact", root, monkeypatch, capsys)
        assert "# Hand-over" in written(root)
        out = run_hook("prompt", root, monkeypatch, capsys)
        line = "second compaction: a new session with the hand-over is cheaper"
        assert (line in out) == (n == 2)
        assert line not in run_hook("prompt", root, monkeypatch, capsys)
    assert line not in run_hook("prompt", root, monkeypatch, capsys, "another-session")
    state = "\n".join(p.read_text() for p in (root / "build/.handover-sessions").glob("*.json"))
    assert "session-13" not in state


def test_session_start_prints_existing_handover_time(desk, monkeypatch, capsys):
    root, _ = desk
    assert cli.main(["handover", "write"]) == 0
    target = root / "build/HANDOVER.md"
    os.utime(target, (1794398760, 1794398760))
    capsys.readouterr()
    out = run_hook("session-start", root, monkeypatch, capsys)
    assert "hand-over: build/HANDOVER.md, written 12:06" in out
    target.unlink()
    assert "hand-over:" not in run_hook("session-start", root, monkeypatch, capsys)


def test_owner_settings_include_all_three_hooks(desk):
    from awb import handover
    assert callable(getattr(handover, "main", None))
    settings = hooks.owner_client_settings("awb hook")["hooks"]
    for event, name in (("PreCompact", "pre-compact"), ("SessionStart", "session-start"),
                        ("UserPromptSubmit", "prompt")):
        assert settings[event][0]["hooks"][0]["command"] == "awb hook " + name
    assert "PreCompact" not in hooks.client_settings("awb hook")["hooks"]


def test_owner_only_and_linked_output_refused(desk, monkeypatch):
    root, _ = desk
    assert cli.main(["handover", "write"]) == 0
    target = root / "build/HANDOVER.md"
    before = target.read_text()
    monkeypatch.setattr(config, "is_work_user", lambda: True)
    assert cli.main(["handover", "write"]) != 0
    assert target.read_text() == before
    monkeypatch.setattr(config, "is_work_user", lambda: False)
    target.unlink()
    outside = root / "keep.md"
    outside.write_text("keep\n")
    target.symlink_to(outside)
    assert cli.main(["handover", "write"]) != 0
    assert outside.read_text() == "keep\n"


def test_owner_status_commands_name_the_home_as_tilde():
    from pathlib import Path as _P

    line = "    awb start project --from %s/export-x --trusted --goal \"...\"" % _P.home()
    assert handover._home_as_tilde(line) == "    awb start project --from ~/export-x --trusted --goal \"...\""
    assert handover._home_as_tilde("no path here") == "no path here"


def test_the_git_log_gets_the_secret_check_only(desk, monkeypatch):
    """2026-11-14: a URL in an old commit subject refused the whole hand-over."""
    from awb import devask

    seen = []
    real = devask.screen
    monkeypatch.setattr(devask, "screen", lambda p, text: seen.append(text) or real(p, text))
    git(Path.cwd(), "-c", "user.name=fixture", "-c", "user.email=fixture@example.invalid",
        "commit", "-q", "--allow-empty", "-m", "docs: see https://docs.example.invalid/x for the limit")
    assert cli.main(["handover", "write"]) == 0
    assert seen and "docs.example.invalid" not in seen[-1]
    assert "docs.example.invalid" in (Path.cwd() / "build" / "HANDOVER.md").read_text()


# These cases use only the temporary checkout supplied by desk, including under a source export.
_REAL_BUILD_LINES = handover._build_lines


def test_hostile_arguments_never_echo_values(capsys):
    secret = "pass" + "word=handover-argument"
    for value in ("bad'quoted'" + secret, "bad'" + secret, 'bad"' + secret, "bad\n" + secret):
        with pytest.raises(SystemExit) as exc:
            handover.main([value])
        assert exc.value.code != 0
        out, err = capsys.readouterr()
        assert secret not in out + err
        assert value not in out + err


def test_output_files_are_bounded_regular_files(desk, monkeypatch):
    import stat

    root, _ = desk
    folder = root / "build"
    folder.mkdir()
    target = folder / "HANDOVER.md"
    # Inspect the descriptor before reading, so a regression fails without hanging on the FIFO.
    real_io_open = io.open
    def guarded_path(path, *args, **kwargs):
        if isinstance(path, (str, Path)) and Path(path) == target and target.exists():
            assert not stat.S_ISFIFO(target.stat().st_mode), "blocking FIFO read"
        return real_io_open(path, *args, **kwargs)
    monkeypatch.setattr(io, "open", guarded_path)
    real_fdopen = os.fdopen
    def guarded(fd, *args, **kwargs):
        if stat.S_ISFIFO(os.fstat(fd).st_mode):
            assert os.get_blocking(fd) is False
        return real_fdopen(fd, *args, **kwargs)
    monkeypatch.setattr(os, "fdopen", guarded)
    os.mkfifo(target)
    # Keep a writer open to make a blocking implementation observable without a timeout.
    keep = os.open(target, os.O_RDWR | os.O_NONBLOCK)
    try:
        os.write(keep, b"old\n")
        with pytest.raises(handover.HandoverError):
            handover.write(root)
    finally:
        os.close(keep)
        target.unlink()
    target.write_bytes(b"x" * (8 * 1024 * 1024 + 1))
    with pytest.raises(handover.HandoverError):
        handover.write(root)
    assert target.stat().st_size == 8 * 1024 * 1024 + 1
    target.write_text("previous")
    from awb import devask
    monkeypatch.setattr(devask, "screen", lambda *args: None)
    monkeypatch.setattr(handover, "_build_lines", lambda: ["B-012  ready  " + "x" * (8 * 1024 * 1024)])
    with pytest.raises(handover.HandoverError):
        handover.write(root)
    assert target.read_text() == "previous"


def test_previous_read_errors_keep_file_and_hide_values(desk, monkeypatch, capsys):
    root, _ = desk
    handover.write(root)
    target = root / "build/HANDOVER.md"
    before = target.read_bytes()
    secret = "pass" + "word=read-error"
    real_open = os.open
    def unreadable(path, *args, **kwargs):
        if Path(path).name == target.name:
            raise PermissionError(secret)
        return real_open(path, *args, **kwargs)
    real_io_open = io.open
    def unreadable_stream(path, *args, **kwargs):
        if isinstance(path, (str, Path)) and Path(path).name == target.name:
            raise PermissionError(secret)
        return real_io_open(path, *args, **kwargs)
    with monkeypatch.context() as patch:
        patch.setattr(os, "open", unreadable)
        patch.setattr(io, "open", unreadable_stream)
        assert handover.main(["write"]) == 2
    assert target.read_bytes() == before
    out, err = capsys.readouterr()
    assert not out and secret not in err
    assert "previous hand-over kept" in err


def test_invalid_utf8_previous_keeps_cursor(desk, capsys):
    root, _ = desk
    folder = root / "build"
    folder.mkdir()
    target = folder / "HANDOVER.md"
    before = b"\xff\n<!-- handover-head: " + b"a" * 40 + b" -->\n"
    target.write_bytes(before)
    assert handover.main(["write"]) == 2
    assert target.read_bytes() == before
    assert "Unicode" not in capsys.readouterr().err


def test_build_directory_links_never_write_outside(desk):
    root, _ = desk
    outside = root.parent / "outside"
    outside.mkdir()
    (root / "build").symlink_to(outside, target_is_directory=True)
    with pytest.raises((OSError, handover.HandoverError)):
        handover.write(root)
    assert list(outside.iterdir()) == []


def test_directory_swap_during_write_keeps_both_locations(desk, monkeypatch):
    root, _ = desk
    handover.write(root)
    before = written(root)
    outside = root.parent / "outside"
    outside.mkdir()
    target = outside / "HANDOVER.md"
    target.write_text("outside stays\n")
    def swap():
        (root / "build").rename(root / "old-build")
        (root / "build").symlink_to(outside, target_is_directory=True)
        return []
    monkeypatch.setattr(handover, "_build_lines", swap)
    with pytest.raises((OSError, handover.HandoverError)):
        handover.write(root)
    assert target.read_text() == "outside stays\n"
    assert (root / "old-build/HANDOVER.md").read_text() == before
    assert sorted(p.name for p in outside.iterdir()) == ["HANDOVER.md"]
    assert not list((root / "old-build").glob(".handover-*"))


def test_lock_rejects_links_and_special_files(desk):
    root, _ = desk
    folder = root / "build"
    folder.mkdir()
    lock = folder / ".handover.lock"
    outside = root.parent / "outside-lock"
    outside.write_text("untouched")
    for kind in ("symlink", "hardlink", "fifo"):
        if kind == "symlink":
            lock.symlink_to(outside)
        elif kind == "hardlink":
            os.link(outside, lock)
        else:
            os.mkfifo(lock)
        try:
            with pytest.raises((OSError, handover.HandoverError)):
                handover.write(root)
            assert not (folder / "HANDOVER.md").exists()
            assert outside.read_text() == "untouched"
        finally:
            lock.unlink()


def test_legacy_queue_links_refused(desk):
    root, _ = desk
    handover.write(root)
    before = written(root)
    outside = root.parent / "outside-queue"
    outside.write_text("| ID | What | State |\n| UX13 | private task | waiting |\n")
    legacy = root / "QUEUE.md"
    legacy.symlink_to(outside)
    with pytest.raises((OSError, handover.HandoverError)):
        handover.write(root)
    assert written(root) == before
    legacy.unlink()
    legacy.symlink_to(outside.with_name("missing"))
    with pytest.raises((OSError, handover.HandoverError)):
        handover.write(root)
    assert written(root) == before


def test_malformed_legacy_row_preserves_previous(desk, capsys):
    root, _ = desk
    handover.write(root)
    before = written(root)
    secret = "pass" + "word=malformed-row"
    (root / "QUEUE.md").write_text("| ID | What | State |\n| UX13 | " + secret + " |\n")
    assert handover.main(["write"]) == 2
    assert written(root) == before
    assert secret not in "".join(capsys.readouterr())


def test_all_rendered_sections_are_screened(desk, monkeypatch, capsys):
    from awb import devask

    root, _ = desk
    handover.write(root)
    before = written(root)
    for unsafe in (fx.CUSTOMER_FORMS[0], "pass" + "word=secret-value-ux13-section"):
        sources = (
            (handover, "_queue_lines", lambda: [unsafe]),
            (handover, "_build_lines", lambda: ["B-012  ready  " + unsafe]),
            (projects, "held_notes", lambda p: [dict(project="tcp-q7m4", file=unsafe, after="a" * 40)]),
            (devask, "open_asks", lambda p: [(Path(unsafe), dict(project="tcp-q7m4", title="review", blocks=False))]),
            (ownerwait, "notes", lambda **kw: [unsafe]),
        )
        for module, name, reader in sources:
            with monkeypatch.context() as patch:
                patch.setattr(module, name, reader)
                assert handover.main(["write"]) == 2
            assert written(root) == before
            assert unsafe not in "".join(capsys.readouterr())


def test_git_subject_is_screened_before_cursor_advances(desk, capsys):
    root, _ = desk
    handover.write(root)
    before = written(root)
    secret = "pass" + "word=secret-value-ux13-commit"
    git(root, "-c", "user.name=fixture", "-c", "user.email=fixture@example.invalid",
        "commit", "--allow-empty", "-qm", secret)
    assert handover.main(["write"]) == 2
    assert written(root) == before
    assert secret not in "".join(capsys.readouterr())


def test_source_exceptions_never_echo_values(desk, monkeypatch, capsys):
    import subprocess

    root, _ = desk
    handover.write(root)
    before = written(root)
    secret = "pass" + "word=reader-error"
    for error in (RuntimeError(secret), PermissionError(secret),
                  subprocess.TimeoutExpired([secret], 1, output=secret, stderr=secret)):
        def fail():
            raise error
        monkeypatch.setattr(handover, "_build_lines", fail)
        assert handover.main(["write"]) == 2
        assert written(root) == before
        out, err = capsys.readouterr()
        assert not out and secret not in err
        assert "previous hand-over kept" in err


def test_atomic_failures_preserve_cursor_and_clean_temps(desk, monkeypatch, capsys):
    root, _ = desk
    handover.write(root)
    before = written(root)
    git(root, "-c", "user.name=fixture", "-c", "user.email=fixture@example.invalid",
        "commit", "--allow-empty", "-qm", "retry this commit")
    secret = "pass" + "word=storage-error"
    for operation in ("fsync", "replace"):
        with monkeypatch.context() as patch:
            def fail(*args, **kwargs):
                raise OSError(secret)
            patch.setattr(os, operation, fail)
            assert handover.main(["write"]) == 2
        assert written(root) == before
        assert not list((root / "build").glob(".handover-*"))
        assert secret not in "".join(capsys.readouterr())
    handover.write(root)
    assert "retry this commit" in written(root)
    assert (root / "build/HANDOVER.md").stat().st_mode & 0o777 == 0o600


def test_git_environment_cannot_redirect_history(desk, monkeypatch):
    root, _ = desk
    outside = root.parent / "other-repo"
    outside.mkdir()
    git(outside, "init", "-q")
    git(outside, "-c", "user.name=fixture", "-c", "user.email=fixture@example.invalid",
        "commit", "--allow-empty", "-qm", "unrelated history")
    monkeypatch.setenv("GIT_DIR", str(outside / ".git"))
    monkeypatch.setenv("GIT_WORK_TREE", str(outside))
    handover.write(root)
    assert "UX13 initial" in written(root)
    assert "unrelated history" not in written(root)


def test_git_config_cannot_inject_decorations(desk):
    root, _ = desk
    git(root, "config", "log.decorate", "full")
    git(root, "tag", "unrequested-label")
    handover.write(root)
    assert "unrequested-label" not in written(root)
    assert "UX13 initial" in written(root)


def test_git_failures_and_invalid_heads_preserve_previous(desk, monkeypatch, capsys):
    import subprocess

    root, _ = desk
    handover.write(root)
    before = written(root)
    secret = "pass" + "word=git-error"
    for status, output in ((1, "a" * 40), (0, "--" + secret)):
        with monkeypatch.context() as patch:
            patch.setattr(subprocess, "run", lambda *a, **kw: subprocess.CompletedProcess(
                a[0], status, stdout=output, stderr=secret))
            assert handover.main(["write"]) == 2
        assert written(root) == before
        assert secret not in "".join(capsys.readouterr())


def test_build_subprocess_captures_errors_and_uses_this_tree(monkeypatch):
    import subprocess
    import sys

    secret = "pass" + "word=build-error"
    seen = []
    def run(command, **kwargs):
        seen.append((command, kwargs))
        return subprocess.CompletedProcess(command, 2, stdout=secret, stderr=secret)
    monkeypatch.setattr(subprocess, "run", run)
    with pytest.raises(handover.HandoverError) as exc:
        _REAL_BUILD_LINES()
    assert secret not in str(exc.value)
    command, kwargs = seen[0]
    assert command[:3] == [sys.executable, "-I", "-c"]
    assert str(Path(handover.__file__).resolve().parent.parent) in command[3]
    assert kwargs["capture_output"] is True and kwargs["timeout"] == 30
    assert kwargs["stdin"] == subprocess.DEVNULL


def test_start_notice_tolerates_disappearing_or_unreadable_file(desk, monkeypatch):
    root, _ = desk
    handover.write(root)
    target = root / "build/HANDOVER.md"
    real_stat = Path.stat
    for error in (FileNotFoundError(), PermissionError()):
        count = 0
        def vanished(path, *args, **kwargs):
            nonlocal count
            if path == target and kwargs.get("follow_symlinks", True):
                count += 1
                if count >= 2:
                    raise error
            return real_stat(path, *args, **kwargs)
        with monkeypatch.context() as patch:
            patch.setattr(Path, "stat", vanished)
            assert handover.start_line(root) == ""


def test_outside_checkout_and_build_user_create_nothing(desk, monkeypatch):
    root, _ = desk
    outside = root.parent / "not-a-repository"
    outside.mkdir()
    with pytest.raises(handover.HandoverError):
        handover.write(outside)
    assert handover.start_line(outside) == ""
    assert handover.compaction(outside, "session") == ""
    assert list(outside.iterdir()) == []
    monkeypatch.setattr(config, "is_build_user", lambda: True)
    with pytest.raises(handover.HandoverError):
        handover.write(root)
    assert handover.compaction(root, "session") == ""
    assert not (root / "build").exists()


def test_compaction_rejects_malformed_and_extended_receipts(desk):
    import hashlib

    root, _ = desk
    handover.compaction(root, "receipt")
    path = root / "build/.handover-sessions" / (hashlib.sha256(b"receipt").hexdigest() + ".json")
    secret = "pass" + "word=receipt-value"
    cases = ["[]", "{", '{"count":true,"shown":false}', '{"count":-1,"shown":false}',
             '{"count":1,"shown":0}', "[" * 2000, "x" * 4097,
             json.dumps(dict(count=1, shown=False, extra=secret))]
    for raw in cases:
        path.write_text(raw)
        with pytest.raises(handover.HandoverError) as exc:
            handover.compaction(root, "receipt")
        assert secret not in str(exc.value)
        assert path.read_text() == raw


def test_compaction_links_never_touch_outside(desk):
    import hashlib

    root, _ = desk
    handover.compaction(root, "receipt")
    marks = root / "build/.handover-sessions"
    receipt = marks / (hashlib.sha256(b"receipt").hexdigest() + ".json")
    outside = root.parent / "outside-receipt"
    original = '{"count":1,"shown":false}'
    outside.write_text(original)
    receipt.unlink()
    receipt.symlink_to(outside)
    with pytest.raises((OSError, handover.HandoverError)):
        handover.compaction(root, "receipt")
    assert outside.read_text() == original
    receipt.unlink()
    marks.rmdir()
    marks.symlink_to(root.parent, target_is_directory=True)
    with pytest.raises((OSError, handover.HandoverError)):
        handover.compaction(root, "receipt")
    assert not (root.parent / receipt.name).exists()


def test_late_prompt_still_claims_compaction_notice_once(desk):
    root, _ = desk
    for _ in range(3):
        handover.compaction(root, "late-session")
    assert handover.compaction(root, "late-session", prompt=True) == handover.SECOND_COMPACTION
    assert handover.compaction(root, "late-session", prompt=True) == ""


def test_session_identifier_is_hashed_and_invalid_ids_are_ignored(desk):
    import hashlib

    root, _ = desk
    for sid in (None, 12, {}, [], ""):
        assert handover.compaction(root, sid) == ""
    assert not (root / "build").exists()
    sid = "../../" + "pass" + "word=session-value"
    handover.compaction(root, sid)
    files = list((root / "build/.handover-sessions").iterdir())
    assert [p.name for p in files] == [hashlib.sha256(sid.encode()).hexdigest() + ".json"]
    assert sid not in files[0].read_text()
    assert files[0].stat().st_mode & 0o777 == 0o600


def test_concurrent_writes_serialize_cursor_updates(desk, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    import threading

    root, _ = desk
    entered = threading.Event()
    release = threading.Event()
    second_started = threading.Event()
    second_read = threading.Event()
    real_git = handover._git
    calls = 0
    def controlled_git(*args):
        nonlocal calls
        if args[1] == "rev-parse":
            calls += 1
            if calls == 1:
                entered.set()
                assert release.wait(5)
            else:
                second_read.set()
        return real_git(*args)
    monkeypatch.setattr(handover, "_git", controlled_git)
    def second():
        second_started.set()
        return handover.write(root)
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(handover.write, root)
        try:
            assert entered.wait(5)
            other = pool.submit(second)
            assert second_started.wait(5)
            assert not second_read.wait(0.2)
        finally:
            release.set()
        assert first.result(timeout=5) == other.result(timeout=5)
    assert "UX13 initial" not in written(root)
    assert not list((root / "build").glob(".handover-*"))


def test_concurrent_compactions_and_prompts_do_not_lose_claims(desk, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    import threading
    import time

    root, _ = desk
    handover.compaction(root, "parallel")
    marks = root / "build/.handover-sessions"
    real_loads = json.loads
    def delayed_loads(*args, **kwargs):
        state = real_loads(*args, **kwargs)
        time.sleep(0.02)
        return state
    monkeypatch.setattr(json, "loads", delayed_loads)
    barrier = threading.Barrier(4)
    def compact(prompt):
        barrier.wait(timeout=5)
        return handover.compaction(root, "parallel", prompt=prompt)
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = [pool.submit(compact, False) for _ in range(4)]
        assert [f.result(timeout=5) for f in results] == [""] * 4
        assert real_loads(next(marks.iterdir()).read_text())["count"] == 5
        next(marks.iterdir()).write_text(json.dumps(dict(count=2, shown=False)))
        results = [pool.submit(compact, True) for _ in range(4)]
        assert [f.result(timeout=5) for f in results].count(handover.SECOND_COMPACTION) == 1


def test_queue_links_cannot_read_or_overwrite_external_files(desk):
    root, _ = desk
    handover.write(root)
    before = written(root)
    external = root.parent / "external"
    workqueue._write(root / "queue.tsv", [workqueue._new("Q-0001", "review result", "check result", "owner")])
    # Include the cache's temporary output: workqueue.load writes it before replacing the cache.
    for name in ("queue.tsv", "queue-done.tsv", ".queue.lock", "queue-checked.json", "queue-checked.json.tmp"):
        path = root / name
        if name == "queue.tsv":
            external.write_bytes(path.read_bytes())
        elif name == "queue-done.tsv":
            workqueue._write(external, [])
        else:
            external.write_text("[]")
        external_before = external.read_bytes()
        saved = path.read_bytes() if path.exists() else None
        path.unlink(missing_ok=True)
        path.symlink_to(external)
        try:
            with pytest.raises(handover.HandoverError):
                handover.write(root)
            assert external.read_bytes() == external_before
            assert written(root) == before
        finally:
            path.unlink()
            if saved is not None:
                path.write_bytes(saved)


def test_queue_special_files_and_linked_parent_refused(desk, monkeypatch):
    root, _ = desk
    handover.write(root)
    before = written(root)
    queue = root / "queue.tsv"
    os.mkfifo(queue)
    # The preflight must reject this before the downstream queue reader can block.
    real_load = workqueue.load
    def no_fifo(path=None):
        import stat
        assert not stat.S_ISFIFO(queue.lstat().st_mode), "blocking queue read"
        return real_load(path)
    monkeypatch.setattr(workqueue, "load", no_fifo)
    with pytest.raises(handover.HandoverError):
        handover.write(root)
    queue.unlink()
    external = root.parent / "queue-folder"
    external.mkdir()
    link = root / "queue-link"
    link.symlink_to(external, target_is_directory=True)
    monkeypatch.setenv("AWB_WORK_QUEUE", str(link / "queue.tsv"))
    with pytest.raises(handover.HandoverError):
        handover.write(root)
    assert list(external.iterdir()) == []
    assert written(root) == before


def test_malformed_modern_queue_preserves_previous(desk, capsys):
    root, _ = desk
    handover.write(root)
    before = written(root)
    secret = "pass" + "word=queue-row-value"
    (root / "queue.tsv").write_text("id\twhat\nQ-0001\t" + secret + "\n")
    assert handover.main(["write"]) == 2
    assert written(root) == before
    assert secret not in "".join(capsys.readouterr())


def test_receipt_directory_swap_cannot_redirect_write(desk, monkeypatch):
    root, _ = desk
    handover.compaction(root, "receipt")
    marks = root / "build/.handover-sessions"
    receipt = next(marks.iterdir())
    before = receipt.read_bytes()
    external = root.parent / "external-marks"
    external.mkdir()
    real_loads = json.loads
    def swap(*args, **kwargs):
        state = real_loads(*args, **kwargs)
        marks.rename(marks.with_name("old-marks"))
        marks.symlink_to(external, target_is_directory=True)
        return state
    monkeypatch.setattr(json, "loads", swap)
    with pytest.raises((OSError, handover.HandoverError)):
        handover.compaction(root, "receipt")
    assert list(external.iterdir()) == []
    assert (marks.with_name("old-marks") / receipt.name).read_bytes() == before
