"""Read-only project access checks. Platform readers are supplied by the command dispatcher.

Only codes, opaque handles, fixed labels and counts leave this module. Unavailable evidence is unknown,
never success. The command does not repair grants, write tokens, fetch documents or log in.
"""
from __future__ import annotations

import hashlib
import ipaddress
import json
import os
import re
import stat
from contextlib import contextmanager
from dataclasses import dataclass
from itertools import islice
from pathlib import Path
from urllib.parse import urlsplit

from awb import codes, grants, patterns, projects


@dataclass(frozen=True)
class Check:
    handle: str
    status: str
    detail: str = ""

    def line(self, code: str) -> str:
        if not isinstance(code, str) or not codes.is_project_code(code):
            raise projects.ProjectError("preflight needs a project code")
        check = self if _valid_check(self) else Check("preflight", "unknown", "unreadable")
        return "%s %s %s%s" % (code, check.handle, check.status,
                                " " + check.detail if check.detail else "")


def handle(kind: str, value: str) -> str:
    return kind + "-" + hashlib.sha256(value.encode("utf-8", errors="surrogatepass")).hexdigest()[:12]


# Bound both individual evidence and the aggregate work of one invocation.
_FILE_LIMIT = 8 * 1024 * 1024
_TOTAL_LIMIT = 32 * 1024 * 1024
_ENTRY_LIMIT = 4096
_DEPTH_LIMIT = 64
_DIR_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC


def _valid_check(check) -> bool:
    if not isinstance(check, Check) or any(not isinstance(v, str)
                                          for v in (check.handle, check.status, check.detail)):
        return False
    fixed = {"grant", "input", "tenants", "tenant-register", "platform", "preflight", "host-list"}
    opaque = re.fullmatch(r"(?:file|host)-[0-9a-f]{12}", check.handle)
    tenant = re.fullmatch(r"[a-z][a-z0-9-]{1,30}/(?:grant|read|eip|vpc|route-table|admin/(?:domain|user|password))",
                          check.handle)
    detail = check.detail in {"", "kind=query", "kind=project", "unreadable", "host-list", "key-service",
                              "none-discovered", "store-age"} or re.fullmatch(r"free=[0-9]+", check.detail)
    return bool((check.handle in fixed or opaque or tenant)
                and check.status in {"ok", "missing", "unknown"} and detail)


@contextmanager
def _directory(path: Path):
    """Open every component without following links; descendants use this pinned descriptor."""
    path = Path(os.path.abspath(path))
    fd = os.open(path.anchor, _DIR_FLAGS)
    try:
        for part in path.parts[1:]:
            child = os.open(part, _DIR_FLAGS, dir_fd=fd)
            os.close(fd)
            fd = child
        yield fd
    finally:
        os.close(fd)


def _read(parent: int, name: str, limit: int) -> bytes:
    # NONBLOCK makes a substituted FIFO fail the type check instead of waiting for a writer.
    fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC, dir_fd=parent)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_size > limit:
            raise ValueError()
        with os.fdopen(fd, "rb", closefd=False) as stream:
            data = stream.read(limit + 1)
        if len(data) > limit:
            raise ValueError()
        return data
    finally:
        os.close(fd)


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError()
        result[key] = value
    return result


def grant(p, row) -> Check:
    kind = projects.LEGACY_KINDS.get(row.kind, row.kind)
    try:
        path = grants.path(p)
        with _directory(path.parent) as parent:
            recorded = json.loads(_read(parent, path.name, _FILE_LIMIT).decode("utf-8"),
                                  object_pairs_hook=_unique_object)
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
    except (OSError, ValueError, RecursionError):
        return Check("grant", "unknown", "kind=" + kind)


_URL = re.compile(r"(?i)\b[a-z][a-z0-9+.-]*://[^\s<>\"'`]*")
_HOST = re.compile(r"(?i)(?<![\w.-])(?:[a-z0-9](?:[a-z0-9-]*[a-z0-9])?\.)+[a-z]{2,63}(?![\w.-])")


def hosts_in(text: str) -> set[str]:
    hosts = set()
    for match in _URL.finditer(text):
        parsed = urlsplit(match.group().rstrip(".,);}"))
        host = parsed.hostname
        # Accessing port also validates malformed or out-of-range port numbers.
        parsed.port
        if not host or any(c in host for c in "\\%"):
            raise ValueError()
        if ":" in host:
            ipaddress.IPv6Address(host)
        else:
            ascii_host = host.rstrip(".").encode("idna").decode("ascii")
            if len(ascii_host) > 253 or any(not re.fullmatch(r"[a-zA-Z0-9](?:[a-zA-Z0-9-]{0,61}[a-zA-Z0-9])?", label)
                                            for label in ascii_host.split(".")):
                raise ValueError()
        hosts.add(host.lower().rstrip("."))
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
    remaining = _TOTAL_LIMIT
    entries = 0

    def unreadable(relative):
        out.append(Check(handle("file", relative), "unknown", "unreadable"))

    def read(parent, name, relative):
        nonlocal remaining
        try:
            data = _read(parent, name, min(_FILE_LIMIT, remaining))
            remaining -= len(data)
            text = data.decode("utf-8")
            if "\0" in text:
                raise ValueError()
            found.update(hosts_in(text))
        except (OSError, ValueError):
            unreadable(relative)

    def walk(parent, name, relative, depth):
        nonlocal entries
        if depth > _DEPTH_LIMIT:
            unreadable(relative)
            return
        try:
            fd = os.open(name, _DIR_FLAGS, dir_fd=parent)
            try:
                # scandir is lazy, so even a huge directory consumes only the entry budget.
                with os.scandir(fd) as scan:
                    names = []
                    for entry in scan:
                        entries += 1
                        if entries > _ENTRY_LIMIT:
                            unreadable(relative)
                            return
                        names.append(entry.name)
                for child in sorted(names):
                    child_relative = relative + "/" + child
                    try:
                        info = os.stat(child, dir_fd=fd, follow_symlinks=False)
                        if stat.S_ISDIR(info.st_mode):
                            walk(fd, child, child_relative, depth + 1)
                        else:
                            read(fd, child, child_relative)
                    except OSError:
                        unreadable(child_relative)
            finally:
                os.close(fd)
        except OSError:
            unreadable(relative)

    try:
        with _directory(root) as parent:
            read(parent, "SCOPE.md", "SCOPE.md")
            walk(parent, "input", "input", 0)
    except (OSError, ValueError):
        unreadable(".")
    try:
        values = allowed()
        if isinstance(values, (str, bytes, dict)):
            raise ValueError()
        permitted = set()
        for index, value in enumerate(values):
            if not isinstance(value, str) or not value or index >= _ENTRY_LIMIT:
                raise ValueError()
            permitted.add(value)
    except (OSError, ValueError, RuntimeError, TypeError, AttributeError):
        permitted = None
        out.append(Check("host-list", "unknown", "unreadable"))
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
        evidence = list(islice(access(p, row, out[0]), _ENTRY_LIMIT + 1))
        if len(evidence) > _ENTRY_LIMIT or not all(_valid_check(check) for check in evidence):
            raise ValueError()
        for check in evidence:
            if check.handle == "grant":
                out[0] = check
            else:
                out.append(check)
    except (OSError, ValueError, RuntimeError, TypeError, AttributeError):
        out.append(Check("tenants", "unknown", "unreadable"))
    out += host_checks(Path(row.path), allowed)
    return out


def report(code: str, checks: list[Check]) -> int:
    if not isinstance(code, str) or not codes.is_project_code(code):
        raise projects.ProjectError("preflight needs a project code")
    checks = [c if _valid_check(c) else Check("preflight", "unknown", "unreadable") for c in checks]
    for check in checks:
        print(check.line(code))
    return int(any(c.status != "ok" for c in checks))
