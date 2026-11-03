"""Large objects to and from OBS in a lab run (task LO, B-030): the key service moves the bytes itself.

    awb lab obs put FILE obs://BUCKET/KEY [--tenant T]     a file of the session into a bucket of its project
    awb lab obs get obs://BUCKET/KEY FILE [--tenant T]     an object into the project folder or the scratch folder
    awb lab obs status [JOB]                               bytes done, total, rate and state of the project's jobs
    awb lab obs abort JOB                                  stops a job; a put also aborts its multipart upload

A lab call (awb/tcp/labadmin.py) carries at most MAX_BODY bytes of JSON or text and its client waits 200 seconds; the
egress filter keeps the work user away from OBS; the temporary keys of an answer come shortened. So the op
`lab_obs` of the call socket has the key service run the transfer as a background job of its own: the session never
holds a key and gets no new egress. Its rules are those of a lab call: an open run of the calling project on
test-10491 or test-10497 (never a read-only tenant), a bucket named after the project (<code>-...), never an exchange
bucket, signed with the run's temporary credential of the admin token only, the way labadmin signs an OBS call.

The file never reaches the service by its path. The client opens it as the work user and passes the open descriptor
over the Unix socket (SCM_RIGHTS), with a second one of the run log notes/lab-runs/<run>.log opened for append: a
file the work user cannot read cannot be put; the service gets no new read right. A put takes a descriptor open
for reading of a regular file. A get takes one open for reading and writing of a regular file of the calling user
inside the project folder (<projects_root>/<code>) or the session scratch folder (SCRATCH, /tmp/claude-<uid>), and
the disk under it must hold the object plus DISK_RESERVE.

A put hashes the file (SHA-256), starts a multipart upload with the hash as the object metadata x-obs-meta-sha256,
sends the parts in order (the part size follows the limits of OBS: at most MAX_PARTS parts of PART_MIN to PART_MAX,
PART_SIZE or more, so 128 GB, the direct import limit of IMS, goes in 1908 parts), each retried after a broken
connection with the pauses of PAUSES, completes it and checks the object: its size equals the file's and its
x-obs-meta-sha256 the hash. A get streams the object (Range from the bytes done on a retry or a resume, If-Match its
ETag), hashes what it wrote and compares it with the object's x-obs-meta-sha256 when it has one. A file or an object
over lab_obs_max_gb of keys.conf (default DEFAULT_MAX_GB) is refused with the limit named.

The job record is <vault>/lab-runs/obs/<job>.json (mode 600), saved after every part: state hashing, running,
verifying, then done, failed, interrupted or aborted, bytes done, total, rate, the hash and the error. A put of the
same file (device, inode, size and modification time) to the same object in the same run resumes an interrupted or
failed job with the parts it has; a get of the same object into the same file goes on from its bytes done. A lock, a
stop or a reload of the key service interrupts every job at once (the old process saves the state and writes the
line of the run log); a job whose process went away is shown and saved as interrupted. A job never vanishes.

The multipart upload and the object it makes are recorded in the run like every create (`obs object` when the object
was not there before, then `obs multipart`, whose delete aborts the upload). `awb lab run end` stops the jobs of the
run (state aborted), then in each pass aborts every open multipart upload of the run's buckets before the deletes:
a bucket delete answers 409 BucketNotEmpty while an incomplete upload is there, even when its listing shows no
object. At the end of a job the service appends bucket, object, size, hash, duration and state to the run log
through the descriptor it got; no key, no token and no upload id goes there.
"""
from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import secrets as _secrets
import stat
import tempfile
import threading
import time
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from pathlib import Path
from xml.sax.saxutils import escape

from awb import labruns, labsecrets

SETTING = "lab_obs_max_gb"
DEFAULT_MAX_GB = 200
GB = 1000 ** 3
MAX_PARTS = 10000
PART_MIN = 100 * 1024
PART_MAX = 5 * 1024 ** 3
PART_SIZE = 64 * 1024 * 1024
"""The part of a smaller object; a larger one takes the size that keeps it within MAX_PARTS, in whole MiB."""
CHUNK = 1 << 20
PAUSES = (2.0, 10.0, 30.0, 60.0)
"""The pauses between the tries of a part or of the rest of a get: five tries, a try that moved bytes starts over."""
PART_TIMEOUT = 300.0
DISK_RESERVE = 256 * 1024 * 1024
SAVE_EVERY = 64 * 1024 * 1024
STOP_WAIT = 10.0
MAX_LIVE = 4
SCRATCH = "/tmp/claude-%d"
"""The session scratch folder of the work user (the client's scratch place), by its uid."""
ACTIVE = ("hashing", "running", "verifying")
RESUMABLE = ("interrupted", "failed")
ENDED = ("done", "failed", "interrupted", "aborted")
JOB_RE = re.compile(r"^obs-\d{8}-\d{6}-[a-z0-9]{4}$")
_BUCKET_RE = re.compile(r"^[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]$")
_KEY_RE = re.compile(r"^[A-Za-z0-9_.=~@+-][A-Za-z0-9_.:/=~@+-]{0,799}$")
RESUME_LINE = "%s the same file again to resume"
LIMIT_LINE = "refused: the %s is %.1f GB, over the limit of %s GB (lab_obs_max_gb in keys.conf)"


class _Stopped(Exception):
    """The job was told to stop: a lock, a stop or a reload of the service, an abort or the end of the run."""


class _Failed(Exception):
    """The job cannot go on."""


class _Broken(Exception):
    """The tries of a part or of a get are used up: the job is interrupted and can be resumed."""


class _Gone(Exception):
    """The multipart upload is no longer there (aborted)."""


class Live:
    """A job running in this process: its descriptors, its thread and the event that stops it."""

    def __init__(self, job_id: str, fd: int, log_fd: int):
        self.id, self.fd, self.log_fd = job_id, fd, log_fd
        self.stop = threading.Event()
        self.why = ""
        self.end_state = "interrupted"
        self.thread: threading.Thread | None = None
        self.t0 = time.monotonic()
        self.done0 = 0


class Jobs:
    """The jobs running in this key service process, in memory; their records are on disk."""

    def __init__(self):
        self.lock = threading.RLock()
        self.live: dict[str, Live] = {}

    def stop_all(self, why: str, state: str = "interrupted", wait: float | None = None, only=None) -> list[str]:
        """Tell every running job (or those of `only`) to stop, wait for them up to `wait` seconds (STOP_WAIT),
        return their ids. A job that does not stop in time is saved with the state by its next step."""
        wait = STOP_WAIT if wait is None else wait
        with self.lock:
            hit = [lv for i, lv in self.live.items() if only is None or i in only]
            for lv in hit:
                lv.why, lv.end_state = why, state
                lv.stop.set()
        end = time.monotonic() + wait
        for lv in hit:
            if lv.thread is not None and lv.thread is not threading.current_thread():
                lv.thread.join(max(0.0, end - time.monotonic()))
        return [lv.id for lv in hit]


# --------------------------------------------------------------------------- the plan and the records


def plan(size: int) -> tuple[int, int]:
    """(part size, parts) of an object of `size` bytes within the limits of OBS."""
    part = max(PART_SIZE, -(-size // MAX_PARTS))
    if part > PART_SIZE:
        part = -(-part // (1 << 20)) * (1 << 20)
    part = max(part, PART_MIN)
    if part > PART_MAX:
        raise _Failed("refused: %d bytes do not fit into %d parts of at most 5 GB" % (size, MAX_PARTS))
    return part, max(1, -(-size // part))


def max_bytes(settings: dict) -> tuple[int, str]:
    """(bytes, the limit as written) of lab_obs_max_gb of keys.conf, the default without the line."""
    value = str(settings.get(SETTING) or DEFAULT_MAX_GB) if isinstance(settings, dict) else str(DEFAULT_MAX_GB)
    if not parse_limit(value):
        value = str(DEFAULT_MAX_GB)
    return int(float(value) * GB), value


def parse_limit(value: str) -> bool:
    """True for a limit keys.conf takes: a positive number of GB."""
    return bool(re.match(r"^(?:\d{1,6}(?:\.\d{1,3})?)$", value.strip())) and float(value) > 0


def jobs_dir(p) -> Path:
    return labruns.runs_dir(p) / "obs"


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _new_id() -> str:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    return "obs-%s-%s" % (stamp, "".join(_secrets.choice("abcdefghijkmnpqrstuvwxyz23456789") for _ in range(4)))


def save_job(p, job: dict) -> None:
    folder = jobs_dir(p)
    folder.mkdir(parents=True, exist_ok=True)
    os.chmod(folder, 0o700)
    fd, tmp = tempfile.mkstemp(prefix=".job.", dir=folder)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(job, fh, indent=1, sort_keys=True)
        os.chmod(tmp, 0o600)
        os.replace(tmp, folder / ("%s.json" % job["id"]))
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise


def load_jobs(p) -> list[dict]:
    out = []
    try:
        names = sorted(os.listdir(jobs_dir(p)))
    except OSError:
        return []
    for name in names:
        if not name.endswith(".json") or not JOB_RE.match(name[:-5]):
            continue
        try:
            job = json.loads((jobs_dir(p) / name).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if isinstance(job, dict) and job.get("id") == name[:-5]:
            out.append(job)
    return out


def _alive(pid) -> bool:
    if not isinstance(pid, int) or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return False
    return True


def _settle(svc, p, job: dict) -> dict:
    """A job record as it stands: one in an active state that no process runs any longer is saved as interrupted."""
    with svc.obs_jobs.lock:
        if job.get("state") in ACTIVE and job["id"] not in svc.obs_jobs.live and \
                (job.get("pid") == os.getpid() or not _alive(job.get("pid"))):
            job["state"] = "interrupted"
            job["error"] = "the key service restarted during the job: %s" % (
                RESUME_LINE % ("put" if job.get("action") == "put" else "get"))
            job["ended"] = _now()
            save_job(p, job)
    return job


def row(job: dict) -> dict:
    """What a session sees of a job: never an upload id, a key or a path of the service."""
    return {k: job.get(k) for k in ("id", "action", "run", "tenant", "bucket", "key", "size", "done", "state",
                                    "rate", "error", "sha256", "started", "ended", "seconds", "parts", "part_size")} \
        | {"parts_done": len(job.get("etags") or {})}


# --------------------------------------------------------------------------- the checks of a request


def _refused(msg: str):
    from awb.tcp import keys

    return keys.Refused(msg)


def _target(svc, req: dict, project: str) -> tuple[str, str]:
    bucket, key = req.get("bucket"), req.get("key")
    if not isinstance(bucket, str) or not _BUCKET_RE.match(bucket):
        raise _refused("a bucket name reads like %s-lab-1" % project)
    if bucket in svc.exchange_buckets():
        raise _refused("the exchange buckets are reached through awb xchg only")
    if not bucket.startswith(project + "-"):
        raise _refused("refused: a lab obs transfer reaches only a bucket named after the project (%s-...)" % project)
    if not isinstance(key, str) or not _KEY_RE.match(key) or ".." in key or "//" in key or key.endswith("/"):
        raise _refused("an object key is plain: images/disk.vhdx")
    return bucket, key


def _fd_path(fd: int) -> str:
    try:
        return os.readlink("/proc/self/fd/%d" % fd)
    except OSError:
        return ""


def _inside(path: str, folder: str) -> bool:
    if not path.startswith("/") or not folder:
        return False
    folder = os.path.realpath(folder)
    try:
        return os.path.commonpath([path, folder]) == folder and path != folder
    except ValueError:
        return False


def _project_folder(p, project: str) -> str:
    root = getattr(p, "projects_root", None)
    return os.path.realpath(os.path.join(str(root), project)) if root else ""


def _file_fd(fds: list[int], action: str, uid: int, p, project: str) -> tuple[int, os.stat_result]:
    """The descriptor of the file: open for reading (a put) or for reading and writing inside the project folder or
    the scratch folder, of the caller (a get), always a regular file."""
    if not fds:
        raise _refused("refused: a lab obs %s passes the open file over the socket (SCM_RIGHTS), never a path"
                       % action)
    fd = fds[0]
    st = os.fstat(fd)
    if not stat.S_ISREG(st.st_mode):
        raise _refused("refused: a lab obs %s takes a regular file" % action)
    mode = fcntl.fcntl(fd, fcntl.F_GETFL) & os.O_ACCMODE
    if action == "put":
        if mode not in (os.O_RDONLY, os.O_RDWR):
            raise _refused("refused: the file of a put is not open for reading")
        return fd, st
    if mode != os.O_RDWR:
        raise _refused("refused: the file of a get is open for reading and writing")
    where = _fd_path(fd)
    if not (_inside(where, _project_folder(p, project)) or _inside(where, SCRATCH % uid)):
        raise _refused("refused: a get writes only inside the project folder or the session scratch folder")
    return fd, st


def _log_fd(fds: list[int], p, project: str, run: str) -> int:
    """The descriptor of the run log notes/lab-runs/<run>.log of the project, open for append."""
    if len(fds) < 2:
        raise _refused("refused: a lab obs transfer passes the run log notes/lab-runs/%s.log too" % run)
    fd = fds[1]
    flags = fcntl.fcntl(fd, fcntl.F_GETFL)
    want = os.path.join(_project_folder(p, project), "notes", "lab-runs", "%s.log" % run)
    if not stat.S_ISREG(os.fstat(fd).st_mode) or not flags & os.O_APPEND or \
            flags & os.O_ACCMODE == os.O_RDONLY or _fd_path(fd) != want:
        raise _refused("refused: the second descriptor is not the run log notes/lab-runs/%s.log of %s, open for "
                       "append" % (run, project))
    return fd


def _identity(st: os.stat_result) -> dict:
    return {"dev": st.st_dev, "ino": st.st_ino, "size": st.st_size, "mtime_ns": st.st_mtime_ns}


# --------------------------------------------------------------------------- the requests to OBS


def _stream(method: str, url: str, headers: dict, body=None, timeout: float = PART_TIMEOUT):
    """(status, headers, response) of an OBS call whose body or answer is large: `body` an object read in blocks,
    the answer read by the caller from `response`, which the caller closes."""
    from awb.tcp import labadmin

    req = urllib.request.Request(url, data=body, method=method)
    for k, v in headers.items():
        req.add_header(k, v)
    try:
        resp = urllib.request.urlopen(req, timeout=timeout)
        return resp.status, dict(resp.headers), resp
    except urllib.error.HTTPError as err:
        return err.code, dict(err.headers or {}), err
    except (urllib.error.URLError, OSError) as err:
        raise labadmin.LabAdminError("no answer from obs (%s)" % type(err).__name__) from None


class _Reader:
    """`length` bytes of the file at `offset` for the body of one part, read with pread; a stop cuts the part."""

    def __init__(self, fd: int, offset: int, length: int, stop: threading.Event):
        self.fd, self.offset, self.length, self.stop, self.pos = fd, offset, length, stop, 0

    def read(self, n: int = -1) -> bytes:
        if self.stop.is_set():
            raise _Stopped()
        left = self.length - self.pos
        n = left if n is None or n < 0 else min(n, left)
        if n <= 0:
            return b""
        data = os.pread(self.fd, min(n, CHUNK), self.offset + self.pos)
        if not data:
            raise _Failed("the file ended before its size: it changed during the transfer")
        self.pos += len(data)
        return data


class _Obs:
    """The OBS calls of one job: signed with the run's temporary credential of the admin token (labadmin._Auth), the
    admin login read again for every new credential, so a lock of the service stops the job."""

    def __init__(self, svc, job: dict):
        self.svc, self.job = svc, job

    def _auth(self):
        from awb.tcp import labadmin

        _tenant, login = labadmin._tenant(self.svc, self.job["tenant"])
        return labadmin._Auth(self.svc, self.job["tenant"], login, self.job["region"])

    def drop(self) -> None:
        self.svc.lab.drop((self.job["tenant"], "obs@%s" % self.job["region"]))

    def signed(self, method: str, path: str, query: dict, headers: dict) -> tuple[str, dict]:
        from awb import obs
        from awb.tcp import cloud

        ak, sk, st = self._auth().credential()
        client, key, sub, url = cloud.Client(obs.Keys(ak, sk), self.job["region"],
                                             endpoint=self.svc.endpoint).obs_call(path, query)
        hdrs = dict(headers)
        hdrs["x-obs-security-token"] = st
        client.sign(method, key, hdrs, sub)
        return url, hdrs

    def small(self, method: str, path: str, query: dict | None = None, headers: dict | None = None,
              data: bytes | None = None) -> tuple[int, dict, bytes]:
        """One call with a small body and answer; a 403 of a credential that expired takes a new one once."""
        from awb.tcp import labadmin

        for attempt in (0, 1):
            url, hdrs = self.signed(method, path, dict(query or {}), dict(headers or {}))
            status, rh, raw = labadmin._http(method, url, hdrs, data)
            if status in (401, 403) and attempt == 0:
                self.drop()
                continue
            return status, rh, raw
        return status, rh, raw


def _code(raw: bytes) -> str:
    from awb import obs

    code = obs.xml_error(raw[:4096])[0]
    return code if re.match(r"^[A-Za-z0-9._]{1,40}$", code or "") else ""


def _xml(raw: bytes):
    from awb import obs

    if not raw or obs._doctype(raw):
        return None
    try:
        return ET.fromstring(raw)
    except ET.ParseError:
        return None


def _field(el, *names: str) -> str:
    from awb import obs

    for name in names:
        el = obs.xml_child(el, name) if el is not None else None
    return obs.xml_text(el).strip()


def _lower(headers: dict) -> dict:
    return {str(k).lower(): v for k, v in (headers or {}).items()}


def _path(job: dict) -> str:
    return "/%s/%s" % (job["bucket"], job["key"])


def list_uploads(o: _Obs, bucket: str) -> list[tuple[str, str]]:
    """(key, upload id) of every incomplete multipart upload of the bucket, at most 20 pages."""
    from awb import obs

    out, query = [], {"uploads": ""}
    for _ in range(20):
        status, _h, raw = o.small("GET", "/" + bucket, query)
        if status == 404:
            return out
        root = _xml(raw)
        if status != 200 or root is None:
            raise _Failed("the multipart uploads of the bucket cannot be listed: HTTP %d %s" % (status, _code(raw)))
        for el in root:
            if obs.xml_local(el.tag) == "Upload":
                key, uid = _field(el, "Key"), _field(el, "UploadId")
                if key and uid:
                    out.append((key, uid))
        if _field(root, "IsTruncated").lower() != "true":
            return out
        km, um = _field(root, "NextKeyMarker"), _field(root, "NextUploadIdMarker")
        if not km and not um:
            return out
        query = {"uploads": "", "key-marker": km, "upload-id-marker": um}
    return out


# --------------------------------------------------------------------------- the job thread


def _save(svc, p, live: Live, job: dict) -> None:
    """Save the record; after a stop an active state becomes the state of the stop with its reason."""
    with svc.obs_jobs.lock:
        if live.stop.is_set() and job.get("state") in ACTIVE:
            job["state"], job["error"], job["ended"] = live.end_state, live.why, _now()
        elapsed = time.monotonic() - live.t0
        if job.get("state") not in ("hashing",) and elapsed > 0:
            job["rate"] = int(max(0, int(job.get("done") or 0) - live.done0) / elapsed)
        save_job(p, job)


def _pause(live: Live, n: int) -> None:
    if live.stop.wait(PAUSES[min(n, len(PAUSES) - 1)]):
        raise _Stopped()


def _record(svc, p, job: dict, made: list[dict]) -> None:
    from awb.tcp import labadmin

    with svc.lab.lock:
        run = labruns.open_run(p, job["project"], job["tenant"])
        if run is None or run["id"] != job["run"]:
            raise _Stopped()
        n = len(run.get("created", []))
        for i, m in enumerate(made, start=1):
            run.setdefault("created", []).append(dict(m, n=n + i, state="live", at=labadmin._now()))
        labruns.save(p, run)


def _upload_done(svc, p, job: dict, upload_id: str) -> None:
    """The recorded multipart upload of the run is gone: completed or aborted."""
    with svc.lab.lock:
        run = next((r for r in labruns.load_runs(p) if r.get("id") == job["run"]), None)
        if run is None:
            return
        for c in run.get("created", []):
            d = c.get("delete") or {}
            if c.get("label") == "obs multipart" and d.get("path") == _path(job) and \
                    (d.get("query") or {}).get("uploadId") == upload_id and c.get("state") in ("live", "failed"):
                c["state"] = "removed"
        labruns.save(p, run)


def _hash(svc, p, live: Live, job: dict) -> str:
    h = hashlib.sha256()
    done, last = 0, 0
    while done < job["size"]:
        if live.stop.is_set():
            raise _Stopped()
        data = os.pread(live.fd, min(8 * CHUNK, job["size"] - done), done)
        if not data:
            raise _Failed("the file ended before its size: it changed during the transfer")
        h.update(data)
        done += len(data)
        if done - last >= 16 * SAVE_EVERY:
            job["done"], last = done, done
            _save(svc, p, live, job)
    return h.hexdigest()


def _part(o: _Obs, live: Live, job: dict, n: int) -> str:
    """The ETag of part `n` sent; a broken connection or a transient answer is tried again."""
    from awb.tcp import labadmin

    offset = (n - 1) * job["part_size"]
    length = min(job["part_size"], job["size"] - offset)
    expired = False
    for attempt in range(len(PAUSES) + 1):
        url, hdrs = o.signed("PUT", _path(job), {"partNumber": str(n), "uploadId": job["upload_id"]},
                             {"Content-Type": "application/octet-stream", "Content-Length": str(length)})
        try:
            status, rh, resp = _stream("PUT", url, hdrs, _Reader(live.fd, offset, length, live.stop))
            try:
                raw = resp.read(64 * 1024)
            finally:
                resp.close()
        except (labadmin.LabAdminError, OSError) as err:
            status, rh, raw = 0, {}, str(err).encode()
        if status == 200:
            etag = _lower(rh).get("etag")
            if isinstance(etag, str) and etag:
                return etag
            status = 0
        if status in (401, 403) and not expired:
            expired = True
            o.drop()
            continue
        if status == 404 and _code(raw) == "NoSuchUpload":
            raise _Gone()
        if status and status not in (408, 429) and status < 500:
            raise _Failed("part %d: OBS answered HTTP %d %s" % (n, status, _code(raw)))
        if attempt == len(PAUSES):
            break
        _pause(live, attempt)
    raise _Broken("the connection to OBS broke at part %d of %d: %s" % (n, job["parts"], RESUME_LINE % "put"))


def _put(svc, p, live: Live, job: dict) -> None:
    o = _Obs(svc, job)
    if not job.get("sha256"):
        job["state"] = "hashing"
        _save(svc, p, live, job)
        job["sha256"] = _hash(svc, p, live, job)
    job["state"] = "running"
    job["done"] = sum(min(job["part_size"], job["size"] - (int(n) - 1) * job["part_size"]) for n in job["etags"])
    live.t0, live.done0 = time.monotonic(), job["done"]
    _save(svc, p, live, job)
    if not job.get("upload_id"):
        status, _h, _r = o.small("HEAD", _path(job))
        absent = status == 404
        status, _h, raw = o.small("POST", _path(job), {"uploads": ""},
                                  {"Content-Type": "application/octet-stream", "x-obs-meta-sha256": job["sha256"]})
        upload_id = _field(_xml(raw), "UploadId") if status == 200 else ""
        if not upload_id:
            raise _Failed("the multipart upload did not start: OBS answered HTTP %d %s" % (status, _code(raw)))
        job["upload_id"], job["etags"] = upload_id, {}
        made = [{"label": "obs object", "name": job["key"][:128],
                 "delete": {"method": "DELETE", "service": "obs", "path": _path(job), "query": {},
                            "region": job["region"]}}] if absent else []
        made.append({"label": "obs multipart", "name": job["key"][:128],
                     "delete": {"method": "DELETE", "service": "obs", "path": _path(job),
                                "query": {"uploadId": upload_id}, "region": job["region"]}})
        _record(svc, p, job, made)
        _save(svc, p, live, job)
    for n in range(1, job["parts"] + 1):
        if str(n) in job["etags"]:
            continue
        if live.stop.is_set():
            raise _Stopped()
        try:
            etag = _part(o, live, job, n)
        except _Gone:
            job["upload_id"], job["etags"] = None, {}
            raise _Failed("the multipart upload is gone (aborted): put the same file again to start it anew") \
                from None
        job["etags"][str(n)] = etag
        job["done"] += min(job["part_size"], job["size"] - (n - 1) * job["part_size"])
        _save(svc, p, live, job)
    st = os.fstat(live.fd)
    if _identity(st) != job["file"]:
        raise _Failed("the file changed during the transfer: put it again")
    job["state"] = "verifying"
    _save(svc, p, live, job)
    body = "<CompleteMultipartUpload>%s</CompleteMultipartUpload>" % "".join(
        "<Part><PartNumber>%d</PartNumber><ETag>%s</ETag></Part>" % (n, escape(job["etags"][str(n)]))
        for n in range(1, job["parts"] + 1))
    status, _h, raw = o.small("POST", _path(job), {"uploadId": job["upload_id"]},
                              {"Content-Type": "application/xml"}, body.encode("utf-8"))
    if status == 404 and _code(raw) == "NoSuchUpload":
        job["upload_id"], job["etags"] = None, {}
        raise _Failed("the multipart upload is gone (aborted): put the same file again to start it anew")
    if status != 200 or _code(raw):
        raise _Failed("the multipart upload did not complete: OBS answered HTTP %d %s" % (status, _code(raw)))
    _upload_done(svc, p, job, job["upload_id"])
    status, rh, _r = o.small("HEAD", _path(job))
    head = _lower(rh)
    if status != 200 or str(head.get("content-length")) != str(job["size"]):
        raise _Failed("the object is not the size of the file: HTTP %d, %s of %d bytes" % (
            status, head.get("content-length"), job["size"]))
    if head.get("x-obs-meta-sha256") != job["sha256"]:
        raise _Failed("the object does not carry the SHA-256 of the file as its metadata")


def _get(svc, p, live: Live, job: dict) -> None:
    o = _Obs(svc, job)
    h = hashlib.sha256()
    done = int(job.get("done") or 0)
    if os.fstat(live.fd).st_size < done:
        done = 0
    os.ftruncate(live.fd, done)
    at = 0
    while at < done:
        data = os.pread(live.fd, min(8 * CHUNK, done - at), at)
        if not data:
            raise _Failed("the file ended before its bytes done: get it again")
        h.update(data)
        at += len(data)
    job["state"], job["done"] = "running", done
    live.t0, live.done0 = time.monotonic(), done
    _save(svc, p, live, job)
    attempt, last = 0, done
    while done < job["size"]:
        if live.stop.is_set():
            raise _Stopped()
        moved = done
        headers = {"If-Match": job["etag"]} if job.get("etag") else {}
        if done:
            headers["Range"] = "bytes=%d-" % done
        url, hdrs = o.signed("GET", _path(job), {}, headers)
        try:
            status, rh, resp = _stream("GET", url, hdrs)
            try:
                if status == 200 and done:
                    done = 0
                    h = hashlib.sha256()
                    os.ftruncate(live.fd, 0)
                if status in (200, 206):
                    while done < job["size"]:
                        if live.stop.is_set():
                            raise _Stopped()
                        chunk = resp.read(CHUNK)
                        if not chunk:
                            break
                        chunk = chunk[:job["size"] - done]
                        view = memoryview(chunk)
                        while view:
                            wrote = os.pwrite(live.fd, view, done)
                            view = view[wrote:]
                            done += wrote
                        h.update(chunk)
                        if done - last >= SAVE_EVERY:
                            job["done"], last = done, done
                            _save(svc, p, live, job)
                    raw = b""
                else:
                    raw = resp.read(64 * 1024)
            finally:
                resp.close()
        except (_Stopped, _Failed):
            raise
        except Exception:          # no answer, a broken connection, http.client.IncompleteRead and the like
            status, raw = 0, b""
        if done >= job["size"]:
            break
        if status == 412:
            raise _Failed("the object changed since the job began: get it again")
        if status in (401, 403) and attempt == 0:
            o.drop()
        elif status == 404:
            raise _Failed("the object is not there (HTTP 404)")
        elif status and status not in (200, 206, 408, 429) and status < 500:
            raise _Failed("OBS answered HTTP %d %s" % (status, _code(raw)))
        if done > moved:
            attempt = 0
        if attempt == len(PAUSES):
            job["done"] = done
            raise _Broken("the connection to OBS broke at byte %d of %d: %s" % (done, job["size"],
                                                                               RESUME_LINE % "get"))
        _pause(live, attempt)
        attempt += 1
    job["done"] = done
    job["state"] = "verifying"
    _save(svc, p, live, job)
    os.fsync(live.fd)
    if os.fstat(live.fd).st_size != job["size"]:
        raise _Failed("the file is not the size of the object")
    job["sha256"] = h.hexdigest()
    if job.get("object_sha256") and job["object_sha256"] != job["sha256"]:
        raise _Failed("the SHA-256 of the file is not the one the object carries")


def _log_line(live: Live, job: dict) -> None:
    lines = ["## %s obs job %s: %s %s" % (_now(), job["id"], job["action"], job["state"]),
             "object: obs://%s/%s" % (job["bucket"], job["key"]),
             "size: %d bytes" % job["size"],
             "sha256: %s" % (job.get("sha256") or "-"),
             "duration: %s s" % job.get("seconds", "-"),
             "state: %s%s" % (job["state"], ": %s" % job["error"] if job.get("error") else "")]
    text, _counts = labsecrets.shorten("\n".join(lines) + "\n")
    data = (text + "\n").encode("utf-8")
    try:
        while data:
            data = data[os.write(live.log_fd, data):]
    except OSError:
        pass


def _work(svc, p, live: Live, job: dict) -> None:
    from awb.tcp import keys, labadmin

    state, error = "done", None
    try:
        (_put if job["action"] == "put" else _get)(svc, p, live, job)
    except _Stopped:
        state, error = live.end_state, live.why or "stopped"
    except _Broken as err:
        state, error = "interrupted", str(err)
    except _Failed as err:
        state, error = "failed", str(err)
    except (keys.KeysError, labadmin.LabAdminError) as err:
        state, error = "interrupted", "%s: %s" % (err, RESUME_LINE % job["action"])
    except Exception as err:
        state, error = "failed", "the transfer stopped (%s)" % type(err).__name__
    try:
        with svc.obs_jobs.lock:
            if live.stop.is_set() and state != "done":
                state, error = live.end_state, live.why or error
            job["state"], job["error"] = state, error
            job["ended"] = _now()
            job["seconds"] = int(time.time() - float(job.get("started_ts") or time.time()))
            elapsed = time.monotonic() - live.t0
            if elapsed > 0:
                job["rate"] = int(max(0, int(job.get("done") or 0) - live.done0) / elapsed)
            save_job(p, job)
        svc._log(job.get("uid", "-"), job["tenant"], "admin", job["action"].upper(), "obs-job", job["region"],
                 state, job["project"])
        _log_line(live, job)
    finally:
        with svc.obs_jobs.lock:
            svc.obs_jobs.live.pop(job["id"], None)
        for fd in (live.fd, live.log_fd):
            try:
                os.close(fd)
            except OSError:
                pass


def _start(svc, p, job: dict, fd: int, log_fd: int) -> None:
    live = Live(job["id"], os.dup(fd), os.dup(log_fd))
    live.done0 = int(job.get("done") or 0)
    job["pid"] = os.getpid()
    with svc.obs_jobs.lock:
        svc.obs_jobs.live[job["id"]] = live
        save_job(p, job)
    live.thread = threading.Thread(target=_work, args=(svc, p, live, job), daemon=True, name="awb-lab-obs")
    live.thread.start()


# --------------------------------------------------------------------------- the op lab_obs


def _jobs_of(svc, p, project: str) -> list[dict]:
    return [_settle(svc, p, j) for j in load_jobs(p) if j.get("project") == project]


def _run_of(svc, req: dict, project: str, alias: str, p) -> dict:
    from awb.tcp import labadmin

    with svc.lab.lock:
        run = labruns.open_run(p, project, alias)
    if run is None or req.get("run") != run["id"]:
        raise _refused(labadmin.NO_RUN_LINE % (project, alias, alias))
    return run


def _region(svc, req: dict, alias: str) -> str:
    from awb.tcp import labadmin

    regions = labadmin._regions(svc, alias)
    region = req.get("region") or regions[0]
    if region not in regions:
        raise _refused("the region is not one of the tenant's")
    return region


def _transfer(svc, req: dict, uid: int, project: str, action: str, fds: list[int]) -> dict:
    from awb.tcp import labadmin

    alias = req.get("tenant")
    labadmin._tenant(svc, alias)
    p = svc.paths_fn()
    run = _run_of(svc, req, project, alias, p)
    region = _region(svc, req, alias)
    bucket, key = _target(svc, req, project)
    fd, st = _file_fd(fds, action, uid, p, project)
    log_fd = _log_fd(fds, p, project, run["id"])
    limit, written = max_bytes(svc.settings)
    with svc.obs_jobs.lock:
        running = set(svc.obs_jobs.live)
    if len(running) >= MAX_LIVE:
        raise _refused("refused: %d lab obs jobs run already: awb lab obs status" % MAX_LIVE)
    busy = next((j["id"] for j in load_jobs(p) if j["id"] in running and j.get("bucket") == bucket
                 and j.get("key") == key), None)
    if busy:
        raise _refused("refused: job %s moves that object already: awb lab obs status %s" % (busy, busy))
    old = [j for j in _jobs_of(svc, p, project) if j.get("run") == run["id"] and j.get("action") == action
           and j.get("bucket") == bucket and j.get("key") == key and j.get("state") in RESUMABLE]
    base = {"run": run["id"], "project": project, "tenant": alias, "region": region, "action": action,
            "bucket": bucket, "key": key, "error": None, "ended": None, "rate": 0}
    if action == "put":
        if st.st_size <= 0:
            raise _refused("refused: the file is empty")
        if st.st_size > limit:
            raise _refused(LIMIT_LINE % ("file", st.st_size / GB, written))
        try:
            part_size, parts = plan(st.st_size)
        except _Failed as err:
            raise _refused(str(err)) from None
        same = next((j for j in reversed(old) if j.get("file") == _identity(st)), None)
        if same is not None:
            job = dict(same, **{k: v for k, v in base.items() if k not in ("run",)})
            job["state"] = "running" if job.get("sha256") else "hashing"
            resumed = True
        else:
            job = dict(base, id=_new_id(), size=st.st_size, part_size=part_size, parts=parts, done=0, etags={},
                       upload_id=None, sha256=None, file=_identity(st), state="hashing", started=_now(),
                       started_ts=time.time())
            resumed = False
    else:
        o = _Obs(svc, dict(base))
        try:
            status, rh, raw = o.small("HEAD", "/%s/%s" % (bucket, key))
        except labadmin.LabAdminError as err:
            raise _refused(str(err)) from None
        head = _lower(rh)
        if status == 404:
            raise _refused("refused: obs://%s/%s is not there" % (bucket, key))
        size = head.get("content-length")
        if status != 200 or not str(size).isdigit():
            raise _refused("refused: the object cannot be read: OBS answered HTTP %d" % status)
        size = int(size)
        if size > limit:
            raise _refused(LIMIT_LINE % ("object", size / GB, written))
        etag = head.get("etag") if isinstance(head.get("etag"), str) else None
        same = next((j for j in reversed(old) if j.get("etag") == etag and j.get("size") == size and
                     (j.get("file") or {}).get("dev") == st.st_dev and (j.get("file") or {}).get("ino") ==
                     st.st_ino), None)
        done = min(int(same.get("done") or 0), st.st_size) if same is not None else 0
        vfs = os.fstatvfs(fd)
        free = vfs.f_bavail * vfs.f_frsize
        need = size - done + DISK_RESERVE
        if free < need:
            raise _refused("refused: the disk of the file has %.1f GB free, the object needs %.1f GB and %d MB to "
                           "spare" % (free / GB, (size - done) / GB, DISK_RESERVE // (1024 * 1024)))
        meta = head.get("x-obs-meta-sha256")
        meta = meta if isinstance(meta, str) and re.match(r"^[0-9a-f]{64}$", meta) else None
        if same is not None:
            job = dict(same, **{k: v for k, v in base.items() if k not in ("run",)})
            job.update(done=done, state="running")
            resumed = True
        else:
            job = dict(base, id=_new_id(), size=size, done=0, etag=etag, object_sha256=meta, sha256=None,
                       file={"dev": st.st_dev, "ino": st.st_ino}, state="running", started=_now(),
                       started_ts=time.time())
            resumed = False
    job["uid"] = uid
    answer = {"ok": True, "job": job["id"], "run": run["id"], "state": job["state"], "size": job["size"],
              "parts": job.get("parts"), "part_size": job.get("part_size"), "resumed": resumed,
              "done": job.get("done", 0), "parts_done": len(job.get("etags") or {})}
    _start(svc, p, job, fd, log_fd)
    svc._log(uid, alias, "admin", action.upper(), "obs-job", region, "resumed" if resumed else "started", project)
    return answer


def serve(svc, req: dict, uid: int, caller, fds: list[int]) -> dict:
    """The op lab_obs: put, get, status and abort of the jobs of the calling project."""
    from awb.tcp import labadmin

    action = req.get("action")
    if action not in ("put", "get", "status", "abort"):
        raise _refused("a lab obs action is put, get, status or abort")
    project = labadmin._project(svc, req, uid, caller)
    p = svc.paths_fn()
    if action in ("put", "get"):
        return _transfer(svc, req, uid, project, action, list(fds))
    named = req.get("job")
    if named is not None and (not isinstance(named, str) or not JOB_RE.match(named)):
        raise _refused("a job id reads like obs-20261103-101500-abcd")
    jobs = _jobs_of(svc, p, project)
    if action == "status":
        return {"ok": True, "jobs": [row(j) for j in jobs if named in (None, j["id"])]}
    job = next((j for j in jobs if j["id"] == named), None)
    if job is None:
        raise _refused("refused: %s has no lab obs job %s" % (project, named))
    abort(svc, p, job, "aborted by the session")
    job = next((j for j in load_jobs(p) if j["id"] == named), job)
    return {"ok": True, "job": row(job)}


def abort(svc, p, job: dict, why: str) -> None:
    """Stop a job and abort its multipart upload; its record says aborted and why."""
    svc.obs_jobs.stop_all(why, "aborted", only={job["id"]})
    with svc.obs_jobs.lock:
        job = next((j for j in load_jobs(p) if j["id"] == job["id"]), job)
        if job["id"] in svc.obs_jobs.live:
            return            # the thread saves the state of the stop at its next step
        if job.get("state") != "done":
            job["state"], job["error"], job["ended"] = "aborted", why, job.get("ended") or _now()
        upload_id = job.get("upload_id") if job.get("state") == "aborted" else None
        save_job(p, job)
    if upload_id:
        from awb.tcp import keys, labadmin

        try:
            status, _h, _r = _Obs(svc, job).small("DELETE", _path(job), {"uploadId": upload_id})
        except (keys.KeysError, labadmin.LabAdminError):
            return            # the run end aborts it with the recorded delete
        if 200 <= status < 300 or status == 404:
            _upload_done(svc, p, job, upload_id)
            with svc.obs_jobs.lock:
                job["upload_id"], job["etags"] = None, {}
                save_job(p, job)


# --------------------------------------------------------------------------- the end of a run (labadmin._end)


def end_jobs(svc, run: dict) -> None:
    """Stop every job of the run and mark it aborted: an ended run is resumed by no job."""
    p = svc.paths_fn()
    mine = [j for j in load_jobs(p) if j.get("run") == run["id"]]
    svc.obs_jobs.stop_all("the lab run ended", "aborted", only={j["id"] for j in mine})
    with svc.obs_jobs.lock:
        for j in load_jobs(p):
            if j.get("run") == run["id"] and j.get("state") in ACTIVE + RESUMABLE and j["id"] not in \
                    svc.obs_jobs.live:
                j["state"], j["error"], j["ended"] = "aborted", "the lab run ended", _now()
                save_job(p, j)


def abort_uploads(svc, run: dict, alias: str, calls: list[dict]) -> None:
    """Abort every incomplete multipart upload of every bucket the run made and has not deleted yet: a bucket delete
    answers 409 BucketNotEmpty while one is there, even when its listing shows no object."""
    from awb.tcp import keys, labadmin

    for c in labadmin.leftovers(run):
        d = c.get("delete") or {}
        if c.get("label") != "obs bucket" or d.get("service") != "obs":
            continue
        bucket = str(d.get("path") or "").strip("/")
        if not _BUCKET_RE.match(bucket):
            continue
        o = _Obs(svc, {"tenant": alias, "region": d.get("region") or labadmin._regions(svc, alias)[0]})
        try:
            found = list_uploads(o, bucket)
        except (_Failed, keys.KeysError, labadmin.LabAdminError):
            continue          # the bucket delete then fails and says so; the next pass tries again
        for key, upload_id in found:
            try:
                status, _h, _r = o.small("DELETE", "/%s/%s" % (bucket, key), {"uploadId": upload_id})
            except (keys.KeysError, labadmin.LabAdminError):
                status = 0
            calls.append({"method": "DELETE", "service": "obs", "path": "/%s/%s?uploadId" % (bucket, key),
                          "status": status, "label": "obs multipart"})
