"""The registry of the owner's Windows lab machines and their windows (task WL part 1, B-035).

    awb winlab add NAME --port PORT --fingerprint SHA256:... --no-customer-data    owner side: register a machine
    awb winlab open NAME --minutes N --project tcp-xxxx                            owner side: a window for a project
    awb winlab close NAME                                                          owner side: end the window now
    awb winlab remove NAME                                                         owner side: forget a machine
    awb winlab list                                                                machines, their window, its end

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
"""
from __future__ import annotations

import datetime
import json
import os
import re
import sys
import tempfile
from pathlib import Path

from awb import config

FILE_NAME = "winlab.json"
PORT_MIN, PORT_MAX = 1024, 65535
MINUTES_MAX = 1440
NAME_RE = re.compile(r"^[a-z][a-z0-9-]{1,30}$")
FINGERPRINT_RE = re.compile(r"^SHA256:[A-Za-z0-9+/]{43}$")
CODE_RE = re.compile(r"^tcp-[a-z0-9]{4}$")
STAMP = "%Y-%m-%dT%H:%M:%SZ"
OWNER_LINE = "the lab machines are the owner's: he runs awb winlab add, open, close and remove in his own terminal"


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
        today: str | None = None) -> str:
    _owner_only()
    if not no_customer_data:
        raise WinlabError("add needs --no-customer-data: your statement that %s holds no customer data" % name)
    if not NAME_RE.match(name or ""):
        raise WinlabError("a machine name is 2 to 31 small letters, digits and -, a letter first (lab-1)")
    if not isinstance(port, int) or not PORT_MIN <= port <= PORT_MAX:
        raise WinlabError("the port of a machine is a loopback port from %d to %d" % (PORT_MIN, PORT_MAX))
    if not FINGERPRINT_RE.match(fingerprint or ""):
        raise WinlabError("a fingerprint reads SHA256: and 43 base64 characters, as ssh-keygen -l prints it")
    machines = load(path)
    if name in machines:
        raise WinlabError("%s is registered already: awb winlab remove %s first" % (name, name))
    for other, m in machines.items():
        if m["port"] == port:
            raise WinlabError("port %d is the port of %s already" % (port, other))
    machines[name] = {"port": port, "fingerprint": fingerprint, "no_customer_data": True,
                      "registered": today or datetime.date.today().isoformat()}
    save(machines, path)
    return "lab machine %s registered: port %d, %s, holds no customer data" % (name, port, fingerprint)


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


# --------------------------------------------------------------------------- command line


def main(argv: list[str] | None = None) -> int:
    from awb.cli import SafeParser

    ap = SafeParser(prog="awb winlab", description="The owner's Windows lab machines and the windows in which a "
                                                   "project may use one.")
    sub = ap.add_subparsers(dest="command")
    a = sub.add_parser("add", help="register a lab machine (owner side, his own terminal)")
    a.add_argument("name")
    a.add_argument("--port", type=int, required=True, help="its loopback port, %d to %d" % (PORT_MIN, PORT_MAX))
    a.add_argument("--fingerprint", required=True, help="its pinned host key, SHA256:... as ssh-keygen -l prints it")
    a.add_argument("--no-customer-data", action="store_true",
                   help="your statement that the machine holds no customer data (required)")
    o = sub.add_parser("open", help="a window in which one project may use the machine; it ends by itself (owner "
                                    "side, his own terminal)")
    o.add_argument("name")
    o.add_argument("--minutes", type=int, required=True, help="1 to %d" % MINUTES_MAX)
    o.add_argument("--project", required=True, help="the project code, tcp-xxxx")
    c = sub.add_parser("close", help="end the window of a machine now (owner side, his own terminal)")
    c.add_argument("name")
    r = sub.add_parser("remove", help="forget a lab machine and its window (owner side, his own terminal)")
    r.add_argument("name")
    sub.add_parser("list", help="the machines, their window and its end")
    try:
        args = ap.parse_args(argv)
    except SystemExit as exc:
        return 0 if exc.code in (0, None) else 2
    if args.command is None:
        ap.print_usage(sys.stderr)
        return 2
    try:
        if args.command == "add":
            print(add(args.name, args.port, args.fingerprint, args.no_customer_data))
        elif args.command == "open":
            print(open_(args.name, args.minutes, args.project))
        elif args.command == "close":
            print(close(args.name))
        elif args.command == "remove":
            print(remove(args.name))
        elif args.command == "list":
            print("\n".join(list_lines()))
    except WinlabError as err:
        print("awb winlab: %s" % err, file=sys.stderr)
        return 1
    except OSError as err:
        print("awb winlab: %s (operating system error)" % type(err).__name__, file=sys.stderr)
        return 1
    return 0
