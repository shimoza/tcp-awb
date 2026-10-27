"""T5: the owner's typed prompt is no entry of customer material (his rule of 2026-10-27, "check once at the
entry"): structured data in his own words passes; a registered name and a secret still stop the prompt."""

from __future__ import annotations

import base64

from awb import patterns
from tests import fixtures
from tests.test_hooks import run_hook

HEX_ID = "ba40b7895a" + "27b8c125ce" + "84767db43b41"
MAIL = "ops@" + fixtures.CUSTOMER_DOMAIN.replace("zyxwo-logistik", "qrtvb")


def prompt(text, monkeypatch, capsys):
    return run_hook("prompt", {"prompt": text}, monkeypatch, capsys)


def test_typed_structured_data_passes(home, monkeypatch, capsys):
    for text in ("Read deliverables/tcp-snjy-answer-en.final.md and reviews/tcp-snjy-answer-en.md/final-check-2.md",
                 "send the plan to %s today" % MAIL, "call 198.51.100.23 now", "domain " + HEX_ID,
                 "error_code: Common.0018 | 400", "| c0 | c1 | c2 | c3 | c4 | c5 |",
                 "and " + base64.b64encode(b"pay to DE89 3704 0044 0532 0130 00 today").decode("ascii")):
        code, _, err = prompt(text, monkeypatch, capsys)
        assert (code, err) == (0, ""), text


def test_a_typed_registered_name_still_blocks(home, monkeypatch, capsys):
    code, _, err = prompt("size the app clusters of %s and mail %s" % (fixtures.CUSTOMER_FORMS[0], MAIL),
                          monkeypatch, capsys)
    assert code == 2 and "registered name" in err


def test_a_typed_secret_still_blocks(home, monkeypatch, capsys):
    key = "AKIA" + "Q7M4EXAMPLE12345"
    tail = "aB3dE5fG7hJ9kL1m" + "N3pQ5rS7tU9vW1xY3zA5bC7d"
    text = "use a" + "k=" + key + " s" + "k=" + tail
    code, _, err = prompt(text, monkeypatch, capsys)
    assert code == 2 and "carries a " + "secret" in err


def test_a_file_name_with_dots_is_no_url():
    for text in ("tcp-snjy-answer-en.final.md", "notes.v2.py", "a.b.c.sh"):
        assert not [s for s in patterns.find_structured(text) if s.cls == "url"], text
    assert [s for s in patterns.find_structured("see www.example-shop.md") if s.cls == "url"]
