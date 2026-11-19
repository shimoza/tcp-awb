"""The owner's open work queue (UX5). Standard-library storage, codes and handles only.

Writers share a lock and replace whole TSV files. Completion writes the archive first;
readers exclude archived ids so an interrupted move never resurrects completed work.
"""
from __future__ import annotations

import csv
import datetime as dt
import fcntl
import hashlib
import hmac
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


VERDICTS = "queue-verdicts.tsv"
"""Beside queue.tsv: one signed verdict per row that passed the check (Q-0011), so the next run checks only a new
or changed row. A line is `id TAB mac`; the mac is an HMAC over the row's fields and the register stamp under a key
in the owner's own state folder (`_verdict_key`), never beside the queue: a line planted with the queue's folder
alone certifies nothing."""

_CODE_MODULES = ("check", "ledger", "gate", "normalize", "intake", "fplearn", "matcher", "register")
_code_mark: str | None = None


def _code_stamp() -> str:
    """The source of the checks that make a verdict: a new release with other rules checks every row again."""
    global _code_mark
    if _code_mark is None:
        digest = hashlib.sha256()
        here = Path(__file__).resolve().parent
        for name in _CODE_MODULES:
            try:
                digest.update(name.encode("ascii") + b"\0" + (here / (name + ".py")).read_bytes())
            except OSError:
                return ""
        _code_mark = digest.hexdigest()
    return _code_mark


def _register_stamp(reg) -> str:
    """A mark of the register state a verdict was earned under, "" when unknown (then no verdict is reused).
    A readable register: its path, its bytes and the learned allow list. A register only the vault daemon reads:
    the daemon's opaque revision, one request instead of one check per field (Q-0011)."""
    try:
        from awb import fplearn

        allow = repr(sorted(fplearn.load_allow(Path(reg).parent))).encode("utf-8")
        base = b"local\0" + os.fsencode(reg) + b"\0" + Path(reg).read_bytes() + b"\0" + allow
    except (OSError, TypeError):
        from awb import check, vault_client

        revision = vault_client.register_revision(check.remote_socket())
        if not revision:
            return ""
        base = b"remote\0" + revision.encode("ascii")
    code = _code_stamp()
    if not code:
        return ""
    return hashlib.sha256(code.encode("ascii") + b"\0" + base).hexdigest()


def _row_key(row: dict[str, str], stamp: str) -> str:
    text = "\0".join(row[k] for k in ("id", "what", "next", "who", "project")) + "\0" + stamp
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _verdict_key() -> bytes | None:
    """The owner's key of the verdict file: 32 random bytes in `$XDG_STATE_HOME/awb/queue-verdict.key` (default
    ~/.local/state), the folder 0700 and the file 0600, made on first use. None when it cannot be read or made:
    then verdicts live in this process only."""
    base = Path(os.environ.get("XDG_STATE_HOME") or Path.home() / ".local" / "state") / "awb"
    path = base / "queue-verdict.key"
    try:
        base.mkdir(parents=True, exist_ok=True, mode=0o700)
        if base.is_symlink() or not base.is_dir():
            return None
        try:
            fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
        except FileNotFoundError:
            try:
                fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
            except FileExistsError:
                fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
            else:
                with os.fdopen(fd, "wb") as stream:
                    stream.write(os.urandom(32))
                    stream.flush()
                    os.fsync(stream.fileno())
                fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
        with os.fdopen(fd, "rb") as stream:
            info = os.fstat(stream.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_mode & 0o077 or info.st_uid != os.getuid():
                return None
            key = stream.read(64)
        return key if len(key) == 32 else None
    except OSError:
        return None


def _mac(key: bytes, row: dict[str, str], stamp: str) -> str:
    return hmac.new(key, _row_key(row, stamp).encode("ascii"), hashlib.sha256).hexdigest()


def _read_verdicts(path: Path) -> dict[str, str]:
    """id -> mac of the verdict file; empty when it is missing, broken or not a plain file."""
    try:
        text = _text(path)
    except (OSError, UnicodeError, QueueError):
        return {}
    out = {}
    for line in text.splitlines()[:100000]:
        ident, sep, mac = line.partition("\t")
        if sep and HANDLE.fullmatch(ident) and re.fullmatch(r"[0-9a-f]{64}", mac):
            out[ident] = mac
    return out


def _check(rows: list[dict[str, str]], cache: Path | None = None, stamp: str | None = None) -> None:
    """Check each row once per register stamp (Q-0011).

    A row whose fields and stamp carry a verdict, earned in this process or signed in the verdict file beside
    `cache`, is not sent again; every other row is screened and, when it passes, its verdict is kept. An unknown
    stamp (an unreadable register and a daemon that cannot say its revision) reuses nothing. A row with a finding
    refuses as before and gets no verdict.
    """
    reg = ledger.register_path()
    ledger.ensure_checkable(reg)
    if stamp is None:
        stamp = _register_stamp(reg)
    passed: set[str] = set()
    key = None
    stored: dict[str, str] = {}
    if cache is not None and stamp:
        previous_stamp, previous = _CHECKED.get(cache.absolute(), ("", set()))
        if previous_stamp == stamp:
            passed = set(previous)
        key = _verdict_key()
        if key is not None:
            stored = _read_verdicts(cache.with_name(VERDICTS))
    for row in rows:
        _shape(row)
    fresh = set()
    macs = {}
    try:
        for row in rows:
            row_key = _row_key(row, stamp)
            mac = _mac(key, row, stamp) if key is not None else ""
            known = row_key in passed or bool(mac) and hmac.compare_digest(stored.get(row["id"], ""), mac)
            if not known:
                # Recheck every printable field, including handles: the register can grow.
                ledger.screen_fields([(k, row[k]) for k in ("id", "what", "next", "who", "project")], reg)
            fresh.add(row_key)
            if mac:
                macs[row["id"]] = mac
    finally:
        # the verdicts earned before a refusal are kept too: the next run checks the refused row alone
        if cache is not None:
            if len(_CHECKED) >= 32:
                _CHECKED.clear()
            _CHECKED[cache.absolute()] = (stamp, fresh)
            if key is not None and macs != stored:
                try:
                    _atomic(cache.with_name(VERDICTS), "".join("%s\t%s\n" % kv for kv in sorted(macs.items())))
                except (OSError, QueueError):
                    pass


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
            "queue-done.tsv", "QUEUE.md", ".queue.lock", "queue-checked.json", "queue-checked.json.tmp", VERDICTS):
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


def _open_read(path: Path) -> list[dict[str, str]]:
    """The open rows for a reader: a completed row that never reached the archive counts as closed."""
    return _open(path, False)


def _open(path: Path, strict: bool = True) -> list[dict[str, str]]:
    if not _regular(path):
        source = path.with_name("QUEUE.md")
        if _regular(source) and not _text(source).startswith(ARCHIVE_NOTICE):
            raise QueueError("legacy queue exists; run awb queue migrate")
    rows, archive = _history(path, strict)
    done = {r["id"] for r in archive}
    return [r for r in rows if r["id"] not in done and not r["done"]]


def _history(path: Path, strict: bool = True) -> tuple[list[dict[str, str]], list[dict[str, str]]]:
    archive = _read(archive_file(path))
    if any(not r["done"] for r in archive):
        raise QueueError("archive row has no completion date")
    done = {r["id"] for r in archive}
    rows = _read(path)
    # a write refuses a completed row that never reached the archive (its id must not be reused or the row dropped);
    # a read (owner status, the hand-over, list) counts it as closed instead of refusing the whole queue
    if strict and any(r["done"] and r["id"] not in done for r in rows):
        raise QueueError("completed queue row is missing from the archive")
    return rows, archive


def load(path: Path | None = None) -> list[dict[str, str]]:
    """Checked open rows for the CLI and owner status. A missing queue is empty."""
    path = path or queue_file()
    _safe_parent(path)
    if not path.parent.exists():
        return []
    with _locked(path):
        rows = _open_read(path)
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
            rows = _open_read(path) if args.command == "list" else _open(path)
            cache = path.with_name("queue-checked.json")
            # one register stamp per run: with the vault daemon that is one request, not one per check
            ledger.ensure_checkable(ledger.register_path())
            stamp = _register_stamp(ledger.register_path())
            _check(rows, cache, stamp)
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
                # checked once here; its verdict is kept, so a list after it sends nothing again
                _check(rows + [row], cache, stamp)
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
                    _check(rows, cache, stamp)
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
