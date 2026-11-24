"""How long every owner item waits (internal mode, step 1, the owner's yes of 2026-11-24).

`awb owner status` and the hourly owner timer (`awb owner status --record`, the ExecStopPost of
awb-report-codes.service) record the items of `ownerwait.collect` in a wait history:

- one entry per item: its label (a project code or a word such as reports), a key (the first 16 hex characters of
  sha256 over label and item text, never the text itself), where it is done, first seen, last seen and, once the
  item is gone from the list, its end (the time of the first run that no longer saw it). An item that comes back
  later opens a new entry;
- the file is `waits.json` in the state folder of the report timer (`ownerwait.report_dir()`, the owner's, 700),
  written atomically (mode 600) under a lock; an entry whose end or last sight is older than KEEP_DAYS is dropped,
  and past MAX_ENTRIES the oldest closed entries go first.

The board runs as the work user and cannot read the owner's state folder, so each record also writes
`owner-waits/waits.tsv` of the shared side (seal/setup.sh makes the folder, the work user's with the group, 2770):
one row per project code with open waits, its count and the first-seen time of its oldest open wait. Codes,
counts and times only; `awb board show` checks every cell against its form before it shows the column.

The labels passed the name check of `collect` before they get here (a label with a hit is "?").
"""
from __future__ import annotations

import datetime as dt
import fcntl
import hashlib
import json
import os
import re
import stat
import tempfile
from pathlib import Path

HISTORY = "waits.json"
LOCK = ".waits.lock"
SHARED_DIR = "owner-waits"
SUMMARY = "waits.tsv"
KEEP_DAYS = 60
MAX_ENTRIES = 5000
MAX_BYTES = 4 * 1024 * 1024
SUMMARY_MAX = 64 * 1024
STALE_HOURS = 3
"""A summary older than this is not a measurement any more: the board says so instead of showing hours."""
HEADER = "code\topen\tsince"
_TIME = "%Y-%m-%dT%H:%M:%SZ"
_TIME_RE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z")
_KEY_RE = re.compile(r"[0-9a-f]{16}")
WHERE = ("project session", "his terminal", "steering session", "")


def _now(now: dt.datetime | None) -> dt.datetime:
    return (now or dt.datetime.now(dt.timezone.utc)).astimezone(dt.timezone.utc).replace(microsecond=0)


def stamp(t: dt.datetime) -> str:
    return t.astimezone(dt.timezone.utc).strftime(_TIME)


def parse(text: str) -> dt.datetime:
    if not isinstance(text, str) or not _TIME_RE.fullmatch(text):
        raise ValueError("not a time")
    return dt.datetime.strptime(text, _TIME).replace(tzinfo=dt.timezone.utc)


def history_path() -> Path:
    from awb import ownerwait

    return ownerwait.report_dir() / HISTORY


def key(label: str, text: str) -> str:
    return hashlib.sha256(("%s\0%s" % (label, text)).encode("utf-8")).hexdigest()[:16]


def item_key(it) -> str:
    """The key of an ownerwait.Item: its label and its full text (the source before shortening, else the text)."""
    return key(it.label, it.source or it.text)


def _valid(e) -> bool:
    if not isinstance(e, dict) or not isinstance(e.get("key"), str) or not _KEY_RE.fullmatch(e["key"]):
        return False
    if not isinstance(e.get("code"), str) or not e["code"] or len(e["code"]) > 64 or e.get("where") not in WHERE:
        return False
    try:
        parse(e["first"]), parse(e["last"])
        if e.get("end") is not None:
            parse(e["end"])
    except (KeyError, ValueError):
        return False
    return True


def load(path: Path | None = None) -> list[dict]:
    """The entries of the history; nothing when it is missing or cannot be read. A malformed entry is dropped."""
    from awb import ownerwait

    try:
        data = json.loads(ownerwait._read_text(path or history_path(), MAX_BYTES))
    except (OSError, ValueError):
        return []
    entries = data.get("entries") if isinstance(data, dict) else None
    return [e for e in entries if _valid(e)] if isinstance(entries, list) else []


def prune(entries: list[dict], now: dt.datetime) -> list[dict]:
    """Drop what is older than KEEP_DAYS, then the oldest closed entries past MAX_ENTRIES."""
    cut = now - dt.timedelta(days=KEEP_DAYS)
    kept = [e for e in entries if parse(e["end"] or e["last"]) >= cut]
    if len(kept) > MAX_ENTRIES:
        closed = sorted((e for e in kept if e["end"]), key=lambda e: e["end"])
        drop = {id(e) for e in closed[:len(kept) - MAX_ENTRIES]}
        kept = [e for e in kept if id(e) not in drop][-MAX_ENTRIES:]
    return kept


def update(entries: list[dict], seen: list[tuple[str, str, str]], now: dt.datetime) -> list[dict]:
    """SEEN is (key, code, where) of every item now. An open entry seen again gets its last sight, an open entry not
    seen is closed at NOW, an item without an open entry opens one."""
    t = stamp(now)
    current = {}
    for k, code, where in seen:
        current.setdefault(k, (code, where))
    open_by_key = {}
    for e in entries:
        if e["end"] is None:
            if e["key"] in current and e["key"] not in open_by_key:
                open_by_key[e["key"]] = e
                e["last"] = t
                e["where"] = current[e["key"]][1]
            else:
                e["end"] = t
    for k, (code, where) in current.items():
        if k not in open_by_key:
            entries.append({"key": k, "code": code, "where": where, "first": t, "last": t, "end": None})
    return prune(entries, now)


def _write_json(path: Path, entries: list[dict]) -> None:
    text = json.dumps({"version": 1, "entries": entries}, indent=0, sort_keys=True) + "\n"
    fd, tmp = tempfile.mkstemp(prefix=".waits.", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise


def record(items, now: dt.datetime | None = None, path: Path | None = None) -> list[dict]:
    """Record the items of one collection and return the history. Raises OSError when the state folder cannot be
    written (its caller decides whether that matters)."""
    now = _now(now)
    path = Path(path or history_path())
    if path.parent.is_symlink() or not path.parent.is_dir():
        raise FileNotFoundError("no state folder")
    seen = [(item_key(it), it.label, it.where if it.where in WHERE else "") for it in items]
    lock_fd = os.open(path.parent / LOCK, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_EX)
        entries = update(load(path), seen, now)
        _write_json(path, entries)
    finally:
        os.close(lock_fd)
    return entries


def hours(first: str, end: dt.datetime) -> float:
    return max(0.0, (end - parse(first)).total_seconds() / 3600)


def hours_text(h: float) -> str:
    return "<1 h" if h < 1 else "%d h" % int(h)


def open_waits(entries: list[dict], now: dt.datetime | None = None) -> dict[str, float]:
    """key -> hours of every open entry."""
    now = _now(now)
    return {e["key"]: hours(e["first"], now) for e in entries if e["end"] is None}


def annotate(items, entries: list[dict], now: dt.datetime | None = None) -> None:
    """Give each item that has an open entry its hours (Item.waited)."""
    waits = open_waits(entries, now)
    for it in items:
        h = waits.get(item_key(it))
        if h is not None:
            it.waited = h


# -- the summary of the shared side, for the board -------------------------------------------------------------------


def summary_rows(entries: list[dict]) -> list[tuple[str, int, str]]:
    """(code, open waits, first seen of the oldest) per project code with an open wait, by code."""
    from awb import codes

    out: dict[str, list] = {}
    for e in entries:
        if e["end"] is None and codes.is_project_code(e["code"]):
            row = out.setdefault(e["code"], [0, e["first"]])
            row[0] += 1
            row[1] = min(row[1], e["first"])
    return [(c, n, since) for c, (n, since) in sorted(out.items())]


def summary_text(entries: list[dict], now: dt.datetime) -> str:
    lines = ["# owner waits, written %s" % stamp(now), HEADER]
    lines += ["%s\t%d\t%s" % row for row in summary_rows(entries)]
    return "\n".join(lines) + "\n"


def summary_path(p) -> Path:
    return Path(p.shared) / SHARED_DIR / SUMMARY


def write_summary(p, entries: list[dict], now: dt.datetime | None = None) -> bool:
    """Write the summary into the folder of the shared side that seal/setup.sh made; False when it is not there or
    cannot be written (a link, another user's file in the folder). The folder is never made here: only the
    seal gives it its group and modes."""
    now = _now(now)
    path = summary_path(p)
    folder = path.parent
    try:
        st = os.lstat(folder)
    except OSError:
        return False
    if not stat.S_ISDIR(st.st_mode):
        return False
    try:
        fd, tmp = tempfile.mkstemp(prefix=".waits.", dir=folder)
    except OSError:
        return False
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(summary_text(entries, now))
        os.chmod(tmp, 0o640)
        os.replace(tmp, path)
    except OSError:
        if os.path.exists(tmp):
            os.unlink(tmp)
        return False
    return True


def read_summary(p, now: dt.datetime | None = None) -> dict:
    """What the board shows: {"state": "missing" | "stale" | "ok", "written": time or None, "hours": {code: h}}.
    Every row is checked against its form; a row that fails is left out."""
    from awb import codes
    from awb.tcp import board

    now = _now(now)
    text = board._read_small(summary_path(p), SUMMARY_MAX)
    if text is None:
        return {"state": "missing", "written": None, "hours": {}}
    lines = text.splitlines()
    m = re.fullmatch(r"# owner waits, written (\S+)", lines[0]) if lines else None
    try:
        written = parse(m.group(1)) if m else None
    except ValueError:
        written = None
    if written is None or len(lines) < 2 or lines[1] != HEADER:
        return {"state": "missing", "written": None, "hours": {}}
    if now - written > dt.timedelta(hours=STALE_HOURS):
        return {"state": "stale", "written": stamp(written), "hours": {}}
    out: dict[str, float] = {}
    for line in lines[2:]:
        cells = line.split("\t")
        if len(cells) != 3 or not codes.is_project_code(cells[0]) or not re.fullmatch(r"[1-9][0-9]{0,4}", cells[1]):
            continue
        try:
            out[cells[0]] = hours(cells[2], now)
        except ValueError:
            continue
    return {"state": "ok", "written": stamp(written), "hours": out}


# -- awb owner status --history CODE ---------------------------------------------------------------------------------


def _show(t: str) -> str:
    return t.replace("T", " ")[:16]


def history_lines(code: str, entries: list[dict], now: dt.datetime | None = None) -> list[str]:
    """The closed and open waits of CODE, oldest first: start, end, hours, where."""
    now = _now(now)
    mine = sorted((e for e in entries if e["code"] == code), key=lambda e: (e["first"], e["key"]))
    if not mine:
        return ["%s: no wait recorded (the history keeps %d days)" % (code, KEEP_DAYS)]
    open_n = sum(1 for e in mine if e["end"] is None)
    lines = ["%s: %d wait%s, %d open (UTC)" % (code, len(mine), "" if len(mine) == 1 else "s", open_n),
             "start             end               hours  where"]
    for e in mine:
        end = parse(e["end"]) if e["end"] else now
        lines.append("%-16s  %-16s  %5s  %s" % (_show(e["first"]), _show(e["end"]) if e["end"] else "open",
                                               hours_text(hours(e["first"], end)), e["where"] or "-"))
    return lines
