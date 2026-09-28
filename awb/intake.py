"""The intake: the only way customer material enters the Workbench.

`run` takes files from the vault inbox (or anywhere), reads each one with `awb.extract`, finds registered
forms and structured data in everything detection can see, looks for strings that look like unregistered
names and then either blocks (candidates found, no force) or writes one sanitised Markdown copy per file to
`<shared>/outbox/<CUST>/<file id>.md`. Every output is checked once more before it counts. Originals move to
`<vault>/originals/<CUST>/` under their file id, only after that check passed. The public report goes next to
the outputs and carries codes, classes and counts. The private report goes to the vault and carries the rest.

When the vault is encrypted (register.tsv.gpg), the register is read through the vault daemon and every
original and private report of the run is sealed right after it is written (`seal_file` of the daemon writes
`<file>.gpg` and removes the plaintext). A file that cannot be sealed stays in plaintext inside the vault and is
counted in `IntakeResult.unsealed`.

The original file name is treated as text: it is scanned and never used for an output. Nothing in this
module writes a matched value, a form or an original name into an exception, a log line or the public side.
"""
from __future__ import annotations

import base64
import os
import re
import secrets
import shutil
import tempfile
import unicodedata
from collections import Counter
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from awb import codes, config, normalize, patterns, register
from awb import extract as _extract
from awb import images as _images
from awb import report as _report
from awb.extract.text import encoded_texts, expand_base64_blocks
from awb.matcher import Matcher, Span

FILE_ID_RE = re.compile(r"F-[A-Z2-7]{4}")
"""The shape of a file id. A member or attachment adds .1, .2 ... to the id of its file."""

MAX_PASSES = 4
SECOND_CLASS = "possible-name"
"""The class of a hit of the second check: a registered form found by its letters across word boundaries."""
_SKELETON_WORD_RE = re.compile(r"[^\W_]+")
_SKELETON_MIN = 3
_SKELETON_CASE_FREE = 6
"""A single word shorter than this counts only when it does not stand in lower case: short forms that are also
ordinary words stay with the case rules of the matcher."""


def _fold(word: str) -> str:
    """Case folded, accents dropped: the letters a reader would still read as the same word."""
    decomposed = unicodedata.normalize("NFKD", word)
    return "".join(c for c in decomposed if not unicodedata.combining(c)).casefold()


def skeleton(text: str) -> str:
    """The letters and digits of `text`, normalised, folded and joined: the same for every way of writing it."""
    return "".join(_fold(w) for w in _SKELETON_WORD_RE.findall(normalize.normalize(text).text))
"""How often the sanitiser normalises and replaces before it gives up and leaves the rest to the final check."""

NEW = "new"


class IntakeError(Exception):
    """The intake cannot run. The message carries codes, counts and positions, never a name or a value."""


@dataclass
class IntakeResult:
    customer: str
    outputs: list[Path]
    public_report: Path       # not written when the run is blocked
    private_report: Path
    blocked: bool
    candidates: int
    states: dict[str, str]    # file id -> state
    unsealed: int = 0         # vault files of this run left in plaintext because sealing failed (encrypted vault)


# --------------------------------------------------------------------------- candidates

_STOP_FILE = patterns.RULES_DIR / "stop-words.txt"


def _load_stop_words() -> tuple[frozenset[str], tuple[re.Pattern, ...]]:
    """Single stop words (case folded) and the multi-word stop phrases as patterns."""
    singles: set[str] = set()
    phrases: list[re.Pattern] = []
    try:
        lines = _STOP_FILE.read_text(encoding="utf-8").splitlines()
    except OSError:
        lines = []
    for line in lines:
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        words = line.split()
        if len(words) == 1:
            singles.add(line.casefold())
        else:
            body = r"\s+".join(re.escape(w) for w in words)
            phrases.append(re.compile(r"(?<![\w-])" + body + r"(?![\w-])", re.IGNORECASE))
    return frozenset(singles), tuple(phrases)


_STOP_SINGLES, _STOP_PHRASES = _load_stop_words()

_OPENERS_FILE = patterns.RULES_DIR / "sentence-openers.txt"


def _load_openers() -> frozenset[str]:
    """Capitalised words that often open a sentence and are never the first half of a name (case folded). A
    missing file gives an empty set: fewer words are dropped, so there are more candidates, never fewer."""
    try:
        lines = _OPENERS_FILE.read_text(encoding="utf-8").splitlines()
    except OSError:
        return frozenset()
    return frozenset(line.strip().casefold() for line in lines
                     if line.strip() and not line.strip().startswith("#") and len(line.split()) == 1)


_OPENERS = _load_openers()

# words that open or close a run of capitalised words without being part of a name: articles, prepositions,
# pronouns, salutations and titles, mail subject prefixes
_FUNCTION_WORDS = frozenset(w.casefold() for w in (
    "Der", "Die", "Das", "Den", "Dem", "Des", "Ein", "Eine", "Einen", "Einem", "Einer", "Eines",
    "Dieser", "Diese", "Dieses", "Diesen", "Diesem", "Jeder", "Jede", "Jedes", "Alle",
    "The", "A", "An", "This", "That", "These", "Those", "Our", "Your", "Their", "Its", "His", "Her",
    "Mit", "Von", "Vom", "Zum", "Zur", "Für", "Bei", "Beim", "Im", "In", "Am", "Auf", "Aus", "Nach",
    "Über", "Unter", "Vor", "Durch", "Gegen", "Ohne", "Um", "Ab", "Bis", "Seit", "Laut", "Gemäß",
    "Und", "Oder", "Aber", "Sowie", "Wir", "Sie", "Ihr", "Ihre", "Ihrer", "Ihren", "Unser", "Unsere",
    "Mein", "Meine", "Es", "Er", "Ich", "Wie", "Wo", "Was", "Wer", "Wann", "Bitte", "Danke",
    "Herr", "Herrn", "Frau", "Hr", "Fr", "Mr", "Mrs", "Ms", "Dr", "Prof", "Liebe", "Lieber",
    "Hallo", "Dear", "Hi", "Hello", "For", "From", "To", "With", "By", "Of", "On", "At", "And", "Or",
    "Re", "Aw", "Wg", "Fw", "Fwd",
))

# names of authoring tools that office and pdf metadata carry in every file ("Microsoft Macintosh Word");
# they would block nearly every intake, so they never count as part of a name
_TOOL_WORDS = frozenset(w.casefold() for w in (
    "Word", "Excel", "PowerPoint", "Outlook", "Office", "Macintosh", "Mac", "Windows", "Writer", "Calc",
    "Impress", "LibreOffice", "OpenOffice", "Acrobat", "Distiller", "Adobe", "Reader", "Quartz", "Normal",
    "Pages", "Numbers", "Keynote", "Skia", "Chromium", "Chrome", "Ghostscript", "Library",
))

# a word before a full stop that does not end a sentence
_ABBREVIATIONS = frozenset((
    "dr", "prof", "hr", "fr", "nr", "str", "bzw", "ca", "vgl", "inkl", "ggf", "usw", "etc", "mr", "mrs",
    "ms", "st", "co", "abs", "tel", "fax", "z", "u", "d", "b", "a", "e", "v",
))

_COMPANY_FORMS = (
    r"GmbH[ \t]*&[ \t]*Co\.?[ \t]*KGaA", r"GmbH[ \t]*&[ \t]*Co\.?[ \t]*KG", r"GMBH[ \t]*&[ \t]*CO\.?[ \t]*KG",
    r"gGmbH", r"GmbH", r"GMBH", r"mbH", r"MBH", r"KGaA", r"KGAA", r"AG", r"KG", r"SE", r"PartG[ \t]+mbB", r"PartG",
    r"mbB", r"Ltd\.?", r"LTD\.?", r"Limited", r"LIMITED", r"Inc\.?", r"INC\.?", r"LLC", r"LLP", r"Corp\.?",
    r"e\.[ \t]?V\.", r"e\.[ \t]?K\.", r"e\.[ \t]?Kfm\.", r"e\.[ \t]?Kfr\.", r"eG", r"OHG", r"GbR", r"GBR",
    r"S\.A\.S\.", r"S\.A\.", r"SAS", r"SARL", r"S\.à[ \t]?r\.l\.", r"S\.p\.A\.", r"S\.r\.l\.", r"s\.r\.o\.",
    r"a\.s\.", r"Sp\.[ \t]?z[ \t]?o\.o\.", r"B\.V\.", r"N\.V\.", r"A/S", r"ApS", r"Oy", r"UG", r"plc", r"PLC",
    r"Kanzlei",
)
_FORM_ALT = "|".join(_COMPANY_FORMS)
_NAME_WORD = r"[A-ZÄÖÜ0-9][\wÄÖÜäöüß'’-]*"
_JOIN = r"(?:[ \t]*&[ \t]*|[ \t]+(?:und|and|\+)[ \t]+|[ \t]+)"
_COMPANY_RE = re.compile(
    r"(?<![\w&.-])(?P<words>%s(?:%s%s){0,5})[ \t]*,?[ \t]+(?P<form>%s)(?![\w])"
    % (_NAME_WORD, _JOIN, _NAME_WORD, _FORM_ALT)
)
# a brand written in lower case before a legal form (a lower-case word before "GmbH" is usually an article or
# an adjective, which the checks in code leave out)
_LOWER_COMPANY_RE = re.compile(
    r"(?<![\w&.-])(?P<word>[^\W\d_A-ZÄÖÜ][\w'’-]{1,40})[ \t]+(?P<form>%s)(?![\w])" % _FORM_ALT
)
_ADJECTIVE_ENDINGS = ("e", "en", "er", "es", "em", "ern", "ens")
# short lower-case words that stand before a legal form in ordinary prose ("die Umwandlung als AG")
_LOWER_SKIP = frozenset((
    "als", "ist", "sind", "wird", "war", "wurde", "per", "pro", "via", "bzw", "ca", "sowie", "auch", "noch",
    "nur", "schon", "bereits", "dass", "wie", "neu", "alt", "is", "are", "was", "as", "via", "plus", "into",
    "than", "then", "new", "old", "our", "your", "its", "this", "that",
))
_KANZLEI_RE = re.compile(r"(?<![\w-])Kanzlei[ \t]+(?P<words>%s(?:%s%s){0,4})" % (_NAME_WORD, _JOIN, _NAME_WORD))

_TOKEN_RE = re.compile(r"[^\W\d_]+(?:['’-][^\W\d_]+)*")
_WORD_RE = re.compile(r"[^\s&+]+")
_MIN_CAPS_LETTERS = 4
"""An ALL CAPS word counts as a name word from this many letters on; shorter ones are acronyms."""

_TITLES = frozenset(w.casefold() for w in ("Herr", "Herrn", "Frau", "Mr", "Mrs", "Ms", "Dr", "Prof", "Dipl"))
_MAX_TITLE_WORDS = 3
_INITIAL_RE = re.compile(r"(?<![\w.])(?P<initial>[A-ZÄÖÜ])\.[ \t]*(?=[^\W\d_])")
_SPACED_RE = re.compile(r"(?<![^\W\d_])(?:[^\W\d_][ \t]{1,12}){3,}[^\W\d_](?![^\W\d_])")
_CAMEL_SPLIT_RE = re.compile(r"(?<=[a-zäöüß])(?=[A-ZÄÖÜ][a-zäöüß])")

_LABEL_RE = re.compile(
    r"(?<![\w-])(?:Kunde|Kundin|Customer|Client|Auftraggeber|Auftraggeberin|Firma|Mandant|Mandantin)"
    r"[ \t]*:[ \t]*(?P<value>[^\n,;|]{1,80})",
    re.IGNORECASE,
)
_LABEL_WORDS = 6

_MAIL_RE = re.compile(r"(?<![\w.%+-])(?P<local>[A-Za-z0-9._%+-]+)@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,24}")
_PERSON_LOCAL_RE = re.compile(
    r"[^\W\d_]{2,}[._-][^\W\d_]{2,}|[^\W\d_][._-][^\W\d_]{2,}|[^\W\d_]{2,}[._-][^\W\d_][._-][^\W\d_]{2,}"
)
# "First von Last": the particles, a second particle that may follow (von der, de la) and the suffixes that make
# the first word a German noun ("Migration von Servern", "Anforderungen zur Umsetzung"), singular and plural
_PARTICLES = frozenset(("von", "van", "de", "zu", "vom", "zur"))
_SECOND_PARTICLES = frozenset(("der", "den", "dem", "la", "le", "du"))
_NOUN_SUFFIXES = (
    "ung", "heit", "keit", "schaft", "tion", "ment", "tät", "ismus", "nis", "tur", "ik", "ei", "er",
    "ungen", "heiten", "keiten", "schaften", "tionen", "mente", "ments", "täten", "ismen", "nisse", "turen", "iken",
    "eien",
)

# "Last, First": letters per word, the comma between them and what joins the items of a list
_PAIR_MIN_LETTERS = 3
_PAIR_MAX_LETTERS = 20
_COMMA_RE = re.compile(r"[ \t]*,[ \t]*")
_LIST_JOIN_RE = re.compile(
    r"[ \t]*,[ \t]*(?:(?:und|and|oder|or|sowie|&)[ \t]+)?|[ \t]+(?:und|and|oder|or|sowie|&)[ \t]+"
)
_LIST_MIN_ITEMS = 3

_ROLE_WORDS = frozenset((
    "info", "sales", "support", "service", "office", "kontakt", "contact", "admin", "noreply", "no", "reply",
    "mail", "post", "team", "hello", "billing", "invoice", "rechnung", "buchhaltung", "vertrieb", "einkauf",
    "marketing", "presse", "press", "jobs", "karriere", "hr", "it", "helpdesk", "security", "abuse",
    "postmaster", "webmaster", "hostmaster", "newsletter", "donotreply", "bounce", "notifications", "alerts",
    "system", "root", "test", "dev", "ops", "devops", "cloud", "do", "not", "zentrale", "empfang", "anfrage",
    "bewerbung", "datenschutz", "privacy", "legal", "compliance", "finance", "accounting", "order", "orders",
))


class _Mask:
    """Which characters of a text are already covered, for fast overlap tests."""

    def __init__(self, size: int, spans):
        self.cover = bytearray(size)
        for s in spans:
            a, b = max(0, s.start), min(size, s.end)
            if b > a:
                self.cover[a:b] = b"\x01" * (b - a)

    def hit(self, a: int, b: int) -> bool:
        return any(self.cover[a:b])


def _sentence_start(text: str, pos: int) -> bool:
    """True when the word at `pos` follows the end of a sentence. A line start is not a sentence start:
    names stand on their own lines in signatures, address blocks and tables."""
    i = pos - 1
    while i >= 0 and text[i] in " \t":
        i -= 1
    if i < 0 or text[i] not in ".!?":
        return False
    if text[i] in "!?":
        return True
    j = i
    while j > 0 and text[j - 1].isalpha():
        j -= 1
    word = text[j:i].casefold()
    return len(word) > 1 and word not in _ABBREVIATIONS


def _opening(text: str, pos: int) -> bool:
    """True when the word at `pos` opens the text, a line or a sentence: where a sentence-opening word of
    rules/sentence-openers.txt may stand."""
    i = pos - 1
    while i >= 0 and text[i] in " \t":
        i -= 1
    return i < 0 or text[i] == "\n" or _sentence_start(text, pos)


def _stop_spans(text: str) -> list[Span]:
    return [Span(m.start(), m.end(), "stop") for rx in _STOP_PHRASES for m in rx.finditer(text)]


def _ordinary(word: str) -> bool:
    """A function word, a stop word or the name of an authoring tool."""
    low = word.casefold()
    return low in _FUNCTION_WORDS or low in _STOP_SINGLES or low in _TOOL_WORDS


def _name_word(word: str) -> bool:
    """A word shaped like part of a name: capitalised (any alphabet, accents, O'Name, Name-Name) or in
    capitals with at least _MIN_CAPS_LETTERS letters."""
    letters = sum(1 for c in word if c.isalpha())
    if letters < 2:
        return False
    if word.isupper():
        return letters >= _MIN_CAPS_LETTERS
    for part in re.split(r"['’-]", word):
        if not part or not part[0].isupper() or not (len(part) == 1 or part[1:].islower()):
            return False
    return True


def _trim(words: list[tuple[int, int, str]], sentence_rule: bool, text: str) -> list[tuple[int, int, str]]:
    """Drop function words and stop words at both ends. With `sentence_rule` also drop the sentence-opening
    words (rules/sentence-openers.txt) that stand first in a run that opens the text, a line or a sentence.
    Any other capitalised word at a sentence start stays: his decision of 2026-09-22 treats it as a name."""
    opening = sentence_rule and bool(words) and _opening(text, words[0][0])
    while words and (words[0][2].casefold() in _FUNCTION_WORDS
                     or words[0][2].casefold() in _STOP_SINGLES
                     or (opening and words[0][2].casefold() in _OPENERS)):
        words = words[1:]
    while words and (words[-1][2].casefold() in _FUNCTION_WORDS or words[-1][2].casefold() in _STOP_SINGLES):
        words = words[:-1]
    return words


def _words(text: str, start: int, end: int) -> list[tuple[int, int, str]]:
    return [(start + m.start(), start + m.end(), m.group(0)) for m in _WORD_RE.finditer(text[start:end])]


def _company_candidates(text: str, blocked: _Mask) -> list[tuple[int, int]]:
    out: list[tuple[int, int]] = []
    for m in _COMPANY_RE.finditer(text):
        if blocked.hit(m.start("form"), m.end("form")):
            continue
        words = _words(text, m.start("words"), m.end("words"))
        # keep only the words after the last one that is already known
        last_known = max((i for i, w in enumerate(words) if blocked.hit(w[0], w[1])), default=-1)
        words = _trim(words[last_known + 1:], False, text)
        if words:
            out.append((words[0][0], m.end("form")))
    for m in _LOWER_COMPANY_RE.finditer(text):
        word = m.group("word")
        low = word.casefold()
        if (blocked.hit(m.start(), m.end()) or _ordinary(word) or low.endswith(_ADJECTIVE_ENDINGS)
                or low in _LOWER_SKIP or sum(1 for c in word if c.isalpha()) < 3):
            continue
        out.append((m.start("word"), m.end("form")))
    for m in _KANZLEI_RE.finditer(text):
        words = _trim(_words(text, m.start("words"), m.end("words")), False, text)
        if words and not blocked.hit(m.start(), words[-1][1]):
            out.append((m.start(), words[-1][1]))
    return out


def _runs(text: str) -> list[list[tuple[int, int, str]]]:
    """Runs of name words that stand next to each other with only spaces or tabs between them."""
    runs: list[list[tuple[int, int, str]]] = []
    current: list[tuple[int, int, str]] = []
    for m in _TOKEN_RE.finditer(text):
        a, b, w = m.start(), m.end(), m.group(0)
        glued = (a > 0 and (text[a - 1].isalnum() or text[a - 1] in "-_")) or (b < len(text) and text[b] in "-_")
        if not _name_word(w) or glued:
            if current:
                runs.append(current)
            current = []
            continue
        if current and not (text[current[-1][1]:a] and text[current[-1][1]:a].strip(" \t") == ""):
            runs.append(current)
            current = []
        current.append((a, b, w))
    if current:
        runs.append(current)
    return runs


def _run_candidates(text: str, blocked: _Mask) -> list[tuple[int, int]]:
    """Two or more name words in a row, anywhere, also at the start of a sentence."""
    return _run_spans(text, blocked, sentence_only=False)


def _sentence_pair_candidates(text: str, blocked: _Mask) -> list[tuple[int, int]]:
    """Two or more name words in a row that start a sentence (after a full stop, ! or ?). The strong rules use
    it; a capitalised pair at the start of a short text or in the middle of a sentence is left to the full
    rules, so that a goal such as "Landing Zone for the first workload" still passes."""
    return _run_spans(text, blocked, sentence_only=True)


def _standalone(text: str, a: int, b: int) -> bool:
    """The word text[a:b] is not glued into an identifier (the same test as in _runs)."""
    return not ((a > 0 and (text[a - 1].isalnum() or text[a - 1] in "-_")) or (b < len(text) and text[b] in "-_"))


def _gap_is_spaces(text: str, a: int, b: int) -> bool:
    gap = text[a:b]
    return bool(gap) and gap.strip(" \t") == ""


def _particle_candidates(text: str, blocked: _Mask) -> list[tuple[int, int]]:
    """First von|van|de|zu|vom|zur Last, also with a second particle (von der, de la). Not when the first word
    is a German noun by its suffix ("Migration von Servern"), a sentence opener, a function word, a stop word or
    the name of a tool. Not when the last word is one of the last three either."""
    out: list[tuple[int, int]] = []
    tokens = [(m.start(), m.end(), m.group(0)) for m in _TOKEN_RE.finditer(text)]
    for i in range(1, len(tokens) - 1):
        if tokens[i][2] not in _PARTICLES:
            continue
        first = tokens[i - 1]
        j = i + 1
        if (tokens[j][2] in _SECOND_PARTICLES and j + 1 < len(tokens)
                and _gap_is_spaces(text, tokens[i][1], tokens[j][0])):
            j += 1
        last = tokens[j]
        if not all(_gap_is_spaces(text, tokens[k][1], tokens[k + 1][0]) for k in range(i - 1, j)):
            continue
        low = first[2].casefold()
        if (not _name_word(first[2]) or not _name_word(last[2]) or _ordinary(first[2]) or _ordinary(last[2])
                or low in _OPENERS or low.endswith(_NOUN_SUFFIXES)):
            continue
        if not _standalone(text, first[0], first[1]) or not _standalone(text, last[0], last[1]):
            continue
        if not blocked.hit(first[0], last[1]):
            out.append((first[0], last[1]))
    return out


def _pair_word(word: str) -> bool:
    """One half of "Last, First": a name word of 3 to 20 letters that is no function word and no opener."""
    letters = sum(1 for c in word if c.isalpha())
    low = word.casefold()
    return (_name_word(word) and _PAIR_MIN_LETTERS <= letters <= _PAIR_MAX_LETTERS
            and low not in _FUNCTION_WORDS and low not in _OPENERS)


def _stopish(word: str) -> bool:
    low = word.casefold()
    return low in _STOP_SINGLES or low in _TOOL_WORDS


def _inverted_candidates(text: str, blocked: _Mask) -> list[tuple[int, int]]:
    """Last, First: two capitalised words around a comma, each of 3 to 20 letters, not both stop words (or tool
    names). Not inside a list of three or more capitalised items joined by commas or by the words and, und, or,
    oder ("Linux, Windows, Terraform")."""
    runs = _runs(text)
    size = [1] * len(runs)   # how many items the list has that each run belongs to
    k = 0
    while k < len(runs):
        m = k
        while m + 1 < len(runs) and _LIST_JOIN_RE.fullmatch(text, runs[m][-1][1], runs[m + 1][0][0]):
            m += 1
        for x in range(k, m + 1):
            size[x] = m - k + 1
        k = m + 1
    out: list[tuple[int, int]] = []
    for k in range(len(runs) - 1):
        (a1, b1, w1), (a2, b2, w2) = runs[k][-1], runs[k + 1][0]
        if size[k] >= _LIST_MIN_ITEMS or not _COMMA_RE.fullmatch(text, b1, a2):
            continue
        if not (_pair_word(w1) and _pair_word(w2)) or (_stopish(w1) and _stopish(w2)):
            continue
        if not blocked.hit(a1, b1) and not blocked.hit(a2, b2):
            out.append((a1, b2))
    return out


def _run_spans(text: str, blocked: _Mask, sentence_only: bool) -> list[tuple[int, int]]:
    out: list[tuple[int, int]] = []
    for run in _runs(text):
        if len(run) < 2 or (sentence_only and not _sentence_start(text, run[0][0])):
            continue
        segment: list[tuple[int, int, str]] = []
        segments: list[list[tuple[int, int, str]]] = []
        for w in run:
            low = w[2].casefold()
            if blocked.hit(w[0], w[1]) or low in _STOP_SINGLES or low in _TOOL_WORDS:
                segments.append(segment)
                segment = []
                continue
            segment.append(w)
        segments.append(segment)
        for seg in segments:
            seg = _trim(seg, True, text)
            if len(seg) >= 2:
                out.append((seg[0][0], seg[-1][1]))
    return out


def _title_candidates(text: str, blocked: _Mask) -> list[tuple[int, int]]:
    """Up to _MAX_TITLE_WORDS name words after a salutation or title (Herr, Frau, Mr, Dr ...)."""
    out: list[tuple[int, int]] = []
    tokens = [(m.start(), m.end(), m.group(0)) for m in _TOKEN_RE.finditer(text)]
    for i, (a, b, w) in enumerate(tokens):
        if w.casefold() not in _TITLES:
            continue
        j = i + 1
        names: list[tuple[int, int, str]] = []
        prev_end = b
        while j < len(tokens) and len(names) < _MAX_TITLE_WORDS:
            ta, tb, tw = tokens[j]
            gap = text[prev_end:ta]
            if not gap or gap.strip(" \t.") or "\n" in gap:
                break
            if tw.casefold() in _TITLES and not names:
                prev_end, j = tb, j + 1
                continue
            if not _name_word(tw) or _ordinary(tw) or blocked.hit(ta, tb):
                break
            names.append(tokens[j])
            prev_end, j = tb, j + 1
        if names:
            out.append((names[0][0], names[-1][1]))
    return out


def _initial_candidates(text: str, blocked: _Mask) -> list[tuple[int, int]]:
    """An initial with a full stop before a name word (X. Name), not the second letter of z. B. or u. a."""
    out: list[tuple[int, int]] = []
    for m in _INITIAL_RE.finditer(text):
        before = text[max(0, m.start() - 3):m.start()]
        if re.search(r"(?:^|[\s(])[^\W\d_]\.\s?$", before):
            continue
        t = _TOKEN_RE.match(text, m.end())
        if not t or not _name_word(t.group(0)) or _ordinary(t.group(0)) or t.group(0).isupper():
            continue
        if not blocked.hit(m.start(), t.end()):
            out.append((m.start(), t.end()))
    return out


def _spaced_words(stretch: str) -> list[str]:
    """The words of a letter-spaced stretch. With single spaces between letters a wider gap is a word gap;
    with wider, uneven gaps (pdftotext -layout) the letters are joined and split before a capital that
    follows a small letter."""
    gaps = [len(g) for g in re.findall(r"[ \t]+", stretch)]
    if gaps and min(gaps) == 1:
        chunks = re.split(r"[ \t]{2,}", stretch)
    else:
        chunks = [stretch]
    words: list[str] = []
    for chunk in chunks:
        words.extend(w for w in _CAMEL_SPLIT_RE.split(re.sub(r"[ \t]", "", chunk)) if w)
    return words


def _spaced_candidates(text: str, blocked: _Mask) -> list[tuple[int, int]]:
    """Letters spread with spaces (a letter-spaced heading). The letters are joined into words and the other
    rules run over them: the whole spread-out stretch is a candidate when one of them fires."""
    out: list[tuple[int, int]] = []
    for m in _SPACED_RE.finditer(text):
        if blocked.hit(m.start(), m.end()):
            continue
        if _collect(" ".join(_spaced_words(m.group(0))), [], _JOINED_RULES):
            out.append((m.start(), m.end()))
    return out


def _label_candidates(text: str, blocked: _Mask) -> list[tuple[int, int]]:
    out: list[tuple[int, int]] = []
    for m in _LABEL_RE.finditer(text):
        words = _words(text, m.start("value"), m.end("value"))[:_LABEL_WORDS]
        words = [w for w in words if w[2].strip(".:-")]
        if not words or codes.is_code(words[0][2].strip(".:")) or codes.is_project_code(words[0][2]):
            continue
        start, end = words[0][0], words[-1][1]
        while end > start and text[end - 1] in ".:-":
            end -= 1
        if end > start and not blocked.hit(start, end):
            out.append((start, end))
    return out


def _mail_candidates(text: str, names: _Mask) -> list[tuple[int, int]]:
    """Person-like local parts of mail addresses. A local part that holds a registered name is known."""
    out: list[tuple[int, int]] = []
    for m in _MAIL_RE.finditer(text):
        local = m.group("local")
        if not _PERSON_LOCAL_RE.fullmatch(local):
            continue
        parts = [p.casefold() for p in re.split(r"[._-]", local) if p]
        if any(p in _ROLE_WORDS for p in parts):
            continue
        a, b = m.span("local")
        if not names.hit(a, b):
            out.append((a, b))
    return out


def _collect(text: str, known: list[Span], rules) -> list[str]:
    if not text:
        return []
    known = list(known)
    names = _Mask(len(text), [s for s in known if s.cls == "name"])
    blocked = _Mask(len(text), known + _stop_spans(text))
    found: list[tuple[int, int, int]] = []   # (priority, start, end)
    for prio, rule in enumerate(rules):
        spans = rule(text, names if rule is _mail_candidates else blocked)
        found.extend((prio, a, b) for a, b in spans)
    kept: list[tuple[int, int]] = []
    for _, a, b in sorted(found):
        if all(b <= ka or a >= kb for ka, kb in kept):
            kept.append((a, b))
    out: list[str] = []
    seen: set[str] = set()
    for a, b in sorted(kept):
        value = re.sub(r"\s+", " ", text[a:b]).strip()
        key = value.casefold()
        if value and key not in seen:
            seen.add(key)
            out.append(value)
    return out


_JOINED_RULES = (_company_candidates, _particle_candidates, _run_candidates, _title_candidates,
                 _initial_candidates)
_ALL_RULES = (_company_candidates, _label_candidates, _mail_candidates, _particle_candidates, _run_candidates,
              _inverted_candidates, _title_candidates, _initial_candidates, _spaced_candidates)
STRONG_RULES = (_company_candidates, _label_candidates, _mail_candidates, _particle_candidates,
                _sentence_pair_candidates, _inverted_candidates, _title_candidates, _spaced_candidates)
"""The rules that rarely fire on ordinary words: a short text such as a project goal is checked with these."""


def unknown_candidates(text: str, known: list[Span], rules=_ALL_RULES, keep=()) -> list[str]:
    """Strings in `text` that look like names but are not covered by `known` (positions in `text`).

    The rules: capitalised or lower-case words before a legal form (GmbH, mbH, AG, KG, SE, Ltd, Inc, e.V.,
    e.K., eG, OHG, GbR, S.A., S.p.A., s.r.o., B.V., PLC and more, also in capitals) or after "Kanzlei"; the
    words after a label such as "Kunde:" or "Customer:"; the local part of a mail address that looks like a
    person (first.last, f.last); "First von Last" (also van, de, zu, vom, zur) unless the first word is a German
    noun by its suffix; two or more name words in a row (capitalised in any alphabet or in capitals with four
    letters or more) that are not stop words and not function words, also at the start of a sentence, where
    only a sentence-opening word of rules/sentence-openers.txt is dropped; "Last, First" around a comma unless
    both are stop words or the pair is part of a list of three or more capitalised items; up to three name
    words after a salutation or title (Herr, Frau, Mr, Dr); an initial before a name word (X. Name); letters
    spread out with spaces (a spaced heading) when the joined words meet one of the rules above.

    `keep` holds phrases he reviewed as not a name (the keep list of the vault): a candidate equal to one of
    them (case and whitespace aside) is dropped. The result is ordered by position, without duplicates. It goes
    to the private report only.
    """
    found = _collect(text, known, rules)
    if keep:
        kept = {keep_key(k) for k in keep}
        found = [c for c in found if keep_key(c) not in kept]
    return found


# --------------------------------------------------------------------------- the keep list


def keep_key(phrase: str) -> str:
    """The form under which a phrase and a candidate are compared: normalised, whitespace runs as one space,
    case folded."""
    return " ".join(normalize.normalize(phrase).text.split()).casefold()


def _sealed_keep(p: config.Paths) -> Path:
    return p.keep_list.with_name(p.keep_list.name + ".gpg")


def _vault_call(p: config.Paths, op: str, **fields) -> dict:
    """One request to the vault daemon for the keep list. Errors carry no value."""
    from awb import vault

    try:
        return vault.admin_call(op, p.admin_sock, **fields)
    except vault.VaultLocked:
        raise IntakeError("keep list: the vault is locked, run awb vault unlock") from None
    except vault.VaultUnavailable:
        raise IntakeError("keep list: no vault daemon") from None
    except vault.VaultError:
        raise IntakeError("keep list: the vault daemon refused the request") from None


def _read_keep(p: config.Paths) -> str:
    """The text of the keep list: the sealed copy through the vault daemon when there is one, plus a plaintext
    copy when there is one. Missing -> empty."""
    parts: list[str] = []
    sealed = _sealed_keep(p)
    if sealed.exists():
        answer = _vault_call(p, "open_file", path=str(sealed))
        try:
            parts.append(base64.b64decode(answer.get("data") or "", validate=True).decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            raise IntakeError("keep list: the vault daemon answered in an unexpected form") from None
    try:
        parts.append(p.keep_list.read_text(encoding="utf-8"))
    except FileNotFoundError:
        pass
    except UnicodeDecodeError:
        raise IntakeError("keep list: the file is not UTF-8 text") from None
    return "\n".join(parts)


def _parse_keep(text: str) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for line in text.splitlines():
        phrase = " ".join(line.split())
        key = keep_key(phrase)
        if phrase and key not in seen:
            seen.add(key)
            out.append(phrase)
    return out


def load_keep(p: config.Paths) -> list[str]:
    """The phrases of the keep list (`Paths.keep_list`, vault side), in the order they were added. One phrase per
    line. When the vault is encrypted the sealed copy is read through the vault daemon. Missing -> []."""
    return _parse_keep(_read_keep(p))


def _write_private(path: Path, text: str) -> None:
    """Atomic write, file mode 600. The folder is created with mode 700 when it is missing."""
    if not path.parent.is_dir():
        path.parent.mkdir(parents=True, exist_ok=True)
        os.chmod(path.parent, 0o700)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".keep-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    os.chmod(path, 0o600)


def keep_phrase(p: config.Paths, phrase: str) -> tuple[bool, int]:
    """Add one phrase to the keep list. Returns (added, phrases in the list); added is False when the phrase was
    kept before. Any phrase is taken, only an empty one is an error. When the vault is encrypted the list is
    sealed again through the vault daemon and no plaintext copy stays behind. Nothing here echoes the phrase."""
    if not isinstance(phrase, str):
        raise IntakeError("keep list: the phrase must be text")
    phrase = " ".join(normalize.normalize(phrase).text.split())
    if not phrase:
        raise IntakeError("keep list: the phrase is empty")
    phrases = load_keep(p)
    if keep_key(phrase) in {keep_key(k) for k in phrases}:
        return False, len(phrases)
    phrases.append(phrase)
    encrypted = p.register_encrypted.exists() or _sealed_keep(p).exists()
    try:
        before = p.keep_list.read_bytes() if encrypted else None
    except FileNotFoundError:
        before = None
    _write_private(p.keep_list, "".join(k + "\n" for k in phrases))
    if encrypted:
        try:
            _vault_call(p, "seal_file", path=str(p.keep_list))
        except BaseException:
            # no new plaintext stays behind; a plaintext copy that was there before is put back as it was
            if before is None:
                p.keep_list.unlink(missing_ok=True)
            else:
                _write_private(p.keep_list, before.decode("utf-8"))
            raise
    return True, len(phrases)


# --------------------------------------------------------------------------- detection and replacement


def _resolve(names: list[Span], structured: list[Span]) -> list[Span]:
    """One list without overlaps. A structured hit that holds name hits wins over them (a mail address at a
    registered domain becomes one MAIL token) and a structured hit inside a name hit is dropped. Spans that
    still overlap (a partial overlap or a chain of them) are merged into one span over all of them, with the
    structured class when one of them is structured."""
    kept: dict[int, Span] = {id(s): s for s in names}
    extra: list[Span] = []
    for s in structured:
        over = [a for a in kept.values() if a.start < s.end and s.start < a.end]
        if over and any(a.start <= s.start and s.end <= a.end for a in over):
            continue
        for a in over:
            kept.pop(id(a), None)
        start = min([s.start] + [a.start for a in over])
        end = max([s.end] + [a.end for a in over])
        extra.append(Span(start, end, s.cls) if (start, end) != (s.start, s.end) else s)
    merged: list[Span] = []
    for s in sorted(list(kept.values()) + extra, key=lambda s: (s.start, s.end)):
        if merged and s.start < merged[-1].end:
            last = merged[-1]
            if last.cls == "name" and s.cls == "name":
                cls, code = "name", last.code
            else:
                cls, code = (last.cls if last.cls != "name" else s.cls), None
            merged[-1] = Span(last.start, max(last.end, s.end), cls, code)
        else:
            merged.append(s)
    return merged


def _canon(cls: str, value: str) -> str:
    """The key under which one structured value gets one token."""
    v = re.sub(r"\s+", "", value).casefold()
    if cls == "phone":
        v = ("+" if v.startswith("+") else "") + re.sub(r"\D", "", v)
    return v


class _Engine:
    """Detection, tokens and replacement for one run. Holds the only map from values to tokens."""

    def __init__(self, entries: list[register.Entry]):
        self.matcher = Matcher(register.forms_for_matching(entries))
        self.forms = len({(e.code, e.form) for e in entries if e.status == "active"})
        # short active forms: a fresh code must not carry one, not even inside the random part
        self._short = sorted({re.sub(r"[\W_]", "", e.form).casefold() for e in entries if e.status == "active"}
                             - {""}, key=len)
        self._short = [f for f in self._short if 3 <= len(f) <= 6]
        self.used: set[str] = set(register.codes(entries))
        self.tokens: dict[tuple[str, str], str] = {}
        self.hits: dict[tuple[str, str], _report.Hit] = {}
        self.keep: tuple[str, ...] = ()   # phrases of the keep list, dropped from the candidates
        # the second check (T-13): every registered variant as a skeleton, compared over whole words
        self._skeletons = {s for s in (skeleton(v) for v, _ in register.forms_for_matching(entries))
                           if len(s) >= _SKELETON_MIN}
        self._skeleton_max = max((len(s) for s in self._skeletons), default=0)

    # tokens

    def safe(self, code: str) -> bool:
        """A code that no check would ever report: no register form in it and no structured shape."""
        low = code.casefold()
        if any(f in low for f in self._short):
            return False
        return not self.matcher.find(code) and not patterns.find_structured(code)

    def fresh(self, kind: str) -> str:
        for _ in range(1000):
            code = codes.new_code(kind, self.used)
            if self.safe(code):
                self.used.add(code)
                return code
        raise IntakeError("could not find a free %s code" % kind)

    def fresh_file_id(self, taken: set[str]) -> str:
        for _ in range(1000):
            fid = "F-" + "".join(secrets.choice(codes.ALPHABET) for _ in range(4))
            if fid not in taken and self.safe(fid):
                taken.add(fid)
                return fid
        raise IntakeError("could not find a free file id")

    def token_for(self, span: Span, value: str) -> str:
        if span.cls == "name" and span.code:
            return span.code
        key = (span.cls, _canon(span.cls, value))
        token = self.tokens.get(key)
        if token is None:
            token = self.fresh(span.cls.upper())
            self.tokens[key] = token
        return token

    def record(self, where: str, span: Span, value: str, token: str) -> None:
        key = (token, value.casefold())
        hit = self.hits.get(key)
        if hit is None:
            hit = self.hits[key] = _report.Hit(token=token, cls=span.cls, value=value)
        hit.count += 1
        hit.where.setdefault(where, []).append(span.start)

    # spans

    def raw_spans(self, text: str) -> tuple[list[Span], list[Span]]:
        return self.matcher.find(text), patterns.find_structured(text)

    def detect(self, where: str, text: str, names_only: bool = False) -> list[str]:
        """Record every hit of `text` with its token and return the candidates of `text`. Positions are
        recorded in `text` as given. With `names_only` only registered forms are looked for and no candidate
        is returned (for raw parts that no reader renders)."""
        if not text:
            return []
        n = normalize.normalize(text)
        names = self.matcher.find(n.text)
        structured = [] if names_only else patterns.find_structured(n.text)
        for s in _resolve(names, structured):
            value = n.text[s.start:s.end]
            first = normalize.original_span(n, s.start, s.end)[0]
            self.record(where, Span(first, first + len(value), s.cls, s.code), value, self.token_for(s, value))
        if names_only:
            return []
        return unknown_candidates(n.text, names + structured, keep=self.keep)

    def sanitize(self, text: str, where: str) -> tuple[str, Counter]:
        """`text` with every hit replaced by its token, normalised. Counts per class of what was replaced."""
        counts: Counter = Counter()
        if not text:
            return text, counts
        # an encoded block that decodes to text is replaced by that text first, so nothing encoded slips past;
        # bidi overrides and tag characters go, whether or not a hit follows: a viewer would render a reversed
        # or hidden text that no check saw
        text, _ = expand_base64_blocks(text)
        text = normalize.strip_invisible(text)
        for _ in range(MAX_PASSES):
            n = normalize.normalize(text)
            spans = _resolve(*self.raw_spans(n.text))
            if not spans:
                return text, counts

            def token(s: Span, _t=n.text) -> str:
                value = _t[s.start:s.end]
                tok = self.token_for(s, value)
                self.record(where, s, value, tok)
                counts[s.cls] += 1
                return tok

            text = self.matcher.replace(n.text, spans, token)
        return text, counts

    def final_hits(self, text: str) -> list[Span]:
        """Every hit the name check would report in `text`, including inside decoded base64 and hex blocks
        and the printable strings of base64 blocks that decode to binary."""
        n = normalize.normalize(text)
        names, structured = self.raw_spans(n.text)
        hits = names + structured
        for block in encoded_texts(text):
            nb = normalize.normalize(block)
            a, b = self.raw_spans(nb.text)
            hits.extend(a + b)
        return hits

    def second_hits(self, text: str) -> list[Span]:
        """The second check (T-13), by another method than the matcher: the words of the normalised text are
        folded to their letters and joined one after the other; a run of whole words that spells a registered
        variant is a hit, whatever stood between the words. A phrase of the keep list is left out. Also run over
        decoded base64 and hex blocks."""
        if not self._skeletons:
            return []
        keep_res = [re.compile(r"\s+".join(re.escape(w) for w in keep_key(k).split()), re.IGNORECASE)
                    for k in self.keep if keep_key(k)]
        hits: list[Span] = []
        for piece in [text] + list(encoded_texts(text)):
            n = normalize.normalize(piece).text
            kept = [(m.start(), m.end()) for r in keep_res for m in r.finditer(n)]
            words = [(m.start(), m.end(), _fold(m.group(0))) for m in _SKELETON_WORD_RE.finditer(n)]
            for i in range(len(words)):
                joined = ""
                for j in range(i, len(words)):
                    joined += words[j][2]
                    if len(joined) > self._skeleton_max:
                        break
                    if joined not in self._skeletons:
                        continue
                    start, end = words[i][0], words[j][1]
                    single = i == j
                    if single and len(joined) < _SKELETON_CASE_FREE and n[start:end].islower():
                        continue
                    if any(a <= start and end <= b for a, b in kept):
                        continue
                    hits.append(Span(start, end, SECOND_CLASS))
                    break
        return hits

    def leaks(self, text: str) -> list[Span]:
        """What neither the matcher nor the second check may find in a written output."""
        return self.final_hits(text) + self.second_hits(text)


# --------------------------------------------------------------------------- the run


def safe_new_code(entries: list[register.Entry], kind: str, taken: set[str] | frozenset[str] = frozenset()) -> str:
    """A fresh top-level code (register.next_code) that carries no register form and no structured shape."""
    engine = _Engine(entries)
    engine.used |= set(taken)
    for _ in range(1000):
        code = register.next_code(list(entries), kind)
        if code not in engine.used and engine.safe(code):
            return code
    raise IntakeError("could not find a free %s code" % kind)


def issued_codes(p: config.Paths) -> set[str]:
    """Customer codes that earlier intakes used: the folders on both sides."""
    out: set[str] = set()
    for base in (p.private_reports, p.originals, p.outbox):
        try:
            out.update(d.name for d in base.iterdir() if d.is_dir() and codes.is_code(d.name))
        except OSError:
            pass
    return out


def _resolve_customer(customer: str, entries: list[register.Entry], p: config.Paths) -> tuple[str, bool]:
    if customer == NEW:
        return safe_new_code(entries, "CUST", issued_codes(p)), True
    if (not isinstance(customer, str) or not codes.is_code(customer) or customer.count("-") != 1
            or codes.kind_of(customer) != "CUST"):
        raise IntakeError("customer must be a top-level CUST code or new")
    states = {e.status for e in entries if e.code == customer}
    if states and "active" not in states:
        raise IntakeError("customer %s is retired in the register" % customer)
    if not states and customer not in issued_codes(p):
        raise IntakeError("customer %s is not in the register and no earlier intake issued it" % customer)
    return customer, False


def _taken_ids(p: config.Paths, customer: str) -> set[str]:
    out: set[str] = set()
    for base in (p.originals / customer, p.outbox / customer):
        try:
            for f in base.iterdir():
                m = FILE_ID_RE.match(f.name)
                if m:
                    out.add(m.group(0))
        except OSError:
            pass
    return out


def _meta_text(meta: dict) -> str:
    lines = []
    for key, value in meta.items():
        if isinstance(value, (list, tuple)):
            value = " ".join(str(v) for v in value)
        if isinstance(value, dict):
            value = " ".join("%s %s" % kv for kv in value.items())
        if isinstance(value, str) and value.strip():
            lines.append("%s: %s" % (key, value))
    return "\n".join(lines)


def _label_of(ex) -> str:
    return str(ex.meta.get("member") or ex.meta.get("attachment") or "")


def _walk(ex, part_id: str):
    """(part id, extraction) for an extraction and all its children, parent first."""
    yield part_id, ex
    for i, child in enumerate(ex.children or [], start=1):
        yield from _walk(child, "%s.%d" % (part_id, i))


def _suffix(name: str, engine: _Engine) -> str:
    """The original suffix when it is a plain short suffix that carries nothing, else none."""
    suffix = Path(name).suffix
    if re.fullmatch(r"\.[A-Za-z0-9]{1,8}", suffix) and not engine.final_hits(suffix[1:]):
        return suffix.lower()
    return ""


def _output_text(record: _report.FileRecord, bodies: dict[str, str]) -> str:
    lines: list[str] = []
    for i, part in enumerate(record.parts):
        if i == 0:
            lines += ["# %s" % part.part_id, ""]
        else:
            lines += ["", "## %s" % part.part_id, ""]
        lines.append("- file id: %s" % part.part_id)
        lines.append("- kind: %s" % part.kind)
        lines.append("- state: %s" % part.state)
        lines.append("- notes: %s" % ("; ".join(part.public_notes) or "none"))
        body = bodies.get(part.part_id, "")
        if body.strip():
            lines += ["", body.strip("\n")]
    return "\n".join(lines) + "\n"


def vault_encrypted(p: config.Paths) -> bool:
    """True when the vault is encrypted: the register exists as register.tsv.gpg only."""
    try:
        return p.register_encrypted.is_file()
    except OSError:
        return False


def seal(p: config.Paths, path: Path) -> str | None:
    """Seal a plaintext file of the vault through the vault daemon (`seal_file`): it writes `<path>.gpg` and
    removes the plaintext. None when that worked, else a short reason without a value; the plaintext then stays
    where it is, inside the vault."""
    from awb import vault

    try:
        vault.admin_call("seal_file", p.admin_sock, path=str(path))
    except vault.VaultLocked:
        return "vault locked"
    except vault.VaultUnavailable:
        return "no vault daemon"
    except vault.VaultError:
        return "refused by the vault daemon"
    return None


def _sealed(path: Path) -> Path:
    return path.with_name(path.name + ".gpg")


def _free_private_path(path: Path) -> Path:
    """`path` or path-2, path-3 ... when the name or its sealed copy is taken: sealing must never replace an
    older sealed report."""
    candidate, n = path, 1
    while candidate.exists() or _sealed(candidate).exists():
        n += 1
        candidate = path.with_name("%s-%d%s" % (path.stem, n, path.suffix))
    return candidate


def _write_private_report(p: config.Paths, path: Path, rec, encrypted: bool) -> tuple[Path, int]:
    """Write the private report and seal it when the vault is encrypted. Returns the path of the report as it
    lies in the vault and 1 when it stayed in plaintext, else 0."""
    written = _report.write_private(_free_private_path(path) if encrypted else path, rec)
    if not encrypted:
        return written, 0
    if seal(p, written) is not None:
        return written, 1
    return _sealed(written), 0


def _move_original(src: Path, dst: Path) -> None:
    dst.parent.mkdir(parents=True, exist_ok=True)
    os.chmod(dst.parent, 0o700)
    if dst.exists():
        raise FileExistsError(dst.name)
    shutil.move(str(src), str(dst))
    os.chmod(dst, 0o600)


def selftest() -> list[str]:
    """Every case of awb.planted, run through an engine of its own with an invented register: the matcher must
    report the case's class and the second check must find the cases marked for it. After the sanitising
    neither may find anything. Returns the failures, empty when all pass (R-004, T-94)."""
    from awb import planted

    failures: list[str] = []
    engine = _Engine(planted.entries())
    for label, text, cls, second in planted.CASES:
        found = {h.cls for h in engine.final_hits(text)}
        if cls not in found:
            failures.append("%s: the matcher does not report %s" % (label, cls))
        if second and not engine.second_hits(text):
            failures.append("%s: the second check does not find it" % label)
        clean, _ = engine.sanitize(text, "selftest")
        if engine.leaks(clean):
            failures.append("%s: something is left after the sanitising" % label)
    if engine.leaks("release 1.4.2 for %s in tcp-q7m4, two app clusters, one per zone" % planted.CODE):
        failures.append("clean text: a check reports a hit")
    return failures


def run(files: list[Path], customer: str, p: config.Paths, *, force: bool = False) -> IntakeResult:
    """Take `files` in for `customer` (a CUST code or "new"). See the module text for the steps. Nothing is
    read before the self-test passed."""
    files = [Path(f) for f in files]
    if not files:
        raise IntakeError("no files given")
    failed = selftest()
    if failed:
        raise IntakeError("the intake failed its self-test and takes nothing in: %s" % "; ".join(failed))
    for i, f in enumerate(files, start=1):
        # a link would move only the link and change the mode of what it points to
        if f.is_symlink():
            raise IntakeError("file %d of %d is a symbolic link, give the file itself" % (i, len(files)))
        if not f.is_file():
            raise IntakeError("file %d of %d is not a regular file" % (i, len(files)))
    if len({f.resolve() for f in files}) != len(files):
        raise IntakeError("the same file is given more than once")

    config.ensure_layout(p)
    from awb import vault

    # one intake at a time and never while `awb vault encrypt` runs: an original written in plaintext after the
    # encryption passed its folder would stay in plaintext
    try:
        with vault.vault_lock(p):
            return _run(files, customer, p, force=force)
    except vault.VaultError as err:
        raise IntakeError(str(err)) from None


def _run(files: list[Path], customer: str, p: config.Paths, *, force: bool = False) -> IntakeResult:
    # an encrypted register is read through the vault daemon; originals and private reports are then sealed
    encrypted = vault_encrypted(p)
    entries = register.load(p.register)
    cust, is_new = _resolve_customer(customer, entries, p)
    engine = _Engine(entries)
    engine.keep = tuple(load_keep(p))
    now = datetime.now()
    rec = _report.Run(date=now.date().isoformat(), time=now.strftime("%H:%M:%S"), customer=cust,
                      new_customer=is_new, force=force, blocked=False, register_forms=engine.forms)

    # steps 2 to 4: file ids, extraction, detection, candidates
    taken = _taken_ids(p, cust)
    extractions: dict[str, list[tuple[str, object]]] = {}
    for src in files:
        fid = engine.fresh_file_id(taken)
        with _extract.temp_root(p.vault / "tmp"):
            ex = _extract.extract(src)
        fr = _report.FileRecord(file_id=fid, original_name=src.name, kind=ex.kind, state=ex.state)
        name_text = re.sub(r"_+", " ", src.name)
        for c in engine.detect("%s file name" % fid, name_text):
            rec.candidates.append((fid, c))
        parts = list(_walk(ex, fid))
        extractions[fid] = parts
        for part_id, pex in parts:
            label = src.name if part_id == fid else _label_of(pex)
            fr.parts.append(_report.Part(part_id=part_id, label=label, kind=pex.kind, state=pex.state,
                                         notes=list(pex.notes), meta=dict(pex.meta)))
            pieces = [pex.detect_text or ""]
            if pex.text and pex.text not in pieces[0]:
                pieces.append(pex.text)
            pieces.append(_meta_text(pex.meta))
            for c in engine.detect("%s detect" % part_id, "\n".join(t for t in pieces if t)):
                rec.candidates.append((part_id, c))
            engine.detect("%s raw parts" % part_id, getattr(pex, "scan_text", "") or "", names_only=True)
        rec.files.append(fr)

    seen: set[str] = set()
    unique: list[tuple[str, str]] = []
    for pid, c in rec.candidates:
        if c.casefold() not in seen:
            seen.add(c.casefold())
            unique.append((pid, c))
    rec.candidates = unique
    rec.candidate_count = len(unique)
    states = {f.file_id: f.state for f in rec.files}

    stamp = now.strftime("%Y-%m-%d-%H%M%S")
    private_path = p.private_reports / cust / ("%s.md" % stamp)
    public_path = p.outbox / cust / _report.PUBLIC_NAME

    # step 5: block
    if unique and not force:
        rec.blocked = True
        rec.hits = list(engine.hits.values())
        written, unsealed = _write_private_report(p, private_path, rec, encrypted)
        return IntakeResult(customer=cust, outputs=[], public_report=public_path, private_report=written,
                            blocked=True, candidates=len(unique), states=states, unsealed=unsealed)

    # steps 6 and 7: sanitise, write, check each output once more
    outdir = config.make_dir(p.outbox / cust, 0o750, shared=True)
    outputs: list[Path] = []
    check_failed: set[str] = set()
    try:
        _write_outputs(rec, extractions, engine, outdir, outputs, check_failed)
    except BaseException:
        # never leave a half run behind: the outputs of this run go, the originals stay in the inbox
        for out in outputs:
            out.unlink(missing_ok=True)
        raise

    # step 8: move the originals of every file that passed the final check; seal them in an encrypted vault
    unsealed = 0
    for src, fr in zip(files, rec.files):
        if fr.file_id in check_failed:
            fr.reasons.append("original left in the inbox because the final check failed")
            continue
        dst = p.originals / cust / ("%s%s" % (fr.file_id, _suffix(src.name, engine)))
        try:
            pictures = _images.hold(p, cust, fr.file_id, src, fr.kind, encrypted=encrypted)
        except (_images.ImageError, OSError) as err:
            pictures = 0
            fr.reasons.append("pictures not held (%s)" % type(err).__name__)
        if pictures:
            fr.parts[0].public_notes.append("%d picture(s) held on the vault side, released only by awb images "
                                            "release after a look" % pictures)
        try:
            _move_original(src, dst)
            fr.original = dst
        except OSError as err:
            fr.reasons.append("original not moved (%s)" % type(err).__name__)
            continue
        if encrypted:
            why = seal(p, dst)
            if why is None:
                fr.original = _sealed(dst)
            else:
                unsealed += 1
                fr.reasons.append("original not sealed (%s), it lies in plaintext in the vault" % why)

    # step 9: reports
    rec.hits = list(engine.hits.values())
    public_text = _public_text(rec, engine)
    public_written = _report.write_public(public_path, public_text)
    private_written, report_unsealed = _write_private_report(p, private_path, rec, encrypted)
    return IntakeResult(customer=cust, outputs=outputs, public_report=public_written,
                        private_report=private_written, blocked=False, candidates=len(unique),
                        states={f.file_id: f.state for f in rec.files}, unsealed=unsealed + report_unsealed)


def _public_text(rec: _report.Run, engine: _Engine) -> str:
    """The public report, checked like an output. Notes that carry a hit are withheld; when the report still
    carries one, only the counts go out."""
    text = _report.render_public(rec)
    if not engine.leaks(text):
        return text
    for fr in rec.files:
        for part in fr.parts:
            part.public_notes = ["notes withheld, see the private report"] if part.notes else []
    text = _report.render_public(rec)
    if not engine.leaks(text):
        return text
    text = _report.render_minimal(rec)
    if engine.leaks(text):
        raise IntakeError("the public report of %s does not pass the final check" % rec.customer)
    return text


def _write_outputs(rec, extractions, engine: _Engine, outdir: Path, outputs: list[Path],
                   check_failed: set[str]) -> None:
    """Steps 6 and 7: sanitise every file, write its output and check it before and after writing."""
    for fr in rec.files:
        bodies: dict[str, str] = {}
        by_id = dict(extractions[fr.file_id])
        for part in fr.parts:
            pex = by_id[part.part_id]
            body, counts = engine.sanitize(pex.text or "", "%s output" % part.part_id)
            bodies[part.part_id] = body
            part.counts.update(counts)
            for note in part.notes:
                clean, _ = engine.sanitize(note, "%s note" % part.part_id)
                part.public_notes.append(clean)
        if not any(b.strip() for b in bodies.values()):
            fr.reasons.append("no text to write, no output")
            continue
        out = outdir / ("%s.md" % fr.file_id)
        text = _output_text(fr, bodies)
        hits = engine.leaks(text)
        if not hits:
            written = _report.write_new(out, text, 0o640, 0o750)
            if written != out:  # the file id was taken meanwhile: never write under another name
                written.unlink(missing_ok=True)
                raise IntakeError("output %s already exists" % fr.file_id)
            outputs.append(out)   # from here on a failure of the run removes it again
            hits = engine.leaks(out.read_text(encoding="utf-8"))
            if hits:
                out.unlink(missing_ok=True)
                outputs.remove(out)
        if hits:
            check_failed.add(fr.file_id)
            fr.state = "failed"
            classes = _report.counts_text(Counter(h.cls for h in hits))
            fr.reasons.append("final check found %s, output deleted" % classes)
            fr.parts[0].public_notes.append("final check failed, output deleted")
            continue
        fr.output = out


def inbox_files(p: config.Paths) -> list[Path]:
    """Every regular, not hidden file directly in the vault inbox, sorted."""
    try:
        return sorted(f for f in p.inbox.iterdir() if f.is_file() and not f.name.startswith("."))
    except OSError:
        return []
