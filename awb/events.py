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

Phase 2 (Q-0014): a lab run of a project (`awb lab run start` and `end`, kind `labrun`, red when a delete
failed), the runs of the awb timer units of the host (kind `job`, the unit's service without `.service`), the lock of
the vault and of the key service (kinds `vault` and `keys`: an open item is the daemon locked or down, its end the
unlock) and the Claude login of the build user (kind `login`, id `build-user`: an open item is a lapsed login, from
the build runner's `held: login missing`). The owner's minute timer (`awb owner status --write`) writes the last
three from what it sees (`observe_locks`, `observe_login`, `observe_jobs`) and then pushes (`push`): one mail per
event through the key service's notify, the path of the 08:00 mail, only for a red end, an end that needs the
owner, a lock, a lapsed login and a blocking dev ask answered. Each event is pushed once (its key in the owner's
state file); a mail that did not go out is tried again on the next minute for 24 hours.

Left out: a login that expires soon (no code reads the expiry of a login the owner cannot open) and the logins of
the owner and of the work user (no code detects them).
"""
from __future__ import annotations

import contextlib
import datetime as dt
import json
import os
import re
import stat
import statistics
from pathlib import Path

from awb import codes

FILE = "events.jsonl"
KINDS = ("task", "check", "deploy", "lab", "ask", "labrun", "job", "vault", "keys", "login")
STATE_KINDS = ("vault", "keys", "login")
"""An open item of these kinds is a state that waits on the owner (locked, lapsed), its end the state gone."""
LOGIN_IDS = ("build-user",)
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
KIND_SHOWN = {"task": "build task", "check": "check", "deploy": "deploy", "lab": "lab window", "ask": "dev ask",
              "labrun": "lab run", "job": "timer job", "vault": "vault", "keys": "key service", "login": "login"}
STATE_SHOWN = {"vault": ("the vault locked", "the vault unlocked"),
               "keys": ("the key service locked", "the key service unlocked"),
               "login": ("the Claude login of the %s lapsed", "the Claude login of the %s back")}


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
            and (project is None or (isinstance(project, str) and bool(PROJECT_RE.fullmatch(project))))
            and (kind not in ("vault", "keys") or (eid == kind and project is None))
            and (kind != "login" or (eid in LOGIN_IDS and project is None)))


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


def _what(item: dict, ended: bool = False) -> str:
    if item["kind"] in STATE_KINDS:
        text = STATE_SHOWN[item["kind"]][1 if ended else 0]
        return text % item["id"].replace("-", " ") if "%s" in text else text
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
        if i["kind"] in STATE_KINDS:
            started = " (%s since %s)" % ("lapsed" if i["kind"] == "login" else "locked",
                                          _hm(i["start"], today)) if i["start"] else ""
        lines.append("  %s %s at %s%s" % (mark, _what(i, ended=True), _hm(i["end"], today), started))
    if not ended:
        lines.append("  nothing ended")
    return lines


def running_rows(p=None, now: dt.datetime | None = None) -> list[dict]:
    """The `running` field of the owner status file: running_now of the events file of P, [] on any error."""
    try:
        return running_now(read(folder(p)), now)
    except Exception:
        return []


def ended_rows(p=None, now: dt.datetime | None = None, hours: int = 24) -> list[dict]:
    """The `ended` field of the owner status file: the items that ended in the last `hours` hours, the newest
    RUNNING_MAX of them, oldest first; [] on any error."""
    try:
        now = _now(now)
        return ended_since(read(folder(p)), now - dt.timedelta(hours=hours), now)[-RUNNING_MAX:]
    except Exception:
        return []


# --------------------------------------------------------------------------- what the owner's timer sees (phase 2)

LOCKED = ("locked", "down")
"""The daemon states (status_shape.DAEMON_STATES) that open a lock item; unlocked and plain end it, unknown changes
nothing."""
FREE = ("unlocked", "plain")
JOB_LONG = 60
"""Seconds a timer job runs before it is listed as running; a shorter run is written only when it ends red."""
JOB_SELF = ("awb-owner-status",)
"""The owner's minute timer that observes: never an item of its own."""


def _open_keys(evs: list[dict], now: dt.datetime) -> set[tuple[str, str]]:
    return {(i["kind"], i["id"]) for i in running_now(evs, now)}


def observe_locks(daemons: dict, *, board: Path | None = None, now: dt.datetime | None = None) -> list[str]:
    """Write the lock of the vault and of the key service from their states ({"vault": {"state": ...}, "keys": ...},
    the daemons of the owner status): a start when one is locked or down and no lock item is open, an end `ok` when
    it is unlocked again. Returns what was written ("vault locked", "keys unlocked"); never raises."""
    out = []
    try:
        now = _now(now)
        board = board or folder()
        opened = _open_keys(read(board), now)
        for name in ("vault", "keys"):
            state = (daemons.get(name) or {}).get("state")
            if state in LOCKED and (name, name) not in opened and start(name, name, board=board, now=now):
                out.append("%s locked" % name)
            elif state in FREE and (name, name) in opened and end(name, name, "ok", board=board, now=now):
                out.append("%s unlocked" % name)
    except Exception:
        pass
    return out


def observe_login(lapsed: bool | None, who: str = "build-user", *, board: Path | None = None,
                  now: dt.datetime | None = None) -> str | None:
    """Write the lapse of a Claude login: a start when it lapsed and no item is open, an end `ok` when it is back.
    `lapsed` None (not known) changes nothing. Returns "lapsed", "back" or None; never raises."""
    try:
        if lapsed is None:
            return None
        now = _now(now)
        board = board or folder()
        is_open = ("login", who) in _open_keys(read(board), now)
        if lapsed and not is_open and start("login", who, board=board, now=now):
            return "lapsed"
        if not lapsed and is_open and end("login", who, "ok", board=board, now=now):
            return "back"
    except Exception:
        pass
    return None


def _unix(value: str) -> dt.datetime | None:
    """A timestamp of `systemctl show --timestamp=unix` (`@1763630000`), None when empty or of another shape."""
    m = re.fullmatch(r"@(\d{9,11})", (value or "").strip())
    return dt.datetime.fromtimestamp(int(m.group(1)), dt.timezone.utc) if m else None


def job_units(run=None) -> list[dict]:
    """The runs of the awb timer units of the host, from systemctl as any user may ask it: one dict per service a
    timer `awb-*` activates (but JOB_SELF), {unit, active, result, start, exit}, the times aware datetimes or None.
    [] when systemctl cannot be asked. `run` is subprocess.run (the tests give their own)."""
    import subprocess

    run = run or subprocess.run
    try:
        timers = run(["systemctl", "list-timers", "--all", "--plain", "--no-legend", "--no-pager", "awb-*"],
                     capture_output=True, text=True, timeout=20)
        services: list[str] = []
        for line in (timers.stdout or "").splitlines():
            parts = line.split()
            name = parts[-1] if parts else ""
            if (name.endswith(".service") and ID_RE.fullmatch(name[:-8]) and name[:-8] not in JOB_SELF
                    and name not in services):
                services.append(name)
        if not services:
            return []
        shown = run(["systemctl", "show", "--timestamp=unix", "--no-pager",
                     "-p", "Id,ActiveState,Result,ExecMainStartTimestamp,ExecMainExitTimestamp", *services],
                    capture_output=True, text=True, timeout=20)
        text = shown.stdout or ""
    except Exception:
        return []
    out = []
    for block in text.split("\n\n"):
        f = dict(line.split("=", 1) for line in block.splitlines() if "=" in line)
        unit = f.get("Id", "")
        if unit not in services:
            continue
        out.append({"unit": unit[:-8], "active": f.get("ActiveState") in ("active", "activating", "reloading"),
                    "result": f.get("Result", ""), "start": _unix(f.get("ExecMainStartTimestamp", "")),
                    "exit": _unix(f.get("ExecMainExitTimestamp", ""))})
    return out


def observe_jobs(units: list[dict], seen: dict | None, *, board: Path | None = None,
                 now: dt.datetime | None = None) -> dict:
    """Write the runs of the timer jobs of `units` (job_units) and return the new `seen` ({unit: {start, open,
    done}}, kept in the owner's state file). A run that lasts JOB_LONG seconds or more gets its start (ETA the
    median of the last jobs) while it runs; an ended run gets its end (ok on the result success, else red) when its
    start was written or it is red, a red short run with its start at its real time. `seen` None (the first tick
    ever) only learns where every unit stands and writes nothing. Never raises."""
    first = seen is None
    seen = {k: dict(v) for k, v in (seen or {}).items() if isinstance(v, dict)}
    try:
        now = _now(now)
        board = board or folder()
        for u in units:
            unit, begun = u.get("unit"), u.get("start")
            if not isinstance(unit, str) or not ID_RE.fullmatch(unit) or not isinstance(begun, dt.datetime):
                continue
            stamp = _z(begun)
            prev = seen.get(unit) or {}
            if prev.get("start") != stamp:
                prev = {"start": stamp, "open": False, "done": False}
            exited = u.get("exit")
            finished = not u.get("active") and isinstance(exited, dt.datetime) and exited >= begun
            if first:
                prev["done"] = bool(finished)
            elif not finished and not prev["open"] and (now - begun).total_seconds() >= JOB_LONG:
                prev["open"] = start("job", unit, history=True, board=board, now=begun)
            elif finished and not prev["done"]:
                outcome = "ok" if u.get("result") == "success" else "red"
                if outcome == "red" and not prev["open"]:
                    prev["open"] = start("job", unit, board=board, now=begun)
                if prev["open"]:
                    end("job", unit, outcome, board=board, now=exited)
                prev["done"] = True
            seen[unit] = prev
    except Exception:
        pass
    return seen


# --------------------------------------------------------------------------- the push (phase 2)

PUSH_HOURS = 24
"""An event older than this is never pushed: a mail that could not go out is tried again for so long."""
PUSH_MAX = 5
"""Mails of one tick; the rest go on the next minute."""
SENT_MAX = 2000
CLOSED_IDS = ("vault", "keys", "login", "job")
"""Kinds whose ids are fixed words of the host (the daemons, the login ids, the unit names): their push goes out
also while the name check cannot run (the vault locked)."""


def _key(e: dict) -> str:
    return "%s|%s|%s|%s" % (e["t"], e["ev"], e["kind"], e["id"])


def push_candidates(evs: list[dict], now: dt.datetime | None = None, blocking=None,
                    hours: int = PUSH_HOURS) -> list[dict]:
    """The events of the last `hours` hours that are pushed, oldest first: {key, word, kind, id, project, t,
    start}. A red end (`red`), an end that needs the owner (`needs-you`), a lock or a lapsed login still open
    (`locked`, `lapsed`) and the answer of a dev ask that blocked its project (`unblocked`, `blocking(handle)`
    says it). Nothing else is pushed."""
    now = _now(now)
    since = now - dt.timedelta(hours=hours)
    opened = _open_keys(evs, now)
    starts: dict[tuple[str, str], tuple[str, str | None]] = {}
    out = []
    for e in evs:
        k = (e["kind"], e["id"])
        word = None
        project = e["project"]
        if e["ev"] == "start":
            starts.setdefault(k, (e["t"], e["project"]))
            began = e["t"]
            if e["kind"] in STATE_KINDS and k in opened:
                word = "lapsed" if e["kind"] == "login" else "locked"
        else:
            began, first_project = starts.pop(k, (None, None))
            project = project or first_project
            if e["outcome"] in ("red", "needs-you"):
                word = e["outcome"]
            elif e["kind"] == "ask" and e["outcome"] == "ok" and blocking is not None:
                try:
                    word = "unblocked" if blocking(e["id"]) else None
                except Exception:
                    word = None
        if word and _parse(e["t"]) >= since:
            out.append({"key": _key(e), "word": word, "kind": e["kind"], "id": e["id"], "project": project,
                        "t": e["t"], "start": began})
    return out


def _utc(stamp: str | None) -> str:
    return _parse(stamp).strftime("%Y-%m-%d %H:%M UTC") if stamp else "-"


def message(c: dict) -> tuple[str, str]:
    """The subject and the body of the push of candidate C: fixed words, the kind, the id, the project code and
    times in UTC, nothing else."""
    what = _what({"kind": c["kind"], "id": c["id"], "project": c["project"]})
    if c["word"] == "red":
        subject = "Workbench: RED %s" % what
        body = "%s ended red at %s%s.\n" % (what, _utc(c["t"]), ", started %s" % _utc(c["start"]) if c["start"]
                                            else "")
    elif c["word"] == "needs-you":
        subject = "Workbench: %s needs you" % what
        body = "%s stopped at %s and waits on you.\n" % (what, _utc(c["t"]))
    elif c["word"] == "unblocked":
        subject = "Workbench: blocking %s answered" % what
        body = "%s, which blocked its project, was answered at %s.\n" % (what, _utc(c["t"]))
    else:
        subject = "Workbench: %s" % what
        body = "%s since %s.\n" % (what[0].upper() + what[1:], _utc(c["t"]))
        body += ("Unlock it in your terminal.\n" if c["word"] == "locked"
                 else "Log the build user in again in your terminal, then retry the held task.\n")
    return subject, body + "awb owner status lists what runs now and what ended.\n"


def read_state(path: Path) -> dict | None:
    """The owner's state of the push and of the jobs: {"sent": [key, ...], "jobs": {...} or None}; None when the
    file does not exist (the first tick), an empty state when it cannot be read."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
    except FileNotFoundError:
        return None
    except OSError:
        return {"sent": [], "jobs": {}}
    try:
        st = os.fstat(fd)
        data = None
        if stat.S_ISREG(st.st_mode) and st.st_size <= READ_MAX:
            data = json.loads(os.read(fd, READ_MAX + 1).decode("utf-8"))
    except (OSError, UnicodeError, ValueError, RecursionError):
        data = None
    finally:
        os.close(fd)
    data = data if isinstance(data, dict) else {}
    sent, jobs = data.get("sent"), data.get("jobs")
    return {"sent": [k for k in sent if isinstance(k, str)] if isinstance(sent, list) else [],
            "jobs": jobs if isinstance(jobs, dict) else {}}


def write_state(path: Path, state: dict) -> bool:
    """Write the state atomically, mode 600, the last SENT_MAX keys. False when it cannot be written."""
    import tempfile

    path = Path(path)
    try:
        fd, tmp = tempfile.mkstemp(prefix=".events-state.", dir=path.parent)
    except OSError:
        return False
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump({"version": 1, "sent": list(state.get("sent") or [])[-SENT_MAX:],
                       "jobs": state.get("jobs") or {}}, fh, sort_keys=True)
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
        return True
    except OSError:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        return False


def push(state: dict | None, send, *, check=None, blocking=None, board: Path | None = None,
         now: dt.datetime | None = None, limit: int = PUSH_MAX) -> tuple[dict, dict]:
    """One mail per new candidate (push_candidates) through `send(subject, body) -> bool`, at most `limit` a tick.
    `state` None (no state file yet) marks every candidate of now as pushed and sends nothing, so the first tick
    after a deploy mails no old event. `check(text)` is the name check, "passed", "unavailable" or "found": a found
    one is never sent and never tried again, an unavailable one goes out only for a kind of CLOSED_IDS. A mail that
    did not go out stays new and the tick stops there. Returns the new sent keys as a state ({"sent": [...]}) and the
    counts {new, sent, held, withheld}; never raises."""
    counts = {"new": 0, "sent": 0, "held": 0, "withheld": 0}
    now = _now(now)
    cutoff = _z(now - dt.timedelta(hours=PUSH_HOURS + 1))
    sent = [k for k in (state or {}).get("sent") or [] if isinstance(k, str) and k[:20] >= cutoff]
    done = set(sent)
    try:
        cands = [c for c in push_candidates(read(board or folder()), now, blocking) if c["key"] not in done]
    except Exception:
        cands = []
    counts["new"] = len(cands)
    if state is None:
        return {"sent": sent + [c["key"] for c in cands]}, counts
    for n, c in enumerate(cands):
        if counts["sent"] >= limit:
            counts["held"] += len(cands) - n
            break
        subject, body = message(c)
        verdict = "passed"
        if check is not None:
            try:
                verdict = check(subject + "\n" + body)
            except Exception:
                verdict = "unavailable"
        if verdict == "found":
            counts["withheld"] += 1
            sent.append(c["key"])
            continue
        if verdict != "passed" and c["kind"] not in CLOSED_IDS:
            counts["held"] += 1
            continue
        try:
            ok = send(subject, body) is True
        except Exception:
            ok = False
        if not ok:
            counts["held"] += len(cands) - n
            break
        counts["sent"] += 1
        sent.append(c["key"])
    return {"sent": sent}, counts
