"""Public T Cloud Public service host names pass the name check in knowledge base entries (task TH): the API short
form, the VPC endpoint service name and every host under the platform domains. Anything else stays a finding, and a
block of `awb kb verify` is recorded for the false-positive learning. Invented names of tests/fixtures.py only."""
from __future__ import annotations

import datetime
import os
from pathlib import Path

import pytest

from awb import fplearn, kb, patterns
from tests import fixtures

HOSTS = (
    "nat.eu-de", "csms.eu-de", "csms.eu-de.otc.t-systems.com", "kms.eu-de", "aom.eu-de",
    "com.t-systems.otc.eu-de.apig", "com.t-systems.otc.eu-de.dns", "com.t-systems.otc.eu-de.obs",
)
EPOCH = datetime.datetime(2000, 1, 1, tzinfo=datetime.timezone.utc)
HIT = "carries hits of the name check"


def _classes(text: str) -> list[str]:
    return [s.cls for s in patterns.find_structured(text)]


def _blocked(text: str, register_path: Path) -> bool:
    return HIT in " ".join(kb.text_problems("the statement", text, register_path))


@pytest.mark.parametrize("host", HOSTS)
def test_a_public_tcp_host_is_no_finding(host):
    for text in ("The service answers at %s." % host, "%s:" % host, "(%s)" % host, "see %s/v1/x" % host,
                 "Host %s, port 443" % host):
        assert _classes(text) == [], text


@pytest.mark.parametrize("host", HOSTS)
def test_a_public_tcp_host_passes_statement_source_and_tried(host, register_path):
    assert kb.text_problems("the statement", "The endpoint %s is reachable in eu-de" % host, register_path) == []
    assert kb.text_problems("the source", "docs mirror, %s" % host, register_path, language=False) == []
    assert kb.text_problems("tried 1", "call to %s" % host, register_path) == []


def test_other_hosts_and_mails_still_block(register_path):
    for text in ("upload to files.example-customer.de now", "write to ops@example-customer.de",
                 "x.eu-de.evil.com", "nat.eu-de.evil.com", "https://files.example-customer.de/a"):
        assert _blocked(text, register_path), text


def test_a_name_glued_into_a_tcp_host_or_next_to_it_still_blocks(register_path):
    person = fixtures.PLANTED_PERSON
    first, last = (w.lower() for w in person.split()[:2])
    for text in ("%s.nat.eu-de" % last, "%s-nat.eu-de" % last, "%s.%s.example-customer.de" % (first, last),
                 "nat.eu-de is run by %s" % fixtures.PERSON_FORMS[0]):
        assert _blocked(text, register_path), text


def test_the_pattern_is_exact():
    for text in ("n.eu-de", "natnatnatnatn.eu-de", "nat.eu-fr", "nat.eu-de.example-customer.de",
                 "com.t-systems.otc.eu-fr.dns"):
        assert "url" in _classes(text), text


def test_a_kb_verify_block_lands_in_the_fp_log_and_in_no_work_user_file(home):
    host = "files.example-customer.de"
    assert kb.text_problems("the statement", "The share is at %s today" % host, home.register)
    rows = fplearn.read_blocks(home.vault, EPOCH)
    assert [(r["source"], r["class"], r["string"]) for r in rows] == [("kb", "url", host)]
    # the string sits on the owner's side alone: the shared side and the knowledge base hold none
    for root in (home.shared, Path(os.environ["AWB_KB"])):
        for f in root.rglob("*"):
            if f.is_file():
                assert host not in f.read_text(encoding="utf-8", errors="ignore"), f
