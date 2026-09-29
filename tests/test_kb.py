"""Tests of awb/kb.py: the knowledge base in a temporary folder (AWB_KB) with the fixture register."""
from __future__ import annotations

import io
import json
import re
import subprocess
import sys
import threading
from dataclasses import replace
from datetime import date, timedelta
from pathlib import Path

import pytest

from awb import check, cli, kb, register
from tests import fixtures

TODAY = date.today()
ID_RE = re.compile(r"\bKB-[A-Z2-7]{4}\b")

S_FLAVOR = "ECS flavor s3.large.2 is offered in eu-de"
S_FLAVOR_NEAR = "The ECS flavor s3.large.2 is offered in region eu-de"
S_GPU = "GPU flavor p2 needs a quota increase in eu-nl"
S_NEGATIVE = "Windows images cannot be imported as BMS images in eu-de"
TRIED = ("image import API call in eu-de", "console import wizard in eu-de")

# planted values, built from pieces so that this file passes the commit gate itself
HEX_ID = "4f1c9a7e2b6d" + "40f8a3c5e9b1d7f2a6c0"
HOME_DIR = "/ho" + "me/builder/logs"
GH_TOKEN = "gh" + "p_" + "Zx8Kq3Lw7Rt2Vn5Bm9Yc4Hd6Jf1Gs0Pa3Ue7Wi"
SECRET_VALUE = "t7Kq2Zp9" + "Lw4Rx8Vn3Bm6Yc"
SECRET_LINE = "db_pass" + "word = " + SECRET_VALUE


def run(argv, capsys, monkeypatch=None, stdin: str | None = None):
    if stdin is not None:
        monkeypatch.setattr(sys, "stdin", io.StringIO(stdin))
    code = cli.main(["kb", *argv])
    out, err = capsys.readouterr()
    return code, out, err


def add_argv(statement: str, *, tag="ecs", grade="docs", cls="api", source="docs mirror, user guide",
             tried=(), scope="tcp", extra=()) -> list[str]:
    argv = ["add", "--scope", scope, "--tag", tag, "--grade", grade, "--class", cls, "--source", source]
    for t in tried:
        argv += ["--tried", t]
    return argv + list(extra) + [statement]


def added_id(out: str) -> str:
    m = re.search(r"^added (KB-[A-Z2-7]{4}) ", out, re.M)
    assert m, "no id in the output of kb add"
    return m.group(1)


def entry_files(home) -> list[Path]:
    folder = home.kb / "entries"
    return sorted(folder.glob("KB-*.md")) if folder.is_dir() else []


def new(statement: str, **kw) -> kb.Entry:
    args = dict(scope="tcp", tags=["ecs"], grade="docs", cls="api", source="docs mirror, user guide")
    args.update(kw)
    return kb.add(statement, **args)


def section(text: str, start: str, end: str | None) -> str:
    a = text.index(start)
    b = text.index(end, a) if end else len(text)
    return text[a:b]


# --------------------------------------------------------------------------- add, find, show


def test_add_an_entry_and_find_it(home, capsys):
    code, out, err = run(add_argv(S_FLAVOR, tag="ECS,api"), capsys)
    assert code == 0 and err == ""
    kb_id = added_id(out)
    path = home.kb / "entries" / (kb_id + ".md")
    assert path.is_file()
    assert "tags: ecs, api\n" in path.read_text(encoding="utf-8")

    code, out, err = run(["find", "flavor", "s3.large.2"], capsys)
    assert code == 0 and err == ""
    line = [x for x in out.splitlines() if kb_id in x]
    assert len(line) == 1 and "docs" in line[0] and "checked %s" % TODAY.isoformat() in line[0]
    assert S_FLAVOR in line[0] and "EXPIRED" not in line[0]

    found = kb.find("s3.large.2 offered", home)
    assert [e.id for _, e in found] == [kb_id] and found[0][0] == 1.0
    assert kb.find("nothing matches here", home) == []


def test_show_prints_the_entry(home, capsys):
    e = new(S_FLAVOR)
    code, out, err = run(["show", e.id], capsys)
    assert code == 0 and err == ""
    assert out == e.path.read_text(encoding="utf-8")
    code, out, err = run(["show", "KB-ZZZZ"], capsys)
    assert code == 2 and "there is no entry KB-ZZZZ" in err
    code, out, err = run(["show", fixtures.CUSTOMER_FORMS[0]], capsys)
    assert code == 2 and "not a knowledge base id" in err
    fixtures.assert_no_fixture_name(out + err, "kb show")


def test_statement_from_standard_input(home, capsys, monkeypatch):
    code, out, _ = run(add_argv("-"), capsys, monkeypatch, stdin="ECS images can be shared with other projects\n")
    assert code == 0
    assert kb.get(added_id(out), home).statement == "ECS images can be shared with other projects"


@pytest.mark.parametrize("cls, days", [("availability", 30), ("api", 180), ("stable", 365)])
def test_header_keys_and_the_computed_expiry_per_class(home, cls, days):
    statement = "The flavor list of a project comes from one API call"
    e = new(statement, tags=["ecs", "api"], cls=cls, checked="2026-09-01", today=date(2026, 9, 22))
    text = e.path.read_text(encoding="utf-8")
    head, sep, body = text.partition("\n---\n")
    assert sep and body == statement + "\n"
    lines = head.splitlines()
    assert [x.split(":", 1)[0] for x in lines] == ["id", "scope", "tags", "grade", "checked", "class", "expires",
                                                    "source"]
    want = (date(2026, 9, 1) + timedelta(days=days)).isoformat()
    assert lines == ["id: %s" % e.id, "scope: tcp", "tags: ecs, api", "grade: docs", "checked: 2026-09-01",
                     "class: %s" % cls, "expires: %s" % want, "source: docs mirror, user guide"]
    assert e.expires == want == kb.expires_for("2026-09-01", cls)
    assert kb.load(home) == [e]


def test_the_checked_date_defaults_to_today_and_may_not_lie_ahead(home, capsys):
    e = new(S_FLAVOR, cls="stable")
    assert e.checked == TODAY.isoformat()
    assert e.expires == (TODAY + timedelta(days=365)).isoformat()
    ahead = (TODAY + timedelta(days=2)).isoformat()
    code, _, err = run(add_argv(S_GPU, tag="gpu", extra=["--checked", ahead]), capsys)
    assert code == 2 and "future" in err
    assert len(entry_files(home)) == 1


def test_index_is_generated_after_every_add(home, capsys):
    first = new(S_FLAVOR)
    index = home.kb / "INDEX.md"
    text = index.read_text(encoding="utf-8")
    assert first.id in text and "1 entry," in text and "Do not edit" in text
    second = new(S_GPU, tags=["gpu"], grade="live", cls="availability")
    text = index.read_text(encoding="utf-8")
    assert first.id in text and second.id in text and "2 entries," in text
    row = [x for x in text.splitlines() if x.startswith("| [%s]" % second.id)]
    assert row and "| tcp | gpu | live | %s | %s |" % (second.checked, second.expires) in row[0]
    index.unlink()
    code, out, _ = run(["index"], capsys)
    assert code == 0 and index.is_file() and first.id in index.read_text(encoding="utf-8")


def test_writing_the_index_alone_reads_and_writes_under_one_lock(home):
    """A retire that lands while `awb kb index` runs must not come back as a live row of the index."""
    gone = new(S_FLAVOR)
    kept = new(S_GPU, tags=["gpu"])
    holding, release, done = threading.Event(), threading.Event(), threading.Event()

    def other_writer():
        # what `retire` does under the same lock, without asking for the lock a second time
        with kb._locked(home.kb):
            holding.set()
            release.wait(5)
            withdrawn = replace(gone, retired=TODAY.isoformat(), why="the fact was wrong and is replaced")
            gone.path.write_text(kb.render(withdrawn), encoding="utf-8")

    def index_writer():
        kb.write_index(home.kb)
        done.set()

    writer = threading.Thread(target=other_writer)
    writer.start()
    assert holding.wait(5)
    indexer = threading.Thread(target=index_writer)
    indexer.start()
    assert not done.wait(0.5), "INDEX.md was written while another writer held the lock"
    release.set()
    writer.join(5)
    indexer.join(5)
    assert done.is_set()

    index = (home.kb / "INDEX.md").read_text(encoding="utf-8")
    assert "| [%s]" % gone.id not in index and "| [%s]" % kept.id in index
    assert "1 entry retired and not listed" in index


# --------------------------------------------------------------------------- refusals


REFUSALS = {
    "cyrillic": (dict(statement="Сервер ECS доступен в регионе eu-de"), "not in English (Cyrillic letters)",
                 "Сервер"),
    "german": (dict(statement="leider ist die Funktion in der Region nicht verfügbar und muss beantragt werden"),
               "German function words", "Funktion"),
    "fixture form": (dict(statement="ECS in eu-de is used by %s" % fixtures.CUSTOMER_FORMS[1]),
                     "the statement carries hits of the name check (name 1)", fixtures.CUSTOMER_FORMS[1]),
    "fixture form in the source": (dict(statement=S_FLAVOR, source="a call with %s" % fixtures.PERSON_FORMS[0]),
                                   "the source carries hits of the name check", fixtures.PERSON_FORMS[0]),
    "structured data": (dict(statement="ECS in eu-de answers on 203.0.113.17 only"),
                        "hits of the name check (ip 1)", "203.0.113.17"),
    "cust code": (dict(statement="ECS in eu-de is used by %s" % fixtures.CUSTOMER_CODE),
                  "1 register or project code;", fixtures.CUSTOMER_CODE),
    "tcp project code": (dict(statement="The runbook of tcp-q7m4 uses ECS in eu-de"),
                         "1 register or project code;", "tcp-q7m4"),
    "32-hex id": (dict(statement="The ECS image id is %s in eu-de" % HEX_ID), "(identifier 1)", HEX_ID),
    "home path": (dict(statement="The agent log lies under %s on the ECS" % HOME_DIR), "(homepath 1)", "builder"),
    "token": (dict(statement="The pipeline pushes with %s to the registry" % GH_TOKEN), "(token 1)", GH_TOKEN),
    "secret": (dict(statement="%s is set on the RDS instance" % SECRET_LINE), "(secret 1)", SECRET_VALUE),
    "price per hour": (dict(statement="The s3.large.2 flavor costs 0.05 EUR per hour in eu-de"),
                       "carries a price", "0.05"),
    "price in euro": (dict(statement="A small ECS in eu-de comes to € 37.40 a month"), "carries a price", "37.40"),
    "price without currency": (dict(statement="The flavor is billed at 0,12 per hour"), "carries a price", "0,12"),
    "negative without tried": (dict(statement=S_NEGATIVE, tag="bms"), "needs 2 different --tried", S_NEGATIVE),
    "negative with one tried": (dict(statement=S_NEGATIVE, tag="bms", tried=TRIED[:1]), "1 given", S_NEGATIVE),
    "negative with the same tried twice": (dict(statement=S_NEGATIVE, tag="bms", tried=(TRIED[0], TRIED[0])),
                                           "1 given", S_NEGATIVE),
    "tried with a fixture form": (dict(statement=S_NEGATIVE, tag="bms",
                                       tried=("%s confirmed it" % fixtures.PERSON_FORMS[1], TRIED[1])),
                                  "tried 1 carries hits of the name check", fixtures.PERSON_FORMS[1]),
    "unknown tag": (dict(statement=S_FLAVOR, tag="ecs,quxbazz"), "tag 2 is not in rules/tags.txt", "quxbazz"),
}


@pytest.mark.parametrize("case", sorted(REFUSALS))
def test_refusals_carry_no_value(home, capsys, case):
    kw, marker, planted = REFUSALS[case]
    kw = dict(kw)
    statement = kw.pop("statement")
    code, out, err = run(add_argv(statement, **kw), capsys)
    assert code == 1
    assert marker in err
    assert "refused" in err
    assert planted not in out + err
    assert statement not in out + err
    fixtures.assert_no_fixture_name(out + err, "kb add refusal")
    assert entry_files(home) == [] and not (home.kb / "INDEX.md").exists()


def test_refused_raises_with_reasons_and_writes_nothing(home):
    with pytest.raises(kb.Refused) as info:
        new("ECS for %s costs 0.05 EUR per hour" % fixtures.CUSTOMER_FORMS[0])
    reasons = info.value.reasons
    assert any("name check" in r for r in reasons) and any("price" in r for r in reasons)
    fixtures.assert_no_fixture_name(str(info.value), "Refused")
    assert not (home.kb / "entries").exists()


def test_usage_errors_do_not_echo(home, capsys):
    form = fixtures.CUSTOMER_FORMS[0]
    for argv in (add_argv(S_FLAVOR, grade=form), add_argv(S_FLAVOR, cls=form),
                 add_argv(S_FLAVOR, scope=form), add_argv(S_FLAVOR, tried=("one; two", "three"))):
        code, out, err = run(argv, capsys)
        assert code == 2 and err
        fixtures.assert_no_fixture_name(out + err, "kb usage error")
    assert entry_files(home) == []


def test_english_with_one_german_looking_word_passes(home):
    e = new("The MIT licensed provider module creates ECS in eu-de")
    assert e.path.is_file()


def test_throughput_without_per_hour_is_not_a_price(home):
    assert new("CBR backups of one ECS move about 30 GB/h to the vault", tags=["cbr"]).path.is_file()


# --------------------------------------------------------------------------- negatives and duplicates


def test_a_negative_with_two_tried_is_accepted_and_shown_in_scope(home, capsys):
    code, out, err = run(add_argv(S_NEGATIVE, tag="bms,windows", grade="live", tried=TRIED), capsys)
    assert code == 0 and err == ""
    kb_id = added_id(out)
    e = kb.get(kb_id, home)
    assert e.negative and e.tried == TRIED
    assert e.path.read_text(encoding="utf-8").split("\n---\n")[0].splitlines()[-1] == "tried: %s; %s" % TRIED

    code, out, err = run(["scope", "bms"], capsys)
    assert code == 0 and err == ""
    negatives = section(out, "## Negatives and what was tried", "## Expired entries")
    assert kb_id in negatives and S_NEGATIVE in negatives and "grade live" in negatives
    assert "  - tried: %s" % TRIED[0] in negatives and "  - tried: %s" % TRIED[1] in negatives
    assert kb_id not in section(out, "## Facts by grade", "## Negatives")


def test_a_near_duplicate_is_refused_and_listed(home, capsys):
    first = new(S_FLAVOR)
    code, out, err = run(add_argv(S_FLAVOR_NEAR), capsys)
    assert code == 1
    assert "too similar to 1 existing entry" in err and "--force-new" in err
    assert first.id in out and "most similar entries" in out
    assert len(entry_files(home)) == 1

    code, out, _ = run(add_argv(S_FLAVOR_NEAR, extra=["--force-new"]), capsys)
    assert code == 0 and first.id in out
    assert len(entry_files(home)) == 2
    # another scope is another fact
    assert new(S_FLAVOR_NEAR, scope="hcs").path.is_file()


def test_similar_shows_at_most_three_best_first(home):
    ids = [new(s).id for s in ("ECS flavor s3.large.2 runs in eu-de", "ECS flavor c6.large.2 runs in eu-nl",
                               "ECS flavor m6.xlarge.8 is sold out in eu-ch2", "EVS disks of type ssd are fast")]
    near = kb.similar("ECS flavor s3.large.2 runs in region eu-de", kb.load(home))
    assert len(near) == 3 and near[0][1].id == ids[0]
    assert [s for s, _ in near] == sorted((s for s, _ in near), reverse=True)
    assert ids[3] not in [e.id for _, e in near]


# --------------------------------------------------------------------------- amend and retire


WIDER = "ECS flavor s3.large.2 is offered in eu-de and in eu-nl"
REASON = "the region was wrong and the fact is replaced"


def test_amend_rewrites_the_statement_and_keeps_the_id(home, capsys):
    e = new(S_FLAVOR, tags=["ecs", "api"], grade="docs")
    code, out, err = run(["amend", e.id, WIDER], capsys)
    assert code == 0 and err == ""
    assert e.id in out and "amended" in out
    after = kb.get(e.id, home)
    assert after.statement == WIDER
    assert after.tags == e.tags and after.grade == e.grade and after.cls == e.cls and after.source == e.source
    assert after.scope == e.scope and after.tried == e.tried and not after.is_retired
    assert entry_files(home) == [e.path]
    assert WIDER in (home.kb / "INDEX.md").read_text(encoding="utf-8")


def test_amend_keeps_the_tried_texts_and_weighs_a_negative_against_them(home, capsys):
    e = new(S_NEGATIVE, tags=["bms"], grade="live", tried=TRIED)
    wider = "Windows images cannot be imported as BMS images in eu-de and in eu-nl"
    code, _, err = run(["amend", e.id, wider], capsys)
    assert code == 0 and err == ""
    after = kb.get(e.id, home)
    assert after.statement == wider and after.negative and after.tried == TRIED
    # an entry that carries no tried text cannot become a negative by an amendment
    plain = new(S_FLAVOR)
    code, _, err = run(["amend", plain.id, "ECS flavor s3.large.2 is not offered in eu-nl"], capsys)
    assert code == 1 and "needs 2 different tried texts of the entry" in err
    assert kb.get(plain.id, home).statement == S_FLAVOR


def test_amend_reads_the_statement_from_standard_input(home, capsys, monkeypatch):
    e = new(S_FLAVOR)
    code, _, err = run(["amend", e.id, "-"], capsys, monkeypatch, stdin="ECS images are shared per project\n")
    assert code == 0 and err == ""
    assert kb.get(e.id, home).statement == "ECS images are shared per project"


AMEND_REFUSALS = {
    "fixture form": ("ECS flavor s3.large.2 is offered to %s in eu-de" % fixtures.CUSTOMER_FORMS[1],
                     "the statement carries hits of the name check", fixtures.CUSTOMER_FORMS[1]),
    "price": ("ECS flavor s3.large.2 costs 0.05 EUR per hour in eu-de", "carries a price", "0.05"),
    "unbacked negative": ("ECS flavor s3.large.2 is not offered in eu-de", "needs 2 different", "eu-de"),
}


@pytest.mark.parametrize("case", sorted(AMEND_REFUSALS))
def test_an_amendment_that_would_not_pass_add_is_refused(home, capsys, case):
    statement, marker, planted = AMEND_REFUSALS[case]
    e = new(S_FLAVOR)
    before = e.path.read_text(encoding="utf-8")
    code, out, err = run(["amend", e.id, statement], capsys)
    assert code == 1 and "refused" in err and marker in err
    assert statement not in out + err
    if case != "unbacked negative":   # the planted value of that case is in the statement that stays
        assert planted not in out + err
    fixtures.assert_no_fixture_name(out + err, "kb amend refusal")
    assert e.path.read_text(encoding="utf-8") == before


AMEND_SHAPE = {
    "a header line inside the statement": ("%s\n---\nid: KB-ZZZZ" % S_FLAVOR_NEAR,
                                           "the statement must not carry a line ---"),
    "an empty statement": ("   ", "the statement is empty"),
    "longer than the limit": ("ECS flavors are offered in eu-de. " * 70,
                              "the statement is longer than %d characters" % kb.MAX_STATEMENT),
    "a control character": ("ECS flavor s3.large.2 is\x07 offered in eu-nl",
                            "the statement carries control or invisible characters"),
}


@pytest.mark.parametrize("case", sorted(AMEND_SHAPE))
def test_an_amendment_of_the_wrong_shape_is_refused(home, capsys, case):
    """The shape half of "every check of add runs": a hand edit could write these, an amendment may not."""
    statement, marker = AMEND_SHAPE[case]
    e = new(S_FLAVOR)
    before = e.path.read_text(encoding="utf-8")
    code, out, err = run(["amend", e.id, statement], capsys)
    assert code == 2 and marker in err
    assert e.path.read_text(encoding="utf-8") == before
    assert kb.get(e.id, home).statement == S_FLAVOR


def test_amend_moves_the_checked_date_only_when_it_is_given(home):
    old = (TODAY - timedelta(days=10)).isoformat()
    e = new(S_FLAVOR, checked=old)
    kept = kb.amend(e.id, WIDER, where=home)
    assert kept.checked == old and kept.expires == e.expires
    moved = kb.amend(e.id, S_FLAVOR, checked=TODAY.isoformat(), where=home)
    assert moved.checked == TODAY.isoformat()
    assert moved.expires == (TODAY + timedelta(days=180)).isoformat()
    on_disk = kb.get(e.id, home)
    assert on_disk.checked == moved.checked and on_disk.expires == moved.expires


@pytest.mark.parametrize("value, marker", [
    ("", "checked is empty"),
    ("   ", "checked is empty"),
    ("not-a-date", "checked must be an ISO date"),
    ((TODAY + timedelta(days=2)).isoformat(), "checked must not be in the future"),
])
def test_amend_refuses_a_checked_date_that_is_empty_wrong_or_ahead(home, capsys, value, marker):
    """A shell variable that did not expand gives --checked "", which must not re-date the fact to today."""
    old = (TODAY - timedelta(days=170)).isoformat()
    e = new(S_FLAVOR, checked=old)
    before = e.path.read_text(encoding="utf-8")
    code, out, err = run(["amend", "--checked", value, e.id, WIDER], capsys)
    assert code == 2 and marker in err
    assert e.path.read_text(encoding="utf-8") == before
    after = kb.get(e.id, home)
    assert after.checked == old and after.expires == e.expires and after.statement == S_FLAVOR
    with pytest.raises(kb.KBError, match=marker):
        kb.amend(e.id, WIDER, checked=value, where=home)
    assert e.path.read_text(encoding="utf-8") == before


def test_an_amendment_is_not_a_duplicate_of_itself_but_of_another_entry(home, capsys):
    e = new(S_FLAVOR)
    code, _, err = run(["amend", e.id, S_FLAVOR_NEAR], capsys)
    assert code == 0 and err == ""
    assert kb.get(e.id, home).statement == S_FLAVOR_NEAR
    other = new(S_GPU, tags=["gpu"])
    code, out, err = run(["amend", other.id, S_FLAVOR], capsys)
    assert code == 1 and "too similar to 1 existing entry" in err and e.id in out
    assert kb.get(other.id, home).statement == S_GPU
    code, _, err = run(["amend", other.id, S_FLAVOR, "--force-new"], capsys)
    assert code == 0 and err == ""


def test_amend_and_retire_of_an_unknown_id(home, capsys):
    new(S_FLAVOR)
    code, _, err = run(["amend", "KB-ZZZZ", WIDER], capsys)
    assert code == 2 and "there is no entry KB-ZZZZ" in err
    code, _, err = run(["retire", "--why", REASON, "KB-ZZZZ"], capsys)
    assert code == 2 and "there is no entry KB-ZZZZ" in err


def test_a_retired_entry_leaves_find_scope_and_expired_and_show_says_why(home, capsys):
    old = (TODAY - timedelta(days=45)).isoformat()
    gone = new(S_FLAVOR, cls="availability", checked=old)
    kept = new(S_GPU, tags=["gpu", "ecs"], cls="availability", checked=old)
    code, out, err = run(["retire", "--why", REASON, gone.id], capsys)
    assert code == 0 and err == "" and gone.id in out

    e = kb.get(gone.id, home)
    assert e.is_retired and e.retired == TODAY.isoformat() and e.why == REASON
    assert e.statement == gone.statement and e.checked == gone.checked

    code, out, _ = run(["find", "flavor"], capsys)
    assert code == 0 and gone.id not in out and kept.id in out
    code, out, _ = run(["scope", "ecs"], capsys)
    assert code == 0 and gone.id not in out and kept.id in out
    code, out, _ = run(["expired"], capsys)
    assert code == 1 and gone.id not in out and kept.id in out
    assert [x.id for x in kb.expired(home)] == [kept.id]
    assert [x.id for _, x in kb.find("flavor", home)] == [kept.id]

    index = (home.kb / "INDEX.md").read_text(encoding="utf-8")
    assert gone.id not in index and kept.id in index and "1 entry retired and not listed" in index

    code, out, err = run(["show", gone.id], capsys)
    assert code == 0 and err == ""
    assert gone.statement in out and "RETIRED on %s: %s" % (TODAY.isoformat(), REASON) in out
    # the entry is past its expiry too, and a withdrawn fact must not also be offered for a fresh check
    assert "EXPIRED" not in out


def test_a_retired_entry_does_not_block_the_corrected_fact(home, capsys):
    wrong = new(S_FLAVOR)
    code, _, err = run(["retire", "--why", REASON, wrong.id], capsys)
    assert code == 0 and err == ""
    code, out, err = run(add_argv(S_FLAVOR_NEAR), capsys)
    assert code == 0 and err == ""
    fresh = added_id(out)
    assert fresh != wrong.id and wrong.id not in out
    assert len(entry_files(home)) == 2

    # the list printed after an amendment leaves out the same two: the retired entry and the amended entry itself
    code, out, err = run(["amend", fresh, WIDER], capsys)
    assert code == 0 and err == ""
    assert "most similar entries" not in out and wrong.id not in out


def test_retiring_twice_is_refused_and_a_retired_entry_cannot_be_amended(home, capsys):
    e = new(S_FLAVOR)
    assert run(["retire", "--why", REASON, e.id], capsys)[0] == 0
    before = e.path.read_text(encoding="utf-8")
    code, _, err = run(["retire", "--why", "it is wrong again", e.id], capsys)
    assert code == 1 and "refused" in err and "retired already" in err
    code, _, err = run(["amend", e.id, WIDER], capsys)
    assert code == 1 and "refused" in err and "is retired" in err
    assert e.path.read_text(encoding="utf-8") == before


def test_a_reason_that_carries_a_name_or_is_empty_is_refused(home, capsys):
    e = new(S_FLAVOR)
    before = e.path.read_text(encoding="utf-8")
    code, out, err = run(["retire", "--why", "%s says so" % fixtures.PERSON_FORMS[0], e.id], capsys)
    assert code == 1 and "the reason carries hits of the name check" in err
    fixtures.assert_no_fixture_name(out + err, "kb retire refusal")
    code, _, err = run(["retire", "--why", "", e.id], capsys)
    assert code == 2 and "the reason is empty" in err
    code, _, err = run(["retire", "--why", "two\nlines", e.id], capsys)
    assert code == 2 and "one line" in err
    assert e.path.read_text(encoding="utf-8") == before
    assert not kb.get(e.id, home).is_retired


def test_a_reason_that_carries_a_name_registered_later_withholds_the_entry(home, capsys):
    """The reason is read text like the statement, so it goes through the name check of every later read too."""
    word = fixtures.CONTROL_UNREGISTERED
    e = new(S_FLAVOR)
    code, _, err = run(["retire", "--why", "the %s site was rebuilt" % word, e.id], capsys)
    assert code == 0 and err == ""
    entries = register.load(home.register)
    register.add(home.register, register.next_code(entries, "ORG"), "ORG", word)

    code, out, err = run(["show", e.id], capsys)
    assert code == 1 and word not in out + err
    assert "carries hits of the name check" in err


def test_an_entry_written_before_retire_existed_still_parses(home):
    folder = home.kb / "entries"
    folder.mkdir(parents=True)
    (folder / "KB-AAAA.md").write_text(
        "id: KB-AAAA\nscope: tcp\ntags: ecs\ngrade: docs\nchecked: 2026-09-01\nclass: api\nexpires: 2027-02-28\n"
        "source: docs mirror, user guide\n---\nECS images are shared per project.\n", encoding="utf-8")
    e = kb.get("KB-AAAA", home)
    assert not e.is_retired and e.retired == "" and e.why == ""
    text = kb.render(e)
    assert "retired:" not in text and "why:" not in text
    assert [x.id for x in kb.load(home)] == ["KB-AAAA"]
    assert [x.id for _, x in kb.find("images", home)] == ["KB-AAAA"]


@pytest.mark.parametrize("head, marker", [
    ("retired: 2026-09-23", "line 9: retired needs the key why"),
    ("retired: 2026-09-23\nwhy:", "line 10: why is empty"),
    ("why: the fact was wrong", "line 9: why needs the key retired"),
    ("retired: 23.09.2026\nwhy: the fact was wrong", "line 9: retired is not an ISO date"),
])
def test_half_a_withdrawal_in_an_entry_file_is_refused(head, marker):
    text = ("id: KB-AAAA\nscope: tcp\ntags: ecs\ngrade: docs\nchecked: 2026-09-01\nclass: api\n"
            "expires: 2027-02-28\nsource: docs mirror\n%s\n---\nA statement.\n" % head)
    with pytest.raises(kb.KBError) as info:
        kb.parse(text, "KB-AAAA")
    assert marker in str(info.value)


# --------------------------------------------------------------------------- find, expired, scope


def test_find_ranks_the_better_match_first(home, capsys):
    flavor = new(S_FLAVOR)
    gpu = new(S_GPU, tags=["gpu"])
    code, out, _ = run(["find", "gpu", "flavor", "quota"], capsys)
    assert code == 0
    assert out.index(gpu.id) < out.index(flavor.id)
    code, out, _ = run(["find", "s3.large.2", "offered"], capsys)
    assert flavor.id in out and gpu.id not in out
    ranked = kb.find(["gpu", "flavor", "quota"], home)
    assert [e.id for _, e in ranked] == [gpu.id, flavor.id] and ranked[0][0] > ranked[1][0]
    # a tag counts as a word of its entry
    assert [e.id for _, e in kb.find("gpu", home)] == [gpu.id]


@pytest.mark.parametrize("age, expired", [(40, True), (31, True), (30, False), (0, False)])
def test_kb_expired_lists_an_entry_whose_check_is_old(home, capsys, age, expired):
    checked = (TODAY - timedelta(days=age)).isoformat()
    code, out, _ = run(add_argv(S_GPU, tag="gpu", cls="availability", extra=["--checked", checked]), capsys)
    assert code == 0
    old = added_id(out)
    fresh = new(S_FLAVOR, cls="stable")
    code, out, err = run(["expired"], capsys)
    assert err == ""
    assert (old in out) is expired and fresh.id not in out
    assert code == (1 if expired else 0)
    assert [e.id for e in kb.expired(home)] == ([old] if expired else [])
    assert [e.id for e in kb.expired(home.kb)] == ([old] if expired else [])
    _, out, _ = run(["find", "flavor"], capsys)
    line = [x for x in out.splitlines() if old in x][0]
    assert ("EXPIRED" in line) is expired


def test_kb_scope_groups_by_grade(home, capsys):
    live = new("The default ECS quota of a project is 100 instances", grade="live")
    docs = new("EVS disks can be resized while the ECS keeps running", tags=["evs", "ecs"], grade="docs")
    said = new("CBR vault backups of ECS run once a day by default", tags=["cbr", "ecs"], grade="said")
    old = new("ECS console sessions time out after 30 minutes", grade="docs", cls="availability",
              checked=(TODAY - timedelta(days=45)).isoformat())
    other = new(S_GPU, tags=["gpu"], grade="live")
    code, out, err = run(["scope", "ecs"], capsys)
    assert code == 0 and err == ""
    assert out.startswith("# Knowledge briefing: ecs\n")
    assert "4 entries tagged ecs" in out and "3 facts, 0 negatives, 1 expired" in out
    assert section(out, "### live", "### docs").count("KB-") == 1 and live.id in section(out, "### live", "### docs")
    assert docs.id in section(out, "### docs", "### said") and old.id not in section(out, "### docs", "### said")
    assert said.id in section(out, "### said", "## Negatives")
    assert "### contract" not in out and "### assumed" not in out
    assert old.id in section(out, "## Expired entries", None) and "expired %s" % old.expires in out
    assert other.id not in out
    assert "  source: docs mirror, user guide" in out
    assert kb.briefing("ecs", home) == out
    # a tag outside the controlled list is an error that does not echo it
    code, out, err = run(["scope", "quxbazz"], capsys)
    assert code == 2 and "quxbazz" not in out + err


# --------------------------------------------------------------------------- the name check


def test_the_name_check_runs_through_check_text_with_the_configured_register(home, monkeypatch):
    calls = []
    real = check.check_text

    def spy(text, register_path):
        calls.append(Path(register_path))
        return real(text, register_path)

    monkeypatch.setattr(check, "check_text", spy)
    new(S_NEGATIVE, tags=["bms"], tried=TRIED)
    assert len(calls) >= 4    # the statement, the source and both tried texts
    assert set(calls) == {home.register}


def test_an_unavailable_name_check_fails_closed(home, tmp_path, monkeypatch, capsys):
    first = new(S_FLAVOR)
    # a vault this user cannot see and no daemon behind the check socket
    monkeypatch.setenv("AWB_VAULT", str(tmp_path / "no-vault"))
    code, out, err = run(add_argv(S_GPU, tag="gpu"), capsys)
    assert code == 2 and "name check unavailable" in err
    assert len(entry_files(home)) == 1
    for argv in (["find", "flavor"], ["scope", "ecs"], ["show", first.id]):
        code, out, err = run(argv, capsys)
        assert code == 2 and "name check unavailable" in err and first.id not in out


def test_a_vault_folder_without_a_register_is_refused(home, tmp_path, monkeypatch, capsys):
    empty = tmp_path / "empty-vault"
    empty.mkdir()
    monkeypatch.setenv("AWB_VAULT", str(empty))
    code, _, err = run(add_argv(S_FLAVOR), capsys)
    assert code == 2 and "no register found" in err
    assert entry_files(home) == []


def test_an_entry_that_carries_a_name_registered_later_is_withheld(home, capsys):
    word = fixtures.CONTROL_UNREGISTERED
    kept = new("The %s rack runs ECS on dedicated hosts" % word, tags=["ecs", "deh"])
    clean = new("Dedicated hosts in eu-de take one ECS flavor family", tags=["ecs", "deh"])
    entries = register.load(home.register)
    register.add(home.register, register.next_code(entries, "ORG"), "ORG", word)

    code, out, _ = run(["find", "dedicated", "rack"], capsys)
    assert code == 0 and word not in out and kept.id not in out
    assert clean.id in out and "1 entry withheld" in out
    code, out, _ = run(["scope", "deh"], capsys)
    assert code == 0 and word not in out and clean.id in out and "1 entry withheld" in out
    code, out, err = run(["show", kept.id], capsys)
    assert code == 1 and word not in out + err


def test_a_malformed_entry_is_named_by_id_and_line_only(home, capsys):
    folder = home.kb / "entries"
    folder.mkdir(parents=True)
    form = fixtures.PERSON_FORMS[0]
    (folder / "KB-AAAA.md").write_text(
        "id: KB-AAAA\nscope: tcp\ntags: ecs\ngrade: %s\nchecked: 2026-09-01\nclass: api\nexpires: 2027-02-28\n"
        "source: docs\n---\nA statement.\n" % form, encoding="utf-8")
    with pytest.raises(kb.KBError) as info:
        kb.load(home)
    assert "KB-AAAA line 4: grade" in str(info.value) and form not in str(info.value)
    code, out, err = run(["find", "statement"], capsys)
    assert code == 2 and "KB-AAAA line 4" in err
    fixtures.assert_no_fixture_name(out + err, "kb find over a malformed entry")
    # files that are not named like an entry are not entries
    (folder / "KB-AAAA.md").unlink()
    (folder / "notes.md").write_text("%s\n" % form, encoding="utf-8")
    assert kb.load(home) == []


def test_parse_and_render_round_trip():
    e = kb.Entry(id="KB-Q7M4", scope="hcs", tags=("hcs-general", "backup"), grade="contract", checked="2026-01-02",
                 cls="stable", expires="2027-01-02", source="service description, section backup",
                 tried=("a first try", "a second try"), statement="Line one.\nLine two.")
    assert kb.parse(kb.render(e), "KB-Q7M4") == e and not e.is_retired
    withdrawn = replace(e, retired="2026-09-23", why="the fact was corrected in another entry")
    assert "tried: a first try; a second try\nretired: 2026-09-23\nwhy: the fact was corrected in another " \
           "entry\n---\n" in kb.render(withdrawn)
    assert kb.parse(kb.render(withdrawn), "KB-Q7M4") == withdrawn and withdrawn.is_retired
    with pytest.raises(kb.KBError, match="does not match the file name"):
        kb.parse(kb.render(e), "KB-ZZZZ")
    with pytest.raises(kb.KBError, match="no line ---"):
        kb.parse("id: KB-Q7M4\n", "KB-Q7M4")


def test_is_negative():
    for text in ("X cannot be done", "GPU flavors are not available in eu-nl", "there is no GPU flavor available",
                 "the feature is not supported", "the API does not exist", "a restore is not possible",
                 "the service is unavailable", "the route doesn't exist"):
        assert kb.is_negative(text), text
    for text in (S_FLAVOR, "GPU flavors are available in eu-de", "ECS can be resized"):
        assert not kb.is_negative(text), text


# --------------------------------------------------------------------------- verify: the checks of add over entry files


def hand_edit(e: kb.Entry, old: str, new_text: str) -> None:
    text = e.path.read_text(encoding="utf-8")
    assert old in text
    e.path.write_text(text.replace(old, new_text), encoding="utf-8")


def reasons(problems) -> str:
    return " | ".join(why for _, why in problems)


def kb_git(repo: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-C", str(repo), "-c", "user.name=awb-test", "-c", "user.email=awb@example.org",
                           "-c", "commit.gpgsign=false", "-c", "init.defaultBranch=main", *args],
                          capture_output=True, text=True, check=False)


def committed_base(repo: Path) -> None:
    assert kb_git(repo, "init", "-q").returncode == 0
    assert kb_git(repo, "add", "-A").returncode == 0
    assert kb_git(repo, "commit", "-q", "--no-verify", "-m", "base").returncode == 0


def test_verify_passes_entries_written_by_add(home):
    a = new(S_FLAVOR)
    b = new(S_NEGATIVE, tried=list(TRIED))
    assert kb.verify([a.path, b.path]) == []


def test_verify_finds_a_hand_written_price(home):
    e = new(S_FLAVOR)
    hand_edit(e, S_FLAVOR, "ECS flavor s3.large.2 costs 0.10 EUR per hour in eu-de")
    problems = kb.verify([e.path])
    assert problems and {label for label, _ in problems} == {str(e.path)}
    assert "price" in reasons(problems)


def test_verify_finds_a_negative_without_two_tries(home):
    e = new(S_FLAVOR)
    hand_edit(e, S_FLAVOR, S_NEGATIVE)
    assert "negative" in reasons(kb.verify([e.path]))


def test_verify_finds_an_unknown_tag_a_missing_key_and_a_wrong_expiry(home):
    e = new(S_FLAVOR)
    text = e.path.read_text(encoding="utf-8")
    hand_edit(e, "tags: ecs", "tags: ecs, not-a-known-tag")
    assert "rules/tags.txt" in reasons(kb.verify([e.path]))
    e.path.write_text("".join(line for line in text.splitlines(keepends=True) if not line.startswith("source:")),
                      encoding="utf-8")
    assert "missing header keys" in reasons(kb.verify([e.path]))
    e.path.write_text(re.sub(r"(?m)^expires: .*$", "expires: 2099-01-01", text), encoding="utf-8")
    assert "expires" in reasons(kb.verify([e.path]))


def test_verify_finds_a_name_and_a_code(home):
    e = new(S_FLAVOR)
    hand_edit(e, S_FLAVOR, "ECS flavor s3.large.2 for %s in eu-de" % fixtures.CUSTOMER_FORMS[0])
    assert "name check" in reasons(kb.verify([e.path]))
    e2 = new(S_GPU)
    hand_edit(e2, S_GPU, "GPU flavor p2 for %s needs a quota increase in eu-nl" % fixtures.CUSTOMER_CODE)
    assert "code" in reasons(kb.verify([e2.path]))


def test_verify_staged_checks_what_the_commit_carries(home):
    new(S_FLAVOR)
    committed_base(home.kb)
    bad = new(S_GPU)
    hand_edit(bad, S_GPU, S_NEGATIVE)
    assert kb.verify(staged_repo=home.kb) == []      # nothing staged yet: not what a commit carries
    assert kb_git(home.kb, "add", "-A").returncode == 0
    problems = kb.verify(staged_repo=home.kb)
    assert problems and {label for label, _ in problems} == {"entries/%s.md" % bad.id}
    assert "negative" in reasons(problems)


def test_verify_staged_refuses_a_deleted_entry(home):
    e = new(S_FLAVOR)
    committed_base(home.kb)
    e.path.unlink()
    assert kb_git(home.kb, "add", "-A").returncode == 0
    assert "retire" in reasons(kb.verify(staged_repo=home.kb))


def test_kb_verify_on_the_command_line(home, capsys):
    e = new(S_FLAVOR)
    code, out, err = run(["verify", str(e.path)], capsys)
    assert code == 0, err
    hand_edit(e, S_FLAVOR, S_NEGATIVE)
    code, out, err = run(["verify", str(e.path)], capsys)
    assert code == 1 and "negative" in out
    code, out, err = run(["verify"], capsys)
    assert code == 2


def test_the_knowledge_base_hook_refuses_a_hand_written_entry(home, monkeypatch):
    from awb import gate
    monkeypatch.setenv("PYTHONPATH", str(Path(__file__).resolve().parent.parent))
    new(S_FLAVOR)
    committed_base(home.kb)
    gate.install_hook(home.kb, kb=True, installed=Path("/nonexistent/usr/local/bin/awb"))
    new(S_GPU)
    assert kb_git(home.kb, "add", "-A").returncode == 0
    ok = kb_git(home.kb, "commit", "-q", "-m", "a fact through kb add")
    assert ok.returncode == 0, ok.stdout + ok.stderr
    bad = new("OBS bucket names are unique across all accounts in eu-de", tags=["obs"])
    hand_edit(bad, "OBS bucket names are unique across all accounts in eu-de", S_NEGATIVE)
    assert kb_git(home.kb, "add", "-A").returncode == 0
    refused = kb_git(home.kb, "commit", "-q", "-m", "a hand edit")
    assert refused.returncode != 0
    assert "does not pass the checks of awb kb add" in refused.stderr


# --------------------------------------------------------------------------- recheck


def test_recheck_records_the_new_evidence_and_keeps_the_statement(home, capsys):
    old = (TODAY - timedelta(days=200)).isoformat()
    e = new(S_FLAVOR, grade="live", source="live API call on a test tenant, eu-de", checked=old)
    code, out, err = run(["recheck", "--grade", "docs", "--source", "API reference in the doc mirror", e.id], capsys)
    assert code == 0 and err == "" and "rechecked %s: grade docs" % e.id in out
    after = kb.get(e.id, home)
    assert after.statement == e.statement and after.tags == e.tags and after.cls == e.cls
    assert after.grade == "docs" and after.source == "API reference in the doc mirror"
    assert after.checked == TODAY.isoformat() and after.expires == (TODAY + timedelta(days=180)).isoformat()
    assert kb.get(e.id, home).path == e.path and len(entry_files(home)) == 1


def test_a_recheck_of_a_negative_needs_two_tried_texts_of_the_new_check(home, capsys):
    e = new(S_NEGATIVE, tags=["bms"], grade="live", tried=TRIED)
    code, _, err = run(["recheck", "--grade", "live", "--source", "live API call, eu-de", e.id], capsys)
    assert code == 1 and "tried of the new check" in err
    assert kb.get(e.id, home).checked == e.checked
    fresh = ("listed the image types of eu-de again", "tried an import of a Windows image as a BMS image again")
    code, _, err = run(["recheck", "--grade", "live", "--source", "live API call, eu-de",
                        "--tried", fresh[0], "--tried", fresh[1], e.id], capsys)
    assert code == 0 and err == ""
    assert kb.get(e.id, home).tried == fresh


def test_a_recheck_is_refused_for_a_retired_entry_a_bad_grade_and_a_name(home, capsys):
    e = new(S_FLAVOR)
    with pytest.raises(kb.KBError, match="grade must be one of"):
        kb.recheck(e.id, grade="rumour", source="x", where=home)
    with pytest.raises(kb.KBError, match="checked is empty"):
        kb.recheck(e.id, grade="docs", source="x", checked="", where=home)
    code, _, err = run(["recheck", "--grade", "docs", "--source", "notes of %s" % fixtures.CUSTOMER_FORMS[0], e.id],
                       capsys)
    assert code == 1 and fixtures.CUSTOMER_FORMS[0] not in err
    kb.retire(e.id, "replaced by a wider fact", where=home)
    with pytest.raises(kb.Refused):
        kb.recheck(e.id, grade="docs", source="API reference", where=home)


def test_amend_with_tried_texts_replaces_them_and_proves_a_new_negative(home, capsys):
    plain = new(S_FLAVOR)
    negative = "ECS flavor s3.large.2 is not offered in eu-nl"
    code, _, err = run(["amend", plain.id, negative], capsys)
    assert code == 1 and "tried texts of the entry" in err
    proof = ("listed the flavors of eu-nl on 2026-09-25", "asked the price API for s3.large.2 in eu-nl")
    code, _, err = run(["amend", "--tried", proof[0], "--tried", proof[1], plain.id, negative], capsys)
    assert code == 0 and err == ""
    after = kb.get(plain.id, home)
    assert after.statement == negative and after.tried == proof and after.checked == plain.checked


# --------------------------------------------------------------------------- the search, measured 2026-09-25


def test_a_word_and_its_stem_count_once():
    units = kb.concepts("list the instances, listing volumes attached to one instance")
    assert frozenset({"instance", "instanc"}) in units
    assert frozenset({"listing", "list"}) in units
    assert sum(1 for u in units if "instance" in u) == 1


def test_a_stem_does_not_outvote_a_rare_word(home):
    rare = new("GET /v1/{project_id}/vpcs lists every VPC with its cidr")
    new("Peering needs CIDR ranges that do not overlap, and route ranges are checked per range", tags=["ecs"],
        force_new=True)
    top = kb.find("How do I list all VPCs and their CIDR ranges on TCP?", where=home)
    assert top[0][1].id == rare.id


def test_a_group_links_a_plain_word_to_an_abbreviation(home):
    eip = new("Tags on an EIP are written with the tags action call", tags=["vpc"])
    new("A floating network interface keeps its tags when it moves", tags=["vpc"], force_new=True)
    hits = [e.id for _, e in kb.find("tag an elastic ip", where=home)]
    assert hits[0] == eip.id


def test_a_term_of_several_words_counts_only_whole():
    groups = [[frozenset({"elb"}), frozenset({"load", "balancer"})]]
    assert kb.expand({"elb", "listener"}, groups) == [[frozenset({"load", "balancer"})]]
    assert kb.expand({"load", "listener"}, groups) == []          # "load" alone is not the term
    assert kb.expand({"load", "balancer"}, groups) == [[frozenset({"elb"})]]


def test_question_words_and_the_platform_name_are_not_searched(home):
    target = new("DNS zones carry DNSSEC signing per record set", tags=["dns"])
    noise = new("How and why TCP works: what we do there", tags=["other"], force_new=True)
    hits = [e.id for _, e in kb.find("What do we do on TCP for DNSSEC and why?", where=home)]
    assert hits == [target.id] and noise.id not in hits


def test_the_groups_file_is_optional(tmp_path):
    assert kb.synonym_groups(tmp_path / "none.txt") == []
    f = tmp_path / "g.txt"
    f.write_text("# comment\nelb, load balancer\n\nsolo\n", encoding="utf-8")
    assert kb.synonym_groups(f) == [[frozenset({"elb"}), frozenset({"load", "balancer"})]]


def test_the_long_name_of_the_platform_is_not_searched_but_public_ip_is(home):
    ip = new("A public IP keeps its address when the ECS is stopped", tags=["vpc"])
    noise = new("On T Cloud Public (TCP) Windows images come from Open Telekom Cloud days", tags=["ims"], force_new=True)
    hits = [e.id for _, e in kb.find("Does a public IP on T Cloud Public stay when stopped?", where=home)]
    assert hits[0] == ip.id
    assert kb.find("T Cloud Public", where=home) == [] and kb.find("t-cloud-public", where=home) == []
    assert kb.find("Open Telekom Cloud", where=home) == [] and noise.id in [e.id for _, e in kb.find("windows images",
                                                                                                    where=home)]


def test_bench_counts_first_places_and_misses(home, tmp_path, capsys):
    vpc = new("GET /v1/{project_id}/vpcs lists every VPC with its cidr")
    gone = new("Cloud Eye keeps raw metric data for two days", tags=["ces"])
    kb.retire(gone.id, "replaced by a wider fact")
    questions = [{"question": "How do I list the VPCs?", "accept": [vpc.id]},
                 {"question": "Which flavors does Kafka offer?", "accept": [vpc.id]},
                 {"question": "How long are raw metrics kept?", "accept": [gone.id]},
                 {"question": "A gap nobody answered yet", "accept": []}]
    r = kb.bench(questions, where=home)
    assert (r["answerable"], r["hit1"], r["hit_top"], r["misses"]) == (2, 1, 1, [1])
    f = tmp_path / "bench.json"
    f.write_text(json.dumps({"about": "invented", "questions": questions}), encoding="utf-8")
    code, out, err = run(["bench", str(f)], capsys)
    assert code == 0 and "2 answerable question(s), the answering entry first for 1 (50%)" in out
    assert "missed #1: Which flavors does Kafka offer?" in out
    bad = tmp_path / "bad.json"
    bad.write_text('{"questions": 3}', encoding="utf-8")
    code, _, err = run(["bench", str(bad)], capsys)
    assert code == 2 and "no list of questions" in err


def test_screen_counts_names_only_and_checks_a_long_list_in_halves(monkeypatch, tmp_path):
    """A broad search found hundreds of entries; a source URL made the group dirty and every entry was then checked
    alone, which ran into the rate limit of the vault daemon (2026-09-29)."""
    entries = [kb.Entry("KB-%04d" % i, "tcp", ("other",), "docs", "2026-09-01", "stable", "2027-09-01",
                        "https://docs.example.org/page", (), "fact %d" % i) for i in range(256)]
    entries[77] = kb.Entry("KB-0077", "tcp", ("other",), "docs", "2026-09-01", "stable", "2027-09-01",
                           "an invented source", (), "fact with a planted form")
    calls = []

    def hits(text, reg):
        calls.append(len(text))
        out = [{"start": 0, "length": 5, "cls": "url"}] if "https://" in text else []
        if "planted form" in text:
            out.append({"start": 0, "length": 5, "cls": "name"})
        return out

    monkeypatch.setattr(kb, "_name_hits", hits)
    monkeypatch.setattr(kb, "_require_register", lambda reg: None)
    shown, withheld = kb.screen(entries, tmp_path / "register.tsv")
    assert withheld == 1 and "KB-0077" not in {e.id for e in shown} and len(shown) == 255
    assert len(calls) <= 2 * 9 + 1, "halves, not one check per entry"
    calls.clear()
    clean = [e for e in entries if e.id != "KB-0077"]
    assert kb.screen(clean, tmp_path / "register.tsv") == (clean, 0) and len(calls) == 1
