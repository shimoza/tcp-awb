"""The host, the run and the units of the build runner.

A part of the build runner (awb/buildrun.py, F1 step 6). `Host` is everything the runner touches, so the tests and
the selftest give their own places and stand-ins. The units sit behind `Units`: `SystemdUnits` builds the one fixed
`sudo -n systemd-run` argument list with its clock, its CPU share and its isolation (DESIGN.md section 4);
`LocalUnits` runs the same steps as this uid in this process. Here too: the files the owner reads and writes, the
run's context, what a unit may not see (`_hidden`), the names a unit reaches (`model_allow`, `name_binds`) and the
board files.
"""
from __future__ import annotations

import contextlib
import errno
import ipaddress
import math
import os
import re
import signal
import socket
import stat
import subprocess
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Callable

from awb import build, config
from awb.build_unit_side import HOOKS_BUILD, unit_main


# --------------------------------------------------------------------------- the fixed values

AWB = "/usr/local/bin/awb"
CLIENT = "/opt/claude-build/bin/claude"
DEPLOYED = Path("/opt/tcp-awb/DEPLOYED")
MODEL_HOST = "api.anthropic.com"
PUSH_URL = "https://github.com/shimoza/tcp-awb.git"
RESOLV = Path("/etc/resolv.conf")
"""The host's own resolv.conf, on the owner's host a link to the stub file of systemd-resolved (127.0.0.53). Never
read for a task unit since the finding of 2026-10-12; see UPSTREAM_RESOLV."""
RESOLV_AT = "/etc/resolv.conf"
"""Where glibc reads its nameservers; a task's session and probe unit see the runner's own file bound over it."""
HOSTS_AT = "/etc/hosts"
"""Where glibc reads host names; a task's session and probe unit see the runner's own file bound over it (B4)."""
UPSTREAM_RESOLV = Path("/run/systemd/resolve/resolv.conf")
"""The upstream nameservers systemd-resolved writes beside its stub file. Without one the queue is held (finding of
2026-10-12). A task unit no longer reaches them (B4): IPAddressAllow= takes no port, so a resolver in the list was
open on every port and protocol and a channel through DNS."""
RESOLV_PLACES = ("/etc", "/run")
"""Where the upstream file and every link on its way may lie."""
UNIT_RESOLV = "resolv.conf"
"""The file the runner writes into the run folder (UNIT_NO_RESOLVER, no nameserver) and binds over RESOLV_AT (B4)."""
UNIT_NO_RESOLVER = "# no nameserver: the unit's names come from /etc/hosts alone (B4)\noptions attempts:1 timeout:1\n"
UNIT_HOSTS = "hosts"
"""The file the runner writes into the run folder (loopback and the names it resolved) and binds over HOSTS_AT."""
RESOLV_HOPS = 8
TOKEN_HOST = "platform.claude.com"
"""Where the pinned client refreshes the login's token (its TOKEN_URL); allowed for the session in the login mode."""

POLL = 30.0
TICK = 1.0
SUITE_SECONDS = 1800        # D-S6
RETRY_WAIT = 600            # D-S4: the one retry of an API error before the first tool call, after ten minutes
SMALL_MAX = 1 << 20
CPU_QUOTA = "200%"
MEMORY_MAX = "6G"
TASKS_MAX = 512
SHM_FS = "/dev/shm:size=64M"
"""Every unit's own /dev/shm (review of 2026-10-13, B5): a tmpfs of its own that goes with the unit, capped, with
PrivateIPC=yes for the System V and POSIX objects and /dev/mqueue inaccessible. Nothing a task leaves there outlives
it or reaches the next task."""
UNIT_HIDDEN = ("/etc/awb", "/dev/mqueue")
"""InaccessiblePaths of every unit, before the clones, homes and records of the other tasks."""

UNIT_RE = re.compile(r"awb-task-(?:probe|[BC]-[0-9]{3,4}(?:-(?:prep|hand|suite))?)")
"""Every unit name the runner starts, stops or resets: the fixed forms, nothing from the agent's side."""
PROBE_UNIT = "awb-task-probe"
FAILED_UNIT = "awb-task-failed@%s.service"
"""The oneshot every task unit names in OnFailure= (seal/setup.sh writes the template, SB5): it runs `awb build
failed <unit>` as the owner, which writes one board line when no runner waits on the unit. The probe unit has none."""
RESULTS = ("success", "exit-code", "signal", "core-dump", "timeout", "watchdog", "start-limit-hit", "resources",
           "oom-kill", "protocol", "exec-condition", "start-failed")
"""The Result words of a systemd service; anything else read back is treated as a failed start."""


# --------------------------------------------------------------------------- files the owner reads and writes

def _write(path: Path, data: bytes | str, mode: int = 0o600) -> None:
    build._write_atomic(Path(path), data.encode("utf-8") if isinstance(data, str) else data, mode)


def _read(path: Path, cap: int = SMALL_MAX) -> bytes | None:
    """A regular file of this uid in the run folder, no link followed; None when it is not there or not safe."""
    try:
        return build._read_regular(Path(path), cap)
    except OSError:
        return None


def _text(path: Path, cap: int = SMALL_MAX) -> str | None:
    data = _read(path, cap)
    if data is None:
        return None
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        return None


def _take(path: Path, uid: int, cap: int, dest: Path | None = None) -> bytes | None:
    """A file the build user wrote (in its out folder): opened without following a link, a regular file of `uid`
    with one name, at most `cap` bytes. With `dest` it is streamed into the owner's own file (600) and b"" comes back.
    None when it is missing or not safe."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
    except OSError:
        return None
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode) or st.st_uid != uid or st.st_nlink != 1 or st.st_size > cap:
            return None
        if dest is None:
            data = b""
            while len(data) <= cap:
                chunk = os.read(fd, 1 << 16)
                if not chunk:
                    break
                data += chunk
            return data if len(data) <= cap else None
        tmp = Path(dest).with_name(".%s.part" % Path(dest).name)
        with contextlib.suppress(FileNotFoundError):
            os.unlink(tmp)
        out = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
        total = 0
        try:
            while True:
                chunk = os.read(fd, 1 << 20)
                if not chunk:
                    break
                total += len(chunk)
                if total > cap:
                    raise OSError(errno.EFBIG, "too large")
                os.write(out, chunk)
        except OSError:
            os.close(out)
            os.unlink(tmp)
            return None
        os.close(out)
        os.replace(tmp, dest)
        return b""
    finally:
        os.close(fd)


# --------------------------------------------------------------------------- the host

@dataclass
class Host:
    """Everything the runner touches, so the tests and the selftest can give their own places and stand-ins."""
    root: Path
    runs: Path
    queue: Path
    units: "Units"
    board: Path | None = None
    deployed: Path = DEPLOYED
    model_key: Path | None = None
    model: str = "key"
    """build_model of the host file: `login` (his subscription login in the build user's home, the default of the
    host file) or `key` (the model key above). The tests and the selftest default to `key` without a key."""
    register: Path | None = None
    build_uid: int | None = None
    git_run: Callable = subprocess.run
    wall: Callable[[], float] = time.time
    now: Callable[[], datetime] = datetime.now
    sleep: Callable[[float], None] = time.sleep
    minute: float = 60.0
    poll: float = POLL
    tick: float = TICK
    suite_seconds: float = SUITE_SECONDS
    retry_wait: float = RETRY_WAIT
    push: bool = True
    push_deployed: bool = False
    """The host file's `build_push: deployed`: push (True too) only up to the commit /opt/tcp-awb/DEPLOYED names."""
    push_url: str = PUSH_URL
    mail: Callable[[str, str], bool] | None = None
    precheck: Callable[[], str | None] | None = None
    resolve: Callable[[], list[str] | None] | None = None
    python: str | None = None
    hooks_path: str = HOOKS_BUILD
    client: str = CLIENT
    awb: str = AWB
    quiet: bool = False
    working: Path | None = None
    """The owner's working copy `awb build check` takes a branch or commit from; None is the current folder."""

    @property
    def main_git(self) -> Path:
        return self.root / "main.git"

    @property
    def wt(self) -> Path:
        return self.root / "wt"

    @property
    def homes(self) -> Path:
        return self.root / "home"

    @property
    def template(self) -> Path:
        return self.root / "home-template"

    @property
    def suite_python(self) -> str:
        return self.python or str(self.root / "venv" / "bin" / "python")

    def clone(self, tid: str, n: int) -> Path:
        return self.wt / ("%s-%d" % (tid, n))

    def out(self, tid: str, n: int) -> Path:
        return self.wt / ("%s-%d.out" % (tid, n))

    def home(self, tid: str, n: int) -> Path:
        return self.homes / ("%s-%d" % (tid, n))

    def uid(self) -> int:
        return self.build_uid if self.build_uid is not None else os.geteuid()

    @classmethod
    def from_config(cls) -> "Host":
        conf = config.host_conf()
        user = config.build_user() or "awb-build"
        try:
            import pwd

            uid = pwd.getpwnam(user).pw_uid
        except KeyError:
            uid = None
        model = conf.get("build_model", "login")
        key = conf.get("build_model_key") if model == "key" else None
        p = config.paths()
        return cls(root=build_root(), runs=build.runs_dir(), queue=build.queue_dir(),
                   units=SystemdUnits(user), board=p.shared / "build", model_key=Path(key) if key else None,
                   model=model,
                   register=p.register, build_uid=uid, push=conf.get("build_push", "yes") in ("yes", "deployed"),
                   push_deployed=conf.get("build_push", "yes") == "deployed",
                   push_url=conf.get("build_push_url", PUSH_URL), mail=send_mail)


def build_root() -> Path:
    """The build root of the runner: AWB_BUILD_ROOT (the tests point it at a throw-away folder, so no test ever opens
    the host's main.git), else config.build_root()."""
    env = os.environ.get("AWB_BUILD_ROOT")
    return Path(env) if env else config.build_root()


def send_mail(subject: str, message: str) -> bool:
    """A mail through the key service's notify (keys.send_mail). True when it was sent. The core reaches the
    platform package through the deploy's client of the key service, never by an import."""
    from awb import deploy

    keys = deploy.owner_keys()
    try:
        return keys.send_mail(subject, message) == "sent"
    except ValueError:
        return False


# --------------------------------------------------------------------------- the units

@dataclass
class Spec:
    """One unit as the build user: the name, the command, its clock and its isolation."""
    name: str
    argv: list[str]
    seconds: float
    cwd: str
    home: str
    rw: list[str] = field(default_factory=list)
    hidden: list[str] = field(default_factory=list)
    binds: list[tuple[str, str]] = field(default_factory=list)
    allow: list[str] = field(default_factory=list)
    netns: bool = False   # a network namespace of its own: its own loopback and nothing else
    env: dict[str, str] = field(default_factory=dict)
    stdin: Path | None = None
    stdout: Path | None = None
    stderr: Path | None = None


@dataclass
class Ended:
    result: str          # a Result word of systemd
    code: int


class Units:
    """The unit layer: start a unit and poll it, attach to one that runs, stop it, read what it left."""

    def start(self, spec: Spec):
        raise NotImplementedError

    def attach(self, name: str):
        """A handle on a unit that may still run (the reconcile at start); None when no unit of that name runs."""
        raise NotImplementedError

    def result(self, name: str) -> str | None:
        """The Result systemd keeps for an ended unit that was not collected, else None."""
        raise NotImplementedError

    def stop(self, name: str) -> None:
        raise NotImplementedError

    def probe(self) -> bool:
        raise NotImplementedError


def _unit(name: str) -> str:
    if not UNIT_RE.fullmatch(name):
        raise ValueError("not a unit name of the runner")
    return name + ".service"


class SystemdUnits(Units):
    """The real units: `sudo -n systemd-run` with one fixed argument list, `systemctl show` to read a unit,
    `sudo -n systemctl stop|reset-failed awb-task-...service`. Nothing else goes through sudo."""

    def __init__(self, user: str, run: Callable = subprocess.run, popen: Callable = subprocess.Popen):
        self.user = user
        self.run = run
        self.popen = popen

    def argv(self, spec: Spec) -> list[str]:
        unit = _unit(spec.name)
        out = ["sudo", "-n", "systemd-run", "--quiet", "--wait", "--pipe", "--unit=%s" % unit,
               "--uid=%s" % self.user, "--gid=%s" % self.user, "--working-directory=%s" % spec.cwd,
               "--setenv=HOME=%s" % spec.home]
        out += ["--setenv=%s=%s" % (k, v) for k, v in sorted(spec.env.items())]
        props = ["RuntimeMaxSec=%d" % max(1, math.ceil(spec.seconds)), "CPUQuota=%s" % CPU_QUOTA,
                 "MemoryMax=%s" % MEMORY_MAX, "TasksMax=%d" % TASKS_MAX, "ProtectSystem=strict", "ProtectHome=yes",
                 "PrivateTmp=yes", "PrivateIPC=yes", "TemporaryFileSystem=%s" % SHM_FS, "NoNewPrivileges=yes"]
        if spec.rw:
            props.append("ReadWritePaths=%s" % " ".join(spec.rw))
        props.append("InaccessiblePaths=%s" % " ".join(["-" + p for p in (*UNIT_HIDDEN, *spec.hidden)]))
        props += ["BindReadOnlyPaths=%s:%s" % pair for pair in spec.binds]
        if spec.netns:
            props.append("PrivateNetwork=yes")
        props.append("IPAddressDeny=any")
        if spec.allow:
            props.append("IPAddressAllow=%s" % " ".join(spec.allow))
        if spec.name != PROBE_UNIT:
            # a task unit that fails while no runner waits on it is never silent (SB5)
            props.append("OnFailure=%s" % (FAILED_UNIT % spec.name))
        out += ["--property=%s" % p for p in props]
        return out + ["--"] + list(spec.argv)

    def start(self, spec: Spec):
        files = []
        try:
            fin = open(spec.stdin, "rb") if spec.stdin else open(os.devnull, "rb")
            files.append(fin)
            fout = open(spec.stdout, "wb") if spec.stdout else open(os.devnull, "wb")
            files.append(fout)
            ferr = open(spec.stderr, "wb") if spec.stderr else open(os.devnull, "wb")
            files.append(ferr)
            proc = self.popen(self.argv(spec), stdin=fin, stdout=fout, stderr=ferr, start_new_session=True,
                              close_fds=True)
        finally:
            for f in files:
                f.close()
        return _SystemdHandle(self, spec.name, proc)

    def show(self, name: str) -> dict[str, str]:
        try:
            r = self.run(["systemctl", "show", "-p", "ActiveState", "-p", "Result", _unit(name)],
                         capture_output=True, text=True, timeout=30, check=False)
        except (OSError, subprocess.SubprocessError):
            return {}
        out = {}
        for line in (r.stdout or "").splitlines():
            k, _, v = line.partition("=")
            out[k.strip()] = v.strip()
        return out

    def result(self, name: str) -> str | None:
        s = self.show(name)
        if s.get("ActiveState") in ("failed", "inactive") and s.get("Result") in RESULTS:
            return s["Result"]
        return None

    def reset(self, name: str) -> None:
        with contextlib.suppress(OSError, subprocess.SubprocessError):
            self.run(["sudo", "-n", "systemctl", "reset-failed", _unit(name)], capture_output=True, timeout=30,
                     check=False)

    def attach(self, name: str):
        if self.show(name).get("ActiveState") in ("active", "activating", "deactivating", "reloading"):
            return _SystemdHandle(self, name, None)
        return None

    def stop(self, name: str) -> None:
        with contextlib.suppress(OSError, subprocess.SubprocessError):
            self.run(["sudo", "-n", "systemctl", "stop", _unit(name)], capture_output=True, timeout=120, check=False)

    def probe(self) -> bool:
        spec = Spec(PROBE_UNIT, ["/usr/bin/true"], 60, "/", "/tmp")
        h = self.start(spec)
        while True:
            e = h.poll()
            if e is not None:
                return e.result == "success" and e.code == 0
            time.sleep(0.2)


class _SystemdHandle:
    def __init__(self, units: SystemdUnits, name: str, proc):
        self.units, self.name, self.proc = units, name, proc

    def poll(self) -> Ended | None:
        if self.proc is not None:
            code = self.proc.poll()
            if code is None:
                return None
        else:
            if self.units.show(self.name).get("ActiveState") in ("active", "activating", "deactivating",
                                                                  "reloading"):
                return None
            code = None
        result = self.units.result(self.name)
        if result is None:
            # a transient unit that ended well is unloaded at once: no Result left means success, unless sudo or
            # systemd-run itself failed (a non-zero code without a unit)
            result = "success" if code in (0, None) else "start-failed"
        self.units.reset(self.name)
        return Ended(result, code if code is not None else (0 if result == "success" else 1))


class LocalUnits(Units):
    """The same steps as this uid in this process: `awb build unit ...` runs in a thread, every other command (the
    client's stand-in) as a process group of its own that the clock kills, as RuntimeMaxSec kills the cgroup. For
    the tests and `awb build selftest`; it knows no isolation and claims none."""

    def __init__(self, commands: dict[str, list[str]] | None = None, awb: str = AWB):
        self.commands = dict(commands or {})
        self.awb = awb
        self.handles: dict[str, "_LocalHandle"] = {}
        self.started: list[Spec] = []
        self.stopped: list[str] = []

    def start(self, spec: Spec):
        _unit(spec.name)
        self.started.append(spec)
        h = _LocalHandle(self, spec)
        self.handles[spec.name] = h
        return h

    def attach(self, name: str):
        h = self.handles.get(name)
        return h if h is not None and h.running() else None

    def result(self, name: str) -> str | None:
        h = self.handles.get(name)
        return h.ended.result if h is not None and h.ended is not None and h.ended.result != "success" else None

    def stop(self, name: str) -> None:
        _unit(name)
        self.stopped.append(name)
        h = self.handles.get(name)
        if h is not None:
            h.kill("success")

    def probe(self) -> bool:
        return True


class _LocalHandle:
    def __init__(self, units: LocalUnits, spec: Spec):
        self.units, self.spec = units, spec
        self.deadline = time.monotonic() + spec.seconds
        self.ended: Ended | None = None
        self.proc = None
        self.thread = None
        self.code: int | None = None
        self.killed: str | None = None
        argv = list(spec.argv)
        if argv[:3] == [units.awb, "build", "unit"]:
            self.thread = threading.Thread(target=self._helper, args=(argv[3:],), daemon=True)
            self.thread.start()
        else:
            cmd = units.commands.get(argv[0], [argv[0]]) + argv[1:]
            env = {"HOME": spec.home, "PATH": os.environ.get("PATH", "/usr/bin:/bin"), "LANG": "C.UTF-8"}
            env.update(spec.env)
            fin = open(spec.stdin, "rb") if spec.stdin else open(os.devnull, "rb")
            fout = open(spec.stdout, "wb") if spec.stdout else open(os.devnull, "wb")
            ferr = open(spec.stderr, "wb") if spec.stderr else open(os.devnull, "wb")
            try:
                self.proc = subprocess.Popen(cmd, stdin=fin, stdout=fout, stderr=ferr, cwd=spec.cwd, env=env,
                                             start_new_session=True, close_fds=True)
            finally:
                fin.close()
                fout.close()
                ferr.close()

    def _helper(self, args: list[str]) -> None:
        fin = open(self.spec.stdin, "rb") if self.spec.stdin else open(os.devnull, "rb")
        try:
            self.code = unit_main(args, fin, home=self.spec.home, seconds=self.spec.seconds)
        except Exception:   # a helper that breaks is a failed unit, as in systemd
            self.code = 1
        finally:
            fin.close()

    def running(self) -> bool:
        return self.ended is None and self.poll() is None

    def kill(self, result: str) -> None:
        self.killed = result
        if self.proc is not None and self.proc.poll() is None:
            with contextlib.suppress(OSError):
                os.killpg(self.proc.pid, signal.SIGKILL)
            self.proc.wait()

    def poll(self) -> Ended | None:
        if self.ended is not None:
            return self.ended
        if self.proc is not None:
            if self.proc.poll() is None and time.monotonic() > self.deadline:
                self.kill("timeout")
            code = self.proc.poll()
            if code is None:
                return None
            # whatever the leader left in its group goes with it, as a cgroup does
            with contextlib.suppress(OSError):
                os.killpg(self.proc.pid, signal.SIGKILL)
        else:
            if self.thread.is_alive():
                if time.monotonic() > self.deadline:
                    self.thread.join()
                    self.ended = Ended("timeout", 1)
                    return self.ended
                return None
            code = self.code if self.code is not None else 1
        if self.killed == "timeout":
            self.ended = Ended("timeout", code)
        elif self.killed == "success":
            self.ended = Ended("success", code)
        else:
            self.ended = Ended("success" if code == 0 else "exit-code", code)
        return self.ended


# --------------------------------------------------------------------------- the run's context

@dataclass
class Ctx:
    task: build.Task
    run: build.Run
    start: str = ""
    tip: str = ""
    commits: list[str] = field(default_factory=list)
    entries: list[tuple[str, str, str]] = field(default_factory=list)     # (path, old mode, new mode)
    widened: tuple[str, ...] = ()
    lines: list[str] = field(default_factory=list)                         # checks.log
    suite: dict | None = None
    suite_minutes: float | None = None
    lost: int = 0
    markers: int = 0
    removed_tests: int = 0
    tail: list[str] = field(default_factory=list)

    @property
    def folder(self) -> Path:
        return self.run.folder

    @property
    def tid(self) -> str:
        return self.run.id

    @property
    def n(self) -> int:
        return self.run.n


# --------------------------------------------------------------------------- the unit helpers of the owner side

_WT_NAME_RE = re.compile(r"[BC]-[0-9]{3,4}-[0-9]{1,6}(?:\.out)?")   # a clone of wt/, its export, a check's export


def _hidden(host: Host, ctx: Ctx | None, clone: bool = True) -> list[str]:
    """What a task's unit may not see: the records, every other clone and every other task home (and, for the
    suite, its own clone: the suite sees the export alone)."""
    out = [str(host.runs)] + _login_paths(host)
    mine = {"%s-%d.out" % (ctx.tid, ctx.n)} if ctx else set()
    if ctx and clone:
        mine.add("%s-%d" % (ctx.tid, ctx.n))
    with contextlib.suppress(OSError):
        for name in sorted(os.listdir(host.wt)):
            # only the names the runner makes: another name could split or join the unquoted path list of the unit
            if name not in mine and _WT_NAME_RE.fullmatch(name):
                out.append(str(host.wt / name))
    # home/ is the build user's 700, closed to the owner: the homes of the runs that are not done, from the records
    for tid in build.ids_with_runs(host.runs):
        runs = build.runs_of(tid, host.runs)
        if runs and runs[-1].state != "done":
            for r in runs:
                if ctx is None or (r.id, r.n) != (ctx.tid, ctx.n):
                    out.append(str(host.home(r.id, r.n)))
    return out


def wait(host: Host, handle, ctx: Ctx | None, deadline: float | None, stop_name: str | None = None) -> Ended:
    """Wait for a unit; `awb build stop ID` (stop-wanted) and a deadline long past stop it with the one stop form."""
    stopped = False
    while True:
        e = handle.poll()
        if e is not None:
            return e
        if stop_name and ctx is not None and not stopped:
            if os.path.lexists(ctx.folder / "stop-wanted") or (deadline is not None
                                                                and host.wall() > deadline + 120):
                host.units.stop(stop_name)
                stopped = True
        host.sleep(host.tick)


def _login_paths(host: Host) -> list[str]:
    """The build user's own client state in home/ (his login, the account file): hidden from every unit but the
    prepare and the clean unit, which copy the login in and a refreshed one back."""
    return [str(host.homes / ".claude"), str(host.homes / ".claude.json")]


# --------------------------------------------------------------------------- the names a unit reaches

def _system_owner(uid: int) -> bool:
    """Root, a system account (systemd-resolve owns the stub file) or the runner's own uid; never another user."""
    return uid == 0 or uid < 1000 or uid == os.geteuid()


def _resolv_text(path: Path) -> str | None:
    """A resolv.conf, following a link of the system: every link and the file lie under RESOLV_PLACES and belong to
    root, a system account or this uid, the file is regular and writable by no group or other, at most RESOLV_HOPS
    links. None for anything else."""
    p = os.path.normpath(str(path))
    for _ in range(RESOLV_HOPS + 1):
        if not any(p.startswith(d.rstrip("/") + "/") for d in RESOLV_PLACES):
            return None
        try:
            st = os.lstat(p)
        except OSError:
            return None
        if not _system_owner(st.st_uid):
            return None
        if stat.S_ISLNK(st.st_mode):
            try:
                p = os.path.normpath(os.path.join(os.path.dirname(p), os.readlink(p)))
            except OSError:
                return None
            continue
        try:
            fd = os.open(p, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
        except OSError:
            return None
        try:
            st = os.fstat(fd)
            if not stat.S_ISREG(st.st_mode) or not _system_owner(st.st_uid) or st.st_mode & 0o022 \
                    or st.st_size > SMALL_MAX:
                return None
            data = os.read(fd, SMALL_MAX + 1)
        except OSError:
            return None
        finally:
            os.close(fd)
        if len(data) > SMALL_MAX:
            return None
        return data.decode("utf-8", "replace")
    return None


def on_host(addr: str) -> bool:
    """True for an address of the host itself: loopback, unspecified, multicast, not an address at all or one a
    socket can bind (an interface's own). A task unit never gets one in its allow list."""
    try:
        ip = ipaddress.ip_address(addr)
    except ValueError:
        return True
    if ip.version == 6 and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped
    if ip.is_loopback or ip.is_unspecified or ip.is_multicast:
        return True
    try:
        with socket.socket(socket.AF_INET6 if ip.version == 6 else socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.bind((str(ip), 0))
    except OSError:
        return False
    return True


def resolvers() -> list[str]:
    """The upstream nameservers of systemd-resolved (UPSTREAM_RESOLV, a system link followed, `_resolv_text`),
    never the stub or another address of the host (`on_host`) and never a scoped one; empty when none is found."""
    out: list[str] = []
    for addr in re.findall(r"(?m)^nameserver\s+([0-9a-fA-F.:]+)\s*$", _resolv_text(UPSTREAM_RESOLV) or ""):
        if addr not in out and not on_host(addr):
            out.append(addr)
    return out


def addresses(name: str) -> list[str] | None:
    """The addresses of `name` (port 443), resolved now by the runner; None when it does not resolve or resolves to
    the host itself."""
    try:
        found = {a[4][0] for a in socket.getaddrinfo(name, 443, proto=socket.IPPROTO_TCP)}
    except OSError:
        return None
    if not found or any(on_host(a) for a in found):
        return None
    return sorted(found)


def model_allow(host: Host) -> tuple[list[str], dict[str, list[str]]] | None:
    """(allow, names) of a task unit from one resolution now (D-S9): the model endpoint and, with build_model = login,
    the token host, each with its addresses (`names`, for the unit's hosts file), and those addresses alone as the
    allow list, never a nameserver (B4). None when no upstream nameserver is found (the hold stays as before B4) or a
    name does not resolve or resolves to the host itself. A stand-in (`Host.resolve`) gives the allow list and no
    name."""
    if host.resolve is not None:
        allow = host.resolve()
        return None if allow is None else (allow, {})
    if not resolvers():
        return None
    names: dict[str, list[str]] = {}
    for name in (MODEL_HOST, TOKEN_HOST) if host.model == "login" else (MODEL_HOST,):
        found = addresses(name)
        if found is None:
            return None
        names[name] = found
    return sorted({a for found in names.values() for a in found}), names


def name_binds(folder: Path, names: dict[str, list[str]]) -> list[tuple[str, str]]:
    """The names of a task's session or probe unit (B4): a hosts file of loopback and `names` (each name with the
    addresses the runner resolved) bound read-only over /etc/hosts and a resolv.conf without a nameserver bound over
    /etc/resolv.conf, both written into `folder` (the run folder). The unit sends no DNS query that could leave: its
    allow list names no resolver and a name outside the file does not resolve."""
    hosts = "127.0.0.1\tlocalhost\n::1\tlocalhost\n" + "".join(
        "%s\t%s\n" % (a, name) for name in sorted(names) for a in names[name])
    out = []
    for file, text, at in ((UNIT_HOSTS, hosts, HOSTS_AT), (UNIT_RESOLV, UNIT_NO_RESOLVER, RESOLV_AT)):
        path = Path(folder) / file
        _write(path, text, 0o644)
        os.chmod(path, 0o644)   # the build user reads it inside the unit, whatever the runner's umask
        out.append((str(path), at))
    return out


def client_argv(host: Host, task: build.Task, session: str | None) -> list[str]:
    """The headless session: the pinned client, the model, effort and budget of the block, every permission
    bypassed inside the unit's walls, the user settings of the template only, no MCP server, one JSON object."""
    argv = [host.client, "-p", "--model", task.model, "--effort", task.effort, "--max-budget-usd", str(task.budget),
            "--permission-mode", "bypassPermissions", "--dangerously-skip-permissions", "--permission-prompts",
            "none", "--setting-sources", "user", "--strict-mcp-config", "--output-format", "json"]
    if session:
        argv += ["--resume", session]
    return argv


# --------------------------------------------------------------------------- the board

def board_write(board: Path | None, name: str, data: bytes) -> bool:
    """A file of the board's folder written atomically through a handle opened without following a link (the
    folder is the work user's, 3770); True when it was written."""
    if board is None:
        return False
    try:
        fd = os.open(board, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
    except OSError:
        return False
    try:
        tmp = ".%s.%d.tmp" % (name, os.getpid())
        with contextlib.suppress(FileNotFoundError):
            os.unlink(tmp, dir_fd=fd)
        out = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, 0o640, dir_fd=fd)
        with os.fdopen(out, "wb") as fh:
            fh.write(data)
        os.rename(tmp, name, src_dir_fd=fd, dst_dir_fd=fd)
        return True
    except OSError:
        return False
    finally:
        os.close(fd)


def board_read(board: Path | None, name: str, cap: int = SMALL_MAX) -> str | None:
    """A file of the board's folder: a regular file, no link followed at any step, under a cap, UTF-8; else None."""
    if board is None:
        return None
    try:
        fd = os.open(board, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
    except OSError:
        return None
    try:
        f = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=fd)
    except OSError:
        os.close(fd)
        return None
    try:
        if not stat.S_ISREG(os.fstat(f).st_mode):
            return None
        data = os.read(f, cap + 1)
    except OSError:
        return None
    finally:
        os.close(f)
        os.close(fd)
    if len(data) > cap:
        return None
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        return None
