"""Review of awb/hooks.py (2026-11-15): hostile hook input, paths that slipped past the write guards, a hook that
failed open on a crash or ran past its budget and the draft gate on an odd transcript.

Each test failed on the code before the review. Secrets come from the gate's own selftest cases, never whole here.
"""
from __future__ import annotations

import io
import json
import os
import random
import subprocess
import sys
import time
from pathlib import Path

import pytest

from awb import config, hooks
from tests import fixtures

CODE = "tcp-q7m4"
REPO = Path(__file__).resolve().parents[1]


def run_hook(name: str, payload, monkeypatch, capsys) -> tuple[int, str, str]:
    raw = payload if isinstance(payload, str) else json.dumps(payload)
    monkeypatch.setattr(sys, "stdin", io.StringIO(raw))
    code = hooks.main([name])
    cap = capsys.readouterr()
    return code, cap.out, cap.err


@pytest.fixture
def project(home) -> Path:
    root = home.projects_root / CODE
    (root / "deliverables").mkdir(parents=True)
    (root / "SCOPE.md").write_text("# Scope of %s\n\n- code: %s\n- kind: lab\n- goal: a small lab\n" % (CODE, CODE),
                                   encoding="utf-8")
    (root / "STATE.md").write_text("Status: building\nNext: the network\n", encoding="utf-8")
    (root / "OPEN.md").write_text("# Open items\n\n- size the backup\n", encoding="utf-8")
    return root


def _secret() -> bytes:
    from awb import gate
    return gate._selftest_cases()["secret"]


def _in_child(code: str, timeout: float = 15.0) -> subprocess.CompletedProcess:
    """Run `code` in a fresh isolated interpreter that imports awb from this tree; fails the test on a hang."""
    prog = "import sys\nsys.path.insert(0, %r)\n%s" % (str(REPO), code)
    try:
        return subprocess.run([sys.executable, "-I", "-c", prog], capture_output=True, text=True, timeout=timeout,
                              stdin=subprocess.DEVNULL)
    except subprocess.TimeoutExpired:
        pytest.fail("the hook code ran longer than %d seconds" % timeout)


# --------------------------------------------------------------------------- regular expressions that hung a hook


def test_paste_blocks_are_found_in_linear_time():
    """A prompt of many opening tags took minutes in _PASTE_RE (a C regex, the alarm cannot stop it), so a prompt
    with a registered name ran into the client's limit and went through unchecked."""
    res = _in_child("from awb import hooks\nt = 'x ' + '<pasted_content>' * 60000\n"
                    "print(hooks._prompt_kind(t), len(hooks._without_pastes(t)) == len(t))\n")
    assert res.returncode == 0 and res.stdout.split() == ["typed", "True"], res.stderr[-300:]


def test_paste_blocks_are_the_blocks_of_the_old_pattern():
    rnd = random.Random(7)
    pieces = ["<pasted_content>", "</pasted_content>", '<pasted_content id="a">', '</pasted_content id="a">',
              '<pasted_content id="b">', '</pasted_content id="b">', "x", "\n", "<", ">", '"', " id="]
    for _ in range(3000):
        text = "".join(rnd.choice(pieces) for _ in range(rnd.randint(0, 12)))
        assert hooks._paste_spans(text) == [m.span() for m in hooks._PASTE_RE.finditer(text)], text
        assert hooks._without_pastes(text) == hooks._PASTE_RE.sub("\n", text), text


def test_draft_paths_are_found_in_linear_time():
    """The draft gate's path pattern took seconds on a run of a few thousand letters in a reply (a key, a hash,
    base64): the stop hook ran past its budget and the client let the reply through."""
    res = _in_child("from awb import hooks\nfrom pathlib import Path\nt = 'a' * 6000 + ' see deliverables/x.md'\n"
                    "print(hooks._named_in(t, 'deliverables/x.md', Path('/nonexistent')))\n")
    assert res.returncode == 0 and res.stdout.strip() == "True", res.stderr[-300:]


def test_draft_paths_are_the_matches_of_the_old_pattern():
    rnd = random.Random(11)
    pieces = ["a", "B", "-", ".", "/", "~", "@", "+", " ", ",", "md", "MD", "txt", "html", "htm", "l", "x", "é",
              ".md", ".eml", "_", "\n"]
    for _ in range(4000):
        text = "".join(rnd.choice(pieces) for _ in range(rnd.randint(0, 14)))
        assert hooks._draft_paths(text) == [m.group(0) for m in hooks._DRAFT_PATH_RE.finditer(text)], text


# --------------------------------------------------------------------------- the budget and crashes fail closed


@pytest.mark.wallclock
def test_a_standard_input_that_never_closes_is_stopped_by_the_budget(home, monkeypatch, capsys):
    """The input was read before the alarm was set: a pipe that stays open held the write guard until the client
    cancelled it, which lets the write through."""
    class Stalled:
        def read(self, *a):
            end = time.monotonic() + 6
            while time.monotonic() < end:
                time.sleep(0.05)
            return "{}"

    monkeypatch.setattr(hooks, "HOOK_BUDGET", 0.4)
    monkeypatch.setattr(sys, "stdin", Stalled())
    t0 = time.monotonic()
    code = hooks.main(["pre-write"])
    assert code == hooks.BLOCK and time.monotonic() - t0 < 3
    assert "nothing passes unchecked" in capsys.readouterr().err


@pytest.mark.wallclock
def test_a_spent_budget_is_not_swallowed_by_a_part_that_steps_back(home, monkeypatch, capsys):
    """The alarm raised an Exception and the parts of the prompt hook that catch every Exception (the rules notice and
    the knowledge block) swallowed it: the hook ran on without a budget."""
    from awb import kbfirst, rulesync

    def slow(*a, **k):
        end = time.monotonic() + 3
        while time.monotonic() < end:
            time.sleep(0.05)
        return None

    monkeypatch.setattr(hooks, "name_hits", lambda text, p=None: [])
    monkeypatch.setattr(rulesync, "notice", slow)
    monkeypatch.setattr(kbfirst, "block", slow)
    monkeypatch.setattr(hooks, "HOOK_BUDGET", 0.3)
    t0 = time.monotonic()
    code, _, err = run_hook("prompt", {"prompt": "size the backup"}, monkeypatch, capsys)
    assert code == hooks.BLOCK and "nothing passes unchecked" in err and time.monotonic() - t0 < 2.5


def test_an_internal_error_of_the_write_guard_blocks(home, monkeypatch, capsys):
    """An unexpected error exited 1, which the client shows without blocking: the write went into the knowledge
    base past a guard that crashed."""
    def broken():
        raise RuntimeError("a value that must not show")

    monkeypatch.setattr(config, "paths", broken)
    code, out, err = run_hook("pre-write", {"tool_input": {"file_path": str(home.kb / "entries" / "KB-X.md")}},
                              monkeypatch, capsys)
    assert code == hooks.BLOCK and "internal error (RuntimeError)" in err
    assert "must not show" not in out + err


def test_an_internal_error_of_the_prompt_hook_blocks(home, monkeypatch, capsys):
    def broken(text):
        raise RuntimeError("boom")

    monkeypatch.setattr(hooks, "_secret_hits", broken)
    code, _, err = run_hook("prompt", {"prompt": "size the backup"}, monkeypatch, capsys)
    assert code == hooks.BLOCK and "internal error" in err


@pytest.mark.parametrize("name", ["pre-write", "post-write"])
@pytest.mark.parametrize("raw", ["not json", "[1, 2]", '"a string"'])
def test_write_hook_input_that_is_no_json_object_blocks(home, monkeypatch, capsys, name, raw):
    code, out, err = run_hook(name, raw, monkeypatch, capsys)
    assert code == hooks.BLOCK and "hook input" in err and out == ""


# --------------------------------------------------------------------------- paths that slipped past the write guards


def test_pre_write_checks_every_path_field_of_the_input(home, monkeypatch, capsys):
    """Only the first path field counted: a notebook path into the knowledge base passed behind a harmless
    file_path."""
    payload = {"tool_name": "NotebookEdit", "tool_input": {"file_path": str(home.shared / "notes.ipynb"),
                                                           "notebook_path": str(home.kb / "entries" / "x.ipynb")}}
    assert run_hook("pre-write", payload, monkeypatch, capsys)[0] == hooks.BLOCK


def test_pre_write_takes_a_variable_as_the_tool_does(home, monkeypatch, capsys):
    """The hook expanded a variable the tool never expands: `$V/..` with V of two parts led the check one folder
    away while the tool's normalised path lay in the knowledge base."""
    monkeypatch.setenv("AWB_REVIEW_TWO", "a/b")
    raw = "%s/$AWB_REVIEW_TWO/../%s/entries/KB-Y.md" % (home.kb.parent, home.kb.name)
    assert run_hook("pre-write", {"tool_input": {"file_path": raw}}, monkeypatch, capsys)[0] == hooks.BLOCK


def test_pre_write_takes_a_dotdot_after_a_link_both_ways(home, tmp_path, monkeypatch, capsys):
    """realpath follows a link before the `..`; a tool that normalises the text first lands one folder away."""
    deep = tmp_path / "deep" / "er"
    deep.mkdir(parents=True)
    link = home.kb.parent / "jump"
    link.symlink_to(deep)
    raw = "%s/../%s/entries/KB-Z.md" % (link, home.kb.name)
    assert run_hook("pre-write", {"tool_input": {"file_path": raw}}, monkeypatch, capsys)[0] == hooks.BLOCK


def test_post_write_checks_the_file_the_tool_wrote_when_a_variable_misleads(project, monkeypatch, capsys):
    """The written file was looked for under the expanded variable, was not there and the hook said nothing."""
    monkeypatch.setenv("AWB_REVIEW_TWO", "a/b")
    f = project / "notes.md"
    f.write_bytes(b"# Notes\n\n" + _secret() + b"\n")
    raw = "%s/$AWB_REVIEW_TWO/../notes.md" % project
    code, out, err = run_hook("post-write", {"tool_name": "Write", "tool_input": {"file_path": raw}},
                              monkeypatch, capsys)
    value = _secret().decode("utf-8").strip().splitlines()[-1][-10:]
    # in a project the secret is shortened in place (task GT) and the session is told so
    assert code == 0 and "carried secrets" in out and value not in f.read_text(encoding="utf-8")
    assert value not in out + err


def test_post_write_refuses_a_path_that_reads_as_two_files(project, monkeypatch, capsys):
    monkeypatch.setenv("AWB_REVIEW_TWO", "a/b")
    (project / "a" / "b").mkdir(parents=True)
    (project / "a" / "notes.md").write_text("one\n", encoding="utf-8")
    (project / "notes.md").write_text("two\n", encoding="utf-8")
    raw = "%s/$AWB_REVIEW_TWO/../notes.md" % project
    code, _, err = run_hook("post-write", {"tool_name": "Write", "tool_input": {"file_path": raw}},
                            monkeypatch, capsys)
    assert code == hooks.BLOCK and "2 different files" in err


def test_post_write_finds_a_secret_in_a_file_with_a_stray_byte(project, monkeypatch, capsys):
    """One byte that is no UTF-8 made the file no text for the hook: its secret passed unseen."""
    f = project / "notes.md"
    f.write_bytes(b"# Notes \xff\n\n" + _secret() + b"\n")
    code, out, err = run_hook("post-write", {"tool_name": "Write", "tool_input": {"file_path": str(f)}},
                              monkeypatch, capsys)
    assert code == hooks.BLOCK and "carries a secret" in err
    assert _secret().decode("utf-8").strip().splitlines()[-1][-10:] not in out + err
    fixtures.assert_no_fixture_name(out + err, "post-write output")


# --------------------------------------------------------------------------- the stop hook and the start


def test_the_draft_gate_reads_a_transcript_with_entries_of_another_shape(tmp_path):
    """A message that is no object or a tool result whose text is no string, took read_turn down."""
    t = tmp_path / "t.jsonl"
    rows = [{"type": "user", "message": {"content": "write the note"}},
            {"type": "assistant", "message": "a message of another shape"},
            {"type": "assistant", "message": {"content": [{"type": "tool_use", "id": "u1", "name": "Bash",
                                                           "input": {"command": "ls"}}]}},
            {"type": "user", "message": {"content": [{"type": "tool_result", "tool_use_id": "u1",
                                                      "content": [{"type": "text", "text": 5}]}]}},
            {"type": "assistant", "message": {"content": [{"type": "text", "text": "done"}]}}]
    t.write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")
    turn = hooks.read_turn(t)
    assert turn.reply == ["done"] and turn.results == {"u1": (False, "")}


@pytest.mark.wallclock
def test_session_start_skips_a_named_pipe_instead_of_waiting(project, monkeypatch, capsys):
    """OPEN.md as a named pipe held the start until its budget was spent and the context was lost."""
    for name in ("kb", "career"):
        monkeypatch.setitem(sys.modules, "awb." + name, None)
    (project / "OPEN.md").unlink()
    os.mkfifo(project / "OPEN.md")
    monkeypatch.setattr(hooks, "HOOK_BUDGET", 1.5)
    t0 = time.monotonic()
    code, out, _ = run_hook("session-start", {"cwd": str(project)}, monkeypatch, capsys)
    text = json.loads(out)["hookSpecificOutput"]["additionalContext"]
    assert code == 0 and time.monotonic() - t0 < 1.2
    assert "a small lab" in text and "## OPEN.md\n\n(missing or unreadable)" in text


def test_session_start_does_not_load_a_file_linked_from_outside_the_project(project, tmp_path, monkeypatch,
                                                                             capsys):
    """OPEN.md as a link to a file of another folder loaded that file into the context past the tool guard."""
    for name in ("kb", "career"):
        monkeypatch.setitem(sys.modules, "awb." + name, None)
    outside = tmp_path / "elsewhere.md"
    outside.write_text("the plan of another folder\n", encoding="utf-8")
    (project / "OPEN.md").unlink()
    (project / "OPEN.md").symlink_to(outside)
    code, out, _ = run_hook("session-start", {"cwd": str(project)}, monkeypatch, capsys)
    text = json.loads(out)["hookSpecificOutput"]["additionalContext"]
    assert code == 0 and "another folder" not in text and "## OPEN.md\n\n(missing or unreadable)" in text
