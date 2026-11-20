"""The version and the self-update of `awb clean` on the laptop (task clean-update, 2026-11-20).

The laptop runs a git clone of the public repository (or, without git, the lists of an HTTPS update) and he starts
WSL rarely, so the module keeps itself current at two moments he already has:

- the first shell after a WSL boot: `awb clean shell` adds a block to ~/.bashrc and, with oh-my-zsh, the file
  awb-update.zsh of its custom folder that starts `awb clean update --boot` in the background once per boot (the
  boot id of /proc/sys/kernel/random/boot_id against the stamp boot-update.id of ~/awb-laptop/), under `timeout 60`,
  its output into ~/awb-laptop/update.log, never blocking the prompt;
- his apt routine: `awb clean apt-hook` prints the one sudo command that writes /etc/apt/apt.conf.d/99awb-clean, a
  Post-Invoke of `apt update` that runs `awb clean update --quiet` as his own user (runuser, never root) and never
  fails apt; `awb clean apt-hook --check` says whether it is there. Nothing here writes into /etc.

Every `awb clean` command on a terminal prints first `awb clean <short commit> (<commit date>)` (the clone's HEAD,
or the VERSION stamp of the rules folder an HTTPS update writes) and, once, what the last boot or apt update did.
An update is a `git pull --ff-only` and never touches local changes: a refusal says so and keeps the old version.
"""
from __future__ import annotations

import fcntl
import json
import os
import pwd
import re
import shlex
import shutil
import subprocess
import sys
import time
from datetime import datetime, timezone
from functools import partial
from pathlib import Path

from awb import laptop

BOOT_ID = Path("/proc/sys/kernel/random/boot_id")
BOOT_STAMP = "boot-update.id"
RECORD = "update-last.json"
LOG = "update.log"
LOCK = "update.lock"
VERSION_FILE = "VERSION"
BOOT_TIMEOUT = 60
"""Seconds the shell gives the boot update (`timeout 60`); git inside it gets GIT_TIMEOUT."""
GIT_TIMEOUT = 50
APT_TIMEOUT = 90
APT_FILE = Path("/etc/apt/apt.conf.d/99awb-clean")
RUNUSER = "/usr/sbin/runuser"
COMMIT_URL = "https://api.github.com/repos/shimoza/tcp-awb/commits/main"
COMPARE_URL = "https://api.github.com/repos/shimoza/tcp-awb/compare/%s...main"
BOOT_BEGIN = "# >>> awb clean update at the first shell after a boot >>>"
BOOT_END = "# <<< awb clean update at boot <<<"
ZSH_UPDATE_FILE = "awb-update.zsh"
SAFE_PATH_RE = re.compile(r"/[A-Za-z0-9/._+-]+")
SAFE_USER_RE = re.compile(r"[a-z_][a-z0-9_-]{0,31}")
HOW = {"boot": "boot update", "apt": "apt update"}


class UpdateSetupError(Exception):
    """A setup step cannot run. The message names the fix, never a value of his environment."""


# --------------------------------------------------------------------------- the version


def _rules(rules: Path | None = None) -> Path:
    from awb import patterns

    return Path(rules or patterns.RULES_DIR)


def _git(repo: Path, *args: str, timeout: int = 20) -> str | None:
    """One read-only git call in the clone, never above it; None when git fails or is missing."""
    env = dict(os.environ, GIT_CEILING_DIRECTORIES=str(repo.parent), GIT_TERMINAL_PROMPT="0", LC_ALL="C")
    try:
        got = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True, timeout=timeout,
                             env=env)
    except (OSError, subprocess.SubprocessError):
        return None
    return got.stdout.strip() if got.returncode == 0 else None


def is_clone(rules: Path | None = None) -> bool:
    return (_rules(rules).parent / ".git").exists()


def version(rules: Path | None = None) -> tuple[str, str] | None:
    """(short commit, commit date) of the clone's HEAD, or of the VERSION stamp of an install without git."""
    rules = _rules(rules)
    if is_clone(rules):
        out = _git(rules.parent, "log", "-1", "--format=%h %cs", "--abbrev=7", "HEAD")
        if out and re.fullmatch(r"[0-9a-f]{7,40} \d{4}-\d{2}-\d{2}", out):
            sha, day = out.split()
            return sha[:7], day
        return None
    stamp = read_stamp(rules)
    return (stamp[0][:7], stamp[1]) if stamp else None


def short(v: tuple[str, str] | None) -> str:
    return v[0] if v else "an unknown version"


def version_line(rules: Path | None = None) -> str:
    v = version(rules)
    if v:
        return "awb clean %s (%s)" % v
    if is_clone(rules):
        return "awb clean, version unknown (git cannot read the clone)"
    return "awb clean, version unknown (no git clone and no VERSION stamp yet: awb clean update writes one)"


def read_stamp(rules: Path) -> tuple[str, str] | None:
    try:
        text = (rules / VERSION_FILE).read_text(encoding="ascii")
    except (OSError, UnicodeError):
        return None
    m = re.fullmatch(r"([0-9a-f]{40}) (\d{4}-\d{2}-\d{2})\n?", text)
    return (m.group(1), m.group(2)) if m else None


def write_stamp(rules: Path, tree_sha: str | None, fetch, *, dir_fd: int | None = None) -> bool:
    """After an HTTPS update: the VERSION stamp of the commit of the public main, written only when the commit's
    tree is the tree the lists were checked against. Returns whether it was written; a failure changes nothing."""
    if not (isinstance(tree_sha, str) and re.fullmatch(r"[0-9a-f]{40}", tree_sha)):
        return False
    try:
        data = json.loads(fetch(COMMIT_URL).decode("utf-8"))
        sha = data["sha"]
        tree = data["commit"]["tree"]["sha"]
        day = data["commit"]["committer"]["date"][:10]
    except Exception:  # noqa: BLE001 - the stamp is optional, the lists are already in place
        return False
    if not (isinstance(sha, str) and re.fullmatch(r"[0-9a-f]{40}", sha) and tree == tree_sha
            and isinstance(day, str) and re.fullmatch(r"\d{4}-\d{2}-\d{2}", day)):
        return False
    tmp = ".%s.%d.tmp" % (VERSION_FILE, os.getpid())
    target = Path(tmp) if dir_fd is not None else rules / tmp
    try:
        fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o644, dir_fd=dir_fd)
        with os.fdopen(fd, "w", encoding="ascii") as fh:
            fh.write("%s %s\n" % (sha, day))
        os.replace(target, VERSION_FILE if dir_fd is not None else rules / VERSION_FILE, src_dir_fd=dir_fd,
                   dst_dir_fd=dir_fd)
    except OSError:
        try:
            os.unlink(target, dir_fd=dir_fd)
        except OSError:
            pass
        return False
    return True


def behind_line(rules: Path | None = None, fetch=None) -> str:
    """The line of `awb clean doctor`: how many commits the public main has that this install has not."""
    from awb import clean

    rules = _rules(rules)
    if is_clone(rules):
        repo = rules.parent
        if _git(repo, "fetch", "--quiet", "--no-tags", clean.PUBLIC_REPO, clean.PUBLIC_BRANCH,
                timeout=GIT_TIMEOUT) is None:
            return "public main: cannot tell how far behind (the public repository could not be fetched)"
        count = _git(repo, "rev-list", "--count", "HEAD..FETCH_HEAD")
        own = _git(repo, "rev-list", "--count", "FETCH_HEAD..HEAD")
        if count is None or not count.isdigit():
            return "public main: cannot tell how far behind (git cannot compare)"
        n = int(count)
    else:
        stamp = read_stamp(rules)
        if not stamp:
            return "public main: cannot tell how far behind (no VERSION stamp yet: awb clean update writes one)"
        try:
            data = json.loads((fetch or clean._fetch)(COMPARE_URL % stamp[0]).decode("utf-8"))
            n = data["ahead_by"]
            own = str(data.get("behind_by", 0))
        except Exception:  # noqa: BLE001 - a doctor line, never the text of the error
            return "public main: cannot tell how far behind (the public repository could not be asked)"
        if not isinstance(n, int):
            return "public main: cannot tell how far behind (an unexpected answer)"
    extra = (", and this clone has %s commit%s the public main has not (no fast-forward until they go)"
             % (own, "" if own == "1" else "s")) if own and own.isdigit() and int(own) else ""
    if n == 0:
        return "public main: up to date with it%s" % extra
    return "public main: %d commit%s behind (awb clean update brings %s)%s" % (
        n, "" if n == 1 else "s", "it" if n == 1 else "them", extra)


# --------------------------------------------------------------------------- the quiet runs and their record


def _home() -> Path:
    path = laptop.home()
    path.mkdir(parents=True, exist_ok=True)
    os.chmod(path, 0o700)
    return path


def _write_json(path: Path, data: dict) -> None:
    tmp = path.with_name(".%s.%d.tmp" % (path.name, os.getpid()))
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        json.dump(data, fh)
    os.replace(tmp, path)


def read_record() -> dict | None:
    try:
        data = json.loads((laptop.home() / RECORD).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) and data.get("how") in HOW else None


def _log(home: Path, line: str) -> None:
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    fd = os.open(home / LOG, os.O_WRONLY | os.O_CREAT | os.O_APPEND | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "a", encoding="utf-8") as fh:
        fh.write("%s %s\n" % (stamp, line))


def describe(rec: dict) -> str:
    """The one line of what a boot or apt update did."""
    what = HOW[rec["how"]]
    old, new = rec.get("from") or "an unknown version", rec.get("to") or "an unknown version"
    if rec.get("state") == "ok":
        return "%s: %s" % (what, "updated %s -> %s" % (old, new) if old != new else "already up to date, on %s" % old)
    if rec.get("state") == "running":
        return "%s: did not finish within %d s, still on %s" % (what, BOOT_TIMEOUT, old)
    return "%s: update failed: %s, still on %s" % (what, rec.get("reason") or "unknown", old)


def run_quiet(how: str, rules: Path | None = None, *, update=None, now=time.time) -> int:
    """One update without a terminal (the boot and the apt hook): the result goes into the record that the next
    `awb clean` command prints once and into update.log. Always 0: it never fails a shell start or apt."""
    import urllib.error

    from awb import clean

    home = _home()
    v = version(rules)
    old = short(v) if v else None
    rec = {"how": how, "state": "running", "from": old, "to": None, "reason": None, "time": int(now()),
           "shown": False}
    _write_json(home / RECORD, rec)
    try:
        (update or clean.update)(rules, run=partial(subprocess.run, timeout=GIT_TIMEOUT))
        rec["state"] = "ok"
    except clean.CleanError as err:
        rec.update(state="failed", reason=str(err))
    except urllib.error.URLError:
        rec.update(state="failed", reason="the public repository could not be reached (awb clean doctor names "
                                          "the fix)")
    except subprocess.TimeoutExpired:
        rec.update(state="failed", reason="git did not answer within %d s" % GIT_TIMEOUT)
    except Exception as err:  # noqa: BLE001 - recorded by its kind, never its text
        rec.update(state="failed", reason="%s in the update" % type(err).__name__)
    v = version(rules)
    rec["to"] = short(v) if v else None
    _write_json(home / RECORD, rec)
    _log(home, describe(rec))
    return 0


def boot(rules: Path | None = None, *, update=None) -> int:
    """`awb clean update --boot`: once per boot, the first caller after a boot runs the update; any other returns."""
    try:
        boot_id = BOOT_ID.read_text(encoding="ascii").strip()
    except (OSError, UnicodeError):
        return 0
    if not re.fullmatch(r"[0-9a-f-]{8,64}", boot_id):
        return 0
    home = _home()
    fd = os.open(home / LOCK, os.O_WRONLY | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return 0
        stamp = home / BOOT_STAMP
        try:
            if stamp.read_text(encoding="ascii").strip() == boot_id:
                return 0
        except (OSError, UnicodeError):
            pass
        stamp.write_text(boot_id + "\n", encoding="ascii")
        return run_quiet("boot", rules, update=update)
    finally:
        os.close(fd)


def notice(now=time.time) -> str | None:
    """The line of what the last boot or apt update did, once; None when there is nothing new."""
    rec = read_record()
    if not rec or rec.get("shown"):
        return None
    if rec.get("state") == "running" and now() - int(rec.get("time") or 0) < BOOT_TIMEOUT + 30:
        return "%s: running now" % HOW[rec["how"]]
    line = describe(rec)
    rec["shown"] = True
    try:
        _write_json(laptop.home() / RECORD, rec)
    except OSError:
        pass
    return line


def interactive() -> bool:
    return sys.stdout.isatty()


def banner(argv: list[str], say=print) -> None:
    """The version line first and the notice of the last quiet update, on a terminal and always for update and
    doctor; never for the quiet runs themselves."""
    if argv[:1] == ["update"] and ("--boot" in argv or "--quiet" in argv):
        return
    if not (interactive() or argv[:1] in (["update"], ["doctor"])):
        return
    say(version_line())
    line = notice()
    if line:
        say(line)


# --------------------------------------------------------------------------- the boot block of the shell


def awb_path(given: str | None = None) -> str:
    path = given or shutil.which("awb") or str(Path(sys.argv[0]).resolve())
    if not SAFE_PATH_RE.fullmatch(path) or not os.access(path, os.X_OK):
        raise UpdateSetupError("the path of awb is not a plain absolute path of an executable; install awb into "
                               "~/.local/bin and run this again")
    return path


def boot_block(awb: str) -> str:
    """The lines of the shell (bash and zsh): once per boot, in the background, never blocking the prompt."""
    lines = (
        BOOT_BEGIN,
        "if [ -r /proc/sys/kernel/random/boot_id ] && [ -x %s ]; then" % awb,
        '  _awb_d="${AWB_LAPTOP_HOME:-$HOME/awb-laptop}"',
        '  if [ "$(cat /proc/sys/kernel/random/boot_id)" != "$(cat "$_awb_d/%s" 2>/dev/null)" ]; then' % BOOT_STAMP,
        '    [ -d "$_awb_d" ] || mkdir -m 700 -p "$_awb_d"',
        '    ( ( if [ -z "${HTTPS_PROXY:-}${https_proxy:-}" ] && [ -f "$HOME/.config/winrelay.sh" ]; then '
        '. "$HOME/.config/winrelay.sh"; fi',
        '        exec timeout %d %s clean update --boot ) </dev/null >>"$_awb_d/%s" 2>&1 & )' % (BOOT_TIMEOUT, awb, LOG),
        "  fi",
        "  unset _awb_d",
        "fi",
        BOOT_END,
    )
    return "\n".join(lines) + "\n"


def _replace_block(text: str, block: str) -> str:
    start = text.find(BOOT_BEGIN)
    end = text.find(BOOT_END, start)
    if start < 0 or end < 0:
        return text + ("\n" if text and not text.endswith("\n") else "") + block
    end += len(BOOT_END)
    if text[end:end + 1] == "\n":
        end += 1
    return text[:start] + block + text[end:]


def _write_atomic(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    part = path.with_name("." + path.name + ".part")
    part.write_text(text, encoding="utf-8")
    try:
        os.chmod(part, path.stat().st_mode & 0o777)
    except FileNotFoundError:
        pass
    os.replace(part, path)


def boot_setup(awb: str | None = None) -> list[str]:
    """`awb clean shell`, second part: the boot block in ~/.bashrc and, with oh-my-zsh, awb-update.zsh. A second
    run changes nothing; a block of an older awb path is replaced."""
    try:
        path = awb_path(awb)
    except UpdateSetupError as err:
        return ["boot update: not set up, %s" % err]
    block = boot_block(path)
    out = []
    bashrc = laptop.user_home() / ".bashrc"
    try:
        text = bashrc.read_text(encoding="utf-8")
    except FileNotFoundError:
        text = ""
    new = _replace_block(text, block)
    if new == text:
        out.append("boot update: %s starts awb clean update once per boot" % laptop._tilde(bashrc))
    else:
        _write_atomic(bashrc, new)
        out.append("boot update: added to %s (once per boot, in the background, log ~/awb-laptop/%s)"
                   % (laptop._tilde(bashrc), LOG))
    if laptop.oh_my_zsh().is_dir():
        target = laptop.zsh_file().parent / ZSH_UPDATE_FILE
        want = "# written by awb clean shell; oh-my-zsh reads it after ~/.zshrc\n" + block
        try:
            same = target.read_text(encoding="utf-8") == want
        except OSError:
            same = False
        if same:
            out.append("boot update: %s starts awb clean update once per boot" % laptop._tilde(target))
        else:
            _write_atomic(target, want)
            out.append("boot update: written to %s (open a new terminal)" % laptop._tilde(target))
    return out


# --------------------------------------------------------------------------- the apt hook


def _user() -> str:
    if os.geteuid() == 0:
        raise UpdateSetupError("run awb clean apt-hook as your own user, not as root: the hook runs the update as "
                               "the user who printed it")
    name = pwd.getpwuid(os.getuid()).pw_name
    if not SAFE_USER_RE.fullmatch(name):
        raise UpdateSetupError("the user name is not a plain login name")
    return name


def apt_content(awb: str, user: str) -> str:
    """The file /etc/apt/apt.conf.d/99awb-clean: after `apt update`, the update as his user (runuser, never root),
    the proxy of ~/.config/winrelay.sh, a timeout, every output to /dev/null (the record and update.log keep it)
    and `true` at the end so apt never fails on it."""
    inner = ("[ -f ~/.config/winrelay.sh ] && . ~/.config/winrelay.sh; exec timeout %d %s clean update --quiet"
             % (APT_TIMEOUT, awb))
    cmd = ("if [ -x %s ] && [ -x %s ]; then %s -u %s -- sh -c '%s' </dev/null >/dev/null 2>&1; fi; true"
           % (RUNUSER, awb, RUNUSER, user, inner))
    return ("// written by awb clean apt-hook: awb clean update as %s after apt update, never fails apt\n"
            'APT::Update::Post-Invoke { "%s"; };\n' % (user, cmd))


def apt_command(awb: str | None = None, user: str | None = None) -> tuple[str, str]:
    """The content and the one sudo command that writes it."""
    content = apt_content(awb_path(awb), user or _user())
    lines = " ".join(shlex.quote(line) for line in content.splitlines())
    return content, "printf '%%s\\n' %s | sudo tee %s >/dev/null" % (lines, APT_FILE)


def apt_check(awb: str | None = None, user: str | None = None) -> tuple[str, int]:
    content, cmd = apt_command(awb, user)
    try:
        have = APT_FILE.read_text(encoding="utf-8")
    except FileNotFoundError:
        return "apt hook: not installed; install it with:\n%s" % cmd, 1
    except OSError:
        return "apt hook: %s cannot be read" % APT_FILE, 1
    if have == content:
        return "apt hook: installed (%s), sudo apt update runs awb clean update as your user" % APT_FILE, 0
    return ("apt hook: %s is there but for another awb path or user; write it again with:\n%s" % (APT_FILE, cmd)), 1


def apt_hook_main(argv: list[str]) -> int:
    from awb.cli import SafeParser

    ap = SafeParser(prog="awb clean apt-hook", description="The one sudo command that makes sudo apt update also "
                    "run awb clean update as your user (/etc/apt/apt.conf.d/99awb-clean); it never fails apt. This "
                    "command itself writes nothing.")
    ap.add_argument("--check", action="store_true", help="say whether the hook is installed as printed")
    try:
        args = ap.parse_args(argv)
    except SystemExit as exc:
        return 0 if exc.code in (0, None) else 2
    try:
        if args.check:
            line, code = apt_check()
            print(line)
            return code
        _, cmd = apt_command()
    except UpdateSetupError as err:
        print("awb clean apt-hook: %s" % err, file=sys.stderr)
        return 2
    print("run this once in your own terminal (it asks for your sudo password):")
    print(cmd)
    print("then: awb clean apt-hook --check")
    return 0
