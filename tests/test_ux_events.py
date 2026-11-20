"""Q-0024: counted frictions in <shared>/ux/events.tsv, awb ux note, awb ux report and the Monday block."""
from __future__ import annotations

import datetime as dt
import io
import json
import os
import stat
import sys
import threading
from pathlib import Path

import pytest

from awb import cli, config, hooks, ownerwait, projects, ux
from tests import fixtures as fx

UTC = dt.timezone.utc
FRIDAY = dt.datetime(2026, 11, 20, 9, 30, tzinfo=UTC)
MONDAY = dt.datetime(2026, 11, 23, 7, 0, tzinfo=UTC)


@pytest.fixture
def world(home, monkeypatch):
    pr = projects.spawn(home, "query", "check the docs", fx.CUSTOMER_CODE, home.register)
    monkeypatch.setattr(config, "paths", lambda: home)
    monkeypatch.setattr(config, "is_work_user", lambda: True)
    monkeypatch.setattr(config, "is_build_user", lambda: False)
    monkeypatch.setattr(ux, "now", lambda: FRIDAY)
    return home, pr


def rows(home) -> list[list[str]]:
    path = home.shared / "ux" / "events.tsv"
    if not path.exists():
        return []
    return [ln.split("\t") for ln in path.read_text(encoding="utf-8").splitlines()]


def _checkout(base: Path) -> Path:
    co = base / "q24-checkout"
    (co / ".git").mkdir(parents=True, exist_ok=True)
    (co / "awb").mkdir(parents=True, exist_ok=True)
    (co / "awb" / "cli.py").write_text("")
    return co


def _passing_prompt(monkeypatch):
    monkeypatch.setattr(hooks, "_sealed", lambda: False)
    monkeypatch.setattr(hooks, "name_hits", lambda text, p=None: [])
    monkeypatch.setattr(hooks, "_dev_requests", lambda data: "")
    monkeypatch.setattr(hooks, "_handover_notice", lambda data, start=False: "")
    monkeypatch.setattr(hooks, "_rules_text", lambda data: None)
    monkeypatch.setattr(hooks, "_kb_text", lambda data: None)


# --------------------------------------------------------------------------- the phrase list


def test_the_phrase_list_is_lower_case_and_names_only_phrase_classes():
    phrases = ux.load_phrases()
    assert phrases, "rules/ux-phrases.tsv gives no phrase"
    classes = {c for c, _ in phrases}
    assert classes == {"status-question", "nudge", "repeated-rule", "bare-yes", "pasted-block"}
    assert all(p == p.lower() and "\t" not in p for _, p in phrases)


@pytest.mark.parametrize("text,want", [
    ("работай дальше", "nudge"),
    ("ok, go on with the next step", "nudge"),
    ("да", "bare-yes"),
    ("  Да! ", "bare-yes"),
    ("да, но поменяй второй пункт", None),
    ("я же говорил: только коды", "repeated-rule"),
    ("статус?", "status-question"),
    ("where are we with the export", "status-question"),
    ('look at this <pasted_content id="a1">some lines</pasted_content id="a1">', "pasted-block"),
    ("build the parser for the price sheet", None),
    ("gone", None),
    ("<task-notification>go on</task-notification>", None),
])
def test_a_prompt_gets_the_class_of_its_first_matching_phrase(text, want):
    assert ux.classify(text) == want


def test_a_phrase_file_that_cannot_be_read_gives_no_class(tmp_path):
    assert ux.load_phrases(tmp_path / "missing.tsv") == []
    bad = tmp_path / "bad.tsv"
    bad.write_text("# comment\nnudge\tgo on\nNOT A CLASS\tx\nnote\tgo\nheld-note\tgo\n\nonly one field\n",
                   encoding="utf-8")
    assert ux.load_phrases(bad) == [("nudge", "go on")]
    assert ux.classify("go on", ux.load_phrases(bad)) == "nudge"


# --------------------------------------------------------------------------- the hooks


def test_the_prompt_hook_counts_a_class_and_never_a_word(world, monkeypatch):
    home, pr = world
    _passing_prompt(monkeypatch)
    text = "работай дальше, the parser of the price sheet"
    assert hooks.hook_prompt({"prompt": text, "cwd": str(pr.path), "session_id": "q24"}) == hooks.OK
    assert rows(home) == [["2026-11-20T09:30:00Z", "work", "project", pr.code, "nudge", "-"]]
    body = (home.shared / "ux" / "events.tsv").read_text(encoding="utf-8")
    assert "parser" not in body and "работай" not in body


def test_a_prompt_that_matches_no_phrase_writes_nothing(world, monkeypatch):
    home, pr = world
    _passing_prompt(monkeypatch)
    assert hooks.hook_prompt({"prompt": "build the parser", "cwd": str(pr.path)}) == hooks.OK
    assert not (home.shared / "ux").exists()


def test_a_blocked_prompt_writes_nothing(world, monkeypatch, capsys):
    home, pr = world
    _passing_prompt(monkeypatch)
    monkeypatch.setattr(hooks, "name_hits", lambda text, p=None: [{"cls": "name", "start": 0, "end": 2}])
    assert hooks.hook_prompt({"prompt": "go on", "cwd": str(pr.path)}) == hooks.BLOCK
    capsys.readouterr()
    assert rows(home) == []


def test_the_prompt_hook_makes_no_vault_call_for_the_count(world, monkeypatch):
    home, pr = world
    from awb import ledger

    def refuse(*a, **k):
        raise AssertionError("the count called the name check")

    monkeypatch.setattr(ledger, "screen", refuse)
    monkeypatch.setattr(ledger, "hit_indexes", refuse)
    assert ux.hook_event("prompt", {"prompt": "go on", "cwd": str(pr.path)}) == "nudge"
    assert rows(home)[0][4] == "nudge"


def test_the_owner_prompt_hook_counts_as_owner_in_development(world, monkeypatch, tmp_path, capsys):
    home, _ = world
    monkeypatch.setattr(config, "is_work_user", lambda: False)
    monkeypatch.setattr(hooks, "for_this_user", lambda: False)
    monkeypatch.setattr(hooks, "_dev_requests", lambda data: "")
    monkeypatch.setattr(hooks, "_handover_notice", lambda data, start=False: "")
    co = _checkout(tmp_path)
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps({"cwd": str(co), "prompt": "статус?"})))
    assert hooks.main(["prompt"]) == 0
    capsys.readouterr()
    assert rows(home) == [["2026-11-20T09:30:00Z", "owner", "development", "-", "status-question", "-"]]


def test_the_pre_compact_hook_counts_a_compaction_at_the_desk(world, monkeypatch, tmp_path):
    home, _ = world
    monkeypatch.setattr(config, "is_work_user", lambda: False)
    desk = tmp_path / "desk"
    desk.mkdir()
    assert hooks.hook_pre_compact({"cwd": str(desk), "session_id": "q24"}) == hooks.OK
    assert rows(home) == [["2026-11-20T09:30:00Z", "owner", "desk", "-", "compaction", "-"]]


def test_the_stop_hook_counts_a_held_reply_by_its_reason(world, monkeypatch, capsys):
    home, pr = world
    monkeypatch.setattr(hooks, "draft_ledger", lambda *a, **k: False)
    monkeypatch.setattr(hooks, "turn_prompt", lambda transcript: None)
    monkeypatch.setattr(hooks, "draft_gate", lambda data, root: ["run awb write check on the draft"])
    monkeypatch.setattr(projects, "mail_owner_commands", lambda *a, **k: None)
    from awb import status as _status
    from awb import opensweep
    monkeypatch.setattr(_status, "reminder", lambda root: None)
    monkeypatch.setattr(opensweep, "reminder", lambda root: None)
    assert hooks.hook_stop({"cwd": str(pr.path), "session_id": "q24"}) == hooks.BLOCK
    capsys.readouterr()
    assert [r[4] for r in rows(home)] == ["held-draft"]
    monkeypatch.setattr(hooks, "draft_gate", lambda data, root: [])
    assert hooks.hook_stop({"cwd": str(pr.path), "session_id": "q24"}) == hooks.OK
    assert len(rows(home)) == 1


def test_a_hook_count_that_cannot_write_never_fails_the_hook(world, monkeypatch, tmp_path):
    home, pr = world
    _passing_prompt(monkeypatch)
    home.shared.mkdir(parents=True, exist_ok=True)
    (home.shared / "ux").symlink_to(tmp_path)
    assert hooks.hook_prompt({"prompt": "go on", "cwd": str(pr.path)}) == hooks.OK
    assert not (tmp_path / "events.tsv").exists()


# --------------------------------------------------------------------------- the shared file


def test_the_folder_is_2775_and_the_file_664(world):
    home, pr = world
    old = os.umask(0o022)
    try:
        ux.append(ux.line("nudge", "project", pr.code, "work"))
    finally:
        os.umask(old)
    folder, path = home.shared / "ux", home.shared / "ux" / "events.tsv"
    assert stat.S_IMODE(os.stat(folder).st_mode) == 0o2775
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o664


def test_a_link_in_place_of_the_file_is_refused(world, tmp_path):
    home, _ = world
    (home.shared / "ux").mkdir(parents=True)
    target = tmp_path / "elsewhere.tsv"
    target.write_text("")
    (home.shared / "ux" / "events.tsv").symlink_to(target)
    with pytest.raises(OSError):
        ux.append(ux.line("nudge", "desk", "-", "owner"))
    assert target.read_text() == ""


def test_appends_from_many_writers_keep_whole_lines(world):
    home, _ = world

    def write(n):
        for _ in range(25):
            ux.append(ux.line("nudge", "desk", "-", "owner", "x" * 200))

    threads = [threading.Thread(target=write, args=(n,)) for n in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    got = rows(home)
    assert len(got) == 100 and all(len(r) == 6 and r[5] == "x" * 200 for r in got)


# --------------------------------------------------------------------------- awb ux note


def test_awb_ux_note_writes_a_checked_note_line(world, monkeypatch, capsys):
    home, pr = world
    monkeypatch.chdir(pr.path)
    assert cli.main(["ux", "note", "the dictation gate\tasked twice for one prompt"]) == 0
    assert pr.code in capsys.readouterr().out
    assert rows(home) == [["2026-11-20T09:30:00Z", "work", "project", pr.code, "note",
                           "the dictation gate asked twice for one prompt"]]


def test_awb_ux_note_refuses_a_registered_name_and_writes_nothing(world, monkeypatch, capsys):
    home, pr = world
    monkeypatch.chdir(pr.path)
    capsys.readouterr()
    assert cli.main(["ux", "note", "the export for %s waited" % fx.CUSTOMER_FORMS[0]]) == 2
    out = capsys.readouterr()
    fx.assert_no_fixture_name(out.out + out.err)
    assert rows(home) == []


def test_awb_ux_note_refuses_an_empty_note(world, capsys):
    assert cli.main(["ux", "note", "   "]) == 2
    assert "empty" in capsys.readouterr().err


# --------------------------------------------------------------------------- awb ux report


def _event(at: dt.datetime, cls: str, text: str = "-") -> str:
    return ux.line(cls, "project", "tcp-q7m4", "work", text, at)


def _seed(home, spec):
    for days_back, cls, n in spec:
        for _ in range(n):
            ux.append(_event(FRIDAY - dt.timedelta(days=days_back), cls))


def test_awb_ux_report_counts_per_week_with_the_change(world, monkeypatch, capsys):
    home, _ = world
    # FRIDAY is in 2026-W47; 7 days back W46, 14 days back W45
    _seed(home, [(0, "status-question", 2), (7, "status-question", 5), (14, "status-question", 1),
                 (0, "nudge", 3), (7, "nudge", 1), (30, "nudge", 9)])
    ux.append(_event(FRIDAY, "note", "a wait of ten minutes on the gate"))
    assert cli.main(["ux", "report", "--since", "2026-11-02"]) == 0
    out = capsys.readouterr().out.splitlines()
    assert out[0].split() == ["class", "W45", "W46", "W47", "change"]
    table = {ln.split()[0]: ln.split()[1:] for ln in out[1:4]}
    assert table["status-question"] == ["1", "5", "2", "-3"]
    assert table["nudge"] == ["0", "1", "3", "+2"]
    assert table["note"] == ["0", "0", "1", "+1"]
    assert "change: 2026-W47 against 2026-W46; 2026-W47 runs until Sunday" in out
    assert out[-1] == "note 2026-11-20 project tcp-q7m4: a wait of ten minutes on the gate"


def test_awb_ux_report_without_since_shows_four_weeks(world, capsys):
    home, _ = world
    _seed(home, [(0, "nudge", 1), (30, "nudge", 9)])
    assert cli.main(["ux", "report"]) == 0
    head = capsys.readouterr().out.splitlines()[0].split()
    assert head == ["class", "W44", "W45", "W46", "W47", "change"]


def test_awb_ux_report_with_no_events_and_a_bad_date(world, capsys):
    assert cli.main(["ux", "report"]) == 0
    assert capsys.readouterr().out.startswith("no ux events since 2026-10-26")
    assert cli.main(["ux", "report", "--since", "last week"]) == 2
    err = capsys.readouterr().err
    assert "YYYY-MM-DD" not in err and "2026-11-16" in err and "last week" not in err


# --------------------------------------------------------------------------- the Monday block of the owner status


def test_the_owner_block_shows_five_lines_on_a_monday_only(world):
    home, _ = world
    # MONDAY 2026-11-23 starts W48: the block is W47 (FRIDAY's week) against W46
    _seed(home, [(0, "status-question", 4), (7, "status-question", 6), (0, "nudge", 3), (0, "bare-yes", 2),
                 (0, "repeated-rule", 1), (0, "compaction", 1), (7, "pasted-block", 2)])
    lines = ux.owner_lines(MONDAY, home)
    assert len(lines) == 5
    assert lines[0] == "UX 2026-W47: 11 events (+3 against 2026-W46), awb ux report for the table"
    assert lines[1:] == ["  status-question: 4 (-2)", "  nudge: 3 (+3)", "  bare-yes: 2 (+2)",
                         "  compaction: 1 (+1)"]
    assert ux.owner_lines(MONDAY + dt.timedelta(days=1), home) == []
    assert ux.owner_lines(FRIDAY, home) == []


def test_the_owner_status_notes_carry_the_block_on_a_monday(world):
    home, _ = world
    _seed(home, [(0, "nudge", 2)])
    got = ownerwait.notes(path=home.shared / "none.json", now=MONDAY, p=home)
    assert "UX 2026-W47: 2 events (+2 against 2026-W46), awb ux report for the table" in got
    assert "  nudge: 2 (+2)" in got
    assert not any(ln.startswith("UX ") for ln in ownerwait.notes(path=home.shared / "none.json", now=FRIDAY, p=home))


def test_the_owner_block_is_empty_without_events(world):
    home, _ = world
    assert ux.owner_lines(MONDAY, home) == []
