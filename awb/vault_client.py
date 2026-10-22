"""The client of the vault daemon: the check, the ping and the admin call, with the errors a caller catches.

A part of the vault (awb/vault.py, F1 step 7). A caller of the check socket or the admin socket needs this module
and nothing of the daemon: `check_remote` sends a text in overlapping chunks and maps the hits back, `ping` and
`ping_state` read the state, `admin_call` makes one request on the admin socket. Only the fixed error words of
ERROR_WORDS reach a caller (VaultError and its kinds). awb/vault.py describes the wire format; every name of this
module is still reached through it (awb/parts.py).
"""
from __future__ import annotations

import json
import re
import socket
import unicodedata
from pathlib import Path

from awb import codes, config

MAX_REQUEST = 4 * 1024 * 1024
"""Largest request line the daemon reads (bytes, without the line end)."""

CHECK_CHUNK = 600_000
"""Characters of text per check request. The request stays under MAX_REQUEST even when every character is
escaped; a larger text goes in several requests (see check_remote)."""
CHECK_OVERLAP = 20_000
"""Characters two neighbouring chunks share, so that a hit across a cut is found whole in the next chunk."""

CLIENT_TIMEOUT = 60.0
MAX_CHECK_ANSWER = 64 * 1024 * 1024
MAX_ADMIN_ANSWER = 1024 * 1024 * 1024
SINCE_FORMAT = "%Y-%m-%dT%H:%M:%SZ"
"""The one shape of the lock time on the wire: UTC, whole seconds, a Z."""
SINCE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")
"""A client takes `since` only in this shape: it is the one daemon supplied text that lands in the output of a
session and on the board (T3)."""

HIT_KEYS = frozenset(("start", "length", "cls"))
HIT_CLASSES = frozenset(("name",) + tuple(k.lower() for k in codes.DATA_KINDS))

ERROR_WORDS = frozenset(("locked", "rate", "refused", "wrong passphrase", "bad request", "unknown op", "too large",
                         "no register", "register invalid", "not encrypted", "invalid", "failed", "no such file",
                         "not allowed", "busy"))
"""The only error words a client repeats. Anything else is an unexpected answer."""
_DETAIL_RE = re.compile(r"[A-Za-z0-9 ,;:.'()-]{1,200}")


class VaultError(Exception):
    """A vault problem. Messages carry no passphrase, no form, no code and no path."""


class VaultLocked(VaultError):
    """The register is encrypted and the daemon holds no passphrase. `since` is the time the daemon became locked
    as the daemon said it (SINCE_FORMAT), None when it did not say or said something of another shape."""

    def __init__(self, message: str = "vault locked", since: str | None = None):
        super().__init__(message)
        self.since = since_or_none(since)


class VaultUnavailable(VaultError):
    """No daemon, the socket is missing or refused, a timeout or an answer of an unexpected form."""


class VaultRateLimited(VaultUnavailable):
    """The daemon refused the request: too many requests of this user in the last minute."""


class VaultBusy(VaultError):
    """The daemon hands itself over to a new process (a reload) and takes no write for these seconds."""

    def __init__(self, message: str = "the vault daemon is reloading, try again"):
        super().__init__(message)


def since_or_none(value) -> str | None:
    """`value` when it is a lock time in SINCE_FORMAT, else None."""
    return value if isinstance(value, str) and SINCE_RE.fullmatch(value) else None


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
        raise VaultLocked("vault locked", answer.get("since"))
    if word == "rate":
        raise VaultRateLimited("rate limit of the vault daemon reached")
    if word == "refused":
        raise VaultUnavailable("the vault daemon refused this user")
    if word == "wrong passphrase":
        raise VaultError("wrong passphrase or damaged file")
    if word == "busy":
        raise VaultBusy()
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


def check_remote(text: str, sock: Path, timeout: float | None = None) -> list[dict]:
    """The name and structured data check of the vault daemon: [{start, length, cls}], positions in the
    normalised text. Raises VaultLocked or VaultUnavailable (also for an answer that carries anything else).

    A text larger than one request goes in chunks that overlap by CHECK_OVERLAP characters, cut at line ends
    where normalising the parts apart gives the same text as normalising the whole. Positions are mapped back to
    the normalised whole text; a hit that reaches the end of a chunk is taken from the next chunk, where it lies
    whole."""
    if not isinstance(text, str):
        raise TypeError("text must be a string")
    if len(text) <= CHECK_CHUNK and _encoded_size(text) <= MAX_REQUEST:
        return _check_one(text, sock, timeout)
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
        hits = _check_one(piece, sock, timeout)
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


def _check_one(text: str, sock: Path, timeout: float | None = None) -> list[dict]:
    answer = _request(Path(sock), {"op": "check", "text": text}, timeout=timeout, limit=MAX_CHECK_ANSWER)
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


RECORD_TIMEOUT = 5.0
"""Seconds a hook waits for the daemon to take a record of a block (awb/fplearn.py); the block stands either way."""


def record_remote(text: str, sock: Path, source: str, extra: dict | None = None,
                  timeout: float = RECORD_TIMEOUT) -> bool:
    """Hand one block to the vault daemon for the learning (awb/fplearn.py): a check request with `record`, which the
    daemon scans again and writes on the owner's side. True when the daemon took it. Never raises."""
    try:
        answer = _request(Path(sock), {"op": "check", "text": text, "record": source, "extra": extra or {}},
                          timeout=timeout, limit=MAX_CHECK_ANSWER)
    except VaultError:
        return False
    return bool(answer.get("ok"))


def ping(sock: Path, timeout: float = 10.0) -> str:
    """The state of the daemon behind a socket: plain, locked or unlocked. Raises VaultUnavailable. Extra keys
    of the answer are left alone, so an older client works with a newer daemon."""
    return ping_state(sock, timeout)["state"]


def ping_state(sock: Path, timeout: float = 10.0) -> dict:
    """{"state": plain, locked or unlocked, "since": the lock time in SINCE_FORMAT or None} of the daemon behind a
    socket, for the board, the portal and `awb vault status`. `since` is None unless the daemon is locked and
    said since when in the fixed shape. Raises VaultUnavailable."""
    answer = _request(Path(sock), {"op": "ping"}, timeout=timeout)
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
    return {"state": state, "since": since_or_none(answer.get("since")) if state == "locked" else None}


def locked_line(since: str | None) -> str:
    """"locked since <time>" for the board, the portal and `awb vault status`; "since unknown" without a time."""
    return "locked since %s" % (since_or_none(since) or "unknown")


def admin_call(op: str, sock: Path | None = None, *, timeout: float | None = None, **fields) -> dict:
    """One request on the admin socket (default: the configured one). Returns the answer when it is ok, else
    raises VaultLocked, VaultBusy, VaultUnavailable or VaultError with a value-free message."""
    sock = Path(sock) if sock is not None else config.paths().admin_sock
    answer = _request(sock, dict(fields, op=op), timeout=timeout, limit=MAX_ADMIN_ANSWER)
    if not answer["ok"]:
        _raise_for(answer)
    return answer
