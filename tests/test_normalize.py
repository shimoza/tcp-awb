"""Tests for awb.normalize. Only invented names from tests/fixtures.py appear here."""
from __future__ import annotations

from awb import normalize as nz
from tests import fixtures

SHORT = fixtures.CUSTOMER_FORMS[1]
FULL = fixtures.CUSTOMER_FORMS[0]


def _span_of(n: nz.Normalized, word: str) -> tuple[int, int]:
    i = n.text.index(word)
    return nz.original_span(n, i, i + len(word))


def _check_shape(n: nz.Normalized) -> None:
    assert len(n.to_original) == len(n.text)
    assert len(n.to_original_end) == len(n.text)
    assert all(a < b for a, b in zip(n.to_original, n.to_original_end))
    assert n.to_original == sorted(n.to_original)


def test_plain_text_unchanged():
    n = nz.normalize("kunde " + FULL + "\nzeile 2\tmit tab")
    _check_shape(n)
    assert n.text == "kunde " + FULL + "\nzeile 2\tmit tab"
    assert n.to_original == list(range(len(n.text)))
    assert _span_of(n, FULL) == (6, 6 + len(FULL))


def test_empty_text():
    n = nz.normalize("")
    assert n.text == "" and n.to_original == []
    assert nz.original_span(n, 0, 0) == (0, 0)


def test_soft_hyphen_and_zero_width_removed():
    raw = "Zy­x​w‌o‍﻿⁠"
    n = nz.normalize(raw)
    _check_shape(n)
    assert n.text == SHORT
    assert n.to_original == [0, 1, 3, 5, 7]
    assert nz.original_span(n, 0, 5) == (0, 8)
    # an empty span at the end sits just after the last original character that survived
    assert nz.original_span(n, 5, 5) == (8, 8)


def test_nbsp_and_other_spaces_become_space():
    raw = "a b c　d e"
    n = nz.normalize(raw)
    _check_shape(n)
    assert n.text == "a b c d e"
    assert n.to_original == list(range(9))
    n = nz.normalize("x y zw")
    assert n.text == "x\ny\nz\nw"


def test_crlf_and_lone_cr_become_lf():
    n = nz.normalize("a\r\nb\rc\n")
    _check_shape(n)
    assert n.text == "a\nb\nc\n"
    assert nz.original_span(n, 1, 2) == (1, 3)
    assert nz.original_span(n, 3, 4) == (4, 5)


def test_hyphenation_rejoined():
    n = nz.normalize("Logis-\ntik")
    assert n.text == "Logistik"
    assert nz.original_span(n, 0, 8) == (0, 10)
    n = nz.normalize("Logis- \n  tik")
    assert n.text == "Logistik"
    assert nz.original_span(n, 0, 8) == (0, 13)
    # soft hyphen and U+2010 at the line end count as hyphenation too
    assert nz.normalize("Logis­\ntik").text == "Logistik"
    assert nz.normalize("Logis‐\ntik").text == "Logistik"
    # a capital on the left with a lower case right side is rejoined as well
    assert nz.normalize("ABC-\ndef").text == "ABCdef"
    # several in a row
    assert nz.normalize("Lo-\ngis-\ntik").text == "Logistik"


def test_hyphenation_kept_when_right_side_is_not_lower_case():
    for raw in ("steuer-\nRecht", "steuer-\n2024", "steuer-\n\nrecht", "steuer -\nrecht", "1-\nfach"):
        n = nz.normalize(raw)
        assert n.text == raw, raw
        _check_shape(n)


def test_html_entities_decoded():
    n = nz.normalize("Pr&auml;zision &#246; &#xF6; &#XF6; &amp; &nbsp;x &zzzz; &auml")
    _check_shape(n)
    assert n.text == "Präzision ö ö ö &  x &zzzz; &auml"
    assert _span_of(n, "Prä") == (0, 8)
    # "&#246;" starts after "Pr&auml;zision " (15 characters) and is 6 characters long
    assert nz.original_span(n, 10, 11) == (15, 21)
    assert nz.original_span(n, 9, 10) == (14, 15)
    assert nz.original_span(n, 12, 13) == (22, 28)


def test_url_encoding_decoded():
    n = nz.normalize("%C3%B6l %20 100% 5%25 %C3 x%41y")
    _check_shape(n)
    assert n.text == "öl   100% 5% %C3 xAy"
    assert nz.original_span(n, 0, 1) == (0, 6)
    assert nz.original_span(n, 0, 2) == (0, 7)
    # "5%25" starts after "%C3%B6l %20 100% " (17 characters)
    assert _span_of(n, "5%") == (17, 21)
    assert _span_of(n, "A") == (27, 30)
    # control characters are not decoded, one exotic pair stays as it is
    assert nz.normalize("a%00b%0Ac").text == "a%00b\nc"
    # two multi byte characters in one run map to their own bytes
    n = nz.normalize("%C3%B6%C3%A4")
    assert n.text == "öä"
    assert nz.original_span(n, 1, 2) == (6, 12)


def test_nfc_composes_and_tracks_positions():
    raw = "Qvörtz"
    n = nz.normalize(raw)
    _check_shape(n)
    assert n.text == "Qvörtz"
    assert n.to_original == [0, 1, 2, 4, 5, 6]
    assert nz.original_span(n, 0, 6) == (0, 7)
    assert nz.original_span(n, 2, 3) == (2, 4)
    # an entity that yields a combining mark is composed after decoding
    assert nz.normalize("o&#776;").text == "ö"


def test_positions_map_back_through_every_change():
    raw = "Kunde: Zy­xwo Logis-\r\ntik Pr&auml;zision %C3%B6l ende"
    n = nz.normalize(raw)
    _check_shape(n)
    assert n.text == "Kunde: Zyxwo Logistik Präzision öl ende"
    for word, original in (
        (SHORT, "Zy­xwo"),
        ("Logistik", "Logis-\r\ntik"),
        ("Präzision", "Pr&auml;zision"),
        ("öl", "%C3%B6l"),
        ("ende", "ende"),
        ("Kunde", "Kunde"),
    ):
        a, b = _span_of(n, word)
        assert raw[a:b] == original, word
    # a span across several changes covers the whole original stretch
    i = n.text.index(SHORT)
    j = n.text.index("öl") + 2
    a, b = nz.original_span(n, i, j)
    assert raw[a:b] == "Zy­xwo Logis-\r\ntik Pr&auml;zision %C3%B6l"
    # positions past the end are clamped
    assert nz.original_span(n, 0, 10_000)[1] == len(raw)


def test_original_span_without_end_map_falls_back():
    n = nz.Normalized("abc", [0, 2, 4])
    assert nz.original_span(n, 0, 3) == (0, 5)
    assert nz.original_span(n, 1, 2) == (2, 3)
    assert nz.original_span(n, 3, 3) == (5, 5)


def test_name_with_nbsp_and_soft_hyphen_becomes_the_form():
    raw = FULL.replace(" ", " ", 1).replace("o", "o­", 1)
    n = nz.normalize(raw)
    assert n.text == FULL
    assert nz.original_span(n, 0, len(FULL)) == (0, len(raw))


# --------------------------------------------------------------------------- review findings: invisible characters
# and encodings the red team used to get a form past the matcher


def test_every_default_ignorable_character_is_removed():
    for invisible in ("⁣", "⁢", "͏", "‎", "᠎", "︀", "\U000e0020", "‮"):
        raw = invisible.join(SHORT) + invisible
        n = nz.normalize(raw)
        _check_shape(n)
        assert n.text == SHORT, repr(invisible)
        assert nz.original_span(n, 0, len(SHORT)) == (0, len(raw) - len(invisible))


def test_escapes_and_encodings_are_decoded():
    org = fixtures.ORG_FORMS[0]
    b64 = __import__("base64").b64encode(FULL.encode("utf-8")).decode("ascii")
    cases = [
        ('{"kunde": "\\u005A%s"}' % SHORT[1:], '{"kunde": "%s"}' % SHORT),
        ("x " + "".join("\\u%04x" % ord(c) for c in org) + " y", "x %s y" % org),
        ("\\ud83d\\ude00 ok", "\U0001F600 ok"),
        (org.replace("ö", "\\'f6").replace("ä", "\\'e4"), org),
        (org.replace("ö", "=C3=B6").replace("ä", "=C3=A4"), org),
        (org.replace("ö", "=F6"), org),
        ("=?utf-8?b?%s?=" % b64, FULL),
        ("=?iso-8859-1?q?%s?=" % org.replace("ö", "=F6").replace("ä", "=E4").replace(" ", "_"), org),
        ("?q=" + org.replace("ö", "%F6").replace("ä", "%E4").replace(" ", "%20"), "?q=" + org),
        ("%25" + "%25".join("%02X" % ord(c) for c in SHORT), SHORT),
    ]
    for raw, want in cases:
        n = nz.normalize(raw)
        _check_shape(n)
        assert n.text == want, raw


def test_encodings_that_are_not_text_stay():
    for raw in ("color=FF0000 a=3D1", "x=A4 y", "\\ud800 alone", "\\u0007 bell", "=?utf-8?b?!!?=", "50%F6 x"):
        assert nz.normalize(raw).text == raw, raw
