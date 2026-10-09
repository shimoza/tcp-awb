"""awb/tcp/refresh.py, the part wipe: the red-team pack of wipe mode measured on every refresh.

A few cases of the pack run through the real intake in a temporary shared folder: the counts land in the report and
in wipe.tsv, a grown count marks the run overdue, and a harness that cannot fail records nothing. Nothing here prints
a planted value.
"""
from __future__ import annotations

import datetime
import functools
from pathlib import Path

from awb.tcp import board, refresh

TODAY = datetime.date.today()
ROOT = Path(__file__).resolve().parent.parent
# one leaking and one clean case of EXPECTED.md (wipe-german), one clean case of the false-positive dimension
ONLY = {"german": ["de-lone-after-preposition", "de-stadt-x"], "losses": []}


def only():
    sel = {k: list(v) for k, v in ONLY.items()}
    h = refresh._wipe_harness(ROOT)
    sel["losses"] = [h.load_cases(h.module_path("losses"))[0]["id"]]
    return sel


def write_history(home, leaks, losses, cases=3):
    tsv = board.wipe_tsv(home)
    tsv.parent.mkdir(parents=True, exist_ok=True)
    tsv.write_text("\t".join(board.WIPE_COLUMNS) + "\n" + "\t".join(str(x) for x in (
        "2026-10-01", cases, leaks, losses, leaks, losses, 0, 0, "abc1234", "german=%d/%d" % (leaks, losses))) + "\n",
        encoding="utf-8")


def test_the_counts_land_in_the_report_and_in_wipe_tsv(home, monkeypatch):
    monkeypatch.setattr(refresh, "part_wipe", functools.partial(refresh.part_wipe, only=only()))
    parts, report = refresh.run(home, parts=("wipe",), update=False, today=TODAY)
    (part,) = parts
    assert not part.error and part.overdue == 0
    text = report.read_text(encoding="utf-8")
    assert report.name == "REPORT-wipe.md"
    assert "## wipe: nothing overdue" in text
    assert "3 cases: 1 with a leak (2 values), 0 with a loss (0 terms), 0 withheld, 0 errors" in text
    assert "  german         2 cases, 1 leaks, 0 losses" in text
    assert "  losses         1 cases, 0 leaks, 0 losses" in text
    assert "the first line of wipe.tsv: nothing to compare" in text
    lines = board.wipe_tsv(home).read_text(encoding="utf-8").splitlines()
    assert lines[0].split("\t") == list(board.WIPE_COLUMNS)
    (row,) = board.wipe_history(home)
    assert (row["date"], row["cases"], row["leaks"], row["losses"]) == (TODAY.isoformat(), 3, 1, 0)
    assert row["release"] == refresh._release() and row["by_module"] == "german=1/0,losses=0/0"
    assert board.wipe_line(home) == "wipe: 1 leaks, 0 losses of 3 (%s)" % TODAY.isoformat()


def test_a_grown_count_marks_the_run_overdue_and_an_equal_one_does_not(home):
    write_history(home, leaks=0, losses=0)
    part = refresh.part_wipe(home, today=TODAY, only=only())
    assert part.overdue == 1
    assert "OVERDUE: leaks grew from 0 to 1 since 2026-10-01" in part.lines
    part = refresh.part_wipe(home, today=TODAY, only=only())      # against the line the run before appended
    assert part.overdue == 0
    assert "against %s: 1 leaks, 0 losses, not more" % TODAY.isoformat() in part.lines
    assert len(board.wipe_history(home)) == 3


def test_a_harness_whose_planted_leak_comes_out_clean_records_nothing(home, monkeypatch):
    write_history(home, leaks=7, losses=7)
    h = refresh._wipe_harness(ROOT)
    monkeypatch.setattr(h.rt, "readable", lambda value, text: None)   # a harness blind to every leak
    part = refresh.part_wipe(home, today=TODAY, only=only())
    assert part.error.startswith("not recorded, the harness did not show that it can fail")
    assert "the sentinel leak was not reported" in part.error
    assert len(board.wipe_history(home)) == 1
    assert board.wipe_line(home) == "wipe: 7 leaks, 7 losses of 3 (2026-10-01)"


def test_a_wipe_error_does_not_stop_the_dataset(home):
    part = refresh.part_dataset(home, today=TODAY, after=[refresh.Part("wipe", error="not recorded")])
    assert part.lines == ["not built: no fact fit to leave"]
    part = refresh.part_dataset(home, today=TODAY, after=[refresh.Part("knowledge", error="KBError: x")])
    assert part.lines == ["not built: knowledge ended in an error"]


def test_the_board_carries_the_wipe_line(home):
    assert board.wipe_line(home) == "wipe: not measured yet"
    write_history(home, leaks=7, losses=7, cases=405)
    line = board.wipe_line(home)
    assert line == "wipe: 7 leaks, 7 losses of 405 (2026-10-01)"
    data = {"generated_at": "2026-10-09T10:00:00+00:00", "projects": [], "wipe": line,
            "summary": {"projects": 0, "current": 0, "lagging": 0, "without_status": 0}}
    assert line in board.markdown(data)
    assert line in board.page(data)
