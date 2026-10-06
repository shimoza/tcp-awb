"""The recorded status of a project: the two lines STATE.md opens with, and how far it lags behind the work.

STATE.md opens with two lines under its title, one sentence each, codes only:

    Status: <where the work stands>
    Next: <the next step>

The session updates them, with the rest of STATE.md and OPEN.md, after every step that changes the status, in the
same commit as the work. The stop hook holds a reply while the project has commits newer than STATE.md or the two
lines are missing; the portal shows them in the project list and on the project page, with the date of STATE.md
and the commits that came after it.
"""
from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass
from pathlib import Path

STATE = "STATE.md"
HEAD_LINES = 40
"""The two lines are looked for in the first lines of STATE.md only."""
HISTORY = 500
"""The most commits read to count the ones newer than STATE.md."""
GRACE = 60
"""Seconds after STATE.md in which a commit still counts as the same step (the work and the status a moment apart)."""
_LINE_RE = {"summary": re.compile(r"^Status:[ \t]*(\S.*?)\s*$", re.M),
            "next": re.compile(r"^Next:[ \t]*(\S.*?)\s*$", re.M)}


@dataclass
class Recorded:
    summary: str | None        # the Status: line, None when STATE.md has none
    next: str | None           # the Next: line, None when STATE.md has none


def lines(text: str) -> Recorded:
    """The Status: and Next: lines of a STATE.md text."""
    head = "\n".join(text.splitlines()[:HEAD_LINES])
    found = {k: (m.group(1) if (m := r.search(head)) else None) for k, r in _LINE_RE.items()}
    return Recorded(found["summary"], found["next"])


def _git(root: Path, *args: str) -> str | None:
    try:
        r = subprocess.run(["git", "-c", "safe.directory=%s" % root, "-C", str(root), *args],
                           capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return None
    return r.stdout if r.returncode == 0 else None


def behind(root: Path) -> int | None:
    """Commits of the project newer than STATE.md by more than GRACE: newer than the last commit that touched it and
    than its last change on disk, so an update not yet committed counts. None when the history or the file cannot
    be read."""
    root = Path(root)
    try:
        on_disk = int((root / STATE).stat().st_mtime)
    except OSError:
        return None
    touched = _git(root, "log", "-1", "--format=%ct", "--", STATE)
    times = _git(root, "log", "-%d" % HISTORY, "--format=%ct")
    if touched is None or times is None:
        return None
    since = max(on_disk, int(touched.strip() or 0))
    return sum(1 for t in times.split() if t.isdigit() and int(t) > since + GRACE)


def reminder(root: Path) -> str | None:
    """What the stop hook tells a session whose STATE.md lags behind its commits or lacks the two lines; None when
    STATE.md is current or cannot be judged (no file, no readable git history: the reminder ends with a commit)."""
    root = Path(root)
    try:
        text = (root / STATE).read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        return None
    late = behind(root)
    if late is None:
        return None
    rec = lines(text)
    parts = []
    if late:
        parts.append("STATE.md and OPEN.md lag behind %d commit(s) of this project: update both with what was "
                     "done and what comes next" % late)
    if rec.summary is None or rec.next is None:
        parts.append("STATE.md needs the two lines Status: and Next: right under its title, one sentence each, "
                     "codes only: the portal shows them as the status of the project")
    if not parts:
        return None
    return "; ".join(parts) + "; then commit."
