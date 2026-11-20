"""Push, not poll, phase 1 (Q-0014, finding 1 of the UX review of 2026-11-18): the start, the ETA and the end of
the long things in one events file, so that `awb owner status` and the owner state of the console show what runs
now and what ended, red ones marked, without a status question.

    <shared>/build/events.jsonl     one JSON object per line, append only

The file lies in the folder of the board (`<shared>/build`, the work user's with the group, 3770 on a sealed host),
the one folder the build runner, the deploy (root), the owner's `awb winlab` and the work user's `awb dev close` all
may write. A line is

    {"t": TIME, "ev": "start", "kind": KIND, "id": ID, "project": CODE or null, "eta": TIME or null,
     "auto_end": true or false}
    {"t": TIME, "ev": "end", "kind": KIND, "id": ID, "project": CODE or null, "outcome": OUTCOME}

with TIME in UTC (`2026-11-20T09:30:00Z`), KIND one of KINDS, ID a short id (a task or check id, a short commit, a
lab machine name, the handle of a dev ask) and OUTCOME one of OUTCOMES. Codes, ids and fixed words only, never free
text. A line is one write() of at most LINE_MAX bytes on a file opened with O_APPEND, so writers of several users
never mix their lines. A writer never fails its caller: a file it cannot open or write is skipped.

The writers (phase 1): the build runner (a task or a check of a branch starts and ends, its ETA the median of the
last durations of its kind), `sudo awb deploy` (start and end, on a sealed host only), `awb winlab open` (the window
end is the ETA and the window ends by itself then) and `awb winlab close`, a dev ask written (`awb dev ask`) and
closed (`awb dev answer`, `awb dev close`). A start with the key (kind, id) of an item still open renews its ETA.

The readers: `running_now` (open items with their ETA, overdue when the ETA passed) and `ended_since` (ended items
since a time, with the start when it is known); `awb owner status` prints both first, the owner status file carries
`running`.

Not in phase 1: the awb timer units, the vault and key service lock, the Claude logins and the push mail.
"""
from __future__ import annotations

import datetime as dt
import json
import os
import re
import stat
import statistics
from pathlib import Path

from awb import codes

FILE = "events.jsonl"
KINDS = ("task", "check", "deploy", "lab", "ask")
OUTCOMES = ("ok", "red", "stopped", "needs-you")
ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._@/-]{0,79}")
PROJECT_RE = re.compile(r"(?:%s|%s)" % (codes.PROJECT, codes.TCP_PROJECT_WIDE))   # status_shape.PROJECT_CODE_RE
TIME_RE = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z")
LINE_MAX = 512
READ_MAX = 1024 * 1024
"""Only the last MiB of the file is read: thousands of lines, weeks of events."""
HISTORY = 10
"""The ETA of a task, a check or a deploy: the median duration of the last HISTORY ended items of its kind."""
RUNNING_MAX = 200
KIND_SHOWN = {"task": "build task", "check": "check", "deploy": "deploy", "lab": "lab window", "ask": "dev ask"}


def _z(t: dt.datetime) -> str:
    return t.astimezone(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse(stamp: str) -> dt.datetime:
    return dt.datetime.strptime(stamp, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=dt.timezone.utc)


def _now(now: dt.datetime | None) -> dt.datetime:
    """An aware time in UTC; a naive one is host local time (the build runner's clock)."""
    now = now or dt.datetime.now(dt.timezone.utc)
    return now.astimezone(dt.timezone.utc)


def folder(p=None) -> Path:
    """The folder of the events file: the board's, `<shared>/build`."""
    if p is None:
        from awb import config

        p = config.paths()
    return Path(p.shared) / "build"


# --------------------------------------------------------------------------- writing


def _valid(kind: str, eid: str, project: str | None) -> bool:
    return (kind in KINDS and isinstance(eid, str) and bool(ID_RE.fullmatch(eid))
            and (project is None or (isinstance(project, str) and bool(PROJECT_RE.fullmatch(project)))))


def _append(board: Path, data: dict, create: bool) -> bool:
    line = (json.dumps(data, separators=(",", ":"), sort_keys=True) + "\n").encode("ascii")
    if len(line) > LINE_MAX:
        return False
    try:
        if create and not os.path.lexists(board) and os.geteuid() != 0:
            from awb import config

            config.make_dir(board, 0o750, shared=True)
        dfd = os.open(board, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
    except (OSError, ValueError):
        return False
    try:
        flags = os.O_WRONLY | os.O_APPEND | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC
        # never O_CREAT on a file that exists: in the sticky board folder fs.protected_regular=2 refuses it when
        # another user owns the file
        try:
            fd = os.open(FILE, flags, dir_fd=dfd)
        except FileNotFoundError:
            try:
                fd = os.open(FILE, flags | os.O_CREAT | os.O_EXCL, 0o660, dir_fd=dfd)
                os.fchmod(fd, 0o660)        # the group appends too: the umask took its bit
            except FileExistsError:
                fd = os.open(FILE, flags, dir_fd=dfd)
        try:
            if not stat.S_ISREG(os.fstat(fd).st_mode):
                return False
            return os.write(fd, line) == len(line)
        finally:
            os.close(fd)
    except OSError:
        return False
    finally:
        os.close(dfd)


def start(kind: str, eid: str, *, project: str | None = None, eta: dt.datetime | str | None = None,
          auto_end: bool = False, history: bool = False, board: Path | None = None, now: dt.datetime | None = None,
          create: bool = True) -> bool:
    """Write the start of KIND EID. `eta` the expected end; with `history` and no `eta` it is now plus the median
    of the last durations of KIND. `auto_end`: the item ends by itself at its ETA (a lab window). False when nothing
    was written; never raises."""
    try:
        if not _valid(kind, eid, project):
            return False
        now = _now(now)
        board = board or folder()
        if isinstance(eta, dt.datetime):
            eta = _z(eta)
        if eta is None and history:
            seconds = median_seconds(read(board), kind)
            eta = _z(now + dt.timedelta(seconds=seconds)) if seconds is not None else None
        if eta is not None and not (isinstance(eta, str) and TIME_RE.fullmatch(eta)):
            return False
        return _append(board, {"t": _z(now), "ev": "start", "kind": kind, "id": eid, "project": project,
                               "eta": eta, "auto_end": bool(auto_end)}, create)
    except Exception:
        return False


def end(kind: str, eid: str, outcome: str, *, project: str | None = None, board: Path | None = None,
        now: dt.datetime | None = None, create: bool = True) -> bool:
    """Write the end of KIND EID with OUTCOME. False when nothing was written; never raises."""
    try:
        if not _valid(kind, eid, project) or outcome not in OUTCOMES:
            return False
        return _append(board or folder(), {"t": _z(_now(now)), "ev": "end", "kind": kind, "id": eid,
                                           "project": project, "outcome": outcome}, create)
    except Exception:
        return False


def build_outcome(state: str) -> str | None:
    """The outcome of a state of the build runner (awb.build.STATES) that ends a run; None for a state that is still
    open work."""
    word = (state or "").split(":", 1)[0]
    if word in ("done", "passed"):
        return "ok"
    if word in ("red", "hung", "stale", "interrupted"):
        return "red"
    if word == "stopped":
        return "stopped"
    if word in ("asked", "held"):
        return "needs-you"
    return None


# --------------------------------------------------------------------------- reading


def _line_ok(e) -> bool:
    if not isinstance(e, dict) or not isinstance(e.get("t"), str) or not TIME_RE.fullmatch(e["t"]):
        return False
    if not _valid(e.get("kind"), e.get("id"), e.get("project")):
        return False
    if e.get("ev") == "start":
        return set(e) == {"t", "ev", "kind", "id", "project", "eta", "auto_end"} and isinstance(
            e["auto_end"], bool) and (e["eta"] is None or (isinstance(e["eta"], str) and bool(TIME_RE.fullmatch(
                e["eta"]))))
    if e.get("ev") == "end":
        return set(e) == {"t", "ev", "kind", "id", "project", "outcome"} and e["outcome"] in OUTCOMES
    return False


def read(board: Path | None = None) -> list[dict]:
    """The checked lines of the events file, oldest first; a line of another shape is skipped, a missing or
    unreadable file is no events."""
    board = board or folder()
    try:
        fd = os.open(Path(board) / FILE, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
    except OSError:
        return []
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode):
            return []
        cut = st.st_size > READ_MAX
        if cut:
            os.lseek(fd, st.st_size - READ_MAX, os.SEEK_SET)
        raw = b""
        while len(raw) < READ_MAX:
            chunk = os.read(fd, READ_MAX - len(raw))
            if not chunk:
                break
            raw += chunk
    except OSError:
        return []
    finally:
        os.close(fd)
    lines = raw.split(b"\n")
    if cut:
        lines = lines[1:]           # the first line may be a part
    out = []
    for line in lines:
        if not line.strip():
            continue
        try:
            e = json.loads(line.decode("ascii"))
        except (UnicodeDecodeError, ValueError, RecursionError):
            continue
        if _line_ok(e):
            out.append(e)
    return out


def _pair(events: list[dict], now: dt.datetime) -> tuple[list[dict], list[dict]]:
    """(open, ended): the items of the events in file order. An open item is {kind, id, project, since, eta,
    auto_end}; an ended item {kind, id, project, start, end, outcome}, start None when its start is not in the
    events. An item that ends by itself is ended `ok` at its ETA once that passed."""
    opened: dict[tuple[str, str], dict] = {}
    ended: list[dict] = []

    def close_auto(key, item, before: dt.datetime) -> bool:
        if item["auto_end"] and item["eta"] and _parse(item["eta"]) <= before:
            ended.append({"kind": item["kind"], "id": item["id"], "project": item["project"],
                          "start": item["since"], "end": item["eta"], "outcome": "ok"})
            del opened[key]
            return True
        return False

    for e in events:
        key = (e["kind"], e["id"])
        cur = opened.get(key)
        t = _parse(e["t"])
        if cur is not None and close_auto(key, cur, t):
            cur = None
        if e["ev"] == "start":
            if cur is None:
                opened[key] = {"kind": e["kind"], "id": e["id"], "project": e["project"], "since": e["t"],
                               "eta": e["eta"], "auto_end": e["auto_end"]}
            else:
                cur.update(eta=e["eta"], auto_end=e["auto_end"], project=e["project"] or cur["project"])
        else:
            ended.append({"kind": e["kind"], "id": e["id"], "project": e["project"] or (cur or {}).get("project"),
                          "start": cur["since"] if cur else None, "end": e["t"], "outcome": e["outcome"]})
            opened.pop(key, None)
    for key, item in list(opened.items()):
        close_auto(key, item, now)
    ended.sort(key=lambda x: x["end"])
    return list(opened.values()), ended


def running_now(events: list[dict], now: dt.datetime | None = None) -> list[dict]:
    """The open items, oldest first: {kind, id, project, since, eta, overdue}; overdue when the ETA passed."""
    now = _now(now)
    opened, _ = _pair(events, now)
    return [{"kind": i["kind"], "id": i["id"], "project": i["project"], "since": i["since"], "eta": i["eta"],
             "overdue": bool(i["eta"]) and _parse(i["eta"]) < now}
            for i in sorted(opened, key=lambda i: i["since"])][:RUNNING_MAX]


def ended_since(events: list[dict], since: dt.datetime, now: dt.datetime | None = None) -> list[dict]:
    """The items that ended at or after `since`, oldest first: {kind, id, project, start, end, outcome}."""
    since = _now(since)
    _, ended = _pair(events, _now(now))
    return [i for i in ended if _parse(i["end"]) >= since]


def median_seconds(events: list[dict], kind: str, last: int = HISTORY) -> float | None:
    """The median duration of the last `last` ended items of KIND whose start is known; None without one."""
    _, ended = _pair(events, _now(None))
    spans = [(_parse(i["end"]) - _parse(i["start"])).total_seconds() for i in ended
             if i["kind"] == kind and i["start"] is not None]
    spans = [s for s in spans if s >= 0][-last:]
    return statistics.median(spans) if spans else None


# --------------------------------------------------------------------------- the owner's view


def _hm(stamp: str | None, today: dt.date) -> str:
    if not stamp:
        return "-"
    t = _parse(stamp).astimezone()
    return t.strftime("%H:%M") if t.date() == today else t.strftime("%m-%d %H:%M")


def _what(item: dict) -> str:
    text = "%s %s" % (KIND_SHOWN[item["kind"]], item["id"])
    return text + (" (%s)" % item["project"] if item["project"] and item["project"] not in item["id"] else "")


def status_lines(board: Path | None = None, now: dt.datetime | None = None, hours: int = 24) -> list[str]:
    """The two blocks `awb owner status` prints first: Running now (item, since, ETA) and Ended in the last
    `hours` hours (red, stopped and needs-you marked in capitals)."""
    now = _now(now)
    today = now.astimezone().date()
    events = read(board)
    lines = ["Running now"]
    running = running_now(events, now)
    for i in running:
        eta = ", ETA %s%s" % (_hm(i["eta"], today), " (overdue)" if i["overdue"] else "") if i["eta"] else ""
        lines.append("  %s since %s%s" % (_what(i), _hm(i["since"], today), eta))
    if not running:
        lines.append("  nothing runs")
    lines.append("Ended in the last %d h" % hours)
    ended = ended_since(events, now - dt.timedelta(hours=hours), now)
    for i in reversed(ended):
        mark = "ok" if i["outcome"] == "ok" else i["outcome"].upper()
        started = " (started %s)" % _hm(i["start"], today) if i["start"] else ""
        lines.append("  %s %s at %s%s" % (mark, _what(i), _hm(i["end"], today), started))
    if not ended:
        lines.append("  nothing ended")
    return lines


def running_rows(p=None, now: dt.datetime | None = None) -> list[dict]:
    """The `running` field of the owner status file: running_now of the events file of P, [] on any error."""
    try:
        return running_now(read(folder(p)), now)
    except Exception:
        return []
