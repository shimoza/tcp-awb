"""`awb paste [--as NAME]`: a long terminal output goes into the project as a file, not into the chat (T8).

The text comes on standard input (the clipboard piped in). It passes the name check and the secret detectors of
the gate first. A registered name, a secret or structured data outside `hooks.SOFT_CLASSES` (a mail, bank data,
a register or tax number) refuses it: nothing is written and the message names the classes only. Addresses,
web addresses, phone numbers and MAC addresses are masked (`[ip]`, `[url]`, `[phone]`, `[mac]`) and the text is
written to `notes/pastes/<date>-<n>.txt` of the project (`<date>-<NAME>.txt` with --as). The command prints the
path and the counts; the session then reads the file. Exit 0 written, 2 refused, 1 a usage error.
"""
from __future__ import annotations

import re
import sys
from datetime import date
from pathlib import Path

MAX_BYTES = 1024 * 1024
"""The largest paste taken: the name check reads about a megabyte a second."""
NAME_RE = re.compile(r"[a-z0-9][a-z0-9-]{0,39}")
FOLDER = Path("notes") / "pastes"


class PasteError(Exception):
    """A refused paste. The message carries classes and counts, never text of the paste."""


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
    folder = root / FOLDER
    if name is not None:
        path = folder / ("%s-%s.txt" % (today.isoformat(), name))
        if path.exists():
            raise PasteError("%s exists already: choose another --as" % path.relative_to(root))
        return path
    n = 1
    while (folder / ("%s-%d.txt" % (today.isoformat(), n))).exists():
        n += 1
    return folder / ("%s-%d.txt" % (today.isoformat(), n))


def paste(text: str, root: Path, name: str | None = None, today: date | None = None) -> tuple[Path, dict, int]:
    """Check `text`, mask it and write it into the project at `root`. Returns the path, the masked counts per
    class and the count of soft hits left in a decoded block. Raises PasteError, hooks.Unavailable."""
    from awb import hooks

    if not text.strip():
        raise PasteError("standard input is empty: pipe the text in")
    if len(text.encode("utf-8")) > MAX_BYTES:
        raise PasteError("the text is over %d MB: split it" % (MAX_BYTES // (1024 * 1024)))
    if name is not None and not NAME_RE.fullmatch(name):
        raise PasteError("--as takes lower case letters, digits and hyphens, up to 40")
    hits = hooks.name_hits(text)
    if name is not None and hooks.name_hits(name):
        raise PasteError("the name of --as carries a registered name: choose a code or a plain word")
    secrets = hooks._secret_hits(text)
    hard = [h for h in hits if h.get("cls") not in hooks.SOFT_CLASSES]
    if hard or secrets:
        found = [{"cls": "registered name" if h.get("cls") == "name" else h.get("cls")} for h in hard] + secrets
        raise PasteError("the text carries %s: nothing was written. A registered name, a mail, bank data and a "
                         "secret never go into a project." % hooks._counts(found))
    soft = [h for h in hits if h.get("cls") in hooks.SOFT_CLASSES]
    masked, outside = mask(text, soft)
    path = _target(root, name, today or date.today())
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "x", encoding="utf-8") as fh:
        fh.write(masked if masked.endswith("\n") else masked + "\n")
    counts: dict[str, int] = {}
    for h in soft:
        counts[str(h.get("cls"))] = counts.get(str(h.get("cls")), 0) + 1
    return path, counts, outside


def main(argv: list[str] | None = None) -> int:
    """`awb paste [--as NAME]` in a project folder, the text on standard input."""
    import os

    from awb import hooks
    from awb.cli import SafeParser

    ap = SafeParser(prog="awb paste", description="Put a long terminal output into the project, masked, instead "
                                                   "of the chat. The text comes on standard input.")
    ap.add_argument("--as", dest="name", default=None, metavar="NAME", help="name of the file instead of a number")
    try:
        args = ap.parse_args(argv)
    except SystemExit as exc:
        return exc.code if isinstance(exc.code, int) else 1
    root = hooks.project_root(os.getcwd())
    if root is None:
        print("awb paste: run it in a project folder (one with a SCOPE.md)", file=sys.stderr)
        return 2
    try:
        path, counts, outside = paste(sys.stdin.read(), root, args.name)
    except PasteError as err:
        print("awb paste: %s" % err, file=sys.stderr)
        return 2
    except hooks.Unavailable as exc:
        print("awb paste: the name check did not run (%s): nothing was written" % exc, file=sys.stderr)
        return 2
    except UnicodeDecodeError:
        print("awb paste: standard input is not UTF-8 text", file=sys.stderr)
        return 2
    lines = path.read_text(encoding="utf-8").count("\n")
    said = ", ".join("%s %d" % kv for kv in sorted(counts.items())) or "nothing"
    print("awb paste: %s, %d lines, masked %s" % (path.relative_to(root), lines, said))
    if outside:
        print("awb paste: %d data items inside an encoded block were not masked" % outside)
    return 0


if __name__ == "__main__":
    sys.exit(main())
