"""Check a text or a file for registered names and structured data.

`check_text` normalises the text, finds the active register forms with `matcher.Matcher` and the structured
data with `patterns.find_structured` and returns the public view of every hit: start, length and class,
with positions in the normalised text. A structured hit that lies wholly inside a name hit is left out, so
a registered domain is reported once as a name and not a second time as a URL.

`check_file` reads a text file directly, with what hides behind base64 and hex blocks appended; an html
file is checked raw and as stripped text. Any other kind (office, pdf, mail, archive) goes through
`awb.extract.extract` when that package is present. Its detect text is checked, with the detect text of
archive members and mail attachments appended in order. Its raw parts (`scan_text`) are checked for
registered forms. A file that cannot be read as text, whose extraction is not `ok` or that was not read in
full (a limit, an encrypted member, an embedded object) also gets one record of class `opaque` (start 0,
length 0): a check never calls a file clean that it could not read. The file name is checked too: a
registered form in it gives one record of class `path` (start 0, length 0).

Nothing here prints, logs or raises with a matched value, a register form or a code. The command line prints
JSON lines with file, start, length and class only. It prints a path only when the path carries no
registered form, else "file N" (N counts the arguments). It exits 2 when there is no register, unless
--no-register says that a check of structured data alone is wanted.

Where the names come from. A readable register is used here, as it always was; a folder that is readable but
holds no register at all (neither register.tsv nor register.tsv.gpg) is an empty register. In every other
case (no register path, a register this user cannot read, an encrypted register) the check goes to the vault
daemon over the check socket (`config.paths().check_socket`, `awb.vault.check_remote`). When that fails too,
`CheckUnavailable` is raised: the check fails closed and never calls a text clean that it could not check.
The command line then exits 2 with "name check unavailable: vault locked" or "...: no vault daemon" and
prints no hit list at all.
"""
from __future__ import annotations

import json
import os
import re
import sys
import time
from pathlib import Path

from awb import normalize, patterns, register
from awb.matcher import Matcher, Span, public

OPAQUE = "opaque"
"""Class of the record for a file that could not be read as text."""
PATH = "path"
"""Class of the record for a file whose name carries a registered form."""

_SNIFF_BYTES = 8192
_BINARY_MAGIC = (
    b"%PDF",
    b"PK\x03\x04",
    b"PK\x05\x06",
    b"PK\x07\x08",
    b"\xd0\xcf\x11\xe0",
    b"\x89PNG",
    b"\xff\xd8\xff",
    b"GIF87a",
    b"GIF89a",
    b"\x1f\x8b",
    b"BZh",
    b"\xfd7zXZ\x00",
)
_PLAIN_KINDS = ("text", "html", "csv", "json")


class CheckUnavailable(register.RegisterError):
    """The name check cannot run: the register is not readable here and the vault daemon did not answer, or
    answered that the vault is locked. A RegisterError, so that every caller that refuses on a register it
    cannot read refuses here too. The message carries no value."""


class CheckRateLimited(CheckUnavailable):
    """The vault daemon refused the check because this user sent too many requests in the last minute. It passes
    within a minute; a hook waits a little and then refuses instead of passing a text unchecked."""


LOCAL = "local"
MISSING = "missing"
REMOTE = "remote"

RATE_WAIT = 90.0
"""Seconds a command line run waits and retries when the vault daemon answers with its rate limit."""


def _exists(path: Path) -> bool | None:
    """True, False or None when it cannot be seen."""
    try:
        os.stat(path)
        return True
    except FileNotFoundError:
        return False
    except OSError:
        return None


def register_source(register_path: Path | None) -> tuple[str, list[register.Entry] | None]:
    """Where the name class comes from: (LOCAL, entries) for a readable register, (MISSING, None) for a folder
    that this user can read and that holds no register at all, (REMOTE, None) for everything else: no path, a
    register this user cannot read, an encrypted register. A readable but broken register raises RegisterError."""
    if register_path is None:
        return REMOTE, None
    path = Path(register_path)
    try:
        data = path.read_bytes()
    except FileNotFoundError:
        if _exists(register.encrypted_path(path)) is False and path.parent.is_dir() \
                and os.access(path.parent, os.R_OK | os.X_OK):
            return MISSING, None
        return REMOTE, None
    except OSError:
        return REMOTE, None
    return LOCAL, register.parse(data)


class LocalCheck:
    """The check over a register read here."""

    def __init__(self, matcher: Matcher):
        self.matcher = matcher

    def scan(self, text: str) -> list[dict]:
        return _scan(text, self.matcher)

    def name_spans(self, text: str) -> list[Span]:
        """Name hits of `text`, positions in its normalised form."""
        return self.matcher.find(normalize.normalize(text).text)


class RemoteCheck:
    """The check of the vault daemon over the check socket. Every failure is CheckUnavailable."""

    def __init__(self, sock: Path, rate_wait: float = 0.0):
        self.sock = Path(sock)
        self.rate_wait = rate_wait

    def ping(self) -> str:
        """The daemon's state. Raises CheckUnavailable when there is no daemon or the vault is locked."""
        from awb import vault

        try:
            state = vault.ping(self.sock)
        except vault.VaultRateLimited as err:
            raise CheckRateLimited(_reason(err)) from None
        except vault.VaultError as err:
            raise CheckUnavailable(_reason(err)) from None
        if state == "locked":
            raise CheckUnavailable("name check unavailable: vault locked")
        return state

    def scan(self, text: str) -> list[dict]:
        from awb import vault

        deadline = time.monotonic() + self.rate_wait
        while True:
            try:
                return vault.check_remote(text, self.sock)
            except vault.VaultRateLimited as err:
                if time.monotonic() + 1.0 > deadline:
                    raise CheckRateLimited(_reason(err)) from None
                time.sleep(1.0)
            except vault.VaultError as err:
                raise CheckUnavailable(_reason(err)) from None

    def name_spans(self, text: str) -> list[Span]:
        """Name hits of `text` as the daemon found them, positions in the normalised form of `text`."""
        return [Span(h["start"], h["start"] + h["length"], "name") for h in self.scan(text) if h["cls"] == "name"]


def _reason(err: Exception) -> str:
    from awb import vault

    if isinstance(err, vault.VaultLocked):
        return "name check unavailable: vault locked"
    if isinstance(err, vault.VaultRateLimited):
        return "name check unavailable: rate limit of the vault daemon, try again in a minute"
    return "name check unavailable: %s" % err


def remote_socket() -> Path:
    from awb import config

    return config.paths().check_socket


def checker(register_path: Path | None, rate_wait: float = 0.0) -> LocalCheck | RemoteCheck:
    """A LocalCheck over a readable register (an empty one when the folder holds no register at all), else a
    RemoteCheck over the check socket of the vault daemon."""
    kind, entries = register_source(register_path)
    if kind == LOCAL:
        return LocalCheck(Matcher(register.forms_for_matching(entries)))
    if kind == MISSING:
        return LocalCheck(Matcher([]))
    return RemoteCheck(remote_socket(), rate_wait)


def _as_checker(obj) -> LocalCheck | RemoteCheck:
    return LocalCheck(obj) if isinstance(obj, Matcher) else obj


_OVERRIDE_RE = re.compile("[\u202d\u202e][^\n\u202c]*")
"""A bidi override (LRO, RLO) and what it governs, up to its pop or the end of the line: a viewer renders that
run reversed, so the check reads it reversed as well (the red team of the hooks, 2026-09-27)."""


def _bidi_views(text: str) -> list[str]:
    """The reversed text of every run under a bidi override in `text`; empty when there is none."""
    if "\u202e" not in text and "\u202d" not in text:
        return []
    return [m.group(0)[1:][::-1] for m in _OVERRIDE_RE.finditer(text) if len(m.group(0)) > 1]


def with_hidden_views(text: str) -> str:
    """`text` followed by the decoded text behind its base64 and hex blocks. Positions past the end of `text`
    belong to these views. (The reversed runs under a bidi override are read by `_scan` itself, on both sides
    of the daemon's socket.)"""
    decoded, _ = _base64_texts(text)
    extra = [b for b in decoded if b]
    if not extra:
        return text
    return text + "".join("\n\n" + v for v in extra)


def _scan(text: str, matcher: Matcher) -> list[dict]:
    """Name and structured hits of `text`, positions in its normalised form, public view only. A run under a
    bidi override is read reversed as well."""
    views = _bidi_views(text)
    if views:
        text = text + "".join("\n\n" + v for v in views)
    n = normalize.normalize(text)
    names = matcher.find(n.text)
    spans: list[Span] = list(names)
    for s in patterns.find_structured(n.text):
        if any(a.start <= s.start and s.end <= a.end for a in names):
            continue
        spans.append(s)
    spans.sort(key=lambda s: (s.start, s.end))
    return public(spans)


def check_text(text: str, register_path: Path | None) -> list[dict]:
    """Every name and structured hit in `text` as {start, length, cls}, what hides behind a base64 or hex block
    and under a bidi override included (positions past the end of `text` point into those views). Local when
    the register is readable, else through the vault daemon. Raises RegisterError on a bad register and
    CheckUnavailable (a RegisterError) when the names cannot be checked at all."""
    return checker(register_path).scan(with_hidden_views(text))


def _opaque() -> dict:
    return {"start": 0, "length": 0, "cls": OPAQUE}


def _extract_module():
    """awb.extract when it is present, else None. Imported only when a file needs it."""
    try:
        from awb import extract as mod
    except ImportError:
        return None
    return mod


def _base64_texts(text: str) -> tuple[list[str], bool]:
    """What hides behind the base64 and hex blocks of `text` (decoded text, strings of binary payloads), when
    the text reader of awb.extract is present, and whether blocks were left undecoded at a limit."""
    try:
        from awb.extract.text import encoded_texts
    except ImportError:
        return [], False
    more: list = []
    out = encoded_texts(text, more=more)
    return out, bool(more)


def _plain_text(path: Path, data: bytes) -> tuple[str | None, bool]:
    """The text of a plain UTF-8 text file and whether encoded blocks were left undecoded at a limit. None
    when the file needs a reader or is binary."""
    head = data[:_SNIFF_BYTES]
    if b"\x00" in head or head.startswith(_BINARY_MAGIC):
        return None, False
    try:
        text = data.decode("utf-8-sig")
    except UnicodeDecodeError:
        return None, False
    mod = _extract_module()
    kind = "text"
    if mod is not None:
        try:
            kind = mod.sniff(path)
        except Exception:
            kind = "text"
        if kind not in _PLAIN_KINDS:
            # a mail file keeps encoded headers and bodies that only its reader decodes
            return None, False
    extra, more = _base64_texts(text)
    if kind == "html" or (mod is not None and _looks_html(text)):
        # the raw page shows a name split by tags to the matcher; the stripped text shows it as it renders
        extra.insert(0, _strip_html(text))
    if extra:
        text = text + "".join("\n\n" + b for b in extra if b)
    return text, more


def _looks_html(text: str) -> bool:
    try:
        from awb.extract.text import looks_html
    except ImportError:
        return False
    return looks_html(text[:4096])


def _strip_html(text: str) -> str:
    try:
        from awb.extract.text import strip_html
    except ImportError:
        return ""
    return strip_html(text)


def _flatten(ex, texts: list[str], raw: list[str]) -> bool:
    """Append the detect text and the raw part text of an extraction and its children, parent first. True if
    any was not read in full."""
    body = ex.detect_text or ex.text or ""
    if body:
        texts.append(body)
    if getattr(ex, "scan_text", ""):
        raw.append(ex.scan_text)
    opaque = ex.state != "ok" or bool(ex.meta.get("incomplete"))
    for child in ex.children or []:
        opaque = _flatten(child, texts, raw) or opaque
    return opaque


def _check_extracted(path: Path, names) -> list[dict]:
    mod = _extract_module()
    if mod is None:
        return [_opaque()]
    ex = mod.extract(path)
    texts: list[str] = []
    raw: list[str] = []
    opaque = _flatten(ex, texts, raw)
    joined = "\n\n".join(texts)
    hits = names.scan(joined) if texts else []
    if raw:
        # raw parts are checked for registered forms only; positions continue after the detect text
        offset = len(normalize.normalize(joined).text) + 2 if texts else 0
        hits.extend({"start": offset + s.start, "length": s.end - s.start, "cls": s.cls}
                    for s in names.name_spans("\n\n".join(raw)))
    if opaque or not texts:
        hits.insert(0, _opaque())
    return hits


def _check_file(path: Path, matcher) -> list[dict]:
    names = _as_checker(matcher)
    path = Path(path)
    data = path.read_bytes()
    text, more = _plain_text(path, data)
    if text is not None:
        hits = names.scan(text)
        if more:
            # encoded blocks past the limit were not decoded: a file not read in full is never called clean
            hits.append(_opaque())
        return hits
    return _check_extracted(path, names)


def _path_hits(path: Path, matcher) -> list[dict]:
    """One `path` record when the file name carries a registered form."""
    names = _as_checker(matcher)
    name = Path(path).name
    hit = names.name_spans(name) or any(names.name_spans(v) for v in _bidi_views(name))
    return [{"start": 0, "length": 0, "cls": PATH}] if hit else []


def check_file(path: Path, register_path: Path | None) -> list[dict]:
    """Every hit in the file as {start, length, cls}, a `path` record first when its name carries a registered
    form. An OSError of the read is raised to the caller, CheckUnavailable when names cannot be checked."""
    names = checker(register_path)
    hits = _check_file(Path(path), names)
    return _path_hits(path, names) + hits


def printable_path(path, index: int, matcher) -> str:
    """The path as given when it carries no registered form, else "file N". `matcher` is a Matcher, a
    LocalCheck or a RemoteCheck."""
    return "file %d" % index if _as_checker(matcher).name_spans(str(path)) else str(path)


def main(argv: list[str] | None = None) -> int:
    """`awb check [--register PATH] [--no-register] FILE...`. Exit 0 clean, 1 on any hit, 2 on an error."""
    from awb.cli import SafeParser

    ap = SafeParser(
        prog="awb check",
        description="Check files for registered names and structured data. Prints positions and classes only.",
    )
    ap.add_argument("--register", type=Path, default=None, help="register file (default: the vault register)")
    ap.add_argument("--no-register", action="store_true",
                    help="run without a register: structured data only, names are not checked")
    ap.add_argument("files", nargs="+", type=Path)
    try:
        args = ap.parse_args(argv)
    except SystemExit as exc:
        return exc.code if isinstance(exc.code, int) else 2

    register_path = args.register
    if register_path is None:
        from awb import config

        register_path = config.paths().register
    try:
        if args.no_register:
            names = LocalCheck(Matcher([]))
        else:
            kind, _ = register_source(register_path)
            if kind == MISSING:
                print("awb check: no register found, names cannot be checked; give --register PATH or "
                      "--no-register for structured data only", file=sys.stderr)
                return 2
            names = checker(register_path, rate_wait=RATE_WAIT)
            if isinstance(names, RemoteCheck):
                names.ping()
        # nothing is printed before every file is checked: an unavailable check prints no hit list at all
        out: list[str] = []
        err_lines: list[str] = []
        hits = 0
        for i, f in enumerate(args.files, start=1):
            shown = printable_path(f, i, names)
            if not f.is_file():
                err_lines.append("awb check: %s is not a readable file" % shown)
                continue
            try:
                found = _path_hits(f, names) + _check_file(f, names)
            except OSError as err:
                err_lines.append("awb check: %s cannot be read (%s)" % (shown, type(err).__name__))
                continue
            out.extend(json.dumps({"file": shown, "start": h["start"], "length": h["length"], "cls": h["cls"]})
                       for h in found)
            hits += len(found)
    except register.RegisterError as err:   # CheckUnavailable included
        print("awb check: %s" % err, file=sys.stderr)
        return 2
    for line in out:
        print(line)
    for line in err_lines:
        print(line, file=sys.stderr)
    if err_lines:
        return 2
    return 1 if hits else 0


if __name__ == "__main__":
    sys.exit(main())
