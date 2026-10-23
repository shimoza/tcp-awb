"""The work rules (seal/work-claude/CLAUDE.md): what every working session reads first and what every spawned
project imports. The seal installs the file root-owned and immutable, so a rule missing here stays missing."""
from __future__ import annotations

from pathlib import Path

import pytest

from awb import gate, writing

ROOT = Path(__file__).resolve().parent.parent
RULES = ROOT / "seal" / "work-claude" / "CLAUDE.md"
BUDGET = 60
"""Lines the rules may take: they are loaded into every working session (T-64)."""


def text() -> str:
    return RULES.read_text(encoding="utf-8")


def test_the_work_rules_stay_within_their_line_budget():
    assert 0 < len(text().splitlines()) <= BUDGET


@pytest.mark.parametrize("must", [
    "T Cloud Public (TCP)",                 # the platform name his customers see
    "never repeat it",                      # a name the register does not know is never echoed
    "The prompt hook stops a prompt",        # a registered name never reaches the model
    "live price API",                       # prices are fetched, never recalled
    "`awb price check`",                    # a price sheet is checked to the cent before it leaves
    "shown that it can fail",               # a clean result counts only after the check failed once
    "say where it lands",                   # repository, remote, folder or account before acting
    "Only `awb kb` writes there",           # facts enter the knowledge base through its checks
    "never what it says",                   # a leak is flagged by kind and place, never repeated
    "`awb service check NAME`",              # offered is what the latest service description lists, nothing else
    "at most one question per reply",       # T13: one question, at the end of the reply
    "unless you say otherwise",             # T13: with the default the session takes
    "goes into OPEN.md with its default",   # T13: a question that does not stop the work is not asked in the chat
    "a \"go\" covers the whole list",        # T13: never re-ask after a go
    "the user and the window named",        # T13: a command for him names the user and the window
    "At most one command block per reply",  # T5: one block for him at most
    "one line naming the user and the command",  # T5: a held take or a blocked intake is one line, no explanation
    "goes through `awb paste`, not into the chat",  # T8: terminal output lands in a file, masked
])
def test_the_work_rules_carry_each_rule(must):
    assert must in text()


def test_the_work_rules_are_not_the_developer_instructions():
    t = text()
    assert "Build state lives in build/" not in t
    assert "first failing one" not in t


def test_the_work_rules_pass_the_writing_check():
    tells, _ = writing.check_file(RULES, mode="doc", scope="tcp")
    assert [(t.cls, t.line) for t in tells if t.blocking] == []


def test_the_work_rules_pass_the_gate():
    assert gate.scan_files([RULES], None) == []


def test_the_work_rules_keep_the_status_current_after_every_step():
    """2026-10-06: STATE.md lagged five days behind the work and the portal showed the old status."""
    t = text()
    assert "After every step that" in t and "Status: and Next: lines" in t and "in the same commit" in t


def test_the_work_rules_name_the_import_and_the_promise_of_wipe_mode():
    """T4: material comes in through his awb import; a hold is one line; the promise is "every name the rules
    recognise", never "every name"."""
    t = " ".join(text().split())
    assert "his `awb import CODE`" in t and "through `awb intake`" not in t
    assert "`held: run awb import tcp-xxxx as the owner in your own terminal`" in t
    assert "every name the rules recognise as a token" in t
    assert "never an `awb intake`, register or bucket block" in t


def test_the_work_rules_check_every_draft_first_and_translate_in_one_step():
    """The work profile of 2026-10, proposal 4: every draft for others passes the voice checks before he sees it,
    and the Russian reading copy and the German version come from the accepted text in one step."""
    t = " ".join(text().split())
    assert "He sees a draft only after `awb write check` of its file ran clean in the same turn" in t
    assert "a mail draft goes into a file" in t and "I and never `we`" in t
    assert "`awb write translate FILE --to de --to ru`" in t and "never by rewriting" in t


def test_chain_1_a_german_customer_gets_german_and_he_reads_russian():
    """LG2 chain 1: the customer writes German: the session talks Russian, the English text is accepted first, then
    one translate step makes the German deliverable and the Russian reading copy and the session shows the copy."""
    t = " ".join(text().split())
    assert "Chain 1, the customer writes German" in t
    assert "you talk to him in Russian" in t
    assert "Draft and check the English text, he accepts it, then both versions come from it in one step" in t
    assert "`awb write translate FILE --to de --to ru`" in t and "Show him the Russian copy" in t


def test_chain_2_he_asks_in_russian_or_english_and_gets_english_only():
    """LG2 chain 2: the talk and the deliverables are English, German or Russian only when he asks; a switch to
    Russian ("по-русски") changes the talk only, never the deliverable."""
    t = " ".join(text().split())
    assert "Chain 2, he asks in Russian or English: you talk to him in English" in t
    assert "the deliverables are English only (German or Russian only when he asks)" in t
    assert '"по-русски" switches the talk to Russian until he switches back; the deliverable stays English' in t


def test_the_work_rules_carry_no_english_note():
    """LG2: the session never comments on his English, his dictation slips or his punctuation."""
    t = " ".join(text().split())
    assert "Never comment on his English, dictation slips or punctuation" in t
    assert "English note" not in t and "Reply in English" not in t


def test_the_drafting_skill_names_both_chains_in_both_copies():
    for rel in ("seal/work-claude/skills/drafting/SKILL.md", "plugin/skills/drafting/SKILL.md"):
        s = " ".join((ROOT / rel).read_text(encoding="utf-8").split())
        assert "Chain 1, the customer writes German" in s and "Chain 2, he asks in Russian or English" in s, rel
        assert "`awb write translate FILE --to de --to ru`" in s and "You never correct his English" in s, rel
        assert "English note" not in s, rel
