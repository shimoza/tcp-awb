"""Pattern cards: the second entry type of the knowledge base (presentations/solution-packages/DESIGN.md 5.1 to 5.3).

A pattern card is not a TCP fact but a reusable piece of work (a sizing rule, a step, a lesson, an objection, a
script) that a source project learned and that a solution package may carry. Cards are written only by this
module, in the session of the source project, because only that session may see the material.

    <kb>/patterns/PAT-XXXX.md                 one card: `key: value` lines, a line `---`, then the statement
    <kb>/patterns/files/PAT-XXXX/NAME         the file of a script card
    <shared>/patterns/salt                    the salt of every hash below, drawn once
    <shared>/patterns/support.tsv             card id and the salted hash of a contributing project code
    <shared>/patterns/fingerprints/H.tsv      class and salted hash of each distinctive string of one project (5.3)

Header keys, in this order: id, scope, packages, kind, tags, support (the number of distinct projects, never
which), facts (KB ids the card rests on), checked, file (script cards only), retired and why (only when retired).

`add` refuses what `kb add` refuses (not English, a hit of the name check, a register or project code, an
identifier, a home path, a secret or a token, a price), an unknown tag, kind or KB id, a near duplicate of a card
of the same scope (word overlap above 0.6; `--support PAT-XXXX` counts this project for the card instead, or
`--force-new`) and, beyond `kb add`, the fingerprint check of 5.3: no intake token (`[person 3]`, `[name 1]`) and
none of the project's own distinctive strings:

    scope word    the tags and goal words of SCOPE.md that are not plain English or TCP vocabulary
    resource      every name, id and address of RESOURCES.md that is not plain English or TCP vocabulary
    total         every result of calc/calc.tsv with three or more significant digits
    date          every date of SCOPE.md, STATE.md and OPEN.md

TCP vocabulary is every word of the knowledge base, of rules/services.tsv, rules/tags.txt, rules/tech-terms.txt,
rules/known-words.txt and GENERIC_TERMS below (public terms of solution areas such as CAD and PDM); plain English
is rules/common-words.txt. A rare tool named in one project is a fingerprint; the same tool named in a KB fact is
not. A refusal names the class and the count, never the string.

Every `add` and `support` also writes the salted hashes of these strings to the fingerprint file of the project,
so the release screen of a package (awb/solutions.py) can find them without ever holding a string. A script card
carries its file, checked like a deliverable: the same screens and the same fingerprint check.

Cards never go into the TCP Facts dataset: `kb.load`, `kb.export` and the dataset build read `entries/` only.

    awb kb pattern add --package SLUG --kind KIND --tag T... [--fact KB-XXXX]... [--file PATH] [--project DIR]
                       [--force-new] [--checked D] STATEMENT|-
    awb kb pattern support [--project DIR] PAT-XXXX
    awb kb pattern find [--package SLUG] [--kind KIND] WORDS...
    awb kb pattern show PAT-XXXX
    awb kb pattern list [--package SLUG]
    awb kb pattern retire --why TEXT PAT-XXXX

Exit codes: 0 ok, 1 refused, 2 usage or error.
"""
from __future__ import annotations

import fcntl
import hashlib
import hmac
import os
import re
import secrets
import stat
import sys
import unicodedata
from dataclasses import dataclass, replace
from datetime import date
from pathlib import Path
from typing import Iterable, Sequence

from awb import codes, config, kb, normalize, patterns

PATTERNS = "patterns"
FILES = "files"
ID = r"PAT-[A-Z2-7]{4}"
ID_RE = re.compile(ID)
ID_ANY_RE = re.compile(r"\b%s\b" % ID)
_FILE_RE = re.compile(ID + r"\.md")
KINDS = ("signal", "sizing-rule", "step", "lesson", "question", "objection", "script", "risk")
CUSTOMER_KINDS = ("signal", "question", "objection", "risk")
"""Kinds that state something about customers: a released package carries them only with support 2 (5.4)."""
KEYS = ("id", "scope", "packages", "kind", "tags", "support", "facts", "checked", "file", "retired", "why")
REQUIRED = ("id", "scope", "packages", "kind", "tags", "support", "facts", "checked")
SLUG = r"[a-z0-9][a-z0-9-]{1,47}"
SLUG_RE = re.compile(SLUG)
_FILE_NAME_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}")
MAX_SCRIPT = 256 * 1024
SIMILAR_LIMIT = kb.SIMILAR_LIMIT

# the intake writes these tokens for what it took out; none of them belongs in a card
_INTAKE_TOKEN_RE = re.compile(r"(?i)\[(?:person|name|company|org|place|site|mail|phone|withheld)[ _-]?\d*\]")

# the tokens of the fingerprint check, the same on both sides (contribution and release screen)
_TOKEN_RE = re.compile(r"[a-z0-9]+(?:[.\-_:/@][a-z0-9]+)*")
_SEP_RE = re.compile(r"[.\-_:/@]")
_NUMBER_RE = re.compile(r"(?<![\w.,])\d(?:[\d.,']*\d)?(?![\w])")
_ISO_DATE_RE = re.compile(r"(?<!\d)(20\d{2})-(0[1-9]|1[0-2])-([0-2]\d|3[01])(?!\d)")
_DOT_DATE_RE = re.compile(r"(?<!\d)([0-3]?\d)\.([01]?\d)\.(20\d{2})(?!\d)")
CLASSES = ("scope word", "resource", "total", "date")
MIN_SIGNIFICANT = 3
GENERIC_TERMS = frozenset("""
cad cae cam pdm plm fem bim gis vdi rds vgpu fslogix poc sizing
""".split())
"""Public terms of solution areas that the word lists lack and that identify no project: never a fingerprint."""
MAX_PARTS = 8


class PatternError(kb.KBError):
    """The pattern cards cannot do what was asked. Messages carry classes, counts, ids and dates only."""


class Refused(PatternError):
    def __init__(self, reasons: Sequence[str], similar: Sequence[tuple[float, "Card"]] = ()):
        super().__init__("; ".join(reasons))
        self.reasons = list(reasons)
        self.similar = list(similar)


@dataclass(frozen=True)
class Card:
    id: str
    scope: str
    packages: tuple[str, ...]
    kind: str
    tags: tuple[str, ...]
    support: int
    facts: tuple[str, ...]
    checked: str
    statement: str
    file: str = ""
    retired: str = ""
    why: str = ""

    @property
    def is_retired(self) -> bool:
        return bool(self.retired)

    @property
    def about_customers(self) -> bool:
        return self.kind in CUSTOMER_KINDS


# --------------------------------------------------------------------------- places


def folder(where=None) -> Path:
    return kb.root(where) / PATTERNS


def shared_folder(p: config.Paths | None = None) -> Path:
    return (p or config.paths()).shared / PATTERNS


def fingerprint_folder(p: config.Paths | None = None) -> Path:
    return shared_folder(p) / "fingerprints"


def _private_write(path: Path, text: str) -> None:
    """Write `path` in one step (a temporary file beside it, then a rename), mode 640, no link followed."""
    path.parent.mkdir(parents=True, exist_ok=True)
    kb._write_atomic(path, text)


def salt(p: config.Paths | None = None) -> bytes:
    """The salt of every hash of the pattern layer, drawn on first use. Without it a hash of a project code would be
    one guess in a million."""
    path = shared_folder(p) / "salt"
    try:
        value = kb._read_text(path).strip()
    except FileNotFoundError:
        value = ""
    if not re.fullmatch(r"[0-9a-f]{64}", value):
        path.parent.mkdir(parents=True, exist_ok=True)
        fresh = secrets.token_hex(32)
        try:
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o640)
        except FileExistsError:
            value = kb._read_text(path).strip()
            if not re.fullmatch(r"[0-9a-f]{64}", value):
                raise PatternError("the salt of the pattern layer is damaged; fix <shared>/patterns/salt by hand")
            return bytes.fromhex(value)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(fresh + "\n")
        value = fresh
    return bytes.fromhex(value)


def hashed(value: str, key: bytes) -> str:
    return hmac.new(key, value.encode("utf-8"), hashlib.sha256).hexdigest()[:40]


# --------------------------------------------------------------------------- the project of the session


def project_root(start: Path | None = None) -> tuple[str, Path]:
    """(code, folder) of the project the session works in: the first folder from `start` up whose name is a
    project code and which holds SCOPE.md."""
    here = Path(os.path.abspath(start or Path.cwd()))
    for d in [here, *here.parents]:
        if codes.PROJECT_RE.fullmatch(d.name) and (d / "SCOPE.md").is_file():
            return d.name, d
    raise PatternError("a pattern card is written in the session of its source project: run it inside a project "
                       "folder or give --project DIR")


# --------------------------------------------------------------------------- the fingerprint check (5.3)


def _norm(text: str) -> str:
    return unicodedata.normalize("NFKC", normalize.normalize(text).text).lower()


def _runs(token: str) -> Iterable[str]:
    """The token and every contiguous run of its parts, with the separators it was written with: a resource name
    `cad-host-01` is found inside `cad-host-01.example` too."""
    yield token
    parts = _SEP_RE.split(token)
    if len(parts) < 2 or len(parts) > MAX_PARTS:
        return
    seps = _SEP_RE.findall(token)
    for i in range(len(parts)):
        for j in range(i + 1, len(parts) + 1):
            if (i, j) == (0, len(parts)):
                continue
            yield "".join(parts[k] + (seps[k] if k < j - 1 else "") for k in range(i, j))


def number_key(raw: str) -> str | None:
    digits = re.sub(r"\D", "", raw).lstrip("0")
    if len(digits.rstrip("0")) < MIN_SIGNIFICANT:
        return None
    return "#" + digits


def _dates(text: str) -> set[str]:
    out = {"@%s-%s-%s" % m.groups() for m in _ISO_DATE_RE.finditer(text)}
    for d, mth, y in _DOT_DATE_RE.findall(text):
        try:
            out.add("@" + date(int(y), int(mth), int(d)).isoformat())
        except ValueError:
            pass
    return out


def candidates(text: str) -> dict[str, set[int]]:
    """Every key the fingerprint check compares, with the lines (1-based) it stands on: the tokens and their runs,
    the numbers as digit strings (`#618546` for 6,185.46 and 6.185,46 alike) and the dates as `@YYYY-MM-DD`."""
    out: dict[str, set[int]] = {}
    for n, line in enumerate(text.splitlines() or [text], start=1):
        low = _norm(line)
        keys: set[str] = set()
        for tok in _TOKEN_RE.findall(low):
            keys.update(_runs(tok))
        for m in _NUMBER_RE.finditer(low):
            k = number_key(m.group(0))
            if k:
                keys.add(k)
        keys |= _dates(low)
        for k in keys:
            out.setdefault(k, set()).add(n)
    return out


_VOCAB: dict[tuple, frozenset[str]] = {}


def _rule_words(name: str, column: int = 0) -> set[str]:
    path = patterns.RULES_DIR / name
    out: set[str] = set()
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return out
    for line in text.splitlines():
        if not line.strip() or line.startswith("#"):
            continue
        cells = line.split("\t")
        cell = cells[column] if column < len(cells) else ""
        out.update(_TOKEN_RE.findall(_norm(cell)))
    return out


def vocabulary(where=None) -> frozenset[str]:
    """Words that identify no project: plain English, the words of the rules lists of the platform and every word
    the knowledge base already states."""
    stamp = tuple(sorted((e.id, e.checked, e.retired) for e in kb.load(where)))
    key = (str(kb.root(where)), stamp)
    if key in _VOCAB:
        return _VOCAB[key]
    words: set[str] = set()
    for name in ("common-words.txt", "known-words.txt", "tech-terms.txt", "tags.txt", "allowed-terms.txt"):
        words |= _rule_words(name)
    words.update(GENERIC_TERMS)
    path = patterns.RULES_DIR / "services.tsv"
    try:
        words.update(_TOKEN_RE.findall(_norm(path.read_text(encoding="utf-8"))))
    except OSError:
        pass
    for e in kb.load(where):
        for tok in _TOKEN_RE.findall(_norm(e.statement)):
            words.update(_runs(tok))
    _VOCAB.clear()
    _VOCAB[key] = frozenset(words)
    return _VOCAB[key]


def _read_project(folder: Path, name: str) -> str:
    try:
        return kb._read_text(folder / name)
    except FileNotFoundError:
        return ""
    except (OSError, UnicodeDecodeError, kb.KBError):
        raise PatternError("%s of the project cannot be read" % name) from None


def _distinct(tokens: Iterable[str], vocab: frozenset[str], *, min_len: int = 3) -> set[str]:
    out = set()
    for tok in tokens:
        if len(tok) < min_len or tok in vocab or tok.isdigit():
            continue
        if codes.PROJECT_RE.fullmatch(tok):
            continue   # a project code is refused as a code, by every screen
        out.add(tok)
    return out


_LINE_KEY_RE = re.compile(r"^\s*-\s*(tags|goal)\s*:\s*(.*)$", re.I | re.M)


def fingerprints(folder: Path, where=None) -> dict[str, set[str]]:
    """The distinctive strings of one project folder by class (CLASSES), as the keys `candidates` makes. Reads
    SCOPE.md, STATE.md, OPEN.md, RESOURCES.md and calc/calc.tsv, never `input/`, `in/` or an original."""
    vocab = vocabulary(where)
    out: dict[str, set[str]] = {c: set() for c in CLASSES}
    scope = _read_project(folder, "SCOPE.md")
    for _, value in _LINE_KEY_RE.findall(scope):
        out["scope word"] |= _distinct(_TOKEN_RE.findall(_norm(value)), vocab)
    resources = _read_project(folder, "RESOURCES.md")
    out["resource"] = _distinct(_TOKEN_RE.findall(_norm(resources)), vocab, min_len=4) - out["scope word"]
    calc = _read_project(folder, "calc/calc.tsv")
    for line in calc.splitlines()[1:]:
        cells = line.split("\t")
        if len(cells) >= 6:
            k = number_key(cells[5].strip())
            if k:
                out["total"].add(k)
    for name in ("SCOPE.md", "STATE.md", "OPEN.md"):
        out["date"] |= _dates(_norm(_read_project(folder, name)))
    return out


def fingerprint_problems(label: str, text: str, prints: dict[str, set[str]]) -> list[str]:
    """Why `text` carries the project's own strings: one message per class with the count, never the string."""
    found = candidates(text)
    out = []
    n_intake = len(_INTAKE_TOKEN_RE.findall(text))
    if n_intake:
        out.append("%s carries %s of the intake; a card names nobody, not even by token"
                   % (label, kb._n(n_intake, "token")))
    for cls in CLASSES:
        n = len(prints.get(cls, set()) & set(found))
        if n:
            out.append("%s carries %s of this project (%s); write it as a rule, a range or a placeholder"
                       % (label, kb._n(n, "distinctive string"), cls))
    return out


def write_fingerprints(code: str, prints: dict[str, set[str]], p: config.Paths | None = None) -> Path:
    """The salted hashes of the project's strings, class by class, for the release screen; never a string."""
    key = salt(p)
    lines = sorted("%s\t%s" % (cls, hashed(v, key)) for cls, values in prints.items() for v in values)
    path = fingerprint_folder(p) / ("%s.tsv" % hashed("project:" + code, key))
    _private_write(path, "# class\thash (awb kb pattern, DESIGN 5.3)\n" + "".join(l + "\n" for l in lines))
    return path


def load_fingerprints(p: config.Paths | None = None) -> dict[str, str]:
    """Every hashed fingerprint of every project: hash -> class. The projects stay unnamed."""
    out: dict[str, str] = {}
    d = fingerprint_folder(p)
    try:
        names = sorted(os.listdir(d))
    except FileNotFoundError:
        return out
    for name in names:
        if not re.fullmatch(r"[0-9a-f]{40}\.tsv", name):
            continue
        for line in kb._read_text(d / name).splitlines():
            cells = line.split("\t")
            if len(cells) == 2 and cells[0] in CLASSES and re.fullmatch(r"[0-9a-f]{40}", cells[1]):
                out[cells[1]] = cells[0]
    return out


# --------------------------------------------------------------------------- support


def support_file(p: config.Paths | None = None) -> Path:
    return shared_folder(p) / "support.tsv"


def _support_rows(p: config.Paths | None = None) -> set[tuple[str, str]]:
    try:
        text = kb._read_text(support_file(p))
    except FileNotFoundError:
        return set()
    rows = set()
    for line in text.splitlines():
        cells = line.split("\t")
        if len(cells) == 2 and ID_RE.fullmatch(cells[0]) and re.fullmatch(r"[0-9a-f]{40}", cells[1]):
            rows.add((cells[0], cells[1]))
    return rows


def support_of(card_id: str, p: config.Paths | None = None) -> int:
    return len({h for c, h in _support_rows(p) if c == card_id})


def _add_support(card_id: str, code: str, p: config.Paths | None = None) -> tuple[int, bool]:
    """(support now, whether this project was new for the card)."""
    key = salt(p)
    d = shared_folder(p)
    fd = os.open(d, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)   # two sessions adding support at once lose no row
        rows = _support_rows(p)
        row = (card_id, hashed("project:" + code, key))
        new = row not in rows
        if new:
            rows.add(row)
            _private_write(support_file(p), "# card\tproject hash (awb kb pattern support)\n"
                           + "".join("%s\t%s\n" % r for r in sorted(rows)))
    finally:
        os.close(fd)
    return len({h for c, h in rows if c == card_id}), new


# --------------------------------------------------------------------------- card files


def render(c: Card) -> str:
    lines = ["id: %s" % c.id, "scope: %s" % c.scope, "packages: %s" % ", ".join(c.packages), "kind: %s" % c.kind,
             "tags: %s" % ", ".join(c.tags), "support: %d" % c.support,
             "facts: %s" % (", ".join(c.facts) if c.facts else "none"), "checked: %s" % c.checked]
    if c.file:
        lines.append("file: %s" % c.file)
    if c.retired:
        lines += ["retired: %s" % c.retired, "why: %s" % c.why]
    return "\n".join(lines) + "\n" + kb.SEPARATOR + "\n" + c.statement + "\n"


def parse(text: str, name: str = "card") -> Card:
    name = name if isinstance(name, str) and ID_RE.fullmatch(name) else "card"
    lines = text.split("\n")
    try:
        cut = lines.index(kb.SEPARATOR)
    except ValueError:
        raise PatternError("%s has no line ---" % name) from None
    head: dict[str, str] = {}
    for i, line in enumerate(lines[:cut], start=1):
        k, sep, v = line.partition(": ")
        if not sep or k not in KEYS or k in head:
            raise PatternError("%s: line %d is not a known header key" % (name, i))
        head[k] = v.strip()
    missing = [k for k in REQUIRED if k not in head]
    if missing:
        raise PatternError("%s lacks %s" % (name, ", ".join(missing)))
    if not ID_RE.fullmatch(head["id"]) or (name != "card" and head["id"] != name):
        raise PatternError("%s: the id does not match the file" % name)
    if not head["support"].isdigit():
        raise PatternError("%s: support is not a number" % name)
    facts = () if head["facts"] == "none" else tuple(f.strip() for f in head["facts"].split(","))
    if any(not kb.ID_RE.fullmatch(f) for f in facts):
        raise PatternError("%s: facts must be KB ids" % name)
    statement = "\n".join(lines[cut + 1:]).strip()
    return Card(id=head["id"], scope=head["scope"], packages=tuple(s.strip() for s in head["packages"].split(",")),
                kind=head["kind"], tags=tuple(t.strip() for t in head["tags"].split(",")),
                support=int(head["support"]), facts=facts, checked=head["checked"], statement=statement,
                file=head.get("file", ""), retired=head.get("retired", ""), why=head.get("why", ""))


def load(where=None) -> list[Card]:
    d = folder(where)
    try:
        names = sorted(os.listdir(d))
    except FileNotFoundError:
        return []
    out = []
    for name in names:
        if _FILE_RE.fullmatch(name):
            try:
                text = kb._read_text(d / name)
            except (OSError, UnicodeError) as err:
                raise PatternError("%s cannot be read (%s)" % (name[:-3], type(err).__name__)) from None
            out.append(parse(text, name[:-3]))
    return out


def get(card_id: str, where=None) -> Card:
    if not isinstance(card_id, str) or not ID_RE.fullmatch(card_id):
        raise PatternError("the argument is not a pattern card id (PAT-XXXX)")
    for c in load(where):
        if c.id == card_id:
            return c
    raise PatternError("there is no card %s" % card_id)


def card_file(c: Card, where=None) -> Path | None:
    return folder(where) / FILES / c.id / c.file if c.file else None


def _new_id(d: Path) -> str:
    taken = set(os.listdir(d)) if d.is_dir() else set()
    for _ in range(1000):
        cid = "PAT-" + "".join(secrets.choice(codes.ALPHABET) for _ in range(4))
        if cid + ".md" not in taken:
            return cid
    raise PatternError("could not find a free card id")


def _write(c: Card, where=None, *, new: bool = False) -> None:
    d = folder(where)
    d.mkdir(parents=True, exist_ok=True)
    kb._write_atomic(d / (c.id + ".md"), render(c), overwrite=not new)


# --------------------------------------------------------------------------- the checks


def similar(statement: str, cards: Iterable[Card], n: int = kb.SIMILAR_SHOWN) -> list[tuple[float, Card]]:
    q = kb.words(statement)
    scored = [(kb.overlap(q, kb.words(c.statement)), c) for c in cards]
    scored = [s for s in scored if s[0] > 0]
    scored.sort(key=lambda t: (-t[0], t[1].id))
    return scored[:n]


def _clean_packages(packages) -> list[str]:
    if isinstance(packages, str):
        packages = [packages]
    out: list[str] = []
    for p in packages:
        out.extend(x.strip().lower() for x in str(p).split(",") if x.strip())
    out = list(dict.fromkeys(out))
    if not out:
        raise PatternError("give at least one --package SLUG")
    if any(not SLUG_RE.fullmatch(p) for p in out):
        raise PatternError("a package slug is lower case letters, digits and dashes (2 to 48)")
    return out


def _fact_problems(facts: Sequence[str], where=None) -> list[str]:
    out = []
    entries = {e.id: e for e in kb.load(where)}
    for i, f in enumerate(facts, start=1):
        if not kb.ID_RE.fullmatch(f):
            out.append("fact %d is not a KB id" % i)
        elif f not in entries:
            out.append("fact %d is not in the knowledge base" % i)
        elif entries[f].is_retired:
            out.append("fact %d (%s) is retired" % (i, f))
    return out


def _script_text(path: Path) -> tuple[str, str]:
    name = path.name
    if not _FILE_NAME_RE.fullmatch(name):
        raise PatternError("the file name is letters, digits, dot, dash and underscore (up to 64)")
    try:
        st = os.stat(path, follow_symlinks=False)
    except FileNotFoundError:
        raise PatternError("the file does not exist") from None
    if not stat.S_ISREG(st.st_mode) or st.st_size > MAX_SCRIPT:
        raise PatternError("the file must be a regular file of at most %d KiB" % (MAX_SCRIPT // 1024))
    try:
        return name, path.read_bytes().decode("utf-8")
    except UnicodeDecodeError:
        raise PatternError("the file must be UTF-8 text") from None


def add(statement: str, *, packages: Sequence[str], kind: str, tags: Sequence[str], facts: Sequence[str] = (),
        file: Path | None = None, project: Path | None = None, force_new: bool = False,
        checked: str | date | None = None, scope: str = "tcp", where=None, register_path: Path | None = None,
        p: config.Paths | None = None, today: date | None = None) -> Card:
    """Check a card and write it, count the source project as its first support and write the project's
    fingerprints. Raises Refused (reasons and the most similar cards) or PatternError; nothing is written unless
    every check passed, but the fingerprints, which are written in every case."""
    today = today or date.today()
    if scope not in kb.SCOPES:
        raise PatternError("scope must be one of: %s" % ", ".join(kb.SCOPES))
    if kind not in KINDS:
        raise PatternError("kind must be one of: %s" % ", ".join(KINDS))
    if (kind == "script") != (file is not None):
        raise PatternError("a script card carries one --file and only a script card does")
    code, proj = project_root(project)
    checked = kb._checked(checked, today)
    statement = kb._clean_statement(statement)
    pkgs = _clean_packages(packages)
    tag_list, reasons = kb._clean_tags(tags)
    facts = list(dict.fromkeys(f.strip() for f in facts if f.strip()))
    reasons += _fact_problems(facts, where)
    reg = kb.register_of(register_path)
    kb._require_register(reg)
    reasons += kb.text_problems("the statement", statement, reg)
    prints = fingerprints(proj, where)
    write_fingerprints(code, prints, p)
    reasons += fingerprint_problems("the statement", statement, prints)
    script = None
    if file is not None:
        script = _script_text(Path(file))
        reasons += kb.text_problems("the file", script[1], reg, language=False)
        reasons += fingerprint_problems("the file", script[1], prints)
    cards = [c for c in load(where) if c.scope == scope and not c.is_retired]
    near = similar(statement, cards)
    if any(s > SIMILAR_LIMIT for s, _ in near) and not force_new:
        reasons.append("the statement is too similar to an existing card (word overlap above %.1f); count this "
                       "project for it with awb kb pattern support PAT-XXXX, or use --force-new when it is a "
                       "different piece" % SIMILAR_LIMIT)
    if reasons:
        raise Refused(reasons, near)
    d = folder(where)
    d.mkdir(parents=True, exist_ok=True)
    cid = _new_id(d)
    card = Card(id=cid, scope=scope, packages=tuple(pkgs), kind=kind, tags=tuple(tag_list), support=1,
                facts=tuple(facts), checked=checked, statement=statement, file=script[0] if script else "")
    if script:
        target = d / FILES / cid
        target.mkdir(parents=True, exist_ok=True)
        kb._write_atomic(target / script[0], script[1], overwrite=False)
    _write(card, where, new=True)
    n, _ = _add_support(cid, code, p)
    if n != 1:
        card = replace(card, support=n)
        _write(card, where)
    return card


def support(card_id: str, *, project: Path | None = None, where=None,
            p: config.Paths | None = None) -> tuple[Card, bool]:
    """Count the project of the session for a card: support rises when the project is new for it. Writes the
    project's fingerprints too. Returns (card, whether support rose)."""
    code, proj = project_root(project)
    card = get(card_id, where)
    if card.is_retired:
        raise PatternError("%s is retired" % card.id)
    write_fingerprints(code, fingerprints(proj, where), p)
    n, new = _add_support(card.id, code, p)
    if n != card.support:
        card = replace(card, support=n)
        _write(card, where)
    return card, new


def retire(card_id: str, why: str, *, where=None, register_path: Path | None = None,
           today: date | None = None) -> Card:
    today = today or date.today()
    why = kb._one_line_field(why, "the reason why")
    reg = kb.register_of(register_path)
    kb._require_register(reg)
    reasons = kb.text_problems("the reason why", why, reg)
    if reasons:
        raise Refused(reasons)
    card = get(card_id, where)
    if card.is_retired:
        raise PatternError("%s is retired already" % card.id)
    card = replace(card, retired=today.isoformat(), why=why)
    _write(card, where)
    return card


def find(query: str | Sequence[str], where=None, *, package: str | None = None, kind: str | None = None,
         include_retired: bool = False) -> list[tuple[float, Card]]:
    q = kb.words(query if isinstance(query, str) else " ".join(query))
    out = []
    for c in load(where):
        if (c.is_retired and not include_retired) or (package and package not in c.packages) \
                or (kind and c.kind != kind):
            continue
        w = kb.words(c.statement + " " + " ".join(c.tags))
        if q & w:
            out.append((len(q & w) / len(q), c))
    out.sort(key=lambda t: (-t[0], t[1].id))
    return out


def screen(cards: Sequence[Card], register_path: Path | None = None) -> tuple[list[Card], int]:
    """(cards that pass the name, code and secret checks now, count withheld), as `kb.screen` does for facts."""
    as_entries = [kb.Entry(id="KB-AAAA", scope=c.scope, tags=c.tags, grade="said", checked=c.checked, cls="stable",
                           expires=c.checked, source="pattern card", tried=(), statement=c.statement, why=c.why)
                  for c in cards]
    shown, withheld = kb.screen(as_entries, register_path)
    keep = {id(e) for e in shown}
    return [c for c, e in zip(cards, as_entries) if id(e) in keep], withheld


# --------------------------------------------------------------------------- commands


def _line(c: Card, score: float | None = None) -> str:
    parts = [c.id] + (["%.2f" % score] if score is not None else [])
    parts += [c.kind, "support %d" % c.support, ",".join(c.packages)]
    if c.is_retired:
        parts.append("RETIRED")
    parts.append(kb._one_line(c.statement))
    return "  ".join(parts)


def _print_cards(scored: Sequence[tuple[float | None, Card]]) -> None:
    shown, withheld = screen([c for _, c in scored])
    keep = {c.id for c in shown}
    for score, c in scored:
        if c.id in keep:
            print("  " + _line(c, score) if score is not None else _line(c))
    if withheld:
        print("  %s withheld: hits of the name check now, fix by hand in %s/" % (
            kb._n(withheld, "card"), PATTERNS))


def _cmd_add(args) -> int:
    statement = kb._statement_of(args.statement)
    try:
        card = add(statement, packages=args.package, kind=args.kind, tags=args.tag, facts=args.fact,
                   file=args.file, project=args.project, force_new=args.force_new, checked=args.checked)
    except Refused as ref:
        for r in ref.reasons:
            print("awb kb pattern add: refused: %s" % r, file=sys.stderr)
        if ref.similar:
            print("most similar cards:")
            _print_cards(ref.similar)
        return 1
    print("added %s (kind %s, support %d, packages %s)" % (card.id, card.kind, card.support, ", ".join(card.packages)))
    return 0


def _cmd_support(args) -> int:
    card, new = support(args.id, project=args.project)
    print("%s support %d%s" % (card.id, card.support, "" if new else " (this project counted already)"))
    return 0


def _cmd_find(args) -> int:
    hits = find(args.words, package=args.package, kind=args.kind)
    if not hits:
        print("no card matches")
        return 0
    _print_cards(hits)
    return 0


def _cmd_list(args) -> int:
    cards = [c for c in load() if not c.is_retired and (not args.package or args.package in c.packages)]
    if not cards:
        print("no cards")
        return 0
    _print_cards([(None, c) for c in cards])
    return 0


def _cmd_show(args) -> int:
    c = get(args.id)
    shown, _ = screen([c])
    if not shown:
        print("awb kb pattern show: %s carries hits of the name check now; fix it by hand" % c.id, file=sys.stderr)
        return 1
    sys.stdout.write(render(c))
    if c.is_retired:
        print("RETIRED on %s: %s" % (c.retired, c.why))
    return 0


def _cmd_retire(args) -> int:
    try:
        c = retire(args.id, args.why)
    except Refused as ref:
        for r in ref.reasons:
            print("awb kb pattern retire: refused: %s" % r, file=sys.stderr)
        return 1
    print("retired %s on %s" % (c.id, c.retired))
    return 0


def _parser():
    from awb.cli import SafeParser

    ap = SafeParser(prog="awb kb pattern", description="Pattern cards: reusable pieces of work for solution packages.")
    sub = ap.add_subparsers(dest="command", parser_class=SafeParser)

    s = sub.add_parser("add", help="check a card and add it, in the session of its source project")
    s.add_argument("--package", action="append", required=True, help="a package slug (repeat or comma list)")
    s.add_argument("--kind", required=True, help=", ".join(KINDS))
    s.add_argument("--tag", action="append", required=True, help="a tag of rules/tags.txt (repeat or comma list)")
    s.add_argument("--fact", action="append", default=[], help="a KB id the card rests on (repeat)")
    s.add_argument("--file", type=Path, default=None, help="the file of a script card")
    s.add_argument("--project", type=Path, default=None, help="the project folder (default: the one of the cwd)")
    s.add_argument("--checked", default=None, help="ISO date (default: today)")
    s.add_argument("--force-new", action="store_true", help="add even when a similar card exists")
    s.add_argument("statement", nargs="+", help="the statement, or - to read it from standard input")
    s.set_defaults(func=_cmd_add)

    s = sub.add_parser("support", help="count the project of this session for a card")
    s.add_argument("--project", type=Path, default=None)
    s.add_argument("id")
    s.set_defaults(func=_cmd_support)

    s = sub.add_parser("find", help="cards ranked by word overlap")
    s.add_argument("--package", default=None)
    s.add_argument("--kind", default=None, choices=KINDS)
    s.add_argument("words", nargs="+")
    s.set_defaults(func=_cmd_find)

    s = sub.add_parser("list", help="the standing cards, of one package with --package")
    s.add_argument("--package", default=None)
    s.set_defaults(func=_cmd_list)

    s = sub.add_parser("show", help="one card")
    s.add_argument("id")
    s.set_defaults(func=_cmd_show)

    s = sub.add_parser("retire", help="withdraw a card; the file stays and the id is never reused")
    s.add_argument("--why", required=True)
    s.add_argument("id")
    s.set_defaults(func=_cmd_retire)
    return ap


def main(argv: list[str] | None = None) -> int:
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
        return func(args)
    except kb.KBError as err:
        print("awb kb pattern: %s" % err, file=sys.stderr)
        return 2
    except OSError as err:
        print("awb kb pattern: %s (operating system error)" % type(err).__name__, file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
