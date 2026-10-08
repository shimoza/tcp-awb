"""Rules that change reach every project and every running session (awb/rulesync.py).

2026-10-06: a session started on 2026-09-30 looked for a file in the vault inbox, because its project's CLAUDE.md and
the rules it had loaded predated the bucket inboxes.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

from awb import cli, rulesync
from tests.test_hooks import run_hook
from tests.test_lifecycle import spawn

OLD_CLAUDE = """# tcp-q7m4

A sealed lab of the workbench. Goal, customer code and tags are in SCOPE.md.

In this project:

- Start with SCOPE.md, STATE.md and OPEN.md. Update STATE.md and OPEN.md before the session ends.
- Codes only (project tcp-q7m4). Never write a real name, not even in a note or a commit.
- Customer material comes in only through `awb intake` and moves from the outbox into input/.
- Never open an original or the vault.
- Proof goes to evidence/, results to deliverables/, review records to reviews/.
- RESOURCES.md lists what the project uses, with platform ids and codes.
- A note of this project: the lab image lives in the lab bucket.
"""
OLD_STATE = ("# State of tcp-q7m4\n\nStatus: Started.\nNext: The first step.\n\n"
             "Where the work stands. Update it at the end of every session. Codes only, never a name.\n\n## Now\n")


def old_project(tmp_path: Path) -> Path:
    root = tmp_path / "tcp-q7m4"
    root.mkdir()
    (root / "CLAUDE.md").write_text(OLD_CLAUDE, encoding="utf-8")
    (root / "STATE.md").write_text(OLD_STATE, encoding="utf-8")
    return root


def test_the_sync_replaces_the_old_template_lines_and_keeps_the_project_notes(tmp_path):
    root = old_project(tmp_path)
    inode = (root / "CLAUDE.md").stat().st_ino
    assert rulesync.sync_project(root) == ["CLAUDE.md", "STATE.md"]
    text = (root / "CLAUDE.md").read_text(encoding="utf-8")
    assert "before the session ends" not in text and "after every step that changes the status" in text
    assert "run `awb inbox take <his words>`, never look in the vault" in text
    lines = text.splitlines()
    assert lines[lines.index("- Proof goes to evidence/, results to deliverables/, review records to reviews/.") + 1] \
        .startswith("- Files for him leave with `awb xchg put FILE`")
    assert "- A note of this project: the lab image lives in the lab bucket." in lines
    assert (root / "CLAUDE.md").stat().st_ino == inode                    # the same file: owner and mode stay
    assert "after every step that changes the status, in the same commit" in (root / "STATE.md").read_text()
    assert rulesync.sync_project(root) == []                               # a second run changes nothing


def test_a_new_project_is_current_from_the_start(home, register_path, capsys):
    pr = spawn(home, register_path)
    text = (Path(pr.path) / "CLAUDE.md").read_text(encoding="utf-8")
    assert "awb inbox take <his words>" in text and "awb xchg put FILE" in text
    assert rulesync.sync_project(Path(pr.path)) == []
    assert cli.main(["projects", "sync"]) == 0
    assert "%s: already current" % pr.code in capsys.readouterr().out


def test_a_running_session_hears_once_that_its_rules_changed(home, tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    root = old_project(tmp_path)
    assert rulesync.notice(home, None, root) is None                       # no session id: nothing to track
    assert rulesync.notice(home, "older-session", root) == rulesync.NOTICE  # older than the check: told once
    assert rulesync.notice(home, "older-session", root) is None
    rulesync.remember(home, "new-session", root)
    assert rulesync.notice(home, "new-session", root) is None
    rulesync.sync_project(root)                                            # the project's CLAUDE.md changes
    assert rulesync.notice(home, "new-session", root) == rulesync.NOTICE
    assert rulesync.notice(home, "new-session", root) is None
    assert not list((home.shared / "sessions").glob("rules-..*"))


def test_the_hooks_remember_at_the_start_and_tell_on_a_clean_prompt(home, register_path, monkeypatch, capsys):
    pr = spawn(home, register_path)
    payload = {"cwd": pr.path, "session_id": "a-session"}
    run_hook("session-start", payload, monkeypatch, capsys)
    assert run_hook("prompt", dict(payload, prompt="size the clusters"), monkeypatch, capsys) == (0, "", "")
    with open(Path(pr.path) / "CLAUDE.md", "a", encoding="utf-8") as fh:
        fh.write("- one more rule of this project\n")
    code, out, _ = run_hook("prompt", dict(payload, prompt="size the clusters"), monkeypatch, capsys)
    assert code == 0 and json.loads(out)["hookSpecificOutput"]["additionalContext"] == rulesync.NOTICE
    assert run_hook("prompt", dict(payload, prompt="size the clusters"), monkeypatch, capsys) == (0, "", "")
    assert os.path.exists(home.shared / "sessions" / "rules-a-session.json")


def test_the_sync_brings_the_import_line_into_a_project_of_the_intake(tmp_path):
    """T4: a project made before wipe mode names `awb intake` for the copies of the outbox; the sync names his
    `awb import` and the notice of new input."""
    root = old_project(tmp_path)
    rulesync.sync_project(root)
    before = (root / "CLAUDE.md").read_text(encoding="utf-8")
    (root / "CLAUDE.md").write_text(before.replace("`awb import` wait in the outbox (the prompt says \"new input\" when "
                                                   "they come)", "`awb intake` wait in the outbox"), encoding="utf-8")
    assert rulesync.sync_project(root) == ["CLAUDE.md"]
    text = (root / "CLAUDE.md").read_text(encoding="utf-8")
    assert "Copies of his own `awb import` wait in the outbox" in text and "`awb intake`" not in text


def test_the_rules_notice_and_the_input_notice_come_in_one_text(home, tmp_path, monkeypatch):
    from tests import fixtures as fx

    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    root = old_project(tmp_path)
    (home.outbox / fx.CUSTOMER_CODE).mkdir(parents=True, exist_ok=True)
    (home.outbox / fx.CUSTOMER_CODE / "F-ABCD.md").write_text("x", encoding="utf-8")
    rulesync.post_input(home, fx.CUSTOMER_CODE, root.name, ["F-ABCD"])
    said = rulesync.notice(home, "a-session", root)
    assert said.startswith(rulesync.NOTICE) and said.endswith(rulesync.INPUT % (1, fx.CUSTOMER_CODE))
    assert rulesync.notice(home, "a-session", root) is None
