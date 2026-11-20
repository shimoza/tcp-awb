"""The recorded status of a project: the two lines STATE.md opens with, and how far it lags behind the work.

STATE.md opens with two lines under its title, one sentence each, codes only:

    Status: <where the work stands>
    Next: <the next step>

The session updates them, with the rest of STATE.md and OPEN.md, after every step that changes the status, in the
same commit as the work. The stop hook holds a reply while the project has commits newer than STATE.md or the two
lines are missing; the console shows them in the project list and on the project page, with the date of STATE.md
and the commits that came after it.

Under the two lines STATE.md may keep a pinned block (Q-0006), a `## Pinned` heading with one line per standing fact:

    - Language: <the language of the replies>
    - Limit: <a standing limit the owner set>
    - Deliverable: <a deliverable and its bucket path>
    - Chain: <a running chain>

A compaction used to lose these facts and the session asked him again. The PreCompact hook hands the two lines and
the block to the summary and the start after a compaction prints them first (awb/hooks.py). The stop hook holds a
reply while the block lost its shape: more than one block, below the first HEAD_LINES lines, over PIN_MAX_LINES
lines or PIN_MAX_CHARS characters or an entry of another kind.
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
LINE_MAX = 300
"""The most characters of the Status: line and of the Next: line, each."""
PIN_HEADING = "## Pinned"
"""The heading of the pinned block of STATE.md (Q-0006): the standing facts of the project that a compaction must
not lose. The PreCompact hook hands them to the summary and the start after a compaction prints them first."""
PIN_KINDS = ("Language", "Limit", "Deliverable", "Chain")
"""What a line of the pinned block may be: the reply language, a standing limit the owner set, a deliverable with
its bucket path and a running chain. Each entry is a line `- Kind: text`."""
PIN_MAX_LINES = 12
PIN_MAX_CHARS = 1500
"""The pinned block stays short: at most this many lines and characters below its heading."""
_PIN_ENTRY_RE = re.compile(r"^- (%s):[ \t]*\S" % "|".join(PIN_KINDS))
_SENTENCE_RE = re.compile(r"[^.!?]+(?:[.!?]+|$)")
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


def shape(summary: str | None, next: str | None) -> str | None:
    """None when the two lines are short and say nothing twice; else what is wrong, with the counts. A missing line
    is not judged here (the reminder names it)."""
    problems = []
    for label, text in (("Status:", summary), ("Next:", next)):
        if text is not None and len(text) > LINE_MAX:
            problems.append("%s has %d characters, at most %d" % (label, len(text), LINE_MAX))
    seen, again = set(), 0
    for text in (summary, next):
        for m in _SENTENCE_RE.finditer(text or ""):
            key = " ".join(m.group().lower().split()).rstrip(".!? ")
            if not key:
                continue
            if key in seen:
                again += 1
            seen.add(key)
    if again:
        problems.append("%d sentence(s) occur twice in the two lines" % again)
    return "; ".join(problems) or None


def _pin_starts(text: str) -> list[int]:
    return [n for n, line in enumerate(text.splitlines()) if line.rstrip() == PIN_HEADING]


def pinned(text: str) -> list[str] | None:
    """The lines of the pinned block of a STATE.md text (blank lines left out), from its heading to the next
    heading; None when STATE.md has no block."""
    starts = _pin_starts(text)
    if not starts:
        return None
    out = []
    for line in text.splitlines()[starts[0] + 1:]:
        if line.startswith("#"):
            break
        if line.strip():
            out.append(line.rstrip())
    return out


def pin_shape(text: str) -> str | None:
    """None when STATE.md has no pinned block or the block keeps its shape: one block, within the first HEAD_LINES
    lines, at most PIN_MAX_LINES lines and PIN_MAX_CHARS characters, every entry `- Kind: text` of PIN_KINDS (a
    plain sentence without the dash may say what the block is for). Else what is wrong, with the counts."""
    starts = _pin_starts(text)
    if not starts:
        return None
    problems = []
    if len(starts) > 1:
        problems.append("STATE.md has %d %s blocks, keep one" % (len(starts), PIN_HEADING))
    if starts[0] >= HEAD_LINES:
        problems.append("the %s block starts at line %d: keep it within the first %d lines, under Status: and Next:"
                        % (PIN_HEADING, starts[0] + 1, HEAD_LINES))
    body = pinned(text) or []
    chars = sum(len(line) for line in body)
    if len(body) > PIN_MAX_LINES or chars > PIN_MAX_CHARS:
        problems.append("the %s block has %d lines and %d characters, at most %d and %d: keep only the standing "
                        "facts and move the rest below" % (PIN_HEADING, len(body), chars, PIN_MAX_LINES,
                                                           PIN_MAX_CHARS))
    odd = sum(1 for line in body if line.startswith("-") and not _PIN_ENTRY_RE.match(line))
    if odd:
        problems.append("%d line(s) of the %s block are no `- Kind: text` entry of %s" % (
            odd, PIN_HEADING, ", ".join(k + ":" for k in PIN_KINDS)))
    return "; ".join(problems) or None


def pin_text(text: str) -> str | None:
    """The two status lines and the pinned block of a STATE.md text as one short block for the compaction and the
    start after it, cut to the size limits; None when STATE.md has neither."""
    rec = lines(text)
    body = pinned(text) or []
    out = []
    if rec.summary is not None:
        out.append("Status: " + cut(rec.summary))
    if rec.next is not None:
        out.append("Next: " + cut(rec.next))
    kept, used = [], 0
    for line in body[:PIN_MAX_LINES]:
        if used + len(line) > PIN_MAX_CHARS:
            break
        kept.append(line)
        used += len(line)
    if kept:
        out += ["", PIN_HEADING] + kept
    return "\n".join(out) if out else None


def cut(text: str, limit: int = LINE_MAX) -> str:
    """A long line cut at limit characters with ..., a short one as it is."""
    return text if len(text) <= limit else text[:limit].rstrip() + "..."


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
                     "codes only: the console shows them as the status of the project")
    bad = shape(rec.summary, rec.next)
    if bad:
        parts.append("the two lines of STATE.md are too long or repeat themselves (%s): move the detail below the "
                     "two lines" % bad)
    pin = pin_shape(text)
    if pin:
        parts.append("the pinned block of STATE.md lost its shape (%s): it carries only the reply language, the "
                     "standing limits, the deliverables with their bucket paths and the running chains" % pin)
    if not parts:
        return None
    return "; ".join(parts) + "; then commit."
