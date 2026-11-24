"""tcp-bytg ask 19 of 2026-11-21: `awb kb merge`, `awb kb renew`, `awb kb expired --days` and a `find` that screens
only what it prints. A temporary knowledge base (AWB_KB) with the fixture register and invented content."""
from __future__ import annotations

import re
from datetime import date, timedelta

from awb import cli, kb, register
from tests import fixtures

TODAY = date.today()

S_KEEP = "ECS flavor s3.large.2 is offered in eu-de with two vCPUs"
S_DROP = "The ECS flavor s3.large.2 is offered in eu-de and has two vCPUs"
S_OTHER = "Object storage buckets keep versions when versioning is switched on"
SOURCE_KEEP = "https://example.org/ecs/flavors"
SOURCE_DROP = "service description, section ecs"


def run(argv, capsys):
    code = cli.main(["kb", *argv])
    out, err = capsys.readouterr()
    return code, out, err


def new(statement: str, **kw) -> kb.Entry:
    args = dict(scope="tcp", tags=["ecs"], grade="docs", cls="api", source=SOURCE_KEEP)
    args.update(kw)
    return kb.add(statement, **args)


def text_of(entry: kb.Entry) -> str:
    return entry.path.read_text(encoding="utf-8")


# --------------------------------------------------------------------------- merge


def test_merge_retires_the_duplicate_into_the_entry_that_stays(home, capsys):
    keep = new(S_KEEP)
    drop = new(S_DROP, tags=["ecs", "api"], source=SOURCE_DROP, force_new=True)
    code, out, err = run(["merge", "--why", "the same flavor fact twice", keep.id, drop.id], capsys)
    assert code == 0 and err == ""
    assert "merged %s into %s" % (drop.id, keep.id) in out
    kept, dropped = kb.get(keep.id), kb.get(drop.id)
    # the entry that stays carries the sources and the tags of both, its statement and grade stay
    assert kept.source == "%s; %s" % (SOURCE_KEEP, SOURCE_DROP)
    assert kept.tags == ("ecs", "api") and kept.statement == S_KEEP and kept.grade == "docs"
    assert kept.checked == keep.checked and not kept.is_retired
    # the duplicate is retired and names the entry it went into
    assert dropped.retired == TODAY.isoformat() and dropped.merged_into == keep.id
    assert dropped.why == "merged into %s: the same flavor fact twice" % keep.id
    assert dropped.statement == S_DROP and dropped.source == SOURCE_DROP
    # show and find of the dropped id answer with the entry that stays
    code, out, _ = run(["show", drop.id], capsys)
    assert code == 0 and "MERGED into %s" % keep.id in out
    code, out, _ = run(["find", drop.id], capsys)
    assert code == 0 and "%s was merged into %s" % (drop.id, keep.id) in out
    assert re.search(r"^%s  " % keep.id, out, re.M)
    # a search by words offers the entry that stays only
    code, out, _ = run(["find", "flavor", "s3.large.2"], capsys)
    assert keep.id in out and drop.id not in out
    # the index and the export (what the dataset build reads) leave the merged entry out
    index = (home.kb / kb.INDEX).read_text(encoding="utf-8")
    assert keep.id in index and "[%s]" % drop.id not in index
    records, left = kb.export(today=TODAY)
    assert [r["id"] for r in records] == [keep.id] and left["retired"] == 1
    # the files pass the checks of add, as the commit hook of the knowledge base runs them
    assert kb.verify([kept.path, dropped.path]) == []


def test_merge_without_a_reason_and_with_one_source_twice(home, capsys):
    keep = new(S_KEEP)
    drop = new(S_DROP, force_new=True, tried=())
    code, _, _ = run(["merge", keep.id, drop.id], capsys)
    assert code == 0
    assert kb.get(keep.id).source == SOURCE_KEEP
    assert kb.get(drop.id).why == "merged into %s" % keep.id


def test_merge_follows_a_chain_in_show_and_find(home, capsys):
    first = new(S_KEEP)
    second = new(S_DROP, force_new=True)
    third = new("ECS flavor s3.large.2 in eu-de is offered with two vCPUs", force_new=True)
    kb.merge(second.id, third.id)
    kb.merge(first.id, second.id)
    assert kb.merged_target(third.id) == [third.id, second.id, first.id]
    code, out, _ = run(["find", third.id.lower()], capsys)
    assert "%s was merged into %s then %s" % (third.id, second.id, first.id) in out
    code, out, _ = run(["show", third.id], capsys)
    assert "MERGED into %s then %s" % (second.id, first.id) in out


def test_merge_refusals_write_nothing(home, capsys):
    keep = new(S_KEEP)
    drop = new(S_DROP, force_new=True)
    other = new("Dedicated hosts in eu-nl run one ECS flavor family", scope="hcs", tags=["ecs"])
    before = {e.id: text_of(e) for e in (keep, drop, other)}

    code, _, err = run(["merge", keep.id, keep.id], capsys)
    assert code == 2 and "into itself" in err
    code, _, err = run(["merge", keep.id, other.id], capsys)
    assert code == 1 and "one scope" in err and "nothing was changed" in err
    name = fixtures.PERSON_FORMS[0]
    code, out, err = run(["merge", "--why", "said so by %s" % name, keep.id, drop.id], capsys)
    assert code == 1 and "the reason carries hits of the name check" in err
    fixtures.assert_no_fixture_name(out + err, "kb merge refusal")
    code, _, err = run(["merge", keep.id, "KB-ZZZZ"], capsys)
    assert code == 2 and "there is no entry KB-ZZZZ" in err
    assert {e.id: text_of(e) for e in (keep, drop, other)} == before

    kb.retire(drop.id, "withdrawn for the test")
    code, _, err = run(["merge", keep.id, drop.id], capsys)
    assert code == 1 and "%s is retired" % drop.id in err
    assert text_of(keep) == before[keep.id]


def test_merge_of_two_long_sources_needs_one_source_line(home, capsys):
    long_a = "service description, section ecs, " + "flavor table " * 18
    long_b = "user guide of ecs, chapter flavors, " + "general purpose " * 15
    keep = new(S_KEEP, source=long_a.strip())
    drop = new(S_DROP, source=long_b.strip(), force_new=True)
    before = text_of(keep)
    code, _, err = run(["merge", keep.id, drop.id], capsys)
    assert code == 1 and "give --source" in err
    assert text_of(keep) == before and not kb.get(drop.id).is_retired
    code, _, _ = run(["merge", "--source", "service description and user guide of ecs, flavors", keep.id, drop.id],
                     capsys)
    assert code == 0 and kb.get(keep.id).source == "service description and user guide of ecs, flavors"


def test_merge_keeps_the_tried_texts_of_both(home):
    tried_a = ("image import api call in eu-de", "console import wizard in eu-de")
    tried_b = ("console import wizard in eu-de", "the api of the image service in eu-nl")
    keep = new("Windows images cannot be imported as dedicated host images in eu-de", tried=tried_a)
    drop = new("Windows images cannot be imported as images of dedicated hosts in eu-de", tried=tried_b,
               force_new=True)
    kept, _ = kb.merge(keep.id, drop.id)
    assert kept.tried == (tried_a[0], tried_a[1], tried_b[1])


# --------------------------------------------------------------------------- renew and expired


def test_renew_moves_the_check_date_and_keeps_the_grade(home, capsys):
    old = (TODAY - timedelta(days=40)).isoformat()
    e = new(S_OTHER, tags=["obs"], grade="contract", cls="availability", checked=old)
    assert e.is_expired(TODAY)
    code, out, _ = run(["expired"], capsys)
    assert code == 1 and e.id in out and "awb kb renew ID --source URL" in out
    code, out, err = run(["renew", e.id, "--source", "https://example.org/obs/versioning"], capsys)
    assert code == 0 and err == "" and "renewed %s" % e.id in out
    got = kb.get(e.id)
    assert got.checked == TODAY.isoformat() and got.grade == "contract"
    assert got.expires == (TODAY + timedelta(days=30)).isoformat()
    assert got.source == "https://example.org/obs/versioning" and got.statement == S_OTHER
    code, out, _ = run(["expired"], capsys)
    assert code == 0 and "no expired entries" in out


def test_renew_refusals_write_nothing(home, capsys):
    e = new(S_OTHER, tags=["obs"], cls="availability", checked=(TODAY - timedelta(days=40)).isoformat())
    before = text_of(e)
    code, _, err = run(["renew", e.id, "--source", "the docs again"], capsys)
    assert code == 2 and "web address" in err
    url = "https://%s/obs" % fixtures.CUSTOMER_DOMAIN
    code, out, err = run(["renew", e.id, "--source", url], capsys)
    assert code == 1 and "the source carries hits of the name check" in err
    fixtures.assert_no_fixture_name(out + err, "kb renew refusal")
    future = (TODAY + timedelta(days=3)).isoformat()
    code, _, err = run(["renew", e.id, "--source", "https://example.org/obs", "--checked", future], capsys)
    assert code == 2 and "future" in err
    assert text_of(e) == before
    kb.retire(e.id, "withdrawn for the test")
    code, _, err = run(["renew", e.id, "--source", "https://example.org/obs"], capsys)
    assert code == 1 and "is retired" in err


def test_retire_from_the_expired_list(home, capsys):
    e = new(S_OTHER, tags=["obs"], cls="availability", checked=(TODAY - timedelta(days=40)).isoformat())
    code, out, _ = run(["retire", e.id, "--why", "the page no longer says so"], capsys)
    assert code == 0 and "retired %s" % e.id in out
    code, out, _ = run(["expired"], capsys)
    assert code == 0 and e.id not in out


def test_expired_days_adds_the_entries_that_expire_soon(home, capsys):
    soon = new(S_OTHER, tags=["obs"], cls="availability", checked=(TODAY - timedelta(days=25)).isoformat())
    later = new(S_KEEP)
    code, out, _ = run(["expired"], capsys)
    assert code == 0 and soon.id not in out
    code, out, _ = run(["expired", "--days", "10"], capsys)
    assert code == 1 and soon.id in out and later.id not in out
    assert "expires %s" % soon.expires in out and "in the next 10 days" in out
    code, _, err = run(["expired", "--days", "-1"], capsys)
    assert code == 2 and "--days" in err


# --------------------------------------------------------------------------- find


def _many(n: int) -> list[kb.Entry]:
    subjects = ("snapshots", "backups", "replicas", "images", "volumes", "templates", "keypairs", "policies")
    return [new("EVS %s are listed by the api of region eu-de" % s, tags=["evs"], force_new=True)
            for s in subjects[:n]]


def test_find_prints_the_top_and_screens_only_what_it_prints(home, capsys, monkeypatch):
    entries = _many(6)
    seen: list[int] = []
    real = kb.screen

    def counting(group, register_path=None):
        seen.append(len(list(group)))
        return real(group, register_path)

    monkeypatch.setattr(kb, "screen", counting)
    code, out, _ = run(["find", "--top", "2", "evs", "listed"], capsys)
    lines = [ln for ln in out.splitlines() if ln.startswith("KB-")]
    assert code == 0 and len(lines) == 2
    assert "4 more matches: --top N or --all prints them" in out
    assert sum(seen) == 2
    seen.clear()
    code, out, _ = run(["find", "--all", "evs", "listed"], capsys)
    assert len([ln for ln in out.splitlines() if ln.startswith("KB-")]) == len(entries)
    assert "more match" not in out and sum(seen) == len(entries)
    code, out, _ = run(["find", "evs"], capsys)
    assert len([ln for ln in out.splitlines() if ln.startswith("KB-")]) == len(entries)
    code, _, err = run(["find", "--top", "0", "evs"], capsys)
    assert code == 2 and "--top" in err


def test_find_fills_the_top_past_a_withheld_entry(home, capsys):
    word = fixtures.CONTROL_UNREGISTERED
    dirty = new("The %s evs snapshots are listed by the api of region eu-de, listed daily" % word, tags=["evs"])
    clean = _many(3)
    entries = register.load(home.register)
    register.add(home.register, register.next_code(entries, "ORG"), "ORG", word)
    code, out, _ = run(["find", "--top", "2", "evs", "snapshots", "listed", "daily"], capsys)
    lines = [ln for ln in out.splitlines() if ln.startswith("KB-")]
    assert code == 0 and len(lines) == 2 and dirty.id not in out
    assert all(any(ln.startswith(e.id) for e in clean) for ln in lines)
    assert "1 entry withheld" in out
    fixtures.assert_no_fixture_name(out, "kb find with a withheld entry")


def test_find_fails_closed_when_nothing_matches(home, tmp_path, monkeypatch, capsys):
    new(S_KEEP)
    monkeypatch.setenv("AWB_VAULT", str(tmp_path / "no-vault"))
    code, out, err = run(["find", "nothing", "here", "matches"], capsys)
    assert code == 2 and "name check unavailable" in err and out == ""
