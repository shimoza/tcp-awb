"""B-106: the ended block of `awb owner status` keeps one line per (kind, id), the latest end, and shows at most
ENDED_OK_MAX ok lines; `ended_rows` is not collapsed. Folders are tmp_path; ids are invented."""
from __future__ import annotations

import datetime as dt
import types

import pytest

from awb import events

T0 = dt.datetime(2026, 11, 20, 9, 0, tzinfo=dt.timezone.utc)


def at(minutes: int) -> dt.datetime:
    return T0 + dt.timedelta(minutes=minutes)


@pytest.fixture
def board(tmp_path):
    b = tmp_path / "shared" / "build"
    b.mkdir(parents=True)
    return b


def ended_lines(board, minute):
    lines = events.status_lines(board, at(minute))
    return lines[lines.index("Ended in the last 24 h") + 1:]


def test_an_item_that_ends_three_times_shows_its_latest_end_only(board):
    events.start("task", "B-001", board=board, now=at(0))
    events.end("task", "B-001", "needs-you", board=board, now=at(1))
    events.end("task", "B-001", "red", board=board, now=at(2))
    events.end("task", "B-001", "ok", board=board, now=at(3))
    hm = at(3).astimezone().strftime("%H:%M")
    assert ended_lines(board, 10) == ["  ok build task B-001 at %s" % hm]


def test_twelve_ok_deploys_give_five_lines_and_a_count(board):
    for n in range(12):
        events.start("deploy", "abc%04d" % n, board=board, now=at(n))
        events.end("deploy", "abc%04d" % n, "ok", board=board, now=at(n) + dt.timedelta(seconds=30))
    lines = ended_lines(board, 20)
    assert len(lines) == 6
    assert [x.split()[2] for x in lines[:5]] == ["abc%04d" % n for n in range(11, 6, -1)]
    assert lines[5] == "  and 7 more ended ok"


def test_every_line_that_is_not_ok_is_shown_and_a_red_last_end_is_in_capitals(board):
    for n in range(8):
        events.start("deploy", "abc%04d" % n, board=board, now=at(n))
        events.end("deploy", "abc%04d" % n, "ok", board=board, now=at(n + 1))
    events.start("check", "C-301", board=board, now=at(20))
    events.end("check", "C-301", "red", board=board, now=at(21))
    events.end("task", "B-002", "stopped", board=board, now=at(22))
    lines = ended_lines(board, 30)
    assert [x.split()[0] for x in lines[:2]] == ["STOPPED", "RED"]
    assert len([x for x in lines if x.startswith("  ok ")]) == 5
    assert lines[-1] == "  and 3 more ended ok"


def test_ended_rows_keeps_every_end(board):
    events.start("task", "B-001", board=board, now=at(0))
    events.end("task", "B-001", "red", board=board, now=at(1))
    events.end("task", "B-001", "ok", board=board, now=at(2))
    paths = types.SimpleNamespace(shared=board.parent)
    assert [r["outcome"] for r in events.ended_rows(paths, at(10))] == ["red", "ok"]
