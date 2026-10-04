"""The register: one TSV file that maps codes to written forms.

Columns: code, kind, form, added, status. One line per written form. The file lives in the vault and is
the only place where a real written form meets its code. Nothing in this module writes a form into an
exception, a log line or any other output. Errors carry a line number and a reason.

The format is strict on purpose. Free text, notes and comments are refused so that the register never
grows into a document. A retired form stays in the file so that its code is never handed out again.

When the vault is encrypted, `register.tsv` is gone and `register.tsv.gpg` is there instead. Then `load` and
`save` go through the admin socket of the vault daemon (`awb.vault`), which holds the passphrase: the intake
and `awb register` work unchanged for the owner. `parse` and `render` are the strict rules without a file, so
that the daemon validates a register in memory and never writes a plaintext copy.
"""
from __future__ import annotations

import os
import re
import tempfile
import unicodedata
from dataclasses import dataclass, replace as _replace
from datetime import date
from pathlib import Path

from awb import codes as _codes

HEADER = ("code", "kind", "form", "added", "status")
STATUSES = ("active", "retired")
FORBIDDEN_IN_FORM = "()[]|#"

_DATE_RE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}")
_WS_RE = re.compile(r"\s+")


# umlauts written long (ae) and short (a); the sharp s is always ss
_UMLAUT_LONG = {"ä": "ae", "ö": "oe", "ü": "ue", "Ä": "Ae", "Ö": "Oe", "Ü": "Ue", "ß": "ss", "ẞ": "SS"}
_UMLAUT_SHORT = {"ä": "a", "ö": "o", "ü": "u", "Ä": "A", "Ö": "O", "Ü": "U", "ß": "ss", "ẞ": "SS"}
_GENITIVE_SUFFIXES = ("s", "'s", "’s")


@dataclass(frozen=True)
class Entry:
    code: str
    kind: str
    form: str
    added: str
    status: str


class RegisterError(Exception):
    """A register problem. The message names the line and the reason, never the content of the line."""


def _fail(where: str, reason: str) -> RegisterError:
    return RegisterError("register %s: %s" % (where, reason))


def _kind_of_code(code: str) -> str:
    """CUST for CUST-Q7M4, PERS for CUST-Q7M4-PERS-1."""
    parts = code.split("-")
    return parts[2] if len(parts) == 4 else parts[0]


def _check(e: Entry, where: str) -> None:
    """Refuse an entry that must not be in the register. `where` is "line N" or "new entry"."""
    for value in (e.code, e.kind, e.form, e.added, e.status):
        if not isinstance(value, str):
            raise _fail(where, "every field must be a string")
    if not _codes.is_code(e.code):
        raise _fail(where, "code does not match the placeholder grammar")
    if e.kind not in _codes.KINDS:
        raise _fail(where, "unknown kind")
    if e.kind != _kind_of_code(e.code):
        raise _fail(where, "kind does not match the code")
    if e.form == "":
        raise _fail(where, "empty form")
    if e.form != e.form.strip():
        raise _fail(where, "form has leading or trailing whitespace")
    if any(unicodedata.category(c) == "Cc" for c in e.form):
        raise _fail(where, "form contains a control character, a tab or a line break")
    if any(c in e.form for c in FORBIDDEN_IN_FORM):
        raise _fail(where, "form contains a bracket, a pipe or a hash; notes are not allowed in a form")
    if not _DATE_RE.fullmatch(e.added):
        raise _fail(where, "added is not an ISO date (YYYY-MM-DD)")
    try:
        date.fromisoformat(e.added)
    except ValueError:
        raise _fail(where, "added is not a valid date") from None
    if e.status not in STATUSES:
        raise _fail(where, "status must be active or retired")


def check_entry(e: Entry, where: str) -> None:
    """Refuse an entry that must not be in the register, before it is written (the web creation of a customer)."""
    _check(e, where)


def _check_all(entries: list[Entry]) -> None:
    """Check a list that is about to be written, with the line each entry would take."""
    seen: dict[tuple[str, str], int] = {}
    for i, e in enumerate(entries, start=2):
        where = "line %d" % i
        _check(e, where)
        key = (e.code, e.form)
        if key in seen:
            raise _fail(where, "the same code and form already appear on line %d" % seen[key])
        seen[key] = i


def encrypted_path(path: Path) -> Path:
    """Where the encrypted register of `path` lives: register.tsv -> register.tsv.gpg."""
    return Path(path).with_suffix(".tsv.gpg")


def _is_encrypted(path: Path) -> bool:
    """True when `path` is missing and its encrypted register exists. False when that cannot be seen."""
    try:
        return not path.exists() and encrypted_path(path).is_file()
    except OSError:
        return False


def _admin_socket_for(path: Path) -> Path:
    """The admin socket of the vault that holds `path`: the configured one for the configured vault, else the
    default place inside the folder of the register."""
    from awb import config

    p = config.paths()
    folder = Path(path).expanduser().resolve().parent
    return p.admin_sock if folder == p.vault else folder / "admin.sock"


def _vault_call(path: Path, op: str, **fields) -> dict:
    """One admin request for the register at `path`. Every failure is a RegisterError without a value."""
    from awb import vault

    try:
        return vault.admin_call(op, _admin_socket_for(path), **fields)
    except vault.VaultLocked:
        raise RegisterError("register is encrypted and the vault is locked; run awb vault unlock") from None
    except vault.VaultUnavailable:
        raise RegisterError("register is encrypted and the vault daemon is not available") from None
    except vault.VaultError as err:
        raise RegisterError("register refused by the vault daemon: %s" % err) from None


def parse(content: bytes | str) -> list[Entry]:
    """The entries of a register text, with every rule of `load`. Anything malformed is a RegisterError."""
    if isinstance(content, bytes):
        try:
            content = content.decode("utf-8")
        except UnicodeDecodeError:
            raise RegisterError("register is not valid UTF-8") from None
    if not isinstance(content, str):
        raise RegisterError("register must be text")
    return _parse_text(content)


def render(entries) -> str:
    """The file text of a list of entries, after the checks of `save`."""
    entries = list(entries)
    _check_all(entries)
    lines = ["\t".join(HEADER)]
    lines.extend("\t".join((e.code, e.kind, e.form, e.added, e.status)) for e in entries)
    return "\n".join(lines) + "\n"


def load(path: Path) -> list[Entry]:
    """Read the register. A missing file is an empty register, unless the encrypted register is there: then
    it is read through the vault daemon. Anything malformed is a RegisterError."""
    path = Path(path)
    try:
        raw = path.read_bytes()
    except FileNotFoundError:
        if _is_encrypted(path):
            text = _vault_call(path, "register_load").get("text")
            if not isinstance(text, str):
                raise RegisterError("register: the vault daemon answered in an unexpected form") from None
            return parse(text)
        return []
    except OSError as exc:
        raise RegisterError("register cannot be read: %s" % exc.__class__.__name__) from None
    return parse(raw)


def _parse_text(content: str) -> list[Entry]:
    if content.startswith("﻿"):
        raise _fail("line 1", "a byte order mark is not allowed")
    lines = content.split("\n")
    if lines and lines[-1] == "":
        lines.pop()
    if not lines:
        raise _fail("line 1", "missing header")
    if lines[0] != "\t".join(HEADER):
        raise _fail("line 1", "header must be exactly the five column names, tab separated")
    entries: list[Entry] = []
    seen: dict[tuple[str, str], int] = {}
    for i, line in enumerate(lines[1:], start=2):
        where = "line %d" % i
        if line == "":
            raise _fail(where, "blank line")
        if line.startswith("#"):
            raise _fail(where, "comment lines are not allowed")
        if "\r" in line:
            raise _fail(where, "carriage return in line, the register uses LF line endings")
        fields = line.split("\t")
        if len(fields) != len(HEADER):
            raise _fail(where, "expected %d tab separated fields, got %d" % (len(HEADER), len(fields)))
        e = Entry(*fields)
        _check(e, where)
        key = (e.code, e.form)
        if key in seen:
            raise _fail(where, "the same code and form already appear on line %d" % seen[key])
        seen[key] = i
        entries.append(e)
    return entries


def save(path: Path, entries: list[Entry]) -> None:
    """Write the register atomically. File mode 600, parent folder mode 700. When only the encrypted register
    exists, the vault daemon validates, encrypts and writes it (no plaintext copy is written here)."""
    path = Path(path)
    text = render(entries)
    if _is_encrypted(path):
        _vault_call(path, "register_save", text=text)
        return
    data = text.encode("utf-8")
    parent = path.parent
    try:
        parent.mkdir(parents=True, exist_ok=True)
        os.chmod(parent, 0o700)
    except OSError as exc:
        raise RegisterError("register folder cannot be secured: %s" % exc.__class__.__name__) from None
    fd, tmp = tempfile.mkstemp(prefix=".%s." % path.name, suffix=".tmp", dir=parent)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    os.chmod(path, 0o600)
    try:
        dfd = os.open(parent, os.O_RDONLY)
        try:
            os.fsync(dfd)
        finally:
            os.close(dfd)
    except OSError:
        pass


def add(path: Path, code: str, kind: str, form: str, added: str | None = None) -> Entry:
    """Add one written form for a code. The form is stripped. A retired code takes no new forms."""
    entries = load(path)
    if isinstance(form, str):
        form = form.strip()
    e = Entry(code, kind, form, added if added is not None else date.today().isoformat(), "active")
    _check(e, "new entry")
    for i, x in enumerate(entries, start=2):
        if x.code == code and x.form == form:
            raise _fail("new entry", "the same code and form already appear on line %d" % i)
    if any(x.code == code and x.status == "retired" for x in entries):
        raise _fail("new entry", "the code is retired and takes no new forms")
    entries.append(e)
    save(path, entries)
    return e


def retire(path: Path, code: str) -> int:
    """Mark every form of the code retired. Returns how many forms changed state."""
    entries = load(path)
    changed = 0
    out: list[Entry] = []
    for e in entries:
        if e.code == code and e.status != "retired":
            e = _replace(e, status="retired")
            changed += 1
        out.append(e)
    if changed:
        save(path, out)
    return changed


def codes(entries) -> set[str]:
    """Every code in the register, active or retired."""
    return {e.code for e in entries}


def next_code(entries, kind: str) -> str:
    """A fresh top-level code of the kind that no entry uses."""
    return _codes.new_code(kind, codes(entries))


def next_sub_code(entries, parent: str, kind: str) -> str:
    """The next free sub code under a parent, for example CUST-Q7M4 + PERS -> CUST-Q7M4-PERS-3."""
    used = codes(entries)
    n = 1
    while True:
        c = _codes.sub_code(parent, kind, n)
        if c not in used:
            return c
        n += 1


def _transliterations(form: str) -> set[str]:
    out = {form}
    for table in (_UMLAUT_LONG, _UMLAUT_SHORT):
        out.add("".join(table.get(c, c) for c in form))
    return out


def _variants(form: str) -> set[str]:
    """The form, its umlaut spellings, their genitives and every one of those with whitespace collapsed."""
    out: set[str] = set()
    for base in _transliterations(form):
        with_genitive = {base}
        if not base.endswith(("s", "S")):
            with_genitive.update(base + suffix for suffix in _GENITIVE_SUFFIXES)
        for g in with_genitive:
            out.add(g)
            out.add(_WS_RE.sub(" ", g))
    return out


def forms_for_matching(entries) -> list[tuple[str, str]]:
    """(variant, code) pairs for active entries, longest variant first. Retired forms are left out."""
    pairs: set[tuple[str, str]] = set()
    for e in entries:
        if e.status != "active":
            continue
        for v in _variants(e.form):
            pairs.add((v, e.code))
    return sorted(pairs, key=lambda p: (-len(p[0]), p[0], p[1]))
