"""The owner's open work queue (UX5). Standard-library storage, codes and handles only.

Writers share a lock and replace whole TSV files. Completion writes the archive first;
readers exclude archived ids so an interrupted move never resurrects completed work.
"""
from __future__ import annotations

import csv
import datetime as dt
import fcntl
import hashlib
import io
import os
import re
import stat
import sys
import tempfile
from contextlib import contextmanager
from pathlib import Path

from awb import codes, config, ledger

FIELDS = ("id", "created", "updated", "next_changed", "what", "next", "who", "project", "done")
ARCHIVE_NOTICE = "Archive: open work moved to queue.tsv; use awb queue add|next|done|list."
HANDLE = re.compile(r"[A-Za-z][A-Za-z0-9-]{0,39}")
CLOSED = re.compile(r"^(done|accepted|complete|completed|closed|cancelled|canceled|dropped|superseded|retired|obsolete)\b", re.I)
_CHECKED: dict[Path, tuple[str, set[str]]] = {}


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


def _register_stamp(reg) -> str:
    try:
        return hashlib.sha256(os.fsencode(reg) + b"\0" + Path(reg).read_bytes()).hexdigest()
    except (OSError, TypeError):
        return ""


def _row_key(row: dict[str, str], stamp: str) -> str:
    text = "\0".join(row[k] for k in ("id", "what", "next", "who", "project")) + "\0" + stamp
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _check(rows: list[dict[str, str]], cache: Path | None = None) -> None:
    """Cache only verdicts earned in this process, with a readable register fingerprint.

    A disk hash list is editable input, not evidence of a safety check. An unreadable
    or remote register has no known revision, so its verdicts must not be reused.
    `cache` identifies the queue without reading or writing the old cache file.
    """
    reg = ledger.register_path()
    ledger.ensure_checkable(reg)
    stamp = _register_stamp(reg)
    passed: set[str] = set()
    if cache is not None and stamp:
        previous_stamp, previous = _CHECKED.get(cache.absolute(), ("", set()))
        if previous_stamp == stamp:
            passed = previous
    fresh = set()
    for row in rows:
        _shape(row)
        key = _row_key(row, stamp)
        if key not in passed:
            # Recheck every printable field, including handles: the register can grow.
            ledger.screen_fields([(k, row[k]) for k in ("id", "what", "next", "who", "project")], reg)
        fresh.add(key)
    if cache is not None:
        if len(_CHECKED) >= 32:
            _CHECKED.clear()
        _CHECKED[cache.absolute()] = (stamp, fresh)


def _safe_parent(path: Path) -> None:
    if "\0" in os.fspath(path):
        raise QueueError("invalid queue storage path")
    for parent in reversed(path.parents):
        try:
            mode = parent.lstat().st_mode
        except FileNotFoundError:
            continue
        if not stat.S_ISDIR(mode):
            raise QueueError("queue parent must be a directory without links")


def _regular(path: Path) -> bool:
    _safe_parent(path)
    try:
        info = path.lstat()
    except FileNotFoundError:
        return False
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
        raise QueueError("queue storage must be a regular file without links")
    return True


def _text(path: Path) -> str:
    _regular(path)
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, "r", encoding="utf-8", newline="") as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise QueueError("queue storage must be a regular file without links")
        return stream.read()


def _read(path: Path) -> list[dict[str, str]]:
    try:
        text = _text(path)
    except FileNotFoundError:
        return []
    try:
        reader = csv.DictReader(io.StringIO(text), delimiter="\t", strict=True)
        if reader.fieldnames != list(FIELDS):
            raise QueueError("invalid queue header")
        rows = list(reader)
    except csv.Error:
        raise QueueError("invalid queue data") from None
    for row in rows:
        _shape(row)
    if len({r["id"] for r in rows}) != len(rows):
        raise QueueError("duplicate queue id")
    return rows


def _atomic(path: Path, text: str) -> None:
    _regular(path)
    fd, name = tempfile.mkstemp(prefix=".queue-", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as stream:
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        _regular(path)
        os.replace(name, path)
        directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
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
    if "\0" in os.fspath(path) or path.name in (
            "queue-done.tsv", "QUEUE.md", ".queue.lock", "queue-checked.json", "queue-checked.json.tmp"):
        raise QueueError("invalid queue storage path")
    _safe_parent(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    lock = path.with_name(".queue.lock")
    _regular(lock)
    fd = os.open(lock, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise QueueError("queue lock must be a regular file without links")
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        os.close(fd)


def _open(path: Path) -> list[dict[str, str]]:
    if not _regular(path):
        source = path.with_name("QUEUE.md")
        if _regular(source) and not _text(source).startswith(ARCHIVE_NOTICE):
            raise QueueError("legacy queue exists; run awb queue migrate")
    rows, archive = _history(path)
    done = {r["id"] for r in archive}
    return [r for r in rows if r["id"] not in done]


def _history(path: Path) -> tuple[list[dict[str, str]], list[dict[str, str]]]:
    archive = _read(archive_file(path))
    if any(not r["done"] for r in archive):
        raise QueueError("archive row has no completion date")
    done = {r["id"] for r in archive}
    rows = _read(path)
    if any(r["done"] and r["id"] not in done for r in rows):
        raise QueueError("completed queue row is missing from the archive")
    return rows, archive


def load(path: Path | None = None) -> list[dict[str, str]]:
    """Checked open rows for the CLI and owner status. A missing queue is empty."""
    path = path or queue_file()
    _safe_parent(path)
    if not path.parent.exists():
        return []
    with _locked(path):
        rows = _open(path)
        _check(rows, path.with_name("queue-checked.json"))
        return rows


def _new(ident: str, what: str, step: str, who: str, project: str = "", at: str | None = None) -> dict[str, str]:
    at = at or now().isoformat()
    return dict(id=ident, created=at, updated=at, next_changed=at, what=what.strip(),
                next=step.strip(), who=who, project=project, done="")


def _migrate(path: Path) -> int:
    source = path.with_name("QUEUE.md")
    text = _text(source)
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
            if len(set(names)) != len(names):
                raise QueueError("duplicate legacy queue column")
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
    existing, archive = _history(path)
    archived = {r["id"] for r in archive}
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

    class QueueParser(SafeParser):
        def error(self, message):
            super().error("invalid queue arguments")

    parser = QueueParser(prog="awb queue", description="Open work on the owner side; codes and handles only.")
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
            _check(rows, path.with_name("queue-checked.json"))
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
    except QueueError as err:
        print("awb queue: %s" % err, file=sys.stderr)
        return 2
    except ledger.LedgerError:
        print("awb queue: name or safety check unavailable", file=sys.stderr)
        return 2
    except (OSError, UnicodeError, csv.Error):
        print("awb queue: storage could not be read or written", file=sys.stderr)
        return 2
