"""Hook notes go into one footer line under the answer, and a notification gets no note (Q-0005).

Finding 2 of the UX review of 2026-11-18: the sessions wrote the notes of the hooks as a paragraph of their own,
often as the last one, and answered interim notifications of background agents with them. The session start now
carries the footer rule, the first note of a held stop carries the footer shape, and a prompt or a stop that
answers a notification adds no note (the notes wait). The checks, blocks and refusals are unchanged.
"""
from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

from awb import hooks, rulesync
from tests.test_hooks import context_of, fake_review, notification, project, run_hook  # noqa: F401
from tests.test_lifecycle import spawn

T0 = 1_790_000_000
STATE_TEXT = "# State of tcp-q7m4\n\nStatus: The appliance image is imported.\nNext: The boot test.\n\n## Now\n"
NOTIFICATIONS = (
    "<task-notification>\n<task-id>a3f9c21</task-id>\n<status>running</status>\n</task-notification>",
    "  [SYSTEM NOTIFICATION] the background task still waits on its tests",
    "the side sessions changed\n<side-sessions-event kind=\"ended\">s-2</side-sessions-event>",
)


def git(root: Path, *args: str, when: int | None = None) -> None:
    env = dict(os.environ, GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@example.org", GIT_COMMITTER_NAME="t",
               GIT_COMMITTER_EMAIL="t@example.org")
    if when is not None:
        env.update(GIT_AUTHOR_DATE="@%d +0000" % when, GIT_COMMITTER_DATE="@%d +0000" % when)
    subprocess.run(["git", "-C", str(root), *args], check=True, capture_output=True, env=env)


def commit(root: Path, name: str, text: str, when: int) -> None:
    (root / name).write_text(text, encoding="utf-8")
    if name == "STATE.md":
        os.utime(root / name, (when, when))
    git(root, "add", name)
    git(root, "commit", "-q", "-m", "work on %s" % name, when=when)


def transcript(tmp_path: Path, prompt: str, reply: str = "done") -> str:
    path = tmp_path / "transcript.jsonl"
    rows = [{"type": "user", "message": {"role": "user", "content": "size the clusters"}},
            {"type": "assistant", "message": {"content": [{"type": "text", "text": "sized"}]}},
            {"type": "user", "message": {"role": "user", "content": prompt}},
            {"type": "assistant", "message": {"content": [{"type": "text", "text": reply}]}}]
    path.write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")
    return str(path)


def lagging(project: Path) -> None:
    (project / "STATE.md").write_text(STATE_TEXT, encoding="utf-8")
    git(project, "init", "-q")
    commit(project, "STATE.md", STATE_TEXT, T0)
    commit(project, "notes.md", "a finding\n", T0 + 100)


# --------------------------------------------------------------------------- the shape rule


def test_the_rule_names_the_footer_prefix_the_join_and_the_two_never():
    for text in (hooks.FOOTER_RULE, hooks.STOP_FOOTER):
        assert "one footer line under your answer" in text and '"Notes:"' in text and '"; "' in text
        assert "never a paragraph" in text.lower() and "never above the answer" in text
        assert "—" not in text
    assert hooks.NOTES_PREFIX == "Notes: "
    assert "notification" in hooks.FOOTER_RULE


def test_the_session_start_carries_the_footer_rule(project, monkeypatch, capsys):  # noqa: F811
    code, out, _ = run_hook("session-start", {"cwd": str(project)}, monkeypatch, capsys)
    text = context_of(out, "SessionStart")["hookSpecificOutput"]["additionalContext"]
    assert code == 0 and hooks.FOOTER_RULE in text.splitlines()
    assert text.index("Codes only, never a name.") < text.index(hooks.FOOTER_RULE) < text.index("## SCOPE.md")


def test_a_start_without_a_project_carries_no_rule(home, tmp_path, monkeypatch, capsys):
    code, out, _ = run_hook("session-start", {"cwd": str(tmp_path)}, monkeypatch, capsys)
    assert code == 0 and hooks.FOOTER_RULE not in out


# --------------------------------------------------------------------------- the stop hook


def test_the_first_note_of_a_held_stop_carries_the_footer_shape(project, monkeypatch, capsys):  # noqa: F811
    lagging(project)
    fake_review(monkeypatch, [])
    code, _, err = run_hook("stop", {"cwd": str(project)}, monkeypatch, capsys)
    assert code == 2 and err.startswith(hooks.STOP_FOOTER) and "lag behind 1 commit(s)" in err
    assert err.count(hooks.STOP_FOOTER) == 1 and err.rstrip().endswith("then commit.")


def test_a_draft_gate_line_alone_keeps_its_text_and_comes_after_the_shaped_note(project, monkeypatch,  # noqa: F811
                                                                               capsys):
    monkeypatch.setattr(hooks, "draft_gate", lambda *a: ["show deliverables/offer.md only after a clean check"])
    fake_review(monkeypatch, [])
    code, _, err = run_hook("stop", {"cwd": str(project)}, monkeypatch, capsys)
    assert (code, err.strip()) == (2, "show deliverables/offer.md only after a clean check")
    lagging(project)
    code, _, err = run_hook("stop", {"cwd": str(project)}, monkeypatch, capsys)
    lines = err.strip().splitlines()
    assert code == 2 and lines[0].startswith(hooks.STOP_FOOTER) and "lag behind" in lines[0]
    assert "show deliverables/offer.md only after a clean check" in lines and err.count(hooks.STOP_FOOTER) == 1


@pytest.mark.parametrize("event", NOTIFICATIONS)
def test_a_stop_after_a_notification_adds_no_note_and_the_note_waits(project, tmp_path, monkeypatch, capsys,  # noqa: F811
                                                                    event):
    lagging(project)
    fake_review(monkeypatch, [])
    said = {"cwd": str(project), "transcript_path": transcript(tmp_path, event)}
    assert run_hook("stop", said, monkeypatch, capsys) == (0, "", "")
    typed = {"cwd": str(project), "transcript_path": transcript(tmp_path, "and the backup?")}
    code, _, err = run_hook("stop", typed, monkeypatch, capsys)
    assert code == 2 and err.startswith(hooks.STOP_FOOTER) and "lag behind 1 commit(s)" in err


def test_a_stop_after_a_notification_keeps_the_draft_gate(project, tmp_path, monkeypatch, capsys):  # noqa: F811
    monkeypatch.setattr(hooks, "draft_gate", lambda *a: ["show deliverables/offer.md only after a clean check"])
    lagging(project)
    fake_review(monkeypatch, [])
    said = {"cwd": str(project), "transcript_path": transcript(tmp_path, NOTIFICATIONS[0])}
    code, _, err = run_hook("stop", said, monkeypatch, capsys)
    assert (code, err.strip()) == (2, "show deliverables/offer.md only after a clean check")


def test_a_stop_after_a_notification_drafts_no_ledger_entry(project, tmp_path, monkeypatch, capsys):  # noqa: F811
    calls = []
    monkeypatch.setattr(hooks, "draft_ledger", lambda *a: calls.append(a) or False)
    fake_review(monkeypatch, [])
    run_hook("stop", {"cwd": str(project), "transcript_path": transcript(tmp_path, NOTIFICATIONS[1])},
             monkeypatch, capsys)
    assert calls == []
    run_hook("stop", {"cwd": str(project), "transcript_path": transcript(tmp_path, "go on")}, monkeypatch, capsys)
    assert len(calls) == 1


def test_the_turn_prompt_skips_tool_results_and_side_chains(tmp_path):
    path = tmp_path / "t.jsonl"
    rows = [{"type": "user", "message": {"content": NOTIFICATIONS[0]}},
            {"type": "user", "message": {"content": [{"type": "tool_result", "tool_use_id": "x", "content": "ok"}]}},
            {"type": "user", "isSidechain": True, "message": {"content": "an agent's own prompt"}},
            "not an object", {"type": "user", "message": "no object"}]
    path.write_text("".join(json.dumps(r) + "\n" for r in rows) + "{broken\n", encoding="utf-8")
    assert hooks.turn_prompt(str(path)) == NOTIFICATIONS[0]
    assert hooks.turn_prompt(str(tmp_path / "missing.jsonl")) is None and hooks.turn_prompt(None) is None


# --------------------------------------------------------------------------- the prompt hook


def test_is_notification_takes_the_three_shapes_and_nothing_else():
    assert all(hooks.is_notification(t) for t in NOTIFICATIONS)
    for text in ("size the clusters", "what does <task-notification> mean here?", "", None, 7,
                 "a [SYSTEM NOTIFICATION] in the middle"):
        assert not hooks.is_notification(text)


@pytest.mark.parametrize("event", NOTIFICATIONS)
def test_a_notification_prompt_gets_no_note_and_the_note_waits(home, register_path, monkeypatch, capsys, event):
    pr = spawn(home, register_path)
    payload = {"cwd": pr.path, "session_id": "a-session"}
    run_hook("session-start", payload, monkeypatch, capsys)
    with open(Path(pr.path) / "CLAUDE.md", "a", encoding="utf-8") as fh:
        fh.write("- one more rule of this project\n")
    assert run_hook("prompt", dict(payload, prompt=event), monkeypatch, capsys) == (0, "", "")
    code, out, _ = run_hook("prompt", dict(payload, prompt="size the clusters"), monkeypatch, capsys)
    assert code == 0 and json.loads(out)["hookSpecificOutput"]["additionalContext"] == rulesync.NOTICE


def test_a_notification_keeps_its_data_line_and_its_block(home, monkeypatch, capsys):
    from tests import fixtures
    result = "checked 198.51.100.23, 198.51.100.24 and https://build.qrtvb.io/run/42: all three answer"
    code, out, err = run_hook("prompt", {"prompt": notification(result)}, monkeypatch, capsys)
    line = context_of(out, "UserPromptSubmit")["hookSpecificOutput"]["additionalContext"]
    assert (code, err) == (0, "")
    assert line == "The agent result carries 3 data items (ip 2, url 1); never repeat them, refer to them by line."
    code, out, err = run_hook("prompt", {"prompt": notification("the draft for %s" % fixtures.CUSTOMER_FORMS[0])},
                              monkeypatch, capsys)
    assert code == 2 and out == "" and "was not delivered" in err
