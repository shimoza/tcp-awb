"""The red team of the client hooks of 2026-09-27 (calibration/redteam-2026-09-27.md, third run): what the
prompt, pre-write, post-write and session-start hooks miss was found by Fable finders in a harness; every
confirmed escape is pinned here. Every name comes from tests/fixtures.py."""
from __future__ import annotations

import base64
import json
import os
import sys
import time
from pathlib import Path

import pytest

from awb import check, hooks, normalize, patterns
from tests import fixtures
from tests.conftest import needs_hook_process
from tests.test_hooks import (CODE, assert_clean, context_of, fake_module, missing_module, no_writing,  # noqa: F401
                              project, run_hook, write_payload)

FULL = fixtures.CUSTOMER_FORMS[0]
SHORT = fixtures.CUSTOMER_FORMS[1]
PERSON = fixtures.PERSON_FORMS[0]


def prompt(text: str, monkeypatch, capsys) -> tuple[int, str, str]:
    return run_hook("prompt", {"prompt": text}, monkeypatch, capsys)


# --------------------------------------------------------------------------- the check reads hidden views


def test_a_name_behind_a_base64_or_hex_block_blocks_the_prompt(home, monkeypatch, capsys):
    b64 = base64.b64encode(("the offer for %s is late" % FULL).encode("utf-8")).decode("ascii")
    for text in ("please decode %s" % b64, "bytes: " + ("the plan of %s" % FULL).encode("utf-8").hex(),
                 "data:text/plain;base64," + b64, json.dumps({"note": b64})):
        code, out, err = prompt(text, monkeypatch, capsys)
        assert code == 2 and "registered name" in err
        assert_clean(out + err, "prompt output")
    iban = base64.b64encode(b"pay to DE89 3704 0044 0532 0130 00 today").decode("ascii")
    code, out, err = prompt("and %s" % iban, monkeypatch, capsys)
    assert code == 2 and "iban" in err


def test_check_text_sees_what_a_bidi_override_shows_reversed(register_path):
    reversed_form = "‮" + FULL[::-1] + "‬"
    hits = check.check_text("call " + reversed_form + " on monday", register_path)
    assert [h["cls"] for h in hits] == ["name"]
    assert check.check_text("call " + FULL[::-1] + " on monday", register_path) == []
    assert check.check_text("‭" + "plain text under an override" + "‬", register_path) == []


def test_a_written_file_or_a_section_with_a_bidi_override_is_refused(project, monkeypatch, capsys, no_writing):
    f = project / "notes.md"
    f.write_text("# Notes\n\nsee ‮%s‬ today\n" % FULL[::-1], encoding="utf-8")
    code, out, err = run_hook("post-write", write_payload(f), monkeypatch, capsys)
    assert code == 2 and "bidi override" in err and "hits of the name check: name" in err
    assert_clean(out + err, "post-write output")
    (project / "OPEN.md").write_text("# Open\n\n- ‮%s‬\n" % FULL[::-1], encoding="utf-8")
    missing_module(monkeypatch, "kb")
    missing_module(monkeypatch, "career")
    code, out, _ = run_hook("session-start", {"cwd": str(project)}, monkeypatch, capsys)
    text = context_of(out, "SessionStart")["hookSpecificOutput"]["additionalContext"]
    assert "withheld: it carries a bidi override" in text and "‮" not in text
    assert_clean(text, "session-start context")


def test_a_file_name_reversed_under_an_override_gives_a_path_record(tmp_path, register_path):
    f = tmp_path / ("‮" + SHORT[::-1] + ".md")
    f.write_text("clean\n", encoding="utf-8")
    assert [h["cls"] for h in check.check_file(f, register_path)] == ["path"]


def test_a_value_inside_markup_is_still_structured_data(register_path):
    for text in ('the box at <!-- 203.0.113.40 --> answers', '<a title="203.0.113.40">x</a>',
                 '<a href="mailto:erika@example.org">mail</a>', '<![CDATA[+49 30 1234567]]>',
                 '<span data-vat="DE123456789">v</span>'):
        assert check.check_text(text, register_path), text
    assert "ip" in {s.cls.lower() for s in patterns.find_structured("<!-- 203.0.113.40 -->")}


def test_a_phone_number_over_two_cells_with_bold_or_code_is_found():
    for text in ("| **+49 30** | **123 4567** |", "| `+49 30` | `123 4567` |", "| +49 30 | **123 4567** |"):
        assert [s.cls.lower() for s in patterns.find_structured(text)] == ["phone"], text


def test_a_long_digit_run_costs_no_time():
    for text in ("0" + "1" * 300 + " " + FULL, " ".join("1234567890" * 8) + "\n" + FULL, "0x" + "ab" * 200):
        t0 = time.monotonic()
        patterns.find_structured(text)
        assert time.monotonic() - t0 < 1.0


def test_a_tag_with_a_long_attribute_or_a_quoted_bracket_is_markup(register_path):
    a, b = SHORT[:3], SHORT[3:]
    assert check.check_text('%s<span style="%s">%s' % (a, "x" * 5000, b), register_path)
    assert check.check_text('%s<a title="1 > 0">%s' % (a, b), register_path)
    assert check.check_text('%s<a title="1 > 0" href="x">%s' % (a, b), register_path)
    from awb import writing
    assert "leverage" in writing.rendered('lev<b title="a>b">er</b>age')


def test_a_double_json_escape_is_decoded():
    assert normalize.normalize("\\u005cu005a").text == "Z"


# --------------------------------------------------------------------------- encoded blocks of a written file


def test_a_glued_prefix_or_a_tool_layout_does_not_hide_a_block(register_path, tmp_path):
    from awb.extract.text import encoded_texts
    line = "the offer for %s is late" % FULL
    b64 = base64.b64encode(line.encode("utf-8")).decode("ascii")
    assert any(FULL in t for t in encoded_texts("sha256-" + b64))
    assert any(FULL in t for t in encoded_texts("abc" + b64))
    raw = line.encode("utf-8").hex()
    assert any(FULL in t for t in encoded_texts(" ".join(raw[i:i + 4] for i in range(0, len(raw), 4))))
    assert any(FULL in t for t in encoded_texts(", ".join("0x" + raw[i:i + 2] for i in range(0, len(raw), 2))))
    assert any(FULL in t for t in encoded_texts("".join("0x" + raw[i:i + 2] for i in range(0, len(raw), 2))))


def test_a_file_with_more_blocks_than_the_limit_is_opaque(tmp_path, register_path, monkeypatch):
    from awb.extract import text as textmod
    monkeypatch.setattr(textmod, "MAX_BASE64_BLOCKS", 3)
    innocent = base64.b64encode(b"nothing to see here at all").decode("ascii")
    hidden = base64.b64encode(("call %s" % FULL).encode("utf-8")).decode("ascii")
    f = tmp_path / "blocks.txt"
    f.write_text("\n".join(["x %s" % innocent] * 3 + ["y %s" % hidden]) + "\n", encoding="utf-8")
    hits = check.check_file(f, register_path)
    assert "opaque" in {h["cls"] for h in hits}


# --------------------------------------------------------------------------- pre-write


def test_pre_write_follows_a_hard_link_expands_the_home_and_fails_closed(home, monkeypatch, capsys):
    kb = home.kb
    kb.mkdir(parents=True, exist_ok=True)
    entry = kb / "fact.md"
    entry.write_text("a fact\n", encoding="utf-8")
    link = home.projects_root / "notes.md"
    home.projects_root.mkdir(parents=True, exist_ok=True)
    os.link(entry, link)
    payload = {"hook_event_name": "PreToolUse", "tool_name": "Write", "tool_input": {"file_path": str(link)}}
    code, _, err = run_hook("pre-write", payload, monkeypatch, capsys)
    assert code == 2 and "knowledge base" in err
    monkeypatch.setenv("HOME", str(kb.parent))
    tilde = {"hook_event_name": "PreToolUse", "tool_name": "Write",
             "tool_input": {"file_path": "~/%s/other.md" % kb.name}}
    assert run_hook("pre-write", tilde, monkeypatch, capsys)[0] == 2
    for bad in ({"tool_input": ["x"]}, {"tool_input": {"file_path": "a\x00b"}},
                {"tool_input": {"file_path": ["a"]}}):
        code, _, err = run_hook("pre-write", bad, monkeypatch, capsys)
        assert code == 2 and "nothing passes unchecked" in err
    other = home.projects_root / "plain.md"
    other.write_text("x\n", encoding="utf-8")
    assert run_hook("pre-write", {"tool_input": {"file_path": str(other)}}, monkeypatch, capsys)[0] == 0
    assert run_hook("pre-write", {"tool_input": {}}, monkeypatch, capsys)[0] == 0


# --------------------------------------------------------------------------- post-write


def test_post_write_refuses_a_secret_and_the_prompt_hook_too(project, home, monkeypatch, capsys, no_writing):
    # the invented secrets are built from pieces, so that this file carries no secret shape for the gate
    key = "".join(["wJalrXUtn", "FEMI/K7MD", "ENG/bPxRf", "iCYq9Lm2Zt"])
    word = "".join(["K7mQ2v", "X9pL4n", "R8sT"])
    f = project / "env-notes.md"
    f.write_text("# Env\n\nexport AWS_SECRET_ACCESS_KEY=%s\n" % key, encoding="utf-8")
    code, out, err = run_hook("post-write", write_payload(f), monkeypatch, capsys)
    assert code == 2 and "carries a secret" in err and key[:8] not in err
    code, out, err = prompt("use password=%s for the box" % word, monkeypatch, capsys)
    assert code == 2 and "carries a secret" in err and word[:6] not in err
    code, out, err = prompt("the password is in the vault under box-one", monkeypatch, capsys)
    assert code == 0


def test_post_write_refuses_a_file_over_the_size_limit_without_reading_it(project, monkeypatch, capsys,
                                                                            no_writing):
    monkeypatch.setattr(hooks, "MAX_WRITE_BYTES", 100)
    monkeypatch.setattr(hooks, "MAX_DELIVERABLE_BYTES", 50)
    f = project / "big.md"
    f.write_text("x" * 101 + "\n", encoding="utf-8")
    code, _, err = run_hook("post-write", write_payload(f), monkeypatch, capsys)
    assert code == 2 and "not checked" in err
    d = project / "deliverables" / "big.md"
    d.write_text("y" * 60 + "\n", encoding="utf-8")
    assert run_hook("post-write", write_payload(d), monkeypatch, capsys)[0] == 2
    small = project / "small.md"
    small.write_text("z" * 40 + "\n", encoding="utf-8")
    assert run_hook("post-write", write_payload(small), monkeypatch, capsys)[0] == 0


def test_post_write_runs_the_writing_check_under_a_nested_scope_and_a_capital_folder(project, monkeypatch, capsys):
    calls = []

    def check_file(path, mode="doc", scope="tcp"):
        calls.append(Path(path).name)
        return [], {}

    fake_module(monkeypatch, "writing", check_file=check_file)
    pack = project / "deliverables" / "pack"
    pack.mkdir()
    (pack / "SCOPE.md").write_text("# a scope written by the session\n", encoding="utf-8")
    f = pack / "plan.md"
    f.write_text("a plan\n", encoding="utf-8")
    assert run_hook("post-write", write_payload(f), monkeypatch, capsys)[0] == 0
    cap = project / "Deliverables"
    cap.mkdir()
    g = cap / "plan.md"
    g.write_text("a plan\n", encoding="utf-8")
    assert run_hook("post-write", write_payload(g), monkeypatch, capsys)[0] == 0
    assert calls == ["plan.md", "plan.md"]


def test_post_write_refuses_a_folder_name_that_carries_a_name(project, monkeypatch, capsys, no_writing):
    folder = project / "notes" / SHORT
    folder.mkdir(parents=True)
    f = folder / "clean.md"
    f.write_text("clean\n", encoding="utf-8")
    code, out, err = run_hook("post-write", write_payload(f), monkeypatch, capsys)
    assert code == 2 and "folder on the path" in err
    assert_clean(out + err, "post-write output")


# --------------------------------------------------------------------------- session-start


def test_session_start_never_shows_a_project_folder_named_with_a_name(home, monkeypatch, capsys):
    root = home.projects_root / SHORT
    root.mkdir(parents=True)
    (root / "SCOPE.md").write_text("# Scope\n\n- goal: a lab\n", encoding="utf-8")
    missing_module(monkeypatch, "kb")
    missing_module(monkeypatch, "career")
    code, out, err = run_hook("session-start", {"cwd": str(root)}, monkeypatch, capsys)
    assert code == 0
    text = context_of(out, "SessionStart")["hookSpecificOutput"]["additionalContext"]
    assert "Workbench project this project." in text
    assert_clean(text + err, "session-start context")


def test_session_start_checks_past_the_cut_and_cuts_at_a_line_end(project, monkeypatch, capsys):
    missing_module(monkeypatch, "kb")
    missing_module(monkeypatch, "career")
    filler = "\n".join("line %d of the open items, nothing in it" % n for n in range(400))
    head = filler[:hooks.SECTION_CHARS - 4]
    (project / "OPEN.md").write_text(head + PERSON + "\n" + filler, encoding="utf-8")
    code, out, _ = run_hook("session-start", {"cwd": str(project)}, monkeypatch, capsys)
    text = context_of(out, "SessionStart")["hookSpecificOutput"]["additionalContext"]
    assert "OPEN.md" in text and "withheld" in text
    assert_clean(text, "session-start context")
    (project / "OPEN.md").write_text(filler, encoding="utf-8")
    code, out, _ = run_hook("session-start", {"cwd": str(project)}, monkeypatch, capsys)
    text = context_of(out, "SessionStart")["hookSpecificOutput"]["additionalContext"]
    loaded = text.split("## OPEN.md")[1].split("## Checks")[0]
    assert loaded.rstrip().endswith("nothing in it"), "the cut falls at a line end"


# --------------------------------------------------------------------------- the budget


def test_a_hook_over_its_budget_fails_closed(home, monkeypatch, capsys):
    monkeypatch.setattr(hooks, "HOOK_BUDGET", 0.2)

    def slow(text, p=None):
        time.sleep(2)
        return []

    monkeypatch.setattr(hooks, "name_hits", slow)
    t0 = time.monotonic()
    code, _, err = prompt("a slow prompt", monkeypatch, capsys)
    assert code == 2 and "nothing passes unchecked" in err and time.monotonic() - t0 < 1.5
    monkeypatch.setattr(hooks, "HOOK_BUDGET", 50.0)
    assert prompt("a quick prompt", monkeypatch, capsys)[0] == 0


@needs_hook_process
def test_the_command_line_hook_stops_itself_before_the_client_would(project):
    env = dict(os.environ, AWB_HOOK_BUDGET_TEST="1")
    code = ("import sys, time\nfrom awb import hooks\nhooks.HOOK_BUDGET = 0.3\n"
            "hooks.name_hits = lambda text, p=None: time.sleep(3) or []\n"
            "sys.exit(hooks.main(['prompt']))\n")
    import subprocess
    res = subprocess.run([sys.executable, "-c", code], input=json.dumps({"prompt": "slow"}), capture_output=True,
                         text=True, timeout=60, env=env)
    assert res.returncode == 2 and "nothing passes unchecked" in res.stderr
