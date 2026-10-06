"""Harvest: what a session learned live on the platform goes into the knowledge base, without anyone asking.

The stop hook calls `due()` with the session's transcript. When the session worked live on the platform since the
last harvest check (`awb cloud call`, `awb cloud get`, `awb tenant snapshot`, a Terraform or OpenTofu apply, plan,
destroy, import or refresh, the openstack command line, or curl, wget or http to a host under .otc.t-systems.com)
and ran no `awb kb add`, `amend` or `recheck` after those calls, the hook stops the session once with `MESSAGE`: the session adds its live findings itself and
then stops. The position up to which a transcript was checked is kept per session in
`<shared>/sessions/harvest-<session id>.json`, so the same calls never ask twice and a session that has nothing new
is asked once per batch of calls, never in a loop.

Only commands are read from the transcript: the name of the tool and the text of a shell command. Nothing of what
the session wrote or received is kept; the state file holds a line count.
"""
from __future__ import annotations

import json
import os
import re
from pathlib import Path

from awb import config

LIVE_RE = re.compile(r"\bawb\s+(?:cloud\s+(?:call|get|projects|sweep)|tenant\s+snapshot)\b"
                     r"|\b(?:terraform|tofu)\s+(?:-chdir=\S+\s+)?(?:apply|plan|destroy|import|refresh)\b"
                     r"|\bopenstack\s+[a-z]"
                     r"|\b(?:curl|wget|http)\b[^\n|;&]*\.otc\.t-systems\.com")
"""Live work on the platform: the Workbench's own calls, Terraform runs, the openstack client and plain HTTP to
the API hosts of TCP. A Terraform init, fmt or validate touches no cloud and does not count."""
KB_RE = re.compile(r"\bawb\s+kb\s+(?:add|amend|recheck)\b")
_SID_RE = re.compile(r"[^A-Za-z0-9_-]")
MAX_TRANSCRIPT = 64 * 1024 * 1024

MESSAGE = (
    "Harvest before you stop: this session worked live on TCP, through its API or Terraform (%d time(s) since the "
    "last check), and added nothing "
    "to the knowledge base after its last call. Add every live finding yourself, without asking him: a call that behaves unlike the "
    "documentation, a limit, an error and what fixed it, a property a resource needs, an order of steps that "
    "matters. Use awb kb add --grade live --class api (availability for what can change within weeks) --source "
    "\"live call on <tenant alias>, project <project code>, <date>\". One fact per entry, codes only. When "
    "nothing is new, end with one line that says so."
)


def commands(transcript: Path, start: int = 0) -> tuple[list[str], int]:
    """The shell commands of the session from line `start` of its transcript, and the number of lines read."""
    out: list[str] = []
    n = 0
    try:
        if os.path.getsize(transcript) > MAX_TRANSCRIPT:
            return [], start
        with open(transcript, "r", encoding="utf-8", errors="replace") as fh:
            for n, line in enumerate(fh, start=1):
                if n <= start:
                    continue
                try:
                    obj = json.loads(line)
                except ValueError:
                    continue
                if not isinstance(obj, dict) or obj.get("type") != "assistant":
                    continue
                content = (obj.get("message") or {}).get("content")
                for c in content if isinstance(content, list) else []:
                    if isinstance(c, dict) and c.get("type") == "tool_use":
                        cmd = (c.get("input") or {}).get("command")
                        if isinstance(cmd, str):
                            out.append(cmd)
    except OSError:
        return [], start
    return out, max(n, start)


def _state_path(p: config.Paths, session_id: str) -> Path:
    sid = _SID_RE.sub("", session_id or "")[:80] or "unknown"
    return p.shared / "sessions" / ("harvest-%s.json" % sid)


def _read_mark(path: Path) -> int:
    try:
        return int(json.loads(path.read_text(encoding="utf-8")).get("line", 0))
    except (OSError, ValueError, AttributeError, TypeError):
        return 0


def _write_mark(path: Path, line: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.is_symlink():
        return
    tmp = path.with_name(".%s.tmp" % path.name)
    tmp.write_text(json.dumps({"line": line}) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def due(p: config.Paths, transcript: str | os.PathLike | None, session_id: str | None) -> str | None:
    """The harvest message when the session made live calls since the last check and added no knowledge entry
    after them; None otherwise. Moves the mark either way, so the same calls are never asked about twice."""
    if not transcript:
        return None
    path = _state_path(p, session_id or "")
    mark = _read_mark(path)
    cmds, end = commands(Path(transcript), mark)
    if end < mark:            # a new transcript under the same session id: start again
        cmds, end = commands(Path(transcript), 0)
    last_live = max((i for i, c in enumerate(cmds) if LIVE_RE.search(c)), default=-1)
    live = sum(1 for c in cmds if LIVE_RE.search(c))
    harvested = any(KB_RE.search(c) for c in cmds[last_live + 1:]) if last_live >= 0 else False
    try:
        _write_mark(path, end)
    except OSError:
        pass
    if live and not harvested:
        return MESSAGE % live
    return None
