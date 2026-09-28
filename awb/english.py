"""The English notes (T-48): every "English note:" a session writes into a reply is also kept in a list.

    <shared>/english/notes.jsonl   one line per note: {"date", "note", "sha"}
    awb english list [--month YYYY-MM]

The stop hook takes the note from the last reply of the session transcript, runs the name check over it and keeps
it only when nothing is found: a note quotes his own sentence, which can carry a name he typed. The same note is
kept once. Once a month the list of that month is read to find the three mistakes that keep coming back, with the
correct form (a session does that with `awb english list --month`).
"""
from __future__ import annotations

import datetime
import hashlib
import json
import re
import sys
from pathlib import Path

from awb import config, ledger

MAX_NOTE = 800
TAIL_BYTES = 2_000_000
_NOTE_RE = re.compile(r"(?is)(?:^|\n)\s*[*_]*English note:?[*_]*:?\s*(.+)$")


def notes_file(p: config.Paths) -> Path:
    return p.shared / "english" / "notes.jsonl"


def last_reply(transcript: Path) -> str:
    """The text of the last assistant message of a session transcript (JSON lines), or ""."""
    try:
        with open(transcript, "rb") as f:
            f.seek(0, 2)
            size = f.tell()
            f.seek(max(0, size - TAIL_BYTES))
            tail = f.read().decode("utf-8", "replace")
    except OSError:
        return ""
    for line in reversed(tail.splitlines()):
        try:
            obj = json.loads(line)
        except ValueError:
            continue
        if not isinstance(obj, dict) or obj.get("type") != "assistant":
            continue
        content = (obj.get("message") or {}).get("content")
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            texts = [c.get("text", "") for c in content if isinstance(c, dict) and c.get("type") == "text"]
            if texts:
                return "\n".join(texts)
    return ""


def note_of(reply: str) -> str | None:
    """The text after "English note:" in a reply, or None."""
    m = _NOTE_RE.search(reply or "")
    if not m:
        return None
    note = " ".join(m.group(1).split())
    return note[:MAX_NOTE] if note else None


def keep(p: config.Paths, note: str, day: datetime.date | None = None) -> bool:
    """Append `note` unless it is kept already or the name check finds anything in it. True when appended."""
    day = day or datetime.date.today()
    found = ledger.screen(note, ledger.register_path(p))
    if found:
        return False
    sha = hashlib.sha256(note.encode("utf-8")).hexdigest()[:16]
    path = notes_file(p)
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            try:
                if json.loads(line).get("sha") == sha:
                    return False
            except ValueError:
                continue
    ledger.append_line(path, {"date": day.isoformat(), "note": note, "sha": sha})
    return True


def capture(p: config.Paths, transcript: Path | str | None) -> bool:
    """The stop hook: keep the English note of the last reply, if there is one."""
    if not transcript:
        return False
    note = note_of(last_reply(Path(transcript)))
    return keep(p, note) if note else False


def load(p: config.Paths, month: str | None = None) -> list[dict]:
    out = []
    try:
        lines = notes_file(p).read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    for line in lines:
        try:
            obj = json.loads(line)
        except ValueError:
            continue
        if isinstance(obj, dict) and (month is None or str(obj.get("date", "")).startswith(month)):
            out.append(obj)
    return out


def main(argv: list[str] | None = None) -> int:
    """`awb english list [--month YYYY-MM]`."""
    from awb.cli import SafeParser

    ap = SafeParser(prog="awb english", description="The English notes of the sessions.")
    sub = ap.add_subparsers(dest="command", parser_class=SafeParser)
    ls = sub.add_parser("list", help="the kept notes, all or of one month")
    ls.add_argument("--month", default=None, help="YYYY-MM")
    try:
        args = ap.parse_args(argv)
    except SystemExit as exc:
        return exc.code if isinstance(exc.code, int) else 2
    if args.command != "list":
        ap.print_usage(sys.stderr)
        return 2
    if args.month and not re.fullmatch(r"\d{4}-\d{2}", args.month):
        print("awb english: the month reads like 2026-09", file=sys.stderr)
        return 2
    rows = load(config.paths(), args.month)
    for r in rows:
        print("%s  %s" % (r.get("date"), r.get("note")))
    print("awb english: %d note(s)%s" % (len(rows), " in %s" % args.month if args.month else ""))
    return 0


if __name__ == "__main__":
    sys.exit(main())
