"""STATE.md kept short: `awb state rotate [--keep N] [--project DIR]`.

A STATE.md grows one dated layer per step. A layer is a heading (level 2 to 4) whose text starts with a date
`YYYY-MM-DD`, with everything under it up to the next heading of its level or a higher one. Rotating keeps the
layers of the last N dates (3 unless `--keep` says other; the layers of one date stay together) and moves every
older one to `history/STATE-YYYY-MM-DD.md` of the project, one file per date of the layer. What is not a dated layer
stays where it is: the title, the Status: and Next: lines on top and every other heading.

The pinned block (Q-0006, awb/status.py): a line of STATE.md that starts with `pinned:` (also as `- pinned:`) and
then reads `Kind: text` with a Kind of `status.PIN_KINDS` is a standing fact. Every such line, in a layer that moves
too, becomes an entry `- Kind: text` of the `## Pinned` block, after the entries the block has already; the block is
made when there is none (before the first heading). The marked line itself stays where it was written, so a second
rotate finds the same entries and changes nothing. A marked line of another kind, or more entries than the block
may hold, is a refusal and nothing is written.
"""
from __future__ import annotations

import os
import re
import sys
from pathlib import Path

from awb import status

KEEP = 3
"""The dated layers (distinct dates) that stay in STATE.md unless `--keep` says other."""
HISTORY = "history"
_HEADING_RE = re.compile(r"^(#{2,4})[ \t]+(\d{4}-\d{2}-\d{2})\b")
_ANY_HEADING_RE = re.compile(r"^(#{1,6})[ \t]")
_MARK_RE = re.compile(r"^[ \t]*(?:[-*][ \t]+)?pinned:[ \t]*(.*?)[ \t]*$", re.I)


class Refused(Exception):
    """A rotation that cannot be made as asked; the message carries no text of the file."""


def _layers(lines: list[str]) -> list[tuple[int, int, str]]:
    """(first line, end line, date) of every dated layer, in file order. The end is the next heading of the same or
    a higher level, or the end of the text."""
    out = []
    n = 0
    while n < len(lines):
        m = _HEADING_RE.match(lines[n])
        if not m:
            n += 1
            continue
        level, end = len(m.group(1)), n + 1
        while end < len(lines):
            h = _ANY_HEADING_RE.match(lines[end])
            if h and len(h.group(1)) <= level:
                break
            end += 1
        out.append((n, end, m.group(2)))
        n = end
    return out


def marked(text: str) -> list[str]:
    """The entries `- Kind: text` of the lines marked `pinned:`, in order, each once. Raises Refused for a line
    whose kind is none of status.PIN_KINDS."""
    out: list[str] = []
    bad = 0
    for line in text.splitlines():
        m = _MARK_RE.match(line)
        if not m:
            continue
        body = m.group(1)
        kind, _, rest = body.partition(":")
        kind = next((k for k in status.PIN_KINDS if k.lower() == kind.strip().lower()), None)
        if kind is None or not rest.strip():
            bad += 1
            continue
        entry = "- %s: %s" % (kind, rest.strip())
        if entry.lower() not in (e.lower() for e in out):
            out.append(entry)
    if bad:
        raise Refused("%d line(s) marked pinned: do not read `Kind: text` with a kind of %s"
                      % (bad, ", ".join(status.PIN_KINDS)))
    return out


def _with_pinned(lines: list[str], entries: list[str]) -> list[str]:
    """`lines` with the entries added to the pinned block (made when missing); unchanged when all are in."""
    starts = [n for n, line in enumerate(lines) if line.rstrip() == status.PIN_HEADING]
    if not starts:
        if not entries:
            return lines
        at = next((n for n, line in enumerate(lines) if n and _ANY_HEADING_RE.match(line)), len(lines))
        block = [status.PIN_HEADING, ""] + entries + [""]
        if at == len(lines) and lines and lines[-1].strip():
            block = [""] + block
        return lines[:at] + block + lines[at:]
    first = starts[0]
    end = first + 1
    while end < len(lines) and not lines[end].startswith("#"):
        end += 1
    have = {line.strip().lower() for line in lines[first + 1:end]}
    new = [e for e in entries if e.lower() not in have]
    if not new:
        return lines
    last = max([n for n in range(first + 1, end) if lines[n].strip()] or [first])
    return lines[:last + 1] + new + lines[last + 1:]


def rotate(root: Path, keep: int = KEEP) -> tuple[int, list[str]]:
    """Rotate the STATE.md of the project `root`. Returns the number of layers moved and the history files written
    (relative paths). Raises Refused, and writes nothing, when the pinned lines are wrong or the block would grow past
    its limits."""
    if keep < 0:
        raise Refused("--keep takes a number of 0 or more")
    path = root / status.STATE
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        raise Refused("%s cannot be read" % status.STATE) from None
    entries = marked(text)
    lines = text.split("\n")
    layers = _layers(lines)
    stay = set(sorted({d for _, _, d in layers}, reverse=True)[:keep])
    top = {n for n, line in enumerate(lines) if status._LINE_RE["summary"].match(line)
           or status._LINE_RE["next"].match(line)}
    moving = [(a, b, d) for a, b, d in layers if d not in stay and not any(a <= n < b for n in top)]
    kept = lines
    if moving:
        drop = {n for a, b, _ in moving for n in range(a, b)}
        kept = [line for n, line in enumerate(lines) if n not in drop]
        # the blank lines a removed layer leaves between two others fold into one
        folded: list[str] = []
        for line in kept:
            if not line.strip() and len(folded) > 1 and not folded[-1].strip():
                continue
            folded.append(line)
        kept = folded
    kept = _with_pinned(kept, entries)
    new_text = "\n".join(kept)
    problem = status.pin_shape(new_text)
    if problem and status.pin_shape(text) != problem:
        raise Refused(problem)
    written: list[str] = []
    by_date: dict[str, list[str]] = {}
    for a, b, d in moving:
        by_date.setdefault(d, []).append("\n".join(lines[a:b]).strip("\n"))
    for d, chunks in sorted(by_date.items()):
        rel = "%s/STATE-%s.md" % (HISTORY, d)
        target = root / rel
        old = target.read_text(encoding="utf-8") if target.exists() else "# State history of %s\n" % d
        add = [c for c in chunks if c not in old]
        if not add:
            continue
        target.parent.mkdir(exist_ok=True)
        _write(target, old.rstrip("\n") + "\n\n" + "\n\n".join(add) + "\n")
        written.append(rel)
    if new_text != text:
        _write(path, new_text)
    return len(moving), written


def _write(path: Path, text: str) -> None:
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


def main(argv: list[str] | None = None) -> int:
    from awb.cli import SafeParser

    argv = list(sys.argv[1:] if argv is None else argv)
    ap = SafeParser(prog="awb state", description="STATE.md kept short.")
    sub = ap.add_subparsers(dest="command")
    s = sub.add_parser("rotate", help="move the dated layers older than the last N to history/STATE-DATE.md and "
                                      "write the pinned block from the lines marked pinned:")
    s.add_argument("--keep", type=int, default=KEEP, metavar="N", help="dated layers that stay (default %d)" % KEEP)
    s.add_argument("--project", default=None)
    args = ap.parse_args(argv)
    if args.command != "rotate":
        ap.print_usage(sys.stderr)
        return 2
    from awb import review

    try:
        root = Path(args.project) if args.project else review.find_project()
        moved, files = rotate(root, args.keep)
    except Refused as exc:
        print("awb state rotate: %s" % exc, file=sys.stderr)
        return 1
    except Exception:
        print("awb state rotate: not in a project folder", file=sys.stderr)
        return 2
    print("%d layer(s) moved%s" % (moved, (" to " + ", ".join(files)) if files else ""))
    return 0
