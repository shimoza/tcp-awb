"""The health strip, the Now block, the dev asks and the queue of the console (Q-0026 and Q-0025, section 5 of the
UX review of 2026-11-19, W1 to W4).

    GET /api/health      both console pages, from the console data service (awb.tcp.web.projects_api, user awb):
                         {"generated_at", "health": [tile], "now": [item]}
    the owner status     `awb owner status --write` adds `dev_asks`, `queue` and `lists_built` to the status file;
                         owner-actions serves them as GET /api/owner/dev-asks and GET /api/owner/queue

A tile is {"id", "label", "state", "value", "detail"}: state is ok, warn, bad or unknown, value and detail are fixed
words with ids, short hashes, numbers and times. The six tiles:

    vault     the ping of the check socket: unlocked, plain, locked since TIME or no answer
    keys      the ping of the key service's call socket (group awb): unlocked, locked or no answer
    runner    <shared>/build/state.tsv: the queue held with its reason, the running task or check, else idle
    release   the live release of /opt/tcp-awb (the src link), main and how many commits are not deployed, the deploy
              journal when a deploy has not finished
    disk      free space of the root file system
    web       the console gateway (awb-web) on the live release: its process started after the switch of the last
              deploy (/proc/PID/stat against the `flip` of the journal, same boot), else it runs an older release

An item of Now is {"kind", "id", "state", "start", "end", "note"}: kind task or check (the open build work of
state.tsv with its start), lab (a window notice of awb winlab in <shared>/notices/ with its end), deploy (a deploy
that has not finished or main ahead of the live release). Codes, ids and fixed words only; no machine name and no
free text. Nothing runs a command: a request reads files, /proc and two sockets and the console data service keeps
the answer CACHE_SECONDS.

The dev asks and the queue rows are built on the owner side (only the owner reads queue.tsv) and carry free text: the
title of an ask, the what and next of a queue row. Each text passes the screen of the owner status (the name check
of `summary_screen`, the same screen as the summaries of the UI runs) before it is written; a text with a finding is
withheld with its class. The texts of one build go to the check in one call and one by one only after a finding, so
the minute timer stays inside the owner's name check budget.
"""
from __future__ import annotations

import datetime as dt
import json
import os
import re
import stat
import time
from pathlib import Path
from typing import Callable

from awb import codes

OPT = Path("/opt/tcp-awb")
PROC = Path("/proc")
DISK = Path("/")
CACHE_SECONDS = 15.0
STATES = ("ok", "warn", "bad", "unknown")
NOW_WORDS = ("running", "queued", "hung", "held", "asked", "answered", "widened", "green")
"""The state words of a task or check that is still open work (awb.build.STATES); done, passed, red, stopped,
interrupted and stale are ended and stay out of Now."""
DISK_WARN, DISK_BAD = 20.0, 10.0      # percent free
LAB_SOON = 60                         # minutes: awb.winlab.WARN_MINUTES, the end is marked as soon
GATEWAY_MARKS = (b"awb/tcp/web/gateway.py", b"awb.tcp.web.gateway")
NOTICE_RE = re.compile(r"winlab-[a-z][a-z0-9-]{1,30}\.(%s)\.json" % codes.TCP_PROJECT_WIDE)   # awb.winlab.NOTICE_RE
NOTICE_MAX = 4096
STAMP_RE = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}")
END_RE = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z")
COMMIT_RE = re.compile(r"[0-9a-f]{40}")
SHORT_RE = re.compile(r"[0-9a-f]{7}")
JOURNAL_WORD = re.compile(r"[a-z]{1,20}")
TEXT_CUT = 240
STALE_HOURS = 24                      # awb queue list: stale means the next step is unchanged for 24 hours
LISTS_EVERY = 5 * 60
"""Seconds between two rebuilds of the dev asks and the queue in the status file of the minute timer."""


def _z(t: dt.datetime) -> str:
    return t.astimezone(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _utc_now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def _local_stamp(stamp: str) -> str | None:
    """A stamp of state.tsv (host local time, no zone, awb.build._STAMP) as UTC with Z; None for anything else."""
    if not isinstance(stamp, str) or not STAMP_RE.fullmatch(stamp):
        return None
    try:
        return _z(dt.datetime.strptime(stamp, "%Y-%m-%dT%H:%M:%S").astimezone())
    except ValueError:
        return None


def _short(commit) -> str | None:
    if isinstance(commit, str) and (COMMIT_RE.fullmatch(commit) or SHORT_RE.fullmatch(commit)):
        return commit[:7]
    return None


def _tile(tid: str, label: str, state: str, value: str, detail: str | None = None) -> dict:
    return {"id": tid, "label": label, "state": state if state in STATES else "unknown", "value": value,
            "detail": detail}


def _read_small(path: Path, cap: int) -> bytes | None:
    """A regular file, never through a link, under a cap; else None."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK)
    except OSError:
        return None
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            return None
        data = os.read(fd, cap + 1)
    except OSError:
        return None
    finally:
        os.close(fd)
    return data if len(data) <= cap else None


# --------------------------------------------------------------------------- the tiles (W1)


def vault_tile(ping: Callable[[], dict]) -> dict:
    try:
        answer = ping()
    except Exception:
        return _tile("vault", "Vault", "bad", "no answer", "the vault daemon does not answer")
    state = answer.get("state") if isinstance(answer, dict) else None
    if state == "unlocked":
        return _tile("vault", "Vault", "ok", "unlocked")
    if state == "plain":
        return _tile("vault", "Vault", "ok", "plain", "the register is not encrypted on this host")
    if state == "locked":
        since = answer.get("since")
        return _tile("vault", "Vault", "bad", "locked",
                     "since %s" % since if isinstance(since, str) and END_RE.fullmatch(since) else "since unknown")
    return _tile("vault", "Vault", "unknown", "unknown")


def keys_tile(ping: Callable[[], str]) -> dict:
    try:
        state = ping()
    except Exception:
        return _tile("keys", "Key service", "bad", "no answer", "the key service does not answer")
    if state == "unlocked":
        return _tile("keys", "Key service", "ok", "unlocked")
    if state == "locked":
        return _tile("keys", "Key service", "bad", "locked", "no tenant key is loaded")
    return _tile("keys", "Key service", "unknown", "unknown")


def runner_tile(build: dict | None, written: str | None) -> dict:
    """The build runner as state.tsv shows it: held, running or idle, with the time the runner last wrote it."""
    if build is None:
        return _tile("runner", "Build runner", "unknown", "no state yet", "no state.tsv of the runner")
    detail = "state of %s" % written if written else None
    if build.get("queue"):
        return _tile("runner", "Build runner", "bad", "held: %s" % build["queue"] if build["queue"] != "held"
                     else "held", detail)
    running = [x["id"] for x in build["tasks"] + build["checks"] if x["state"].split(": ")[0] == "running"]
    hung = [x["id"] for x in build["tasks"] + build["checks"] if x["state"].split(": ")[0] == "hung"]
    if hung:
        return _tile("runner", "Build runner", "bad", "hung: %s" % " ".join(hung), detail)
    if build.get("failed"):
        return _tile("runner", "Build runner", "warn", "unit failed: %s" % build["failed"][-1]["unit"], detail)
    if running:
        return _tile("runner", "Build runner", "ok", "running %s" % " ".join(running), detail)
    queued = sum(1 for x in build["tasks"] + build["checks"] if x["state"] == "queued")
    return _tile("runner", "Build runner", "ok", "idle" if not queued else "%d queued" % queued, detail)


def release_tile(live: str | None, journal: dict | None, build: dict | None) -> dict:
    if live is None:
        return _tile("release", "Live release", "unknown", "none", "no release under /opt/tcp-awb")
    value = _short(live) or ("plain" if live == "plain" else "unknown")
    state = journal.get("state") if isinstance(journal, dict) else None
    if isinstance(state, str) and JOURNAL_WORD.fullmatch(state) and state != "done":
        return _tile("release", "Live release", "warn", value, "deploy %s" % state)
    if build:
        nd = build.get("not_deployed")
        main = build.get("main") if SHORT_RE.fullmatch(str(build.get("main"))) else "unknown"
        if isinstance(nd, int) and nd > 0:
            return _tile("release", "Live release", "warn", value, "main %s, %d not deployed" % (main, nd))
        return _tile("release", "Live release", "ok", value, "main %s" % main)
    return _tile("release", "Live release", "ok", value)


def disk_tile(path: Path = DISK, statvfs: Callable = os.statvfs) -> dict:
    try:
        st = statvfs(str(path))
    except OSError:
        return _tile("disk", "Disk free", "unknown", "unknown")
    total = st.f_blocks * st.f_frsize
    free = st.f_bavail * st.f_frsize
    if total <= 0:
        return _tile("disk", "Disk free", "unknown", "unknown")
    pct = free * 100.0 / total
    state = "bad" if pct < DISK_BAD else "warn" if pct < DISK_WARN else "ok"
    return _tile("disk", "Disk free", state, "%.0f%%" % pct, "%.1f GB of %.0f GB" % (free / 1e9, total / 1e9))


def gateway_starts(proc: Path = PROC) -> list[int]:
    """The start of every process of the console gateway in microseconds since boot (/proc/PID/stat, field 22)."""
    out = []
    tick = os.sysconf("SC_CLK_TCK") if hasattr(os, "sysconf") else 100
    try:
        pids = [n for n in os.listdir(proc) if n.isdigit()]
    except OSError:
        return out
    for pid in pids:
        try:
            with open(proc / pid / "cmdline", "rb") as fh:
                cmd = fh.read(8192)
        except OSError:
            continue
        if not any(mark in cmd for mark in GATEWAY_MARKS):
            continue
        try:
            with open(proc / pid / "stat", "rb") as fh:
                line = fh.read(4096).decode("ascii", "replace")
            fields = line[line.rindex(")") + 2:].split()
            out.append(int(fields[19]) * 1000000 // tick)
        except (OSError, ValueError, IndexError):
            continue
    return out


def web_tile(journal: dict | None, live: str | None, proc: Path = PROC) -> dict:
    """awb-web on the live release: every gateway process started after the switch of the last deploy (the
    journal's `flip`, monotonic microseconds, on the same boot) or the deploy ran on an earlier boot."""
    if not isinstance(journal, dict) or not isinstance(live, str):
        return _tile("web", "awb-web", "unknown", "unknown", "no deploy record")
    starts = gateway_starts(proc)
    if not starts:
        return _tile("web", "awb-web", "unknown", "not running", "it starts on the next request")
    try:
        boot = (proc / "sys" / "kernel" / "random" / "boot_id").read_text().strip()
    except OSError:
        boot = ""
    flip = journal.get("flip")
    if journal.get("state") != "done" or isinstance(flip, bool) or not isinstance(flip, int):
        return _tile("web", "awb-web", "unknown", "deploy running")
    if not boot or journal.get("boot") != boot or all(s > flip for s in starts):
        return _tile("web", "awb-web", "ok", "live release", _short(live))
    return _tile("web", "awb-web", "warn", "older release",
                 "started before the switch to %s; the next deploy restarts it" % (_short(live) or "the live release"))


# --------------------------------------------------------------------------- Now (W2)


def build_items(build: dict | None) -> list[dict]:
    out = []
    for kind, rows in (("task", (build or {}).get("tasks", [])), ("check", (build or {}).get("checks", []))):
        for r in rows:
            word = r["state"].split(": ")[0]
            if word not in NOW_WORDS:
                continue
            out.append({"kind": kind, "id": r["id"], "state": r["state"], "start": _local_stamp(r.get("since", "")),
                        "end": None, "note": None})
    return out


def lab_items(folder: Path, now: dt.datetime) -> list[dict]:
    """The lab windows of the notices of awb winlab that have not ended: the project code and the end. A window
    closed before its end keeps its notice, so the item says `as opened`."""
    out = []
    try:
        names = sorted(n for n in os.listdir(folder) if NOTICE_RE.fullmatch(n))
    except OSError:
        return out
    for name in names:
        raw = _read_small(folder / name, NOTICE_MAX)
        try:
            data = json.loads(raw) if raw is not None else None
        except ValueError:
            continue
        code = NOTICE_RE.fullmatch(name).group(1)
        end = data.get("end") if isinstance(data, dict) else None
        if not isinstance(end, str) or not END_RE.fullmatch(end) or data.get("project") != code:
            continue
        at = dt.datetime.strptime(end, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=dt.timezone.utc)
        if at <= now:
            continue
        left = int((at - now).total_seconds() // 60)
        out.append({"kind": "lab", "id": code, "state": "ends soon" if left < LAB_SOON else "open", "start": None,
                    "end": end, "note": "window as opened"})
    return out


def deploy_items(journal: dict | None, build: dict | None, live: str | None) -> list[dict]:
    out = []
    state = journal.get("state") if isinstance(journal, dict) else None
    if isinstance(state, str) and JOURNAL_WORD.fullmatch(state) and state != "done":
        started = journal.get("started")
        out.append({"kind": "deploy", "id": _short(journal.get("target")) or "unknown", "state": state,
                    "start": started if isinstance(started, str) and END_RE.fullmatch(started) else None,
                    "end": None, "note": None})
    nd = (build or {}).get("not_deployed")
    if isinstance(nd, int) and nd > 0:
        out.append({"kind": "deploy", "id": _short((build or {}).get("main")) or "unknown", "state": "pending",
                    "start": None, "end": None,
                    "note": "%d commit%s on main, live %s" % (nd, "" if nd == 1 else "s", _short(live) or "unknown")})
    return out


def now_items(build, journal, live, notices: Path, now: dt.datetime) -> list[dict]:
    """Running first, then by start; the lab windows by end; the deploys last."""
    work = build_items(build)
    work.sort(key=lambda i: (i["state"] != "running", i["start"] or "9999", i["id"]))
    return work + sorted(lab_items(notices, now), key=lambda i: i["end"]) + deploy_items(journal, build, live)


# --------------------------------------------------------------------------- the answer of GET /api/health


def health(paths, *, opt: Path = OPT, proc: Path = PROC, disk: Path = DISK, now: dt.datetime | None = None,
           vault_ping: Callable[[], dict] | None = None, keys_ping: Callable[[], str] | None = None) -> dict:
    from awb import deploy
    from awb.tcp import board

    now = now or _utc_now()
    if vault_ping is None:
        from awb import vault

        def vault_ping():
            return vault.ping_state(paths.check_socket, timeout=3)
    if keys_ping is None:
        def keys_ping():
            from awb.tcp import keys
            return keys.request(keys.call_socket(), {"op": "ping"}, timeout=3)["state"]
    build = board.build_state(paths)
    try:
        written = _z(dt.datetime.fromtimestamp(os.stat(board.build_tsv(paths)).st_mtime, dt.timezone.utc))
    except OSError:
        written = None
    live = deploy.live_release(opt)
    journal = deploy.read_journal(opt / deploy.JOURNAL_NAME)
    tiles = [vault_tile(vault_ping), keys_tile(keys_ping), runner_tile(build, written),
             release_tile(live, journal, build), disk_tile(disk), web_tile(journal, live, proc)]
    return {"generated_at": _z(now), "health": tiles,
            "now": now_items(build, journal, live, paths.shared / "notices", now)}


_CACHE: dict = {}


def cached_health(paths, clock: Callable[[], float] = time.monotonic, **kw) -> dict:
    """health() kept CACHE_SECONDS: two pages and a refresh each minute never ping the daemons more often."""
    t = clock()
    hit = _CACHE.get("answer")
    if hit is not None and 0 <= t - hit[0] < CACHE_SECONDS:
        return hit[1]
    answer = health(paths, **kw)
    _CACHE["answer"] = (t, answer)
    return answer


# --------------------------------------------------------------------------- the dev asks and the queue (W3, W4)


def _line(text: str, cut: int) -> str:
    text = " ".join("".join(c if c.isprintable() else " " for c in text).split())
    return text if len(text) <= cut else text[:cut - 3].rstrip() + "..."


def screen_all(texts: list[str], screen) -> list[tuple[str | None, str | None]]:
    """(text, None) or (None, classes) for each text: one call for all of them, one call each only after a finding
    or when the check cannot run."""
    if not texts:
        return []
    joined = screen("\n".join(texts))
    if joined[1] is None:
        return [(t, None) for t in texts]
    if joined[1] == "unavailable":
        return [(None, "unavailable")] * len(texts)
    return [screen(t) for t in texts]


def ask_rows(p, screen) -> list[dict]:
    """The open asks of <shared>/notices/dev/ (awb dev ask): code, file, title after the screen, created, blocks;
    the oldest first."""
    from awb import devask

    found = []
    folder = p.shared / devask.FOLDER
    try:
        names = sorted(n for n in os.listdir(folder) if devask.FILE_RE.fullmatch(n))
    except OSError:
        return []
    for name in names:
        try:
            data = devask.read(folder / name)
            created = dt.datetime.fromisoformat(data["created"])
        except (devask.AskError, ValueError, TypeError):
            continue
        if data["state"] != "open":
            continue
        found.append((name, data, created))
    found.sort(key=lambda x: (x[2], x[0]))
    found = found[-200:]
    shown = screen_all([_line(d["title"], 120) for _, d, _ in found], screen)
    return [{"code": d["project"], "file": n, "title": title or None, "withheld": withheld, "created": _z(c),
             "blocks": d["blocks"]} for (n, d, c), (title, withheld) in zip(found, shown)]


def queue_rows(screen, path: Path | None = None, now: dt.datetime | None = None) -> dict:
    """The open rows of awb queue: id, who, project, what and next after the screen, created, next_changed, stale
    (the next step unchanged for STALE_HOURS); the oldest first. readable is False when the queue cannot be read."""
    from awb import ledger, workqueue

    now = now or _utc_now()
    try:
        rows = workqueue.load(path)
    except (workqueue.QueueError, ledger.LedgerError, OSError, UnicodeError):
        return {"readable": False, "rows": []}
    rows = sorted(rows, key=lambda r: (r["created"], r["id"]))[:200]
    texts = []
    for r in rows:
        texts += [_line(r["what"], TEXT_CUT), _line(r["next"], TEXT_CUT)]
    shown = screen_all(texts, screen)
    out = []
    for i, r in enumerate(rows):
        (what, w1), (nxt, w2) = shown[2 * i], shown[2 * i + 1]
        withheld = w1 or w2
        changed = dt.datetime.fromisoformat(r["next_changed"])
        out.append({"id": r["id"], "who": r["who"], "project": r["project"] or None,
                    "what": None if withheld else what, "next": None if withheld else nxt, "withheld": withheld,
                    "created": _z(dt.datetime.fromisoformat(r["created"])), "next_changed": _z(changed),
                    "stale": now - changed >= dt.timedelta(hours=STALE_HOURS)})
    return {"readable": True, "rows": out}


def withhold_lists(status: dict) -> dict:
    """The status with every text of the dev asks and the queue withheld as unavailable: the name check of the
    whole status could not run."""
    status = dict(status)
    if "lists_built" in status:
        status["lists_built"] = 0           # built again at the next write, as the decisions
    if "dev_asks" in status:
        status["dev_asks"] = [dict(r, title=None, withheld="unavailable") if r.get("title") else r
                              for r in status["dev_asks"]]
    if "queue" in status:
        q = status["queue"]
        status["queue"] = dict(q, rows=[dict(r, what=None, next=None, withheld="unavailable")
                                        if r.get("what") or r.get("next") else r for r in q["rows"]])
    return status
