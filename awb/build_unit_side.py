"""What runs inside the build user's units: `awb build unit prepare|rekey|handover|rebase|suite|clean|outdir`.

A part of the build runner (awb/buildrun.py, F1 step 6). These helpers run as the build user and never as the owner
(`unit_allowed`); they import nothing of the runner. They make the task home with the login or the model key and the
clone of main from the bundle on stdin, hand the branch back as a bundle, rebase it, run the suite on an export (the
junit outcomes, the one rerun of failed nodes) and clean up.
"""
from __future__ import annotations

import contextlib
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tarfile
import tempfile
import time
import xml.etree.ElementTree as ET
from pathlib import Path

from awb import build, config


# --------------------------------------------------------------------------- the fixed values

HOOKS_BUILD = "/opt/tcp-awb/src/hooks-build"
KEY_IN_HOME = ".config/awb-build/model.key"
"""Where the prepare unit puts the model key in the task home (build_model = key); the template's settings read it
with apiKeyHelper. The home and the key go when the task closes."""
LOGIN_IN_HOME = ".claude/.credentials.json"
"""The client's login (build_model = login, D-S7 changed): his subscription login lies in the build user's own home
<build_root>/home/.claude/; the prepare unit copies it into the task home (600) and the clean unit removes it with
the home, after a newer token of the task went back. The owner never reads it and it never enters runs/."""
LOGIN_MAX = 64 << 10
LOGIN_ACCOUNT = "subscriptionType"
"""The field of the login (`claudeAiOauth`) that names what the login belongs to, the only one of that kind in the
file (review of 2026-10-13, B7): a login of the task goes back to the build user's home only when it names the same
value as the login there, and a login without the field never does. A task that writes a login of its own, with a
far-future expiresAt, keeps the build user's login where it was."""
LOGIN_IDENTITY = ("accountUuid", "organizationUuid")
"""Fields of `claudeAiOauth` that name one account, not a plan (review of 2026-10-20, K8): when the login in the build
user's home has one of them the task's login must hold the same value."""
LOGIN_TOKEN_FIELDS = ("accessToken", "refreshToken", "expiresAt")
LOGIN_EXPIRY_STEP_MS = 48 * 3600 * 1000
"""The most a refresh moves expiresAt ahead of the login it replaces (K8); a later one is not a refresh."""
LOGIN_KEPT = 6
"""Exit of the clean unit when a login of the task was not copied back for its account (the home went all the same);
the runner puts LOGIN_KEPT_LINE into the run's result."""
LOGIN_KEPT_LINE = "- login: the task's login names another account or none and was not copied back; the build user's stays"
MODELS = ("login", "key")
FAULT_SECONDS = 600         # every thread's stack when one test takes ten minutes
RERUN_MAX = 20              # A7: more failed nodes than this get no rerun, a broad failure is no flake
LOG_MAX = 16 << 20
UUID_RE = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
SHA_RE = re.compile(r"[0-9a-f]{40}")


# --------------------------------------------------------------------------- the helpers in the units (build user)

_HANDIN_RE = re.compile(rb"AWB-HANDIN 1 ([0-9]{1,5})\n")


def handin(key: bytes, bundle: Path | None) -> bytes:
    """The framing of the prepare unit's stdin: one header line, the key, then the bundle to the end."""
    return b"AWB-HANDIN 1 %d\n" % len(key) + key + (Path(bundle).read_bytes() if bundle else b"")


def _helper_env(root: Path, home: str) -> dict[str, str]:
    """git as the build user: the owner's identity from the root template (D-S16), no system file, no pager."""
    return {"HOME": home, "PATH": os.environ.get("PATH", "/usr/bin:/bin"), "LANG": "C.UTF-8",
            "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": str(root / "home-template" / ".gitconfig"),
            "GIT_TERMINAL_PROMPT": "0"}


# the fixed configuration of git in a helper unit: every key of a clone's .git/config that runs a command is either
# overridden here or refused by _clone_ok (review of 2026-10-13, B1)
_HGIT_FIXED = (("core.fsmonitor", "false"), ("core.hooksPath", "/dev/null"), ("core.attributesFile", "/dev/null"),
               ("commit.gpgSign", "false"), ("tag.gpgSign", "false"))
# what a clone's .git/config may hold besides the keys of _HGIT_FIXED: what the clone, the remote removal and the
# branch of the prepare unit leave and keys that run nothing
_CLONE_KEY_RE = re.compile(r"core\.(?:repositoryformatversion|filemode|bare|logallrefupdates|ignorecase|"
                           r"precomposeunicode|symlinks)|user\.(?:name|email)|branch\.[^\n]+\.(?:remote|merge)")


def _hgit(env: dict, *args: str, timeout: float = 300, stdin=None) -> subprocess.CompletedProcess:
    fixed = [a for k, v in _HGIT_FIXED for a in ("-c", "%s=%s" % (k, v))]
    return subprocess.run(["git", *fixed, *args], env=env, capture_output=True, timeout=timeout, check=False,
                          stdin=stdin)


def _clone_ok(env: dict, clone: Path) -> bool:
    """Before git runs in a task's clone: .git is a folder of its own (no gitfile, no link), holds no commondir,
    config.worktree or info/attributes, and its config holds only the keys of _CLONE_KEY_RE and _HGIT_FIXED, read
    without includes. A filter, a diff or merge driver, gpg.program, an include or an extension of the session's
    choice never runs in a helper unit."""
    git_dir = clone / ".git"
    if git_dir.is_symlink() or not git_dir.is_dir():
        return False
    if any(os.path.lexists(git_dir / rel) for rel in ("commondir", "config.worktree", "info/attributes")):
        return False
    cfg = git_dir / "config"
    if cfg.is_symlink() or not cfg.is_file():
        return False
    r = _hgit(env, "config", "--file", str(cfg), "--no-includes", "--null", "--list")
    if r.returncode:
        return False
    fixed = {k.lower() for k, _ in _HGIT_FIXED}
    for item in r.stdout.split(b"\0"):
        key = item.split(b"\n", 1)[0].decode("utf-8", "replace")
        if key and not (_CLONE_KEY_RE.fullmatch(key) or key.lower() in fixed):
            return False
    return True


def _helper_places(root: str, tid: str, n: str) -> tuple[Path, Path, Path, Path]:
    if not (build.ID_RE.fullmatch(tid) or build.CHECK_ID_RE.fullmatch(tid)) or not re.fullmatch(r"[1-9][0-9]{0,3}", n):
        raise ValueError("not a task id or run number")
    r = Path(root)
    name = "%s-%s" % (tid, n)
    return r, r / "wt" / name, r / "wt" / (name + ".out"), r / "home" / name


def _write_key(home: Path, key: bytes, rel: str = KEY_IN_HOME) -> None:
    d = home / os.path.dirname(rel)
    d.mkdir(parents=True, exist_ok=True, mode=0o700)
    target = home / rel
    with contextlib.suppress(FileNotFoundError):
        os.unlink(target)
    fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
    with os.fdopen(fd, "wb") as fh:
        fh.write(key)


def _read_login(base: Path) -> bytes | None:
    """The client's login under `base` (a home): a regular file of this uid, reached without following a link at
    any step, at most LOGIN_MAX bytes and not empty; None otherwise. Never printed."""
    fds = []
    try:
        fds.append(os.open(base, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC))
        parts = LOGIN_IN_HOME.split("/")
        for part in parts[:-1]:
            fds.append(os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=fds[-1]))
        fd = os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC, dir_fd=fds[-1])
        fds.append(fd)
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode) or st.st_uid != os.geteuid() or not 0 < st.st_size <= LOGIN_MAX:
            return None
        data = os.read(fd, LOGIN_MAX + 1)
        return data if 0 < len(data) <= LOGIN_MAX else None
    except OSError:
        return None
    finally:
        for f in reversed(fds):
            os.close(f)


def _login_expiry(data: bytes | None) -> float | None:
    """expiresAt of a login file (`claudeAiOauth` with an access and a refresh token as strings); None when the
    bytes are not that shape."""
    if data is None:
        return None
    try:
        obj = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, ValueError, RecursionError):
        return None
    oauth = obj.get("claudeAiOauth") if isinstance(obj, dict) else None
    if not isinstance(oauth, dict):
        return None
    exp = oauth.get("expiresAt")
    if not all(isinstance(oauth.get(k), str) and oauth.get(k) for k in ("accessToken", "refreshToken")) or \
            isinstance(exp, bool) or not isinstance(exp, (int, float)):
        return None
    return float(exp)


def _login_account(data: bytes | None) -> str | None:
    """LOGIN_ACCOUNT of a login file in the login's shape, a string with something in it; None otherwise."""
    if _login_expiry(data) is None:
        return None
    account = json.loads(data.decode("utf-8"))["claudeAiOauth"].get(LOGIN_ACCOUNT)
    return account if isinstance(account, str) and account.strip() else None


def _login_back(r: Path, home: Path) -> bool:
    """A token the task's client refreshed goes back to the build user's own home before the task home goes: only
    over a login that is there, only in the login's shape, only when it names the same LOGIN_ACCOUNT as the one
    there (B7) and only when it expires later. The refresh may rotate the refresh token, so the next task would
    otherwise start from a spent one. False when a login of the task was there and not copied for its account (it
    names another or none): the build user's login stays and the caller says so. True otherwise."""
    persistent = r / "home"
    new = _read_login(home)
    old = _read_login(persistent)
    exp_new, exp_old = _login_expiry(new), _login_expiry(old)
    if new is None or exp_new is None or exp_old is None:
        return True
    account = _login_account(new)
    if account is None or account != _login_account(old):
        return False
    oauth_old = json.loads(old.decode("utf-8"))["claudeAiOauth"]
    oauth_new = json.loads(new.decode("utf-8"))["claudeAiOauth"]
    if any(k in oauth_old and oauth_old[k] != oauth_new.get(k) for k in LOGIN_IDENTITY):
        return False
    if exp_new > exp_old + LOGIN_EXPIRY_STEP_MS:
        return False
    if exp_new <= exp_old:
        return True
    # K8: only the token fields travel, into the object that is there; a key the task added stays behind
    for k in LOGIN_TOKEN_FIELDS:
        oauth_old[k] = oauth_new[k]
    merged = json.loads(old.decode("utf-8"))
    merged["claudeAiOauth"] = oauth_old
    new = json.dumps(merged).encode("utf-8")
    d = persistent / os.path.dirname(LOGIN_IN_HOME)
    if d.is_symlink() or not d.is_dir():
        return True
    tmp = d / (".credentials.awb-%d" % os.getpid())
    with contextlib.suppress(FileNotFoundError):
        os.unlink(tmp)
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(new)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, persistent / LOGIN_IN_HOME)
    finally:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(tmp)
    return True


def unit_prepare(root: str, tid: str, n: str, hooks: str, fin, resume: str | None = None,
                 model: str = "key") -> int:
    """The task home and the clone. stdin: the hand-in (the key, then a bundle of main). A fresh home from the root
    template (a home that is there already is refused: a task never inherits another's), the key, a clone of main
    from the bundle (no remote left), the branch task/<ID>, core.hooksPath to the installed build hooks, the start
    commit in out/start.txt. With `resume` (a session id) only the key goes into the existing home; exit 4 says
    the session's transcript is gone. With `model` login the hand-in carries no key: the login of the build user's
    own home is copied into the task home instead (600), and exit 7, before anything is made, says there is none."""
    r, clone, out, home = _helper_places(root, tid, n)
    if model not in MODELS:
        return 2
    head = fin.readline(64)
    m = _HANDIN_RE.fullmatch(head)
    if not m:
        return 2
    key = fin.read(int(m.group(1)))
    login = None
    if model == "login":
        if key:
            return 2
        login = _read_login(r / "home")
        if login is None:
            return 7
    if resume is not None:
        if not UUID_RE.fullmatch(resume) or not home.is_dir() or home.is_symlink():
            return 4
        found = list((home / ".claude" / "projects").glob("*/%s.jsonl" % resume))
        if not found:
            return 4
        if key:
            _write_key(home, key)
        if login:
            _write_key(home, login, LOGIN_IN_HOME)
        return 0
    if os.path.lexists(home) or os.path.lexists(clone) or os.path.lexists(out):
        return 3
    (r / "home").mkdir(exist_ok=True)
    os.mkdir(home, 0o700)
    os.mkdir(home / ".claude", 0o700)
    t = r / "home-template"
    for rel in (".claude/settings.json", ".claude/CLAUDE.md", ".gitconfig"):
        src = t / rel
        if src.is_file() and not src.is_symlink():
            shutil.copyfile(src, home / rel, follow_symlinks=False)
            os.chmod(home / rel, 0o444)
    if key:
        _write_key(home, key)
    if login:
        _write_key(home, login, LOGIN_IN_HOME)
    login = None
    os.mkdir(out, 0o755)
    bundle = out / "main.bundle"
    with open(bundle, "wb") as fh:
        shutil.copyfileobj(fin, fh)
    env = _helper_env(r, str(home))
    try:
        if _hgit(env, "clone", "--quiet", "--no-hardlinks", "--branch", "main", str(bundle), str(clone)).returncode:
            return 5
        for args in (("remote", "remove", "origin"), ("checkout", "--quiet", "-b", "task/%s" % tid),
                     ("config", "core.hooksPath", hooks)):
            if _hgit(env, "-C", str(clone), *args).returncode:
                return 5
        start = _hgit(env, "-C", str(clone), "rev-parse", "HEAD")
        if start.returncode:
            return 5
        (out / "start.txt").write_bytes(start.stdout.strip() + b"\n")
    finally:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(bundle)
    return 0


def unit_handover(root: str, tid: str, n: str) -> int:
    """task.bundle (every branch and tag of the clone) and status.txt (`git status --porcelain`, untracked files
    too) into the out folder, with the fixed configuration of _hgit; exit 4 refuses a clone _clone_ok refuses."""
    r, clone, out, home = _helper_places(root, tid, n)
    env = _helper_env(r, str(home) if home.is_dir() else "/tmp")
    if not clone.is_dir() or clone.is_symlink():
        return 2
    if not _clone_ok(env, clone):
        return 4
    for name in ("task.bundle", "status.txt"):
        with contextlib.suppress(FileNotFoundError):
            os.unlink(out / name)
    st = _hgit(env, "-C", str(clone), "status", "--porcelain=v1", "-z", "--untracked-files=all")
    if st.returncode:
        return 5
    (out / "status.txt").write_bytes(st.stdout)
    b = _hgit(env, "-C", str(clone), "bundle", "create", "--quiet", str(out / "task.bundle"), "--branches", "--tags")
    return 0 if b.returncode == 0 else 6


def unit_rebase(root: str, tid: str, n: str, old: str, fin) -> int:
    """The branch onto a fresh main: stdin a bundle of main; exit 3 on a conflict (the rebase is aborted), then the
    hand-over again; exit 4 refuses a clone _clone_ok refuses."""
    r, clone, out, home = _helper_places(root, tid, n)
    if not SHA_RE.fullmatch(old) or not clone.is_dir():
        return 2
    env = _helper_env(r, "/tmp")
    if not _clone_ok(env, clone):
        return 4
    bundle = out / "main.bundle"
    with open(bundle, "wb") as fh:
        shutil.copyfileobj(fin, fh)
    try:
        if _hgit(env, "-C", str(clone), "fetch", "--quiet", "--no-tags", str(bundle),
                 "+refs/heads/main:refs/heads/main").returncode:
            return 5
    finally:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(bundle)
    rb = _hgit(env, "-C", str(clone), "rebase", "--quiet", "--onto", "refs/heads/main", old, "task/%s" % tid)
    if rb.returncode:
        _hgit(env, "-C", str(clone), "rebase", "--abort")
        return 3
    return unit_handover(root, tid, n)


def failed_nodes(results: dict[str, str] | None) -> list[str]:
    """The nodes of a suite that failed or broke, for the one rerun of A7: none when there are more than RERUN_MAX
    (a broad failure is no flake) or when a node id could be read as an option."""
    nodes = sorted(i for i, o in (results or {}).items() if o in ("failed", "error"))
    if len(nodes) > RERUN_MAX or any(not i or i.startswith("-") or "\n" in i or "\0" in i for i in nodes):
        return []
    return nodes


def unit_suite(root: str, tid: str, n: str, mode: str, python: str, tokens: list[str], fin,
               seconds: float | None = None) -> int:
    """The suite, the baseline or the task's own check on the export given as a tar on stdin, extracted into a fresh
    folder of this unit's private /tmp: the suite's interpreter with -I -B and the cache provider off, a fault handler
    after ten minutes, the outcome per test in out/<mode>.xml and the output in out/<mode>.log. A7: when the whole
    suite (mode `suite`, never the baseline or the task's own check) has failures, the failed nodes alone run once
    more in this unit on the same tree, into out/rerun.xml and out/rerun.log; the exit code stays the first run's."""
    r, clone, out, home = _helper_places(root, tid, n)
    if mode not in ("base", "suite", "check"):
        return 2
    out.mkdir(exist_ok=True)
    for name in ("rerun.xml", "rerun.log"):
        with contextlib.suppress(FileNotFoundError):
            os.unlink(out / name)
    tmp = Path(tempfile.mkdtemp(prefix="awb-suite-"))
    try:
        tree = tmp / "tree"
        tree.mkdir()
        with tarfile.open(fileobj=fin, mode="r|") as tar:
            tar.extractall(tree, filter="data")
        (tmp / "home").mkdir()
        xml = tmp / "junit.xml"
        env = {"HOME": str(tmp / "home"), "PATH": os.environ.get("PATH", "/usr/bin:/bin"), "LANG": "C.UTF-8",
               "PYTHONDONTWRITEBYTECODE": "1", "TMPDIR": str(tmp)}
        t0 = time.monotonic()

        def pytest(name: str, junit: Path, args: list[str], limit: float | None) -> int:
            cmd = [python, "-I", "-B", "-m", "pytest", "-p", "no:cacheprovider", "-q", "-o",
                   "faulthandler_timeout=%d" % FAULT_SECONDS, "-o", "junit_family=xunit1", "--junitxml", str(junit),
                   *args]
            with open(out / ("%s.log" % name), "wb") as log:
                try:
                    return subprocess.run(cmd, cwd=tree, env=env, stdin=subprocess.DEVNULL, stdout=log,
                                          stderr=subprocess.STDOUT, timeout=limit, check=False).returncode
                except subprocess.TimeoutExpired:
                    return 124

        rc = pytest(mode, xml, list(tokens), seconds)
        if xml.is_file():
            shutil.copyfile(xml, out / ("%s.xml" % mode))
        if mode == "suite" and rc not in (0, 124) and xml.is_file():
            nodes = failed_nodes(outcomes(xml.read_bytes()))
            left = None if seconds is None else seconds - (time.monotonic() - t0)
            if nodes and (left is None or left > 1):
                again = tmp / "rerun.xml"
                pytest("rerun", again, nodes, left)
                if again.is_file():
                    shutil.copyfile(again, out / "rerun.xml")
        return rc
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def unit_clean(root: str, tid: str, n: str, what: str, model: str = "key") -> int:
    """`key`: the model key and the login out of the task home; `home`: the task home; `all`: the home, the clone
    and the out folder. rmtree follows no link. With `model` login a token the task refreshed goes back to the build
    user's own home first (_login_back); exit LOGIN_KEPT when a login of the task was not copied for its account. With
    `model` key nothing goes back: the task had no login to refresh."""
    r, clone, out, home = _helper_places(root, tid, n)
    targets = [home] if what == "home" else [home, clone, out] if what == "all" else []
    if (what != "key" and not targets) or model not in MODELS:
        return 2
    rc = 0
    if model == "login" and home.is_dir() and not home.is_symlink():
        try:
            rc = 0 if _login_back(r, home) else LOGIN_KEPT
        except (OSError, ValueError, RecursionError):
            rc = LOGIN_KEPT     # K22: whatever the task's file did, the home still goes below
    if what == "key":
        for rel in (KEY_IN_HOME, LOGIN_IN_HOME):
            with contextlib.suppress(FileNotFoundError, NotADirectoryError):
                os.unlink(home / rel)
        return rc
    for t in targets:
        if os.path.islink(t):
            os.unlink(t)
        elif t.exists():
            for folder, dirs, _files in os.walk(t):
                for d in dirs:
                    with contextlib.suppress(OSError):
                        os.chmod(os.path.join(folder, d), 0o700)
            shutil.rmtree(t, ignore_errors=True)
    return rc


def unit_outdir(root: str, tid: str, n: str) -> int:
    """The out folder of a check of a branch (awb build check), which has no prepare unit: the suite unit may write
    only a folder that is there. Exit 3 when something is at its place already."""
    r, clone, out, home = _helper_places(root, tid, n)
    if not build.CHECK_ID_RE.fullmatch(tid):
        return 2
    if os.path.lexists(out):
        return 3
    os.mkdir(out, 0o755)
    return 0


def unit_main(args: list[str], fin=None, home: str | None = None, seconds: float | None = None) -> int:
    """`awb build unit ACTION ...`, what the units run as the build user. Never the owner on a sealed host."""
    fin = fin if fin is not None else sys.stdin.buffer
    if not args:
        return 2
    action, rest = args[0], args[1:]
    try:
        if action == "prepare" and len(rest) in (4, 5):
            return unit_prepare(*rest[:4], fin=fin, model=rest[4] if len(rest) == 5 else "key")
        if action == "rekey" and len(rest) in (4, 5):
            return unit_prepare(rest[0], rest[1], rest[2], HOOKS_BUILD, fin, resume=rest[3],
                                model=rest[4] if len(rest) == 5 else "key")
        if action == "handover" and len(rest) == 3:
            return unit_handover(*rest)
        if action == "rebase" and len(rest) == 4:
            return unit_rebase(*rest, fin=fin)
        if action == "suite" and len(rest) >= 5:
            return unit_suite(rest[0], rest[1], rest[2], rest[3], rest[4], rest[5:], fin, seconds)
        if action == "clean" and len(rest) in (4, 5):
            return unit_clean(*rest)
        if action == "outdir" and len(rest) == 3:
            return unit_outdir(*rest)
    except (ValueError, OSError, tarfile.TarError, subprocess.SubprocessError):
        return 2
    return 2


def unit_allowed() -> str | None:
    """The unit helpers run as the build user; the owner and the work user of a sealed host never run them."""
    if config.is_work_user():
        return "awb build unit is the build user's"
    owner = config.host_conf().get("owner")
    if owner:
        import pwd

        try:
            if pwd.getpwuid(os.geteuid()).pw_name == owner:
                return "awb build unit never runs as the owner: it runs in the build user's units"
        except KeyError:
            pass
    return None


# --------------------------------------------------------------------------- the outcomes of a suite run

def outcomes(xml: bytes | None) -> dict[str, str] | None:
    """Node id -> passed, failed, error or skipped from a junit file of the xunit1 family; None when it cannot be
    read (too large, a DOCTYPE, not XML)."""
    if xml is None or len(xml) > LOG_MAX or b"<!DOCTYPE" in xml or b"<!ENTITY" in xml:
        return None
    try:
        root = ET.fromstring(xml)
    except ET.ParseError:
        return None
    out: dict[str, str] = {}
    for case in root.iter("testcase"):
        name = case.get("name") or ""
        cls = case.get("classname") or ""
        file = case.get("file") or ""
        if file.endswith(".py"):
            mod = file[:-3].replace("/", ".")
            rest = cls[len(mod) + 1:] if cls.startswith(mod + ".") else ""
            node = file + "::" + (rest.replace(".", "::") + "::" if rest else "") + name
        else:
            node = cls + "::" + name
        kinds = {child.tag for child in case}
        out[node] = ("failed" if "failure" in kinds else "error" if "error" in kinds
                     else "skipped" if "skipped" in kinds else "passed")
    return out
