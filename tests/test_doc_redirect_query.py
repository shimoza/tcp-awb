"""awb doc fetch follows a redirect whose target carries a query string when the host is allowed (B-082).

The first URL still takes no query. The fake getter of tests/test_doc.py answers by (host, path with query).
"""
from __future__ import annotations

import pytest

from awb.tcp import doc
from tests.test_doc import HOSTS, Getter, html_page


def test_a_redirect_with_a_query_to_an_allowed_host_is_followed_and_the_page_records_it():
    g = Getter({
        ("example.com", "/start"): (302, {"location": "https://docs.example.org/dl?id=7&v=2"}, b""),
        ("docs.example.org", "/dl?id=7&v=2"): html_page("<p>landed</p>"),
    })
    page = doc.fetch("https://example.com/start", getter=g, hosts=HOSTS)
    assert page["url"] == "https://docs.example.org/dl?id=7&v=2"
    assert page["path"] == "/dl" and page["text"].strip() == "landed"
    assert g.asked[-1] == ("docs.example.org", "/dl?id=7&v=2")


def test_a_relative_redirect_with_a_query_is_followed():
    g = Getter({
        ("example.com", "/start"): (301, {"location": "/next?a=1"}, b""),
        ("example.com", "/next?a=1"): html_page("<p>here</p>"),
    })
    assert doc.fetch("https://example.com/start", getter=g, hosts=HOSTS)["url"] == "https://example.com/next?a=1"


@pytest.mark.parametrize("target", ["https://attacker.example.net/c?x=1", "https://attacker.example.net/c"])
def test_a_redirect_off_the_lists_is_refused_with_or_without_a_query(target):
    g = Getter({("example.com", "/start"): (302, {"location": target}, b"")})
    with pytest.raises(doc.DocError, match="redirect to a URL the rules refuse"):
        doc.fetch("https://example.com/start", getter=g, hosts=HOSTS)
    assert all(host != "attacker.example.net" for host, _ in g.asked)


def test_the_first_url_still_takes_no_query():
    g = Getter({})
    with pytest.raises(doc.DocError, match="no query string"):
        doc.fetch("https://example.com/a?x=1", getter=g, hosts=HOSTS)
    assert g.asked == []


def test_a_redirect_query_with_a_character_that_is_not_url_safe_is_refused():
    g = Getter({("example.com", "/start"): (302, {"location": "https://example.com/c?x=<b>"}, b"")})
    with pytest.raises(doc.DocError, match="redirect to a URL the rules refuse"):
        doc.fetch("https://example.com/start", getter=g, hosts=HOSTS)


def test_the_redirect_limit_holds_with_queries():
    pages = {("example.com", "/0" if n == 0 else "/%d?q=%d" % (n, n)): (302, {"location": "/%d?q=%d" % (n + 1, n + 1)}, b"")
             for n in range(6)}
    pages[("example.com", "/3?q=3")] = html_page("<p>three</p>")
    assert doc.fetch("https://example.com/0", getter=Getter(pages), hosts=HOSTS)["text"].strip() == "three"
    pages[("example.com", "/3?q=3")] = (302, {"location": "/4?q=4"}, b"")
    with pytest.raises(doc.DocError, match="more than 3 redirects"):
        doc.fetch("https://example.com/0", getter=Getter(pages), hosts=HOSTS)
