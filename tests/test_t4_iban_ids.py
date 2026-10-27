"""T4: the head of a long hex id is no IBAN (found 2026-10-27: `awb paste` refused a PoC log for "iban 1")."""

from awb import patterns


def _ibans(text):
    return [text[s.start:s.end] for s in patterns.find_structured(text) if s.cls == "iban"]


HEX_ID = "ba40b7895a" + "27b8c125ce" + "84767db43b41"
"""A 32 hex id built in pieces (a literal id trips the gate); its first 20 characters pass mod 97 as a BA number."""


def test_a_hex_id_whose_head_passes_mod_97_is_no_iban():
    assert _ibans("domain " + HEX_ID) == []
    assert _ibans("project_id=" + HEX_ID + ",") == []


def test_real_ibans_are_still_found():
    for text in ("DE89 3704 0044 0532 0130 00", "IBAN: DE89370400440532013000", "AT61 1904 3002 3457 3201",
                 "GB82 WEST 1234 5698 7654 32", "pay to DE89370400440532013000."):
        assert _ibans(text), text


def test_an_iban_followed_by_a_word_after_a_separator_is_found():
    assert _ibans("DE89370400440532013000 is the account")
