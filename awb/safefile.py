"""File safety in one place (Q-0023, finding R4 of the UX review of 2026-11-19).

The pinned read, the atomic write and the private and shared publish were written module by module; each copy now
calls this module. The calls keep what their copy did: the mode, the name of the temporary file, the fsync, the error
a caller catches. Where copies differ the difference is a parameter here, never a silent unification.

- `open_regular` opens a file without following a link (unless `follow`) and without waiting on a named pipe, then
  judges the opened file: a regular file, of this uid when `own`, with one name when `single_link`.
- `read_fd` and `read_regular` read such a file, at most `cap` bytes when a cap is given.
- `write_atomic` writes a temporary file beside the target (exclusive, never through a link), fsyncs it and renames
  it over the target, so a reader sees the old file or the new one, never a part. `publish_private` and
  `publish_shared` are the two common cases: mode 600, and mode 664 for a file the other user reads through the group.
- `write_new` creates a file that must not exist yet.

MODES is the one table of modes per path class. SHARED_TREE names the folders of `<shared>` that both users write,
with their folder and file class; `seal/verify.sh` reads it (`python3 -I awb/safefile.py table`) and checks the host.
The module imports the standard library only, so the seal can run it as a script.
"""
from __future__ import annotations

import os
import secrets
import stat
import sys
from typing import Callable

PRIVATE = 0o600
"""A file only its owner reads."""
PRIVATE_DIR = 0o700
SHARED = 0o664
"""A file the other user (owner or work user, one group) reads and writes through the group."""
SHARED_DIR = 0o2775
"""A folder both users write: group writable with the setgid bit, so what one side creates keeps the group."""

MODES: dict[str, int] = {
    "private": PRIVATE,
    "private-dir": PRIVATE_DIR,
    "shared": SHARED,
    "shared-dir": SHARED_DIR,
}
"""The mode of each path class."""

SHARED_TREE: tuple[tuple[str, str, str], ...] = (
    ("recheck", "shared-dir", "shared"),
    ("ux", "shared-dir", "shared"),
)
"""(folder under `<shared>`, class of the folder, class of each file in it) for every folder both users write."""

NOT_REGULAR = "not a regular file"
NOT_OWN = "not a file of this user"
MANY_LINKS = "a file with more than one name"
TOO_LARGE = "larger than %d bytes"


def open_regular(name, *, dir_fd: int | None = None, flags: int = os.O_RDONLY, mode: int = 0o600,
                 follow: bool = False, nonblock: bool = True, cloexec: bool = False, own: bool = False,
                 single_link: bool = False, refuse: Callable[[], BaseException] | None = None) -> int:
    """The descriptor of `name` (relative to `dir_fd` when given), opened with `flags` and judged by the opened file:
    a regular file, of this uid when `own`, with one name when `single_link`. A failed check closes the descriptor
    and raises `refuse()` when given, else OSError with the reason (NOT_REGULAR, NOT_OWN, MANY_LINKS). An error of
    the open itself (FileNotFoundError, a link refused with ELOOP) passes unchanged."""
    flags |= (0 if follow else os.O_NOFOLLOW) | (os.O_NONBLOCK if nonblock else 0)
    flags |= getattr(os, "O_CLOEXEC", 0) if cloexec else 0
    fd = os.open(name, flags, mode, dir_fd=dir_fd)
    try:
        st = os.fstat(fd)
        reason = None
        if not stat.S_ISREG(st.st_mode):
            reason = NOT_REGULAR
        elif own and st.st_uid != os.geteuid():
            reason = NOT_OWN
        elif single_link and st.st_nlink != 1:
            reason = MANY_LINKS
        if reason is not None:
            raise refuse() if refuse is not None else OSError(reason)
        return fd
    except BaseException:
        os.close(fd)
        raise


def read_fd(fd: int, cap: int | None = None, *, too_large: str = TOO_LARGE) -> bytes:
    """Every byte of the open file `fd`. With `cap`: a file over `cap` bytes, by its size or by what the read
    returns, raises OSError(too_large) (`%d` in it takes the cap)."""
    def over() -> OSError:
        return OSError(too_large % cap if "%d" in too_large else too_large)
    if cap is not None and os.fstat(fd).st_size > cap:
        raise over()
    chunks, size = [], 0
    while cap is None or size <= cap:
        chunk = os.read(fd, 65536 if cap is None else min(65536, cap + 1 - size))
        if not chunk:
            return b"".join(chunks)
        chunks.append(chunk)
        size += len(chunk)
    raise over()


def read_regular(name, *, dir_fd: int | None = None, cap: int | None = None, own: bool = False,
                 single_link: bool = False, follow: bool = False, cloexec: bool = False,
                 refuse: Callable[[], BaseException] | None = None, too_large: str = TOO_LARGE) -> bytes:
    """The bytes of a regular file opened by `open_regular`, read by `read_fd`."""
    fd = open_regular(name, dir_fd=dir_fd, follow=follow, cloexec=cloexec, own=own, single_link=single_link,
                      refuse=refuse)
    try:
        return read_fd(fd, cap, too_large=too_large)
    finally:
        os.close(fd)


def default_temp(name: str) -> str:
    return ".%s.%s.tmp" % (name, secrets.token_hex(8))


def write_atomic(path, data: bytes | str, *, dir_fd: int | None = None, mode: int = PRIVATE,
                 create_mode: int | None = None, chmod: bool = False, owner: tuple[int, int] | None = None,
                 temp: Callable[[str], str] | None = None, exclusive: bool = True, fsync: bool = True,
                 fsync_dir: bool = False, overwrite: bool = True,
                 rename: Callable[[str, str], None] | None = None) -> None:
    """Write `data` (text is written as UTF-8) to `path` through a temporary file beside it.

    `path` is a name in `dir_fd` when `dir_fd` is given (the caller pinned the folder), else a path. The temporary
    file is named `temp(name)` (default `.NAME.HEX.tmp`), opened O_CREAT|O_NOFOLLOW and O_EXCL (O_TRUNC when not
    `exclusive`) with `create_mode` (default `mode`, the umask applies). `chmod` sets exactly `mode` on it after the
    write, `owner` (uid, gid) is set after that, then the file is fsynced when `fsync`. It is renamed over the
    target (`rename(tmp, path)` when given, path mode only) or, without `overwrite`, linked to it, which refuses an
    existing target with FileExistsError. `fsync_dir` fsyncs `dir_fd` after the rename. On every way out the
    temporary file is gone; an error of that cleanup never hides the error that caused it."""
    if isinstance(data, str):
        data = data.encode("utf-8")
    path = os.fspath(path)
    if dir_fd is None:
        folder, name = os.path.split(path)
    else:
        folder, name = "", path
    tmp_name = (temp or default_temp)(name)
    tmp = os.path.join(folder, tmp_name) if folder else tmp_name
    flags = os.O_WRONLY | os.O_CREAT | os.O_NOFOLLOW | (os.O_EXCL if exclusive else os.O_TRUNC)
    fd = os.open(tmp, flags, mode if create_mode is None else create_mode, dir_fd=dir_fd)
    done = False
    try:
        with os.fdopen(fd, "wb") as fh:        # the file object closes fd on every way out
            fh.write(data)
            fh.flush()
            if chmod:
                os.fchmod(fh.fileno(), mode)
            if owner is not None:
                os.fchown(fh.fileno(), owner[0], owner[1])
            if fsync:
                os.fsync(fh.fileno())
        if not overwrite:
            os.link(tmp, path, src_dir_fd=dir_fd, dst_dir_fd=dir_fd, follow_symlinks=False)
        elif rename is not None and dir_fd is None:
            rename(tmp, path)
            done = True
        else:
            os.replace(tmp, path, src_dir_fd=dir_fd, dst_dir_fd=dir_fd)
            done = True
        if fsync_dir and dir_fd is not None:
            os.fsync(dir_fd)
    except BaseException:
        if not done:
            try:
                os.unlink(tmp, dir_fd=dir_fd)
            except OSError:
                pass
        raise
    if not overwrite:
        try:
            os.unlink(tmp, dir_fd=dir_fd)
        except FileNotFoundError:
            pass


def publish_private(path, data: bytes | str, **kw) -> None:
    """`write_atomic` with exactly mode 600."""
    write_atomic(path, data, mode=PRIVATE, chmod=True, **kw)


def publish_shared(path, data: bytes | str, **kw) -> None:
    """`write_atomic` with exactly mode 664: the other user reads it through the group (a shared folder of mode 2775
    gives it the group)."""
    write_atomic(path, data, mode=SHARED, chmod=True, **kw)


def write_new(path, data: bytes, *, mode: int = PRIVATE, dir_fd: int | None = None) -> None:
    """Create `path`, which must not exist (FileExistsError, also for a link), with `mode` under the umask."""
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode, dir_fd=dir_fd)
    with os.fdopen(fd, "wb") as fh:
        fh.write(data)


def table_lines() -> list[str]:
    """`FOLDER DIRMODE FILEMODE` in octal for each folder of SHARED_TREE, the lines seal/verify.sh reads."""
    return ["%s %o %o" % (rel, MODES[d], MODES[f]) for rel, d, f in SHARED_TREE]


def main(argv: list[str]) -> int:
    if argv != ["table"]:
        print("usage: python3 -I awb/safefile.py table", file=sys.stderr)
        return 2
    print("\n".join(table_lines()))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
