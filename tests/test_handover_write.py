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
