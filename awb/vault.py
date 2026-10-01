"""The vault: encryption at rest, the vault daemon and its clients.

Encryption. gpg symmetric AES256 through subprocess, always with --batch --pinentry-mode loopback
--no-symkey-cache and a private homedir (`<vault>/.gnupg`, mode 700). The passphrase goes through its own pipe
(--passphrase-fd with pass_fds), the data through standard input and output. No plaintext temporary file is
ever written. An explicit s2k count keeps gpg from starting an agent of its own.

The daemon (`awb vault serve`, run as the owner) listens on two unix sockets, one JSON object per line and one
answer per request:

    admin socket  <vault>/admin.sock, mode 600, peer uid must be the daemon's own uid
                  ping, unlock, lock, status, register_load, register_save, seal_file, open_file
    check socket  /run/awb/check.sock, mode 660, the group of the daemon process
                  ping, check

States: `plain` (no register.tsv.gpg: the plaintext register is used without unlock), `locked` (the encrypted
register exists and no passphrase is in memory) and `unlocked`. A check answers start, length and class per
hit and nothing else; the check socket never sees a code, a form or a count of forms. Every check request is
logged to `<vault>/log/checks-YYYY-MM.tsv` with time, peer uid (SO_PEERCRED), text length, hit count and
result, never the text. Requests on the check socket are limited per peer uid; a connection that gets the answer
"rate" is closed. Each socket has its own cap of open connections (the check socket also per peer uid) and
every connection has a maximum lifetime, so that no load on the check socket can lock the owner out of the admin
socket. A text larger than one request is checked in overlapping chunks (`check_remote`).

`vault_lock` holds the vault for one intake or one encryption, so that no intake writes a plaintext original
while `awb vault encrypt` runs.

Nothing here puts a passphrase, a form, a code or a path of the vault into a message, a log line or an answer
of the check socket.
"""
from __future__ import annotations

import base64
import fcntl
import getpass
import json
import os
import re
import signal
import socket
import stat
import struct
import subprocess
import sys
import tempfile
import threading
import time
import unicodedata
from collections import deque
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from awb import check, codes, config, register
from awb.matcher import Matcher

GPG = "gpg"
S2K_COUNT = 65011712
"""Iterations of the passphrase hash. 65536 or more: below that gpg asks an agent to calibrate it."""
GPG_TIMEOUT = 300

MAX_REQUEST = 4 * 1024 * 1024
"""Largest request line the daemon reads (bytes, without the line end)."""
READ_TIMEOUT = 10.0
"""Seconds the daemon waits for one complete request on a connection."""
MAX_CONNECTION_SECONDS = 120.0
"""Seconds a connection may stay open at all, however often it sends a request."""
RATE_LIMIT = 600
RATE_WINDOW = 60.0
RATE_BYTES = 16 * 1024 * 1024
"""Bytes of requests one uid may send per window: a request that packs a dictionary into one line spends its
share by size, not only one token (the review of 2026-09-27)."""
MAX_CHECK_CHARS = 700_000
"""Characters one check may carry; the client sends a long text in pieces below that."""
MAX_CONCURRENT_SCANS = 2
"""Scans of the check socket that run at once; the rest wait, so that the admin socket is never starved."""
SCAN_WAIT = 5.0
"""At most RATE_LIMIT requests per RATE_WINDOW seconds per peer uid on the check socket. A connection that gets
the answer "rate" is closed."""
MAX_CONNECTIONS = 64
"""Open connections of the check socket. The admin socket counts its own (MAX_ADMIN_CONNECTIONS), so that load
on the check socket never locks the owner out."""
MAX_CONNECTIONS_PER_UID = 32
"""Open connections of the check socket per peer uid."""
MAX_ADMIN_CONNECTIONS = 16
MAX_SOCKET_PATH = 100
LOCK_WAIT = 60.0
"""Seconds `vault_lock` waits for an intake or an encryption that holds the vault."""

CHECK_CHUNK = 600_000
"""Characters of text per check request. The request stays under MAX_REQUEST even when every character is
escaped; a larger text goes in several requests (see check_remote)."""
CHECK_OVERLAP = 20_000
"""Characters two neighbouring chunks share, so that a hit across a cut is found whole in the next chunk."""

CLIENT_TIMEOUT = 60.0
MAX_CHECK_ANSWER = 64 * 1024 * 1024
MAX_ADMIN_ANSWER = 1024 * 1024 * 1024
MAX_PASSPHRASE = 1024
MIN_NEW_PASSPHRASE = 12

HIT_KEYS = frozenset(("start", "length", "cls"))
HIT_CLASSES = frozenset(("name",) + tuple(k.lower() for k in codes.DATA_KINDS))
CHECK_LOG_HEADER = "time\tuid\tlength\thits\tresult\n"

ERROR_WORDS = frozenset(("locked", "rate", "refused", "wrong passphrase", "bad request", "unknown op", "too large",
                         "no register", "register invalid", "not encrypted", "invalid", "failed", "no such file",
                         "not allowed"))
"""The only error words a client repeats. Anything else is an unexpected answer."""
_DETAIL_RE = re.compile(r"[A-Za-z0-9 ,;:.'()-]{1,200}")


class VaultError(Exception):
    """A vault problem. Messages carry no passphrase, no form, no code and no path."""


class VaultLocked(VaultError):
    """The register is encrypted and the daemon holds no passphrase."""


class VaultUnavailable(VaultError):
    """No daemon, the socket is missing or refused, a timeout or an answer of an unexpected form."""


class VaultRateLimited(VaultUnavailable):
    """The daemon refused the request: too many requests of this user in the last minute."""


# --------------------------------------------------------------------------- encryption


def _homedir(homedir: Path) -> Path:
    homedir = Path(homedir)
    homedir.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(homedir, 0o700)
    return homedir


def _gpg_env() -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if not k.startswith("GNUPG") and k != "GPG_AGENT_INFO"}
    env["LC_ALL"] = "C"
    return env


def _check_passphrase(passphrase) -> str:
    if not isinstance(passphrase, str) or not passphrase:
        raise VaultError("the passphrase is empty")
    if len(passphrase) > MAX_PASSPHRASE:
        raise VaultError("the passphrase is longer than %d characters" % MAX_PASSPHRASE)
    if any(c in passphrase for c in "\r\n\x00"):
        raise VaultError("the passphrase must be one line")
    return passphrase


def _gpg(args: list[str], data: bytes, passphrase: str, homedir: Path) -> subprocess.CompletedProcess:
    """Run gpg with the passphrase on its own pipe and the data on standard input."""
    passphrase = _check_passphrase(passphrase)
    home = _homedir(homedir)
    r, w = os.pipe()
    try:
        os.write(w, passphrase.encode("utf-8") + b"\n")
        os.close(w)
        w = -1
        cmd = [GPG, "--batch", "--no-tty", "--quiet", "--no-autostart", "--pinentry-mode", "loopback",
               "--no-symkey-cache", "--homedir", str(home), "--passphrase-fd", str(r), "--status-fd", "2", *args]
        return subprocess.run(cmd, input=data, capture_output=True, pass_fds=(r,), env=_gpg_env(),
                              timeout=GPG_TIMEOUT, check=False)
    except subprocess.TimeoutExpired:
        raise VaultError("gpg timed out") from None
    except OSError as err:
        raise VaultError("gpg cannot be run (%s)" % type(err).__name__) from None
    finally:
        if w >= 0:
            os.close(w)
        os.close(r)


def _status(res: subprocess.CompletedProcess) -> set[str]:
    return {line.split()[1] for line in res.stderr.decode("utf-8", "replace").splitlines()
            if line.startswith("[GNUPG:] ") and len(line.split()) > 1}


def encrypt_bytes(data: bytes, passphrase: str, homedir: Path) -> bytes:
    """`data` encrypted with gpg symmetric AES256 under `passphrase`."""
    if not isinstance(data, (bytes, bytearray)):
        raise TypeError("data must be bytes")
    res = _gpg(["--symmetric", "--cipher-algo", "AES256", "--s2k-mode", "3", "--s2k-digest-algo", "SHA512",
                "--s2k-count", str(S2K_COUNT), "--output", "-"], bytes(data), passphrase, homedir)
    if res.returncode != 0 or "END_ENCRYPTION" not in _status(res) or not res.stdout:
        raise VaultError("encryption failed")
    return res.stdout


def decrypt_bytes(data: bytes, passphrase: str, homedir: Path) -> bytes:
    """The plaintext of `data`. Raises VaultError("wrong passphrase or damaged file") on any failure, also for
    data that is not encrypted at all."""
    if not isinstance(data, (bytes, bytearray)):
        raise TypeError("data must be bytes")
    res = _gpg(["--decrypt", "--output", "-"], bytes(data), passphrase, homedir)
    status = _status(res)
    if res.returncode != 0 or "DECRYPTION_OKAY" not in status or "DECRYPTION_FAILED" in status:
        raise VaultError("wrong passphrase or damaged file")
    return res.stdout


# --------------------------------------------------------------------------- files


def _write_atomic(path: Path, data: bytes, mode: int = 0o600) -> None:
    path = Path(path)
    fd, tmp = tempfile.mkstemp(prefix=".awb-", suffix=".tmp", dir=path.parent)
    try:
        os.fchmod(fd, mode)
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    try:
        dfd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(dfd)
        finally:
            os.close(dfd)
    except OSError:
        pass


def _shred(path: Path) -> None:
    """Overwrite a plaintext file with zeros, then unlink it."""
    fd = os.open(path, os.O_WRONLY | getattr(os, "O_NOFOLLOW", 0))
    try:
        size = os.fstat(fd).st_size
        block = b"\x00" * 65536
        done = 0
        while done < size:
            done += os.write(fd, block[:min(len(block), size - done)])
        os.fsync(fd)
    finally:
        os.close(fd)
    os.unlink(path)


def _is_regular(path: Path) -> bool:
    try:
        return stat.S_ISREG(os.lstat(path).st_mode)
    except OSError:
        return False


def _gpg_path(path: Path) -> Path:
    return path.with_name(path.name + ".gpg")


def seal_path(path: Path, passphrase: str, homedir: Path) -> Path:
    """Encrypt a plaintext file to `<path>.gpg`, prove that it decrypts, then overwrite and remove the plaintext."""
    path = Path(path)
    data = path.read_bytes()
    enc = encrypt_bytes(data, passphrase, homedir)
    if decrypt_bytes(enc, passphrase, homedir) != data:
        raise VaultError("the encrypted copy does not decrypt to the original, the plaintext is kept")
    target = _gpg_path(path)
    _write_atomic(target, enc)
    _shred(path)
    return target


def _vault_homedir(p: config.Paths) -> Path:
    return p.vault / ".gnupg"


def plaintext_files(p: config.Paths) -> list[Path]:
    """What `awb vault encrypt` seals: keep.tsv, every regular file under originals/ and reports/ that is not
    a .gpg yet. The register comes last, so that a failed run can be repeated."""
    out: list[Path] = []
    if _is_regular(p.keep_list):
        out.append(p.keep_list)
    for folder in (p.originals, p.private_reports):
        if not folder.is_dir():
            continue
        for root, dirs, files in os.walk(folder):
            dirs.sort()
            for name in sorted(files):
                f = Path(root) / name
                if not name.endswith(".gpg") and _is_regular(f):
                    out.append(f)
    out.append(p.register)
    return out


@contextmanager
def vault_lock(p: config.Paths, wait: float | None = None):
    """Hold the vault for one intake or one encryption: an exclusive lock on the vault folder itself (no lock
    file). Waits up to `wait` seconds (LOCK_WAIT by default), then raises VaultError. The daemon never takes it,
    so an intake that holds it can still have its files sealed."""
    wait = LOCK_WAIT if wait is None else wait
    fd = os.open(p.vault, os.O_RDONLY | os.O_DIRECTORY)
    try:
        deadline = time.monotonic() + wait
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise VaultError("an intake or an encryption holds the vault, try again when it is done") \
                        from None
                time.sleep(0.1)
        yield
    finally:
        os.close(fd)


def _sealed_files(p: config.Paths) -> list[Path]:
    """The .gpg files an earlier run of `awb vault encrypt` left: keep list, originals and reports."""
    out: list[Path] = []
    keep = _gpg_path(p.keep_list)
    if _is_regular(keep):
        out.append(keep)
    for folder in (p.originals, p.private_reports):
        if folder.is_dir():
            for root, dirs, files in os.walk(folder):
                dirs.sort()
                out.extend(Path(root) / n for n in sorted(files) if n.endswith(".gpg") and _is_regular(Path(root) / n))
    return out


def encrypt_vault(p: config.Paths, passphrase: str) -> int:
    """The one-time encryption of a plain vault. Returns the number of files sealed.

    It holds the vault lock, so that no intake writes a plaintext original while it runs. A run that failed half
    way can be repeated, but only with the same passphrase: when files of the earlier run are sealed already, the
    new passphrase must open one of them, else nothing is done (the daemon could never open both kinds)."""
    _check_passphrase(passphrase)
    if len(passphrase) < MIN_NEW_PASSPHRASE:
        raise VaultError("a new passphrase needs at least %d characters" % MIN_NEW_PASSPHRASE)
    with vault_lock(p):
        if p.register_encrypted.exists():
            raise VaultError("the vault is already encrypted")
        if not _is_regular(p.register):
            raise VaultError("there is no register to encrypt; run awb init first")
        register.parse(p.register.read_bytes())
        home = _homedir(_vault_homedir(p))
        earlier = _sealed_files(p)
        if earlier:
            try:
                decrypt_bytes(earlier[0].read_bytes(), passphrase, home)
            except VaultError:
                raise VaultError("files of an earlier run are sealed with another passphrase; run it again with "
                                 "the passphrase of that run, nothing was changed") from None
        files = plaintext_files(p)
        for f in files:
            seal_path(f, passphrase, home)
        return len(files)


# --------------------------------------------------------------------------- client


def _request(sock: Path, obj: dict, *, timeout: float | None = None, limit: int = MAX_CHECK_ANSWER) -> dict:
    """One request and its answer. Every failure to talk to the daemon is VaultUnavailable. The request is plain
    ASCII JSON: a lone surrogate (a file name that is not UTF-8, an escape in hook input) travels escaped."""
    line = (json.dumps(obj) + "\n").encode("ascii")
    if len(line) > MAX_REQUEST + 1:
        raise VaultUnavailable("the request is larger than the vault daemon accepts")
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.settimeout(CLIENT_TIMEOUT if timeout is None else timeout)
    buf = bytearray()
    try:
        try:
            s.connect(str(sock))
        except PermissionError:
            raise VaultUnavailable("no vault daemon (the socket refused this user)") from None
        except TimeoutError:
            raise VaultUnavailable("the vault daemon timed out") from None
        except OSError:
            raise VaultUnavailable("no vault daemon") from None
        try:
            s.sendall(line)
            while b"\n" not in buf:
                chunk = s.recv(1 << 16)
                if not chunk:
                    raise VaultUnavailable("the vault daemon closed the connection")
                buf += chunk
                if len(buf) > limit:
                    raise VaultUnavailable("the answer of the vault daemon is too large")
        except TimeoutError:
            raise VaultUnavailable("the vault daemon timed out") from None
        except OSError:
            raise VaultUnavailable("the vault daemon closed the connection") from None
    finally:
        s.close()
    try:
        answer = json.loads(bytes(buf[:buf.index(b"\n")]).decode("utf-8"))
    except (ValueError, RecursionError):
        raise VaultUnavailable("the vault daemon answered in an unexpected form") from None
    if not isinstance(answer, dict) or not isinstance(answer.get("ok"), bool):
        raise VaultUnavailable("the vault daemon answered in an unexpected form")
    return answer


def _raise_for(answer: dict, details: bool = True) -> None:
    """Turn an error answer into an exception. Only the fixed error words pass through. On the admin side a
    value-free detail of the owner's own daemon passes too."""
    err = answer.get("error")
    word = err if isinstance(err, str) and err in ERROR_WORDS else "unexpected answer"
    if word == "locked":
        raise VaultLocked("vault locked")
    if word == "rate":
        raise VaultRateLimited("rate limit of the vault daemon reached")
    if word == "refused":
        raise VaultUnavailable("the vault daemon refused this user")
    if word == "wrong passphrase":
        raise VaultError("wrong passphrase or damaged file")
    detail = answer.get("detail")
    if details and isinstance(detail, str) and _DETAIL_RE.fullmatch(detail):
        raise VaultError("%s: %s" % (word, detail))
    raise VaultError(word)


def _encoded_size(text: str) -> int:
    return len(json.dumps(text)) + 32


def _cut_ok(text: str, i: int) -> bool:
    """True when normalising text[:i] and text[i:] apart gives the normalised text[:i] + text[i:]: the cut
    splits no line-end hyphenation, soft line break, folded encoded word, line end pair or combining sequence."""
    if i <= 0 or i >= len(text):
        return True
    nxt = text[i]
    if nxt in " \t\r\n" or unicodedata.combining(nxt) or unicodedata.category(nxt) in ("Mn", "Mc", "Me", "Cf"):
        return False
    line = text[max(0, i - 200):i - 1].rsplit("\n", 1)[-1].rstrip(" \t\r")
    return not line.endswith(("-", "=", "\u00ad")) and not re.search(r"[\u2010-\u2015\u2212]$", line)


def _safe_cut(text: str, lo: int, hi: int) -> int:
    """A cut position in [lo, hi]: after a line break where _cut_ok holds, else after a space, else hi."""
    lo = max(1, lo)
    j = hi
    while True:
        j = text.rfind("\n", lo - 1, j)
        if j < 0:
            break
        if _cut_ok(text, j + 1):
            return j + 1
    j = hi
    while True:
        j = text.rfind(" ", lo - 1, j)
        if j < 0:
            break
        if _cut_ok(text, j + 1):
            return j + 1
    return hi


def check_remote(text: str, sock: Path) -> list[dict]:
    """The name and structured data check of the vault daemon: [{start, length, cls}], positions in the
    normalised text. Raises VaultLocked or VaultUnavailable (also for an answer that carries anything else).

    A text larger than one request goes in chunks that overlap by CHECK_OVERLAP characters, cut at line ends
    where normalising the parts apart gives the same text as normalising the whole. Positions are mapped back to
    the normalised whole text; a hit that reaches the end of a chunk is taken from the next chunk, where it lies
    whole."""
    if not isinstance(text, str):
        raise TypeError("text must be a string")
    if len(text) <= CHECK_CHUNK and _encoded_size(text) <= MAX_REQUEST:
        return _check_one(text, sock)
    from awb import normalize

    found: set[tuple[int, int, str]] = set()
    start, offset, n = 0, 0, len(text)
    while True:
        size = CHECK_CHUNK
        while True:
            last = start + size >= n
            end = n if last else _safe_cut(text, start + size // 2, start + size)
            if _encoded_size(text[start:end]) <= MAX_REQUEST or size <= 2 * CHECK_OVERLAP:
                break
            size //= 2
        piece = text[start:end]
        hits = _check_one(piece, sock)
        length = None if last else len(normalize.normalize(piece).text)
        for h in hits:
            if length is not None and h["start"] + h["length"] >= length:
                continue
            found.add((offset + h["start"], h["length"], h["cls"]))
        if last:
            break
        nxt = _safe_cut(text, max(start + 1, end - 2 * CHECK_OVERLAP), max(start + 1, end - CHECK_OVERLAP))
        offset += len(normalize.normalize(text[start:nxt]).text)
        start = nxt
    return [{"start": s, "length": ln, "cls": c} for s, ln, c in sorted(found)]


def _check_one(text: str, sock: Path) -> list[dict]:
    answer = _request(Path(sock), {"op": "check", "text": text}, limit=MAX_CHECK_ANSWER)
    if not answer["ok"]:
        try:
            _raise_for(answer, details=False)
        except (VaultLocked, VaultUnavailable):
            raise
        except VaultError as err:
            raise VaultUnavailable("the vault daemon cannot check (%s)" % err) from None
    if set(answer) != {"ok", "hits"} or not isinstance(answer["hits"], list):
        raise VaultUnavailable("the vault daemon answered in an unexpected form")
    out: list[dict] = []
    for h in answer["hits"]:
        if not isinstance(h, dict) or set(h) != HIT_KEYS:
            raise VaultUnavailable("the vault daemon answered in an unexpected form")
        start, length, cls = h["start"], h["length"], h["cls"]
        if type(start) is not int or type(length) is not int or start < 0 or length < 0 or cls not in HIT_CLASSES:
            raise VaultUnavailable("the vault daemon answered in an unexpected form")
        out.append({"start": start, "length": length, "cls": cls})
    return out


def ping(sock: Path) -> str:
    """The state of the daemon behind a socket: plain, locked or unlocked. Raises VaultUnavailable."""
    answer = _request(Path(sock), {"op": "ping"}, timeout=10.0)
    if not answer["ok"]:
        try:
            _raise_for(answer, details=False)
        except VaultUnavailable:
            raise
        except VaultError as err:
            raise VaultUnavailable("the vault daemon cannot answer (%s)" % err) from None
    state = answer.get("state")
    if state not in ("plain", "locked", "unlocked"):
        raise VaultUnavailable("the vault daemon answered in an unexpected form")
    return state


def admin_call(op: str, sock: Path | None = None, **fields) -> dict:
    """One request on the admin socket (default: the configured one). Returns the answer when it is ok, else
    raises VaultLocked, VaultUnavailable or VaultError with a value-free message."""
    sock = Path(sock) if sock is not None else config.paths().admin_sock
    answer = _request(sock, dict(fields, op=op), limit=MAX_ADMIN_ANSWER)
    if not answer["ok"]:
        _raise_for(answer)
    return answer


# --------------------------------------------------------------------------- daemon


class _Refused(Exception):
    """An error answer: a fixed word and an optional value-free detail."""

    def __init__(self, word: str, detail: str | None = None):
        super().__init__(word)
        self.word = word
        self.detail = detail


def _error(word: str, detail: str | None = None) -> dict:
    out = {"ok": False, "error": word}
    if detail:
        out["detail"] = detail
    return out


def _peer_uid(conn: socket.socket) -> int:
    raw = conn.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize("3i"))
    return struct.unpack("3i", raw)[1]


def _answers(path: Path) -> bool:
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.settimeout(1.0)
    try:
        s.connect(str(path))
        return True
    except OSError:
        return False
    finally:
        s.close()


def _bind(path: Path, mode: int, folder_mode: int | None) -> tuple[socket.socket, tuple[int, int]]:
    """A listening socket at `path` with `mode`, created in a private folder and renamed into place so that
    nobody can connect before the mode is set. Returns the socket and the (device, inode) of its file."""
    if len(str(path)) > MAX_SOCKET_PATH:
        raise VaultError("a socket path is longer than %d characters" % MAX_SOCKET_PATH)
    parent = path.parent
    if folder_mode is not None and not parent.exists():
        parent.mkdir(parents=True, mode=folder_mode)
        os.chmod(parent, folder_mode)
    if _answers(path):
        raise VaultError("another vault daemon answers on a socket")
    try:
        if not stat.S_ISSOCK(os.lstat(path).st_mode):
            raise VaultError("a socket path is taken by a file that is not a socket")
    except FileNotFoundError:
        pass
    srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    tmpdir = None
    try:
        tmpdir = tempfile.mkdtemp(prefix=".awb", dir=parent)
        tmp = os.path.join(tmpdir, "s")
        if len(tmp) < 104:
            srv.bind(tmp)
            os.chmod(tmp, mode)
            try:
                os.chown(tmp, -1, os.getegid())
            except OSError:
                pass
            os.replace(tmp, path)
        else:
            try:
                os.unlink(path)
            except FileNotFoundError:
                pass
            srv.bind(str(path))
            os.chmod(path, mode)
        srv.listen(16)
        srv.settimeout(0.2)
        st = os.lstat(path)
        return srv, (st.st_dev, st.st_ino)
    except BaseException:
        srv.close()
        raise
    finally:
        if tmpdir is not None:
            for leftover in (os.path.join(tmpdir, "s"), tmpdir):
                try:
                    (os.rmdir if leftover == tmpdir else os.unlink)(leftover)
                except OSError:
                    pass


class Daemon:
    """The vault daemon. `start()` binds both sockets and serves them in threads, `stop()` ends every thread,
    removes the sockets and drops the passphrase. `serve_forever()` runs until `stopping` is set."""

    def __init__(self, p: config.Paths | None = None, *, admin_sock: Path | None = None,
                 check_sock: Path | None = None, rate_limit: int = RATE_LIMIT, rate_window: float = RATE_WINDOW):
        self.p = p if p is not None else config.paths()
        self.admin_path = Path(admin_sock) if admin_sock is not None else self.p.admin_sock
        self.check_path = Path(check_sock) if check_sock is not None else self.p.check_socket
        self.rate_limit = rate_limit
        self.rate_window = rate_window
        self.stopping = threading.Event()
        self._lock = threading.RLock()
        self._log_lock = threading.Lock()
        self._rate_lock = threading.Lock()
        self._scan_slots = threading.BoundedSemaphore(MAX_CONCURRENT_SCANS)
        self._conn_lock = threading.Lock()
        self._passphrase: str | None = None
        self._entries: list[register.Entry] | None = None
        self._matcher: Matcher | None = None
        self._plain: tuple | None = None
        self._rate: dict[int, deque] = {}
        self._servers: list[tuple[socket.socket, Path, tuple[int, int]]] = []
        self._threads: list[threading.Thread] = []
        self._conns: dict[socket.socket, threading.Thread] = {}
        self._conn_info: dict[socket.socket, tuple[bool, int]] = {}   # (admin, peer uid)
        self._log_warned = False
        self._started = False

    # ----------------------------------------------------------------- life cycle

    @property
    def homedir(self) -> Path:
        return _vault_homedir(self.p)

    def start(self) -> None:
        if self._started:
            return
        self.stopping.clear()
        try:
            for path, mode, folder_mode, admin in ((self.admin_path, 0o600, None, True),
                                                   (self.check_path, 0o660, 0o750, False)):
                srv, ident = _bind(path, mode, folder_mode)
                self._servers.append((srv, path, ident))
                t = threading.Thread(target=self._accept_loop, args=(srv, admin), daemon=True,
                                     name="awb-vault-%s" % ("admin" if admin else "check"))
                self._threads.append(t)
        except BaseException:
            self._close_servers()
            raise
        self._started = True
        for t in self._threads:
            t.start()

    def serve_forever(self) -> None:
        self.start()
        try:
            while not self.stopping.wait(0.5):
                pass
        finally:
            self.stop()

    def stop(self) -> None:
        self.stopping.set()
        for t in self._threads:
            t.join(timeout=5)
        self._threads = []
        self._close_servers()
        with self._conn_lock:
            conns = list(self._conns.items())
        for conn, _ in conns:
            try:
                conn.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
        for _, t in conns:
            t.join(timeout=5)
        with self._lock:
            self._forget()
        self._started = False

    def _close_servers(self) -> None:
        for srv, path, ident in self._servers:
            srv.close()
            try:
                st = os.lstat(path)
                if (st.st_dev, st.st_ino) == ident:
                    os.unlink(path)
            except OSError:
                pass
        self._servers = []

    # ----------------------------------------------------------------- connections

    def _accept_loop(self, srv: socket.socket, admin: bool) -> None:
        while not self.stopping.is_set():
            try:
                conn, _ = srv.accept()
            except TimeoutError:
                continue
            except OSError:
                if self.stopping.is_set():
                    break
                time.sleep(0.05)
                continue
            try:
                uid = _peer_uid(conn)
            except OSError:
                conn.close()
                continue
            with self._conn_lock:
                if self.stopping.is_set() or not self._room(admin, uid):
                    conn.close()
                    continue
                t = threading.Thread(target=self._serve_conn, args=(conn, admin), daemon=True,
                                     name="awb-vault-conn")
                self._conns[conn] = t
                self._conn_info[conn] = (admin, uid)
            t.start()

    def _room(self, admin: bool, uid: int) -> bool:
        """Room for one more connection: each socket counts its own, the check socket also per peer uid. Called
        with the connection lock held."""
        same = [u for a, u in self._conn_info.values() if a == admin]
        if admin:
            return len(same) < MAX_ADMIN_CONNECTIONS
        return len(same) < MAX_CONNECTIONS and same.count(uid) < MAX_CONNECTIONS_PER_UID

    def _serve_conn(self, conn: socket.socket, admin: bool) -> None:
        try:
            uid = _peer_uid(conn)
            if admin and uid != os.getuid():
                conn.sendall(json.dumps(_error("refused")).encode() + b"\n")
                return
            buf = bytearray()
            end_of_life = time.monotonic() + MAX_CONNECTION_SECONDS
            deadline = min(time.monotonic() + READ_TIMEOUT, end_of_life)
            while not self.stopping.is_set():
                nl = buf.find(b"\n")
                if nl < 0:
                    if len(buf) > MAX_REQUEST:
                        conn.sendall(json.dumps(_error("too large")).encode() + b"\n")
                        return
                    left = deadline - time.monotonic()
                    if left <= 0:
                        return
                    conn.settimeout(left)
                    chunk = conn.recv(1 << 16)
                    if not chunk:
                        return
                    buf += chunk
                    continue
                line = bytes(buf[:nl])
                del buf[:nl + 1]
                if len(line) > MAX_REQUEST:
                    conn.sendall(json.dumps(_error("too large")).encode() + b"\n")
                    return
                answer = self._answer(line, admin, uid)
                conn.settimeout(READ_TIMEOUT)
                conn.sendall(json.dumps(answer, ensure_ascii=False).encode("utf-8") + b"\n")
                if answer.get("error") == "rate":
                    return   # a client over its budget gets its answer and loses the connection
                deadline = min(time.monotonic() + READ_TIMEOUT, end_of_life)
        except OSError:
            pass
        finally:
            try:
                conn.close()
            except OSError:
                pass
            with self._conn_lock:
                self._conns.pop(conn, None)
                self._conn_info.pop(conn, None)

    def _answer(self, line: bytes, admin: bool, uid: int) -> dict:
        allowed = admin or self._allow(uid, len(line))
        try:
            req = json.loads(line.decode("utf-8"))
        except (ValueError, RecursionError):
            req = None
        if not isinstance(req, dict) or not isinstance(req.get("op"), str):
            return _error("bad request" if allowed else "rate")
        op = req["op"]
        if not allowed:
            if op == "check":
                text = req.get("text")
                self._log(uid, len(text) if isinstance(text, str) else None, None, "rate")
            return _error("rate")
        handler = (self._ADMIN_OPS if admin else self._CHECK_OPS).get(op)
        if handler is None:
            return _error("unknown op")
        try:
            return handler(self, req, uid)
        except _Refused as r:
            return _error(r.word, r.detail)
        except VaultError:
            return _error("failed")
        except OSError as err:
            return _error("failed", "%s (operating system error)" % type(err).__name__)
        except Exception as err:   # never a traceback with a value; the daemon keeps serving
            return _error("failed", "%s (internal error)" % type(err).__name__)

    def _allow(self, uid: int, size: int = 0) -> bool:
        """One request of `size` bytes: within the count per window and within the bytes per window."""
        now = time.monotonic()
        with self._rate_lock:
            q = self._rate.setdefault(uid, deque())
            while q and q[0][0] <= now - self.rate_window:
                q.popleft()
            if len(q) >= self.rate_limit or sum(n for _, n in q) + size > RATE_BYTES:
                return False
            q.append((now, size))
            return True

    def _log(self, uid: int, length: int | None, hits: int | None, result: str) -> None:
        now = datetime.now(timezone.utc)
        f = self.p.check_log / ("checks-%s.tsv" % now.strftime("%Y-%m"))
        line = "%s\t%d\t%s\t%s\t%s\n" % (now.strftime("%Y-%m-%dT%H:%M:%SZ"), uid,
                                         "-" if length is None else str(length),
                                         "-" if hits is None else str(hits), result)
        with self._log_lock:
            try:
                self.p.check_log.mkdir(mode=0o700, parents=True, exist_ok=True)
                fd = os.open(f, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
                try:
                    if os.fstat(fd).st_size == 0:
                        os.write(fd, CHECK_LOG_HEADER.encode("ascii"))
                    os.write(fd, line.encode("ascii"))
                finally:
                    os.close(fd)
            except OSError as err:
                if not self._log_warned:
                    self._log_warned = True
                    print("awb vault: the check log cannot be written (%s)" % type(err).__name__, file=sys.stderr)

    # ----------------------------------------------------------------- register state

    def _forget(self) -> None:
        """Drop the passphrase and every copy of the register: the decrypted one and the cached plain one."""
        self._passphrase = None
        self._entries = None
        self._matcher = None
        self._plain = None

    def state(self) -> str:
        with self._lock:
            return self._state()

    def _state(self) -> str:
        if _is_regular(self.p.register_encrypted):
            # the vault is encrypted: a copy of the plain register from before must not outlive it
            self._plain = None
            return "unlocked" if self._passphrase is not None else "locked"
        if self._passphrase is not None:
            self._forget()
        return "plain"

    def _plain_register(self) -> tuple[list[register.Entry], Matcher]:
        path = self.p.register
        try:
            st = os.stat(path)
        except FileNotFoundError:
            raise _Refused("no register") from None
        key = (st.st_dev, st.st_ino, st.st_mtime_ns, st.st_size)
        if self._plain is not None and self._plain[0] == key:
            return self._plain[1], self._plain[2]
        try:
            entries = register.parse(path.read_bytes())
        except register.RegisterError:
            raise _Refused("register invalid") from None
        m = Matcher(register.forms_for_matching(entries))
        self._plain = (key, entries, m)
        return entries, m

    def _current(self) -> tuple[str, list[register.Entry] | None, Matcher | None]:
        with self._lock:
            state = self._state()
            if state == "unlocked":
                return state, self._entries, self._matcher
            if state == "locked":
                return state, None, None
            entries, m = self._plain_register()
            return state, entries, m

    def _need_unlocked(self) -> str:
        state = self._state()
        if state == "locked":
            raise _Refused("locked")
        if state == "plain":
            raise _Refused("not encrypted")
        return self._passphrase

    def _vault_file(self, raw, *, encrypted: bool) -> Path:
        if not isinstance(raw, str) or not raw or "\x00" in raw:
            raise _Refused("bad request")
        given = Path(raw)
        if not given.is_absolute():
            raise _Refused("bad request", "the path must be absolute")
        vault = self.p.vault.resolve()
        try:
            real = given.resolve(strict=True)
        except (OSError, RuntimeError):
            raise _Refused("no such file") from None
        if vault not in real.parents or real != Path(os.path.abspath(given)) or not _is_regular(real):
            raise _Refused("not allowed", "only a regular file inside the vault, no link")
        if real.name.endswith(".gpg") != encrypted:
            raise _Refused("not allowed", "open_file takes a .gpg file, seal_file a plaintext file")
        if real in (self.p.register.resolve(), self.p.register_encrypted.resolve()):
            raise _Refused("not allowed", "the register goes through register_load and register_save only")
        if self.homedir.resolve() in real.parents:
            raise _Refused("not allowed", "only a regular file inside the vault, no link")
        return real

    # ----------------------------------------------------------------- check socket ops

    def _op_ping(self, req: dict, uid: int) -> dict:
        return {"ok": True, "state": self.state()}

    def _op_check(self, req: dict, uid: int) -> dict:
        text = req.get("text")
        if not isinstance(text, str):
            self._log(uid, None, None, "bad request")
            return _error("bad request")
        try:
            state, _, matcher = self._current()
        except _Refused as r:
            self._log(uid, len(text), None, r.word.replace(" ", "-"))
            raise
        if state == "locked":
            self._log(uid, len(text), None, "locked")
            return _error("locked")
        if len(text) > MAX_CHECK_CHARS:
            self._log(uid, len(text), None, "too-long")
            return _error("bad request", "the text is longer than one check takes, send it in pieces")
        if not self._scan_slots.acquire(timeout=SCAN_WAIT):
            self._log(uid, len(text), None, "busy")
            return _error("rate")
        try:
            hits = check._scan(text, matcher)
        finally:
            self._scan_slots.release()
        self._log(uid, len(text), len(hits), "ok")
        return {"ok": True, "hits": [{"start": h["start"], "length": h["length"], "cls": h["cls"]} for h in hits]}

    # ----------------------------------------------------------------- admin socket ops

    def _op_unlock(self, req: dict, uid: int) -> dict:
        pw = req.get("passphrase")
        try:
            pw = _check_passphrase(pw)
        except VaultError:
            raise _Refused("bad request", "the passphrase must be one non-empty line") from None
        with self._lock:
            if not _is_regular(self.p.register_encrypted):
                raise _Refused("not encrypted")
            try:
                data = decrypt_bytes(self.p.register_encrypted.read_bytes(), pw, self.homedir)
            except VaultError:
                raise _Refused("wrong passphrase") from None
            try:
                entries = register.parse(data)
            except register.RegisterError as err:
                raise _Refused("invalid", str(err)) from None
            self._passphrase = pw
            self._entries = entries
            self._matcher = Matcher(register.forms_for_matching(entries))
            return {"ok": True, "state": "unlocked"}

    def _op_lock(self, req: dict, uid: int) -> dict:
        with self._lock:
            self._forget()
            return {"ok": True, "state": self._state()}

    def _op_status(self, req: dict, uid: int) -> dict:
        with self._lock:
            state = self._state()
            out = {"ok": True, "state": state}
            entries = None
            if state == "unlocked":
                entries = self._entries
            elif state == "plain":
                try:
                    entries = self._plain_register()[0]
                except _Refused as r:
                    out["register"] = r.word
            if entries is not None:
                out["codes"] = len(register.codes(entries))
                out["forms"] = len(entries)
            if state != "plain":
                out["plaintext_register"] = self.p.register.exists()
            return out

    def _op_register_load(self, req: dict, uid: int) -> dict:
        state, entries, _ = self._current()
        if state == "locked":
            raise _Refused("locked")
        return {"ok": True, "text": register.render(entries)}

    def _op_register_save(self, req: dict, uid: int) -> dict:
        text = req.get("text")
        if not isinstance(text, str):
            raise _Refused("bad request")
        try:
            entries = register.parse(text)
            canonical = register.render(entries)
        except register.RegisterError as err:
            raise _Refused("invalid", str(err)) from None
        with self._lock:
            state = self._state()
            if state == "locked":
                raise _Refused("locked")
            if state == "unlocked":
                current = self._entries or []
            else:
                try:
                    current = self._plain_register()[0]
                except _Refused as r:
                    if r.word != "no register":
                        raise
                    current = []
            dropped = {(e.code, e.form) for e in current} - {(e.code, e.form) for e in entries}
            if dropped:
                raise _Refused("invalid", "the new register drops %d forms of the current one; forms are retired, "
                                          "never removed" % len(dropped))
            if state == "unlocked":
                data = canonical.encode("utf-8")
                enc = encrypt_bytes(data, self._passphrase, self.homedir)
                if decrypt_bytes(enc, self._passphrase, self.homedir) != data:
                    raise _Refused("failed", "the encrypted register does not decrypt, nothing was written")
                _write_atomic(self.p.register_encrypted, enc)
                self._entries = entries
                self._matcher = Matcher(register.forms_for_matching(entries))
            else:
                register.save(self.p.register, entries)
                self._plain = None
            return {"ok": True, "codes": len(register.codes(entries)), "forms": len(entries)}

    def _op_seal_file(self, req: dict, uid: int) -> dict:
        with self._lock:
            pw = self._need_unlocked()
            path = self._vault_file(req.get("path"), encrypted=False)
            seal_path(path, pw, self.homedir)
            return {"ok": True}

    def _op_open_file(self, req: dict, uid: int) -> dict:
        with self._lock:
            pw = self._need_unlocked()
            path = self._vault_file(req.get("path"), encrypted=True)
            try:
                data = decrypt_bytes(path.read_bytes(), pw, self.homedir)
            except VaultError:
                raise _Refused("failed", "the file does not decrypt with the passphrase in memory") from None
            return {"ok": True, "data": base64.b64encode(data).decode("ascii")}

    _CHECK_OPS = {"ping": _op_ping, "check": _op_check}
    _ADMIN_OPS = {"ping": _op_ping, "unlock": _op_unlock, "lock": _op_lock, "status": _op_status,
                  "register_load": _op_register_load, "register_save": _op_register_save,
                  "seal_file": _op_seal_file, "open_file": _op_open_file}


# --------------------------------------------------------------------------- command line


def _read_secret(args, prompt: str) -> str:
    if getattr(args, "stdin", False):
        line = sys.stdin.readline()
        return line[:-1] if line.endswith("\n") else line
    return getpass.getpass(prompt)


def _cmd_serve(args, p: config.Paths) -> int:
    d = Daemon(p, admin_sock=args.admin_socket, check_sock=args.check_socket)
    d.start()
    if threading.current_thread() is threading.main_thread():
        for sig in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
            signal.signal(sig, lambda *_: d.stopping.set())
    print("awb vault: serving, state %s" % d.state(), file=sys.stderr, flush=True)
    d.serve_forever()
    print("awb vault: stopped", file=sys.stderr, flush=True)
    return 0


def _cmd_unlock(args, p: config.Paths) -> int:
    pw = _read_secret(args, "vault passphrase: ")
    answer = admin_call("unlock", p.admin_sock, passphrase=pw)
    print("vault %s" % answer.get("state", "unlocked"))
    return 0


def _cmd_lock(args, p: config.Paths) -> int:
    answer = admin_call("lock", p.admin_sock)
    print("vault %s" % answer.get("state", "locked"))
    return 0


def _cmd_status(args, p: config.Paths) -> int:
    answer = admin_call("status", p.admin_sock)
    print("state: %s" % answer.get("state"))
    for key in ("codes", "forms"):
        if isinstance(answer.get(key), int):
            print("%s: %d" % (key, answer[key]))
    if answer.get("register"):
        print("register: %s" % answer["register"])
    if answer.get("plaintext_register"):
        print("warning: a plaintext register.tsv lies next to the encrypted register")
    return 0


def _cmd_encrypt(args, p: config.Paths) -> int:
    first = _read_secret(args, "new vault passphrase: ")
    second = _read_secret(args, "the same passphrase again: ")
    if first != second:
        raise VaultError("the two passphrases differ, nothing was encrypted")
    n = encrypt_vault(p, first)
    print("encrypted %d files, the plaintext is removed" % n)
    try:
        admin_call("unlock", p.admin_sock, passphrase=first)
    except VaultUnavailable:
        print("no vault daemon answers; run awb vault unlock once it runs")
        return 0
    print("vault unlocked")
    return 0


def in_assistant_session() -> bool:
    """True inside a Claude Code session (its shell carries CLAUDECODE or CLAUDE_CODE_ENTRYPOINT)."""
    return bool(os.environ.get("CLAUDECODE") or os.environ.get("CLAUDE_CODE_ENTRYPOINT"))


def _cmd_show(args, p: config.Paths) -> int:
    """A sealed file of the vault (a private intake report) on the owner's own terminal. Refused inside an
    assistant session, to a pipe or a file and as the work user: what it prints carries names."""
    if config.is_work_user():
        print("awb vault: show is for the owner", file=sys.stderr)
        return 2
    if in_assistant_session():
        print("awb vault: show never runs inside an assistant session; run it in your own terminal", file=sys.stderr)
        return 2
    if not sys.stdout.isatty():
        print("awb vault: show prints to a terminal only, never to a pipe or a file", file=sys.stderr)
        return 2
    answer = admin_call("open_file", path=str(Path(args.file).expanduser().resolve()))
    try:
        text = base64.b64decode(answer.get("data") or "", validate=True).decode("utf-8")
    except (ValueError, UnicodeDecodeError):
        raise VaultError("the vault daemon answered in an unexpected form") from None
    sys.stdout.write(text if text.endswith("\n") else text + "\n")
    return 0


def main(argv: list[str] | None = None) -> int:
    """`awb vault serve|unlock|lock|status|encrypt|show`. Exit 0 ok, 2 on an error."""
    from awb.cli import SafeParser

    ap = SafeParser(prog="awb vault", description="The vault: encryption at rest and the vault daemon.")
    sub = ap.add_subparsers(dest="command", parser_class=SafeParser)
    s = sub.add_parser("serve", help="run the vault daemon (the owner, under systemd)")
    s.add_argument("--admin-socket", type=Path, default=None)
    s.add_argument("--check-socket", type=Path, default=None)
    s.set_defaults(func=_cmd_serve)
    s = sub.add_parser("unlock", help="give the passphrase to the daemon")
    s.add_argument("--stdin", action="store_true", help="read the passphrase from standard input")
    s.set_defaults(func=_cmd_unlock)
    s = sub.add_parser("lock", help="drop the passphrase and the register from the daemon's memory")
    s.set_defaults(func=_cmd_lock)
    s = sub.add_parser("status", help="state, count of codes and forms")
    s.set_defaults(func=_cmd_status)
    s = sub.add_parser("show", help="a sealed file of the vault, such as a private intake report, on your terminal")
    s.add_argument("file")
    s.set_defaults(func=_cmd_show)
    s = sub.add_parser("encrypt", help="encrypt a plain vault once, then unlock the daemon")
    s.add_argument("--stdin", action="store_true", help="read the new passphrase twice from standard input")
    s.set_defaults(func=_cmd_encrypt)
    try:
        args = ap.parse_args(argv)
    except SystemExit as exc:
        return exc.code if isinstance(exc.code, int) else 2
    func = getattr(args, "func", None)
    if func is None:
        ap.print_usage(sys.stderr)
        return 2
    try:
        return func(args, config.paths())
    except (VaultError, register.RegisterError) as err:
        print("awb vault: %s" % err, file=sys.stderr)
        return 2
    except OSError as err:
        print("awb vault: %s (operating system error)" % type(err).__name__, file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
