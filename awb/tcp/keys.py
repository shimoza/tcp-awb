"""The key service: keys, logins and passwords of the test tenants, used for the sessions and never shown to them
(T-100).

The service (`awb keys serve`, a systemd service of the owner like the vault daemon) holds the secrets in memory
only. The owner loads them from his password store after every start (`awb keys unlock`); a restart forgets them.
A working session never reads a key: it sends its call to the service, which checks it, fills in what it may,
signs it, sends it to the TCP API and answers with every secret taken out.

The password store layout, one tenant per alias (the alias of `awb tenant add`):

    awb/tenant/<alias>/ak, awb/tenant/<alias>/sk            the read key: GET and HEAD only
    awb/tenant/<alias>/lab/ak, awb/tenant/<alias>/lab/sk    the lab key: every method, writes need a project code
    awb/tenant/<alias>/secret/<name>                        a login, a password or another secret by name

Two sockets, one JSON object per line and one answer per connection:

    admin  <vault>/keys-admin.sock, mode 600, the owner only     ping, status, load, lock
    call   /run/awb-keys/cloud.sock, mode 660, group awb         ping, tenants, call

A call names the tenant, the role (read or lab), the method, the service, the path (with {project_id} when
needed), the region, the query and a JSON body. Checks before anything is sent: the tenant is registered (awb
tenant add) and loaded, the role has a key and allows the method, the region is one of the tenant's, a write names
an active project (tcp-xxxx) of the project register, the path and service are plain, a write never reaches an
identity service such as iam. A body may carry a secret
only as a whole value `{{secret:NAME}}` in a password field (admin_pass, password, user_password and the like):
nowhere else, so that no secret is put into a field the session can read back. The answer carries the status and
the parsed body with every key and secret of the tenant replaced by <secret>, also in base64 and JSON-escaped
form. Every call is logged to `<vault>/log/cloud-YYYY-MM.tsv` with time, peer uid, tenant, role, method, service,
region, status and project; never a path, a body or a value.

Known limits: a secret that the platform returns in a changed form (hashed, split or otherwise encoded) is not
recognised in the answer; that is why a secret may only fill a password field. A read key can list everything the
tenant's IAM rights allow; give it list rights only.
"""
from __future__ import annotations

import base64
import json
import os
import re
import socket
import subprocess
import sys
import tempfile
import threading
import time
from collections import deque
from datetime import datetime, timezone
from pathlib import Path

from awb import config, obs

CALL_SOCKET_ENV = "AWB_KEYS_SOCKET"
ADMIN_SOCKET_ENV = "AWB_KEYS_ADMIN"
DEFAULT_CALL_SOCKET = Path("/run/awb-keys/cloud.sock")
STORE_PREFIX = "awb/tenant"
ROLES = {"read": ("GET", "HEAD"), "lab": ("GET", "HEAD", "POST", "PUT", "PATCH", "DELETE")}
WRITE_METHODS = ("POST", "PUT", "PATCH", "DELETE")
NO_WRITE_SERVICES = frozenset({"iam"})
"""Services the lab key never writes to: a new access key, user or agency would come back in a form the answer
mask does not know. Identities and credentials are made on the owner's side, never from a session."""
MAX_REQUEST = 512 * 1024
MAX_BODY = 256 * 1024
MAX_ANSWER = 8 * 1024 * 1024
READ_TIMEOUT = 10.0
CALL_TIMEOUT = 180.0
MAX_CONNECTIONS = 16
RATE_LIMIT = 120
RATE_WINDOW = 60.0
_ALIAS_RE = re.compile(r"^[a-z][a-z0-9-]{1,30}$")
_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_.-]{0,40}$")
_PATH_RE = re.compile(r"^/[A-Za-z0-9_{}.:/-]{0,500}$")
_PROJECT_RE = re.compile(r"^tcp-[a-z0-9]{4}$")
_REF_RE = re.compile(r"^\{\{secret:([a-z0-9][a-z0-9_.-]{0,40})\}\}$")
_SECRET_FIELD_RE = re.compile(r"(?i)^(?:admin_?pass(?:word)?|password|passwd|user_?password|root_?password"
                              r"|db_?password|login_?password)$")
MASK = "<secret>"
CONSOLE_USER = "awb-console"
"""The system user of the console's tenant inventory (F2, his decision of 2026-10-04): it alone reads a test tenant
without a project, with the read key only. Sessions run as the work user and keep the query/project rule."""


class KeysError(Exception):
    """A step of the key service that cannot run. The message never carries a key, a secret or a path of the
    password store."""


class Refused(KeysError):
    """A call the service will not make."""


# --------------------------------------------------------------------------- the password store (owner side)


def store_root() -> Path:
    return Path(os.environ.get("PASSWORD_STORE_DIR", "~/.password-store")).expanduser()


def store_entries(root: Path | None = None) -> list[str]:
    """The entry names under awb/tenant of the password store, from the file names only."""
    base = (root or store_root()) / STORE_PREFIX
    if not base.is_dir():
        return []
    return sorted(str(f.relative_to(base))[:-4] for f in base.rglob("*.gpg") if f.is_file())


def pass_reader(entry: str) -> str:
    """The first line of a password store entry under awb/tenant."""
    try:
        r = subprocess.run(["pass", "show", "%s/%s" % (STORE_PREFIX, entry)], capture_output=True, text=True,
                           timeout=60, stdin=subprocess.DEVNULL, check=False)
    except (OSError, subprocess.SubprocessError) as err:
        raise KeysError("the password store cannot be read (%s)" % type(err).__name__) from None
    lines = r.stdout.splitlines() if r.returncode == 0 else []
    if not lines or not lines[0].strip():
        raise KeysError("an entry of the password store gives nothing")
    return lines[0].strip()


def collect(entries: list[str], reader=pass_reader) -> dict:
    """{alias: {"roles": {role: {"ak", "sk"}}, "secrets": {name: value}}} from the entry names. An entry outside
    the layout is refused by its position in the list, never by its name."""
    tenants: dict = {}
    for n, entry in enumerate(entries, start=1):
        parts = entry.split("/")
        if not parts or not _ALIAS_RE.match(parts[0]):
            raise KeysError("entry %d of awb/tenant is not under a tenant alias" % n)
        t = tenants.setdefault(parts[0], {"roles": {}, "secrets": {}})
        if len(parts) == 2 and parts[1] in ("ak", "sk"):
            t["roles"].setdefault("read", {})[parts[1]] = reader(entry)
        elif len(parts) == 3 and parts[1] in ROLES and parts[2] in ("ak", "sk"):
            t["roles"].setdefault(parts[1], {})[parts[2]] = reader(entry)
        elif len(parts) == 3 and parts[1] == "secret" and _NAME_RE.match(parts[2]):
            t["secrets"][parts[2]] = reader(entry)
        else:
            raise KeysError("entry %d of awb/tenant does not follow the layout (ak, sk, lab/ak, lab/sk, secret/NAME)"
                            % n)
    for alias, t in tenants.items():
        for role, pair in t["roles"].items():
            if set(pair) != {"ak", "sk"}:
                raise KeysError("the %s key of %s lacks its ak or sk" % (role, alias))
    return tenants


# --------------------------------------------------------------------------- the service


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _variants(value: str) -> list[str]:
    raw = value.encode("utf-8")
    out = {value, json.dumps(value)[1:-1], base64.b64encode(raw).decode(), base64.urlsafe_b64encode(raw).decode()}
    return sorted((v for v in out if len(v) >= 4), key=len, reverse=True)


def fill(obj, secrets: dict[str, str], key: str | None = None):
    """The body with every `{{secret:NAME}}` replaced, allowed only as the whole value of a password field."""
    if isinstance(obj, dict):
        return {k: fill(v, secrets, str(k)) for k, v in obj.items()}
    if isinstance(obj, list):
        return [fill(v, secrets, key) for v in obj]
    if isinstance(obj, str) and "{{secret" in obj:
        m = _REF_RE.match(obj)
        if not m:
            raise Refused("a secret reference must be the whole value: {{secret:NAME}}")
        if key is None or not _SECRET_FIELD_RE.match(key):
            raise Refused("a secret may only fill a password field (admin_pass, password, user_password ...)")
        if m.group(1) not in secrets:
            raise Refused("the tenant has no secret of that name")
        return secrets[m.group(1)]
    return obj


class Service:
    """The key service. `start()` binds both sockets, `stop()` ends them and forgets every secret."""

    def __init__(self, admin_path: Path, call_path: Path, *, endpoint: str | None = None,
                 paths_fn=config.paths, log_dir: Path | None = None, obs_endpoints: dict | None = None,
                 console_user: str = CONSOLE_USER):
        self.admin_path = Path(admin_path)
        self.call_path = Path(call_path)
        self.endpoint = endpoint
        self.obs_endpoints = dict(obs_endpoints or {})
        self.console_user = console_user
        self.settings: dict = {}
        self.paths_fn = paths_fn
        self.log_dir = log_dir
        self.tenants: dict = {}
        self.lock = threading.Lock()
        self.stopping = threading.Event()
        self._servers: list[socket.socket] = []
        self._threads: list[threading.Thread] = []
        self._open = 0
        self._rate: dict[int, deque] = {}

    # sockets
    def start(self) -> None:
        from awb import vault

        for path, mode, folder, admin in ((self.admin_path, 0o600, 0o700, True), (self.call_path, 0o660, None, False)):
            srv, _ = vault._bind(path, mode, folder)
            self._servers.append(srv)
            t = threading.Thread(target=self._accept, args=(srv, admin), daemon=True, name="awb-keys")
            t.start()
            self._threads.append(t)

    def stop(self) -> None:
        self.stopping.set()
        for srv in self._servers:
            srv.close()
        for t in self._threads:
            t.join(timeout=2)
        for path in (self.admin_path, self.call_path):
            try:
                path.unlink()
            except OSError:
                pass
        with self.lock:
            self.tenants = {}

    def serve_forever(self) -> None:
        import signal

        signal.signal(signal.SIGTERM, lambda *_: self.stopping.set())
        self.start()
        try:
            while not self.stopping.is_set():
                time.sleep(0.5)
        except KeyboardInterrupt:
            pass
        finally:
            self.stop()

    def _accept(self, srv: socket.socket, admin: bool) -> None:
        from awb import vault

        while not self.stopping.is_set():
            try:
                conn, _ = srv.accept()
            except (TimeoutError, socket.timeout):
                continue
            except OSError:
                if self.stopping.is_set():
                    return
                time.sleep(0.05)
                continue
            with self.lock:
                if self._open >= MAX_CONNECTIONS:
                    conn.close()
                    continue
                self._open += 1
            try:
                uid = vault._peer_uid(conn)
            except OSError:
                self._done(conn)
                continue
            threading.Thread(target=self._serve, args=(conn, admin, uid), daemon=True).start()

    def _done(self, conn: socket.socket) -> None:
        try:
            conn.close()
        finally:
            with self.lock:
                self._open -= 1

    def _serve(self, conn: socket.socket, admin: bool, uid: int) -> None:
        try:
            conn.settimeout(READ_TIMEOUT)
            buf = bytearray()
            while b"\n" not in buf:
                chunk = conn.recv(1 << 16)
                if not chunk:
                    return
                buf += chunk
                if len(buf) > MAX_REQUEST:
                    self._send(conn, {"ok": False, "error": "the request is too large"})
                    return
            try:
                req = json.loads(bytes(buf[:buf.index(b"\n")]))
                if not isinstance(req, dict):
                    raise ValueError
            except ValueError:
                self._send(conn, {"ok": False, "error": "a request is one JSON object on one line"})
                return
            upload = None
            stream = None
            try:
                if not admin and req.get("op") == "obs" and str(req.get("method") or "").upper() == "PUT":
                    upload = self._receive(conn, buf[buf.index(b"\n") + 1:], req)
                if admin:
                    answer = self._admin(req, uid)
                else:
                    answer = self._call_socket(req, uid, upload)
                    if isinstance(answer, tuple):
                        answer, stream = answer
                conn.settimeout(CALL_TIMEOUT)
                if stream is not None:
                    answer = dict(answer, length=stream.stat().st_size)
                self._send(conn, answer)
                if stream is not None:
                    with open(stream, "rb") as fh:
                        while True:
                            chunk = fh.read(1 << 16)
                            if not chunk:
                                break
                            conn.sendall(chunk)
            except KeysError as err:
                self._send(conn, {"ok": False, "error": str(err)})
            finally:
                for f in (upload, stream):
                    if f is not None:
                        try:
                            f.unlink()
                        except OSError:
                            pass
        except (OSError, socket.timeout):
            pass
        finally:
            self._done(conn)

    def _xchg(self):
        from awb.tcp import xchg

        return xchg.Context(self.settings, self._lab_keys, self._obs_client, self.paths_fn, self._project_row,
                            self._notify)

    def _receive(self, conn: socket.socket, head: bytes, req: dict) -> Path:
        """The bytes of a put, streamed into a private file of the owner, at most the limit of keys.conf."""
        length = req.get("length")
        limit = self._xchg().max_bytes()
        if not isinstance(length, int) or length < 0:
            raise KeysError("a put names its length")
        if length > limit:
            raise KeysError("the file is larger than the limit of keys.conf (max_mb)")
        from awb.tcp import xchg

        fd, name = tempfile.mkstemp(prefix="awb-put-", dir=xchg._tmp_dir(self._xchg()))
        got = 0
        with os.fdopen(fd, "wb") as out:
            data = bytes(head[:length])
            out.write(data)
            got = len(data)
            conn.settimeout(CALL_TIMEOUT)
            while got < length:
                chunk = conn.recv(min(1 << 16, length - got))
                if not chunk:
                    raise KeysError("the put ended before its length")
                out.write(chunk)
                got += len(chunk)
        return Path(name)

    def _lab_keys(self, alias):
        with self.lock:
            pair = (self.tenants.get(alias) or {}).get("roles", {}).get("lab")
        return obs.Keys(pair["ak"], pair["sk"]) if pair else None

    def _obs_client(self, bucket: str, keys_):
        return obs.Client(bucket, keys_, self.settings.get("region") or "eu-de",
                          endpoint=self.obs_endpoints.get(bucket), timeout=CALL_TIMEOUT)

    def _console_uid(self) -> int | None:
        import pwd

        try:
            return pwd.getpwnam(self.console_user).pw_uid
        except KeyError:
            return None

    def _project_row(self, code):
        from awb import projects

        if not isinstance(code, str) or not _PROJECT_RE.match(code):
            return None
        try:
            return next((r for r in projects.load(self.paths_fn()) if r.code == code and r.state == "active"),
                        None)
        except Exception:
            return None

    def _notify(self, subject: str, message: str) -> None:
        """A mail to the owner through the SMN topic of keys.conf; quietly nothing when none is set."""
        from awb.tcp import cloud

        topic = self.settings.get("notify_topic")
        alias = self.settings.get("bucket_tenant")
        keys_ = self._lab_keys(alias) if alias else None
        if not topic or keys_ is None:
            return
        try:
            client = cloud.Client(keys_, self.settings.get("region") or "eu-de", endpoint=self.endpoint,
                                  label=alias, timeout=30.0)
            client.request("POST", "smn", "/v2/{project_id}/notifications/topics/%s/publish" % topic,
                           body={"subject": subject[:100], "message": message})
        except Exception:
            pass

    @staticmethod
    def _send(conn: socket.socket, answer: dict) -> None:
        conn.sendall(json.dumps(answer, ensure_ascii=False).encode("utf-8") + b"\n")

    # the admin socket
    def _admin(self, req: dict, uid: int) -> dict:
        if uid != os.getuid():
            return {"ok": False, "error": "refused"}
        op = req.get("op")
        if op == "ping":
            return {"ok": True}
        if op == "status":
            return {"ok": True, "tenants": self._summary()}
        if op == "lock":
            with self.lock:
                self.tenants = {}
            return {"ok": True}
        if op == "load":
            data = req.get("tenants")
            if not isinstance(data, dict):
                return {"ok": False, "error": "load needs tenants"}
            with self.lock:
                self.tenants = data
                self.settings = dict(req.get("settings") or {})
            return {"ok": True, "tenants": self._summary()}
        return {"ok": False, "error": "unknown operation"}

    def _summary(self) -> dict:
        with self.lock:
            return {a: {"roles": sorted(t.get("roles", {})), "secrets": sorted(t.get("secrets", {}))}
                    for a, t in sorted(self.tenants.items())}

    # the call socket
    def _call_socket(self, req: dict, uid: int, upload: Path | None = None):
        now = time.monotonic()
        with self.lock:
            q = self._rate.setdefault(uid, deque())
            while q and now - q[0] > RATE_WINDOW:
                q.popleft()
            if len(q) >= RATE_LIMIT:
                return {"ok": False, "error": "rate"}
            q.append(now)
        op = req.get("op")
        if op == "ping":
            return {"ok": True, "state": "unlocked" if self.tenants else "locked"}
        if op == "tenants":
            regions = self._registered()
            return {"ok": True, "tenants": {a: dict(s, regions=list(regions.get(a, ())))
                                            for a, s in self._summary().items()}}
        if op == "call":
            try:
                return self._call(req, uid)
            except Refused as err:
                return {"ok": False, "error": str(err)}
            except KeysError as err:
                return {"ok": False, "error": str(err)}
        if op in ("obs", "owner_has", "take_owner"):
            return self._exchange(op, req, uid, upload)
        if op == "web_read":
            # the materials service of the web console runs as the owner, like this service; nobody else reads
            # the owner bucket through it
            if uid != os.getuid():
                return {"ok": False, "kind": "refused", "error": "refused"}
            return self._exchange(op, req, uid, upload)
        return {"ok": False, "error": "unknown operation"}

    def _exchange(self, op: str, req: dict, uid: int, upload: Path | None):
        from awb.tcp import xchg

        if not self.tenants:
            return {"ok": False, "kind": "locked", "error": "the key service is locked: the owner runs awb keys unlock"}
        ctx = self._xchg()
        status, stream = "ok", None
        try:
            if op == "obs":
                answer, stream = xchg.serve_obs(ctx, req, upload)
            elif op == "owner_has":
                answer = xchg.serve_owner_has(ctx, req)
            elif op == "web_read":
                answer, stream = xchg.serve_web_read(ctx, req)
                status = "ok" if answer.get("ok") else answer.get("kind", "refused")
            else:
                answer = xchg.serve_take_owner(ctx, req)
                status = answer.get("state", "ok")
        except (xchg.XchgError, obs.OBSError) as err:
            answer, status = {"ok": False, "error": str(err)}, "refused"
        except Exception as err:
            answer, status = {"ok": False, "error": "the exchange failed (%s)" % type(err).__name__}, "failed"
        method = str(req.get("method") or req.get("what") or op).upper()
        self._log(uid, self.settings.get("bucket_tenant") or "-", "lab", method, "obs" if op == "obs" else op,
                  self.settings.get("region") or "eu-de", status if not answer.get("ok") else
                  "%s %s" % (status, answer.get("size", "")), req.get("project") or answer.get("project"))
        return (answer, stream) if stream is not None else answer

    def _registered(self) -> dict[str, tuple[str, ...]]:
        from awb.tcp import tenants as _tenants

        try:
            return {t.alias: t.regions for t in _tenants.load(self.paths_fn())}
        except Exception:
            return {}

    def _active_project(self, code: str) -> bool:
        from awb import projects

        try:
            return any(r.code == code and r.state == "active" for r in projects.load(self.paths_fn()))
        except Exception:
            return False

    def _call(self, req: dict, uid: int) -> dict:
        from awb.tcp import cloud

        alias = req.get("tenant")
        role = req.get("role") or "read"
        method = str(req.get("method") or "GET").upper()
        service, path = req.get("service"), req.get("path")
        with self.lock:
            if not self.tenants:
                raise Refused("the key service is locked: the owner runs awb keys unlock")
            tenant = self.tenants.get(alias) if isinstance(alias, str) else None
        if tenant is None:
            raise Refused("the key service holds no tenant of that alias")
        if role not in ROLES:
            raise Refused("the role is read or lab")
        pair = tenant.get("roles", {}).get(role)
        if not pair:
            raise Refused("the tenant has no %s key" % role)
        if method not in ROLES[role]:
            raise Refused("the %s key allows %s only" % (role, ", ".join(ROLES[role])))
        regions = self._registered().get(alias)
        if not regions:
            raise Refused("the tenant is not registered (awb tenant add)")
        region = req.get("region") or regions[0]
        if region not in regions:
            raise Refused("the region is not one of the tenant's")
        project = req.get("project") or ""
        if uid != os.getuid() and uid == self._console_uid():
            # the console's tenant inventory: the read key and no project, nothing else (F2)
            if role != "read":
                raise Refused("the console reads with the read key only")
        elif uid != os.getuid():
            # a session reaches a tenant only from a project of the kind project, never from a query
            from awb import projects as _projects

            row = self._project_row(project)
            if row is None:
                raise Refused("a call to a test tenant runs from the folder of an active project")
            if not _projects.tenant_access(row.kind):
                raise Refused("%s is a query: it has no test tenant; the owner switches it with awb projects kind "
                              "%s project" % (project, project))
        if method in WRITE_METHODS:
            if not (isinstance(project, str) and _PROJECT_RE.match(project) and self._active_project(project)):
                raise Refused("a write needs the code of an active project")
        if not isinstance(service, str) or not re.match(r"^[a-z][a-z0-9-]{1,30}$", service):
            raise Refused("a service name reads like ecs or vpc")
        if method in WRITE_METHODS and service in NO_WRITE_SERVICES:
            raise Refused("the key service does not write to %s; credentials and identities are an owner task"
                          % service)
        if not isinstance(path, str) or not _PATH_RE.match(path) or ".." in path or "//" in path:
            raise Refused("a path is plain: /v1/{project_id}/cloudservers")
        query = req.get("query") or {}
        if not isinstance(query, dict) or not all(isinstance(k, str) and isinstance(v, (str, list))
                                                  for k, v in query.items()):
            raise Refused("a query is an object of strings")
        body = req.get("body")
        if body is not None:
            if not isinstance(body, (dict, list)) or len(json.dumps(body)) > MAX_BODY:
                raise Refused("a body is a JSON object of at most %d bytes" % MAX_BODY)
            body = fill(body, tenant.get("secrets", {}))
        keys = obs.Keys(pair["ak"], pair["sk"])
        client = cloud.Client(keys, region, endpoint=self.endpoint, label=alias, timeout=60.0)
        try:
            r = client.request(method, service, path, query=query or None, body=body)
            answer = {"ok": True, "status": r.status, "data": r.data, "text": r.text}
        except cloud.CloudError as err:
            answer = {"ok": True, "status": err.status or 0, "data": None, "text": str(err)}
        self._log(uid, alias, role, method, service, region, answer["status"], project)
        return self._mask(answer, tenant)

    def _mask(self, answer: dict, tenant: dict) -> dict:
        text = json.dumps(answer, ensure_ascii=False)
        values = [v for pair in tenant.get("roles", {}).values() for v in pair.values()]
        values += list(tenant.get("secrets", {}).values())
        for value in values:
            for form in _variants(value):
                text = text.replace(form, MASK)
        if len(text) > MAX_ANSWER:
            return {"ok": True, "status": answer["status"], "data": None, "text": "the answer is too large"}
        return json.loads(text)

    def _log(self, uid, alias, role, method, service, region, status, project) -> None:
        try:
            folder = self.log_dir or (self.paths_fn().vault / "log")
            folder.mkdir(parents=True, exist_ok=True)
            path = folder / ("cloud-%s.tsv" % datetime.now(timezone.utc).strftime("%Y-%m"))
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND | os.O_NOFOLLOW, 0o600)
            fields = (_now(), uid, alias, role, method, service, region, status, project or "-")
            with os.fdopen(fd, "a", encoding="utf-8") as fh:
                fh.write("\t".join(re.sub(r"[\t\r\n]+", " ", str(x)) for x in fields) + "\n")
        except OSError:
            pass


# --------------------------------------------------------------------------- clients


def call_socket() -> Path:
    return Path(os.environ.get(CALL_SOCKET_ENV) or DEFAULT_CALL_SOCKET)


def admin_socket() -> Path:
    env = os.environ.get(ADMIN_SOCKET_ENV)
    return Path(env) if env else config.paths().vault / "keys-admin.sock"


def request(sock: Path, obj: dict, timeout: float = CALL_TIMEOUT + 10, *, upload: Path | None = None,
            download: Path | None = None) -> dict:
    """One request. `upload` sends a file's bytes after the request line (a put); `download` receives the bytes
    that follow an answer carrying a length (a get)."""
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.settimeout(timeout)
    try:
        s.connect(str(sock))
        if upload is not None:
            obj = dict(obj, length=Path(upload).stat().st_size)
        s.sendall(json.dumps(obj).encode("utf-8") + b"\n")
        if upload is not None:
            try:
                with open(upload, "rb") as fh:
                    while True:
                        chunk = fh.read(1 << 16)
                        if not chunk:
                            break
                        s.sendall(chunk)
            except (BrokenPipeError, ConnectionResetError):
                pass        # the service refused before the bytes were all sent: its answer is waiting
        buf = bytearray()
        while b"\n" not in buf:
            chunk = s.recv(1 << 16)
            if not chunk:
                break
            buf += chunk
            if len(buf) > MAX_ANSWER + 4096 and b"\n" not in buf:
                raise KeysError("the answer of the key service is too large")
        head, _, rest = bytes(buf).partition(b"\n")
        try:
            answer = json.loads(head)
        except ValueError:
            raise KeysError("the key service gave no readable answer") from None
        length = answer.get("length") if isinstance(answer, dict) else None
        if download is not None and isinstance(length, int):
            with open(download, "wb") as out:
                out.write(rest[:length])
                got = min(len(rest), length)
                while got < length:
                    chunk = s.recv(min(1 << 16, length - got))
                    if not chunk:
                        raise KeysError("the file from the key service ended early")
                    out.write(chunk)
                    got += len(chunk)
        return answer
    except (FileNotFoundError, ConnectionRefusedError):
        raise KeysError("the key service is not running") from None
    except PermissionError:
        raise KeysError("the key service refused this user") from None
    except (OSError, socket.timeout) as err:
        raise KeysError("no answer from the key service (%s)" % type(err).__name__) from None
    finally:
        s.close()


def unlock(sock: Path | None = None, entries: list[str] | None = None, reader=pass_reader,
           settings: dict | None = None) -> dict:
    """Load every tenant of the password store into the service. Returns the summary (aliases, roles, secret
    names)."""
    tenants = collect(store_entries() if entries is None else entries, reader)
    if not tenants:
        raise KeysError("the password store holds nothing under awb/tenant")
    from awb.tcp import xchg

    answer = request(sock or admin_socket(), {"op": "load", "tenants": tenants,
                                              "settings": xchg.read_settings() if settings is None else settings},
                     timeout=30)
    if not answer.get("ok"):
        raise KeysError("the key service refused the load: %s" % answer.get("error"))
    return answer["tenants"]


# --------------------------------------------------------------------------- command line


def main(argv: list[str] | None = None) -> int:
    from awb.cli import SafeParser

    ap = SafeParser(prog="awb keys", description="The key service: keys and secrets of the test tenants.")
    sub = ap.add_subparsers(dest="command")
    sub.add_parser("serve", help="run the service (systemd, as the owner)")
    sub.add_parser("unlock", help="load the keys and secrets from the password store (owner)")
    sub.add_parser("lock", help="forget every key and secret (owner)")
    sub.add_parser("status", help="the tenants, their roles and the names of their secrets")
    try:
        args = ap.parse_args(argv)
    except SystemExit as exc:
        return 0 if exc.code == 0 else 2
    if not args.command:
        ap.print_help()
        return 2
    try:
        if args.command == "serve":
            if config.is_work_user():
                print("awb keys: the service runs as the owner", file=sys.stderr)
                return 2
            Service(admin_socket(), call_socket(), endpoint=os.environ.get("AWB_CLOUD_ENDPOINT") or None
                    ).serve_forever()
            return 0
        if args.command in ("unlock", "lock") and config.is_work_user():
            print("awb keys: %s is for the owner" % args.command, file=sys.stderr)
            return 2
        if args.command == "unlock":
            summary = unlock()
            _print_summary(summary)
            return 0
        if args.command == "lock":
            answer = request(admin_socket(), {"op": "lock"}, timeout=10)
            print("locked" if answer.get("ok") else "awb keys: %s" % answer.get("error"))
            return 0 if answer.get("ok") else 1
        answer = request(call_socket(), {"op": "tenants"}, timeout=10)
        if not answer.get("ok"):
            print("awb keys: %s" % answer.get("error"), file=sys.stderr)
            return 1
        if not answer["tenants"]:
            print("locked: the owner runs awb keys unlock")
            return 1
        _print_summary(answer["tenants"])
        return 0
    except KeysError as err:
        print("awb keys: %s" % err, file=sys.stderr)
        return 2


def _print_summary(tenants: dict) -> None:
    for alias, t in sorted(tenants.items()):
        regions = ", ".join(t.get("regions", [])) if t.get("regions") is not None else ""
        print("%-12s keys: %-10s secrets: %s%s" % (alias, ", ".join(t["roles"]) or "-",
                                                   ", ".join(t["secrets"]) or "-",
                                                   "  regions: %s" % regions if regions else ""))


if __name__ == "__main__":
    sys.exit(main())
