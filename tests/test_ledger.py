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
    args = ["ledger", "add", "--kind", "proof", "--done", "Sized the landing zone", "--date", "2026-09-22"]
    value = "Workshop with %s about the landing zone" % form
    if option == "--done":
        args[args.index("--done") + 1] = value
    else:
        args += [option, value]
    code, out, err = run(args, capsys)
    assert code == 1
    assert "%s refused" % what in err and "name 1" in err
    fixtures.assert_no_fixture_name(out + err, "ledger output")
    assert all_ledger_lines(home) == []


@pytest.mark.parametrize("text,cls", [
    ("mailed the offer to ops@" + "nrgtz-beratung.de", "mail"),
    ("used the list of CLI" + "ENT3 from the notes", "blocklist"),
])
def test_structured_data_and_blocked_words_are_refused(home, capsys, text, cls):
    code, out, err = run(["ledger", "add", "--kind", "other", "--done", "Sent the offer", "--outcome", text],
                         capsys)
    assert code == 1
    assert "outcome refused" in err and cls in err
    assert text not in err and "CUSTOMER" not in err
    assert all_ledger_lines(home) == []


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
    assert code == 2
    assert "name check unavailable" in err
    assert all_ledger_lines(home) == []


def test_a_vault_folder_without_a_register_refuses(home, capsys):
    home.register.unlink()
    code, out, err = run(["ledger", "add", "--kind", "proof", "--done", "Sized the landing zone"], capsys)
    assert code == 2 and "names cannot be checked" in err
    assert all_ledger_lines(home) == []


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
        assert fixtures.CONTROL_UNREGISTERED not in out + err
        assert ledger.WITHHELD in out and "Clean outcome" in out
        assert "1 texts withheld" in err


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
    """TM0 item 11: the planted line `call with <first> <last>` is refused with the class and the reword hint; the
    same words of the allowed terms pass."""
    from awb import intake

    with pytest.raises(ledger.Refused) as err:
        ledger.add("other", "call with %s about the sizing" % fixtures.FIRST_LAST, day="2026-09-22", p=home)
    text = str(err.value)
    assert "candidate-person" in text and "reword it" in text and "rules/allowed-terms.txt" in text
    assert fixtures.FIRST_LAST not in text and not ledger.load(home)
    code, _, err_ = run(["ledger", "add", "--kind", "other", "--done", "call with %s" % fixtures.FIRST_LAST], capsys)
    assert code != 0 and "reword it" in err_ and fixtures.FIRST_LAST not in err_
    ledger.add("other", "Sizing of the two clusters", day="2026-09-22", p=home)
    allowed = intake._SHORT_ALLOWED | set(fixtures.FIRST_LAST.casefold().split())
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(intake, "_SHORT_ALLOWED", allowed)
        ledger.add("other", "call with %s about the sizing" % fixtures.FIRST_LAST, day="2026-09-22", p=home)
    assert len(ledger.load(home)) == 2
