"""The release notes of T Cloud Public as a mirror (awb mirror rn) and as a part of the refresh. A page made here by
hand, with the structure of the live list page, stands in for the site; nothing touches the network."""
from __future__ import annotations

import datetime
import json
import urllib.error
from pathlib import Path

import pytest

from awb import cli, kb, offered
from awb.tcp import mirror, refresh

PAGE = "https://www.t-cloud-public.com/en/support/release-notes"


@pytest.fixture(autouse=True)
def the_site(monkeypatch):
    """The conftest points the page at a dead address; here the fake pages answer for the real address."""
    monkeypatch.setattr(mirror, "RN_PAGE", PAGE)


def article(date, title, text, slug, services=(), types=(), href=None):
    tags = "".join('<a href="%s?tag=%d" class="tag link-app">%s</a>' % (PAGE, i, s) for i, s in enumerate(services))
    tags += "".join('<a href="%s?type=9" class="tag link-app">%s</a>' % (PAGE, t) for t in types)
    href = href if href is not None else "/en/support/release-notes/" + slug
    return ('<article class="page-teaser"><div class="details"><time datetime="%s">x</time><h2>%s</h2><p>%s</p>'
            '<div class="tag-list">%s</div></div><a class="teaser-link" href="%s">read more</a></article>'
            % (date, title, text, tags, href))


def page(*articles, last=3, current=1):
    pager = "".join('<li><a href="%s?p=%d">%d</a></li>' % (PAGE, n, n) for n in range(1, last + 1))
    return ('<html><head><script>var p = "<article>";</script></head><body><main><header><form>'
            '<select name="tag"><option>all</option></select></form></header><section>%s'
            '<nav><div class="pagination"><ul class="pages">%s</ul></div></nav></section></main></body></html>'
            % ("".join(articles), pager))


P1 = [article("2026-10-16", "new key type", "A key type for  message checks.", "new-key-type",
              ["Key Management Service"], ["Announcements"]),
      article("2026-10-14", "dns architecture", "The new architecture.", "dns-architecture",
              ["Domain Name Service"])]
P2 = [article("2026-09-11", "end of life", "An image goes out of support.", "end-of-life",
              ["Elastic Cloud Server", "gadget hub"]),
      article("2026-09-09", "a note for all", "Text.", "for-all", ["News for users"])]
P3 = [article("2026-08-01", "old note", "Old.", "old-note", ["Elastic Cloud Server"])]


class Site:
    """The list pages by number; a call log; a note that can be added to page 1."""

    def __init__(self, pages):
        self.pages = pages
        self.calls: list[str] = []

    def __call__(self, url):
        self.calls.append(url)
        n = int(url.rsplit("?p=", 1)[1]) if "?p=" in url else 1
        return page(*self.pages[n - 1], last=len(self.pages))


def run(base, site, **kw):
    sleeps: list[float] = []
    r = mirror.sync_release_notes(base, fetch=site, sleep=sleeps.append, **kw)
    return r, sleeps


def test_a_page_is_read_into_date_title_text_link_and_tags():
    notes, last = mirror.parse_release_notes(page(*P1, last=7))
    assert last == 7
    assert [n["id"] for n in notes] == ["2026-10-16-new-key-type", "2026-10-14-dns-architecture"]
    n = notes[0]
    assert (n["date"], n["title"], n["text"]) == ("2026-10-16", "new key type", "A key type for message checks.")
    assert n["link"] == PAGE + "/new-key-type"
    assert n["services"] == ["Key Management Service"] and n["types"] == ["Announcements"]


def test_a_note_without_a_date_or_with_a_link_outside_the_site_or_into_neos_is_left_out():
    html = page(article("", "no date", "t", "no-date"),
                article("2026-10-01", "elsewhere", "t", "x", href="https://other.example/a/b"),
                article("2026-10-01", "inside neos", "t", "x", href="/neos/media/b"),
                article("2026-10-01", "fine", "t", "fine"))
    assert [n["id"] for n in mirror.parse_release_notes(html)[0]] == ["2026-10-01-fine"]


def test_the_first_run_reads_every_page_with_a_pause_between_them(tmp_path):
    site = Site([P1, P2, P3])
    r, sleeps = run(tmp_path, site, pause=1.5)
    assert site.calls == [PAGE, PAGE + "?p=2", PAGE + "?p=3"]          # one request per page, the list pages only
    assert sleeps == [1.5, 1.5]
    assert (r["new"], r["known"], r["pages"], r["total"]) == (5, 0, 3, 5)
    one = json.loads((tmp_path / "release-notes" / "notes" / "2026-10-16-new-key-type.json").read_text())
    assert one["title"] == "new key type" and one["services"] == ["Key Management Service"]
    index = json.loads((tmp_path / "release-notes" / "INDEX.json").read_text())
    assert sorted(index["notes"]) == sorted(n["id"] for n in mirror.release_notes(tmp_path))
    assert index["notes"]["2026-10-16-new-key-type"]["file"] == "notes/2026-10-16-new-key-type.json"
    assert [n["id"] for n in mirror.release_notes(tmp_path)][0] == "2026-10-16-new-key-type"   # newest first


def test_a_second_run_adds_nothing_and_reads_page_one_only(tmp_path):
    run(tmp_path, Site([P1, P2, P3]))
    f = tmp_path / "release-notes" / "notes" / "2026-10-16-new-key-type.json"
    f.write_text('{"kept": true}\n', encoding="utf-8")
    site = Site([P1, P2, P3])
    r, sleeps = run(tmp_path, site)
    assert (r["new"], r["known"], r["pages"]) == (0, 2, 1) and sleeps == [] and site.calls == [PAGE]
    assert f.read_text() == '{"kept": true}\n'                          # a known note is never written again


def test_a_new_note_on_page_one_is_the_only_one_added(tmp_path):
    run(tmp_path, Site([P1, P2, P3]))
    newer = article("2026-10-20", "later", "Later.", "later", ["Elastic Cloud Server"])
    r, _ = run(tmp_path, Site([[newer] + P1, P2, P3]))
    assert r["ids"] == ["2026-10-20-later"] and r["total"] == 6 and r["pages"] == 2


def test_pages_caps_the_run_and_the_next_run_goes_on(tmp_path):
    r, _ = run(tmp_path, Site([P1, P2, P3]), pages=1)
    assert (r["new"], r["pages"]) == (2, 1)
    r, _ = run(tmp_path, Site([P1, P2, P3]))
    assert r["total"] == 5 and r["new"] == 3


def test_a_page_without_notes_is_an_error_and_a_failed_fetch_keeps_what_was_done(tmp_path):
    with pytest.raises(mirror.MirrorError, match="shape changed"):
        mirror.sync_release_notes(tmp_path, fetch=lambda url: "<html><body>changed</body></html>", sleep=lambda s: None)
    assert not (tmp_path / "release-notes" / INDEX_NAME).exists()

    def fetch(url):
        if "p=2" in url:
            raise urllib.error.HTTPError(url, 503, "x", None, None)
        return page(*P1, last=3)
    with pytest.raises(mirror.MirrorError, match="HTTP 503"):
        mirror.sync_release_notes(tmp_path, fetch=fetch, sleep=lambda s: None)
    assert len(mirror.release_notes(tmp_path)) == 2


INDEX_NAME = "INDEX.json"


def test_the_command_fetches_and_status_tells(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("AWB_MIRRORS", str(tmp_path))
    monkeypatch.setattr(mirror, "_fetch_page", Site([P1, P2, P3]))
    assert cli.main(["mirror", "rn", "--pause", "0"]) == 0
    assert "5 new note(s)" in capsys.readouterr().out
    assert cli.main(["mirror", "status"]) == 0
    assert "release notes: 5 notes, newest 2026-10-16" in capsys.readouterr().out


# --------------------------------------------------------------------------- the refresh


def entry(statement, tags):
    return kb.add(statement, scope="tcp", tags=tags, grade="live", cls="availability", source="live API call, eu-de")


def test_the_lines_name_entries_of_the_service_and_services_the_list_does_not_know(home):
    by_tag = entry("a flavor family is offered in the region", ["ecs"])
    by_name = entry("an Elastic Cloud Server console shows the image list", ["ims"])
    by_short = entry("the KMS key list answers in under a second", ["kms"])
    gone = entry("an ECS statement that stands no more", ["ecs"])
    kb.retire(gone.id, "not true")
    other = entry("an RDS instance can be resized online", ["rds"])
    notes = mirror.parse_release_notes(page(*P1, *P2))[0]
    lines = refresh.release_note_lines(notes, kb.load(home), offered.load())
    # RN3: an entry is listed only when it shares two topic words with the note; these three share none or one
    assert lines == ["new service?: gadget hub (release note 2026-09-11 end of life)"]
    assert gone.id not in "".join(lines) and other.id not in "".join(lines)


def test_the_part_lists_the_notes_since_the_last_refresh_and_changes_no_entry(home, tmp_path, monkeypatch):
    base = tmp_path / "mirrors"
    monkeypatch.setenv("AWB_MIRRORS", str(base))
    e = entry("the KMS key list answers in under a second", ["kms"])
    before = (home.kb / "entries" / (e.id + ".md")).read_bytes()
    today = datetime.date(2026, 10, 19)
    out = home.shared / "refresh" / "2026-10-19"
    site = Site([P1, P2, P3])
    monkeypatch.setattr(mirror, "_fetch_page", site)
    monkeypatch.setattr(mirror.time, "sleep", lambda s: None)
    part = refresh.part_release_notes(home, base, out, update=True, today=today)
    text = "\n".join(part.lines)
    assert "2 release note(s) dated since 2026-09-19" in text            # the first run: the last 30 days
    assert "recheck: %s (release note 2026-10-16 Key Management Service)" % e.id not in text   # RN3: one shared word
    assert "gadget hub" not in text                                      # its note is older than 30 days
    assert (out / "release-notes.txt").exists()
    again = refresh.part_release_notes(home, base, out, update=True, today=today)
    assert "0 release note(s) first seen since" in "\n".join(again.lines) and again.overdue == 0
    assert (home.kb / "entries" / (e.id + ".md")).read_bytes() == before


def test_the_part_uses_what_is_first_seen_and_a_run_without_update_keeps_the_day(home, tmp_path, monkeypatch):
    base = tmp_path / "mirrors"
    monkeypatch.setenv("AWB_MIRRORS", str(base))
    monkeypatch.setattr(mirror.time, "sleep", lambda s: None)
    clock = iter("2026-10-19T10:00:%02dZ" % n for n in range(60))
    monkeypatch.setattr(mirror, "_now", lambda: next(clock))
    entry("a flavor family is offered in the region", ["ecs"])
    out = home.shared / "refresh" / "x"
    today = datetime.date(2026, 10, 19)
    monkeypatch.setattr(mirror, "_fetch_page", Site([P1, P2, P3]))
    refresh.part_release_notes(home, base, out, update=True, today=today)
    state = refresh.rn_state(home).read_text()
    older = article("2026-09-11", "another end of life", "Text.", "another-end-of-life", ["gadget hub"])
    monkeypatch.setattr(mirror, "_fetch_page", Site([[older] + P1, P2, P3]))
    part = refresh.part_release_notes(home, base, out, update=True, today=today)
    assert any(line.startswith("  new service?: gadget hub") for line in part.lines)   # a note not yet seen, dated old
    assert part.overdue == 1
    refresh.part_release_notes(home, base, out, update=False, today=today)
    assert refresh.rn_state(home).read_text() != state                                  # the update run moved the day
    last = refresh.rn_state(home).read_text()
    refresh.part_release_notes(home, base, out, update=False, today=today)
    assert refresh.rn_state(home).read_text() == last


def test_an_empty_mirror_is_overdue_and_a_failed_fetch_is_said(home, tmp_path, monkeypatch):
    base = tmp_path / "mirrors"
    part = refresh.part_release_notes(home, base, tmp_path / "o", update=False, today=datetime.date(2026, 10, 19))
    assert part.overdue == 1 and "run awb mirror rn" in part.lines[0]

    def broken(url):
        raise urllib.error.URLError("x")
    monkeypatch.setattr(mirror, "_fetch_page", broken)
    part = refresh.part_release_notes(home, base, tmp_path / "o", update=True, today=datetime.date(2026, 10, 19))
    assert part.overdue == 2 and "not updated" in part.lines[0]


def test_the_part_runs_in_the_refresh_between_mirrors_and_prices(home, tmp_path, monkeypatch):
    monkeypatch.setenv("AWB_MIRRORS", str(tmp_path / "mirrors"))
    assert refresh.PARTS.index("mirrors") + 1 == refresh.PARTS.index("release-notes") == refresh.PARTS.index("prices") - 1
    done, report = refresh.run(home, parts=("release-notes",), update=False)
    assert done[0].name == "release-notes" and report.name == "REPORT-release-notes.md"
