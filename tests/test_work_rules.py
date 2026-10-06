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
    "Reply in English",                     # he dictates in Russian, the answer is English
    "T Cloud Public (TCP)",                 # the platform name his customers see
    "never repeat it",                      # a name the register does not know is never echoed
    "The prompt hook stops a prompt",        # a registered name never reaches the model
    "live price API",                       # prices are fetched, never recalled
    "`awb price check`",                    # a price sheet is checked to the cent before it leaves
    "shown that it can fail",               # a clean result counts only after the check failed once
    "say where it lands",                   # repository, remote, folder or account before acting
    "Never for Russian or German text",     # the English note is for his own English only
    "Only `awb kb` writes there",           # facts enter the knowledge base through its checks
    "never what it says",                   # a leak is flagged by kind and place, never repeated
    "`awb service check NAME`",              # offered is what the latest service description lists, nothing else
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
