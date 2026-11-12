"""UX3 of 2026-11-11: a command in the owner status stands only in backticks after run, type or his terminal."""
from __future__ import annotations

from awb import projects, rulesync
from tests import fixtures as fx

CODE = "tcp-q7m4"


def test_prose_after_a_bare_command_name_gives_no_command():
    item = "Check awb import has run before the review (owner)"
    assert projects.item_command(item) is None


def test_a_backticked_command_that_is_only_mentioned_gives_no_command():
    assert projects.item_command("The `awb gate` refuses names, so the text waits (owner)") is None


def test_run_with_a_backticked_command_gives_it():
    assert projects.item_command("Import the file, run: `awb import %s` (owner)" % CODE) == "awb import %s" % CODE
    assert projects.item_command("his terminal: `awb unlock`") == "awb unlock"


def test_a_bare_command_after_run_is_not_enough():
    assert projects.item_command("run: awb import %s" % CODE) is None


def test_the_template_and_sync_carry_the_rule():
    assert projects.OPEN_COMMAND_RULE in projects._claude(CODE, "project", fx.CUSTOMER_CODE)
    assert projects.OPEN_COMMAND_RULE in rulesync.CLAUDE_APPEND[-1]
