"""Side sessions of a project (SS, 2026-11-13): side work (a report, research, an analysis) runs in a session of
its own next to the main session, so it does not fill the main session's context.

    awb side start NAME --brief FILE [--model MODEL] [--effort LEVEL]
    awb side status
    awb side take NAME [--merge]
    awb side stop NAME
    awb side drop NAME

It runs in the root of a project folder (a project code). `start` makes a git worktree .side/NAME on the
branch side/NAME (`.side/` goes into .git/info/exclude, never into a commit), and starts a headless client session
there in the background with the brief as its task: the brief is a file of the project the main session writes,
so the side session gets that text and the files it names, never the main session's context. The side session is
a session of the same user in a folder of the same project: the managed settings, the deny rules and the hooks of
the work user apply to it as to the main session. It runs without approval prompts (no one is there to answer
one) and without MCP servers. It writes its result to SIDE-RESULT.md in its worktree and commits there; the main
session never writes in .side/NAME and the side session never writes outside it, so two writers never touch the
same files.

`status` prints one line per side session: name, state (running, done, failed, lost), model, effort, commits and
whether a result of its own is there. `take` copies SIDE-RESULT.md to notes/side-NAME.md of the main folder (not
one equal to the SIDE-RESULT.md of the start commit: an earlier result a merge brought in) and lists the
commits and files of the branch; with `--merge` it merges side/NAME into the main branch (no fast-forward; a
conflict is aborted and named) and removes the worktree and the branch. `stop` ends a running side session,
`drop` removes its worktree and branch without taking anything.

The model and the effort are the main session's choice per task (for example fable at effort high for an
analysis, sonnet for a report). AWB_SIDE_CLIENT names another client binary (tests).
"""
from __future__ import annotations

import fcntl
import functools
import inspect
import json
import os
import re
import shutil
import signal
import time
import subprocess
import sys
import stat
import secrets
import unicodedata
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

SIDE_DIR = ".side"
RESULT = "SIDE-RESULT.md"
NOTES = "notes"
MAX_BRIEF = 64 * 1024
MAX_RECORD = 1024 * 1024
MAX_RESULT = 8 * 1024 * 1024
EFFORTS = ("low", "medium", "high", "xhigh", "max")
DEFAULT_MODEL = "sonnet"
DEFAULT_EFFORT = "medium"
CLIENT_ENV = "AWB_SIDE_CLIENT"
_NAME_RE = re.compile(r"[a-z0-9][a-z0-9-]{0,30}")
_MODEL_RE = re.compile(r"[a-z0-9][a-z0-9.\-\[\]]{0,59}")
_DROP_ENV = ("CLAUDECODE", "CLAUDE_CODE_ENTRYPOINT", "CLAUDE_CODE_SSE_PORT")

PREAMBLE = """You are a side session of project {code}, started by its main session for one piece of side work.
Your folder is a git worktree of the project on the branch side/{name}. Work only inside this folder. The project
rules in CLAUDE.md apply to you as to the main session. Do not change STATE.md or OPEN.md: they belong to the main
session. Write your result to {result} in this folder (plain Markdown, codes only, never a name), put any other
file you make under deliverables/ or notes/, commit your own paths with `git commit -- PATH...`, and end with one
last line that starts with DONE. If something blocks you, write it into {result}, commit and end with BLOCKED.

The task from the main session:

"""


class SideError(Exception):
    """A fixed message; never a file's content."""


def is_side_worktree(root: Path | None) -> bool:
    """True when `root` is the folder of a side session: a linked git worktree (its .git is a file) that is
    .side/NAME of a project or is on a branch side/NAME. STATE.md and OPEN.md belong to the main session there, so
    the stop hook asks a side session for neither (tcp-6edq). Reads two small files, runs no git."""
    if root is None:
        return False
    root = Path(root)
    try:
        dotgit = root / ".git"
        if not stat.S_ISREG(os.lstat(dotgit).st_mode):
            return False
        if root.parent.name == SIDE_DIR and _NAME_RE.fullmatch(root.name):
            return True
        with open(dotgit, encoding="utf-8") as fh:
            line = fh.read(4096).strip()
        if not line.startswith("gitdir:"):
            return False
        gitdir = Path(line[len("gitdir:"):].strip())
        gitdir = gitdir if gitdir.is_absolute() else root / gitdir
        with open(gitdir / "HEAD", encoding="utf-8") as fh:
            head = fh.read(4096).strip()
    except (OSError, UnicodeError, ValueError):
        return False
    return head.startswith("ref: refs/heads/side/")


def now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def project_root(cwd: Path | None = None) -> Path:
    root = (cwd or Path.cwd()).resolve()
    from awb import codes

    if not codes.is_project_code(root.name):
        raise SideError("run awb side in the root of a project folder (tcp-xxxx)")
    if (root / ".git").is_file() and not (root / ".git").is_symlink():
        raise SideError("run awb side in the main folder of the project, not in a side session")
    if not _plain(root, root / ".git", directory=True) or not _plain(root, root / "CLAUDE.md"):
        raise SideError("run awb side in the root of a project folder (tcp-xxxx)")
    return root


def _plain(root: Path, path: Path, *, directory: bool = False) -> bool:
    """Check every component, including dangling links. Reads also use directory descriptors below."""
    try:
        parts = path.relative_to(root).parts
        if ".." in parts:
            return False
        current = root
        for i, part in enumerate(parts):
            current /= part
            mode = current.lstat().st_mode
            if i < len(parts) - 1 or directory:
                if not stat.S_ISDIR(mode):
                    return False
            elif not stat.S_ISREG(mode):
                return False
        return True
    except (OSError, ValueError):
        return False


@contextmanager
def _parent(root: Path, path: Path):
    parts = path.relative_to(root).parts
    if not parts or ".." in parts:
        raise SideError("the path must stay inside the project")
    fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        for part in parts[:-1]:
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            os.close(fd)
            fd = child
        yield fd, parts[-1]
    finally:
        os.close(fd)


def _read(root: Path, path: Path, limit: int = MAX_RECORD) -> str:
    with _parent(root, path) as (parent, name):
        fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
        with os.fdopen(fd, "rb") as f:
            st = os.fstat(f.fileno())
            if not stat.S_ISREG(st.st_mode) or st.st_nlink != 1:
                raise SideError("a regular unlinked project file is required")
            raw = f.read(limit + 1)
    if len(raw) > limit:
        raise SideError("the file is too large")
    return raw.decode("utf-8")


def _write(root: Path, path: Path, text: str) -> None:
    """Replace atomically without truncating a linked inode or following a parent link."""
    with _parent(root, path) as (parent, name):
        try:
            st = os.stat(name, dir_fd=parent, follow_symlinks=False)
        except FileNotFoundError:
            pass
        else:
            if not stat.S_ISREG(st.st_mode) or st.st_nlink != 1:
                raise SideError("a regular unlinked project file is required")
        temp = ".side-write-" + secrets.token_hex(12)
        fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=parent)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(text)
            os.replace(temp, name, src_dir_fd=parent, dst_dir_fd=parent)
        finally:
            try:
                os.unlink(temp, dir_fd=parent)
            except FileNotFoundError:
                pass


def _safe(text: str) -> bool:
    """No content check inside (DMZ 4): a control character only, where a line format needs it."""
    return not any(ord(c) < 32 and c not in "\n\t" for c in text)


def _display_safe(text: str) -> bool:
    return not any(unicodedata.category(c) in ("Cc", "Cf", "Zl", "Zp") for c in text)


def _require_safe(text: str) -> None:
    if not _safe(text):
        raise SideError("text refused: it holds a control character")


def _require_safe_result(text: str) -> None:
    """The result of a side session is shown as written: a name, an address, a host name or a secret in it passes."""
    if not _safe(text):
        raise SideError("text refused: it holds a control character")


def _command(fn):
    """One cooperating command per project; never leave a stale lock after a crash."""
    signature = inspect.signature(fn)

    @functools.wraps(fn)
    def run(*args, **kwargs):
        try:
            bound = signature.bind(*args, **kwargs)
            root = project_root(bound.arguments.get("cwd"))
            with _parent(root, root / ".git" / "awb-side.lock") as (parent, name):
                fd = os.open(name, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK,
                             0o600, dir_fd=parent)
            try:
                st = os.fstat(fd)
                if not stat.S_ISREG(st.st_mode) or st.st_nlink != 1:
                    raise SideError("the side session lock is not a regular file")
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    raise SideError("side sessions are busy; try again") from None
                base = root / SIDE_DIR
                if os.path.lexists(base) and not _plain(root, base, directory=True):
                    raise SideError(".side must be a directory of this project")
                return fn(*args, **kwargs)
            finally:
                os.close(fd)
        except (OSError, ValueError, RuntimeError):
            raise SideError("the side session operation failed; check project files and permissions") from None
    return run


def _name(name: str) -> str:
    if not isinstance(name, str) or not _NAME_RE.fullmatch(name):
        raise SideError("NAME is 1-31 lowercase letters, digits or hyphens")
    _require_safe(name)
    return name


def _environment() -> dict[str, str]:
    # Repository selectors and injected Git config can override -C and the client's worktree cwd.
    identity = {"GIT_AUTHOR_NAME", "GIT_AUTHOR_EMAIL", "GIT_AUTHOR_DATE",
                "GIT_COMMITTER_NAME", "GIT_COMMITTER_EMAIL", "GIT_COMMITTER_DATE"}
    return {k: v for k, v in os.environ.items()
            if k not in _DROP_ENV and (not k.startswith("GIT_") or k in identity)}


def _git(root: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess:
    r = subprocess.run(["git", "-C", str(root), *args], capture_output=True, text=True, errors="replace",
                       env=_environment())
    if check and r.returncode != 0:
        raise SideError("git %s failed" % args[0])
    return r


def _paths(root: Path, name: str) -> dict[str, Path]:
    base = root / SIDE_DIR
    return {"base": base, "wt": base / name, "state": base / (name + ".state"), "exit": base / (name + ".exit"),
            "out": base / (name + ".json"), "err": base / (name + ".err")}


def _exclude(root: Path) -> None:
    info = root / ".git" / "info"
    info.mkdir(parents=True, exist_ok=True)
    f = info / "exclude"
    text = _read(root, f) if os.path.lexists(f) else ""
    if not any(line.strip() in (SIDE_DIR + "/", "/" + SIDE_DIR + "/") for line in text.splitlines()):
        _write(root, f, text + ("" if not text or text.endswith("\n") else "\n") + "/" + SIDE_DIR + "/\n")


def _client() -> str:
    c = os.environ.get(CLIENT_ENV) or shutil.which("claude") or str(Path.home() / ".local" / "bin" / "claude")
    if not os.path.isfile(c) or not os.access(c, os.X_OK):
        raise SideError("the client binary is missing")
    return c


def client_argv(client: str, model: str, effort: str) -> list[str]:
    """The headless side session: the model and effort the main session chose, no approval prompt, no MCP server,
    one JSON object at the end. The managed settings and hooks of the user apply as always."""
    return [client, "-p", "--model", model, "--effort", effort, "--permission-mode", "bypassPermissions",
            "--strict-mcp-config", "--output-format", "json"]


# Retain the exact launch signature to recognize sessions already running during an upgrade.
_LEGACY_WRAPPER = r"""
import os, subprocess, sys
argv, wt, out, err, exitf, prompt = sys.argv[1:-5], *sys.argv[-5:]
with open(prompt, "rb") as p, open(out, "wb") as o, open(err, "wb") as e:
    rc = subprocess.run(argv, cwd=wt, stdin=p, stdout=o, stderr=e).returncode
os.unlink(prompt)
with open(exitf + ".tmp", "w") as f:
    f.write("%d\n" % rc)
os.replace(exitf + ".tmp", exitf)
"""

_WRAPPER = r"""
import os, secrets, subprocess, sys
argv = sys.argv[1:-5]
base, prompt, out, err = map(int, sys.argv[-5:-1])
exitf = sys.argv[-1]
rc = 1
try:
    with os.fdopen(prompt, "rb") as p, os.fdopen(out, "wb") as o, os.fdopen(err, "wb") as e:
        rc = subprocess.run(argv, stdin=p, stdout=o, stderr=e).returncode
finally:
    temp = ".exit-" + secrets.token_hex(12)
    fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=base)
    with os.fdopen(fd, "w") as f:
        f.write("%d\n" % rc)
    os.replace(temp, exitf, src_dir_fd=base, dst_dir_fd=base)
    os.close(base)
"""


@_command
def start(name: str, brief: str, model: str = DEFAULT_MODEL, effort: str = DEFAULT_EFFORT,
          cwd: Path | None = None) -> str:
    root = project_root(cwd)
    name = _name(name)
    if not isinstance(model, str) or not _MODEL_RE.fullmatch(model):
        raise SideError("MODEL is a model alias or id such as sonnet, opus or fable")
    if effort not in EFFORTS:
        raise SideError("LEVEL is one of: %s" % ", ".join(EFFORTS))
    _require_safe(model)
    bpath = root / brief
    if not _plain(root, bpath) or bpath.is_relative_to(root / SIDE_DIR):
        raise SideError("the brief is a file of this project, outside .side/")
    if bpath.stat().st_size > MAX_BRIEF:
        raise SideError("the brief is longer than 64 KiB; point to files instead of pasting them")
    text = _read(root, bpath, MAX_BRIEF)
    _require_safe(text)
    p = _paths(root, name)
    prompt = p["base"] / (name + ".prompt")
    if any(os.path.lexists(path) for key, path in p.items() if key != "base") or os.path.lexists(prompt):
        raise SideError("a side session of that name exists: awb side status, awb side drop NAME")
    if _git(root, "rev-parse", "--verify", "--quiet", "refs/heads/side/" + name, check=False).returncode == 0:
        raise SideError("the branch side/NAME exists: awb side drop NAME")
    client = _client()
    p["base"].mkdir(mode=0o700, exist_ok=True)
    p["base"].chmod(0o700)
    _exclude(root)
    from awb import rulesync

    proc = None
    fds = []
    try:
        base = _git(root, "rev-parse", "HEAD").stdout.strip()
        _git(root, "worktree", "add", "--quiet", "-b", "side/" + name, str(p["wt"]), base)
        try:
            rulesync.sync_project(p["wt"])
        except Exception:  # the start guard names the rules then
            pass
        try:  # B-118: a side session runs the review too; a project not synced yet lacks the folder at HEAD
            from awb import projects

            projects.add_workflows_dir(p["wt"])
        except Exception:
            pass
        _write(root, prompt, PREAMBLE.format(code=root.name, name=name, result=RESULT) + text)
        basefd = os.open(p["base"], os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        fds.append(basefd)
        fds.append(os.open(prompt.name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=basefd))
        for key in ("out", "err"):
            fds.append(os.open(p[key].name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                               0o600, dir_fd=basefd))
        env = _environment()
        proc = subprocess.Popen([sys.executable, "-I", "-c", _WRAPPER, *client_argv(client, model, effort),
                                 *map(str, fds), p["exit"].name], cwd=str(p["wt"]), env=env,
                                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                start_new_session=True, pass_fds=tuple(fds))
        os.unlink(prompt.name, dir_fd=basefd)
        _write(root, p["state"], json.dumps({"name": name, "model": model, "effort": effort, "started": now(),
                                           "pid": proc.pid, "birth": _birth(proc.pid),
                                           "brief": bpath.relative_to(root).as_posix(), "base": base}) + "\n")
    except Exception:
        if proc is not None:
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            proc.wait()
        _drop(root, name)
        raise SideError("the side session could not start") from None
    finally:
        for fd in fds:
            os.close(fd)
    return ("side session %s started (%s, effort %s) in %s/%s; awb side status, then awb side take %s"
            % (name, model, effort, SIDE_DIR, name, name))


def _birth(pid: int) -> str:
    try:
        return Path("/proc/%d/stat" % pid).read_text().rsplit(")", 1)[1].split()[19]
    except (OSError, IndexError):
        return ""


def _owned(meta: dict, wt: Path) -> bool:
    pid = meta.get("pid")
    if type(pid) is not int or not 1 < pid <= 2147483647:
        return False
    try:
        argv = Path("/proc/%d/cmdline" % pid).read_bytes().split(b"\0")
        signature = (_WRAPPER.encode() in argv and bool(meta.get("birth"))
                     and meta["birth"] == _birth(pid))
        legacy = not meta.get("birth") and _LEGACY_WRAPPER.encode() in argv
        return ((signature or legacy) and os.getpgid(pid) == pid
                and Path("/proc/%d" % pid).stat().st_uid == os.getuid()
                and Path("/proc/%d/cwd" % pid).resolve() == wt
                and _alive(pid))
    except (OSError, ValueError):
        return False


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    try:
        stat = Path("/proc/%d/stat" % pid).read_text()
        return stat.rsplit(")", 1)[1].split()[0] != "Z"
    except (FileNotFoundError, ProcessLookupError):
        return False    # reaped between the signal and the read (Q-0013: a dead client read as alive)
    except (OSError, IndexError):
        return True


def _state(p: dict[str, Path]) -> tuple[str, dict]:
    root = p["base"].parent
    try:
        meta = json.loads(_read(root, p["state"]))
        if not isinstance(meta, dict):
            return "lost", {}
    except (OSError, ValueError, SideError, RecursionError):
        return "lost", {}
    if _owned(meta, p["wt"]):
        return "running", meta
    if os.path.lexists(p["exit"]):
        try:
            rc = int(_read(root, p["exit"], 32).strip())
            if rc != 0:
                return "failed", meta
            out = json.loads(_read(root, p["out"]))
        except (OSError, ValueError, SideError, RecursionError):
            return "failed", meta
        turns = out.get("num_turns") if isinstance(out, dict) else None
        done = (type(turns) is int and turns > 0 and out.get("is_error", False) is False)
        return ("done" if done else "failed"), meta
    return "lost", meta


_COMMIT_RE = re.compile(r"[0-9a-f]{40}|[0-9a-f]{64}")


def _inherited(root: Path, name: str, meta: dict, text: str) -> bool:
    """True when `text`, the SIDE-RESULT.md of the worktree, is the one of the commit the side session started
    from: the result of an earlier side session that a merge brought into the project, not this session's
    (tcp-6edq: a take of failed sessions with no commit copied such an old result). The start commit is the one
    the record names, else the merge base of the project and side/NAME. False when git cannot tell."""
    base = meta.get("base") if isinstance(meta, dict) else None
    if not (isinstance(base, str) and _COMMIT_RE.fullmatch(base)):
        r = _git(root, "merge-base", "HEAD", "side/" + name, check=False)
        base = r.stdout.strip() if r.returncode == 0 else ""
        if not _COMMIT_RE.fullmatch(base):
            return False
    r = _git(root, "show", "%s:%s" % (base, RESULT), check=False)
    return r.returncode == 0 and r.stdout == text


def _has_result(root: Path, p: dict[str, Path], name: str, meta: dict) -> bool:
    """A SIDE-RESULT.md of this side session is in its worktree (not one it inherited)."""
    if not _plain(root, p["wt"] / RESULT):
        return False
    try:
        return not _inherited(root, name, meta, _read(root, p["wt"] / RESULT, MAX_RESULT))
    except (OSError, SideError):
        return True


@_command
def status_lines(cwd: Path | None = None) -> list[str]:
    root = project_root(cwd)
    base = root / SIDE_DIR
    lines = []
    for f in sorted(base.glob("*.state")) if base.is_dir() else []:
        name = f.name[:-len(".state")]
        if not _NAME_RE.fullmatch(name) or not _safe(name):
            continue
        p = _paths(root, name)
        st, meta = _state(p)
        n = _git(root, "rev-list", "--count", "HEAD..side/" + name, check=False).stdout.strip() or "0"
        model, effort = meta.get("model"), meta.get("effort")
        model = model if isinstance(model, str) and _MODEL_RE.fullmatch(model) and _safe(model) else "?"
        effort = effort if isinstance(effort, str) and effort in EFFORTS else "?"
        n = n if re.fullmatch(r"[0-9]+", n) else "0"
        lines.append("%s  %s  %s/%s  %s commit(s)  result: %s"
                     % (name, st, model, effort, n,
                        "yes" if _has_result(root, p, name, meta) else "no"))
        if st == "failed":
            lines.append("  reason: %s" % _failure_reason(root, p))
    return lines or ["no side sessions"]


def _failure_reason(root: Path, p: dict[str, Path]) -> str:
    """Why a side session failed, in one line: the client prints its error into the JSON answer, not to standard
    error (tcp-s2z5 2026-11-25), so the first line of its error text, then of the error file, else a plain line."""
    for key in ("out", "err"):
        try:
            text = _read(root, p[key], MAX_RESULT) if os.path.lexists(p[key]) else ""
        except (OSError, SideError):
            continue
        if key == "out":
            try:
                answer = json.loads(text or "{}")
            except ValueError:
                answer = {}
            if not isinstance(answer, dict):
                continue
            parts = [str(answer[k]) for k in ("api_error_status", "result", "subtype")
                     if answer.get(k) and (k != "subtype" or answer.get("is_error"))]
            text = "\n".join(parts)
        line = next((x.strip() for x in text.splitlines() if x.strip()), "")
        line = re.sub(r"[\x00-\x1f\x7f]", "", line)[:200]
        if line:
            return line
    return "the client gave no reason (no answer and an empty error file)"


@_command
def take(name: str, merge: bool = False, cwd: Path | None = None) -> list[str]:
    root = project_root(cwd)
    p = _paths(root, _name(name))
    if not p["state"].exists():
        raise SideError("no side session of that name: awb side status")
    st, meta = _state(p)
    if st == "running":
        raise SideError("the side session still runs: awb side status, or awb side stop NAME")
    out = []
    result = p["wt"] / RESULT
    if os.path.lexists(p["wt"]) and not _plain(root, p["wt"], directory=True):
        raise SideError("the worktree must be a directory of this project")
    text = _read(root, result, MAX_RESULT) if os.path.lexists(result) else None
    fresh = None
    if text is not None and _inherited(root, name, meta, text):
        out.append("result: none (%s is the one of the start commit, an earlier result, not this session's)"
                   % RESULT)
    elif text is not None:
        _require_safe_result(text)
        fresh = text
        if not merge:
            _write_note(root, name, text)
        out.append("result: %s/side-%s.md" % (NOTES, name))
    else:
        out.append("result: none (%s missing)" % RESULT)
    log = _git(root, "log", "--format=%h %s", "HEAD..side/" + name).stdout.strip()
    files = [f for f in _git(root, "diff", "--name-only", "-z", "HEAD...side/" + name).stdout.split("\0") if f]
    out.append("commits: %d" % (len(log.splitlines()) if log else 0))
    out += ["  " + (ln if _display_safe(ln) else "[withheld]") for ln in log.splitlines()[:20]]
    out.append("files: %s" % (", ".join(f if _display_safe(f) else "[withheld]" for f in files[:30])
                              if files else "none"))
    if merge and log:
        try:
            _merge(root, name, fresh)
        except SideError:
            if fresh is not None:
                _write_note(root, name, fresh)
            raise
        out.append("merged side/%s" % name)
    elif merge and fresh is not None:
        _write_note(root, name, fresh)
    if merge:
        out.append(_drop(root, name))
    return out


def _write_note(root: Path, name: str, text: str) -> None:
    notes = root / NOTES
    notes.mkdir(exist_ok=True)
    _write(root, notes / ("side-%s.md" % name), text)


def _tracked(root: Path, rel: str) -> bool:
    return _git(root, "ls-files", "--error-unmatch", "--", rel, check=False).returncode == 0


def _merge(root: Path, name: str, fresh: str | None) -> None:
    """Merge side/NAME with its result as notes/side-NAME.md and without SIDE-RESULT.md (tcp-bj3o ask
    2026-11-24-1): every side branch adds SIDE-RESULT.md at the root, so it stays as HEAD has it and the second
    merge does not conflict on it; a copy of an earlier plain take (untracked, equal to the result) gives way to
    the merge. A conflict on any other path aborts the merge and names nothing of the content."""
    note = "%s/side-%s.md" % (NOTES, name)
    note_path = root / NOTES / ("side-%s.md" % name)
    if fresh is not None and not _tracked(root, note) and os.path.lexists(note_path):
        if _plain(root, note_path) and _read(root, note_path, MAX_RESULT) == fresh:
            os.unlink(note_path)
    r = _git(root, "merge", "--no-ff", "--no-commit", "side/" + name, check=False)
    conflict = [f for f in _git(root, "diff", "--name-only", "-z", "--diff-filter=U",
                                check=False).stdout.split("\0") if f]
    started = _git(root, "rev-parse", "-q", "--verify", "MERGE_HEAD", check=False).returncode == 0
    if not started or [f for f in conflict if f not in (RESULT, note)]:
        if started:
            _git(root, "merge", "--abort", check=False)
        raise SideError("the merge of side/%s %s; nothing merged, the side stays"
                        % (name, "conflicts" if conflict or r.returncode else "failed (git merge)"))
    if _git(root, "cat-file", "-e", "HEAD:" + RESULT, check=False).returncode == 0:
        _git(root, "checkout", "HEAD", "--", RESULT)
    else:
        _git(root, "rm", "-q", "-f", "--ignore-unmatch", "--", RESULT)
    if fresh is not None:
        _write_note(root, name, fresh)
        _git(root, "add", "--", note)
    elif note in conflict:
        _git(root, "checkout", "--theirs", "--", note)
        _git(root, "add", "--", note)
    r = _git(root, "commit", "-q", "--no-edit", check=False)
    if r.returncode != 0:
        _git(root, "merge", "--abort", check=False)
        raise SideError("the merge of side/%s failed (git commit); nothing merged, the side stays" % name)


def _drop(root: Path, name: str) -> str:
    p = _paths(root, name)
    if os.path.lexists(p["wt"]):
        if not _plain(root, p["wt"], directory=True):
            raise SideError("the worktree must be a directory of this project")
        _git(root, "worktree", "remove", "--force", str(p["wt"]))
    if _git(root, "rev-parse", "--verify", "--quiet", "refs/heads/side/" + name, check=False).returncode == 0:
        _git(root, "branch", "-D", "side/" + name)
    for k in ("state", "exit", "out", "err"):
        p[k].unlink(missing_ok=True)
    (p["base"] / (name + ".prompt")).unlink(missing_ok=True)
    return "side session %s removed" % name


@_command
def stop(name: str, cwd: Path | None = None) -> str:
    root = project_root(cwd)
    p = _paths(root, _name(name))
    st, meta = _state(p)
    if st != "running":
        return "side session %s is %s" % (name, st)
    try:
        os.killpg(int(meta["pid"]), signal.SIGTERM)
    except ProcessLookupError:
        pass
    # A client may ignore TERM even after the wrapper exits. Finish the whole process group before
    # publishing completion, so drop cannot remove a worktree that still has a writer.
    time.sleep(0.1)
    try:
        os.killpg(meta["pid"], signal.SIGKILL)
    except ProcessLookupError:
        pass
    try:
        os.waitpid(meta["pid"], 0)
    except ChildProcessError:
        pass
    _write(root, p["exit"], "143\n")
    return "side session %s stopped" % name


@_command
def drop(name: str, cwd: Path | None = None) -> str:
    root = project_root(cwd)
    p = _paths(root, _name(name))
    if _state(p)[0] == "running":
        raise SideError("the side session still runs: awb side stop NAME first")
    return _drop(root, name)


def main(argv: list[str] | None = None) -> int:
    from awb.cli import SafeParser

    ap = SafeParser(prog="awb side", description="Side sessions of a project: side work in a session of its own.")
    sub = ap.add_subparsers(dest="command", required=True, parser_class=SafeParser)
    s = sub.add_parser("start", help="start a side session in a worktree with a brief file of the project")
    s.add_argument("name", metavar="NAME")
    s.add_argument("--brief", required=True, metavar="FILE")
    s.add_argument("--model", default=DEFAULT_MODEL, metavar="MODEL")
    s.add_argument("--effort", default=DEFAULT_EFFORT, metavar="LEVEL")
    sub.add_parser("status", help="one line per side session: state, model, commits, result")
    t = sub.add_parser("take", help="copy the result to notes/side-NAME.md; --merge merges the branch and removes it")
    t.add_argument("name", metavar="NAME")
    t.add_argument("--merge", action="store_true")
    for cmd, helptext in (("stop", "end a running side session"), ("drop", "remove a side session unused")):
        sub.add_parser(cmd, help=helptext).add_argument("name", metavar="NAME")
    args = ap.parse_args(argv)
    try:
        if args.command == "start":
            print(start(args.name, args.brief, args.model, args.effort))
        elif args.command == "status":
            print("\n".join(status_lines()))
        elif args.command == "take":
            print("\n".join(take(args.name, args.merge)))
        elif args.command == "stop":
            print(stop(args.name))
        else:
            print(drop(args.name))
    except SideError as err:
        print("awb side: %s" % err, file=sys.stderr)
        return 2
    except (OSError, ValueError, RuntimeError):
        print("awb side: the operation failed; check project files and permissions", file=sys.stderr)
        return 2
    return 0
