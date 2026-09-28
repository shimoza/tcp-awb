"""Structured data patterns. Every example is a documentation example or an invented fixture value."""
from __future__ import annotations

import pytest

from awb import patterns
from tests import fixtures


def kinds(text: str) -> list[str]:
    return [s.cls for s in patterns.find_structured(text)]


def values(text: str) -> list[str]:
    return [text[s.start:s.end] for s in patterns.find_structured(text)]


@pytest.mark.parametrize("text,cls,value", [
    ("Mail an tobias.beispielmann@%s bitte" % fixtures.CUSTOMER_DOMAIN, "mail", "tobias.beispielmann@%s" % fixtures.CUSTOMER_DOMAIN),
    ("see https://www.%s/angebot/v2." % fixtures.CUSTOMER_DOMAIN, "url", "https://www.%s/angebot/v2" % fixtures.CUSTOMER_DOMAIN),
    ("host %s answers" % fixtures.CUSTOMER_DOMAIN, "url", fixtures.CUSTOMER_DOMAIN),
    ("zone intern.zyxwo.example is new", "url", "intern.zyxwo.example"),
    ("ping 203.0.113.7 now", "ip", "203.0.113.7"),
    ("route 198.51.100.0/24 via gw", "ip", "198.51.100.0/24"),
    ("v6 2001:db8::1 is up", "ip", "2001:db8::1"),
    ("v6 2001:0db8:0000:0000:0000:ff00:0042:8329 full", "ip", "2001:0db8:0000:0000:0000:ff00:0042:8329"),
    ("mac 00:1A:2B:3C:4D:5E on eth0", "mac", "00:1A:2B:3C:4D:5E"),
    ("mac 00-1a-2b-3c-4d-5e on eth1", "mac", "00-1a-2b-3c-4d-5e"),
    ("IBAN: DE89 3704 0044 0532 0130 00.", "iban", "DE89 3704 0044 0532 0130 00"),
    ("IBAN DE89370400440532013000 end", "iban", "DE89370400440532013000"),
    ("GB82 WEST 1234 5698 7654 32 is fine", "iban", "GB82 WEST 1234 5698 7654 32"),
    ("BIC: MARKDEF1100", "bic", "MARKDEF1100"),
    ("SWIFT COBADEFFXXX", "bic", "COBADEFFXXX"),
    ("BIC DEUTDEFF ok", "bic", "DEUTDEFF"),
    ("USt-IdNr. DE123456789", "vat", "DE123456789"),
    ("UID ATU12345678", "vat", "ATU12345678"),
    ("btw NL123456789B01", "vat", "NL123456789B01"),
    ("Tel.: +49 40 123456-78", "phone", "+49 40 123456-78"),
    ("Tel. 040 123456-78", "phone", "040 123456-78"),
    ("Mobil: 0171/1234567", "phone", "0171/1234567"),
    ("Fax 030 1234567 bitte", "phone", "030 1234567"),
    ("+44 20 7946 0958 London", "phone", "+44 20 7946 0958"),
    ("Rufnummer 0301234567 im Text", "phone", "0301234567"),
    ("the host at 203.0.113.45 answered", "ip", "203.0.113.45"),
    ("section 4 names 203.0.113.9 as the peer", "ip", "203.0.113.9"),
    ("Amtsgericht, HRB 99999 B", "hrb", "HRB 99999 B"),
    ("HRA 12345", "hrb", "HRA 12345"),
    ("St.-Nr. 12/345/67890", "tax", "12/345/67890"),
    ("Steuernummer: 2181508150123", "tax", "2181508150123"),
])
def test_found(text, cls, value):
    spans = patterns.find_structured(text)
    got = [(s.cls, text[s.start:s.end]) for s in spans]
    assert (cls, value) in got, got


@pytest.mark.parametrize("text", [
    "metadata 169.254.169.254 answers",
    "internal endpoint 100.125.203.17",
    "carrier grade 100.64.1.1",
    "private 10.0.0.1 and 192.168.1.1 and 172.20.0.5",
    "loopback 127.0.0.1 and ::1 and fe80::1",
    "docs at docs.aws.amazon.com and learn.microsoft.com",
    "https://learn.microsoft.com/en-us/azure/ is public",
    "version 2.1.220 of the client",
    "on 22.09.2026 we met",
    "costs 1.234,56 EUR per month",
    "a share of 15 % and 0.5 %",
    "at 12:30:45 local time",
    "std::vector and a::b in code",
    "files report.md setup.py main.tf awb.cli config.yaml",
    "DEUTSCHE BAHN and PASSWORD in capitals",
    "WAF error codes 010000 and 080263 in a response",
    "the job returned 0123456 as its handle",
    "the service description section 3.1.4.2 lists the host types",
    "clause 6.2.1.13 of the contract sets the limit",
    "Abschnitt 2.4.1.9 nennt die Grenze",
    "wrong length IBAN DE89 3704 0044 0532 0130 0",
    "wrong checksum IBAN DE88 3704 0044 0532 0130 00",
    "the codes CUST-Q7M4 and tcp-q7m4 stay",
    "an order number 4711 and page 12",
])
def test_not_found(text):
    assert patterns.find_structured(text) == [], values(text)


def test_spans_sorted_and_not_overlapping():
    text = ("Kontakt tobias.beispielmann@%s, https://www.%s/x, Tel +49 40 123456-78, "
            "IBAN DE89 3704 0044 0532 0130 00, 203.0.113.7" % (fixtures.CUSTOMER_DOMAIN, fixtures.CUSTOMER_DOMAIN))
    spans = patterns.find_structured(text)
    assert [s.cls for s in spans] == ["mail", "url", "phone", "iban", "ip"]
    for a, b in zip(spans, spans[1:]):
        assert a.end <= b.start


def test_mail_wins_over_its_domain():
    text = "write to a.b@%s" % fixtures.CUSTOMER_DOMAIN
    assert kinds(text) == ["mail"]


def test_allow_list_is_loaded_from_rules():
    assert "learn.microsoft.com" in patterns.ALLOW_DOMAINS
    assert all(not d.startswith("#") for d in patterns.ALLOW_DOMAINS)


def test_spans_carry_no_value():
    s = patterns.find_structured("mail x.y@%s" % fixtures.CUSTOMER_DOMAIN)[0]
    assert s.code is None and s.form is None


def test_country_tlds_make_a_bare_domain():
    # the country and generic TLDs stay in the list, the test texts above use documentation TLDs only
    tlds = patterns._TLDS.split("|")
    for tld in ("de", "at", "ch", "eu", "com", "example"):
        assert tld in tlds


def test_mail_at_an_ip_literal_is_one_mail():
    local = fixtures.PLANTED_PERSON.split()[1].lower()
    for text, value in (("an %s@203.0.113.9 senden" % local, "%s@203.0.113.9" % local),
                        ("an %s@[198.51.100.7] senden" % local, "%s@[198.51.100.7]" % local)):
        spans = patterns.find_structured(text)
        assert [(s.cls, text[s.start:s.end]) for s in spans] == [("mail", value)]
