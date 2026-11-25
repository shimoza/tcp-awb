"""Commands, jobs and files of a project session on a Windows lab machine (task WL part 3 of 4, B-037).

    awb winlab run NAME [--vm VM] [--timeout S] -- POWERSHELL    run a command, print its output
    awb winlab start NAME [--vm VM] -- POWERSHELL                a long job as a scheduled task, prints its job id
    awb winlab status JOB                                        running, ended with its exit code or offline
    awb winlab log JOB                                           the new part of the job's log
    awb winlab stop JOB                                          end the job
    awb winlab put FILE WINPATH                                  a file of winlab-out/ to the machine
    awb winlab get WINPATH FILE                                  a file of the machine into the project

They run in the folder of a project (its SCOPE.md) and each first asks `winlab.allowed(NAME, CODE)` of part 1: the
project must hold the open window of the machine. A job remembers its machine in notes/winlab/jobs/<JOB>.json.

The machine is reached through a Transport (below): run(text) -> (exit code, stdout, stderr), put_chunk, get_chunk
and stat. Every method raises Offline when the machine cannot be reached or the connection drops. `connect` gives
the BrokerTransport of part 4 (B-038): each call goes to the lab broker (awb/tcp/winbroker.py, run by root), which
holds the key, checks the window again and speaks SSH to the machine; the session sends the key token of its folder
and never holds a key. Without the broker's socket `connect` refuses ("no transport") and nothing is sent; the
tests put a fake in its place. On a sealed host the session cannot read the owner's registry: the window check
(`_allowed`) and the machine of a put or get (`_pick`) then ask the broker.

What goes out. The PowerShell text of the session passes four checks before it goes out (a refusal sends
nothing): no `{{secret:NAME}}` reference (nothing is filled in for a lab machine), nothing that names the machine's
own state (awb-winlab, the ssh folder of ProgramData, administrators_authorized_keys), the name check (a registered
name refuses) and the secret check (the gate's secret classes, the passwords, keys and tokens of awb/labsecrets.py
and a password typed into ConvertTo-SecureString or a -Password argument). No key of the cloud and no password of
the session goes to the machine. A VM guest password never passes through the session either: `--vm VM` runs the
command in the VM with PowerShell Direct and the machine side reads the credential from
%ProgramData%\\awb-winlab\\vm\\<VM>.txt (first line the user, second the password; refused unless only
Administrators and SYSTEM may read it).

What comes back. powershell.exe over SSH writes CLIXML progress records (`#< CLIXML`, `<Objs ...>`): they are
stripped and the text of an error or warning record is kept. Output lands by the rules of `awb paste --own`
(paste.own_text): ids stay, secrets are shortened or replaced, a registered name withholds the output (not printed,
not logged). Every command, its output, every job and every transfer is logged in notes/winlab/ of the project:
<date>-<machine>.log, a job's log in jobs/<JOB>.log. A refusal is logged with its reason, never the text.

Files. put takes a regular file of the project's winlab-out/ only (no link, no hard link, no file of input/, the
vault or the shared folders) and passes it through the name check and the secret check first. A disk image built in
the lab (`disk_image`: .vhdx, .vhd, .vmdk, .qcow2, .iso with the signature of its format, a raw .img of whole
sectors) is our own output and gets the secret check only, at transfer speed (`_image_secret_count`; the owner's
decision of 2026-11-06, B-040); customer material is checked once at its entry (awb intake). get writes into the
project, never into input/ or .git, never over a file. Both go in chunks to WINPATH.awb-part or FILE.awb-part and
resume after a break: the same command again goes on from the size of the part when the record of the transfer
(notes/winlab/transfers/) still matches the file. At the end the SHA-256 of both sides is compared; a mismatch
deletes the part and fails. Neither overwrites a file on the other side. Built for 12 GB: nothing is held whole in
memory, three breaks in a row are taken again after a pause before the command gives up.

Standard library only. Nothing here prints a secret or a text a check refused.
"""
from __future__ import annotations

import argparse
import base64
import datetime
import fcntl
import functools
import hashlib
import html
import inspect
import json
import os
import re
import secrets
import shutil
import socket
import stat
import sys
import tempfile
import time
from pathlib import Path

from awb import codes, winlab
from awb.winlab import WinlabError

LOG_FOLDER = Path("notes") / "winlab"
OUT_FOLDER = "winlab-out"
PART = ".awb-part"
TASK_PATH = "\\awb-winlab\\"
JOB_RE = re.compile(r"^wl-[0-9]{8}-[0-9]{6}-[0-9a-f]{4}$")
VM_RE = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9 ._-]{0,62}[A-Za-z0-9._-])?$")
_SEG = r"[^\\/:*?\"<>|%$`'~\x00-\x1f]+"
WINPATH_RE = re.compile(r"^[A-Za-z]:\\(?:%s\\)*%s$" % (_SEG, _SEG))
WINPATH_MAX = 240
CHUNK = 4 * 1024 * 1024
RETRIES = 3
RUN_TIMEOUT = 300
CHECK_WHOLE_MAX = 64 * 1024 * 1024
"""The largest file the name check reads whole (it opens office files and archives); a larger one goes in pieces."""
CHECK_READ_MAX = 2 * 1024 ** 3
"""The largest disk image the whole name check reads: it reads the file into memory, and a 21.9 GB image of tcp-bytg
(2026-11-19) ended in Unavailable. A larger image gets the name check per piece of its bytes, which resumes."""
PIECE = 4 * 1024 * 1024
OVERLAP = 512
RUNS_MIN = 256 * 1024 ** 2
"""A disk image over this size that the whole check does not read gets the name check over the unique text runs of
its bytes (_TextRuns) instead of over every piece: a piece cost the byte budget of the vault daemon (16 MB a minute
per uid), and 444 MB of a 21.9 GB image of tcp-bytg took 70 minutes (2026-11-20)."""
RUN_MIN = 3
"""Characters a text run needs besides its spaces to be checked: a registered form has three or more."""
RUN_OVERLAP = 4096
"""Bytes of the piece before that are read again with the next one, so a run across the cut is seen whole."""
RUN_BATCH = 400_000
"""Characters of unique runs per name check call: one request of the check socket (under vault_client.CHECK_CHUNK
with room for the decoded views of base64 blocks, under MAX_REQUEST with every character escaped)."""
RUN_MARK = 512 * 1024 ** 2
"""Bytes of the image between two resume marks at most: the runs read so far are checked and `checked` is written."""
RUN_SEEN_MAX = 4_000_000
"""Distinct runs kept to drop repeats; the set starts again when it is full (a run is then checked once more)."""
RUN_SEEN_CHARS = 256 * 1024 ** 2
RUN_PACE_BYTES = 14 * 1024 ** 2
RUN_PACE_WINDOW = 60.0
"""Request bytes the run check sends per window: under the daemon's RATE_BYTES (16 MB a minute per uid), so the hooks
of the same user keep 2 MB a minute and no request comes back as "rate"."""
PROGRESS_EVERY = 60.0
"""Seconds between two progress lines of the run check on stderr."""
STAGED_RE = re.compile(r"^\.winlab-[0-9a-f]{32}(?:\.[A-Za-z0-9]{1,16})?$")
"""The private name a received file has while it is checked (_check_and_publish); the record keeps it as `staged`."""
KEPT_LINE = ("the received file is kept unpublished as %s (checked to %s of %s): %s; `awb winlab get %s %s --recheck` "
             "goes on from there without a transfer")
"""The state line of a received file that is complete and not published yet: where it is, how far the checks got,
why and the command that finishes them."""
AGAIN_LINE = ("the received file is kept unpublished as %s (%s, complete): %s; `awb winlab get %s %s` again, while the "
              "window is open, compares it without a transfer and runs the checks")
"""The state line of a complete received file whose SHA-256 was not compared yet."""
DISK_IMAGES = {".vhdx": "vhdx", ".vhd": "vhd", ".vmdk": "vmdk", ".qcow2": "qcow2", ".iso": "iso", ".img": "img"}
"""The suffix of a disk image and its kind."""
SECTOR = 512
LOG_READ = 4 * 1024 * 1024
"""The most of a job's log one `awb winlab log` fetches."""
PRINT_MAX = 2000
PRINT_SIDE = 1000
WAIT_TIMEOUT = 3600
WAIT_FIRST = 2.0
WAIT_LAST = 60.0
BROKER_SOCKET_ENV = "AWB_WINLAB_SOCKET"
BROKER_SOCKET = "/run/awb-winlab/broker.sock"
BROKER_SLACK = 120.0
"""Seconds a call to the broker may take beyond the command's own timeout (the key scan and the connection)."""
BROKER_MAX_ANSWER = 64 * 1024 * 1024
NO_TRANSPORT = ("there is no transport to a lab machine yet for this user: the socket of the lab broker "
                "(awb-winlab.service) is not there or not open to it; nothing was sent")
EXIT_OK, EXIT_REFUSED, EXIT_REMOTE, EXIT_OFFLINE = 0, 1, 3, 4
NAME_CLASSES = ("name", "path")

SECRET_REF_RE = re.compile(r"\{\{\s*secret\s*:", re.I)
STATE_RE = re.compile(r"awb-winlab|programdata[\\/]+ssh\b|administrators_authorized_keys", re.I)
def _binary(path) -> bool:
    """True when the first 64 KiB of the file hold a NUL byte: no text file of a lab machine does."""
    with open(path, "rb") as fh:
        return b"\x00" in fh.read(65536)


_PROGRAM = (b"\x7fELF", b"MZ")
"""The first bytes of a program: ELF (Linux) and PE (Windows)."""

_PS_PLAIN_RE = re.compile(
    r"(?i)ConvertTo-SecureString\b(?=[^\n;|)]*-AsPlainText)(?=[^\n;|)]*[\"'][^\"'\n]{3,}[\"'])"
    r"|[\"'][^\"'\n]{3,}[\"']\s*\|\s*ConvertTo-SecureString\b"
    r"|(?<![\w-])-(?:Password|Passwd|Pwd|Kennwort)\s+[\"'][^\"'\n]+[\"']")
"""A password typed into the text: a quoted value for ConvertTo-SecureString or a -Password argument. The
ConvertTo-SecureString part ends at a closing bracket too, so a quoted -Description after (ConvertTo-SecureString $pw
-AsPlainText -Force) is not taken for the password."""
_COMPRESSED = (b"PK\x03\x04", b"\x1f\x8b", b"7z\xbc\xaf\x27\x1c", b"\xfd7zXZ\x00", b"BZh", b"MSCF", b"Rar!",
               b"\x28\xb5\x2f\xfd")
"""Containers whose text no piece of the file shows: refused when the check cannot open them."""

_CLIXML_MARK_RE = re.compile(r"(?m)^#< CLIXML\r?(?:\n|$)")
_OBJS = r"<Objs\b[^>]*xmlns=\"http://schemas\.microsoft\.com/powershell/2004/04\"[^>]*>"
_OBJS_RE = re.compile(_OBJS + r"(.*?)</Objs>", re.S)
_OBJS_OPEN_RE = re.compile(_OBJS + r".*\Z", re.S)
_PROGRESS_RE = re.compile(r"<Obj\b[^>]*\bS=\"progress\"[^>]*>.*?</Obj>", re.S)
_STREAM_RE = re.compile(r"<S\b[^>]*\bS=\"[A-Za-z]+\"[^>]*>(.*?)</S>", re.S)
_XESC_RE = re.compile(r"_x([0-9A-Fa-f]{4})_")


class Offline(Exception):
    """The machine cannot be reached or the connection dropped. The message is one line, never a text sent."""


class Stopped(BaseException):
    """SIGTERM or SIGHUP during a get (a tool call that ran out of time, a closed session): a BaseException, so no
    `except Exception` takes it and every finally gives the received file back under its part name (tcp-bytg ask 11:
    ten complete transfers of disk images were lost when the check was cut off)."""


class Transport:
    """What a session needs of a lab machine. The SSH transport of part 4 implements it, the tests a fake. Every
    method raises Offline when the machine cannot be reached or the connection drops."""

    def run(self, text: str, timeout: float | None = None) -> tuple[int, str, str]:
        """Run the PowerShell `text` on the machine: (exit code, stdout, stderr) as powershell.exe wrote them."""
        raise NotImplementedError

    def put_chunk(self, path: str, offset: int, data: bytes) -> int:
        """Write `data` at `offset` of the file `path` on the machine and return its size after. `offset` is 0
        (the file and its missing folders are made, an old file emptied first) or the present size of the file;
        another offset raises ValueError."""
        raise NotImplementedError

    def get_chunk(self, path: str, offset: int, size: int) -> bytes:
        """Up to `size` bytes of the file `path` from `offset`; b"" at its end."""
        raise NotImplementedError

    def stat(self, path: str, digest: bool = False) -> dict | None:
        """{"size": bytes, "mtime": text} of the file `path`, with "sha256" (64 hex digits) when `digest`; None
        when there is no file."""
        raise NotImplementedError

    def close(self) -> None:
        """Let the connection go."""


def _broker_call(request: dict, payload: bytes = b"", timeout: float = BROKER_SLACK) -> tuple[dict, bytes]:
    """One call to the lab broker: the request line and the bytes of a put, the answer line and the bytes of a get.
    A refusal raises WinlabError, a machine or broker that cannot be reached Offline, a put at another offset than
    the size of the file ValueError."""
    path = os.environ.get(BROKER_SOCKET_ENV) or BROKER_SOCKET
    try:
        # a socket this user cannot even see (missing, or in the broker's folder of mode 750) is no transport
        if not stat.S_ISSOCK(os.stat(path).st_mode):
            raise WinlabError(NO_TRANSPORT)
    except OSError:
        raise WinlabError(NO_TRANSPORT) from None
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.settimeout(timeout)
    try:
        try:
            s.connect(path)
        except (FileNotFoundError, PermissionError, NotADirectoryError):
            raise WinlabError(NO_TRANSPORT) from None
        except OSError:
            raise Offline("the lab broker does not answer (awb-winlab.service)") from None
        try:
            s.sendall(json.dumps(request).encode("utf-8") + b"\n" + payload)
            buf = bytearray()
            while b"\n" not in buf:
                chunk = s.recv(1 << 16)
                if not chunk:
                    raise Offline("the lab broker closed the connection")
                buf += chunk
                if len(buf) > BROKER_MAX_ANSWER:
                    raise Offline("the answer of the lab broker is too large")
            cut = buf.index(b"\n")
            answer = winlab._json(bytes(buf[:cut]))
            if not isinstance(answer, dict) or type(answer.get("ok")) is not bool:
                raise ValueError("not an object")
            data = bytearray(buf[cut + 1:])
            length = answer.get("length") if answer.get("ok") else None
            if length is not None:
                if (type(length) is not int or not 0 <= length <= min(BROKER_MAX_ANSWER, request.get("size", BROKER_MAX_ANSWER))
                        or len(data) > length):
                    raise ValueError("invalid payload length")
                while len(data) < length:
                    chunk = s.recv(min(1 << 16, length - len(data)))
                    if not chunk:
                        raise Offline("the connection to the lab broker dropped")
                    data += chunk
        except (OSError, ValueError, RecursionError) as exc:
            raise Offline("the connection to the lab broker dropped (%s)" % type(exc).__name__) from None
    finally:
        s.close()
    if answer.get("ok"):
        return answer, bytes(data)
    error = "the lab broker refused the call"
    # Preserve the documented window refusal, but never relay arbitrary dependency text. The refusal is the
    # broker's own generated line of a machine name and a project code: the pattern is its check, no name check.
    supplied = answer.get("error")
    if isinstance(supplied, str) and re.fullmatch(
            r"the window of [a-z][a-z0-9-]{1,30} is open for another project, not for %s" % codes.TCP_PROJECT_WIDE,
            supplied):
        error = supplied
    if answer.get("kind") == "offline":
        raise Offline(error)
    if answer.get("kind") == "offset":
        raise ValueError(error)
    raise WinlabError(error)


class BrokerTransport(Transport):
    """The Transport over the lab broker (part 4): every call names the machine, the project and the key token of
    the project folder; the broker checks the window, holds the key and speaks SSH."""

    def __init__(self, name: str, project: str, token: str | None, started: str | None = None):
        self.name = name
        self.project = project
        self.token = token
        self.started = started

    def _call(self, request: dict, timeout: float = BROKER_SLACK, payload: bytes = b"") -> tuple[dict, bytes]:
        request = dict(request, machine=self.name, project=self.project, token=self.token)
        if self.started:
            request["job_started"] = self.started
        return _broker_call(request, payload, timeout)

    def check(self) -> dict:
        """The window is open for the project: {"name", "end"}; else WinlabError with the broker's line."""
        return _broker_machine(self._call({"op": "check"})[0], self.name)

    def run(self, text: str, timeout: float | None = None) -> tuple[int, str, str]:
        seconds = timeout or RUN_TIMEOUT
        answer, _ = self._call({"op": "run", "text": text, "timeout": seconds}, seconds + BROKER_SLACK)
        if (type(answer.get("exit")) is not int or not isinstance(answer.get("stdout"), str)
                or not isinstance(answer.get("stderr"), str)):
            raise WinlabError("the lab broker returned an invalid command result")
        return answer["exit"], answer["stdout"], answer["stderr"]

    def put_chunk(self, path: str, offset: int, data: bytes) -> int:
        answer, _ = self._call({"op": "put_chunk", "path": path, "offset": offset, "length": len(data)},
                               BROKER_SLACK * 3, payload=data)
        return int(answer["size"])

    def get_chunk(self, path: str, offset: int, size: int) -> bytes:
        return self._call({"op": "get_chunk", "path": path, "offset": offset, "size": size}, BROKER_SLACK * 3)[1]

    def stat(self, path: str, digest: bool = False) -> dict | None:
        answer, _ = self._call({"op": "stat", "path": path, "digest": digest},
                               3600 + BROKER_SLACK if digest else BROKER_SLACK * 3)
        return answer.get("stat")


def _token(root) -> str | None:
    from awb import labclient

    return labclient._token(Path(root)) if root else None


def connect(machine: dict) -> Transport:
    """The transport to `machine` (what _session returns: the machine with the session's project and folder): the
    lab broker's. Its window check comes first, so a refusal or a missing broker sends nothing."""
    transport = BrokerTransport(machine.get("name"), machine.get("project"), _token(machine.get("root")),
                                 machine.get("job_started"))
    transport.check()
    return transport


def _local_registry() -> bool:
    """True when this user has a registry of its own (the owner on his own host, the tests); a session of the work
    user on a sealed host has none and asks the broker."""
    try:
        return winlab.registry_path().exists()
    except Exception:
        return False


def _broker_names(answer: dict) -> list[str]:
    names = answer.get("machines")
    if not isinstance(names, list) or len(names) > 1024:
        raise WinlabError("the lab broker returned an invalid machine list")
    for name in names:
        winlab._check_name(name)
        check_own(name, "the machine name")
    return names


def _broker_machine(answer: dict, name: str) -> dict:
    machine = answer.get("machine")
    try:
        if (not isinstance(machine, dict) or machine.get("name") != name
                or winlab._stamp(winlab._parse(machine["end"])) != machine["end"]
                or machine.get("public_image", True) is not True):
            raise ValueError()
    except (ValueError, KeyError, TypeError):
        raise WinlabError("the lab broker returned an invalid window") from None
    return machine


def session_list(start: Path | None = None) -> list[str]:
    """`awb winlab list` in a session of the work user, who reads no registry of his own: the machines whose window
    is open for this project and its end, from the broker. Other machines and other projects' windows stay unseen."""
    root = _root(start)
    code = _code(root)
    token = _token(root)
    names = _broker_names(_broker_call({"op": "windows", "project": code, "token": token})[0])
    if not names:
        return ["no lab machine has an open window for %s (the owner: awb winlab open NAME --minutes N --project %s)"
                % (code, code)]
    lines = ["machine  window for  end"]
    for name in names:
        answer, _ = _broker_call({"op": "check", "machine": name, "project": code, "token": token})
        lines.append("%s  %s  %s" % (name, code, _broker_machine(answer, name)["end"]))
    return lines


def _allowed(name: str, code: str, root: Path, started: str | None = None) -> dict:
    """winlab.allowed from this user's registry, else the broker's check (the owner's registry). `started` is the
    start of a job: its status, log and stop are still allowed after the window ended."""
    if _local_registry():
        return winlab.allowed_job(name, code, started) if started else winlab.allowed(name, code)
    request = {"op": "check", "machine": name, "project": code, "token": _token(root)}
    if started:
        request["job_started"] = started
    answer, _ = _broker_call(request)
    return _broker_machine(answer, name)


def _pause(seconds: float) -> None:
    time.sleep(seconds)


def _now() -> datetime.datetime:
    return datetime.datetime.now(datetime.timezone.utc)


def _stamp() -> str:
    return _now().strftime("%Y-%m-%dT%H:%M:%SZ")


# --------------------------------------------------------------------------- the project


def _root(start: Path | None = None) -> Path:
    from awb import config

    root = config.scope_root(Path(os.path.abspath(start or Path.cwd())))
    if root is None:
        raise WinlabError("awb winlab runs in the folder of a project (one with a SCOPE.md)")
    return root


def _code(root: Path) -> str:
    from awb import labclient

    return labclient._code(root)


def _inside(path: Path, folder: Path) -> bool:
    try:
        Path(os.path.realpath(path)).relative_to(os.path.realpath(folder))
        return True
    except ValueError:
        return False


def _write_json(path: Path, data: dict) -> None:
    winlab._atomic_json(path, data)


def _unlink(path: Path) -> None:
    try:
        with winlab._parent(path) as folder:
            os.unlink(path.name, dir_fd=folder)
    except FileNotFoundError:
        pass


def _read_json(path: Path) -> dict | None:
    try:
        data = winlab._json(winlab._read_bytes(path))
    except (OSError, UnicodeError, ValueError, RecursionError, WinlabError):
        return None
    return data if isinstance(data, dict) else None


def log_path(root: Path, machine: str) -> Path:
    winlab._check_name(machine)
    check_own(machine, "the machine name")
    return root / LOG_FOLDER / ("%s-%s.log" % (_now().strftime("%Y-%m-%d"), machine))


def _append(path: Path, lines: list[str]) -> None:
    """A log over lablog.LIMIT moves to full/ beside it (git-ignored) and git keeps its summary (Q-0021). The home
    folder part of a path (Windows console output, C:\\Users\\<user>\\...) is written as ~ (lablog.mask_home,
    Q-0010), in a project log and in one outside a project alike."""
    from awb import lablog

    root = _project_of(path)
    if root is None:
        with winlab._parent(path, create=True) as folder:
            fd = winlab._regular(folder, path.name, os.O_WRONLY | os.O_CREAT | os.O_APPEND)
            with os.fdopen(fd, "a", encoding="utf-8") as fh:
                fh.write(lablog.mask_home("\n".join(lines).rstrip("\n") + "\n\n"))
        return
    try:
        lablog.append(root, path, "\n".join(lines).rstrip("\n") + "\n\n")
    except lablog.LabLogError:
        raise WinlabError("lab storage requires a regular file with no links") from None


def _project_of(path: Path) -> Path | None:
    """The project folder above a log under notes/winlab/ (None for a log elsewhere)."""
    path = Path(os.path.abspath(path))
    for up in path.parents:
        if up.name == "notes" and path.is_relative_to(up / LOG_FOLDER.name):
            return up.parent
    return None


def _serial(fn):
    """Serialize jobs, log cursors and transfers of a project across processes."""
    signature = inspect.signature(fn)

    @functools.wraps(fn)
    def locked(*args, **kwargs):
        bound = signature.bind(*args, **kwargs)
        root = bound.arguments.get("root") or _root()
        bound.arguments["root"] = root
        with winlab._parent(root / ".winlab-lock") as folder:
            winlab._lock(folder)
            try:
                return fn(*bound.args, **bound.kwargs)
            finally:
                fcntl.flock(folder, fcntl.LOCK_UN)
    return locked


class _CheckedTransport:
    """Transport exceptions may contain commands or credentials. Keep only the failure class."""

    def __init__(self, transport):
        self.transport = transport

    def __getattr__(self, name):
        method = getattr(self.transport, name)

        def call(*args, **kwargs):
            try:
                return method(*args, **kwargs)
            except Offline:
                raise Offline("the connection to the lab machine dropped") from None
            except (WinlabError, ValueError):
                raise WinlabError("the lab machine refused the operation") from None
        return call


def _block(label: str, text: str) -> list[str]:
    return ["%s:" % label] + ["    " + line for line in text.rstrip("\n").split("\n")] if text.strip() else []


def log(root: Path, machine: str, title: str, lines: list[str]) -> Path:
    """Append an entry to notes/winlab/<date>-<machine>.log; its lines carry no secret and no name."""
    path = log_path(root, machine)
    _append(path, ["## %s %s" % (_stamp(), title)] + lines)
    return path


# --------------------------------------------------------------------------- what goes out


def _secret_count(text: str) -> int:
    from awb import hooks, labsecrets

    counts = labsecrets.shorten(text)[1]
    return (len(hooks._secret_hits(text)) + sum(n for kind, n in counts.items() if kind != "login")
            + len(_PS_PLAIN_RE.findall(text)))


def _name_count(text: str) -> int:
    from awb import hooks

    try:
        return len([h for h in hooks.name_hits(text) if h.get("cls") in NAME_CLASSES])
    except hooks.Unavailable:
        raise WinlabError("the name check did not run: nothing was sent") from None


CHECKS_KEY = "winlab_checks"


def content_checks() -> bool:
    """False when the host file says `winlab_checks = off`: the owner's word of 2026-11-25 ("WinLab работает
    исключительно с образами, которые мы качаем с интернета ... железобетонно не может быть никаких файлов
    заказчика ... Убери там абсолютно все проверки"). Then no name check and no secret check runs on what goes to a
    lab machine (a command, a put) or comes back (a get); a get keeps only its SHA-256 compare. The guards of the
    machine itself stay: no {{secret:}} reference is filled in, no command reaches the machine's own state (its ssh
    keys), a path stays a plain whole path, and logs keep their secret masking. Without the line the checks run."""
    from awb import config

    return config.host_conf().get(CHECKS_KEY, "on").strip().lower() != "off"


def check_text(text: str, what: str = "the command") -> None:
    """Refuse `text` before it goes to a lab machine: a {{secret:}} reference, the machine's own state, a registered
    name or a secret. The refusal names the class and the count, never the text."""
    _check_own(text, what, names=True)


def check_own(text: str, what: str) -> None:
    """check_text without the name check, for text the Workbench generated itself (a lab machine name, a window
    line): the material was checked for names once when it entered, generated text gets the secret check only."""
    _check_own(text, what, names=False)


def _check_own(text: str, what: str, names: bool) -> None:
    if not text.strip():
        raise WinlabError("%s is empty" % what)
    if SECRET_REF_RE.search(text):
        raise WinlabError("%s carries a {{secret:NAME}} reference: nothing is filled in for a lab machine, nothing "
                          "was sent" % what)
    if STATE_RE.search(text):
        raise WinlabError("%s names the machine's own state (awb-winlab, the ssh keys): no session command reaches "
                          "it, nothing was sent" % what)
    if not content_checks():
        return
    found = _name_count(text) if names else 0
    if found:
        raise WinlabError("%s carries %d registered name(s): nothing was sent" % (what, found))
    found = _secret_count(text)
    if found:
        raise WinlabError("%s carries %d secret(s): no password and no key goes to a lab machine, nothing was sent"
                          % (what, found))


def check_winpath(winpath: str) -> None:
    if not WINPATH_RE.fullmatch(winpath or "") or len(winpath) > WINPATH_MAX \
            or any(s.strip(". ") == "" or s != s.rstrip(". ") for s in winpath[3:].split("\\")):
        raise WinlabError("WINPATH is a whole path like C:\\lab\\file.txt: no variable, no quote, no .. and at most "
                          "%d characters" % WINPATH_MAX)
    if any(re.fullmatch(r"(?i)(?:con|prn|aux|nul|com[1-9¹²³]|lpt[1-9¹²³])", part.split(".")[0])
           for part in winpath[3:].split("\\")):
        raise WinlabError("WINPATH must name a file, not a Windows device")
    if winpath.lower().endswith(PART):
        raise WinlabError("WINPATH may not end in %s" % PART)
    check_text(winpath, "WINPATH")


def check_vm(vm: str | None) -> None:
    if vm is None:
        return
    if not VM_RE.fullmatch(vm):
        raise WinlabError("--vm takes the name of the VM: letters, digits, space, dot, - and _, at most 64")
    check_text(vm, "the VM name")


def _ps(value: str) -> str:
    """A single-quoted PowerShell string."""
    return "'%s'" % value.replace("'", "''")


def _header(op: str, **args) -> str:
    """The first line of every script: a comment the logs and the fake of the tests read."""
    return "# awb-winlab " + json.dumps(dict(args, op=op), sort_keys=True, ensure_ascii=True)


def _vm_body(vm: str, text: str) -> list[str]:
    """`text` run in the VM with the credential the machine reads itself; the password never leaves it."""
    return [
        "$awbVm = %s" % _ps(vm),
        "$awbFile = Join-Path $env:ProgramData ('awb-winlab\\vm\\' + $awbVm + '.txt')",
        "if (-not (Test-Path -LiteralPath $awbFile -PathType Leaf)) { throw ('the credential file of the VM ' + $awbVm "
        "+ ' is missing on the lab machine: the owner puts it there') }",
        "$awbOpen = @((Get-Acl -LiteralPath $awbFile -ErrorAction Stop).Access | Where-Object {",
        "    $_.AccessControlType -eq 'Allow' -and",
        "    $_.IdentityReference.Translate([Security.Principal.SecurityIdentifier]).Value -notin "
        "@('S-1-5-32-544', 'S-1-5-18') })",
        "if ($awbOpen.Count -gt 0) { throw ('the credential file of the VM ' + $awbVm + ' is open to more than "
        "Administrators and SYSTEM') }",
        "$awbLines = @(Get-Content -LiteralPath $awbFile -Encoding UTF8 -ErrorAction Stop)",
        "if ($awbLines.Count -lt 2) { throw ('the credential file of the VM ' + $awbVm + ' holds no user line and "
        "no second line') }",
        "$awbSecure = New-Object Security.SecureString",
        "foreach ($awbChar in $awbLines[1].ToCharArray()) { $awbSecure.AppendChar($awbChar) }",
        "$awbSecure.MakeReadOnly()",
        "$awbCred = New-Object Management.Automation.PSCredential ($awbLines[0].Trim(), $awbSecure)",
        "Remove-Variable awbLines, awbChar -ErrorAction SilentlyContinue",
        "Invoke-Command -VMName $awbVm -Credential $awbCred -ErrorAction Stop -ScriptBlock {",
        "$ProgressPreference = 'SilentlyContinue'",
        text.rstrip("\n"),
        "}",
    ]


_FORMAT_RE = re.compile(r"\b(?:Format|Out)-[A-Za-z]+\b|\|\s*(?:ft|fl|fw|fc|oh)\b", re.I)
_EXIT_RE = re.compile(r"\s*(?:exit|return|throw)\b", re.I)
_STATEMENT_RE = re.compile(r"\s*(?:\$|\(|if\b|foreach\b|for\b|while\b|switch\b|try\b|do\b|function\b|param\b)", re.I)
OUT_STRING = "| Out-String -Width 200"
MISSING_CREDENTIAL_RE = re.compile(r"^.*the credential file of the VM .* is missing on the lab machine.*$", re.M)


def with_out_string(text: str) -> str:
    """`text` with `| Out-String -Width 200` after it when it has no Format or Out command: PowerShell writes a
    formatted table of a bare command to the pipe only when the command ends in one (a Get-VM printed nothing).
    A command of several statements goes in a script block first."""
    if _FORMAT_RE.search(text) or _EXIT_RE.match(text):
        return text
    body = text.strip("\n")
    if "\n" in body or ";" in body or "#" in body or _STATEMENT_RE.match(body):
        return "& {\n%s\n} %s" % (body, OUT_STRING)
    return "%s %s" % (body, OUT_STRING)


def plain_credential_line(text: str | None) -> str | None:
    """A text that holds the line of a missing guest credential file is that line alone, not the error record
    around it."""
    found = MISSING_CREDENTIAL_RE.search(text) if text else None
    return found.group(0).strip() if found else text


def command_script(text: str, vm: str | None = None) -> str:
    """The script `awb winlab run` sends: the header, no progress records, the command (in the VM with `vm`)."""
    text = with_out_string(text)
    body = _vm_body(vm, text) if vm else [text.rstrip("\n")]
    return "\n".join([_header("run", vm=vm), "$ProgressPreference = 'SilentlyContinue'"] + body) + "\n"


def _job_dir(job: str) -> str:
    return "Join-Path $env:ProgramData %s" % _ps("awb-winlab\\jobs\\" + job)


def _task(job: str) -> str:
    return "-TaskName %s -TaskPath %s" % (_ps("awb-winlab-job-" + job), _ps(TASK_PATH))


def job_file(job: str, text: str, vm: str | None = None) -> str:
    """job.ps1 on the machine: the command with all its output in log.txt, its exit code in exit.txt at the end."""
    body = _vm_body(vm, text) if vm else [text.rstrip("\n")]
    return "\n".join([
        _header("job", job=job, vm=vm),
        "$ProgressPreference = 'SilentlyContinue'",
        "$awbDir = %s" % _job_dir(job),
        "$awbLog = Join-Path $awbDir 'log.txt'",
        "$awbCode = 0",
        "try {",
        "    & {",
    ] + body + [
        "    } *>&1 | Out-File -LiteralPath $awbLog -Append -Encoding utf8 -Width 400",
        "    if ($LASTEXITCODE) { $awbCode = $LASTEXITCODE }",
        "} catch {",
        "    $_ | Out-File -LiteralPath $awbLog -Append -Encoding utf8 -Width 400",
        "    $awbCode = 1",
        "}",
        "Set-Content -LiteralPath (Join-Path $awbDir 'exit.txt') -Value $awbCode -Encoding ascii",
    ]) + "\n"


def job_start_script(job: str, text: str, vm: str | None = None) -> str:
    """Writes job.ps1 and starts it as the scheduled task \\awb-winlab\\awb-winlab-job-<JOB> of the logged on
    account (S4U, no password kept, no time limit); prints started= and log=."""
    encoded = base64.b64encode(job_file(job, text, vm).encode("utf-8")).decode("ascii")
    return "\n".join([
        _header("job-start", job=job, vm=vm),
        "$ErrorActionPreference = 'Stop'",
        "$ProgressPreference = 'SilentlyContinue'",
        "$awbDir = %s" % _job_dir(job),
        "New-Item -ItemType Directory -Force -Path $awbDir | Out-Null",
        "$awbJob = Join-Path $awbDir 'job.ps1'",
        "[IO.File]::WriteAllText($awbJob, [Text.Encoding]::UTF8.GetString([Convert]::FromBase64String(%s)), "
        "(New-Object Text.UTF8Encoding $true))" % _ps(encoded),
        "$awbAction = New-ScheduledTaskAction -Execute 'powershell.exe' -Argument ('-NoProfile -NonInteractive "
        "-ExecutionPolicy Bypass -File \"' + $awbJob + '\"')",
        "$awbUser = [Security.Principal.WindowsIdentity]::GetCurrent().Name",
        "$awbPrincipal = New-ScheduledTaskPrincipal -UserId $awbUser -LogonType S4U -RunLevel Highest",
        "$awbSettings = New-ScheduledTaskSettingsSet -ExecutionTimeLimit ([TimeSpan]::Zero) -AllowStartIfOnBatteries "
        "-DontStopIfGoingOnBatteries -MultipleInstances IgnoreNew",
        "Register-ScheduledTask %s -Action $awbAction -Principal $awbPrincipal -Settings $awbSettings | Out-Null"
        % _task(job),
        "Start-ScheduledTask %s" % _task(job),
        "'started=%s'" % job,
        "'log=' + (Join-Path $awbDir 'log.txt')",
    ]) + "\n"


def job_status_script(job: str) -> str:
    """Prints state=running, state=ended with exit=N, state=lost (no task runs and no exit code) or state=unknown."""
    return "\n".join([
        _header("job-status", job=job),
        "$awbDir = %s" % _job_dir(job),
        "$awbExit = Join-Path $awbDir 'exit.txt'",
        "$awbTask = Get-ScheduledTask %s -ErrorAction SilentlyContinue" % _task(job),
        "if (Test-Path -LiteralPath $awbExit) {",
        "    'state=ended'",
        "    'exit=' + (Get-Content -LiteralPath $awbExit -Raw).Trim()",
        "    if ($awbTask) { Unregister-ScheduledTask %s -Confirm:$false }" % _task(job),
        "} elseif ($awbTask -and ('Running', 'Queued') -contains [string]$awbTask.State) {",
        "    'state=running'",
        "} elseif ($awbTask -or (Test-Path -LiteralPath $awbDir)) {",
        "    'state=lost'",
        "} else {",
        "    'state=unknown'",
        "}",
    ]) + "\n"


def job_stop_script(job: str) -> str:
    """Stops the task, writes exit.txt `stopped` unless the job ended before, removes the task; prints the state."""
    return "\n".join([
        _header("job-stop", job=job),
        "$awbDir = %s" % _job_dir(job),
        "$awbExit = Join-Path $awbDir 'exit.txt'",
        "$awbTask = Get-ScheduledTask %s -ErrorAction SilentlyContinue" % _task(job),
        "if ($awbTask) { Stop-ScheduledTask %s }" % _task(job),
        "if ((Test-Path -LiteralPath $awbDir) -and -not (Test-Path -LiteralPath $awbExit)) {",
        "    Set-Content -LiteralPath $awbExit -Value 'stopped' -Encoding ascii",
        "}",
        "if ($awbTask) { Unregister-ScheduledTask %s -Confirm:$false }" % _task(job),
        "if (Test-Path -LiteralPath $awbExit) {",
        "    $awbCode = (Get-Content -LiteralPath $awbExit -Raw).Trim()",
        "    if ($awbCode -eq 'stopped') { 'state=stopped' } else { 'state=ended'; 'exit=' + $awbCode }",
        "} else {",
        "    'state=unknown'",
        "}",
    ]) + "\n"


def file_move_script(src: str, dst: str, overwrite: bool = False) -> str:
    if overwrite:
        return "\n".join([_header("file-move", src=src, dst=dst, overwrite=True), "$ErrorActionPreference = 'Stop'",
                          "Move-Item -LiteralPath %s -Destination %s -Force" % (_ps(src), _ps(dst))]) + "\n"
    return "\n".join([_header("file-move", src=src, dst=dst), "$ErrorActionPreference = 'Stop'",
                      "[IO.File]::Move(%s, %s)" % (_ps(src), _ps(dst))]) + "\n"


def file_remove_script(path: str) -> str:
    return "\n".join([_header("file-remove", path=path), "$ErrorActionPreference = 'Stop'",
                      "if ([IO.File]::Exists(%s)) { [IO.File]::Delete(%s) }" % (_ps(path), _ps(path))]) + "\n"


# --------------------------------------------------------------------------- what comes back


def _unescape(text: str) -> str:
    return _XESC_RE.sub(lambda m: chr(int(m.group(1), 16)), html.unescape(text))


def strip_clixml(text: str) -> str:
    """`text` without the CLIXML records powershell.exe writes over SSH: progress records go, the text of an error,
    warning or other stream record stays as plain text."""
    if "CLIXML" not in text and "<Objs" not in text:
        return text

    def objs(m: re.Match) -> str:
        body = _PROGRESS_RE.sub("", m.group(1))
        return "".join(_unescape(s.group(1)) for s in _STREAM_RE.finditer(body))

    text = _OBJS_RE.sub(objs, text)
    text = _OBJS_OPEN_RE.sub("", text)
    return _CLIXML_MARK_RE.sub("", text)


def landed(text: str) -> tuple[str | None, str]:
    """Output of the machine by the rules of `awb paste --own`: (the text to print and log, a note). None when a
    registered name withholds it or the name check did not run."""
    from awb import hooks, paste

    text = strip_clixml(text).replace("\r\n", "\n")
    if not text.strip():
        return "", ""
    note = ""
    raw = text.encode("utf-8")
    if len(raw) > paste.OWN_MAX_BYTES:
        return None, "withheld: output exceeds the size limit"
    try:
        clean, n = paste.own_text(text)
    except paste.PasteRefused:
        return None, "withheld: it carries a registered name"
    except hooks.Unavailable:
        return None, "withheld: the name check did not run"
    if n:
        note = ", ".join(x for x in (note, "%d secret(s) shortened or replaced" % n) if x)
    return clean, note


def _print_cut(text: str, where: str, stream=None) -> None:
    stream = stream or sys.stdout
    lines = text.rstrip("\n").split("\n")
    if len(lines) <= PRINT_MAX:
        stream.write("\n".join(lines) + "\n")
        return
    cut = len(lines) - 2 * PRINT_SIDE
    stream.write("\n".join(lines[:PRINT_SIDE]) + "\n")
    stream.write("... %d lines cut here, the whole output is in %s\n" % (cut, where))
    stream.write("\n".join(lines[-PRINT_SIDE:]) + "\n")


# --------------------------------------------------------------------------- run


def _session(name: str, root: Path | None, started: str | None = None) -> tuple[Path, str, dict]:
    root = root or _root()
    winlab._check_name(name)
    check_own(name, "the machine name")
    code = _code(root)
    # Refuse unsafe log storage before contacting a machine.
    path = log_path(root, name)
    with winlab._parent(path, create=True) as folder:
        try:
            fd = winlab._regular(folder, path.name)
        except FileNotFoundError:
            pass
        else:
            os.close(fd)
    return root, code, dict(_allowed(name, code, root, started), project=code, root=str(root), job_started=started)


def _refused(root: Path | None, machine: str, title: str, err: Exception) -> None:
    if root is not None and winlab.NAME_RE.fullmatch(machine or ""):
        try:
            log(root, machine, title, ["refused: %s" % err])
        except (OSError, WinlabError):
            pass


def _connect(root: Path, name: str, title: str, machine: dict) -> Transport:
    """connect(), with a refusal or an offline machine logged."""
    try:
        return _CheckedTransport(connect(machine))
    except WinlabError as err:
        _refused(root, name, title, err)
        raise
    except Offline:
        log(root, name, title, ["offline: the connection to the lab machine dropped"])
        raise Offline("the connection to the lab machine dropped") from None


@_serial
def run(name: str, text: str, vm: str | None = None, timeout: float = RUN_TIMEOUT,
        root: Path | None = None) -> dict:
    """Run `text` on the machine `name`. Returns {"exit", "stdout", "stderr", "notes", "log"}: stdout and stderr
    as they landed (None when withheld) and a note for each. Raises WinlabError (refused, nothing sent), Offline."""
    root = root or _root()
    try:
        root, code, machine = _session(name, root)
        check_vm(vm)
        check_text(text)
    except WinlabError as err:
        _refused(root, name, "run on %s" % name, err)
        raise
    head = ["command:"] + ["    " + line for line in text.rstrip("\n").split("\n")]
    title = "run on %s%s" % (name, " in vm %s" % vm if vm else "")
    transport = _connect(root, name, title, machine)
    began = _stamp()
    try:
        exit_code, out, err = transport.run(command_script(text, vm), timeout=timeout)
    except Offline as exc:
        log(root, name, title, head + ["started %s" % began, "offline: %s" % exc])
        raise
    finally:
        transport.close()
    out_text, out_note = landed(out)
    err_text, err_note = landed(err)
    err_text = plain_credential_line(err_text)
    lines = head + ["started %s, ended %s" % (began, _stamp()), "exit %d" % exit_code]
    for label, body, note in (("stdout", out_text, out_note), ("stderr", err_text, err_note)):
        lines += ["%s: %s" % (label, note)] if body is None else _block(label + (" (%s)" % note if note else ""),
                                                                           body)
    path = log(root, name, title, lines)
    return {"exit": exit_code, "stdout": out_text, "stderr": err_text, "notes": (out_note, err_note),
            "log": path.relative_to(root)}


# --------------------------------------------------------------------------- jobs


def _job_winlog(path, job: str) -> bool:
    if not isinstance(path, str) or not WINPATH_RE.fullmatch(path):
        return False
    return (path.lower().endswith("\\awb-winlab\\jobs\\%s\\log.txt" % job)
            and len(path) <= WINPATH_MAX
            and all(part.strip(". ") and part == part.rstrip(". ") for part in path[3:].split("\\")))


def _job_record(root: Path, job: str) -> tuple[Path, dict]:
    if not JOB_RE.fullmatch(job or ""):
        raise WinlabError("a job id reads like wl-20261105-101500-a1b2, as awb winlab start printed it")
    path = root / LOG_FOLDER / "jobs" / ("%s.json" % job)
    record = _read_json(path)
    if record is None or not winlab.NAME_RE.fullmatch(str(record.get("machine", ""))):
        raise WinlabError("no job %s in this project (awb winlab start prints the id)" % job)
    if (record.get("project") != _code(root) or record.get("job") != job
            or type(record.get("log_offset")) is not int or record["log_offset"] < 0
            or type(record.get("log_discard_line", False)) is not bool
            or not _job_winlog(record.get("winlog"), job)):
        raise WinlabError("the job record is malformed")
    return path, record


def _parse_state(out: str) -> dict:
    found = dict(line.strip().split("=", 1) for line in strip_clixml(out).splitlines() if "=" in line)
    state = found.get("state", "unknown")
    if state not in ("running", "ended", "stopped", "lost", "unknown"):
        state = "unknown"
    result: dict = {"state": state, "exit": None}
    if state == "ended":
        value = found.get("exit", "")
        if value == "stopped":
            result["state"] = "stopped"
        elif re.fullmatch(r"-?[0-9]{1,10}", value):
            result["exit"] = int(value)
    return result


def state_line(job: str, machine: str, result: dict) -> str:
    state = result["state"]
    said = {
        "running": "running",
        "ended": "ended with exit code %s" % result.get("exit"),
        "stopped": "stopped",
        "lost": "lost: no task runs it and it left no exit code (the laptop restarted?)",
        "unknown": "unknown on the machine",
        "offline": "machine offline (the job goes on there: ask again later)",
    }[state]
    return "job %s on %s: %s" % (job, machine, said)


@_serial
def start(name: str, text: str, vm: str | None = None, root: Path | None = None) -> dict:
    """Start `text` as a long job on the machine `name`. Returns {"job", "log"}. Raises WinlabError, Offline."""
    root = root or _root()
    try:
        root, code, machine = _session(name, root)
        check_vm(vm)
        check_text(text)
    except WinlabError as err:
        _refused(root, name, "start on %s" % name, err)
        raise
    folder = root / LOG_FOLDER / "jobs"
    while True:
        job = "wl-%s-%04x" % (_now().strftime("%Y%m%d-%H%M%S"), secrets.randbelow(0x10000))
        if not (folder / ("%s.json" % job)).exists():
            break
    head = ["job %s" % job, "command:"] + ["    " + line for line in text.rstrip("\n").split("\n")]
    title = "start on %s%s" % (name, " in vm %s" % vm if vm else "")
    transport = _connect(root, name, title, machine)
    try:
        exit_code, out, err = transport.run(job_start_script(job, text, vm), timeout=120)
    except Offline as exc:
        log(root, name, title, head + ["offline: %s" % exc])
        raise
    finally:
        transport.close()
    found = dict(line.strip().split("=", 1) for line in strip_clixml(out).splitlines() if "=" in line)
    winlog = found.get("log", "")
    if exit_code != 0 or found.get("started") != job or not _job_winlog(winlog, job):
        err_text, note = landed(err)
        lines = head + ["exit %d: the job did not start" % exit_code]
        lines += ["stderr: %s" % note] if err_text is None else _block("stderr", err_text)
        path = log(root, name, title, lines)
        raise WinlabError("the job did not start on %s (exit %d): see %s" % (name, exit_code, path.relative_to(root)))
    _write_json(folder / ("%s.json" % job), {
        "job": job, "machine": name, "project": code, "vm": vm, "started": _stamp(), "winlog": winlog,
        "log_offset": 0, "state": "running"})
    log(root, name, title, head + ["started"])
    return {"job": job, "log": (folder / ("%s.log" % job)).relative_to(root)}


def _job_started(record: dict) -> str | None:
    """The start of the job as a stamp, for the window check after the window ended; None when it is no stamp."""
    value = record.get("started")
    try:
        return value if isinstance(value, str) and winlab._stamp(winlab._parse(value)) == value else None
    except ValueError:
        return None


def _job_call(job: str, root: Path | None, script, op: str, quiet: bool = False) -> tuple[Path, dict, dict]:
    root = root or _root()
    path, record = _job_record(root, job)
    name = record["machine"]
    try:
        root, code, machine = _session(name, root, _job_started(record))
        transport = _connect(root, name, "%s of job %s" % (op, job), machine)
    except WinlabError as err:
        _refused(root, name, "%s of job %s" % (op, job), err)
        raise
    except Offline:
        result = {"state": "offline", "exit": None}
    else:
        try:
            exit_code, out, _err = transport.run(script(job), timeout=60)
            result = _parse_state(out) if exit_code == 0 else {"state": "unknown", "exit": None}
        except Offline:
            result = {"state": "offline", "exit": None}
        finally:
            transport.close()
    if result["state"] != "offline":
        record["state"] = result["state"]
        record["exit"] = result["exit"]
        if result["state"] != "running" and "ended" not in record:
            record["ended"] = _stamp()
        _write_json(path, record)
    if not quiet:
        log(root, name, "%s of job %s" % (op, job), [state_line(job, name, result)])
    return root, record, result


@_serial
def status(job: str, root: Path | None = None) -> dict:
    """{"state": running|ended|stopped|lost|unknown|offline, "exit": N or None, "machine"}. Raises WinlabError."""
    _root_, record, result = _job_call(job, root, job_status_script, "status")
    return dict(result, machine=record["machine"])


@_serial
def _poll(job: str, root: Path | None = None) -> dict:
    _root_, record, result = _job_call(job, root, job_status_script, "status", quiet=True)
    return dict(result, machine=record["machine"], started=record.get("started"), ended=record.get("ended"))


def wait(job: str, timeout: float = WAIT_TIMEOUT, root: Path | None = None) -> dict:
    """Block until the job is not running: status after 2 s, 4 s and so on up to 60 s (an offline machine is asked
    again, the job goes on there). Returns the state as status does with "waited" (seconds) and "timed_out" (the
    job still runs or the machine was offline when `timeout` ran out). Only the first and the last answer go to the
    log. Raises WinlabError."""
    root = root or _root()
    slept, delay = 0.0, WAIT_FIRST
    began = time.monotonic()
    first = True
    while True:
        result = _poll(job, root)
        if first:
            log(root, result["machine"], "wait for job %s" % job, [state_line(job, result["machine"], result)])
            first = False
        waited = max(slept, time.monotonic() - began)
        done = result["state"] not in ("running", "offline")
        if done or waited >= timeout:
            result = dict(result, waited=int(waited), timed_out=not done)
            log(root, result["machine"], "wait for job %s" % job,
                [state_line(job, result["machine"], result), "waited %d s%s" % (
                    result["waited"], ", gave up: the job goes on" if result["timed_out"] else "")])
            return result
        step = min(delay, max(timeout - waited, 0.0))
        _pause(step)
        slept += step
        delay = min(delay * 2, WAIT_LAST)


@_serial
def stop(job: str, root: Path | None = None) -> dict:
    _root_, record, result = _job_call(job, root, job_stop_script, "stop")
    return dict(result, machine=record["machine"])


@_serial
def job_log(job: str, root: Path | None = None) -> dict:
    """The part of the job's log since the last call: {"text" (None when withheld), "note", "bytes", "more", "log"}.
    It is appended to notes/winlab/jobs/<JOB>.log. Raises WinlabError, Offline."""
    root = root or _root()
    path, record = _job_record(root, job)
    name = record["machine"]
    winlog = str(record.get("winlog", ""))
    try:
        root, code, machine = _session(name, root, _job_started(record))
        if not WINPATH_RE.fullmatch(winlog):
            raise WinlabError("the record of job %s names no log file" % job)
    except WinlabError as err:
        _refused(root, name, "log of job %s" % job, err)
        raise
    transport = _connect(root, name, "log of job %s" % job, machine)
    offset = int(record.get("log_offset") or 0)
    try:
        st = transport.stat(winlog)
        size = st["size"] if st else 0
        if size < offset:
            offset = 0
        data = bytearray()
        while offset + len(data) < size and len(data) < LOG_READ:
            piece = transport.get_chunk(winlog, offset + len(data), min(CHUNK, size - offset - len(data),
                                                                      LOG_READ - len(data)))
            if not piece:
                break
            data += piece
    except Offline as exc:
        log(root, name, "log of job %s" % job, ["offline: %s" % exc])
        raise
    finally:
        transport.close()
    more = offset + len(data) < size
    consumed = len(data)
    discarding = record.get("log_discard_line", False)
    if discarding:
        end = data.find(b"\n")
        if end < 0:
            data = bytearray()
        else:
            data = data[end + 1:]
            discarding = False
    if more and data:
        end = data.rfind(b"\n")
        if end < 0:
            data = bytearray()
            discarding = True
        else:
            consumed -= len(data) - end - 1
            data = data[:end + 1]
    record["log_discard_line"] = discarding
    if offset == 0 and data.startswith(b"\xef\xbb\xbf"):
        text = bytes(data[3:]).decode("utf-8", "replace")
    else:
        text = bytes(data).decode("utf-8", "replace")
    clean, note = landed(text)
    clean = plain_credential_line(clean)
    record["log_offset"] = offset + consumed
    _write_json(path, record)
    joblog = root / LOG_FOLDER / "jobs" / ("%s.log" % job)
    if data:
        if clean is None:
            _append(joblog, ["## %s %d bytes %s" % (_stamp(), len(data), note)])
        elif clean.strip():
            _append(joblog, [clean])
    log(root, name, "log of job %s" % job, ["%d new bytes%s%s" % (len(data), ", " + note if note else "",
                                                                  ", more to come" if more else "")])
    return {"text": clean, "note": note, "bytes": consumed, "more": more, "log": joblog.relative_to(root)}


# --------------------------------------------------------------------------- files


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for block in iter(lambda: fh.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def _record_path(root: Path, way: str, machine: str, winpath: str) -> Path:
    key = hashlib.sha256(("%s\n%s" % (machine, winpath.lower())).encode("utf-8")).hexdigest()[:16]
    return root / LOG_FOLDER / "transfers" / ("%s-%s-%s.json" % (way, machine, key))


def source(root: Path, file: str) -> Path:
    """The file of winlab-out/ that `file` names. Refused: a file elsewhere (input/, the vault, the shared folders),
    a link, a hard link, anything but a regular file."""
    from awb import config

    given = Path(file)
    path = given if given.is_absolute() else Path.cwd() / given
    real = Path(os.path.realpath(path))
    p = config.paths()
    for folder, what in ((root / "input", "a file of input/ (customer material)"),
                         (p.vault, "a file of the vault"), (p.shared, "a file of the shared folder"),
                         (p.kb, "a file of the knowledge base")):
        if _inside(real, folder):
            raise WinlabError("%s never goes to a lab machine: put takes files of %s/ only" % (what, OUT_FOLDER))
    out = root / OUT_FOLDER
    if out.is_symlink() or not out.is_dir() or not _inside(out, root):
        raise WinlabError("put takes files of the project's own folder %s/ (a folder, not a link)" % OUT_FOLDER)
    if not _inside(real, out) or real == Path(os.path.realpath(out)):
        raise WinlabError("put takes files of %s/ of the project only" % OUT_FOLDER)
    here = Path(os.path.abspath(root))
    try:
        parts = Path(os.path.abspath(path)).relative_to(here).parts
    except ValueError:
        parts = ()
    for part in parts:
        here = here / part
        if here.is_symlink():
            raise WinlabError("put takes the file itself, not a link to it")
    if path.is_symlink():
        raise WinlabError("put takes the file itself, not a link to it")
    st = os.stat(real)
    if not stat.S_ISREG(st.st_mode):
        raise WinlabError("put takes a regular file")
    if st.st_nlink > 1:
        raise WinlabError("the file has a second name elsewhere (a hard link): copy it into %s/" % OUT_FOLDER)
    return real


def _piece_text(data: bytes) -> str:
    """The text a piece of a file shows: UTF-8 where it decodes, every other byte and control character a newline."""
    text = data.decode("utf-8", "replace")
    return re.sub(r"[\x00-\x08\x0b-\x1f\x7f\ufffd]+", "\n", text)


def disk_image(path: Path, size: int, suffix: str | None = None) -> str | None:
    """The kind of disk image the file at `path` is (None for any other file). Its name ends in the suffix of the
    kind (any case) and its first bytes carry the format's own signature: vhdx "vhdxfile", vhd "conectix" at offset 0
    or in the footer of the last 512 bytes, vmdk "KDMV" or the text descriptor, qcow2 "QFI\\xfb", iso "CD001" at
    offset 0x8001. A raw .img has no signature: it is one by its name when its size is a whole number of sectors."""
    kind = DISK_IMAGES.get((path.suffix if suffix is None else suffix).lower())
    if kind is None:
        return None
    if kind == "img":
        return kind if size % SECTOR == 0 else None
    with open(path, "rb") as fh:
        head = fh.read(0x8006)
        fh.seek(max(0, size - SECTOR))
        foot = fh.read(SECTOR)
    signed = {"vhdx": head.startswith(b"vhdxfile"),
              "vhd": head.startswith(b"conectix") or foot.startswith(b"conectix"),
              "vmdk": head.startswith((b"KDMV", b"# Disk DescriptorFile")),
              "qcow2": head.startswith(b"QFI\xfb"),
              "iso": head[0x8001:0x8006] == b"CD001"}
    return kind if signed[kind] else None


# The secret check of a disk image. On 4 MB of random bytes the check of _secret_count takes about 27 s: no pattern
# backtracks, but some 45 patterns of the gate and of awb/labsecrets.py each scan every position of the piece's
# text (0.2 to 1.2 s apiece), the table and key pair rules walk a million short lines in Python and shorten and
# _secret_hits both run the gate's detectors. Noise has no secret, so _image_secret_count runs the same check over
# the lines that can carry one with the lines around them. Only when that finds any does the whole piece go
# through it again, for the count and to stay exactly as strict. A line can carry a secret when it holds a word of
# a secret key or a secret pattern (case folded and without the separators the gate's key rule drops), a run of
# 15 characters without a space (every token, key, hash and pgpass line of the gate), a literal of the flag and
# URL patterns on a line long enough for its value or a table row with a value cell. A key word of two letters
# (ak, sk, pw) counts on a line of 9 characters or more: a value of 16 for it on a later line is such a run
# itself; a table row has its own rule.
_CONTEXT = 3
"""Lines kept before and after a seed line, blank lines not counted: the gate takes a value up to three lines below
its key and a key pair two lines apart; the separator of a password may stand on a line of its own."""
_SHORT_ATOM_LINE = 9
_LONG_LINE_RE = re.compile(r"[^\n]{%d,}" % _SHORT_ATOM_LINE)
_RUN_RE = re.compile(r"\S{15}")
_CELL_RE = re.compile(r"\S{8}")
_SEED_WORDS = ("pwd", "kennwort", "access", "signature", "bearer", "identifiedby", "converttosecurestring",
               "begin", "privatelines", "парол")
"""The words of the secret patterns of the gate, labsecrets and _PS_PLAIN_RE beside the gate's key words, case
folded and squeezed (identified by, ConvertTo-SecureString, Private-Lines)."""
_SEED_LITERALS = (("-p", 21), ("-u", 13), ("--creds", 13), ("://", 14), ('"kty"', 0))
"""A literal of a pattern without a key word and the shortest line that holds it with its secret: a -p flag with
a quoted value, -u with a user and a password, the userinfo of a URL and the "kty" a JWK needs anywhere in the text."""
_SEEDS: list = []


def _seed_atoms() -> tuple[tuple[str, ...], tuple[str, ...], tuple[str, ...]]:
    """(words, words of two letters, characters squeezed out) for _seed_lines."""
    if not _SEEDS:
        from awb import gate

        words = set(gate._SECRET_PARTS) | {w.replace("_", "") for w in gate._SECRET_WORDS} | set(_SEED_WORDS)
        squeezed = tuple(sorted({chr(c) for c in range(0x110000) if chr(c).isspace()} - {"\n"})) + tuple("_.:-")
        _SEEDS.append((tuple(sorted(w for w in words if len(w) > 2)), tuple(sorted(w for w in words if len(w) <= 2)),
                       squeezed))
    return _SEEDS[0]


def _squeezed(text: str, chars: tuple[str, ...]) -> str:
    # (?i) takes the dotless i and the dotted capital I for an i; casefold makes the second i and a combining dot
    folded = text.casefold().replace("ı", "i").replace("i̇", "i")
    for ch in chars:
        folded = folded.replace(ch, "")
    return folded


def _found_lines(text: str, word: str, out: set[int]) -> None:
    """The line of every place `word` stands in `text`, into `out`."""
    line, last = 0, 0
    at = text.find(word)
    while at >= 0:
        line += text.count("\n", last, at)
        last = at
        out.add(line)
        at = text.find(word, at + 1)


def _seed_lines(text: str, lines: list[str]) -> set[int]:
    """The lines of `text` that can carry a secret (see above)."""
    from awb import gate

    words, short, chars = _seed_atoms()
    folded = _squeezed(text, chars)
    seeds: set[int] = set()
    for word in words:
        _found_lines(folded, word, seeds)
    for literal, shortest in _SEED_LITERALS:
        found: set[int] = set()
        _found_lines(text, literal, found)
        seeds.update(i for i in found if len(lines[i]) >= shortest)
    i, last = 0, 0
    for m in _LONG_LINE_RE.finditer(text):
        i += text.count("\n", last, m.start())
        last = m.start()
        line = m.group(0)
        if i in seeds:
            continue
        if (len(line) >= 15 and _RUN_RE.search(line)) \
                or (any(d in line for d in gate._TABLE_DELIMITERS) and _CELL_RE.search(line)):
            seeds.add(i)
        else:
            low = _squeezed(line, chars)
            if any(w in low for w in short):
                seeds.add(i)
    return seeds


def _secret_lines(text: str) -> str:
    """The seed lines of `text` with _CONTEXT lines around each and, for a line with a table delimiter, the whole
    run of such lines it stands in and the line on either side (where the gate's table rule starts again). Each
    stretch in between becomes one empty line."""
    from awb import gate

    lines = text.split("\n")
    n = len(lines)
    keep = bytearray(n)

    def table(i: int) -> bool:
        return any(d in lines[i] for d in gate._TABLE_DELIMITERS)

    run_end = -1
    for seed in sorted(_seed_lines(text, lines)):
        keep[seed] = 1
        for step in (-1, 1):
            i, left = seed, _CONTEXT
            while left and 0 <= i + step < n:
                i += step
                keep[i] = 1
                if lines[i].strip():
                    left -= 1
        if seed > run_end and table(seed):
            lo = seed
            while lo > 0 and table(lo - 1):
                lo -= 1
            run_end = seed
            while run_end + 1 < n and table(run_end + 1):
                run_end += 1
            a, b = max(0, lo - 1), min(n, run_end + 2)
            keep[a:b] = b"\x01" * (b - a)
    out: list[str] = []
    a = keep.find(1)
    while a >= 0:
        b = keep.find(0, a)
        b = n if b < 0 else b
        if out:
            out.append("")
        out.extend(lines[a:b])
        a = keep.find(1, b)
    return "\n".join(out)


def _image_secret_count(text: str) -> int:
    """_secret_count of a piece of a disk image at transfer speed: over its _secret_lines first and over the whole
    piece only when they carry a secret."""
    lines = _secret_lines(text)
    if lines == text:
        return _secret_count(text)
    return _secret_count(text) if _secret_count(lines) else 0


def check_kind(path: Path, size: int) -> str:
    """Which check a put of the file at `path` runs, for its log line."""
    if not content_checks():
        return "no check (winlab_checks = off, the owner's word of 2026-11-25)"
    image = disk_image(path, size)
    return "secret check only, disk image %s" % image if image else "name and secret check"


def check_file(path: Path, size: int) -> str:
    """The name check and the secret check over the file at `path` before it goes to a lab machine; returns
    check_kind. A file up to CHECK_WHOLE_MAX goes whole to the name check, which opens office files and archives.
    Every file but a plain text goes to it again in pieces of its text (a binary file can come back clean from the
    whole check); a compressed file the check cannot open is refused. The secret check reads the pieces of every
    file. A disk image (disk_image) skips the name check and gets the secret check of _image_secret_count.
    With winlab_checks = off (the owner's word of 2026-11-25) nothing is checked."""
    from awb import hooks

    if not content_checks():
        return check_kind(path, size)

    image = disk_image(path, size)
    opaque, plain = True, False
    if size <= CHECK_WHOLE_MAX and not image:
        try:
            hits = hooks.file_hits(path)
        except hooks.Unavailable:
            raise WinlabError("the name check did not run: nothing was sent") from None
        names = [h for h in hits if h.get("cls") in NAME_CLASSES]
        if names:
            raise WinlabError("the file carries %d registered name(s): nothing was sent" % len(names))
        opaque = any(h.get("cls") == "opaque" for h in hits)
        data = path.read_bytes()
        try:
            plain = b"\x00" not in data[:8192] and bool(data.decode("utf-8"))
        except UnicodeDecodeError:
            plain = False
    with open(path, "rb") as fh:
        if opaque and fh.read(8).startswith(_COMPRESSED):
            raise WinlabError("the check cannot open this compressed file: put the files in it one by one")
        fh.seek(0)
        tail = ""
        while True:
            data = fh.read(PIECE)
            if not data:
                break
            text = tail + _piece_text(data)
            # the name check read a plain text whole; any other file it reads again in the pieces of its text
            if not plain and not image:
                names = _name_count(text)
                if names:
                    raise WinlabError("the file carries %d registered name(s): nothing was sent" % names)
            found = _image_secret_count(text) if image else _secret_count(text)
            if found:
                raise WinlabError("the file carries %d secret(s): no password and no key goes to a lab machine, "
                                  "nothing was sent" % found)
            tail = text[-OVERLAP:]
    return check_kind(path, size)


def _size(n: int) -> str:
    for unit, div in (("GB", 1024 ** 3), ("MB", 1024 ** 2), ("KB", 1024)):
        if n >= div:
            return "%.1f %s" % (n / div, unit)
    return "%d bytes" % n


def _broken(way: str, done: int, size: int, exc: Exception) -> Offline:
    return Offline("the %s broke at %s of %s (%s): the same command again resumes it" % (way, _size(done),
                                                                                         _size(size), exc))


NO_WINDOW = "no-window"
"""The log name of a put or get refused before a machine was found: notes/winlab/<date>-no-window.log."""


def _pick(root: Path, machine: str | None) -> str:
    """The machine of the project's open window; `machine` when given."""
    if machine is not None:
        return machine
    code = _code(root)
    if _local_registry():
        names = [n for n, w in winlab.windows() if w["project"] == code]
    else:
        names = _broker_names(_broker_call({"op": "windows", "project": code, "token": _token(root)})[0])
    if not names:
        raise WinlabError("no lab machine has an open window for %s (the owner: awb winlab open NAME --minutes N "
                          "--project %s)" % (code, code))
    if len(names) > 1:
        raise WinlabError("windows are open for %s on %s: name one with --machine" % (code, ", ".join(names)))
    return names[0]


@_serial
def put(file: str, winpath: str, machine: str | None = None, root: Path | None = None,
        overwrite: bool = False) -> dict:
    """The file `file` of winlab-out/ to `winpath` on the machine of the project's window. Returns {"machine",
    "size", "sha256", "resumed", "check", "log"}. Raises WinlabError (refused or failed), Offline (broken: run it
    again). A file at `winpath` is replaced with `overwrite`, else refused."""
    root = root or _root()
    try:
        machine = _pick(root, machine)
        root, _code_, found = _session(machine, root)
        src = source(root, file)
        check_text(src.relative_to(os.path.realpath(root)).as_posix(), "the name of FILE")
        check_winpath(winpath)
    except WinlabError as err:
        _refused(root, machine or NO_WINDOW, "put to %s" % (machine or "a lab machine"), err)
        raise
    try:
        return _put(root, found["name"], found, src, winpath, overwrite)
    except WinlabError as err:
        log(root, machine, "put to %s" % machine, ["to %s" % winpath, "failed: %s" % err])
        raise


def _put(root: Path, name: str, machine: dict, src: Path, winpath: str, overwrite: bool = False) -> dict:
    with winlab._parent(src) as folder:
        fd = winlab._regular(folder, src.name)
        with os.fdopen(fd, "rb") as source_file, tempfile.TemporaryDirectory(prefix="awb-winlab-") as scratch:
            st = os.fstat(source_file.fileno())
            snapshot = Path(scratch) / src.name
            with open(snapshot, "xb") as copy:
                shutil.copyfileobj(source_file, copy, CHUNK)
            after = os.fstat(source_file.fileno())
            if (after.st_size, after.st_mtime_ns, after.st_ctime_ns) != (st.st_size, st.st_mtime_ns, st.st_ctime_ns):
                raise WinlabError("the source changed while it was copied: nothing was sent")
            return _put_snapshot(root, name, machine, src, winpath, snapshot, st, overwrite)


def _put_snapshot(root: Path, name: str, machine: dict, src: Path, winpath: str, snapshot: Path, st,
                  overwrite: bool = False) -> dict:
    rel = src.relative_to(os.path.realpath(root)).as_posix()
    size, ident = st.st_size, [st.st_size, st.st_mtime_ns, st.st_ino]
    rpath = _record_path(root, "put", name, winpath)
    record = _read_json(rpath) or {}
    same = record.get("source") == rel and record.get("ident") == ident and record.get("winpath") == winpath
    sha = _sha256(snapshot)
    checked = check_kind(snapshot, size)
    check_file(snapshot, size)
    record ={"source": rel, "ident": ident, "winpath": winpath, "sha256": sha, "checked": sha, "size": size}
    _write_json(rpath, record)
    part = winpath + PART
    title = "put %s to %s" % (rel, name)
    transport = _connect(root, name, title, machine)
    began = _stamp()
    try:
        if not overwrite and transport.stat(winpath) is not None:
            raise WinlabError("%s exists on %s: put does not overwrite, choose another WINPATH" % (winpath, name))
        there = transport.stat(part) if same else None
        offset = there["size"] if there and there["size"] <= size else 0
        resumed = offset
        try:
            _send(transport, snapshot, part, offset, size)
        except Offline as exc:
            raise _broken("put", _part_size(transport, part), size, exc) from None
        final = transport.stat(part, digest=True)
        if not final or final.get("size") != size or str(final.get("sha256", "")).lower() != sha:
            transport.run(file_remove_script(part), timeout=60)
            _unlink(rpath)
            raise WinlabError("the SHA-256 of %s on %s does not match the file: the part was deleted, nothing kept"
                              % (winpath, name))
        exit_code, _out, _err = transport.run(file_move_script(part, winpath, overwrite), timeout=60)
        if exit_code != 0:
            raise WinlabError("the part on %s could not be renamed to %s (exit %d): the same put again tries it"
                              % (name, winpath, exit_code))
    except Offline as exc:
        log(root, name, title, ["to %s" % winpath, "broken: %s" % exc])
        raise
    finally:
        transport.close()
    _unlink(rpath)
    path = log(root, name, title, ["to %s" % winpath, "started %s, ended %s" % (began, _stamp()),
                                   "%s, sha256 %s, %s, %s" % (_size(size), sha, "resumed at %s" % _size(resumed)
                                                              if resumed else "whole", checked)])
    return {"machine": name, "size": size, "sha256": sha, "resumed": resumed, "check": checked,
            "log": path.relative_to(root)}


def _part_size(transport: Transport, part: str) -> int:
    try:
        st = transport.stat(part)
    except Offline:
        return 0
    return st["size"] if st else 0


def _send(transport: Transport, src: Path, part: str, offset: int, size: int) -> None:
    tries, verify, empty = 0, False, size == 0
    with open(src, "rb") as fh:
        while offset < size or empty:
            try:
                if verify:
                    st = transport.stat(part)
                    offset = st["size"] if st and st["size"] <= size else 0
                    verify = False
                    continue
                fh.seek(offset)
                data = fh.read(min(CHUNK, size - offset))
                if not data and offset < size:
                    raise WinlabError("the source got shorter during the put")
                after = transport.put_chunk(part, offset, data)
                if after != offset + len(data):
                    raise WinlabError("the machine holds %d bytes of the part, not %d" % (after, offset + len(data)))
                offset, tries, empty = after, 0, False
            except Offline:
                tries += 1
                if tries > RETRIES:
                    raise
                _pause(2 ** tries)
                verify = True


def target(root: Path, file: str, overwrite: bool = False) -> Path:
    """Where get writes `file`: inside the project, not in input/ or .git, not through a link; over a file only
    with `overwrite` (a regular file; a folder or a link never)."""
    given = Path(file)
    path = Path(os.path.abspath(given if given.is_absolute() else Path.cwd() / given))
    parent = Path(os.path.realpath(path.parent))
    if not _inside(parent, root):
        raise WinlabError("get writes into the project folder only")
    rel = (parent / path.name).relative_to(os.path.realpath(root))
    if rel.parts[:1] in (("input",), (".git",)) or not rel.parts:
        raise WinlabError("get never writes into input/ (customer material comes through awb intake) or .git")
    check_text(rel.as_posix(), "the name of FILE")
    try:
        with winlab._parent(path):
            pass
    except FileNotFoundError:
        pass
    if os.path.lexists(parent / path.name):
        if not overwrite:
            raise WinlabError("%s exists: get does not overwrite, choose another FILE or give --overwrite"
                              % rel.as_posix())
        if os.path.isdir(parent / path.name) or os.path.islink(parent / path.name):
            raise WinlabError("%s is a folder or a link: --overwrite replaces a regular file only" % rel.as_posix())
    if path.name.endswith(PART):
        raise WinlabError("FILE may not end in %s" % PART)
    check_text(path.name, "the name of FILE")
    return parent / path.name


@_serial
def get(winpath: str, file: str, machine: str | None = None, root: Path | None = None, check: bool = False,
        overwrite: bool = False) -> dict:
    """`winpath` of the machine of the project's window into the project as `file`. Returns {"machine", "size",
    "sha256", "resumed", "file", "log"}. Raises WinlabError, Offline (broken: run it again). A disk image is our own
    lab output and gets no name check after the SHA-256 compare (OWN_IMAGE); `check` runs it. `overwrite` replaces
    an existing FILE once the received file passed every check."""
    # every module the checks and the log need is loaded before the transfer: a deploy during an hour of transfer
    # replaces the installed tree, and a module first imported at the end then fails (tcp-bytg 2026-11-25)
    from awb import config, gate, hooks, labsecrets, lablog, ledger  # noqa: F401
    root = root or _root()
    try:
        machine = _pick(root, machine)
        root, _code_, found = _session(machine, root)
        check_winpath(winpath)
        dst = target(root, file, overwrite)
    except WinlabError as err:
        _refused(root, machine or NO_WINDOW, "get from %s" % (machine or "a lab machine"), err)
        raise
    try:
        return _get(root, found["name"], found, winpath, dst, check, overwrite)
    except WinlabError as err:
        log(root, machine, "get from %s" % machine, ["from %s" % winpath, "failed: %s" % err])
        raise
    except Stopped:
        log(root, machine, "get from %s" % machine, ["from %s" % winpath, "stopped by a signal during the transfer: "
                                                     "the same get again resumes it"])
        raise


def _get(root: Path, name: str, machine: dict, winpath: str, dst: Path, check: bool = False,
         overwrite: bool = False) -> dict:
    # Keep the destination directory pinned through transfer, checks and publication.
    with winlab._parent(dst, create=True) as folder:
        return _get_at(root, name, machine, winpath, dst, folder, check, overwrite)


def _get_at(root: Path, name: str, machine: dict, winpath: str, dst: Path, folder: int, check: bool = False,
            overwrite: bool = False) -> dict:
    from awb import hooks

    rel = dst.relative_to(os.path.realpath(root)).as_posix()
    part = dst.name + PART
    rpath = _record_path(root, "get", name, winpath)
    record = _read_json(rpath) or {}
    title = "get %s from %s" % (rel, name)
    transport = _connect(root, name, title, machine)
    began = _stamp()
    try:
        st = transport.stat(winpath)
        if st is None:
            raise WinlabError("there is no file %s on %s" % (winpath, name))
        if not isinstance(st, dict) or type(st.get("size")) is not int or st["size"] < 0:
            raise WinlabError("the machine returned an invalid file size")
        size = st["size"]
        if not _public_window(machine) and _disk_suffix(winpath, dst):
            # a disk image of a window without --public-image gets the secret check after the whole transfer, and a
            # hit deletes it: said before the transfer starts (tcp-bytg 2026-11-22: 10.3 GB for 15 minutes, deleted)
            print(DISK_NOTE % (winpath, name), file=sys.stderr, flush=True)
        same = (record.get("winpath") == winpath and record.get("file") == rel and record.get("size") == size
                and record.get("mtime") == st.get("mtime"))
        _reclaim(folder, record, part, size)
        _write_json(rpath, {"winpath": winpath, "file": rel, "size": size, "mtime": st.get("mtime"), "machine": name,
                            **({"overwrite": True} if overwrite else {})})
        fd = winlab._regular(folder, part, os.O_RDWR | os.O_CREAT)
        with os.fdopen(fd, "r+b") as fh:
            have = os.fstat(fh.fileno()).st_size
            offset = have if same and have <= size else 0
            fh.truncate(offset)
            resumed = offset
            try:
                _receive(transport, fh, winpath, offset, size)
            except Offline as exc:
                raise _broken("get", os.fstat(fh.fileno()).st_size, size, exc) from None
        final = transport.stat(winpath, digest=True)
    except Offline as exc:
        log(root, name, title, ["from %s" % winpath, "broken: %s" % exc])
        raise
    finally:
        transport.close()
    expected = str(final.get("sha256", "")).lower() if final and final.get("size") == size else None
    if expected is not None and not re.fullmatch(r"[0-9a-f]{64}", expected):
        expected = None                     # no SHA-256 from the machine: kept, never deleted (tcp-bytg ask 11)
    public_window = _public_window(machine)
    rec = dict(record if same else {}, winpath=winpath, file=rel, size=size, mtime=st.get("mtime"), machine=name)
    rec.pop("overwrite", None)
    if overwrite:
        rec["overwrite"] = True
    mismatch = "the SHA-256 of the copy does not match %s on %s" % (winpath, name)
    own = not check and _own_image(folder, part, dst, size)
    sha, public = _check_and_publish(rpath, rec, folder, part, dst, size, expected, public_window, mismatch,
                                      names=not own)
    _unlink(rpath)
    path = log(root, name, title, ["from %s" % winpath, "started %s, ended %s" % (began, _stamp()),
                                   "%s, sha256 %s, %s" % (_size(size), sha, "resumed at %s" % _size(resumed)
                                                          if resumed else "whole")]
               + (["secret check skipped: a public image window of the owner"] if public else [])
               + ([OWN_IMAGE] if own else []))
    if own:
        _ledger_own(root, rel, size, sha)
    return {"machine": name, "size": size, "sha256": sha, "resumed": resumed, "file": rel,
            "log": path.relative_to(root)}


DISK_NOTE = ("note: %s is a disk image and the window of %s was opened without --public-image: after the whole "
             "transfer it gets the secret check, and a secret in it deletes the copy. For a public image built in the "
             "lab the owner opens the window with `awb winlab open NAME --minutes N --project CODE --public-image` in "
             "his own terminal; stop with Ctrl-C now to do that first")
"""The line before the transfer of a disk image in a window without --public-image (tcp-bytg 2026-11-22)."""


def _public_window(machine: dict) -> bool:
    return machine.get("public_image") is True or (machine.get("window") or {}).get("public_image") is True


def _disk_suffix(winpath: str, dst: Path) -> bool:
    """True when the Windows path or the target ends in a disk image suffix (DISK_IMAGES, any case)."""
    tail = winpath.replace("\\", "/").rsplit("/", 1)[-1]
    return any(n.lower().endswith(tuple(DISK_IMAGES)) for n in (tail, dst.name))


OWN_IMAGE = "disk image from the lab machine: own lab output, no name check (owner decision 2026-11-20)"
"""The log and ledger line of a disk image a get published without the name check (the secret check stays unless
the window is a public image window). The rule is to check once at entry: customer images enter through awb intake,
and an image the lab built is our own output. `awb winlab get --check` runs the name check too."""


def _own_image(folder: int, part: str, dst: Path, size: int) -> bool:
    """True when the received part is a disk image (disk_image: the suffix of `dst` and the signature of its
    format)."""
    return disk_image(Path("/proc/self/fd/%d" % folder) / part, size, dst.suffix) is not None


def _ledger_own(root: Path, rel: str, size: int, sha: str) -> None:
    """One ledger line of the project; a ledger that cannot be written leaves the get as it is (the log has it)."""
    try:
        from awb import ledger

        ledger.add("other", "%s, %s, sha256 %s: %s" % (rel, _size(size), sha, OWN_IMAGE), project=_code(root))
    except Exception as exc:
        print("awb winlab: the ledger line was not written (%s)" % type(exc).__name__, file=sys.stderr)


def _piece_names(text: str) -> int:
    """The registered names in a piece of a received file; raises hooks.Unavailable when the check cannot run."""
    from awb import hooks

    return len([h for h in hooks.name_hits(text) if h.get("cls") in NAME_CLASSES])


# The name check of a large disk image. A name in an image is text: UTF-8 (ASCII included) or UTF-16 (Windows keeps
# its strings in UTF-16LE). _text_runs cuts every run of text characters out of the bytes, in C speed, with tab, line
# end and space inside a run so a name over two words or two lines stays whole. Zero blocks and binary noise give no
# run of RUN_MIN; the same string in thousands of system files is checked once (_TextRuns keeps a set), and the
# unique runs go to the name check in calls of RUN_BATCH characters, paced under the daemon's byte budget.
_RUN_UTF8 = bytes(b if b in (9, 10, 13) or 32 <= b != 127 else 0 for b in range(256))
"""Every control byte but tab and line end becomes NUL; bytes of 0x80 and up stay for the UTF-8 decode."""
_RUN_HI0 = bytes(255 if b == 0 else 0 for b in range(256))
_RUN_HI1 = bytes(255 if b == 1 else 0 for b in range(256))
_RUN_LO = bytes(255 if b in (9, 10, 13) or 32 <= b < 127 or b >= 0xA0 else 0 for b in range(256))
"""A UTF-16 character of text: U+0009, U+000A, U+000D, U+0020 to U+007E, U+00A0 to U+00FF and U+0100 to U+01FF."""


def _mask(op, a: bytes, b: bytes) -> bytes:
    n = len(a)
    return op(int.from_bytes(a, "little"), int.from_bytes(b, "little")).to_bytes(n, "little")


def _collapse(text, nul):
    for k in (256, 16, 4, 2, 2, 2):
        text = text.replace(nul * k, nul)
    return text


def _text_runs(data: bytes) -> list[str]:
    """Every run of text in `data`: UTF-8 (every valid sequence, an invalid byte cuts) and UTF-16 little and big
    endian at both alignments. A run may be empty or short; the caller drops those."""
    text = _collapse(data.translate(_RUN_UTF8), b"\0").decode("utf-8", "replace")
    out = text.replace("�", "\0").split("\0")
    for a in (0, 1):
        x, y = data[a::2], data[a + 1::2]
        x = x[:len(y)]
        for lo, hi in ((x, y), (y, x)):
            keep = _mask(int.__or__, _mask(int.__and__, hi.translate(_RUN_HI0), lo.translate(_RUN_LO)),
                         hi.translate(_RUN_HI1))
            if b"\xff\xff\xff" not in keep:
                continue
            buf = bytearray(2 * len(keep))
            buf[0::2] = _mask(int.__and__, lo, keep)
            buf[1::2] = _mask(int.__and__, hi, keep)
            out.extend(_collapse(buf.decode("utf-16-le"), "\0").split("\0"))
    return out


class _TextRuns:
    """The unique text runs of a disk image and their name check. `feed` takes the bytes of a piece (with the last
    RUN_OVERLAP bytes of the one before), `flush` sends what waits in calls of RUN_BATCH characters, `progress` is
    the line for stderr. Raises WinlabError on a registered name, hooks.Unavailable when the check cannot run."""

    def __init__(self, size: int):
        self.size = size
        self.seen: set[str] = set()
        self.seen_chars = 0
        self.waiting: list[str] = []
        self.chars = 0
        self.requests = 0
        self.sent = 0
        self.unique = 0
        self.window: list[tuple[float, int]] = []

    def feed(self, data: bytes) -> None:
        if not data or data.count(0) == len(data):
            return
        new = set(_text_runs(data))
        new.difference_update(self.seen)
        if len(self.seen) + len(new) > RUN_SEEN_MAX or self.seen_chars > RUN_SEEN_CHARS:
            self.seen.clear()
            self.seen_chars = 0
        self.seen.update(new)
        for run in new:
            self.seen_chars += len(run)
            if len(run.strip()) >= RUN_MIN:
                self.waiting.append(run)
                self.chars += len(run) + 1
                self.unique += len(run) + 1

    def full(self) -> bool:
        return self.chars >= RUN_BATCH

    def flush(self) -> None:
        batch, size = [], 0
        for run in self.waiting:
            if batch and size + len(run) + 1 > RUN_BATCH:
                self._send(batch)
                batch, size = [], 0
            batch.append(run)
            size += len(run) + 1
        if batch:
            self._send(batch)
        self.waiting, self.chars = [], 0

    def _send(self, batch: list[str]) -> None:
        text = "\n".join(batch)
        cost = len(json.dumps(text)) + 32
        self._pace(cost)
        self.requests += 1
        self.sent += cost
        if _piece_names(text):
            raise WinlabError("the file carries registered name(s): the copy was deleted")

    def _pace(self, cost: int) -> None:
        """Wait until `cost` more request bytes fit into RUN_PACE_BYTES of the last RUN_PACE_WINDOW seconds."""
        now = time.monotonic()
        self.window = [(t, n) for t, n in self.window if t > now - RUN_PACE_WINDOW]
        while self.window and sum(n for _, n in self.window) + cost > RUN_PACE_BYTES:
            t, _ = self.window.pop(0)
            wait = t + RUN_PACE_WINDOW - time.monotonic()
            if wait > 0:
                _pause(wait)
        self.window.append((time.monotonic(), cost))

    def progress(self, at: int) -> str:
        return ("name check of the disk image: read %s of %s, %d check request(s), %s of unique text, no name so far"
                % (_size(at), _size(self.size), self.requests, _size(self.unique)))


def _check_and_publish(rpath: Path, rec: dict, folder: int, part: str, dst: Path, size: int, expected: str | None,
                       public_window: bool, mismatch: str, names: bool = True) -> tuple[str, bool]:
    """Check the received part in place and publish it as `dst`. Returns (sha256, whether the secret check was
    skipped for a public image). A name or a secret hit and a SHA-256 other than `expected` delete the part and the
    record `rpath`. When the name check cannot run (hooks.Unavailable) the part stays under its private part name
    and the record keeps the SHA-256 and how far the checks got: `awb winlab get WINPATH FILE --recheck` goes on
    from there without a transfer (tcp-bytg 2026-11-19: a 21.9 GB image was deleted after 1 h 45 of transfer)."""
    from awb import hooks

    # The part is renamed to a private name for the checks, so nothing can replace it while they run. A copy did
    # the same at twice the disk: a 21.9 GB image of tcp-bytg (2026-11-19) filled the host and ended in OSError.
    if expected is None:
        # a complete transfer is never deleted for a SHA-256 the machine did not give (tcp-bytg ask 11)
        raise WinlabError(AGAIN_LINE % (rec["file"] + PART, _size(size), "the machine gave no SHA-256 to compare",
                                        rec["winpath"], rec["file"]))
    suffix = dst.suffix if re.fullmatch(r"\.[A-Za-z0-9]{1,16}", dst.suffix) else ""
    staged = ".winlab-" + secrets.token_hex(16) + suffix
    snapshot = Path("/proc/self/fd/%d" % folder) / staged
    winlab._regular(folder, part)                           # a regular file, no link, before it is moved
    # the record names the private name first: a get or a recheck after a kill (SIGKILL, no finally) finds the
    # file there and gives it its part name back (_reclaim, tcp-bytg ask 11)
    rec["staged"] = staged
    _write_json(rpath, rec)
    os.rename(part, staged, src_dir_fd=folder, dst_dir_fd=folder)
    back = True                                             # an unexpected failure gives the part back for a resume
    try:
        try:
            sha = _sha256(snapshot)
        except Stopped:
            raise WinlabError(AGAIN_LINE % (rec["file"] + PART, _size(size),
                                            "stopped by a signal before its SHA-256 was compared", rec["winpath"],
                                            rec["file"])) from None
        if expected != sha:
            back = False
            _unlink(rpath)
            raise WinlabError("%s: the part was deleted, nothing kept" % mismatch)
        done = rec.get("checked")
        start = done if rec.get("sha256") == sha and type(done) is int and 0 < done <= size else 0
        rec.update(sha256=sha, public=public_window, checked=start)
        _write_json(rpath, rec)
        try:
            # A compressed file or a disk image shows no name in the pieces of its bytes, and a piece costs some
            # hundred calls of the rate limited name check (hours for a chunk of an image): it gets the whole
            # check alone, which opens the archive (B-040; tcp-bytg 2026-11-17). A disk image over CHECK_READ_MAX
            # cannot be read whole: it gets the name check per piece, which resumes where it stopped.
            with open(snapshot, "rb") as fh:
                head = fh.read(8)
            compressed = head.startswith(_COMPRESSED)
            image = disk_image(snapshot, size) is not None
            packed = compressed or image
            # the owner's statement on the window (awb winlab open --public-image): public images built in the lab,
            # and vendor programs of the lab machine (an ELF or PE file, tcp-bytg 2026-11-25: the Veeam extract
            # binary carries key-like byte runs); the name check stays for both
            program = head.startswith(_PROGRAM)
            public = (packed or program) and public_window
            whole = size <= CHECK_WHOLE_MAX or compressed or (image and size <= CHECK_READ_MAX)
            piece_names = not packed or not whole
            # a disk image of the lab is own output (OWN_IMAGE): without `names` it gets the secret check alone
            own = image and not names
            if own:
                piece_names, whole = False, False
            # the owner's word of 2026-11-25 ("железобетонно сделать, чтобы при передаче таких файлов ничего не
            # проверялось"): in a window he opened with --public-image a binary file (a NUL byte in its first
            # 64 KiB: a disk image, a program, a Veeam backup .vbk/.vib/.vbm) gets no name and no secret check; only
            # the SHA-256 compare stays. A text file and a compressed archive (zip, gzip, 7z: it may carry documents)
            # keep their checks.
            bare = not content_checks() or (public_window and not compressed and _binary(snapshot))
            if bare:
                public, whole, piece_names = True, False, False
            # A large disk image gets the name check over the unique text runs of its bytes: a few thousand calls
            # for 22 GB instead of some hundred per piece. `checked` then moves only when every run read before it
            # was checked (a mark at least every RUN_MARK bytes), RUN_OVERLAP before the end of what was read.
            runs = _TextRuns(size) if image and not own and not whole and not bare and size > RUNS_MIN else None
            if runs is not None:
                piece_names = False
            if whole and not start:
                names = [h for h in hooks.file_hits(snapshot) if h.get("cls") in NAME_CLASSES]
                if names:
                    raise WinlabError("the file carries %d registered name(s): the copy was deleted" % len(names))
            with open(snapshot, "rb") as fh:
                at = max(0, start - OVERLAP)
                fh.seek(at)
                tail, before, marked, shown = "", b"", at, time.monotonic()
                if runs is not None and start:
                    print("name check of the disk image: resumed at %s of %s" % (_size(start), _size(size)),
                          file=sys.stderr, flush=True)
                elif start:
                    print("checks of the received file: resumed at %s of %s" % (_size(start), _size(size)),
                          file=sys.stderr, flush=True)
                while not bare and (data := fh.read(PIECE)):
                    text = tail + _piece_text(data)
                    if piece_names and _piece_names(text):
                        raise WinlabError("the file carries registered name(s): the copy was deleted")
                    if runs is not None:
                        runs.feed(before + data)
                        before = data[-RUN_OVERLAP:]
                    if not public and (_image_secret_count(text) if packed else _secret_count(text)):
                        raise WinlabError("the file carries secret(s): the copy was deleted")
                    tail = text[-OVERLAP:]
                    at += len(data)
                    if runs is None:
                        if size > CHECK_WHOLE_MAX:
                            rec["checked"] = at
                            _write_json(rpath, rec)
                            if time.monotonic() - shown >= PROGRESS_EVERY:
                                print("checks of the received file: read %s of %s, nothing found so far; it stays "
                                      "unpublished until they end" % (_size(at), _size(size)), file=sys.stderr,
                                      flush=True)
                                shown = time.monotonic()
                        continue
                    if runs.full() or at - marked >= RUN_MARK:
                        runs.flush()
                        marked = at
                        rec["checked"] = max(start, at - RUN_OVERLAP) if at < size else size
                        _write_json(rpath, rec)
                    if time.monotonic() - shown >= PROGRESS_EVERY:
                        print(runs.progress(at), file=sys.stderr, flush=True)
                        shown = time.monotonic()
                if runs is not None:
                    runs.flush()
                    print(runs.progress(at), file=sys.stderr, flush=True)
        except hooks.Unavailable as exc:
            # back stays True: the part keeps its private name, the record its SHA-256 and the progress
            raise WinlabError("the name check did not run (%s): the received file is kept unpublished as %s "
                              "(checked to %s of %s); the same get with --recheck checks it again without a "
                              "transfer" % (exc, rec["file"] + PART, _size(rec.get("checked") or 0), _size(size))
                              ) from None
        except Stopped:
            # a signal cut the checks off (tcp-bytg ask 11): the file is complete, kept, and the checks resume
            raise WinlabError(KEPT_LINE % (rec["file"] + PART, _size(rec.get("checked") or 0), _size(size),
                                           "the checks were stopped by a signal", rec["winpath"], rec["file"])
                              ) from None
        except WinlabError:
            back = False
            _unlink(rpath)
            raise
        # Hard-link publication cannot replace a destination another writer created.
        if rec.get("overwrite") is True:
            # a second private name is linked first and renamed over FILE: the old file stays until then
            fresh = ".winlab-" + secrets.token_hex(16)
            os.link(staged, fresh, src_dir_fd=folder, dst_dir_fd=folder, follow_symlinks=False)
            try:
                os.replace(fresh, dst.name, src_dir_fd=folder, dst_dir_fd=folder)
            except OSError:
                os.unlink(fresh, dir_fd=folder)
                raise
        else:
            os.link(staged, dst.name, src_dir_fd=folder, dst_dir_fd=folder, follow_symlinks=False)
        back = False
    finally:
        try:
            if back:
                os.rename(staged, part, src_dir_fd=folder, dst_dir_fd=folder)
                if rec.pop("staged", None) is not None and os.path.exists(rpath):
                    _write_json(rpath, rec)
            else:
                os.unlink(staged, dir_fd=folder)
        except FileNotFoundError:
            pass
    return sha, public


def _reclaim(folder: int, rec: dict, part: str, size: int) -> bool:
    """The received file back under its part name when a get or a recheck was killed while it was checked under its
    private name (`staged` of the record; tcp-bytg ask 11). A complete file replaces a shorter part a later get
    began; a shorter one is left where it is. True when it moved."""
    staged = rec.get("staged")
    if not isinstance(staged, str) or not STAGED_RE.fullmatch(staged):
        return False
    try:
        fd = winlab._regular(folder, staged)
    except FileNotFoundError:
        return False
    try:
        have = os.fstat(fd).st_size
    finally:
        os.close(fd)
    try:
        fd = winlab._regular(folder, part)
        other = os.fstat(fd).st_size
        os.close(fd)
    except FileNotFoundError:
        other = -1
    if have != size and other >= 0:
        return False
    os.replace(staged, part, src_dir_fd=folder, dst_dir_fd=folder)
    print("awb winlab: the received file of an earlier get that was cut off is back as %s (%s)"
          % (part, _size(have)), file=sys.stderr)
    return True


@_serial
def recheck(winpath: str, file: str, machine: str | None = None, root: Path | None = None,
            check: bool = False) -> dict:
    """The checks of a get again over the part a get kept when the name check could not run, without a transfer
    and without the machine. Returns {"machine", "size", "sha256", "file", "log"}. Raises WinlabError. A kept disk
    image follows the default of get (owner decision 2026-11-20): its size and SHA-256 are verified, it gets no name
    check (OWN_IMAGE) and is published; `check` runs the name check."""
    root = root or _root()
    title = "get --recheck %s" % file
    name = machine
    try:
        check_winpath(winpath)
        dst = target(root, file, overwrite=True)
        rel = dst.relative_to(os.path.realpath(root)).as_posix()
        name, rpath, rec = _kept(root, winpath, rel, machine)
        if os.path.lexists(dst) and rec.get("overwrite") is not True:
            raise WinlabError("%s exists: get does not overwrite, choose another FILE or give --overwrite" % rel)
    except WinlabError as err:
        _refused(root, name or NO_WINDOW, title, err)
        raise
    try:
        with winlab._parent(dst, create=False) as folder:
            part = dst.name + PART
            if _reclaim(folder, rec, part, rec["size"]):
                rec.pop("staged", None)
                _write_json(rpath, rec)
            try:
                fd = winlab._regular(folder, part)
            except FileNotFoundError:
                raise WinlabError("there is no kept part of %s: run the get again" % rel) from None
            have = os.fstat(fd).st_size
            os.close(fd)
            if have != rec["size"]:
                raise WinlabError("the kept part of %s is %s, the get received %s: run the get again"
                                  % (rel, _size(have), _size(rec["size"])))
            own = not check and _own_image(folder, part, dst, rec["size"])
            sha, public = _check_and_publish(rpath, rec, folder, part, dst, rec["size"], rec["sha256"],
                                              rec.get("public") is True,
                                              "the kept part of %s changed since the get" % rel, names=not own)
    except WinlabError as err:
        log(root, name, "get %s from %s" % (rel, name), ["from %s" % winpath, "recheck failed: %s" % err])
        raise
    _unlink(rpath)
    path = log(root, name, "get %s from %s" % (rel, name), ["from %s" % winpath, "%s, sha256 %s, checked again "
                                                             "without a transfer" % (_size(rec["size"]), sha)]
               + (["secret check skipped: a public image window of the owner"] if public else [])
               + ([OWN_IMAGE] if own else []))
    if own:
        _ledger_own(root, rel, rec["size"], sha)
    return {"machine": name, "size": rec["size"], "sha256": sha, "file": rel, "log": path.relative_to(root)}


def _kept(root: Path, winpath: str, rel: str, machine: str | None) -> tuple[str, Path, dict]:
    """(machine, record path, record) of the part a get kept for `winpath` and `rel`."""
    folder = root / LOG_FOLDER / "transfers"
    found = []
    for path in sorted(folder.glob("get-*.json")) if folder.is_dir() else []:
        rec = _read_json(path)
        if not rec or rec.get("winpath") != winpath or rec.get("file") != rel:
            continue
        name = rec.get("machine")
        if not isinstance(name, str) or (machine is not None and name != machine):
            continue
        if not winlab.NAME_RE.fullmatch(name) or _record_path(root, "get", name, winpath) != path:
            continue
        if not isinstance(rec.get("sha256"), str) or type(rec.get("size")) is not int:
            continue
        found.append((name, path, rec))
    if not found:
        raise WinlabError("no get kept a part of %s from %s: nothing to check again" % (rel, winpath))
    if len(found) > 1:
        raise WinlabError("gets from %s machines kept a part of %s: name one with --machine" % (len(found), rel))
    return found[0]


def _receive(transport: Transport, fh, winpath: str, offset: int, size: int) -> None:
    tries = 0
    while offset < size:
        try:
            data = transport.get_chunk(winpath, offset, min(CHUNK, size - offset))
        except Offline:
            tries += 1
            if tries > RETRIES:
                raise
            _pause(2 ** tries)
            continue
        if len(data) > min(CHUNK, size - offset):
            raise WinlabError("the machine returned too many bytes")
        if not data:
            raise WinlabError("%s got shorter on the machine during the get: the same get again starts over"
                              % winpath)
        fh.seek(offset)
        fh.write(data)
        fh.flush()
        offset += len(data)
        tries = 0


# --------------------------------------------------------------------------- command line (from awb winlab)


def main(args: argparse.Namespace, tail: list[str] | None) -> int:
    """The session commands of `awb winlab`; `tail` is what came after `--`."""
    command = args.command
    try:
        if command in ("run", "start"):
            text = " ".join(list(args.powershell or []) + list(tail or []))
            if command == "run":
                result = run(args.name, text, vm=args.vm, timeout=args.timeout)
                for body, note, stream in ((result["stdout"], result["notes"][0], sys.stdout),
                                           (result["stderr"], result["notes"][1], sys.stderr)):
                    if body is None:
                        print("awb winlab: output %s" % note, file=sys.stderr)
                    elif body.strip():
                        _print_cut(body, result["log"].as_posix(), stream)
                print("awb winlab: exit %d on %s, logged in %s" % (result["exit"], args.name,
                                                                    result["log"].as_posix()), file=sys.stderr)
                return EXIT_OK if result["exit"] == 0 else EXIT_REMOTE
            result = start(args.name, text, vm=args.vm)
            print(result["job"])
            print("awb winlab: job %s started on %s: awb winlab status|log|stop %s" % (
                result["job"], args.name, result["job"]), file=sys.stderr)
            return EXIT_OK
        if command == "wait":
            result = wait(args.job, timeout=args.timeout)
            print(state_line(args.job, result["machine"], result))
            if result["timed_out"]:
                print("awb winlab: still not ended after %d s: the job goes on, run wait again" % result["waited"],
                      file=sys.stderr)
                return EXIT_OFFLINE
            return EXIT_OK if result["state"] == "ended" and result["exit"] == 0 else EXIT_REMOTE
        if command in ("status", "stop"):
            result = (status if command == "status" else stop)(args.job)
            print(state_line(args.job, result["machine"], result))
            return EXIT_OFFLINE if result["state"] == "offline" else EXIT_OK
        if command == "log":
            result = job_log(args.job)
            if result["text"] is None:
                print("awb winlab: %d new bytes %s" % (result["bytes"], result["note"]), file=sys.stderr)
            elif result["text"].strip():
                _print_cut(result["text"], result["log"].as_posix())
            print("awb winlab: %d new bytes of job %s%s, the whole log in %s" % (
                result["bytes"], args.job, " (more to come: run it again)" if result["more"] else "",
                result["log"].as_posix()), file=sys.stderr)
            return EXIT_OK
        if command == "put":
            result = put(args.file, args.winpath, machine=args.machine, overwrite=args.overwrite)
            print("awb winlab: put %s to %s on %s, sha256 matches%s" % (
                _size(result["size"]), args.winpath, result["machine"],
                ", resumed at %s" % _size(result["resumed"]) if result["resumed"] else ""))
            return EXIT_OK
        if command == "get":
            return _stoppable(_get_command, args)
    except Offline as err:
        print("awb winlab: offline: %s" % err, file=sys.stderr)
        return EXIT_OFFLINE
    except WinlabError as err:
        print("awb winlab: %s" % err, file=sys.stderr)
        return EXIT_REFUSED
    except OSError as err:
        print("awb winlab: %s (operating system error)" % type(err).__name__, file=sys.stderr)
        return EXIT_REFUSED
    return EXIT_REFUSED


def _stoppable(fn, args) -> int:
    """`fn(args)` with SIGTERM and SIGHUP turned into Stopped (only the main thread can set a handler), so a get
    that is cut off keeps what it received and says so in one line."""
    import signal

    def stop(signum, _frame):
        raise Stopped(signum)

    old = {}
    try:
        for sig in (signal.SIGTERM, signal.SIGHUP):
            old[sig] = signal.signal(sig, stop)
    except ValueError:
        pass
    try:
        return fn(args)
    except Stopped:
        print("awb winlab: stopped by a signal: what was received stays as its part; the same get again resumes it",
              file=sys.stderr)
        return EXIT_REFUSED
    finally:
        for sig, handler in old.items():
            signal.signal(sig, handler)


def _get_command(args) -> int:
    """`awb winlab get` and `get --recheck`; the errors go to main."""
    if getattr(args, "recheck", False):
        result = recheck(args.winpath, args.file, machine=args.machine, check=getattr(args, "check", False))
        print("awb winlab: checked %s of %s from %s again, sha256 %s, published as %s" % (
            _size(result["size"]), args.winpath, result["machine"], result["sha256"][:12], result["file"]))
        return EXIT_OK
    result = get(args.winpath, args.file, machine=args.machine, check=getattr(args, "check", False),
                 overwrite=getattr(args, "overwrite", False))
    print("awb winlab: got %s into %s from %s, sha256 matches%s" % (
        _size(result["size"]), result["file"], result["machine"],
        ", resumed at %s" % _size(result["resumed"]) if result["resumed"] else ""))
    return EXIT_OK
