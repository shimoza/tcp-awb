"""`awb material add [--kind mail|transcript|table|note] [--title WORDS]`: text the owner pasted into the chat of
a project session, kept as project material (CS6).

The text is read from standard input inside a project folder (one with a SCOPE.md) and passes the checks that need
no vault: the secret scan of the gate, its home-path rule and the owner's blocklist, then the name check through
the check socket of the work user. Any hit refuses the whole text: nothing is written and the message names the
class and the line, never the value. The owner has said the text holds no name at all, so a structured item (a
mail, a web address, a phone number) refuses it as well.

On a pass the text is written unchanged to `input/` of the project as `note-<date>-<n>.md`, `mail-<date>-<n>.md`,
`transcript-<date>-<n>.md` or `table-<date>-<n>.csv` (the numbers the paste door uses), one line goes to the
ledger and the command prints the file name only. Exit 0 written, 2 refused, 1 written but the ledger line is not.
The owner's own door for text that may hold names stays `awb import CODE --paste`.
"""
from __future__ import annotations

import bisect
import os
import re
import sys
from collections import Counter
from datetime import date
from pathlib import Path

from awb import codes, config, pastein

KINDS = ("note",) + pastein.KINDS
NAMES = {"note": ("note", ".md"), "mail": ("mail", ".md"), "transcript": ("transcript", ".md"),
         "table": ("table", ".csv")}
FOLDER = "input"
MAX_TITLE = 80
SCREENED = ("secret", "private-key", "token", "homepath", "blocklist")
"""The classes of the gate that need no register: the secret classes, the home path and the owner's blocklist."""


class MaterialRefused(Exception):
    """The text is not kept. The message carries classes and lines, never a part of the text."""


def _lines(text: str, offsets: list[tuple[str, int]]) -> list[tuple[str, int]]:
    """(class, line) of the hits, sorted. An offset past the end (a hidden view of the text) counts for the last line."""
    starts = [0] + [m.end() for m in re.finditer("\n", text)]
    last = len(starts)
    found = {(cls, min(bisect.bisect_right(starts, off), last)) for cls, off in offsets}
    return sorted(found, key=lambda f: (f[1], f[0]))


def _said(found: list[tuple[str, int]]) -> str:
    label = {"name": "registered name"}
    shown = ", ".join("%s at line %d" % (label.get(cls, cls), line) for cls, line in found[:8])
    return shown + (" and %d more" % (len(found) - 8) if len(found) > 8 else "")


def screen(text: str) -> None:
    """Raise MaterialRefused for a secret, a home path, a blocklist hit, a name or structured data in `text`.
    Raises hooks.Unavailable when the name check cannot run (a text is never called clean then)."""
    from awb import gate, hooks

    found = [(f.cls, f.line) for f in gate._scan_text("material", text, None) if f.cls in SCREENED]
    names = [("name" if h.get("cls") == "name" else str(h.get("cls", "unknown")), h.get("start", 0))
             for h in hooks.name_hits(text) if isinstance(h, dict)]
    found += _lines(text, [(c, o if isinstance(o, int) and o >= 0 else 0) for c, o in names])
    if found:
        found = sorted(set(found), key=lambda f: (f[1], f[0]))
        raise MaterialRefused("the text carries %s: nothing was written. Take it out of the text and send it "
                              "again; text with names goes in through awb import in the owner's terminal."
                              % _said(found))


def clean_title(title: str | None) -> str:
    """The title as one plain line of at most MAX_TITLE characters, "" for none."""
    if title is None:
        return ""
    one = " ".join(title.split())
    if len(one) > MAX_TITLE:
        raise MaterialRefused("--title is over %d characters" % MAX_TITLE)
    return one


def add(text: str, root: Path, kind: str = "note", title: str | None = None, *, today: date | None = None,
        p: config.Paths | None = None) -> tuple[Path, bool]:
    """Check `text`, write it to `input/` of the project at `root` and add the ledger line. Returns the path and
    whether the ledger line was added. Raises MaterialRefused, hooks.Unavailable."""
    if kind not in NAMES:
        raise MaterialRefused("a kind is note, mail, transcript or table")
    title = clean_title(title)
    try:
        text = pastein.read_text(text.encode("utf-8", "surrogatepass"))
    except pastein.PasteRefused as err:
        raise MaterialRefused(str(err)) from None
    screen(text + ("\n" + title if title else ""))
    word, ext = NAMES[kind]
    day = (today or date.today()).isoformat()
    outdir = root / FOLDER
    path = pastein._write_copy(outdir, word, ext, day, pastein._next_number(word, ext, day, [outdir]), text)
    return path, _ledger(path, root, kind, title, p, today)


def _ledger(path: Path, root: Path, kind: str, title: str, p: config.Paths | None, today: date | None) -> bool:
    from awb import ledger

    code = root.name if codes.is_project_code(root.name) else None
    done = "material added: %s%s" % (kind, ", " + title if title else "")
    try:
        ledger.add("other", done, project=code, tags=(), day=today, p=p)
    except (ledger.LedgerError, OSError):
        return False
    return True


def main(argv: list[str] | None = None) -> int:
    from awb import hooks
    from awb.cli import SafeParser

    ap = SafeParser(prog="awb material", description="Keep pasted text that holds no name as project material.")
    sub = ap.add_subparsers(dest="cmd", required=True)
    a = sub.add_parser("add", help="read the text on standard input and keep it in input/ of the project")
    a.add_argument("--kind", choices=KINDS, default="note", help="note (default), mail, transcript or table")
    a.add_argument("--title", default=None, metavar="WORDS", help="a few words for the ledger line")
    try:
        args = ap.parse_args(argv)
    except SystemExit as exc:
        return exc.code if isinstance(exc.code, int) else 2
    root = hooks.project_root(os.getcwd())
    if root is None:
        print("awb material: run it in a project folder (one with a SCOPE.md)", file=sys.stderr)
        return 2
    try:
        data = sys.stdin.buffer.read(pastein.MAX_BYTES + 1)
        text = pastein.read_text(data)
        path, ledgered = add(text, root, args.kind, args.title)
    except (MaterialRefused, pastein.PasteRefused) as err:
        print("awb material: %s" % err, file=sys.stderr)
        return 2
    except hooks.Unavailable as exc:
        print("awb material: the name check did not run (%s): nothing was written" % exc, file=sys.stderr)
        return 2
    except OSError as err:
        print("awb material: %s (operating system error)" % type(err).__name__, file=sys.stderr)
        return 2
    print(path.name)
    if not ledgered:
        print("awb material: the file is kept but the ledger line was not added", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
