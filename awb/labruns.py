"""The records of the lab runs (task LR), owner side: <vault>/lab-runs/<run>.json, mode 600.

The key service (awb/tcp/labadmin.py) writes them; `awb owner status` (awb/ownerwait.py) reads them for the runs
open for more than OLD_AFTER seconds and the runs that ended with failed deletes. A record holds the run id, the
project code, the tenant alias, a note, the state (open, ended), the start, the count of calls and every resource
the run made with the call that deletes it and its state (live, removed, failed). Standard library only; core code,
so the owner status needs no module of the platform package.
"""
from __future__ import annotations

import json
import os
import re
import secrets as _secrets
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

OLD_AFTER = 2 * 3600
RUN_RE = re.compile(r"^run-\d{8}-\d{6}-[a-z0-9]{4}$")


def runs_dir(p) -> Path:
    return Path(p.vault) / "lab-runs"


def save(p, run: dict) -> None:
    folder = runs_dir(p)
    folder.mkdir(parents=True, exist_ok=True)
    os.chmod(folder, 0o700)
    fd, tmp = tempfile.mkstemp(prefix=".run.", dir=folder)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(run, fh, indent=1, sort_keys=True)
        os.chmod(tmp, 0o600)
        os.replace(tmp, folder / ("%s.json" % run["id"]))
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise


def load_runs(p) -> list[dict]:
    out = []
    try:
        names = sorted(os.listdir(runs_dir(p)))
    except OSError:
        return []
    for name in names:
        if not name.endswith(".json") or not RUN_RE.match(name[:-5]):
            continue
        try:
            run = json.loads((runs_dir(p) / name).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if isinstance(run, dict) and run.get("id") == name[:-5]:
            out.append(run)
    return out


def open_run(p, project: str, tenant: str) -> dict | None:
    return next((r for r in load_runs(p) if r.get("project") == project and r.get("tenant") == tenant
                 and r.get("state") == "open"), None)


def leftovers(run: dict) -> list[dict]:
    return [c for c in run.get("created", []) if c.get("state") in ("live", "failed")]


def new_id(clock=time.time) -> str:
    stamp = datetime.fromtimestamp(clock(), timezone.utc).strftime("%Y%m%d-%H%M%S")
    return "run-%s-%s" % (stamp, "".join(_secrets.choice("abcdefghijkmnpqrstuvwxyz23456789") for _ in range(4)))


def attention(p, now: float | None = None) -> list[dict]:
    """The runs the owner hears of: open for more than OLD_AFTER seconds, or ended with failed deletes."""
    now = time.time() if now is None else now
    out = []
    for run in load_runs(p):
        left = leftovers(run)
        if run.get("state") == "open" and now - float(run.get("started_ts") or now) > OLD_AFTER:
            out.append(dict(run, why="open", hours=int((now - float(run["started_ts"])) // 3600), left=left))
        elif run.get("state") == "ended" and any(c.get("state") == "failed" for c in left):
            out.append(dict(run, why="failed", left=left))
    return out
