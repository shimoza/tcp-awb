"""The receipt of a sealed start: the guards a session start checks and the line its context opens with.

A part of the client hooks (awb/hooks.py, F1 step 6). `receipt` checks the guards of GUARDS; `_start_receipt` logs
the start in <shared>/sessions/receipts.tsv and leaves the mark the prompt hook reads. `keys_call` is the core's one
request to the key service (awb/projects.py uses it too).
"""
from __future__ import annotations

import datetime
import fcntl
import hashlib
import json
import os
import re
import secrets
import stat
import time
from contextlib import contextmanager
from pathlib import Path

from awb import config


# --------------------------------------------------------------------------- the fixed values

INSTALLED_AWB = Path("/usr/local/bin/awb")
"""The command the seal installs for the work user (seal/setup.sh): a root owned wrapper that runs the installed
interpreter isolated (`python3 -I -m awb`); a symlink to the venv script on a host sealed before T3."""
SINCE_RE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z\Z")
"""The one shape of a lock time that goes into a message (vault.SINCE_FORMAT); any other value is "unknown"."""
LOCKED_START = ("The Workbench is locked since %s. Every prompt is refused until the owner runs awb vault unlock. "
                "The project files were not loaded.")
"""The whole context of a session that starts while the name check cannot run (T3)."""


# --------------------------------------------------------------------------- the receipt of a sealed start

MANAGED_FILE = Path("/etc/claude-code/managed-settings.d/awb-workbench.json")
"""The managed client settings the seal installs (seal/setup.sh): the hooks of every session of the host."""
MANAGED_SOURCE = Path(__file__).resolve().parent.parent / "seal" / "work-claude" / "managed-settings.json"
"""The managed settings of the running release, the hooks the installed file must carry."""
KEYS_SOCKET = Path("/run/awb-keys/cloud.sock")
KEYS_SOCKET_ENV = "AWB_KEYS_SOCKET"
"""The call socket of the key service and its variable, those of awb/tcp/keys.py (the core imports no awb.tcp)."""
RECEIPT_TIMEOUT = 5.0
"""Seconds the receipt waits for the vault daemon and for the key service."""
GUARDS = (
    ("hooks", "the client hooks", "the managed settings or the installed command are not the release's: sudo awb "
                                  "deploy"),
    ("gate", "the commit gate of the project", "awb gate --install in the project folder"),
    ("vault", "the vault daemon", "it does not answer a check: the owner starts awb-vaultd"),
    ("keys", "the key service", "it does not answer a ping: the owner starts awb-keyd"),
    ("rules", "the rules digest", "the rules are not the installed ones: awb projects sync"),
    ("mode", "the host mode", "the host file names no mode this release knows: sudo awb deploy"),
)
"""Every guard of the receipt in its order: key, the name a refusal shows (never a path) and the fix."""
RECEIPT_LINE = "guards: %d of %d active (%s)"
REFUSED_START = ("Start refused, %s missing: %s (%s). Every prompt is refused until it is fixed; the project files "
                 "were not loaded.")
RECEIPTS_LOG = "receipts.tsv"
"""<shared>/sessions/receipts.tsv: one line per start of a sealed session (UTC time, session id, project code or
-, "ok" or the keys of the missing guards)."""


@contextmanager
def _directory(path: Path, *, create: bool = False):
    """Open each directory without following links, retaining the final descriptor for I/O."""
    path = Path(path).absolute()
    fd = os.open(path.anchor, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        for part in path.parts[1:]:
            if create:
                try:
                    os.mkdir(part, 0o700, dir_fd=fd)
                except FileExistsError:
                    pass
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            os.close(fd)
            fd = child
        yield fd
    finally:
        os.close(fd)


def _regular(parent: int, name: str, flags: int) -> int:
    fd = os.open(name, flags | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600, dir_fd=parent)
    st = os.fstat(fd)
    if not stat.S_ISREG(st.st_mode) or st.st_nlink != 1:
        os.close(fd)
        raise OSError("a regular unlinked receipt file is required")
    return fd


def _read_file(path: Path, limit: int = 1024 * 1024) -> str:
    with _directory(path.parent) as parent:
        with os.fdopen(_regular(parent, path.name, os.O_RDONLY), "rb") as fh:
            raw = fh.read(limit + 1)
    if len(raw) > limit:
        raise ValueError("the receipt file is too large")
    return raw.decode("utf-8")


@contextmanager
def _sessions(p: config.Paths, *, lock: bool = False):
    with _directory(p.shared / "sessions", create=lock) as folder:
        if not lock:
            yield folder
            return
        fd = _regular(folder, ".receipt.lock", os.O_RDWR | os.O_CREAT)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            yield folder
        finally:
            os.close(fd)


def _release() -> str:
    """The short name of the running release: the first 7 characters of its commit, else an unknown release."""
    try:
        from awb import vault

        name = os.path.basename(vault.release_path())
    except Exception:
        return "unknown release"
    return name[:7] if re.fullmatch(r"[0-9a-f]{40}", name) else "unknown release"


def _hook_entries(data) -> set[tuple[str, str, str]] | None:
    """(event, matcher, command) of every command hook of a settings object; None when it has no such shape."""
    if not isinstance(data, dict) or not isinstance(data.get("hooks"), dict):
        return None
    if data.get("disableAllHooks") is True:
        return None
    out = set()
    for event, entries in data["hooks"].items():
        if not isinstance(entries, list):
            return None
        for entry in entries:
            if not isinstance(entry, dict) or not isinstance(entry.get("matcher", ""), str):
                return None
            hooks = entry.get("hooks", [])
            if not isinstance(hooks, list):
                return None
            for h in hooks:
                if not isinstance(h, dict):
                    return None
                if h.get("type") == "command":
                    if not isinstance(h.get("command"), str) or h.get("async") is True:
                        return None
                    out.add((event, entry.get("matcher", ""), h["command"]))
    return out


def _guard_hooks() -> bool:
    """Every hook of the release's managed settings is in the installed managed file, and each of them runs the
    installed command (INSTALLED_AWB, an executable file)."""
    try:
        want = _hook_entries(json.loads(_read_file(Path(MANAGED_SOURCE))))
        have = _hook_entries(json.loads(_read_file(Path(MANAGED_FILE))))
    except (OSError, ValueError, RecursionError):
        return False
    if not want or have is None or not want <= have:
        return False
    prefix = "%s hook " % INSTALLED_AWB
    if not all(cmd.startswith(prefix) for _, _, cmd in want):
        return False
    try:
        return Path(INSTALLED_AWB).is_file() and os.access(INSTALLED_AWB, os.X_OK)
    except OSError:
        return False


def _project_target(project: Path, base: Path, target: Path, *, directory: bool = False) -> Path:
    """Resolve a registration path only through plain components inside this project."""
    path = target if target.is_absolute() else base / target
    current = project
    parts = path.relative_to(project).parts
    for index, part in enumerate(parts):
        current = current.parent if part == ".." else current / part
        current.relative_to(project)
        mode = current.lstat().st_mode
        if not (stat.S_ISDIR(mode) if directory or index < len(parts) - 1 else stat.S_ISREG(mode)):
            raise ValueError("a worktree path must contain only plain components")
    if directory:
        with _directory(current):
            pass
    return current


def _side_git(root: Path, gitfile: Path) -> Path | None:
    """The .git folder of the project whose side session's worktree ROOT is (`<project>/.side/NAME`, awb/side.py):
    only when the worktree's gitdir is `<project>/.git/worktrees/<x>` of exactly that project, else None."""
    from awb import codes, side

    project = root.parent.parent
    if root.parent.name != ".side" or not codes.is_project_code(project.name):
        return None
    try:
        if not side._plain(project, project / ".git", directory=True):
            return None
        line = side._read(project, gitfile, 4096).strip()
        if not line.startswith("gitdir: "):
            return None
        target = Path(line[len("gitdir: "):])
        gitdir = _project_target(project, root, target, directory=True)
        main = project / ".git"
        if gitdir.parent != main / "worktrees" or not side._plain(project, gitdir, directory=True):
            return None
        back = Path(side._read(project, gitdir / "gitdir", 4096).strip())
        common = Path(side._read(project, gitdir / "commondir", 4096).strip())
        back = _project_target(project, gitdir, back)
        common = _project_target(project, gitdir, common, directory=True)
        if back != gitfile or common != main:
            return None
        return main
    except (OSError, ValueError, RuntimeError, side.SideError):
        return None


def _guard_gate(root: Path | None) -> bool:
    """The project's repository carries the three hooks of the commit gate (awb gate --install) and runs its hooks
    from .git/hooks (no core.hooksPath)."""
    if root is None:
        return False
    from awb import gate, side

    git = Path(root) / ".git"
    try:
        if git.is_file() and not git.is_symlink():
            git = _side_git(Path(root), git)     # SS: a side session's worktree runs the hooks of its project
            if git is None:
                return False
        if not git.is_dir() or git.is_symlink():
            return False
        with _directory(Path(root)):
            pass
        configs = [git / "config", git / "config.worktree"]
        if Path(root) / ".git" != git:
            line = side._read(Path(root), Path(root) / ".git", 4096).strip()
            admin = _project_target(git.parent, Path(root), Path(line[len("gitdir: "):]), directory=True)
            configs.append(admin / "config.worktree")
        for index, path in enumerate(configs):
            try:
                conf = _read_file(path)
            except FileNotFoundError:
                if index == 0:
                    return False
                continue
            # Includes can hide overrides; a bare hooksPath also redirects Git's hooks.
            if re.search(r"(?im)^(?:\s*\[[^]\n]+\])?\s*hookspath(?:\s*=|\s*(?:[#;].*)?$)", conf) or re.search(
                    r'(?im)^\s*\[\s*include(?:if)?(?:[.\s]|\])', conf):
                return False
        for name in gate.HOOK_NAMES:
            hook = git / "hooks" / name
            if not side._plain(git, hook) or not os.access(hook, os.X_OK):
                return False
            if gate.HOOK_MARK not in side._read(git, hook):
                return False
    except (OSError, ValueError, RuntimeError, side.SideError):
        return False
    return True


def _guard_vault(p: config.Paths) -> str:
    """"ok", "locked" or "down": the vault daemon answers a check, says it is locked, or does not answer."""
    from awb import vault

    try:
        state = vault.ping_state(p.check_socket, timeout=RECEIPT_TIMEOUT)
        if not isinstance(state, dict) or state.get("state") not in ("plain", "locked", "unlocked"):
            return "down"
    except Exception:
        return "down"
    return "locked" if state["state"] == "locked" else "ok"


def _keys_socket() -> Path:
    """The call socket of the key service; for the work user of a sealed host never the one of the environment."""
    env = None if config.is_work_user() else os.environ.get(KEYS_SOCKET_ENV)
    return Path(env) if env else Path(KEYS_SOCKET)


def keys_call(obj: dict, sock: Path | None = None, timeout: float = RECEIPT_TIMEOUT) -> dict:
    """One request line to the key service and its answer line, without awb.tcp (the core imports none of it).
    Raises OSError or ValueError when the service cannot be reached or answers no object."""
    import socket

    def unique_object(pairs):
        answer = {}
        for key, value in pairs:
            if key in answer:
                raise ValueError("duplicate key")
            answer[key] = value
        return answer

    try:
        deadline = time.monotonic() + timeout
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
            s.settimeout(timeout)
            s.connect(str(sock or _keys_socket()))
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError()
            s.settimeout(remaining)
            s.sendall(json.dumps(obj).encode("utf-8") + b"\n")
            buf = bytearray()
            while b"\n" not in buf:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError()
                s.settimeout(remaining)
                part = s.recv(min(4096, 65537 - len(buf)))
                if not part:
                    raise ValueError("incomplete reply")
                buf.extend(part)
                if len(buf) > 65536:
                    raise ValueError("oversized reply")
        answer = json.loads(buf.decode("utf-8"), object_pairs_hook=unique_object)
        if not isinstance(answer, dict):
            raise ValueError("no object")
    except OSError:
        raise OSError("the key service cannot be reached") from None
    except (ValueError, RecursionError):
        raise ValueError("the key service answered an invalid object") from None
    return answer


def _guard_keys() -> bool:
    """The key service answers {"op":"ping"} with ok true on its call socket."""
    try:
        answer = keys_call({"op": "ping"})
    except (OSError, ValueError):
        return False
    return answer.get("ok") is True


def _guard_rules(root: Path | None, owner: bool) -> bool:
    from awb import rulesync

    try:
        return rulesync.current(root, None if owner else Path.home() / ".claude" / "CLAUDE.md")
    except Exception:
        return False


def receipt(p: config.Paths, root: Path | None, owner: bool = False) -> tuple[list[str], list[str], str]:
    """The guards of a sealed start: (the keys checked, the keys missing, the line for the context). An owner
    session is checked without the project repository. A locked vault makes the line the locked start (T3)."""
    vault = _guard_vault(p)
    results = {
        "hooks": _guard_hooks,
        "gate": lambda: _guard_gate(root),
        "vault": lambda: vault == "ok",
        "keys": _guard_keys,
        "rules": lambda: _guard_rules(root, owner),
        "mode": lambda: config.mode() in config.KNOWN_MODES,
    }
    checked = [k for k, _, _ in GUARDS if not (owner and k == "gate")]
    missing = []
    for key in checked:
        try:
            ok = bool(results[key]())
        except Exception:
            ok = False
        if not ok:
            missing.append(key)
    if not missing:
        return checked, missing, RECEIPT_LINE % (len(checked), len(checked), _release())
    if missing == ["vault"] and vault == "locked":
        since = None
        try:
            from awb import vault as _vault

            since = _vault.ping_state(p.check_socket, timeout=RECEIPT_TIMEOUT)["since"]
        except Exception:
            pass
        since = since if isinstance(since, str) and SINCE_RE.fullmatch(since) else "unknown"
        return checked, missing, LOCKED_START % since
    names = [n for k, n, _ in GUARDS if k in missing]
    fixes = [f for k, _, f in GUARDS if k in missing]
    return checked, missing, REFUSED_START % ("guard" if len(names) == 1 else "guards", ", ".join(names),
                                              "; ".join(fixes))


def _session_digest(sid: str) -> str:
    return "sha256-" + hashlib.sha256(sid.encode("utf-8", errors="surrogatepass")).hexdigest()


def _session_id(sid) -> str:
    """Keep ordinary ids as written (no content check inside, DMZ 4), hash the others without truncation or lossy
    cleaning."""
    if not isinstance(sid, str) or not sid:
        return ""
    if re.fullmatch(r"[A-Za-z0-9_-]{1,80}", sid) and not sid.startswith("sha256-"):
        return sid
    return _session_digest(sid)


def _receipt_mark(p: config.Paths, sid) -> Path | None:
    sid = _session_id(sid)
    if not sid:
        return None
    mark = p.shared / "sessions" / ("receipt-%s.ok" % sid)
    try:
        with _directory(p.shared):
            pass
        with _sessions(p) as folder:
            try:
                st = os.stat(mark.name, dir_fd=folder, follow_symlinks=False)
            except FileNotFoundError:
                return mark
            if not stat.S_ISREG(st.st_mode) or st.st_nlink != 1:
                return None
    except FileNotFoundError:
        return mark
    except OSError:
        return None
    return mark


def _append_receipt(folder: int, sid, root: Path | None, missing: list[str]) -> None:
    from awb import codes

    code = root.name if root is not None and codes.PROJECT_RE.fullmatch(root.name) else "-"
    known = [key for key, _, _ in GUARDS]
    safe_missing = missing if all(key in known for key in missing) else known
    line = "%s\t%s\t%s\t%s\n" % (datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                                   _session_id(sid) or "-", code, ",".join(safe_missing) or "ok")
    with os.fdopen(_regular(folder, RECEIPTS_LOG, os.O_WRONLY | os.O_CREAT | os.O_APPEND), "wb") as fh:
        fh.write(line.encode("utf-8"))


def _log_receipt(p: config.Paths, sid, root: Path | None, missing: list[str]) -> None:
    try:
        with _sessions(p, lock=True) as folder:
            _append_receipt(folder, sid, root, missing)
    except OSError:
        pass


def refused_today(p: config.Paths, day: str | None = None) -> int | None:
    """Count complete, valid refused receipt rows for one UTC day; unreadable evidence is unknown."""
    from awb import codes

    day = day or datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d")
    if not isinstance(day, str) or not re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", day):
        return None
    try:
        text = _read_file(p.shared / "sessions" / RECEIPTS_LOG, 16 * 1024 * 1024)
    except FileNotFoundError:
        return 0
    except (OSError, ValueError):
        return None
    known = {key for key, _, _ in GUARDS}
    seen = set()
    for line in text.splitlines(keepends=True):
        if not line.endswith("\n"):
            continue
        f = line[:-1].split("\t")
        if len(f) != 4 or not SINCE_RE.fullmatch(f[0]) or f[0][:10] != day:
            continue
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,80}", f[1]) or not (
                f[2] == "-" or codes.PROJECT_RE.fullmatch(f[2])):
            continue
        if f[3] != "ok" and set(f[3].split(",")) <= known:
            identity = f[1] if re.fullmatch(r"sha256-[0-9a-f]{64}", f[1]) else _session_digest(f[1])
            seen.add(identity if f[1] != "-" else line)
    return len(seen)


def refused_line(p: config.Paths) -> str:
    n = refused_today(p)
    return "sessions refused at start today: %s" % ("unknown" if n is None else n)


def _write_mark(folder: int, name: str, line: str) -> None:
    """Publish a complete mark with a unique temporary name, never truncating another inode."""
    temp = ".receipt-" + secrets.token_hex(16)
    fd = _regular(folder, temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(line + "\n")
        os.replace(temp, name, src_dir_fd=folder, dst_dir_fd=folder)
    finally:
        try:
            os.unlink(temp, dir_fd=folder)
        except FileNotFoundError:
            pass


def _start_receipt(p: config.Paths, data: dict, root: Path | None, owner: bool) -> tuple[bool, str]:
    """Serialize checks and publication so an earlier success cannot overwrite a later refusal."""
    result = None
    try:
        with _sessions(p, lock=True) as folder:
            _, missing, line = receipt(p, root, owner)
            result = (not missing, line)
            sid = data.get("session_id")
            try:
                _append_receipt(folder, sid, root, missing)
            except OSError:
                pass
            if isinstance(sid, str) and sid and not owner:
                if missing:
                    # Screening may be unavailable now although an earlier start kept a checked raw id.
                    ids = {_session_digest(sid)}
                    if re.fullmatch(r"[A-Za-z0-9_-]{1,80}", sid) and not sid.startswith("sha256-"):
                        ids.add(sid)
                    for identity in ids:
                        try:
                            os.unlink("receipt-%s.ok" % identity, dir_fd=folder)
                        except FileNotFoundError:
                            pass
                else:
                    _write_mark(folder, "receipt-%s.ok" % _session_id(sid), line)
    except OSError:
        pass
    if result is None:
        _, missing, line = receipt(p, root, owner)
        result = (not missing, line)
    return result
