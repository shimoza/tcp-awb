"""Notes from development (task DN, 2026-10-27): the development session tells a project session something
without him passing it on.

    awb projects note CODE --title "..." FILE|-

writes notes/from-dev/<YYYY-MM-DD>-<n>.md in the project folder (mode 640, owned by whoever writes it: run it as the
work user, `sudo -n -u awb -H awb projects note ...`, on a sealed host) and leaves a notice in <shared>/notices/
like the "new input" notice of `awb import` (awb/rulesync.py). The prompt hook of a session of that project shows
it once per session: "note from development: TITLE (notes/from-dev/FILE): read it first". The work rules of the
project (projects._claude) say a note is followed like his word on the Workbench and never overrides his own word
on the project. The note text and the title run through the secret check only: a note is written by the
development side, which carries no customer material.
"""
from __future__ import annotations

import json
import os
import re
import secrets
from datetime import date, datetime, timezone
from pathlib import Path

from awb import config

FOLDER = "notes/from-dev"
LINE = "note from development: %s (%s/%s): read it first"
MAX_TITLE = 120
MAX_TEXT = 64 * 1024
_NOTE_RE = re.compile(r"note-[0-9TZ]+-[0-9a-f]{8}\.json")
_FILE_RE = re.compile(r"\d{4}-\d{2}-\d{2}-\d{1,3}\.md")


RESOURCES_LINE = "- RESOURCES.md lists what the project uses, with platform ids and codes."
FILE_RULE = ("- Never link a file on the host, in the work user's home or in the bucket: he cannot open it. Say in one "
             "line where it is and how to get it (bucket awb, folder <YYYY-MM>/tcp-xxxx/out/: Total Commander, or "
             "awb clean fetch on the laptop). A deliverable you put goes out as HTML and md. Text he asks to read in "
             "the chat: plain paragraphs, no tables or bullets.")
NOTE_RULE = ("- A note from development (notes/from-dev/) is read first and followed like his word on the Workbench; "
             "it never overrides his own word on the project.")
CLAUDE_ADD = ((RESOURCES_LINE, FILE_RULE, "he cannot open it"), (FILE_RULE, NOTE_RULE, "notes/from-dev/"))
"""The two lines of the project CLAUDE.md (his rules of 2026-10-27), after the RESOURCES.md line; `awb projects
sync` adds them once to an older project (rulesync.CLAUDE_ADD)."""


class NoteError(Exception):
    """A note cannot be written. The message names the reason, never the text."""


def _secret_check(text: str, what: str) -> None:
    from awb import check

    found = check._secret_classes(text)
    if found:
        raise NoteError("the %s carries a secret (%s); nothing was written" % (what, ", ".join(sorted(found))))


def clean_title(title) -> str:
    title = " ".join(str(title or "").split())
    if not title:
        raise NoteError("give the note a --title")
    if len(title) > MAX_TITLE:
        raise NoteError("the title is longer than %d characters" % MAX_TITLE)
    if any(ord(ch) < 32 or ord(ch) == 127 for ch in title):
        raise NoteError("the title carries a control character")
    return title


def _project(p: config.Paths, code: str):
    from awb import projects

    for pr in projects.load(p):
        if pr.code == code:
            if pr.state not in projects.LIVE_STATES:
                raise NoteError("%s is %s: a note goes to an active or parked project" % (code, pr.state))
            return pr
    raise NoteError("%s is not a registered project" % code)


def write(p: config.Paths, code: str, title: str, text: str, today: date | None = None) -> Path:
    """Write the note into the project and leave its notice. Returns the path of the note."""
    title = clean_title(title)
    if not text.strip():
        raise NoteError("the note is empty")
    if len(text.encode("utf-8")) > MAX_TEXT:
        raise NoteError("the note is larger than %d bytes" % MAX_TEXT)
    _secret_check(title, "title")
    _secret_check(text, "note")
    pr = _project(p, code)
    root = Path(pr.path)
    folder = root / FOLDER
    if root.is_symlink() or any(x.is_symlink() for x in (root / "notes", folder) if x.exists()):
        raise NoteError("the notes folder of %s is a link" % code)
    try:
        folder.mkdir(parents=True, exist_ok=True)
    except PermissionError:
        raise NoteError("cannot write into %s: run it as the work user (sudo -n -u awb -H awb projects note ...)"
                        % code) from None
    day = (today or date.today()).isoformat()
    body = "# %s\n\n%s\n" % (title, text.strip("\n"))
    for n in range(1, 1000):
        path = folder / ("%s-%d.md" % (day, n))
        try:
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o640)
        except FileExistsError:
            continue
        except PermissionError:
            raise NoteError("cannot write into %s: run it as the work user (sudo -n -u awb -H awb projects note "
                            "...)" % code) from None
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            os.fchmod(fd, 0o640)
            fh.write(body)
        break
    else:
        raise NoteError("%s has a thousand notes of today already" % code)
    post(p, code, path.name, title)
    return path


def post(p: config.Paths, code: str, name: str, title: str) -> Path:
    """The notice of one note for the sessions of the project: its code, the file name and the title."""
    from awb import codes, rulesync

    if not codes.is_project_code(code) or not _FILE_RE.fullmatch(name):
        raise NoteError("a note notice carries a project code and a note file name")
    folder = config.make_dir(rulesync.notices_dir(p), 0o750, shared=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    path = folder / ("note-%s-%s.json" % (stamp, secrets.token_hex(4)))
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o640)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        os.fchmod(fd, 0o640)
        fh.write(json.dumps({"project": code, "file": name, "title": clean_title(title)}) + "\n")
    return path


def notices(p: config.Paths, session_id: str | None, root: Path | None) -> list[str]:
    """The lines of the notes of this session's project it has not been told yet, each once per session, while
    the note is still in the project."""
    from awb import rulesync

    if not session_id or root is None:
        return []
    code = Path(root).name
    try:
        names = sorted(f.name for f in rulesync.notices_dir(p).iterdir() if _NOTE_RE.fullmatch(f.name))
    except OSError:
        return []
    mark_path = rulesync._mark(p, session_id)
    mark = rulesync._read_mark(mark_path)
    seen = [x for x in (mark.get("notes") or []) if isinstance(x, str)]
    out: list[str] = []
    for name in names:
        if name in seen:
            continue
        f = rulesync.notices_dir(p) / name
        if f.is_symlink():
            continue
        data = rulesync._read_mark(f)
        file, title = data.get("file"), data.get("title")
        if data.get("project") != code or not isinstance(file, str) or not _FILE_RE.fullmatch(file):
            continue
        if not isinstance(title, str) or not (Path(root) / FOLDER / file).is_file():
            continue
        out.append(LINE % (" ".join(title.split())[:MAX_TITLE], FOLDER, file))
        seen.append(name)
    if out:
        mark["notes"] = seen[-200:]
        mark.setdefault("digest", rulesync.digest(root))
        rulesync._write_mark(mark_path, mark)
    return out
