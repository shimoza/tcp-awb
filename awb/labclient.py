"""The lab run client of a project session (task LR): scripts call the lab tenants through the key service.

    awb lab run start --tenant T [--note TEXT]    open a run of this folder's project (only on his word)
    awb lab run keep ITEM --reason TEXT [--tenant T]   keep one recorded create (its number or id) past the end
    awb lab run end [--tenant T]                  delete what the run made, newest first; report and close it
    awb lab run status                            the open runs of this project and their leftovers
    awb lab run end --keep NAME[,NAME...]         end the run and leave the named OBS buckets (the kept list)
    awb lab kept drop NAME                        delete a kept bucket later, with the delete and proof of an end

A change to a stand the project holds (task LK) stays: `call(..., keep="why")` keeps the create it makes (a rule of
a listed security group), `awb lab run keep` keeps one already recorded. The key service allows it on a lab tenant
only, for a project whose RESOURCES.md lists a live row on that tenant or a live parent the create names; the end
skips it and reports it as kept, `awb owner status` shows it, and this client writes its row in RESOURCES.md as live
(KEEP_NOTE). A PUT, PATCH or tag action on an existing resource records nothing, so the end never undoes it. A delete
of a resource listed live in RESOURCES.md needs `call(..., delete_listed=True)`; when it answers 2xx or 404 the key
service records it as removed in the run and this client sets its row in RESOURCES.md to deleted (mark_deleted).

A script of the project imports this module with the installed Workbench's python:

    /opt/tcp-awb/venv/bin/python3 evidence/<folder>/poc.py      # the script runs `from awb import labclient`

    status, headers, body = labclient.call("POST", "iam", "/v3/users", body={"user": {...}})

`call(method, service, path, body=None, params=None, region=None)` goes through the key service socket and the
open run of the tenant (the one open run of the project, or `tenant=`), so a script needs no login, no password and
no urllib of its own. The key service signs with the tenant's admin login, records every create and answers in full;
a token, a temporary AK/SK and a security token in the answer come shortened to 8 characters, "..." and the length,
and a password or a login never comes. A shortened token of an earlier answer of the run works as the header
`X-Auth-Token` (and a shortened temporary access key as `credential=`): the service puts the whole value in.
`{project_id}` and `{domain_id}` in a path are filled in by the service. Every call is appended to
notes/lab-runs/<run>.log of the project, request and answer, shortened the same way (awb/labsecrets.py); a boot
script (user_data) is written there only as a placeholder with its length (BOOT_SCRIPT_KEYS). A log over 2 MiB
moves to notes/lab-runs/full/ (the project's .gitignore keeps it out of git) and git keeps
notes/lab-runs/<run>.summary.md with its counts and its SHA-256 (awb/lablog.py, Q-0021).

A large file goes to or from OBS as a job of the key service (task LO, awb/tcp/labobs.py), not through `call`:

    awb lab obs put FILE obs://BUCKET/KEY [--tenant T]    labclient.obs_put(file, url) -> {"job": ..., ...}
    awb lab obs get obs://BUCKET/KEY FILE [--tenant T]    labclient.obs_get(url, file)
    awb lab obs status [JOB]                              labclient.obs_status(job=None) -> [job, ...]
    awb lab obs abort JOB                                 labclient.obs_abort(job); obs_wait(job) waits for its end
    awb lab obs link BUCKET KEY [--minutes N]             labclient.obs_link(bucket, key) -> {"url": ..., ...}

The file goes to the service as an open descriptor (SCM_RIGHTS) with one of the run log, never as a path; a get
writes only inside the project folder or the session scratch folder (SCRATCH). The same put or get once more resumes
an interrupted job.

A reader without a key (an ECS of the run, a public ACL is refused) gets a link: `obs_link` asks the key service to
sign a GET of one object of a bucket the open run made, for --minutes (default 60, at most 720). The URL comes back
once and is printed once; the run log gets the bucket, the key's hash, the minutes and the expiry, never the URL.
"""
from __future__ import annotations

import argparse
import fcntl
import functools
import secrets
import stat
import time
from contextlib import contextmanager
import json
import math
import os
import re
import socket
import sys
from datetime import datetime, timezone
from pathlib import Path

from awb import safefile, tenantname

EXIT_OK, EXIT_REFUSED, EXIT_LEFT = 0, 2, 4
LOG_FOLDER = Path("notes") / "lab-runs"
SOCKET_ENV = "AWB_KEYS_SOCKET"
DEFAULT_SOCKET = "/run/awb-keys/cloud.sock"
TIMEOUT = 200.0
"""Seconds of one request: the key service's end of a run takes at most about 150 seconds of deletes."""
MAX_ANSWER = 8 * 1024 * 1024 + 4096
KEEP_NOTE = "kept from lab run %s on %s: %s"
"""The note of the RESOURCES.md row of a kept create: run, tenant, reason."""
SCRATCH = "/tmp/claude-%d"
"""The session scratch folder of a uid, where a lab obs get may write beside the project folder."""
OBS_ENDED = ("done", "failed", "interrupted", "aborted")


class LabClientError(Exception):
    """A lab call that could not be made: the one line of the key service or of this client."""


def _tenant_name(alias):
    """The alias of --tenant as the work rules write it: test-10491 is the old name of tcp-10491 (one warning line
    in its window, refused after it). The name given is kept: the key service maps it to the tenant it holds, and
    the run log of a project keeps the name the run started under."""
    try:
        tenantname.canonical(alias)
        return alias
    except tenantname.OldNameRefused as err:
        raise LabClientError(str(err)) from None


def _root(start: Path | None = None) -> Path:
    from awb import config

    root = config.scope_root(Path(os.path.abspath(start or Path.cwd())))
    if root is None:
        raise LabClientError("a lab run runs from the folder of a project (its SCOPE.md)")
    return root


MAX_FILE = 1024 * 1024


def _identifier(value, kind: str) -> str:
    from awb import codes

    patterns = {"run": r"run-[0-9]{8}-[0-9]{6}-[a-z0-9]{4}",
                "tenant": r"[a-z][a-z0-9-]{0,63}", "project": codes.TCP_PROJECT_WIDE}
    if not isinstance(value, str) or not re.fullmatch(patterns[kind], value):
        raise LabClientError("invalid %s identifier" % kind)
    return value


def _safe_text(text: str) -> str:
    """Prepare complete output for printing: a control or bidi character or a text that is no UTF-8 is withheld and
    the key service secrets are shortened (labsecrets.shorten). No name check runs (B-089: inside, the check is at
    the entry and at the exit)."""
    from awb import labsecrets

    try:
        text.encode("utf-8")
        if any(ord(c) < 32 and c not in "\n\t" or 0x7f <= ord(c) < 0xa0 or
               c in "\u202a\u202b\u202c\u202d\u202e\u2066\u2067\u2068\u2069" for c in text):
            return "[withheld]"
        text, _ = labsecrets.shorten(text)
        return text
    except Exception:
        return "[withheld]"


def _print(text: str, **kwargs) -> None:
    print(_safe_text(text), **kwargs)


def _service_error(answer: dict) -> str:
    # Keep the established diagnosis without relaying arbitrary service prose.
    error = answer.get("error")
    if isinstance(error, str) and re.fullmatch(r"refused: (?:test-|tcp-)[0-9]+ is no lab tenant.*", error):
        return "refused: no lab tenant"
    # The key service's own refusals ("refused: ...", one short line) carry the reason the session needs; they are
    # relayed only when the screen leaves them unchanged. Anything else stays generic.
    if isinstance(error, str) and re.fullmatch(r"refused: [^\r\n]{1,300}", error) and _safe_text(error) == error:
        return error
    # A call the service answered with an error (a 401 of a lab call): its HTTP status and error code (HTTP 401
    # TMS.0003) are relayed, the rest of the line stays withheld (tcp-ui5o, 2026-11-20).
    m = re.match(r"HTTP [1-5][0-9]{2}(?: [A-Z][A-Za-z]{1,11}\.[0-9]{1,8})?(?=[;:,) ]|\Z)", error) \
        if isinstance(error, str) else None
    if m:
        return m.group(0)
    return "the key service refused the request"


def _display_error(value) -> str:
    """Service diagnostics may contain rejected credentials even without a field label."""
    if isinstance(value, str) and re.fullmatch(
            r"HTTP [1-5][0-9]{2}(?: [A-Z]{2,12}\.[0-9]{1,8})?|not recorded|"
            r"still there after delete: [A-Za-z0-9_.:-]{1,128}|"
            r"the platform keeps an IAM project after delete: [A-Za-z0-9_.:-]{1,128} is left behind", value):
        return value
    return "operation failed"


def _json_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError
        result[key] = value
    return result


def _invalid_constant(value):
    raise ValueError


def _json_float(value):
    number = float(value)
    if not math.isfinite(number):
        raise ValueError
    return number


@contextmanager
def _directory(path: Path, create: bool = False):
    """Pin every ancestor; never follow a directory link between checking and opening."""
    path = Path(os.path.abspath(path))
    fd = os.open(path.anchor, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
    try:
        for part in path.parts[1:]:
            if create:
                try:
                    os.mkdir(part, 0o700, dir_fd=fd)
                except FileExistsError:
                    pass
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=fd)
            os.close(fd)
            fd = child
        yield fd
    finally:
        os.close(fd)


def _file(parent: int, name: str, flags: int) -> int:
    return safefile.open_regular(name, dir_fd=parent, flags=flags, cloexec=True, single_link=True,
                                 refuse=lambda: LabClientError("refused: expected a regular file with one link"))


def _read(parent: int, name: str, limit: int = MAX_FILE) -> str:
    fd = _file(parent, name, os.O_RDONLY)
    with os.fdopen(fd, "rb") as fh:
        raw = fh.read(limit + 1)
    if len(raw) > limit:
        raise LabClientError("refused: project file is too large")
    return raw.decode("utf-8")


SHARED_MODES = {"RESOURCES.md": safefile.SHARED}
"""The mode of a project file this client creates that others read: the lab daemon reads RESOURCES.md."""


def _write(parent: int, name: str, text: str) -> None:
    """Replace only a regular destination, using a private, unique temporary file. The new file keeps the mode of
    the one it replaces (a new RESOURCES.md gets 664): the lab daemon reads RESOURCES.md through the group, and a
    replace with the private mode of the temporary file left it unreadable, so the next keep was refused (tcp-6edq,
    2026-11-18)."""
    mode = SHARED_MODES.get(name, 0o600)
    try:
        fd = _file(parent, name, os.O_RDONLY)
    except FileNotFoundError:
        pass
    else:
        try:
            mode = stat.S_IMODE(os.fstat(fd).st_mode) & 0o777
        finally:
            os.close(fd)
    safefile.write_atomic(name, text, dir_fd=parent, mode=mode, create_mode=0o600, chmod=True,
                          temp=lambda _name: ".lab-" + secrets.token_hex(16))


def _boundary(fn):
    @functools.wraps(fn)
    def wrapped(*args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except (OSError, ValueError, TypeError, KeyError, IndexError, AttributeError, RecursionError, OverflowError):
            raise LabClientError("lab client could not read or write valid data") from None
    return wrapped


def _locked(fn):
    @functools.wraps(fn)
    def wrapped(root, *args, **kwargs):
        if root is None:
            return fn(root, *args, **kwargs)
        with _directory(root) as fd:
            fcntl.flock(fd, fcntl.LOCK_EX)
            return fn(root, *args, **kwargs)
    return wrapped


@_boundary
def _code(root: Path) -> str:
    with _directory(root) as fd:
        try:
            text = _read(fd, "SCOPE.md")
        except FileNotFoundError:
            text = ""
    m = re.search(r"^- code: *(\S+) *$", text, re.MULTILINE)
    return _identifier(m.group(1) if m else root.name, "project")


def _folder(root: Path) -> Path:
    folder = root / LOG_FOLDER
    with _directory(folder, create=True):
        pass
    return folder


def _marker(root: Path, tenant: str) -> Path:
    return root / LOG_FOLDER / ("%s.open" % _identifier(tenant, "tenant"))


@_boundary
def _open_runs(root: Path) -> dict[str, str]:
    out = {}
    try:
        with _directory(root / LOG_FOLDER) as fd:
            for name in sorted(os.listdir(fd)):
                if name.endswith(".open"):
                    tenant = _identifier(name[:-5], "tenant")
                    run = _read(fd, name, 128)
                    # Only the single newline written by start is framing, not arbitrary whitespace.
                    out[tenant] = _identifier(run.removesuffix("\n"), "run")
    except FileNotFoundError:
        # A missing directory is normal; a disappearing marker is not a usable run.
        return {}
    return out


def _pick(root: Path, tenant: str | None, run: str | None) -> tuple[str, str]:
    if tenant is not None:
        _identifier(tenant, "tenant")
    if run is not None:
        _identifier(run, "run")
    runs = _open_runs(root)
    if tenant is None:
        if len(runs) != 1:
            raise LabClientError("name the tenant: %s" % ("no lab run is open here (awb lab run start --tenant T, "
                                                         "only on his word)" if not runs else
                                                         "multiple runs are open"))
        tenant = next(iter(runs))
    if run is None:
        run = runs.get(tenant)
        if not run:
            raise LabClientError("no lab run is open here on the requested tenant")
    return tenant, run


def _stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


@_boundary
def log(root: Path, run: str, lines: list[str]) -> None:
    """Append one screened block through a pinned directory and a checked descriptor. A log over lablog.LIMIT
    moves to notes/lab-runs/full/ (git-ignored) and git keeps notes/lab-runs/<run>.summary.md (Q-0021)."""
    from awb import lablog

    _identifier(run, "run")
    text = _safe_text("\n".join(lines) + "\n")
    text = (text if text.endswith("\n") else text + "\n") + "\n"
    lablog.append(root, root / LOG_FOLDER / (run + ".log"), text)


def _log_place(logs: int, run: str) -> int:
    """A descriptor that appends to the run log where it is now: its full log once it left git (Q-0021,
    lablog.open_live)."""
    from awb import lablog

    try:
        return lablog.open_live(logs, run + ".log")
    except lablog.LabLogError:
        raise LabClientError("refused: expected a regular file with one link") from None


BOOT_SCRIPT_KEYS = frozenset({"userdata", "alphaccepreinstall", "alphaccepostinstall", "preinstall", "postinstall",
                              "userscript", "customdata", "cloudinit", "bootscript", "startupscript"})
"""The keys of a boot script, compared without case, separators and a prefix up to the last colon
(`OS-EXT-SRV-ATTR:user_data`, `user_data`, `alpha.cce/preInstall`). `GET .../cloudservers/detail` answers the
user_data of every server of the tenant, also of other projects (container cluster nodes), so the run log keeps
only a placeholder with its length (tcp-s5i3, 2026-11-19). The script still gets the whole answer."""
BOOT_SCRIPT_NOTE = "[boot script withheld, %d characters]"


def _boot_key(key) -> bool:
    return isinstance(key, str) and re.sub(r"[^a-z0-9]", "", key.rsplit(":", 1)[-1].lower()) in BOOT_SCRIPT_KEYS


def _withhold(value, depth: int = 0):
    """`value` with every boot script (BOOT_SCRIPT_KEYS) replaced by BOOT_SCRIPT_NOTE, base64 or not."""
    if depth > 64:
        return value
    if isinstance(value, dict):
        return {k: (BOOT_SCRIPT_NOTE % len(v if isinstance(v, str) else json.dumps(v))
                    if _boot_key(k) and v not in (None, "") else _withhold(v, depth + 1)) for k, v in value.items()}
    if isinstance(value, list):
        return [_withhold(v, depth + 1) for v in value]
    return value


def _pretty(value) -> str:
    if isinstance(value, str):
        return value
    return json.dumps(_withhold(value), indent=1, ensure_ascii=False, sort_keys=True)


def _token(root: Path | None) -> str | None:
    """K4: the key token of the project folder; the work user writes one into a folder that has none yet, as every
    client of the key service does (awb/tcp/keys.py own_token)."""
    from awb import config, projects

    if root is None:
        return None
    try:
        token = projects.read_key_token(root)
        if token is None and config.is_work_user() and not os.path.lexists(root / projects.BIND_FILE):
            token = projects.write_key_token(root)
        return token
    except (OSError, ValueError):
        return None


@_boundary
def _request(obj: dict, root: Path | None = None, fds: tuple[int, ...] = ()) -> dict:
    """One request on the call socket of the key service, one JSON line each way; `fds` go with the line as
    SCM_RIGHTS (a lab obs put or get). This module speaks the socket itself: it is core code and imports nothing of
    the platform package."""
    token = _token(root)
    if token is not None:
        obj = dict(obj, token=token)
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        deadline = time.monotonic() + TIMEOUT
        s.settimeout(TIMEOUT)
        s.connect(os.environ.get(SOCKET_ENV) or DEFAULT_SOCKET)
        line = json.dumps(obj, allow_nan=False).encode("utf-8") + b"\n"
        if fds:
            sent = socket.send_fds(s, [line], list(fds))
            s.sendall(line[sent:])
        else:
            s.sendall(line)
        buf = bytearray()
        while b"\n" not in buf:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise LabClientError("the key service answer timed out")
            s.settimeout(remaining)
            chunk = s.recv(1 << 16)
            if not chunk:
                break
            buf += chunk
            if len(buf) > MAX_ANSWER:
                raise LabClientError("the answer of the key service is too large")
        if b"\n" not in buf:
            raise ValueError
        answer = json.loads(bytes(buf).partition(b"\n")[0], object_pairs_hook=_json_object,
                            parse_constant=_invalid_constant, parse_float=_json_float)
        if not isinstance(answer, dict) or type(answer.get("ok")) is not bool:
            raise ValueError
        return answer
    except (FileNotFoundError, ConnectionRefusedError):
        raise LabClientError("the key service is not running") from None
    except PermissionError:
        raise LabClientError("the key service refused this user") from None
    except (ValueError, TypeError, RecursionError):
        raise LabClientError("the key service gave no readable answer") from None
    except OSError as err:
        raise LabClientError("no answer from the key service (%s)" % type(err).__name__) from None
    finally:
        s.close()


@_boundary
def call(method: str, service: str, path: str, body=None, params=None, region=None, *, tenant: str | None = None,
         run: str | None = None, headers: dict | None = None, scope: str | None = None, credential: str | None = None,
         keep: str | None = None, delete_listed: bool = False, start: Path | None = None):
    """(status, headers, body) of one call to the lab tenant of the open run. `body` is JSON (a dict or a list) or
    a text (an OBS body); `params` the query; `scope` a project name or id for a token of that project; `keep` a
    reason that keeps what the call creates past the end of the run (its row goes into RESOURCES.md);
    `delete_listed` allows the delete of a resource listed live in RESOURCES.md. A refusal of the key service
    raises LabClientError with its one line."""
    root = _root(start)
    tenant, run = _pick(root, tenant, run)
    query = {str(k): (v if isinstance(v, (str, list)) else str(v)) for k, v in (params or {}).items()}
    req = {"op": "lab_call", "tenant": tenant, "run": run, "project": _code(root), "method": method.upper(),
           "service": service, "path": path, "query": query, "body": body, "region": region,
           "headers": dict(headers or {})}
    if scope is not None:
        req["scope"] = scope
    if credential is not None:
        req["credential"] = credential
    if keep is not None:
        req["keep"] = keep
    if delete_listed:
        req["delete_listed"] = True
    answer = _request(req, root)
    head = ["## %s %s %s %s" % (_stamp(), method.upper(), service, path)]
    head += ["region: %s" % region] if region else []
    head += ["query: %s" % json.dumps(query, ensure_ascii=False)] if query else []
    head += ["request headers: %s" % json.dumps(headers, ensure_ascii=False)] if headers else []
    head += ["request body:", _pretty(body)] if body is not None else []
    head += ["keep: %s" % keep] if keep is not None else []
    head += ["delete_listed: yes"] if delete_listed else []
    if not answer.get("ok"):
        log(root, run, head + ["refused: %s" % _service_error(answer)])
        raise LabClientError(_service_error(answer))
    data = answer.get("data")
    out = data if data is not None else (answer.get("text") or "")
    lines = head + ["answer: %s%s" % (answer.get("status"), " (path %s)" % answer["path"]
                                      if answer.get("path") not in (None, path) else ""),
                    "answer headers: %s" % json.dumps(answer.get("headers") or {}, ensure_ascii=False, sort_keys=True)]
    if out not in ("", None):
        lines += ["answer body:", _pretty(out)]
    if answer.get("created"):
        lines += ["recorded: %s" % ", ".join(answer["created"])]
    if answer.get("tagged"):
        lines += ["project tag: %s" % answer["tagged"]]
    for k in answer.get("kept") or []:
        lines += ["kept: %s %s (%s), row %s in RESOURCES.md" % (k["label"], k["id"], k["reason"],
                                                                  keep_row(root, run, tenant, k))]
    gone = [g for g in answer.get("listed_removed") or [] if isinstance(g, dict)]
    if gone:
        marked = mark_deleted(root, run, gone)
        lines += ["removed listed: %s, %s" % (", ".join(_safe_text(str(g.get("handle") or "-")) for g in gone),
                                              "row %s in RESOURCES.md set to deleted" % ", ".join(marked)
                                              if marked else "no row in RESOURCES.md matched")]
    log(root, run, lines)
    return answer.get("status"), answer.get("headers") or {}, out


DELETED_NOTE = "deleted in lab run %s"


@_boundary
@_locked
def mark_deleted(root: Path, run: str, gone: list[dict]) -> list[str]:
    """Set the state of the RESOURCES.md rows of the listed resources a delete of the run removed (the key service
    names their handle and id) to deleted; the handles of the rows it changed. A row is found by its handle or its
    id, compared without case; other rows and lines stay as they are."""
    _identifier(run, "run")
    want = set()
    for g in gone:
        for key in ("handle", "id"):
            v = str(g.get(key) or "").strip().lower()
            if re.fullmatch(r"[a-z0-9_.:-]{1,80}", v):
                want.add(v)
    if not want:
        return []
    with _directory(root) as fd:
        try:
            text = _read(fd, "RESOURCES.md")
        except FileNotFoundError:
            return []
        head = None
        out, marked = [], []
        for line in text.splitlines(keepends=True):
            body = line.rstrip("\r\n")
            if not body.strip().startswith("|"):
                out.append(line)
                continue
            cells = body.strip().strip("|").split("|")
            plain = [c.strip().strip("`").strip().lower() for c in cells]
            if head is None:
                head = plain
                out.append(line)
                continue
            if "state" not in head or len(cells) != len(head) or all(set(c) <= set("-: ") for c in plain):
                out.append(line)
                continue
            st = head.index("state")
            keys_ = {plain[head.index(k)] for k in ("handle", "id") if k in head}
            if plain[st] in ("deleted", "not created") or not keys_ & want:
                out.append(line)
                continue
            cells[st] = " deleted "
            if "note" in head:
                n = head.index("note")
                note = cells[n].strip()
                cells[n] = " %s " % ("; ".join(x for x in (note if note not in ("", "-") else "",
                                                          DELETED_NOTE % run) if x))
            out.append("|" + "|".join(cells) + "|" + line[len(body):])
            marked.append(plain[head.index("handle")] if "handle" in head else plain[0])
        if marked:
            _write(fd, "RESOURCES.md", "".join(out))
    return marked


@_boundary
@_locked
def keep_row(root: Path, run: str, tenant: str, k: dict) -> str:
    """Add one kept resource without losing another writer's row or forging Markdown cells."""
    from awb import projects

    _identifier(run, "run")
    _identifier(tenant, "tenant")
    def cell(value):
        return " ".join(_safe_text(str(value or "-")).split()).replace("|", "/").replace("`", "")
    rid, label, region, reason = (cell(k.get(key)) for key in ("id", "label", "region", "reason"))
    with _directory(root) as fd:
        try:
            text = _read(fd, "RESOURCES.md")
        except FileNotFoundError:
            text = projects._resources(_code(root))
        handles = []
        for line in text.splitlines():
            cells = [c.strip().strip("`") for c in line.strip().strip("|").split("|")] if \
                line.strip().startswith("|") else []
            if len(cells) >= 8 and rid not in ("-", "[withheld]", "[secret]") and rid == cells[1]:
                return cells[0]
            if cells:
                handles.append(cells[0])
        words = label.split()
        kind = re.sub(r"[^a-z0-9]+", "-", words[-1].lower() if words else "resource").strip("-") or "resource"
        kind = kind[:-1] if kind.endswith("s") and len(kind) > 3 else kind
        n = 1 + max([int(h.rsplit("-", 1)[1]) for h in handles if
                     re.fullmatch(re.escape(kind) + r"-[0-9]{1,9}", h)] + [0])
        handle = "%s-%d" % (kind, n)
        note = cell(KEEP_NOTE % (run, tenant, reason))
        row = "| %s | %s | %s | %s | - | - | live | %s |\n" % (handle, rid if rid != "-" else "unknown",
                                                                   label, region, note)
        _write(fd, "RESOURCES.md", text + ("\n" if text and not text.endswith("\n") else "") + row)
    return handle


# --------------------------------------------------------------------------- large objects (LO): awb lab obs


def _object(url: str) -> tuple[str, str]:
    m = re.fullmatch(r"obs://([^/\s]+)/([^\r\n\x00-\x1f\x7f]+)", url or "")
    if not m:
        raise LabClientError("an object reads obs://BUCKET/KEY")
    return m.group(1), m.group(2)


@_boundary
def _transfer(action: str, path: str, url: str, tenant: str | None, run: str | None, region: str | None,
              start: Path | None, vendor_hash: str | None = None) -> dict:
    root = _root(start)
    tenant, run = _pick(root, tenant, run)
    bucket, key = _object(url) if action != "fetch" else (None, None)
    where = Path(os.path.abspath(path))
    if action != "put":
        inside = (Path(os.path.abspath(root)), Path(SCRATCH % os.getuid()))
        if not any(where != base and where.is_relative_to(base) for base in inside):
            raise LabClientError("refused: a %s writes only inside the project folder or the session scratch folder" % action)
    made = False
    try:
        with _directory(where.parent) as parent:
            if action == "put":
                fd = _file(parent, where.name, os.O_RDONLY)
            else:
                try:
                    fd = _file(parent, where.name, os.O_RDWR | os.O_CREAT | os.O_EXCL)
                    made = True
                except FileExistsError:
                    fd = _file(parent, where.name, os.O_RDWR)
            try:
                original = os.fstat(fd)
                with _directory(root / LOG_FOLDER, create=True) as logs:
                    log_fd = _log_place(logs, run)
                    try:
                        req = {"op": "lab_obs", "action": action, "tenant": tenant, "run": run,
                               "project": _code(root), "bucket": bucket, "key": key, "region": region}
                        if action == "fetch":
                            req = dict(req, url=url, hash=vendor_hash)
                        answer = _request(req, root, (fd, log_fd))
                    finally:
                        os.close(log_fd)
                if not answer.get("ok") and made:
                    try:
                        current = os.stat(where.name, dir_fd=parent, follow_symlinks=False)
                        if (current.st_dev, current.st_ino) == (original.st_dev, original.st_ino):
                            os.unlink(where.name, dir_fd=parent)
                    except FileNotFoundError:
                        pass
            finally:
                os.close(fd)
    except PermissionError:
        raise LabClientError("refused: this user cannot %s the session file" %
                             ("read" if action == "put" else "write")) from None
    except FileNotFoundError:
        if action != "put" and not where.parent.is_dir():
            raise LabClientError("no such folder: %s (make it first, then %s again)" %
                                 (_safe_text(str(where.parent)), action)) from None
        raise LabClientError("no such file or folder for the transfer") from None
    head = ["## %s obs %s %s %s" % (_stamp(), action, where if action == "put" else url,
                                    url if action == "put" else where)]
    if not answer.get("ok"):
        log(root, run, head + ["refused: %s" % _service_error(answer)])
        raise LabClientError(_service_error(answer))
    log(root, run, head + ["job: %s%s, %d bytes%s" % (answer["job"], " resumed" if answer.get("resumed") else "",
                                                      answer.get("size") or 0, ", %d parts of %d bytes" % (
                                                          answer["parts"], answer["part_size"])
                                                      if answer.get("parts") else "")])
    return answer


def obs_put(file, url: str, *, tenant: str | None = None, run: str | None = None, region: str | None = None,
            start: Path | None = None) -> dict:
    """Start the put of a file of the session to obs://BUCKET/KEY (a bucket named after the project) as a job of
    the key service: {"job", "state", "size", "parts", "part_size", "resumed", ...} at once. The file goes as an
    open descriptor, never as a path. The same file to the same object resumes an interrupted job."""
    return _transfer("put", str(file), url, tenant, run, region, start)


def obs_get(url: str, file, *, tenant: str | None = None, run: str | None = None, region: str | None = None,
            start: Path | None = None) -> dict:
    """Start the get of obs://BUCKET/KEY into a file inside the project folder or the session scratch folder as a
    job of the key service; the same object into the same file goes on where an interrupted job stopped."""
    return _transfer("get", str(file), url, tenant, run, region, start)


def obs_fetch(url: str, file, *, sha256: str | None = None, sha1: str | None = None, tenant: str | None = None,
              run: str | None = None, start: Path | None = None) -> dict:
    """Start the download of a vendor's file (a host of rules/vendor-hosts.txt) into a file inside the project
    folder or the session scratch folder as a job of the key service (VF). The hash the vendor publishes (sha256 or
    sha1) is checked at the end; without one the job records the hashes it computed."""
    if sha256 and sha1:
        raise LabClientError("name one vendor hash: --sha256 or --sha1")
    want = "sha256:%s" % sha256.lower() if sha256 else "sha1:%s" % sha1.lower() if sha1 else None
    return _transfer("fetch", str(file), url, tenant, run, None, start, want)


def obs_hosts() -> tuple[list[str], list[str]]:
    """(download hosts, portal hosts) of rules/vendor-hosts.txt."""
    from awb import vendorhosts

    return vendorhosts.read()


LINK_RE = re.compile(r"https?://[A-Za-z0-9.:-]+/[A-Za-z0-9%._~/@:=+-]+\?AccessKeyId=[A-Za-z0-9]{8,128}"
                     r"&Expires=[0-9]{9,11}&Signature=[A-Za-z0-9%._~-]{20,200}"
                     r"(?:&x-obs-security-token=[A-Za-z0-9%._~-]{16,16384})?")
"""The form of a link the key service signs; any other answer is withheld."""


@_boundary
def obs_link(bucket: str, key: str, *, minutes: int | None = None, tenant: str | None = None, run: str | None = None,
             start: Path | None = None) -> dict:
    """A time-limited GET link to obs://BUCKET/KEY in a bucket the open run made, signed by the key service:
    {"url", "expires", "minutes", "bucket", "key", "key_sha256", "run"}. The URL is in this answer only: the run
    log gets the bucket, the key hash, the minutes and the expiry."""
    root = _root(start)
    tenant, run = _pick(root, tenant, run)
    if minutes is not None and (isinstance(minutes, bool) or not isinstance(minutes, int)):
        raise LabClientError("refused: minutes is a whole number")
    req = {"op": "lab_obs", "action": "link", "tenant": tenant, "run": run, "project": _code(root),
           "bucket": bucket, "key": key, "minutes": minutes}
    answer = _request(req, root)
    if not answer.get("ok"):
        log(root, run, ["## %s obs link refused" % _stamp(), "refused: %s" % _service_error(answer)])
        raise LabClientError(_service_error(answer))
    url = answer.get("url")
    shown = "obs://%s/%s" % (answer.get("bucket"), answer.get("key"))
    if not isinstance(url, str) or not LINK_RE.fullmatch(url) or answer.get("bucket") != bucket \
            or answer.get("key") != key or _safe_text(shown) != shown:
        raise LabClientError("the key service gave no usable link")
    log(root, run, ["## %s obs link obs://%s/<key sha256 %s> GET, %s min, until %s (the URL is not logged)" % (
        _stamp(), bucket, answer.get("key_sha256"), answer.get("minutes"), answer.get("expires"))])
    return {k: answer.get(k) for k in ("url", "expires", "minutes", "bucket", "key", "key_sha256", "run")}


@_boundary
def obs_status(job: str | None = None, *, start: Path | None = None) -> list[dict]:
    """The lab obs jobs of the project (or the one named): id, action, bucket, key, size, done, rate (bytes per
    second), state (hashing, running, verifying, done, failed, interrupted, aborted), error, sha256."""
    root = _root(start)
    answer = _request({"op": "lab_obs", "action": "status", "project": _code(root), "job": job}, root)
    if not answer.get("ok"):
        raise LabClientError(_service_error(answer))
    return answer.get("jobs") or []


@_boundary
def obs_abort(job: str, *, start: Path | None = None) -> dict:
    """Stop a job; a put also aborts its multipart upload."""
    root = _root(start)
    answer = _request({"op": "lab_obs", "action": "abort", "project": _code(root), "job": job}, root)
    if not answer.get("ok"):
        raise LabClientError(_service_error(answer))
    return answer.get("job") or {}


@_boundary
def obs_wait(job: str, *, every: float = 15.0, start: Path | None = None) -> dict:
    """The job once it has ended (done, failed, interrupted or aborted), asked every `every` seconds."""
    import time

    while True:
        rows = obs_status(job, start=start)
        if not rows:
            raise LabClientError("no lab obs job found")
        if rows[0].get("state") in OBS_ENDED:
            return rows[0]
        time.sleep(every)


def _obs_parser(action: str) -> argparse.ArgumentParser:
    from awb.cli import SafeParser

    helps = {"put": "put a file of the session into a bucket of this project, a job of the key service",
             "fetch": "download a vendor's appliance image (a host of awb lab obs hosts) into the project folder or "
                      "the session scratch folder, a job of the key service",
             "hosts": "the vendor download hosts of a fetch and the portal hosts it refuses",
             "get": "get an object into the project folder or the session scratch folder, a job of the key service",
             "status": "bytes done, total, rate and state of this project's lab obs jobs",
             "abort": "stop a job; a put also aborts its multipart upload",
             "link": "a time-limited GET link to one object of a bucket the open lab run made, signed by the key "
                     "service (never a public ACL); printed once, never logged"}
    ap = SafeParser(prog="awb lab obs %s" % action, description=helps[action])
    if action == "put":
        ap.add_argument("file", metavar="FILE")
        ap.add_argument("url", metavar="obs://BUCKET/KEY")
    elif action == "get":
        ap.add_argument("url", metavar="obs://BUCKET/KEY")
        ap.add_argument("file", metavar="FILE")
    elif action == "fetch":
        ap.add_argument("url", metavar="URL")
        ap.add_argument("file", metavar="FILE")
        ap.add_argument("--sha256", default=None, help="the SHA-256 the vendor publishes")
        ap.add_argument("--sha1", default=None, help="the SHA-1 the vendor publishes")
    elif action == "hosts":
        pass
    elif action == "link":
        ap.add_argument("bucket", metavar="BUCKET")
        ap.add_argument("key", metavar="KEY")
        ap.add_argument("--minutes", type=int, default=None, help="how long the link works: 1 to 720 (default 60)")
    elif action == "status":
        ap.add_argument("job", metavar="JOB", nargs="?", default=None)
    else:
        ap.add_argument("job", metavar="JOB")
    if action in ("put", "get", "fetch", "link"):
        ap.add_argument("--tenant", default=None, help="a lab tenant of the owner's list or a granted customer "
                                                       "tenant in write mode (default: the one open run)")
    return ap


def _size(n) -> str:
    return "%.1f GB" % (float(n or 0) / 1e9) if (n or 0) >= 1e8 else "%d bytes" % int(n or 0)


def obs_main(argv: list[str]) -> int:
    action = argv[0] if argv else ""
    if action not in ("put", "get", "fetch", "hosts", "status", "abort", "link"):
        _print("awb lab obs: put, get, fetch, hosts, status, abort or link", file=sys.stderr)
        return EXIT_REFUSED
    try:
        args = _obs_parser(action).parse_args(argv[1:])
    except SystemExit as exc:
        return exc.code if isinstance(exc.code, int) else EXIT_REFUSED
    try:
        args.tenant = _tenant_name(getattr(args, "tenant", None))
        if action == "hosts":
            hosts, portals = obs_hosts()
            for h in hosts:
                _print(h)
            for h in portals:
                _print("%s  (portal: refused, hand the download to the owner)" % h)
            return EXIT_OK
        if action in ("put", "get", "fetch"):
            if action == "put":
                a = obs_put(args.file, args.url, tenant=args.tenant)
            elif action == "fetch":
                a = obs_fetch(args.url, args.file, sha256=args.sha256, sha1=args.sha1, tenant=args.tenant)
            else:
                a = obs_get(args.url, args.file, tenant=args.tenant)
            _print("job %s %s: %s of %s%s; awb lab obs status %s" % (
                a["job"], "resumed" if a.get("resumed") else "started", action, _size(a.get("size")),
                " in %d parts" % a["parts"] if a.get("parts") else "", a["job"]))
            return EXIT_OK
        if action == "link":
            a = obs_link(args.bucket, args.key, minutes=args.minutes, tenant=args.tenant)
            _print("GET link to obs://%s/%s, %s min, until %s; shown once, not logged:" % (
                a["bucket"], a["key"], a["minutes"], a["expires"]), file=sys.stderr)
            print(a["url"])         # whole: the screen of _print would shorten its signature and token
            return EXIT_OK
        if action == "abort":
            j = obs_abort(args.job)
            _print("job %s %s" % (j.get("id"), j.get("state")))
            return EXIT_OK
        rows = obs_status(args.job)
        if not rows:
            _print("no lab obs job")
        for j in rows:
            pct = 100.0 * (j.get("done") or 0) / j["size"] if j.get("size") else 0.0
            what = "%s -> %s" % (j.get("url"), j.get("file_name")) if j["action"] == "fetch" else \
                "obs://%s/%s" % (j["bucket"], j["key"])
            _print("%s  %s %s  %s  %s of %s (%.0f%%)  %.1f MB/s%s" % (
                j["id"], j["action"], what, j["state"], _size(j.get("done")), _size(j.get("size")),
                pct, (j.get("rate") or 0) / 1e6, "  %s" % _display_error(j["error"]) if j.get("error") else ""))
            if j.get("state") == "done" and j.get("sha256"):
                _print("    sha256 %s, %s s" % (j["sha256"], j.get("seconds")))
            if j["action"] == "fetch" and j.get("sha1"):
                _print("    sha1 %s, vendor hash: %s" % (j["sha1"], j.get("vendor_hash") or "-"))
        return EXIT_OK
    except LabClientError as err:
        _print("awb lab obs: %s" % err, file=sys.stderr)
        return EXIT_REFUSED
    except (OSError, ValueError, TypeError, KeyError, IndexError, AttributeError, OverflowError):
        _print("awb lab obs: invalid service data", file=sys.stderr)
        return EXIT_REFUSED


# --------------------------------------------------------------------------- awb lab run start|keep|end|status


def _parser(action: str) -> argparse.ArgumentParser:
    from awb.cli import SafeParser

    helps = {"start": "open a lab run of this folder's project on a lab tenant, only on his word",
             "keep": "keep one recorded create of the open run past its end (a change to a stand the project holds); "
                     "its row goes into RESOURCES.md as live",
             "end": "delete what the run made, newest first, report created, removed and failed, close the run",
             "status": "the open runs of this project and their leftovers"}
    ap = SafeParser(prog="awb lab run %s" % action, description=helps[action])
    if action == "end":
        ap.add_argument("--keep", default="", metavar="NAME[,NAME...]",
                        help="OBS bucket names of the run that stay (the kept list of the project); any other "
                             "kind is refused before a delete")
    if action == "keep":
        ap.add_argument("item", help="the number of the recorded create (awb lab run status) or its id")
        ap.add_argument("--reason", required=True, help="why it stays, one line")
    if action in ("start", "end", "keep"):
        ap.add_argument("--tenant", required=action == "start", default=None,
                        help="a lab tenant of the owner's list or a granted customer tenant in write mode (end: "
                             "default the one open run)")
    if action == "start":
        ap.add_argument("--note", default="", help="a short note of what the run tests")
    ap.add_argument("--project", default=None, help="default: the code of this folder's project")
    return ap


def run_main(argv: list[str]) -> int:
    action = argv[0] if argv else ""
    if action not in ("start", "keep", "end", "status"):
        _print("awb lab run: start, keep, end or status", file=sys.stderr)
        return EXIT_REFUSED
    ap = _parser(action)
    try:
        args = ap.parse_args(argv[1:])
    except SystemExit as exc:
        return exc.code if isinstance(exc.code, int) else EXIT_REFUSED
    try:
        root = _root()
    except LabClientError:
        root = None
    try:
        args.tenant = _tenant_name(getattr(args, "tenant", None))
        project = args.project or (_code(root) if root else None)
        if project is not None:
            _identifier(project, "project")
        if action == "status":
            return _status(project, root)
        if not project:
            raise LabClientError("this folder names no project; give --project")
        if action == "start":
            return _start(root, project, args.tenant, args.note)
        if action == "keep":
            return _keep(root, project, args.tenant, args.item, args.reason)
        return _end(root, project, args.tenant, [n for n in args.keep.split(",") if n.strip()] if args.keep else [])
    except LabClientError as err:
        _print("awb lab run: %s" % err, file=sys.stderr)
        return EXIT_REFUSED


def _event(word: str, run: str, project: str) -> None:
    """The start or the end (ok, red) of the lab run in the events file of the board (awb/events.py, Q-0014 phase
    2): kind labrun, the run id and the project code. A run whose deletes failed ends red. Never fails the run."""
    try:
        from awb import events

        if word == "start":
            events.start("labrun", run, project=project, history=True)
        else:
            events.end("labrun", run, word, project=project)
    except Exception:
        pass


@_boundary
@_locked
def _start(root: Path | None, project: str, tenant: str, note: str) -> int:
    _identifier(project, "project")
    _identifier(tenant, "tenant")
    if root is not None:
        _open_runs(root)
    answer = _request({"op": "lab_run", "action": "start", "project": project, "tenant": tenant, "note": note}, root)
    if not answer.get("ok"):
        raise LabClientError(_service_error(answer))
    run = _identifier(answer["run"], "run")
    if root is not None:
        with _directory(root / LOG_FOLDER, create=True) as fd:
            _write(fd, _marker(root, tenant).name, run + "\n")
        log(root, run, ["# lab run %s of %s on %s, opened %s" % (run, project, tenant, answer.get("started")),
                        "note: %s" % note if note else "note: -"])
    _event("start", run, project)
    _print("run %s open on %s for %s: scripts call through awb.labclient, then awb lab run end" % (run, tenant, project))
    _print("run a script with %s (plain python3 has no awb module)" % (sys.executable or "/opt/tcp-awb/venv/bin/python3"))
    return EXIT_OK


@_boundary
def _keep(root: Path | None, project: str, tenant: str | None, item: str, reason: str) -> int:
    if root is None:
        raise LabClientError("keep runs from the project folder: its RESOURCES.md gets the row")
    tenant, run = _pick(root, tenant, None)
    answer = _request({"op": "lab_run", "action": "keep", "project": project, "tenant": tenant, "run": run,
                       "item": item, "reason": reason}, root)
    if not answer.get("ok"):
        raise LabClientError(_service_error(answer))
    for k in answer.get("kept") or []:
        handle = keep_row(root, run, tenant, k)
        log(root, run, ["## %s keep %s %s (%s), row %s in RESOURCES.md" % (_stamp(), k["label"], k["id"],
                                                                         k["reason"], handle)])
        _print("kept %s %s as %s: the end of run %s leaves it, RESOURCES.md lists it live" % (k["label"], k["id"],
                                                                                          handle, run))
    return EXIT_OK


@_boundary
@_locked
def _end(root: Path | None, project: str, tenant: str | None, keep: list[str] | None = None) -> int:
    _identifier(project, "project")
    if tenant is not None:
        _identifier(tenant, "tenant")
    run = None
    if tenant is None:
        if root is None:
            raise LabClientError("name the tenant: --tenant T")
        tenant, run = _pick(root, None, None)
    elif root is not None:
        run = _open_runs(root).get(tenant)
    answer = _request({"op": "lab_run", "action": "end", "project": project, "tenant": tenant, "run": run,
                       "keep": [k.strip() for k in keep or []]}, root)
    if not answer.get("ok"):
        raise LabClientError(_service_error(answer))
    run = _identifier(answer["run"], "run")
    for key in ("failed", "removed", "kept"):
        rows = answer.get(key, [])
        if not isinstance(rows, list) or any(not isinstance(row, dict) or
                                            not isinstance(row.get("label"), str) for row in rows):
            raise LabClientError("the key service gave invalid resource rows")
    failed = answer.get("failed") or []
    if root is not None:
        lines = ["## %s end of run %s" % (_stamp(), run)]
        lines += ["delete %s %s %s: %s (%s)" % (c["method"], c["service"], c["path"], c["status"], c["label"])
                  for c in answer.get("calls") or []]
        lines += ["kept %s %s: %s" % (c["label"], c["id"], c.get("reason") or "-") for c in answer.get("kept") or []]
        lines += ["created %d, removed %d, failed %d, kept %d" % (answer.get("created", 0),
                                                                   len(answer.get("removed") or []), len(failed),
                                                                   len(answer.get("kept") or []))]
        log(root, run, lines)
        from awb import lablog

        lablog.refresh(root, root / LOG_FOLDER / (run + ".log"))
        with _directory(root / LOG_FOLDER) as fd:
            try:
                if _read(fd, _marker(root, tenant).name, 128).removesuffix("\n") == run:
                    os.unlink(_marker(root, tenant).name, dir_fd=fd)
            except FileNotFoundError:
                pass
    _event("red" if failed else "ok", run, project)
    _print("run %s ended on %s: created %d, removed %d, failed %d, kept %d" % (
        run, tenant, answer.get("created", 0), len(answer.get("removed") or []), len(failed),
        len(answer.get("kept") or [])))
    for c in answer.get("kept") or []:
        _print("  kept     %s %s: %s" % (c["label"], c["id"], c.get("reason") or "-"))
    for c in answer.get("removed") or []:
        _print("  removed  %s %s" % (c["label"], c.get("name") or ""))
    for c in failed:
        _print("  failed   %s %s: %s (%s)" % (c["label"], c.get("name") or "", _display_error(c.get("error")), c.get("delete")))
    if failed:
        _print("awb lab run end --tenant %s tries the failed ones again; the owner sees them in awb owner status"
              % tenant)
    return EXIT_LEFT if failed else EXIT_OK


@_boundary
def _status(project: str | None, root: Path | None = None) -> int:
    answer = _request({"op": "lab_run", "action": "status", "project": project}, root)
    if not answer.get("ok"):
        raise LabClientError(_service_error(answer))
    rows = answer.get("runs") or []
    kept = answer.get("kept_list") or []
    if not rows:
        _print("no open lab run and no leftovers%s" % (" of %s" % project if project else ""))
    _kept_lines(kept)
    if not rows:
        return EXIT_OK
    for r in rows:
        _print("%s  %s on %s  %s, %d min, %d calls, %d left, %d kept" % (
            r["run"], r["project"], r["tenant"], r["state"], r.get("minutes", 0), r.get("calls", 0),
            len(r.get("left") or []), r.get("kept", 0)))
        for c in r.get("left") or []:
            _print("    #%s %s %s %s%s" % (c.get("n") or "-", c["state"], c["label"], c.get("name") or "",
                                     " (%s)" % _display_error(c["error"]) if c.get("error") else ""))
    return EXIT_OK


def _kept_lines(kept: list) -> None:
    """The kept list of the project, one line each: tenant, region, kind, name, run, date."""
    if not isinstance(kept, list):
        return
    for k in kept:
        if isinstance(k, dict):
            _print("kept  %s  %s  %s  %s  %s  %s" % tuple(str(k.get(f) or "-") for f in (
                "tenant", "region", "kind", "name", "run", "date")))


def kept_main(argv: list[str]) -> int:
    """awb lab kept drop NAME: delete a kept bucket through the key service, with the delete and the proof of a run
    end (the read after the delete answers 404)."""
    from awb.cli import SafeParser

    ap = SafeParser(prog="awb lab kept", description="The buckets kept past the end of a lab run.")
    sub = ap.add_subparsers(dest="command", parser_class=SafeParser)
    d = sub.add_parser("drop", help="delete a kept bucket and prove it gone")
    d.add_argument("name")
    d.add_argument("--project", default=None, help="default: the code of this folder's project")
    try:
        args = ap.parse_args(argv)
    except SystemExit as exc:
        return exc.code if isinstance(exc.code, int) else EXIT_REFUSED
    if args.command != "drop":
        ap.print_usage(sys.stderr)
        return EXIT_REFUSED
    try:
        root = _root()
    except LabClientError:
        root = None
    try:
        project = args.project or (_code(root) if root else None)
        if not project:
            raise LabClientError("this folder names no project; give --project")
        _identifier(project, "project")
        answer = _request({"op": "lab_run", "action": "drop", "project": project, "name": args.name}, root)
        if not answer.get("ok"):
            raise LabClientError(_service_error(answer))
    except LabClientError as err:
        _print("awb lab kept: %s" % err, file=sys.stderr)
        return EXIT_REFUSED
    if answer.get("dropped"):
        _print("kept bucket %s deleted and gone (404 of the read after the delete)" % args.name)
        return EXIT_OK
    for c in answer.get("failed") or []:
        if isinstance(c, dict):
            _print("  failed   %s %s: %s" % (c.get("label"), c.get("name") or "", _display_error(c.get("error"))))
    _print("kept bucket %s is still there: awb lab kept drop %s tries again" % (args.name, args.name))
    return EXIT_LEFT
