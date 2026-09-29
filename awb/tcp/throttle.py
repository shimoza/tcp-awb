"""How the Workbench stays far below the request limits of the TCP API gateway.

TCP throttles the calls of a tenant (HTTP 429, error code Common.1503, "Limited by API traffic control"); the
documentation publishes no number for it. So the Workbench never sends a burst and counts what it sends:

- `wait_turn(host)`: at most one call every MIN_INTERVAL seconds to one host from one user, shared by every process
  of that user through a lock file (a service with a private /tmp keeps its own). A loop of a hundred calls is
  spread out instead of arriving at once.
- `retry_after(value)`: the wait a 429 or 503 answer names in its Retry-After header, in seconds, capped at
  MAX_RETRY_AFTER; the client waits that long before it tries again.
- `log_call(...)` and `usage_lines(...)`: one line per call to the tenant API in
  `<shared>/tenants/calls-YYYY-MM.jsonl` (time, tenant alias, service, region, method, status, seconds waited),
  never a path, an id or a body; `awb cloud usage` and the portal's tenant page count them per day and show every 429.
"""
from __future__ import annotations

import datetime
import email.utils
import fcntl
import json
import os
import re
import tempfile
import threading
import time
from pathlib import Path

MIN_INTERVAL = 0.25
"""Seconds between two calls to one host: at most four per second, far below any gateway limit a tenant meets."""
MAX_RETRY_AFTER = 30.0
CALLS = "calls-%s.jsonl"
_SAFE_RE = re.compile(r"[^A-Za-z0-9.-]+")
_local_lock = threading.Lock()
_local_last: dict[str, float] = {}


def throttle_dir() -> Path:
    return Path(os.environ.get("AWB_THROTTLE_DIR") or Path(tempfile.gettempdir()) / ("awb-throttle-%d" % os.getuid()))


def wait_turn(host: str, interval: float = MIN_INTERVAL, clock=time.time, sleep=time.sleep) -> float:
    """Wait until `interval` seconds have passed since the last call of this user to `host`; return the wait."""
    name = _SAFE_RE.sub("_", host)[:120] or "host"
    try:
        d = throttle_dir()
        d.mkdir(mode=0o700, parents=True, exist_ok=True)
        fd = os.open(d / name, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    except OSError:
        with _local_lock:        # no lock file here: the calls of this process are spaced all the same
            waited = max(0.0, _local_last.get(name, 0.0) + interval - clock())
            if waited:
                sleep(waited)
            _local_last[name] = clock()
            return waited
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        raw = os.pread(fd, 64, 0).decode("ascii", "replace").strip()
        try:
            last = float(raw)
        except ValueError:
            last = 0.0
        waited = max(0.0, last + interval - clock())
        if waited:
            sleep(waited)
        stamp = ("%.6f" % clock()).encode("ascii")
        os.ftruncate(fd, 0)
        os.pwrite(fd, stamp, 0)
        return waited
    finally:
        os.close(fd)


def retry_after(value: str | None, now: datetime.datetime | None = None) -> float | None:
    """Seconds from a Retry-After header (a number or an HTTP date), capped; None when there is none."""
    if not value:
        return None
    value = value.strip()
    if value.isdigit():
        return min(float(value), MAX_RETRY_AFTER)
    try:
        when = email.utils.parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return None
    now = now or datetime.datetime.now(datetime.timezone.utc)
    return min(max(0.0, (when - now).total_seconds()), MAX_RETRY_AFTER)


def log_call(tenant: str, service: str, region: str, method: str, status: int, waited: float,
             shared: Path | None = None, now: datetime.datetime | None = None) -> None:
    """One line per call; a log that cannot be written never fails the call."""
    now = now or datetime.datetime.now(datetime.timezone.utc)
    try:
        if shared is None:
            from awb import config

            shared = config.paths().shared
        f = Path(shared) / "tenants" / (CALLS % now.strftime("%Y-%m"))
        line = json.dumps({"t": now.strftime("%Y-%m-%dT%H:%M:%SZ"), "tenant": tenant, "service": service,
                           "region": region, "method": method, "status": status, "waited": round(waited, 2)})
        if not f.parent.is_dir():
            from awb import config

            config.make_dir(f.parent, 0o770, shared=True)
        with open(f, "a", encoding="utf-8") as fh:
            fh.write(line + "\n")
    except OSError:
        pass


def calls(shared: Path, month: str) -> list[dict]:
    try:
        lines = (Path(shared) / "tenants" / (CALLS % month)).read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        return []
    out = []
    for line in lines:
        try:
            out.append(json.loads(line))
        except ValueError:
            continue
    return out


def usage_lines(shared: Path, month: str) -> list[str]:
    days: dict[str, dict] = {}
    for c in calls(shared, month):
        d = days.setdefault(c.get("t", "")[:10], {"calls": 0, "429": 0, "errors": 0, "waited": 0.0, "services": {}})
        d["calls"] += 1
        d["waited"] += float(c.get("waited") or 0)
        status = int(c.get("status") or 0)
        if status == 429:
            d["429"] += 1
        elif status == 0 or status >= 500:
            d["errors"] += 1
        svc = c.get("service", "?")
        d["services"][svc] = d["services"].get(svc, 0) + 1
    if not days:
        return ["%s: no call to the TCP API recorded" % month]
    out = ["%-10s  %5s  %4s  %6s  %7s  %s" % ("day", "calls", "429", "errors", "waited", "by service")]
    for day in sorted(days):
        d = days[day]
        out.append("%-10s  %5d  %4d  %6d  %6.1fs  %s" % (day, d["calls"], d["429"], d["errors"], d["waited"],
                   " ".join("%s %d" % kv for kv in sorted(d["services"].items()))))
    total_429 = sum(d["429"] for d in days.values())
    out.append("throttled (429) this month: %d%s" % (total_429, "  <- look at the days above" if total_429 else ""))
    return out
