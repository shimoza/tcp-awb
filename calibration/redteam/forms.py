"""Red team, dimension forms: written shapes of a registered form in plain text, Markdown, html, csv, json,
yaml, xml, rtf, ics and svg files, beyond awb/planted.py and the DISGUISED list of tests/test_intake.py.

Every value comes from tests/fixtures.py. Where the harness cannot read a shape (an encoding it does not
decode, a look-alike letter outside its own table), the encoded string itself is planted as a value so that
the raw view reports whether it survived; the notes say so per case.
"""
import base64
import binascii
import gzip
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))   # the repository that holds this pack
from tests import fixtures as fx  # noqa: E402

FULL, SHORT, ACRONYM, ENGLISH = fx.CUSTOMER_FORMS
PERSON, SURNAME = fx.PERSON_FORMS
ORG, ORG_SHORT = fx.ORG_FORMS
LAWFIRM, LAW_SHORT = fx.LAWFIRM_FORMS
PLACE = fx.PLACE_FORMS[0]
DOMAIN = fx.CUSTOMER_DOMAIN
FILE_NUMBER = fx.FILE_NUMBER
TENDER = fx.TENDER_ID

MARK = "stage two of the invented plan"


def txt(name, text):
    def build(inbox):
        p = inbox / name
        p.write_text(text, encoding="utf-8")
        return p
    return build


def raw(name, data: bytes):
    def build(inbox):
        p = inbox / name
        p.write_bytes(data)
        return p
    return build


def html(name, body):
    return txt(name, "<html><body>%s</body></html>\n" % body)


def svg(body):
    return txt("figure.svg", '<?xml version="1.0"?>\n<svg xmlns="http://www.w3.org/2000/svg" width="200" height="60">\n'
               "<text x=\"10\" y=\"30\">%s</text>\n</svg>\n" % body)


def tags(s: str) -> str:
    return "".join(chr(0xE0000 + ord(c)) for c in s)


def b64(s: str) -> str:
    return base64.b64encode(s.encode()).decode()


# --- base64 blocks cut at a line boundary -----------------------------------------------------------------
_PREFIX52 = (MARK + ", ")[:52].ljust(52, "x")          # 52 bytes, then the tender id starts at byte 52
_QUOTED_TEXT = _PREFIX52 + TENDER + " end of the note, nothing else"
_QUOTED_B64 = b64(_QUOTED_TEXT)
_QUOTED_LINES = "\n".join("> " + _QUOTED_B64[i:i + 76] for i in range(0, len(_QUOTED_B64), 76))
# 76 base64 characters are 57 bytes: line one ends inside the id (bytes 52..56 are "VG-20"), line two starts
# with the rest of it
_LINE20_TEXT = "ref " + TENDER + " " + MARK            # 15 bytes per line of 20 characters: the id is cut before its last digit
_LINE20_B64 = b64(_LINE20_TEXT)
_LINE20_LINES = "\n".join(_LINE20_B64[i:i + 20] for i in range(0, len(_LINE20_B64), 20))

_B32_FULL = base64.b32encode(FULL.encode()).decode()
_GZ_FULL = base64.b64encode(gzip.compress(FULL.encode(), mtime=0)).decode()
_UU_FULL = binascii.b2a_uu(FULL.encode()).decode().rstrip("\n")
_B64_SHORT = b64(SHORT)                                # 8 characters, under MIN_INLINE_BLOCK
_UTF7_ORG = ORG_SHORT.encode("utf-7").decode()          # Qv+APY-rtz
_UTF7_SHORT_ALL = "+" + base64.b64encode(SHORT.encode("utf-16-be")).decode().rstrip("=") + "-"
_QP_SHORT = "".join("=%02X" % b for b in SHORT.encode())
_XESC_SHORT = "".join("\\x%02X" % b for b in SHORT.encode())
_RTF_U_ORG = "".join(c if ord(c) < 128 else "\\u%d?" % ord(c) for c in ORG_SHORT)
_RTF_U_SHORT = "".join("\\u%d?" % ord(c) for c in SHORT)
_PUNY_ORG = ORG_SHORT.lower().encode("idna").decode()   # the punycode label of the umlaut short form
_LEET_FULL = FULL.replace("o", "0").replace("i", "1").replace("s", "5")
# the full form spelled phonetically in Cyrillic letters, built from the fixture through a letter table
_CYRILLIC = {"Z": "З", "y": "и", "x": "кс", "w": "в", "o": "о", "L": "Л", "g": "г", "i": "и", "s": "с", "t": "т",
             "k": "к", "G": "Г", "m": "м", "b": "б", "H": "Х", " ": " "}
_CYR_PHONETIC = "".join(_CYRILLIC[c] for c in FULL)
# modifier (superscript) and small capital spellings of the short form; the tables are in alphabetical order so
# that this source does not spell the form itself
_MODIFIER = {"o": "\u1d52", "w": "\u02b7", "x": "\u02e3", "y": "\u02b8", "z": "\u1dbb"}
_SMALL_CAP = {"o": "\u1d0f", "w": "\u1d21", "x": "x", "y": "\u028f", "z": "\u1d22"}
_MODIFIER_SHORT = "".join(_MODIFIER[c] for c in SHORT.lower())
_SMALL_CAP_SHORT = "".join(_SMALL_CAP[c] for c in SHORT.lower())
_MATH_BOLD_SHORT = "".join(chr(0x1D400 + (ord(c) - 65)) if c.isupper() else chr(0x1D41A + (ord(c) - 97)) for c in SHORT)
_JSON_SURROGATES = "".join("\\u%04x\\u%04x" % (0xD800 + ((ord(c) - 0x10000) >> 10), 0xDC00 + ((ord(c) - 0x10000) & 0x3FF))
                           for c in _MATH_BOLD_SHORT)
_LONG_STYLE = ("font-family:'Liberation Sans',sans-serif;font-size:12px;fill:#000000;stroke:none;"
               "font-weight:normal;font-style:normal;letter-spacing:0px;word-spacing:0px;"
               "text-anchor:start;dominant-baseline:auto;paint-order:stroke;opacity:1;")
assert len(_LONG_STYLE) > 200

_TSPAN_ONE_LINE = "".join('<tspan x="%d">%s</tspan>' % (10 + 12 * i, c) for i, c in enumerate(SHORT))
_TSPAN_MULTI = "".join('<tspan\n   x="%d"\n   y="30">%s</tspan>' % (10 + 12 * i, c) for i, c in enumerate(SHORT))
_TSPAN_STYLE = "".join('<tspan style="%s">%s</tspan>' % (_LONG_STYLE, c) for c in SHORT)
_RUBY = "<ruby>" + "".join("%s<rt>%s</rt>" % (c, t) for c, t in zip(SHORT, ("zed", "why", "ex", "dub", "oh"))) + "</ruby>"

CASES = [
    # ---------------------------------------------------------------- leet
    {"id": "leet-short-0", "cls": "name", "carrier": "txt body, 0 for o", "values": [SHORT],
     "build": txt("a.txt", "the %s team, %s\n" % (SHORT.replace("o", "0"), MARK))},
    {"id": "leet-full", "cls": "name", "carrier": "txt body, 0 1 5 for o i s", "values": [FULL],
     "build": txt("a.txt", "offer for %s, %s\n" % (_LEET_FULL, MARK))},
    # ---------------------------------------------------------------- bidi
    {"id": "bidi-rlo-short", "cls": "name", "carrier": "txt body, RLO + reversed letters, no other hit", "values": [SHORT],
     "build": txt("a.txt", "the ‮%s‬ team, %s\n" % (SHORT[::-1], MARK))},
    {"id": "bidi-rlo-full", "cls": "name", "carrier": "txt body, RLO + reversed full form, no other hit", "values": [FULL],
     "build": txt("a.txt", "offer for ‮%s‬, %s\n" % (FULL[::-1], MARK))},
    {"id": "bidi-rli-reversed", "cls": "name", "carrier": "txt body, RLI isolate + reversed letters (a viewer keeps Latin order)", "values": [SHORT],
     "build": txt("a.txt", "the ⁧%s⁩ team, %s\n" % (SHORT[::-1], MARK))},
    {"id": "bidi-rlo-with-other-hit", "cls": "name", "carrier": "txt body, RLO reversed plus a mail address elsewhere", "values": [SHORT],
     "build": txt("a.txt", "the ‮%s‬ team, write to anna.berg@example.org, %s\n" % (SHORT[::-1], MARK))},
    # ---------------------------------------------------------------- punycode
    {"id": "puny-org-domain", "cls": "url", "carrier": "txt body, punycode label of the umlaut short form under .example", "values": [_PUNY_ORG],
     "build": txt("a.txt", "see https://%s.example/offer, %s\n" % (_PUNY_ORG, MARK))},
    {"id": "puny-org-host-label", "cls": "name", "carrier": "txt body, punycode label as a host name without tld", "values": [_PUNY_ORG, ORG_SHORT],
     "build": txt("a.txt", "host %s-web01 is down, %s\n" % (_PUNY_ORG, MARK))},
    # ---------------------------------------------------------------- base encodings
    {"id": "b32-full", "cls": "name", "carrier": "txt body, base32 of the full form", "values": [_B32_FULL, FULL],
     "build": txt("a.txt", "tag %s, %s\n" % (_B32_FULL, MARK))},
    {"id": "b64-gzip-full", "cls": "name", "carrier": "txt body, base64 of gzip of the full form", "values": [_GZ_FULL, FULL],
     "build": txt("a.txt", "blob %s, %s\n" % (_GZ_FULL, MARK))},
    {"id": "b64-nopad-full", "cls": "name", "carrier": "txt body, base64 with the padding stripped", "values": [FULL],
     "build": txt("a.txt", "tag %s, %s\n" % (b64(FULL).rstrip("="), MARK))},
    {"id": "b64-short-8chars", "cls": "name", "carrier": "txt body, base64 of the short form (8 chars, under the 16 minimum)", "values": [_B64_SHORT, SHORT],
     "build": txt("a.txt", "tag %s, %s\n" % (_B64_SHORT, MARK))},
    {"id": "b64-quoted-lines-cut", "cls": "other", "carrier": "txt body, MIME base64 quoted with '> ', the tender id cut at the 76-char line boundary", "values": [TENDER, TENDER[3:]],
     "build": txt("a.txt", "quoted attachment:\n%s\n" % _QUOTED_LINES)},
    {"id": "b64-lines-20-cut", "cls": "other", "carrier": "txt body, base64 in lines of 20 chars, the tender id cut at a line boundary", "values": [TENDER, TENDER[:-1]],
     "build": txt("a.txt", "%s\n" % _LINE20_LINES)},
    {"id": "uuencode-full", "cls": "name", "carrier": "txt body, uuencoded full form", "values": [_UU_FULL, FULL],
     "build": txt("a.txt", "begin 644 note.txt\n%s\n`\nend\n" % _UU_FULL)},
    # ---------------------------------------------------------------- quoted printable, encoded words, utf-7
    {"id": "qp-ascii-short", "cls": "name", "carrier": "txt body, quoted-printable of ASCII letters only", "values": [_QP_SHORT, SHORT],
     "build": txt("a.txt", "the %s team, %s\n" % (_QP_SHORT, MARK))},
    {"id": "encw-utf7-b-org", "cls": "name", "carrier": "txt body, encoded word charset utf-7, B", "values": [ORG_SHORT],
     "build": txt("a.txt", "s =?utf-7?b?%s?= today, %s\n" % (b64(_UTF7_ORG), MARK))},
    {"id": "encw-iso2022jp-b-full", "cls": "name", "carrier": "txt body, encoded word charset iso-2022-jp, B", "values": [FULL],
     "build": txt("a.txt", "s =?iso-2022-jp?b?%s?= today, %s\n" % (b64(FULL), MARK))},
    {"id": "encw-gb2312-q-full", "cls": "name", "carrier": "txt body, encoded word charset gb2312, Q with underscores", "values": [FULL],
     "build": txt("a.txt", "s =?gb2312?q?%s?= today, %s\n" % (FULL.replace(" ", "_"), MARK))},
    {"id": "encw-unknown-charset-short", "cls": "name", "carrier": "txt body, encoded word with an unknown charset and an 8-char payload", "values": [_B64_SHORT, SHORT],
     "build": txt("a.txt", "s =?x-nonesuch?b?%s?= today, %s\n" % (_B64_SHORT, MARK))},
    {"id": "utf7-plain-org", "cls": "name", "carrier": "txt body, utf-7 of the umlaut short form", "values": [_UTF7_ORG, ORG_SHORT],
     "build": txt("a.txt", "supplier %s today, %s\n" % (_UTF7_ORG, MARK))},
    {"id": "utf7-plain-all-encoded", "cls": "name", "carrier": "txt body, utf-7 with every letter in the base64 run", "values": [_UTF7_SHORT_ALL, SHORT],
     "build": txt("a.txt", "the %s team, %s\n" % (_UTF7_SHORT_ALL, MARK))},
    # ---------------------------------------------------------------- json escapes
    {"id": "json-surrogate-math-bold", "cls": "name", "carrier": "json string, surrogate pairs of mathematical bold letters", "values": [SHORT],
     "build": txt("a.json", '{"customer": "%s", "stage": 2}\n' % _JSON_SURROGATES)},
    {"id": "json-escaped-slash-tender", "cls": "other", "carrier": "json string, escaped slashes as the separators of the tender id", "values": [TENDER],
     "build": txt("a.json", '{"ref": "%s", "stage": 2}\n' % TENDER.replace("-", "\\/"))},
    {"id": "json-x-escape-short", "cls": "name", "carrier": "txt body, \\xHH escapes", "values": [_XESC_SHORT, SHORT],
     "build": txt("a.txt", "the %s team, %s\n" % (_XESC_SHORT, MARK))},
    # ---------------------------------------------------------------- html entities
    {"id": "ent-dec-nosemi-md", "cls": "name", "carrier": "md body, decimal entities without semicolons", "values": [SHORT],
     "build": txt("a.md", "the %s team, %s\n" % ("".join("&#%d" % ord(c) for c in SHORT), MARK))},
    {"id": "ent-hex-8digits-md", "cls": "name", "carrier": "md body, hex entity with eight digits for the first letter", "values": [SHORT],
     "build": txt("a.md", "the &#x%08X;%s team, %s\n" % (ord(SHORT[0]), SHORT[1:], MARK))},
    {"id": "ent-dec-8digits-md", "cls": "name", "carrier": "md body, decimal entity with eight digits for the first letter", "values": [SHORT],
     "build": txt("a.md", "the &#%08d;%s team, %s\n" % (ord(SHORT[0]), SHORT[1:], MARK))},
    {"id": "ent-dec-hex-mixed-html", "cls": "name", "carrier": "html body, decimal and hex entities mixed, every letter", "values": [SHORT],
     "build": html("a.html", "<p>the %s team, %s</p>" % ("".join(("&#%d;" if i % 2 else "&#x%X;") % ord(c) for i, c in enumerate(SHORT)), MARK))},
    {"id": "ent-named-accent-md", "cls": "name", "carrier": "md body, named entities for the umlauts", "values": [ORG_SHORT],
     "build": txt("a.md", "supplier %s today, %s\n" % (ORG_SHORT.replace("ö", "&ouml;"), MARK))},
    {"id": "ent-named-nosemi-md", "cls": "name", "carrier": "md body, legacy named entity without semicolon", "values": [ORG_SHORT],
     "build": txt("a.md", "supplier %s today, %s\n" % (ORG_SHORT.replace("ö", "&ouml"), MARK))},
    # ---------------------------------------------------------------- html: css, ruby, attributes, hidden
    {"id": "css-content-html", "cls": "name", "carrier": "html style, CSS content string (rendered by a viewer)", "values": [FULL],
     "visible": ["marker-css-one"],
     "build": html("a.html", '<style>p::after{content:"%s marker-css-one"}</style><p>%s</p>' % (FULL, MARK))},
    {"id": "ruby-html", "cls": "name", "carrier": "html body, ruby annotation per letter", "values": [SHORT],
     "build": html("a.html", "<p>the %s team, %s</p>" % (_RUBY, MARK))},
    {"id": "html-attrs-unseen", "cls": "name", "carrier": "html alt, title, aria-label and data-* attributes", "values": [FULL],
     "visible": ["marker-alt-one", "marker-title-one", "marker-aria-one", "marker-data-one"],
     "build": html("a.html", '<p>%s</p><img src="x.png" alt="%s marker-alt-one"><span title="marker-title-one">a</span>'
                   '<div aria-label="marker-aria-one" data-note="marker-data-one">b</div>' % (MARK, FULL))},
    {"id": "html-noscript-unseen", "cls": "name", "carrier": "html noscript element", "values": [FULL],
     "visible": ["marker-noscript-one"],
     "build": html("a.html", "<p>%s</p><noscript>marker-noscript-one %s</noscript>" % (MARK, FULL))},
    {"id": "html-hidden-whole", "cls": "name", "carrier": "html element with hidden attribute", "values": [FULL],
     "build": html("a.html", "<p>%s</p><span hidden>%s</span>" % (MARK, FULL))},
    {"id": "html-comment-multiline-md", "cls": "name", "carrier": "md body, html comment with line breaks inside the word", "values": [SHORT],
     "build": txt("a.md", "the %s<!--\n\n-->%s team, %s\n" % (SHORT[:3], SHORT[3:], MARK))},
    {"id": "md-doctype-split", "cls": "name", "carrier": "md body, a doctype declaration inside the word", "values": [SHORT],
     "build": txt("a.md", "the %s<!DOCTYPE html>%s team, %s\n" % (SHORT[:3], SHORT[3:], MARK))},
    {"id": "md-long-tag-split", "cls": "name", "carrier": "md body, an inline tag over 200 chars inside the word", "values": [SHORT],
     "build": txt("a.md", 'the %s<em data-x="%s">%s</em> team, %s\n' % (SHORT[:3], _LONG_STYLE, SHORT[3:], MARK))},
    {"id": "md-tag-newline-split", "cls": "name", "carrier": "md body, an inline tag with a line break inside, inside the word", "values": [SHORT],
     "build": txt("a.md", 'the %s<em\nclass="a">%s</em> team, %s\n' % (SHORT[:3], SHORT[3:], MARK))},
    # ---------------------------------------------------------------- svg and xml
    {"id": "svg-tspan-one-line", "cls": "name", "carrier": "svg text, one letter per tspan, tags on one line", "values": [SHORT],
     "build": svg(_TSPAN_ONE_LINE)},
    {"id": "svg-tspan-multiline-attrs", "cls": "name", "carrier": "svg text, one letter per tspan, attributes on their own lines", "values": [SHORT],
     "build": svg(_TSPAN_MULTI)},
    {"id": "svg-tspan-long-style", "cls": "name", "carrier": "svg text, one letter per tspan with a style attribute over 200 chars", "values": [SHORT],
     "build": svg(_TSPAN_STYLE)},
    {"id": "svg-textpath-tspans", "cls": "name", "carrier": "svg textPath with one letter per tspan", "values": [SHORT],
     "build": txt("figure.svg", '<svg xmlns="http://www.w3.org/2000/svg"><defs><path id="p" d="M10,40 L190,40"/></defs>'
                  '<text><textPath href="#p">%s</textPath></text></svg>\n' % _TSPAN_ONE_LINE)},
    {"id": "xml-cdata-split", "cls": "name", "carrier": "xml text, an empty CDATA section inside the word", "values": [SHORT],
     "build": txt("a.xml", '<?xml version="1.0"?>\n<note><customer>%s<![CDATA[]]>%s</customer><stage>%s</stage></note>\n' % (SHORT[:3], SHORT[3:], MARK))},
    {"id": "xml-pi-split", "cls": "name", "carrier": "xml text, a processing instruction inside the word", "values": [SHORT],
     "build": txt("a.xml", '<?xml version="1.0"?>\n<note><customer>%s<?xml-stylesheet type="text/css"?>%s</customer><stage>%s</stage></note>\n' % (SHORT[:3], SHORT[3:], MARK))},
    {"id": "xml-cdata-whole", "cls": "name", "carrier": "xml CDATA section holding the full form", "values": [FULL],
     "build": txt("a.xml", '<?xml version="1.0"?>\n<note><customer><![CDATA[%s]]></customer><stage>%s</stage></note>\n' % (FULL, MARK))},
    # ---------------------------------------------------------------- yaml and markdown structure
    {"id": "yaml-folded-tender-indent10", "cls": "other", "carrier": "yaml folded scalar, tender id over three lines indented ten spaces", "values": [TENDER],
     "build": txt("a.yaml", "note: %s\nref: >-\n          %s\n          %s\n          %s\n" % ((MARK,) + tuple(TENDER.split("-"))))},
    {"id": "md-ref-link-title", "cls": "name", "carrier": "md reference link definition, form in the title only", "values": [FULL],
     "build": txt("a.md", "see the [offer][r], %s\n\n[r]: https://example.org/offer \"%s\"\n" % (MARK, FULL))},
    {"id": "md-image-alt", "cls": "name", "carrier": "md image alt text", "values": [FULL],
     "build": txt("a.md", "![%s](figure.png)\n\n%s\n" % (FULL, MARK))},
    {"id": "md-list-words-tender", "cls": "other", "carrier": "md list, one piece of the tender id per item", "values": [TENDER],
     "build": txt("a.md", "%s\n\n- %s-\n- %s-\n- %s\n" % ((MARK,) + tuple(TENDER.split("-"))))},
    {"id": "md-table-rows-surname", "cls": "name", "carrier": "md table, the surname split over two rows", "values": [SURNAME],
     "build": txt("a.md", "| part |\n| --- |\n| %s |\n| %s |\n\n%s\n" % (SURNAME[:8], SURNAME[8:], MARK))},
    {"id": "letters-per-line-indent9", "cls": "name", "carrier": "txt body, one letter per line indented nine spaces", "values": [SHORT],
     "build": txt("a.txt", "%s\n%s\n" % (MARK, "\n".join(" " * 9 + c for c in SHORT)))},
    {"id": "md-numbered-list-letters", "cls": "name", "carrier": "md numbered list, one letter per item", "values": [SHORT],
     "build": txt("a.md", "%s\n\n%s\n" % (MARK, "\n".join("%d. %s" % (i + 1, c) for i, c in enumerate(SHORT))))},
    # ---------------------------------------------------------------- separators, marks, look-alikes
    {"id": "emoji-separated", "cls": "name", "carrier": "txt body, an emoji between every letter", "values": [SHORT],
     "build": txt("a.txt", "the %s team, %s\n" % ("\U0001F642".join(SHORT), MARK))},
    {"id": "combining-every-letter", "cls": "name", "carrier": "txt body, a combining diaeresis on every letter", "values": [SHORT],
     "build": txt("a.txt", "the %s team, %s\n" % ("".join(c + "̈" for c in SHORT), MARK))},
    {"id": "enclosing-mark-every-letter", "cls": "name", "carrier": "txt body, a combining enclosing circle on every letter", "values": [SHORT],
     "build": txt("a.txt", "the %s team, %s\n" % ("".join(c + "⃝" for c in SHORT), MARK))},
    {"id": "enclosed-letters", "cls": "name", "carrier": "txt body, circled letters", "values": [SHORT],
     "build": txt("a.txt", "the %s team, %s\n" % ("".join(chr(0x24B6 + ord(c) - 65) if c.isupper() else chr(0x24D0 + ord(c) - 97) for c in SHORT), MARK))},
    {"id": "superscript-letters", "cls": "name", "carrier": "txt body, modifier (superscript) letters", "values": [SHORT],
     "build": txt("a.txt", "the %s team, %s\n" % (_MODIFIER_SHORT, MARK))},
    {"id": "math-script-letters", "cls": "name", "carrier": "txt body, mathematical script letters", "values": [SHORT],
     "build": txt("a.txt", "the %s team, %s\n" % ("\U0001D4B5\U0001D4CE\U0001D4CD\U0001D4CC\U0001D4B8", MARK))},
    {"id": "small-caps", "cls": "name", "carrier": "txt body, small capital letters", "values": [SHORT, _SMALL_CAP_SHORT],
     "build": txt("a.txt", "the %s team, %s\n" % (_SMALL_CAP_SHORT, MARK))},
    {"id": "cyrillic-omega-w", "cls": "name", "carrier": "txt body, Cyrillic omega for w (not in the table)", "values": [SHORT, SHORT.replace("w", "ѡ")],
     "build": txt("a.txt", "the %s team, %s\n" % (SHORT.replace("w", "ѡ"), MARK))},
    {"id": "cyrillic-straight-u-y", "cls": "name", "carrier": "txt body, Cyrillic straight u for y (not in the table)", "values": [SHORT, SHORT.replace("y", "ү")],
     "build": txt("a.txt", "the %s team, %s\n" % (SHORT.replace("y", "ү"), MARK))},
    {"id": "latin-z-stroke", "cls": "name", "carrier": "txt body, Latin Z with stroke", "values": [SHORT, "Ƶ" + SHORT[1:]],
     "build": txt("a.txt", "the %s team, %s\n" % ("Ƶ" + SHORT[1:], MARK))},
    {"id": "ligature-st-place", "cls": "name", "carrier": "txt body, st ligature inside the place name", "values": [PLACE],
     "build": txt("a.txt", "the site in %s, %s\n" % (PLACE.replace("st", "ﬆ"), MARK))},
    {"id": "dotless-i-full", "cls": "name", "carrier": "txt body, Turkish dotless i", "values": [FULL],
     "build": txt("a.txt", "offer for %s, %s\n" % (FULL.replace("i", "ı"), MARK))},
    {"id": "cyrillic-phonetic", "cls": "name", "carrier": "txt body, phonetic spelling in Cyrillic letters", "values": [FULL, _CYR_PHONETIC],
     "build": txt("a.txt", "offer for %s, %s\n" % (_CYR_PHONETIC, MARK))},
    {"id": "acronym-spaced-dashes", "cls": "name", "carrier": "txt body, acronym letters with ' - ' between (gap 3)", "values": [ACRONYM],
     "build": txt("a.txt", "the %s team, %s\n" % (" - ".join(ACRONYM), MARK))},
    # ---------------------------------------------------------------- file number and tender id shapes
    {"id": "tender-upper-prefix-glued", "cls": "other", "carrier": "txt body, tender id glued after an upper-case prefix", "values": [TENDER],
     "build": txt("a.txt", "see REF%s for it, %s\n" % (TENDER, MARK))},
    {"id": "tender-no-separators", "cls": "other", "carrier": "txt body, tender id without its dashes", "values": [TENDER],
     "build": txt("a.txt", "see %s for it, %s\n" % (TENDER.replace("-", ""), MARK))},
    {"id": "tender-fullwidth", "cls": "other", "carrier": "txt body, tender id in fullwidth letters and digits", "values": [TENDER],
     "build": txt("a.txt", "see %s for it, %s\n" % ("".join(chr(ord(c) + 0xFEE0) for c in TENDER), MARK))},
    {"id": "filenumber-no-separator", "cls": "other", "carrier": "txt body, file number without its dash", "values": [FILE_NUMBER],
     "build": txt("a.txt", "file %s, %s\n" % (FILE_NUMBER.replace("-", ""), MARK))},
    {"id": "filenumber-spaced-endash", "cls": "other", "carrier": "txt body, file number with ' – ' (spaces and en dash)", "values": [FILE_NUMBER],
     "build": txt("a.txt", "file %s, %s\n" % (FILE_NUMBER.replace("-", " – "), MARK))},
    {"id": "filenumber-slash", "cls": "other", "carrier": "txt body, file number with a slash", "values": [FILE_NUMBER],
     "build": txt("a.txt", "file %s, %s\n" % (FILE_NUMBER.replace("-", "/"), MARK))},
    {"id": "filenumber-underscore", "cls": "other", "carrier": "txt body, file number with an underscore", "values": [FILE_NUMBER],
     "build": txt("a.txt", "file %s, %s\n" % (FILE_NUMBER.replace("-", "_"), MARK))},
    # ---------------------------------------------------------------- no hit at all: what survives
    {"id": "tagchars-smuggle-no-hit", "cls": "name", "carrier": "txt body, the short form as Unicode tag characters (invisible), no other hit", "values": [SHORT],
     "note": "the harness cannot read tag characters; checked by hand with --keep",
     "build": txt("a.txt", "%s %s\n" % (MARK, tags(SHORT)))},
    # ---------------------------------------------------------------- rtf, ics, csv
    {"id": "rtf-unicode-escape-org", "cls": "name", "carrier": "rtf body, \\uN? escape for the umlaut", "values": [_RTF_U_ORG, ORG_SHORT],
     "build": txt("a.rtf", "{\\rtf1\\ansi supplier %s today, %s}\n" % (_RTF_U_ORG, MARK))},
    {"id": "rtf-unicode-escape-ascii", "cls": "name", "carrier": "rtf body, \\uN? escape for every letter", "values": [_RTF_U_SHORT, SHORT],
     "build": txt("a.rtf", "{\\rtf1\\ansi the %s team, %s}\n" % (_RTF_U_SHORT, MARK))},
    {"id": "ics-folded-tender", "cls": "other", "carrier": "ics line folded (CRLF + space) inside the tender id", "values": [TENDER],
     "build": raw("a.ics", ("BEGIN:VCALENDAR\r\nBEGIN:VEVENT\r\nSUMMARY:%s\r\nX-REF:%s\r\n %s\r\nEND:VEVENT\r\nEND:VCALENDAR\r\n"
                            % (MARK, TENDER[:5], TENDER[5:])).encode())},
    {"id": "csv-letters-per-cell", "cls": "name", "carrier": "csv, one letter per cell", "values": [SHORT],
     "build": txt("a.csv", "%s\n1,2,3,4,5\n" % ",".join(SHORT))},
]

# ============================================================================ round 2: boundaries of the escapes
_LOWER_STYLE = ("font-family:sans-serif;font-size:12px;fill:#000000;stroke:none;font-weight:normal;"
                "font-style:normal;letter-spacing:0px;word-spacing:0px;text-anchor:start;"
                "dominant-baseline:auto;paint-order:stroke;opacity:1;text-rendering:auto;")
assert len(_LOWER_STYLE) > 200
_STYLE150 = _LOWER_STYLE[:150]
_TSPAN_STYLE_LOWER = "".join('<tspan style="%s">%s</tspan>' % (_LOWER_STYLE, c) for c in SHORT)
_UTF32_FULL_NOBOM = base64.b64encode(FULL.encode("utf-32-le")).decode()
_UTF32_FULL_BOM = base64.b64encode(FULL.encode("utf-32")).decode()
# a base64 blob whose first line is short (16 chars, 12 bytes) and whose next lines are 76 chars
_FIRST_TEXT = "a " + TENDER + " " + MARK + ", " + MARK           # bytes 0..11 = "ab VG-2026-04", byte 12.. = "57 ..."
_FIRST_B64 = b64(_FIRST_TEXT)
_FIRST_LINES = "\n".join([_FIRST_B64[:16]] + [_FIRST_B64[i:i + 76] for i in range(16, len(_FIRST_B64), 76)])
_RTF_U_FALLBACK_ORG = "".join(c if ord(c) < 128 else "\\u%d\\'%02x" % (ord(c), ord(c)) for c in ORG_SHORT)
_RTF_HEX_SHORT = "".join("\\'%02x" % b for b in SHORT.encode())
_CYR_PHONETIC_LOWER = _CYR_PHONETIC.lower()

CASES += [
    {"id": "r2-bidi-rlo-with-tender-hit", "cls": "name", "carrier": "txt body, RLO reversed short form plus the tender id elsewhere (a hit)", "values": [SHORT],
     "build": txt("a.txt", "the ‮%s‬ team, see %s, %s\n" % (SHORT[::-1], TENDER, MARK))},
    {"id": "r2-html-rlo-entity", "cls": "name", "carrier": "html body, RLO as a numeric entity plus reversed letters, no other hit", "values": [SHORT],
     "build": html("a.html", "<p>the &#8238;%s&#8236; team, %s</p>" % (SHORT[::-1], MARK))},
    {"id": "r2-b64-utf32-nobom", "cls": "name", "carrier": "txt body, base64 of the full form in UTF-32 without a BOM", "values": [FULL],
     "build": txt("a.txt", "tag %s, %s\n" % (_UTF32_FULL_NOBOM, MARK))},
    {"id": "r2-b64-utf32-bom", "cls": "name", "carrier": "txt body, base64 of the full form in UTF-32 with a BOM", "values": [FULL],
     "build": txt("a.txt", "tag %s, %s\n" % (_UTF32_FULL_BOM, MARK))},
    {"id": "r2-b64-quoted-lines-cut", "cls": "other", "carrier": "txt body, MIME base64 quoted with '> ', the tender id cut at the 76-char line boundary", "values": [TENDER, TENDER[5:]],
     "build": txt("a.txt", "quoted attachment:\n%s\n" % _QUOTED_LINES)},
    {"id": "r2-b64-first-line-short", "cls": "other", "carrier": "txt body, base64 whose first line has 16 chars and the next 76, the tender id cut after line one", "values": [TENDER, TENDER[:-2]],
     "build": txt("a.txt", "%s\n" % _FIRST_LINES)},
    {"id": "r2-ent-dec-nosemi-html", "cls": "name", "carrier": "html body, decimal entities without semicolons", "values": [SHORT],
     "build": html("a.html", "<p>the %s team, %s</p>" % ("".join("&#%d" % ord(c) for c in SHORT), MARK))},
    {"id": "r2-ent-hex-8digits-html", "cls": "name", "carrier": "html body, hex entity with eight digits", "values": [SHORT],
     "build": html("a.html", "<p>the &#x%08X;%s team, %s</p>" % (ord(SHORT[0]), SHORT[1:], MARK))},
    {"id": "r2-ent-hex-nosemi-md", "cls": "name", "carrier": "md body, hex entities without semicolons", "values": [SHORT],
     "build": txt("a.md", "the %s team, %s\n" % ("".join("&#x%X" % ord(c) for c in SHORT), MARK))},
    {"id": "r2-ent-dec-7digits-md", "cls": "name", "carrier": "md body, decimal entity with seven digits (the regex limit)", "values": [SHORT],
     "build": txt("a.md", "the &#%07d;%s team, %s\n" % (ord(SHORT[0]), SHORT[1:], MARK))},
    {"id": "r2-ent-named-nosemi-html", "cls": "name", "carrier": "html body, legacy named entity without semicolon", "values": [ORG_SHORT],
     "build": html("a.html", "<p>supplier %s today, %s</p>" % (ORG_SHORT.replace("ö", "&ouml"), MARK))},
    {"id": "r2-md-span-newline-split", "cls": "name", "carrier": "md body, a span tag with a line break inside the word (span flips the reader to html)", "values": [SHORT],
     "build": txt("a.md", 'the %s<span\nclass="a">%s</span> team, %s\n' % (SHORT[:3], SHORT[3:], MARK))},
    {"id": "r2-md-tag-150-split", "cls": "name", "carrier": "md body, an inline tag of 150 chars inside the word", "values": [SHORT],
     "build": txt("a.md", 'the %s<em data-x="%s">%s</em> team, %s\n' % (SHORT[:3], _STYLE150, SHORT[3:], MARK))},
    {"id": "r2-md-long-tag-split-lower", "cls": "name", "carrier": "md body, an inline tag over 200 chars inside the word, no capitalised words in it", "values": [SHORT],
     "build": txt("a.md", 'the %s<em data-x="%s">%s</em> team, %s\n' % (SHORT[:3], _LOWER_STYLE, SHORT[3:], MARK))},
    {"id": "r2-svg-tspan-long-style-lower", "cls": "name", "carrier": "svg text, one letter per tspan, style attribute over 200 chars without capitalised words", "values": [SHORT],
     "build": svg(_TSPAN_STYLE_LOWER)},
    {"id": "r2-svg-text-per-letter-lines", "cls": "name", "carrier": "svg, one text element per letter, each on its own line", "values": [SHORT],
     "build": txt("figure.svg", '<svg xmlns="http://www.w3.org/2000/svg">\n%s\n</svg>\n'
                  % "\n".join('<text x="%d" y="30">%s</text>' % (10 + 12 * i, c) for i, c in enumerate(SHORT)))},
    {"id": "r2-tender-digit-prefix-glued", "cls": "other", "carrier": "txt body, tender id glued after a digit", "values": [TENDER],
     "build": txt("a.txt", "see 2%s for it, %s\n" % (TENDER, MARK))},
    {"id": "r2-rtf-unicode-with-fallback", "cls": "name", "carrier": "rtf body, \\uN with the \\'hh fallback that Word writes", "values": [_RTF_U_FALLBACK_ORG, ORG_SHORT],
     "build": txt("a.rtf", "{\\rtf1\\ansi supplier %s today, %s}\n" % (_RTF_U_FALLBACK_ORG, MARK))},
    {"id": "r2-rtf-hex-ascii", "cls": "name", "carrier": "rtf body, \\'hh escapes for ASCII letters", "values": [SHORT],
     "build": txt("a.rtf", "{\\rtf1\\ansi the %s team, %s}\n" % (_RTF_HEX_SHORT, MARK))},
    {"id": "r2-md-numbered-table-letters", "cls": "name", "carrier": "md table, a row number and one letter per row (hand check)", "values": [SHORT],
     "build": txt("a.md", "| n | letter |\n| --- | --- |\n%s\n\n%s\n" % ("\n".join("| %d | %s |" % (i + 1, c) for i, c in enumerate(SHORT)), MARK))},
    {"id": "r2-cyrillic-phonetic-lower", "cls": "name", "carrier": "txt body, phonetic Cyrillic spelling in lower case (no candidate)", "values": [FULL, _CYR_PHONETIC_LOWER],
     "build": txt("a.txt", "offer for %s, %s\n" % (_CYR_PHONETIC_LOWER, MARK))},
    {"id": "r2-zwj-no-hit", "cls": "name", "carrier": "txt body, a zero width joiner and a tag character inside innocent words, no hit (hand check)", "values": [],
     "build": txt("a.txt", "stage two of the in‍vented pl\U000e0061an\n")},
    {"id": "r2-tagchars-md", "cls": "name", "carrier": "md body, the short form as tag characters after a heading, no other hit (hand check)", "values": [SHORT],
     "build": txt("a.md", "# plan\n\n%s %s\n" % (MARK, tags(SHORT)))},
]

# ============================================================================ round 3
_L36_TEXT = "stage two of the " + TENDER + " " + MARK          # 27 bytes per line of 36 chars: the id is cut before its last two digits
_L36_B64 = b64(_L36_TEXT)
_L36_LINES = "\n".join(_L36_B64[i:i + 36] for i in range(0, len(_L36_B64), 36))
_SUPER_DIGITS = dict(zip("0123456789", "⁰¹²³⁴⁵⁶⁷⁸⁹"))
_MATH_DIGITS = {d: chr(0x1D7CE + int(d)) for d in "0123456789"}
_ARABIC_DIGITS = {d: chr(0x0660 + int(d)) for d in "0123456789"}
_TENDER_SUPER = "".join(_SUPER_DIGITS.get(c, c) for c in TENDER)
_TENDER_MATH = "".join(_MATH_DIGITS.get(c, c) for c in TENDER)
_TENDER_ARABIC = "".join(_ARABIC_DIGITS.get(c, c) for c in TENDER)

CASES += [
    {"id": "r3-b64-lines-36-cut", "cls": "other", "carrier": "txt body, base64 in lines of 36 chars (under the 40 MIME threshold), the tender id cut at a line boundary", "values": [TENDER, TENDER[:-2]],
     "build": txt("a.txt", "%s\n" % _L36_LINES)},
    {"id": "r3-html-em-long-tag-split", "cls": "name", "carrier": "html body, an inline tag over 200 chars inside the word", "values": [SHORT],
     "build": html("a.html", '<p>the %s<em data-x="%s">%s</em> team, %s</p>' % (SHORT[:3], _LOWER_STYLE, SHORT[3:], MARK))},
    {"id": "r3-html-cdata-split", "cls": "name", "carrier": "html body, an empty CDATA section inside the word", "values": [SHORT],
     "build": html("a.html", "<p>the %s<![CDATA[]]>%s team, %s</p>" % (SHORT[:3], SHORT[3:], MARK))},
    {"id": "r3-md-pi-short-split", "cls": "name", "carrier": "md body, a six-char processing instruction inside the word (gap under MAX_GAP)", "values": [SHORT],
     "build": txt("a.md", "the %s<?x?>%s team, %s\n" % (SHORT[:3], SHORT[3:], MARK))},
    {"id": "r3-svg-tspan-multiline-with-anchor", "cls": "name", "carrier": "svg with a link element (flips the reader to html), one letter per multi-line tspan", "values": [SHORT],
     "build": txt("figure.svg", '<svg xmlns="http://www.w3.org/2000/svg"><a href="https://example.org/">link</a>\n<text>%s</text></svg>\n' % _TSPAN_MULTI)},
    {"id": "r3-html-inline-svg-tspan-multiline", "cls": "name", "carrier": "html body with an inline svg, one letter per multi-line tspan", "values": [SHORT],
     "build": html("a.html", "<p>%s</p><svg><text>%s</text></svg>" % (MARK, _TSPAN_MULTI))},
    {"id": "r3-filenumber-upper-prefix-glued", "cls": "other", "carrier": "txt body, file number glued after an upper-case prefix", "values": [FILE_NUMBER],
     "build": txt("a.txt", "see AZ%s for it, %s\n" % (FILE_NUMBER, MARK))},
    {"id": "r3-tender-superscript-digits", "cls": "other", "carrier": "txt body, tender id with superscript digits", "values": [TENDER],
     "build": txt("a.txt", "see %s for it, %s\n" % (_TENDER_SUPER, MARK))},
    {"id": "r3-tender-math-digits", "cls": "other", "carrier": "txt body, tender id with mathematical bold digits", "values": [TENDER],
     "build": txt("a.txt", "see %s for it, %s\n" % (_TENDER_MATH, MARK))},
    {"id": "r3-tender-arabic-indic-digits", "cls": "other", "carrier": "txt body, tender id with Arabic-Indic digits", "values": [TENDER, _TENDER_ARABIC],
     "build": txt("a.txt", "see %s for it, %s\n" % (_TENDER_ARABIC, MARK))},
    {"id": "r3-md-entity-rlo", "cls": "name", "carrier": "md body, RLO as a numeric entity plus reversed letters (a Markdown viewer renders the entity; the harness looks for the raw character)", "values": [SHORT],
     "build": txt("a.md", "the &#8238;%s&#8236; team, %s\n" % (SHORT[::-1], MARK))},
    {"id": "r3-encw-utf7-q-org", "cls": "name", "carrier": "txt body, encoded word charset utf-7, Q", "values": [ORG_SHORT],
     "build": txt("a.txt", "s =?utf-7?q?%s?= today, %s\n" % (_UTF7_ORG, MARK))},
    {"id": "r3-tagchars-with-hit", "cls": "name", "carrier": "txt body, the short form as tag characters plus the tender id elsewhere (a hit; hand check)", "values": [SHORT],
     "build": txt("a.txt", "%s %s, see %s\n" % (MARK, tags(SHORT), TENDER))},
]

# ============================================================================ round 4
_HEX_UTF16_SHORT = SHORT.encode("utf-16-le").hex()
_HEX_DUMP_SHORT = " ".join("%02x" % b for b in SHORT.encode())
_TENDER_IZHITSA = "Ѵ" + TENDER[1:]                     # Cyrillic izhitsa for V
_REGIONAL_SHORT = "".join(chr(0x1F1E6 + ord(c.lower()) - 97) for c in SHORT)
_UTF32BE_FULL_NOBOM = base64.b64encode(FULL.encode("utf-32-be")).decode()
_TSPAN_CLOSE_MULTI = "".join('<tspan x="%d">%s</tspan\n>' % (10 + 12 * i, c) for i, c in enumerate(SHORT))

CASES += [
    {"id": "r4-hex-utf16", "cls": "name", "carrier": "txt body, hex of the short form in UTF-16", "values": [SHORT],
     "build": txt("a.txt", "h %s, %s\n" % (_HEX_UTF16_SHORT, MARK))},
    {"id": "r4-hex-spaced-dump", "cls": "name", "carrier": "txt body, hex dump with a space per byte", "values": [_HEX_DUMP_SHORT, SHORT],
     "build": txt("a.txt", "h %s, %s\n" % (_HEX_DUMP_SHORT, MARK))},
    {"id": "r4-tender-izhitsa-v", "cls": "other", "carrier": "txt body, tender id with Cyrillic izhitsa for V (not in the table)", "values": [TENDER, _TENDER_IZHITSA],
     "build": txt("a.txt", "see %s for it, %s\n" % (_TENDER_IZHITSA, MARK))},
    {"id": "r4-json-rlo-escape", "cls": "name", "carrier": "json string, RLO as a \\u escape plus reversed letters (hand check)", "values": [SHORT],
     "build": txt("a.json", '{"customer": "\\u202e%s\\u202c", "note": "%s"}\n' % (SHORT[::-1], MARK))},
    {"id": "r4-mixed-scripts-in-table", "cls": "name", "carrier": "txt body, Greek chi and Cyrillic o inside the word (both in the table)", "values": [SHORT],
     "build": txt("a.txt", "the %s team, %s\n" % (SHORT.replace("x", "χ").replace("o", "о"), MARK))},
    {"id": "r4-regional-indicators", "cls": "name", "carrier": "txt body, regional indicator symbols spelling the short form", "values": [SHORT, _REGIONAL_SHORT],
     "build": txt("a.txt", "the %s team, %s\n" % (_REGIONAL_SHORT, MARK))},
    {"id": "r4-md-b-newline-split", "cls": "name", "carrier": "md body, a b tag with a line break and a class inside the word", "values": [SHORT],
     "build": txt("a.md", 'the %s<b\nclass="a">%s</b> team, %s\n' % (SHORT[:3], SHORT[3:], MARK))},
    {"id": "r4-svg-closing-tag-newline", "cls": "name", "carrier": "svg text, one letter per tspan, a line break inside every closing tag", "values": [SHORT],
     "build": svg(_TSPAN_CLOSE_MULTI)},
    {"id": "r4-b64-utf32-be-nobom", "cls": "name", "carrier": "txt body, base64 of the full form in UTF-32 big endian without a BOM", "values": [FULL],
     "build": txt("a.txt", "tag %s, %s\n" % (_UTF32BE_FULL_NOBOM, MARK))},
    {"id": "r4-html-template-input-unseen", "cls": "name", "carrier": "html template element and input value attribute", "values": [FULL],
     "visible": ["marker-template-one", "marker-input-one"],
     "build": html("a.html", '<p>%s</p><template><p>marker-template-one %s</p></template><input type="text" value="marker-input-one %s">' % (MARK, FULL, FULL))},
    {"id": "r4-md-numbered-list-tender-parts", "cls": "other", "carrier": "md numbered list, one piece of the tender id per item (hand check, digits between)", "values": [TENDER],
     "build": txt("a.md", "%s\n\n%s\n" % (MARK, "\n".join("%d. %s" % (i + 1, p + ("-" if i < 2 else "")) for i, p in enumerate(TENDER.split("-")))))},
]

# ============================================================================ round 5
_B64_FULL_LINES40 = "\n".join(b64(FULL + ", " + MARK + ", " + MARK)[i:i + 40] for i in range(0, len(b64(FULL + ", " + MARK + ", " + MARK)), 40))
_B64_TENDER76 = b64(_QUOTED_TEXT)
_B64_TENDER76_LINES = "\n".join(_B64_TENDER76[i:i + 76] for i in range(0, len(_B64_TENDER76), 76))

CASES += [
    {"id": "r5-b64-lines-40-joined", "cls": "name", "carrier": "txt body, base64 in lines of exactly 40 chars (the MIME threshold), the form cut at a line boundary", "values": [FULL],
     "build": txt("a.txt", "%s\n" % _B64_FULL_LINES40)},
    {"id": "r5-b64-lines-76-unquoted", "cls": "other", "carrier": "txt body, MIME base64 lines of 76 chars without quoting, the tender id cut at the line boundary", "values": [TENDER, TENDER[5:]],
     "build": txt("a.txt", "attachment:\n%s\n" % _B64_TENDER76_LINES)},
    {"id": "r5-svg-tspan-multiline-xhtml", "cls": "name", "carrier": "xhtml file (html reader by suffix), one letter per multi-line tspan", "values": [SHORT],
     "build": txt("figure.xhtml", '<svg xmlns="http://www.w3.org/2000/svg"><text>%s</text></svg>\n' % _TSPAN_MULTI)},
    {"id": "r5-encw-cp037-b-full", "cls": "name", "carrier": "txt body, encoded word charset cp037 (EBCDIC), B", "values": [FULL],
     "build": txt("a.txt", "s =?cp037?b?%s?= today, %s\n" % (base64.b64encode(FULL.encode("cp037")).decode(), MARK))},
    {"id": "r5-md-img-alt-raw", "cls": "name", "carrier": "md body, an img tag with the form in alt (no other html tag, so the text reader)", "values": [FULL],
     "build": txt("a.md", '%s\n\n<img src="x.png" alt="%s">\n' % (MARK, FULL))},
    {"id": "r5-csv-tender-split-cells", "cls": "other", "carrier": "csv, the tender id over three cells", "values": [TENDER],
     "build": txt("a.csv", "a,b,c\n%s\n" % ",".join(p + ("-" if i < 2 else "") for i, p in enumerate(TENDER.split("-"))))},
    {"id": "r5-yaml-literal-letters-indent2", "cls": "name", "carrier": "yaml literal block, one letter per line indented two spaces", "values": [SHORT],
     "build": txt("a.yaml", "note: %s\nname: |\n%s\n" % (MARK, "\n".join("  " + c for c in SHORT)))},
    {"id": "r5-ics-folded-person", "cls": "name", "carrier": "ics line folded (CRLF + space) inside the surname", "values": [PERSON],
     "build": raw("a.ics", ("BEGIN:VCALENDAR\r\nBEGIN:VEVENT\r\nSUMMARY:%s\r\nDESCRIPTION:call with %s\r\n %s\r\nEND:VEVENT\r\nEND:VCALENDAR\r\n"
                            % (MARK, PERSON[:15], PERSON[15:])).encode())},
    {"id": "r5-json-tender-escaped-dashes", "cls": "other", "carrier": "json string, the dashes of the tender id as \\u002d", "values": [TENDER],
     "build": txt("a.json", '{"ref": "%s", "stage": 2}\n' % TENDER.replace("-", "\\u002d"))},
    {"id": "r5-tender-lower-prefix-glued", "cls": "other", "carrier": "txt body, tender id glued after a lower-case prefix (case change boundary)", "values": [TENDER],
     "build": txt("a.txt", "see ref%s for it, %s\n" % (TENDER, MARK))},
]


# What tests/test_redteam_pack.py accepts besides the fixture forms. DERIVED: strings built from a fixture form
# (a typo, an encoding, a transliteration). INVENTED: structured values this module builds, invented but valid
# in shape (documentation ranges, fake digit families). A value may carry one of them whole or in part.
DERIVED = (_B32_FULL, _GZ_FULL, _UU_FULL, _B64_SHORT, _UTF7_ORG, _UTF7_SHORT_ALL, _QP_SHORT, _XESC_SHORT, _RTF_U_ORG,
           _RTF_U_SHORT, _RTF_U_FALLBACK_ORG, _RTF_HEX_SHORT, _PUNY_ORG, _LEET_FULL, _CYR_PHONETIC, _CYR_PHONETIC_LOWER,
           _MODIFIER_SHORT, _SMALL_CAP_SHORT, _MATH_BOLD_SHORT, _UTF32_FULL_NOBOM, _UTF32BE_FULL_NOBOM, _TENDER_SUPER,
           _TENDER_MATH, _TENDER_ARABIC, _HEX_UTF16_SHORT, _HEX_DUMP_SHORT, _TENDER_IZHITSA, _REGIONAL_SHORT,
           SHORT.replace("w", "ѡ"), SHORT.replace("y", "ү"), "Ƶ" + SHORT[1:])
INVENTED = ()
