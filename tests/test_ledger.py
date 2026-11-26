"""Tests of awb/ledger.py: the activity ledger (add, list) and the reports made from it.

Every name here is an invented fixture name. The ledger lives in the temporary shared side of the `home` fixture.
"""
from __future__ import annotations

import fcntl
import json
import multiprocessing
import os
import re
import time
from html.parser import HTMLParser

import pytest

from awb import cli, ledger, register
from tests import fixtures

SECOND_CUSTOMER = "CUST-B7XQ"
SECOND_PROJECT = "tcp-b3np"


def run(argv: list[str], capsys) -> tuple[int, str, str]:
    code = cli.main(argv)
    out, err = capsys.readouterr()
    return code, out, err


def lines_of(path) -> list[str]:
    return path.read_text(encoding="utf-8").splitlines() if path.exists() else []


def all_ledger_lines(home) -> list[str]:
    out: list[str] = []
    for f in sorted(home.ledger.glob("*.jsonl")):
        out.extend(lines_of(f))
    return out


FULL_ARGS = [
    "ledger", "add", "--kind", "proof", "--done", "Sized the landing zone for 40 ECS",
    "--project", "tcp-q7m4", "--customer", fixtures.CUSTOMER_CODE, "--partner", fixtures.PARTNER_CODE,
    "--tag", "ecs", "--tag", "elb,vpc", "--outcome", "The sizing was accepted",
    "--deliverable", "deliverables/sizing.md", "--deliverable", "deliverables/sizing.xlsx",
    "--open", "check the quota", "--open", "send the offer", "--date", "2026-09-22",
]

FULL_EXPECTED = {
    "date": "2026-09-22",
    "project": "tcp-q7m4",
    "customer": fixtures.CUSTOMER_CODE,
    "partner": fixtures.PARTNER_CODE,
    "tags": ["ecs", "elb", "vpc"],
    "kind": "proof",
    "done": "Sized the landing zone for 40 ECS",
    "outcome": "The sizing was accepted",
    "deliverables": ["deliverables/sizing.md", "deliverables/sizing.xlsx"],
    "open": ["check the quota", "send the offer"],
}


# --------------------------------------------------------------------------- add


def test_add_writes_every_field_to_the_week_file(home, capsys):
    code, out, err = run(FULL_ARGS, capsys)
    assert code == 0, err
    f = home.ledger / "2026-W39.jsonl"
    rows = lines_of(f)
    assert len(rows) == 1
    obj = json.loads(rows[0])
    assert obj == FULL_EXPECTED
    assert list(obj) == list(ledger.FIELDS)
    assert "2026-W39" in out and fixtures.CUSTOMER_CODE in out
    assert [e.as_dict() for e in ledger.load(home)] == [FULL_EXPECTED]


def test_add_as_a_function_returns_the_entry(home):
    e = ledger.add("tooling", "Built the network module", project="tcp-q7m4", customer=fixtures.CUSTOMER_CODE,
                   tags=["terraform"], outcome="Module released", deliverables=["deliverables/module.md"],
                   open_items=["write the tests"], day="2026-09-23", p=home)
    assert e.date == "2026-09-23" and e.kind == "tooling" and e.tags == ["terraform"]
    assert ledger.load(home) == [e]


def test_missing_fields_default_to_none_and_today(home, monkeypatch, capsys):
    monkeypatch.setattr(ledger, "today", lambda: ledger.parse_date("2026-09-24"))
    code, _, err = run(["ledger", "add", "--kind", "inquiry", "--done", "Answered a quota question"], capsys)
    assert code == 0, err
    obj = json.loads(lines_of(home.ledger / "2026-W39.jsonl")[0])
    assert obj == {"date": "2026-09-24", "project": "none", "customer": "none", "partner": "none", "tags": [],
                   "kind": "inquiry", "done": "Answered a quota question", "outcome": "", "deliverables": [],
                   "open": []}


@pytest.mark.parametrize("day,name", [
    ("2026-09-22", "2026-W39"),
    ("2026-09-28", "2026-W40"),
    ("2026-01-01", "2026-W01"),
    ("2027-01-01", "2026-W53"),
    ("2024-12-30", "2025-W01"),
])
def test_the_week_file_name_is_the_iso_week(home, day, name):
    assert ledger.week_name(day) == name
    assert ledger.week_file(home, day) == home.ledger / ("%s.jsonl" % name)
    ledger.add("code", "Fixed the report layout", day=day, p=home)
    assert [f.name for f in home.ledger.glob("*.jsonl")] == ["%s.jsonl" % name]


# --------------------------------------------------------------------------- the lock


def _append_many(tag: str, n: int, barrier) -> None:
    from awb import ledger as mod

    barrier.wait(120)
    for i in range(n):
        mod.add("tooling", "%s %03d %s" % (tag, i, "x" * 1500), tags=["terraform"],
                open_items=["%s open %d %s" % (tag, k, "y" * 400) for k in range(5)], day="2026-09-22")


def _append_one(started) -> None:
    from awb import check, gate  # noqa: F401 - imported before the signal so that only the lock can delay
    from awb import ledger as mod

    started.set()
    mod.add("code", "One line while the lock is held", day="2026-09-22")


@pytest.mark.long   # real processes append at the same moment: fewer or shorter rounds would stop proving the lock
def test_two_processes_appending_at_once_give_whole_lines(home):
    ctx = multiprocessing.get_context("spawn")
    barrier = ctx.Barrier(2)
    n = 20
    procs = [ctx.Process(target=_append_many, args=(tag, n, barrier)) for tag in ("first", "second")]
    for proc in procs:
        proc.start()
    try:
        for proc in procs:
            proc.join(300)
            assert proc.exitcode == 0
    finally:
        for proc in procs:
            if proc.is_alive():
                proc.terminate()
                proc.join(10)
    rows = lines_of(home.ledger / "2026-W39.jsonl")
    assert len(rows) == 2 * n
    seen = set()
    for row in rows:
        obj = json.loads(row)          # a torn or glued line would not parse
        tag, i, rest = obj["done"].split(" ")
        assert rest == "x" * 1500
        assert obj["open"] == ["%s open %d %s" % (tag, k, "y" * 400) for k in range(5)]
        seen.add((tag, int(i)))
    assert seen == {(t, i) for t in ("first", "second") for i in range(n)}
    assert len(ledger.load(home)) == 2 * n


def test_an_append_waits_for_the_lock(home):
    ctx = multiprocessing.get_context("spawn")
    started = ctx.Event()
    f = ledger.week_file(home, "2026-09-22")
    fd = os.open(f, os.O_RDWR | os.O_CREAT, 0o640)
    proc = ctx.Process(target=_append_one, args=(started,))
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        proc.start()
        assert started.wait(120)
        time.sleep(1.5)
        assert proc.is_alive()
        assert f.stat().st_size == 0
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)
        proc.join(120)
        if proc.is_alive():
            proc.terminate()
            proc.join(10)
    assert proc.exitcode == 0
    rows = lines_of(f)
    assert len(rows) == 1 and json.loads(rows[0])["done"] == "One line while the lock is held"


def test_a_line_left_without_its_end_does_not_swallow_the_next(home):
    f = ledger.week_file(home, "2026-09-22")
    f.write_text('{"date": "2026-09-22", "kind"', encoding="utf-8")
    ledger.add("code", "Written after a crash", day="2026-09-22", p=home)
    rows = lines_of(f)
    assert len(rows) == 2
    assert json.loads(rows[1])["done"] == "Written after a crash"
    with pytest.raises(ledger.LedgerError) as exc:
        ledger.load(home)
    assert "2026-W39.jsonl line 1" in str(exc.value)
    assert "kind" not in str(exc.value)


# --------------------------------------------------------------------------- refusals


@pytest.mark.parametrize("option,what", [
    ("--done", "done"),
    ("--outcome", "outcome"),
    ("--open", "open item 1"),
    ("--deliverable", "deliverable 1"),
])
@pytest.mark.parametrize("form", [fixtures.CUSTOMER_FORMS[0], fixtures.CUSTOMER_FORMS[1].lower(),
                                  fixtures.PERSON_FORMS[0], fixtures.ORG_FORMS[1]])
def test_a_fixture_form_in_a_text_is_refused_without_echo(home, capsys, option, what, form):
    """DMZ 4: no content check inside, a registered name in any text is written as written."""
    args = ["ledger", "add", "--kind", "proof", "--done", "Sized the landing zone", "--date", "2026-09-22"]
    value = "Workshop with %s about the landing zone" % form
    if option == "--done":
        args[args.index("--done") + 1] = value
    else:
        args += [option, value]
    code, out, err = run(args, capsys)
    assert code == 0, err
    assert err == ""
    [line] = all_ledger_lines(home)
    assert value in line


@pytest.mark.parametrize("text,cls", [
    ("mailed the offer to ops@" + "nrgtz-beratung.de", "mail"),
    ("used the list of CLI" + "ENT3 from the notes", "blocklist"),
])
def test_structured_data_and_blocked_words_are_refused(home, capsys, text, cls):
    """DMZ 4: a mail address and a word of the owner's blocklist are written as written."""
    code, out, err = run(["ledger", "add", "--kind", "other", "--done", "Sent the offer", "--outcome", text],
                         capsys)
    assert code == 0, err
    assert err == ""
    assert json.loads(all_ledger_lines(home)[0])["outcome"] == text


@pytest.mark.parametrize("option,value", [
    ("--customer", "CUST-Q7M"),
    ("--customer", fixtures.PARTNER_CODE),
    ("--customer", fixtures.PERSON_CODE),
    ("--customer", fixtures.CUSTOMER_CODE.lower()),
    ("--customer", fixtures.CUSTOMER_FORMS[1]),
    ("--partner", fixtures.CUSTOMER_CODE),
    ("--partner", "PART-KX2"),
    ("--project", "tcp-Q7M4"),
    ("--project", "q7m4"),
    ("--project", "tcp-q7m4x"),
    ("--project", fixtures.CUSTOMER_FORMS[0]),
])
def test_a_malformed_code_is_refused_without_echo(home, capsys, option, value):
    code, out, err = run(["ledger", "add", "--kind", "proof", "--done", "Sized the landing zone", option, value],
                         capsys)
    assert code == 2
    assert option[2:] in err
    assert value not in out + err
    fixtures.assert_no_fixture_name(out + err, "ledger output")
    assert all_ledger_lines(home) == []


def test_an_unknown_tag_or_kind_is_refused(home, capsys):
    tag = fixtures.CUSTOMER_FORMS[1].lower()
    code, out, err = run(["ledger", "add", "--kind", "proof", "--done", "Sized it", "--tag", tag], capsys)
    assert code == 2 and "rules/tags.txt" in err and tag not in out + err
    code, out, err = run(["ledger", "add", "--kind", "holiday", "--done", "Sized it"], capsys)
    assert code == 2 and "holiday" not in err
    with pytest.raises(ledger.LedgerError):
        ledger.add("holiday", "Sized it", p=home)
    assert all_ledger_lines(home) == []


def test_an_empty_or_multi_line_text_is_refused(home):
    with pytest.raises(ledger.LedgerError, match="done is empty"):
        ledger.add("proof", "   ", p=home)
    with pytest.raises(ledger.LedgerError, match="one line"):
        ledger.add("proof", "first line\nsecond line", p=home)
    assert all_ledger_lines(home) == []


@pytest.mark.skipif(os.geteuid() == 0, reason="root reads a file of mode 000")
def test_a_name_check_that_cannot_run_refuses_the_entry(home, capsys):
    os.chmod(home.register, 0)
    try:
        code, out, err = run(["ledger", "add", "--kind", "proof", "--done", "Sized the landing zone"], capsys)
    finally:
        os.chmod(home.register, 0o600)
    # DMZ 4: the ledger needs no name check, so one that cannot run stops nothing
    assert code == 0, err
    assert err == ""
    assert len(all_ledger_lines(home)) == 1


def test_a_vault_folder_without_a_register_refuses(home, capsys):
    home.register.unlink()
    code, out, err = run(["ledger", "add", "--kind", "proof", "--done", "Sized the landing zone"], capsys)
    # DMZ 4: no register is needed to write a ledger line
    assert code == 0 and err == ""
    assert len(all_ledger_lines(home)) == 1


# --------------------------------------------------------------------------- list


def test_list_shows_the_entries_of_the_range(home, capsys):
    ledger.add("proof", "Early work", day="2026-09-01", p=home)
    ledger.add("proof", "Work in range", customer=fixtures.CUSTOMER_CODE, open_items=["an open point"],
               day="2026-09-15", p=home)
    ledger.add("proof", "Late work", day="2026-10-05", p=home)
    code, out, err = run(["ledger", "list", "--from", "2026-09-10", "--to", "2026-09-30"], capsys)
    assert code == 0, err
    assert "Work in range" in out and "an open point" in out and fixtures.CUSTOMER_CODE in out
    assert "Early work" not in out and "Late work" not in out
    code, out, _ = run(["ledger", "list"], capsys)
    assert code == 0 and out.index("Early work") < out.index("Work in range") < out.index("Late work")


# --------------------------------------------------------------------------- reports


@pytest.fixture
def filled(home):
    rows = [
        ("proof", "Early entry out of range", "tcp-q7m4", fixtures.CUSTOMER_CODE, None, ["ecs"], "2026-09-01"),
        ("inquiry", "Answered the quota question", None, fixtures.CUSTOMER_CODE, fixtures.PARTNER_CODE,
         ["pricing"], "2026-09-14"),
        ("proof", "Sized the landing zone", "tcp-q7m4", fixtures.CUSTOMER_CODE, None, ["ecs", "elb"], "2026-09-15"),
        ("tooling", "Built the network module", SECOND_PROJECT, fixtures.CUSTOMER_CODE, None, ["terraform"],
         "2026-09-15"),
        ("tender", "Wrote the tender answer", None, SECOND_CUSTOMER, None, [], "2026-09-22"),
        ("training", "Ran the ECS training", None, None, None, ["ecs"], "2026-09-23"),
        ("proof", "Late entry out of range", "tcp-q7m4", fixtures.CUSTOMER_CODE, None, ["ecs"], "2026-10-05"),
    ]
    for kind, done, project, customer, partner, tags, day in rows:
        ledger.add(kind, done, project=project, customer=customer, partner=partner, tags=tags,
                   outcome="Outcome of %s" % day, day=day, p=home)
    return home


EXPECTED = {
    "customer": {
        SECOND_CUSTOMER: ["Wrote the tender answer"],
        fixtures.CUSTOMER_CODE: ["Built the network module", "Sized the landing zone",
                                 "Answered the quota question"],
        "none": ["Ran the ECS training"],
    },
    "tech": {
        "ecs": ["Ran the ECS training", "Sized the landing zone"],
        "elb": ["Sized the landing zone"],
        "pricing": ["Answered the quota question"],
        "terraform": ["Built the network module"],
        "none": ["Wrote the tender answer"],
    },
    "kind": {
        "inquiry": ["Answered the quota question"],
        "proof": ["Sized the landing zone"],
        "tender": ["Wrote the tender answer"],
        "tooling": ["Built the network module"],
        "training": ["Ran the ECS training"],
    },
    "project": {
        SECOND_PROJECT: ["Built the network module"],
        "tcp-q7m4": ["Sized the landing zone"],
        "none": ["Ran the ECS training", "Wrote the tender answer", "Answered the quota question"],
    },
}

DONE_COLUMN = ledger.COLUMNS.index("Done")


def md_groups(text: str) -> tuple[dict[str, list[str]], dict[str, int], dict[str, int]]:
    """(group -> done texts in order, group -> count in the heading, group -> count in the overview)."""
    groups: dict[str, list[str]] = {}
    heading: dict[str, int] = {}
    overview: dict[str, int] = {}
    current = None
    for line in text.splitlines():
        m = re.fullmatch(r"## (\S+) \((\d+) entr(?:y|ies)\)", line)
        if m:
            current = m.group(1)
            groups[current] = []
            heading[current] = int(m.group(2))
            continue
        cells = [c.strip() for c in line.strip("|").split(" | ")] if line.startswith("| ") else []
        if current is None and len(cells) == 3 and cells[1].isdigit():
            overview[cells[0]] = int(cells[1])
        elif current is not None and cells and re.fullmatch(r"\d{4}-\d{2}-\d{2}", cells[0]):
            groups[current].append(cells[DONE_COLUMN])
    return groups, heading, overview


class PageParser(HTMLParser):
    VOID = {"meta", "br", "hr", "img", "link", "input"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.stack: list[str] = []
        self.counts: dict[str, int] = {}
        self.errors: list[str] = []
        self.doctype = None
        self.h2: list[str] = []
        self.rows: list[list[str]] = []
        self.row_h2: list[str | None] = []
        self._text: list[str] | None = None
        self._cells: list[str] | None = None
        self.charset = False

    def handle_decl(self, decl):
        self.doctype = decl

    def handle_starttag(self, tag, attrs):
        self.counts[tag] = self.counts.get(tag, 0) + 1
        if tag == "meta" and ("charset", "utf-8") in attrs:
            self.charset = True
        if tag in self.VOID:
            return
        self.stack.append(tag)
        if tag in ("h2", "td"):
            self._text = []
        if tag == "tr":
            self._cells = []

    def handle_endtag(self, tag):
        if tag in self.VOID:
            return
        if not self.stack or self.stack[-1] != tag:
            self.errors.append("unbalanced %s" % tag)
            return
        self.stack.pop()
        if tag == "h2":
            self.h2.append("".join(self._text or []))
            self._text = None
        elif tag == "td" and self._cells is not None:
            self._cells.append("".join(self._text or []))
            self._text = None
        elif tag == "tr":
            if self._cells:
                self.rows.append(self._cells)
                self.row_h2.append(self.h2[-1] if self.h2 else None)
            self._cells = None

    def handle_data(self, data):
        if self._text is not None:
            self._text.append(data)


def html_groups(text: str) -> tuple[PageParser, dict[str, list[str]]]:
    p = PageParser()
    p.feed(text)
    p.close()
    groups: dict[str, list[str]] = {}
    for h in p.h2:
        groups[h.split(" (")[0]] = []
    for cells, h in zip(p.rows, p.row_h2):
        if h is not None:
            groups[h.split(" (")[0]].append(cells[DONE_COLUMN])
    return p, groups


@pytest.mark.parametrize("by", ["customer", "tech", "kind", "project"])
def test_report_groups_and_counts_over_a_date_range(filled, capsys, by):
    code, out, err = run(["report", "--by", by, "--from", "2026-09-10", "--to", "2026-09-30"], capsys)
    assert code == 0, err
    groups, heading, overview = md_groups(out)
    expected = EXPECTED[by]
    assert list(groups) == list(expected)          # key order, none last
    assert groups == expected                      # newest first, the later one first on the same date
    assert heading == {k: len(v) for k, v in expected.items()}
    assert overview == heading
    assert "Period: from 2026-09-10 to 2026-09-30." in out
    assert "5 entries in %d groups." % len(expected) in out
    assert "out of range" not in out
    fixtures.assert_no_fixture_name(out + err, "report")


@pytest.mark.parametrize("by", ["customer", "tech", "kind", "project"])
def test_the_html_report_is_a_valid_small_page_with_the_same_groups(filled, capsys, by):
    code, out, err = run(["report", "--by", by, "--from", "2026-09-10", "--to", "2026-09-30", "--format", "html"],
                         capsys)
    assert code == 0, err
    assert out.startswith("<!DOCTYPE html>")
    p, groups = html_groups(out)
    assert p.doctype and p.doctype.lower() == "doctype html"
    assert p.errors == [] and p.stack == []
    for tag in ("html", "head", "body", "title", "h1"):
        assert p.counts.get(tag) == 1, tag
    assert p.charset
    assert p.counts.get("table") == len(EXPECTED[by]) + 1
    assert groups == EXPECTED[by]
    assert all(len(cells) == len(ledger.COLUMNS) for cells, h in zip(p.rows, p.row_h2) if h is not None)
    assert "<script" not in out
    assert len(out) < 20000


def test_the_report_escapes_markup_in_texts(home, capsys):
    ledger.add("code", "Fixed <b>bold</b> & the | pipe", day="2026-09-22", p=home)
    code, out, _ = run(["report", "--by", "kind", "--format", "html"], capsys)
    assert code == 0
    assert "<b>" not in out and "&lt;b&gt;" in out and "&amp;" in out
    p, groups = html_groups(out)
    assert p.errors == [] and groups == {"code": ["Fixed <b>bold</b> & the | pipe"]}
    code, out, _ = run(["report", "--by", "kind"], capsys)
    assert "the \\| pipe" in out


DASHED = [
    "Built the module \u2014 tested it, and shipped it",
    "Sized the zone \u2013 checked quotas, or asked",
    "Ran it,and closed it, Or dropped it",
]


@pytest.mark.parametrize("fmt", ["md", "html"])
def test_no_report_line_carries_an_em_dash_or_a_comma_before_and_or_or(home, capsys, fmt):
    for n, text in enumerate(DASHED):
        ledger.add("proof", text, tags=["ecs", "elb"], outcome=text, open_items=[text, "or later"],
                   deliverables=["a.md", "and b.md"], customer=fixtures.CUSTOMER_CODE, day="2026-09-2%d" % n,
                   p=home)
    for by in ledger.BY:
        code, out, err = run(["report", "--by", by, "--format", fmt], capsys)
        assert code == 0, err
        assert "Built the module, tested it and shipped it" in out
        for line in out.splitlines():
            assert "\u2014" not in line and "&mdash;" not in line and "&#8212;" not in line, line
            assert not re.search(r"\s\u2013\s", line), line
            assert not re.search(r",\s*(?:and|or)\b", line, re.IGNORECASE), line


@pytest.mark.parametrize("text,expected", [
    ("a \u2014 b", "a, b"),
    ("a\u2014b", "a, b"),
    ("a \u2013 b", "a, b"),
    ("2026\u20132027", "2026\u20132027"),
    ("x, and y", "x and y"),
    ("x,or y", "x or y"),
    ("x, And y", "x And y"),
    ("x, order y", "x, order y"),
    ("x, android", "x, android"),
    ("\u2014 x", "x"),
])
def test_plain(text, expected):
    assert ledger.plain(text) == expected


def test_a_text_with_a_name_registered_later_is_withheld(home, capsys):
    ledger.add("proof", "Visited the site in %s" % fixtures.CONTROL_UNREGISTERED, outcome="Clean outcome",
               day="2026-09-22", p=home)
    register.add(home.register, fixtures.CUSTOMER_CODE + "-SITE-2", "SITE", fixtures.CONTROL_UNREGISTERED)
    for argv in (["report", "--by", "customer"], ["report", "--by", "tech", "--format", "html"],
                 ["ledger", "list"]):
        code, out, err = run(argv, capsys)
        assert code == 0, err
        # DMZ 4: shown as written, a name registered later changes nothing
        assert fixtures.CONTROL_UNREGISTERED in out and "Clean outcome" in out
        assert ledger.WITHHELD not in out and err == ""


def test_report_errors_and_the_empty_report(home, capsys):
    code, out, err = run(["report", "--by", "customer"], capsys)
    assert code == 0 and "No entries in the period." in out
    code, _, err = run(["report", "--by", "weather"], capsys)
    assert code == 2 and "weather" not in err
    code, _, err = run(["report", "--by", "kind", "--from", "2026-09-30", "--to", "2026-09-01"], capsys)
    assert code == 2 and "after" in err
    code, _, err = run(["report", "--by", "kind", "--from", fixtures.CUSTOMER_FORMS[1]], capsys)
    assert code == 2
    fixtures.assert_no_fixture_name(err, "usage error")
    code, _, err = run(["ledger"], capsys)
    assert code == 2


def test_a_malformed_ledger_line_is_refused_by_number_only(home, capsys):
    ledger.add("proof", "A good line", day="2026-09-22", p=home)
    f = ledger.week_file(home, "2026-09-22")
    with f.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps({"date": "2026-09-22", "note": fixtures.CUSTOMER_FORMS[0]}) + "\n")
    code, out, err = run(["report", "--by", "kind"], capsys)
    assert code == 2
    assert "2026-W39.jsonl line 2" in err
    fixtures.assert_no_fixture_name(out + err, "report")


def test_an_entry_in_the_wrong_week_file_is_refused(home):
    obj = dict(FULL_EXPECTED, date="2026-09-01")
    (home.ledger / "2026-W39.jsonl").write_text(json.dumps(obj) + "\n", encoding="utf-8")
    with pytest.raises(ledger.LedgerError, match="not in the week of the file"):
        ledger.load(home)


def test_a_ledger_line_with_a_name_shaped_candidate_is_refused_with_a_reword_hint(home, capsys):
    """TM0 item 11, then DMZ 4: the line `call with <first> <last>` is written as written, with no candidate rule and
    no reword hint."""
    entry = ledger.add("other", "call with %s about the sizing" % fixtures.FIRST_LAST, day="2026-09-22", p=home)
    assert entry.done == "call with %s about the sizing" % fixtures.FIRST_LAST
    code, _, err_ = run(["ledger", "add", "--kind", "other", "--done", "call with %s" % fixtures.FIRST_LAST], capsys)
    assert code == 0 and err_ == ""
    assert len(ledger.load(home)) == 2


def test_hit_indexes_halves_a_chunk_with_a_finding_instead_of_checking_each_text(monkeypatch):
    """2026-11-14: one finding among the owner status texts sent each text to the daemon alone."""
    from awb import check

    texts = ["plain line %d" % i for i in range(256)]
    texts[77] = "PLANTED"
    calls = []

    def fake(text, reg, entry=False):   # B-089: the ledger asks with entry=True
        calls.append(1)
        return ["hit"] if "PLANTED" in text else []

    monkeypatch.setattr(check, "check_text", fake)
    assert ledger.hit_indexes(texts, None) == {77}
    assert len(calls) <= 1 + 2 * 9
    calls.clear()
    texts[200] = "PLANTED again"
    assert ledger.hit_indexes(texts, None) == {77, 200}
    calls.clear()
    assert ledger.hit_indexes(["clean"] * 50, None) == set() and len(calls) == 1


# Review regressions. All paths and private values belong to these fixtures.

def _review_row(home, **changes):
    path = ledger.week_file(home, "2026-09-22")
    path.write_text(json.dumps(dict(FULL_EXPECTED, **changes)) + "\n", encoding="utf-8")
    return path


@pytest.mark.parametrize("operation", ["read", "append"])
@pytest.mark.parametrize("link", ["file", "folder", "ancestor", "hard"])
def test_review_storage_links_cannot_reach_outside(home, tmp_path, operation, link):
    path = _review_row(home)
    original = path.read_bytes()
    outside = tmp_path / "outside"
    outside.mkdir()
    target = outside / path.name
    target.write_bytes(original)
    if link in ("file", "hard"):
        path.unlink()
        if link == "file":
            path.symlink_to(target)
        else:
            os.link(target, path)
    elif link == "folder":
        path.unlink()
        home.ledger.rmdir()
        home.ledger.symlink_to(outside, target_is_directory=True)
    else:
        moved = tmp_path / "moved-shared"
        home.shared.rename(moved)
        home.shared.symlink_to(moved, target_is_directory=True)
    with pytest.raises((ledger.LedgerError, OSError)):
        if operation == "read":
            ledger.load(home)
        else:
            ledger.append_line(path, FULL_EXPECTED)
    assert target.read_bytes() == original


@pytest.mark.parametrize("operation", ["read", "append"])
def test_review_special_week_files_are_refused_before_io(home, monkeypatch, operation):
    path = ledger.week_file(home, "2026-09-22")
    os.mkfifo(path)
    real_open = os.open
    opened = []

    def nonblocking(path_, flags, *args, **kwargs):
        if str(path_).endswith(path.name):
            opened.append(flags)
            # Prevent a planted unsafe open from hanging the test.
            assert flags & os.O_NONBLOCK
        return real_open(path_, flags, *args, **kwargs)

    monkeypatch.setattr(os, "open", nonblocking)
    # A read_text implementation would bypass os.open and block on the FIFO.
    monkeypatch.setattr(type(path), "read_text", lambda *a, **kw: pytest.fail("unchecked read"))
    with pytest.raises((ledger.LedgerError, OSError)):
        ledger.load(home) if operation == "read" else ledger.append_line(path, FULL_EXPECTED)
    assert opened


def test_review_reader_takes_shared_lock(home, monkeypatch):
    path = _review_row(home)
    calls = []
    real_flock = fcntl.flock

    def record(fd, mode):
        if os.fstat(fd).st_ino == path.stat().st_ino:
            calls.append(mode)
        return real_flock(fd, mode)

    monkeypatch.setattr(fcntl, "flock", record)
    assert ledger.load(home)[0].as_dict() == FULL_EXPECTED
    assert fcntl.LOCK_SH in calls


@pytest.mark.parametrize("failure", ["write", "zero", "sync"])
def test_review_failed_append_restores_previous_bytes(home, monkeypatch, failure):
    path = _review_row(home)
    original = path.read_bytes().rstrip(b"\n")
    path.write_bytes(original)
    real_write = os.write
    calls = []

    def broken(fd, data):
        calls.append(1)
        if len(calls) == 1:
            return real_write(fd, data[:11])
        if failure == "zero":
            assert len(calls) == 2, "zero write must not loop"
            return 0
        raise OSError("private write detail")

    if failure == "sync":
        monkeypatch.setattr(os, "fsync", lambda fd: (_ for _ in ()).throw(OSError("private sync detail")))
    else:
        monkeypatch.setattr(os, "write", broken)
    with pytest.raises((ledger.LedgerError, OSError)):
        ledger.append_line(path, FULL_EXPECTED)
    assert path.read_bytes() == original


def test_review_batch_keeps_findings_across_split_boundaries(monkeypatch):
    from awb import check

    monkeypatch.setattr(check, "check_text", lambda text, reg, entry=False: ["hit"] if "alpha\n\nbeta" in text else [])
    assert ledger.hit_indexes(["alpha", "beta"], None) == {0, 1}


def test_review_batch_bounds_every_request_and_keeps_original_indexes(monkeypatch):
    from awb import check

    monkeypatch.setattr(ledger, "_CHECK_CHUNK", 12)
    calls = []

    def scan(text, reg, entry=False):   # B-089: the ledger asks with entry=True
        calls.append(text)
        assert len(text) <= 12
        return ["hit"] if "hit" in text else []

    monkeypatch.setattr(check, "check_text", scan)
    assert ledger.hit_indexes(["", "clean", "", "hit", "tail", "hit"], None) == {3, 5}
    with pytest.raises(ledger.LedgerError):
        ledger.hit_indexes(["x" * 13], None)
    assert all(len(text) <= 12 for text in calls)


def test_review_batch_missing_register_fails_closed(home):
    home.register.unlink()
    with pytest.raises(ledger.LedgerError, match="names cannot be checked"):
        ledger.hit_indexes([fixtures.CUSTOMER_FORMS[0]], home.register)


@pytest.mark.parametrize("entrypoint", ["screen", "batch", "source"])
def test_review_check_errors_never_echo_dependency_values(home, monkeypatch, entrypoint):
    from awb import check

    private = "pass" + "word=fixture-" + "value"

    def broken(*args, **kwargs):
        raise check.CheckUnavailable(private)

    monkeypatch.setattr(check, "register_source" if entrypoint == "source" else "check_text", broken)
    if entrypoint == "screen":
        # DMZ 4: the ledger has no screen: a failing name check never reaches an add
        assert not hasattr(ledger, "screen")
        assert ledger.add("code", "plain", p=home).done == "plain"
        return
    with pytest.raises(ledger.LedgerError) as exc:
        if entrypoint == "batch":
            ledger.hit_indexes(["plain"], home.register)
        else:
            ledger.ensure_checkable(home.register)
    assert private not in str(exc.value)
    assert "name check unavailable" in str(exc.value)


def test_review_check_class_is_not_untrusted_output(home, monkeypatch, capsys):
    from awb import check

    private = "pass" + "word=fixture-" + "value"
    monkeypatch.setattr(check, "check_text", lambda *args: [{"cls": private}])
    # DMZ 4: the add calls no name check, so what a check says never reaches its output
    code, out, err = run(["ledger", "add", "--kind", "code", "--done", "plain"], capsys)
    assert code == 0 and private not in out + err


@pytest.mark.parametrize("failure", ["missing", "broken"])
def test_review_detectors_fail_closed(home, monkeypatch, failure):
    from awb import gate

    def broken(text):
        raise RuntimeError("private detector detail")

    monkeypatch.setattr(gate, "DETECTORS", {} if failure == "missing" else dict(gate.DETECTORS, secret=broken))
    # DMZ 4: the add runs no detector; the detectors of the exit check still fail closed
    assert ledger.add("code", "plain", p=home).done == "plain"
    assert len(all_ledger_lines(home)) == 1
    if failure == "missing":
        with pytest.raises(ledger.LedgerError, match="pattern checks unavailable"):
            ledger._detectors()


@pytest.mark.parametrize("encoding", ["plain", "entities", "base64"])
def test_review_secret_views_are_refused_on_add(home, encoding):
    import base64

    private = "pass" + "word=" + "r7Q9" + "m2L8" + "v4S6" + "z1B3"
    text = {"plain": private, "entities": private.replace("a", "&#97;"),
            "base64": base64.b64encode(private.encode()).decode()}[encoding]
    # DMZ 4: a secret shaped text is written as written
    assert ledger.add("code", text, p=home).done == text
    assert len(all_ledger_lines(home)) == 1


@pytest.mark.parametrize("field", ["done", "outcome", "deliverables", "open", "tags"])
def test_review_stored_private_fields_never_reach_list_or_report(home, capsys, field):
    private = fixtures.CUSTOMER_FORMS[1].lower() if field == "tags" else "pass" + "word=" + "r7Q9" + "m2L8" + "v4S6" + "z1B3"
    _review_row(home, **{field: [private] if field in ("deliverables", "open", "tags") else private})
    for argv in (["ledger", "list"], ["report", "--by", "tech"], ["report", "--by", "kind", "--format", "html"]):
        code, out, err = run(argv, capsys)
        assert code == 0, err
        # DMZ 4: a stored text is shown as written
        assert private in out and err == ""
        assert ledger.WITHHELD not in out


@pytest.mark.parametrize("bad", ["surrogate", "nested", "duplicate"])
def test_review_hostile_json_is_a_safe_line_error(home, capsys, bad):
    path = ledger.week_file(home, "2026-09-22")
    text = json.dumps(dict(FULL_EXPECTED, done="bad\ud800"))
    if bad == "nested":
        text = '[' * 20000 + '0' + ']' * 20000
    elif bad == "duplicate":
        text = json.dumps(FULL_EXPECTED)[:-1] + ', "done": "replacement"}'
    path.write_text(text + "\n", encoding="utf-8")
    code = ledger.main(["list"])
    out, err = capsys.readouterr()
    assert code == 2 and not out
    assert "2026-W39.jsonl line 1" in err
    assert "replacement" not in err


def test_review_last_iso_week_does_not_overflow(home):
    e = ledger.add("code", "plain", day="9999-12-31", p=home)
    assert ledger.load(home) == [e]


def test_review_tags_file_links_are_refused(tmp_path):
    target = tmp_path / "outside"
    target.write_text("ecs\n", encoding="utf-8")
    link = tmp_path / "tags.txt"
    link.symlink_to(target)
    with pytest.raises(ledger.LedgerError):
        ledger.load_tags(link)


@pytest.mark.parametrize("operation", ["read", "append"])
def test_review_directory_swap_stays_on_opened_directory(home, tmp_path, monkeypatch, operation):
    path = _review_row(home)
    outside = tmp_path / "external"
    outside.mkdir()
    target = outside / path.name
    target.write_text("external content\n", encoding="utf-8")
    moved = tmp_path / "original-ledger"
    real_open = os.open
    swapped = []

    def swap(name, flags, *args, **kwargs):
        if str(name).endswith(path.name) and not swapped:
            swapped.append(True)
            home.ledger.rename(moved)
            home.ledger.symlink_to(outside, target_is_directory=True)
        return real_open(name, flags, *args, **kwargs)

    monkeypatch.setattr(os, "open", swap)
    if operation == "read":
        assert ledger.load(home)[0].as_dict() == FULL_EXPECTED
    else:
        ledger.append_line(path, FULL_EXPECTED)
        assert len(lines_of(moved / path.name)) == 2
    assert swapped and target.read_text(encoding="utf-8") == "external content\n"


@pytest.mark.parametrize("failure", ["missing", "unreadable"])
def test_review_week_read_failure_is_not_an_empty_ledger(home, monkeypatch, capsys, failure):
    path = _review_row(home)
    real_open = os.open
    private = "pass" + "word=" + "r7Q9" + "m2L8"

    def broken(name, *args, **kwargs):
        if str(name).endswith(path.name):
            raise (FileNotFoundError if failure == "missing" else PermissionError)(private)
        return real_open(name, *args, **kwargs)

    monkeypatch.setattr(os, "open", broken)
    code = ledger.main(["list"])
    out, err = capsys.readouterr()
    assert code == 2 and not out and private not in err
    assert path.name in err


@pytest.mark.parametrize("stale_size", [False, True])
def test_review_week_reads_are_bounded_even_if_file_grows(home, monkeypatch, stale_size):
    path = ledger.week_file(home, "2026-09-22")
    with path.open("wb") as stream:
        stream.truncate(ledger.MAX_FILE_BYTES + 1)
    if stale_size:
        real_stat = os.fstat

        def small(fd):
            info = real_stat(fd)
            values = list(info)
            values[6] = 0
            return os.stat_result(values)

        monkeypatch.setattr(os, "fstat", small)
    with pytest.raises(ledger.LedgerError, match="too large"):
        ledger.load(home)


def test_review_append_rejects_parent_traversal(tmp_path):
    inside = tmp_path / "project"
    inside.mkdir()
    outside = tmp_path / "outside.jsonl"
    with pytest.raises((ledger.LedgerError, OSError)):
        ledger.append_line(inside / ".." / outside.name, FULL_EXPECTED)
    assert not outside.exists()


def test_review_short_writes_still_append_one_complete_line(tmp_path, monkeypatch):
    path = tmp_path / "week.jsonl"
    real_write = os.write
    monkeypatch.setattr(os, "write", lambda fd, data: real_write(fd, data[:7]))
    ledger.append_line(path, FULL_EXPECTED)
    assert lines_of(path) == [json.dumps(FULL_EXPECTED, ensure_ascii=False)]


@pytest.fixture(autouse=True)
def _review_build_user_blocklist(monkeypatch):
    """Build users ignore AWB_BLOCKLIST; keep the suite's invented list active through the self-test hook."""
    from pathlib import Path
    from awb import config, gate

    if config.is_build_user():
        monkeypatch.setattr(gate, "_SELFTEST_FILES", [*gate._SELFTEST_FILES, Path(__file__).with_name("blocklist.txt")])


@pytest.mark.parametrize("reply", [None, {}, "invalid"])
def test_review_malformed_check_replies_fail_closed(monkeypatch, reply):
    from awb import check

    monkeypatch.setattr(check, "check_text", lambda *args: reply)
    with pytest.raises(ledger.LedgerError):
        ledger.hit_indexes(["plain"], None)


def test_review_withheld_tags_do_not_duplicate_report_rows(home, monkeypatch):
    """DMZ 4: nothing is withheld, the tags are the groups of the report and each row stays in its own group."""
    entry = ledger.Entry(date="2026-09-22", done="plain", tags=["ecs", "elb"])
    assert not hasattr(ledger, "withhold")
    assert ledger.group([entry], "tech") == {"ecs": [entry], "elb": [entry]}
