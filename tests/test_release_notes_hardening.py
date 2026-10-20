"""K-hardening: the findings K6 K12 K13 K23 K24 of the attacker review of 2026-10-20 for the release notes. Pages made
here by hand stand in for the site; nothing touches the network."""
from __future__ import annotations

import http.client
import io
import urllib.error

import pytest

from awb.tcp import mirror

PAGE = "https://www.t-cloud-public.com/en/support/release-notes"


@pytest.fixture(autouse=True)
def the_site(monkeypatch):
    monkeypatch.setattr(mirror, "RN_PAGE", PAGE)


def article(date, title, slug, text="text", services=("Key Management Service",)):
    tags = "".join('<a href="%s?tag=%d" class="tag link-app">%s</a>' % (PAGE, i, s) for i, s in enumerate(services))
    return ('<article class="page-teaser"><time datetime="%s">x</time><h2>%s</h2><p>%s</p>%s'
            '<a class="teaser-link" href="/en/support/release-notes/%s">more</a></article>'
            % (date, title, text, tags, slug))


def page(*articles, pager=""):
    return "<html><body>%s<nav>%s</nav></body></html>" % ("".join(articles), pager)


class Opener:
    def __init__(self, resp):
        self.resp = resp

    def open(self, req, timeout=0):
        return self.resp


def sync(tmp_path, fetch, **kw):
    return mirror.sync_release_notes(tmp_path, fetch=fetch, sleep=lambda s: None, **kw)


# K6

def test_a_pager_link_to_a_huge_page_does_not_lead_the_run_past_the_cap(tmp_path):
    urls = []

    def fetch(url):
        urls.append(url)
        n = len(urls)
        return page(article("2026-10-%02d" % (n % 28 + 1), "title %d" % n, "note-%d" % n),
                    pager='<a href="%s?p=999999">last</a>' % PAGE)

    r = sync(tmp_path, fetch)
    assert r["pages"] == mirror.RN_MAX_PAGES == len(urls)


def test_a_page_larger_than_the_cap_is_refused(tmp_path, monkeypatch):
    class Resp(io.BytesIO):
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    monkeypatch.setattr(mirror, "RN_MAX_BYTES", 1000)
    monkeypatch.setattr(mirror, "_OPENER", Opener(Resp(b"x" * 5000)))
    with pytest.raises(mirror.MirrorError, match="larger"):
        mirror._fetch_page(PAGE)


def test_a_page_that_trickles_past_the_deadline_is_refused(monkeypatch):
    class Resp(io.BytesIO):
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    clock = iter(range(0, 1000, 100))
    monkeypatch.setattr(mirror.time, "monotonic", lambda: next(clock))
    monkeypatch.setattr(mirror, "_OPENER", Opener(Resp(b"x" * 200000)))
    with pytest.raises(mirror.MirrorError, match="seconds"):
        mirror._fetch_page(PAGE)


def test_titles_texts_and_tags_are_cut_to_their_caps():
    html = page(article("2026-10-16", "t" * 5000, "long", text="p" * 50000,
                        services=["s" * 500] * 40))
    notes, _ = mirror.parse_release_notes(html, PAGE)
    n = notes[0]
    assert len(n["title"]) == mirror.RN_MAX_TITLE and len(n["text"]) == mirror.RN_MAX_TEXT
    assert len(n["services"]) == mirror.RN_MAX_TAGS and len(n["services"][0]) == mirror.RN_MAX_TAG


def test_a_page_holds_at_most_the_capped_number_of_notes():
    html = page(*[article("2026-10-16", "title", "n-%d" % i) for i in range(mirror.RN_MAX_NOTES + 50)])
    notes, _ = mirror.parse_release_notes(html, PAGE)
    assert len(notes) == mirror.RN_MAX_NOTES


# K12

def test_a_pager_number_of_thousands_of_digits_is_ignored_and_the_run_goes_on(tmp_path):
    html = page(article("2026-10-16", "title", "one"), pager='<a href="%s?p=%s">x</a>' % (PAGE, "9" * 5000))
    r = sync(tmp_path, lambda url: html)
    assert r["new"] == 1 and r["pages"] == 1


@pytest.mark.parametrize("exc", [http.client.IncompleteRead(b"ab"), http.client.RemoteDisconnected("cut"),
                                 OSError("reset")])
def test_a_cut_body_or_a_reset_is_a_mirror_error_that_names_the_page(tmp_path, exc):
    def fetch(url):
        raise exc

    with pytest.raises(mirror.MirrorError, match="could not be fetched"):
        sync(tmp_path, fetch)


def test_the_refresh_part_reports_a_cut_body_and_goes_on(tmp_path, monkeypatch, home):
    import datetime

    from awb.tcp import refresh

    def cut(url):
        raise http.client.IncompleteRead(b"")

    monkeypatch.setattr(mirror, "_fetch_page", cut)
    part = refresh.part_release_notes(home, tmp_path / "mirrors", tmp_path / "out", update=True,
                                      today=datetime.date(2026, 10, 19))
    assert any("not updated" in line for line in part.lines) and part.overdue >= 1


# K23

@pytest.mark.parametrize("date", ["٢٠٣٠-٠١-٠١", "2026-13-40", "2026-02-30", "2026-1-01"])
def test_a_date_in_other_digits_or_off_the_calendar_leaves_the_note_out(date):
    notes, _ = mirror.parse_release_notes(page(article(date, "title", "odd")), PAGE)
    assert notes == []


def test_a_plain_date_is_kept():
    notes, _ = mirror.parse_release_notes(page(article("2026-02-28", "title", "fine")), PAGE)
    assert [n["id"] for n in notes] == ["2026-02-28-fine"]


# K24

@pytest.mark.parametrize("target", ["http://www.t-cloud-public.com/x", "https://169.254.169.254/latest",
                                    "https://other.example/x", "ftp://www.t-cloud-public.com/x"])
def test_a_redirect_off_the_site_or_to_plain_http_is_not_followed(target):
    req = mirror.urllib.request.Request(PAGE)
    assert mirror._SameSite().redirect_request(req, None, 302, "found", {}, target) is None


def test_a_redirect_on_the_same_host_over_https_is_followed():
    req = mirror.urllib.request.Request(PAGE)
    got = mirror._SameSite().redirect_request(req, None, 301, "moved", {}, "/en/support/release-notes/")
    assert got is not None and got.full_url == PAGE + "/"


def test_the_fetch_goes_through_the_same_site_opener():
    assert any(isinstance(h, mirror._SameSite) for h in mirror._OPENER.handlers)


# K13

def test_a_title_and_a_tag_with_escapes_or_other_characters_reach_the_lines_as_plain_ascii(home):
    import datetime

    from awb import kb, offered
    from awb.tcp import refresh

    n = {"id": "2026-10-16-odd", "date": "2026-10-16", "title": "ignore \x1b[2J this\x9b and ‮ more",
         "text": "text", "services": ["gadget \x1b]0;x\x07 hub", "z" * 300]}
    got = refresh.release_note_lines([n], kb.load(home), offered.load(), today=datetime.date(2026, 10, 19))
    assert len(got) == 2
    for line in got:
        assert all(" " <= c <= "~" for c in line) and len(line) < 200
    assert "ignore ?[2J this? and ? more" in got[0]
