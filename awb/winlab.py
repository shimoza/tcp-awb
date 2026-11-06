"""The registry of the owner's Windows lab machines and their windows (task WL part 1, B-035).

    awb winlab add NAME --port PORT --fingerprint SHA256:... --no-customer-data    owner side: register a machine
        [--account awblab]
    awb winlab open NAME --minutes N --project tcp-xxxx                            owner side: a window for a project
    awb winlab close NAME                                                          owner side: end the window now
    awb winlab remove NAME [--script-only]                                         owner side: forget a machine,
                                                                                   print remove.ps1 for the laptop
    awb winlab list                                                                machines, their window, its end
    awb winlab bootstrap NAME --host-key PUB --tunnel USER@HOST [--port PORT]      owner side: print setup.ps1
        [--account awblab] [--server-key PUB] [--ssh-port 22]                      for the laptop (part 2, B-036)
    awb winlab run|start NAME [--vm VM] -- POWERSHELL, status|log|stop JOB,      a project session on a machine
        put FILE WINPATH, get WINPATH FILE                                         (part 3, awb/winlabsession.py)
    awb winlab serve [--socket PATH] [--key PATH]                                  the lab broker, run by root under
                                                                                   its unit (part 4, awb/tcp/winbroker.py)

A lab machine is a Windows laptop of the owner's (Hyper-V, an evaluation VM, no customer data) that a later part
reaches over a loopback port. It is registered by a name (lab-1), the loopback port (1024 to 65535), the pinned
fingerprint of its host key (`SHA256:` and 43 base64 characters, as ssh-keygen -l prints it), the day of the
registration and the owner's statement that it holds no customer data (`--no-customer-data`, required).

A window lets one project use one machine from its start to its end (1 to 1440 minutes). It ends by itself at its
end: nothing has to run for that, every check compares the end with the clock. A machine has at most one open
window; `close` ends it before its end. `allowed(name, project)` is the one check the later parts call: it returns
the machine or raises WinlabError with one line (unregistered machine, no window, expired window, another project).
`awb owner status` (awb/ownerwait.py) lists every open window with its end.

The registry is winlab.json beside keys.conf, the file of the read-only tenant settings (`AWB_KEYS_CONF`, default
~/.config/awb/keys.conf), mode 600, written to a fresh name and renamed. add, open, close and remove are the owner's:
refused for the work user and in an assistant session, as `awb tenant mode` is. No network here: nothing is reached,
nothing is run on a machine. Standard library only; core code, so the owner status needs no module of the platform
package.

Part 2 (B-036): `bootstrap` prints awb/winlab/setup.ps1 and `remove` prints awb/winlab/remove.ps1, two templates
filled here and run by the owner on the laptop as administrator. setup.ps1 sets up the OpenSSH server on 127.0.0.1
only, a local account (default awblab, Administrators and Hyper-V Administrators, key logon only) that the host's
key (--host-key) logs on to, the laptop's own tunnel key and the visible scheduled task awb-winlab-tunnel: at boot
it holds `ssh -N -R 127.0.0.1:PORT:127.0.0.1:22 USER@HOST` (--tunnel; the host's server key, --server-key, pinned)
and starts it again when it ends. It prints the fingerprint of the laptop's host key for `add` and the tunnel public
key. remove.ps1 takes away what setup.ps1 made; setup.ps1 leaves %ProgramData%\\awb-winlab\\state.txt for it. Both
are checked as text (tests/test_wl_scripts.py); nothing here runs PowerShell. bootstrap writes nothing: a machine
not registered yet needs --port, because the fingerprint `add` needs comes from setup.ps1.

Part 3 (B-037): the commands of a project session live in awb/winlabsession.py, loaded by name when one runs.

Part 4 (B-038): the lab broker (awb/tcp/winbroker.py, `awb winlab serve`, run by root) holds the host's private key
for the lab machines and is the one way to them: a session asks it over a Unix socket and it checks the window here
again before every call. `add --account` records the laptop's account when setup.ps1 made another one than awblab.
`online(port)` says whether a machine's tunnel answers on its loopback port (an SSH banner, no key, no login);
`awb owner status` lists every registered machine that is online.
"""
from __future__ import annotations

import base64
import datetime
import json
import os
import re
import socket
import sys
import tempfile
from pathlib import Path

from awb import codes, config

FILE_NAME = "winlab.json"
PORT_MIN, PORT_MAX = 1024, 65535
MINUTES_MAX = 1440
NAME_RE = re.compile(r"^[a-z][a-z0-9-]{1,30}$")
FINGERPRINT_RE = re.compile(r"^SHA256:[A-Za-z0-9+/]{43}$")
CODE_RE = re.compile("^%s$" % codes.TCP_PROJECT_WIDE)
STAMP = "%Y-%m-%dT%H:%M:%SZ"
OWNER_LINE = "the lab machines are the owner's: he runs awb winlab add, open, close and remove in his own terminal"
SCRIPTS = Path(__file__).resolve().parent / "winlab"     # setup.ps1 and remove.ps1, data of the package
PLACEHOLDER_RE = re.compile(r"@@[A-Z_]+@@")
ACCOUNT = "awblab"
ACCOUNT_RE = re.compile(r"^[a-z][a-z0-9-]{1,19}$")
USER_RE = re.compile(r"^[a-z_][a-z0-9_.-]{0,31}$")
HOST_RE = re.compile(r"^(?=.{1,253}$)[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?"
                     r"(?:\.[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?)*$")
KEY_TYPES = ("ssh-ed25519", "ecdsa-sha2-nistp256", "ecdsa-sha2-nistp384", "ecdsa-sha2-nistp521", "ssh-rsa")
SERVER_KEY = "/etc/ssh/ssh_host_ed25519_key.pub"
LOOPBACK = "127.0.0.1"
ONLINE_TIMEOUT = 1.5
"""Seconds `online` waits for the SSH banner of a machine's tunnel."""


class WinlabError(Exception):
    """A refusal; its text is one line."""


def _now() -> datetime.datetime:
    return datetime.datetime.now(datetime.timezone.utc)


def _stamp(t: datetime.datetime) -> str:
    return t.astimezone(datetime.timezone.utc).strftime(STAMP)


def _parse(stamp: str) -> datetime.datetime:
    return datetime.datetime.strptime(stamp, STAMP).replace(tzinfo=datetime.timezone.utc)


def _shown(stamp: str) -> str:
    return _parse(stamp).strftime("%Y-%m-%d %H:%M UTC")


def registry_path() -> Path:
    """winlab.json beside keys.conf of the owner."""
    from awb import tenantmode

    return tenantmode.settings_file().parent / FILE_NAME


def _owner_only() -> None:
    from awb import vault

    if config.is_work_user() or vault.in_assistant_session():
        raise WinlabError(OWNER_LINE)


def _valid_window(w) -> bool:
    if not isinstance(w, dict) or not CODE_RE.match(str(w.get("project", ""))):
        return False
    try:
        return _parse(str(w["start"])) < _parse(str(w["end"]))
    except (KeyError, ValueError):
        return False


def _valid_machine(m) -> bool:
    return (isinstance(m, dict) and isinstance(m.get("port"), int) and PORT_MIN <= m["port"] <= PORT_MAX
            and FINGERPRINT_RE.match(str(m.get("fingerprint", ""))) is not None
            and re.match(r"^\d{4}-\d{2}-\d{2}$", str(m.get("registered", ""))) is not None
            and m.get("no_customer_data") is True
            and ("account" not in m or ACCOUNT_RE.match(str(m["account"])) is not None)
            and (m.get("window") is None or _valid_window(m["window"])))


def load(path: Path | None = None) -> dict[str, dict]:
    """{name: machine} of the registry; nothing when there is no file. A file that does not read as a registry is
    refused, so a broken registry allows nothing."""
    path = path or registry_path()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except (OSError, UnicodeDecodeError, ValueError):
        raise WinlabError("the lab registry %s cannot be read" % path.name) from None
    machines = data.get("machines") if isinstance(data, dict) else None
    if not isinstance(machines, dict) or not all(NAME_RE.match(str(n)) and _valid_machine(m)
                                                 for n, m in machines.items()):
        raise WinlabError("the lab registry %s is not a list of valid machines" % path.name)
    return machines


def save(machines: dict[str, dict], path: Path | None = None) -> None:
    """The registry to a fresh name in its folder, mode 600, then renamed over the old one."""
    path = path or registry_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".%s." % FILE_NAME, dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump({"machines": machines}, fh, indent=1, sort_keys=True)
            fh.write("\n")
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _get(machines: dict[str, dict], name: str) -> dict:
    if name not in machines:
        raise WinlabError("%s is not a registered lab machine (awb winlab list)" % name)
    return machines[name]


def open_window(machine: dict, now: datetime.datetime) -> dict | None:
    """The window of a machine while it is open at `now`; None without one or after its end."""
    w = machine.get("window")
    return w if w and now < _parse(w["end"]) else None


# --------------------------------------------------------------------------- the owner's commands


def add(name: str, port: int, fingerprint: str, no_customer_data: bool, *, path: Path | None = None,
        today: str | None = None, account: str = ACCOUNT) -> str:
    _owner_only()
    if not no_customer_data:
        raise WinlabError("add needs --no-customer-data: your statement that %s holds no customer data" % name)
    if not NAME_RE.match(name or ""):
        raise WinlabError("a machine name is 2 to 31 small letters, digits and -, a letter first (lab-1)")
    if not isinstance(port, int) or not PORT_MIN <= port <= PORT_MAX:
        raise WinlabError("the port of a machine is a loopback port from %d to %d" % (PORT_MIN, PORT_MAX))
    if not FINGERPRINT_RE.match(fingerprint or ""):
        raise WinlabError("a fingerprint reads SHA256: and 43 base64 characters, as ssh-keygen -l prints it")
    if not ACCOUNT_RE.match(account or ""):
        raise WinlabError("the local account is 2 to 20 small letters, digits and -, a letter first (%s)" % ACCOUNT)
    machines = load(path)
    if name in machines:
        raise WinlabError("%s is registered already: awb winlab remove %s first" % (name, name))
    for other, m in machines.items():
        if m["port"] == port:
            raise WinlabError("port %d is the port of %s already" % (port, other))
    machines[name] = {"port": port, "fingerprint": fingerprint, "no_customer_data": True,
                      "registered": today or datetime.date.today().isoformat()}
    if account != ACCOUNT:
        machines[name]["account"] = account
    save(machines, path)
    return "lab machine %s registered: port %d, %s%s, holds no customer data" % (
        name, port, fingerprint, "" if account == ACCOUNT else ", account %s" % account)


def open_(name: str, minutes: int, project: str, *, path: Path | None = None,
          now: datetime.datetime | None = None) -> str:
    _owner_only()
    if not CODE_RE.match(project or ""):
        raise WinlabError("a project code reads like tcp-q7m4")
    if not isinstance(minutes, int) or not 1 <= minutes <= MINUTES_MAX:
        raise WinlabError("a window lasts 1 to %d minutes" % MINUTES_MAX)
    now = now or _now()
    machines = load(path)
    m = _get(machines, name)
    w = open_window(m, now)
    if w:
        raise WinlabError("%s has an open window for %s until %s: awb winlab close %s first"
                          % (name, w["project"], _shown(w["end"]), name))
    end = now + datetime.timedelta(minutes=minutes)
    m["window"] = {"project": project, "start": _stamp(now), "end": _stamp(end)}
    save(machines, path)
    return "lab machine %s: window open for %s until %s (%d min)" % (name, project, _shown(m["window"]["end"]),
                                                                     minutes)


def close(name: str, *, path: Path | None = None, now: datetime.datetime | None = None) -> str:
    _owner_only()
    now = now or _now()
    machines = load(path)
    m = _get(machines, name)
    w = open_window(m, now)
    had = m.pop("window", None)
    if had:
        save(machines, path)
    if not w:
        return "lab machine %s: no open window" % name
    return "lab machine %s: window of %s closed" % (name, w["project"])


def remove(name: str, *, path: Path | None = None, now: datetime.datetime | None = None) -> str:
    _owner_only()
    now = now or _now()
    machines = load(path)
    w = open_window(_get(machines, name), now)
    del machines[name]
    save(machines, path)
    return "lab machine %s removed%s" % (name, ", its window of %s closed" % w["project"] if w else "")


# --------------------------------------------------------------------------- what the later parts call


def allowed(name: str, project: str, *, path: Path | None = None, now: datetime.datetime | None = None) -> dict:
    """The machine `name` when `project` holds its open window now, as {"name", "port", "fingerprint", ...}; else
    WinlabError with one line: unregistered machine, no window, expired window or another project."""
    now = now or _now()
    m = _get(load(path), name)
    w = m.get("window")
    if not w:
        raise WinlabError("%s has no open window: the owner opens one with awb winlab open %s --minutes N "
                          "--project %s" % (name, name, project))
    if now >= _parse(w["end"]):
        raise WinlabError("the window of %s for %s ended at %s" % (name, w["project"], _shown(w["end"])))
    if w["project"] != project:
        raise WinlabError("the window of %s is open for another project, not for %s" % (name, project))
    return dict(m, name=name)


def windows(path: Path | None = None, now: datetime.datetime | None = None) -> list[tuple[str, dict]]:
    """(name, window) of every open window now, by name; nothing when the registry cannot be read."""
    now = now or _now()
    try:
        machines = load(path)
    except WinlabError:
        return []
    return [(n, w) for n, w in ((n, open_window(m, now)) for n, m in sorted(machines.items())) if w]


def online(port: int, timeout: float = ONLINE_TIMEOUT) -> bool:
    """True when something answers on 127.0.0.1:`port` with an SSH banner: the tunnel of a lab machine is up. No key
    and no login, nothing is sent."""
    try:
        with socket.create_connection((LOOPBACK, port), timeout=timeout) as s:
            s.settimeout(timeout)
            return s.recv(64).startswith(b"SSH-2.0-")
    except OSError:
        return False


def online_machines(path: Path | None = None, probe=None) -> list[tuple[str, dict]]:
    """(name, machine) of every registered machine whose tunnel answers now, by name; nothing when the registry
    cannot be read."""
    probe = probe or online
    try:
        machines = load(path)
    except WinlabError:
        return []
    return [(n, m) for n, m in sorted(machines.items()) if probe(m["port"])]


def list_lines(path: Path | None = None, now: datetime.datetime | None = None) -> list[str]:
    now = now or _now()
    machines = load(path)
    if not machines:
        return ["no lab machine registered (the owner: awb winlab add NAME --port PORT --fingerprint SHA256:... "
                "--no-customer-data)"]
    rows = [["machine", "port", "fingerprint", "registered", "window", "end"]]
    for name, m in sorted(machines.items()):
        w = m.get("window")
        if not w:
            window, end = "none", "-"
        elif now < _parse(w["end"]):
            left = int((_parse(w["end"]) - now).total_seconds() // 60)
            window, end = "open for %s" % w["project"], "%s (%d min left)" % (_shown(w["end"]), left)
        else:
            window, end = "ended for %s" % w["project"], _shown(w["end"])
        rows.append([name, str(m["port"]), m["fingerprint"], m["registered"], window, end])
    widths = [max(len(r[i]) for r in rows) for i in range(len(rows[0]))]
    return ["  ".join(c.ljust(widths[i]) for i, c in enumerate(r)).rstrip() for r in rows]


# --------------------------------------------------------------------------- the scripts for the laptop (part 2)


def _public_key(path: str, option: str) -> tuple[str, str]:
    """(type, base64) of the one OpenSSH public key in the file at `path`; the comment is dropped. A refusal names
    the option, never a line of the file."""
    try:
        text = Path(path).read_text(encoding="ascii")
    except FileNotFoundError:
        raise WinlabError("%s: there is no file at the path given" % option) from None
    except (OSError, UnicodeDecodeError):
        raise WinlabError("%s: the file cannot be read as a public key" % option) from None
    if "PRIVATE KEY" in text:
        raise WinlabError("%s: the file is a private key, give its .pub file" % option)
    lines = [ln for ln in text.splitlines() if ln.strip() and not ln.lstrip().startswith("#")]
    parts = lines[0].split() if len(lines) == 1 else []
    if len(parts) < 2 or parts[0] not in KEY_TYPES:
        raise WinlabError("%s: the file is not one OpenSSH public key (ssh-ed25519 AAAA...)" % option)
    try:
        blob = base64.b64decode(parts[1], validate=True)
    except ValueError:
        blob = b""
    # the blob is length-prefixed fields to its last byte, the first one the type
    fields, at = [], 0
    while at + 4 <= len(blob):
        size = int.from_bytes(blob[at:at + 4], "big")
        fields.append(blob[at + 4:at + 4 + size])
        at += 4 + size
    if at != len(blob) or len(fields) < 2 or fields[0] != parts[0].encode("ascii"):
        raise WinlabError("%s: the key in the file is damaged" % option)
    return parts[0], parts[1]


def _script(file_name: str, values: dict[str, str]) -> str:
    """A template of awb/winlab/ with its @@PLACEHOLDERS@@ filled; one left unfilled refuses the script."""
    try:
        text = (SCRIPTS / file_name).read_text(encoding="ascii")
    except (OSError, UnicodeDecodeError):
        raise WinlabError("the template awb/winlab/%s cannot be read" % file_name) from None
    for key, value in values.items():
        text = text.replace("@@%s@@" % key, value)
    if PLACEHOLDER_RE.search(text):
        raise WinlabError("the template awb/winlab/%s has a placeholder no value fills" % file_name)
    return text


def bootstrap(name: str, host_key: str, tunnel: str, *, port: int | None = None, account: str = ACCOUNT,
              server_key: str = SERVER_KEY, ssh_port: int = 22, path: Path | None = None) -> str:
    """setup.ps1 for the lab laptop `name`: the host's public key (`host_key`, a .pub file) logs on to the local
    account, the laptop's tunnel goes to `tunnel` (USER@HOST, port `ssh_port`) and pins the host's server key
    (`server_key`, a .pub file). The loopback port is the registered one; a machine that is not registered yet (the
    fingerprint comes from this script) needs `port`. Nothing is written."""
    _owner_only()
    if not NAME_RE.match(name or ""):
        raise WinlabError("a machine name is 2 to 31 small letters, digits and -, a letter first (lab-1)")
    machines = load(path)
    if name in machines:
        if port is not None and port != machines[name]["port"]:
            raise WinlabError("%s is registered with port %d, not %d" % (name, machines[name]["port"], port))
        port = machines[name]["port"]
    if port is None:
        raise WinlabError("%s is not registered yet: give its loopback port with --port" % name)
    if not isinstance(port, int) or not PORT_MIN <= port <= PORT_MAX:
        raise WinlabError("the port of a machine is a loopback port from %d to %d" % (PORT_MIN, PORT_MAX))
    for other, m in machines.items():
        if other != name and m["port"] == port:
            raise WinlabError("port %d is the port of %s already" % (port, other))
    if not ACCOUNT_RE.match(account or ""):
        raise WinlabError("the local account is 2 to 20 small letters, digits and -, a letter first (%s)" % ACCOUNT)
    user, _, host = (tunnel or "").partition("@")
    if not USER_RE.match(user) or not HOST_RE.match(host):
        raise WinlabError("--tunnel reads USER@HOST: the account and the name or IPv4 address of the Workbench host")
    if not isinstance(ssh_port, int) or not 1 <= ssh_port <= 65535:
        raise WinlabError("the SSH port of the host is 1 to 65535")
    key_type, key = _public_key(host_key, "--host-key")
    server_type, server = _public_key(server_key, "--server-key")
    known = host if ssh_port == 22 else "[%s]:%d" % (host, ssh_port)
    return _script("setup.ps1", {
        "MACHINE": name, "PORT": str(port), "ACCOUNT": account, "TUNNEL": tunnel, "TUNNEL_USER": user,
        "SSH_PORT": str(ssh_port), "HOST_KEY": "%s %s awb-winlab-host" % (key_type, key),
        "KNOWN_HOST": "%s %s %s" % (known, server_type, server)})


def removal_script(name: str) -> str:
    """remove.ps1 for the lab laptop `name`; it reads what to remove from the state.txt setup.ps1 left there."""
    _owner_only()
    if not NAME_RE.match(name or ""):
        raise WinlabError("a machine name is 2 to 31 small letters, digits and -, a letter first (lab-1)")
    return _script("remove.ps1", {"MACHINE": name})


# --------------------------------------------------------------------------- command line

SESSION_COMMANDS = ("run", "start", "status", "log", "stop", "put", "get")
"""The commands of a project session on a machine (part 3, awb/winlabsession.py)."""


def _session_module():
    """awb/winlabsession.py, loaded by name: it reads the paste rules and the checks, which no unit imports, so the
    owner status that imports this module keeps out of it (deploy.DYNAMIC)."""
    import importlib

    return importlib.import_module("awb.winlabsession")


def _broker_module():
    """awb/tcp/winbroker.py, the lab broker of part 4, loaded by name for `awb winlab serve` only."""
    import importlib

    return importlib.import_module("awb.tcp.winbroker")


def main(argv: list[str] | None = None) -> int:
    from awb.cli import SafeParser

    ap = SafeParser(prog="awb winlab", description="The owner's Windows lab machines, the windows in which a "
                                                   "project may use one and the commands, jobs and files of a project "
                                                   "session there.")
    sub = ap.add_subparsers(dest="command")
    a = sub.add_parser("add", help="register a lab machine (owner side, his own terminal)")
    a.add_argument("name")
    a.add_argument("--port", type=int, required=True, help="its loopback port, %d to %d" % (PORT_MIN, PORT_MAX))
    a.add_argument("--fingerprint", required=True, help="its pinned host key, SHA256:... as ssh-keygen -l prints it")
    a.add_argument("--no-customer-data", action="store_true",
                   help="your statement that the machine holds no customer data (required)")
    a.add_argument("--account", default=ACCOUNT,
                   help="the local account on the laptop, as bootstrap --account made it (default %s)" % ACCOUNT)
    o = sub.add_parser("open", help="a window in which one project may use the machine; it ends by itself (owner "
                                    "side, his own terminal)")
    o.add_argument("name")
    o.add_argument("--minutes", type=int, required=True, help="1 to %d" % MINUTES_MAX)
    o.add_argument("--project", required=True, help="the project code, tcp-xxxx")
    c = sub.add_parser("close", help="end the window of a machine now (owner side, his own terminal)")
    c.add_argument("name")
    r = sub.add_parser("remove", help="forget a lab machine and its window and print remove.ps1 for the laptop "
                                      "(owner side, his own terminal)")
    r.add_argument("name")
    r.add_argument("--script-only", action="store_true",
                   help="print remove.ps1 and keep the machine registered (also for a machine never registered)")
    sub.add_parser("list", help="the machines, their window and its end")
    b = sub.add_parser("bootstrap", help="print setup.ps1, which the owner runs once on the laptop as administrator "
                                         "(owner side, his own terminal)")
    b.add_argument("name")
    b.add_argument("--host-key", required=True, help="the .pub file of the key the host logs on to the laptop with")
    b.add_argument("--tunnel", required=True, help="USER@HOST: the account on the Workbench host the laptop's "
                                                   "tunnel logs on to and the host's name or IPv4 address")
    b.add_argument("--port", type=int, help="the loopback port, for a machine that is not registered yet")
    b.add_argument("--account", default=ACCOUNT, help="the local account on the laptop (default %s)" % ACCOUNT)
    b.add_argument("--server-key", default=SERVER_KEY,
                   help="the .pub file of the host's own SSH server key the laptop pins (default %s)" % SERVER_KEY)
    b.add_argument("--ssh-port", type=int, default=22, help="the SSH port of the host (default 22)")
    for name, what in (("run", "run a PowerShell command on the machine and print its output (project session, part "
                               "3)"),
                       ("start", "start a long PowerShell job on the machine as a scheduled task; prints its job id "
                                 "(project session)")):
        s = sub.add_parser(name, help=what)
        s.add_argument("name")
        s.add_argument("powershell", nargs="*", metavar="POWERSHELL",
                       help="the PowerShell command, after -- (awb winlab %s lab-1 -- Get-VM)" % name)
        s.add_argument("--vm", help="run it in this VM with PowerShell Direct; the machine reads the guest credential "
                                    "itself")
        if name == "run":
            s.add_argument("--timeout", type=int, default=300, help="seconds the command may take (default 300)")
    for name, what in (("status", "running, ended with its exit code, stopped, lost or machine offline"),
                       ("log", "the new part of the job's log, also appended to notes/winlab/jobs/JOB.log"),
                       ("stop", "end the job")):
        s = sub.add_parser(name, help="%s (project session)" % what)
        s.add_argument("job", help="the job id awb winlab start printed")
    s = sub.add_parser("put", help="a file of the project's winlab-out/ to the machine, in chunks that resume, the "
                                   "SHA-256 compared (project session)")
    s.add_argument("file")
    s.add_argument("winpath", help="the whole path on the machine, C:\\lab\\file.txt")
    s.add_argument("--machine", help="the machine, when the project holds windows on two")
    s = sub.add_parser("get", help="a file of the machine into the project, in chunks that resume, the SHA-256 "
                                   "compared (project session)")
    s.add_argument("winpath", help="the whole path on the machine, C:\\lab\\file.txt")
    s.add_argument("file")
    s.add_argument("--machine", help="the machine, when the project holds windows on two")
    s = sub.add_parser("serve", help="run the lab broker, the one way to the lab machines (root, under its unit)")
    s.add_argument("--socket", help="its Unix socket (default /run/awb-winlab/broker.sock)")
    s.add_argument("--key", help="the host's private key for the lab machines (default /etc/awb/winlab/id_ed25519)")
    argv = list(sys.argv[1:] if argv is None else argv)
    tail = None
    if argv[:1] and argv[0] in SESSION_COMMANDS and "--" in argv:
        # what follows -- is the PowerShell text, whatever it looks like
        cut = argv.index("--")
        argv, tail = argv[:cut], argv[cut + 1:]
    try:
        args = ap.parse_args(argv)
    except SystemExit as exc:
        return 0 if exc.code in (0, None) else 2
    if args.command is None:
        ap.print_usage(sys.stderr)
        return 2
    if args.command in SESSION_COMMANDS:
        return _session_module().main(args, tail)
    if args.command == "serve":
        return _broker_module().main(args)
    try:
        if args.command == "add":
            print(add(args.name, args.port, args.fingerprint, args.no_customer_data, account=args.account))
        elif args.command == "open":
            print(open_(args.name, args.minutes, args.project))
        elif args.command == "close":
            print(close(args.name))
        elif args.command == "remove":
            # the script alone on standard output, so `> remove.ps1` takes it; the registry line goes to stderr
            script = removal_script(args.name)
            line = None if args.script_only else remove(args.name)
            sys.stdout.write(script)
            print(line or "remove.ps1 for %s printed; the machine stays registered" % args.name, file=sys.stderr)
        elif args.command == "list":
            from awb import winlabsession

            if winlabsession._local_registry():
                print("\n".join(list_lines()))
            else:               # a session of the work user: the broker answers for its own project
                print("\n".join(winlabsession.session_list()))
        elif args.command == "bootstrap":
            sys.stdout.write(bootstrap(args.name, args.host_key, args.tunnel, port=args.port, account=args.account,
                                       server_key=args.server_key, ssh_port=args.ssh_port))
            print("setup.ps1 for %s printed: run it once on the laptop as administrator, powershell -NoProfile "
                  "-ExecutionPolicy Bypass -File .\\setup.ps1" % args.name, file=sys.stderr)
    except WinlabError as err:
        print("awb winlab: %s" % err, file=sys.stderr)
        return 1
    except OSError as err:
        print("awb winlab: %s (operating system error)" % type(err).__name__, file=sys.stderr)
        return 1
    return 0
