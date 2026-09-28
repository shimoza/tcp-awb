"""`awb seal check`: the checks of seal/verify.sh that need no root, run for the current user.

Run as the work user it proves the seal from the inside: it cannot read the register (plain or encrypted), list
the vault, the owner's password store, .claude, .ssh or home, enter the owner's home (where the owner's folders
are) or run `sudo -n true`; it is in none of the groups of the owner or of the host admins; the check socket
answers ping; it can write tcp-shared and the outbox. One line per check, PASS or FAIL. Exit 0 when every check
passes, 1 on any FAIL, 2 on a usage error.

A "cannot" check passes only when the operating system refuses with a permission error. A path that is simply
absent proves nothing and fails the check: a wrong path must never read as a sealed one.

The owner's home comes from the key `owner` of /etc/awb/paths.conf (written by seal/setup.sh) through the password
database, else it is the folder that holds the vault. Lines name what was checked, never a path or a user name.
"""
from __future__ import annotations

import grp
import json
import os
import pwd
import socket
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path

from awb import config

FORBIDDEN_GROUPS = ("ubuntu", "docker", "adm", "sudo", "lxd")
PING_TIMEOUT = 5.0
SUDO_TIMEOUT = 10


@dataclass(frozen=True)
class Result:
    ok: bool
    what: str
    note: str = ""


# --------------------------------------------------------------------------- probes (tests replace some of them)


def _denied(action) -> tuple[bool, str]:
    """(True, "") when `action` raises PermissionError, else (False, why)."""
    try:
        action()
    except PermissionError:
        return True, ""
    except FileNotFoundError:
        return False, "absent here, which proves nothing"
    except OSError as exc:
        return False, type(exc).__name__
    return False, "allowed"


def _read_one_byte(path: Path) -> None:
    with open(path, "rb") as fh:
        fh.read(1)


def _enter(folder: Path) -> None:
    """Look up a name inside `folder`: refused without the search permission of the folder."""
    os.stat(folder / ".awb-seal-probe")


def _sudo_allowed() -> bool:
    """True when `sudo -n true` succeeds for the current user. Never prompts."""
    try:
        res = subprocess.run(["sudo", "-n", "true"], stdin=subprocess.DEVNULL, capture_output=True,
                             timeout=SUDO_TIMEOUT)
    except (OSError, subprocess.SubprocessError):
        return False
    return res.returncode == 0


def _group_names() -> set[str]:
    """Names of the groups of the current process, the primary group included."""
    names = set()
    for gid in set(os.getgroups()) | {os.getgid()}:
        try:
            names.add(grp.getgrgid(gid).gr_name)
        except KeyError:
            continue
    return names


def _ping(sock: Path) -> bool:
    """True when the vault daemon answers {"op":"ping"} with ok true on the check socket."""
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
            s.settimeout(PING_TIMEOUT)
            s.connect(str(sock))
            s.sendall(b'{"op":"ping"}\n')
            buf = b""
            while not buf.endswith(b"\n") and len(buf) < 65536:
                part = s.recv(4096)
                if not part:
                    break
                buf += part
        answer = json.loads(buf.decode("utf-8"))
    except (OSError, ValueError):
        return False
    return isinstance(answer, dict) and answer.get("ok") is True


def _can_write(folder: Path) -> tuple[bool, str]:
    try:
        fd, name = tempfile.mkstemp(prefix=".awb-seal-", dir=folder)
    except OSError as exc:
        return False, type(exc).__name__
    os.close(fd)
    os.unlink(name)
    return True, ""


# --------------------------------------------------------------------------- the checks


def owner_home(p: config.Paths, conf: dict[str, str] | None = None) -> Path:
    """The owner's home: the user `owner` of the host file, else the folder that holds the vault."""
    conf = config.host_conf() if conf is None else conf
    name = conf.get("owner", "")
    if name:
        try:
            return Path(pwd.getpwnam(name).pw_dir)
        except KeyError:
            pass
    return p.vault.parent


def forbidden_groups(home: Path) -> tuple[str, ...]:
    """The fixed list plus the primary group of the owner, when the owner's home belongs to a known group."""
    extra = []
    try:
        extra.append(grp.getgrgid(os.stat(home).st_gid).gr_name)
    except (OSError, KeyError):
        pass
    return tuple(dict.fromkeys(FORBIDDEN_GROUPS + tuple(extra)))


def run_checks(p: config.Paths | None = None, home: Path | None = None) -> list[Result]:
    """Every check that needs no root, for the current user."""
    p = p or config.paths()
    home = home or owner_home(p)
    out: list[Result] = []

    def denied(what: str, action) -> None:
        ok, note = _denied(action)
        out.append(Result(ok, what, note))

    reads = [_denied(lambda f=f: _read_one_byte(f)) for f in (p.register, p.register_encrypted)]
    out.append(Result(all(ok for ok, _ in reads), "cannot read the register (plain or encrypted)",
                      "; ".join(n for _, n in reads if n)))
    denied("cannot list the vault", lambda: os.listdir(p.vault))
    denied("cannot list the owner's password store", lambda: os.listdir(home / ".password-store"))
    denied("cannot list the owner's .claude", lambda: os.listdir(home / ".claude"))
    denied("cannot list the owner's .ssh", lambda: os.listdir(home / ".ssh"))
    denied("cannot list the owner's home", lambda: os.listdir(home))
    denied("cannot enter the owner's home (its folders included)", lambda: _enter(home))
    out.append(Result(not _sudo_allowed(), "cannot run sudo -n true"))
    mine = _group_names()
    for g in forbidden_groups(home):
        out.append(Result(g not in mine, "not in the group %s" % g))
    out.append(Result(_ping(p.check_socket), "the check socket answers ping"))
    ok, note = _can_write(p.shared)
    out.append(Result(ok, "can write tcp-shared", note))
    ok, note = _can_write(p.outbox)
    out.append(Result(ok, "can write the outbox", note))
    return out


def main(argv: list[str] | None = None) -> int:
    """`awb seal check`. Exit 0 all PASS, 1 any FAIL, 2 usage error."""
    from awb.cli import SafeParser

    ap = SafeParser(prog="awb seal", description="Checks of the seal between the vault and working sessions.")
    sub = ap.add_subparsers(dest="command", parser_class=SafeParser)
    sub.add_parser("check", help="run the checks that need no root for the current user, PASS or FAIL per line")
    try:
        args = ap.parse_args(argv)
    except SystemExit as exc:
        return exc.code if isinstance(exc.code, int) else 2
    if args.command != "check":
        ap.print_usage(sys.stderr)
        return 2
    results = run_checks()
    for r in results:
        print("%s  %s%s" % ("PASS" if r.ok else "FAIL", r.what, " (%s)" % r.note if r.note and not r.ok else ""))
    failed = sum(1 for r in results if not r.ok)
    print("# %d checks, %d failed" % (len(results), failed))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
