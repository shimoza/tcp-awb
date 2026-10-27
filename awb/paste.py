"""`awb paste [--own] [--as NAME] [--retry STAMP]`: a long terminal output goes into the project as a file, not
into the chat (T8, P1).

The text comes on standard input (the clipboard piped in). Without --own it is customer text and gets the entry
check: it passes the name check and the secret detectors of the gate first. A registered name, a secret or
structured data outside `hooks.SOFT_CLASSES` (a mail, bank data, a register or tax number) refuses it: nothing is
written and the message names the classes only. Addresses, web addresses, phone numbers and MAC addresses are
masked (`[ip]`, `[url]`, `[phone]`, `[mac]`) and the text is written to `notes/pastes/<date>-<n>.txt` of the project
(`<date>-<NAME>.txt` with --as; a name taken today gets a number, `<date>-<NAME>-2.txt`, and the command says so).
The command prints the path and the counts; the session then reads the file. Exit 0 written, 2 refused, 1 a usage
error.

--own is for output our own tools made (a PoC script, a lab run on our own tenant, an answer of the key service):
it carries no new customer data. Every id, address, host and other structured item stays; a registered name still
refuses it (task GT, 2026-10-27). Its secrets are handled by awb/labsecrets.py: passwords and logins masked,
temporary tokens and keys shortened to 8 characters and the length, a permanent key pair `[secret]`; what the
secret finding of the gate (classes secret, private-key, token) still finds after that becomes `[secret]` in place,
the rest of the line kept.

A refused paste is never lost: the raw text is kept 0600 in the pasting user's own state folder
(`$XDG_STATE_HOME/awb/refused-pastes/<stamp>-<name>.txt`, default `~/.local/state`, the folder 0700, never inside a
project), and the refusal names `awb paste --retry STAMP` and `awb paste --own --retry STAMP`. A retry reads that
file as its input, a written paste deletes it, and a kept file older than seven days goes at the next paste. The
kept text is never printed.
"""
from __future__ import annotations

import os
import re
import sys
import time
from datetime import date, datetime
from pathlib import Path

MAX_BYTES = 1024 * 1024
"""The largest paste taken: the name check reads about a megabyte a second."""
OWN_MAX_BYTES = 8 * 1024 * 1024
"""The largest own output taken: no name check runs on it, only the secret detectors."""
NAME_RE = re.compile(r"[a-z0-9][a-z0-9-]{0,39}")
FOLDER = Path("notes") / "pastes"
SECRET = "[secret]"
KEEP_DAYS = 7
STAMP_RE = re.compile(r"[0-9]{8}-[0-9]{6}[a-z]?")
_KEPT_RE = re.compile(r"(?P<stamp>[0-9]{8}-[0-9]{6}[a-z]?)-(?P<name>[a-z0-9][a-z0-9-]{0,39})\.txt")


class PasteError(Exception):
    """A refused paste. The message carries classes and counts, never text of the paste."""


class PasteRefused(PasteError):
    """The text itself was refused (a name, a secret, a mail, bank data): the raw input is kept for a retry."""


def mask(text: str, hits: list[dict]) -> tuple[str, int]:
    """`text` with every hit of a soft class replaced by `[cls]`. Positions of the hits are in the normalised
    text and are mapped back to `text`. Returns the masked text and the number of hits that lie outside it (in
    a decoded block) and so stay as they are."""
    from awb import normalize

    norm = normalize.normalize(text)
    spans: list[tuple[int, int, str]] = []
    outside = 0
    for h in hits:
        start, length = h.get("start", 0), h.get("length", 0)
        if not (isinstance(start, int) and isinstance(length, int)) or length <= 0 \
                or start + length > len(norm.text):
            outside += 1
            continue
        a, b = normalize.original_span(norm, start, start + length)
        spans.append((a, b, str(h.get("cls"))))
    out = text
    last = len(text) + 1
    for a, b, cls in sorted(spans, reverse=True):
        if b > last:          # overlaps the span masked before it
            b = last
        if b <= a:
            continue
        out = out[:a] + "[%s]" % cls + out[b:]
        last = a
    return out, outside


def _target(root: Path, name: str | None, today: date) -> Path:
    """The file of the paste. A name taken today gets the first free number (`<date>-<NAME>-2.txt`)."""
    folder = root / FOLDER
    if name is not None:
        path = folder / ("%s-%s.txt" % (today.isoformat(), name))
        n = 2
        while path.exists() or path.is_symlink():
            path = folder / ("%s-%s-%d.txt" % (today.isoformat(), name, n))
            n += 1
        return path
    n = 1
    while (folder / ("%s-%d.txt" % (today.isoformat(), n))).exists():
        n += 1
    return folder / ("%s-%d.txt" % (today.isoformat(), n))


def _write(root: Path, name: str | None, today: date | None, text: str) -> Path:
    path = _target(root, name, today or date.today())
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "x", encoding="utf-8") as fh:
        fh.write(text if text.endswith("\n") else text + "\n")
    return path


def paste(text: str, root: Path, name: str | None = None, today: date | None = None) -> tuple[Path, dict, int]:
    """Check `text`, mask it and write it into the project at `root`. Returns the path, the masked counts per
    class and the count of soft hits left in a decoded block. Raises PasteError (PasteRefused for the text),
    hooks.Unavailable."""
    from awb import hooks

    if not text.strip():
        raise PasteError("standard input is empty: pipe the text in")
    if len(text.encode("utf-8")) > MAX_BYTES:
        raise PasteError("the text is over %d MB: split it" % (MAX_BYTES // (1024 * 1024)))
    if name is not None and not NAME_RE.fullmatch(name):
        raise PasteError("--as takes lower case letters, digits and hyphens, up to 40")
    hits = hooks.name_hits(text)
    if name is not None and hooks.name_hits(name):
        raise PasteRefused("the name of --as carries a registered name: choose a code or a plain word")
    secrets = hooks._secret_hits(text)
    hard = [h for h in hits if h.get("cls") not in hooks.SOFT_CLASSES]
    if hard or secrets:
        found = [{"cls": "registered name" if h.get("cls") == "name" else h.get("cls")} for h in hard] + secrets
        raise PasteRefused("the text carries %s: nothing was written. A registered name, a mail, bank data and a "
                           "secret never go into a project." % hooks._counts(found))
    soft = [h for h in hits if h.get("cls") in hooks.SOFT_CLASSES]
    masked, outside = mask(text, soft)
    path = _write(root, name, today, masked)
    counts: dict[str, int] = {}
    for h in soft:
        counts[str(h.get("cls"))] = counts.get(str(h.get("cls")), 0) + 1
    return path, counts, outside


# --------------------------------------------------------------------------- our own output (--own)


_STOP = frozenset(" \t\r\n\"'`<")
"""A secret value ends at a blank, a quote or the start of a closing tag."""
_PRIVATE_END_RE = re.compile(r"-{4,5} ?END (?:[A-Z0-9]+[ \t]+)*PRIVATE[ \t]+KEY(?: BLOCK)? ?-{4,5}")
_PUTTY_END_RE = re.compile(r"(?m)^Private-MAC:[^\n]*")
_LINES_RE = re.compile(r"Private-Lines: ([0-9]+)")
_LONG_RUN_RE = re.compile(r"[A-Za-z0-9+/=_-]{16,}")


def _value_end(text: str, start: int) -> int:
    end = start
    while end < len(text) and text[end] not in _STOP:
        end += 1
    return end


def _line_end(text: str, start: int, lines: int) -> int:
    """The end of the line `lines - 1` lines after the one of `start` (its newline kept)."""
    end = start
    for _ in range(lines):
        nl = text.find("\n", end)
        if nl < 0:
            return len(text)
        end = nl + 1
    return end - 1


def _span(text: str, cls: str, off: int) -> tuple[int, int]:
    """The span of the value a secret detector of the gate found at `off`: a private key block to its end line,
    a value to the next blank or quote."""
    if cls == "private-key":
        if text.startswith("PuTTY-User-Key-File", off):
            m = _PUTTY_END_RE.search(text, off)
            return off, m.end() if m else len(text)
        m = _LINES_RE.match(text, off)
        if m:
            return off, _line_end(text, off, int(m.group(1)) + 1)
        if text.startswith('"d"', off):
            start = text.find('"', text.find(":", off)) + 1
            return start, _value_end(text, start)
        m = _PRIVATE_END_RE.search(text, off)
        return off, m.end() if m else len(text)
    start = off + 1 if off < len(text) and text[off] in "\"'`" else off
    return start, _value_end(text, start)


def _keypair_ids(text: str, spans: list[tuple[int, int]]) -> list[tuple[int, int]]:
    """The key id of every key pair whose secret is in `spans`: the id goes with its secret."""
    from awb import gate

    out: list[tuple[int, int]] = []
    starts = gate._line_starts(text)
    for m in gate._AK_RE.finditer(text):
        line = text.count("\n", 0, m.start())
        lo = starts[max(0, line - gate._KEYPAIR_LINES)]
        hi_line = line + gate._KEYPAIR_LINES + 1
        hi = starts[hi_line] if hi_line < len(starts) else len(text)
        if any(lo <= a < hi and gate._SK_RE.fullmatch(text[a:b]) for a, b in spans):
            out.append((m.start(), m.end()))
    return out


def _replace(text: str, spans: list[tuple[int, int]]) -> tuple[str, int]:
    merged: list[list[int]] = []
    for a, b in sorted(s for s in spans if s[1] > s[0]):
        if merged and a <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], b)
        else:
            merged.append([a, b])
    for a, b in reversed(merged):
        text = text[:a] + SECRET + text[b:]
    return text, len(merged)


def own_secrets(text: str) -> tuple[str, int]:
    """`text` with every value the gate's secret detectors find replaced by [secret] in place, and the count.
    What the passes leave (a secret inside an encoded block) goes by its long runs, then by its whole line, so
    no secret the gate finds is left."""
    from awb import gate, hooks

    total = 0
    for _ in range(3):
        hits = [(cls, off) for cls, off in gate._detect_all(text, None) if cls in hooks.SECRET_CLASSES]
        if not hits:
            break
        spans = [_span(text, cls, off) for cls, off in hits]
        text, n = _replace(text, spans + _keypair_ids(text, spans))
        total += n
    for whole in (False, True):
        lines = sorted({f.line for f in gate._scan_text("paste", text, None)
                        if f.cls in hooks.SECRET_CLASSES and f.line > 0})
        if not lines:
            break
        rows = text.split("\n")
        for n in lines:
            if 0 < n <= len(rows):
                rows[n - 1] = SECRET if whole else _LONG_RUN_RE.sub(SECRET, rows[n - 1])
                total += 1
        text = "\n".join(rows)
    return text, total


def own(text: str, root: Path, name: str | None = None, today: date | None = None) -> tuple[Path, int]:
    """Our own output into the project at `root`: its ids kept, its secrets shortened or replaced, refused only for
    a registered name (PasteRefused, the text kept for a retry). Returns the path and the number of secrets
    shortened or replaced. Raises PasteError, hooks.Unavailable."""
    from awb import hooks, labsecrets

    if not text.strip():
        raise PasteError("standard input is empty: pipe the text in")
    if len(text.encode("utf-8")) > OWN_MAX_BYTES:
        raise PasteError("the text is over %d MB: split it" % (OWN_MAX_BYTES // (1024 * 1024)))
    if name is not None and not NAME_RE.fullmatch(name):
        raise PasteError("--as takes lower case letters, digits and hyphens, up to 40")
    if name is not None and [h for h in hooks.name_hits(name) if h.get("cls") in NAME_CLASSES]:
        raise PasteRefused("the name of --as carries a registered name: choose a code or a plain word")
    named = [h for h in hooks.name_hits(text) if h.get("cls") in NAME_CLASSES]
    if named:
        raise PasteRefused("the text carries %d registered name(s): nothing was written. A customer name never goes "
                           "into a project." % len(named))
    short, counts = labsecrets.shorten(text)
    clean, n = own_secrets(short)
    return _write(root, name, today, clean), n + sum(counts.values())


NAME_CLASSES = ("name", "path")
"""What still refuses our own output (task GT): a registered name."""


# --------------------------------------------------------------------------- a refused paste is kept


def keep_dir() -> Path:
    """The pasting user's own folder for refused pastes, outside every project."""
    base = os.environ.get("XDG_STATE_HOME") or str(Path.home() / ".local" / "state")
    return Path(base) / "awb" / "refused-pastes"


def _inside(path: Path, root: Path) -> bool:
    try:
        Path(os.path.realpath(path)).relative_to(os.path.realpath(root))
        return True
    except ValueError:
        return False


def keep(text: str, name: str | None, root: Path, now: datetime | None = None) -> str | None:
    """Keep the raw `text` of a refused paste 0600 under keep_dir(); the stamp, or None when it cannot be kept
    (the folder is a link, lies inside the project or cannot be written)."""
    folder = keep_dir()
    if _inside(folder, root):
        return None
    try:
        folder.mkdir(parents=True, exist_ok=True, mode=0o700)
        if folder.is_symlink() or not folder.is_dir():
            return None
        os.chmod(folder, 0o700)
    except OSError:
        return None
    base = (now or datetime.now()).strftime("%Y%m%d-%H%M%S")
    for suffix in [""] + [chr(c) for c in range(ord("a"), ord("z") + 1)]:
        stamp = base + suffix
        if kept(stamp) is not None:
            continue
        path = folder / ("%s-%s.txt" % (stamp, name or "paste"))
        try:
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        except FileExistsError:
            continue
        except OSError:
            return None
        try:
            os.fchmod(fd, 0o600)
            os.write(fd, text.encode("utf-8"))
        finally:
            os.close(fd)
        return stamp
    return None


def kept(stamp: str) -> Path | None:
    """The kept file of `stamp`, or None."""
    if not STAMP_RE.fullmatch(stamp or ""):
        return None
    try:
        for path in keep_dir().iterdir():
            m = _KEPT_RE.fullmatch(path.name)
            if m and m.group("stamp") == stamp and not path.is_symlink() and path.is_file():
                return path
    except OSError:
        return None
    return None


def kept_name(path: Path) -> str | None:
    """The --as name a kept file was refused under, None for a paste without one."""
    m = _KEPT_RE.fullmatch(path.name)
    return None if m is None or m.group("name") == "paste" else m.group("name")


def clean_kept(now: float | None = None) -> int:
    """Delete the kept files older than KEEP_DAYS; their count."""
    limit = (now if now is not None else time.time()) - KEEP_DAYS * 86400
    gone = 0
    try:
        paths = list(keep_dir().iterdir())
    except OSError:
        return 0
    for path in paths:
        try:
            if _KEPT_RE.fullmatch(path.name) and not path.is_symlink() and path.is_file() \
                    and path.stat().st_mtime < limit:
                path.unlink()
                gone += 1
        except OSError:
            continue
    return gone


def retry_line(stamp: str | None) -> str:
    if stamp is None:
        return "The text could not be kept: paste it again."
    return ("The text is kept for %d days: retry with `awb paste --retry %s`, or with `awb paste --own --retry %s` "
            "when it is our own output (a script or a lab run of ours)." % (KEEP_DAYS, stamp, stamp))


# --------------------------------------------------------------------------- the command


def main(argv: list[str] | None = None) -> int:
    """`awb paste [--own] [--as NAME] [--retry STAMP]` in a project folder, the text on standard input."""
    from awb import hooks
    from awb.cli import SafeParser

    ap = SafeParser(prog="awb paste", description="Put a long terminal output into the project, masked, instead "
                                                   "of the chat. The text comes on standard input.")
    ap.add_argument("--as", dest="name", default=None, metavar="NAME", help="name of the file instead of a number")
    ap.add_argument("--own", action="store_true",
                    help="our own output (a script or a lab run of ours): ids stay, secrets are shortened, only a "
                         "registered name refuses")
    ap.add_argument("--retry", default=None, metavar="STAMP", help="paste the kept text of a refused paste again")
    try:
        args = ap.parse_args(argv)
    except SystemExit as exc:
        return exc.code if isinstance(exc.code, int) else 1
    root = hooks.project_root(os.getcwd())
    if root is None:
        print("awb paste: run it in a project folder (one with a SCOPE.md)", file=sys.stderr)
        return 2
    clean_kept()
    source: Path | None = None
    name = args.name
    try:
        if args.retry is not None:
            source = kept(args.retry)
            if source is None:
                print("awb paste: no kept paste %s (a kept paste stays %d days)"
                      % (args.retry if STAMP_RE.fullmatch(args.retry) else "of that stamp", KEEP_DAYS),
                      file=sys.stderr)
                return 2
            text = source.read_text(encoding="utf-8")
            name = name or kept_name(source)
        else:
            text = sys.stdin.read()
    except UnicodeDecodeError:
        print("awb paste: standard input is not UTF-8 text", file=sys.stderr)
        return 2
    try:
        if args.own:
            path, replaced = own(text, root, name)
            counts, outside = {}, 0
        else:
            path, counts, outside = paste(text, root, name)
    except PasteRefused as err:
        stamp = args.retry if source is not None else keep(text, name, root)
        print("awb paste: %s. %s" % (str(err).rstrip("."), retry_line(stamp)), file=sys.stderr)
        return 2
    except PasteError as err:
        print("awb paste: %s" % err, file=sys.stderr)
        return 2
    except hooks.Unavailable as exc:
        stamp = args.retry if source is not None else keep(text, name, root)
        print("awb paste: the name check did not run (%s): nothing was written. %s" % (exc, retry_line(stamp)),
              file=sys.stderr)
        return 2
    if source is not None:
        try:
            source.unlink()
        except OSError:
            pass
    if name is not None and path.name != "%s-%s.txt" % (path.name[:10], name):
        print("awb paste: the name %s is taken today, written as %s" % (name, path.stem[11:]))
    lines = path.read_text(encoding="utf-8").count("\n")
    if args.own:
        print("awb paste: own output: %d secrets replaced, %s, %d lines" % (replaced, path.relative_to(root), lines))
        return 0
    said = ", ".join("%s %d" % kv for kv in sorted(counts.items())) or "nothing"
    print("awb paste: %s, %d lines, masked %s" % (path.relative_to(root), lines, said))
    if outside:
        print("awb paste: %d data items inside an encoded block were not masked" % outside)
    return 0


if __name__ == "__main__":
    sys.exit(main())
