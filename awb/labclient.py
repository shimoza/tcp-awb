"""The lab run client of a project session (task LR): scripts call the two lab tenants through the key service.

    awb lab run start --tenant T [--note TEXT]    open a run of this folder's project (only on his word)
    awb lab run keep ITEM --reason TEXT [--tenant T]   keep one recorded create (its number or id) past the end
    awb lab run end [--tenant T]                  delete what the run made, newest first; report and close it
    awb lab run status                            the open runs of this project and their leftovers

A change to a stand the project holds (task LK) stays: `call(..., keep="why")` keeps the create it makes (a rule of
a listed security group), `awb lab run keep` keeps one already recorded. The key service allows it on a lab tenant
only, for a project whose RESOURCES.md lists a live row on that tenant or a live parent the create names; the end
skips it and reports it as kept, `awb owner status` shows it, and this client writes its row in RESOURCES.md as live
(KEEP_NOTE). A PUT, PATCH or tag action on an existing resource records nothing, so the end never undoes it. A delete
of a resource listed live in RESOURCES.md needs `call(..., delete_listed=True)`, then its row is marked deleted.

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

A large file goes to or from OBS as a job of the key service (task LO, awb/tcp/labobs.py), not through `call`:

    awb lab obs put FILE obs://BUCKET/KEY [--tenant T]    labclient.obs_put(file, url) -> {"job": ..., ...}
    awb lab obs get obs://BUCKET/KEY FILE [--tenant T]    labclient.obs_get(url, file)
    awb lab obs status [JOB]                              labclient.obs_status(job=None) -> [job, ...]
    awb lab obs abort JOB                                 labclient.obs_abort(job); obs_wait(job) waits for its end

The file goes to the service as an open descriptor (SCM_RIGHTS) with one of the run log, never as a path; a get
writes only inside the project folder or the session scratch folder (SCRATCH). The same put or get once more resumes
an interrupted job.
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
KEEP_NOTE = "kept from lab run %s on %s: %s"
"""The note of the RESOURCES.md row of a kept create: run, tenant, reason."""
SCRATCH = "/tmp/claude-%d"
"""The session scratch folder of a uid, where a lab obs get may write beside the project folder."""
OBS_ENDED = ("done", "failed", "interrupted", "aborted")


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


def _request(obj: dict, root: Path | None = None, fds: tuple[int, ...] = ()) -> dict:
    """One request on the call socket of the key service, one JSON line each way; `fds` go with the line as
    SCM_RIGHTS (a lab obs put or get). This module speaks the socket itself: it is core code and imports nothing of
    the platform package."""
    token = _token(root)
    if token is not None:
        obj = dict(obj, token=token)
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.settimeout(TIMEOUT)
    try:
        s.connect(os.environ.get(SOCKET_ENV) or DEFAULT_SOCKET)
        line = json.dumps(obj).encode("utf-8") + b"\n"
        if fds:
            sent = socket.send_fds(s, [line], list(fds))
            s.sendall(line[sent:])
        else:
            s.sendall(line)
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
    if answer.get("tagged"):
        lines += ["project tag: %s" % answer["tagged"]]
    for k in answer.get("kept") or []:
        lines += ["kept: %s %s (%s), row %s in RESOURCES.md" % (k["label"], k["id"], k["reason"],
                                                                  keep_row(root, run, tenant, k))]
    log(root, run, lines)
    return answer.get("status"), answer.get("headers") or {}, out


def keep_row(root: Path, run: str, tenant: str, k: dict) -> str:
    """The handle of the live row in RESOURCES.md of a kept create: the row there with its id, else a new one
    (kind-N after the highest N of its kind)."""
    from awb import projects

    path = root / "RESOURCES.md"
    if not path.exists():
        path.write_text(projects._resources(_code(root)), encoding="utf-8")
    text = path.read_text(encoding="utf-8")
    rid = str(k.get("id") or "")
    handles = []
    for line in text.splitlines():
        cells = [c.strip().strip("`") for c in line.strip().strip("|").split("|")] if line.strip().startswith("|") \
            else []
        if cells and rid and rid in cells:
            return cells[0]
        if cells:
            handles.append(cells[0])
    kind = re.sub(r"[^a-z0-9]+", "-", str(k.get("label") or "resource").split()[-1].lower()).strip("-") or "resource"
    kind = kind[:-1] if kind.endswith("s") and len(kind) > 3 else kind
    n = 1 + max([int(h.rsplit("-", 1)[1]) for h in handles if re.fullmatch(re.escape(kind) + r"-\d+", h)] + [0])
    handle = "%s-%d" % (kind, n)
    note = (KEEP_NOTE % (run, tenant, k.get("reason") or "-")).replace("|", "/")
    row = "| %s | %s | %s | %s | - | - | live | %s |\n" % (handle, rid or "unknown", k.get("label") or "-",
                                                         k.get("region") or "-", note)
    if text and not text.endswith("\n"):
        text += "\n"
    tmp = path.with_name(".RESOURCES.md.tmp")
    tmp.write_text(text + row, encoding="utf-8")
    os.replace(tmp, path)
    return handle


# --------------------------------------------------------------------------- large objects (LO): awb lab obs


def _object(url: str) -> tuple[str, str]:
    m = re.match(r"^obs://([^/]+)/(.+)$", url or "")
    if not m:
        raise LabClientError("an object reads obs://BUCKET/KEY")
    return m.group(1), m.group(2)


def _transfer(action: str, path: str, url: str, tenant: str | None, run: str | None, region: str | None,
              start: Path | None, vendor_hash: str | None = None) -> dict:
    root = _root(start)
    tenant, run = _pick(root, tenant, run)
    bucket, key = _object(url) if action != "fetch" else (None, None)
    where = os.path.abspath(path)
    made = False
    try:
        if action == "put":
            fd = os.open(where, os.O_RDONLY | os.O_CLOEXEC)
        else:
            inside = [str(root.resolve()), os.path.realpath(SCRATCH % os.getuid())]
            real = os.path.realpath(where)
            if not any(real.startswith(f.rstrip("/") + "/") for f in inside):
                raise LabClientError("refused: a %s writes only inside the project folder or the session scratch "
                                     "folder (%s)" % (action, SCRATCH % os.getuid()))
            made = not os.path.lexists(where)
            fd = os.open(where, os.O_RDWR | os.O_CREAT | os.O_CLOEXEC, 0o666)
    except PermissionError:
        raise LabClientError("refused: this user cannot %s %s; the key service takes only a file the session itself "
                             "opens" % ("read" if action == "put" else "write", path)) from None
    except FileNotFoundError:
        raise LabClientError("no such file: %s" % path) from None
    except IsADirectoryError:
        raise LabClientError("refused: %s is a folder" % path) from None
    try:
        log_fd = os.open(_folder(root) / ("%s.log" % run), os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_CLOEXEC,
                         0o666)
        try:
            req = {"op": "lab_obs", "action": action, "tenant": tenant, "run": run, "project": _code(root),
                   "bucket": bucket, "key": key, "region": region}
            if action == "fetch":
                req = dict(req, url=url, hash=vendor_hash)
            answer = _request(req, root, (fd, log_fd))
        finally:
            os.close(log_fd)
    finally:
        os.close(fd)
    head = ["## %s obs %s %s %s" % (_stamp(), action, path if action == "put" else url,
                                    url if action == "put" else path)]
    if not answer.get("ok"):
        if made:
            try:
                os.unlink(where)
            except OSError:
                pass
        log(root, run, head + ["refused: %s" % answer.get("error")])
        raise LabClientError(str(answer.get("error")))
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


def obs_status(job: str | None = None, *, start: Path | None = None) -> list[dict]:
    """The lab obs jobs of the project (or the one named): id, action, bucket, key, size, done, rate (bytes per
    second), state (hashing, running, verifying, done, failed, interrupted, aborted), error, sha256."""
    root = _root(start)
    answer = _request({"op": "lab_obs", "action": "status", "project": _code(root), "job": job}, root)
    if not answer.get("ok"):
        raise LabClientError(str(answer.get("error")))
    return answer.get("jobs") or []


def obs_abort(job: str, *, start: Path | None = None) -> dict:
    """Stop a job; a put also aborts its multipart upload."""
    root = _root(start)
    answer = _request({"op": "lab_obs", "action": "abort", "project": _code(root), "job": job}, root)
    if not answer.get("ok"):
        raise LabClientError(str(answer.get("error")))
    return answer.get("job") or {}


def obs_wait(job: str, *, every: float = 15.0, start: Path | None = None) -> dict:
    """The job once it has ended (done, failed, interrupted or aborted), asked every `every` seconds."""
    import time

    while True:
        rows = obs_status(job, start=start)
        if not rows:
            raise LabClientError("no lab obs job %s" % job)
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
             "abort": "stop a job; a put also aborts its multipart upload"}
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
    elif action == "status":
        ap.add_argument("job", metavar="JOB", nargs="?", default=None)
    else:
        ap.add_argument("job", metavar="JOB")
    if action in ("put", "get", "fetch"):
        ap.add_argument("--tenant", default=None, help="test-10491, test-10497 or a granted customer tenant in write "
                                                       "mode (default: the one open run)")
    return ap


def _size(n) -> str:
    return "%.1f GB" % (float(n or 0) / 1e9) if (n or 0) >= 1e8 else "%d bytes" % int(n or 0)


def obs_main(argv: list[str]) -> int:
    action = argv[0] if argv else ""
    if action not in ("put", "get", "fetch", "hosts", "status", "abort"):
        print("awb lab obs: put, get, fetch, hosts, status or abort", file=sys.stderr)
        return EXIT_REFUSED
    try:
        args = _obs_parser(action).parse_args(argv[1:])
    except SystemExit as exc:
        return exc.code if isinstance(exc.code, int) else EXIT_REFUSED
    try:
        if action == "hosts":
            hosts, portals = obs_hosts()
            for h in hosts:
                print(h)
            for h in portals:
                print("%s  (portal: refused, hand the download to the owner)" % h)
            return EXIT_OK
        if action in ("put", "get", "fetch"):
            if action == "put":
                a = obs_put(args.file, args.url, tenant=args.tenant)
            elif action == "fetch":
                a = obs_fetch(args.url, args.file, sha256=args.sha256, sha1=args.sha1, tenant=args.tenant)
            else:
                a = obs_get(args.url, args.file, tenant=args.tenant)
            print("job %s %s: %s of %s%s; awb lab obs status %s" % (
                a["job"], "resumed" if a.get("resumed") else "started", action, _size(a.get("size")),
                " in %d parts" % a["parts"] if a.get("parts") else "", a["job"]))
            return EXIT_OK
        if action == "abort":
            j = obs_abort(args.job)
            print("job %s %s" % (j.get("id"), j.get("state")))
            return EXIT_OK
        rows = obs_status(args.job)
        if not rows:
            print("no lab obs job%s" % (" %s" % args.job if args.job else ""))
        for j in rows:
            pct = 100.0 * (j.get("done") or 0) / j["size"] if j.get("size") else 0.0
            what = "%s -> %s" % (j.get("url"), j.get("file_name")) if j["action"] == "fetch" else \
                "obs://%s/%s" % (j["bucket"], j["key"])
            print("%s  %s %s  %s  %s of %s (%.0f%%)  %.1f MB/s%s" % (
                j["id"], j["action"], what, j["state"], _size(j.get("done")), _size(j.get("size")),
                pct, (j.get("rate") or 0) / 1e6, "  %s" % j["error"] if j.get("error") else ""))
            if j.get("state") == "done" and j.get("sha256"):
                print("    sha256 %s, %s s" % (j["sha256"], j.get("seconds")))
            if j["action"] == "fetch" and j.get("sha1"):
                print("    sha1 %s, vendor hash: %s" % (j["sha1"], j.get("vendor_hash") or "-"))
        return EXIT_OK
    except LabClientError as err:
        print("awb lab obs: %s" % err, file=sys.stderr)
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
    if action == "keep":
        ap.add_argument("item", help="the number of the recorded create (awb lab run status) or its id")
        ap.add_argument("--reason", required=True, help="why it stays, one line")
    if action in ("start", "end", "keep"):
        ap.add_argument("--tenant", required=action == "start", default=None,
                        help="test-10491, test-10497 or a granted customer tenant in write mode (end: default the "
                             "one open run)")
    if action == "start":
        ap.add_argument("--note", default="", help="a short note of what the run tests")
    ap.add_argument("--project", default=None, help="default: the code of this folder's project")
    return ap


def run_main(argv: list[str]) -> int:
    action = argv[0] if argv else ""
    if action not in ("start", "keep", "end", "status"):
        print("awb lab run: start, keep, end or status", file=sys.stderr)
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
        if action == "keep":
            return _keep(root, project, args.tenant, args.item, args.reason)
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
    print("run a script with %s (plain python3 has no awb module)" % (sys.executable or "/opt/tcp-awb/venv/bin/python3"))
    return EXIT_OK


def _keep(root: Path | None, project: str, tenant: str | None, item: str, reason: str) -> int:
    if root is None:
        raise LabClientError("keep runs from the project folder: its RESOURCES.md gets the row")
    tenant, run = _pick(root, tenant, None)
    answer = _request({"op": "lab_run", "action": "keep", "project": project, "tenant": tenant, "run": run,
                       "item": item, "reason": reason}, root)
    if not answer.get("ok"):
        raise LabClientError(str(answer.get("error")))
    for k in answer.get("kept") or []:
        handle = keep_row(root, run, tenant, k)
        log(root, run, ["## %s keep %s %s (%s), row %s in RESOURCES.md" % (_stamp(), k["label"], k["id"],
                                                                         k["reason"], handle)])
        print("kept %s %s as %s: the end of run %s leaves it, RESOURCES.md lists it live" % (k["label"], k["id"],
                                                                                          handle, run))
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
        lines += ["kept %s %s: %s" % (c["label"], c["id"], c.get("reason") or "-") for c in answer.get("kept") or []]
        lines += ["created %d, removed %d, failed %d, kept %d" % (answer.get("created", 0),
                                                                   len(answer.get("removed") or []), len(failed),
                                                                   len(answer.get("kept") or []))]
        log(root, run, lines)
        try:
            _marker(root, tenant).unlink()
        except OSError:
            pass
    print("run %s ended on %s: created %d, removed %d, failed %d, kept %d" % (
        run, tenant, answer.get("created", 0), len(answer.get("removed") or []), len(failed),
        len(answer.get("kept") or [])))
    for c in answer.get("kept") or []:
        print("  kept     %s %s: %s" % (c["label"], c["id"], c.get("reason") or "-"))
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
        print("%s  %s on %s  %s, %d min, %d calls, %d left, %d kept" % (
            r["run"], r["project"], r["tenant"], r["state"], r.get("minutes", 0), r.get("calls", 0),
            len(r.get("left") or []), r.get("kept", 0)))
        for c in r.get("left") or []:
            print("    #%s %s %s %s%s" % (c.get("n") or "-", c["state"], c["label"], c.get("name") or "",
                                     " (%s)" % c["error"] if c.get("error") else ""))
    return EXIT_OK
