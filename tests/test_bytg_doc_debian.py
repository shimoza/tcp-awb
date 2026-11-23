"""awb doc fetch of a Debian page (tcp-bytg ask of 2026-11-23): debian.org was on rules/allow-domains.txt, but every
page redirects to www.debian.org or wiki.debian.org, which were not, so no Debian lifecycle page could be read. The
two hosts are lines of the repository list now; the host rule stays exact (no suffix match)."""
from __future__ import annotations

import pytest

from awb.tcp import doc
from tests.test_doc import Getter, html_page


def test_bytg_the_redirect_of_debian_org_to_www_is_followed():
    g = Getter({("debian.org", "/releases/"): (302, {"location": "https://www.debian.org/releases/"}, b""),
                ("www.debian.org", "/releases/"): html_page()})
    page = doc.fetch("https://debian.org/releases/", getter=g, hosts=doc.allowed_hosts())
    assert page["status"] == 200 and page["host"] == "www.debian.org"


@pytest.mark.parametrize("host", ["www.debian.org", "wiki.debian.org"])
def test_bytg_the_debian_hosts_are_on_the_list(host):
    assert doc.check_url("https://%s/LTS" % host, doc.allowed_hosts())[0] == host


def test_bytg_the_host_rule_stays_exact():
    with pytest.raises(doc.DocError):
        doc.check_url("https://lists.debian.org/x", doc.allowed_hosts())
