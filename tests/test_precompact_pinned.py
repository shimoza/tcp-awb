"""Q-0006: the pinned block of STATE.md is kept across a compaction (finding 3 of the UX review of 2026-11-18).

A project session that was compacted lost the reply language, the limits the owner set, the deliverables with their
bucket paths and the running chains. The session then asked him again. STATE.md keeps them in a `## Pinned` block
under its Status: and Next: lines. The PreCompact hook `awb hook compact` hands the two lines and the block to the
summary as plain text and the start with source `compact` prints them before every file, both checked like STATE.md.
The stop hook holds a reply while the block lost its shape. Temporary project folders and invented data only.
"""
from __future__ import annotations

import json

import pytest

from awb import hooks, projects, status
from tests import fixtures
from tests.test_hooks import (CODE, assert_clean, context_of, fake_review, missing_module,  # noqa: F401
                              project, run_hook)
from tests.test_status import T0, commit, git

PINNED = ("## Pinned\n\n"
          "- Language: Russian for the replies, English in files.\n"
          "- Limit: the customer software on vm-01 and vm-02 stays as it is, never change inside.\n"
          "- Deliverable: the operating manual v3, bucket awb 2026-11/tcp-q7m4/out/manual-v3.pdf\n"
          "- Chain: the nightly import of the lab logs runs until the end of the test.\n")
STATE = ("# State of tcp-q7m4\n\nStatus: The appliance image is imported.\nNext: The boot test.\n\n" + PINNED
         + "\n## Now\n\n- the boot test waits on the network\n")


def payload(root, **extra) -> dict:
    data = {"cwd": str(root), "session_id": "a-session-of-q6"}
    data.update(extra)
    return data


# --------------------------------------------------------------------------- the block in STATE.md


def test_the_block_is_read_from_its_heading_to_the_next_heading():
    body = status.pinned(STATE)
    assert body is not None and len(body) == 4
    assert body[0].startswith("- Language: Russian") and body[-1].startswith("- Chain:")
    assert status.pinned("# State\n\nStatus: a.\nNext: b.\n\n## Now\n") is None
    assert status.pin_shape(STATE) is None and status.pin_shape("# State\n\n## Now\n") is None


def test_the_block_text_carries_the_two_lines_and_the_entries():
    text = status.pin_text(STATE)
    assert text.splitlines()[:2] == ["Status: The appliance image is imported.", "Next: The boot test."]
    assert "## Pinned" in text and "manual-v3.pdf" in text and "the boot test waits" not in text
    assert status.pin_text("# State\n\n## Now\n") is None


def test_a_block_over_its_size_is_cut_for_the_compaction():
    many = "".join("- Limit: limit number %d stays.\n" % n for n in range(30))
    text = status.pin_text("# State\n\nStatus: a.\nNext: b.\n\n## Pinned\n\n" + many)
    entries = [line for line in text.splitlines() if line.startswith("- ")]
    assert len(entries) == status.PIN_MAX_LINES
    wide = "".join("- Limit: %s\n" % ("x" * 400) for _ in range(6))
    text = status.pin_text("# State\n\n## Pinned\n\n" + wide)
    assert sum(len(line) for line in text.splitlines() if line.startswith("- ")) <= status.PIN_MAX_CHARS


@pytest.mark.parametrize("block, said", [
    ("".join("- Limit: limit %d.\n" % n for n in range(13)), "13 lines"),
    ("- Limit: %s\n" % ("y" * 1600), "at most 12 and 1500"),
    ("- Mood: calm.\n- Language: English.\n", "1 line(s) of the ## Pinned block are no `- Kind: text` entry"),
    ("- Language: English.\n\n## Pinned\n\n- Chain: none.\n", "2 ## Pinned blocks"),
])
def test_a_block_that_lost_its_shape_is_named_with_its_counts(block, said):
    text = "# State\n\nStatus: a.\nNext: b.\n\n## Pinned\n\n" + block
    assert said in status.pin_shape(text)


def test_a_block_below_the_head_of_state_is_held():
    text = "# State\n\nStatus: a.\nNext: b.\n" + "line\n" * 40 + "## Pinned\n\n- Language: English.\n"
    assert "keep it within the first 40 lines" in status.pin_shape(text)


def test_a_new_project_starts_with_a_block_in_shape():
    text = projects._state(CODE)
    assert status.pinned(text) is not None and status.pin_shape(text) is None
    assert text.index("## Pinned") < text.index("## Now")


# --------------------------------------------------------------------------- the stop hook keeps the shape


def test_the_stop_hook_holds_a_reply_while_the_block_lost_its_shape(project, monkeypatch, capsys):  # noqa: F811
    git(project, "init", "-q")
    commit(project, "STATE.md", STATE, T0)
    fake_review(monkeypatch, [])
    assert run_hook("stop", payload(project), monkeypatch, capsys) == (0, "", "")
    grown = STATE.replace("## Now", "".join("- Limit: limit %d.\n" % n for n in range(12)) + "\n## Now")
    commit(project, "STATE.md", grown, T0 + 100)
    code, _, err = run_hook("stop", payload(project), monkeypatch, capsys)
    assert code == 2 and "pinned block of STATE.md lost its shape" in err and "16 lines" in err
    assert run_hook("stop", payload(project, stop_hook_active=True), monkeypatch, capsys) == (0, "", "")
    commit(project, "STATE.md", STATE, T0 + 200)
    assert run_hook("stop", payload(project), monkeypatch, capsys) == (0, "", "")


# --------------------------------------------------------------------------- the PreCompact hook


def test_the_compaction_gets_the_two_lines_and_the_block_as_plain_text(project, monkeypatch, capsys):  # noqa: F811
    (project / "STATE.md").write_text(STATE, encoding="utf-8")
    code, out, err = run_hook("compact", payload(project, hook_event_name="PreCompact", trigger="auto",
                                                 custom_instructions=None), monkeypatch, capsys)
    assert (code, err) == (0, "")
    with pytest.raises(ValueError):
        json.loads(out)                      # the client takes plain text as summary instructions here
    assert out.startswith(hooks.COMPACT_LEAD)
    for line in ("Status: The appliance image is imported.", "Next: The boot test.", "- Language: Russian",
                 "- Limit: the customer software on vm-01 and vm-02", "manual-v3.pdf", "- Chain: the nightly"):
        assert line in out, line
    assert "the boot test waits on the network" not in out
    assert_clean(out, "compaction text")


def test_a_block_with_a_name_is_withheld_from_the_compaction(project, monkeypatch, capsys):  # noqa: F811
    named = STATE.replace("the nightly import", "the call with %s" % fixtures.PERSON_FORMS[0])
    (project / "STATE.md").write_text(named, encoding="utf-8")
    code, out, err = run_hook("compact", payload(project), monkeypatch, capsys)
    assert code == 0 and err == ""
    assert "withheld" not in out and "the call with %s" % fixtures.PERSON_FORMS[0] in out    # DMZ 2: as written
    assert "- Language:" in out


def test_a_block_is_withheld_when_the_name_check_cannot_run(project, monkeypatch, capsys):  # noqa: F811
    (project / "STATE.md").write_text(STATE, encoding="utf-8")

    def down(*args, **kwargs):
        raise hooks.Unavailable("no register")

    monkeypatch.setattr(hooks, "name_hits", down)
    code, out, _ = run_hook("compact", payload(project), monkeypatch, capsys)
    assert code == 0 and "the name check did not run" not in out and "- Language:" in out    # DMZ 2: no check runs


def test_a_bidi_override_withholds_the_block(project, monkeypatch, capsys):  # noqa: F811
    (project / "STATE.md").write_text(STATE.replace("English in files", "English ‮ in files"),
                                      encoding="utf-8")
    code, out, _ = run_hook("compact", payload(project), monkeypatch, capsys)
    assert code == 0 and "bidi override" in out and "- Language:" not in out


def test_the_compaction_hook_is_silent_outside_a_project_and_for_the_owner(project, home, tmp_path,  # noqa: F811
                                                                          monkeypatch, capsys):
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    assert run_hook("compact", payload(elsewhere), monkeypatch, capsys) == (0, "", "")
    (project / "STATE.md").write_text("# State\n\n## Now\n", encoding="utf-8")
    assert run_hook("compact", payload(project), monkeypatch, capsys) == (0, "", "")
    (project / "STATE.md").write_text(STATE, encoding="utf-8")
    monkeypatch.setattr(hooks, "for_this_user", lambda: False)
    assert run_hook("compact", payload(project), monkeypatch, capsys) == (0, "", "")


def test_a_broken_state_file_never_blocks_the_compaction(project, monkeypatch, capsys):  # noqa: F811
    (project / "STATE.md").write_bytes(b"\xff\xfe not text")
    assert run_hook("compact", payload(project), monkeypatch, capsys) == (0, "", "")
    (project / "STATE.md").unlink()
    (project / "STATE.md").symlink_to(project / "OPEN.md")
    code, out, _ = run_hook("compact", payload(project), monkeypatch, capsys)
    assert code == 0 and "size the backup" not in out


# --------------------------------------------------------------------------- the start after a compaction


def _context(project, source, monkeypatch, capsys) -> str:  # noqa: F811
    missing_module(monkeypatch, "kb")
    missing_module(monkeypatch, "career")
    code, out, err = run_hook("session-start", payload(project, hook_event_name="SessionStart", source=source),
                              monkeypatch, capsys)
    assert (code, err) == (0, "")
    return context_of(out, "SessionStart")["hookSpecificOutput"]["additionalContext"]


def test_the_start_after_a_compaction_prints_the_block_first(project, monkeypatch, capsys):  # noqa: F811
    (project / "STATE.md").write_text(STATE, encoding="utf-8")
    text = _context(project, "compact", monkeypatch, capsys)
    assert text.startswith(hooks.PIN_TITLE)
    assert text.index(hooks.PIN_TITLE) < text.index("Workbench project") < text.index("## SCOPE.md")
    head = text[:text.index("Workbench project")]
    assert "Status: The appliance image is imported." in head and "- Limit: the customer software" in head
    assert_clean(text, "start context")


def test_the_start_without_a_compaction_has_no_extra_block(project, monkeypatch, capsys):  # noqa: F811
    (project / "STATE.md").write_text(STATE, encoding="utf-8")
    for source in ("startup", "resume", "clear"):
        text = _context(project, source, monkeypatch, capsys)
        assert hooks.PIN_TITLE not in text and "## STATE.md" in text, source


def test_the_start_after_a_compaction_withholds_a_block_with_a_name(project, monkeypatch, capsys):  # noqa: F811
    (project / "STATE.md").write_text(STATE.replace("never change inside", "ask %s first" %
                                                    fixtures.PERSON_FORMS[0]), encoding="utf-8")
    text = _context(project, "compact", monkeypatch, capsys)
    assert text.startswith(hooks.PIN_TITLE) and "withheld" not in text
    assert "ask %s first" % fixtures.PERSON_FORMS[0] in text.split("Workbench project")[0]    # DMZ 2: as written


def test_the_start_asks_for_a_block_when_state_has_none(project, monkeypatch, capsys):  # noqa: F811
    text = _context(project, "startup", monkeypatch, capsys)
    assert "STATE.md has no ## Pinned block" in text.split("## Checks")[1]
    (project / "STATE.md").write_text(STATE, encoding="utf-8")
    text = _context(project, "startup", monkeypatch, capsys)
    assert "has no ## Pinned block" not in text


# --------------------------------------------------------------------------- where the hook is registered


def test_the_client_settings_run_compact_at_precompact_and_never_the_owner_handover():
    s = hooks.client_settings("/usr/local/bin/awb hook")["hooks"]
    assert [h["command"] for e in s["PreCompact"] for h in e["hooks"]] == ["/usr/local/bin/awb hook compact"]
    commands = [h["command"] for entries in s.values() for e in entries for h in e["hooks"]]
    assert not any(c.endswith(" pre-compact") for c in commands)
    assert hooks.EVENTS["compact"] == "PreCompact" and "compact" in hooks.HOOKS
    assert "compact" not in hooks.BLOCKING_HOOKS
