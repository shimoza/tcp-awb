"""The owner's open work queue (UX5). Standard-library storage, codes and handles only.

Writers share a lock and replace whole TSV files. Completion writes the archive first;
readers exclude archived ids so an interrupted move never resurrects completed work.
"""
from __future__ import annotations

import csv
import datetime as dt
import fcntl
import io
import os
import re
import sys
import tempfile
from contextlib import contextmanager
from pathlib import Path

from awb import codes, config, ledger

FIELDS = ("id", "created", "updated", "next_changed", "what", "next", "who", "project", "done")
ARCHIVE_NOTICE = "Archive: open work moved to queue.tsv; use awb queue add|next|done|list."
HANDLE = re.compile(r"[A-Za-z][A-Za-z0-9-]{0,39}")
CLOSED = re.compile(r"^(done|accepted|complete|completed|closed|cancelled|canceled|dropped|superseded|retired|obsolete)\b", re.I)


class QueueError(Exception):
    """Fixed messages only, never an input value."""


def now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def queue_file() -> Path:
    return Path(os.environ.get("AWB_WORK_QUEUE") or Path.home() / "tcp-awb/presentations/work-profile/queue.tsv")


def archive_file(path: Path) -> Path:
    return path.with_name("queue-done.tsv")


def _date(value: str) -> dt.datetime:
    try:
        parsed = dt.datetime.fromisoformat(value)
        if parsed.utcoffset() is None:
            raise ValueError
        return parsed
    except ValueError:
        raise QueueError("invalid queue date") from None


def _shape(row: dict[str, str]) -> None:
    if set(row) != set(FIELDS) or any(not isinstance(v, str) for v in row.values()):
        raise QueueError("invalid queue fields")
    if not HANDLE.fullmatch(row["id"]):
        raise QueueError("invalid queue id")
    who = row["who"]
    if who not in ("owner", "desk", "development") and not (
            who.startswith("session:") and HANDLE.fullmatch(who[8:])):
        raise QueueError("who must be owner, desk, development or session:CODE")
    if row["project"] and not codes.is_project_code(row["project"]):
        raise QueueError("project must be a project code")
    for key in ("what", "next"):
        value = row[key]
        if not value.strip() or len(value) > 2000 or any(ord(c) < 32 or ord(c) == 127 for c in value):
            raise QueueError("what and next must be nonempty single lines of at most 2000 characters")
    for key in ("created", "updated", "next_changed"):
        _date(row[key])
    if row["done"]:
        _date(row["done"])


def _check(rows: list[dict[str, str]]) -> None:
    reg = ledger.register_path()
    ledger.ensure_checkable(reg)
    for row in rows:
        _shape(row)
        # Recheck every printable field, including handles: the register can grow.
        ledger.screen_fields([(key, row[key]) for key in ("id", "what", "next", "who", "project")], reg)


def _read(path: Path) -> list[dict[str, str]]:
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return []
    reader = csv.DictReader(io.StringIO(text), delimiter="\t")
    if reader.fieldnames != list(FIELDS):
        raise QueueError("invalid queue header")
    rows = list(reader)
    for row in rows:
        _shape(row)
    if len({r["id"] for r in rows}) != len(rows):
        raise QueueError("duplicate queue id")
    return rows


def _atomic(path: Path, text: str) -> None:
    fd, name = tempfile.mkstemp(prefix=".queue-", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as stream:
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, path)
    finally:
        Path(name).unlink(missing_ok=True)


def _write(path: Path, rows: list[dict[str, str]]) -> None:
    stream = io.StringIO(newline="")
    writer = csv.DictWriter(stream, fieldnames=FIELDS, delimiter="\t", lineterminator="\n")
    writer.writeheader()
    writer.writerows(rows)
    _atomic(path, stream.getvalue())


@contextmanager
def _locked(path: Path):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path.with_name(".queue.lock"), os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        os.close(fd)


def _open(path: Path) -> list[dict[str, str]]:
    if not path.exists():
        source = path.with_name("QUEUE.md")
        if source.exists() and not source.read_text(encoding="utf-8").startswith(ARCHIVE_NOTICE):
            raise QueueError("legacy queue exists; run awb queue migrate")
    done = {r["id"] for r in _read(archive_file(path))}
    return [r for r in _read(path) if r["id"] not in done and not r["done"]]


def load(path: Path | None = None) -> list[dict[str, str]]:
    """Checked open rows for the CLI and owner status. A missing queue is empty."""
    path = path or queue_file()
    if not path.parent.exists():
        return []
    with _locked(path):
        rows = _open(path)
        _check(rows)
        return rows


def _new(ident: str, what: str, step: str, who: str, project: str = "", at: str | None = None) -> dict[str, str]:
    at = at or now().isoformat()
    return dict(id=ident, created=at, updated=at, next_changed=at, what=what.strip(),
                next=step.strip(), who=who, project=project, done="")


def _migrate(path: Path) -> int:
    source = path.with_name("QUEUE.md")
    text = source.read_text(encoding="utf-8")
    if text.startswith(ARCHIVE_NOTICE):
        return 0
    at = dt.datetime.fromtimestamp(source.stat().st_mtime, dt.timezone.utc).isoformat()
    imported = []
    columns = []
    tables = 0
    for line in text.splitlines():
        if not line.startswith("|"):
            if not line.strip() or line.startswith("#"):
                columns = []
            continue
        cells = [c.strip().replace("\\|", "|") for c in re.split(r"(?<!\\)\|", line.strip().strip("|"))]
        names = [c.lower() for c in cells]
        if "state" in names and "what" in names and ("id" in names or "step" in names):
            columns = names
            tables += 1
            continue
        if not columns or all(re.fullmatch(r"[-: ]+", c) for c in cells):
            continue
        if len(cells) != len(columns):
            raise QueueError("invalid legacy queue row")
        old = dict(zip(columns, cells))
        state = old["state"].strip("*` ")
        if CLOSED.match(state):
            continue
        ident = old.get("id", old.get("step", "")).strip("` ")
        step = old.get("next", "").strip()
        if step.lower() in ("", "none", "-", "n/a"):
            step = "review next step"
        who = old.get("who", "")
        if not who:
            who = "owner" if re.search(r"^(waiting|asked)\b|\bwaits? on (him|you|the owner)\b", state, re.I) else "desk"
            if who == "desk" and re.search(r"\bdevelopment\b", state + " " + step, re.I):
                who = "development"
        imported.append(_new(ident, old["what"], step, who, old.get("project", ""), at))
    if not tables:
        raise QueueError("no legacy queue tables found")
    _check(imported)
    if len({r["id"] for r in imported}) != len(imported):
        raise QueueError("duplicate legacy queue id")
    existing = _read(path)
    archived = {r["id"] for r in _read(archive_file(path))}
    by_id = {r["id"]: r for r in existing}
    for row in imported:
        if row["id"] in archived:
            continue
        prior = by_id.get(row["id"])
        if prior is not None and prior != row:
            raise QueueError("legacy id conflicts with an existing queue row")
        if prior is None:
            existing.append(row)
    _check(existing)
    _write(path, existing)
    # Last: a retry after interruption matches the rows already written above.
    _atomic(source, ARCHIVE_NOTICE + "\n\n" + text)
    return len(imported)


def main(argv: list[str] | None = None) -> int:
    from awb.cli import SafeParser

    parser = SafeParser(prog="awb queue", description="Open work on the owner side; codes and handles only.")
    sub = parser.add_subparsers(dest="command", required=True)
    add = sub.add_parser("add", help="add open work with its next step and responsible handle")
    add.add_argument("what", metavar="WHAT")
    add.add_argument("--next", required=True, dest="step", metavar="STEP")
    add.add_argument("--who", required=True, metavar="owner|desk|development|session:CODE")
    add.add_argument("--project", default="", metavar="CODE")
    nxt = sub.add_parser("next", help="change the next step and responsible handle")
    nxt.add_argument("id", metavar="ID")
    nxt.add_argument("step", metavar="STEP")
    nxt.add_argument("--who", required=True, metavar="owner|desk|development|session:CODE")
    done = sub.add_parser("done", help="move an item to queue-done.tsv")
    done.add_argument("id", metavar="ID")
    listing = sub.add_parser("list", help="list open rows; stale means the next step is unchanged for 24 hours")
    listing.add_argument("--mine", action="store_true", help="only rows whose who is owner")
    sub.add_parser("migrate", help="once: import open rows of QUEUE.md and mark it as the archive")
    args = parser.parse_args(argv)
    try:
        if config.is_work_user():
            raise QueueError("owner side only")
        path = queue_file()
        with _locked(path):
            if args.command == "migrate":
                print("queue: %d open rows migrated" % _migrate(path))
                return 0
            rows = _open(path)
            _check(rows)
            if args.command == "list":
                for row in rows:
                    if args.mine and row["who"] != "owner":
                        continue
                    stale = now() - _date(row["next_changed"]) >= dt.timedelta(hours=24)
                    print("%s  %s  %s  %s  next: %s%s" % (
                        row["id"], row["project"] or "-", row["who"], row["what"], row["next"],
                        "  [stale]" if stale else ""))
                return 0
            archive = _read(archive_file(path))
            if args.command == "add":
                ids = [int(r["id"][2:]) for r in rows + archive if re.fullmatch(r"Q-[0-9]+", r["id"])]
                row = _new("Q-%04d" % (max(ids, default=0) + 1), args.what, args.step, args.who, args.project)
                _check([row])
                rows.append(row)
            else:
                if not HANDLE.fullmatch(args.id):
                    raise QueueError("invalid queue id")
                row = next((r for r in rows if r["id"] == args.id), None)
                if row is None:
                    if args.command == "done" and any(r["id"] == args.id for r in archive):
                        _write(path, rows)
                        print("queue: already done")
                        return 0
                    raise QueueError("no open row for that id")
                if args.command == "next":
                    step = args.step.strip()
                    if row["next"] != step:
                        row["next_changed"] = now().isoformat()
                    row.update(next=step, who=args.who, updated=now().isoformat())
                    _check([row])
                else:
                    _write(archive_file(path), archive + [dict(row, done=now().isoformat())])
                    rows.remove(row)
            _write(path, rows)
            print("%s %s" % (row["id"], {"add": "added", "next": "updated", "done": "done"}[args.command]))
        return 0
    except ledger.Refused:
        print("awb queue: row refused by the name or safety check", file=sys.stderr)
        return 1
    except (QueueError, ledger.LedgerError) as err:
        print("awb queue: %s" % err, file=sys.stderr)
        return 2
    except (OSError, UnicodeError, csv.Error):
        print("awb queue: storage could not be read or written", file=sys.stderr)
        return 2
