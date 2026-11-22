"""The budget of the build runner (owner's yes of 2026-11-22): a subscription limit holds a task, it does not fail it,
and with `build_model = auto` the background work goes on on the model key until the limit resets.

    build_model = login     his subscription login, as before; a limit hit holds the task and the queue until the reset
    build_model = key       the model key, as before; every key run is counted in the ledger, no cap holds it
    build_model = auto      the login; after a limit hit the held task runs again and new ones run on the key until the
                            reset time passes, then the login again. On the key a block's opus becomes sonnet unless
                            its title names a security review (the word "security"); the weekly cap holds the key runs
    key_week_cap_usd = 25   the cap of the key runs of one ISO week (UTC), in USD; 25 when the line is missing

What a run reports as its cost is `total_cost_usd` of the client's one JSON object; a run that reports none counts
its --max-budget-usd. A task that starts under the cap may end above it by its budget at most.

The records lie in the runner's own runs folder, the owner's alone (600):

    runs/budget.json        {"version": 1, "until": EPOCH or null, "announced": null, "key" or "cap WEEK"}
    runs/key-ledger.tsv     one line per key run: week, epoch, task, run, the key of the session part, USD, source

A switch writes one event (kind `budget`, awb/events.py) and one mail of the runner: to the key (the event's ETA is
the reset), back to the login, the cap reached. Codes and fixed words only. Each mail names the spend of the week.

The limit texts of the client are in LIMIT_RE, EPOCH_RE and RESET_RE alone. A reset the text does not give is
DEFAULT_WAIT from the hit: the login is tried again then. A second hit holds again.
"""
from __future__ import annotations

import contextlib
import datetime as dt
import json
import os
import re
import stat
from pathlib import Path

from awb import safefile

# no import of awb.build: `awb owner status` reads this module; awb.build reaches the runner
RUN_ID_RE = re.compile(r"[BC]-[0-9]{3,4}")      # awb.build.ID_RE and CHECK_ID_RE

MODES = ("login", "key", "auto")
STATE_FILE = "budget.json"
LEDGER_FILE = "key-ledger.tsv"
HELD_LIMIT = "held: usage limit"
HOLD_LIMIT = "usage limit"
HOLD_CAP = "key cap"
HOLDS = (HOLD_LIMIT, HOLD_CAP)
"""The queue holds of the budget: the switch mail says them, the runner sends no queue held mail for them."""
CAP_DEFAULT = 25.0
DEFAULT_WAIT = 3600
"""Seconds from a hit whose text names no reset to the next try on the login."""
SCAN_MAX = 256 << 10
LEDGER_MAX = 4 << 20
EVENT_FALLBACK = "key-fallback"
EVENT_CAP = "key-cap"
SECURITY_RE = re.compile(r"(?i)\bsecurity\b")

LIMIT_RE = re.compile(
    r"(?i)(?:hit your (?:weekly |5-hour |session |opus |sonnet |usage |daily )?limit"
    r"|(?:weekly|5-hour|session|usage|opus|sonnet) limit (?:reached|hit)"
    r"|claude ai usage limit reached"
    r"|rate_limit_error.{0,200}\b429\b|\b429\b.{0,200}rate_limit)", re.S)
"""A subscription limit in the client's output: the weekly and the 5-hour form (`You've hit your weekly limit ·
resets 12am (UTC)`), the older `Claude AI usage limit reached|EPOCH` and a 429 rate_limit answer."""
EPOCH_RE = re.compile(r"(?i)usage limit reached\|(\d{9,11})")
_MONTHS = ("jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec")
RESET_RE = re.compile(
    r"(?i)resets\s+(?:at\s+|on\s+)?"
    r"(?:(?P<mon>jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\.?\s+(?P<day>\d{1,2}),?\s+(?:at\s+)?)?"
    r"(?P<h>\d{1,2})(?::(?P<m>\d{2}))?\s*(?P<ap>am|pm)?"
    r"(?:\s*\((?P<tz>[A-Za-z_]+(?:/[A-Za-z_+-]+){0,2})\))?")


# --------------------------------------------------------------------------- the limit in the output

def _zone(name: str | None) -> dt.tzinfo:
    if not name or name.upper() in ("UTC", "GMT", "Z"):
        return dt.timezone.utc
    try:
        from zoneinfo import ZoneInfo

        return ZoneInfo(name)
    except Exception:
        return dt.timezone.utc


def parse_reset(text: str, wall: float) -> int | None:
    """The reset time a limit text names, as an epoch after `wall`; None when it names none."""
    m = EPOCH_RE.search(text)
    if m:
        return int(m.group(1))
    m = RESET_RE.search(text)
    if not m:
        return None
    hour, minute = int(m.group("h")), int(m.group("m") or 0)
    ap = (m.group("ap") or "").lower()
    if ap:
        if not 1 <= hour <= 12:
            return None
        hour = hour % 12 + (12 if ap == "pm" else 0)
    if hour > 23 or minute > 59:
        return None
    zone = _zone(m.group("tz"))
    now = dt.datetime.fromtimestamp(wall, zone)
    if m.group("mon"):
        month, day = _MONTHS.index(m.group("mon").lower()[:3]) + 1, int(m.group("day"))
        for year in (now.year, now.year + 1):
            try:
                at = dt.datetime(year, month, day, hour, minute, tzinfo=zone)
            except ValueError:
                return None
            if at.timestamp() > wall - 86400:
                return int(at.timestamp())
        return None
    at = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if at.timestamp() <= wall:
        at += dt.timedelta(days=1)
    return int(at.timestamp())


def find_limit(texts: list[bytes | str | None], wall: float) -> tuple[bool, int | None]:
    """(hit, reset) from the session's output (the result object, its stderr): hit when a limit text is there,
    reset the epoch it names or None."""
    for raw in texts:
        if raw is None:
            continue
        text = raw[:SCAN_MAX].decode("utf-8", "replace") if isinstance(raw, bytes) else raw[:SCAN_MAX]
        if LIMIT_RE.search(text):
            return True, parse_reset(text, wall)
    return False, None


def fallback_model(model: str, title: str) -> str:
    """The model of a block on the key fallback: opus becomes sonnet unless the title names a security review."""
    if model == "opus" and not SECURITY_RE.search(title or ""):
        return "sonnet"
    return model


# --------------------------------------------------------------------------- the records

def week(wall: float) -> str:
    """The ISO week of an epoch in UTC, `2026-W47`."""
    return dt.datetime.fromtimestamp(wall, dt.timezone.utc).strftime("%G-W%V")


def next_week(wall: float) -> int:
    """The epoch of the next Monday 00:00 UTC."""
    t = dt.datetime.fromtimestamp(wall, dt.timezone.utc)
    monday = (t - dt.timedelta(days=t.weekday())).replace(hour=0, minute=0, second=0, microsecond=0)
    return int((monday + dt.timedelta(days=7)).timestamp())


def _read(path: Path, cap: int) -> bytes | None:
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
    except OSError:
        return None
    try:
        data = os.read(fd, cap + 1)
    except OSError:
        return None
    finally:
        os.close(fd)
    return data if len(data) <= cap else None


def load(runs: Path) -> dict:
    """budget.json of the runs folder; the empty state when it is missing or of another shape."""
    state = {"until": None, "announced": None}
    data = _read(Path(runs) / STATE_FILE, 4096)
    try:
        obj = json.loads(data.decode("utf-8")) if data else {}
    except (UnicodeDecodeError, ValueError):
        obj = {}
    if isinstance(obj, dict):
        if isinstance(obj.get("until"), int) and obj["until"] > 0:
            state["until"] = obj["until"]
        a = obj.get("announced")
        if a == "key" or (isinstance(a, str) and re.fullmatch(r"cap \d{4}-W\d{2}", a)):
            state["announced"] = a
    return state


def _mkdir(path: Path) -> None:
    """The runs folder (700), as awb.build._mkdir: an existing one must be a real folder of this uid."""
    try:
        os.mkdir(path, 0o700)
    except FileExistsError:
        st = os.lstat(path)
        if not stat.S_ISDIR(st.st_mode) or st.st_uid != os.geteuid():
            raise OSError("%s is not a folder of this user" % path.name) from None


def runs_dir() -> Path:
    """awb.build.runs_dir: AWB_BUILD_RUNS (tests), else `<build_root>/runs`."""
    from awb import config

    env = os.environ.get("AWB_BUILD_RUNS")
    return Path(env) if env else config.build_root() / "runs"


def save(runs: Path, state: dict) -> None:
    _mkdir(Path(runs))
    safefile.write_atomic(Path(runs) / STATE_FILE, (json.dumps(
        {"version": 1, "until": state.get("until"), "announced": state.get("announced")}, sort_keys=True)
        + "\n").encode("ascii"), mode=0o600)


def _ledger_rows(runs: Path) -> list[list[str]]:
    data = _read(Path(runs) / LEDGER_FILE, LEDGER_MAX)
    rows = []
    for line in (data or b"").decode("utf-8", "replace").splitlines():
        f = line.split("\t")
        if len(f) == 7 and re.fullmatch(r"\d{4}-W\d{2}", f[0]):
            rows.append(f)
    return rows


def spent(runs: Path, wall: float) -> float:
    """The USD of the key runs of the week of `wall`."""
    w = week(wall)
    total = 0.0
    for f in _ledger_rows(runs):
        if f[0] == w:
            with contextlib.suppress(ValueError):
                total += max(0.0, float(f[5]))
    return round(total, 4)


def cost_of(obj: dict | None, budget: float) -> tuple[float, str]:
    """(USD, source) of one key run: total_cost_usd of the result object, else the block's budget."""
    value = obj.get("total_cost_usd") if isinstance(obj, dict) else None
    if isinstance(value, (int, float)) and not isinstance(value, bool) and 0 <= value < 10000:
        return float(value), "reported"
    return float(budget), "budget"


def record(runs: Path, wall: float, tid: str, n: int, part: str, usd: float, source: str) -> bool:
    """One ledger line for a key run (tid, n, part once: a reconcile that sees the run again adds nothing)."""
    if not RUN_ID_RE.fullmatch(tid) or not re.fullmatch(r"[0-9a-z-]{1,40}", part):
        return False
    for f in _ledger_rows(runs):
        if f[2] == tid and f[3] == str(n) and f[4] == part:
            return False
    line = "%s\t%d\t%s\t%d\t%s\t%.4f\t%s\n" % (week(wall), int(wall), tid, n, part, usd, source)
    _mkdir(Path(runs))
    fd = os.open(Path(runs) / LEDGER_FILE, os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
    try:
        os.fchmod(fd, 0o600)
        os.write(fd, line.encode("ascii"))
    finally:
        os.close(fd)
    return True


# --------------------------------------------------------------------------- the mode now

def cap_of(conf: dict) -> float:
    try:
        value = float(conf.get("key_week_cap_usd", CAP_DEFAULT))
    except (TypeError, ValueError):
        return CAP_DEFAULT
    return value if 0 <= value < 100000 else CAP_DEFAULT


def choose(mode: str, runs: Path, wall: float, cap: float) -> tuple[str, str | None]:
    """(model, hold) of the next session: the model login or key, the hold usage limit (login mode, the limit not
    reset yet), key cap (auto mode on the key at the cap) or None."""
    if mode == "key":
        return "key", None
    until = load(runs)["until"]
    if until is None or wall >= until:
        return "login", None
    if mode != "auto":
        return "login", HOLD_LIMIT
    if spent(runs, wall) >= cap:
        return "key", HOLD_CAP
    return "key", None


def _utc(epoch: int | None) -> str:
    return dt.datetime.fromtimestamp(epoch, dt.timezone.utc).strftime("%Y-%m-%d %H:%M UTC") if epoch else "-"


def spend_text(runs: Path, wall: float, cap: float) -> str:
    return "%.2f of %.2f USD this week" % (spent(runs, wall), cap)


def mode_line(mode: str, runs: Path, wall: float, cap: float) -> str:
    """The one line of `awb build list` and `awb owner status`: login, key, key fallback until TIME or key cap
    reached, with the spend of the week."""
    if mode not in MODES:
        return "build model: unknown (build_model = %s)" % (mode if re.fullmatch(r"[a-z]{1,12}", mode or "")
                                                            else "?")
    model, hold = choose(mode, runs, wall, cap)
    until = load(runs)["until"]
    spend = spend_text(runs, wall, cap)
    if mode == "key":
        return "build model: key (build_model = key), %s" % spend
    if hold == HOLD_LIMIT:
        return "build model: login, usage limit until %s, the queue waits" % _utc(until)
    if hold == HOLD_CAP:
        return "build model: key cap reached, the queue waits until %s, %s" % (
            _utc(min(until, next_week(wall))), spend)
    if model == "key":
        return "build model: key fallback until %s, %s" % (_utc(until), spend)
    return "build model: login%s, %s" % (" (build_model = auto)" if mode == "auto" else "", spend)


def host_line(runs: Path | None = None, wall: float | None = None) -> str:
    """mode_line from the host file and the runs folder of this host; a line that says unknown on any error."""
    import time

    from awb import config

    try:
        conf = config.host_conf()
        return mode_line(conf.get("build_model", "login"), Path(runs or runs_dir()),
                         time.time() if wall is None else wall, cap_of(conf))
    except Exception:
        return "build model: unknown"


# --------------------------------------------------------------------------- the switches

def _event(kind: str, eid: str, board: Path | None, wall: float, eta: int | None = None) -> None:
    from awb import events

    if board is None:
        return
    now = dt.datetime.fromtimestamp(wall, dt.timezone.utc)
    if kind == "start":
        events.start("budget", eid, eta=dt.datetime.fromtimestamp(eta, dt.timezone.utc) if eta else None,
                     board=board, now=now)
    else:
        events.end("budget", eid, "ok", board=board, now=now)


def hit(runs: Path, wall: float, reset: int | None) -> int:
    """A limit hit on the login: the reset (DEFAULT_WAIT from now when the text named none) goes into budget.json;
    returns it."""
    until = reset if reset is not None and reset > wall else int(wall) + DEFAULT_WAIT
    state = load(runs)
    state["until"] = max(until, state["until"] or 0)
    save(runs, state)
    return state["until"]


def tick(mode: str, runs: Path, wall: float, cap: float, board: Path | None, mail) -> list[str]:
    """The switches since the last tick: one event and one mail(subject, lines) each. Returns their words (`key`,
    `cap`, `login`); never raises."""
    out: list[str] = []
    try:
        state = load(runs)
        until, announced = state["until"], state["announced"]
        if until is None:
            return out
        spend = spend_text(runs, wall, cap)
        if wall >= until:
            if mode == "auto" and announced is not None:
                if announced.startswith("cap "):
                    _event("end", EVENT_CAP, board, wall)
                _event("end", EVENT_FALLBACK, board, wall)
                mail("Workbench build: back on the login",
                     ["The subscription limit reset at %s." % _utc(until), "Build tasks run on the login again.",
                      "key spend: %s" % spend])
                out.append("login")
            save(runs, {"until": None, "announced": None})
            return out
        if mode != "auto":
            return out
        capped = spent(runs, wall) >= cap
        w = week(wall)
        if announced is None:
            _event("start", EVENT_FALLBACK, board, wall, until)
        if capped and announced != "cap " + w:
            if announced is None or not announced.startswith("cap "):
                _event("start", EVENT_CAP, board, wall, min(until, next_week(wall)))
            mail("Workbench build: key cap reached",
                 ["The weekly cap of the key runs is reached.",
                  "Build tasks wait until %s." % _utc(min(until, next_week(wall))), "key spend: %s" % spend])
            state["announced"] = "cap " + w
            out.append("cap")
        elif not capped and announced != "key":
            if announced is not None:
                _event("end", EVENT_CAP, board, wall)
            mail("Workbench build: on the API key",
                 ["The subscription limit was hit.", "Build tasks run on the API key until %s." % _utc(until),
                  "key spend: %s" % spend])
            state["announced"] = "key"
            out.append("key")
        save(runs, state)
    except Exception:
        pass
    return out
