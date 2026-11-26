"""B-102: check 3 of the build runner reads a DONE line at the start or at the end of the report."""
from __future__ import annotations

import pytest

from awb import build, build_checks, buildrun

REPORT = "DONE: commit abc adds X\n\n- Tests: test_x passes\n\nThe code is in awb/x.py."


@pytest.fixture
def ctx(tmp_path):
    task = buildrun.check_task("C-001")
    run = build.new_run("B-401", tmp_path, state="running")
    return buildrun.Ctx(task, run)


def _verdict(ctx, text):
    try:
        build_checks.check_answer(ctx, {"result": text})
    except build_checks.Stop as stop:
        return stop.state
    return "ok"


def test_a_done_line_at_the_start_passes(ctx):
    assert _verdict(ctx, REPORT) == "ok"


def test_a_done_line_at_the_end_passes(ctx):
    assert _verdict(ctx, "Changed the value.\nDONE: the value is two") == "ok"


def test_a_blocked_line_anywhere_is_asked(ctx):
    text = REPORT + "\nBLOCKED: a test outside the paths"
    assert _verdict(ctx, text) == "asked"
    assert _verdict(ctx, "DONE: x\nBLOCKED: y\nmore text") == "asked"


def test_a_report_that_ends_with_a_question_is_asked(ctx):
    assert _verdict(ctx, REPORT + "\n\nShall I change the other file too?") == "asked"


def test_a_report_with_no_done_is_asked(ctx):
    assert _verdict(ctx, "Changed the value.\n\nThe code is in awb/x.py.") == "asked"
    assert _verdict(ctx, "") == "asked"
