"""The candidate rules of the intake and the forms they teach (wipe mode, T4 of 2026-10-08).

The intake replaces every registered form by its code (the matcher) and every piece of structured data by its
token (the patterns). The rules here find what neither knows: strings shaped like the name of a person, a company or
a place. In wipe mode every string they recognise becomes a neutral token of its class, without a stop:

    person    [person 1]   a title, a salutation, a sign-off, a mail header, a speaker label, a person label or
                           column, a by-line, an initial, "Last, First", a login, a first name of the list ...
    company   [company 1]  a legal form, a company label or column, "Firma X", a Russian legal form, the domain of a
                           mail address whose local part names a person
    place     [place 1]    a street and number, a postcode and town, a place label or column
    unknown   [name 1]     a run of capitalised words that no list knows, an identifier, a lone word in a heading

The numbers count per class within one import run and restart with the next one: they tell two persons of one mail
chain apart and link nothing across imports. `collect` gives the spans of a text with their class and the rule that
found them (`WipeSpan`); `WipeState` holds what one run learned: the forms of every wiped person and company, matched
over every output of the run like a registered form (whole word, glued into an identifier, with German and Russian
endings), the tokens issued and the product-label words of the run.

The lists that keep a text readable are rules files: the public corpus (rules/known-words.txt and
rules/known-phrases.txt, written by `awb words update`), rules/known-words-de.txt, rules/known-words-ru.txt,
rules/first-names.txt, rules/source-platforms.txt, rules/standards.txt, the section german-function of
rules/sentence-openers.txt and the section intake-seed of rules/allowed-terms.txt. A missing list gives fewer
exemptions, never more: more is wiped, nothing passes that would not pass with the list.

The rules of the first release (the legal form, the company label, the mail local part, First von Last, the run, Last
First, the title, the initial, the spaced heading) live here too; `unknown_candidates` with `rules` runs them alone,
the way the goal check of `awb spawn` does. The reference for every rule of wipe mode is the prototype of the red team
of 2026-10-07 (presentations/names/proto/wipe_proto.py, not shipped); presentations/names/REDTEAM.md names the case
behind each one. Nothing in this module prints, logs or raises with a value.
"""
from __future__ import annotations

import inspect
import re
import unicodedata
from collections import Counter
from dataclasses import dataclass

from awb import codes, normalize, patterns, register
from awb import report as _awb_report
from awb import words as _word_lists
from awb.matcher import GLUE_AFFIXES, Matcher, Span, fold_text


def _list(name: str, section: str | None = None, single: bool = False) -> frozenset[str]:
    """The entries of rules/<name>, case folded; with `section` only the lines after "# section: <section>" up to
    the next section line; with `single` only entries of one word. A missing file gives an empty set."""
    try:
        lines = (patterns.RULES_DIR / name).read_text(encoding="utf-8").splitlines()
    except OSError:
        return frozenset()
    out: set[str] = set()
    inside = section is None
    for line in lines:
        line = line.strip()
        if line.startswith("# section:"):
            inside = section is not None and line[len("# section:"):].strip() == section
            continue
        if not line or line.startswith("#") or not inside:
            continue
        if single and len(line.split()) != 1:
            continue
        out.add(line.casefold())
    return frozenset(out)


def _fold(word: str) -> str:
    """Case folded, accents dropped: the letters a reader would still read as the same word."""
    decomposed = unicodedata.normalize("NFKD", word)
    return "".join(c for c in decomposed if not unicodedata.combining(c)).casefold()


_SKELETON_WORD_RE = re.compile(r"[^\W_]+")


def skeleton(text: str) -> str:
    """The letters and digits of `text`, normalised, folded and joined: the same for every way of writing it."""
    return "".join(_fold(w) for w in _SKELETON_WORD_RE.findall(normalize.normalize(text).text))


_REPORT_WORDS = frozenset(w.casefold() for w in re.findall(r"[A-Za-z]{4,}", inspect.getsource(_awb_report)))
"""Every word of the report module's source: a learned form may never be one of them, or the public report fails
its own check (Candidates, Files, Outputs, Notes)."""

# --------------------------------------------------------------------------- the rules of the first release

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


# --------------------------------------------------------------------------- word lists of wipe mode

_LEGAL_WORDS = frozenset(w.casefold() for w in (
    "GmbH", "gGmbH", "mbH", "AG", "KG", "KGaA", "SE", "Ltd", "Limited", "Inc", "LLC", "LLP", "Corp", "Co", "eV",
    "eG", "OHG", "GbR", "UG", "plc", "SAS", "SARL", "BV", "NV", "ApS", "Oy", "Kanzlei", "PartG", "mbB", "Sp",
    "ООО", "ОАО", "ЗАО", "ПАО", "АО", "ИП", "НКО", "ГУП", "ФГУП", "ТОО",
))
_GENERIC_COMPANY_WORDS = frozenset(w.casefold() for w in (
    "Beratung", "Consulting", "Consultants", "Services", "Service", "Systems", "Systeme", "Solutions", "Software",
    "Logistik", "Logistics", "Technik", "Technologies", "Technology", "Group", "Gruppe", "Holding", "Partner",
    "Partners", "International", "Deutschland", "Germany", "Europe", "Europa", "Global", "Bank", "Versicherung",
    "Industrie", "Industries", "Industrial", "Engineering", "Networks", "Network", "Digital", "Data", "Cloud",
    "Media", "Medien", "Energie", "Energy", "Handel", "Trading", "Transport", "Spedition", "Immobilien", "Verlag",
    "Werke", "Werk", "Labs", "Lab", "Studio", "Agentur", "Agency", "Institut", "Institute", "Stiftung",
    "Foundation", "Verein", "Verband", "Association", "Company", "Unternehmen", "Firma", "Kanzlei", "Praxis",
    "Klinik", "Clinic", "Hospital", "Krankenhaus", "Universität", "University", "Hochschule", "Schule", "School",
    "Stadt", "Stadtwerke", "Gemeinde", "Landkreis", "Kreis", "Bundesamt", "Ministerium", "Ministry", "Amt",
    "Behörde", "Office", "Center", "Centre", "Zentrum", "Management", "Capital", "Invest", "Investment",
    "Automotive", "Pharma", "Chemie", "Chemicals", "Textil", "Food", "Retail", "Express", "Mobile", "Mobil",
    "Online", "Design", "Marketing", "Sales", "Vertrieb", "Entwicklung", "Development", "Research", "Forschung",
    "Innovation", "Innovations", "Communications", "Kommunikation", "Telecom", "Telekom", "Informatik", "Infra",
    "Infrastructure", "Infrastruktur", "Security", "Sicherheit", "Consult", "Projekt", "Project", "Projects",
    "Team", "Teams", "Support", "Operations", "Betrieb", "Portal", "Shop", "Store", "Market", "Markt", "Haus",
    "House", "Home", "Nord", "Süd", "West", "Ost", "North", "South", "East", "Mitte", "Central", "Neue", "Neuer",
    "Deutsche", "Deutscher", "Deutsches", "Erste", "Erster", "Allgemeine", "Allgemeiner", "Vereinigte",
    "Internationale", "Europäische", "European", "German", "Swiss", "Schweizer", "Austrian", "Österreichische",
))
_ROLE_WORDS = frozenset(w.casefold() for w in (
    "Senior", "Junior", "Lead", "Head", "Chief", "Manager", "Director", "Consultant", "Architect", "Engineer",
    "Developer", "Administrator", "Admin", "Analyst", "Specialist", "Expert", "Officer", "President", "Vice",
    "Executive", "Partner", "Associate", "Assistant", "Coordinator", "Owner", "Sales", "Account", "Key",
    "Projektleiter", "Projektleiterin", "Geschäftsführer", "Geschäftsführerin", "Leiter", "Leiterin", "Berater",
    "Beraterin", "Architekt", "Architektin", "Entwickler", "Entwicklerin", "Mitarbeiter", "Mitarbeiterin",
    "Vorstand", "Prokurist", "Abteilung", "Bereich", "Team", "CEO", "CTO", "CIO", "CFO", "COO", "CISO", "CSO",
    "Solutions", "Solution", "Cloud", "Security", "Product", "Produkt", "Service", "Services", "Customer",
    "Kunde", "Kunden", "Technical", "Technischer", "Technische", "Principal", "Staff", "Member", "Mitglied",
    "Presales", "Pre-Sales", "Delivery", "Operations", "Infrastructure", "Platform", "Plattform", "Data",
    "Business", "Development", "Enterprise", "Global", "Regional", "Digital", "Program", "Programme", "Project",
    "Projekt", "Portfolio", "Strategy", "Strategie", "Transformation", "Migration", "Governance", "Compliance",
    "Risk", "Quality", "Qualität", "Testing", "Test", "Support", "Trainer", "Dozent", "Referent", "Coach",
    "Mentor", "Intern", "Student", "Werkstudent", "Praktikant", "Freelancer", "Contractor", "Extern", "External",
    "Internal", "Intern", "Guest", "Gast", "Moderator", "Moderatorin", "Host", "Speaker", "Sprecher",
    "Interviewer", "Teilnehmer", "Teilnehmerin", "Participant", "Attendee", "Author", "Autor", "Editor",
    "Desk", "Hotline", "Helpdesk", "Servicedesk", "Service-Desk", "Center", "Centre", "Zentrale", "Leitstelle",
    "Operations", "Betrieb", "Team", "Office", "Backoffice", "Frontoffice",
    "руководитель", "руководителя", "директор", "директора", "начальник", "начальника", "заместитель", "заместителя",
    "менеджер", "менеджера", "инженер", "инженера", "архитектор", "архитектора", "администратор", "администратора",
    "специалист", "специалиста", "консультант", "консультанта", "аналитик", "аналитика", "разработчик", "разработчика",
    "ведущий", "старший", "младший", "главный", "генеральный", "технический", "коммерческий", "финансовый",
    "бухгалтер", "бухгалтера", "отдел", "отдела", "департамент", "департамента", "служба", "службы", "управление",
    "управления", "проекта", "проектов", "продаж", "разработки", "эксплуатации", "поддержки", "безопасности",
    "информационной", "информационных", "технологий", "инфраструктуры", "сопровождения", "внедрения",
))
_GENERIC_ADDRESSEES = frozenset(w.casefold() for w in (
    "zusammen", "alle", "all", "everyone", "everybody", "team", "folks", "guys", "both", "beide", "Damen", "Herren",
    "Sir", "Madam", "Sirs", "friends", "Freunde", "Leute", "miteinander", "colleagues", "Kollegen", "Kolleginnen",
    "Kollegin", "Kollege", "Mitarbeiter", "Mitarbeiterinnen", "Kunde", "Kunden", "Kundin", "Partner", "Nutzer",
    "User", "Users", "Mitglied", "Mitglieder", "Teilnehmer", "Teilnehmende", "Gäste", "Guests", "there", "world",
    "Welt", "again", "nochmal", "Herr", "Herrn", "Frau", "Mr", "Mrs", "Ms", "Dr", "Prof", "Dear", "liebe", "lieber",
    "коллеги", "друзья", "все", "всем", "команда", "господа", "уважаемые",
))
_TITLE_WORDS = frozenset(w.casefold() for w in (
    "Herr", "Herrn", "Frau", "Hr", "Fr", "Mr", "Mrs", "Ms", "Mx", "Dr", "Prof", "Dipl", "Ing", "Mag", "Sir", "Lord",
    "Lady", "Dame", "г-н", "г-жа", "господин", "госпожа", "тов", "товарищ", "уважаемый", "уважаемая", "Madame",
    "Monsieur", "Mme", "M", "Signor", "Signora", "Don", "Doña",
))
_PARTICLE_WORDS = frozenset(("von", "van", "de", "zu", "vom", "zur", "der", "den", "dem", "la", "le", "du", "del",
                             "della", "di", "da", "dos", "das", "el", "al", "bin", "ibn", "ben", "af", "av", "ter"))
_MONTHS_DAYS = frozenset(w.casefold() for w in (
    "Januar", "Februar", "März", "April", "Mai", "Juni", "Juli", "August", "September", "Oktober", "November",
    "Dezember", "January", "February", "March", "May", "June", "July", "October", "December", "Montag", "Dienstag",
    "Mittwoch", "Donnerstag", "Freitag", "Samstag", "Sonntag", "Monday", "Tuesday", "Wednesday", "Thursday",
    "Friday", "Saturday", "Sunday", "Jan", "Feb", "Mar", "Apr", "Jun", "Jul", "Aug", "Sep", "Sept", "Oct", "Nov",
    "Dec", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun", "Mo", "Di", "Mi", "Do", "Fr", "Sa", "So",
    "января", "февраля", "марта", "апреля", "мая", "июня", "июля", "августа", "сентября", "октября", "ноября",
    "декабря", "понедельник", "вторник", "среда", "четверг", "пятница", "суббота", "воскресенье",
))
_NEUTRAL_LABELS = frozenset(w.casefold() for w in (
    "note", "notes", "hinweis", "hinweise", "betreff", "subject", "date", "datum", "time", "zeit", "uhrzeit",
    "summary", "zusammenfassung", "action", "actions", "action item", "action items", "todo", "to do", "todos",
    "decision", "decisions", "entscheidung", "entscheidungen", "question", "questions", "frage", "fragen",
    "answer", "antwort", "q", "a", "f", "moderator", "interviewer", "agenda", "topic", "topics", "thema", "themen",
    "result", "results", "ergebnis", "ergebnisse", "status", "next", "next steps", "nächste schritte", "source",
    "quelle", "warning", "warn", "error", "errors", "fehler", "info", "debug", "trace", "fatal", "critical",
    "example", "examples", "beispiel", "output", "input", "step", "steps", "schritt", "option", "options",
    "pro", "contra", "cons", "pros", "risk", "risks", "risiko", "risiken", "due", "deadline", "location", "ort",
    "attendees", "anwesend", "abwesend", "absent", "present", "re", "aw", "fw", "fwd", "wg", "cc", "to", "from",
    "von", "an", "kunde", "customer", "firma", "company", "note to self", "important", "wichtig", "tip", "tipp",
    "hint", "caution", "attention", "achtung", "usage", "syntax", "returns", "return", "raises", "args",
    "arguments", "parameters", "parameter", "description", "beschreibung", "title", "titel", "name", "id",
    "type", "typ", "value", "wert", "default", "see", "siehe", "references", "reference", "link", "links", "url",
    "path", "pfad", "file", "datei", "version", "stand", "author", "autor", "owner", "reviewer", "approver",
    "total", "summe", "gesamt", "sum", "note", "remark", "remarks", "bemerkung", "bemerkungen", "comment",
    "comments", "kommentar", "kommentare", "thanks", "danke", "ps", "pps", "nb", "key", "keys", "host", "hosts",
    "server", "client", "user", "password", "passwort", "login", "region", "zone", "project", "projekt", "image",
    "flavor", "flavour", "size", "price", "preis", "cost", "kosten", "unit", "quantity", "menge", "amount",
    "label", "labels", "tag", "tags", "scope", "goal", "ziel", "ziele", "context", "kontext", "background",
    "hintergrund", "problem", "solution", "lösung", "approach", "ansatz", "assumption", "assumptions", "annahme",
    "annahmen", "constraint", "constraints", "requirement", "requirements", "anforderung", "anforderungen",
    "speaker", "sprecher", "participant", "participants", "teilnehmer", "me", "ich", "you", "du", "sie", "system",
    "assistant", "bot", "ai", "chatgpt", "claude", "gpt", "model", "human", "user", "operator", "support", "sales",
    "presenter", "transcript", "recording", "chat", "message", "nachricht", "sender", "recipient", "empfänger",
    "absender", "phone", "telefon", "tel", "fax", "mail", "e-mail", "email", "mobile", "mobil", "web", "www",
    "http", "https", "ftp", "ssh", "smtp", "imap", "dns", "ip", "vpn", "api", "cli", "ui", "gui", "sdk", "os",
    "todo", "fixme", "xxx", "bug", "feature", "task", "aufgabe", "aufgaben", "milestone", "meilenstein", "phase",
    "sprint", "release", "build", "deploy", "deployment", "test", "tests", "prod", "dev", "stage", "staging",
    "примечание", "тема", "дата", "время", "вопрос", "ответ", "итог", "итоги", "решение", "решения", "статус",
    "задача", "задачи", "участники", "автор", "источник", "пример", "внимание", "важно", "спасибо", "ошибка",
    "file id", "part id", "kind", "state", "notes", "totals", "states", "outputs", "files", "customer", "date",
    "interviewee", "interviewed", "befragter", "befragte", "interviewpartner", "interviewpartnerin", "respondent",
    "fazit", "vorschlag", "vorschläge", "vereinbarung", "vereinbarungen", "beschluss", "beschlüsse", "aufgabe",
    "aufgaben", "nächster schritt", "offen", "erledigt", "klärung", "anmerkung", "anmerkungen", "bemerkung",
    "zusammenfassung", "protokoll", "begrüßung", "einleitung", "abschluss", "diskussion", "präsentation", "demo",
    "pause", "ende", "start", "top", "tagesordnungspunkt", "punkt", "notiz", "memo", "update", "feedback",
    "rückmeldung", "bedarf", "lösung", "termin", "budget", "angebot", "vertrag", "conclusion", "proposal",
    "agreement", "resolution", "clarification", "remark", "presentation", "break", "item", "need", "date",
    "offer", "contract", "hinweis", "wichtig", "achtung", "frage", "antwort", "vorname", "nachname", "e-mail",
    "telefon", "mobil", "rolle", "role", "funktion", "function", "firma", "abteilung", "department", "standort",
    "antwort", "ergebnis", "ergebnisse", "nächste schritte", "next steps", "open items", "offene punkte", "todo",
    "action items", "entscheidungen", "decisions", "teilnehmer", "attendees", "anwesend", "entschuldigt",
    "abwesend", "verteiler", "distribution", "anlagen", "attachments", "anhang", "attachment", "betreff", "subject",
    "von", "an", "cc", "bcc", "datum", "uhrzeit", "ort", "location", "dauer", "duration", "agenda", "ziel", "goal",
    "ziele", "goals", "status", "fortschritt", "progress", "risiken", "risks", "abhängigkeiten", "dependencies",
    "annahmen", "assumptions", "kosten", "costs", "nutzen", "benefit", "benefits", "aufwand", "effort", "zeitplan",
    "timeline", "meilensteine", "milestones", "verantwortlich", "responsible", "owner", "zuständig",
    "описание", "результат", "ожидаемый результат", "фактический результат", "шаги", "шаг", "предусловия",
    "предусловие", "постусловия", "статус", "комментарий", "комментарии", "действие", "действия", "проверка",
    "проверки", "условие", "условия", "данные", "вход", "выход", "ожидание", "причина", "следствие", "замечание",
    "замечания", "риск", "риски", "срок", "сроки", "приоритет", "категория", "тип", "версия", "окружение", "среда",
    "сценарий", "сценарии", "кейс", "тест", "тесты", "этап", "этапы", "цель", "цели", "задача", "задачи", "вывод",
    "выводы", "итого", "всего", "сумма", "количество", "стоимость", "цена", "название", "наименование", "номер",
    "адрес", "телефон", "почта", "email", "сайт", "контакты", "реквизиты", "подпись", "печать", "м.п.", "место",
))

_GERMAN_KNOWN = _list("known-words-de.txt")
"""German words of tender, minutes and architecture texts (rules/known-words-de.txt)."""

_COMPOUND_HEADS = (
    "verzeichnis", "blatt", "nachweis", "frist", "stelle", "bereich", "wesen", "referat", "amt", "ordnung", "satzung",
    "gesetz", "schein", "buch", "karte", "nummer", "kennung", "antrag", "bescheid", "erklärung", "preis",
    "erfassung", "verwaltung", "abrechnung", "steuerung", "regelung", "versorgung", "entsorgung", "beschaffung",
    "haltung", "wartung", "pflege", "betreuung", "beratung", "schulung", "bildung", "forschung", "lehre",
    "zentrum", "zentrale", "dienst", "werk", "werke", "betrieb", "betriebe", "leitung", "leiter", "leiterin",
    "architektur", "vergleich", "konzept", "modell", "plan", "planung", "bericht", "analyse", "übersicht",
    "strategie", "migration", "betrieb", "management", "verwaltung", "sicherheit", "lösung", "struktur", "prozess",
    "system", "systeme", "plattform", "umgebung", "landschaft", "anbindung", "verbindung", "schnittstelle",
    "schätzung", "rechnung", "kalkulation", "kosten", "preise", "liste", "tabelle", "matrix", "katalog",
    "handbuch", "dokument", "beschreibung", "anforderung", "anforderungen", "bedingungen", "vereinbarung",
    "vertrag", "angebot", "ausschreibung", "phase", "phasen", "schritt", "schritte", "plattformen", "dienste",
    "dienst", "service", "services", "netz", "netze", "netzwerk", "speicher", "server", "datenbank", "datenbanken",
    "cluster", "zone", "zonen", "region", "regionen", "standort", "standorte", "zugang", "zugriff", "verfahren",
    "richtlinie", "richtlinien", "vorgabe", "vorgaben", "kriterien", "kennzahlen", "ziele", "ziel", "bild",
    "situation", "zustand", "stand", "status", "ablauf", "abläufe", "workflow", "workflows", "test", "tests",
    "abnahme", "übergabe", "einführung", "umstellung", "umzug", "ablösung", "erneuerung", "modernisierung",
    "konsolidierung", "virtualisierung", "automatisierung", "standardisierung", "optimierung", "skalierung",
    "sicherung", "wiederherstellung", "notfall", "wartung", "support", "schulung", "dokumentation", "team",
    "teams", "leitung", "leiter", "leiterin", "verantwortung", "rolle", "rollen", "gremium", "gremien",
    "projekt", "projekte", "szenario", "fächer", "sätze", "zeiten", "fenster", "regel", "regeln", "eintrag", "einträge",
)
"""German compound nouns end in a known head: Zielarchitektur, Kostenvergleich, Betriebsmodell, Sollkonzept."""


def _compound_known(low: str) -> bool:
    return len(low) >= 7 and any(low.endswith(h) and len(low) >= len(h) + 3 for h in _COMPOUND_HEADS)

_SOURCE_PLATFORM_WORDS = _list("source-platforms.txt")
"""The vendors, products and services of the source platforms and of a typical estate (rules/source-platforms.txt)."""
_STANDARD_WORDS = _list("standards.txt")
_FIRST_NAMES = _list("first-names.txt")
"""Common first names of the languages of this host's material; a first name is a name even when the public corpus
carries the word (Peter, Michael, Jan, Max, Will, Mark) (rules/first-names.txt)."""
_RUSSIAN_KNOWN = _list("known-words-ru.txt")

_GERMAN_NOUN_SUFFIXES = ("ung", "ungen", "heit", "heiten", "keit", "keiten", "schaft", "schaften", "tion",
                         "tionen", "ität", "itäten", "ierung", "ierungen")
_GERMAN_SUFFIX_MIN = 8
_GERMAN_FUNCTION = _list("sentence-openers.txt", section="german-function")
"""German prepositions, conjunctions and adverbs that open a sentence with a capital: never a name, trimmed off a run."""
_EXTRA_KNOWN = _list("allowed-terms.txt", section="intake-seed", single=True)
"""Words the public corpus lacks that typical architecture, contract and agile texts carry (rules/allowed-terms.txt)."""
_ORG_WORDS = frozenset(("team", "teams", "abteilung", "gruppe", "leitung", "büro", "board", "gremium", "ausschuss",
                        "referat", "bereich", "support", "service", "desk", "office", "committee", "council",
                        "department", "unit", "division", "management", "vorstand", "geschäftsführung", "betriebsrat",
                        "projektteam", "projektleitung", "architekturteam", "netzwerkteam", "einkauf", "vertrieb",
                        "buchhaltung", "it", "edv", "rz", "hotline", "helpdesk", "servicedesk"))
_ORG_TAILS = ("team", "abteilung", "gruppe", "leitung", "büro", "board", "gremium", "ausschuss", "referat", "bereich",
              "support", "service", "desk", "office", "kreis", "runde", "stelle")
_SECTION_WORDS = frozenset(("teil", "kapitel", "abschnitt", "anhang", "anlage", "annex", "appendix", "section", "part",
                            "schedule", "exhibit", "punkt", "phase", "variante", "option", "szenario", "teilprojekt",
                            "los", "stufe", "welle", "block", "modul", "gruppe", "klasse", "typ", "plan", "paket",
                            "arbeitspaket", "meilenstein", "schritt", "stage", "step", "wave", "tier", "level",
                            "chapter", "attachment", "figure", "table", "tabelle", "abbildung", "bild", "format",
                            "version", "release", "zone", "region", "cluster", "site", "standort", "halle", "gebäude",
                            "raum", "rack", "reihe", "spalte", "zeile", "position", "pos", "nr", "no", "item"))
_GENERIC_LOCALS = frozenset(("support", "info", "sales", "docs", "doc", "help", "noreply", "no-reply", "service", "kontakt",
                             "contact", "office", "team", "helpdesk", "admin", "hostmaster", "postmaster", "webmaster",
                             "abuse", "security", "billing", "invoice", "rechnung", "bewerbung", "jobs", "karriere",
                             "career", "press", "presse", "marketing", "newsletter", "news", "mail", "mailer",
                             "notifications", "notification", "alerts", "alert", "bounce", "do-not-reply", "donotreply",
                             "vertrieb", "einkauf", "buchhaltung", "zentrale", "empfang", "reception", "hr", "it", "edv",
                             "rz", "noc", "soc", "cert", "ops", "devops", "orders", "order", "shop", "store", "feedback",
                             "privacy", "datenschutz", "legal", "recht", "compliance", "partner", "partners", "vendors"))
_GERMAN_ADJ = frozenset("""klein groß gross lang kurz jung alt weiß weiss schwarz braun roth rot grün gruen neu frisch kühn
fromm stark schnell ernst wild voll reich arm fein hart weich hell dunkel früh spät breit schmal hoch tief schön klug lieb
treu mutig sauer süß bitter scharf mild still laut leise froh lustig fest lose nah fern nett rein fertig gut böse klar
eng weit dick dünn leicht schwer warm kalt heiß nass trocken grob glatt rau bunt grau blau gelb rosa lila""".split())
"""German adjectives that are also surnames (Klein, Lang, Groß, Weiß): known words, so that a surname learned from
a title is matched capitalised only and the adjective in the same text stays."""
_PRODUCT_LABEL_RE = re.compile(
    r"(?im)^[ \t>*_-]*(?:tools?|werkzeuge?|produkte?|products?|software|komponenten?|components?|hersteller|vendor|"
    r"manufacturer|lösung(?:en)?|solutions?|technologien?|technolog(?:y|ies)|plattform(?:en)?|platforms?|stack|"
    r"module?|lizenz(?:en)?|licen[cs]es?|versionen?|versions?|dienste|systeme?|anwendung(?:en)?|applications?|"
    r"backup-?tool|monitoring|ticketsystem|betriebssysteme?|datenbank(?:en)?|middleware|hypervisor|firewall|"
    r"loadbalancer|virtualisierung)[ \t]*:[ \t]*(?P<v>[^\n]{1,160}?)[ \t]*$")
"""A key-value line whose label announces a product or a vendor: its value is never a name candidate."""
_PARTICIPLE_RE = re.compile(r"(?:ge|be|ver|er|ent|zer|ein|aus|auf|ab|an|vor|zu|um|über|unter|durch|nach|mit|weg|her|hin|fort|zusammen)"
                            r"[a-zäöüß]{2,}(?:ierte|ierten|iertes|ierter|ene|enen|enes|ener|ete|eten|etes|eter|te|ten|tes|ter)")
"""Eingesetzte, Betroffene, Verwendete, Migrierte: a German participle with a verb prefix and an adjective ending."""
_LETTER_LINE_RE = re.compile(r"^[ \t>*_-]*[A-ZÄÖÜА-ЯЁ]\.[ \t]+\S")


def _org_word(w: str) -> bool:
    low = w.casefold().strip(".,;:!?()[]\"'")
    return low in _ORG_WORDS or (len(low) >= 7 and low.endswith(_ORG_TAILS))


def _speech_shaped(v: str) -> bool:
    """The value after a speaker label speaks: a question or an exclamation, a sentence that ends in a full stop,
    a lower-case ordinary word in it (so passt es, Ok, danke), or six words and more. Ticketsystem der IT,
    Version 6.3, srv-bkp-01 and 20 TB are data."""
    v = v.strip()
    if not v:
        return False
    if "?" in v or "!" in v:
        return True
    ws = [w.strip(".,;:()\"'") for w in v.split()]
    ws = [w for w in ws if w]
    if len(ws) >= 2 and v.endswith((".", "…")):
        return True
    for w in ws:
        low = w.casefold()
        if (w[:1].islower() and w.isalpha() and len(w) >= 2 and low not in _FUNCTION_WORDS
                and low not in _PARTICLE_WORDS and low not in _UNIT_WORDS and low not in _SYSTEM_USERS
                and low not in _GERMAN_FUNCTION):
            return True
    return len(ws) >= 6


def _lettered_list(text: str, line_start: int) -> bool:
    """The line at line_start stands in a list of lettered lines (A. B. C.): a neighbouring non-blank line starts
    with another single letter and a dot."""
    before = [l for l in text[:line_start].split("\n")[-3:-1] if l.strip()]
    nl = text.find("\n", line_start)
    after = [l for l in text[nl + 1:nl + 200].split("\n")[:2] if l.strip()] if nl >= 0 else []
    own = text[line_start:nl if nl >= 0 else len(text)]
    neighbours = ([before[-1]] if before else []) + ([after[0]] if after else [])
    return any(_LETTER_LINE_RE.match(l) and l.strip()[:1] != own.strip()[:1] for l in neighbours)


def _generic_local(value: str) -> bool:
    """The local part of a mail address names a role, not a person: support@, info@, noreply@."""
    local = value.split("@")[0].casefold().strip("<>\"' ")
    parts = [p for p in re.split(r"[._+-]", local) if p]
    if local in _GENERIC_LOCALS or local in _SYSTEM_USERS:
        return True
    return bool(parts) and all(p in _GENERIC_LOCALS or p in _SYSTEM_USERS or p.isdigit()
                               or (p.isalpha() and len(p) >= 3 and p not in _FIRST_NAMES and _known_word(p)) for p in parts)


def _letters(word: str) -> int:
    return sum(1 for c in word if c.isalpha())


def _generic_word(word: str, months: bool = True) -> bool:
    """A word that is never a name in a strong shape: a function word, a stop word, a title, a generic addressee,
    a role word, a neutral label, a legal form, a number, and (unless months is False) a month or a weekday."""
    low = word.casefold().strip(".,;:!?()[]\"'")
    if not low or not any(c.isalpha() for c in low):
        return True
    if low in _FIRST_NAMES:
        return False
    return (low in _FUNCTION_WORDS or low in _STOP_SINGLES or low in _TOOL_WORDS
            or low in _TITLE_WORDS or low in _GENERIC_ADDRESSEES or low in _ROLE_WORDS or low in _NEUTRAL_LABELS
            or (months and low in _MONTHS_DAYS) or low in _LEGAL_WORDS or low in _PARTICLE_WORDS
            or low in _UNIT_WORDS or low in _GERMAN_FUNCTION or _role_shape(low))


_SQL_WORDS = frozenset(w.casefold() for w in (
    "TRUNCATE", "EXEC", "EXECUTE", "CALL", "GRANT", "REVOKE", "ANALYZE", "ANALYSE", "MERGE", "UPSERT", "VACUUM",
    "EXPLAIN", "COMMIT", "ROLLBACK", "BEGIN", "DECLARE", "SET", "SHOW", "USE", "WITH", "UNION", "EXCEPT", "INTERSECT",
    "HAVING", "LIMIT", "OFFSET", "FETCH", "OVER", "PARTITION", "WINDOW", "CASE", "WHEN", "THEN", "ELSE", "END", "CAST",
    "COALESCE", "NULLIF", "EXISTS", "BETWEEN", "LIKE", "ILIKE", "DISTINCT", "ASC", "DESC", "NULLS", "FIRST", "LAST",
    "RETURNING", "CONFLICT", "NOTHING", "CASCADE", "RESTRICT", "CONSTRAINT", "REFERENCES", "CHECK", "UNIQUE", "INDEX",
    "VIEW", "SEQUENCE", "TRIGGER", "FUNCTION", "PROCEDURE", "SCHEMA", "DATABASE", "ROLE", "OWNER", "TABLESPACE",
    "VARCHAR", "INTEGER", "BIGINT", "SMALLINT", "SERIAL", "BOOLEAN", "TIMESTAMP", "NUMERIC", "DECIMAL", "TEXT", "JSONB",
    "JSON", "UUID", "BYTEA", "CHAR", "DATE", "TIME", "INTERVAL", "ARRAY", "ENUM", "PRIMARY", "FOREIGN", "KEY", "NOT",
    "NULL", "DEFAULT", "AUTO_INCREMENT", "IDENTITY", "GENERATED", "ALWAYS", "STORED", "VIRTUAL", "COLLATE", "ENGINE",
    "CHARSET", "COMMENT", "LOCK", "UNLOCK", "TABLES", "FLUSH", "PRIVILEGES", "IDENTIFIED", "PASSWORD", "BACKUP",
    "RESTORE", "DUMP", "LOAD", "COPY", "IMPORT", "EXPORT", "OUTER", "INNER", "LEFT", "RIGHT", "FULL", "CROSS", "NATURAL",
    "ORDER", "GROUP", "BY", "AS", "ON", "IN", "IS", "AND", "OR", "ALL", "ANY", "SOME", "TOP", "PERCENT", "ROWS",
    "ROW", "ONLY", "TIES", "RECURSIVE", "MATERIALIZED", "REFRESH", "CONCURRENTLY", "REINDEX", "CLUSTER", "LISTEN",
    "NOTIFY", "PREPARE", "DEALLOCATE", "SAVEPOINT", "RELEASE", "ISOLATION", "LEVEL", "READ", "WRITE", "SERIALIZABLE",
))
_CODE_KIND_WORDS = frozenset(w.casefold() for w in codes.KINDS) | frozenset(("fileid", "file", "code", "codes", "kind",
                                                                              "tcp", "hcs", "awb", "cust", "part"))
_UNIT_WORDS = frozenset(w.casefold() for w in (
    "TiB", "GiB", "MiB", "KiB", "PiB", "TB", "GB", "MB", "KB", "PB", "EB", "vCPU", "vCPUs", "GHz", "MHz", "Gbit", "Mbit",
    "Gbps", "Mbps", "Gbit/s", "Mbit/s", "IOPS", "SAS", "SATA", "SSD", "HDD", "NVMe", "RAM", "CPU", "GPU", "RPM", "ms",
    "Submillisecond", "Submillis", "EUR", "USD", "CHF", "Euro", "Cent", "Std", "Stk", "pcs", "Mio", "Mrd",
))


def _known_word(word: str, suffix: bool = True) -> bool:
    """A word the run rule may not take for a name in proposed mode: a function word, a stop word, an opener, a
    tool name, a legal form, a month, a unit, a word of the public corpus or of the German list, a word with a long
    German noun suffix (not when `suffix` is False: a surname may end in -ung), a number or a code. Ligatures and
    look-alike letters are folded first."""
    from awb.matcher import fold_text
    raw = word.casefold().strip(".,;:!?()[]\"'")
    low = fold_text(word).casefold().strip(".,;:!?()[]\"'")
    if not low:
        return True
    for w in {raw, low}:
        if (w in _FUNCTION_WORDS or w in _STOP_SINGLES or w in _TOOL_WORDS
                or w in _OPENERS or w in _LEGAL_WORDS or w in _MONTHS_DAYS or w in _VOCAB
                or w in _ROLE_WORDS or w in _SOURCE_PLATFORM_WORDS or w in _UNIT_WORDS or w in _STANDARD_WORDS
                or w in _RUSSIAN_KNOWN or (w in _SQL_WORDS and word.isupper()) or (w in _CODE_KIND_WORDS and word.isupper())):
            return True
    if not any(c.isalpha() for c in low):
        return True
    if low in _RUN_EXEMPT:
        return True   # a value under a product label (Hersteller: Kemp) is a term of this run
    if _role_shape(raw):
        return True
    if "-" in low and len(low) >= 5 and all(_letters(p) < 3 or _known_word(p, suffix=suffix) for p in low.split("-")):
        return True   # Non-Disclosure: known parts
    for ending in ("ern", "en", "er", "es", "e", "n", "s"):
        if low.endswith(ending) and len(low) - len(ending) >= 4 and low[:-len(ending)] in _VOCAB:
            return True   # Netze, Europas, Mandanten: a plural or a genitive of a known word
    if suffix and low not in _FIRST_NAMES and len(low) >= 7 and _PARTICIPLE_RE.fullmatch(low):
        return True   # Eingesetzte Werkzeuge: a participle opens the heading
    if suffix and len(low) >= _GERMAN_SUFFIX_MIN and low.endswith(_GERMAN_NOUN_SUFFIXES):
        return True
    if suffix and _compound_known(raw):
        return True
    return False


def _person_known(word: str) -> bool:
    """A word that cannot be a person's name: known, and not a first name of the list."""
    low = word.casefold().strip(".,;:!?()[]\"'")
    if low in _FIRST_NAMES:
        return False
    return _known_word(word, suffix=False)


_ROLE_SUFFIXES = ("leiter", "leiterin", "manager", "managerin", "berater", "beraterin", "architekt", "architektin",
                  "ingenieur", "ingenieurin", "entwickler", "entwicklerin", "experte", "expertin", "spezialist",
                  "spezialistin", "beauftragter", "beauftragte", "verantwortlicher", "verantwortliche",
                  "koordinator", "koordinatorin", "assistent", "assistentin", "referent", "referentin",
                  "administrator", "administratorin", "techniker", "technikerin", "sachbearbeiter",
                  "sachbearbeiterin", "vorstand", "direktor", "direktorin", "inhaber", "inhaberin", "consultant",
                  "engineer", "developer", "officer", "director", "owner", "lead", "head")


def _role_shape(low: str) -> bool:
    """A German or English compound role word (Teamleiterin, Abteilungsleiter, Projektmanager, Cloud-Architekt)."""
    low = low.replace("-", "")
    return len(low) >= 9 and any(low.endswith(x) and len(low) > len(x) + 2 for x in _ROLE_SUFFIXES)


# --------------------------------------------------------------------------- the rules of proposed mode

_TOKEN_RE = _TOKEN_RE
_WORD_RE = _WORD_RE
_CODE_RES = (
    codes.PLACEHOLDER_RE, codes.PROJECT_CODE_RE,
    re.compile(r"(?<![\w-])F-[A-Z2-7]{4}(?:\.\d+)*(?![\w-])"),
    re.compile(r"\[(?:person|company|place|name)(?: \d+)?\]"),
)


def _token_spans(text: str) -> list[Span]:
    return [Span(m.start(), m.end(), "token") for rx in _CODE_RES for m in rx.finditer(text)]


def _value_words(text: str, a: int, b: int) -> list[tuple[int, int, str]]:
    return [(a + m.start(), a + m.end(), m.group(0)) for m in _TOKEN_RE.finditer(text[a:b])]


def _trim_generic(words: list[tuple[int, int, str]], extra=frozenset(), months: bool = True) -> list[tuple[int, int, str]]:
    """Drop titles, generic addressees, role words, function words and `extra` at both ends."""
    def drop(w: str) -> bool:
        low = w.casefold().strip(".,;:!?()[]\"'")
        if low in _FIRST_NAMES:
            return False
        return (low in _TITLE_WORDS or low in _GENERIC_ADDRESSEES or low in _FUNCTION_WORDS
                or low in _ROLE_WORDS or _role_shape(low) or low in extra or (months and low in _MONTHS_DAYS))
    while words and drop(words[0][2]):
        words = words[1:]
    while words and drop(words[-1][2]):
        words = words[:-1]
    return words


def _person_value(text: str, a: int, b: int, blocked, *, allow_lower: bool = False,
                  max_words: int = 4, vocabulary: bool = False, months: bool = True) -> tuple[int, int] | None:
    """The span of a person name inside text[a:b]: the words after titles, roles and generic addressees, at most
    `max_words`, blocked words (a registered form) and a parenthesised part cut off at the edges, none of the rest a
    generic word (or, with `vocabulary`, a known word that is no first name), a lower-case word only with
    allow_lower."""
    cut = re.search(r"[(\[<|]", text[a:b])
    if cut:
        b = a + cut.start()
    words = _trim_generic(_value_words(text, a, b), months=months)
    if any(_CAMEL_RE.search(w[2]) for w in words):
        return None
    while words and blocked.hit(words[0][0], words[0][1]):
        words = words[1:]
    while words and blocked.hit(words[-1][0], words[-1][1]):
        words = words[:-1]
    if not allow_lower:
        while words and not words[0][2][:1].isupper():
            words = words[1:]
        while words and not words[-1][2][:1].isupper():
            words = words[:-1]
        if any(not w[2][:1].isupper() for w in words):
            return None
    words = words[:max_words]
    if not words:
        return None
    if any(blocked.hit(w[0], w[1]) for w in words):
        return None
    known = _person_known if vocabulary else (lambda w: _generic_word(w, months=months))
    if vocabulary:
        n0 = len(words)
        while words and known(words[0][2]):
            words = words[1:]
        while words and known(words[-1][2]):
            words = words[:-1]
        if not words:
            return None
        if len(words) < n0 and len(words) == 1 and not (words[0][2].casefold() in _FIRST_NAMES
                                                       or (_name_shaped(words[0][2]) and not _known_word(words[0][2]))):
            return None   # "Fachbereich Logistik" minus the known words leaves no name
    if all(known(w[2]) for w in words):
        return None
    if any(c.isdigit() for w in words for c in w[2]):
        return None
    if len(words) == 1 and (words[0][2].isupper() and len(words[0][2]) <= 5 or codes.is_code(words[0][2])):
        return None   # an acronym or a code is no one-word name
    return words[0][0], words[-1][1]


_GREETING = (r"(?:(?:sehr[ \t]+)?geehrte[rs]?|liebe[rs]?|hallo|hi|hey|hello|dear|moin|servus|grüß[ \t]+gott|"
             r"guten[ \t]+(?:morgen|tag|abend)|привет|здравствуйте|добрый[ \t]+(?:день|вечер)|доброе[ \t]+утро|"
             r"уважаем(?:ый|ая|ые)|доброго[ \t]+(?:времени[ \t]+суток|дня|утра|вечера)|приветствую|"
             r"bonjour|ciao|hola|good[ \t]+(?:morning|afternoon|evening))")
_SALUTATION_RE = re.compile(
    r"(?im)(?:^[ \t>*_-]*|,[ \t]*|\b(?:und|and)[ \t]+)" + _GREETING + r"[ \t,]+(?P<v>[^\n,!:;]{1,60}?)[ \t]*(?=[,!:;.]|[ \t]*$)")
_NAME_FIRST_GREETING_RE = re.compile(
    r"(?im)^[ \t>*_-]*(?P<v>[^\W\d_][\w'’-]*(?:[ \t]+[^\W\d_][\w'’-]*){0,2}),[ \t]*" + _GREETING + r"[ \t]*[,!.]*[ \t]*$")


def _salutation_candidates(text: str, blocked) -> list[tuple[int, int, str]]:
    out = []
    for rx in (_SALUTATION_RE, _NAME_FIRST_GREETING_RE):
        for m in rx.finditer(text):
            comma_form = rx is _SALUTATION_RE and text[m.start("v") - 2:m.start("v")].strip().endswith(",")
            value = text[m.start("v"):m.end("v")]
            if comma_form and not all(w[:1].isupper() for w in value.split()):
                continue   # "Привет, Veeam поставил": a sentence after the comma, not an addressee
            span = _person_value(text, m.start("v"), m.end("v"), blocked, allow_lower=not comma_form, max_words=3,
                                 months=False)
            if span:
                sw = text[span[0]:span[1]].split()
                if not any(w.casefold().strip(".,") in _TITLE_WORDS for w in value.split()) \
                        and all(_person_known(w) or _generic_word(w) or _org_word(w) for w in sw):
                    continue   # Hallo Netzwerkteam, Hallo Einkauf: a group, not a person
                out.append((span[0], span[1], "person"))
    return out


_CLOSING = (r"(?:mit[ \t]+(?:freundlichen|besten|herzlichen|lieben|sonnigen|freundlichem|bestem|herzlichem|liebem)[ \t]+gr(?:ü|ue|u)(?:ß|ss)(?:en|e)?|"
            r"(?:viele|beste|liebe|herzliche|freundliche|schöne|sonnige|sportliche|kollegiale)[ \t]+gr(?:ü|ue)(?:ß|ss)e|"
            r"(?:schönen|lieben|besten)[ \t]+gru(?:ß|ss)|gru(?:ß|ss)|gr(?:ü|ue)(?:ß|ss)e|lg|vg|mfg|hg|bg|vlg|glg|"
            r"best[ \t]+regards|kind[ \t]+regards|warm[ \t]+regards|warmest[ \t]+regards|regards|rgds|br|kr|best|"
            r"best[ \t]+wishes|all[ \t]+the[ \t]+best|take[ \t]+care|cheers|thanks|thx|thank[ \t]+you|many[ \t]+thanks|"
            r"thanks[ \t]+and[ \t]+regards|sincerely|yours[ \t]+(?:sincerely|faithfully|truly)|с[ \t]+уважением|"
            r"всего[ \t]+доброго|всего[ \t]+хорошего|всего[ \t]+наилучшего|с[ \t]+наилучшими[ \t]+пожеланиями|удачи|"
            r"хорошего[ \t]+(?:дня|вечера)|с[ \t]+благодарностью|искренне[ \t]+ваш(?:а)?|до[ \t]+связи|до[ \t]+встречи|"
            r"заранее[ \t]+спасибо|с[ \t]+уважением[ \t]+и[ \t]+наилучшими[ \t]+пожеланиями|cordialement|"
            r"bien[ \t]+à[ \t]+vous|saludos|un[ \t]+saludo|cordiali[ \t]+saluti)")
_CLOSING_RE = re.compile(
    r"(?im)^[ \t>*_-]*(?:(?:vielen[ \t]+dank|danke|thanks|thank[ \t]+you|спасибо)[ \t]*(?:und|and|,|и)?[ \t]*)?" + _CLOSING +
    r"(?![^\W\d_])(?:[ \t]*[/|,-][ \t]*" + _CLOSING + r"(?![^\W\d_]))?"
    r"(?:[ \t]+(?:aus|from|nach|из)[ \t]+(?P<place>[^\W\d_][\w'’-]*(?:[ \t]+[^\W\d_][\w'’-]*){0,2}))?"
    r"[ \t]*[,.!:]*[ \t]*(?P<same>[^\n]{0,80}?)[ \t]*$")
_SIGNER_PREFIX_RE = re.compile(r"(?i)^[ \t>*_-]*(?:i\.[ \t]?A\.|i\.[ \t]?V\.|ppa\.|gez\.|--|—)?[ \t]*")


def _signoff_candidates(text: str, blocked) -> list[tuple[int, int, str]]:
    out = []
    for m in _CLOSING_RE.finditer(text):
        if m.group("place"):
            a, b = m.start("place"), m.end("place")
            if not blocked.hit(a, b) and not all(_known_word(w) for w in text[a:b].split()):
                out.append((a, b, "place"))
        same = m.group("same")
        if same and same.strip():
            # the signer on the closing's line: capitalised words only (LG Tobias), never "und ein schönes Wochenende"
            if all(w[:1].isupper() or w.casefold() in _PARTICLE_WORDS for w in same.split()) and len(same.split()) <= 4:
                span = _person_value(text, m.start("same"), m.end("same"), blocked, allow_lower=False, max_words=4)
                if span and not any(c.isdigit() for c in text[span[0]:span[1]]):
                    out.append((span[0], span[1], "person"))
                continue
            if not re.search(r"[^\W\d_]", same.strip(" ,.!")):
                pass
            else:
                continue
        pos = m.end()
        seen = 0
        while pos < len(text) and seen < 4:
            nl = text.find("\n", pos)
            line_end = len(text) if nl < 0 else nl
            line = text[pos:line_end]
            nxt = line_end + 1
            if line.strip(" \t>*_-—") == "":
                pos = nxt
                seen += 1
                continue
            if re.fullmatch(r"[ \t>*_-]*\[(?:person|company|place|name)(?: \d+)?\][ \t,.]*", line):
                break   # the signer was wiped in an earlier pass; the role line below it stays
            seen += 1
            p = _SIGNER_PREFIX_RE.match(line)
            start = pos + (p.end() if p else 0)
            cut = len(line)
            for sep in ("|", ",", " / ", " - ", " – ", "(", "<", "@"):
                i = line.find(sep, p.end() if p else 0)
                if i >= 0:
                    cut = min(cut, i)
            # a company line (a legal form or a registered form) is not the signer: look one line further
            if _COMPANY_RE.search(line):
                pos = nxt
                continue
            line_words = text[start:pos + cut].split()
            lower_line = bool(line_words) and line_words[0][:1].islower()
            if lower_line and not (len(line_words) <= 2 and line_words[0].casefold().strip(".,") in _FIRST_NAMES):
                break   # "stage two of the plan": prose after a closing, no signer
            span = _person_value(text, start, pos + cut, blocked, allow_lower=lower_line, max_words=4)
            if span and not any(w.casefold().strip(".,") in _TITLE_WORDS for w in line_words) \
                    and all(_person_known(w) or _generic_word(w) or _org_word(w) for w in text[span[0]:span[1]].split()):
                span = None   # Das Projektteam, TCP Architekturteam: a team signs, not a person
            if span and not any(c.isdigit() for c in text[span[0]:span[1]]):
                out.append((span[0], span[1], "person"))
                break
            if blocked.hit(start, pos + cut):
                pos = nxt
                continue
            break
    return out


_HEADER_RE = re.compile(
    r"(?im)^[ \t>*_]*(?:from|von|to|an|cc|bcc|sender|absender|reply-to|antwort[ \t]+an|от[ \t]+кого|от|кому|копия|"
    r"скрытая[ \t]+копия|получатель|адресат|отправитель)"
    r"[ \t]*:[ \t]*[*_]*[ \t]*(?P<v>.+?)[ \t]*$(?P<cont>(?:\n[ \t]+[^\n]*)*"
    r"(?:\n(?![ \t>*_]*(?:from|von|to|an|cc|bcc|gesendet|sent|betreff|subject|date|datum|тема|дата)[ \t]*:)"
    r"[ \t]*[^\W\d_][\w'’.-]*(?:[ \t]+[^\W\d_][\w'’.-]*){0,2}[ \t]*$)?)")
_ON_BEHALF_RE = re.compile(r"(?i)[ \t]+(?:im[ \t]+auftrag[ \t]+von|on[ \t]+behalf[ \t]+of|via)[ \t]+")
_ADDRESS_IN_HEADER_RE = re.compile(r"<[^<>\n]*>|(?<![\w.%+-])[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
_INVERTED_ITEM_RE = re.compile(r"^\s*([^\W\d_][\w'’-]*)\s*,\s*([^\W\d_][\w'’-]*(?:\s+[^\W\d_]\.)?)\s*$")


def _header_items(value: str, offset: int) -> list[tuple[int, int]]:
    """(start, end) of every display name of a header value, positions in the text."""
    items: list[tuple[int, int]] = []
    pieces: list[tuple[int, int]] = []
    if _ADDRESS_IN_HEADER_RE.search(value):
        last = 0
        for m in _ADDRESS_IN_HEADER_RE.finditer(value):
            pieces.append((last, m.start()))
            last = m.end()
            # a separator after the address
            while last < len(value) and value[last] in ",; \t":
                last += 1
        if last < len(value):
            pieces.append((last, len(value)))
    else:
        parts = value.split(";") if ";" in value else [value]
        pos = 0
        for part in parts:
            a, b = pos, pos + len(part)
            pos = b + 1
            if _INVERTED_ITEM_RE.match(part):
                pieces.append((a, b))
            else:
                sub = 0
                for item in part.split(","):
                    pieces.append((a + sub, a + sub + len(item)))
                    sub += len(item) + 1
    for a, b in pieces:
        seg = value[a:b]
        lead = len(seg) - len(seg.lstrip(" \t\"'“”„‚‘’"))
        trail = len(seg) - len(seg.rstrip(" \t\"'“”„‚‘’,;"))
        if b - trail > a + lead:
            items.append((offset + a + lead, offset + b - trail))
    return items


def _header_candidates(text: str, blocked) -> list[tuple[int, int, str]]:
    out = []
    for m in _HEADER_RE.finditer(text):
        a0, b0 = m.start("v"), m.end("cont") if m.group("cont") else m.end("v")
        value = text[a0:b0]
        pieces = []
        last = 0
        for sep in _ON_BEHALF_RE.finditer(value):
            pieces.append((last, sep.start()))
            last = sep.end()
        pieces.append((last, len(value)))
        for pa, pb in pieces:
            for a, b in _header_items(value[pa:pb], a0 + pa):
                seg = text[a:b]
                cut = len(seg)
                for sep in ("(", "|", " via "):
                    i = seg.find(sep)
                    if i > 0:
                        cut = min(cut, i)
                seg_end = a + len(seg[:cut].rstrip())
                if seg_end <= a:
                    continue
                words = _value_words(text, a, seg_end)
                if _COMPANY_RE.search(seg[:cut]) or any(w.casefold() in _LEGAL_WORDS for w in seg[:cut].split()):
                    if words and not any(blocked.hit(w[0], w[1]) for w in words):
                        out.append((words[0][0], words[-1][1], "company"))
                    continue
                # a notification service as the sender (Elastic Cloud Server Alerts) is no person: every word known
                if words and all(_person_known(w[2]) for w in words):
                    continue
                span = _person_value(text, a, seg_end, blocked, allow_lower=True, max_words=4)
                if span:
                    out.append((span[0], span[1], "person"))
    return out


_NAME_RUN = r"(?:[^\W\d_][\w'’.-]*[ \t]+){0,3}[^\W\d_][\w'’.-]*"
_VERB = r"(?:schrieb|schreibt|wrote|writes|написал(?:а)?|пишет|писал(?:а)?|a[ \t]+écrit|escribió|ha[ \t]+scritto)"
_REPLY_DE_RE = re.compile(r"(?im)^[ \t>]*am[ \t]+.{4,60}?[ \t]+schrieb[ \t]+(?P<v>[^<:\n]{2,80}?)[ \t]*(?:<[^>\n]*>)?[ \t]*:?[ \t]*$")
_REPLY_EN_RE = re.compile(r"(?im)^[ \t>]*on[ \t]+.{4,80}?[ \t](?P<v>" + _NAME_RUN + r")[ \t]*(?:<[^>\n]*>)?[ \t\n>]+wrote:[ \t]*$")
_REPLY_RU_RE = re.compile(r"(?im)^[ \t>]*(?:.{4,80}?[ \t])?(?P<v>" + _NAME_RUN + r")[ \t]*(?:<[^>\n]*>)?[ \t]+(?:написал(?:а)?|пишет|писал(?:а)?)[ \t]*(?:\(а\))?:[ \t]*$")
_QUOTE_HEAD_RE = re.compile(r"(?im)^[ \t>]*(?P<v>" + _NAME_RUN + r")[ \t]*(?:<[^>\n]*>)?[ \t]+" + _VERB + r"(?:[ \t]+[^\n]{0,80})?:[ \t]*$")
_REPLY_NOVERB_RE = re.compile(r"(?im)^[ \t>]*(?:[^\n<]{4,80}?,[ \t]*)(?P<v>" + _NAME_RUN + r")[ \t]*<[^>\n]*>[ \t]*:[ \t]*$")


def _reply_candidates(text: str, blocked) -> list[tuple[int, int, str]]:
    out = []
    for rx in (_REPLY_DE_RE, _REPLY_EN_RE, _REPLY_RU_RE, _QUOTE_HEAD_RE, _REPLY_NOVERB_RE):
        for m in rx.finditer(text):
            a, b = m.start("v"), m.end("v")
            words = _value_words(text, a, b)
            # the date and time words in front of the name are known words or digits
            while words and (_known_word(words[0][2]) and words[0][2].casefold() not in _FIRST_NAMES
                             or words[0][2].casefold() in ("am", "pm", "uhr", "um", "at", "г")):
                words = words[1:]
            if not words:
                continue
            span = _person_value(text, words[0][0], b, blocked, allow_lower=True, max_words=4)
            if span:
                out.append((span[0], span[1], "person"))
    return out


_NAME_SHAPE_RE = re.compile(r"[^\W\d_]+(?:[-'’][^\W\d_]+)*\.?")
"""A word of a name: letters of any script, an inner hyphen or apostrophe, an initial's dot."""
_CAMEL_RE = re.compile(r"[a-zäöüß][A-ZÄÖÜ]")


def _name_shaped(w: str) -> bool:
    """Letters with an inner hyphen or apostrophe, no inner capital after a small letter (RegisterError, vSphere)."""
    return bool(_NAME_SHAPE_RE.fullmatch(w)) and not _CAMEL_RE.search(w)
_STAMP = r"(?:\[?\(?(?:[\d./-]{6,10}[,\s]+)?\d{1,2}:\d{2}(?::\d{2})?(?:[ \t]*(?:AM|PM|am|pm))?\)?\]?[ \t]*[-–]?[ \t]*)"
_SPEAKER_RE = re.compile(
    r"(?m)^[ \t>*_-]*" + _STAMP + r"?(?P<label>[^\s:\[\]|<>(){}\"'#*_-][^:\n\[\]|<>{}]{0,40}?)"
    r"(?:[ \t]*\((?P<paren>[^()\n]{1,30})\))?[*_]*[ \t]*(?:\(\d{1,2}:\d{2}(?::\d{2})?\))?[ \t]*:[*_]*(?:[ \t]+(?=\S)|[ \t]*$)")
_SPEAKER_PIPE_RE = re.compile(r"(?m)^[ \t>*_-]*" + _STAMP + r"?(?P<label>[^\s:\[\]|<>(){}\"'#*_-][^:\n\[\]|<>{}]{0,40}?)[ \t]*\|[ \t]*(?P<company>[^:\n|]{1,40}?)[ \t]*:[ \t]+(?=\S)")
_STAMP_ONLY_RE = re.compile(r"(?m)^[ \t>*_-]*" + _STAMP + r"(?P<label>[^\W\d_][\w'’.-]*(?:[ \t]+[^\W\d_][\w'’.-]*){0,3})[ \t]*(?:\([^()\n]{1,30}\))?[ \t]*$")
_TEAMS_RE = re.compile(r"(?m)^[ \t]*(?P<label>[^\n\t]{2,60}?)(?:[ \t]{2,}|\t+)\d{1,2}:\d{2}(?::\d{2})?(?:[ \t]*(?:AM|PM|am|pm))?[ \t]*$")
_ZOOM_RE = re.compile(r"(?m)^[ \t]*\d{1,2}:\d{2}(?::\d{2})?[ \t]+From[ \t]+(?P<label>[^\n:]{1,60}?)[ \t]+to[ \t]+[^\n:]{1,40}?[ \t]*:")
_QA_PAREN_RE = re.compile(r"(?m)^[ \t>*_-]*(?:[A-Za-z]{1,2}|Frage|Antwort|Question|Answer)[ \t]*\((?P<label>[^()\n]{2,40})\)[ \t]*:")
_LEGEND_RE = re.compile(r"(?im)^[ \t>*_-]*(?:speaker|sprecher|teilnehmer|participant)[ \t]*\d{1,2}[ \t]*[=:][ \t]*(?P<label>[^\n,;(]{2,60})")
_NOUN_VON_RE = re.compile(r"(?m)^[ \t>*_-]*(?:[A-ZÄÖÜ][\wäöüß-]+)[ \t]+(?:von|from|by|of)[ \t]+(?P<label>[^\W\d_][\w'’.-]*(?:[ \t]+[^\W\d_][\w'’.-]*){0,2})[ \t]*:")
_VTT_V_RE = re.compile(r"<v[ \t]+(?P<label>[^>\n]{1,40})>")
_LABEL_AFTER_VON_RE = re.compile(r"[ \t]+(?:von|van|de|vom|aus|from)[ \t]+(?P<company>[^\W\d_][\w'’.-]*(?:[ \t]+[^\W\d_][\w'’.-]*){0,2})[ \t]*$")


def _speaker_candidates(text: str, blocked) -> list[tuple[int, int, str]]:
    counts: Counter = Counter()
    speech: dict[str, bool] = {}
    fields: set[str] = set()
    found: list[tuple[int, int, str, str]] = []   # (start, end, key, company after von or "")
    for m in _SPEAKER_RE.finditer(text):
        label = m.group("label").strip()
        company = ""
        von = _LABEL_AFTER_VON_RE.search(label)
        if von:
            company = von.group("company")
            label = label[:von.start()].strip()
        key = " ".join(label.split()).casefold()
        if not label or key in _NEUTRAL_LABELS or any(c.isdigit() for c in label) or len(key) < 2:
            continue
        words = label.split()
        if not 1 <= len(words) <= 4 or len(label) > 40:
            continue
        if not all(_name_shaped(w) for w in words):
            continue   # "def check(text", "`check`", "Places (x)", "class VaultError" are code, never a speaker
        if len(words) >= 2 and not all(w[:1].isupper() or w.casefold() in _PARTICLE_WORDS for w in words):
            continue   # "check socket ops:", "file alone:" are fragments of prose, not a speaker
        if key.endswith(("s",)) and key[:-1] in _NEUTRAL_LABELS:
            continue
        rest = [w for w in words if w.casefold().strip(".") not in _TITLE_WORDS and not _role_word(w)]
        if not rest or len(rest) > 3:
            continue
        lead = len(rest) < len(words) and words[0] not in rest   # Architekt Zrbl: the role stands before the name
        rkey = " ".join(rest).casefold()
        if rkey in _NEUTRAL_LABELS or rkey in _COMPANY_LABELS:
            continue
        if any(w.casefold() in _PARTICLE_WORDS for w in rest) and not any(w.casefold() in _FIRST_NAMES for w in rest):
            counts[rkey] -= 1   # "Breite der Anbindung:" is a field; a name with a particle must repeat to count
        # a speaker is named by words nobody knows, or by a first name; "file id", "Note", "Status" are labels
        if len(rest) == 1 and _person_known(rest[0]) and not (rest[0].isupper() and len(rest[0]) <= 3):
            continue
        if len(rest) == 1 and rest[0].isupper() and len(rest[0]) <= 3:
            ikey = "initials:" + rest[0]
            counts[ikey] += 1
            line_end = text.find("\n", m.end())
            speech[ikey] = speech.get(ikey, False) or _speech_shaped(text[m.end():line_end if line_end >= 0 else len(text)])
            a = m.start("label") + m.group("label").find(rest[0])
            found.append((a, a + len(rest[0]), ikey, company))
            continue
        if len(rest) >= 2 and all(_person_known(w) for w in rest):
            continue
        unknown = [w for w in rest if not _person_known(w) and w.casefold() not in _FIRST_NAMES]
        if len(unknown) == 1 and not lead and not any(w.casefold() in _FIRST_NAMES for w in rest):
            # Zammad:, Bareos Director:, Bareos Storage Daemon: one unknown word, the rest product words or a
            # trailing role word: a speaker only when the label speaks somewhere, else a field of a list
            line_end = text.find("\n", m.end())
            value = text[m.end():line_end if line_end >= 0 else len(text)]
            speech[rkey] = speech.get(rkey, False) or _speech_shaped(value)
            fields.add(rkey)
        counts[rkey] += 1
        if lead:
            counts[rkey] += 1   # a title or a role before the name is a strong signal (Architekt Zrbl:)
        a = m.start("label") + m.group("label").find(rest[0])
        found.append((a, a + len(" ".join(rest)), rkey, company))
    out = []
    for a, b, key, company in found:
        words = key.split()
        if key.startswith("initials:"):
            if counts[key] >= 2 and not blocked.hit(a, b) and (not _known_word(key[9:]) or speech.get(key)):
                out.append((a, b, "person"))   # "TB:" that repeats and speaks is a person's initials, not a unit
            continue
        strong = len(words) >= 2 or counts[key] >= 2 or key in _FIRST_NAMES
        if key in fields and not speech.get(key):
            continue   # a product or a field name: its values are data, nobody speaks
        if strong and not blocked.hit(a, b):
            out.append((a, b, "person"))
            if company and not all(_known_word(w) for w in company.split()):
                i = text.find(company, b, b + 60)
                if i >= 0 and not blocked.hit(i, i + len(company)):
                    out.append((i, i + len(company), "company"))
    for m in _SPEAKER_PIPE_RE.finditer(text):
        span = _person_value(text, m.start("label"), m.end("label"), blocked, allow_lower=True, max_words=4)
        if not span:
            continue
        out.append((span[0], span[1], "person"))
        a, b = m.start("company"), m.end("company")
        if not blocked.hit(a, b) and not all(_known_word(w) for w in text[a:b].split()):
            out.append((a, b, "company"))
    for rx in (_STAMP_ONLY_RE, _TEAMS_RE, _ZOOM_RE, _QA_PAREN_RE, _LEGEND_RE, _NOUN_VON_RE, _VTT_V_RE):
        for m in rx.finditer(text):
            span = _person_value(text, m.start("label"), m.end("label"), blocked, allow_lower=True, max_words=4,
                                 vocabulary=(rx is _STAMP_ONLY_RE or rx is _NOUN_VON_RE))
            if span:
                out.append((span[0], span[1], "person"))
    return out


def _role_word(w: str) -> bool:
    low = w.casefold().strip(".,;:")
    return low in _ROLE_WORDS or _role_shape(low)


def _proposed_name_shape(word: str) -> bool:
    """A single capitalised word that is not known could be a first name (a speaker label)."""
    return word[:1].isupper() and not _known_word(word)


_PERSON_LABELS = (
    "name", "vorname", "nachname", "familienname", "ansprechpartner", "ansprechpartnerin", "kontakt", "contact",
    "contact person", "autor", "author", "authors", "verfasser", "ersteller", "erstellt von", "prepared by",
    "bearbeiter", "bearbeiterin", "sachbearbeiter", "sachbearbeiterin", "projektleiter", "projektleiterin",
    "project lead", "project manager", "owner", "besitzer", "verantwortlich", "verantwortlicher", "responsible",
    "teilnehmer", "teilnehmende", "participants", "attendees", "anwesend", "interviewer", "interviewee", "speaker",
    "sprecher", "presenter", "referent", "trainer", "dozent", "approved by", "freigegeben von", "geprüft von",
    "reviewed by", "signed", "unterschrift", "signature", "имя", "фамилия", "фио", "автор", "контакт",
    "ответственный", "участники", "докладчик", "manager", "lead", "reporter", "assignee", "requester", "requested by",
    "submitted by", "created by", "modified by", "last modified by", "geändert von", "zuletzt geändert von",
    "генеральный директор", "главный бухгалтер", "технический директор", "коммерческий директор", "финансовый директор",
    "исполнительный директор", "директор", "руководитель проекта", "руководитель", "начальник отдела", "начальник",
    "ответственный исполнитель", "контактное лицо", "подписант", "от исполнителя", "от заказчика", "представитель",
    "gesprächspartner", "ansprechperson", "ansprechpersonen", "kontaktperson", "key account", "account manager",
    "technischer ansprechpartner", "kaufmännischer ansprechpartner", "geschäftsführer", "geschäftsführerin",
    "geschäftsführung", "vorstand", "vorstandsvorsitzender", "vorstandsvorsitzende", "aufsichtsratsvorsitzender",
    "inhaber", "inhaberin", "managing director", "managing directors", "ceo", "cto", "cio", "cfo", "director",
    "directors", "real_name", "full_name", "display_name", "user_name", "username", "author_name", "owner_name",
    "contact_name", "fullname", "realname", "displayname", "sender_name", "from_name", "to_name", "cn",
    "unterzeichner", "unterzeichnet", "gez", "sachbearbeitung", "bearbeitung", "ihr ansprechpartner",
    "ihre ansprechpartnerin", "ihr gesprächspartner", "zuständig", "betreuer", "betreuerin", "kundenbetreuer",
    "kundenbetreuerin", "vertriebsbeauftragter", "vertriebsmitarbeiter", "berater", "beraterin",
    "first name", "last name", "surname", "given name", "family name", "middle name", "vor- und nachname",
    "vorname nachname", "vorname und nachname", "vor- und zuname", "zuname", "full name", "fullname", "person",
    "personen", "people", "mitarbeiter", "mitarbeiterin", "mitarbeitende", "kollege", "kollegin", "kollegen",
    "accountable", "consulted", "informed", "отчество", "patronymic", "сотрудник", "сотрудники", "участник",
    "ответственное лицо", "контактное лицо", "руководитель", "менеджер", "инженер", "architect", "engineer",
    "last_modified_by", "lastmodifiedby", "modified_by", "modifiedby", "created_by", "createdby", "maintainer",
    "maintainers", "maintained by", "initial-creator", "initial_creator", "creator", "comment author",
    "last modified by", "author_name", "ownername", "owner_name", "nick", "nickname", "handle", "login", "user",
    "ansible_user", "remote_user", "become_user", "ssh_user", "db_user", "git_user", "sender", "recipient",
    "empfänger", "absender", "vorgelegt von", "genehmigt von", "erstellt von", "verfasst von", "präsentiert von",
)
_LOGIN_LABELS = frozenset(("name", "login", "user", "username", "user_name", "nick", "nickname", "handle", "display_name",
                           "displayname", "real_name", "realname", "ansible_user", "remote_user", "become_user",
                           "ssh_user", "db_user", "git_user", "owner", "assignee", "reporter", "maintainer",
                           "created_by", "createdby", "modified_by", "modifiedby", "last_modified_by"))
"""Labels whose value may be a login in lower case (tpomblet, t.pomblet): one token of letters, dots and digits."""
_COMPANY_LABELS = (
    "firma", "company", "unternehmen", "organisation", "organization", "kunde", "kundin", "customer", "client",
    "auftraggeber", "auftraggeberin", "auftragnehmer", "mandant", "mandantin", "partner", "lieferant", "supplier",
    "vendor", "hersteller", "anbieter", "dienstleister", "reseller", "arbeitgeber", "employer", "компания",
    "заказчик", "клиент", "организация", "фирма", "партнёр", "партнер", "поставщик", "исполнитель", "подрядчик",
    "субподрядчик", "покупатель", "продавец", "арендатор", "арендодатель", "лицензиар", "лицензиат", "сторона",
    "стороны", "контрагент", "оператор", "провайдер", "интегратор", "вендор", "производитель",
)
_HOST_LABELS = ("hostname", "host", "hosts", "server", "servername", "fqdn", "node", "nodename", "vm", "instance",
                "rechner", "maschine", "hostnames", "servers", "nodes")
_TECH_HEADERS = frozenset(("ip", "typ", "type", "host", "hostname", "port", "os", "version", "status", "cpu", "ram",
                           "disk", "size", "größe", "flavor", "image", "region", "zone", "az", "vpc", "subnet", "mac",
                           "id", "uuid", "tag", "tags", "env", "environment", "umgebung", "cluster", "namespace", "pod",
                           "service", "dienst", "anwendung", "application", "app", "url", "endpoint", "path", "pfad",
                           "kategorie", "category", "preis", "price", "kosten", "cost", "menge", "quantity", "anzahl",
                           "modell", "model", "hersteller", "vendor", "lizenz", "license", "ablauf", "expiry"))
"""Headers that make a table technical: a Name column next to them holds resources, not people."""
_PLACE_LABELS = ("site", "sites", "datacenter", "data center", "rechenzentrum", "rz", "campus", "werk", "filiale",
                 "region", "ort", "standort", "city", "stadt", "location", "adresse", "address", "anschrift", "sitz",
                 "sitz der gesellschaft", "registergericht", "amtsgericht", "handelsregister", "firmensitz",
                 "hauptsitz", "niederlassung", "büro", "office", "lieferadresse", "rechnungsadresse", "postanschrift",
                 "город", "адрес", "место", "юридический адрес")
_LABEL_VALUE_RE = re.compile(
    r"(?im)(?<![A-Za-zÀ-ÿА-Яа-я-])_*(?P<label>%s)_*[\"']?[ \t]*[:=][ \t]*(?P<q>[\"']?)(?P<v>[^\n|\"]{1,160})" % "|".join(
        re.escape(l) for l in sorted(_PERSON_LABELS + _COMPANY_LABELS + _PLACE_LABELS, key=len, reverse=True)))
_ITEM_SPLIT_RE = re.compile(r"[ \t]*(?:;|,|/|&| und | and | и |\+)[ \t]*")


def _label_class(label: str) -> str:
    low = label.casefold()
    if low in _PERSON_LABELS:
        return "person"
    if low in _COMPANY_LABELS:
        return "company"
    return "place"


_WEAK_PERSON_LABELS = frozenset(("name", "owner", "besitzer", "manager", "lead", "contact", "kontakt", "reporter",
                                 "assignee", "requester", "responsible", "verantwortlich", "verantwortlicher",
                                 "key account", "account manager", "speaker", "sprecher", "presenter", "signed"))


_LOGIN_RE = re.compile(r"^[ \t]*(?P<v>[a-z][a-z0-9]{2,}(?:[._][a-z0-9]{2,}){0,2})[ \t]*$")
_SYSTEM_USERS = frozenset(("root", "admin", "administrator", "ubuntu", "ec2-user", "centos", "debian", "nginx", "www-data",
                           "apache", "postgres", "mysql", "redis", "docker", "jenkins", "gitlab", "git", "ansible",
                           "terraform", "vagrant", "nobody", "daemon", "sshd", "systemd", "nobody", "guest", "test",
                           "service", "svc", "app", "application", "backup", "monitoring", "deploy", "deployer",
                           "operator", "support", "unassigned", "none", "null", "true", "false", "default", "system",
                           "localhost", "server", "client", "user", "users", "bot", "cron", "mail", "postfix", "ftp",
                           "tomcat", "elastic", "kibana", "grafana", "prometheus", "node", "nodejs", "python", "java",
                           "netops", "devops", "secops", "dataops", "mlops", "finops", "gitops", "infra", "platform",
                           "network", "security", "dba", "dbas", "sre", "ops", "it", "itsm", "helpdesk", "servicedesk",
                           "cloudops", "team", "teams", "group", "groups", "squad", "tribe", "staff", "sales", "hr",
                           "finance", "legal", "marketing", "product", "engineering", "research", "qa", "test", "tester",
                           "automation", "pipeline", "ci", "cd", "build", "release", "stage", "staging", "prod", "dev",
                           "oncall", "pager", "alerts", "alert", "noreply", "info", "contact", "office", "noc", "soc"))


def _login_value(item: str) -> tuple[int, int] | None:
    """A lower-case login of letters, digits, dots and underscores (tpomblet, t.pomblet, pomblet_t) that is no
    system user and no known word: (start, end) inside `item`."""
    m = _LOGIN_RE.match(item)
    if not m:
        return None
    v = m.group("v")
    parts = [x for x in re.split(r"[._]", v) if x]
    if v in _SYSTEM_USERS or all(x in _SYSTEM_USERS or (x.isalpha() and _known_word(x)) or x.isdigit() for x in parts):
        return None
    if not any(sum(c.isalpha() for c in x) >= 4 for x in parts):
        return None
    return m.start("v"), m.end("v")


def _classed_items(text: str, a: int, b: int, cls: str, blocked, weak: bool = False,
                   quoted: bool = False, login_ok: bool = False) -> list[tuple[int, int, str]]:
    """Each item of a value (split at commas, semicolons, slashes, and, und) as a candidate of `cls`. A weak person
    label (Name:, Owner: ...) takes one word only when it is a first name or an unknown name-shaped word; a company
    value starts with a capital, has at most six words and no sentence in it."""
    out = []
    value = text[a:b]
    pos = 0
    for item in _ITEM_SPLIT_RE.split(value):
        start = value.find(item, pos)
        if start < 0:
            continue
        pos = start + len(item)
        if not item.strip():
            continue
        cut = re.search(r"[<(\[]", item)
        paren = None
        if cut:
            inner = re.match(r"\(([^()\n]{2,60})\)", item[cut.start():])
            if inner and cls == "person":
                paren = (a + start + cut.start() + 1, a + start + cut.start() + 1 + len(inner.group(1)))
            item = item[:cut.start()].rstrip()   # Author: First Last <mail>, Ansprechpartner: X (Company GmbH)
            if not item.strip():
                continue
        if re.search(r"[=>)\]{}`]|->", item):
            continue
        if cls == "person":
            login = _login_value(item) if (login_ok or quoted) else None
            if login is not None:
                span = (a + start + login[0], a + start + login[1])
            else:
                span = _person_value(text, a + start, a + start + len(item), blocked, allow_lower=False, max_words=4,
                                     vocabulary=True)
            if span and not login and re.search(r"[-_\d]", text[span[0]:span[1]]) \
                    and not all(w[:1].isupper() for w in re.split(r"[-\s]", text[span[0]:span[1]])):
                span = None   # svc-nrgtz-prod is an identifier; Müller-Lüdenscheidt a name
            if span and not login and weak and len(_value_words(text, span[0], span[1])) < 2:
                w = text[span[0]:span[1]]
                if not (w.casefold() in _FIRST_NAMES or (_name_shaped(w) and not _known_word(w))):
                    span = None
        else:
            words = _value_words(text, a + start, a + start + len(item))
            if not words or len(words) > 6 or any(blocked.hit(w[0], w[1]) for w in words):
                span = None
            elif not words[0][2][:1].isupper():
                span = None
            elif cls == "company" and len(words) == 1 and (words[0][2].casefold() in _GENERIC_COMPANY_WORDS
                                                           or words[0][2].casefold() in _ROLE_WORDS
                                                           or _known_word(words[0][2])):
                span = None   # "Partner", "intern", "Hersteller" under a company header are no company
            elif cls == "company" and (all(_known_word(w[2]) and w[2].casefold() not in _GENERIC_COMPANY_WORDS
                                           for w in words)
                                       or sum(1 for w in words if not w[2][:1].isupper()
                                              and w[2].casefold() not in ("und", "and", "&", "von", "de", "of", "for",
                                                                          "the", "la", "le", "del", "der", "des", "di",
                                                                          "en", "e", "y")) >= 1):
                span = None
            elif cls == "place" and all(_known_word(w[2]) for w in words):
                span = None
            else:
                if cls == "place":
                    while len(words) > 1 and _known_word(words[0][2]):
                        words = words[1:]   # Amtsgericht Musterhausen: the court word stays
                span = (words[0][0], words[-1][1])
        if span:
            out.append((span[0], span[1], cls))
            if paren and not blocked.hit(*paren):
                pw = text[paren[0]:paren[1]].split()
                if pw and not all(_known_word(w) for w in pw) and not any(codes.is_code(w) for w in pw):
                    out.append((paren[0], paren[1], "company"))   # Teilnehmer: Surname (Company)
    return out


def _dialogue_labels(text: str) -> set[str]:
    """Labels that open three lines or more are the roles of a dialogue (Kunde:, Berater:, Partner:), not fields."""
    counts: Counter = Counter()
    for m in re.finditer(r"(?m)^[ \t>*_-]*([^\W\d_][\w'’ -]{0,30}?)[ \t]*:[ \t]+\S", text):
        counts[m.group(1).strip().casefold()] += 1
    return {k for k, n in counts.items() if n >= 3}


def _label_value_candidates(text: str, blocked) -> list[tuple[int, int, str]]:
    out = []
    dialogue = _dialogue_labels(text)
    for m in _LABEL_VALUE_RE.finditer(text):
        label = m.group("label").casefold()
        cls = _label_class(m.group("label"))
        if cls != "person" and label in dialogue:
            continue
        value = m.group("v")
        # the value ends at the next label on the same line ("Name: X Firma: Y")
        nxt = _LABEL_VALUE_RE.search(value, 1)
        b = m.start("v") + (nxt.start() if nxt else len(value))
        out.extend(_classed_items(text, m.start("v"), b, cls, blocked, weak=label in _WEAK_PERSON_LABELS,
                                  quoted=bool(m.group("q")) and label in _LOGIN_LABELS, login_ok=label in _LOGIN_LABELS))
    return out


_TABLE_ROW_RE = re.compile(r"(?m)^[ \t]*\|(.*)\|[ \t]*$")
_TABLE_SEP_RE = re.compile(r"^[ \t]*\|?[ \t:]*-{2,}[ \t:]*(?:\|[ \t:]*-{2,}[ \t:]*)*\|?[ \t]*$")


def _table_cells(line_start: int, body: str) -> list[tuple[int, int]]:
    """(start, end) in the text of every cell of one Markdown row body (between its outer pipes)."""
    cells = []
    pos = 0
    for part in body.split("|"):
        a = line_start + pos
        b = a + len(part)
        pos += len(part) + 1
        lead = len(part) - len(part.lstrip())
        trail = len(part) - len(part.rstrip())
        if b - trail > a + lead:
            cells.append((a + lead, b - trail))
    return cells


_PIPE_ROW_RE = re.compile(r"(?m)^[ \t]*\|?(?P<body>[^\n|]*(?:\|[^\n|]*)+?)\|?[ \t]*$")
_LEGAL_ONLY_RE = re.compile(r"^(?:%s)$" % _FORM_ALT)


def _row_cells(text: str, start: int, line: str) -> list[tuple[int, int]] | None:
    """The cells of a pipe row, with or without outer pipes; None for a line without a pipe."""
    if "|" not in line or _TABLE_SEP_RE.match(line):
        return None
    body = line
    lead = 0
    stripped = line.lstrip()
    if stripped.startswith("|"):
        lead = len(line) - len(stripped) + 1
        body = stripped[1:]
    if body.rstrip().endswith("|"):
        body = body.rstrip()[:-1]
    return _table_cells(start + lead, body)


def _header_class(label: str) -> str | None:
    label = " ".join(label.split()).casefold().strip("*_ :")
    if label in _PERSON_LABELS + _COMPANY_LABELS + _PLACE_LABELS:
        return _label_class(label)
    if label in _HOST_LABELS:
        return "host"
    first = label.split()[0] if label else ""
    if first in _PERSON_LABELS or first in _COMPANY_LABELS or first in _PLACE_LABELS:
        return _label_class(first)   # "Ansprechpartner Kunde", "Ansprechpartner technisch"
    return None


_DASH_LINE_RE = re.compile(r"(?m)^[ \t]*(?:-{3,}[ \t]+)+-{3,}[ \t]*$|^[ \t]*-{6,}[ \t]*$")


def _fixed_table_candidates(text: str, blocked) -> list[tuple[int, int, str]]:
    """A fixed-width text table (pandoc, a console listing): the line over a dashed line is the header, the words
    of a column with a person, company or place header are values; columns are separated by two spaces or more."""
    out = []
    lines = text.split("\n")
    starts, pos = [], 0
    for line in lines:
        starts.append(pos)
        pos += len(line) + 1
    for i, line in enumerate(lines):
        if not _DASH_LINE_RE.match(line) or i == 0:
            continue
        header = lines[i - 1]
        cols = [(m.start(), m.end()) for m in re.finditer(r"\S+(?:[ \t]\S+)*", header)]
        if len(cols) < 2:
            continue
        classes = [_header_class(header[a:b]) for a, b in cols]
        if not any(classes):
            continue
        j = i + 1
        while j < len(lines) and lines[j].strip() and not _DASH_LINE_RE.match(lines[j]):
            row = lines[j]
            for (a, b), cls in zip(cols, classes):
                if not cls or cls == "host":
                    continue
                # the cell under the header column: from this column start to the next column start
                k = cols.index((a, b))
                cb = cols[k + 1][0] if k + 1 < len(cols) else len(row)
                cell = row[a:cb] if a < len(row) else ""
                if cell.strip():
                    x = starts[j] + a + (len(cell) - len(cell.lstrip()))
                    y = starts[j] + a + len(cell.rstrip())
                    out.extend(_classed_items(text, x, y, cls, blocked, weak=False))
            j += 1
    return out


def _table_candidates(text: str, blocked) -> list[tuple[int, int, str]]:
    """Cells of a table column whose header is a person, company, place or host label; a cell that holds only a
    legal form makes the cell before it a company; a two-column table of label cells classes its second column.
    Tables with or without outer pipes, with a separator line or two rows of the same width, a caption row above
    the header."""
    out = []
    lines = text.split("\n")
    starts = []
    pos = 0
    for line in lines:
        starts.append(pos)
        pos += len(line) + 1
    rows = [(_row_cells(text, starts[i], lines[i]), _TABLE_SEP_RE.match(lines[i]) is not None) for i in range(len(lines))]
    # the key-value pass: label cells in the first column class the second column (never the header row)
    all_labels = _PERSON_LABELS + _COMPANY_LABELS + _PLACE_LABELS
    for i, (cells, sep) in enumerate(rows):
        if not cells or len(cells) < 2 or sep:
            continue
        if i + 1 < len(rows) and rows[i + 1][1]:
            continue   # the header row of a table
        label = " ".join(text[cells[0][0]:cells[0][1]].split()).casefold().strip("*_ :")
        second = " ".join(text[cells[1][0]:cells[1][1]].split()).casefold().strip("*_ :")
        if label in all_labels and second not in all_labels:
            out.extend(_classed_items(text, cells[1][0], cells[1][1], _label_class(label), blocked,
                                      weak=label in _WEAK_PERSON_LABELS, login_ok=label in _LOGIN_LABELS))
    # the column pass
    i = 0
    while i < len(lines):
        cells, sep = rows[i]
        if not cells or sep:
            i += 1
            continue
        # a table starts here when a separator or a row of the same width follows
        nxt_sep = i + 1 < len(rows) and rows[i + 1][1]
        same_width = i + 1 < len(rows) and rows[i + 1][0] is not None and len(rows[i + 1][0]) == len(cells)
        if not (nxt_sep or same_width):
            i += 1
            continue
        if text[cells[0][0]:cells[0][1]].strip().endswith(":"):
            i += 1
            continue   # "Owner: | value": a key-value row, the key-value pass has it, never a header
        header_i = i
        header = cells
        j = i + 2 if nxt_sep else i + 1
        # a caption row (one non-empty cell) or a header without any label: try the next row as the header
        if (len(header) == 1 or not any(_header_class(text[a:b]) for a, b in header)) and j < len(rows) \
                and rows[j][0] and len(rows[j][0]) >= 2 and any(_header_class(text[a:b]) for a, b in rows[j][0]):
            header_i, header = j, rows[j][0]
            j = j + 2 if (j + 1 < len(rows) and rows[j + 1][1]) else j + 1
        if any(_LEGAL_ONLY_RE.match(text[a:b].strip()) for a, b in header):
            j = header_i   # "| Brand | GmbH |" as the first row: a table without a header, every row is data
            header = []
        labels = [" ".join(text[a:b].split()).casefold().strip("*_ :") for a, b in header]
        classes = [_header_class(text[a:b]) for a, b in header]
        technical = any(l in _TECH_HEADERS for l in labels)
        # a header cell of two or three unknown name-shaped words is a person (a RACI with names as columns)
        for (a, b), cls in zip(header, classes):
            words = text[a:b].split()
            if cls is None and 2 <= len(words) <= 3 and all(_name_shaped(w) and w[:1].isupper() for w in words) \
                    and not any(_person_known(w) for w in words) and not blocked.hit(a, b):
                out.append((a, b, "person"))
        while j < len(rows):
            rc, rsep = rows[j]
            if rc is None:
                break
            if rsep:
                j += 1
                continue
            for k, (a, b) in enumerate(rc):
                cell = text[a:b].strip()
                cls = classes[k] if k < len(classes) else None
                label = labels[k] if k < len(labels) else ""
                # a cell that is only a legal form: the cell before it is the company
                if _LEGAL_ONLY_RE.match(cell) and k >= 1:
                    pa, pb = rc[k - 1]
                    if not blocked.hit(pa, pb) and text[pa:pb].strip() and not _TABLE_SEP_RE.match(text[pa:pb]):
                        out.append((pa, pb, "company"))
                    continue
                if cls == "host":
                    out.extend(_host_piece_spans(text, a, b, blocked))
                elif cls == "person" and technical and label in _WEAK_PERSON_LABELS:
                    if label in _LOGIN_LABELS:
                        lv = _login_value(text[a:b])
                        if lv is not None and not blocked.hit(a + lv[0], a + lv[1]):
                            out.append((a + lv[0], a + lv[1], "person"))   # the owner login of a host inventory
                    continue   # a Name column of hosts or applications next to Typ, IP, Version
                elif cls == "person" and len(cell.split()) == 1 and (_person_known(cell) or _compound_known(cell.casefold())):
                    continue   # Zeiterfassung, Netzwerkbetrieb: a one-word application or department; never by a suffix alone
                elif cls:
                    out.extend(_classed_items(text, a, b, cls, blocked, weak=False, login_ok=label in _LOGIN_LABELS))
            j += 1
        i = j
    return out


_STREET_RE = re.compile(
    r"(?<![\w-])(?P<v>[A-ZÄÖÜ][\wäöüß.-]*(?:[ \t][A-ZÄÖÜ][\wäöüß.-]*){0,2}(?:[ \t-]?)(?:[Ss]traße|[Ss]trasse|[Ss]tr\.|"
    r"weg|[Ww]eg|allee|[Aa]llee|platz|[Pp]latz|gasse|[Gg]asse|ring|[Rr]ing|damm|[Dd]amm|ufer|[Uu]fer|chaussee|"
    r"[Cc]haussee|steig|markt|[Mm]arkt|promenade)[ \t]+\d{1,4}[ \t]?[a-z]?(?:[-/]\d{1,4})?)(?![\w-])")
_STREET2_RE = re.compile(r"(?<![\w-])(?P<v>(?:Am|An der|Auf dem|Im|In der|Zum|Zur)[ \t]+[A-ZÄÖÜ][\wäöüß-]+[ \t]+\d{1,4}[a-z]?)(?![\w-])")
_PLZ_CITY_RE = re.compile(r"(?<![\w.-])(?:D-\d{5}|(?:A-|CH-)\d{4}|\d{5})[ \t]+(?P<v>[^\W\d_a-zäöüß](?:[^\W\d_A-ZÄÖÜ]+|[^\W\d_a-zäöüß]{2,})(?:(?:[ \t](?:am|an|im|der|bei|ob)[ \t]|[ \t-])[^\W\d_a-zäöüß](?:[^\W\d_A-ZÄÖÜ]+|[^\W\d_a-zäöüß]{2,})){0,2})(?![\w-])")
_EN_STREET_RE = re.compile(r"(?<![\w-])(?P<v>\d{1,5}[ \t]+[A-Z][a-z]+(?:[ \t][A-Z][a-z]+){0,2}[ \t]+(?:Street|St\.|Road|Rd\.|Avenue|Ave\.|Lane|Drive|Dr\.|Boulevard|Blvd\.|Square|Way|Place))(?![\w-])")
_RU_STREET_RE = re.compile(r"(?<![\w-])(?<!и[ \t])(?<!т\.[ \t])(?<!т\.)(?P<v>(?:ул\.|улица|пр\.|проспект|пер\.|переулок|наб\.|набережная|пл\.|площадь|б-р|бульвар|ш\.|шоссе)[ \t]*[А-ЯЁ][а-яё-]+(?:[ \t][А-ЯЁ][а-яё-]+){0,2}(?:,?[ \t]*(?:д\.|дом)?[ \t]*\d{1,4}[а-я]?)?)(?![\w-])")
_RU_CITY_RE = re.compile(r"(?<![\w\d-])(?<!\d[ \t])(?<!\d)(?:г\.|город)[ \t]*(?P<v>[А-ЯЁ][а-яё-]+(?:[ \t][А-ЯЁ][а-яё-]+)?)(?![\w-])")
_PLACE_DATE_RE = re.compile(r"(?m)^[ \t]*(?P<v>[^\W\d_a-zäöüß][^\W\d_A-ZÄÖÜ-]+(?:[ \t][^\W\d_][^\W\d_A-ZÄÖÜ-]+){0,2}),[ \t]+(?:den|am|im|il|le|)[ \t]*\d{1,2}\.?[ \t]*(?:[^\W\d_a-zäöüß][^\W\d_A-ZÄÖÜ]+|[а-яё]{3,8}|\d{1,2}\.)[ \t]*\d{2,4}")
_POSTFACH_RE = re.compile(r"(?<![\w-])(?P<v>(?:Postfach|P\.?O\.? Box|PO Box)[ \t]+\d{1,8})(?![\w-])")


def _address_candidates(text: str, blocked) -> list[tuple[int, int, str]]:
    out = []
    for rx in (_STREET_RE, _STREET2_RE, _EN_STREET_RE, _RU_STREET_RE, _RU_CITY_RE, _POSTFACH_RE, _PLZ_CITY_RE, _PLACE_DATE_RE):
        for m in rx.finditer(text):
            a, b = m.start("v"), m.end("v")
            if blocked.hit(a, b):
                continue
            if rx is _PLACE_DATE_RE and all(_known_word(w) for w in m.group("v").split()):
                continue
            if rx is _RU_STREET_RE and not re.search(r"\d|,", m.group("v")) and not re.match(r"[ \t]*(?:,|д\.|дом|\d|$)", text[m.end():m.end() + 6]):
                continue   # "и пр. Миграция данных": the abbreviation of "и прочее", no street without a number
            if rx is _STREET2_RE:
                noun = m.group("v").split()[-2]
                if _known_word(noun) and not re.search(r"\b\d{5}\b", text[m.end():m.end() + 80]):
                    continue   # Am Standort 2 laufen 40 VMs: a known noun and no postcode nearby is no street
            if rx is _PLZ_CITY_RE:
                city = m.group("v")
                before = text[max(0, m.start() - 16):m.start()]
                if _known_word(city) or re.search(r"[\d.]$", before.rstrip()):
                    continue
                if re.search(r"(?i)(?:\b(?:rund|etwa|ca|circa|über|unter|knapp|als|zu|mindestens|maximal|insgesamt|weitere|"
                             r"nur|noch|dazu|gibt|sind|haben|hat|von|mit|auf|about|around|over|under|roughly|nearly|almost|"
                             r"some|than|to|approx)\.?)[ \t]*$", before):
                    continue   # rund 30000 Postfächer: a quantity, not a postcode
            out.append((a, b, "place"))
    return out


_RU_COMPANY_NOUN = (r"(?:компани(?:я|и|ей|ею|ю|ях|ям|ями)|фирм(?:а|ы|е|у|ой|ою|ах|ам|ами)|заказчик(?:а|у|ом|е|и|ов|ам)?|"
                    r"клиент(?:а|у|ом|е|ы|ов|ам)?|партн[её]р(?:а|у|ом|е|ы|ов|ам)?|"
                    r"банк(?:а|у|ом|е)?|холдинг(?:а|у|ом|е)?|фонд(?:а|у|ом|е)?|гк|группа[ \t]+компаний|группы[ \t]+компаний|"
                    r"группе[ \t]+компаний|группой[ \t]+компаний|корпораци[яиейю]|концерн(?:а|у|ом|е)?|поставщик(?:а|у|ом|е)?|"
                    r"подрядчик(?:а|у|ом|е)?|исполнител[ьяюем]|оператор(?:а|у|ом|е)?|провайдер(?:а|у|ом|е)?|интегратор(?:а|у|ом|е)?)")
_FIRMA_RE = re.compile(
    r"(?i)(?<![\w-])(?:Firma|Fa\.|company|%s)"
    r"[ \t]+(?P<v>(?-i:[A-ZÄÖÜА-ЯЁ])[\w'’-]*(?:[ \t]+(?-i:[A-ZÄÖÜА-ЯЁ])[\w'’-]*){0,3})(?![\w-])" % _RU_COMPANY_NOUN)
_RU_QUOTED_RE = re.compile(
    r"(?i)(?<![\w-])(?:%s|ООО|ОАО|ЗАО|ПАО|АО|НКО|ГУП|ФГУП|ТОО|ИП|АНО|МУП|ГБУ|ФГБУ|OOO|ZAO|PAO|OAO|TOO)"
    r"[ \t]*[«\"„“](?P<v>[^»\"“”\n]{2,60}?)[»\"“”]" % _RU_COMPANY_NOUN)
_RU_COMPANY_RE = re.compile(
    r"(?<![\w-])(?:ООО|ОАО|ЗАО|ПАО|АО|НКО|ГУП|ФГУП|ТОО|ИП|АНО|МУП|ГБУ|ФГБУ|OOO|ZAO|PAO|OAO|TOO)"
    r"[ \t]+(?P<v>[A-ZÄÖÜА-ЯЁ][\w'’-]*(?:[ \t]+[A-ZÄÖÜА-ЯЁ][\w'’-]*){0,3})(?![\w-])")
_CYR_COMPANY_RE = re.compile(
    r"(?<![\w&.-])(?P<v>[А-ЯЁ][\w'’-]*(?:[ \t]+[А-ЯЁ][\w'’-]*){0,4})[ \t]*,?[ \t]+(?:%s)(?![\w])" % _FORM_ALT)
_RU_IP_RE = re.compile(r"(?<![\w-])ИП[ \t]+(?P<v>[А-ЯЁ][а-яё]+(?:[ \t]+[А-ЯЁ]\.){0,2})(?![\w-])")
_NAME_INITIAL_RE = re.compile(r"(?<![\w.-])(?P<v>[A-ZÄÖÜА-ЯЁ][a-zäöüßа-яё]{2,}[ \t]+[A-ZÄÖÜА-ЯЁ]\.)(?![\w-])")


_LOWER_LEGAL_RE = re.compile(r"(?<![\w-])(?P<v>[a-zäöüß][\w'’-]{2,}(?:[ \t][a-zäöüß][\w'’-]{2,}){0,3})[ \t]+(?:gmbh|ag|kg|ug|ohg|gbr|ltd|inc|llc|mbh)(?![\w-])")


def _lower_company_candidates(text: str, blocked) -> list[tuple[int, int, str]]:
    """A company typed in lower case with its legal form (nrgtz beratung gmbh in a chat): the unknown words before
    the form, known words at the edges trimmed."""
    out = []
    for m in _LOWER_LEGAL_RE.finditer(text):
        words = _value_words(text, m.start("v"), m.end("v"))
        # the brand stands after the last function word: "reden mit nrgtz gmbh" is nrgtz
        cut = max((i for i, w in enumerate(words) if w[2].casefold() in _GERMAN_FUNCTION
                   or w[2].casefold() in _FUNCTION_WORDS), default=-1)
        words = words[cut + 1:]
        while words and (_known_word(words[0][2]) or _generic_word(words[0][2]) or words[0][2].casefold() in _PRONOUNS):
            words = words[1:]
        while words and (_known_word(words[-1][2]) and words[-1][2].casefold() not in _GENERIC_COMPANY_WORDS):
            words = words[:-1]
        if not words or any(blocked.hit(w[0], w[1]) for w in words):
            continue
        if all(_known_word(w[2]) for w in words):
            continue
        out.append((words[0][0], words[-1][1], "unknown"))
    return out


def _firma_candidates(text: str, blocked) -> list[tuple[int, int, str]]:
    out = []
    for m in _FIRMA_RE.finditer(text):
        a, b = m.start("v"), m.end("v")
        words = _value_words(text, a, b)
        if not words or blocked.hit(a, b) or all(_known_word(w[2]) for w in words):
            continue
        out.append((words[0][0], words[-1][1], "company"))
    for rx in (_RU_QUOTED_RE, _RU_COMPANY_RE, _RU_IP_RE, _CYR_COMPANY_RE):
        for m in rx.finditer(text):
            a, b = m.start("v"), m.end("v")
            if rx is _CYR_COMPANY_RE:
                words = _value_words(text, a, b)
                while words and (_known_word(words[0][2]) or _generic_word(words[0][2])):
                    words = words[1:]
                if not words:
                    continue
                a = words[0][0]
            if not blocked.hit(a, b) and text[a:b].strip() and not all(_known_word(w) for w in text[a:b].split()):
                out.append((a, b, "company"))
    for m in _NAME_INITIAL_RE.finditer(text):
        a, b = m.start("v"), m.end("v")
        first = text[a:b].split()[0]
        if blocked.hit(a, b) or _known_word(first) or first.casefold() in _SECTION_WORDS:
            continue   # Szenario B., Exhibit B.: a section letter, not an initial
        out.append((a, b, "person"))
    return out


_SENTENCE_END_IN_RE = re.compile(r"[^\W\d_]\.(?=\s)")


def _spaced_after_title_candidates(text: str, blocked) -> list[tuple[int, int, str]]:
    """A title (Herr, Frau, Dr ...) before a letter spaced stretch: the stretch is a person."""
    out = []
    for m in _SPACED_RE.finditer(text):
        if blocked.hit(m.start(), m.end()):
            continue
        before = text[max(0, m.start() - 12):m.start()].rstrip(" \t.")
        raw_title = re.split(r"[\s.-]+", before)[-1] if before else ""
        title = raw_title.casefold()
        if (title in _TITLE_WORDS or title in _TITLES_EXTRA) and not (len(raw_title) >= 2 and raw_title.isupper()):
            out.append((m.start(), m.end(), "person"))
    return out


def _title_candidates_cut(text: str, blocked) -> list[tuple[int, int, str]]:
    """The title rule of the intake, cut at a full stop that ends a sentence between two name words ("Herr X.
    Tobias Y" is two sentences, not one person); a dot right after a title word (Dr.) is not a sentence end."""
    out = []
    for a, b in _title_candidates(text, blocked, titles=_TITLES_WIPE):
        before = text[:a].rstrip(" \t.")
        title = (re.split(r"[\s.]+", before)[-1] if before else "").lstrip("-")
        if title and not title[:1].isupper() and title.casefold() not in _TITLES_EXTRA:
            continue   # "ms Submillis": a unit, not a title; господин and г-ну are titles
        if len(title) >= 2 and title.isupper() and title.casefold() not in _RU_TITLES:
            continue   # DR Konzept, MS Teams, HR Portal, NAT Regel: an all-caps abbreviation is no title
        parts = re.split(r"[\s.]+", before)
        prev = parts[-2].casefold().strip(",;:") if len(parts) >= 2 else ""
        prev2 = parts[-3].casefold().strip(",;:") if len(parts) >= 3 else ""
        weekdays = ("mo", "di", "mi", "do", "fr", "sa", "so")
        if title.casefold() in ("fr", "hr", "mo", "di", "mi", "do", "sa", "so") and (
                prev in weekdays or (prev in ("bis", "und", "and", "to", "von") and prev2 in weekdays)
                or re.search(r"(?i)\b(?:mo|di|mi|do|fr|sa|so)\.?[-–/][ \t]*%s$" % re.escape(title), text[:a].rstrip(" \t."))):
            continue   # Mo bis Fr Supportzeiten, Mo.-Fr. Wartungsfenster: a weekday, not Frau; "Hr. X und Fr. Y" stays
        m = _SENTENCE_END_IN_RE.search(text, a, b)
        while m and text[a:m.end()].rstrip(".").split()[-1].casefold().rstrip(".") in _TITLE_WORDS:
            m = _SENTENCE_END_IN_RE.search(text, m.end(), b)
        end = m.end() - 1 if m else b
        # a role word after the title stays: Frau Projektleiterin Pomblet
        words = _value_words(text, a, end)
        while words and _role_word(words[0][2]):
            words = words[1:]
        if not words:
            continue
        out.append((words[0][0], end, "person"))
    return out


# --------------------------------------------------------------------------- the rule sets

def _wrap(rule, cls: str):
    def run(text: str, blocked):
        return [(a, b, cls) for a, b in rule(text, blocked)]
    run.__name__ = rule.__name__
    return run


_CN_RE = re.compile(r"(?im)(?<![\w-])CN=(?P<v>[^:;\n\"]{2,80})")
_DC_RE = re.compile(r"(?i)(?:DC=([^,\n]+),?)+")
_BY_VERBS_DE = ("erstellt", "vorgelegt", "genehmigt", "geprüft", "freigegeben", "präsentiert", "verfasst", "bearbeitet",
                "erarbeitet", "zusammengestellt", "gezeichnet", "unterschrieben", "eingereicht", "geändert", "überprüft",
                "reviewt", "vorbereitet", "moderiert", "protokolliert", "aufgenommen", "notiert", "gehalten")
_BY_VERBS_EN = ("prepared", "presented", "reviewed", "approved", "created", "written", "submitted", "authored",
                "compiled", "drafted", "edited", "modified", "updated", "signed", "owned", "maintained", "comment",
                "revision", "revised", "checked", "verified", "delivered", "hosted", "moderated", "recorded")
_BYLINE_RE = re.compile(
    r"(?im)(?<![\w-])(?:(?:%s)[ \t]+von|(?:%s)[ \t]+by)[ \t]+(?P<v>[^\W\d_][\w'’.-]*(?:[ \t]+[^\W\d_][\w'’.-]*){0,3})"
    % ("|".join(_BY_VERBS_DE), "|".join(_BY_VERBS_EN)))


_INITIALS_RE = re.compile(r"(?<![\w.])(?P<v>(?:[A-ZÄÖÜА-ЯЁ]\.[ \t]?){1,3}[A-ZÄÖÜА-ЯЁ][^\W\d_]{2,})(?![^\W\d_])")


def _initial_candidates_known(text: str, blocked) -> list[tuple[int, int, str]]:
    """The initial rule of the intake (X. Surname), not for a list letter: at a line start before a known word, in
    a lettered list (A. B. C. on neighbouring lines) or after a section word (Teil V. Betrieb)."""
    out = []
    for a, b in _initial_candidates(text, blocked):
        words = text[a:b].split()
        last = words[-1] if words else ""
        line_start = text.rfind("\n", 0, a) + 1
        at_line_start = not text[line_start:a].strip(" \t>*_-#|")
        head = text[:a].rstrip(" \t")
        prev = re.split(r"[\s.]+", head)[-1].casefold().strip(",;:") if head.strip() else ""
        if prev in _SECTION_WORDS or (_known_word(last) and at_line_start):
            continue   # Szenario B. Lift and Shift, Teil V. Betrieb, A. Einleitung: a section letter
        if at_line_start and _lettered_list(text, line_start):
            continue
        out.append((a, b, "person"))
    return out


def _initials_candidates(text: str, blocked) -> list[tuple[int, int, str]]:
    """Initials with dots before a surname, Latin or Cyrillic, one to three of them (Т. Surname, Т.И. Surname)."""
    out = []
    for m in _INITIALS_RE.finditer(text):
        a, b = m.start("v"), m.end("v")
        last = re.split(r"[. \t]+", text[a:b])[-1]
        if blocked.hit(a, b) or _known_word(last) or _generic_word(last):
            continue
        before = text[max(0, a - 3):a]
        if re.search(r"[^\W\d_]\.\s?$", before):
            continue   # z. B., u. a.
        head = text[:a].rstrip(" \t")
        prev = re.split(r"[\s.]+", head)[-1].casefold().strip(",;:") if head.strip() else ""
        line_start = text.rfind("\n", 0, a) + 1
        if prev in _SECTION_WORDS or (not text[line_start:a].strip(" \t>*_-#|") and _lettered_list(text, line_start)):
            continue   # Szenario B. Lift and Shift, a lettered list: a section letter, not an initial
        out.append((a, b, "person"))
    return out


def _byline_candidates(text: str, blocked) -> list[tuple[int, int, str]]:
    """Erstellt von X, Vorgelegt von X, Prepared by X, comment by X: the words after the verb and the particle."""
    out = []
    for m in _BYLINE_RE.finditer(text):
        span = _person_value(text, m.start("v"), m.end("v"), blocked, allow_lower=False, max_words=4, vocabulary=True)
        if span and any(_org_word(w) for w in m.group("v").split()):
            continue   # Reviewed by Architecture Board, Erstellt von Abteilung Netze: a body, not a person
        if span:
            out.append((span[0], span[1], "person"))
    return out
_SYSLOG_RE = re.compile(r"(?m)^(?:[A-Z][a-z]{2}[ \t]+\d{1,2}[ \t]+\d\d:\d\d:\d\d|\d{4}-\d\d-\d\dT[\d:.+Z-]+)[ \t]+(?P<host>[a-z][a-z0-9.-]*)[ \t]+\S+(?:\[\d+\])?:[ \t](?P<rest>.*)$")
_SYSLOG_USER_RE = re.compile(r"(?:\bfor(?:[ \t]+invalid[ \t]+user)?|\buser|\bsudo:|USER=|\bby|\bsession opened for user)[ \t]+(?P<v>[a-z][a-z0-9._-]{2,})(?![\w.-])")
_SUBJECT_RE = re.compile(r"(?im)^[ \t>*_]*(?:subject|betreff|тема)[ \t]*:[ \t]*(?:(?:aw|re|fw|fwd|wg|sv|antw|ответ|antwort)[ \t]*:[ \t]*)*(?P<v>[^\n]{1,120})")
_LONE_WORD_RE = re.compile(r"(?<![^\W\d_])(?P<v>[A-ZÄÖÜА-ЯЁ][^\W\d_]{2,})(?![^\W\d_])")


_IDENT_RE = re.compile(r"(?<![\w-])[^\W_]+(?:[_-][^\W_]+)+(?![\w-])|(?<![\w-])[^\W\d_a-z][^\W\d_A-Z]+(?:[^\W\d_a-z][^\W\d_A-Z]*)+(?![\w-])")
_COMPANY_PART_WORDS = frozenset(("gmbh", "ag", "kg", "ug", "ohg", "gbr", "ltd", "inc", "llc", "corp", "ev",
                                 "beratung", "consulting", "systemhaus", "holding", "group", "gruppe", "partner",
                                 "partners", "industries", "logistik", "logistics", "handel", "verlag", "versicherung",
                                 "immobilien", "spedition", "technik", "elektrotechnik", "maschinenbau"))


def _identifier_run_candidates(text: str, blocked) -> list[tuple[int, int, str]]:
    """Capitalised parts of an identifier or file name joined by underscores or hyphens (Angebot_Nrgtz_Beratung,
    Nrgtz-Beratung-Angebot): two or more in a row with an unknown word among them are a name inside the identifier;
    the parts alone are replaced, the rest of the identifier stays."""
    out = []
    for m in _IDENT_RE.finditer(text):
        token = m.group(0)
        if blocked.hit(m.start(), m.end()) or not any(c.isalpha() for c in token):
            continue
        if "_" not in token and "-" in token and token.count("-") < 2:
            # Soll-Konzept, Go-Live, Ist-Situation: a hyphenated compound of two known parts is a word; with an
            # unknown name-shaped first part (Nrgtz-Lösung, Nrgtz-seitig, Pomblet-Konzept) the first part is a name
            first, second = token.split("-", 1)
            if (first[:1].isupper() and _letters(first) >= 4 and _name_shaped(first) and not _known_word(first)
                    and not _generic_word(first) and (_known_word(second) or _generic_word(second) or second.islower())
                    and not any(c.isdigit() for c in first)):
                if not blocked.hit(m.start(), m.start() + len(first)):
                    out.append((m.start(), m.start() + len(first), "unknown"))
            continue
        if "_" not in token and "-" not in token:
            core = token
            for form in ("GmbH", "GMBH", "AG", "KG", "UG", "SE", "Ltd", "Inc", "LLC", "GbR", "OHG"):
                if core.endswith(form) and len(core) > len(form) + 3:
                    core = core[:-len(form)]   # NrgtzBeratungGmbH: the legal form closes the name
                    break
            parts = [(m.start() + x.start(), m.start() + x.end(), x.group(0)) for x in re.finditer(r"[^\W\d_a-z][^\W\d_A-Z]*", core)]
            parts = [x for x in parts if _letters(x[2]) >= 2]
            if core != token and parts and not all(_known_word(x[2]) and x[2].casefold() not in _GENERIC_COMPANY_WORDS
                                                   for x in parts) and not blocked.hit(m.start(), m.end()):
                out.append((m.start(), m.end(), "company"))   # NrgtzBeratungGmbH
                continue
            if not (parts and parts[0][2].casefold() in _FIRST_NAMES) and any(_known_word(x[2]) for x in parts):
                continue   # IntakeResult, RevealError: a known part makes a code identifier
        else:
            parts = [(m.start() + x.start(), m.start() + x.end(), x.group(0)) for x in re.finditer(r"[^_-]+", token)]
            lows = [x[2].casefold() for x in parts]
            if any(l in _COMPANY_PART_WORDS for l in lows):
                # angebot_nrgtz_beratung_gmbh, nrgtz-beratung-angebot: an unknown lower-case part next to a company
                # word is the brand
                for x in parts:
                    low = x[2].casefold()
                    if (_letters(x[2]) >= 4 and x[2].isalpha() and low not in _COMPANY_PART_WORDS
                            and not _known_word(x[2]) and not _generic_word(x[2]) and not blocked.hit(x[0], x[1])):
                        out.append((x[0], x[1], "unknown"))
        run: list[tuple[int, int, str]] = []
        for part in parts + [(0, 0, "")]:
            w = part[2]
            if w and w[:1].isupper() and w[1:].islower() and _letters(w) >= 2 and not any(c.isdigit() for c in w):
                run.append(part)
                continue
            if (len(run) >= 2 and any(not _person_known(x[2]) and not _generic_word(x[2]) for x in run)
                    and not any(x[2].casefold() in _FUNCTION_WORDS for x in run)):
                if not blocked.hit(run[0][0], run[-1][1]):
                    out.append((run[0][0], run[-1][1], "unknown"))
            run = []
    return out


_BARE_USER_HOST_RE = re.compile(r"(?<![\w.@-])(?P<local>[a-z][a-z0-9._-]{2,})@(?P<host>[a-z][a-z0-9-]{1,}(?:\.[a-z0-9-]+)*)(?::(?P<owner>[a-z][\w-]{2,})/)?(?![\w.@-])")
_DECORATORS = frozenset(("dataclass", "property", "staticmethod", "classmethod", "abstractmethod", "cached_property",
                         "wraps", "lru_cache", "contextmanager", "override", "overload", "pytest", "mock", "patch",
                         "task", "dag", "route", "app", "router", "bp", "api", "login_required", "receiver", "signal",
                         "command", "group", "option", "argument", "click", "cli", "fixture", "parametrize", "mark",
                         "skip", "skipif", "xfail", "timeout", "retry", "singledispatch", "total_ordering", "unique",
                         "verify", "validator", "field_validator", "model_validator", "root_validator", "pydantic",
                         "cache", "memoize", "profile", "deprecated", "experimental", "beta", "internal", "export",
                         "component", "injectable", "input", "output", "override", "test", "before", "after", "setup",
                         "teardown", "given", "when", "then", "step", "scenario", "feature", "tag", "media", "import",
                         "extends", "implements", "interface", "param", "return", "returns", "throws", "see", "since",
                         "author", "version", "deprecated", "link", "code", "value", "example", "brief", "file"))
_HANDLE_RE = re.compile(r"(?<![\w.@/-])@(?P<v>[a-z][\w-]{2,})(?P<slash>/)?")


def _host_piece_spans(text: str, a: int, b: int, blocked) -> list[tuple[int, int, str]]:
    """The unknown pieces of a host or inventory name (srv-pomblet-02: pomblet), four letters or more, no affix."""
    from awb.matcher import GLUE_AFFIXES
    out = []
    first = text[a:b].split(".")[0]
    for m in re.finditer(r"[a-z]{4,}", first.casefold()):
        piece = m.group(0)
        if piece in GLUE_AFFIXES or piece in _SYSTEM_USERS or _known_word(piece) or piece in _PUBLIC_PROVIDERS:
            continue
        x, y = a + m.start(), a + m.end()
        if not blocked.hit(x, y):
            out.append((x, y, "unknown"))
    return out


_INVENTORY_HOST_RE = re.compile(r"(?m)^[ \t]*(?P<host>[a-z][a-z0-9.-]{3,})[ \t]+(?=ansible_|[a-z_]+=)")
_USERS_LIST_RE = re.compile(r"(?i)\"(?:users|members|admins|logins|owners|assignees|reviewers|approvers|maintainers|accounts)\"[ \t]*:[ \t]*\[(?P<v>[^\]]*)\]")


def _inventory_candidates(text: str, blocked) -> list[tuple[int, int, str]]:
    """Hosts of an Ansible inventory line (pomblet-web01 ansible_host=...) and the logins of a JSON users list."""
    out = []
    for m in _INVENTORY_HOST_RE.finditer(text):
        out.extend(_host_piece_spans(text, m.start("host"), m.end("host"), blocked))
    for m in _USERS_LIST_RE.finditer(text):
        for item in re.finditer(r"\"([^\"]{3,40})\"", m.group("v")):
            v = item.group(1)
            if _login_value(v.lower()) is not None:
                a = m.start("v") + item.start(1)
                if not blocked.hit(a, a + len(v)):
                    out.append((a, a + len(v), "person"))
    return out


def _login_candidates(text: str, blocked) -> list[tuple[int, int, str]]:
    """ssh login@host without a domain, git@host:owner/repo, @handle of a code owners file: the login, the owner
    and the handle are wiped when they are no system user and no known word; the host labels are learned."""
    out = []
    for m in _BARE_USER_HOST_RE.finditer(text):
        if blocked.hit(m.start(), m.end()):
            continue
        local = m.group("local")
        if "." in m.group("host") and _MAIL_RE.match(text, m.start()):
            continue   # a real address, the MAIL token has it
        if _login_value(local) is not None or _PERSON_LOCAL_RE.fullmatch(local):
            out.append((m.start("local"), m.end("local"), "person"))
        if m.group("owner") and _login_value(m.group("owner")) is not None:
            out.append((m.start("owner"), m.end("owner"), "unknown"))
        out.extend(_host_piece_spans(text, m.start("host"), m.end("host"), blocked))
    for m in _HANDLE_RE.finditer(text):
        if blocked.hit(m.start("v"), m.end("v")) or _login_value(m.group("v")) is None:
            continue
        line_start = text.rfind("\n", 0, m.start()) + 1
        after = text[m.end():m.end() + 1]
        if text[line_start:m.start()].strip() == "" and (after in ("(", ".", "", "\n") or m.group("v") in _DECORATORS):
            continue   # @dataclass, @property, @pytest.mark: a decorator, not a mention
        out.append((m.start("v"), m.end("v"), "unknown" if m.group("slash") else "person"))
    # the owner segment of an scp-style git remote (git@host:owner/repo.git); the address part is a MAIL token
    for m in re.finditer(r"@[\w.-]+:(?P<owner>[a-z][\w-]{2,})/[\w.-]+", text):
        if not blocked.hit(m.start("owner"), m.end("owner")) and _login_value(m.group("owner")) is not None:
            out.append((m.start("owner"), m.end("owner"), "unknown"))
    return out


_HEADING_RE = re.compile(r"(?m)^#{1,6}[ \t]+(?P<v>[^\n]{2,60})[ \t]*$")


def _subject_candidates(text: str, blocked) -> list[tuple[int, int, str]]:
    """A subject line or a Markdown heading (a sheet name): a lone unknown name-shaped word in it (AW: Pomblet,
    ## Pomblet) is a name or a product, [name]."""
    out = []
    for m in list(_SUBJECT_RE.finditer(text)) + list(_HEADING_RE.finditer(text)):
        words = _value_words(text, m.start("v"), m.end("v"))
        if len(words) != 1:
            continue
        a, b, w = words[0]
        if blocked.hit(a, b) or not _NAME_SHAPE_RE.fullmatch(w) or not w[:1].isupper() or w.isupper() \
                or _known_word(w) or codes.is_code(w) or text[b:b + 1].isdigit() or re.match(r"[ \t]+\d", text[b:b + 4]):
            continue   # Лист1, Sheet2, Slide 1, Welle 2: a word with a number is a label, no name
        out.append((a, b, "person" if w.casefold() in _FIRST_NAMES else "unknown"))
    return out


_LONE_LINE_RE = re.compile(r"(?m)^[ \t]*(?P<v>[^\W\d_][\w'’-]{2,})[ \t]*\n[ \t]*(?P<next>[^\n]{3,80})$")


def _syslog_candidates(text: str, blocked) -> list[tuple[int, int, str]]:
    """The host field and the user of a syslog line (sshd: Accepted password for tpomblet, sudo: tpomblet)."""
    out = []
    for m in _SYSLOG_RE.finditer(text):
        host = m.group("host")
        for part in re.finditer(r"[a-z]{4,}", host.split(".")[0]):
            w = part.group(0)
            if not _known_word(w) and w not in _SYSTEM_USERS:
                a = m.start("host") + part.start()
                if not blocked.hit(a, a + len(w)):
                    out.append((a, a + len(w), "unknown"))
        for u in _SYSLOG_USER_RE.finditer(m.group("rest")):
            v = u.group("v")
            if _login_value(v) is not None:
                a = m.start("rest") + u.start("v")
                if not blocked.hit(a, a + len(v)):
                    out.append((a, a + len(v), "person"))
    return out


_HYPHEN_BREAK_RE = re.compile(r"(?m)^[ \t]*(?P<v>[^\W\d_][\w'’]{2,})-[ \t]*\n[ \t]*(?P<next>[^\n]{3,80})$")


def _hyphen_break_candidates(text: str, blocked) -> list[tuple[int, int, str]]:
    """A word that ends in a hyphen at a line end before a company line: the brand of a hyphenated company broken
    at its hyphen (Nrgtz-\\nBeratung GmbH)."""
    out = []
    for m in _HYPHEN_BREAK_RE.finditer(text):
        w, nxt = m.group("v"), m.group("next")
        if not w[:1].isupper() or _known_word(w) or _generic_word(w) or blocked.hit(m.start("v"), m.end("v")):
            continue
        if _COMPANY_RE.search(nxt):
            out.append((m.start("v"), m.end("v"), "company"))
    return out


def _lone_line_candidates(text: str, blocked) -> list[tuple[int, int, str]]:
    """A lone unknown capitalised word on a line of its own, followed by a company line (a legal form) or a role
    line: the signer of a mail without a closing phrase."""
    out = []
    for m in _LONE_LINE_RE.finditer(text):
        w, nxt = m.group("v"), m.group("next")
        if not w[:1].isupper() or _person_known(w) or _generic_word(w) or blocked.hit(m.start("v"), m.end("v")):
            continue
        words = nxt.split()
        if _COMPANY_RE.search(nxt) or (words and all(_role_word(x) or _known_word(x) for x in words)
                                             and any(_role_word(x) for x in words)):
            out.append((m.start("v"), m.end("v"), "person"))
    return out


_TWO_LINE_NAME_RE = re.compile(r"(?m)^[ \t]*(?P<first>[^\W\d_][\w'’-]{1,})[ \t]*\n(?=[ \t]*(?P<last>[^\W\d_][\w'’-]{2,})[ \t]*$)")
"""The second line is a lookahead, so a heading over the first name does not swallow the pair below it."""


def _two_line_name_candidates(text: str, blocked) -> list[tuple[int, int, str]]:
    """A first name of the list on a line of its own, an unknown capitalised word on the next line: the boxes of
    an org chart, a contact slide, a signature laid out in two paragraphs."""
    out = []
    for m in _TWO_LINE_NAME_RE.finditer(text):
        first, last = m.group("first"), m.group("last")
        if not last[:1].isupper() or _person_known(last) or _generic_word(last) or not _name_shaped(last):
            continue
        if first.casefold() not in _FIRST_NAMES:
            # an unknown capitalised word over another unknown one, a role or company line below: a name box
            nxt = text[m.end():m.end() + 80].split("\n")[1] if text[m.end():m.end() + 80].count("\n") >= 1 else ""
            words = nxt.split()
            if not (first[:1].isupper() and _name_shaped(first) and not _person_known(first) and not _generic_word(first)
                    and words and (_COMPANY_RE.search(nxt) or (all(_role_word(x) or _known_word(x) for x in words)
                                                                      and any(_role_word(x) for x in words)))):
                continue
        if blocked.hit(m.start("first"), m.end("last")):
            continue
        out.append((m.start("first"), m.end("first"), "person"))
        out.append((m.start("last"), m.end("last"), "person"))
    return out


def _lone_first_name_candidates(text: str, blocked) -> list[tuple[int, int, str]]:
    """A first name of the list standing alone in prose (Markus hat die Firewall gebaut), capitalised, not a word
    of the public corpus and not written in lower case elsewhere in the text."""
    out = []
    lows = None
    for m in _LONE_WORD_RE.finditer(text):
        w = m.group("v")
        if not w[:1].isupper() or w.isupper() or w.casefold() not in _FIRST_NAMES or blocked.hit(m.start(), m.end()):
            continue
        if w.casefold() in _UNIT_WORDS or w.casefold() in _ABBREVIATIONS or text[m.end():m.end() + 1] == ".":
            continue   # Max. Bandbreite is an abbreviation
        prev = re.search(r"([^\W\d_][\w'’-]*)[ \t]+$", text[max(0, m.start() - 40):m.start()])
        nxt = re.match(r"[ \t]+([^\W\d_][\w'’-]*)", text[m.end():m.end() + 40])
        if (prev and prev.group(1)[:1].isupper()) or (nxt and nxt.group(1)[:1].isupper()):
            continue   # part of a run of capitalised words, the run rule's business
        if re.match(r"[ \t]+\d", text[m.end():m.end() + 4]):
            continue   # Julia 1.9 is a language, not a person
        if nxt and nxt.group(1).casefold() in _PARTICLE_WORDS | {"of", "del", "da"}:
            continue   # Bill of Materials, Thomas von X: the particle rule decides
        if lows is None:
            lows = set(x.casefold() for x in re.findall(r"(?<![\w.@/-])[a-zäöüß]{3,}(?![\w.@/-])", text))
        if w.casefold() in lows:
            continue
        out.append((m.start("v"), m.end("v"), "person"))
    return out


def _cn_candidates(text: str, blocked) -> list[tuple[int, int, str]]:
    """The common-name parameter of a calendar attendee or organiser (ATTENDEE;CN=Name:mailto:...)."""
    out = []
    for m in _CN_RE.finditer(text):
        span = _person_value(text, m.start("v"), m.end("v"), blocked, allow_lower=True, max_words=4)
        if span:
            out.append((span[0], span[1], "person"))
    return out


_VON_UNKNOWN_RE = re.compile(r"(?<![\w-])(?P<first>[A-ZÄÖÜ][\wäöüß-]{3,})[ \t]+(?:von|vom)[ \t]+(?P<v>[A-ZÄÖÜ][\wäöüß'’-]{3,})(?![\w-])(?![ \t]+[A-ZÄÖÜ])")


def _von_unknown_candidates(text: str, blocked) -> list[tuple[int, int, str]]:
    """"Firewall von Nrgtz", "Ablösung von Qwertz": a known noun, von, then exactly one unknown name-shaped word
    (no vendor, no corpus or list word, no first name, no capitalised word after it): the word is [name]."""
    out = []
    for m in _VON_UNKNOWN_RE.finditer(text):
        first, v = m.group("first"), m.group("v")
        a, b = m.start("v"), m.end("v")
        if not _known_word(first) or blocked.hit(a, b) or _known_word(v) or _generic_word(v) or codes.is_code(v):
            continue
        if v.casefold() in _FIRST_NAMES or not _name_shaped(v) or v.isupper():
            continue
        out.append((a, b, "unknown"))
    return out


_VOCATIVE_RE = re.compile(r"(?im)(?<![\w-])(?:danke|hallo|hi|hey|moin|servus|thanks|thx|merci|спасибо|привет)[ \t]+"
                          r"(?P<v>[A-ZÄÖÜА-ЯЁ][\wäöüßа-яё'’-]{2,})(?=[ \t]*(?:[,.!?;:]|$))")


def _vocative_candidates(text: str, blocked) -> list[tuple[int, int, str]]:
    """"Danke Tobi, dann teste ich": a capitalised unknown word right after thanks or a greeting inside a line,
    before a comma, a full stop or the line end, is the person spoken to (a nickname the learned forms miss)."""
    out = []
    for m in _VOCATIVE_RE.finditer(text):
        v = m.group("v")
        a, b = m.start("v"), m.end("v")
        if blocked.hit(a, b) or _person_known(v) or _generic_word(v) or codes.is_code(v) or v.isupper():
            continue
        out.append((a, b, "person"))
    return out


def _particle_candidates_known(text: str, blocked) -> list[tuple[int, int, str]]:
    """First von Last of the intake, not when the first word is a known word that is no first name (Freigabe von X)
    or the last word is a known word (Peter von Siemens stays a limit)."""
    out = []
    for a, b in _particle_candidates(text, blocked):
        words = [w for _, _, w in _value_words(text, a, b)]
        if words and (_person_known(words[0]) or _generic_word(words[0]) or _known_word(words[-1])
                      or words[0].casefold() in _BY_VERBS_DE or words[0].casefold() in _BY_VERBS_EN):
            continue   # Erstellt von Tobias: the by-line rule takes the person after the particle
        out.append((a, b, "person"))
    return out


PROPOSED_RULES = (
    _firma_candidates,
    _wrap(_company_candidates, "company"),
    _lower_company_candidates,
    _wrap(_label_candidates, "company"),
    _label_value_candidates,
    _table_candidates,
    _fixed_table_candidates,
    _header_candidates,
    _reply_candidates,
    _salutation_candidates,
    _signoff_candidates,
    _speaker_candidates,
    _address_candidates,
    _spaced_after_title_candidates,
    _title_candidates_cut,
    _particle_candidates_known,
    _von_unknown_candidates,
    _vocative_candidates,
    _initial_candidates_known,
    _initials_candidates,
    _cn_candidates,
    _byline_candidates,
    _subject_candidates,
    _wrap(_run_candidates, "unknown"),
    _wrap(_inverted_candidates, "person"),
    _wrap(_spaced_candidates, "unknown"),
    _identifier_run_candidates,
    _two_line_name_candidates,
    _syslog_candidates,
    _inventory_candidates,
    _hyphen_break_candidates,
    _lone_line_candidates,
    _lone_first_name_candidates,
    _login_candidates,
)




def _plausible(text: str, a: int, b: int, cls: str) -> bool:
    """Proposed: a candidate carries a letter; a company label value carries a capitalised word; a "Last, First"
    pair is no person when both halves are known words or one is an acronym."""
    value = text[a:b]
    if not any(c.isalpha() for c in value):
        return False
    words = [w for _, _, w in _value_words(text, a, b)]
    if cls == "company" and not any(w[:1].isupper() for w in words):
        return False
    if cls == "person" and "," in value and len(words) == 2:
        if _person_known(words[0]) or _generic_word(words[0]) or words[0].casefold() in _GREETING_WORDS \
                or all(w.isupper() and len(w) <= 5 for w in words):
            return False
        if any(w.isupper() and len(w) <= 5 for w in words) and not any(w.casefold() in _FIRST_NAMES for w in words):
            return False
    if cls == "company" and len(words) >= 2 and sum(
            1 for w in words if not w[:1].isupper() and w.casefold() not in _COMPANY_JOINERS) >= 1:
        return False   # "Wir wollen eine Landing Zone bis": a sentence after a label is no company
    if cls == "company" and words and words[-1].casefold().strip(".") in _AMBIGUOUS_LEGAL \
            and all(_known_word(w) for w in words[:-1]):
        return False   # "TiB SAS", "Standard SE": a unit or a known word before a form that is also a word
    before = text[:a].rstrip()
    prev = re.split(r"[\s|]+", before)[-1].strip(".,") if before and "\n" not in text[len(before):a] else ""
    # only a capitalised word in front names the entity (T-Systems International GmbH); "from" and "into" are SQL
    # words of the stop list, and "from Nrgtz Beratung GmbH" is a company like any other
    prev = prev.casefold() if prev[:1].isupper() else ""
    if cls == "company" and words and (words[0].casefold().strip(".,") in _SOURCE_PLATFORM_WORDS
                                       or words[0].casefold().strip(".,") in _STOP_SINGLES
                                       or prev in _SOURCE_PLATFORM_WORDS or prev in _STOP_SINGLES):
        return False   # T-Systems International GmbH, Microsoft Deutschland GmbH: the vendor names the entity
    return True


_COMPANY_JOINERS = frozenset(("und", "and", "&", "von", "de", "of", "for", "the", "la", "le", "del", "der", "des",
                              "di", "en", "e", "y", "für", "im", "am", "zu", "zur", "zum", "und", "co", "mbh", "gmbh"))
_GREETING_WORDS = frozenset(("hallo", "hi", "hey", "hello", "dear", "moin", "servus", "привет", "здравствуйте", "danke",
                             "thanks", "thx", "спасибо", "grüße", "gruß", "regards", "cheers", "ciao", "hola", "bonjour",
                             "guten", "good", "liebe", "lieber", "sehr", "geehrte", "geehrter", "уважаемый", "уважаемая"))
_AMBIGUOUS_LEGAL = frozenset(("sas", "se", "ag", "kg", "sa", "oy", "ug", "co", "inc", "plc", "ltd", "mbh"))


_ROLE_GENERIC = frozenset(("data", "cloud", "security", "product", "produkt", "service", "services", "customer", "kunde",
                           "kunden", "technical", "technischer", "technische", "solutions", "solution", "business",
                           "development", "enterprise", "global", "regional", "digital", "program", "programme", "project",
                           "projekt", "portfolio", "strategy", "strategie", "transformation", "migration", "governance",
                           "compliance", "risk", "quality", "qualität", "testing", "test", "support", "key", "account",
                           "sales", "delivery", "operations", "infrastructure", "platform", "plattform", "intern",
                           "internal", "external", "extern", "guest", "gast", "desk", "hotline", "helpdesk", "servicedesk",
                           "center", "centre", "zentrale", "leitstelle", "betrieb", "team", "office", "backoffice",
                           "frontoffice", "member", "mitglied", "staff", "vice", "executive", "senior", "junior",
                           "principal", "associate", "partner", "abteilung", "bereich"))
"""Role words that are also ordinary words of a product or a department: they trim a run but do not make a person."""
_PRONOUNS = frozenset(("my", "mine", "me", "we", "you", "us", "our", "your", "their", "its", "his", "her", "them",
                       "mein", "meine", "dein", "deine", "unser", "unsere", "euer", "eure", "ihr", "ihre", "sein", "seine"))


def _trim_run(text: str, a: int, b: int) -> tuple:
    """A run of capitalised words minus the known words at both ends (a first name of the list stays): "Protokoll
    Abstimmung Nrgtz" leaves Nrgtz, "Projektleiter Pomblet" leaves Pomblet as a person. (start, end, class) or
    (None, None, None) when nothing is left."""
    words = _value_words(text, a, b)
    role = company = False
    left = right = 0
    # a first name of the list in front makes the rest a person's words: the German noun suffix says nothing of a
    # surname (Max_Qwertzung_export, tf-ident-known-first-suffix-surname)
    person = bool(words) and words[0][2].casefold().strip(".,;:") in _FIRST_NAMES and not words[0][2].isupper()

    def known(w: str) -> bool:
        low = w.casefold().strip(".,;:")
        if low in _FIRST_NAMES and not w.isupper():
            return False
        if person:
            return _person_known(w) or _generic_word(w) or low in _PRONOUNS
        return _known_word(w) or _generic_word(w) or low in _PRONOUNS

    while words and known(words[0][2]):
        low = words[0][2].casefold().strip(".,;:")
        role = role or _role_shape(low) or (low in _ROLE_WORDS and low not in _ROLE_GENERIC)
        company = company or low in _GENERIC_COMPANY_WORDS or low in _COMPANY_PART_WORDS
        words = words[1:]
        left += 1
    while words and known(words[-1][2]):
        low = words[-1][2].casefold().strip(".,;:")
        company = company or low in _GENERIC_COMPANY_WORDS or low in _COMPANY_PART_WORDS
        words = words[:-1]
        right += 1
    if not words:
        return None, None, None
    if len(words) == 1:
        w = words[0][2]
        if w.isupper() and len(w) <= 5 or codes.is_code(w):
            return None, None, None   # "CUST FILEID" minus the known word leaves an acronym, not a name
        low = w.casefold().strip(".,;:")
        if left == 0 and right >= 1 and low not in _FIRST_NAMES and not role and not company:
            head = text[:words[0][0]].rstrip(" \t")
            if not head or head.endswith(("\n", ".", "!", "?", ":", ";", "|", ">", "-", "*")):
                return None, None, None   # "Je Mandant", "Statt Backup": a sentence opener nobody listed, not a name
    cls = "person" if role else "unknown"
    return words[0][0], words[-1][1], cls


def _run_rule_exempt(text: str, a: int, b: int) -> bool:
    """Proposed: a run of capitalised words is not a name when every word is known (a first name of the list in
    front is never known), when the run is a known phrase of the corpus, a neutral label or a stop phrase, or when
    a vendor or platform word opens it (past a short acronym): Juniper Mist, HPE Nimble Storage."""
    words = [w for _, _, w in _value_words(text, a, b)]
    if not words:
        return True
    phrase = " ".join(words).casefold()
    if phrase in _PHRASES or phrase in _NEUTRAL_LABELS:
        return True
    if words[0].casefold().strip(".,;:") in _FIRST_NAMES and not words[0].isupper():
        rest = [w.casefold().strip(".,;:") for w in words[1:]]
        if rest and all(w in _CORPUS or w in _SOURCE_PLATFORM_WORDS or w in _UNIT_WORDS or w in _STANDARD_WORDS
                        for w in rest) and not any(w in _FIRST_NAMES for w in rest):
            return True   # Grace Period, Max Pods: a first name that opens a phrase of the corpus
        return False
    if all(_known_word(w) or w.casefold() in _PRONOUNS for w in words):
        return True
    rest = words
    while rest and rest[0].isupper() and len(rest[0]) <= 3:
        rest = rest[1:]
    first = rest[0].casefold().strip(".,;:") if rest else ""
    if first in _SOURCE_PLATFORM_WORDS or first in _STANDARD_WORDS:
        return True   # the vendor names the product: Juniper Mist, Veeam ONE, Sophos Firewall
    return False


_MARKS_RE = re.compile(r"\*{1,3}|</?(?:b|i|u|em|strong|span|font|a|mark|small|sup|sub)\b[^<>\n]{0,80}>")


def _blank_marks(text: str) -> str:
    """The text with Markdown emphasis marks and inline html tags turned into spaces of the same length, so that
    **First** **Last** and <b>First</b> Last read as a run (positions unchanged)."""
    if "*" not in text and "<" not in text:
        return text
    return _MARKS_RE.sub(lambda m: " " * len(m.group(0)), text)


# --------------------------------------------------------------------------- learned forms (proposed)

_PUBLIC_PROVIDERS = frozenset((
    "gmail", "googlemail", "outlook", "hotmail", "live", "msn", "yahoo", "ymail", "icloud", "me", "mac", "aol",
    "gmx", "web", "t-online", "tonline", "freenet", "arcor", "posteo", "mailbox", "protonmail", "proton", "tutanota",
    "yandex", "mail", "rambler", "bk", "list", "inbox", "example", "test", "localhost", "invalid", "github", "gitlab",
    "bitbucket", "microsoft", "google", "amazon", "apple", "facebook", "linkedin", "twitter", "xing", "slack",
    "zoom", "teams", "webex", "atlassian", "notion", "dropbox", "box", "wetransfer", "telekom", "t-systems",
    "otc", "open-telekom-cloud", "opentelekomcloud", "huaweicloud", "huawei", "docs", "wiki", "www", "t-cloud",
    "tcloud", "t-systems", "tsystems", "telekom", "magentacloud", "cloudflare", "cloudflareaccess",
))


def _host_labels(value: str) -> list[str]:
    """The other labels of a host (srv-pomblet-01.nrgtz.example: pomblet), split at dots and hyphens, without
    digits, affixes, known words and short pieces: a surname in a host name."""
    host = value
    if "@" in host:
        host = host.rsplit("@", 1)[1]
    host = re.sub(r"^[a-z]+://", "", host, flags=re.I).split("/")[0].split(":")[0].strip("[]<>.").casefold()
    labels = [l for l in host.split(".") if l]
    if len(labels) < 3 or patterns._allowed_host(host) or labels[-1] in patterns._FILE_SUFFIX_TLDS:
        return []
    out = []
    from awb.matcher import GLUE_AFFIXES
    for label in labels[:-2]:
        for piece in re.split(r"[-_]", label):
            if (_letters(piece) >= 4 and piece.isalpha() and piece not in GLUE_AFFIXES and piece not in _PUBLIC_PROVIDERS
                    and not _known_word(piece) and piece not in _SYSTEM_USERS):
                out.append(piece)
    return out


def _domain_label(value: str) -> str | None:
    """The second-level label of the host of a mail address or URL when it is not a known word, a public
    provider or a platform host; the label without its hyphens is what a company is called in identifiers."""
    host = value
    if "@" in host:
        host = host.rsplit("@", 1)[1]
    host = re.sub(r"^[a-z]+://", "", host, flags=re.I).split("/")[0].split(":")[0].strip("[]<>.")
    labels = [l for l in re.split(r"\.", host.casefold()) if l]
    if len(labels) < 2 or patterns._allowed_host(host) or labels[-1] in patterns._FILE_SUFFIX_TLDS:
        return None   # the platform's own hosts, the documentation hosts and file names name no customer
    # the label before the public suffix (two labels for co.uk, com.au and the like)
    idx = -2
    if len(labels) >= 3 and labels[-2] in ("co", "com", "org", "net", "ac", "gov", "edu") and len(labels[-1]) == 2:
        idx = -3
    label = labels[idx]
    if label in _PUBLIC_PROVIDERS or _letters(label) < 4 or label.startswith("xn--") or "_" in label:
        return None
    if _known_word(label) or all(_known_word(p) or len(p) < 3 for p in label.split("-")):
        return None
    return label


_LATIN_ENDINGS = ("s", "es", "n", "en", "ns", "'s", "’s", "sche", "schen", "scher", "sches", "'sche", "’sche", "in")
_CYRILLIC_RE = re.compile(r"^[А-ЯЁа-яё-]+$")
_LOWER_WORD_BOUND = r"(?<![\w.@/-])%s(?![\w.@/-])"


class _Learned:
    """The run-local register: words of wiped persons and companies, matched like registered forms."""

    def __init__(self):
        self.words: dict[str, tuple[str, tuple[str, str]]] = {}   # casefold word -> (word as seen, token key)
        self.strong: dict[str, bool] = {}   # learned from a person or company shape, never a common word
        self.caps_only: dict[str, bool] = {}   # a dictionary word that is a surname: only when capitalised
        self.version = 0
        self.matcher: Matcher | None = None
        self.short_res: list[tuple[re.Pattern, tuple[str, str], bool]] = []
        self.cyr_res: list[tuple[re.Pattern, tuple[str, str]]] = []
        self.built = -1
        self.texts: list[str] = []

    def learn(self, cls: str, value: str, key: tuple[str, str], unknown_only: bool = False) -> None:
        strong = cls in ("person", "company")
        for raw in re.split(r"[\s/&+]+", value):
            for part in {raw} | set(re.split(r"[-'’.]", raw)):
                part = part.strip(".,;:!?()[]\"'«»„“”`")
                if _letters(part) < 3 or not part[0].isalpha() or not _name_shaped(part):
                    continue
                if part.isupper() and (len(part) <= 3 or cls != "person" and len(part) <= 5) and not value.isupper():
                    continue   # an acronym is no company word; a surname in capitals (ZRBL, QWERTZUIO) is learned
                if codes.is_code(part) or codes.is_project_code(part):
                    continue
                if "-" in part and all(_known_word(x) or _letters(x) < 3 for x in part.split("-")):
                    continue   # T-Systems, t-cloud: known words with a hyphen
                low = part.casefold()
                if low in self.words:
                    continue
                if low in _TITLE_WORDS or low in _PARTICLE_WORDS or low in _NEUTRAL_LABELS or low in _GENERIC_ADDRESSEES:
                    continue
                if low in _REPORT_WORDS or low in _RUN_EXEMPT or low in _GERMAN_FUNCTION:
                    continue   # the words of the reports' own text and the values under product labels are never learned
                caps_only = False
                if cls == "company" and _known_word(part, suffix=False):
                    continue
                if cls != "company" and _person_known(part):
                    if cls == "person" and part[:1].isupper() and not unknown_only and len(value.split()) <= 2:
                        caps_only = True   # Herr Lang, Frau Klein: learned, matched capitalised only
                    else:
                        continue
                if unknown_only and (part[:1].islower() or _letters(part) < 4):
                    continue
                if cls == "company" and low in _GENERIC_COMPANY_WORDS:
                    continue
                self.words[low] = (part, key)
                self.strong[low] = strong
                self.caps_only[low] = caps_only
                self.version += 1

    def _common(self, low: str) -> bool:
        """A Latin word that stands in lower case somewhere in the run's prose is a common word, not a name: not
        a first name of the list, not a word learned in lower case (a chat nick), and not an occurrence after a
        greeting or thanks ("danke tobias") or before a colon (a chat label)."""
        if _CYRILLIC_RE.match(low) or low in _FIRST_NAMES:
            return False
        if self.words.get(low, ("",))[0] == low:
            return False
        rx = re.compile(_LOWER_WORD_BOUND % re.escape(low))
        for t in self.texts:
            for m in rx.finditer(t):
                before = t[max(0, m.start() - 12):m.start()].casefold()
                if re.search(r"(?:hallo|hi|hey|danke|thanks|thx|dear|liebe|lieber|moin|servus|cc|@|an|to|привет|спасибо)[ ,]*$", before):
                    continue
                if t[m.end():m.end() + 1] == ":":
                    continue
                if m.start() >= 2 and t[m.start() - 1] == "(" and t[m.start() - 2].isalnum() and t[m.end():m.end() + 1] == ")":
                    continue   # TODO(pomblet), owner(pomblet): a tag argument, not prose
                return True
        return False

    def build(self) -> None:
        if self.built == self.version:
            return
        self.built = self.version
        pairs: list[tuple[str, str]] = []
        self.short_res, self.cyr_res = [], []
        for low, (word, key) in self.words.items():
            if self._common(low):
                if not self.strong.get(low):
                    continue
                if _known_word(word, suffix=False) or low in _CORPUS or low in _GERMAN_ADJ:
                    self.caps_only[low] = True   # Frau Klein is learned; "zu klein" in the same text stays
                # an unknown strong word (a surname no dictionary knows) matches in any case: "ask pomblet first"
            code = "L:%s:%s" % key
            if _CYRILLIC_RE.match(word):
                low = word.casefold()
                if low.endswith(("ский", "цкий", "ская", "цкая", "ской", "цкой", "ского", "цкого")):
                    stem = word[:-4] if low.endswith(("ского", "цкого")) else word[:-3]
                    ending = r"[а-яё]{2,4}"
                elif len(word) >= 4 and low[-1] in "аеиоуыэюяй":
                    stem = word[:-2] if low.endswith("ия") else word[:-1]
                    ending = r"[а-яё]{0,3}"
                else:
                    stem, ending = word, r"[а-яё]{0,3}"
                if len(stem) >= 3:
                    self.cyr_res.append((re.compile(r"(?<![^\W\d_])%s%s(?![^\W\d_])" % (re.escape(stem), ending),
                                         re.IGNORECASE), key))
                continue
            if _letters(word) >= 5:
                for v in register._variants(word):
                    pairs.append((v, code))
                for ending in _LATIN_ENDINGS:
                    pairs.append((word + ending, code))
            else:
                rx = re.compile(r"(?<![^\W\d])(?P<initial>[a-z])?(?:%s)(?:s|'s|’s)?(?![^\W\d])" % re.escape(word),
                                re.IGNORECASE)
                self.short_res.append((rx, key, word[:1].islower()))
        self.matcher = Matcher(pairs) if pairs else None

    def find(self, text: str, taken, keep=()) -> list[Span]:
        """Every learned form in `text` that `taken` leaves free and no phrase of `keep` covers."""
        self.build()
        if keep:
            kept = [(m.start(), m.end()) for k in keep if keep_key(k)
                    for m in re.finditer(r"(?<![\w])" + r"\s+".join(re.escape(w) for w in keep_key(k).split())
                                         + r"(?![\w])", text, re.IGNORECASE)]
            if kept:
                inner = taken

                def taken(a: int, b: int) -> bool:
                    return inner(a, b) or any(x <= a and b <= y for x, y in kept)
        out: list[Span] = []
        if self.matcher is not None:
            for s in self.matcher.find(text):
                key = s.code.split(":", 2)[1:]
                a, b = s.start, s.end
                matched = text[a:b]
                low_key = re.sub(r"[^\w]", "", matched.casefold())
                if any(self.caps_only.get(w) for w in (low_key, low_key.rstrip("s"))) and not matched[:1].isupper():
                    continue   # klein the adjective stays, Klein the surname goes
                letters = sum(c.isalpha() for c in matched)
                letter_spaced = letters >= 5 and re.fullmatch(r"(?:[^\W\d_][ \t.·-]{1,4}){4,}[^\W\d_]", matched)
                strong = any(self.strong.get(w) for w in (low_key, low_key.rstrip("s")))
                if not re.fullmatch(r"[^\W\d_]+(?:['’-][^\W\d_]+)*", matched) and letters < 8 \
                        and not (letter_spaced and strong):
                    continue   # spread out or broken by marks: only a long or a strong learned form is read that way
                left = text[a - 1] if a > 0 else " "
                right = text[b] if b < len(text) else " "
                if left.isalnum() or right.isalnum():
                    # glued inside a token: an identifier (a digit, a separator or a case change in it) or a long
                    # word; never a short word inside a plain word
                    i, j = a, b
                    while i > 0 and text[i - 1].isalnum():
                        i -= 1
                    while j < len(text) and text[j].isalnum():
                        j += 1
                    token = text[i:j]
                    around = text[max(0, i - 1):j + 1]
                    identifier = (any(c.isdigit() for c in token) or re.search(r"[a-z][A-Z]", token)
                                  or any(c in "_.-@/" for c in around))
                    initial = (i == a - 1 and j == b)   # one letter before the surname: a login (tzrbko)
                    rest = (text[i:a] + text[b:j]).casefold()
                    compound = len(rest) >= 3 and (_known_word(rest) or rest in _GERMAN_KNOWN)   # Nrgtzlösung
                    if not identifier and not initial and not compound and (b - a) < 7:
                        continue
                if not taken(a, b):
                    out.append(Span(a, b, key[0], "L:%s:%s" % (key[0], key[1])))
        for rx, key, any_case in self.short_res:
            for m in rx.finditer(text):
                initial = bool(m.group("initial"))
                if not any_case and not initial and not m.group(0)[:1].isupper():
                    # inside an identifier (a hyphen, an underscore or a digit next to it) any case counts
                    around = text[max(0, m.start() - 1):m.end() + 1]
                    if not re.search(r"[-_\d]", around):
                        continue
                if initial and m.group(0)[:1].isupper() and m.group(0)[1:2].islower():
                    continue   # "Azrbl" capitalised with an initial is another word, not initial plus surname
                if not taken(m.start(), m.end()):
                    out.append(Span(m.start(), m.end(), key[0], "L:%s:%s" % key))
        for rx, key in self.cyr_res:
            for m in rx.finditer(text):
                if not taken(m.start(), m.end()):
                    out.append(Span(m.start(), m.end(), key[0], "L:%s:%s" % key))
        return out

    def skeletons(self) -> set[str]:
        return {skeleton(w) for w, _ in self.words.values() if _letters(w) >= 5 and not self._common(w.casefold())}


_RU_TITLES = frozenset(("г-н", "г-на", "г-ну", "г-ном", "г-не", "г-жа", "г-жи", "г-же", "г-жу", "г-жой", "господин",
                        "господина", "господину", "господином", "господине", "господа", "госпожа", "госпожи", "госпоже",
                        "госпожу", "госпожой", "уважаемый", "уважаемая", "уважаемые", "уважаемого", "уважаемому",
                        "уважаемым", "уважаемой", "уважаемую", "товарищ", "товарища", "тов", "коллега", "коллеге"))
_TITLES_EXTRA = _RU_TITLES | frozenset(("ing", "mag", "dipl-ing", "mx", "sir", "hr", "hrn",
                           "fr", "frl", "fam", "herren", "damen", "med", "rer", "nat", "jur", "phil", "oec", "pol",
                           "habil", "kfm", "kffr", "inform", "wirt", "päd", "theol", "vet", "dent", "pharm", "psych",
                           "ba", "ma", "msc", "bsc", "mba", "llm", "phd", "dipl-kfm", "dipl-inf", "dipl-wirt"))


# --------------------------------------------------------------------------- the vocabulary

_CORPUS = frozenset(_word_lists.load(_word_lists.WORDS_FILE))
"""The capitalised words of the public corpus (rules/known-words.txt from two files on)."""
_PHRASES = frozenset(_word_lists.load(_word_lists.PHRASES_FILE))
_VOCAB = _CORPUS | _GERMAN_KNOWN | _GERMAN_FUNCTION | _EXTRA_KNOWN | _GERMAN_ADJ
_RUN_EXEMPT: set = set()
"""Casefolded words of the values under product labels (Hersteller: Kemp, Tools: Gitea, Zammad) of the run that is
bound now (`bind`)."""
_TITLES_WIPE = _TITLES | _TITLES_EXTRA


# --------------------------------------------------------------------------- one run

WIPE_CLASSES = ("person", "company", "place", "unknown")
TOKEN_TEXT = {"person": "person", "company": "company", "place": "place", "unknown": "name"}
TOKEN_RE = re.compile(r"\[(person|company|place|name)(?: \d+)?\]")
LEARNED = "learned form"
RULE_LABELS = {
    "_firma_candidates": "Firma word or Russian legal form",
    "_company_candidates": "legal form",
    "_lower_company_candidates": "legal form after lower-case words",
    "_label_candidates": "company label",
    "_label_value_candidates": "label",
    "_table_candidates": "table column",
    "_fixed_table_candidates": "fixed-width table column",
    "_header_candidates": "mail header",
    "_reply_candidates": "reply line",
    "_salutation_candidates": "salutation",
    "_signoff_candidates": "sign-off",
    "_speaker_candidates": "speaker label",
    "_address_candidates": "postal address",
    "_spaced_after_title_candidates": "title before spaced letters",
    "_title_candidates_cut": "title",
    "_particle_candidates_known": "particle",
    "_von_unknown_candidates": "known noun von unknown word",
    "_vocative_candidates": "vocative",
    "_initial_candidates_known": "initial",
    "_initials_candidates": "initials",
    "_cn_candidates": "calendar common name",
    "_byline_candidates": "by-line",
    "_subject_candidates": "subject or heading",
    "_run_candidates": "run of capitalised words",
    "_inverted_candidates": "Last, First",
    "_spaced_candidates": "spaced letters",
    "_identifier_run_candidates": "identifier",
    "_two_line_name_candidates": "names over two lines",
    "_syslog_candidates": "syslog line",
    "_inventory_candidates": "inventory or users list",
    "_hyphen_break_candidates": "hyphen at a line end",
    "_lone_line_candidates": "lone line before a company or role line",
    "_lone_first_name_candidates": "lone first name",
    "_login_candidates": "login or handle",
}
"""The rule of each span in words, for the reports: the private one lists every wiped value with it, the public one
counts per class how often each rule fired."""


@dataclass(frozen=True)
class WipeSpan:
    """One candidate of a text: positions in the text, its class and the rule that found it."""

    start: int
    end: int
    cls: str
    rule: str


class WipeState:
    """What one import run knows beyond the register: the tokens it issued, the forms it learned, the words of its
    product labels and the keep list. One per engine; `bind` makes it the one the rules read."""

    def __init__(self, keep: tuple[str, ...] = ()):
        self.tokens: dict[tuple[str, str], str] = {}
        self.numbers: Counter = Counter()
        self.learned = _Learned()
        self.run_exempt: set[str] = set()
        self.keep: tuple[str, ...] = tuple(keep)
        self.sources: dict[str, str] = {}   # learned word (case folded) -> the value it came from, private report


def bind(state: WipeState) -> None:
    """The rules read the product-label words of this run (`_known_word`)."""
    global _RUN_EXEMPT
    _RUN_EXEMPT = state.run_exempt


def _canon(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip().casefold()


def token_for_key(state: WipeState, key: tuple[str, str]) -> str:
    """The token of one value: one number per value and class in the run; a person value that shares a word of four
    letters or more with an earlier person gets that person's number."""
    tok = state.tokens.get(key)
    if tok is None:
        cls, canon = key
        if cls == "person":
            words = {w for w in re.split(r"[\s'’.-]+", canon) if _letters(w) >= 4}
            for (k_cls, k_canon), k_tok in state.tokens.items():
                if k_cls == "person" and words & {w for w in re.split(r"[\s'’.-]+", k_canon) if _letters(w) >= 4}:
                    tok = k_tok
                    break
        if tok is None:
            state.numbers[cls] += 1
            tok = "[%s %d]" % (TOKEN_TEXT.get(cls, "name"), state.numbers[cls])
        state.tokens[key] = tok
    return tok


def token_for(state: WipeState, span: Span, value: str) -> str:
    """The token of a candidate span or a learned form (whose code carries the key of its source)."""
    if span.code and span.code.startswith("L:"):
        _, cls, canon = span.code.split(":", 2)
        return token_for_key(state, (cls, canon))
    return token_for_key(state, (span.cls, _canon(value)))


def candidate_spans(state: WipeState, text: str, known: list[Span]) -> list[Span]:
    """The candidate spans of `text` with their classes and rules (in `form`), without overlaps, keep phrases dropped,
    codes, tokens and product-label values never inside."""
    if not text:
        return []
    bind(state)
    raw = text
    text = _blank_marks(text)
    extra = _token_spans(text) + [Span(m.start("v"), m.end("v"), "token") for m in _PRODUCT_LABEL_RE.finditer(text)]
    blocked = _Mask(len(text), known + _stop_spans(text) + extra)
    found: list[tuple[int, int, int, str]] = []
    for prio, rule in enumerate(PROPOSED_RULES):
        for a, b, cls in rule(text, blocked):
            pieces = [(a, b)]
            spaced = rule.__name__ == "_spaced_candidates"
            if cls == "unknown" and not spaced and re.search(r"[ \t]{3,}", text[a:b]):
                cut = []
                pos = a
                for m in re.finditer(r"[ \t]{3,}", text[a:b]):
                    cut.append((pos, a + m.start()))
                    pos = a + m.end()
                cut.append((pos, b))
                # columns of a pdftotext table: every piece keeps two words, else the gap is just wide spacing
                if all(len(_value_words(text, x, y)) >= 2 for x, y in cut):
                    pieces = cut
            for x, y in pieces:
                if spaced:
                    joined = " ".join(_spaced_words(text[x:y]))
                    if _run_rule_exempt(joined, 0, len(joined)):
                        continue
                    jw = joined.split()
                    cls2 = "person" if jw and jw[0].casefold() in _FIRST_NAMES else cls
                    found.append((prio, x, y, cls2))
                    continue
                if cls == "unknown":
                    if _run_rule_exempt(text, x, y):
                        continue
                    x, y, cls2 = _trim_run(text, x, y)
                    if x is None:
                        continue
                    found.append((prio, x, y, cls2))
                    continue
                found.append((prio, x, y, cls))
    kept: list[tuple[int, int, str, int]] = []
    for prio, a, b, cls in sorted(found):
        if not _plausible(text, a, b, cls):
            continue
        if a < b and all(b <= ka or a >= kb for ka, kb, _, _ in kept):
            kept.append((a, b, cls, prio))
    keep_keys = {keep_key(k) for k in state.keep}
    learned = state.learned.words
    out: list[Span] = []
    text = raw
    for a, b, cls, prio in sorted(kept):
        # a span that starts or ends on a blanked mark is trimmed to the words
        while a < b and not text[a].strip():
            a += 1
        while b > a and not text[b - 1].strip():
            b -= 1
        value = re.sub(r"\s+", " ", text[a:b]).strip()
        if not value or keep_key(value) in keep_keys:
            continue
        if cls == "unknown" and learned:
            classes = {learned[w.casefold()][1][0] for w in value.split() if w.casefold() in learned}
            if "person" in classes:
                cls = "person"
            elif "company" in classes:
                cls = "company"
        if cls == "unknown":
            ws = value.split()
            if 2 <= len(ws) <= 3 and ws[0].casefold().strip(".") in _FIRST_NAMES and all(_name_shaped(w) for w in ws):
                cls = "person"
            elif 2 <= len(ws) <= 3 and any(w.casefold().endswith(("ович", "евич", "овна", "евна", "ична", "инична")) for w in ws):
                cls = "person"
            elif len(ws) >= 2 and ws[-1].casefold() in _GENERIC_COMPANY_WORDS | {"systemhaus", "software", "solutions"}:
                cls = "company"
            elif len(ws) >= 2 and ws[0].casefold() in ("stadt", "landkreis", "gemeinde", "kreis", "land", "bezirk",
                                                      "universität", "hochschule", "klinikum", "sparkasse", "stadtwerke",
                                                      "firma", "fa.", "unternehmen", "verein", "verband", "stiftung"):
                cls = "company"
        out.append(Span(a, b, cls, None, RULE_LABELS.get(PROPOSED_RULES[prio].__name__, "candidate")))
    return out


def wipe_spans(state: WipeState, text: str, names: list[Span], structured: list[Span]) -> list[Span]:
    """The candidates of `text` and the learned forms of the run that stand in it, none over a registered form or a
    piece of structured data."""
    spans = candidate_spans(state, text, names + structured)
    taken_spans = names + structured + spans

    def taken(a: int, b: int) -> bool:
        return any(a < s.end and s.start < b for s in taken_spans)

    return spans + state.learned.find(text, taken, keep=state.keep)


def _person_like_run(value: str) -> bool:
    words = [w for w in value.split() if w.casefold() not in _PARTICLE_WORDS]
    if not 2 <= len(words) <= 3 or not all(_name_shaped(w) for w in words):
        return False
    if any(w.isupper() and len(w) <= 3 or codes.is_code(w) or w.casefold() in _CODE_KIND_WORDS for w in words):
        return False   # CUST FILEID: codes and their kinds are no person; XQARV POMBLET in capitals is
    if words[0].casefold().strip(".") in _FIRST_NAMES and not words[0].isupper():
        return True
    return all(not _known_word(w, suffix=False) and not _generic_word(w) for w in words)


def _learn(state: WipeState, cls: str, value: str, key: tuple[str, str], unknown_only: bool = False) -> None:
    before = set(state.learned.words)
    state.learned.learn(cls, value, key, unknown_only=unknown_only)
    for w in set(state.learned.words) - before:
        state.sources[w] = value


def learn(state: WipeState, text: str, names: list[Span], structured: list[Span]) -> list[Span]:
    """Detection of one text (normalised): the words of its product labels become terms of the run, the values of
    its person and company candidates, its person-like runs, the parts of person-like mail local parts and the
    labels of its mail and URL hosts become learned forms. Returns the candidate spans."""
    bind(state)
    state.learned.texts.append(text)
    for m in _PRODUCT_LABEL_RE.finditer(text):
        state.run_exempt.update(w.casefold() for w in re.findall(r"[^\W\d_][\w'’-]{2,}", m.group("v"))
                                if not codes.is_code(w))
    spans = candidate_spans(state, text, names + structured)
    for s in spans:
        value = text[s.start:s.end]
        if _SPACED_RE.fullmatch(value):
            value = " ".join(_spaced_words(value))   # a letter spaced stretch teaches its joined words
            if _COMPANY_RE.search(value):
                _learn(state, "company", value, ("company", _canon(value)))
                continue
            if s.cls == "unknown" and 2 <= len(value.split()) <= 3 and all(_name_shaped(w) for w in value.split()):
                _learn(state, "unknown", value, ("unknown", _canon(value)))
                continue
        if s.cls in ("person", "company"):
            _learn(state, s.cls, value, (s.cls, _canon(value)))
        elif s.cls == "unknown" and _person_like_run(value):
            # a run that looks like a person (a first name first, or unknown words only): its words are learned
            # with the run's token, so the surname of a full name is wiped where it stands alone later
            _learn(state, "unknown", value, ("unknown", _canon(value)), unknown_only=True)
    # the person-like local parts of mail addresses name the people of a mail chain
    for a, b in _mail_candidates(text, _Mask(len(text), names)):
        local = text[a:b]
        _learn(state, "person", " ".join(p for p in re.split(r"[._-]", local) if p), ("person", _canon(local)))
    # the distinctive label of a mail host names the company behind it; the other labels of a mail or URL host
    for s in structured:
        value = text[s.start:s.end]
        # a mail address, a URL with a scheme or a bare host of three labels or more names a host; a bare
        # "intake.run" of two labels may be code or a file and teaches nothing
        bare_labels = len([l for l in re.sub(r"^[a-z]+://", "", value, flags=re.I).split("/")[0].split("@")[-1]
                           .strip(".").split(".") if l])
        if s.cls == "mail" or (s.cls == "url" and (re.match(r"(?i)[a-z]+://|www\.", value) or bare_labels >= 3)):
            label = _domain_label(value) if s.cls == "mail" and not _generic_local(value) else None
            if label:
                _learn(state, "company", label, ("company", label.casefold()))
            for other in _host_labels(value):
                _learn(state, "unknown", other, ("unknown", other))
    for dc in _DC_RE.finditer(text):
        host = ".".join(x.strip() for x in re.findall(r"(?i)DC=([^,\n]+)", dc.group(0)))
        label = _domain_label("x@" + host) if host.count(".") >= 1 else None
        if label:
            _learn(state, "company", label, ("company", label.casefold()))
    return spans


_CODE_MERGE_RE = re.compile(r"(?<![\w-])(CUST-[A-Z2-7]{4})[ \t]+\[company(?: \d+)?\]")


def merge_customer_code(text: str) -> str:
    """A [company] token right after the customer code is a longer form of the customer: one code. A [name] after
    the code is not merged ("CUST-Q7M4 Technische Zielarchitektur" keeps its heading)."""
    return _CODE_MERGE_RE.sub(r"\1", text)


def learned_skeletons(state: WipeState) -> set[str]:
    """The learned forms for the second check: the letters of every learned word of five letters or more."""
    return state.learned.skeletons()


# --------------------------------------------------------------------------- the interface of the intake

def collect(text: str, known: list[Span], state: WipeState | None = None) -> list[WipeSpan]:
    """The candidates of `text` (positions in `text`) that `known` does not cover, with their class and rule. With
    a `state` its learned forms are found too and the keep list of the state applies."""
    if not text:
        return []
    state = state if state is not None else WipeState()
    names = [s for s in known if s.cls == "name"]
    structured = [s for s in known if s.cls != "name"]
    spans = wipe_spans(state, text, names, structured)
    return sorted((WipeSpan(s.start, s.end, s.cls, s.form or LEARNED) for s in spans), key=lambda w: (w.start, w.end))


def wipe_values(text: str, known: list[Span], keep=()) -> list[str]:
    """The values of `collect`, ordered by position, without duplicates (case aside)."""
    out: list[str] = []
    seen: set[str] = set()
    for w in collect(text, known, WipeState(tuple(keep))):
        value = re.sub(r"\s+", " ", text[w.start:w.end]).strip()
        if value and value.casefold() not in seen:
            seen.add(value.casefold())
            out.append(value)
    return out


def keep_key(phrase: str) -> str:
    """The form under which a phrase and a candidate are compared: normalised, whitespace runs as one space,
    case folded."""
    return " ".join(normalize.normalize(phrase).text.split()).casefold()
