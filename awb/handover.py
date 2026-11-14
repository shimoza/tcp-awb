"""The daemon toolkit the vault daemon and the key service share: their sockets and the hand-over of a reload.

A part of the vault (awb/vault.py, F1 step 7), used by awb/tcp/keys.py too. `_bind` makes a listening socket that
nobody reaches before its mode is set, `_adopt` and `_inherited` take one over, `_peer_uid` reads SO_PEERCRED.
`hand_over` is the parent's side of a reload (T2): it starts the new process with the listening sockets and an
anonymous socket pair (`_Pair`), waits for its ok and its serving and tells systemd the new main pid (`sd_notify`).
`no_dump` keeps the owner's other processes out of a daemon; `release_path` names the release a daemon runs.
Every name of this module is still reached through awb/vault.py (awb/parts.py).

UX13 also uses this module for `awb handover write`: a source-only owner session snapshot, with a Git cursor
in build/HANDOVER.md and per-session compaction receipts. It does not alter the daemon reload protocol.
"""
from __future__ import annotations

import ctypes
import datetime as dt
import fcntl
import hashlib
import json
import os
import re
import secrets
import socket
import stat
import struct
import subprocess
import tempfile
import time
from contextlib import contextmanager
from pathlib import Path

from awb.vault_client import VaultError

MAX_SOCKET_PATH = 100
HANDOVER_DEADLINE = 45.0
"""Seconds a reload may take from the moment the daemon reads the request: the new process must answer ok and
serving within it, else the old daemon keeps serving (D-T2j)."""
RELOAD_CLIENT_TIMEOUT = 75.0
"""Seconds `awb vault reload` waits for the answer: longer than the daemon's own deadline, so the daemon decides."""


# --------------------------------------------------------------------------- the sockets of a daemon


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
        return srv, _adopt(srv, path)
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


def _adopt(srv: socket.socket, path: Path) -> tuple[int, int]:
    """A listening socket as the accept loops want it: a timeout of 0.2 s (a socket inherited from the daemon
    before arrives non-blocking) and the (device, inode) of its file, the identity a clean stop checks before it
    removes the file (fstat of a socket gives a sockfs inode, so it is the file's lstat)."""
    srv.settimeout(0.2)
    st = os.lstat(path)
    return st.st_dev, st.st_ino


def _inherited(fd) -> socket.socket:
    if type(fd) is not int or fd < 3:
        raise ValueError("not a descriptor")
    s = socket.socket(fileno=fd)
    if s.family != socket.AF_UNIX or s.type != socket.SOCK_STREAM or \
            not s.getsockopt(socket.SOL_SOCKET, socket.SO_ACCEPTCONN):
        raise ValueError("not a listening unix socket")
    return s


# --------------------------------------------------------------------------- the hand-over (T2)

PR_GET_DUMPABLE = 3
PR_SET_DUMPABLE = 4
MAX_PAIR_LINE = 4 * 1024 * 1024


def no_dump() -> bool:
    """prctl(PR_SET_DUMPABLE, 0): /proc/<pid> and ptrace are closed to the owner's other processes (D-T2c). The
    flag resets on exec, so every daemon sets it at its own start. False when the call failed."""
    try:
        return ctypes.CDLL(None, use_errno=True).prctl(PR_SET_DUMPABLE, 0, 0, 0, 0) == 0
    except (OSError, AttributeError):
        return False


def sd_notify(message: str) -> bool:
    """One datagram to systemd's $NOTIFY_SOCKET (an @ first means an abstract address). Nothing without the
    variable. True when it was sent."""
    addr = os.environ.get("NOTIFY_SOCKET")
    if not addr:
        return False
    if addr.startswith("@"):
        addr = "\0" + addr[1:]
    s = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
    try:
        s.connect(addr)
        s.sendall(message.encode("ascii"))
        return True
    except OSError:
        return False
    finally:
        s.close()


def release_path() -> str:
    """The real path of the tree the running package came from: a release folder under /opt/tcp-awb/releases on a
    sealed host. A daemon answers it, so that `awb deploy` sees which release each daemon runs."""
    import awb

    return os.path.dirname(os.path.realpath(awb.__path__[0]))


class _Pair:
    """One end of the socket pair of a hand-over: one JSON object per line in both directions, each read within a
    deadline. Nothing else can address it: it has no path and lives in the two processes only."""

    def __init__(self, sock: socket.socket):
        self.sock = sock
        self.buf = bytearray()

    def send(self, obj: dict) -> bool:
        try:
            self.sock.settimeout(10.0)
            self.sock.sendall(json.dumps(obj).encode("ascii") + b"\n")
            return True
        except OSError:
            return False

    def read(self, deadline: float) -> dict | None:
        """The next line as an object; None on a timeout, an end, a line too long or not an object."""
        while b"\n" not in self.buf:
            left = deadline - time.monotonic()
            if left <= 0:
                return None
            try:
                self.sock.settimeout(left)
                chunk = self.sock.recv(1 << 16)
            except OSError:
                return None
            if not chunk:
                return None
            self.buf += chunk
            if len(self.buf) > MAX_PAIR_LINE:
                return None
        nl = self.buf.index(b"\n")
        line = bytes(self.buf[:nl])
        del self.buf[:nl + 1]
        try:
            obj = json.loads(line.decode("utf-8"))
        except (ValueError, RecursionError):
            return None
        return obj if isinstance(obj, dict) else None

    def wait_end(self, deadline: float) -> bool:
        """True when the other side closed its write side within the deadline."""
        while True:
            left = deadline - time.monotonic()
            if left <= 0:
                return False
            try:
                self.sock.settimeout(left)
                if not self.sock.recv(1 << 16):
                    return True
            except OSError:
                return False

    def shut_write(self) -> None:
        try:
            self.sock.shutdown(socket.SHUT_WR)
        except OSError:
            pass

    def close(self) -> None:
        try:
            self.sock.close()
        except OSError:
            pass


def _end_child(child: subprocess.Popen) -> None:
    """SIGKILL to a child that still lives, then wait for it: no zombie."""
    if child.poll() is None:
        try:
            child.kill()
        except OSError:
            pass
    try:
        child.wait(timeout=10)
    except subprocess.TimeoutExpired:
        pass


def hand_over(owner, command, fds: tuple[int, ...], line: dict, deadline: float, check=None) -> tuple[dict | None, str]:
    """The parent's side of a reload, shared by the vault daemon and the key service (design 8.2 and 8.3).

    Starts `command(<pair fd>)` with the pair and `fds` passed, writes `line`, waits for {"ok": true, "pid",
    "release"}, lets `check(reply)` refuse it (a word), pauses the owner's accept loops, writes go, waits for
    {"serving": true}, sends MAINPID to systemd and closes its end (the child's sign that it is the main process
    now). Returns (reply, "") or (None, word); on a failure the child is killed and waited for, then the accept
    loops resume and MAINPID was never sent."""
    a, b = socket.socketpair()
    try:
        child = subprocess.Popen(command(b.fileno()), pass_fds=(b.fileno(),) + tuple(fds), close_fds=True, cwd="/",
                                 stdin=subprocess.DEVNULL)
    except OSError:
        a.close()
        return None, "it could not be started"
    finally:
        b.close()
    pair = _Pair(a)
    paused = False

    def fail(word: str) -> tuple[None, str]:
        _end_child(child)
        if paused:
            owner._resume()
        pair.close()
        return None, word

    try:
        if not pair.send(line):
            return fail("the line did not go")
        line = None
        reply = pair.read(deadline)
        if reply is None:
            return fail("it exited" if child.poll() is not None else "no answer in time")
        if reply.get("ok") is not True:
            err = reply.get("error")
            return fail(err if err in ("wrong passphrase", "failed") else "not ok")
        if reply.get("pid") != child.pid or not isinstance(reply.get("release"), str):
            return fail("an answer of another form")
        word = check(reply) if check is not None else None
        if word:
            return fail(word)
        paused = True
        if not owner._pause():
            return fail("the accept loops did not pause")
        if not pair.send({"go": True}):
            return fail("go did not go")
        if pair.read(deadline) != {"serving": True}:
            return fail("no serving after go")
        sd_notify("MAINPID=%d" % child.pid)
        pair.shut_write()
        pair.close()
        return reply, ""
    except BaseException:
        fail("internal error")
        raise


# --------------------------------------------------------------------------- owner session hand-over (UX13)

HANDOVER = "build/HANDOVER.md"
SECOND_COMPACTION = "second compaction: a new session with the hand-over is cheaper"
_HEAD = re.compile(r"<!-- handover-head: ([0-9a-f]{40,64}) -->")
_MAX_TEXT = 8 * 1024 * 1024


class HandoverError(Exception):
    """Fixed diagnostic text only; source values never enter an error message."""


def repository_root(folder: Path | str) -> Path | None:
    """Only an owner checkout, including a Git worktree, has hand-over hooks."""
    from awb import config

    if config.is_work_user() or config.is_build_user():
        return None
    folder = Path(folder).resolve()
    return next((p for p in (folder, *folder.parents)
                 if (p / ".git").exists() and (p / "awb/cli.py").is_file()), None)


@contextmanager
def _directory(path, *, dir_fd=None, create=False):
    if create:
        try:
            os.mkdir(path, mode=0o700, dir_fd=dir_fd)
        except FileExistsError:
            pass
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=dir_fd)
    try:
        yield fd
    finally:
        os.close(fd)


def _same_directory(path, fd: int, *, dir_fd=None) -> None:
    info = os.stat(path, dir_fd=dir_fd, follow_symlinks=False)
    opened = os.fstat(fd)
    if (info.st_dev, info.st_ino) != (opened.st_dev, opened.st_ino):
        raise HandoverError("the output folder changed")


@contextmanager
def _owner_lock(root: Path):
    # Anchor every output operation to opened directories, including temporary-file cleanup.
    with _directory(root) as root_fd, _directory("build", dir_fd=root_fd, create=True) as folder_fd:
        fd = os.open(".handover.lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK,
                     0o600, dir_fd=folder_fd)
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                raise HandoverError("invalid hand-over lock")
            fcntl.flock(fd, fcntl.LOCK_EX)
            current = os.stat(".handover.lock", dir_fd=folder_fd, follow_symlinks=False)
            if (info.st_dev, info.st_ino) != (current.st_dev, current.st_ino):
                raise HandoverError("the hand-over lock changed")
            _same_directory("build", folder_fd, dir_fd=root_fd)
            yield folder_fd
        finally:
            os.close(fd)


def _read_text(path, *, dir_fd=None, missing=None, limit=_MAX_TEXT) -> str | None:
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=dir_fd)
    except FileNotFoundError:
        return missing
    with os.fdopen(fd, "rb") as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_size > limit:
            raise HandoverError("the source is not a bounded regular file")
        data = stream.read(limit + 1)
        if len(data) > limit:
            raise HandoverError("the source is too large")
        return data.decode("utf-8")


def _replace(path: Path, text: str, *, dir_fd: int) -> None:
    temp = ".handover-" + secrets.token_hex(16)
    fd = os.open(temp, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o600, dir_fd=dir_fd)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp, path.name, src_dir_fd=dir_fd, dst_dir_fd=dir_fd)
    finally:
        try:
            os.unlink(temp, dir_fd=dir_fd)
        except FileNotFoundError:
            pass


def _queue_lines() -> list[str]:
    from awb import workqueue

    path = workqueue.queue_file()
    legacy = path.with_name("QUEUE.md")
    # The queue reader also opens a lock and writes its check cache. Refuse redirected or
    # special files before either it or the owner-status reader can touch those paths.
    if any(parent.is_symlink() for parent in path.parents):
        raise HandoverError("the queue folder is a link")
    for source in (path, legacy, workqueue.archive_file(path), path.with_name(".queue.lock"),
                   path.with_name("queue-checked.json"), path.with_name("queue-checked.json.tmp")):
        try:
            info = source.lstat()
        except FileNotFoundError:
            continue
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_size > _MAX_TEXT:
            raise HandoverError("invalid queue file")
    if path.exists() or not legacy.exists():
        return ["%s  %s  %s  %s  next: %s" %
                (r["id"], r["project"] or "-", r["who"], r["what"], r["next"])
                for r in workqueue.load(path)]
    text = _read_text(legacy)
    if text is None:
        raise HandoverError("the legacy queue disappeared")
    if text.startswith(workqueue.ARCHIVE_NOTICE):
        return []
    columns = []
    out = []
    for line in text.splitlines():
        if not line.startswith("|"):
            if not line.strip() or line.startswith("#"):
                columns = []
            continue
        cells = [c.strip().replace("\\|", "|") for c in re.split(r"(?<!\\)\|", line.strip().strip("|"))]
        names = [c.lower() for c in cells]
        if "state" in names and "what" in names and ("id" in names or "step" in names):
            columns = names
            continue
        if not columns or all(re.fullmatch(r"[-: ]+", c) for c in cells):
            continue
        if len(cells) != len(columns):
            raise HandoverError("invalid legacy queue row")
        row = dict(zip(columns, cells))
        state = row["state"].strip("*` ")
        if not workqueue.CLOSED.match(state) and re.search(
                r"^(waiting|asked)\b|\bwaits? on (him|you|the owner)\b", state, re.I):
            out.append(line)
    return out


def _git(root: Path, *args: str) -> str:
    # -C does not override an inherited GIT_DIR or injected Git configuration.
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    result = subprocess.run(["git", "--no-pager", "-C", str(root), *args], capture_output=True, text=True,
                            stdin=subprocess.DEVNULL, timeout=15, env=env)
    if result.returncode:
        raise HandoverError("git history unavailable; previous hand-over kept")
    return result.stdout.strip()


def _build_lines() -> list[str]:
    """Read the command across a process boundary: daemon reload helpers must not import the build runner."""
    import sys

    # the code of this checkout, also where the package is not installed (the runner's fresh export)
    code = str(Path(__file__).resolve().parent.parent)
    run = "import sys; sys.path.insert(0, %r); from awb import cli; sys.exit(cli.main(['build', 'list']))" % code
    result = subprocess.run([sys.executable, "-I", "-c", run], capture_output=True,
                            text=True, stdin=subprocess.DEVNULL, timeout=30)
    if result.returncode:
        raise HandoverError("build list unavailable; previous hand-over kept")
    return result.stdout.splitlines()


def write(root: Path | None = None) -> Path:
    """Snapshot checked sources atomically. The previous file holds the Git cursor too."""
    from awb import config, devask, ownerwait, projects

    root = repository_root(root or Path.cwd())
    if root is None:
        raise HandoverError("run in an owner repository checkout")
    p = config.paths()
    with _owner_lock(root) as folder_fd:
        target = root / HANDOVER
        if target.is_symlink():
            raise HandoverError("the hand-over file is a link")
        previous = _read_text(target.name, dir_fd=folder_fd, missing="")
        head = _git(root, "rev-parse", "HEAD")
        if not re.fullmatch(r"[0-9a-f]{40,64}", head):
            raise HandoverError("invalid git head")
        cursor = _HEAD.search(previous)
        revision = cursor.group(1) + ".." + head if cursor else head
        history = _git(root, "log", "--format=%h %s", revision, "--").splitlines()
        queue = _queue_lines()
        tasks = [line for line in _build_lines()
                 if len(line.split("  ")) > 1 and
                 line.split("  ")[1].split(":", 1)[0] not in ("done", "stopped")]
        commits = projects.note_commits()
        held = ["%s  %s  after %s" % (n["project"], n["file"], n["after"])
                for n in projects.held_notes(p) if n["after"] not in commits]
        asks = ["%s  %s  %s%s" % (d["project"], path.name, d["title"], " [blocks]" if d["blocks"] else "")
                for path, d in devask.open_asks(p)]
        items, parked = ownerwait.collect(p)
        sections = (("Open queue", queue), ("Git log", history), ("Open build tasks", tasks),
                    ("Held notes", held), ("Open asks", asks),
                    ("Owner status", [_home_as_tilde(line) for line in
                                      ownerwait.render(items, parked, ownerwait.notes(p=p))]))
        body = "# Hand-over\n\nWritten: %s UTC\n<!-- handover-head: %s -->\n" % (
            dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d %H:%M"), head)
        screened = body
        for title, lines in sections:
            part = "\n## %s\n\n%s\n" % (title, "\n".join(lines) or "(none)")
            body += part
            if title != "Git log":
                screened += part
        if len(body.encode("utf-8")) > _MAX_TEXT:
            raise HandoverError("the hand-over is too large")
        # the subjects of this repository's own commits passed the commit gate when they were made; they get the
        # secret check only, as our own output does (a URL or an IP in an old subject refused the whole file)
        from awb import check

        if check._secret_classes("\n".join(history)):
            raise HandoverError("a secret in the git log; previous hand-over kept")
        devask.screen(p, screened)
        _same_directory(root / "build", folder_fd)
        _replace(target, body, dir_fd=folder_fd)
    return target


def _home_as_tilde(line: str) -> str:
    """A ready command of owner status names the owner's home (an export folder, B-060); the hand-over writes it as
    ~, since a home path in the file is refused by its secret check (2026-11-14)."""
    home = str(Path.home())
    return line.replace(home + "/", "~/") if home not in ("", "/") else line


def start_line(folder: Path | str) -> str:
    root = repository_root(folder)
    if root is None:
        return ""
    target = root / HANDOVER
    try:
        if (root / "build").is_symlink() or target.is_symlink() or not target.is_file():
            return ""
        stamp = dt.datetime.fromtimestamp(target.stat().st_mtime, dt.timezone.utc).strftime("%H:%M")
    except (OSError, ValueError, OverflowError):
        return ""
    return "hand-over: %s, written %s" % (HANDOVER, stamp)


def compaction(folder: Path | str, session_id, *, prompt: bool = False) -> str:
    """Persist the count across hook processes; claim the second notice under the same lock."""
    root = repository_root(folder)
    if root is None or not isinstance(session_id, str) or not session_id:
        return ""
    with _owner_lock(root) as folder_fd:
        marks = root / "build/.handover-sessions"
        if marks.is_symlink():
            raise HandoverError("the compaction folder is a link")
        with _directory(marks.name, dir_fd=folder_fd, create=True) as marks_fd:
            path = marks / (hashlib.sha256(session_id.encode()).hexdigest() + ".json")
            try:
                raw = _read_text(path.name, dir_fd=marks_fd, limit=4096)
                state = json.loads(raw) if raw is not None else {"count": 0, "shown": False}
            except (ValueError, RecursionError):
                raise HandoverError("invalid compaction receipt") from None
            if (not isinstance(state, dict) or set(state) != {"count", "shown"}
                    or type(state.get("count")) is not int or state["count"] < 0
                    or type(state.get("shown")) is not bool):
                raise HandoverError("invalid compaction receipt")
            line = ""
            if prompt:
                if state["count"] >= 2 and not state["shown"]:
                    state["shown"] = True
                    line = SECOND_COMPACTION
            else:
                state["count"] += 1
            _same_directory(root / "build", folder_fd)
            _same_directory(marks.name, marks_fd, dir_fd=folder_fd)
            _replace(path, json.dumps(state) + "\n", dir_fd=marks_fd)
            return line


def main(argv: list[str] | None = None) -> int:
    import sys
    from awb.cli import SafeParser

    class Parser(SafeParser):
        def error(self, message):
            super().error("invalid hand-over arguments")

    parser = Parser(prog="awb handover", description="Source-only owner session hand-over; codes and handles only.")
    parser.add_subparsers(dest="command", required=True).add_parser(
        "write", help="write build/HANDOVER.md from open work, git history and default owner status")
    parser.parse_args(argv)
    try:
        write()
    except Exception:
        # Source readers can raise messages containing source values or host paths.
        print("awb handover: sources or output unavailable or unsafe; previous hand-over kept", file=sys.stderr)
        return 2
    print(start_line(Path.cwd()))
    return 0
