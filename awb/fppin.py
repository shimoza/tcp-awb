"""The pin of a closed project's fingerprint on the owner side (K16 of docs/reviews/REVIEW-2026-10-20.md).

`awb close` writes the fingerprint of a project to `<shared>/fingerprints/CODE.json`, a file the work user writes,
so a session could empty the fingerprint of another closed project and its deliverables would pass the send gate.
Close therefore pins the sha256 of the file it wrote in `<vault>/fingerprint-pins.tsv` (mode 600, the owner's).
On a sealed host close runs as the work user and cannot write the vault: it asks the vault daemon (op `pin` on
the check socket), which reads and hashes the shared file itself and never takes a hash from the client.

The first pin of a code wins. A later pin with the same hash changes nothing; one with another hash marks the
code as a conflict, which the gate refuses for good: a session that pinned a forged file before the real close,
or that changes the file after it, earns a refusal, never a pass. The gate refuses a closed project without a
pin, with a conflict or whose file does not match its pin, and it reads the bytes it checks once.
"""
from __future__ import annotations

import fcntl
import hashlib
import os
import stat
import tempfile
from pathlib import Path

from awb import codes, config

PINS = "fingerprint-pins.tsv"
LOCK = ".fingerprint-pins.lock"
CONFLICT = "conflict"
MAX_BYTES = 512 * 1024 * 1024
"""A fingerprint larger than this is not read (a planted endless file never hangs the gate or the daemon)."""

PINNED, SAME = "pinned", "same"


def pins_path(vault) -> Path:
    return Path(vault) / PINS


def shared_path(p, code: str) -> Path:
    """Where close writes the fingerprint of `code` (review.fingerprint_path names the same file)."""
    return Path(p.shared) / "fingerprints" / ("%s.json" % code)


def read_bytes(path: Path) -> bytes | None:
    """The bytes of a regular file, never through a link; None when it is missing, a link, too large or unreadable."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except OSError:
        return None
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode) or st.st_size > MAX_BYTES:
            return None
        chunks, size = [], 0
        while True:
            block = os.read(fd, 1 << 20)
            if not block:
                break
            size += len(block)
            if size > MAX_BYTES:
                return None
            chunks.append(block)
        return b"".join(chunks)
    except OSError:
        return None
    finally:
        os.close(fd)


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def load(vault) -> dict[str, str]:
    """Code to pinned sha256 (or CONFLICT). A missing or unreadable file is no pin at all."""
    data = read_bytes(pins_path(vault))
    if data is None:
        return {}
    out: dict[str, str] = {}
    for line in data.decode("utf-8", "replace").splitlines():
        cells = line.split("\t")
        if len(cells) == 2 and codes.is_project_code(cells[0]) and (cells[1] == CONFLICT or (
                len(cells[1]) == 64 and all(c in "0123456789abcdef" for c in cells[1]))):
            out[cells[0]] = cells[1]
    return out


def write(vault, code: str, sha: str) -> str:
    """Pin `sha` for `code` in the vault: PINNED (first pin), SAME (the pin it has) or CONFLICT (another hash came
    before, the code is refused from now on). Raises OSError when the vault cannot be written."""
    if not codes.is_project_code(code):
        raise ValueError("not a project code")
    vault = Path(vault)
    lock_fd = os.open(vault / LOCK, os.O_WRONLY | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_EX)
        pins = load(vault)
        old = pins.get(code)
        if old == sha:
            return SAME
        result = PINNED if old is None else CONFLICT
        pins[code] = sha if old is None else CONFLICT
        body = "".join("%s\t%s\n" % kv for kv in sorted(pins.items())).encode("utf-8")
        fd, tmp = tempfile.mkstemp(prefix=".pins-", dir=str(vault))
        try:
            with os.fdopen(fd, "wb") as fh:
                os.fchmod(fh.fileno(), 0o600)
                fh.write(body)
            os.replace(tmp, pins_path(vault))
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise
        return result
    finally:
        os.close(lock_fd)


def pin_shared(p, code: str) -> str:
    """Owner side (the vault daemon, or close where the vault is writable): hash the shared fingerprint of `code`
    and pin it. Raises OSError when there is none to read or the vault cannot be written."""
    data = read_bytes(shared_path(p, code))
    if data is None:
        raise FileNotFoundError("no readable fingerprint")
    return write(p.vault, code, digest(data))


def pin(p, code: str) -> str:
    """Pin the fingerprint close just wrote; the line close prints. Never raises."""
    try:
        if config.is_work_user():
            from awb import vault_client

            answer = vault_client._request(Path(p.check_socket), {"op": "pin", "code": code}, timeout=10.0)
            result = answer.get("pin") if answer.get("ok") else None
            if result not in (PINNED, SAME, CONFLICT):
                raise OSError("the vault daemon did not pin it")
        else:
            result = pin_shared(p, code)
    except Exception:
        return ("fingerprint not pinned on the owner side: the send gate refuses for %s until awb close %s runs "
                "again with the vault daemon up" % (code, code))
    if result == CONFLICT:
        return ("the fingerprint of %s differs from the one pinned before: the send gate refuses for %s; the owner "
                "removes its line in the vault file %s" % (code, code, PINS))
    return "fingerprint pinned on the owner side"


def pinned_bytes(p, code: str) -> tuple[bytes | None, str]:
    """For the gate: (the fingerprint's bytes, "") when the shared file matches its pin, else (None, the reason)."""
    sha = load(p.vault).get(code)
    if sha is None:
        return None, "unpinned"
    if sha == CONFLICT:
        return None, "conflict"
    data = read_bytes(shared_path(p, code))
    if data is None:
        return None, "missing"
    if digest(data) != sha:
        return None, "changed"
    return data, ""
