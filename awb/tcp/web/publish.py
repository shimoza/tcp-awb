"""Publish the console page in one step (owner side).

    sudo awb web publish --from-queue ID [--dry-run]
    sudo awb web publish FILE [--dry-run]
    sudo awb web publish --rollback [NAME]
    awb web status
    sudo awb web user add|reset|remove LOGIN, sudo awb web user list    (awb/tcp/web/users.py)
    sudo awb web host HOST

`publish` takes the page a run of the UI queue produced: the applied source for a run in state completed, the
run's workspace candidate for a candidate-only run in state ready, both only when the run's validation.txt starts
with PASS; or FILE. It checks the page (registered names, the gate's secret, token and private-key detectors, one
<title>, no external script), keeps the current page as index.html.before-<ID or date-time> and installs the new
one with mode 644, owned by root. Under sudo every read and every check runs in a child that dropped root to
SUDO_USER (`awb web check --emit`); root only keeps the backup and installs the bytes the child handed back. A
refusal names the class of the finding and nothing else.

`--rollback` installs a backup back (the newest without NAME); the page it replaces is kept as a backup too.
`status` prints the size and time of the page, the last backup and the answers of the gateway and of /health, and
from the sign-in log the attempts of the last day by result and every login that waits (the log belongs to the
gateway: under sudo, or AWB_WEB_SIGNIN_LOG for another file).

`host` writes `owner_host = HOST` into /etc/awb/paths.conf (D-HOST): the host name of the owner level, one level
under the zone of the site and not the site itself. The next deploy renders it; until it is there the web side's
installer stops. AWB_CONF points it at another file (the tests).

AWB_WEB_ROOT (or --root) points the commands at another folder than /srv/awb-web; such a folder needs no sudo.
AWB_UI_QUEUE and AWB_UI_SOURCE point at another queue state folder and applied source (the tests use both).
"""
from __future__ import annotations

import argparse
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import time
import urllib.error
import urllib.request
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from awb import check, config, gate, register

SRV = Path("/srv/awb-web")
INDEX = "index.html"
BACKUP_PREFIX = INDEX + ".before-"
QUEUE_DIR = Path(".local/state/awb-ui-handoff")                               # under the owner's home
SOURCE = Path("Documents/Codex/2026-10-01/new-chat/outputs/architect-workbench.html")   # under the owner's home
CANDIDATE = "workspace/architect-workbench.html"                                # in the run folder
GATEWAY_UNIT = Path("/etc/systemd/system/awb-web.service")
STATUS_SOCKET = "/run/awb-web-status.sock"
HEALTH_PORT = 8180
HOST_RE = re.compile(r"^[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?(\.[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?)+$")
SIGNIN_LOG = Path("/var/lib/awb-web/signin.log")
MAX_PAGE = 4 * 1024 * 1024
ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
CLASSES = ("name", "secret", "token", "private-key")
"""The finding classes that refuse a page. The other classes of the gate (home paths, identifiers, the blocklist)
belong to commits, not to a page that shows API answers."""


class Refused(Exception):
    """The page or the run is refused; the text names a class or a state, never a value."""


@dataclass
class Host:
    geteuid: Callable = os.geteuid
    runner: Callable = subprocess.run
    environ: dict = field(default_factory=lambda: dict(os.environ))
    chown: Callable = os.chown
    bin: str = "/usr/local/bin/awb"


# --------------------------------------------------------------------------- where things are


def web_root(given: str | None, environ: dict) -> Path:
    return Path(given or environ.get("AWB_WEB_ROOT") or SRV)


def queue_dir() -> Path:
    return Path(os.environ.get("AWB_UI_QUEUE") or Path.home() / QUEUE_DIR)


def applied_source() -> Path:
    return Path(os.environ.get("AWB_UI_SOURCE") or Path.home() / SOURCE)


def _read(path: Path) -> bytes:
    if not path.is_file():
        raise Refused("the page file is missing")
    if path.stat().st_size > MAX_PAGE:
        raise Refused("the page is larger than %d bytes" % MAX_PAGE)
    return path.read_bytes()


def run_state(job_id: str) -> tuple[str, bool]:
    """(state, applies) of a queue run, read from the queue database without writing to it."""
    db = queue_dir() / "queue.sqlite3"
    if not db.is_file():
        raise Refused("the queue has no database")
    con = sqlite3.connect("file:%s?mode=ro" % db, uri=True)
    try:
        row = con.execute("SELECT state, apply_changes FROM jobs WHERE id=?", (job_id,)).fetchone()
    except sqlite3.Error:
        raise Refused("the queue database cannot be read") from None
    finally:
        con.close()
    if row is None:
        raise Refused("the queue has no run with this id")
    return str(row[0]), bool(row[1])


def queue_page(job_id: str) -> bytes:
    """The page of a run: the applied source of a completed run, the candidate of a candidate-only run in state
    ready. The validation of the run must start with PASS; a completed run's source must still be its candidate."""
    if not ID_RE.match(job_id):
        raise Refused("the run id has a form the queue never gives")
    state, applies = run_state(job_id)
    run = queue_dir() / "runs" / job_id
    if applies and state != "completed":
        raise Refused("the run is in state %s, not completed" % state)
    if not applies and state != "ready":
        raise Refused("the candidate-only run is in state %s, not ready" % state)
    try:
        validation = (run / "validation.txt").read_text(encoding="utf-8", errors="replace")
    except OSError:
        raise Refused("the run has no validation.txt") from None
    if not validation.startswith("PASS"):
        raise Refused("the validation of the run is not PASS")
    candidate = _read(run / CANDIDATE)
    if not applies:
        return candidate
    source = _read(applied_source())
    if source != candidate:
        raise Refused("the applied source changed after the run: publish the newer run or the file")
    return source


# --------------------------------------------------------------------------- the content checks


_TITLE_RE = re.compile(r"<title[\s>]", re.I)
_SCRIPT_RE = re.compile(r"<script\b([^>]*)>", re.I)
_SRC_RE = re.compile(r"""\bsrc\s*=\s*["']?\s*([^"'\s>]*)""", re.I)


def html_problems(text: str) -> list[str]:
    out = []
    if len(_TITLE_RE.findall(text)) != 1:
        out.append("html: not exactly one <title>")
    for m in _SCRIPT_RE.finditer(text):
        src = _SRC_RE.search(m.group(1))
        if src and re.match(r"^([a-z][a-z0-9+.-]*:|//)", src.group(1).strip(), re.I):
            out.append("html: an external script")
            break
    return out


def content_problems(data: bytes, matcher) -> list[str]:
    """The classes the page is refused for, each with its count of lines; empty when the page passes."""
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        return ["html: not UTF-8"]
    counts = Counter(f.cls for f in gate._scan_text(INDEX, text, matcher) if f.cls in CLASSES)
    out = ["%s: %d line%s" % (cls, counts[cls], "" if counts[cls] == 1 else "s") for cls in CLASSES if counts[cls]]
    return out + html_problems(text)


def name_matcher():
    """The name check of this user: the register it can read or the vault's check socket."""
    path = config.paths().register
    kind, _ = check.register_source(path)
    if kind == check.MISSING:
        raise Refused("no register: names cannot be checked")
    matcher = gate._load_matcher(path, check.RATE_WAIT)
    if isinstance(matcher, check.RemoteCheck):
        matcher.ping()
    return matcher


def checked_page(source: str | None, file: Path | None, matcher=None) -> bytes:
    """The bytes of the page after every check; Refused names the class of what was found."""
    data = queue_page(source) if source else _read(Path(file))
    try:
        problems = content_problems(data, matcher if matcher is not None else name_matcher())
    except check.CheckUnavailable:
        raise Refused("the name check cannot run: is the vault unlocked?") from None
    if problems:
        raise Refused("the page is refused: " + "; ".join(problems))
    return data


# --------------------------------------------------------------------------- the install (root's part)


def _stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")


def backup_name(root: Path, label: str) -> str:
    name = BACKUP_PREFIX + label
    n = 2
    while (root / name).exists():
        name = "%s%s-%d" % (BACKUP_PREFIX, label, n)
        n += 1
    return name


def backups(root: Path) -> list[Path]:
    return sorted((p for p in root.glob(BACKUP_PREFIX + "*") if p.is_file()),
                  key=lambda p: (p.stat().st_mtime, p.name))


def install(root: Path, data: bytes, label: str, host: Host) -> str | None:
    """Keep the current page as a backup and put `data` in its place atomically. Returns the backup name (None
    when there was no page). Owned by root when root runs it."""
    as_root = host.geteuid() == 0
    index = root / INDEX
    tmp = root / (".%s.new-%d" % (INDEX, os.getpid()))
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
            fh.flush()
            os.fchmod(fh.fileno(), 0o644)
            os.fsync(fh.fileno())
        if as_root:
            host.chown(tmp, 0, 0)
        name = None
        if index.exists():
            name = backup_name(root, label)
            shutil.copy2(index, root / name)
            if as_root:
                host.chown(root / name, 0, 0)
        os.replace(tmp, index)
    finally:
        if tmp.exists():
            tmp.unlink()
    return name


# --------------------------------------------------------------------------- the commands


def _say(text: str) -> None:
    print(text, flush=True)


def _owner_pw(host: Host):
    """The passwd entry of SUDO_USER; Refused for root, the work user or none."""
    import pwd

    name = host.environ.get("SUDO_USER") or ""
    if not name:
        raise Refused("run it with sudo as the owner (SUDO_USER is empty)")
    if name == "root":
        raise Refused("the owner must be a user, not root")
    if name == config.work_user():
        raise Refused("the work user cannot publish")
    try:
        return pwd.getpwnam(name)
    except KeyError:
        raise Refused("the owner is not a user of this host") from None


def _page_as_owner(a, host: Host) -> bytes:
    """Root's way to the page: a child dropped to SUDO_USER reads and checks it and hands back the bytes."""
    from awb import deploy

    pw = _owner_pw(host)
    cmd = [host.bin, "web", "check", "--emit"]
    cmd += ["--from-queue", a.from_queue] if a.from_queue else [os.path.abspath(a.file)]
    res = deploy.as_user(host.runner, cmd, pw, host.environ, pw.pw_dir, capture_output=True)
    if res.returncode != 0:
        msg = (res.stderr or b"").decode("utf-8", "replace").strip().splitlines()
        raise Refused(msg[-1].removeprefix("awb web: ") if msg else "the check as the owner failed")
    return res.stdout


def _publish(a, host: Host) -> int:
    root = web_root(a.root, host.environ)
    if not root.is_dir():
        raise Refused("the web folder is missing")
    if a.rollback is not None:
        return _rollback(root, a.rollback, a.dry_run, host)
    if bool(a.from_queue) == bool(a.file):
        raise Refused("give either --from-queue ID or FILE")
    is_root = host.geteuid() == 0
    if not is_root and not a.dry_run and root == SRV:
        raise Refused("run it with sudo: sudo awb web publish (--dry-run works without)")
    data = _page_as_owner(a, host) if is_root else checked_page(a.from_queue, a.file and Path(a.file))
    label = a.from_queue or _stamp()
    if a.dry_run:
        _say("dry run: the page passes, %d bytes; the current page would be kept as %s"
             % (len(data), backup_name(root, label)))
        return 0
    name = install(root, data, label, host)
    _say("published: %d bytes, backup %s" % (len(data), name or "none (there was no page)"))
    return 0


def _rollback(root: Path, name: str, dry_run: bool, host: Host) -> int:
    if host.geteuid() != 0 and not dry_run and root == SRV:
        raise Refused("run it with sudo: sudo awb web publish --rollback")
    if name:
        if "/" in name or not name.startswith(BACKUP_PREFIX) or not (root / name).is_file():
            raise Refused("no such backup in the web folder")
        chosen = root / name
    else:
        found = backups(root)
        if not found:
            raise Refused("the web folder has no backup")
        chosen = found[-1]
    data = chosen.read_bytes()
    if dry_run:
        _say("dry run: %s (%d bytes) would be installed" % (chosen.name, len(data)))
        return 0
    kept = install(root, data, "rollback-" + _stamp(), host)
    _say("rolled back to %s: %d bytes, the replaced page kept as %s" % (chosen.name, len(data), kept or "none"))
    return 0


def _check(a) -> int:
    """The owner's side of a publish under sudo; also usable alone. --emit writes the page to standard output."""
    if bool(a.from_queue) == bool(a.file):
        raise Refused("give either --from-queue ID or FILE")
    data = checked_page(a.from_queue, a.file and Path(a.file))
    if a.emit:
        sys.stdout.buffer.write(data)
        sys.stdout.flush()
    else:
        _say("the page passes: %d bytes" % len(data))
    return 0


def _domain(unit: Path) -> str | None:
    try:
        text = unit.read_text(encoding="utf-8")
    except OSError:
        return None
    m = re.search(r"^ExecStart=.*--domain\s+([A-Za-z0-9.-]+)", text, re.M)
    return m.group(1) if m else None


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kw):
        return None


def _ask(url: str, host_header: str | None = None) -> str:
    req = urllib.request.Request(url, headers={"Host": host_header} if host_header else {})
    try:
        with urllib.request.build_opener(_NoRedirect).open(req, timeout=3) as resp:
            body = resp.read(200).decode("utf-8", "replace").strip()
            return "%d %s" % (resp.status, body.splitlines()[0] if body else "")
    except urllib.error.HTTPError as err:
        return "%d (%s)" % (err.code, "sign-in asked" if err.code in (302, 303) else "answers")
    except (urllib.error.URLError, OSError):
        return "no answer"


def _ask_socket(path: str) -> str:
    """GET /health on the gateway's status socket: the status line and the first line of the body."""
    import socket

    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.settimeout(3)
    try:
        s.connect(path)
        s.sendall(b"GET /health HTTP/1.0\r\nHost: localhost\r\n\r\n")
        data = b""
        while len(data) < 400:
            chunk = s.recv(400)
            if not chunk:
                break
            data += chunk
    except OSError:
        return "no answer"
    finally:
        s.close()
    head, _, body = data.decode("utf-8", "replace").partition("\r\n\r\n")
    m = re.match(r"HTTP/1\.[01] (\d{3})", head)
    return "%s %s" % (m.group(1), body.strip().splitlines()[0] if body.strip() else "") if m else "no answer"


def _owner_host(a, host: Host) -> int:
    name = a.name.strip().lower()
    if not HOST_RE.fullmatch(name):
        raise Refused("a host name in lower case without scheme, such as owner.example.com")
    domain = _domain(GATEWAY_UNIT)
    if domain and name == domain.lower():
        raise Refused("the owner level needs a host name of its own, not the site's")
    path = Path(host.environ.get("AWB_CONF") or config.HOST_CONF)
    if path == Path(config.HOST_CONF) and host.geteuid() != 0:
        raise Refused("run it with sudo: sudo awb web host HOST")
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        raise Refused("%s is missing: run seal/setup.sh first" % path) from None
    lines = [line for line in text.splitlines() if not re.match(r"\s*owner_host\s*=", line)]
    lines.append("owner_host = %s" % name)
    tmp = path.with_name(".%s.new-%d" % (path.name, os.getpid()))
    tmp.write_text("\n".join(lines) + "\n", encoding="utf-8")
    os.chmod(tmp, 0o644)
    os.replace(tmp, path)
    _say("owner_host = %s in %s; the next sudo awb deploy renders it. In Cloudflare: the DNS name %s and one "
         "ingress line to the front socket" % (name, path, name))
    return 0


def _status(a, host: Host) -> int:
    root = web_root(a.root, host.environ)
    index = root / INDEX
    stamp = lambda p: datetime.fromtimestamp(p.stat().st_mtime, timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    if index.is_file():
        _say("page: %d bytes, %s" % (index.stat().st_size, stamp(index)))
    else:
        _say("page: missing")
    found = backups(root) if root.is_dir() else []
    _say("last backup: %s" % ("%s, %s" % (found[-1].name, stamp(found[-1])) if found else "none"))
    domain = _domain(GATEWAY_UNIT)
    gw = _ask_socket(host.environ.get("AWB_WEB_STATUS_SOCKET") or STATUS_SOCKET) if domain else "not installed"
    _say("gateway: %s" % gw)
    _say("health: %s" % _ask("http://127.0.0.1:%d/health" % HEALTH_PORT))
    for line in signin_lines(Path(host.environ.get("AWB_WEB_SIGNIN_LOG") or SIGNIN_LOG)):
        _say(line)
    return 0


def signin_lines(log: Path, now: float | None = None) -> list[str]:
    """The sign-ins of the last day by result and the waits that hold now, replayed from the gateway's log."""
    from awb.tcp.web import gateway

    now = time.time() if now is None else now
    if not os.access(log, os.R_OK):
        try:
            missing = not log.exists() and os.access(log.parent, os.R_OK)
        except PermissionError:
            missing = False
        return ["sign-ins: %s" % ("no log yet" if missing else "run sudo awb web status to read the log")]
    day = [e for e in gateway.read_log(log, now - 24 * 3600) if e[0] <= now]
    counts = Counter(result for _, _, result in day)
    out = ["sign-ins, last 24 h: %d (%s)" % (len(day), ", ".join("%s %d" % kv for kv in sorted(counts.items()))
                                            if counts else "none")]
    limiter = gateway.replay(e for e in gateway.read_log(log, now - 2 * gateway.MAX_WAIT) if e[0] <= now)
    closed = limiter.site_closed_until(now)
    if closed:
        out.append("site closed: %d failures within the hour, open again at %s"
                   % (len(limiter.site), gateway.log_time(closed)))
    waits = limiter.cooldowns(now)
    out.append("logins in cooldown: %s" % (", ".join("%s until %s" % (k, gateway.log_time(t))
                                                      for k, t in sorted(waits.items())) or "none"))
    return out


def _parser():
    from awb.cli import SafeParser

    ap = SafeParser(prog="awb web", description="Publish the console page (owner side).")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("publish", help="check a queue run or a file and install it with a backup (sudo)")
    p.add_argument("file", nargs="?", metavar="FILE")
    p.add_argument("--from-queue", metavar="ID")
    p.add_argument("--dry-run", action="store_true", help="check only, install nothing")
    p.add_argument("--rollback", nargs="?", const="", default=None, metavar="NAME",
                   help="install a backup back (the newest without NAME)")
    p.add_argument("--root", help=argparse.SUPPRESS)
    c = sub.add_parser("check", help="check a queue run or a file as this user")
    c.add_argument("file", nargs="?", metavar="FILE")
    c.add_argument("--from-queue", metavar="ID")
    c.add_argument("--emit", action="store_true", help="write the checked page to standard output")
    s = sub.add_parser("status", help="the page, the last backup, the gateway and /health")
    s.add_argument("--root", help=argparse.SUPPRESS)
    h = sub.add_parser("host", help="set the host name of the owner level in /etc/awb/paths.conf (sudo)")
    h.add_argument("name", metavar="HOST")
    u = sub.add_parser("user", help="the logins of the console: add, reset (a new password), remove, list (sudo)")
    u.add_argument("action", choices=("add", "reset", "remove", "list"))
    u.add_argument("login", nargs="?", metavar="LOGIN")
    u.add_argument("--level", choices=("reader", "owner"), default=None,
                   help="with add: reader (the default) or owner (also a TOTP code for the owner host)")
    return ap


def main(argv: list[str] | None = None, host: Host | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    host = host or Host()
    if config.is_work_user():
        print("awb web: the work user cannot publish", file=sys.stderr)
        return 2
    try:
        a = _parser().parse_args(argv)
    except SystemExit as exc:
        return 0 if exc.code in (0, None) else 2
    try:
        if a.cmd == "publish":
            return _publish(a, host)
        if a.cmd == "check":
            return _check(a)
        if a.cmd == "host":
            return _owner_host(a, host)
        if a.cmd == "user":
            from awb.tcp.web import users

            if (a.action == "list") != (a.login is None):
                raise Refused("give LOGIN with add, reset and remove, none with list")
            try:
                return users.main(a, host.environ, host.geteuid, _say)
            except users.Refused as err:
                raise Refused(str(err)) from None
        return _status(a, host)
    except Refused as err:
        print("awb web: %s" % err, file=sys.stderr)
        return 1
    except register.RegisterError:
        print("awb web: the register cannot be read", file=sys.stderr)
        return 2
    except OSError as err:
        print("awb web: %s (operating system error)" % type(err).__name__, file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
