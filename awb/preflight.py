"""Read-only project access checks. Platform readers are supplied by the command dispatcher.

Only codes, opaque handles, fixed labels and counts leave this module. Unavailable evidence is unknown,
never success. The command does not repair grants, write tokens, fetch documents or log in.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

from awb import grants, patterns, projects


@dataclass(frozen=True)
class Check:
    handle: str
    status: str
    detail: str = ""

    def line(self, code: str) -> str:
        return "%s %s %s%s" % (code, self.handle, self.status, " " + self.detail if self.detail else "")


def handle(kind: str, value: str) -> str:
    return kind + "-" + hashlib.sha256(value.encode("utf-8")).hexdigest()[:12]


def grant(p, row) -> Check:
    kind = projects.LEGACY_KINDS.get(row.kind, row.kind)
    try:
        recorded = json.loads(grants.path(p).read_text(encoding="utf-8"))
        if not isinstance(recorded, dict):
            raise ValueError()
        if row.code not in recorded:
            return Check("grant", "missing", "kind=" + kind)
        entry = recorded[row.code]
        if not isinstance(entry, dict) or entry.get("kind") not in grants.KINDS:
            raise ValueError()
        return Check("grant", "ok" if entry["kind"] == kind else "missing", "kind=" + kind)
    except FileNotFoundError:
        return Check("grant", "missing", "kind=" + kind)
    except (OSError, ValueError):
        return Check("grant", "unknown", "kind=" + kind)


_URL = re.compile(r"(?i)\b[a-z][a-z0-9+.-]*://[^\s<>\"'`]+")
_HOST = re.compile(r"(?i)(?<![\w.-])(?:[a-z0-9](?:[a-z0-9-]*[a-z0-9])?\.)+[a-z]{2,63}(?![\w.-])")


def hosts_in(text: str) -> set[str]:
    hosts = set()
    for match in _URL.finditer(text):
        try:
            host = urlsplit(match.group().rstrip(".,);]}")).hostname
            if host:
                hosts.add(host.lower().rstrip("."))
        except ValueError:
            pass
    # Do not turn SCOPE.md, source filenames or dotted selectors into host requirements.
    for match in _HOST.finditer(_URL.sub(" ", text)):
        host = match.group().lower()
        suffix = host.rsplit(".", 1)[-1]
        if suffix in patterns._TLD_SET | {"example", "test", "invalid", "local"} \
                and suffix not in patterns._FILE_SUFFIX_TLDS:
            hosts.add(host)
    return hosts


def host_checks(root: Path, allowed) -> list[Check]:
    out, found = [], set()
    files = [root / "SCOPE.md"]
    try:
        def failed(_error):
            out.append(Check("input", "unknown", "unreadable"))

        input_root = root / "input"
        if input_root.is_symlink():
            out.append(Check(handle("file", "input"), "unknown", "unreadable"))
        for folder, dirs, names in (() if input_root.is_symlink() else
                                    os.walk(input_root, followlinks=False, onerror=failed)):
            for name in list(dirs):
                if (Path(folder) / name).is_symlink():
                    dirs.remove(name)
                    out.append(Check(handle("file", str((Path(folder) / name).relative_to(root))),
                                     "unknown", "unreadable"))
            files.extend(Path(folder) / name for name in sorted(names))
        for path in files:
            try:
                if path.is_symlink() or not path.is_file():
                    raise OSError()
                text = path.read_text(encoding="utf-8")
                if "\0" in text:
                    raise ValueError()
                found.update(hosts_in(text))
            except (OSError, ValueError):
                out.append(Check(handle("file", str(path.relative_to(root))), "unknown", "unreadable"))
    except OSError:
        out.append(Check("input", "unknown", "unreadable"))
    try:
        permitted = set(allowed())
    except (OSError, ValueError):
        permitted = None
    for host in sorted(found):
        out.append(Check(handle("host", host), "unknown" if permitted is None else (
            "ok" if host in permitted else "missing"), "host-list"))
    return out


def checks(p, code: str, access, allowed) -> list[Check]:
    row = next((r for r in projects.load(p) if r.code == code), None)
    if row is None:
        raise projects.ProjectError("no registered project of that code")
    out = [grant(p, row)]
    try:
        for check in access(p, row, out[0]):
            if check.handle == "grant":
                out[0] = check
            else:
                out.append(check)
    except (OSError, ValueError, RuntimeError):
        out.append(Check("tenants", "unknown", "unreadable"))
    out += host_checks(Path(row.path), allowed)
    return out


def report(code: str, checks: list[Check]) -> int:
    for check in checks:
        print(check.line(code))
    return int(any(c.status != "ok" for c in checks))
