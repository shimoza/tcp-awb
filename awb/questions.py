"""The questions count (T13): how many replies of a session end with a question to him.

    <shared>/english/questions.tsv   one row per day and project: date, project, replies, questions
    awb report --by questions        the monthly counts, on either side

The stop hook reads the last reply of the session transcript (the English capture already opens it there) and
adds one reply to the row of the day and the project, and one question when the reply ends with "?". A question
inside a code block or in the English note does not count. Only counts are written, never a word of the reply.
"""
from __future__ import annotations

import datetime
import fcntl
import os
import re
from pathlib import Path

from awb import config, english

COLUMNS = ("date", "project", "replies", "questions")
NONE = "none"
_PROJECT_RE = re.compile(r"tcp-[a-z0-9]{4}")
_FENCE_RE = re.compile(r"(?ms)^\s*(```|~~~).*?(^\s*\1[^\n]*$|\Z)")
_NOTE_RE = re.compile(r"(?is)(?:^|\n)\s*[*_]*English note:?[*_]*:?.*$")
_TRAIL = " \t\r\n*_`\"')]}>»“”’"


def counts_file(p: config.Paths) -> Path:
    return p.shared / "english" / "questions.tsv"


def ends_with_question(reply: str) -> bool:
    """True when the reply, without its code blocks and its English note, ends with "?"."""
    text = _FENCE_RE.sub("", reply or "")
    text = _NOTE_RE.sub("", text)
    return text.rstrip(_TRAIL).endswith(("?", "？"))


def project_of(root: Path | None) -> str:
    """The project code of a session folder, or "none": never a path or a folder name of another shape."""
    name = root.name if root is not None else ""
    return name if _PROJECT_RE.fullmatch(name) else NONE


def _read(text: str) -> dict[tuple[str, str], list[int]]:
    rows: dict[tuple[str, str], list[int]] = {}
    for line in text.splitlines():
        cells = line.split("\t")
        if len(cells) != 4 or cells[0] == COLUMNS[0]:
            continue
        try:
            rows[(cells[0], cells[1])] = [int(cells[2]), int(cells[3])]
        except ValueError:
            continue
    return rows


def add(p: config.Paths, project: str, question: bool, day: datetime.date | None = None) -> None:
    """Count one reply (and one question when `question`) for the day and the project, under a file lock."""
    day = day or datetime.date.today()
    project = project if _PROJECT_RE.fullmatch(project or "") else NONE
    path = counts_file(p)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o640)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        data = b""
        while chunk := os.read(fd, 1 << 16):
            data += chunk
        rows = _read(data.decode("utf-8", "replace"))
        row = rows.setdefault((day.isoformat(), project), [0, 0])
        row[0] += 1
        row[1] += 1 if question else 0
        out = "\t".join(COLUMNS) + "\n" + "".join(
            "%s\t%s\t%d\t%d\n" % (d, pr, r, q) for (d, pr), (r, q) in sorted(rows.items()))
        os.lseek(fd, 0, os.SEEK_SET)
        os.ftruncate(fd, 0)
        view = memoryview(out.encode("utf-8"))
        while view:
            view = view[os.write(fd, view):]
    finally:
        os.close(fd)


def capture(p: config.Paths, transcript: Path | str | None, root: Path | None) -> bool:
    """The stop hook: count the last reply of the transcript. True when a reply was counted."""
    if not transcript:
        return False
    reply = english.last_reply(Path(transcript))
    if not reply.strip():
        return False
    add(p, project_of(root), ends_with_question(reply))
    return True


def monthly(p: config.Paths) -> list[tuple[str, int, int]]:
    """(month, replies, questions) per month, oldest first."""
    try:
        rows = _read(counts_file(p).read_text(encoding="utf-8"))
    except OSError:
        return []
    months: dict[str, list[int]] = {}
    for (day, _project), (r, q) in rows.items():
        m = months.setdefault(day[:7], [0, 0])
        m[0] += r
        m[1] += q
    return [(m, r, q) for m, (r, q) in sorted(months.items())]


def render(p: config.Paths) -> str:
    rows = monthly(p)
    if not rows:
        return "# Questions per month\n\nNo replies counted yet.\n"
    out = ["# Questions per month", "", "| Month | Replies | With a question | Share |", "|---|---|---|---|"]
    for m, r, q in rows:
        out.append("| %s | %d | %d | %d%% |" % (m, r, q, round(100 * q / r) if r else 0))
    return "\n".join(out) + "\n"
