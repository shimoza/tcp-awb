"""Zip and tar archives (tar also gzip, bzip2 and xz compressed) and single compressed files.

Member names go to `detect_text` because file names carry names, together with what else an archive
carries about its members: every plausible decoding of a zip name written without the UTF-8 flag, the raw
name of a zip member that a unicode path extra field (0x7075) replaced (the name other tools show), the
archive comment and member comments, the owner names and link targets of tar members, the original name
and comment in a gzip header (of a compressed tar as well as of a single compressed file). A zip member
named with a trailing slash that carries data is a file, not a folder. The names of zip members past the
member limit go to `scan_text`, which is checked for registered forms only. Members are written to
a private temporary folder (mode 700, under `awb.extract.temp_root` when set) under an index and their
suffix only and passed through `extract` as children. The folder is removed again before this function
returns. A single compressed file (report.txt.gz) is listed as "member" plus its inner suffix: its name
is the name of the file the intake got, which never goes into an output.

Bomb guard: one archive tree (the archive and every archive nested in it) lists at most MAX_MEMBERS
members, unpacks at most MAX_TOTAL_BYTES and, for tar, reads past at most MAX_TOTAL_BYTES of declared
member data (a compressed tar is decompressed to reach the next member). When a limit is reached the walk
stops and a note says so. Counting over the whole tree matters: a nested bomb stays small at every level.
A walk that skipped anything (a limit, the nesting depth, an encrypted or unreadable member) sets
`meta["incomplete"]`, so that a check never calls the archive clean.

Each child keeps the member name in `meta["member"]`. Its `path` points at the temporary copy, which
no longer exists when this function returns.
"""
from __future__ import annotations

import bz2
import contextvars
import gzip
import lzma
import os
import re
import shutil
import struct
import tarfile
import zipfile
import zlib
from dataclasses import dataclass
from pathlib import Path

from awb.extract import INCOMPLETE, MAX_ARCHIVE_DEPTH, Extraction, make_temp_dir

MAX_MEMBERS = 200
MAX_TOTAL_BYTES = 200 * 1024 * 1024
MAX_LISTED_NAMES = 10000
"""Zip member names past the member limit that still go to detection (read from the central directory)."""

_SAFE_NAME_RE = re.compile(r"[^A-Za-z0-9._-]+")
_READ_ERRORS = (
    zipfile.BadZipFile, tarfile.TarError, EOFError, OSError, zlib.error, lzma.LZMAError,
    NotImplementedError, RuntimeError, ValueError,
)


@dataclass
class _Budget:
    """What one archive tree may still unpack. Shared by nested archives through a context variable."""

    members: int = 0
    bytes: int = 0
    read: int = 0
    stopped: bool = False

    def members_left(self) -> bool:
        return self.members < MAX_MEMBERS

    def bytes_left(self) -> int:
        return MAX_TOTAL_BYTES - self.bytes


_BUDGET: contextvars.ContextVar[_Budget | None] = contextvars.ContextVar("awb_archive_budget", default=None)


_NAME_ENCODINGS = ("utf-8", "cp437", "cp850", "cp1252")


_COMMON_LETTERS = frozenset("äöüÄÖÜß")


def _name_score(name: str) -> int:
    """How plausible a decoding is: German umlauts count most, other Latin-1 letters next, other letters
    least; any other character outside ASCII counts against it."""
    score = 0
    for c in name:
        if c.isascii():
            continue
        if c in _COMMON_LETTERS:
            score += 3
        elif c.isalpha() and c <= "\u00ff":
            score += 2
        elif c.isalpha():
            score += 1
        else:
            score -= 1
    return score


def _decodings(raw: bytes) -> list[str]:
    """Every decoding of a name's bytes, the most plausible first (UTF-8 when it decodes, else the code page
    that gives the most letters; cp437, the zip default, wins a tie)."""
    found: list[str] = []
    for enc in _NAME_ENCODINGS:
        try:
            text = raw.decode(enc)
        except UnicodeDecodeError:
            continue
        if text not in found:
            found.append(text)
    if not found:
        return [raw.decode("latin-1")]
    if raw.isascii():
        return found[:1]
    try:
        utf8 = raw.decode("utf-8")
        return [utf8] + [f for f in found if f != utf8]
    except UnicodeDecodeError:
        pass
    best = max(found, key=_name_score)   # max keeps the first of equals, cp437 comes before the others
    return [best] + [f for f in found if f != best]


def _cp437_decodings(name: str) -> list[str]:
    """Every plausible decoding of a name that zipfile decoded as cp437."""
    try:
        return _decodings(name.encode("cp437"))
    except UnicodeEncodeError:
        return [name]


def _zip_names(info: zipfile.ZipInfo) -> list[str]:
    """The member name to list first, then the other plausible decodings of a name without the UTF-8 flag
    (zipfile always decodes such a name as cp437). When a unicode path extra field (0x7075) replaced the raw
    name, the listed name is that path and the decodings of the raw name follow: other tools show the raw name."""
    name = info.filename
    utf8 = bool(info.flag_bits & 0x800)
    replaced = info.orig_filename != name
    names = [name] if utf8 or replaced else _cp437_decodings(name)
    if replaced:
        for other in [info.orig_filename] if utf8 else _cp437_decodings(info.orig_filename):
            if other not in names:
                names.append(other)
    return names


def _tar_names(value: str) -> list[str]:
    """A tar name that did not decode as UTF-8 carries surrogate escapes: decode its bytes by code page."""
    if not any("\udc80" <= c <= "\udcff" for c in value):
        return [value]
    return _decodings(value.encode("utf-8", "surrogateescape"))


def _safe_member_path(folder: Path, index: int, name: str) -> Path:
    """A flat file name inside `folder` made of the index and the suffix only, so that the temporary path
    carries no name. The suffix survives so that sniffing can use it."""
    base = os.path.basename(name.replace("\\", "/").rstrip("/")) or "member"
    _, dot, suffix = base.rpartition(".")
    suffix = _SAFE_NAME_RE.sub("", suffix)[:16] if dot else ""
    fname = "%04d" % index
    if suffix:
        fname += "." + suffix
    return folder / fname


def _gzip_header_strings(path: Path) -> list[str]:
    """The original file name and the comment a gzip header may carry (RFC 1952), for detection."""
    try:
        with open(path, "rb") as fh:
            head = fh.read(64 * 1024)
    except OSError:
        return []
    if len(head) < 10 or head[:2] != b"\x1f\x8b":
        return []
    flags = head[3]
    pos = 10
    out: list[str] = []
    if flags & 0x04 and len(head) >= pos + 2:
        pos += 2 + struct.unpack("<H", head[pos:pos + 2])[0]
    for bit in (0x08, 0x10):
        if flags & bit:
            end = head.find(b"\x00", pos)
            if end < 0:
                break
            if end > pos:
                out.extend(_decodings(head[pos:end]))
            pos = end + 1
    return out


def _copy_capped(src, dst: Path, budget: int) -> int:
    """Copy a member stream to `dst`, never more than `budget` bytes. Returns bytes read, budget + 1 if cut."""
    written = 0
    fd = os.open(dst, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "wb") as out:
        while True:
            chunk = src.read(65536)
            if not chunk:
                break
            if written + len(chunk) > budget:
                written = budget + 1
                break
            out.write(chunk)
            written += len(chunk)
    return written


def _member_listing(names: list[str], sizes: list[int]) -> str:
    lines = ["| member | bytes |", "| --- | --- |"]
    for name, size in zip(names, sizes):
        cell = name.replace("|", "\\|").replace("\r", " ").replace("\n", " ")
        lines.append("| %s | %d |" % (cell, size))
    return "\n".join(lines)


def _finish(ex: Extraction, names: list[str], sizes: list[int], extra: list[str], scan: list[str]) -> Extraction:
    ex.text = _member_listing(names, sizes) if names else ""
    ex.detect_text = "\n".join(names + [e for e in extra if e and e.strip()])
    ex.scan_text = "\n".join(scan)
    ex.meta["member_count"] = len(names)
    ex.meta["child_count"] = len(ex.children)
    if ex.state == "ok" and not names:
        ex.state = "unreadable"
        ex.notes.append("archive has no members")
    return ex


class _Walk:
    """State of one archive walk: the listing, the children and the temporary folder."""

    def __init__(self, ex: Extraction, tmp: Path, depth: int, budget: _Budget):
        from awb.extract import extract

        self.ex = ex
        self.tmp = tmp
        self.depth = depth
        self.budget = budget
        self.extract = extract
        self.expand = depth < MAX_ARCHIVE_DEPTH
        self.names: list[str] = []
        self.sizes: list[int] = []
        self.extra: list[str] = []      # what else detection must see: comments, owners, other decodings
        self.scan: list[str] = []       # names past the member limit, checked for registered forms only
        self.unreadable_members = 0

    def incomplete(self) -> None:
        self.ex.meta[INCOMPLETE] = True

    def list_member(self, name: str, size: int, total: int | None) -> bool:
        """Add a member to the listing. False when the member limit stops the walk."""
        if not self.budget.members_left():
            self.budget.stopped = True
            self.incomplete()
            if total is not None:
                self.ex.notes.append(
                    "member limit of %d reached, %d of %d members listed" % (MAX_MEMBERS, len(self.names), total)
                )
            else:
                self.ex.notes.append("member limit of %d reached, first %d members listed" % (MAX_MEMBERS, len(self.names)))
            return False
        self.budget.members += 1
        self.names.append(name)
        self.sizes.append(size)
        return True

    def unpack(self, index: int, name: str, declared: int, open_stream) -> None:
        """Copy one member into the temporary folder within the budget and extract it as a child."""
        if not self.expand:
            self.incomplete()
            return
        if declared > self.budget.bytes_left():
            self._size_stop(index)
            return
        dst = _safe_member_path(self.tmp, index, name)
        try:
            with open_stream() as src:
                written = _copy_capped(src, dst, self.budget.bytes_left())
        except _READ_ERRORS:
            self.unreadable_members += 1
            self.incomplete()
            dst.unlink(missing_ok=True)
            return
        if written > self.budget.bytes_left():
            dst.unlink(missing_ok=True)
            self._size_stop(index)
            return
        self.budget.bytes += written
        child = self.extract(dst, self.depth + 1)
        child.meta["member"] = name
        self.ex.children.append(child)

    def _size_stop(self, index: int) -> None:
        self.ex.notes.append(
            "size limit of %d MB reached at member %d, remaining members not extracted"
            % (MAX_TOTAL_BYTES // (1024 * 1024), index + 1)
        )
        self.budget.stopped = True
        self.expand = False
        self.size_stopped = True
        self.incomplete()

    size_stopped = False

    def read_past(self, size: int) -> bool:
        """Count declared member data a tar walk must decompress to reach the next member. False when the
        read budget of the tree is used up: the walk stops before reading it."""
        self.budget.read += max(0, size)
        if self.budget.read > MAX_TOTAL_BYTES:
            self.ex.notes.append("read limit of %d MB reached, remaining members not listed"
                                 % (MAX_TOTAL_BYTES // (1024 * 1024)))
            self.budget.stopped = True
            self.incomplete()
            return False
        return True

    def close_notes(self) -> None:
        if self.unreadable_members:
            self.ex.notes.append("%d member(s) could not be read, review the original" % self.unreadable_members)


def extract_archive(path: Path, kind: str, depth: int = 0) -> Extraction:
    """Read a zip or tar archive. `kind` is zip or tar. `depth` is the nesting level of `path`."""
    path = Path(path)
    ex = Extraction(path, kind, "ok")
    budget = _BUDGET.get()
    token = None
    if budget is None:
        budget = _Budget()
        token = _BUDGET.set(budget)
    stopped_before = budget.stopped
    tmp = make_temp_dir("awb-")
    walk = _Walk(ex, tmp, depth, budget)
    try:
        if not walk.expand:
            ex.notes.append("nesting depth %d reached, members listed but not extracted" % MAX_ARCHIVE_DEPTH)
        try:
            if kind == "zip":
                _walk_zip(walk, path)
            else:
                _walk_tar_or_stream(walk, path)
        except _READ_ERRORS as err:
            if not walk.names:
                ex.state = "failed"
            walk.incomplete()
            ex.notes.append("%s while reading the archive" % type(err).__name__)
        walk.close_notes()
        if budget.stopped and not stopped_before and not any("limit" in n for n in ex.notes):
            ex.notes.append("a limit was reached inside a nested archive, see the notes of the members")
            walk.incomplete()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
        if token is not None:
            _BUDGET.reset(token)
    return _finish(ex, walk.names, walk.sizes, walk.extra, walk.scan)


def _comment(raw: bytes) -> list[str]:
    return _decodings(raw) if raw and raw.strip(b"\x00 \t\r\n") else []


def _walk_zip(walk: _Walk, path: Path) -> None:
    encrypted = 0
    with zipfile.ZipFile(path) as zf:
        infos = zf.infolist()
        walk.extra.extend(_comment(zf.comment))
        for index, info in enumerate(infos):
            names = _zip_names(info)
            if not walk.list_member(names[0], info.file_size, len(infos)):
                # the central directory is read already: every other name still goes to detection
                for rest in infos[index:index + MAX_LISTED_NAMES]:
                    walk.scan.extend(_zip_names(rest))
                    walk.scan.extend(_comment(rest.comment))
                break
            walk.extra.extend(names[1:])
            walk.extra.extend(_comment(info.comment))
            if info.is_dir() and info.file_size == 0:
                continue
            if info.flag_bits & 0x1:
                encrypted += 1
                continue
            walk.unpack(index, names[0], info.file_size, lambda info=info: zf.open(info))
    if encrypted:
        walk.incomplete()
        walk.ex.notes.append("%d encrypted member(s) skipped, review the original" % encrypted)


def _walk_tar_or_stream(walk: _Walk, path: Path) -> None:
    # a gzip header carries an original name and a comment, around a tar as well as around a single file
    walk.extra.extend(_gzip_header_strings(path))
    try:
        tf = tarfile.open(path, "r:*")
    except tarfile.ReadError:
        # not a tar: maybe one compressed file (report.txt.gz); read it as a single member
        opener = _stream_opener(path)
        if opener is None:
            raise
        walk.ex.meta["compressed_single_file"] = True
        inner = Path(path.name[: -len(path.suffix)] if path.suffix else path.name).suffix
        name = "member" + (inner.lower() if re.fullmatch(r"\.[A-Za-z0-9]{1,8}", inner) else "")
        if walk.list_member(name, 0, 1):
            walk.unpack(0, name, 0, lambda: opener(path, "rb"))
        if walk.expand and not walk.ex.children:
            walk.ex.state = "failed"
        return
    with tf:
        skipped_links = 0
        for index, member in enumerate(tf):
            names = _tar_names(member.name)
            if not walk.list_member(names[0], member.size, None):
                break
            walk.extra.extend(names[1:])
            for value in (member.uname, member.gname, member.linkname, *member.pax_headers.values()):
                if isinstance(value, str) and value.strip():
                    walk.extra.extend(_tar_names(value))
            if not walk.read_past(member.size if member.isfile() else 0):
                break
            if member.issym() or member.islnk():
                skipped_links += 1
                continue
            if not member.isfile():
                continue
            walk.unpack(index, names[0], member.size, lambda member=member: _tar_stream(tf, member))
            if walk.size_stopped:
                # every further member would be decompressed only to be listed
                walk.ex.notes.append("walk stopped after the size limit, later members not listed")
                break
        if skipped_links:
            walk.ex.notes.append("%d link member(s) not followed" % skipped_links)


def _tar_stream(tf: tarfile.TarFile, member: tarfile.TarInfo):
    src = tf.extractfile(member)
    if src is None:
        raise tarfile.ExtractError("member has no data")
    return src


def _stream_opener(path: Path):
    with open(path, "rb") as fh:
        head = fh.read(6)
    if head.startswith(b"\x1f\x8b"):
        return gzip.open
    if head.startswith(b"BZh"):
        return bz2.open
    if head.startswith(b"\xfd7zXZ\x00"):
        return lzma.open
    return None
