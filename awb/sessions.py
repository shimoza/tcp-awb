"""One owner per project (T-62): the session that works in a project holds a claim with a heartbeat.

    <shared>/sessions/<project code>.json   {"session": ..., "since": ..., "heartbeat": ...}

The session-start hook claims the project for its session. The prompt hook moves the heartbeat. A claim whose
heartbeat is older than LIVE_MINUTES is stale and the next session takes it over. A second live session in the same
project is told at its start who holds it, because two sessions in one project overwrite each other's STATE.md.
A claim carries a session id and times only, never a goal or a name.
"""
from __future__ import annotations

import datetime
import json
import os
from pathlib import Path

from awb import codes, config

LIVE_MINUTES = 30


def _now(now: datetime.datetime | None) -> datetime.datetime:
    return now or datetime.datetime.now(datetime.timezone.utc)


def _file(p: config.Paths, code: str) -> Path:
    if not codes.is_project_code(code):
        raise ValueError("not a project code")
    return p.shared / "sessions" / (code + ".json")


def _read(path: Path) -> dict | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) and isinstance(data.get("session"), str) else None


def _write(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name("." + path.name + ".tmp")
    tmp.write_text(json.dumps(data) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def _live(claim: dict, now: datetime.datetime) -> bool:
    try:
        beat = datetime.datetime.fromisoformat(claim["heartbeat"])
    except (KeyError, TypeError, ValueError):
        return False
    return now - beat < datetime.timedelta(minutes=LIVE_MINUTES)


def owner(p: config.Paths, code: str, now: datetime.datetime | None = None) -> dict | None:
    """The live claim of a project, or None."""
    claim = _read(_file(p, code))
    return claim if claim and _live(claim, _now(now)) else None


def claim(p: config.Paths, code: str, session: str, now: datetime.datetime | None = None) -> dict | None:
    """Claim `code` for `session`. Returns the claim of another live session, which keeps the project; None when
    the claim is this session's now."""
    if not isinstance(session, str) or not session.strip():
        raise ValueError("a session id is needed")
    now = _now(now)
    path = _file(p, code)
    held = _read(path)
    if held and held["session"] != session and _live(held, now):
        return held
    since = held["since"] if held and held["session"] == session else now.isoformat()
    _write(path, {"session": session, "since": since, "heartbeat": now.isoformat()})
    return None


def beat(p: config.Paths, code: str, session: str, now: datetime.datetime | None = None) -> bool:
    """Move the heartbeat when the claim is this session's. True when it moved."""
    path = _file(p, code)
    held = _read(path)
    if not held or held["session"] != session:
        return False
    held["heartbeat"] = _now(now).isoformat()
    _write(path, held)
    return True


def short(session: str) -> str:
    return session[:8]
