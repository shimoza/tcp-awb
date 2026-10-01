"""The knowledge base: one checked fact per file (INTERFACES.md, Release 2, T-30 to T-36).

The folder is `Paths.kb` (AWB_KB in tests), a git repository of its own:

    entries/KB-XXXX.md   one fact: a header of `key: value` lines, a line `---`, then the statement
    INDEX.md             generated after every add, never edited by hand

Header keys, always in this order:

    id        KB-XXXX (four characters of the code alphabet, drawn at random)
    scope     tcp | hcs
    tags      a comma list from rules/tags.txt
    grade     live | contract | docs | said | assumed
    checked   ISO date of the check
    class     availability (30 days) | api (180 days) | stable (365 days)
    expires   checked plus the days of the class, computed
    source    where the fact comes from, one line
    tried     what was tried to prove a negative, `;`-separated, at least two (only when given)
    retired   ISO date the entry was withdrawn (only when it was)
    why       why it was withdrawn, one line (only when it was)

`add` refuses a text that is not English (Cyrillic letters, or more than 15 % German function words and at
least two of them), that carries a hit of the name check (`check.check_text` with `config.paths().register`,
the vault daemon when the register is not readable here), a register code or a project code, an identifier, a
home path, a secret or a token (the detectors of `awb.gate`), a price (a number with any currency symbol or code,
a rate per hour, or a price word such as billed, cost or fee with a number at most three words after it), a
negative statement without two `--tried` (contracted negations such as isn't and won't count, and so do "not
offered" and "no longer"), an unknown tag, and a statement whose word overlap with an entry of the same scope is
above 0.6 (unless `--force-new`). A code counts also when it is written with a look-alike dash or fullwidth
characters. The statement, the source and every tried text are checked.

`recheck` records a fresh check of an entry whose statement still holds: grade, source and check date become
those of the new check, and a negative needs two tried texts of the new check. `amend` corrects the statement of
an entry and keeps its id: the new statement goes through every check of
`add`, with the tried texts the entry already carries, and the checked date moves only when it is given, because
an amendment corrects the wording and is not a fresh check of the fact. `retire` withdraws an entry: the file
and the id stay, `find`, `scope`, `expired` and INDEX.md stop offering it, `show` still prints it with the date
and the reason, so someone following an id from an old note learns why it went away. A retired entry is no
longer compared for near duplicates, so the corrected fact can be added.

Nothing here prints, logs or raises with a refused value: messages carry field names, classes, counts, ids and
dates. What `find`, `show`, `scope` and `expired` print passed these checks when it was added; it goes through
the name check once more before it is printed, so an entry that carries a name registered later is withheld.
A name check that cannot run stops every command with exit 2: the knowledge base fails closed.

    awb kb add --scope S --tag T... --grade G --class C --source TEXT [--tried TEXT]... [--checked D]
               [--force-new] STATEMENT|-
    awb kb amend [--checked D] [--tried TEXT]... [--force-new] ID STATEMENT|-
    awb kb recheck --grade G --source TEXT [--tried TEXT]... [--checked D] ID
    awb kb bench [--top N] FILE
    awb kb retire --why TEXT ID
    awb kb find [--scope S] WORDS...
    awb kb show ID
    awb kb scope [--scope S] TAG
    awb kb expired
    awb kb index
    awb kb export --out FILE [--scope S] [--grade G]... [--tag T]... [--include-expired] [--force]

Exit codes: 0 ok, 1 refused (add, amend, retire) or expired entries found (expired), 2 usage or error.
"""
from __future__ import annotations

import fcntl
import functools
import json
import math
import os
import re
import secrets
import sys
import tempfile
import unicodedata
from contextlib import contextmanager
from dataclasses import dataclass, replace as _replace
from datetime import date, timedelta
from pathlib import Path
from typing import Iterable, Sequence

from awb import check, codes, config, gate, normalize, patterns, register

SCOPES = ("tcp", "hcs")
SCOPE_FILE = "SCOPE"
"""A file in the root of a knowledge base that names the one scope it holds (tcp or hcs). Without it a base
takes both, as the tests' temporary bases do."""
GRADES = ("live", "contract", "docs", "said", "assumed")
CLASSES = {"availability": 30, "api": 180, "stable": 365}
"""Class of a fact and the days until it has to be checked again."""
KEYS = ("id", "scope", "tags", "grade", "checked", "class", "expires", "source", "tried", "retired", "why")
REQUIRED = ("id", "scope", "tags", "grade", "checked", "class", "expires", "source")
"""The keys every entry file carries. `tried`, `retired` and `why` are written only when they are set, so an
entry written before they existed still parses. Listed by name, not by position in KEYS: a new key must be
decided to be required, never become required by being appended."""
SEPARATOR = "---"
ENTRIES = "entries"
INDEX = "INDEX.md"
TAGS_FILE = patterns.RULES_DIR / "tags.txt"
SYNONYMS_FILE = patterns.RULES_DIR / "kb-synonyms.txt"
SYNONYM_WEIGHT = 0.5
IDF_POWER = 1.5
"""How strongly a rare word of a query outweighs a common one: its weight is (log(N / carriers) + 1) ** IDF_POWER."""
"""A word that `find` adds from a group of rules/kb-synonyms.txt counts half as much as a word of the query."""

SIMILAR_LIMIT = 0.6
"""An entry of the same scope with a word overlap above this refuses the add (unless force_new)."""
SIMILAR_SHOWN = 3
GERMAN_LIMIT = 0.15
"""More than this share of German function words among the words of a text refuses it."""
MIN_TRIED = 2
MAX_STATEMENT = 2000
MAX_LINE = 300

ID_RE = re.compile(r"KB-[A-Z2-7]{4}")
_FILE_RE = re.compile(r"KB-[A-Z2-7]{4}\.md")
_DATE_RE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}")
_TAG_RE = re.compile(r"[a-z0-9][a-z0-9.+_-]{0,47}")

MIN_GERMAN = 2
"""A text is refused as German only with at least this many German function words (a short English statement
with one look-alike word stays English)."""

# words that occur in German sentences and hardly ever in English technical text; words that are English too
# (in, an, am, so, was, will, also, man, war, es, er, da, bin, gut, hat) are left out on purpose
_GERMAN = frozenset("""
    der die das den dem des ein eine einer eines einem einen und oder aber nicht ist sind wird werden wurde
    wurden sein seine ihre haben hatte kann können koennen muss müssen muessen soll sollen darf dürfen
    duerfen mit von für fuer auf bei zu zum zur im ins vom beim als auch noch nur wie wenn dass daß sich ich wir
    sie ihr kein keine keinen keiner nach über ueber unter aus durch gegen ohne um dieser diese dieses diesen
    diesem jede jeder jedes welche welcher welches sehr schon bis dann denn weil ob zwischen wo wer hier dort
    jetzt nun doch ja nein bereits sowie damit jedoch mehr
""".split())
_LETTERS_RE = re.compile(r"[^\W\d_]+")

_NEGATIVE_RE = re.compile(
    r"(?i)\b(?:cannot|can\s+not|unavailable|unsupported|impossible"
    # every contracted negation: can't, won't, isn't, aren't, doesn't, didn't, hasn't, wouldn't ...
    r"|(?:ca|wo|is|are|was|were|does|do|did|has|have|had|would|could|should|must|need)n['’]t"
    r"|not\s+(?:\w+\s+)?(?:supported|possible|available|offered|provided|included|allowed|permitted|enabled"
    r"|released|planned|sold|listed)"
    r"|(?:does|do|did)\s+not\s+(?:exist|support|work)"
    r"|no\s+longer"
    r"|no\s+(?:\S+\s+){0,5}?available)\b"
)

_NUMBER = r"\d(?:[\d.,]*\d)?"
_CURRENCY_SYMBOL = r"[€$£¥₣]"
_CURRENCY_WORD = r"(?:eur|euros?|usd|chf|gbp|jpy|cny|rmb|dollars?|francs?|pounds?|cents?)"
_PRICE_WORD = r"(?:prices?|priced|pricing|costs?|costing|fees?|charged?|charges|billed|paid|tariffs?|discounts?)"
_PRICE_RE = re.compile(
    # a currency (symbol or code, any currency) next to a number
    r"(?i)(?:" + _CURRENCY_SYMBOL + r"|\b" + _CURRENCY_WORD + r"\b)\s?\d"
    r"|" + _NUMBER + r"\s?(?:" + _CURRENCY_SYMBOL + r"|" + _CURRENCY_WORD + r"\b)"
    # a rate per hour
    r"|" + _NUMBER + r"\s?(?:[^\W\d]+\s+){0,2}?(?:per\s+hour\b|/\s?hour\b|/\s?h\b)"
    # a price word with a number at most three words after it: "billed at 4200 per month", "the fee is 12"
    r"|\b" + _PRICE_WORD + r"\b(?:\s+[^\W\d]+){0,3}\s+" + _NUMBER
)

_GATE_CLASSES = ("identifier", "homepath", "secret", "token", "private-key", "blocklist")

_WORD_RE = re.compile(r"[a-z0-9]+(?:[.\-_/][a-z0-9]+)*")
_STOP = frozenset("""
    the a an of to in on for is are be been was were and or with by at as it its this that these those from per
    can via into than then there their has have had which when where also only all any each
""".split())


# Words of a question that say nothing about which entry is wanted: the question words, the pronouns, "do" and the
# name of the platform, which nearly every question carries. Only the query drops them; entries keep every word, so
# the near-duplicate check is not touched. "does" and "can" stay: measured on the blind questions of 2026-09-22,
# they carry a negation or a capability often enough to help.
_QUERY_STOP = frozenset("""
    how what why who whom whose i me my we our us you your please do tcp
""".split())


_PLATFORM_RE = re.compile(r"(?i)\bt[\s-]*cloud[\s-]*public\b|\bopen\s+telekom\s+cloud\b")


@dataclass(frozen=True)
class Entry:
    id: str
    scope: str
    tags: tuple[str, ...]
    grade: str
    checked: str
    cls: str               # the header key is "class"
    expires: str
    source: str
    tried: tuple[str, ...]
    statement: str
    retired: str = ""      # the ISO date it was withdrawn, empty while the entry stands
    why: str = ""          # why it was withdrawn
    path: Path | None = None

    @property
    def negative(self) -> bool:
        return is_negative(self.statement)

    @property
    def is_retired(self) -> bool:
        return bool(self.retired)

    def is_expired(self, today: date | None = None) -> bool:
        return date.fromisoformat(self.expires) < (today or date.today())


class KBError(Exception):
    """The knowledge base cannot do what was asked. The message carries field names, classes, counts, ids and
    dates, never a value that was given or found."""


class Refused(KBError):
    """`add`, `amend` or `retire` refused what was given. `reasons` are messages without values, `similar` the
    most similar entries as (overlap, Entry), best first."""

    def __init__(self, reasons: Sequence[str], similar: Sequence[tuple[float, Entry]] = ()):
        super().__init__("; ".join(reasons))
        self.reasons = list(reasons)
        self.similar = list(similar)


# --------------------------------------------------------------------------- places and rules


def root(where=None) -> Path:
    """The knowledge base folder: `where` as a folder, the kb of a config.Paths, or config.paths().kb."""
    if where is None:
        return config.paths().kb
    if isinstance(where, config.Paths):
        return where.kb
    return Path(where)


def folder_scope(where=None) -> str | None:
    """The scope the knowledge base holds, from its SCOPE file, or None when it names none."""
    try:
        word = (root(where) / SCOPE_FILE).read_text(encoding="utf-8").split()
    except (OSError, UnicodeDecodeError):
        return None
    return word[0] if word and word[0] in SCOPES else None


def known_tags(path: Path | None = None) -> tuple[str, ...]:
    """The controlled tag list of rules/tags.txt, in file order. Fails closed when it cannot be read."""
    f = Path(path) if path is not None else TAGS_FILE
    try:
        text = f.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        raise KBError("rules/tags.txt cannot be read, tags cannot be checked") from None
    out = [line.strip().lower() for line in text.splitlines()]
    return tuple(dict.fromkeys(t for t in out if t and not t.startswith("#")))


def expires_for(checked: str, cls: str) -> str:
    """The expiry date of a fact of class `cls` checked on `checked` (ISO dates)."""
    if cls not in CLASSES:
        raise KBError("class must be one of: %s" % ", ".join(CLASSES))
    return (date.fromisoformat(checked) + timedelta(days=CLASSES[cls])).isoformat()


# --------------------------------------------------------------------------- text checks


def is_negative(text: str) -> bool:
    """True for a statement that says something cannot be done, is not supported or does not exist."""
    return bool(_NEGATIVE_RE.search(normalize.normalize(text).text))


def german_share(text: str) -> tuple[int, int]:
    """(German function words, all words) of `text`."""
    words = [w.lower() for w in _LETTERS_RE.findall(text)]
    return sum(1 for w in words if w in _GERMAN), len(words)


def _cyrillic(text: str) -> bool:
    return any(ord(c) > 0x3FF and "CYRILLIC" in unicodedata.name(c, "") for c in text)


def _count(text: str, norm: str, find) -> int:
    """Hits of `find` (a pattern or a detector) in the text as given or in its normalised form, whichever is more."""
    if isinstance(find, re.Pattern):
        return max(len(find.findall(text)), len(find.findall(norm)))
    return max(len(find(text)), len(find(norm)))


def _n(count: int, singular: str, plural: str | None = None) -> str:
    """"1 entry", "2 entries"."""
    return "%d %s" % (count, singular if count == 1 else (plural or singular + "s"))


def _counts(pairs: Iterable[tuple[str, int]]) -> str:
    return ", ".join("%s %d" % (cls, n) for cls, n in pairs if n)


def _unavailable(err: Exception) -> str:
    if isinstance(err, check.CheckUnavailable):
        return str(err)   # "name check unavailable: vault locked" and the like, no value by design
    return "name check unavailable: the register cannot be read"


def register_of(register_path: Path | None = None) -> Path:
    return Path(register_path) if register_path is not None else config.paths().register


def _require_register(register_path: Path) -> None:
    """Refuse to run on a readable vault folder that holds no register: names could not be checked at all."""
    try:
        kind, _ = check.register_source(register_path)
    except register.RegisterError as err:
        raise KBError(_unavailable(err)) from None
    if kind == check.MISSING:
        raise KBError("no register found, names cannot be checked")


def _name_hits(text: str, register_path: Path) -> list[dict]:
    try:
        return check.check_text(text, register_path)
    except register.RegisterError as err:   # check.CheckUnavailable included
        raise KBError(_unavailable(err)) from None


def text_problems(label: str, text: str, register_path: Path, *, language: bool = True) -> list[str]:
    """Why `text` may not go into the knowledge base, as messages that name `label` and never the text."""
    norm = normalize.normalize(text).text
    out: list[str] = []
    if _cyrillic(norm):
        out.append("%s is not in English (Cyrillic letters)" % label)
    elif language:
        german, total = german_share(norm)
        if german >= MIN_GERMAN and german / total > GERMAN_LIMIT:
            out.append("%s is not in English (%d of %d words are German function words)" % (label, german, total))
    hits = _name_hits(text, register_path)
    if hits:
        by_cls: dict[str, int] = {}
        for h in hits:
            cls = h.get("cls", "unknown") if isinstance(h, dict) else "unknown"
            by_cls[cls] = by_cls.get(cls, 0) + 1
        out.append("%s carries hits of the name check (%s)" % (label, _counts(sorted(by_cls.items()))))
    # a code with a look-alike dash or fullwidth characters inside is still a code (codes.count_codes)
    n_codes = max(sum(codes.count_codes(text)), sum(codes.count_codes(norm)))
    if n_codes:
        out.append("%s carries %s; a fact names no customer and no project"
                   % (label, _n(n_codes, "register or project code")))
    found = [(cls, _count(text, norm, gate.DETECTORS[cls])) for cls in _GATE_CLASSES if cls in gate.DETECTORS]
    if any(n for _, n in found):
        out.append("%s carries an identifier, a home path, a secret or a token (%s)" % (label, _counts(found)))
    n_price = _count(text, norm, _PRICE_RE)
    if n_price:
        out.append("%s carries a price (%d); prices come from the live price API (awb price), never from the "
                   "knowledge base" % (label, n_price))
    return out


# --------------------------------------------------------------------------- similarity


def _stem(w: str) -> str | None:
    """The root of an ordinary English verb or noun form, or None when there is nothing to take off.

    Deliberately small: -ing and -ed off, a doubled final consonant collapsed, and a final -e dropped so
    that "listing", "listed" and "lists" all reach "list" and "create" reaches the same root as "creating".
    It is only used to ADD a form, never to replace one, so an over-eager cut costs recall, not precision.
    """
    if not w.isalpha() or len(w) < 5:
        return None
    for suffix in ("ing", "ed"):
        if w.endswith(suffix) and len(w) - len(suffix) >= 3:
            s = w[:-len(suffix)]
            if len(s) > 3 and s[-1] == s[-2] and s[-1] not in "aeiou":
                s = s[:-1]
            return s
    if w.endswith("e"):
        return w[:-1]
    return None


def words(text: str) -> set[str]:
    """Content words of `text` for the overlap: lower case, compound tokens (s3.large.2, eu-de) and their parts,
    a plural s dropped, common English function words left out.

    A word also brings its stem, so a question asking how to "list" something reaches an entry that calls it
    the "listing". Both forms stay in the set: the exact word still scores, the stem only adds a way in.
    """
    return set(_words(text))


@functools.lru_cache(maxsize=32768)
def _words(text: str) -> frozenset[str]:
    """`words` behind a cache: a search reads every entry, and the same statements come back on every search."""
    out: set[str] = set()
    for tok in _WORD_RE.findall(normalize.normalize(text).text.lower()):
        parts = [tok]
        if re.search(r"[.\-_/]", tok):
            parts += re.split(r"[.\-_/]", tok)
        for w in parts:
            if len(w) > 3 and w.isalpha() and w.endswith("s") and not w.endswith("ss"):
                w = w[:-1]
            if len(w) >= 2 and w not in _STOP:
                out.add(w)
                root = _stem(w)
                if root and len(root) >= 3 and root not in _STOP:
                    out.add(root)
    return frozenset(out)


def overlap(a: set[str], b: set[str]) -> float:
    """Word overlap of two word sets: shared words over all words (0 to 1)."""
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def similar(statement: str, entries: Iterable[Entry], n: int = SIMILAR_SHOWN) -> list[tuple[float, Entry]]:
    """The `n` entries whose statements overlap most with `statement`, best first, overlap above 0 only."""
    q = words(statement)
    scored = [(overlap(q, words(e.statement)), e) for e in entries]
    scored = [s for s in scored if s[0] > 0]
    scored.sort(key=lambda t: (-t[0], t[1].id))
    return scored[:n]


# --------------------------------------------------------------------------- entry files


def render(e: Entry) -> str:
    """The file text of an entry."""
    lines = [
        "id: %s" % e.id,
        "scope: %s" % e.scope,
        "tags: %s" % ", ".join(e.tags),
        "grade: %s" % e.grade,
        "checked: %s" % e.checked,
        "class: %s" % e.cls,
        "expires: %s" % e.expires,
        "source: %s" % e.source,
    ]
    if e.tried:
        lines.append("tried: %s" % "; ".join(e.tried))
    if e.retired:
        lines += ["retired: %s" % e.retired, "why: %s" % e.why]
    return "\n".join(lines) + "\n" + SEPARATOR + "\n" + e.statement + "\n"


def _valid_date(value: str) -> bool:
    if not _DATE_RE.fullmatch(value):
        return False
    try:
        date.fromisoformat(value)
    except ValueError:
        return False
    return True


def parse(text: str, name: str = "entry", path: Path | None = None) -> Entry:
    """An entry from its file text. Strict: KBError with the entry id and a line number, never the content."""
    lines = text.replace("\r\n", "\n").split("\n")
    try:
        sep = lines.index(SEPARATOR)
    except ValueError:
        raise KBError("%s: no line --- between the header and the statement" % name) from None
    fields: dict[str, str] = {}
    at: dict[str, int] = {}
    for i, line in enumerate(lines[:sep], start=1):
        key, colon, value = line.partition(":")
        key = key.strip()
        if not colon or key not in KEYS:
            raise KBError("%s line %d: not a header line with a known key" % (name, i))
        if key in fields:
            raise KBError("%s line %d: the key %s appears twice" % (name, i, key))
        fields[key] = value.strip()
        at[key] = i
    missing = [k for k in REQUIRED if k not in fields]
    if missing:
        raise KBError("%s: missing header keys: %s" % (name, ", ".join(missing)))

    def bad(key: str, why: str) -> KBError:
        return KBError("%s line %d: %s %s" % (name, at[key], key, why))

    if not ID_RE.fullmatch(fields["id"]):
        raise bad("id", "is not a knowledge base id")
    if name != "entry" and fields["id"] != name:
        raise bad("id", "does not match the file name")
    if fields["scope"] not in SCOPES:
        raise bad("scope", "must be one of: %s" % ", ".join(SCOPES))
    if fields["grade"] not in GRADES:
        raise bad("grade", "must be one of: %s" % ", ".join(GRADES))
    if fields["class"] not in CLASSES:
        raise bad("class", "must be one of: %s" % ", ".join(CLASSES))
    for key in ("checked", "expires", "retired"):
        if key in fields and not _valid_date(fields[key]):
            raise bad(key, "is not an ISO date (YYYY-MM-DD)")
    # both keys of a withdrawal or neither: one of them alone is a file somebody edited by hand
    if "why" in fields and not fields["why"]:
        raise bad("why", "is empty")
    if "retired" in fields and "why" not in fields:
        raise bad("retired", "needs the key why, the reason the entry was withdrawn")
    if "why" in fields and "retired" not in fields:
        raise bad("why", "needs the key retired, the date the entry was withdrawn")
    tags = tuple(t.strip() for t in fields["tags"].split(",") if t.strip())
    if not tags or not all(_TAG_RE.fullmatch(t) for t in tags):
        raise bad("tags", "must be a comma list of tags")
    if not fields["source"]:
        raise bad("source", "is empty")
    tried = tuple(t.strip() for t in fields.get("tried", "").split(";") if t.strip())
    statement = "\n".join(lines[sep + 1:]).strip()
    if not statement:
        raise KBError("%s: the statement is empty" % name)
    return Entry(id=fields["id"], scope=fields["scope"], tags=tags, grade=fields["grade"], checked=fields["checked"],
                 cls=fields["class"], expires=fields["expires"], source=fields["source"], tried=tried,
                 statement=statement, retired=fields.get("retired", ""), why=fields.get("why", ""), path=path)


def load(where=None) -> list[Entry]:
    """Every entry of the knowledge base, sorted by id. A missing folder is an empty list. Only files named
    KB-XXXX.md are entries; a malformed one is a KBError (id and line number only)."""
    folder = root(where) / ENTRIES
    try:
        names = sorted(os.listdir(folder))
    except FileNotFoundError:
        return []
    except OSError as err:
        raise KBError("the knowledge base cannot be read (%s)" % type(err).__name__) from None
    out: list[Entry] = []
    for name in names:
        if not _FILE_RE.fullmatch(name):
            continue
        path = folder / name
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as err:
            raise KBError("%s cannot be read (%s)" % (name[:-3], type(err).__name__)) from None
        out.append(parse(text, name[:-3], path))
    return out


def get(kb_id: str, where=None) -> Entry:
    """One entry by id."""
    if not isinstance(kb_id, str) or not ID_RE.fullmatch(kb_id):
        raise KBError("the argument is not a knowledge base id (KB-XXXX)")
    path = root(where) / ENTRIES / (kb_id + ".md")
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        raise KBError("there is no entry %s" % kb_id) from None
    except (OSError, UnicodeDecodeError) as err:
        raise KBError("%s cannot be read (%s)" % (kb_id, type(err).__name__)) from None
    return parse(text, kb_id, path)


def _write_atomic(path: Path, text: str) -> None:
    fd, tmp = tempfile.mkstemp(prefix=".%s." % path.name, suffix=".tmp", dir=path.parent)
    try:
        os.fchmod(fd, 0o640)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


@contextmanager
def _locked(folder: Path):
    """One writer at a time: an exclusive lock on the entries folder itself (no lock file in the repository)."""
    d = folder / ENTRIES
    d.mkdir(parents=True, exist_ok=True)
    fd = os.open(d, os.O_RDONLY | os.O_DIRECTORY)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        os.close(fd)


def _new_id(folder: Path) -> str:
    taken = set(os.listdir(folder / ENTRIES))
    for _ in range(1000):
        kb_id = "KB-" + "".join(secrets.choice(codes.ALPHABET) for _ in range(4))
        if kb_id + ".md" not in taken:
            return kb_id
    raise KBError("could not find a free knowledge base id")


# --------------------------------------------------------------------------- the index


def _one_line(text: str, width: int = 120) -> str:
    s = " ".join(text.split())
    return s if len(s) <= width else s[: width - 3].rstrip() + "..."


def _cell(text: str) -> str:
    return text.replace("|", "\\|")


def render_index(entries: Sequence[Entry], today: date | None = None) -> str:
    today = today or date.today()
    rows = sorted((e for e in entries if not e.is_retired), key=lambda e: (e.scope, e.tags[0] if e.tags else "", e.id))
    out = [
        "# Knowledge base",
        "",
        "Generated by `awb kb` on %s. Do not edit: every `awb kb add` writes this file again." % today.isoformat(),
        "",
        "%s, one file per fact in %s/." % (_n(len(rows), "entry", "entries"), ENTRIES),
    ]
    gone = len(entries) - len(rows)
    if gone:
        out += ["", "%s retired and not listed; the files stay and `awb kb show` still prints them."
                % _n(gone, "entry", "entries")]
    out += [
        "",
        "| id | scope | tags | grade | checked | expires | statement |",
        "|----|-------|------|-------|---------|---------|-----------|",
    ]
    for e in rows:
        out.append("| [%s](%s/%s.md) | %s | %s | %s | %s | %s | %s |" % (
            e.id, ENTRIES, e.id, e.scope, ", ".join(e.tags), e.grade, e.checked, e.expires,
            _cell(_one_line(e.statement, 100))))
    return "\n".join(out) + "\n"


def write_index(where=None, entries: Sequence[Entry] | None = None, today: date | None = None) -> Path:
    """Write INDEX.md from the entries (all of them when not given). Returns its path.

    Reading and writing go under one lock when the entries are not given: a retire that lands between the two
    would otherwise be overwritten and the generated index would keep offering a withdrawn fact as current. A
    caller that already holds the lock hands over its entries, so it never asks for the lock a second time.
    """
    folder = root(where)
    if entries is not None:
        return _index_file(folder, entries, today)
    with _locked(folder):
        return _index_file(folder, load(folder), today)


def _index_file(folder: Path, entries: Sequence[Entry], today: date | None) -> Path:
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / INDEX
    _write_atomic(path, render_index(entries, today))
    return path


# --------------------------------------------------------------------------- add


def _checked(value, today: date) -> str:
    if value is None or value == "":
        return today.isoformat()
    if isinstance(value, date):
        value = value.isoformat()
    if not isinstance(value, str) or not _valid_date(value):
        raise KBError("checked must be an ISO date (YYYY-MM-DD)")
    if date.fromisoformat(value) > today:
        raise KBError("checked must not be in the future")
    return value


def _one_line_field(value, label: str) -> str:
    if not isinstance(value, str):
        raise KBError("%s must be text" % label)
    value = value.strip()
    if not value:
        raise KBError("%s is empty" % label)
    if len(value) > MAX_LINE:
        raise KBError("%s is longer than %d characters" % (label, MAX_LINE))
    if any(unicodedata.category(c) in ("Cc", "Cf", "Zl", "Zp") for c in value):
        raise KBError("%s must be one line without control or invisible characters" % label)
    return value


def _clean_statement(statement) -> str:
    if not isinstance(statement, str):
        raise KBError("the statement must be text")
    statement = statement.replace("\r\n", "\n").replace("\r", "\n").strip()
    if not statement:
        raise KBError("the statement is empty")
    if len(statement) > MAX_STATEMENT:
        raise KBError("the statement is longer than %d characters" % MAX_STATEMENT)
    if any(unicodedata.category(c) in ("Cc", "Cf", "Zl", "Zp") and c not in "\n\t" for c in statement):
        raise KBError("the statement carries control or invisible characters")
    if any(line.strip() == SEPARATOR for line in statement.split("\n")):
        raise KBError("the statement must not carry a line ---")
    return statement


def _negative_problems(statement: str, tried: Sequence[str], hint: str) -> list[str]:
    """Why a negative statement is not backed: it needs MIN_TRIED different tried texts. `hint` names where
    they come from, the option of `add` or the entry an amendment keeps."""
    distinct = len({" ".join(t.lower().split()) for t in tried})
    if is_negative(statement) and distinct < MIN_TRIED:
        return ["the statement is negative and needs %d different %s (what was done to prove it), %d given"
                % (MIN_TRIED, hint, distinct)]
    return []


def _near(statement: str, entries: Iterable[Entry], scope: str, skip_id: str | None = None):
    """The most similar entries of the same scope. A retired entry is left out, so a withdrawn wrong fact does
    not block the corrected one, and so is `skip_id`, so an amendment is not a duplicate of itself."""
    return similar(statement, [e for e in entries
                               if e.scope == scope and not e.is_retired and e.id != skip_id])


def _duplicate_problems(near: Sequence[tuple[float, Entry]], force_new: bool) -> list[str]:
    over = [s for s, _ in near if s > SIMILAR_LIMIT]
    if not over or force_new:
        return []
    return ["the statement is too similar to %s (word overlap above %.1f); "
            "use --force-new when it is a different fact"
            % (_n(len(over), "existing entry", "existing entries"), SIMILAR_LIMIT)]


def _pick(entries: Sequence[Entry], kb_id: str) -> Entry:
    for e in entries:
        if e.id == kb_id:
            return e
    raise KBError("there is no entry %s" % kb_id)


def _clean_tags(tags) -> tuple[list[str], list[str]]:
    """(tags, refusals). Tags are lower case, comma lists split, duplicates dropped, unknown tags refused."""
    if isinstance(tags, str):
        tags = [tags]
    given: list[str] = []
    for t in tags:
        if not isinstance(t, str):
            raise KBError("tags must be text")
        given.extend(p.strip().lower() for p in t.split(",") if p.strip())
    given = list(dict.fromkeys(given))
    if not given:
        raise KBError("give at least one --tag from rules/tags.txt")
    known = set(known_tags())
    refusals = ["tag %d is not in rules/tags.txt" % i for i, t in enumerate(given, start=1) if t not in known]
    return given, refusals


def add(statement: str, *, scope: str, tags: Sequence[str], grade: str, cls: str, source: str,
        tried: Sequence[str] = (), checked: str | date | None = None, force_new: bool = False,
        where=None, register_path: Path | None = None, today: date | None = None) -> Entry:
    """Check a fact and write it as a new entry, then write INDEX.md again. Raises Refused (with the reasons
    and the most similar entries) or KBError. Nothing is written unless every check passed."""
    today = today or date.today()
    if scope not in SCOPES:
        raise KBError("scope must be one of: %s" % ", ".join(SCOPES))
    holds = folder_scope(where)
    if holds and scope != holds:
        raise KBError("this knowledge base holds %s facts only; a fact of scope %s belongs to the %s knowledge base"
                      % (holds, scope, scope))
    if grade not in GRADES:
        raise KBError("grade must be one of: %s" % ", ".join(GRADES))
    if cls not in CLASSES:
        raise KBError("class must be one of: %s" % ", ".join(CLASSES))
    checked = _checked(checked, today)
    statement = _clean_statement(statement)
    source = _one_line_field(source, "the source")
    if isinstance(tried, str):
        tried = [tried]
    tried = [_one_line_field(t, "tried %d" % i) for i, t in enumerate(tried, start=1)]
    for i, t in enumerate(tried, start=1):
        if ";" in t:
            raise KBError("tried %d must not carry a semicolon (it separates the tried texts)" % i)
    tag_list, reasons = _clean_tags(tags)

    reg = register_of(register_path)
    _require_register(reg)
    reasons += text_problems("the statement", statement, reg)
    reasons += text_problems("the source", source, reg, language=False)
    for i, t in enumerate(tried, start=1):
        reasons += text_problems("tried %d" % i, t, reg)
    reasons += _negative_problems(statement, tried, "--tried")

    folder = root(where)

    def judge(entries: list[Entry]) -> tuple[list[tuple[float, Entry]], list[str]]:
        near = _near(statement, entries, scope)
        return near, reasons + _duplicate_problems(near, force_new)

    if reasons:   # refused already: nothing is created, not even the folder
        near, out = judge(load(folder))
        raise Refused(out, near)
    with _locked(folder):
        entries = load(folder)
        near, out = judge(entries)
        if out:
            raise Refused(out, near)
        kb_id = _new_id(folder)
        path = folder / ENTRIES / (kb_id + ".md")
        entry = Entry(id=kb_id, scope=scope, tags=tuple(tag_list), grade=grade, checked=checked, cls=cls,
                      expires=expires_for(checked, cls), source=source, tried=tuple(tried), statement=statement,
                      path=path)
        _write_atomic(path, render(entry))
        write_index(folder, entries + [entry], today)
    return entry


# --------------------------------------------------------------------------- amend and retire


def _save(folder: Path, entries: Sequence[Entry], entry: Entry, today: date) -> Entry:
    """Write one changed entry and INDEX.md again. Called under `_locked(folder)`."""
    _write_atomic(folder / ENTRIES / (entry.id + ".md"), render(entry))
    write_index(folder, [entry if e.id == entry.id else e for e in entries], today)
    return entry


def amend(kb_id: str, statement: str, *, checked: str | date | None = None, force_new: bool = False,
          tried: Sequence[str] | None = None, where=None, register_path: Path | None = None,
          today: date | None = None) -> Entry:
    """Correct the statement of an entry, keeping its id, its scope, its tags, its grade, its class, its source
    and its tried texts. The new statement goes through every check of `add`, a negative against the tried texts
    the entry already carries, or against `tried` when given: those replace the old ones, because they prove the
    corrected statement. `checked` moves only when a date is given, because an amendment corrects the wording and
    is not a fresh check of the fact; an empty one is refused, never read as today; `expires` follows it. Raises
    Refused or KBError. Nothing is written unless every check passed."""
    today = today or date.today()
    folder = root(where)
    get(kb_id, folder)   # the id, the file and its form, before anything is locked or written
    statement = _clean_statement(statement)
    new_tried: list[str] | None = None
    if tried is not None:
        new_tried = [_one_line_field(t, "tried %d" % i) for i, t in enumerate([tried] if isinstance(tried, str)
                                                                                else tried, start=1)]
        for i, t in enumerate(new_tried, start=1):
            if ";" in t:
                raise KBError("tried %d must not carry a semicolon (it separates the tried texts)" % i)
    if checked is not None and not str(checked).strip():
        # a shell variable that did not expand arrives here as "", and `_checked` reads that as today: the fact
        # would leave `awb kb expired` with a full class of freshness nobody checked. It fails loudly instead.
        raise KBError("checked is empty; leave it out to keep the date of the entry")
    when = None if checked is None else _checked(checked, today)
    reg = register_of(register_path)
    _require_register(reg)
    problems = text_problems("the statement", statement, reg)
    for i, t in enumerate(new_tried or [], start=1):
        problems += text_problems("tried %d" % i, t, reg)
    # the rest of the checks read the entry, so they run under the lock and on what is on disk now
    with _locked(folder):
        entries = load(folder)
        old = _pick(entries, kb_id)
        near = _near(statement, entries, old.scope, skip_id=kb_id)
        reasons: list[str] = []
        if old.is_retired:
            reasons.append("%s is retired (retired %s); add the corrected fact as a new entry"
                           % (kb_id, old.retired))
        reasons += problems
        if new_tried is None:
            reasons += _negative_problems(statement, old.tried, "tried texts of the entry")
        else:
            reasons += _negative_problems(statement, new_tried, "--tried")
        reasons += _duplicate_problems(near, force_new)
        if reasons:
            raise Refused(reasons, near)
        at = when or old.checked
        return _save(folder, entries, _replace(old, statement=statement, checked=at,
                                               tried=old.tried if new_tried is None else tuple(new_tried),
                                               expires=expires_for(at, old.cls)), today)


def recheck(kb_id: str, *, grade: str, source: str, tried: Sequence[str] = (), checked: str | date | None = None,
            where=None, register_path: Path | None = None, today: date | None = None) -> Entry:
    """Record a fresh check of an entry whose statement still holds. The statement, scope, tags and class stay;
    the grade, the source and the check date become those of the new check, `expires` follows. A negative needs
    two tried texts of the new check, because the old ones proved it on another day. `checked` defaults to today:
    a recheck is a check. Raises Refused or KBError. Nothing is written unless every check passed."""
    today = today or date.today()
    folder = root(where)
    get(kb_id, folder)
    if grade not in GRADES:
        raise KBError("grade must be one of: %s" % ", ".join(GRADES))
    if checked is not None and not str(checked).strip():
        raise KBError("checked is empty; leave it out for today")
    when = _checked(checked, today)
    source = _one_line_field(source, "the source")
    if isinstance(tried, str):
        tried = [tried]
    tried = [_one_line_field(t, "tried %d" % i) for i, t in enumerate(tried, start=1)]
    for i, t in enumerate(tried, start=1):
        if ";" in t:
            raise KBError("tried %d must not carry a semicolon (it separates the tried texts)" % i)
    reg = register_of(register_path)
    _require_register(reg)
    problems = text_problems("the source", source, reg, language=False)
    for i, t in enumerate(tried, start=1):
        problems += text_problems("tried %d" % i, t, reg)
    with _locked(folder):
        entries = load(folder)
        old = _pick(entries, kb_id)
        reasons: list[str] = []
        if old.is_retired:
            reasons.append("%s is retired (retired %s); add the fact again as a new entry" % (kb_id, old.retired))
        reasons += problems
        reasons += _negative_problems(old.statement, tried, "--tried of the new check")
        if reasons:
            raise Refused(reasons)
        return _save(folder, entries, _replace(old, grade=grade, source=source,
                                               tried=tuple(tried) if tried else old.tried, checked=when,
                                               expires=expires_for(when, old.cls)), today)


def retire(kb_id: str, why: str, *, where=None, register_path: Path | None = None,
           today: date | None = None) -> Entry:
    """Withdraw an entry: the file, its history and its id stay, the readers stop offering it. The reason goes
    through the same text checks as a statement, so a withdrawal cannot smuggle in a name, a code or a path.
    Raises Refused or KBError."""
    today = today or date.today()
    folder = root(where)
    get(kb_id, folder)
    why = _one_line_field(why, "the reason")
    reg = register_of(register_path)
    _require_register(reg)
    problems = text_problems("the reason", why, reg)
    with _locked(folder):
        entries = load(folder)
        old = _pick(entries, kb_id)
        reasons: list[str] = []
        if old.is_retired:
            reasons.append("%s is retired already (retired %s)" % (kb_id, old.retired))
        reasons += problems
        if reasons:
            raise Refused(reasons)
        return _save(folder, entries, _replace(old, retired=today.isoformat(), why=why), today)


# --------------------------------------------------------------------------- reading


# --------------------------------------------------------------------------- verify: the checks of add over entry files


def verify_text(text: str, name: str, register_path: Path) -> list[str]:
    """Why an entry file would not pass `kb add`: its form, its fields, the expiry its class gives, its tags and
    every check of its statement, source, tried texts and withdrawal reason. `name` is the file name. The
    near-duplicate scan is left out: it guards a new fact, not the ones already in."""
    base = Path(name).name
    kb_id = base[:-3] if _FILE_RE.fullmatch(base) else "entry"
    try:
        e = parse(text, kb_id)
        _clean_statement(e.statement)
    except KBError as err:
        return [str(err)]
    out: list[str] = []
    try:
        out += _clean_tags(list(e.tags))[1]
    except KBError as err:
        out.append(str(err))
    if e.expires != expires_for(e.checked, e.cls):
        out.append("expires does not follow checked and class (%s gives %d days)" % (e.cls, CLASSES[e.cls]))
    out += text_problems("the statement", e.statement, register_path)
    out += text_problems("the source", e.source, register_path, language=False)
    for i, tried in enumerate(e.tried, start=1):
        out += text_problems("tried %d" % i, tried, register_path)
    out += _negative_problems(e.statement, e.tried, "tried texts")
    if e.why:
        out += text_problems("the reason why", e.why, register_path)
    return out


def _staged_deletions(repo: Path) -> list[str]:
    from awb import gate
    top = gate._toplevel(Path(repo))
    out = gate._git(top, "diff", "--cached", "--name-only", "-z", "--no-renames", "--diff-filter=D")
    return [os.fsdecode(x) for x in out.split(b"\0") if x]


def verify(files: Sequence[Path] = (), *, staged_repo: Path | None = None,
           register_path: Path | None = None) -> list[tuple[str, str]]:
    """(file, reason) for every entry file that would not pass `kb add`: the files given and, with
    `staged_repo`, every entry a commit of that repository carries. A staged deletion of an entry is a reason
    too: a wrong fact is withdrawn with `awb kb retire`, which keeps its file, its history and its id.
    Raises KBError when the name check cannot run."""
    reg = register_of(register_path)
    _require_register(reg)
    items: list[tuple[str, str | None]] = []
    for f in files:
        try:
            items.append((str(f), Path(f).read_text(encoding="utf-8")))
        except (OSError, UnicodeDecodeError):
            items.append((str(f), None))
    problems: list[tuple[str, str]] = []
    if staged_repo is not None:
        from awb import gate
        try:
            staged = gate.staged_blobs(staged_repo)
            deleted = _staged_deletions(staged_repo)
        except gate.GateError as err:
            raise KBError(str(err)) from None
        for rel, data in staged:
            if rel.startswith(ENTRIES + "/") and rel.endswith(".md"):
                items.append((rel, data.decode("utf-8", "replace") if data is not None else None))
        for rel in deleted:
            if rel.startswith(ENTRIES + "/") and rel.endswith(".md"):
                problems.append((rel, "the entry is deleted; withdraw a wrong fact with awb kb retire instead"))
    for label, text in items:
        if text is None:
            problems.append((label, "the file cannot be read as text"))
            continue
        problems.extend((label, why) for why in verify_text(text, Path(label).name, reg))
    return problems


def find(query: str | Sequence[str], where=None, scope: str | None = None) -> list[tuple[float, Entry]]:
    """Entries ranked by the share of the query an entry carries, each query word weighted by how rare it is
    in the base, then by the overlap of both word sets (the more focused entry first), then the newer check.
    Entries without a shared word are left out.

    A word that half the base uses says almost nothing about which entry is wanted, and a plain count of
    shared words lets it outvote the one word that does. At 77 entries that hardly showed; at 1356 it
    decided the ranking, and questions whose answer was present came back behind entries that merely shared
    "vpc" and "api". Each word is weighted by log(N / entries carrying it), so the rare word wins.
    """
    text = query if isinstance(query, str) else " ".join(query)
    text = _PLATFORM_RE.sub(" ", text)   # the platform's long names say nothing; "public ip" and "cloud eye" do
    q = words(text)
    # one unit per word of the query: the word and its stem count once, never twice ("instance" and "instanc")
    units = [u for u in concepts(text) if not u <= _QUERY_STOP] or concepts(text)
    if not units:
        return []
    q = set().union(*units)            # the tie-break below weighs the same words the score counts
    other = expand(q, synonym_groups())
    entries = [e for e in load(where) if not e.is_retired and (scope is None or e.scope == scope)]
    sets = [(e, words(e.statement) | words(" ".join(e.tags))) for e in entries]
    n = len(sets) or 1
    weight = [(math.log(n / (1 + sum(1 for _, ew in sets if u & ew))) + 1.0) ** IDF_POWER for u in units]
    # a term of a group counts only when an entry carries all its words, and a group credits an entry once
    term_seen = {t: sum(1 for _, ew in sets if t <= ew) for terms in other for t in terms}
    term_weight = {t: SYNONYM_WEIGHT * (math.log(n / (1 + c)) + 1.0) for t, c in term_seen.items()}
    total = sum(weight) or 1.0
    ranked: list[tuple[float, float, int, str, Entry]] = []
    for e, ew in sets:
        got = sum(w for u, w in zip(units, weight) if u & ew)
        via = sum(max((term_weight[t] for t in terms if t <= ew), default=0.0) for terms in other)
        if got or via:
            ranked.append(((got + via) / total, overlap(q, ew), date.fromisoformat(e.checked).toordinal(), e.id, e))
    ranked.sort(key=lambda t: (-t[0], -t[1], -t[2], t[3]))
    return [(min(t[0], 1.0), t[4]) for t in ranked]


def concepts(text: str) -> list[frozenset[str]]:
    """The content words of `text` as units: each word with its stem, and units that share a form merged, so that
    "listing" and "list" in one query are one unit. Built like `words`, which is the union of all units."""
    units: list[set[str]] = []
    for tok in _WORD_RE.findall(normalize.normalize(text).text.lower()):
        parts = [tok]
        if re.search(r"[.\-_/]", tok):
            parts += re.split(r"[.\-_/]", tok)
        for w in parts:
            if len(w) > 3 and w.isalpha() and w.endswith("s") and not w.endswith("ss"):
                w = w[:-1]
            if len(w) < 2 or w in _STOP:
                continue
            forms = {w}
            root = _stem(w)
            if root and len(root) >= 3 and root not in _STOP:
                forms.add(root)
            joined = [u for u in units if u & forms]
            for u in joined:
                forms |= u
                units.remove(u)
            units.append(forms)
    return [frozenset(u) for u in units]


def bench(questions: Sequence[dict], where=None, top: int = 5) -> dict:
    """How well `find` answers questions whose answering entries are known: {"answerable", "hit1", "hit_top",
    "top", "misses"} where misses are the positions of the questions whose entries are not in the first `top`.
    A question counts only while one of its accepted entries stands, so a retirement never reads as a miss."""
    live = {e.id for e in load(where) if not e.is_retired}
    n = hit1 = hit_top = 0
    misses: list[int] = []
    for i, q in enumerate(questions):
        accept = [a for a in (q.get("accept") or []) if a in live]
        if not accept or not isinstance(q.get("question"), str):
            continue
        n += 1
        ranked = [e.id for _, e in find(q["question"], where)[:top]]
        rank = next((k + 1 for k, x in enumerate(ranked) if x in accept), 0)
        hit1 += rank == 1
        hit_top += rank > 0
        if not rank:
            misses.append(i)
    return {"answerable": n, "hit1": hit1, "hit_top": hit_top, "top": top, "misses": misses}


def load_bench(path: Path) -> list[dict]:
    """A question file: a JSON list of {"question", "accept": [ids]}, or an object that holds it under
    "questions". Raises KBError when it is neither."""
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError) as err:
        raise KBError("the question file cannot be read (%s)" % type(err).__name__) from None
    questions = data.get("questions") if isinstance(data, dict) else data
    if not isinstance(questions, list) or not all(isinstance(q, dict) for q in questions):
        raise KBError("the question file holds no list of questions")
    return questions


def synonym_groups(path: Path | None = None) -> list[list[frozenset[str]]]:
    """The groups of rules/kb-synonyms.txt, each a list of terms as word sets. No file, no group: the search
    works without them."""
    f = Path(path) if path is not None else SYNONYMS_FILE
    try:
        text = f.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return []
    groups = []
    for line in text.splitlines():
        line = line.split("#", 1)[0].strip()
        terms = [frozenset(words(t)) for t in line.split(",")] if line else []
        terms = [t for t in terms if t]
        if len(terms) >= 2:
            groups.append(terms)
    return groups


def expand(q: set[str], groups: Sequence[Sequence[frozenset[str]]]) -> list[list[frozenset[str]]]:
    """For every group in which `q` carries one term in full, the other terms of that group (the ones `q` does not
    carry already)."""
    out = []
    for terms in groups:
        if any(t <= q for t in terms):
            rest = [t for t in terms if not t <= q]
            if rest:
                out.append(rest)
    return out


def expired(where=None, today: date | None = None) -> list[Entry]:
    """Entries past their expiry date, the oldest expiry first. `where` may be a config.Paths or a folder."""
    today = today or date.today()
    return sorted((e for e in load(where) if not e.is_retired and e.is_expired(today)),
                  key=lambda e: (e.expires, e.id))


def screen(entries: Sequence[Entry], register_path: Path | None = None) -> tuple[list[Entry], int]:
    """(entries that pass the name check now, count withheld). An entry that carries a name registered after it
    was added is withheld. Raises KBError when the name check cannot run."""
    entries = list(entries)
    if not entries:
        return [], 0
    reg = register_of(register_path)
    _require_register(reg)

    def dirty(group: list[Entry]) -> bool:
        # names only: structured data passed the checks of add, and a source URL must not send every search
        # into one check per entry
        hits = _name_hits("\n\n".join(_entry_text(e) for e in group), reg)
        return any(isinstance(h, dict) and h.get("cls") == "name" for h in hits)

    def clean(group: list[Entry]) -> list[Entry]:
        # halves instead of one check per entry: a few requests for a long list, so a broad search never runs
        # into the rate limit of the vault daemon
        if not dirty(group):
            return group
        if len(group) == 1:
            return []
        mid = len(group) // 2
        return clean(group[:mid]) + clean(group[mid:])

    shown = clean(entries)
    return shown, len(entries) - len(shown)


EXPORT_GRADES = ("live", "contract", "docs")
"""What an export carries unless --grade says otherwise: a statement of the product team (said) may be
confidential and needs a clearance before it leaves; an assumption (assumed) is a lead, not a fact."""


def export_record(e: Entry, today: date) -> dict:
    return {"id": e.id, "scope": e.scope, "statement": e.statement, "grade": e.grade, "checked": e.checked,
            "expires": e.expires, "expired": e.is_expired(today), "class": e.cls, "tags": list(e.tags),
            "source": e.source, "negative": e.negative, "tried": list(e.tried)}


def export(where=None, *, scope: str | None = "tcp", grades: Sequence[str] = EXPORT_GRADES,
           tags: Sequence[str] = (), include_expired: bool = False, today: date | None = None,
           register_path: Path | None = None) -> tuple[list[dict], dict[str, int]]:
    """The entries fit to leave as a dataset, as records, and the counts of what was left out and why. Retired
    entries never leave; expired ones only when asked; every entry passes the name check again first."""
    today = today or date.today()
    bad = [g for g in grades if g not in GRADES]
    if bad:
        raise KBError("a grade is one of %s" % ", ".join(GRADES))
    left: dict[str, int] = {"retired": 0, "other scope": 0, "grade": 0, "tag": 0, "expired": 0, "withheld": 0}
    keep: list[Entry] = []
    for e in load(where):
        if e.is_retired:
            left["retired"] += 1
        elif scope and e.scope != scope:
            left["other scope"] += 1
        elif e.grade not in grades:
            left["grade"] += 1
        elif tags and not set(tags) & set(e.tags):
            left["tag"] += 1
        elif e.is_expired(today) and not include_expired:
            left["expired"] += 1
        else:
            keep.append(e)
    shown, withheld = screen(keep, register_path)
    left["withheld"] = withheld
    return [export_record(e, today) for e in sorted(shown, key=lambda x: x.id)], left


def _entry_text(e: Entry) -> str:
    return "\n".join([e.statement, e.source, e.why, *e.tried])


def _withheld_line(n: int) -> str:
    return ("%s withheld: %s hits of the name check now, fix by hand in %s/"
            % (_n(n, "entry", "entries"), "it carries" if n == 1 else "they carry", ENTRIES))


def render_briefing(tag: str, entries: Sequence[Entry], today: date | None = None, withheld: int = 0) -> str:
    """The Markdown briefing of one tag: facts by grade, negatives with what was tried, expired entries."""
    today = today or date.today()
    live = [e for e in entries if not e.is_expired(today)]
    facts = [e for e in live if not e.negative]
    negatives = [e for e in live if e.negative]
    old = sorted((e for e in entries if e.is_expired(today)), key=lambda e: (e.expires, e.id))
    out = [
        "# Knowledge briefing: %s" % tag,
        "",
        "%s tagged %s on %s: %s, %s, %d expired."
        % (_n(len(entries), "entry", "entries"), tag, today.isoformat(), _n(len(facts), "fact"),
           _n(len(negatives), "negative"), len(old)),
    ]
    if withheld:
        out += ["", _withheld_line(withheld) + "."]
    out += ["", "## Facts by grade"]
    if not facts:
        out += ["", "none"]
    for grade in GRADES:
        group = sorted((e for e in facts if e.grade == grade), key=lambda e: (e.checked, e.id), reverse=True)
        if not group:
            continue
        out += ["", "### %s" % grade, ""]
        for e in group:
            out.append("- %s (%s, checked %s, expires %s): %s" % (e.id, e.scope, e.checked, e.expires,
                                                                 _one_line(e.statement, 600)))
            out.append("  source: %s" % e.source)
    out += ["", "## Negatives and what was tried"]
    if not negatives:
        out += ["", "none"]
    else:
        out.append("")
        for e in sorted(negatives, key=lambda e: (GRADES.index(e.grade), e.id)):
            out.append("- %s (%s, grade %s, checked %s, expires %s): %s" % (
                e.id, e.scope, e.grade, e.checked, e.expires, _one_line(e.statement, 600)))
            out += ["  - tried: %s" % t for t in e.tried]
    out += ["", "## Expired entries"]
    if not old:
        out += ["", "none"]
    else:
        out.append("")
        for e in old:
            out.append("- %s (%s, grade %s, checked %s, expired %s): %s" % (
                e.id, e.scope, e.grade, e.checked, e.expires, _one_line(e.statement, 600)))
            out += ["  - tried: %s" % t for t in e.tried]
    return "\n".join(out) + "\n"


def briefing(tag: str, where=None, *, scope: str | None = None, today: date | None = None,
             register_path: Path | None = None) -> str:
    """`awb kb scope TAG`: the briefing of every entry with the tag (of one scope when given), name-checked."""
    tag = (tag or "").strip().lower()
    if tag not in known_tags():
        raise KBError("the tag is not in rules/tags.txt")
    if scope is not None and scope not in SCOPES:
        raise KBError("scope must be one of: %s" % ", ".join(SCOPES))
    chosen = [e for e in load(where)
              if tag in e.tags and not e.is_retired and (scope is None or e.scope == scope)]
    shown, withheld = screen(chosen, register_path)
    return render_briefing(tag, shown, today, withheld)


# --------------------------------------------------------------------------- command line


def _list_line(e: Entry, today: date, score: float | None = None) -> str:
    parts = [e.id]
    if score is not None:
        parts.append("%.2f" % score)
    parts += [e.scope, e.grade, "checked %s" % e.checked]
    if e.is_expired(today):
        parts.append("EXPIRED")
    parts.append(_one_line(e.statement))
    return "  ".join(parts)


def _print_similar(near: Sequence[tuple[float, Entry]], reg: Path, today: date) -> None:
    if not near:
        return
    shown, withheld = screen([e for _, e in near], reg)
    keep = {e.id for e in shown}
    print("most similar entries:")
    for score, e in near:
        if e.id in keep:
            print("  " + _list_line(e, today, score))
    if withheld:
        print("  " + _withheld_line(withheld))


def _statement_of(parts: Sequence[str]) -> str:
    text = " ".join(parts)
    return sys.stdin.read() if text == "-" else text


def _refused(command: str, ref: Refused, reg: Path, today: date) -> int:
    for reason in ref.reasons:
        print("awb kb %s: refused: %s" % (command, reason), file=sys.stderr)
    _print_similar(ref.similar, reg, today)
    return 1


def _cmd_add(args, today: date) -> int:
    reg = register_of()
    try:
        entry = add(_statement_of(args.statement), scope=args.scope, tags=args.tag, grade=args.grade, cls=args.cls,
                    source=args.source, tried=args.tried, checked=args.checked, force_new=args.force_new,
                    today=today)
    except Refused as ref:
        return _refused("add", ref, reg, today)
    print("added %s (scope %s, grade %s, class %s, expires %s)" % (entry.id, entry.scope, entry.grade, entry.cls,
                                                                   entry.expires))
    _print_similar(_near(entry.statement, load(), entry.scope, skip_id=entry.id), reg, today)
    return 0


def _cmd_amend(args, today: date) -> int:
    reg = register_of()
    try:
        entry = amend(args.id, _statement_of(args.statement), checked=args.checked, force_new=args.force_new,
                      tried=args.tried, today=today)
    except Refused as ref:
        return _refused("amend", ref, reg, today)
    print("amended %s: the statement is replaced (checked %s, expires %s)" % (entry.id, entry.checked,
                                                                              entry.expires))
    _print_similar(_near(entry.statement, load(), entry.scope, skip_id=entry.id), reg, today)
    return 0


def _cmd_recheck(args, today: date) -> int:
    reg = register_of()
    try:
        entry = recheck(args.id, grade=args.grade, source=args.source, tried=args.tried, checked=args.checked,
                        today=today)
    except Refused as ref:
        return _refused("recheck", ref, reg, today)
    print("rechecked %s: grade %s, checked %s, expires %s" % (entry.id, entry.grade, entry.checked, entry.expires))
    return 0


def _cmd_bench(args, today: date) -> int:
    questions = load_bench(args.file)
    r = bench(questions, top=args.top)
    n = r["answerable"] or 1
    print("awb kb bench: %d answerable question(s), the answering entry first for %d (%d%%), in the first %d for %d "
          "(%d%%)" % (r["answerable"], r["hit1"], round(100 * r["hit1"] / n), r["top"], r["hit_top"],
                      round(100 * r["hit_top"] / n)))
    for i in r["misses"]:
        print("  missed #%d: %s" % (i, " ".join(questions[i]["question"].split())[:110]))
    return 0


def _cmd_retire(args, today: date) -> int:
    reg = register_of()
    try:
        entry = retire(args.id, args.why, today=today)
    except Refused as ref:
        return _refused("retire", ref, reg, today)
    print("retired %s on %s: find, scope, expired and INDEX.md no longer offer it, show still does"
          % (entry.id, entry.retired))
    return 0


def _cmd_find(args, today: date) -> int:
    ranked = find(args.words, scope=args.scope)
    shown, withheld = screen([e for _, e in ranked])
    keep = {e.id for e in shown}
    if not ranked:
        print("no entry matches")
    for score, e in ranked:
        if e.id in keep:
            print(_list_line(e, today, score))
    if withheld:
        print(_withheld_line(withheld))
    return 0


def _cmd_show(args, today: date) -> int:
    e = get(args.id)
    shown, _ = screen([e])
    if not shown:
        print("awb kb show: %s carries hits of the name check now; fix it by hand" % e.id, file=sys.stderr)
        return 1
    sys.stdout.write(render(e))
    if e.is_retired:
        # an id in an old note leads here, and the reason is the answer to why the fact went away
        print("RETIRED on %s: %s" % (e.retired, e.why))
    elif e.is_expired(today):
        print("EXPIRED since %s: check the fact again" % e.expires)
    return 0


def _cmd_scope(args, today: date) -> int:
    sys.stdout.write(briefing(args.tag, scope=args.scope, today=today))
    return 0


def _cmd_export(args, today: date) -> int:
    out = Path(args.out)
    if out.exists() and not args.force:
        print("awb kb export: the output file exists; give --force to replace it", file=sys.stderr)
        return 2
    if out.is_symlink():
        print("awb kb export: the output file is a link", file=sys.stderr)
        return 2
    records, left = export(scope=args.scope or None, grades=args.grade or EXPORT_GRADES, tags=args.tag,
                           include_expired=args.include_expired, today=today)
    text = "".join(json.dumps(r, ensure_ascii=False, sort_keys=True) + "\n" for r in records)
    tmp = out.with_name(".%s.tmp" % out.name)
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, out)
    grades = sorted({r["grade"] for r in records})
    print("exported %s to %s (grades %s)" % (_n(len(records), "entry", "entries"), out, ", ".join(grades) or "-"))
    print("left out: %s" % ", ".join("%s %d" % kv for kv in left.items() if kv[1]) or "left out: nothing")
    return 0


def _cmd_expired(args, today: date) -> int:
    old = expired(today=today)
    if not old:
        print("no expired entries")
        return 0
    shown, withheld = screen(old)
    for e in shown:
        print("  ".join([e.id, "expired %s" % e.expires, e.scope, e.grade, "checked %s" % e.checked,
                         _one_line(e.statement)]))
    if withheld:
        print(_withheld_line(withheld))
    print("%s expired" % _n(len(old), "entry", "entries"))
    return 1


def _cmd_verify(args, today: date) -> int:
    if not (args.staged or args.paths):
        print("awb kb verify: give --staged or at least one entry file", file=sys.stderr)
        return 2
    repo = (args.repo if args.repo is not None else Path.cwd()) if args.staged else None
    problems = verify(args.paths, staged_repo=repo)
    for label, why in problems:
        print("%s: %s" % (label, why))
    if problems:
        return 1
    print("awb kb verify: every entry passes the checks of kb add")
    return 0


def _cmd_index(args, today: date) -> int:
    path = write_index(today=today)
    print("wrote %s" % path.name)
    return 0


def _parser():
    from awb.cli import SafeParser

    ap = SafeParser(prog="awb kb", description="The knowledge base: one checked fact per file.")
    sub = ap.add_subparsers(dest="command", parser_class=SafeParser)

    s = sub.add_parser("add", help="check a fact and add it")
    s.add_argument("--scope", required=True, help="tcp or hcs")
    s.add_argument("--tag", action="append", required=True, help="a tag of rules/tags.txt (repeat or comma list)")
    s.add_argument("--grade", required=True, help=", ".join(GRADES))
    s.add_argument("--class", dest="cls", required=True, help=", ".join(CLASSES))
    s.add_argument("--source", required=True, help="where the fact comes from, one line")
    s.add_argument("--tried", action="append", default=[], help="what was tried to prove a negative (twice)")
    s.add_argument("--checked", default=None, help="ISO date of the check (default: today)")
    s.add_argument("--force-new", action="store_true", help="add even when a similar entry exists")
    s.add_argument("statement", nargs="+", help="the statement, or - to read it from standard input")
    s.set_defaults(func=_cmd_add)

    s = sub.add_parser("amend", help="correct the statement of an entry, keeping its id")
    s.add_argument("--checked", default=None, help="ISO date of a new check (default: the date of the entry)")
    s.add_argument("--tried", action="append", default=None,
                   help="what proves the corrected statement, for a negative (twice); replaces the old tried texts")
    s.add_argument("--force-new", action="store_true", help="amend even when a similar entry exists")
    s.add_argument("id")
    s.add_argument("statement", nargs="+", help="the new statement, or - to read it from standard input")
    s.set_defaults(func=_cmd_amend)

    s = sub.add_parser("bench", help="how well find answers questions whose answering entries are known")
    s.add_argument("--top", type=int, default=5)
    s.add_argument("file", type=Path)
    s.set_defaults(func=_cmd_bench)

    s = sub.add_parser("recheck", help="record a fresh check of an entry whose statement still holds")
    s.add_argument("--grade", required=True, help="live, contract, docs, said, assumed: what the new check was")
    s.add_argument("--source", required=True, help="where the new check comes from, one line")
    s.add_argument("--tried", action="append", default=[], help="what the new check tried, for a negative (twice)")
    s.add_argument("--checked", default=None, help="ISO date of the check (default: today)")
    s.add_argument("id")
    s.set_defaults(func=_cmd_recheck)

    s = sub.add_parser("retire", help="withdraw an entry; the file stays and the id is never reused")
    s.add_argument("--why", required=True, help="why it is withdrawn, one line")
    s.add_argument("id")
    s.set_defaults(func=_cmd_retire)

    s = sub.add_parser("find", help="entries ranked by word overlap")
    s.add_argument("--scope", default=None, choices=SCOPES)
    s.add_argument("words", nargs="+")
    s.set_defaults(func=_cmd_find)

    s = sub.add_parser("show", help="one entry")
    s.add_argument("id")
    s.set_defaults(func=_cmd_show)

    s = sub.add_parser("scope", help="a Markdown briefing of one tag")
    s.add_argument("--scope", default=None, choices=SCOPES)
    s.add_argument("tag")
    s.set_defaults(func=_cmd_scope)

    s = sub.add_parser("expired", help="entries past their expiry date")
    s.set_defaults(func=_cmd_expired)

    s = sub.add_parser("export", help="the facts fit to leave as a dataset, one JSON object per line")
    s.add_argument("--out", required=True, help="the JSON Lines file to write")
    s.add_argument("--scope", default="tcp", help="tcp or hcs; empty for both")
    s.add_argument("--grade", action="append", default=[], help="a grade to export (default: live, contract, docs)")
    s.add_argument("--tag", action="append", default=[], help="only entries with one of these tags")
    s.add_argument("--include-expired", action="store_true", help="also entries past their expiry, marked expired")
    s.add_argument("--force", action="store_true", help="replace an existing output file")
    s.set_defaults(func=_cmd_export)
    s = sub.add_parser("index", help="write INDEX.md again")
    s.set_defaults(func=_cmd_index)

    s = sub.add_parser("verify", help="check entry files with the checks of add (the commit hook runs --staged)")
    s.add_argument("--staged", action="store_true", help="check every entry a commit carries")
    s.add_argument("--repo", type=Path, default=None, help="the repository for --staged (default: the current one)")
    s.add_argument("paths", nargs="*", type=Path, help="entry files")
    s.set_defaults(func=_cmd_verify)
    return ap


def main(argv: list[str] | None = None) -> int:
    """`awb kb ...`. Exit 0 ok, 1 refused or expired entries found, 2 usage or error."""
    ap = _parser()
    try:
        args = ap.parse_args(argv)
    except SystemExit as exc:
        return exc.code if isinstance(exc.code, int) else 2
    func = getattr(args, "func", None)
    if func is None:
        ap.print_usage(sys.stderr)
        return 2
    try:
        return func(args, date.today())
    except KBError as err:
        print("awb kb: %s" % err, file=sys.stderr)
        return 2
    except OSError as err:
        print("awb kb: %s (operating system error)" % type(err).__name__, file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
