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
    monkeypatch.setattr(mirror.urllib.request, "urlopen", lambda req, timeout=0: Resp(b"x" * 5000))
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
    monkeypatch.setattr(mirror.urllib.request, "urlopen", lambda req, timeout=0: Resp(b"x" * 200000))
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
