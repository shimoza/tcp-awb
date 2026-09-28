"""Tests for awb.matcher. Only invented names from tests/fixtures.py appear here."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from awb import matcher, register
from tests import fixtures

CUST = fixtures.CUSTOMER_CODE
FULL, SHORT, ACRONYM, ENGLISH = fixtures.CUSTOMER_FORMS
REF_FILE = CUST + "-REF-1"
REF_TENDER = CUST + "-REF-2"

# (label, text, expected hits as (matched text, code)); every planted pattern from the build task
PLANTED = [
    ("full legal name", "angebot an " + FULL + " vom mai", [(FULL, CUST)]),
    ("upper case", FULL.upper(), [(FULL.upper(), CUST)]),
    ("lower case", FULL.lower(), [(FULL.lower(), CUST)]),
    ("short form alone", "der kunde " + SHORT + " bestellt", [(SHORT, CUST)]),
    ("acronym", "intern als " + ACRONYM + " bekannt", [(ACRONYM, CUST)]),
    ("english form", "trading as " + ENGLISH + " abroad", [(ENGLISH, CUST)]),
    ("genitive with s", "das netz " + SHORT + "s ist alt", [(SHORT + "s", CUST)]),
    ("genitive with apostrophe", "das netz von " + SHORT + "'s ist alt", [(SHORT + "'s", CUST)]),
    ("split over a line break", "kunde\n" + FULL.replace(" ", "\n", 1) + "\nende", [(FULL.replace(" ", "\n", 1), CUST)]),
    ("double space", "kunde " + FULL.replace(" ", "  ", 1) + " ende", [(FULL.replace(" ", "  ", 1), CUST)]),
    ("nbsp inside", "kunde " + FULL.replace(" ", " ", 1) + " ende", [(FULL.replace(" ", " ", 1), CUST)]),
    ("file name", "1-26-0999_Angebotsaufforderung_Zyxwo.pdf", [("26-0999", REF_FILE), ("Zyxwo", CUST)]),
    ("hostname", "host vpc-zyxwo-prod ist erreichbar", [("zyxwo", CUST)]),
    ("lower case glued", "job zyxwologistik-backup failed", [("zyxwo", CUST)]),
    ("camel case", "server ZyxwoProd01 rebooted", [("Zyxwo", CUST)]),
    ("url path", "https://portal.example/zyxwo/login?x=1", [("zyxwo", CUST)]),
    ("before a full stop", "wir danken " + SHORT + ". weiter", [(SHORT, CUST)]),
    ("in brackets", "der kunde (" + SHORT + ") hat", [(SHORT, CUST)]),
    ("in quotes", 'der kunde "' + SHORT + '" hat', [(SHORT, CUST)]),
    ("person", "ansprechpartner: " + fixtures.PERSON_FORMS[0], [(fixtures.PERSON_FORMS[0], fixtures.PERSON_CODE)]),
    ("surname alone", "grüße an " + fixtures.PERSON_FORMS[1] + ",", [(fixtures.PERSON_FORMS[1], fixtures.PERSON_CODE)]),
    ("place", "standort " + fixtures.PLACE_FORMS[0] + " süd", [(fixtures.PLACE_FORMS[0], fixtures.PLACE_CODE)]),
    ("law firm with ampersand", "vertreten durch " + fixtures.LAWFIRM_FORMS[0] + ",", [(fixtures.LAWFIRM_FORMS[0], fixtures.LAWFIRM_CODE)]),
    ("umlaut written oe", "lieferant Qvoertz liefert", [("Qvoertz", fixtures.ORG_CODE)]),
    ("umlaut written o", "lieferant Qvortz liefert", [("Qvortz", fixtures.ORG_CODE)]),
    ("tender id", "vergabe " + fixtures.TENDER_ID + " offen", [(fixtures.TENDER_ID, REF_TENDER)]),
    ("file number", "aktenzeichen " + fixtures.FILE_NUMBER + " offen", [(fixtures.FILE_NUMBER, REF_FILE)]),
    ("markdown table cell", "| kunde | " + SHORT + " |\n|---|---|", [(SHORT, CUST)]),
    ("json string with escaped quotes", json.dumps({"kunde": '"' + FULL + '"'}), [(FULL, CUST)]),
    ("start and end of text", SHORT + " liefert an " + SHORT, [(SHORT, CUST), (SHORT, CUST)]),
]

CONTROLS = [
    ("unregistered control name", fixtures.CONTROL_UNREGISTERED + " ist ein ort"),
    ("acronym inside a longer token", "code PRZLGX ist frei"),
    ("retired form", [line for line in fixtures.register_lines() if line.endswith("retired")][0].split("\t")[2]),
    ("empty", ""),
] + [("keep word " + w, "wir nutzen " + w + " hier") for w in fixtures.KEEP_WORDS]


@pytest.fixture
def m(register_path) -> matcher.Matcher:
    return matcher.Matcher(register.forms_for_matching(register.load(register_path)))


def _hits(m, text):
    return [(text[s.start:s.end], s.code) for s in m.find(text)]


@pytest.mark.parametrize("label,text,expected", PLANTED, ids=[p[0] for p in PLANTED])
def test_planted_pattern_is_found(m, label, text, expected):
    spans = m.find(text)
    assert _hits(m, text) == expected, label
    assert all(s.cls == "name" for s in spans)
    assert all(s.form == text[s.start:s.end] for s in spans)


def test_start_and_end_positions(m):
    text = SHORT + " liefert an " + SHORT
    spans = m.find(text)
    assert spans[0].start == 0
    assert spans[-1].end == len(text)


@pytest.mark.parametrize("label,text", CONTROLS, ids=[c[0] for c in CONTROLS])
def test_control_is_not_matched(m, label, text):
    assert m.find(text) == [], label


def test_thirty_plants_in_one_text_and_replace(m):
    text = "\n---\n".join(p[1] for p in PLANTED)
    spans = m.find(text)
    expected = sum(len(p[2]) for p in PLANTED)
    assert len(spans) == expected
    for a, b in zip(spans, spans[1:]):
        assert a.end <= b.start, "overlap"
    out = m.replace(text, spans, lambda s: "<" + s.code + ">")
    fixtures.assert_no_fixture_name(out, "replaced text")
    assert out.count("<" + CUST + ">") == sum(1 for p in PLANTED for h in p[2] if h[1] == CUST)
    assert out.count("<" + fixtures.PERSON_CODE + ">") == 2
    assert "<" + REF_FILE + ">" in out and "<" + REF_TENDER + ">" in out
    assert "vpc-<" + CUST + ">-prod" in out
    assert "<" + CUST + ">logistik-backup" in out
    assert "<" + CUST + ">Prod01" in out
    assert "1-<" + REF_FILE + ">_Angebotsaufforderung_<" + CUST + ">.pdf" in out
    # untouched text is still there, in order
    assert "angebot an <" + CUST + "> vom mai" in out
    assert m.find(out) == []


def test_longest_variant_wins(m):
    text = FULL + " und " + SHORT + " und " + fixtures.LAWFIRM_FORMS[0]
    hits = _hits(m, text)
    assert hits == [(FULL, CUST), (SHORT, CUST), (fixtures.LAWFIRM_FORMS[0], fixtures.LAWFIRM_CODE)]


def test_glued_needs_five_letters():
    m2 = matcher.Matcher([("Abcd", "CUST-AAAA"), ("Abcde", "CUST-BBBB")])
    assert len(m2) == 3
    assert _hits(m2, "xabcdx xabcdex") == [("abcde", "CUST-BBBB")]
    assert _hits(m2, "abcd abcde") == [("abcd", "CUST-AAAA"), ("abcde", "CUST-BBBB")]


def test_boundary_rule():
    m2 = matcher.Matcher([("Zlgx", "CUST-AAAA")])
    assert _hits(m2, "zlgx-1 _zlgx_ /zlgx/ 'zlgx' [zlgx] zlgx. (zlgx)") == [("zlgx", "CUST-AAAA")] * 7
    assert _hits(m2, "azlgx zlgxb") == []
    # a form of 3 or 4 letters glued to digits is found (his decision of 2026-09-22; this said [] before)
    assert _hits(m2, "zlgx1 1zlgx") == [("zlgx", "CUST-AAAA")] * 2


def test_whitespace_in_form_matches_any_whitespace_run():
    m2 = matcher.Matcher([("Abcde Fghij", "CUST-AAAA")])
    for sep in (" ", "  ", "\n", " \n ", "\t", " ", "\r\n"):
        text = "x abcde" + sep + "fghij y"
        assert _hits(m2, text) == [("abcde" + sep + "fghij", "CUST-AAAA")], repr(sep)
    assert _hits(m2, "abcdefghij") == [("abcdefghij", "CUST-AAAA")]  # glued spelling


def test_empty_matcher_and_empty_text(m):
    assert matcher.Matcher([]).find("x " + FULL) == []
    assert m.find("") == []
    assert m.replace("abc", [], lambda s: "?") == "abc"


def test_replace_is_applied_right_to_left():
    text = "aa bb cc"
    spans = [matcher.Span(0, 2, "name", "C1"), matcher.Span(6, 8, "name", "C2")]
    seen = []

    def token(s):
        seen.append(s.start)
        return "[" + s.code + "]"

    assert matcher.Matcher([]).replace(text, spans, token) == "[C1] bb [C2]"
    assert seen == [6, 0]


def test_public_output_has_only_start_length_cls(m):
    text = "kunde " + FULL + " und " + fixtures.PERSON_FORMS[0]
    spans = m.find(text)
    pub = matcher.public(spans)
    assert len(pub) == 2
    for d in pub:
        assert set(d) == {"start", "length", "cls"}
        assert d["cls"] == "name"
        assert isinstance(d["start"], int) and isinstance(d["length"], int)
    assert pub[0] == {"start": 6, "length": len(FULL), "cls": "name"}
    dumped = json.dumps(pub)
    assert CUST not in dumped and "code" not in dumped and "form" not in dumped
    fixtures.assert_no_fixture_name(dumped, "public output")


# --------------------------------------------------------------------------- review findings: forms the red team
# got past the matcher (spread-out letters, look-alike letters, case changes, numbers glued to words)

def _spread(word: str, sep: str) -> str:
    return sep.join(word)


SPREAD = [
    ("spaced letters", "Kunde ist %s, Ansprechpartner %s." % (_spread(SHORT, " "), _spread(fixtures.PERSON_FORMS[1], " ")),
     [CUST, fixtures.PERSON_CODE]),
    ("dotted letters", "kunde %s." % _spread(SHORT, "."), [CUST]),
    ("dashed letters", "kunde %s ende" % _spread(SHORT, "-"), [CUST]),
    ("split by a line break", "kunde %s\n%s ende" % (SHORT[:3], SHORT[3:]), [CUST]),
    ("split by a table border", "| %s|%s |" % (SHORT[:3], SHORT[3:]), [CUST]),
    ("markdown emphasis", "%s**%s** und %s*%s*" % (SHORT[:3], SHORT[3:], fixtures.PERSON_FORMS[1][:8],
                                                  fixtures.PERSON_FORMS[1][8:]), [CUST, fixtures.PERSON_CODE]),
    ("empty html tags", "%s<b></b>%s" % (SHORT[:3], SHORT[3:]), [CUST]),
    ("html comment", "%s<!-- x -->%s" % (SHORT[:3], SHORT[3:]), [CUST]),
    ("capitals hyphenated at a line end", "%s-\n%s" % (SHORT[:3].upper(), SHORT[3:].upper()), [CUST]),
    ("non-breaking hyphens", fixtures.TENDER_ID.replace("-", "‑"), [REF_TENDER]),
    ("en dash", fixtures.FILE_NUMBER.replace("-", "–"), [REF_FILE]),
    ("spaces for dashes", fixtures.TENDER_ID.replace("-", " "), [REF_TENDER]),
    ("one dash left out", fixtures.TENDER_ID.replace("-", "", 1), [REF_TENDER]),
    ("spaced heading of the full name", _spread(FULL.replace(" ", ""), " "), [CUST]),
    ("acronym with dots", "kunde %s. ende" % _spread(ACRONYM, "."), [CUST]),
]


@pytest.mark.parametrize("label,text,codes", SPREAD, ids=[s[0] for s in SPREAD])
def test_spread_out_form_is_found(m, label, text, codes):
    spans = m.find(text)
    assert [s.code for s in spans] == codes, label
    out = m.replace(text, spans, lambda s: s.code)
    fixtures.assert_no_fixture_name(out, label)


def test_multi_word_form_with_separators_is_one_span():
    m2 = matcher.Matcher(register.forms_for_matching([register.Entry(CUST, "CUST", FULL, fixtures.TODAY, "active")]))
    for sep in ("-", "_", ".", " - "):
        text = "backup-%s-01" % FULL.lower().replace(" ", sep)
        spans = m2.find(text)
        assert len(spans) == 1 and spans[0].code == CUST, repr(sep)
        assert text[spans[0].start:spans[0].end].lower().replace(sep, " ") == FULL.lower()


LOOKALIKE = [
    ("full width", "".join(chr(ord(c) + 0xFEE0) for c in SHORT), CUST),
    ("mathematical bold", "".join(chr(0x1D400 + ord(c) - 65) if c.isupper() else chr(0x1D41A + ord(c) - 97)
                                  for c in SHORT), CUST),
    ("cyrillic o", SHORT[:-1] + "о", CUST),
    ("cyrillic a", fixtures.PERSON_FORMS[1].replace("a", "а"), fixtures.PERSON_CODE),
    ("greek capital zeta", "Ζ" + SHORT[1:], CUST),
    ("oe ligature", "Partner " + fixtures.ORG_FORMS[1].replace("ö", "œ"), fixtures.ORG_CODE),
    ("accent on a letter", SHORT.replace("y", "ý"), CUST),
    ("combining stroke after every letter", "".join(c + "̶" for c in SHORT), CUST),
]


@pytest.mark.parametrize("label,text,code", LOOKALIKE, ids=[s[0] for s in LOOKALIKE])
def test_look_alike_letters_are_found(m, label, text, code):
    spans = m.find(text)
    assert [s.code for s in spans] == [code], label
    # positions point into the text as given
    assert spans[0].form == text[spans[0].start:spans[0].end]
    assert m.replace(text, spans, lambda s: s.code).strip().endswith(code)


CASE_CHANGE = [
    ("acronym before a CamelCase word", "host %sProd01 up" % ACRONYM, [(ACRONYM, CUST)]),
    ("acronym after lower case letters", "host srv%s up" % ACRONYM, [(ACRONYM, CUST)]),
    ("acronym in capitals before digits", "job %s2026 ok" % ACRONYM, [(ACRONYM, CUST)]),
    ("reference glued after a word", "Ref%s ok" % fixtures.TENDER_ID, [(fixtures.TENDER_ID, REF_TENDER)]),
    ("file number glued after letters", "Akte%s ok" % fixtures.FILE_NUMBER, [(fixtures.FILE_NUMBER, REF_FILE)]),
]


@pytest.mark.parametrize("label,text,expected", CASE_CHANGE, ids=[s[0] for s in CASE_CHANGE])
def test_case_change_and_number_boundaries(m, label, text, expected):
    assert _hits(m, text) == expected, label


def test_case_change_rules_keep_their_limits(m):
    # glued inside capitals, digits on both sides of a number, lower case between letters: no boundary.
    # (The acronym in lower case before digits was in this list; it is found now, see GLUED_SHORT below.)
    for text in ("code PR%sX frei" % ACRONYM, "nr 1%s ok" % fixtures.FILE_NUMBER, "nr %s1 ok" % fixtures.FILE_NUMBER,
                 "x%sy" % ACRONYM.lower()):
        assert m.find(text) == [], text
    # three letters spread over a line break, or with long gaps, are not an acronym
    for text in ("%s\n%s" % (ACRONYM[0], ACRONYM[1:]), " ;;; ".join(ACRONYM)):
        assert m.find(text) == [], repr(text)


def test_spread_letters_need_a_boundary(m):
    # the letters of the form inside longer words are not the form
    assert m.find("ab" + _spread(SHORT, " ") + "c") == []
    assert m.find(_spread(SHORT, " ") + "x") == []


def test_replace_refuses_overlapping_spans():
    text = "aa bb cc"
    with pytest.raises(ValueError) as err:
        matcher.Matcher([]).replace(text, [matcher.Span(0, 5, "name", "C1"), matcher.Span(3, 8, "url")], str)
    assert "overlap" in str(err.value)
    with pytest.raises(ValueError):
        matcher.Matcher([]).replace(text, [matcher.Span(6, 9, "name", "C1")], str)


# --------------------------------------------------------------------------- short forms glued into identifiers
# (release 2, his decision of 2026-09-22): a form of 3 or 4 letters also matches inside an identifier token when
# the rest of the token is digits or affixes of rules/glue-affixes.txt

LOW = ACRONYM.lower()

GLUED_SHORT = [
    ("affix after", "job %sbackup failed" % LOW, LOW),
    ("digits after", "host %s01 up" % LOW, LOW),
    ("one digit after", "id %s7 ok" % LOW, LOW),
    ("capitals and a hyphen", "vm %s-prod ok" % ACRONYM, ACRONYM),
    ("affix before", "host srv%s up" % LOW, LOW),
    ("affix and digits", "host %sprod01 up" % LOW, LOW),
    ("digits before", "id 2%s ok" % LOW, LOW),
    ("affixes on both sides", "k8s%scluster3" % LOW, LOW),
    ("capitalised with an affix", "%sweb02" % ACRONYM.capitalize(), ACRONYM.capitalize()),
    ("in a path", "/srv/backup/%sdb01/dump.sql" % LOW, LOW),
    ("affix only in the rules file", "%sstaging" % LOW, LOW),
]


@pytest.mark.parametrize("label,text,found", GLUED_SHORT, ids=[g[0] for g in GLUED_SHORT])
def test_short_form_glued_to_digits_or_affixes_is_found(m, label, text, found):
    assert _hits(m, text) == [(found, CUST)], label
    out = m.replace(text, m.find(text), lambda s: s.code)
    assert LOW not in out.lower() and CUST in out
    assert m.find(out) == []


ORDINARY_GLUE = [
    "puz" + LOW + "ame",          # an ordinary word: letters on both sides that are no affix
    "x" + LOW + "y",
    LOW + "ame",
    LOW + "testing",              # an affix followed by more letters
    "code PR%sX frei" % ACRONYM,
    "a" + LOW + "b01",
    "backupx" + LOW,
]


@pytest.mark.parametrize("text", ORDINARY_GLUE)
def test_short_form_inside_an_ordinary_word_is_not_found(m, text):
    assert m.find(text) == [], text


def test_glued_short_forms_need_three_or_four_letters():
    m2 = matcher.Matcher([("Qxvb", "CUST-AAAA"), ("Qx", "CUST-BBBB"), ("Qxvbn", "CUST-CCCC")])
    assert _hits(m2, "qxvbdb01 aqxvb qxdb01 qxprod") == [("qxvb", "CUST-AAAA")]
    # the five-letter glued rule still works on its own: no boundary and no affix needed
    assert _hits(m2, "xqxvbnx") == [("qxvbn", "CUST-CCCC")]
    # a form with a space or a digit is not a short glued form
    m3 = matcher.Matcher([("Q xv", "CUST-AAAA"), ("Qx1", "CUST-BBBB")])
    assert m3.find("qxvdb01 srvqx1") == []


def test_five_letter_glued_rule_still_works(m):
    for text in ("job %sbackup01 failed" % SHORT.lower(), "host srv%s up" % SHORT.lower(),
                 "x%sy" % SHORT.lower()):
        assert _hits(m, text) == [(SHORT.lower(), CUST)], text


def test_glue_affixes_come_from_the_rules_file():
    rules = Path(matcher.__file__).resolve().parent.parent / "rules" / "glue-affixes.txt"
    words = {w.strip() for w in rules.read_text(encoding="utf-8").splitlines() if w.strip() and not w.startswith("#")}
    assert words and words == set(matcher.GLUE_AFFIXES)
    m2 = matcher.Matcher([("Zlgx", "CUST-AAAA")])
    for affix in sorted(words):
        assert _hits(m2, "zlgx" + affix) == [("zlgx", "CUST-AAAA")], affix
        assert _hits(m2, affix + "zlgx") == [("zlgx", "CUST-AAAA")], affix


def test_a_long_token_is_searched_quickly(m):
    import time

    text = ("1" * 20000 + LOW + "x") * 20 + " " + ("db" * 20000 + LOW) * 5
    start = time.monotonic()
    assert m.find(text) == []
    assert time.monotonic() - start < 5
