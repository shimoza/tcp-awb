"""The owner's rule of 2026-11-20: the Windows lab machines (awb winlab) are for a Windows migration test only. A
project that is no Windows migration is never offered them: its CLAUDE.md carries no word of winlab and the work
rules every session reads say so in one sentence. A Windows migration project still gets the offer."""
from __future__ import annotations

from pathlib import Path

import pytest

from awb import cli, projects, winlab
from tests.fixtures import CUSTOMER_CODE

ROOT = Path(__file__).resolve().parent.parent
RULES = ROOT / "seal" / "work-claude" / "CLAUDE.md"
CODE = "tcp-q7m4"


@pytest.mark.parametrize("tags,goal", [
    ([], "Price two Linux web servers for a PoC"),
    (["migration"], "Move the file servers from the old data centre"),
    (["windows"], "price windows licences for twelve servers"),
    (["windows", "pricing"], "Check the BYOL rules"),
])
def test_a_project_that_is_no_windows_migration_is_not_one(tags, goal):
    assert not winlab.windows_migration(tags, goal)


@pytest.mark.parametrize("tags,goal", [
    (["windows", "migration"], "Plan the move"),
    ([], "migrate windows server 2012 r2 machines to tcp"),
    (["windows"], "Move the domain controllers"),
    (["migration"], "Rehost the windows estate"),
])
def test_a_windows_migration_is_one(tags, goal):
    assert winlab.windows_migration(tags, goal)


@pytest.mark.parametrize("kind", ["query", "project"])
def test_the_claude_md_of_a_project_that_is_no_windows_migration_names_no_winlab(kind):
    offer = projects._winlab_offer(["migration"], "Move two Linux web servers")
    text = projects._claude(CODE, kind, CUSTOMER_CODE, offer)
    assert offer is False
    assert "winlab" not in text.lower() and "lab machine" not in text.lower()
    assert text.count("\n") <= projects.MAX_CLAUDE_LINES


@pytest.mark.parametrize("kind", ["query", "project"])
def test_the_claude_md_of_a_windows_migration_offers_the_lab_machines(kind):
    offer = projects._winlab_offer(["windows", "migration"], "migrate windows server machines to tcp")
    text = projects._claude(CODE, kind, CUSTOMER_CODE, offer)
    plain = projects._claude(CODE, kind, CUSTOMER_CODE)
    assert offer is True
    assert winlab.OFFER_LINE in text and "awb winlab" in text
    assert text.count("\n") == plain.count("\n") <= projects.MAX_CLAUDE_LINES


@pytest.mark.skipif(not RULES.is_file(), reason="the work rules are not in this copy of the tree")
def test_the_work_rules_carry_the_one_neutral_sentence_and_no_other_winlab_word():
    text = RULES.read_text(encoding="utf-8")
    assert winlab.NOT_OFFERED in text
    assert text.lower().count("winlab") == 1


def test_the_awb_help_names_winlab_for_windows_migration_tests_only():
    assert "for Windows migration tests only" in cli.DELEGATED["winlab"][1]
