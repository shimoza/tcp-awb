"""RN3: the release-notes lines of awb refresh list an entry only when it names the service and shares the topic of
the note. Fixture notes and invented entries; nothing touches the network."""
from __future__ import annotations

import datetime

from awb import kb, offered
from awb.tcp import refresh

TODAY = datetime.date(2026, 10, 19)


def entry(statement, tags=("kms",)):
    return kb.add(statement, scope="tcp", tags=list(tags), grade="live", cls="availability",
                  source="live API call, eu-de", force_new=True)


def note(title, text, date="2026-10-16", services=("Key Management Service",), slug="n"):
    return {"id": "%s-%s" % (date, slug), "date": date, "title": title, "text": text, "services": list(services)}


def lines(notes, home, **kw):
    kw.setdefault("today", TODAY)
    return refresh.release_note_lines(notes, kb.load(home), offered.load(), **kw)


def test_a_note_about_one_feature_lists_only_the_entries_of_that_feature(home):
    rotation = entry("the KMS key rotation interval is configurable per key through the API")
    rotation2 = entry("automatic key rotation is off by default in eu-de")
    other = entry("the KMS key list answers in under a second")
    one_word = entry("a key rotation test runs here")
    n = note("key rotation by API", "Automatic key rotation can be set through the API.")
    got = lines([n], home)
    assert any(rotation.id in line for line in got) and any(rotation2.id in line for line in got)
    assert not any(other.id in line or one_word.id in line for line in got)


def test_the_lines_come_by_shared_words_most_first(home):
    few = entry("the key rotation interval is fixed")
    many = entry("the key rotation interval is configurable through the API in eu-de")
    n = note("key rotation interval API", "The rotation interval of a key is set through the API in eu-de.")
    got = lines([n], home)
    assert [line.split()[1] for line in got] == [many.id + "", few.id + ""]


def test_the_list_is_capped_at_fifteen_and_all_prints_every_match(home):
    ids = [entry("key rotation interval number %d is set through the API" % i).id for i in range(20)]
    n = note("key rotation interval", "The key rotation interval is set through the API.")
    got = lines([n], home)
    assert len(got) == 16 and got[-1] == "and 5 more (awb refresh --part release-notes --all)"
    assert all(line.startswith("recheck: ") for line in got[:15])
    everything = lines([n], home, limit=None)
    assert len(everything) == 20 and not any(line.startswith("and ") for line in everything)
    assert {line.split()[1] for line in everything} == set(ids)


def test_a_note_dated_after_today_is_listed_in_the_block_announced_with_its_date(home):
    e = entry("the key rotation interval is configurable through the API")
    later = note("key rotation interval", "The key rotation interval is set through the API.", date="2026-11-03")
    now = note("key rotation interval", "The key rotation interval is set through the API.", slug="m")
    got = lines([later, now], home)
    assert got[0].startswith("recheck: %s (release note 2026-10-16" % e.id)
    assert got[1:] == ["announced:", "  recheck: %s (release note 2026-11-03 Key Management Service)" % e.id]


def test_the_part_takes_all_and_the_cli_flag_exists(home, tmp_path, monkeypatch):
    import datetime as dt
    from awb.tcp import mirror
    base = tmp_path / "mirrors"
    monkeypatch.setenv("AWB_MIRRORS", str(base))
    for i in range(17):
        entry("key rotation interval number %d is set through the API" % i)
    n = note("key rotation interval", "The key rotation interval is set through the API.")
    monkeypatch.setattr(mirror, "release_notes", lambda b: [dict(n, seen="2026-10-19T10:00:00Z")])
    out = home.shared / "refresh" / "x"
    short = refresh.part_release_notes(home, base, out, update=False, today=TODAY)
    assert any("and 2 more (awb refresh --part release-notes --all)" in line for line in short.lines)
    full = refresh.part_release_notes(home, base, out, update=False, today=TODAY, show_all=True)
    assert sum("recheck:" in line for line in full.lines) == 17
    assert refresh.main(["--part", "release-notes", "--no-update", "--all"]) in (0, 1)
