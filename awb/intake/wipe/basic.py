"""The rules of the first release (wipe mode keeps them): the stop words of rules/stop-words.txt, the legal form,
the company label, the mail local part, First von Last, the run of capitalised words, Last First, the title, the
initial and the spaced heading, with the character mask and `unknown_candidates`, which runs them alone (the goal
check of `awb spawn`). One family of the wipe rules (F1 step 8); the package text is awb/intake/wipe/__init__.py.
"""
from __future__ import annotations

import functools
import re

from awb import codes, patterns
from awb.matcher import Span


# --------------------------------------------------------------------------- the rules of the first release

_STOP_FILE = patterns.RULES_DIR / "stop-words.txt"


def _load_stop_words() -> tuple[frozenset[str], tuple[re.Pattern, ...], tuple[str, ...]]:
    """Single stop words (case folded), the multi-word stop phrases as patterns and the case folded first word of
    each phrase (same order)."""
    singles: set[str] = set()
    phrases: list[re.Pattern] = []
    firsts: list[str] = []
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
            firsts.append(words[0].casefold())
    return frozenset(singles), tuple(phrases), tuple(firsts)


_STOP_SINGLES, _STOP_PHRASES, _STOP_FIRSTS = _load_stop_words()
# one scan finds which first words occur in a text, so only the phrases that can match run (there are hundreds)
_STOP_FIRST_RE = re.compile(r"(?<![\w-])(?:%s)(?![\w-])" % "|".join(
    re.escape(w) for w in sorted(set(_STOP_FIRSTS), key=len, reverse=True)), re.IGNORECASE) if _STOP_FIRSTS else None

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
_POSSESSIVE_RE = re.compile(r"(?<=[^\W\d_])['’][sS]$")
"""The English possessive at the end of a word, with a straight or a typographic apostrophe."""

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
    # the same text is scanned again and again in one intake, and the answer depends on the text alone
    return [Span(a, b, "stop") for a, b in _stop_ranges(text)]


@functools.lru_cache(maxsize=64)
def _stop_ranges(text: str) -> tuple[tuple[int, int], ...]:
    if _STOP_FIRST_RE is None:
        return ()
    present = {m.group(0).casefold() for m in _STOP_FIRST_RE.finditer(text)}
    if not present:
        return ()
    return tuple((m.start(), m.end()) for rx, first in zip(_STOP_PHRASES, _STOP_FIRSTS) if first in present
                 for m in rx.finditer(text))


def _ordinary(word: str) -> bool:
    """A function word, a stop word or the name of an authoring tool."""
    low = word.casefold()
    return low in _FUNCTION_WORDS or low in _STOP_SINGLES or low in _TOOL_WORDS


@functools.lru_cache(maxsize=4096)
def _name_word(word: str) -> bool:
    """A word shaped like part of a name: capitalised (any alphabet, accents, O'Name, Name-Name) or in
    capitals with at least _MIN_CAPS_LETTERS letters. An English possessive ('s, ’s) at the end is no part of the
    word: Vrelkam's is the name Vrelkam (finding 7 of the review of 2026-10-10)."""
    word = _POSSESSIVE_RE.sub("", word)
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
    return [list(run) for run in _runs_cached(text)]


@functools.lru_cache(maxsize=64)
def _runs_cached(text: str) -> tuple[tuple[tuple[int, int, str], ...], ...]:
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
    return tuple(tuple(run) for run in runs)


def _run_candidates(text: str, blocked: _Mask) -> list[tuple[int, int]]:
    """Two or more name words in a row, anywhere, also at the start of a sentence."""
    return _run_spans(text, blocked, sentence_only=False)


def _sentence_pair_candidates(text: str, blocked: _Mask) -> list[tuple[int, int]]:
    """Two or more name words in a row that start a sentence (after a full stop, ! or ?). The strong rules use
    it; a capitalised pair at the start of a short text or in the middle of a sentence is left to the full
    rules, so that a goal such as "Landing Zone for the first workload" still passes."""
    return _run_spans(text, blocked, sentence_only=True)


def _first_name_pair_candidates(text: str, blocked: _Mask) -> list[tuple[int, int]]:
    """Two or more name words in a row, anywhere, whose first word is a first name of rules/first-names.txt
    ("call with Jonas Vrelkam"). The short texts of a commit message and a ledger line take it on top of the
    strong rules (TM0); a pair of two unknown words is left to the full rules."""
    out = []
    for a, b in _run_spans(text, blocked, sentence_only=False):
        first = re.match(r"\w+", text[a:b])
        if first and first.group(0).casefold() in _FIRST_NAMES:
            out.append((a, b))
    return out


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


def _title_candidates(text: str, blocked: _Mask, titles: frozenset[str] = _TITLES) -> list[tuple[int, int]]:
    """Up to _MAX_TITLE_WORDS name words after a salutation or title (Herr, Frau, Mr, Dr ...)."""
    out: list[tuple[int, int]] = []
    tokens = [(m.start(), m.end(), m.group(0)) for m in _TOKEN_RE.finditer(text)]
    for i, (a, b, w) in enumerate(tokens):
        if w.casefold() not in titles:
            continue
        j = i + 1
        names: list[tuple[int, int, str]] = []
        prev_end = b
        while j < len(tokens) and len(names) < _MAX_TITLE_WORDS:
            ta, tb, tw = tokens[j]
            gap = text[prev_end:ta]
            if not gap or gap.strip(" \t.") or "\n" in gap:
                break
            if tw.casefold() in titles and not names:
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
        if _collect_values(" ".join(_spaced_words(m.group(0))), [], _JOINED_RULES):
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


def _collect_values(text: str, known: list[Span], rules) -> list[str]:
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
    found = _collect_values(text, known, rules)
    if keep:
        kept = {keep_key(k) for k in keep}
        found = [c for c in found if keep_key(c) not in kept]
    return found


# the names of the later families this one calls; they import this module, so these come after its own
from awb.intake.wipe.lexicon import _FIRST_NAMES, _ROLE_WORDS  # noqa: E402
from awb.intake.wipe.runstate import keep_key  # noqa: E402
