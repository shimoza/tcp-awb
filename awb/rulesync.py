"""Rules that change reach every project and every running session.

Two gaps closed here (2026-10-06: a session started on 2026-09-30 still looked for a file in the vault inbox, because
its project's CLAUDE.md and the rules it had loaded at its start predated the bucket inboxes):

- `awb projects sync [CODE...]` brings the template lines of each project's own CLAUDE.md and STATE.md up to the
  current ones: a known old line is replaced by its new form and a line the template gained is added after its
  anchor. Every other line of the project, its own notes included, stays as it is. Nothing is committed: the
  session commits with its next step.
- The session-start hook remembers a digest of the rules a session loaded (the work rules of the seal and the
  project's CLAUDE.md); the prompt hook compares it with the files of now and, when they changed or the session
  started before this check existed, tells the session once to read them again.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
from pathlib import Path

from awb import config

CLAUDE_FIXES = (
    ("- Start with SCOPE.md, STATE.md and OPEN.md. Update STATE.md and OPEN.md before the session ends.",
     "- Start with SCOPE.md, STATE.md and OPEN.md. Update STATE.md (with its Status: and Next: lines) and OPEN.md "
     "after every step that changes the status, in the same commit."),
    ("- Customer material comes in only through `awb intake` and moves from the outbox into input/.",
     "- Files from him come through the bucket inboxes: when he says a file is in the inbox, run `awb inbox take "
     "<his words>`, never look in the vault. Copies of his own `awb intake` wait in the outbox: move them into "
     "input/."),
)
CLAUDE_ADD = (
    ("- Proof goes to evidence/, results to deliverables/, review records to reviews/.",
     "- Files for him leave with `awb xchg put FILE` (the lab bucket, from-session/<date>/), after the name check "
     "and, for a customer project, the review.",
     "awb xchg put"),
)
STATE_FIXES = (
    ("Where the work stands. Update it at the end of every session. Codes only, never a name.",
     "Where the work stands. Update it and OPEN.md after every step that changes the status, in the same commit. "
     "Codes only, never a name."),
)
NOTICE = ("The Workbench rules or the CLAUDE.md of this project changed since this session started (or the session "
          "is older than this check). Read ~/.claude/CLAUDE.md and the CLAUDE.md of this project again now and work "
          "by them: they replace what you loaded at the start. In short: \"the inbox\" is the bucket inboxes of "
          "`awb inbox take`, files for him leave with `awb xchg put`, and STATE.md keeps its Status: and Next: lines "
          "current after every step.")
_SID_RE = re.compile(r"[^A-Za-z0-9_-]")


# --------------------------------------------------------------------------- the files of a project


def _fix_lines(text: str, fixes, adds=()) -> str:
    lines = text.split("\n")
    for old, new in fixes:
        lines = [new if line.rstrip() == old else line for line in lines]
    for anchor, new, marker in adds:
        if any(marker in line for line in lines):
            continue
        at = next((i for i, line in enumerate(lines) if line.rstrip() == anchor), None)
        if at is not None:
            lines.insert(at + 1, new)
    return "\n".join(lines)


def sync_project(root: Path) -> list[str]:
    """Bring the template lines of one project up to date. Returns the names of the files it changed."""
    changed = []
    for name, fixes, adds in (("CLAUDE.md", CLAUDE_FIXES, CLAUDE_ADD), ("STATE.md", STATE_FIXES, ())):
        path = Path(root) / name
        try:
            if path.is_symlink() or not path.is_file():
                continue
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeError):
            continue
        new = _fix_lines(text, fixes, adds)
        if new != text:
            with open(path, "r+", encoding="utf-8") as fh:     # the same file: its owner and mode stay
                fh.write(new)
                fh.truncate()
            changed.append(name)
    return changed


def sync(p: config.Paths, codes: list[str] | None = None) -> list[tuple[str, list[str]]]:
    """Every active project, or the ones named: (code, files changed)."""
    from awb import projects

    out = []
    for r in projects.load(p):
        if r.state != "active" or (codes and r.code not in codes):
            continue
        out.append((r.code, sync_project(Path(r.path))))
    return out


# --------------------------------------------------------------------------- what a running session loaded


def _rules_files(root: Path | None) -> list[Path]:
    from awb import projects

    files = [Path.home() / ".claude" / "CLAUDE.md", Path(projects.RULES_FILE)]
    if root is not None:
        files.append(Path(root) / "CLAUDE.md")
    return files


def digest(root: Path | None) -> str:
    h = hashlib.sha256()
    for f in _rules_files(root):
        try:
            data = f.read_bytes()
        except OSError:
            data = b""
        h.update(str(f).encode() + b"\0" + data + b"\0")
    return h.hexdigest()


def _mark(p: config.Paths, session_id: str) -> Path:
    sid = _SID_RE.sub("", session_id or "")[:80] or "unknown"
    return p.shared / "sessions" / ("rules-%s.json" % sid)


def remember(p: config.Paths, session_id: str | None, root: Path | None) -> None:
    """The session-start hook: what this session loaded."""
    if not session_id:
        return
    path = _mark(p, session_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.is_symlink():
        return
    tmp = path.with_name(".%s.tmp" % path.name)
    tmp.write_text(json.dumps({"digest": digest(root)}) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def notice(p: config.Paths, session_id: str | None, root: Path | None) -> str | None:
    """The prompt hook: NOTICE once when the rules changed since the session loaded them, or when the session is
    older than this check; None otherwise and for a prompt without a session id."""
    if not session_id:
        return None
    path = _mark(p, session_id)
    try:
        seen = json.loads(path.read_text(encoding="utf-8")).get("digest")
    except (OSError, ValueError, AttributeError):
        seen = None
    now = digest(root)
    if seen == now:
        return None
    remember(p, session_id, root)
    return NOTICE
