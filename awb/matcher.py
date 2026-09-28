"""Find registered written forms in a text.

Matching is case-insensitive and runs over a folded view of the text: compatibility forms (full width and
mathematical letters, ligatures) become plain letters and accents are dropped. Letters of other scripts
that look like Latin letters (a Cyrillic o, a Greek capital Z) count as those letters. The forms are folded
the same way. Positions always refer to the text that was given, never to the view.

A form matches at a boundary: not preceded and not followed by a letter or a digit, so underscores, dots,
slashes, hyphens, quotes and brackets all count as boundaries and names inside file names, host names and
identifiers are found. A whitespace run inside a form matches any whitespace run in the text, including a
line break. A change of case also counts as a boundary (an acronym inside srv<ACRONYM> or <ACRONYM>Prod01).
An acronym written in capitals may be followed by digits. A form that starts or ends with a digit (a
file number) only needs no digit next to it. Longer variants win over shorter ones and spans never overlap.

A form with at least five letters is also searched as its lower-case, space-free spelling without the
boundary rule, which catches names glued into identifiers (a host name such as <name>logistik-backup, a
CamelCase id such as <Name>Prod01).

A form of three or four letters (an acronym) is also found glued inside an identifier token (a run of letters
and digits) when the rest of the token, before and after it, is made of digits and of the affixes of
rules/glue-affixes.txt: <acronym>backup, <acronym>01, srv<acronym>, <acronym>prod02. Inside an ordinary word
(letters before or after it that are not such an affix) it is not found.

A form with at least four letters and digits is also found when its letters are spread out: separated by
spaces, dots, dashes, line breaks, table borders, Markdown emphasis or html tags (a spaced heading, a name
split over two table cells). A form of three letters is found spread out only with one or two characters
between its letters on one line (an acronym written with dots).

Nothing here writes a matched value anywhere.
"""
from __future__ import annotations

import bisect
import functools
import itertools
import re
import unicodedata
from dataclasses import dataclass
from pathlib import Path

MIN_GLUED_LETTERS = 5
SHORT_GLUED_LETTERS = (3, 4)
"""A form of this many letters (and nothing else) is found glued between digits and the affixes below."""
GLUE_AFFIX_FILE = Path(__file__).resolve().parent.parent / "rules" / "glue-affixes.txt"
_DEFAULT_AFFIXES = (
    "backup", "bkp", "prod", "dev", "test", "stage", "staging", "vpn", "db", "sql", "srv", "server", "web", "app",
    "fw", "lb", "net", "vpc", "mail", "dc", "k8s", "cluster", "data", "share", "log", "mon", "gw", "api", "portal",
    "cloud", "ext", "int",
)
"""The affixes when rules/glue-affixes.txt cannot be read (an installed copy without the rules folder), so that
the rule never silently stops."""


def _load_affixes() -> tuple[str, ...]:
    try:
        lines = GLUE_AFFIX_FILE.read_text(encoding="utf-8").splitlines()
    except OSError:
        lines = list(_DEFAULT_AFFIXES)
    words = {w.strip().lower() for w in lines if w.strip() and not w.strip().startswith("#")}
    words = {w for w in words if re.fullmatch(r"[a-z0-9]+", w)}
    return tuple(sorted(words or _DEFAULT_AFFIXES, key=lambda w: (-len(w), w)))


GLUE_AFFIXES = _load_affixes()
_GLUE_REST_RE = re.compile(r"(?:%s|[0-9])*" % "|".join(re.escape(a) for a in GLUE_AFFIXES))
"""What may stand before and after a short form inside one identifier token: digits and affixes, in any order.
One digit per step, so that a long run of digits cannot make the search backtrack."""
MAX_GLUE_REST = 64
"""Longest stretch before or after a short form that is still read as digits and affixes."""
MIN_SPREAD = 4
"""A form with at least this many letters and digits is found with any separators between its letters."""
MAX_GAP = 8
"""Most characters (html tags not counted) between two letters of a spread-out form."""

_WS_RE = re.compile(r"\s+")
_LETTER_RE = re.compile(r"[^\W\d_]")
_NOT_AFTER_ALNUM = r"(?<![^\W_])"
_NOT_BEFORE_ALNUM = r"(?![^\W_])"
_NON_ASCII_RE = re.compile(r"[^\x00-\x7f]+")
_ALNUM_RUN_RE = re.compile(r"[^\W_]+")
_TAG_ATTRS = r"""(?:\s(?:(?>"[^"<]*")|(?>'[^'<]*')|[^<>])*+)?"""
"""The attribute part of a tag: a quoted value is read whole, so a > inside it does not end the tag (the way a
browser reads it); atomic and possessive, so a stray quote or a long value costs no backtracking, and no cap
on its length (a data: URI in src is one tag)."""
_MARKUP_RE = re.compile(
    r"<!--.*?-->|<!\[CDATA\[.*?\]\]>|<\?.*?\?>|<![^<>]{0,200}>|</?[A-Za-z][A-Za-z0-9:-]*%s/?>" % _TAG_ATTRS, re.S)
"""Markup a viewer does not show: comments, CDATA sections, processing instructions, declarations and tags,
also pretty printed over several lines and with long style attributes. A tag is a tag name followed by
attributes or the closing bracket: an address in angle brackets is not markup."""
_MARKUP_FILL = "\x01"
_TABLE_CHARS = "|-:\n\t "
"""Characters of a Markdown table border and of its header separator line, not counted as a gap."""

# letters that no decomposition turns into a plain Latin letter
_LIGATURES = {
    "œ": "oe", "Œ": "OE", "æ": "ae", "Æ": "AE", "ø": "o", "Ø": "O", "đ": "d", "Đ": "D", "ł": "l", "Ł": "L",
    "ı": "i", "ſ": "s", "ŀ": "l", "Ŀ": "L", "ħ": "h", "Ħ": "H", "ŧ": "t", "Ŧ": "T", "ĸ": "k",
}

# letters of other scripts that look like a Latin letter in common fonts (after accents are dropped)
_LOOKALIKES = dict(zip(
    # Cyrillic
    "аеорсухѕіјһԁԛԝӏкɡ"
    "АВЕКМНОРСТУХЅІЈԚԜҺӀҮ"
    # Greek
    "ΑΒΕΖΗΙΚΜΝΟΡΤΥΧϹϿͿ"
    "οαιικυχγϲνρϳ"
    # Armenian and Latin letters from phonetics and small capitals
    "օսհɑɩʏᴀʙᴄᴅᴇɢʜɪᴊᴋʟᴍɴᴏᴘʀꜱᴛᴜᴠᴡᴢ"
    # Cherokee capitals
    "ᎪᏴᏟᎬᏀᎻᎫᏦᏞᎷᏚᎢᏙᏔᏃ"
    # Cyrillic omega, izhitsa and straight u, Latin z with stroke
    "ѡѵүѠѴҮƶƵ",
    "aeopcyxsijhdqwlkg"
    "ABEKMHOPCTYXSIJQWHIY"
    "ABEZHIKMNOPTYXCOJ"
    "oaiikuxycvpj"
    "ouhaiyabcdeghijklmnoprstuvwz"
    "ABCEGHJKLMSTVWZ"
    "wvyWVYzZ",
))


def _fold(s: str) -> str:
    """Lower case the way the regex engine does for a case-insensitive search of Latin text."""
    return s.lower().replace("ı", "i").replace("ſ", "s")


@functools.lru_cache(maxsize=4096)
def _fold_char(c: str) -> str:
    """The plain letters a non-ASCII character stands for in the view. Case is kept."""
    if c in _LIGATURES:
        return _LIGATURES[c]
    if c in _LOOKALIKES:
        return _LOOKALIKES[c]
    digit = unicodedata.digit(c, None)
    if digit is not None:
        return str(digit)
    d = unicodedata.normalize("NFKD", c)
    d = "".join(x for x in d if not unicodedata.combining(x))
    if not d:
        return ""
    return "".join(_LIGATURES.get(x) or _LOOKALIKES.get(x) or x for x in d)


def fold_text(text: str) -> str:
    """The folded view of `text` without positions (used for the forms)."""
    if text.isascii():
        return text
    return _NON_ASCII_RE.sub(lambda m: "".join(_fold_char(c) for c in m.group(0)), text)


def _view(text: str) -> tuple[str, list[int] | None, list[int] | None]:
    """The folded view of `text` and, when positions differ, the start and end in `text` of every view
    character. When every character folds to exactly one character the positions stay the same."""
    if text.isascii():
        return text, None, None
    folds = {c: _fold_char(c) for c in set(text) if not c.isascii()}
    if all(len(f) == 1 for f in folds.values()):
        table = {ord(c): f for c, f in folds.items() if f != c}
        return (text.translate(table) if table else text), None, None
    out: list[str] = []
    starts: list[int] = []
    ends: list[int] = []
    last = 0
    changed = False
    for m in _NON_ASCII_RE.finditer(text):
        a, b = m.span()
        if a > last:
            out.append(text[last:a])
            starts.extend(range(last, a))
            ends.extend(range(last + 1, a + 1))
        for i in range(a, b):
            f = _fold_char(text[i])
            if f != text[i]:
                changed = True
            out.extend(f)
            starts.extend([i] * len(f))
            ends.extend([i + 1] * len(f))
        last = b
    if not changed:
        return text, None, None
    if last < len(text):
        out.append(text[last:])
        starts.extend(range(last, len(text)))
        ends.extend(range(last + 1, len(text) + 1))
    return "".join(out), starts, ends


def _skeleton_of(s: str) -> str:
    return "".join(c for c in _fold(s) if c.isalnum())


@dataclass
class Span:
    start: int
    end: int
    cls: str
    code: str | None = None
    form: str | None = None


class _Needle:
    __slots__ = ("variant", "code", "glued", "regex", "loose", "probe", "skeleton", "digit_left", "digit_right")

    def __init__(self, variant: str, code: str, glued: bool):
        self.variant = variant
        self.code = code
        self.glued = glued
        self.skeleton = _skeleton_of(variant)
        self.digit_left = variant[:1].isdigit()
        self.digit_right = variant[-1:].isdigit()
        if glued:
            self.regex = re.compile(re.escape(variant), re.IGNORECASE)
            self.loose = None
            self.probe = _fold(variant)
        else:
            tokens = variant.split(" ")
            body = r"\s+".join(re.escape(t) for t in tokens)
            self.regex = re.compile(_NOT_AFTER_ALNUM + body + _NOT_BEFORE_ALNUM, re.IGNORECASE)
            # the form without the boundary, for the case-change boundary checked in code
            self.loose = re.compile(body, re.IGNORECASE)
            # a cheap presence check before the regex runs: the first token, or the whole variant
            # when it has no whitespace (whitespace inside a form may be any run in the text)
            self.probe = _fold(tokens[0])


def _left_ok(view: str, a: int, digit_left: bool) -> bool:
    """A boundary before view[a] for the case-change pass."""
    if a == 0:
        return True
    p, c = view[a - 1], view[a]
    if not p.isalnum():
        return True
    if digit_left:
        return not p.isdigit()
    if p.islower() and c.isupper():
        return True
    return p.isupper() and c.isupper() and a + 1 < len(view) and view[a + 1].islower()


def _right_ok(view: str, a: int, b: int, digit_right: bool) -> bool:
    """A boundary after view[b - 1] for the case-change pass."""
    if b >= len(view):
        return True
    c, n = view[b - 1], view[b]
    if not n.isalnum():
        return True
    if digit_right:
        return not n.isdigit()
    if c.islower() and n.isupper():
        return True
    if c.isupper() and n.isupper() and b + 1 < len(view) and view[b + 1].islower():
        return True
    # an acronym written in capitals, followed by digits
    word = view[a:b]
    return n.isdigit() and c.isalpha() and word.isupper() and sum(ch.isalpha() for ch in word) >= 2


class _Skeleton:
    """The letters and digits of a view in one lower-case string with html tags left out. For every run of
    them it keeps where it starts in the view. What separates a run from the one before is read on demand."""

    def __init__(self, view: str):
        self.blank = _MARKUP_RE.sub(lambda m: _MARKUP_FILL * len(m.group(0)), view) if "<" in view else view
        self.spans = [m.span() for m in _ALNUM_RUN_RE.finditer(self.blank)]
        raw = "".join(self.blank[a:b] for a, b in self.spans)
        low = _fold(raw)
        if len(low) != len(raw):
            low = "".join((_fold(c) or c)[0] for c in raw)
        self.text = low
        self.size = len(low)
        self.run_skel = list(itertools.accumulate((b - a for a, b in self.spans), initial=0))[:-1]

    def _run(self, i: int) -> int:
        return bisect.bisect_right(self.run_skel, i) - 1

    def _view_pos(self, i: int) -> int:
        r = self._run(i)
        return self.spans[r][0] + (i - self.run_skel[r])

    def _separated(self, i: int) -> tuple[bool, int, bool, bool]:
        """(skeleton index i starts a run, gap before it with tags not counted, a tag before it, a line break
        before it)."""
        r = self._run(i)
        if self.run_skel[r] != i:
            return False, 0, False, False
        sep = self.blank[self.spans[r - 1][1] if r else 0:self.spans[r][0]]
        fill = sep.count(_MARKUP_FILL)
        # the borders of a Markdown table and its header separator line are no distance: a name over two rows
        table = sum(1 for c in sep if c in _TABLE_CHARS)
        return True, len(sep) - fill - table, fill > 0, "\n" in sep

    def _glued_rest(self, i: int, before: bool) -> str:
        """The letters and digits of the run that skeleton index `i` lies in, before it (or after it)."""
        r = self._run(i)
        start = self.run_skel[r]
        end = start + (self.spans[r][1] - self.spans[r][0])
        return self.text[start:i] if before else self.text[i:end]

    def find(self, key: str):
        """(start, end) in the view of every spread-out occurrence of `key`. The key starts and ends at a run
        boundary; a key of MIN_GLUED_LETTERS or more may also start or end inside a run when the rest of that
        run is digits (abc-de01, 01abc-de), a long key (twice that) also before or after a glued word of three
        letters or more (first|namestr)."""
        n = len(key)
        idx = self.text.find(key)
        while idx >= 0:
            j = idx + n
            ok = idx == 0 or self._separated(idx)[0] or self._inside_ok(idx, key, before=True)
            ok = ok and (j == self.size or self._separated(j)[0] or self._inside_ok(j, key, before=False))
            spread = False
            if ok:
                for t in range(idx + 1, j):
                    starts, gap, tag, newline = self._separated(t)
                    if not starts:
                        continue
                    spread = True
                    if n < MIN_SPREAD and (tag or newline or gap > 2):
                        ok = False
                        break
                    if gap > MAX_GAP:
                        ok = False
                        break
            if ok and spread:
                yield self._view_pos(idx), self._view_pos(j - 1) + 1
            idx = self.text.find(key, idx + 1)

    def _inside_ok(self, i: int, key: str, before: bool) -> bool:
        """A key of letters may start or end inside a run when the rest of the run is digits (abc-de01) or, for
        a long key, a glued word of three letters or more (first|namestr); one or two letters are the start
        of the next word (the genitive variant of a place before the next word). A key with digits in it (a
        file number) never: a digit glued to a number makes another number."""
        n = len(key)
        if n < MIN_GLUED_LETTERS or not key.isalpha():
            return False
        rest = self._glued_rest(i, before)
        if rest.isdigit():
            return True
        # the key's own part of that run must be a piece of the key (name|str), not the one letter of a
        # genitive variant reaching into the next word (GmbH s|tage): that letter belongs to the next word
        part = self._run_part(i, before)
        return n >= 2 * MIN_GLUED_LETTERS and len(rest) >= 3 and rest.isalpha() and part >= 3

    def _run_part(self, i: int, before: bool) -> int:
        """How many characters of the run that skeleton index `i` lies in belong to a key that starts at `i`
        (`before`) or ends at `i`."""
        r = self._run(i)
        start = self.run_skel[r]
        end = start + (self.spans[r][1] - self.spans[r][0])
        return end - i if before else i - start


class Matcher:
    def __init__(self, forms: list[tuple[str, str]]):
        """`forms` are (variant, code) pairs, as register.forms_for_matching gives them."""
        seen: set[tuple[str, str, bool]] = set()
        needles: list[_Needle] = []
        for variant, code in forms:
            v = _WS_RE.sub(" ", fold_text(variant).strip())
            if not v:
                continue
            key = (_fold(v), code, False)
            if key not in seen:
                seen.add(key)
                needles.append(_Needle(v, code, False))
            if len(_LETTER_RE.findall(v)) >= MIN_GLUED_LETTERS:
                g = _WS_RE.sub("", v).lower()
                key = (g, code, True)
                if key not in seen:
                    seen.add(key)
                    needles.append(_Needle(g, code, True))
        needles.sort(key=lambda n: (-len(n.variant), n.glued, n.variant, n.code))
        self._needles = needles
        # forms of three or four letters, lower case, for the glued search inside identifier tokens
        short: set[tuple[str, str]] = set()
        for variant, code in forms:
            v = fold_text(variant).strip()
            if v.isalpha() and SHORT_GLUED_LETTERS[0] <= len(v) <= SHORT_GLUED_LETTERS[1]:
                short.add((_fold(v), code))
        self._short = sorted(short, key=lambda s: (-len(s[0]), s[0], s[1]))

    def __len__(self) -> int:
        """The number of needles of the main search (the glued search of short forms is not counted)."""
        return len(self._needles)

    def _short_glued(self, view: str, low: str, same_length: bool, claim) -> None:
        """Short forms glued inside an identifier token between digits and affixes (see the module text)."""
        for key, code in self._short:
            if low.find(key) < 0:
                continue
            if same_length:
                starts = []
                i = low.find(key)
                while i >= 0:
                    starts.append(i)
                    i = low.find(key, i + 1)
            else:
                starts = [m.start() for m in re.finditer(re.escape(key), view, re.IGNORECASE)]
            for i in starts:
                j = i + len(key)
                a, b = i, j
                while a > 0 and view[a - 1].isalnum() and i - a <= MAX_GLUE_REST:
                    a -= 1
                while b < len(view) and view[b].isalnum() and b - j <= MAX_GLUE_REST:
                    b += 1
                if (a, b) == (i, j) or i - a > MAX_GLUE_REST or b - j > MAX_GLUE_REST:
                    continue   # the whole token is the boundary rule of the main search; a long rest is no id
                if _GLUE_REST_RE.fullmatch(_fold(view[a:i])) and _GLUE_REST_RE.fullmatch(_fold(view[j:b])):
                    claim(i, j, code)

    def find(self, text: str) -> list[Span]:
        """Every registered form in `text`, longest variant first, without overlaps, sorted by position."""
        if not text or not self._needles:
            return []
        view, starts, ends = _view(text)
        low = _fold(view)
        taken = bytearray(len(view))
        found: list[tuple[int, int, str]] = []
        skeleton: _Skeleton | None = None
        spread_done: set[tuple[str, str]] = set()

        def claim(a: int, b: int, code: str) -> None:
            if a < b and not any(taken[a:b]):
                taken[a:b] = b"\x01" * (b - a)
                found.append((a, b, code))

        same_length = len(low) == len(view)

        def positions_of(probe: str):
            """Every position of `probe` in the folded view (the regex runs only there)."""
            i = low.find(probe)
            while i >= 0:
                yield i
                i = low.find(probe, i + 1)

        for n in self._needles:
            present = low.find(n.probe) >= 0
            if n.glued:
                if present:
                    for m in (n.regex.finditer(view) if not same_length else
                              filter(None, (n.regex.match(view, i) for i in positions_of(n.probe)))):
                        claim(m.start(), m.end(), n.code)
                continue
            if present:
                positions = positions_of(n.probe) if same_length else (m.start() for m in n.loose.finditer(view))
                for i in positions:
                    m = n.regex.match(view, i)
                    if m:
                        claim(m.start(), m.end(), n.code)
                        continue
                    m = n.loose.match(view, i)
                    if m and _left_ok(view, i, n.digit_left) and _right_ok(view, i, m.end(), n.digit_right):
                        claim(i, m.end(), n.code)
            if len(n.skeleton) >= 3 and (n.skeleton, n.code) not in spread_done:
                spread_done.add((n.skeleton, n.code))
                if skeleton is None:
                    skeleton = _Skeleton(view)
                if n.skeleton in skeleton.text:
                    for a, b in skeleton.find(n.skeleton):
                        claim(a, b, n.code)

        if self._short:
            self._short_glued(view, low, same_length, claim)

        spans: list[Span] = []
        for a, b, code in found:
            if starts is not None:
                a, b = starts[a], ends[b - 1]
                # marks that the view dropped after the last letter belong to it
                while b < len(text) and unicodedata.category(text[b]) in ("Mn", "Me"):
                    b += 1
            spans.append(Span(a, b, "name", code, text[a:b]))
        spans.sort(key=lambda s: (s.start, s.end))
        return spans

    def replace(self, text: str, spans: list[Span], token) -> str:
        """Replace every span by `token(span)`, working from the right so positions stay valid.

        Overlapping or out of range spans raise ValueError (with positions only): skipping one would leave
        part of a form in the text."""
        pieces: list[str] = []
        limit = len(text)
        for s in sorted(spans, key=lambda s: (s.start, s.end), reverse=True):
            if s.start < 0 or s.start > s.end or s.end > len(text):
                raise ValueError("span %d to %d is outside the text" % (s.start, s.end))
            if s.end > limit:
                raise ValueError("span %d to %d overlaps the span that starts at %d" % (s.start, s.end, limit))
            pieces.append(text[s.end:limit])
            pieces.append(str(token(s)))
            limit = s.start
        pieces.append(text[:limit])
        pieces.reverse()
        return "".join(pieces)


def public(spans: list[Span]) -> list[dict]:
    """The public view of spans: position, length and class. Never the code, never the value."""
    return [{"start": s.start, "length": s.end - s.start, "cls": s.cls} for s in spans]
