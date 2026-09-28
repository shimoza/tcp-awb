"""Normalise text before matching and keep a map back to the original positions.

Steps, in this order: encoded words of mail headers (=?utf-8?b?...?=), JSON, Python (\\xhh) and RTF escapes,
quoted-printable runs, HTML entities and URL encoding (twice, for a doubly encoded value) decoded into the
character; CRLF and lone CR to LF; terminal colour sequences (ESC [ ... m, also written as their escapes)
removed; the overstrike bold and underline of a man page reduced to the character; every default-ignorable
character removed (zero width characters, invisible operators, bidi marks, variation selectors, tag
characters); NFC; hyphenation at a line end rejoined; soft hyphens removed; Unicode spaces turned into a plain
space and Unicode line separators into LF.

Every step keeps, for every character of the result, the range of original characters it came from.
`to_original[i]` is the start of that range and `to_original_end[i]` the index just after it. A decoded
entity such as "&auml;" gives one character whose range covers all six original characters. A removed
character leaves no trace. A composed character (NFC) covers the base and its marks.
"""
from __future__ import annotations

import base64
import binascii
import html
import quopri
import re
import unicodedata
from dataclasses import dataclass, field


@dataclass
class Normalized:
    text: str
    to_original: list[int]
    # index in the original text just after the characters that produced normalized character i
    to_original_end: list[int] = field(default_factory=list)


_Triples = list[tuple[str, int, int]]

_ENTITY_RE = re.compile(r"&(?:#[0-9]+;?|#[xX][0-9a-fA-F]+;?|[A-Za-z][A-Za-z0-9]{1,31};)")
"""A character reference: a numeric one also without its semicolon and with any number of digits, the way
html.unescape and every browser read it; a named one only with the semicolon (his decision, a bare &auml in
prose stays as it is)."""
_PERCENT_RE = re.compile(r"(?:%[0-9A-Fa-f]{2})+")
_CR_RE = re.compile(r"\r\n?")
# the Unicode default-ignorable code points except the soft hyphen, which the hyphenation step needs; they
# render as nothing, so a name with one of them after every letter looks like the name
_INVISIBLE_RE = re.compile(
    "[\u034f\u061c\u115f\u1160\u17b4\u17b5\u180b-\u180f\u200b-\u200f\u202a-\u202e\u2060-\u206f"
    "\u3164\ufe00-\ufe0f\ufeff\uffa0\ufff0-\ufff8\U0001bca0-\U0001bca3\U0001d173-\U0001d17a"
    "\U000e0000-\U000e0fff]+"
)
_ENCODED_WORD_RE = re.compile(r"=\?([A-Za-z0-9._-]{1,40})(?:\*[A-Za-z-]{1,20})?\?([BbQq])\?([^?\s]{1,2000})\?=")
_JSON_ESCAPE_RE = re.compile(r"(?:\\u[0-9a-fA-F]{4})+")
_RTF_ESCAPE_RE = re.compile(r"(?:\\'[0-9a-fA-F]{2})+")
_RTF_UNICODE_RE = re.compile(r"\\u(-?[0-9]{1,6}) ?(?:\\'[0-9a-fA-F]{2}|[^\\{}\s])?")
"""The RTF unicode escape \\uN with its one fallback character (\\uc1, the default)."""
_HEX_ESCAPE_RE = re.compile(r"(?:\\x[0-9a-fA-F]{2})+")
_CSI_RE = re.compile(r"(?:\x1b|\\u001[bB]|\\x1[bB]|\\e|\\033)\[[0-9;?]*[ -/]*[@-~]|\x1b[@-Z\\-_]")
"""Terminal control sequences (the colours of grep, ls and the like) pasted into a text, also written as the
literal escapes of a notebook, a log or a shell script; grep --color puts one around the matched part of a word."""
_OVERSTRIKE_RE = re.compile(r"(.)\x08(.)")
"""The bold and underline of a formatted man page: a character, a backspace, the same character or an underscore."""
_QP_SOFT_BREAK_RE = re.compile(r"=[ \t]*\n")
_PUNYCODE_RE = re.compile(r"(?<![A-Za-z0-9-])xn--[A-Za-z0-9-]{1,59}(?![A-Za-z0-9-])")
"""An internationalised host label as the wire writes it (xn--...): decoded to its letters."""
_QP_RE = re.compile(r"(?:=[0-9A-F]{2})+")
PERCENT_PASSES = 3
"""URL decoding runs this often, so that a doubly encoded value (%255A) is decoded too."""
_SOFT_HYPHEN_RE = re.compile("­+")
# a letter, a hyphen (also a soft hyphen or U+2010), optional spaces, one line break, optional spaces,
# then a letter that is checked for lower case in code
_HYPHENATION_RE = re.compile(r"(?<=[^\W\d_])[-­‐][ \t ]*\n[ \t ]*(?=([^\W\d_]))")
_SPACE_RE = re.compile("[   -   　  ]")
_LINE_SEPARATORS = "  "
_MARK_CATEGORIES = ("Mn", "Mc", "Me")


def _rewrite(chars, starts, ends, pattern, fn):
    """Apply `fn` to every match of `pattern` over the current text.

    `fn(match, starts_of_match, ends_of_match)` returns the replacement as (char, start, end) triples or
    None to keep the match as it is. Unmatched stretches are copied through in one piece.
    """
    s = "".join(chars)
    if pattern.search(s) is None:
        return chars, starts, ends
    out_c: list[str] = []
    out_s: list[int] = []
    out_e: list[int] = []
    last = 0
    for m in pattern.finditer(s):
        a, b = m.span()
        rep = fn(m, starts[a:b], ends[a:b])
        if rep is None:
            continue
        out_c.extend(chars[last:a])
        out_s.extend(starts[last:a])
        out_e.extend(ends[last:a])
        for c, st, en in rep:
            out_c.append(c)
            out_s.append(st)
            out_e.append(en)
        last = b
    out_c.extend(chars[last:])
    out_s.extend(starts[last:])
    out_e.extend(ends[last:])
    return out_c, out_s, out_e


def _entity(m, st, en) -> _Triples | None:
    decoded = html.unescape(m.group(0))
    if decoded == m.group(0):
        return None
    return [(c, st[0], en[-1]) for c in decoded]


def _has_control(text: str) -> bool:
    return any(unicodedata.category(c) == "Cc" and c not in "\t\n\r" for c in text)


def _letters_around(m) -> int:
    """How many sides of the match (0, 1 or 2) touch a letter."""
    s, a, b = m.string, m.start(), m.end()
    return int(a > 0 and s[a - 1].isalpha()) + int(b < len(s) and s[b].isalpha())


def _bytes_to_chars(raw: bytes, width: int, st, en, sides: int, m) -> _Triples | None:
    """Decode `raw` (each byte written with `width` characters) as UTF-8, else as cp1252 when every decoded
    character is a letter and at least `sides` sides of the run touch a letter (%F6 inside a word). Control
    characters are never decoded."""
    try:
        decoded = raw.decode("utf-8")
        sizes = [len(c.encode("utf-8")) for c in decoded]
    except UnicodeDecodeError:
        decoded = raw.decode("cp1252", errors="replace")
        if not all(c.isalpha() for c in decoded) or _letters_around(m) < sides:
            return None
        sizes = [1] * len(decoded)
    if _has_control(decoded):
        return None
    out: _Triples = []
    k = 0
    for c, size in zip(decoded, sizes):
        out.append((c, st[width * k], en[width * (k + size) - 1]))
        k += size
    return out


def _percent(m, st, en) -> _Triples | None:
    raw = bytes.fromhex(m.group(0).replace("%", ""))
    return _bytes_to_chars(raw, 3, st, en, 1, m)


def _json_escape(m, st, en) -> _Triples | None:
    """JSON and Python escapes such as \\u00f6 (surrogate pairs joined) decoded into the character."""
    units = [int(m.group(0)[i + 2:i + 6], 16) for i in range(0, len(m.group(0)), 6)]
    out: _Triples = []
    i = 0
    while i < len(units):
        u = units[i]
        a, b = st[6 * i], en[6 * i + 5]
        if 0xD800 <= u < 0xDC00 and i + 1 < len(units) and 0xDC00 <= units[i + 1] < 0xE000:
            c = chr(0x10000 + ((u - 0xD800) << 10) + (units[i + 1] - 0xDC00))
            b = en[6 * (i + 1) + 5]
            i += 2
        else:
            c = chr(u)
            i += 1
        if 0xD800 <= ord(c) < 0xE000 or _has_control(c):
            return None
        out.append((c, a, b))
    return out


def _hex_escape(m, st, en) -> _Triples | None:
    """Python and C escapes such as \\xf6 or \\xc3\\xb6, one byte each, decoded as UTF-8 else as cp1252 when the
    run gives letters only. Control characters are never decoded."""
    raw = bytes(int(m.group(0)[i + 2:i + 4], 16) for i in range(0, len(m.group(0)), 4))
    return _bytes_to_chars(raw, 4, st, en, 1, m)


def _overstrike(m, st, en) -> _Triples | None:
    a, b = m.group(1), m.group(2)
    if a == b or b == "_":
        return [(a, st[0], en[-1])]
    if a == "_":
        return [(b, st[0], en[-1])]
    return None


def _rtf_escape(m, st, en) -> _Triples | None:
    """RTF hex escapes such as \\'f6, one byte each, decoded as cp1252 (the RTF default code page)."""
    raw = bytes(int(m.group(0)[i + 2:i + 4], 16) for i in range(0, len(m.group(0)), 4))
    decoded = raw.decode("cp1252", errors="replace")
    if _has_control(decoded):
        return None
    return [(c, st[4 * k], en[4 * k + 3]) for k, c in enumerate(decoded)]


def _rtf_unicode(m, st, en) -> _Triples | None:
    n = int(m.group(1))
    c = chr(n % 65536) if n < 0 else chr(n) if n < 0x110000 else None
    if c is None or _has_control(c) or 0xD800 <= ord(c) < 0xE000:
        return None
    return [(c, st[0], en[-1])]


def _quoted_printable(m, st, en) -> _Triples | None:
    """A quoted-printable run such as =C3=B6. Decoded only when it gives a non-ASCII character, so that code
    such as a=3D1 or color=FF0000 stays as it is."""
    raw = bytes.fromhex(m.group(0).replace("=", ""))
    if raw.isascii():
        return None
    return _bytes_to_chars(raw, 3, st, en, 2, m)


def _encoded_word(m, st, en) -> _Triples | None:
    """An RFC 2047 encoded word (=?utf-8?b?...?= or =?iso-8859-1?q?...?=) pasted into text."""
    charset, enc, payload = m.group(1), m.group(2).upper(), m.group(3)
    try:
        if enc == "B":
            raw = base64.b64decode(payload + "=" * (-len(payload) % 4), validate=True)
        else:
            raw = quopri.decodestring(payload.replace("_", " ").encode("ascii"), header=True)
        decoded = raw.decode(charset)
    except (binascii.Error, ValueError, LookupError, UnicodeError):
        return None
    if not decoded or _has_control(decoded):
        return None
    return [(c, st[0], en[-1]) for c in decoded]


def _punycode(m, st, en) -> _Triples | None:
    """The label, or its longest prefix that ends before a hyphen (a label glued to a suffix, xn--abc-kua-web01),
    decoded; the rest stays as it is."""
    label = m.group(0)
    cut = len(label)
    while cut > 4:
        try:
            decoded = label[:cut].encode("ascii").decode("idna")
        except (UnicodeError, ValueError):
            decoded = None
        if decoded and decoded != label[:cut] and not _has_control(decoded):
            out = [(c, st[0], en[cut - 1]) for c in decoded]
            out.extend((label[i], st[i], en[i]) for i in range(cut, len(label)))
            return out
        cut = label.rfind("-", 0, cut)
    return None


def _to_lf(m, st, en) -> _Triples:
    return [("\n", st[0], en[-1])]


def _drop(m, st, en) -> _Triples:
    return []


def _hyphenation(m, st, en) -> _Triples | None:
    return [] if m.group(1).islower() else None


def _space(m, st, en) -> _Triples:
    c = "\n" if m.group(0) in _LINE_SEPARATORS else " "
    return [(c, st[0], en[0])]


_NON_ASCII_RE = re.compile(r"[^\x00-\x7f]+")


def _is_mark(c: str) -> bool:
    return unicodedata.category(c) in _MARK_CATEGORIES


def _nfc(chars, starts, ends):
    """NFC in chunks of one base character plus its combining marks, so positions stay exact.

    ASCII never takes part in composition, so ASCII stretches are copied through in one piece and only
    the non-ASCII runs are walked character by character. A run that starts with a combining mark takes
    the character before it as its base.
    """
    s = "".join(chars)
    if unicodedata.is_normalized("NFC", s):
        return chars, starts, ends
    out_c: list[str] = []
    out_s: list[int] = []
    out_e: list[int] = []
    last = 0
    for m in _NON_ASCII_RE.finditer(s):
        a, b = m.span()
        if a > last and _is_mark(s[a]):
            a -= 1
        out_c.extend(chars[last:a])
        out_s.extend(starts[last:a])
        out_e.extend(ends[last:a])
        i = a
        while i < b:
            j = i + 1
            while j < b and _is_mark(chars[j]):
                j += 1
            chunk = s[i:j]
            composed = unicodedata.normalize("NFC", chunk)
            if composed == chunk or len(composed) == len(chunk):
                out_c.extend(composed)
                out_s.extend(starts[i:j])
                out_e.extend(ends[i:j])
            else:
                for c in composed:
                    out_c.append(c)
                    out_s.append(starts[i])
                    out_e.append(ends[j - 1])
            i = j
        last = b
    out_c.extend(chars[last:])
    out_s.extend(starts[last:])
    out_e.extend(ends[last:])
    return out_c, out_s, out_e


_DECODE = (
    (_ENCODED_WORD_RE, _encoded_word),
    (_JSON_ESCAPE_RE, _json_escape),
    (_HEX_ESCAPE_RE, _hex_escape),
    (_RTF_UNICODE_RE, _rtf_unicode),
    (_RTF_ESCAPE_RE, _rtf_escape),
    (_QP_RE, _quoted_printable),
    (_ENTITY_RE, _entity),
    (_PUNYCODE_RE, _punycode),
)
_BEFORE_NFC = (
    (_CR_RE, _to_lf),
    (_CSI_RE, _drop),
    (_OVERSTRIKE_RE, _overstrike),
    (_INVISIBLE_RE, _drop),
)
_AFTER_NFC = (
    (_HYPHENATION_RE, _hyphenation),
    (_SOFT_HYPHEN_RE, _drop),
    (_SPACE_RE, _space),
)


def normalize(text: str) -> Normalized:
    """Normalise `text` for matching and record where every resulting character came from."""
    chars = list(text)
    starts = list(range(len(text)))
    ends = list(range(1, len(text) + 1))
    if _QP_RE.search(text):
        # a text with quoted printable escapes also has its soft line breaks (= at the end of a line)
        chars, starts, ends = _rewrite(chars, starts, ends, _QP_SOFT_BREAK_RE, _drop)
    for pattern, fn in _DECODE:
        chars, starts, ends = _rewrite(chars, starts, ends, pattern, fn)
    for _ in range(2):
        # a JSON escape whose backslash is itself an escape (\u005cu005a) gives another escape: read it again
        before = len(chars)
        chars, starts, ends = _rewrite(chars, starts, ends, _JSON_ESCAPE_RE, _json_escape)
        if len(chars) == before:
            break
    for _ in range(PERCENT_PASSES):
        before = len(chars)
        chars, starts, ends = _rewrite(chars, starts, ends, _PERCENT_RE, _percent)
        if len(chars) == before:
            break
    for pattern, fn in _BEFORE_NFC:
        chars, starts, ends = _rewrite(chars, starts, ends, pattern, fn)
    chars, starts, ends = _nfc(chars, starts, ends)
    for pattern, fn in _AFTER_NFC:
        chars, starts, ends = _rewrite(chars, starts, ends, pattern, fn)
    return Normalized("".join(chars), starts, ends)


def strip_invisible(text: str) -> str:
    """`text` without the characters a viewer does not show but acts on: bidi controls, zero width and tag
    characters, terminal control sequences. What a session reads is then what a reader sees."""
    return _INVISIBLE_RE.sub("", _CSI_RE.sub("", text))


def original_span(n: Normalized, start: int, end: int) -> tuple[int, int]:
    """The range of the original text that normalized[start:end] came from."""
    size = len(n.to_original)
    start = max(0, min(start, size))
    end = max(start, min(end, size))
    has_ends = len(n.to_original_end) == size
    if start >= end:
        if start < size:
            pos = n.to_original[start]
        elif has_ends and size:
            pos = n.to_original_end[-1]
        elif size:
            pos = n.to_original[-1] + 1
        else:
            pos = 0
        return (pos, pos)
    first = n.to_original[start]
    last = n.to_original_end[end - 1] if has_ends else n.to_original[end - 1] + 1
    return (first, last)
