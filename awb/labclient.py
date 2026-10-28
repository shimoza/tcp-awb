"""The lab run client of a project session (task LR): scripts call the two lab tenants through the key service.

    awb lab run start --tenant T [--note TEXT]    open a run of this folder's project (only on his word)
    awb lab run end [--tenant T]                  delete what the run made, newest first; report and close it
    awb lab run status                            the open runs of this project and their leftovers

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
notes/lab-runs/<run>.log of the project, request and answer, shortened the same way (awb/labsecrets.py).
"""
from __future__ import annotations

import argparse
import json
import os
import re
import socket
import sys
from datetime import datetime, timezone
from pathlib import Path

EXIT_OK, EXIT_REFUSED, EXIT_LEFT = 0, 2, 4
LOG_FOLDER = Path("notes") / "lab-runs"
SOCKET_ENV = "AWB_KEYS_SOCKET"
DEFAULT_SOCKET = "/run/awb-keys/cloud.sock"
TIMEOUT = 200.0
"""Seconds of one request: the key service's end of a run takes at most about 150 seconds of deletes."""
MAX_ANSWER = 8 * 1024 * 1024 + 4096


class LabClientError(Exception):
    """A lab call that could not be made: the one line of the key service or of this client."""


def _root(start: Path | None = None) -> Path:
    from awb import config

    root = config.scope_root(Path(os.path.abspath(start or Path.cwd())))
    if root is None:
        raise LabClientError("a lab run runs from the folder of a project (its SCOPE.md)")
    return root


def _code(root: Path) -> str:
    """The `code:` line of the project's SCOPE.md, else the folder's name (as awb lab run RECIPE reads it)."""
    try:
        text = (root / "SCOPE.md").read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        text = ""
    m = re.search(r"^- code: *(\S+) *$", text, re.MULTILINE)
    return m.group(1) if m else root.name


def _folder(root: Path) -> Path:
    folder = root / LOG_FOLDER
    folder.mkdir(parents=True, exist_ok=True)
    return folder


def _marker(root: Path, tenant: str) -> Path:
    return root / LOG_FOLDER / ("%s.open" % tenant)


def _open_runs(root: Path) -> dict[str, str]:
    """{tenant: run} of the markers `awb lab run start` wrote."""
    out = {}
    folder = root / LOG_FOLDER
    for f in sorted(folder.glob("*.open")) if folder.is_dir() else []:
        try:
            out[f.name[:-5]] = f.read_text(encoding="utf-8").strip()
        except OSError:
            continue
    return out


def _pick(root: Path, tenant: str | None, run: str | None) -> tuple[str, str]:
    runs = _open_runs(root)
    if tenant is None:
        if len(runs) != 1:
            raise LabClientError("name the tenant: %s" % ("no lab run is open here (awb lab run start --tenant T, "
                                                         "only on his word)" if not runs else
                                                         "runs are open on %s" % ", ".join(sorted(runs))))
        tenant = next(iter(runs))
    if run is None:
        run = runs.get(tenant)
        if not run:
            raise LabClientError("no lab run is open here on %s (awb lab run start --tenant %s, only on his word)"
                                 % (tenant, tenant))
    return tenant, run


def _stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def log(root: Path, run: str, lines: list[str]) -> None:
    """Append a block to notes/lab-runs/<run>.log, shortened as awb/labsecrets.py shortens a lab log."""
    from awb import labsecrets

    text, _counts = labsecrets.shorten("\n".join(lines) + "\n")
    with open(_folder(root) / ("%s.log" % run), "a", encoding="utf-8") as fh:
        fh.write(text + "\n")


def _pretty(value) -> str:
    if isinstance(value, str):
        return value
    return json.dumps(value, indent=1, ensure_ascii=False, sort_keys=True)


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


def _request(obj: dict, root: Path | None = None) -> dict:
    """One request on the call socket of the key service, one JSON line each way. This module speaks the socket
    itself: it is core code and imports nothing of the platform package."""
    token = _token(root)
    if token is not None:
        obj = dict(obj, token=token)
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.settimeout(TIMEOUT)
    try:
        s.connect(os.environ.get(SOCKET_ENV) or DEFAULT_SOCKET)
        s.sendall(json.dumps(obj).encode("utf-8") + b"\n")
        buf = bytearray()
        while b"\n" not in buf:
            chunk = s.recv(1 << 16)
            if not chunk:
                break
            buf += chunk
            if len(buf) > MAX_ANSWER:
                raise LabClientError("the answer of the key service is too large")
        answer = json.loads(bytes(buf).partition(b"\n")[0])
        if not isinstance(answer, dict):
            raise ValueError
        return answer
    except (FileNotFoundError, ConnectionRefusedError):
        raise LabClientError("the key service is not running") from None
    except PermissionError:
        raise LabClientError("the key service refused this user") from None
    except ValueError:
        raise LabClientError("the key service gave no readable answer") from None
    except OSError as err:
        raise LabClientError("no answer from the key service (%s)" % type(err).__name__) from None
    finally:
        s.close()


def call(method: str, service: str, path: str, body=None, params=None, region=None, *, tenant: str | None = None,
         run: str | None = None, headers: dict | None = None, scope: str | None = None, credential: str | None = None,
         start: Path | None = None):
    """(status, headers, body) of one call to the lab tenant of the open run. `body` is JSON (a dict or a list) or
    a text (an OBS body); `params` the query; `scope` a project name or id for a token of that project. A refusal of
    the key service raises LabClientError with its one line."""
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
    answer = _request(req, root)
    head = ["## %s %s %s %s" % (_stamp(), method.upper(), service, path)]
    head += ["region: %s" % region] if region else []
    head += ["query: %s" % json.dumps(query, ensure_ascii=False)] if query else []
    head += ["request headers: %s" % json.dumps(headers, ensure_ascii=False)] if headers else []
    head += ["request body:", _pretty(body)] if body is not None else []
    if not answer.get("ok"):
        log(root, run, head + ["refused: %s" % answer.get("error")])
        raise LabClientError(str(answer.get("error")))
    data = answer.get("data")
    out = data if data is not None else (answer.get("text") or "")
    lines = head + ["answer: %s%s" % (answer.get("status"), " (path %s)" % answer["path"]
                                      if answer.get("path") not in (None, path) else ""),
                    "answer headers: %s" % json.dumps(answer.get("headers") or {}, ensure_ascii=False, sort_keys=True)]
    if out not in ("", None):
        lines += ["answer body:", _pretty(out)]
    if answer.get("created"):
        lines += ["recorded: %s" % ", ".join(answer["created"])]
    log(root, run, lines)
    return answer.get("status"), answer.get("headers") or {}, out


# --------------------------------------------------------------------------- awb lab run start|end|status


def _parser(action: str) -> argparse.ArgumentParser:
    from awb.cli import SafeParser

    helps = {"start": "open a lab run of this folder's project on a lab tenant, only on his word",
             "end": "delete what the run made, newest first, report created, removed and failed, close the run",
             "status": "the open runs of this project and their leftovers"}
    ap = SafeParser(prog="awb lab run %s" % action, description=helps[action])
    if action in ("start", "end"):
        ap.add_argument("--tenant", required=action == "start", default=None,
                        help="test-10491 or test-10497 (end: default the one open run)")
    if action == "start":
        ap.add_argument("--note", default="", help="a short note of what the run tests")
    ap.add_argument("--project", default=None, help="default: the code of this folder's project")
    return ap


def run_main(argv: list[str]) -> int:
    action = argv[0] if argv else ""
    if action not in ("start", "end", "status"):
        print("awb lab run: start, end or status", file=sys.stderr)
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
        project = args.project or (_code(root) if root else None)
        if action == "status":
            return _status(project, root)
        if not project:
            raise LabClientError("this folder names no project; give --project")
        if action == "start":
            return _start(root, project, args.tenant, args.note)
        return _end(root, project, args.tenant)
    except LabClientError as err:
        print("awb lab run: %s" % err, file=sys.stderr)
        return EXIT_REFUSED


def _start(root: Path | None, project: str, tenant: str, note: str) -> int:
    answer = _request({"op": "lab_run", "action": "start", "project": project, "tenant": tenant, "note": note}, root)
    if not answer.get("ok"):
        raise LabClientError(str(answer.get("error")))
    run = answer["run"]
    if root is not None:
        _folder(root)
        _marker(root, tenant).write_text(run + "\n", encoding="utf-8")
        log(root, run, ["# lab run %s of %s on %s, opened %s" % (run, project, tenant, answer.get("started")),
                        "note: %s" % note if note else "note: -"])
    print("run %s open on %s for %s: scripts call through awb.labclient, then awb lab run end" % (run, tenant, project))
    return EXIT_OK


def _end(root: Path | None, project: str, tenant: str | None) -> int:
    run = None
    if tenant is None:
        if root is None:
            raise LabClientError("name the tenant: --tenant T")
        tenant, run = _pick(root, None, None)
    elif root is not None:
        run = _open_runs(root).get(tenant)
    answer = _request({"op": "lab_run", "action": "end", "project": project, "tenant": tenant, "run": run}, root)
    if not answer.get("ok"):
        raise LabClientError(str(answer.get("error")))
    run = answer["run"]
    failed = answer.get("failed") or []
    if root is not None:
        lines = ["## %s end of run %s" % (_stamp(), run)]
        lines += ["delete %s %s %s: %s (%s)" % (c["method"], c["service"], c["path"], c["status"], c["label"])
                  for c in answer.get("calls") or []]
        lines += ["created %d, removed %d, failed %d" % (answer.get("created", 0), len(answer.get("removed") or []),
                                                          len(failed))]
        log(root, run, lines)
        try:
            _marker(root, tenant).unlink()
        except OSError:
            pass
    print("run %s ended on %s: created %d, removed %d, failed %d" % (run, tenant, answer.get("created", 0),
                                                                      len(answer.get("removed") or []), len(failed)))
    for c in answer.get("removed") or []:
        print("  removed  %s %s" % (c["label"], c.get("name") or ""))
    for c in failed:
        print("  failed   %s %s: %s (%s)" % (c["label"], c.get("name") or "", c.get("error"), c.get("delete")))
    if failed:
        print("awb lab run end --tenant %s tries the failed ones again; the owner sees them in awb owner status"
              % tenant)
    return EXIT_LEFT if failed else EXIT_OK


def _status(project: str | None, root: Path | None = None) -> int:
    answer = _request({"op": "lab_run", "action": "status", "project": project}, root)
    if not answer.get("ok"):
        raise LabClientError(str(answer.get("error")))
    rows = answer.get("runs") or []
    if not rows:
        print("no open lab run and no leftovers%s" % (" of %s" % project if project else ""))
        return EXIT_OK
    for r in rows:
        print("%s  %s on %s  %s, %d min, %d calls, %d left" % (r["run"], r["project"], r["tenant"], r["state"],
                                                                r.get("minutes", 0), r.get("calls", 0),
                                                                len(r.get("left") or [])))
        for c in r.get("left") or []:
            print("    %s %s %s%s" % (c["state"], c["label"], c.get("name") or "",
                                     " (%s)" % c["error"] if c.get("error") else ""))
    return EXIT_OK
