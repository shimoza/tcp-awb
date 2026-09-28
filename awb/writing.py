"""The writing checker, the fact keeper of the voice pass and the voice learner.

    awb write check FILE [--mode mail|doc|chat] [--scope tcp|hcs]
    awb write keep BEFORE AFTER
    awb voice learn DRAFT SENT

`check_text` reads a text the way he writes it (Markdown or plain text) and returns tells and metrics. A tell is
(line, class, blocking, hint). The hint names the rule and quotes at most one word. That word always comes from
the rule itself (a banned word, "and", "we"), never from the checked text.

Blocking classes:

    em-dash          U+2014 and its look-alikes (U+2015, U+2E3A, U+2E3B, U+FE58, U+FE31) anywhere; U+2013 and
                     the other dash and minus forms (U+2010 to U+2012, U+2212, U+FE63, U+FF0D) used as a dash
                     between spaces (a range such as 10-20 written with U+2013 and no spaces is fine)
    comma-and-or     a comma directly before "and" or "or" (also the Arabic, ideographic, small and fullwidth
                     commas)
    banned-word      a word of rules/banned-words.txt (whole word, any case)
    banned-phrase    a phrase of rules/banned-phrases.txt (whole words, any case, any whitespace between words);
                     a banned word inside a banned phrase is reported once, as the phrase
    we-in-mail       we, our, us in mode mail ("US" in capitals is the country and a region name such as us-east
                     is a literal, both are left alone)
    commitment       I will, I'll, we will, we'll, I am raising, I'm raising, will come back, I promise, I commit
    earlier-mail     as mentioned, as discussed, in my last mail, as per my previous, as I wrote
    vendor-name      scope tcp only: a name of rules/vendor-names.txt
    certification    BSI, ISO 27001, SOC 2, TISAX, KRITIS, C5, IT-Grundschutz
    bold-lead-list   a run of three or more bullets where more than one starts with bold

Reported, not blocking: the tell `list-of-three` for every list of exactly three items (a bullet list with three
top-level items or three items inline such as "fast, cheap and simple") and the tell `sentence-length` when the
median or the 90th percentile of words per sentence is above its target in rules/voice.txt (median 11, p90 21).
Also reported, each with its line (T-52, T-53):

    fragment         a sentence of running prose (not a bullet item) of at most 12 words that ends with . ! or ?
                     and holds no finite verb ("No downtime." "The catch?"); a one-word answer such as "Yes." is
                     not counted
    comma-splice     two clauses with a verb each joined by a comma alone ("The quota is fine, it is the flavor")
    verdict-heading  a heading that states a conclusion ("CCE is the right choice", "Why CCE wins",
                     "Recommendation: CCE") instead of naming a topic
    connector        a connector of `other_connectors` in rules/voice.txt (his set is and, so, then, but, also,
                     because; the connectors that are banned words already block as banned words)
    i-rate           mode mail only, from 40 words on: I, my and me below half of his rate (29.5 per 1000 words)

A finite verb is found by rule, without a tagger (see the comment above `_finite`), so the three structural counts
are a signal for a look and never a gate.
The metrics carry the sentence count, the words, the median and p90 of words per sentence (p90 by nearest rank),
the words longer than nine letters, the lists of three, the fragments and their share of the sentences, the comma
splices and the headings written as verdicts (of all headings). Per 1000 words they carry the commas before and/or,
the dashes, the words I, my and me, his connectors and the other connectors.

What is not prose. Fenced code blocks (``` or ~~~, closed by a matching fence) and inline code are skipped: a banned
word inside code is a literal, not his voice. So are URLs, mail addresses and link targets. A fence that is never
closed is not code, so the rest of the file is still checked. Indented code blocks are checked as prose. A word glued
into an identifier (next to / \\ _ @ or with a dot or colon between it and a letter or digit: a path, a package or
a host name) is a literal too and gives no tell. Headings and table rows are checked like any other line; they do
not count as sentences in the metrics.

What the reader sees. The classes are checked twice: in the prose as written and in its rendered view
(`rendered`): inline HTML tags, entities, emphasis marks (* and ~), invisible format characters, fullwidth forms,
accents and look-alike letters of other scripts are taken out or folded, so that a banned word or a vendor name
split by markup or spelled with look-alike letters is still found. A hit found in both counts once.

Line numbers count from 1. Line 0 is the file name: `check_file` also checks the name of the file (its suffix
dropped, - _ and . read as spaces) for the same classes.

Modes: doc (default), mail (adds we-in-mail), chat (like doc, but a bold-lead list is reported and not blocking,
because bold helps scanning in a chat answer). Scopes: tcp (vendor names block) and hcs (vendor names are fine).

Facts before voice (V-15, T-56). For a text in his name the facts are checked first and the voice pass comes
second. `awb write keep BEFORE AFTER` compares the text before the voice pass with the text after it: the numbers
(the number words two to ninety count as digits), the identifiers (tokens with digits and letters or with . / : +
_ inside, regions such as eu-de, words in capitals, CamelCase words, inline code, URLs, mail addresses), the
negations (not, no, never, n't, without, unavailable and the like, by their count) and the fenced code blocks. Any
difference blocks (exit 1). The report names kinds, counts and line numbers, never a value. A voice pass that
rewrites "not supported" as "unsupported" or "2" as "two" changes nothing here; one that turns "not" into nothing
or "eu-de" into "eu-nl" does. Plain capitalised words (a product name such as a tool name) are not compared: at the
start of a sentence they cannot be told from any other word.

Voice learning. `awb voice learn DRAFT SENT` reads both files as UTF-8 text and name-checks both first (the name
check of `check.check_text`, through the vault daemon when the register is not readable here, plus the pattern
detectors of the commit gate). A hit refuses the pair (exit 1) and nothing is written. A check that cannot run
refuses too (exit 2). Then the pair goes to `<shared>/voice/pairs/<date>-<n>/draft.md` and `sent.md`. The report
`<shared>/voice/suggestions-<date>.md` is written again for every pair of that date: the words and phrases he
removed (in the draft, not in the sent text), the words he added and the change in sentence length and in the
rate of commas before and/or. It also lists candidates for the banned lists: a removed word or phrase that occurs
in two or more pairs and is not banned yet. Nothing is ever added to the rules: he edits them himself.

Messages carry classes, counts and line numbers. They never carry a file name, a matched name or a register line.
"""
from __future__ import annotations

import difflib
import html
import math
import os
import re
import statistics
import sys
import tempfile
import unicodedata
from bisect import bisect_right
from collections import Counter
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

from awb import config

EXIT_OK = 0
EXIT_FINDINGS = 1
EXIT_ERROR = 2

MODES = ("mail", "doc", "chat")
SCOPES = ("tcp", "hcs")

RULES_DIR = Path(__file__).resolve().parent.parent / "rules"
WORDS_FILE = "banned-words.txt"
PHRASES_FILE = "banned-phrases.txt"
ALLOWED_FILE = "allowed-terms.txt"
"""Technical and product terms that hold a banned word; inside them the banned word is not reported."""
VENDORS_FILE = "vendor-names.txt"
VOICE_FILE = "voice.txt"

BLOCKING = ("em-dash", "comma-and-or", "banned-word", "banned-phrase", "we-in-mail", "commitment",
            "earlier-mail", "vendor-name", "certification", "bold-lead-list")
REPORTED = ("list-of-three", "sentence-length", "fragment", "comma-splice", "verdict-heading", "connector",
            "i-rate")
CLASSES = BLOCKING + REPORTED

FILE_NAME_LINE = 0
"""The line number of a tell found in the file name."""

DEFAULT_TARGETS = {
    "median_sentence_words": 11.0,
    "p90_sentence_words": 21.0,
    "comma_before_and_or_per_1000": 0.0,
    "em_dash_per_1000": 0.0,
    "i_my_me_per_1000": 29.5,
    "other_connectors_per_1000": 0.0,
}
LONG_WORD_LETTERS = 9
"""A word with more letters than this counts as long."""
FRAGMENT_MAX_WORDS = 12
"""A sentence without a finite verb counts as a fragment up to this many words (longer ones are more likely a verb
the list does not know)."""
I_RATE_MIN_WORDS = 40
"""The I rate of a mail is judged from this many words on."""
I_RATE_SHARE = 0.5
"""A mail whose I rate is below this share of his rate gets the reported tell `i-rate`."""
HIS_CONNECTORS = ("and", "so", "then", "but", "also", "because")

MAX_FILE_BYTES = 20_000_000
MAX_VOICE_CHARS = 1_000_000
"""Characters of one voice file (the vault daemon takes check requests up to 4 MB)."""
MAX_DIFF_WORDS = 20_000
"""Above this many words per side the phrase diff of a voice pair is skipped (removed words are still listed)."""
LIST_LIMIT = 60
"""Words or phrases shown per list of the suggestions report."""
MIN_PAIRS = 2
"""A removed word or phrase is a candidate for the banned lists when it was removed in this many pairs."""

VOICE_DIR = "voice"
PAIRS_DIR = "pairs"
DRAFT_NAME = "draft.md"
SENT_NAME = "sent.md"


class WritingError(Exception):
    """A usage, rule file or read problem. Also a check that cannot run. The message carries no value. Exit 2."""


class Refused(WritingError):
    """A voice file carries a hit of the name check or of a gate detector. Exit 1."""


@dataclass
class Tell:
    line: int
    cls: str
    blocking: bool
    hint: str   # names the rule, never quotes more than one word


# --------------------------------------------------------------------------- rules


@dataclass(frozen=True)
class Rules:
    banned_words: tuple[str, ...]
    banned_phrases: tuple[str, ...]
    vendor_names: tuple[str, ...]
    allowed_terms: tuple[str, ...] = ()
    targets: dict = field(default_factory=dict, hash=False, compare=False)
    connectors: tuple[str, ...] = HIS_CONNECTORS
    other_connectors: tuple[str, ...] = ()


def _read_list(path: Path) -> tuple[str, ...]:
    """One entry per line, whitespace runs collapsed, # starts a comment line, repeats dropped."""
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        raise WritingError("the rule file rules/%s cannot be read" % path.name) from None
    out: list[str] = []
    seen: set[str] = set()
    for line in text.splitlines():
        item = " ".join(line.split())
        if not item or item.startswith("#") or item.lower() in seen:
            continue
        seen.add(item.lower())
        out.append(item)
    return tuple(out)


def _read_targets(path: Path) -> dict:
    """The numeric targets of rules/voice.txt ("key = value" lines). Missing keys keep their defaults."""
    targets = dict(DEFAULT_TARGETS)
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return targets
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = (s.strip() for s in line.split("=", 1))
        try:
            targets[key] = float(value)
        except ValueError:
            continue
    return targets


def _read_voice_lists(path: Path) -> dict[str, tuple[str, ...]]:
    """The list keys of rules/voice.txt ("key = a, b, c" lines): `connectors` (his set) and `other_connectors`
    (connectors outside his set that are not banned words already). Lower case, repeats dropped."""
    out: dict[str, tuple[str, ...]] = {}
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return out
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = (x.strip() for x in line.split("=", 1))
        if key not in ("connectors", "other_connectors"):
            continue
        items: list[str] = []
        for item in value.split(","):
            item = " ".join(item.split()).lower()
            if item and item not in items:
                items.append(item)
        out[key] = tuple(items)
    return out


_RULES_CACHE: dict = {}


def _stamp(path: Path):
    try:
        st = path.stat()
        return st.st_mtime_ns, st.st_size
    except OSError:
        return None


def load_rules(rules_dir: Path | None = None) -> Rules:
    """The rules of `rules_dir` (default: rules/ of the repository), read again when a file changed."""
    folder = Path(rules_dir) if rules_dir is not None else RULES_DIR
    files = [folder / n for n in (WORDS_FILE, PHRASES_FILE, VENDORS_FILE, VOICE_FILE, ALLOWED_FILE)]
    key = (str(folder), tuple(_stamp(f) for f in files))
    cached = _RULES_CACHE.get(str(folder))
    if cached is not None and cached[0] == key:
        return cached[1]
    lists = _read_voice_lists(files[3])
    rules = Rules(
        banned_words=tuple(w.lower() for w in _read_list(files[0])),
        banned_phrases=tuple(p.lower() for p in _read_list(files[1])),
        vendor_names=_read_list(files[2]),
        allowed_terms=_read_list(files[4]) if files[4].exists() else (),
        targets=_read_targets(files[3]),
        connectors=lists.get("connectors", HIS_CONNECTORS),
        other_connectors=lists.get("other_connectors", ()),
    )
    _RULES_CACHE[str(folder)] = (key, rules)
    _COMPILED.pop(str(folder), None)
    return rules


# --------------------------------------------------------------------------- patterns

_APOSTROPHES = "'\u2019"
_HYPHENS = "-\u2010\u2011"

COMMITMENTS = (
    ("will", "I will"), ("I'll", "I'll"), ("will", "we will"), ("we'll", "we'll"),
    ("raising", "I am raising"), ("raising", "I'm raising"), ("will", "will come back"),
    ("promise", "I promise"), ("commit", "I commit"),
)
EARLIER_MAIL = (
    ("mentioned", "as mentioned"), ("discussed", "as discussed"), ("last", "in my last mail"),
    ("last", "in my last e-mail"), ("last", "in my last email"), ("previous", "as per my previous"),
    ("wrote", "as I wrote"),
)
WE_WORDS = ("we", "our", "us")
# (label, regular expression). Acronyms that are ordinary letters in lower case (BSI, C5) match in capitals only.
CERTIFICATIONS = (
    ("BSI", r"BSI"),
    ("ISO", r"(?i:ISO(?:/IEC)?[ \u00a0-]?27001)"),
    ("SOC", r"(?i:SOC[ \u00a0-]?2)"),
    ("TISAX", r"(?i:TISAX)"),
    ("KRITIS", r"(?i:KRITIS)"),
    # C5 is also an instance family of a public cloud: "C5 instances" is a flavor, not a certification claim
    ("C5", r"C5(?![ \u00a0]*(?i:instances?|instance[ \u00a0]+(?:family|type)|family|flavou?rs?|VMs?|nodes?)(?!\w))"),
    ("IT-Grundschutz", r"(?i:(?:IT[-\u2010\u2011 \u00a0]?)?Grundschutz)"),
)

# a dash used as a pause: the em dash and its look-alikes anywhere (horizontal bar, two- and three-em dash, the
# small and vertical forms), the en dash, figure dash, minus sign, hyphen and the small and fullwidth hyphen-minus
# when they stand between spaces (a range such as 10-20 is fine)
_EM_DASH_RE = re.compile(r"[\u2014\u2015\u2e3a\u2e3b\ufe58\ufe31]"
                         r"|(?<!\S)[\u2010-\u2013\u2212\ufe63\uff0d\ufe32](?!\S)")
# a comma directly before "and" or "or", also the comma look-alikes (Arabic, ideographic, small, fullwidth and the
# low single quotation mark)
_COMMA_AND_OR_RE = re.compile(r"[,\u060c\u3001\ufe50\ufe51\uff0c\u201a]\s*(?:(?P<g0>and)|(?P<g1>or))(?!\w)",
                              re.IGNORECASE)
_COMMA_LABELS = ["and", "or"]


def _phrase_source(phrase: str) -> str:
    """A rule phrase as a regular expression: any whitespace run between words, both apostrophes, all hyphens."""
    out: list[str] = []
    for i, word in enumerate(phrase.split()):
        if i:
            out.append(r"\s+")
        for ch in word:
            if ch in _APOSTROPHES:
                out.append("[%s]" % _APOSTROPHES)
            elif ch in _HYPHENS:
                out.append("[%s]" % _HYPHENS)
            else:
                out.append(re.escape(ch))
    return "".join(out)


def _named(items, flags=0):
    """One pattern over (label, source) items, longest source first, whole words. (pattern, labels) or None."""
    items = sorted(((lab, src) for lab, src in items if src), key=lambda it: -len(it[1]))
    if not items:
        return None
    body = "|".join("(?P<g%d>%s)" % (i, src) for i, (_, src) in enumerate(items))
    return re.compile(r"(?<!\w)(?:%s)(?!\w)" % body, flags), [lab for lab, _ in items]


def _label_word(phrase: str) -> str:
    """The word a hint quotes for a phrase: its longest word (the first of equals)."""
    words = phrase.split()
    return max(words, key=lambda w: (sum(c.isalnum() for c in w), -words.index(w)))


@dataclass
class _Compiled:
    words: tuple | None
    phrases: tuple | None
    vendors: tuple | None
    commitments: tuple | None
    earlier: tuple | None
    we: tuple | None
    certifications: tuple | None
    allowed: tuple | None = None
    others: tuple | None = None
    his: tuple | None = None


_COMPILED: dict = {}


def _compiled(rules: Rules, rules_dir: Path | None = None) -> _Compiled:
    key = str(Path(rules_dir) if rules_dir is not None else RULES_DIR)
    cached = _COMPILED.get(key)
    if cached is not None and cached[0] is rules:
        return cached[1]
    ci = re.IGNORECASE
    comp = _Compiled(
        words=_named([(w, _phrase_source(w)) for w in rules.banned_words], ci),
        phrases=_named([(_label_word(p), _phrase_source(p)) for p in rules.banned_phrases], ci),
        vendors=_named([("", _phrase_source(v)) for v in rules.vendor_names], ci),
        commitments=_named([(lab, _phrase_source(p)) for lab, p in COMMITMENTS], ci),
        earlier=_named([(lab, _phrase_source(p)) for lab, p in EARLIER_MAIL], ci),
        we=_named([(w, re.escape(w)) for w in WE_WORDS], ci),
        certifications=_named(CERTIFICATIONS),
        allowed=_named([("", _phrase_source(t)) for t in rules.allowed_terms], ci),
        others=_named([(_label_word(c), _phrase_source(c)) for c in rules.other_connectors
                       if c not in rules.banned_words], ci),
        his=_named([(c, _phrase_source(c)) for c in rules.connectors], ci),
    )
    _COMPILED[key] = (rules, comp)
    return comp


# --------------------------------------------------------------------------- what is prose

_FENCE_RE = re.compile(r"^ {0,3}(`{3,}|~{3,})(.*)$")
_URL_RE = re.compile(r"(?i:\b(?:https?|ftp)://|\bwww\.)[^\s<>()\[\]\"'`]+")
_MAIL_RE = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")
_LINK_TARGET_RE = re.compile(r"(?<=\])\([^()\s]*(?:\s+\"[^\"]*\")?\)")
_TRAILING = ".,;:!?"


def _placeholder(n: int) -> str:
    """What a literal becomes: one neutral word, padded to the same length."""
    return "x" + " " * (n - 1) if n > 0 else ""


def _code_spans(line: str) -> list[tuple[int, int]]:
    """(start, end) of every inline code span of a line, backticks included."""
    spans: list[tuple[int, int]] = []
    i, n = 0, len(line)
    while i < n:
        if line[i] != "`":
            i += 1
            continue
        j = i
        while j < n and line[j] == "`":
            j += 1
        width = j - i
        k, end = j, -1
        while k < n:
            if line[k] != "`":
                k += 1
                continue
            e = k
            while e < n and line[e] == "`":
                e += 1
            if e - k == width:
                end = e
                break
            k = e
        if end < 0:
            i = j
        else:
            spans.append((i, end))
            i = end
    return spans


def _mask_code_spans(line: str) -> str:
    out: list[str] = []
    last = 0
    for a, b in _code_spans(line):
        out.append(line[last:a])
        out.append(_placeholder(b - a))
        last = b
    out.append(line[last:])
    return "".join(out)


def _mask_urls(line: str) -> str:
    def url(m: re.Match) -> str:
        s = m.group(0)
        keep = len(s) - len(s.rstrip(_TRAILING))
        return _placeholder(len(s) - keep) + s[len(s) - keep:]

    line = _LINK_TARGET_RE.sub(lambda m: " " * len(m.group(0)), line)
    line = _URL_RE.sub(url, line)
    return _MAIL_RE.sub(lambda m: _placeholder(len(m.group(0))), line)


def _mask_line(line: str) -> str:
    return _mask_urls(_mask_code_spans(line))


def prose(text: str) -> str:
    """`text` with line breaks normalised and every literal (code blocks, inline code, URLs, mail addresses, link
    targets) taken out. The lines stay where they are, so line numbers are those of the text."""
    lines = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    out = list(lines)
    i = 0
    while i < len(lines):
        m = _FENCE_RE.match(lines[i])
        if m and not (m.group(1)[0] == "`" and "`" in m.group(2)):
            fence = m.group(1)
            close = re.compile(r"^ {0,3}%s{%d,}[ \t]*$" % (re.escape(fence[0]), len(fence)))
            end = next((k for k in range(i + 1, len(lines)) if close.match(lines[k])), None)
            if end is not None:
                for k in range(i, end + 1):
                    out[k] = ""
                i = end + 1
                continue
        out[i] = _mask_line(lines[i])
        i += 1
    return "\n".join(out)


# --------------------------------------------------------------------------- the checks


@dataclass
class _Hit:
    start: int
    end: int
    cls: str
    label: str = ""


def _glued(text: str, start: int, end: int) -> bool:
    """True when the match is part of an identifier: a path, a package, a host or file name."""
    before = text[start - 1] if start > 0 else ""
    after = text[end] if end < len(text) else ""
    if (before and before in "/\\_@") or (after and after in "/\\_@"):
        return True
    if before in (".", ":") and start >= 2 and text[start - 2].isalnum():
        return True
    if after in (".", ":") and end + 1 < len(text) and text[end + 1].isalnum():
        return True
    return False


def _find(text: str, compiled, cls: str, skip=None) -> list[_Hit]:
    if compiled is None:
        return []
    pattern, labels = compiled
    out = []
    for m in pattern.finditer(text):
        if _glued(text, m.start(), m.end()):
            continue
        if skip is not None and skip(text, m):
            continue
        out.append(_Hit(m.start(), m.end(), cls, labels[int(m.lastgroup[1:])]))
    return out


def _skip_we(text: str, m: re.Match) -> bool:
    word = m.group(0)
    if word == "US":
        return True
    end = m.end()
    return end + 1 < len(text) and text[end] == "-" and text[end + 1].isalnum()


def _merge(hits: list[_Hit]) -> list[_Hit]:
    """Overlapping hits of one class count once (the first)."""
    out: list[_Hit] = []
    last_end: dict[str, int] = {}
    for h in sorted(hits, key=lambda h: (h.start, -h.end)):
        if h.start < last_end.get(h.cls, -1):
            continue
        out.append(h)
        last_end[h.cls] = max(h.end, last_end.get(h.cls, -1))
    return out


_TAG_RE = re.compile(r"""</?[A-Za-z][A-Za-z0-9]*(?:\s(?:(?>"[^"<\n]*")|(?>'[^'<\n]*')|[^<>\n])*+)?/?>""")
"""An inline tag, its quoted attribute values read whole (a > inside one does not end the tag)."""
_MARKS = str.maketrans("", "", "*~")


def rendered(text: str) -> str:
    """The prose as a reader sees it once it is rendered, line by line (the line count stays): inline HTML tags
    dropped, entities decoded, emphasis and strike marks (* ~) dropped, invisible format characters dropped,
    fullwidth forms, ligatures, accents and look-alike letters of other scripts folded to plain letters. So
    le*ver*age, lev<b>er</b>age, lev&#101;rage and a Cyrillic look-alike all read as the banned word."""
    from awb.matcher import fold_text

    out: list[str] = []
    for line in text.split("\n"):
        v = _TAG_RE.sub("", line)
        if "&" in v:
            v = html.unescape(v)
        v = v.translate(_MARKS)
        v = "".join(c for c in v if unicodedata.category(c) != "Cf")
        v = fold_text(v)
        out.append(v.replace("\r", " ").replace("\n", " "))
    return "\n".join(out)


def _line_starts(text: str) -> list[int]:
    return [0] + [i + 1 for i, c in enumerate(text) if c == "\n"]


def _lined_hits(masked: str, mode: str, scope: str, comp: _Compiled) -> list[tuple[int, _Hit]]:
    """(line, hit) of the word and punctuation classes, found in the prose as written and in its rendered view.
    Per line, class and label the larger count wins, so a hit found in both counts once."""
    groups: list[dict] = []
    for text in (masked, rendered(masked)):
        starts = _line_starts(text)
        found: dict = {}
        for h in _prose_hits(text, mode, scope, comp):
            line = bisect_right(starts, h.start)
            found.setdefault((line, h.cls, h.label), []).append((line, h))
        groups.append(found)
    out: list[tuple[int, _Hit]] = []
    for key in set(groups[0]) | set(groups[1]):
        a, b = groups[0].get(key, []), groups[1].get(key, [])
        out.extend(a if len(a) >= len(b) else b)
    out.sort(key=lambda t: (t[0], t[1].start))
    return out


def _prose_hits(text: str, mode: str, scope: str, comp: _Compiled) -> list[_Hit]:
    """Hits of the word and punctuation classes in a text that is already prose."""
    hits: list[_Hit] = [_Hit(m.start(), m.end(), "em-dash") for m in _EM_DASH_RE.finditer(text)]
    hits += [_Hit(m.start(), m.end(), "comma-and-or", _COMMA_LABELS[int(m.lastgroup[1:])])
             for m in _COMMA_AND_OR_RE.finditer(text)]
    allowed = _find(text, comp.allowed, "allowed") if comp.allowed else []

    def _in_allowed(h) -> bool:
        return any(a.start <= h.start and h.end <= a.end for a in allowed)

    phrases = [h for h in _find(text, comp.phrases, "banned-phrase") if not _in_allowed(h)]
    hits += phrases
    hits += [h for h in _find(text, comp.words, "banned-word")
             if not any(p.start <= h.start and h.end <= p.end for p in phrases) and not _in_allowed(h)]
    if mode == "mail":
        hits += _find(text, comp.we, "we-in-mail", _skip_we)
    hits += _find(text, comp.commitments, "commitment")
    hits += _find(text, comp.earlier, "earlier-mail")
    if scope == "tcp":
        hits += _find(text, comp.vendors, "vendor-name")
    hits += _find(text, comp.certifications, "certification")
    return _merge(hits)


def _hint(cls: str, label: str = "") -> str:
    if cls == "em-dash":
        return "a dash used as a pause: write a comma, a full stop or parentheses"
    if cls == "comma-and-or":
        return 'a comma directly before "%s": drop the comma' % label
    if cls == "banned-word":
        return 'banned word "%s" (rules/%s)' % (label, WORDS_FILE)
    if cls == "banned-phrase":
        return 'banned phrase with "%s" (rules/%s)' % (label, PHRASES_FILE)
    if cls == "we-in-mail":
        return 'a mail speaks as I: no "%s" in mode mail' % label
    if cls == "commitment":
        return 'a commitment with "%s": say what happens, not what you promise' % label
    if cls == "earlier-mail":
        return 'a pointer to an earlier mail with "%s": say the point again' % label
    if cls == "vendor-name":
        return "a vendor name in TCP prose (rules/%s): keep literal identifiers in code" % VENDORS_FILE
    if cls == "certification":
        return 'a certification claim with "%s": state it only with a source' % label
    return label


# --------------------------------------------------------------------------- structure: lines, bullets, sentences

_HEADING_RE = re.compile(r"^ {0,3}#{1,6}(?:[ \t]|$)")
_RULE_RE = re.compile(r"^ {0,3}([-*_])(?:[ \t]*\1){2,}[ \t]*$")
_SETEXT_RE = re.compile(r"^ {0,3}(?:=+|-+)[ \t]*$")
_TABLE_RE = re.compile(r"^[ \t]*\|")
_TABLE_SEP_RE = re.compile(r"^[ \t]*\|?[ \t]*:?-+:?[ \t]*(?:\|[ \t]*:?-+:?[ \t]*)+\|?[ \t]*$")
_BULLET_RE = re.compile(r"^(?P<indent>[ \t]*)(?:[-*+]|\d{1,9}[.)])[ \t]+(?P<body>\S.*)$")
_BOLD_START_RE = re.compile(r"(?:\*\*|__)\S")

_WORD_RE = re.compile(r"[^\W_]+(?:['\u2019\-\u2010\u2011][^\W_]+)*")
_LETTERS_RE = re.compile(r"[^\W\d_]+")
_END_RE = re.compile(r"[.!?]+[\"')\]\u2019\u201d]*(?=\s|$)")
_ABBREV_BEFORE_RE = re.compile(r"(?<![^\W\d_])([^\W\d_]+(?:\.[^\W\d_]+)*)$")
_ABBREVIATIONS = frozenset("""
e.g i.e etc vs cf approx incl excl esp resp fig no nr ca z.b bzw d.h u.a usw ggf evtl inkl mr mrs ms dr prof
st jr sr inc ltd co corp dept min max sec p pp vol
""".split())
_I_MY_ME = frozenset(("i", "my", "me", "mine", "myself", "i'm", "i'll", "i've", "i'd"))


@dataclass
class _Line:
    start: int
    text: str
    kind: str   # blank, heading, rule, table, bullet, text


@dataclass
class _Sentence:
    start: int
    text: str
    words: int
    bullet: bool = False


FRONT_MATTER_LINES = 40
"""A block between two --- lines at the very top, at most this long, is front matter (YAML of a skill or a page)."""


def _front_matter(lines: list[_Line]) -> int:
    """The index of the closing --- of front matter at the top of a text, or -1."""
    if not lines or lines[0].text.rstrip() != "---":
        return -1
    for i in range(1, min(len(lines), FRONT_MATTER_LINES + 2)):
        if lines[i].text.rstrip() in ("---", "..."):
            return i
    return -1


def _lines(text: str) -> list[_Line]:
    out: list[_Line] = []
    pos = 0
    for raw in text.split("\n"):
        out.append(_Line(pos, raw, "text"))
        pos += len(raw) + 1
    close = _front_matter(out)
    for i, ln in enumerate(out):
        s = ln.text
        if i == 0 and close > 0 or i == close:
            ln.kind = "rule"      # the fences of front matter; its key lines stay text
        elif not s.strip():
            ln.kind = "blank"
        elif _HEADING_RE.match(s):
            ln.kind = "heading"
        elif _SETEXT_RE.match(s) and i and out[i - 1].kind == "text":
            ln.kind = "rule"
            out[i - 1].kind = "heading"
        elif _RULE_RE.match(s) or _TABLE_SEP_RE.match(s):
            ln.kind = "rule"
        elif _TABLE_RE.match(s):
            ln.kind = "table"
        elif _BULLET_RE.match(s):
            ln.kind = "bullet"
    return out


def _indent(s: str) -> int:
    return len(s.expandtabs(4)) - len(s.expandtabs(4).lstrip())


def _bullet_runs(lines: list[_Line]) -> list[list[int]]:
    """Runs of bullet lines (indexes). A blank line between bullets keeps the run, an indented line or a line
    right after an item continues it, any other line ends it."""
    runs: list[list[int]] = []
    cur: list[int] = []
    gap = False
    for i, ln in enumerate(lines):
        if ln.kind == "bullet":
            cur.append(i)
            gap = False
        elif ln.kind == "blank":
            gap = bool(cur)
        elif ln.kind == "text" and cur and (not gap or ln.text[:1] in (" ", "\t")):
            continue
        elif cur:
            runs.append(cur)
            cur, gap = [], False
    if cur:
        runs.append(cur)
    return runs


def _units(lines: list[_Line]) -> list[tuple[int, int, bool]]:
    """Paragraphs and bullet items as (start, end, is a bullet item). Headings, tables and rules are not units."""
    units: list[tuple[int, int, bool]] = []
    cur: list | None = None
    prev = None
    for ln in lines:
        if ln.kind == "bullet":
            if cur:
                units.append((cur[0], cur[1], cur[2]))
            m = _BULLET_RE.match(ln.text)
            cur = [ln.start + m.start("body"), ln.start + len(ln.text), True]
        elif ln.kind == "text":
            if cur is not None and prev in ("text", "bullet"):
                cur[1] = ln.start + len(ln.text)
            else:
                if cur:
                    units.append((cur[0], cur[1], cur[2]))
                cur = [ln.start, ln.start + len(ln.text), False]
        elif cur:
            units.append((cur[0], cur[1], cur[2]))
            cur = None
        prev = ln.kind
    if cur:
        units.append((cur[0], cur[1], cur[2]))
    return units


def _is_abbreviation(seg: str, pos: int) -> bool:
    m = _ABBREV_BEFORE_RE.search(seg, 0, pos)
    return bool(m) and m.group(1).lower() in _ABBREVIATIONS


def _sentences(text: str, units: list[tuple[int, int, bool]]) -> list[_Sentence]:
    out: list[_Sentence] = []

    def add(offset: int, piece: str, bullet: bool) -> None:
        n = len(_WORD_RE.findall(piece))
        if n:
            lead = len(piece) - len(piece.lstrip())
            out.append(_Sentence(offset + lead, piece.strip(), n, bullet))

    for a, b, bullet in units:
        seg = text[a:b]
        start = 0
        for m in _END_RE.finditer(seg):
            if m.group(0).rstrip("\"')]\u2019\u201d") == "." and _is_abbreviation(seg, m.start()):
                continue
            add(a + start, seg[start:m.end()], bullet)
            start = m.end()
        add(a + start, seg[start:], bullet)
    return out


_NOT_AN_ITEM_START = frozenset("""
i we you he she it they this that these those there if when while because since although though so then but
which who where
""".split())


def _first_word(s: str) -> str:
    m = _WORD_RE.search(s)
    return m.group(0).lower() if m else ""


def _triads(sentence: str) -> int:
    """Lists of exactly three items written inline such as "A, B and C" (with an Oxford comma too)."""
    count = 0
    for m in re.finditer(r"(?<!\w)(?:and|or)(?!\w)", sentence, re.IGNORECASE):
        before = sentence[:m.start()]
        cut = max(before.rfind(";"), before.rfind(":"), before.rfind("("))
        parts = before[cut + 1:].split(",")
        if len(parts) < 2:
            continue
        if not parts[-1].strip():
            parts = parts[:-1]
        if len(parts) < 2:
            continue
        items = 0
        ok = True
        for k, part in enumerate(reversed(parts)):
            n = len(_WORD_RE.findall(part))
            if n == 0:
                ok = False
                break
            first = k == len(parts) - 1 or n > 3
            if not first and _first_word(part) in _NOT_AN_ITEM_START:
                ok = False
                break
            items += 1
            if n > 3:
                break
        after = re.split(r"[,;:()]", sentence[m.end():], maxsplit=1)[0]
        n_after = len(_WORD_RE.findall(after))
        if not ok or not 1 <= n_after <= 4 or _first_word(after) in _NOT_AN_ITEM_START:
            continue
        if items + 1 == 3:
            count += 1
    return count


# --------------------------------------------------------------------------- his voice: fragments, comma splices,
# verdict headings. There is no tagger in the standard library, so a finite verb is found by rule: an auxiliary or
# a contraction, a subject pronoun with a word after it, a base verb at the start (an order), or a verb form of the
# list below after a word that is not a determiner or a preposition. The rule misses a verb it does not know and
# takes a noun such as "check" for a verb after an adjective. The numbers are reported, never blocking.

_AUXILIARIES = frozenset("""
am is are was were be been being has have had do does did can could will would shall should may might must ought
cannot
""".split())
_SUBJECT_CONTRACTIONS = frozenset("""
i'm you're we're they're he's she's it's that's there's here's what's who's i've you've we've they've i'll you'll
we'll they'll he'll she'll it'll that'll i'd you'd we'd they'd he'd she'd it'd let's
""".split())
_SUBJECTS = frozenset("i you he she it we they".split())
_DETERMINERS = frozenset("""
a an the this that these those my your our their its his her no each every any some all both either neither one
two three first second next last same other another such of for to in on at by with from into onto per via about
""".split())
_VERB_BASES = frozenset("""
accept add agree allow answer apply arrive ask attach avoid become begin believe belong block book break bring
build buy call cancel carry cause change charge check choose clean close come compare configure confirm connect
contain continue copy correct cost count cover create cut decide define delete deliver depend deploy describe
destroy differ disable do drop enable encrypt end enter exist expect expire explain fail fall feel fetch fill
find finish fit fix follow forget get give go grow happen hear help hide hit hold hope host include increase
install keep know last lead learn leave let lie like limit link list listen live load lock log look lose love
make match matter mean meet migrate mind miss mount move need offer open order own pass pay plan play point
prefer prepare prevent print promise protect prove provide publish pull push put reach read receive reduce refer
register reject release remain remember remove rename replace reply report request require reserve resize restart
restore return reuse review run save say scale see seem sell send serve set share show shut sign sit sleep sort
sound speak spend split stand start state stay stop store suggest supply support suppose sync take talk teach tell
test thank think throw track transfer try turn understand update upgrade upload use wait want warn watch win wish
work worry write
""".split())
_IRREGULAR_PAST = frozenset("""
became began bought brought built came chose did drew drove fell felt found forgot froze gave went got grew heard
held hid kept knew laid led left lost made meant met paid ran rose said saw sold sent shook shot showed sat slept
spoke spent stood stole stuck swore took taught tore told thought threw understood woke wore won wrote rebuilt
rewrote
""".split())


def _verb_forms(bases) -> frozenset:
    """The -s and past forms of the base verbs, generated by the regular rules (a few extra forms do no harm)."""
    forms: set[str] = set()
    for b in bases:
        forms.add(b + "s")
        if b.endswith(("s", "x", "z", "ch", "sh", "o")):
            forms.add(b + "es")
        if len(b) > 2 and b.endswith("y") and b[-2] not in "aeiou":
            forms.update((b[:-1] + "ies", b[:-1] + "ied"))
        elif b.endswith("e"):
            forms.add(b + "d")
        else:
            forms.add(b + "ed")
            if len(b) >= 3 and b[-1] not in "aeiouwxy" and b[-2] in "aeiou" and b[-3] not in "aeiou":
                forms.add(b + b[-1] + "ed")
    return frozenset(forms)


_VERB_FORMS = _verb_forms(_VERB_BASES) | _IRREGULAR_PAST
_ANSWER_WORDS = frozenset("yes no correct right true false done agreed sure ok okay thanks hi hello".split())
_SUBORDINATORS = frozenset("""
if when while because since although though as after before once unless until whereas whether where whenever
wherever even
""".split())
_SPLICE_SUBJECTS = frozenset("i you he she it we they there this that these those".split())
_TERMINAL = ".!?"
_CLOSERS = "\"')]’”*_"


def _lowered(text: str) -> list[str]:
    return [w.lower().replace("’", "'") for w in _WORD_RE.findall(text)]


def _finite(words: list[str]) -> bool:
    """True when the lower-cased words hold a finite verb by the rule above."""
    for i, w in enumerate(words):
        if w in _AUXILIARIES or w in _SUBJECT_CONTRACTIONS or w.endswith("n't"):
            return True
        if w in _SUBJECTS and i + 1 < len(words):
            return True
        prev = words[i - 1] if i else ""
        if i == 0 or (i == 1 and prev == "please"):
            if w in _VERB_BASES:
                return True
            continue
        if prev in _DETERMINERS:
            continue
        if w in _VERB_BASES or w in _VERB_FORMS:
            return True
    return False


def _is_fragment(s: _Sentence) -> bool:
    """A sentence of running prose (not a bullet item) that ends with . ! or ?, has at most FRAGMENT_MAX_WORDS
    words, is not a one-word answer or greeting and holds no finite verb: "No downtime." "The catch?"."""
    if s.bullet or s.words > FRAGMENT_MAX_WORDS:
        return False
    end = s.text.rstrip().rstrip(_CLOSERS)
    if not end or end[-1] not in _TERMINAL:
        return False
    words = _lowered(s.text)
    if len(words) == 1 and words[0] in _ANSWER_WORDS:
        return False
    return not _finite(words)


def _splices(sentence: str) -> int:
    """Comma splices: two clauses with a verb each, joined by a comma alone: "The quota is fine, it is the flavor
    that fails." Not counted: a clause that starts with if, when, because and the like, an opening word or phrase
    ("Yes, it works"), a comma before then, so or but, a short aside (", I think,") and a short tail (", I think.")."""
    count = 0
    for m in re.finditer(r",\s+", sentence):
        after = _lowered(sentence[m.end():])
        if len(after) < 3:
            continue
        w0, w1 = after[0], after[1]
        if not (w0 in _SUBJECT_CONTRACTIONS
                or (w0 in _SPLICE_SUBJECTS
                    and (w1 in _AUXILIARIES or w1.endswith("n't") or w1 in _VERB_FORMS
                         or (w1 in _VERB_BASES and w0 in ("i", "you", "we", "they"))))):
            continue
        rest = sentence[m.end():]
        comma = rest.find(",")
        if comma >= 0 and len(_WORD_RE.findall(rest[:comma])) <= 3:
            continue
        before = sentence[:m.start()]
        cut = max(before.rfind(","), before.rfind(";"), before.rfind(":"), before.rfind("("))
        clause = _lowered(before[cut + 1:])
        if len(clause) < 2 or clause[0] in _SUBORDINATORS or not _finite(clause):
            continue
        count += 1
    return count


_HEADING_MARKS_RE = re.compile(r"^ {0,3}#{1,6}[ \t]*|[ \t]+#+[ \t]*$")
_HEADING_NUMBER_RE = re.compile(r"^(?:\d+(?:\.\d+)*\.?|[A-Z]\.|(?i:step|part|phase)\s+\d+\s*[:.]?)\s+")
_VERDICT_RE = re.compile(
    r"(?i)(?<!\w)(?:verdict|bottom line|winners?|wins|takeaways?|tl;?dr|short answer|in a nutshell|no-brainer"
    r"|game[- ]changer|the way to go|the clear choice|the right choice|the better (?:choice|fit|option)"
    r"|pays off|makes sense|matters|beats)(?!\w)")
_VERDICT_LEAD_RE = re.compile(
    r"(?i)^(?:recommendation|decision|answer|result|conclusion|outcome|summary|bottom line)\s*:\s*\S+(?:\s+\S+)*")
_QUESTION_WORDS = frozenset("what how when where which who whom whose".split())


def heading_text(line: str) -> str:
    """The words of a heading line: the # marks, a numbering and emphasis marks taken off."""
    t = _HEADING_MARKS_RE.sub("", line).strip()
    t = t.replace("**", "").replace("__", "").strip()
    return _HEADING_NUMBER_RE.sub("", t).strip()


def _is_verdict(heading: str) -> bool:
    """A heading written as a verdict: it states a conclusion instead of naming a topic. "CCE is the right choice",
    "Why CCE wins", "Recommendation: CCE", "Costs stay low." A topic ("Network", "Next steps") and a question
    ("What is in scope") are not verdicts."""
    t = heading.strip()
    if not t:
        return False
    if _VERDICT_RE.search(t) or _VERDICT_LEAD_RE.match(t):
        return True
    words = _lowered(t)
    if not words or words[0] in _QUESTION_WORDS:
        return False
    if t.rstrip(_CLOSERS)[-1:] in (".", "!"):
        return True
    return len(words) >= 3 and any(w in _AUXILIARIES or w in _SUBJECT_CONTRACTIONS or w.endswith("n't")
                                   for w in words[1:])


def _p90(values: list[int]) -> int:
    """The 90th percentile by nearest rank."""
    if not values:
        return 0
    ordered = sorted(values)
    return ordered[max(0, math.ceil(0.9 * len(ordered)) - 1)]


def _num(x) -> str:
    x = float(x)
    return "%d" % x if x == int(x) else "%.1f" % x


def _per_1000(count: int, words: int) -> float:
    return round(count * 1000.0 / words, 1) if words else 0.0


def _structure(text: str) -> tuple[list[_Line], list[_Sentence]]:
    lines = _lines(text)
    return lines, _sentences(text, _units(lines))


# --------------------------------------------------------------------------- public checks


def _check_args(mode: str, scope: str) -> None:
    if mode not in MODES:
        raise WritingError("mode must be mail, doc or chat")
    if scope not in SCOPES:
        raise WritingError("scope must be tcp or hcs")


def check_text(text: str, mode: str = "doc", scope: str = "tcp") -> tuple[list[Tell], dict]:
    """(tells, metrics) of a text. Raises WritingError on an unknown mode or scope or an unreadable rule file."""
    _check_args(mode, scope)
    rules = load_rules()
    comp = _compiled(rules)
    masked = prose(text if isinstance(text, str) else "")
    starts = _line_starts(masked)

    def line_of(pos: int) -> int:
        return bisect_right(starts, pos)

    lined = _lined_hits(masked, mode, scope, comp)
    hits = [h for _, h in lined]
    tells = [Tell(line, h.cls, True, _hint(h.cls, h.label)) for line, h in lined]

    lines, sentences = _structure(masked)
    lists_of_three = 0
    for run in _bullet_runs(lines):
        first = run[0] + 1
        bold = sum(1 for i in run if _BOLD_START_RE.match(_BULLET_RE.match(lines[i].text).group("body")))
        if len(run) >= 3 and bold > 1:
            tells.append(Tell(first, "bold-lead-list", mode != "chat",
                              "a list of %d bullets where %d start with bold: write plain items" % (len(run), bold)))
        top = min(_indent(lines[i].text) for i in run)
        if sum(1 for i in run if _indent(lines[i].text) < top + 2) == 3:
            lists_of_three += 1
            tells.append(Tell(first, "list-of-three", False, "a list of exactly three items"))
    for s in sentences:
        n = _triads(s.text)
        if n:
            lists_of_three += n
            tells.extend(Tell(line_of(s.start), "list-of-three", False, "a list of exactly three items")
                         for _ in range(n))

    fragments = splices = 0
    for s in sentences:
        if _is_fragment(s):
            fragments += 1
            tells.append(Tell(line_of(s.start), "fragment", False,
                              "a sentence without a verb: write it as a full sentence or join it to the next"))
        n = _splices(s.text)
        if n:
            splices += n
            tells.extend(Tell(line_of(s.start), "comma-splice", False,
                              "two sentences joined by a comma: use a full stop or join them with and, so or but")
                         for _ in range(n))
    headings = verdicts = 0
    for i, ln in enumerate(lines):
        if ln.kind != "heading":
            continue
        headings += 1
        if _is_verdict(heading_text(ln.text)):
            verdicts += 1
            tells.append(Tell(i + 1, "verdict-heading", False,
                              "a heading that states a verdict: name the topic, the verdict goes in the text"))
    others = _find(masked, comp.others, "connector")
    tells.extend(Tell(line_of(h.start), "connector", False,
                      'a connector outside his set with "%s": use and, so, then, but, also or because' % h.label)
                 for h in others)
    his = len(_find(masked, comp.his, "his-connector"))

    counts = [s.words for s in sentences]
    words = sum(counts)
    median = float(statistics.median(counts)) if counts else 0.0
    p90 = _p90(counts)
    targets = rules.targets
    t_median = targets.get("median_sentence_words", DEFAULT_TARGETS["median_sentence_words"])
    t_p90 = targets.get("p90_sentence_words", DEFAULT_TARGETS["p90_sentence_words"])
    if counts and (median > t_median or p90 > t_p90):
        longest = max(sentences, key=lambda s: s.words)
        tells.append(Tell(line_of(longest.start), "sentence-length", False,
                          "median %s and p90 %s words per sentence, targets %s and %s"
                          % (_num(median), _num(p90), _num(t_median), _num(t_p90))))
    tokens = [w for s in sentences for w in _WORD_RE.findall(s.text)]
    long_words = sum(1 for w in tokens for part in _LETTERS_RE.findall(w) if len(part) > LONG_WORD_LETTERS)
    i_my_me = sum(1 for w in tokens if w.lower().replace("\u2019", "'") in _I_MY_ME)
    i_rate = _per_1000(i_my_me, words)
    t_i = targets.get("i_my_me_per_1000", DEFAULT_TARGETS["i_my_me_per_1000"])
    if mode == "mail" and words >= I_RATE_MIN_WORDS and i_rate < t_i * I_RATE_SHARE:
        tells.append(Tell(line_of(sentences[0].start), "i-rate", False,
                          "I, my and me at %s per 1000 words, his rate is %s: a mail in his name speaks as I"
                          % (_num(i_rate), _num(t_i))))

    order = {c: i for i, c in enumerate(CLASSES)}
    tells.sort(key=lambda t: (t.line, order.get(t.cls, len(order))))
    metrics = {
        "mode": mode,
        "scope": scope,
        "sentences": len(counts),
        "words": words,
        "median_sentence_words": median,
        "p90_sentence_words": p90,
        "long_words": long_words,
        "lists_of_three": lists_of_three,
        "comma_before_and_or_per_1000": _per_1000(sum(1 for h in hits if h.cls == "comma-and-or"), words),
        "em_dash_per_1000": _per_1000(sum(1 for h in hits if h.cls == "em-dash"), words),
        "i_my_me_per_1000": i_rate,
        "fragments": fragments,
        "fragment_share": round(100.0 * fragments / len(counts), 1) if counts else 0.0,
        "comma_splices": splices,
        "headings": headings,
        "verdict_headings": verdicts,
        "his_connectors_per_1000": _per_1000(his, words),
        "other_connectors_per_1000": _per_1000(len(others), words),
        "targets": dict(targets),
        "blocking": sum(1 for t in tells if t.blocking),
        "reported": sum(1 for t in tells if not t.blocking),
    }
    return tells, metrics


def name_as_text(name: str) -> str:
    """A file name as the words it reads as: the suffix dropped, - _ and . as spaces."""
    stem = name.strip()
    dot = stem.rfind(".")
    if dot > 0:
        stem = stem[:dot]
    return re.sub(r"[-_.\s]+", " ", stem).strip()


def check_name(name: str, mode: str = "doc", scope: str = "tcp") -> list[Tell]:
    """Tells of a file name, all on line 0."""
    _check_args(mode, scope)
    text = name_as_text(name).replace("\n", " ")
    hits = [h for _, h in _lined_hits(text, mode, scope, _compiled(load_rules()))]
    return [Tell(FILE_NAME_LINE, h.cls, True, "file name: %s" % _hint(h.cls, h.label)) for h in hits]


_BINARY_MAGIC = (b"%PDF", b"PK\x03\x04", b"PK\x05\x06", b"\xd0\xcf\x11\xe0", b"\x89PNG", b"\xff\xd8\xff",
                 b"GIF87a", b"GIF89a", b"\x1f\x8b")


def _decode(data: bytes) -> str | None:
    head = data[:8192]
    if head.startswith(_BINARY_MAGIC):
        return None
    if head.startswith((b"\xff\xfe", b"\xfe\xff")):
        try:
            return data.decode("utf-16")
        except UnicodeDecodeError:
            return None
    if b"\x00" in head:
        return None
    try:
        return data.decode("utf-8-sig")
    except UnicodeDecodeError:
        return None


def _extracted(path: Path) -> str | None:
    """The text of an office or pdf file through awb.extract, None when it cannot be read in full."""
    try:
        from awb import extract
    except ImportError:
        return None
    try:
        ex = extract.extract(path)
    except Exception:
        return None
    if getattr(ex, "state", "") != "ok" or not (getattr(ex, "text", "") or "").strip():
        return None
    return ex.text


def read_text(path: Path) -> str:
    """The text of a file: UTF-8 (or UTF-16 with a BOM) directly, an office or pdf file through awb.extract.
    Raises OSError from the read and WritingError when there is no text to check."""
    path = Path(path)
    data = path.read_bytes()
    if len(data) > MAX_FILE_BYTES:
        raise WritingError("the file is larger than the writing check reads (%d MB)" % (MAX_FILE_BYTES // 1_000_000))
    text = _decode(data)
    if text is None:
        text = _extracted(path)
    if text is None:
        raise WritingError("the file cannot be read as text")
    return text


def check_file(path: Path, mode: str = "doc", scope: str = "tcp") -> tuple[list[Tell], dict]:
    """(tells, metrics) of a file, the tells of its name first (line 0)."""
    _check_args(mode, scope)
    path = Path(path)
    tells, metrics = check_text(read_text(path), mode, scope)
    named = check_name(path.name, mode, scope)
    if named:
        metrics["blocking"] += sum(1 for t in named if t.blocking)
    return named + tells, metrics


# --------------------------------------------------------------------------- facts before voice (V-15)

KEEP_KINDS = ("number", "identifier", "negation", "code block")
KEEP_LINES_SHOWN = 12
"""Line numbers shown per side and kind in `awb write keep`."""

_NUMBER_WORDS = {w: str(i) for i, w in enumerate(
    "zero _ two three four five six seven eight nine ten eleven twelve thirteen fourteen fifteen sixteen seventeen "
    "eighteen nineteen twenty".split()) if w != "_"}
_NUMBER_WORDS.update({w: str(10 * i) for i, w in enumerate("thirty forty fifty sixty seventy eighty ninety".split(), 3)})
_NEGATIONS = frozenset("not no never none nothing nobody nowhere neither nor without cannot".split())
_NEGATED = frozenset("""
unavailable unsupported unable unencrypted unlimited unknown unused unchanged unreachable unnecessary unclear unsafe
unstable unmanaged unpaid unassigned unattached unlisted invalid incorrect impossible inactive incompatible
insufficient incomplete inconsistent nonexistent non-existent
""".split())
_NOT_IDENTIFIERS = frozenset("ok ps fyi asap cc bcc".split())
_KEEP_TOKEN_RE = re.compile(r"[^\W_](?:[\w./:+'’-]*[^\W_])?")
_KEEP_MARKER_RE = re.compile(r"^(?: {0,3}#{1,6}[ \t]+(?:\d+(?:\.\d+)*\.?[ \t]+)?|[ \t]*(?:[-*+]|\d{1,9}[.)])[ \t]+)")
_REGION_RE = re.compile(r"^[a-z]{2}-[a-z]{2}(?:-[a-z0-9]+)*$")
_PLAIN_NUMBER_RE = re.compile(r"^\d+(?:[.,]\d+)*$")
_DOTTED_ABBREV_RE = re.compile(r"^(?:[a-z]\.)+[a-z]$")
_CAMEL_RE = re.compile(r"[a-z][A-Z]|^[A-Z][a-z]+[A-Z]")


@dataclass
class Kept:
    """One kind of fact in the text before and after a voice pass. `before` and `after` count distinct numbers and
    identifiers, every negation and every code block. The lines are those of what differs: of a number, identifier
    or code block the other text lacks, of every negation when the two counts differ."""
    kind: str
    before: int
    after: int
    lines_before: list[int]
    lines_after: list[int]

    @property
    def same(self) -> bool:
        return self.before == self.after and not self.lines_before and not self.lines_after


def _fact_kind(token: str) -> tuple[str, str] | None:
    """(kind, value) of one token of prose, None for a plain word."""
    tok = re.sub(r"['’]s$", "", token)
    low = tok.lower().replace("’", "'")
    if low in _NEGATIONS or low.endswith("n't") or low in _NEGATED:
        return "negation", "not"
    if low in _NUMBER_WORDS:
        return "number", _NUMBER_WORDS[low]
    if _PLAIN_NUMBER_RE.match(tok):
        return "number", tok
    if not tok:
        return None
    if any(c.isdigit() for c in tok) and any(c.isalpha() for c in tok):
        return "identifier", tok
    if any(c in "./:+_" for c in tok[1:-1]):
        if _DOTTED_ABBREV_RE.match(low) or low in _ABBREVIATIONS:
            return None
        return "identifier", tok
    if "'" in low:
        return None
    if _REGION_RE.match(tok):
        return "identifier", tok
    letters = [c for c in tok if c.isalpha()]
    if len(letters) >= 2 and all(c.isupper() for c in letters) and low not in _NOT_IDENTIFIERS:
        return "identifier", tok
    if _CAMEL_RE.search(tok):
        return "identifier", tok
    return None


def facts_of(text: str) -> dict[str, list[tuple[str, int]]]:
    """(value, line) of every fact of a text by kind: numbers (digits, and the number words two to ninety as
    digits), identifiers (a token with digits and letters or with . / : + _ inside, a region such as eu-de, a word
    in capitals, a CamelCase word, an inline code span, a URL, a mail address), negations (not, no, never, n't,
    without, unavailable and the like, all as one value) and fenced code blocks. List and heading numbers are
    structure, not facts, and are left out. The word "one" is counted apart (see `keep`)."""
    lines = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    out: dict[str, list[tuple[str, int]]] = {k: [] for k in KEEP_KINDS}
    out["one"] = []
    body = list(lines)
    i = 0
    while i < len(lines):
        m = _FENCE_RE.match(lines[i])
        if m and not (m.group(1)[0] == "`" and "`" in m.group(2)):
            fence = m.group(1)
            close = re.compile(r"^ {0,3}%s{%d,}[ \t]*$" % (re.escape(fence[0]), len(fence)))
            end = next((k for k in range(i + 1, len(lines)) if close.match(lines[k])), None)
            if end is not None:
                block = "\n".join(x.rstrip() for x in lines[i + 1:end]).strip("\n")
                out["code block"].append((block, i + 1))
                for k in range(i, end + 1):
                    body[k] = ""
                i = end + 1
                continue
        i += 1
    for n, line in enumerate(body, 1):
        chars = list(line)
        for a, b in _code_spans(line):
            out["identifier"].append((line[a:b].strip("`").strip(), n))
            chars[a:b] = " " * (b - a)
        rest = "".join(chars)
        for pattern in (_URL_RE, _MAIL_RE):
            for m in pattern.finditer(rest):
                out["identifier"].append((m.group(0).rstrip(_TRAILING), n))
            rest = pattern.sub(lambda m: " " * len(m.group(0)), rest)
        marker = _KEEP_MARKER_RE.match(rest)
        if marker:
            rest = " " * marker.end() + rest[marker.end():]
        for m in _KEEP_TOKEN_RE.finditer(rest):
            if m.group(0).lower() == "one":
                out["one"].append(("1", n))
                continue
            found = _fact_kind(m.group(0))
            if found:
                out[found[0]].append((found[1], n))
    return out


def _lines_of(items: list[tuple[str, int]], values) -> list[int]:
    return sorted({line for value, line in items if value in values})


def keep(before: str, after: str) -> list[Kept]:
    """V-15: the facts of the text before a voice pass against the text after it, one `Kept` per kind in the order
    of KEEP_KINDS. Numbers and identifiers are compared as sets of values (a repeat that the pass merged is not a
    change), negations by their count, code blocks by their content. The word "one" pairs with a 1 that the other
    text has and lacks there: "one node" and "1 node" are the same fact."""
    a, b = facts_of(before), facts_of(after)
    out: list[Kept] = []
    for kind in KEEP_KINDS:
        if kind == "negation":
            n_a, n_b = len(a[kind]), len(b[kind])
            differ = n_a != n_b
            out.append(Kept(kind, n_a, n_b, sorted({x for _, x in a[kind]}) if differ else [],
                            sorted({x for _, x in b[kind]}) if differ else []))
            continue
        if kind == "code block":
            ca, cb = Counter(v for v, _ in a[kind]), Counter(v for v, _ in b[kind])
            out.append(Kept(kind, sum(ca.values()), sum(cb.values()), _lines_of(a[kind], set(ca - cb)),
                            _lines_of(b[kind], set(cb - ca))))
            continue
        va, vb = {v for v, _ in a[kind]}, {v for v, _ in b[kind]}
        only_a, only_b = va - vb, vb - va
        if kind == "number":
            if "1" in only_a and b["one"]:
                only_a.discard("1")
                va.discard("1")
            if "1" in only_b and a["one"]:
                only_b.discard("1")
                vb.discard("1")
        out.append(Kept(kind, len(va), len(vb), _lines_of(a[kind], only_a), _lines_of(b[kind], only_b)))
    return out


def keep_files(before: Path, after: Path) -> list[Kept]:
    """`keep` over two files, read like `check_file` reads them."""
    return keep(read_text(Path(before)), read_text(Path(after)))


def _shown(lines: list[int]) -> str:
    head = ", ".join(str(n) for n in lines[:KEEP_LINES_SHOWN])
    more = len(lines) - KEEP_LINES_SHOWN
    return head + (" and %d more" % more if more > 0 else "")


def keep_lines(results: list[Kept]) -> list[str]:
    """The report of `awb write keep`: kinds, counts and line numbers, never a value."""
    out = []
    for k in results:
        label = k.kind + "s"
        if k.same:
            out.append("%s  same, %d in both" % (label, k.before))
            continue
        parts = ["%d before, %d after" % (k.before, k.after)]
        if k.lines_before:
            parts.append("see BEFORE line%s %s" % ("" if len(k.lines_before) == 1 else "s", _shown(k.lines_before)))
        if k.lines_after:
            parts.append("see AFTER line%s %s" % ("" if len(k.lines_after) == 1 else "s", _shown(k.lines_after)))
        out.append("%s  differ: %s" % (label, "; ".join(parts)))
    return out


# --------------------------------------------------------------------------- voice learning


def today() -> date:
    """The date of today. Tests replace it."""
    return date.today()


_FUNCTION_WORDS = frozenset("""
a an the and or but nor so yet to of in on at by for with from into onto over under about as than then that this
these those it its is are was were be been being am do does did done have has had having i me my we us our you your
he him his she her they them their there here what which who whom whose when where why how if not no yes all any
some each both either neither can could will would shall should may might must also too up down out off
""".split())


def _learn_words(text: str) -> list[str]:
    return [w.lower().replace("\u2019", "'") for w in _WORD_RE.findall(prose(text)) if _LETTERS_RE.search(w)]


@dataclass
class PairStats:
    words: int
    sentences: int
    median: float
    p90: int
    comma_and_or_per_1000: float


def _stats(text: str) -> PairStats:
    masked = prose(text)
    _, sentences = _structure(masked)
    counts = [s.words for s in sentences]
    words = sum(counts)
    commas = sum(1 for _ in _COMMA_AND_OR_RE.finditer(masked))
    return PairStats(words=words, sentences=len(counts),
                     median=float(statistics.median(counts)) if counts else 0.0, p90=_p90(counts),
                     comma_and_or_per_1000=_per_1000(commas, words))


@dataclass
class PairDiff:
    removed_words: list[str]
    added_words: list[str]
    removed_phrases: list[str]
    draft: PairStats
    sent: PairStats
    phrases_skipped: bool = False


def compare(draft: str, sent: str) -> PairDiff:
    """What changed from the draft to the sent text."""
    dw, sw = _learn_words(draft), _learn_words(sent)
    dset, sset = set(dw), set(sw)
    dcount, scount = Counter(dw), Counter(sw)
    removed = sorted((w for w in dcount if w not in sset), key=lambda w: (-dcount[w], w))
    added = sorted((w for w in scount if w not in dset), key=lambda w: (-scount[w], w))
    phrases: list[str] = []
    skipped = len(dw) > MAX_DIFF_WORDS or len(sw) > MAX_DIFF_WORDS
    if not skipped:
        sm = difflib.SequenceMatcher(None, dw, sw, autojunk=False)
        for tag, i1, i2, _, _ in sm.get_opcodes():
            if tag in ("delete", "replace") and 2 <= i2 - i1 <= 8:
                phrase = " ".join(dw[i1:i2])
                if phrase not in phrases:
                    phrases.append(phrase)
    return PairDiff(removed, added, phrases, _stats(draft), _stats(sent), skipped)


@dataclass
class Learned:
    pair: str                  # <date>-<n>
    pair_dir: Path
    report: Path
    removed_words: int
    added_words: int
    removed_phrases: int
    candidates: int
    withheld: bool = False     # the word lists of the report were withheld by the name check


def _voice_root(p: config.Paths) -> Path:
    return p.shared / VOICE_DIR


def _mkdir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(path, 0o750)
    except OSError:
        pass


def _write(path: Path, text: str) -> None:
    """Write `text` atomically, mode 640."""
    fd, tmp = tempfile.mkstemp(prefix=".%s." % path.name, suffix=".tmp", dir=path.parent)
    try:
        os.fchmod(fd, 0o640)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _read_voice_file(path: Path, what: str) -> str:
    try:
        data = Path(path).read_bytes()
    except OSError as err:
        raise WritingError("the %s cannot be read (%s)" % (what, type(err).__name__)) from None
    text = _decode(data)
    if text is None:
        raise WritingError("the %s is not a UTF-8 text file" % what)
    if len(text) > MAX_VOICE_CHARS:
        raise WritingError("the %s is longer than %d characters" % (what, MAX_VOICE_CHARS))
    if not text.strip():
        raise WritingError("the %s is empty" % what)
    return text


def _detectors() -> dict:
    try:
        from awb import gate
    except ImportError:
        return {}
    return dict(getattr(gate, "DETECTORS", {}) or {})


def _unavailable(err: Exception) -> WritingError:
    from awb import check

    if isinstance(err, check.CheckUnavailable):
        return WritingError(str(err) or "name check unavailable")
    return WritingError("the register cannot be read")


def _require_register(reg: Path) -> None:
    from awb import check

    try:
        kind, _ = check.register_source(reg)
    except Exception as err:
        raise _unavailable(err) from None
    if kind == check.MISSING:
        raise WritingError("no register found, names cannot be checked")


def screen(text: str, reg: Path, *, names_only: bool = False) -> dict[str, int]:
    """Counts per class of the name check (names and structured data) and of the gate detectors over `text`.
    Raises WritingError when the check cannot run. Never returns or raises with a value."""
    from awb import check

    try:
        hits = check.check_text(text, reg)
    except Exception as err:
        raise _unavailable(err) from None
    counts: Counter = Counter()
    for h in hits:
        cls = h.get("cls", "unknown") if isinstance(h, dict) else "unknown"
        if not names_only or cls == "name":
            counts[cls] += 1
    if not names_only:
        for cls, fn in _detectors().items():
            try:
                n = len(fn(text))
            except Exception:
                n = 0
            if n:
                counts[cls] += n
    return dict(counts)


def _counts_text(counts: dict[str, int]) -> str:
    return ", ".join("%s %d" % kv for kv in sorted(counts.items()))


_PAIR_RE = re.compile(r"(?P<date>[0-9]{4}-[0-9]{2}-[0-9]{2})-(?P<n>[0-9]{1,6})")


def _pairs(root: Path) -> list[tuple[str, int, Path]]:
    """Every stored pair as (date, n, folder), oldest first."""
    folder = root / PAIRS_DIR
    try:
        names = os.listdir(folder)
    except OSError:
        return []
    out = []
    for name in names:
        m = _PAIR_RE.fullmatch(name)
        if m and (folder / name).is_dir():
            out.append((m.group("date"), int(m.group("n")), folder / name))
    return sorted(out, key=lambda t: (t[0], t[1]))


def _new_pair_dir(root: Path, day: str) -> tuple[str, Path]:
    folder = root / PAIRS_DIR
    _mkdir(folder)
    n = 1 + max((k for d, k, _ in _pairs(root) if d == day), default=0)
    while True:
        name = "%s-%d" % (day, n)
        try:
            os.mkdir(folder / name, 0o750)
        except FileExistsError:
            n += 1
            continue
        os.chmod(folder / name, 0o750)
        return name, folder / name


def _load_pair(folder: Path) -> tuple[str, str] | None:
    try:
        return ((folder / DRAFT_NAME).read_text(encoding="utf-8"),
                (folder / SENT_NAME).read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError):
        return None


def candidates(root: Path, rules: Rules) -> tuple[list[tuple[str, int]], list[tuple[str, int]]]:
    """Words and phrases removed in at least MIN_PAIRS stored pairs that no banned list has yet."""
    words: Counter = Counter()
    phrases: Counter = Counter()
    banned_words = set(rules.banned_words)
    banned_phrases = {" ".join(p.split()) for p in rules.banned_phrases}
    for _, _, folder in _pairs(root):
        pair = _load_pair(folder)
        if pair is None:
            continue
        diff = compare(*pair)
        words.update({w for w in diff.removed_words
                      if w not in banned_words and w not in _FUNCTION_WORDS
                      and sum(c.isalpha() for c in w) >= 3})
        phrases.update({p for p in diff.removed_phrases if len(p.split()) <= 4 and p not in banned_phrases
                        and not all(w in _FUNCTION_WORDS for w in p.split())})
    pick = lambda c: sorted(((k, n) for k, n in c.items() if n >= MIN_PAIRS), key=lambda kv: (-kv[1], kv[0]))
    return pick(words), pick(phrases)


def _bullets(items: list[str], withheld: bool) -> list[str]:
    if withheld:
        return ["- %d items, withheld by the name check" % len(items)] if items else ["- none"]
    if not items:
        return ["- none"]
    out = ["- %s" % i for i in items[:LIST_LIMIT]]
    if len(items) > LIST_LIMIT:
        out.append("- and %d more" % (len(items) - LIST_LIMIT))
    return out


def _change(a: float, b: float) -> str:
    d = round(b - a, 1)
    return ("+%s" % _num(d)) if d > 0 else _num(d)


def render_report(day: str, pairs: list[tuple[str, PairDiff]], words: list[tuple[str, int]],
                  phrases: list[tuple[str, int]], withheld: bool = False) -> str:
    lines = ["# Voice suggestions of %s" % day, "",
             "Pairs learned on this date: %d. This report changes no rule. When you agree with a candidate, add it "
             "to rules/%s or rules/%s yourself." % (len(pairs), WORDS_FILE, PHRASES_FILE), ""]
    for name, diff in pairs:
        d, s = diff.draft, diff.sent
        lines += ["## Pair %s" % name, "",
                  "- words per sentence, median: %s in the draft, %s as sent (change %s)"
                  % (_num(d.median), _num(s.median), _change(d.median, s.median)),
                  "- words per sentence, p90: %s in the draft, %s as sent (change %s)"
                  % (_num(d.p90), _num(s.p90), _change(d.p90, s.p90)),
                  "- sentences: %d in the draft, %d as sent" % (d.sentences, s.sentences),
                  "- commas before and/or per 1000 words: %s in the draft, %s as sent (change %s)"
                  % (_num(d.comma_and_or_per_1000), _num(s.comma_and_or_per_1000),
                     _change(d.comma_and_or_per_1000, s.comma_and_or_per_1000)),
                  "", "### Words removed (in the draft, not in the sent text)", ""]
        lines += _bullets(diff.removed_words, withheld)
        lines += ["", "### Phrases removed", ""]
        if diff.phrases_skipped:
            lines.append("- not compared: the texts are too long for the phrase diff")
        else:
            lines += _bullets(diff.removed_phrases, withheld)
        lines += ["", "### Words added", ""]
        lines += _bullets(diff.added_words, withheld)
        lines.append("")
    lines += ["## Candidates for the banned lists", "",
              "Removed in %d or more pairs and not banned yet." % MIN_PAIRS, "", "### Words", ""]
    lines += _bullets(["%s (%d pairs)" % kv for kv in words], withheld)
    lines += ["", "### Phrases", ""]
    lines += _bullets(["%s (%d pairs)" % kv for kv in phrases], withheld)
    return "\n".join(lines) + "\n"


def learn(draft: Path, sent: Path, p: config.Paths | None = None, day=None) -> Learned:
    """Name-check both files, store the pair and write the suggestions report of the day. Raises Refused on a
    hit (nothing is written) and WritingError when a file or the check fails."""
    p = p or config.paths()
    run = day if isinstance(day, date) else (date.fromisoformat(day) if day else today())
    stamp = run.isoformat()
    texts = [(_read_voice_file(draft, "draft"), "draft"), (_read_voice_file(sent, "sent text"), "sent text")]
    reg = p.register
    _require_register(reg)
    for text, what in texts:
        found = screen(text, reg)
        if found:
            raise Refused("the %s carries hits of the name check (%s), nothing was written"
                          % (what, _counts_text(found)))
    rules = load_rules()
    root = _voice_root(p)
    _mkdir(root)
    name, folder = _new_pair_dir(root, stamp)
    try:
        _write(folder / DRAFT_NAME, texts[0][0])
        _write(folder / SENT_NAME, texts[1][0])
    except BaseException:
        for f in (folder / DRAFT_NAME, folder / SENT_NAME):
            try:
                f.unlink()
            except OSError:
                pass
        try:
            folder.rmdir()
        except OSError:
            pass
        raise
    this = compare(texts[0][0], texts[1][0])
    of_day = []
    for d, k, f in _pairs(root):
        if d != stamp:
            continue
        pair = (texts[0][0], texts[1][0]) if f == folder else _load_pair(f)
        if pair is not None:
            of_day.append(("%s-%d" % (d, k), this if f == folder else compare(*pair)))
    words, phrases = candidates(root, rules)
    text = render_report(stamp, of_day, words, phrases)
    withheld = False
    if screen(text, reg, names_only=True):
        # single words that passed alone can meet a registered form once they stand in a list
        withheld = True
        text = render_report(stamp, of_day, words, phrases, withheld=True)
        if screen(text, reg, names_only=True):
            raise Refused("the suggestions report does not pass the name check, it was not written")
    report = root / ("suggestions-%s.md" % stamp)
    _write(report, text)
    return Learned(pair=name, pair_dir=folder, report=report, removed_words=len(this.removed_words),
                   added_words=len(this.added_words), removed_phrases=len(this.removed_phrases),
                   candidates=len(words) + len(phrases), withheld=withheld)


# --------------------------------------------------------------------------- command line


def _exit_code(exc: SystemExit) -> int:
    if exc.code is None:
        return EXIT_OK
    return exc.code if isinstance(exc.code, int) else EXIT_ERROR


def _plural(n: int, word: str) -> str:
    return "%d %s%s" % (n, word, "" if n == 1 else "s")


def _fail(prog: str, err: Exception) -> int:
    if isinstance(err, OSError):
        print("%s: %s (operating system error)" % (prog, type(err).__name__), file=sys.stderr)
        return EXIT_ERROR
    print("%s: %s" % (prog, err), file=sys.stderr)
    return EXIT_FINDINGS if isinstance(err, Refused) else EXIT_ERROR


def metric_lines(metrics: dict) -> list[str]:
    t = metrics.get("targets", {})

    def target(key: str) -> str:
        return " (target %s)" % _num(t[key]) if key in t else ""

    return [
        "sentences  %d" % metrics["sentences"],
        "words  %d" % metrics["words"],
        "median words per sentence  %s%s" % (_num(metrics["median_sentence_words"]),
                                             target("median_sentence_words")),
        "p90 words per sentence  %s%s" % (_num(metrics["p90_sentence_words"]), target("p90_sentence_words")),
        "words longer than nine letters  %d" % metrics["long_words"],
        "lists of exactly three  %d" % metrics["lists_of_three"],
        "commas before and/or per 1000 words  %s%s" % (_num(metrics["comma_before_and_or_per_1000"]),
                                                       target("comma_before_and_or_per_1000")),
        "dashes per 1000 words  %s%s" % (_num(metrics["em_dash_per_1000"]), target("em_dash_per_1000")),
        "I my me per 1000 words  %s%s" % (_num(metrics["i_my_me_per_1000"]), target("i_my_me_per_1000")),
        "fragments  %d (%s%% of sentences)" % (metrics["fragments"], _num(metrics["fragment_share"])),
        "comma splices  %d" % metrics["comma_splices"],
        "headings written as verdicts  %d of %d" % (metrics["verdict_headings"], metrics["headings"]),
        "his connectors per 1000 words  %s" % _num(metrics["his_connectors_per_1000"]),
        "other connectors per 1000 words  %s%s" % (_num(metrics["other_connectors_per_1000"]),
                                                   target("other_connectors_per_1000")),
    ]


def _main_write(argv: list[str]) -> int:
    from awb.cli import SafeParser

    prog = "awb write"
    ap = SafeParser(prog=prog, description="Check a text against the house style. Prints class, line and hint.")
    sub = ap.add_subparsers(dest="command", parser_class=SafeParser)
    s = sub.add_parser("check", help="check one file")
    s.add_argument("file", type=Path)
    s.add_argument("--mode", choices=MODES, default="doc")
    s.add_argument("--scope", choices=SCOPES, default="tcp")
    k = sub.add_parser("keep", help="the facts of a text before and after the voice pass must be the same")
    k.add_argument("before", type=Path)
    k.add_argument("after", type=Path)
    try:
        args = ap.parse_args(argv)
    except SystemExit as exc:
        return _exit_code(exc)
    if args.command == "keep":
        return _main_keep(prog, args.before, args.after)
    if args.command != "check":
        ap.print_usage(sys.stderr)
        return EXIT_ERROR
    try:
        tells, metrics = check_file(args.file, args.mode, args.scope)
    except (WritingError, OSError) as err:
        return _fail(prog, err)
    for t in tells:
        print("%s  %d  %s%s" % (t.cls, t.line, t.hint, "" if t.blocking else " (not blocking)"))
    print("metrics:")
    for line in metric_lines(metrics):
        print("  " + line)
    blocking = sum(1 for t in tells if t.blocking)
    print("result: %d blocking, %d reported" % (blocking, len(tells) - blocking))
    return EXIT_FINDINGS if blocking else EXIT_OK


def _main_keep(prog: str, before: Path, after: Path) -> int:
    try:
        results = keep_files(before, after)
    except (WritingError, OSError) as err:
        return _fail(prog, err)
    for line in keep_lines(results):
        print(line)
    differ = [k for k in results if not k.same]
    if differ:
        print("result: %s changed by the voice pass, put the facts of BEFORE back into AFTER"
              % _plural(len(differ), "kind"))
        return EXIT_FINDINGS
    print("result: every number, identifier, negation and code block is kept")
    return EXIT_OK


def _main_voice(argv: list[str]) -> int:
    from awb.cli import SafeParser

    prog = "awb voice"
    ap = SafeParser(prog=prog, description="Learn from a draft and the version that was sent.")
    sub = ap.add_subparsers(dest="command", parser_class=SafeParser)
    s = sub.add_parser("learn", help="store a pair and write the suggestions report")
    s.add_argument("draft", type=Path)
    s.add_argument("sent", type=Path)
    try:
        args = ap.parse_args(argv)
    except SystemExit as exc:
        return _exit_code(exc)
    if args.command != "learn":
        ap.print_usage(sys.stderr)
        return EXIT_ERROR
    try:
        res = learn(args.draft, args.sent)
    except (WritingError, OSError) as err:
        return _fail(prog, err)
    print("voice: pair %s stored under %s/%s" % (res.pair, VOICE_DIR, PAIRS_DIR))
    print("voice: removed %s and %s, added %s, %s for the banned lists"
          % (_plural(res.removed_words, "word"), _plural(res.removed_phrases, "phrase"),
             _plural(res.added_words, "word"), _plural(res.candidates, "candidate")))
    print("voice: report %s/%s" % (VOICE_DIR, res.report.name))
    if res.withheld:
        print("voice: the word lists of the report were withheld by the name check", file=sys.stderr)
    return EXIT_OK


def main(argv: list[str] | None = None) -> int:
    """`awb write check ...` (argv without "write") and `awb voice learn ...` when argv[0] is "voice"."""
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv and argv[0] == "voice":
        return _main_voice(argv[1:])
    return _main_write(argv)


if __name__ == "__main__":
    sys.exit(main())
