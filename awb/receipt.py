"""The receipt of a sealed start: the guards a session start checks and the line its context opens with.

A part of the client hooks (awb/hooks.py, F1 step 6). `receipt` checks the guards of GUARDS; `_start_receipt` logs
the start in <shared>/sessions/receipts.tsv and leaves the mark the prompt hook reads. `keys_call` is the core's one
request to the key service (awb/projects.py uses it too).
"""
from __future__ import annotations

import datetime
import json
import os
import re
from pathlib import Path

from awb import config


# --------------------------------------------------------------------------- the fixed values

INSTALLED_AWB = Path("/usr/local/bin/awb")
"""The command the seal installs for the work user (seal/setup.sh): a root owned wrapper that runs the installed
interpreter isolated (`python3 -I -m awb`); a symlink to the venv script on a host sealed before T3."""
SINCE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")
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


def _release() -> str:
    """The short name of the running release: the first 7 characters of its commit, else the folder name."""
    try:
        from awb import vault

        name = os.path.basename(vault.release_path())
    except Exception:
        return "unknown release"
    return name[:7] if re.fullmatch(r"[0-9a-f]{40}", name) else (name or "unknown release")


def _hook_entries(data) -> set[tuple[str, str, str]] | None:
    """(event, matcher, command) of every command hook of a settings object; None when it has no such shape."""
    if not isinstance(data, dict) or not isinstance(data.get("hooks"), dict):
        return None
    out = set()
    for event, entries in data["hooks"].items():
        for entry in entries if isinstance(entries, list) else ():
            if not isinstance(entry, dict):
                continue
            for h in entry.get("hooks") or ():
                if isinstance(h, dict) and h.get("type") == "command" and isinstance(h.get("command"), str):
                    out.add((str(event), str(entry.get("matcher") or ""), h["command"]))
    return out


def _guard_hooks() -> bool:
    """Every hook of the release's managed settings is in the installed managed file, and each of them runs the
    installed command (INSTALLED_AWB, an executable file)."""
    try:
        want = _hook_entries(json.loads(Path(MANAGED_SOURCE).read_text(encoding="utf-8")))
        have = _hook_entries(json.loads(Path(MANAGED_FILE).read_text(encoding="utf-8")))
    except (OSError, ValueError, UnicodeDecodeError):
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


def _guard_gate(root: Path | None) -> bool:
    """The project's repository carries the three hooks of the commit gate (awb gate --install) and runs its hooks
    from .git/hooks (no core.hooksPath)."""
    if root is None:
        return False
    from awb import gate

    git = Path(root) / ".git"
    try:
        if not git.is_dir() or git.is_symlink():
            return False
        conf = (git / "config").read_text(encoding="utf-8", errors="replace")
        if re.search(r"(?im)^\s*hookspath\s*=", conf):
            return False
        for name in gate.HOOK_NAMES:
            hook = git / "hooks" / name
            if not hook.is_file() or not os.access(hook, os.X_OK):
                return False
            if gate.HOOK_MARK not in hook.read_text(encoding="utf-8", errors="replace"):
                return False
    except OSError:
        return False
    return True


def _guard_vault(p: config.Paths) -> str:
    """"ok", "locked" or "down": the vault daemon answers a check, says it is locked, or does not answer."""
    from awb import vault

    try:
        state = vault.ping_state(p.check_socket, timeout=RECEIPT_TIMEOUT)
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

    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
        s.settimeout(timeout)
        s.connect(str(sock or _keys_socket()))
        s.sendall(json.dumps(obj).encode("utf-8") + b"\n")
        buf = b""
        while not buf.endswith(b"\n") and len(buf) < 65536:
            part = s.recv(4096)
            if not part:
                break
            buf += part
    answer = json.loads(buf.decode("utf-8"))
    if not isinstance(answer, dict):
        raise ValueError("the key service answered no object")
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
        return checked, missing, LOCKED_START % (since if since and SINCE_RE.match(since) else "unknown")
    names = [n for k, n, _ in GUARDS if k in missing]
    fixes = [f for k, _, f in GUARDS if k in missing]
    return checked, missing, REFUSED_START % ("guard" if len(names) == 1 else "guards", ", ".join(names),
                                              "; ".join(fixes))


def _receipt_mark(p: config.Paths, sid) -> Path | None:
    sid = re.sub(r"[^A-Za-z0-9_-]", "", sid or "")[:80] if isinstance(sid, str) else ""
    return p.shared / "sessions" / ("receipt-%s.ok" % sid) if sid else None


def _log_receipt(p: config.Paths, sid, root: Path | None, missing: list[str]) -> None:
    from awb import codes

    code = root.name if root is not None and codes.PROJECT_RE.fullmatch(root.name) else "-"
    sid = re.sub(r"[^A-Za-z0-9_-]", "", sid)[:80] if isinstance(sid, str) else ""
    line = "%s\t%s\t%s\t%s\n" % (datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                                 sid or "-", code, ",".join(missing) or "ok")
    try:
        folder = p.shared / "sessions"
        folder.mkdir(parents=True, exist_ok=True)
        with open(folder / RECEIPTS_LOG, "a", encoding="utf-8") as fh:
            fh.write(line)
    except OSError:
        pass


def refused_today(p: config.Paths, day: str | None = None) -> int | None:
    """How many sessions started today (UTC) with a refused receipt; None when the log cannot be read. For awb
    board show and /health."""
    day = day or datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d")
    try:
        text = (p.shared / "sessions" / RECEIPTS_LOG).read_text(encoding="utf-8", errors="replace")
    except FileNotFoundError:
        return 0
    except OSError:
        return None
    seen = set()
    for line in text.splitlines():
        f = line.split("\t")
        if len(f) == 4 and f[0].startswith(day) and f[3] != "ok":
            seen.add(f[1] if f[1] != "-" else line)
    return len(seen)


def refused_line(p: config.Paths) -> str:
    n = refused_today(p)
    return "sessions refused at start today: %s" % ("unknown" if n is None else n)


def _start_receipt(p: config.Paths, data: dict, root: Path | None, owner: bool) -> tuple[bool, str]:
    """The receipt of a start on a sealed host, logged: (all guards active, the line)."""
    _, missing, line = receipt(p, root, owner)
    sid = data.get("session_id")
    _log_receipt(p, sid, root, missing)
    mark = _receipt_mark(p, sid)
    if mark is not None and not owner:
        try:
            if missing:
                mark.unlink(missing_ok=True)
            else:
                mark.parent.mkdir(parents=True, exist_ok=True)
                mark.write_text(line + "\n", encoding="utf-8")
        except OSError:
            pass
    return not missing, line
