"""Grants of the egress filter: one host for the work user for a limited time, asked for by one project.

    awb egress allow HOST --project CODE --until DATE --why TEXT   the owner (desk or development): grant HOST
    awb egress allow --list                                         the owner: every grant, active or expired
    awb egress revoke HOST --project CODE                           the owner: end a grant before its date

A project that needs a host the shared list (seal/egress/ALLOW.md) does not name asks with `awb dev ask`; the desk or
development decides and grants it here, as the owner user, never as the work user, the build user, root or from a
project folder. A grant is one host name (no address, no network), one project code, a last day at most 30 days
ahead (the grant ends at the end of that day, UTC) and a reason. A host of ALLOW.md (allowed or watched) takes no
grant: a permanent addition or a change of a watched host stays a development commit of ALLOW.md.

The grants live in the owner's file `~/.config/awb/egress-grants.tsv` (0600, one tab separated line per grant:
host, project, last day, the day it was given, the reason). Every allow and revoke appends a line to
`~/.config/awb/egress-grants.log` and an entry to the ledger (kind tooling, the project code, the reason as its
outcome; no host name, which the ledger check refuses as a domain). The reason passes the ledger's name check first:
a hit refuses the grant and nothing is written.

Enforcement: `awb egress load` (root, the timer every 5 minutes and `sudo awb egress load`) reads the file of the
owner the host file names, only when it is a regular file of the owner's uid that no group or other may write, in a
folder no other may write. Each active grant adds its host to the allowed set (tcp 443) of the work user; an expired
grant, a malformed line, a host of ALLOW.md and a grant longer than 30 days are left out. The filter knows uids, not
projects: every working session of the work user reaches a granted host, the project code says who asked and why.
Connections the grant opened stay until they close; new ones are refused after the next load.
"""
from __future__ import annotations

import fcntl
import os
import pwd
import re
import stat
import subprocess
import time
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Callable

from awb import codes

MAX_DAYS = 30
MAX_GRANTS = 50
MAX_WHY = 200
GRANTS_FILE = "egress-grants.tsv"
LOG_FILE = "egress-grants.log"
_HOST_RE = re.compile(r"^(?=.{1,253}$)(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}$")
_DAY_RE = re.compile(r"^[0-9]{4}-[0-9]{2}-[0-9]{2}$")
_MAX_FILE = 256 * 1024


class GrantError(Exception):
    """Exit 2: nothing was written. The message carries no value of the caller."""


class GrantRefused(Exception):
    """Exit 1: the reason failed the name check; nothing was written."""


@dataclass(frozen=True)
class Grant:
    host: str
    project: str
    until: str          # the last day, YYYY-MM-DD
    given: str          # the day it was given, YYYY-MM-DD
    why: str

    def line(self) -> str:
        return "\t".join((self.host, self.project, self.until, self.given, self.why))

    def end(self) -> float:
        """The moment the grant ends: the end of its last day, UTC."""
        d = _day(self.until)
        return datetime(d.year, d.month, d.day, tzinfo=timezone.utc).timestamp() + 86400

    def active(self, now: float) -> bool:
        return now < self.end()


def _day(text: str) -> date:
    if not _DAY_RE.match(text or ""):
        raise ValueError("not a day")
    return date.fromisoformat(text)


def valid_host(host: str) -> bool:
    return bool(_HOST_RE.match(host or ""))


def parse(text: str) -> tuple[list[Grant], int]:
    """(the well formed grants, the count of lines left out). A line is well formed when it has five fields, a host
    name, a project code, two days with the last day at most MAX_DAYS after the day it was given."""
    out, bad = [], 0
    for line in text.splitlines():
        if not line.strip() or line.startswith("#"):
            continue
        parts = line.split("\t")
        try:
            if len(parts) != 5:
                raise ValueError("fields")
            g = Grant(*parts)
            given, until = _day(g.given), _day(g.until)
            if not valid_host(g.host) or not codes.is_project_code(g.project) or not 0 <= (until - given).days <= MAX_DAYS:
                raise ValueError("shape")
        except ValueError:
            bad += 1
            continue
        out.append(g)
    return out[:MAX_GRANTS], bad + max(len(out) - MAX_GRANTS, 0)


# --------------------------------------------------------------------------- the root side (awb egress load)


def _open_dir(path: Path) -> int:
    path = Path(os.path.abspath(path))
    fd = os.open(path.anchor, os.O_RDONLY | os.O_DIRECTORY)
    try:
        for part in path.parts[1:]:
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            os.close(fd)
            fd = child
        return fd
    except BaseException:
        os.close(fd)
        raise


def read_trusted(path: Path, uid: int) -> str | None:
    """The text of the grants file when it is safe to trust as root: a regular file with one link, of `uid`, not
    writable by group or other, in a folder of `uid` or root that no other may write. None for no file; GrantError
    for a file that is there but not to be trusted."""
    try:
        parent = _open_dir(path.parent)
    except FileNotFoundError:
        return None
    except OSError:
        raise GrantError("the grants folder cannot be opened safely") from None
    try:
        dst = os.fstat(parent)
        if dst.st_uid not in (uid, 0) or dst.st_mode & 0o002:
            raise GrantError("the grants folder may be written by another user")
        try:
            fd = os.open(path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
        except FileNotFoundError:
            return None
        except OSError:
            raise GrantError("the grants file cannot be opened safely") from None
        with os.fdopen(fd, "rb") as fh:
            st = os.fstat(fh.fileno())
            if not stat.S_ISREG(st.st_mode) or st.st_nlink != 1 or st.st_uid != uid or st.st_mode & 0o022:
                raise GrantError("the grants file is not a private file of the owner")
            raw = fh.read(_MAX_FILE + 1)
        if len(raw) > _MAX_FILE:
            raise GrantError("the grants file is too large")
        try:
            return raw.decode("utf-8")
        except UnicodeDecodeError:
            raise GrantError("the grants file is not UTF-8") from None
    finally:
        os.close(parent)


def owner_file(owner: str | None) -> tuple[Path, int] | None:
    """(the grants file, the uid) of the owner the host file names; None when the owner has no account here."""
    if not owner:
        return None
    try:
        pw = pwd.getpwnam(owner)
    except KeyError:
        return None
    return Path(pw.pw_dir) / ".config" / "awb" / GRANTS_FILE, pw.pw_uid


def active_hosts(text: str | None, now: float, listed: set[str]) -> tuple[list[str], dict[str, int]]:
    """(the hosts of the active grants, counts {active, expired, left_out}); a host of ALLOW.md is left out."""
    if text is None:
        return [], {"active": 0, "expired": 0, "left_out": 0}
    grants, bad = parse(text)
    hosts, expired = [], 0
    for g in grants:
        if not g.active(now):
            expired += 1
        elif g.host in listed:
            bad += 1
        elif g.host not in hosts:
            hosts.append(g.host)
    return hosts, {"active": len(hosts), "expired": expired, "left_out": bad}


# --------------------------------------------------------------------------- the owner side


@dataclass
class Owner:
    """What the owner commands touch, replaced by the tests."""
    folder: Path | None = None          # None: ~/.config/awb of the current user
    allow_md: Path | None = None        # None: seal/egress/ALLOW.md of this release
    conf_text: Callable | None = None   # None: the root host file, None when unreadable
    user: Callable | None = None        # None: the name of the current uid
    geteuid: Callable = os.geteuid
    cwd: Callable = Path.cwd
    ledger_add: Callable | None = None  # None: awb.ledger.add, handed to the work user when the owner cannot write
    run: Callable = subprocess.run      # the hand-over to the work user (sudo -n -u WORK awb ledger add)
    now: Callable = time.time

    def dir(self) -> Path:
        return Path(self.folder) if self.folder is not None else \
            Path(pwd.getpwuid(os.getuid()).pw_dir) / ".config" / "awb"

    def day(self) -> date:
        return datetime.fromtimestamp(self.now(), timezone.utc).date()


def _conf_text() -> str | None:
    from awb import config
    try:
        return Path(config.HOST_CONF).read_text(encoding="utf-8")
    except OSError:
        return None


def _conf_value(text: str, key: str) -> str | None:
    m = re.search(r"(?m)^[ \t]*%s[ \t]*=[ \t]*(\S+)[ \t]*$" % re.escape(key), text)
    return m.group(1) if m else None


def require_owner(o: Owner) -> None:
    """The owner user alone, outside a project folder: never root, the work user or the build user."""
    from awb import config

    if o.geteuid() == 0:
        raise GrantError("run it as the owner user, without sudo")
    me = o.user() if o.user else pwd.getpwuid(os.getuid()).pw_name
    text = (o.conf_text or _conf_text)()
    if text is not None:
        if me in (_conf_value(text, "work_user"), _conf_value(text, "build_user")):
            raise GrantError("the work user and the build user never grant a host: a project asks with awb dev ask")
        owner = _conf_value(text, "owner")
        if owner and me != owner:
            raise GrantError("only the owner of the host file grants a host")
    if config.scope_root(Path(os.path.abspath(o.cwd())), strict=True) is not None:
        raise GrantError("not from a project folder: a project asks with awb dev ask, the desk or development grants")


def _listed(o: Owner) -> set[str]:
    from awb import egress

    path = Path(o.allow_md) if o.allow_md is not None else egress.SOURCE / egress.ALLOW_FILE
    try:
        return {e.dest for e in egress._rows(path.read_text(encoding="utf-8"))}
    except (OSError, egress.EgressError):
        return set()


def _lock(folder: Path):
    folder.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd = os.open(folder / ".egress-grants.lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    fcntl.flock(fd, fcntl.LOCK_EX)
    return fd


def _read_own(folder: Path) -> str:
    return read_trusted(folder / GRANTS_FILE, os.getuid()) or ""


def _write_own(folder: Path, grants: list[Grant]) -> None:
    path = folder / GRANTS_FILE
    tmp = folder / (".%s.%d" % (GRANTS_FILE, os.getpid()))
    text = "".join(g.line() + "\n" for g in grants)
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(text)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, path)


def _log(folder: Path, action: str, g: Grant, now: float) -> None:
    stamp = datetime.fromtimestamp(now, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    fd = os.open(folder / LOG_FILE, os.O_WRONLY | os.O_CREAT | os.O_APPEND | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "a", encoding="utf-8") as fh:
        fh.write("\t".join((stamp, action, g.host, g.project, g.until, g.why)) + "\n")


def _ledger_as_work(o: Owner, done: str, project: str, why: str) -> None:
    """The shared ledger belongs to the work user and the owner may only read it: the entry goes through
    `awb ledger add` as the work user, which runs the same name check."""
    from awb import config

    work = config.work_user()
    if not work:
        raise GrantError("the ledger cannot be written by this user and the host file names no work user, "
                         "nothing was written")
    awb_bin = "/usr/local/bin/awb" if os.path.exists("/usr/local/bin/awb") else "awb"
    cmd = ["sudo", "-n", "-u", work, "-H", awb_bin, "ledger", "add", "--kind", "tooling", "--done", done,
           "--project", project, "--outcome", why]
    try:
        done_run = o.run(cmd, cwd="/", stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL, timeout=120)
    except (OSError, subprocess.SubprocessError):
        raise GrantError("the ledger entry could not be handed to the work user, nothing was written") from None
    if done_run.returncode == 1:
        raise GrantRefused("the reason failed the name check: write it with codes, nothing was written")
    if done_run.returncode != 0:
        raise GrantError("the work user could not take the ledger entry (sudo -n -u %s awb ledger add), nothing "
                         "was written" % work)


def _ledger(o: Owner, done: str, project: str, why: str) -> None:
    from awb import ledger

    try:
        if o.ledger_add is not None:
            o.ledger_add("tooling", done, project=project, outcome=why, tags=())
        else:
            try:
                ledger.add("tooling", done, project=project, outcome=why, tags=())
            except PermissionError:
                _ledger_as_work(o, done, project, why)
    except ledger.Refused:
        raise GrantRefused("the reason failed the name check: write it with codes, nothing was written") from None
    except ledger.LedgerError:
        raise GrantError("the ledger could not check or take the entry, nothing was written") from None
    except OSError:
        raise GrantError("the ledger could not be written, nothing was written") from None


def _clean_why(why: str) -> str:
    why = " ".join((why or "").split())
    if not why:
        raise GrantError("--why needs a reason")
    if len(why) > MAX_WHY:
        raise GrantError("--why takes at most %d characters" % MAX_WHY)
    return why


def allow(o: Owner, host: str, project: str, until: str, why: str) -> str:
    require_owner(o)
    host = (host or "").strip().lower().rstrip(".")
    if not valid_host(host):
        raise GrantError("HOST is one host name, never an address, a network or a pattern")
    if not codes.is_project_code(project or ""):
        raise GrantError("--project takes a project code (tcp-xxxx)")
    try:
        last = _day(until)
    except ValueError:
        raise GrantError("--until takes a day, YYYY-MM-DD") from None
    today = o.day()
    if last < today:
        raise GrantError("--until lies in the past")
    if (last - today).days > MAX_DAYS:
        raise GrantError("--until is at most %d days ahead: a lasting host is a development commit of "
                         "seal/egress/ALLOW.md" % MAX_DAYS)
    why = _clean_why(why)
    if host in _listed(o):
        raise GrantError("the host is a row of seal/egress/ALLOW.md already: a change there is a development commit")
    folder = o.dir()
    lock = _lock(folder)
    try:
        grants, _ = parse(_read_own(folder))
        now = o.now()
        kept = [g for g in grants if g.active(now) and not (g.host == host and g.project == project)]
        if len(kept) >= MAX_GRANTS:
            raise GrantError("%d grants are active already: revoke one first" % MAX_GRANTS)
        g = Grant(host, project, last.isoformat(), today.isoformat(), why)
        _ledger(o, "egress grant: one host for the work user until %s (awb egress allow --list)" % g.until,
                project, why)
        _write_own(folder, kept + [g])
        _log(folder, "allow", g, now)
    finally:
        os.close(lock)
    return ("egress: granted %s to the work user for %s until the end of %s UTC; the filter knows no project, so "
            "every session of the work user reaches it\negress: the timer loads it within 5 minutes; at once: "
            "sudo awb egress load" % (host, project, g.until))


def revoke(o: Owner, host: str, project: str) -> str:
    require_owner(o)
    host = (host or "").strip().lower().rstrip(".")
    if not valid_host(host) or not codes.is_project_code(project or ""):
        raise GrantError("revoke takes one host name and --project with a project code")
    folder = o.dir()
    lock = _lock(folder)
    try:
        grants, _ = parse(_read_own(folder))
        gone = [g for g in grants if g.host == host and g.project == project]
        if not gone:
            raise GrantError("no grant of this host for this project")
        _ledger(o, "egress grant revoked: one host for the work user (awb egress allow --list)", project,
                gone[0].why)
        _write_own(folder, [g for g in grants if g not in gone])
        for g in gone:
            _log(folder, "revoke", g, o.now())
    finally:
        os.close(lock)
    return ("egress: revoked %s for %s; the timer drops it within 5 minutes; at once: sudo awb egress load"
            % (host, project))


def listing(o: Owner) -> list[str]:
    require_owner(o)
    grants, bad = parse(_read_own(o.dir()))
    now = o.now()
    lines = ["%s\t%s\tuntil %s\t%s\t%s" % (g.host, g.project, g.until, "active" if g.active(now) else "expired",
                                          g.why) for g in sorted(grants, key=lambda g: (g.until, g.host))]
    if bad:
        lines.append("egress: %d line(s) of the grants file are malformed and are never loaded" % bad)
    return lines or ["egress: no grant"]
