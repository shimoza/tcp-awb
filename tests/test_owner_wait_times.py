"""Internal mode step 1: the wait history of the owner items, `awb owner status` with hours, `--history CODE`,
`--record`, the summary of the shared side and the column "Waits on you" of the board."""
from __future__ import annotations

import copy
import datetime as dt
import json
import os
import stat
from pathlib import Path

import pytest

from awb import config, ledger, ownerwait, ownerwaits
from awb.ownerwait import Item, WHERE_SESSION, WHERE_TERMINAL
from awb.tcp import board, owner

T0 = dt.datetime(2026, 11, 23, 17, 48, tzinfo=dt.timezone.utc)
A = "tcp-q7m4"
B = "tcp-kx2a"


@pytest.fixture(autouse=True)
def state_folder(monkeypatch, tmp_path):
    """The state folder of the report timer, as systemd makes it (StateDirectory=), and no queue of the owner."""
    ownerwait.report_dir().mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("AWB_WORK_QUEUE", str(tmp_path / "no-queue.tsv"))


def at(hours: float) -> dt.datetime:
    return T0 + dt.timedelta(hours=hours)


def items(*texts, code=A, where=WHERE_SESSION):
    return [Item(code, t, where, kind="owner", source=t + " (full)") for t in texts]


def history() -> Path:
    folder = ownerwait.report_dir()
    folder.mkdir(parents=True, exist_ok=True)
    return folder / ownerwaits.HISTORY


def shared_folder() -> Path:
    folder = Path(config.paths().shared) / ownerwaits.SHARED_DIR
    folder.mkdir(parents=True, exist_ok=True)
    return folder


def test_an_item_is_opened_kept_and_closed_with_its_times_and_no_text():
    path = history()
    ownerwaits.record(items("read the chapter", "put the copy"), at(0))
    entries = ownerwaits.record(items("read the chapter", "put the copy"), at(5))
    assert [(e["first"], e["last"], e["end"]) for e in entries] == [("2026-11-23T17:48:00Z", "2026-11-23T22:48:00Z",
                                                                     None)] * 2
    entries = ownerwaits.record(items("read the chapter"), at(13.15))
    closed = [e for e in entries if e["end"]]
    assert len(closed) == 1 and closed[0]["end"] == "2026-11-24T06:57:00Z" and closed[0]["code"] == A
    raw = path.read_text(encoding="utf-8")
    assert "chapter" not in raw and "copy" not in raw and "(full)" not in raw
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600
    assert not [f for f in path.parent.iterdir() if f.name.startswith(".waits.") and f.name != ownerwaits.LOCK]
    # the same item back after it was gone opens a new wait
    entries = ownerwaits.record(items("read the chapter", "put the copy"), at(20))
    assert len(entries) == 3 and sum(1 for e in entries if e["end"] is None) == 2


def test_old_entries_go_after_sixty_days_and_the_size_is_capped(monkeypatch):
    ownerwaits.record(items("one", "two"), at(0))
    ownerwaits.record([], at(1))
    entries = ownerwaits.record(items("three"), at(24 * 61))
    assert [e["first"] for e in entries] == [ownerwaits.stamp(at(24 * 61))]
    monkeypatch.setattr(ownerwaits, "MAX_ENTRIES", 3)
    for n in range(6):
        ownerwaits.record(items("round %d" % n), at(24 * 62 + n))
    entries = ownerwaits.load()
    assert len(entries) == 3 and [e for e in entries if e["end"] is None][0]["first"] == ownerwaits.stamp(
        at(24 * 62 + 5))


def test_a_broken_history_is_read_as_empty_and_a_bad_entry_is_dropped():
    path = history()
    path.write_text("{not json", encoding="utf-8")
    assert ownerwaits.load() == []
    good = {"key": "0123456789abcdef", "code": A, "where": WHERE_SESSION, "first": "2026-11-23T17:48:00Z",
            "last": "2026-11-23T17:48:00Z", "end": None}
    path.write_text(json.dumps({"entries": [good, dict(good, key="x"), dict(good, first="yesterday"),
                                            dict(good, where="somewhere")]}), encoding="utf-8")
    assert ownerwaits.load() == [good]


def test_the_list_shows_the_hours_after_each_item_and_a_kept_line_takes_the_longest():
    p = config.paths()
    first = items("read the chapter") + items("run the import", where=WHERE_TERMINAL)
    later = items("read the chapter") + items("run the import", where=WHERE_TERMINAL) + items("a new one")
    kept = [Item(B, "", WHERE_SESSION, kind="kept", extra="test-00001", source="create one"),
            Item(B, "", WHERE_SESSION, kind="kept", extra="test-00002", source="create two")]
    ownerwait.record_waits(p, kept[:1], now=at(-3))
    ownerwait.record_waits(p, first + kept, now=at(0))
    word = ownerwait.record_waits(p, later + kept, now=at(14.5))
    assert word.startswith("5 open, 0 closed") and word.endswith("board summary not written")
    lines = ownerwait.render(ownerwait.decide(later + kept), 0)
    assert "%s  read the chapter  [project session]  waits 14 h" % A in lines
    assert "%s  run the import  [his terminal]  waits 14 h" % A in lines
    assert "%s  a new one  [project session]  waits <1 h" % A in lines
    assert any(x.startswith(B) and x.endswith("waits 17 h") for x in lines)
    # an item the history does not know renders as before
    assert ownerwait.render([Item(A, "plain", WHERE_SESSION)], 0)[1] == "%s  plain  [project session]" % A


def test_the_measure_leaves_out_tooling_ended_and_online_lines():
    online = Item("labbox", "lab machine online", "")
    pool = items("decide this") + [Item(A, "fix the hook", "", kind="dev"), Item("B-0001", "ended", "", kind="done"),
                                   online, Item("refresh", "weekly run failed: x", "")]
    assert [it.text for it in ownerwait.waiting(pool, [online])] == ["decide this", "weekly run failed: x"]


def test_collect_records_every_item_whatever_the_view(monkeypatch):
    p = config.paths()
    monkeypatch.setattr(ownerwait, "open_items", lambda p: (items("first") + items("second", code=B), 0))
    monkeypatch.setattr(ledger, "hit_indexes", lambda texts, reg: set())
    shown, _ = ownerwait.collect(p, view="all", code=A, record=True, now=at(0))
    assert [it.label for it in shown] == [A] and shown[0].waited == 0.0
    assert sorted(e["code"] for e in ownerwaits.load()) == sorted([A, B])
    assert ownerwait.LAST_RECORD.startswith("2 open")
    ownerwait.collect(p, record=False, now=at(9))
    assert all(e["last"] == "2026-11-23T17:48:00Z" for e in ownerwaits.load())


def test_a_state_folder_that_cannot_be_written_leaves_the_items_without_hours(monkeypatch, tmp_path):
    monkeypatch.setenv(ownerwait.REPORT_ENV, str(tmp_path / "missing"))
    pool = items("first")
    assert ownerwait.record_waits(config.paths(), pool, now=at(0)) == "not recorded: FileNotFoundError"
    assert pool[0].waited is None


def test_the_summary_carries_project_codes_counts_and_times_only():
    p = config.paths()
    pool = items("first", "second") + items("third", code=B) + [Item("reports", "the last put failed", "")]
    assert not ownerwaits.write_summary(p, ownerwaits.record(pool, at(0)), at(0))
    folder = shared_folder()
    ownerwaits.record(items("first", "second"), at(1))
    entries = ownerwaits.record(pool, at(2))
    assert ownerwaits.write_summary(p, entries, at(2))
    path = folder / ownerwaits.SUMMARY
    assert path.read_text(encoding="utf-8") == (
        "# owner waits, written 2026-11-23T19:48:00Z\ncode\topen\tsince\n"
        "%s\t1\t2026-11-23T19:48:00Z\n%s\t2\t2026-11-23T17:48:00Z\n" % (B, A))
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o640
    seen = ownerwaits.read_summary(p, at(3))
    assert seen["state"] == "ok" and seen["hours"] == {A: 3.0, B: 1.0}
    assert ownerwaits.read_summary(p, at(6))["state"] == "stale"


def test_the_reader_drops_a_row_out_of_form_and_refuses_a_link(tmp_path):
    p = config.paths()
    folder = shared_folder()
    path = folder / ownerwaits.SUMMARY
    path.write_text("# owner waits, written 2026-11-23T17:48:00Z\ncode\topen\tsince\n"
                    "%s\t1\t2026-11-23T17:48:00Z\nsomebody\t1\t2026-11-23T17:48:00Z\n%s\tmany\tnow\n" % (A, B),
                    encoding="utf-8")
    assert ownerwaits.read_summary(p, at(2))["hours"] == {A: 2.0}
    path.unlink()
    other = tmp_path / "elsewhere.tsv"
    other.write_text("# owner waits, written 2026-11-23T17:48:00Z\ncode\topen\tsince\n", encoding="utf-8")
    path.symlink_to(other)
    assert ownerwaits.read_summary(p, at(1))["state"] == "missing"
    path.unlink()
    assert ownerwaits.read_summary(p, at(1)) == {"state": "missing", "written": None, "hours": {}}


def _board(waits=None) -> dict:
    p = {"code": A, "kind": "tcp", "customer": "none", "goal": "g", "created": "2026-11-20",
         "status": {"summary": "s", "next": "n", "updated": None, "behind": 0}, "open_items": 4,
         "live_resources": 0, "waiting_on": {"items": [], "total": 0}, "open": {"items": [], "total": 0},
         "deliverables": {"status": "available", "counts": {}}}
    data = {"summary": {"projects": 2, "current": 2, "lagging": 0, "without_status": 0},
            "generated_at": "2026-11-24T07:00:00+00:00", "projects": [p, dict(copy.deepcopy(p), code=B)],
            "parked": []}
    if waits is not None:
        data["waits"] = waits
    return data


def test_the_board_has_a_column_waits_on_you():
    data = _board({"state": "ok", "written": "2026-11-24T06:57:00Z", "hours": {A: 13.15}})
    md, page = board.markdown(data), board.page(data)
    assert "| Project | Customer | Status | Next | Status as of | Open | Waits on you | Live |" in md
    assert "| %s (tcp) | internal | s | n | unknown | 4 | 13 h | 0 |" % A in md
    assert "| %s (tcp) | internal | s | n | unknown | 4 | - | 0 |" % B in md
    assert "<th>Waits on you</th>" in page and "<td>13 h</td>" in page
    assert "waits on you:" not in md
    stale = board.markdown(_board({"state": "stale", "written": "2026-11-23T20:00:00Z", "hours": {}}))
    assert "waits on you: not measured since 23.11.2026 21:00 CET" in stale
    assert "waits on you: not measured yet" in board.page(_board({"state": "missing", "written": None, "hours": {}}))
    assert "waits on you:" not in board.markdown(_board())


def test_history_lists_closed_and_open_waits_of_a_project(capsys):
    ownerwaits.record(items("first", "second", where=WHERE_TERMINAL) + items("other", code=B), at(0))
    ownerwaits.record(items("first", where=WHERE_TERMINAL), at(13.15))
    lines = ownerwaits.history_lines(A, ownerwaits.load(), at(20))
    assert lines[:2] == ["%s: 2 waits, 1 open (UTC)" % A, "start             end               hours  where"]
    assert sorted(lines[2:]) == ["2026-11-23 17:48  2026-11-24 06:57   13 h  his terminal",
                                 "2026-11-23 17:48  open               20 h  his terminal"]
    assert owner.main(["status", "--history", "tcp-zz99"]) == 0
    assert capsys.readouterr().out == "tcp-zz99: no wait recorded (the history keeps 60 days)\n"
    assert owner.main(["status", "--history", A]) == 0
    out = capsys.readouterr().out
    assert out.startswith("%s: 2 waits, 1 open (UTC)\n" % A) and "first" not in out and B not in out


def test_record_prints_one_line_and_fails_when_nothing_could_be_written(monkeypatch, capsys, tmp_path):
    monkeypatch.setattr(ownerwait, "open_items", lambda p: (items("first"), 0))
    monkeypatch.setattr(ledger, "hit_indexes", lambda texts, reg: set())
    history()
    assert owner.main(["status", "--record"]) == 0
    assert capsys.readouterr().out.startswith("awb owner: waits 1 open, 0 closed in 60 days; board summary")
    monkeypatch.setenv(ownerwait.REPORT_ENV, str(tmp_path / "missing"))
    assert owner.main(["status", "--record"]) == 1
    assert capsys.readouterr().out == "awb owner: waits not recorded: FileNotFoundError\n"


def test_the_hourly_unit_records_after_the_put_and_may_write_only_the_summary_folder(tmp_path):
    from tests.test_seal import _persist_run

    unit = (Path(__file__).resolve().parents[1] / "seal" / "work-jobs" / "awb-report-codes.service").read_text()
    lines = unit.splitlines()
    assert "ExecStopPost=-/usr/local/bin/awb owner status --record" in lines
    assert "ReadWritePaths=-@WORK_HOME@/tcp-shared/owner-waits" in lines
    assert sum(1 for x in lines if x.startswith("ExecStart=")) == 1
    res, _ = _persist_run(tmp_path, 'OWNER_JOBS=awb-report-codes\nowner=ownr\nCONF_FILE=/etc/awb/paths.conf\n'
                                    'step_work_units\n')
    assert res.returncode == 0, res.stderr + res.stdout
    text = (tmp_path / "units" / "awb-report-codes.service").read_text()
    assert "ReadWritePaths=-%s/work/tcp-shared/owner-waits\n" % tmp_path in text and "@" not in text
    assert (tmp_path / "work" / "tcp-shared" / "owner-waits").is_dir()
    assert "install -d -o awb -g awb -m 2770 %s/work/tcp-shared/owner-waits" % tmp_path in res.stdout


def test_nothing_is_recorded_while_the_name_check_cannot_run(monkeypatch):
    p = config.paths()
    monkeypatch.setattr(ownerwait, "open_items", lambda p: (items("first"), 0))
    monkeypatch.setattr(ledger, "hit_indexes", lambda texts, reg: set())
    ownerwait.collect(p, view="all", record=True, now=at(0))

    def broken(texts, reg):
        raise ledger.LedgerError("no check")
    monkeypatch.setattr(ledger, "hit_indexes", broken)
    shown, _ = ownerwait.collect(p, view="all", record=True, now=at(5))
    assert ownerwait.LAST_RECORD == "not recorded: the name check cannot run" and shown[0].waited is None
    assert [(e["code"], e["last"], e["end"]) for e in ownerwaits.load()] == [(A, "2026-11-23T17:48:00Z", None)]
