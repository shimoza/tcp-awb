"""A lab log that grows over LIMIT leaves git (Q-0021): its full text moves to `full/` beside it, which the project's
.gitignore keeps out of git (git keeps a summary instead).

A lab run log (`notes/lab-runs/<run>.log`, awb/labclient.py) and a Windows lab log (`notes/winlab/...`,
awb/winlabsession.py) are written through `append`. While a log stays under LIMIT it is appended in place and
committed as before (the gate gives a lab run log the secret check only, gate.OWN_OUTPUT). The first append that
takes it over LIMIT moves the file into `<folder>/full/` (a rename: a descriptor still open on it, such as the one
the key service holds for an OBS job, keeps writing into the same file), adds `<folder>/full/` to the project's
.gitignore and writes `<name>.summary.md` beside where the log was: start, end, calls, errors, created resources,
the path of the full log and its SHA-256. Every later append goes to the full log; the summary is written again
when it is older than REFRESH_SECONDS and by `refresh` (the end of a lab run).

A log of 105 MB took the commit gate more than 25 minutes (tcp-bytg, 2026-11-19); the gate now refuses any text
file over gate.TEXT_LIMIT at once and names this rule. The upload of the full log to the project bucket is the
owner's step (his cloud key) and not done here.
"""
from __future__ import annotations

import fcntl
import hashlib
import os
import re
import secrets
import stat
import time
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath

LIMIT = 2 * 1024 * 1024
"""The size in bytes over which a lab log leaves git."""
FULL = "full"
"""The git-ignored folder beside a log that holds the full text of a large log."""
SUMMARY_SUFFIX = ".summary.md"
REFRESH_SECONDS = 300
"""The age after which an append to a full log writes its summary again."""
MAX_LISTED = 100
"""The most created resources the summary lists by label; the count is always whole."""
IGNORE_NOTE = "# written by awb: the full text of a large lab log stays out of git, its summary is committed"
_CHUNK = 1 << 20
_STAMP = r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z"
_ENTRY_RE = re.compile(rb"^## (" + _STAMP.encode() + rb") (\S+)")
_OPENED_RE = re.compile(rb"^# lab run \S+ of \S+ on \S+, opened (\S+)")
_METHODS = frozenset((b"GET", b"POST", b"PUT", b"PATCH", b"DELETE", b"HEAD", b"OPTIONS", b"obs"))
_ANSWER_RE = re.compile(rb"^answer: (\d{3})\b")
_ERROR_RE = re.compile(rb"^(?:refused|offline|failed|error)\b|^delete .*: (?:[45]\d\d) \(")
_RECORDED_RE = re.compile(rb"^recorded: (.+)$")
_END_RE = re.compile(rb"^## " + _STAMP.encode() + rb" end of run ")


class LabLogError(OSError):
    """A log place that is no regular file of one link or a folder that is a link."""


def _open(parent: int, name: str, flags: int) -> int:
    fd = os.open(name, flags | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC, 0o600, dir_fd=parent)
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode) or st.st_nlink != 1:
            raise LabLogError("refused: expected a regular file with one link")
        return fd
    except BaseException:
        os.close(fd)
        raise


def _subdir(parent: int, create: bool = False) -> int | None:
    if create:
        try:
            os.mkdir(FULL, 0o700, dir_fd=parent)
        except FileExistsError:
            pass
    try:
        return os.open(FULL, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=parent)
    except FileNotFoundError:
        return None
    except NotADirectoryError:
        raise LabLogError("refused: the full log folder is no folder") from None


def _exists(parent: int, name: str) -> bool:
    try:
        os.stat(name, dir_fd=parent, follow_symlinks=False)
        return True
    except FileNotFoundError:
        return False


def summary_name(name: str) -> str:
    return (name[:-4] if name.endswith(".log") else name) + SUMMARY_SUFFIX


def _write_locked(fd: int, data: bytes) -> None:
    fcntl.flock(fd, fcntl.LOCK_EX)
    try:
        view = memoryview(data)
        while view:
            view = view[os.write(fd, view):]
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)


def _merge(parent: int, full: int, name: str) -> None:
    """A log written beside its full log after the move (a writer that looked before the move): its text goes to
    the end of the full log and the file goes."""
    try:
        fd = _open(parent, name, os.O_RDWR)
    except FileNotFoundError:
        return
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        if os.fstat(fd).st_nlink == 0:
            return
        os.lseek(fd, 0, os.SEEK_SET)
        rest = b""
        while True:
            piece = os.read(fd, _CHUNK)
            if not piece:
                break
            rest += piece
        if rest:
            out = _open(full, name, os.O_WRONLY | os.O_APPEND | os.O_CREAT)
            try:
                _write_locked(out, rest)
            finally:
                os.close(out)
        os.unlink(name, dir_fd=parent)
    finally:
        os.close(fd)


def ignore(root: Path, folder: str) -> None:
    """`<folder>/full/` in the project's .gitignore, before the full log lands there."""
    from awb.labclient import _directory

    line = str(PurePosixPath(folder) / FULL) + "/"
    with _directory(root) as top:
        fd = _open(top, ".gitignore", os.O_RDWR | os.O_CREAT)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            raw = b""
            while True:
                piece = os.read(fd, _CHUNK)
                if not piece:
                    break
                raw += piece
            text = raw.decode("utf-8")
            if line in text.splitlines():
                return
            add = ("" if not text or text.endswith("\n") else "\n")
            add += (IGNORE_NOTE + "\n" if IGNORE_NOTE not in text.splitlines() else "") + line + "\n"
            os.lseek(fd, 0, os.SEEK_END)
            data = add.encode("utf-8")
            while data:
                data = data[os.write(fd, data):]
        finally:
            os.close(fd)


def append(root: Path, path: Path, text: str) -> Path:
    """Append `text` to the lab log `path` (inside the project folder `root`) and return where it went: the log
    itself or its full log once it moved there. Raises OSError (LabLogError for a link or a special file)."""
    from awb.labclient import _directory

    root = Path(os.path.abspath(root))
    path = Path(os.path.abspath(path))
    folder = path.parent.relative_to(root).as_posix()
    name = path.name
    data = text.encode("utf-8")
    with _directory(path.parent, create=True) as parent:
        for _ in range(3):
            full = _subdir(parent)
            if full is not None:
                try:
                    if _exists(full, name):
                        if _exists(parent, name):
                            _merge(parent, full, name)
                        fd = _open(full, name, os.O_WRONLY | os.O_APPEND | os.O_CREAT)
                        try:
                            _write_locked(fd, data)
                        finally:
                            os.close(fd)
                        _refresh_if_old(root, folder, parent, full, name)
                        return path.parent / FULL / name
                finally:
                    os.close(full)
            fd = _open(parent, name, os.O_RDWR | os.O_APPEND | os.O_CREAT)
            try:
                fcntl.flock(fd, fcntl.LOCK_EX)
                st = os.fstat(fd)
                if st.st_nlink == 0:            # merged into the full log while this one waited for the lock
                    continue
                with os.fdopen(fd, "w", encoding="utf-8", closefd=False) as fh:
                    fh.write(text)
                st = os.fstat(fd)
                try:
                    here = os.stat(name, dir_fd=parent, follow_symlinks=False)
                except FileNotFoundError:
                    here = None
                if here is not None and (here.st_dev, here.st_ino) == (st.st_dev, st.st_ino) and st.st_size > LIMIT:
                    _move(root, folder, parent, name, fd)
                    return path.parent / FULL / name
                return path
            finally:
                os.close(fd)
    raise LabLogError("the lab log moved while it was written")


def _move(root: Path, folder: str, parent: int, name: str, fd: int) -> None:
    """The log `name` (open as `fd`, its lock held by the caller) goes to full/: the ignore line first, then the
    move, then the summary. A rename keeps the file, so a descriptor another process holds on it writes on into the
    full log. When a full log of that name is there already, the text goes to its end and the file goes."""
    ignore(root, folder)
    full = _subdir(parent, create=True)
    try:
        if _exists(full, name):
            os.lseek(fd, 0, os.SEEK_SET)
            rest = b""
            while True:
                piece = os.read(fd, _CHUNK)
                if not piece:
                    break
                rest += piece
            out = _open(full, name, os.O_WRONLY | os.O_APPEND | os.O_CREAT)
            try:
                _write_locked(out, rest)
            finally:
                os.close(out)
            os.unlink(name, dir_fd=parent)
        else:
            os.rename(name, name, src_dir_fd=parent, dst_dir_fd=full)
        _write_summary(folder, parent, full, name)
    finally:
        os.close(full)


def _refresh_if_old(root: Path, folder: str, parent: int, full: int, name: str) -> None:
    try:
        age = time.time() - os.stat(summary_name(name), dir_fd=parent, follow_symlinks=False).st_mtime
    except FileNotFoundError:
        age = REFRESH_SECONDS + 1
    if age > REFRESH_SECONDS:
        ignore(root, folder)
        _write_summary(folder, parent, full, name)


def refresh(root: Path, path: Path) -> Path | None:
    """Write the summary of a lab log whose full text left git (the end of a lab run calls it); the summary path,
    or None for a log that is still small and in git."""
    from awb.labclient import _directory

    root = Path(os.path.abspath(root))
    path = Path(os.path.abspath(path))
    folder = path.parent.relative_to(root).as_posix()
    if not path.parent.is_dir():
        return None
    with _directory(path.parent) as parent:
        full = _subdir(parent)
        if full is None:
            return None
        try:
            if not _exists(full, path.name):
                return None
            if _exists(parent, path.name):
                _merge(parent, full, path.name)
            ignore(root, folder)
            _write_summary(folder, parent, full, path.name)
            return path.parent / summary_name(path.name)
        finally:
            os.close(full)


def summarize(fd: int) -> dict:
    """Start, end, calls, entries, errors, created resources, lines, bytes and SHA-256 of an open log."""
    digest = hashlib.sha256()
    out = {"start": None, "end": None, "calls": 0, "entries": 0, "errors": 0, "created": [], "lines": 0,
           "bytes": 0, "ended": False}
    os.lseek(fd, 0, os.SEEK_SET)
    tail = b""

    def line(raw: bytes) -> None:
        out["lines"] += 1
        m = _ENTRY_RE.match(raw)
        if m:
            out["entries"] += 1
            stamp = m.group(1).decode()
            out["start"] = out["start"] or stamp
            out["end"] = stamp
            if m.group(2) in _METHODS:
                out["calls"] += 1
            if _END_RE.match(raw):
                out["ended"] = True
            return
        m = _OPENED_RE.match(raw)
        if m and out["start"] is None:
            out["start"] = m.group(1).decode("utf-8", "replace")
            return
        m = _ANSWER_RE.match(raw)
        if m:
            if int(m.group(1)) >= 400:
                out["errors"] += 1
            return
        if _ERROR_RE.match(raw):
            out["errors"] += 1
            return
        m = _RECORDED_RE.match(raw)
        if m:
            out["created"].extend(x.strip() for x in m.group(1).decode("utf-8", "replace").split(", ") if x.strip())

    while True:
        piece = os.read(fd, _CHUNK)
        if not piece:
            break
        digest.update(piece)
        out["bytes"] += len(piece)
        parts = (tail + piece).split(b"\n")
        tail = parts.pop()
        for raw in parts:
            line(raw.rstrip(b"\r"))
    if tail:
        line(tail.rstrip(b"\r"))
    out["sha256"] = digest.hexdigest()
    return out


def summary_text(folder: str, name: str, facts: dict) -> str:
    rel = str(PurePosixPath(folder) / FULL / name)
    created = facts["created"]
    limit = "%d MiB" % (LIMIT // (1 << 20)) if LIMIT % (1 << 20) == 0 else "%d bytes" % LIMIT
    lines = ["# Summary of the lab log %s" % name, "",
             "The full log grew over %s and stays out of git in `%s` (git-ignored, on this host only). "
             "This summary is committed instead (Q-0021)." % (limit, rel), "",
             "- full log: `%s`" % rel,
             "- sha256: %s" % facts["sha256"],
             "- size: %d bytes, %d lines" % (facts["bytes"], facts["lines"]),
             "- start: %s" % (facts["start"] or "-"),
             "- end: %s%s" % (facts["end"] or "-", "" if facts["ended"] else " (last entry so far)"),
             "- calls: %d (entries %d)" % (facts["calls"], facts["entries"]),
             "- errors: %d" % facts["errors"],
             "- created resources: %d" % len(created),
             "- summary written: %s" % datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")]
    if created:
        lines += ["", "## Created resources", ""]
        lines += ["- %s" % c.replace("`", "") for c in created[:MAX_LISTED]]
        if len(created) > MAX_LISTED:
            lines += ["- and %d more (in the full log)" % (len(created) - MAX_LISTED)]
    return "\n".join(lines) + "\n"


def _write_summary(folder: str, parent: int, full: int, name: str) -> None:
    fd = _open(full, name, os.O_RDONLY)
    try:
        facts = summarize(fd)
    finally:
        os.close(fd)
    text = summary_text(folder, name, facts)
    target = summary_name(name)
    temp = ".lablog-" + secrets.token_hex(16)
    fd = _open(parent, temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(temp, target, src_dir_fd=parent, dst_dir_fd=parent)
    finally:
        try:
            os.unlink(temp, dir_fd=parent)
        except FileNotFoundError:
            pass
