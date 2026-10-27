"""The key service: keys, logins and passwords of the test tenants, used for the sessions and never shown to them
(T-100).

The service (`awb keys serve`, a systemd service of the owner like the vault daemon) holds the secrets in memory
only. The owner loads them from his password store after every start (`awb keys unlock`); a restart forgets them.
A reload (`awb keys reload`, what `awb deploy` runs after a code update) keeps them: the service starts
`awb keys serve --takeover FD` itself and hands the tenants and settings to it as one line on an anonymous socket
pair, with a sha256 over both that the new process must give back before it serves (awb/handover.py, hand_over).
A working session never reads a key: it sends its call to the service, which checks it, fills in what it may,
signs it, sends it to the TCP API and answers with every secret taken out. A call to obs is signed the OBS way
(awb/obs.py, path `/BUCKET[/KEY]`, `/` lists the buckets), every other service the way of the API gateway
(SDK-HMAC-SHA256, awb/sign.py). An OBS call never reaches the two exchange buckets (awb xchg has them) and an OBS
write reaches only a bucket named after the project of the call (<code>-...), as the lease policy allows.

The password store layout, one tenant per alias (the alias of `awb tenant add`):

    awb/tenant/<alias>/ak, awb/tenant/<alias>/sk            the read key: GET and HEAD only
    awb/tenant/<alias>/lab/ak, awb/tenant/<alias>/lab/sk    the lab key: every method, writes need a project code
    awb/tenant/<alias>/secret/<name>                        a login, a password or another secret by name
    awb/tenant/<alias>/login/domain, login/user, login/password
                                                            the console login of a read-only tenant (RT)

A read-only tenant (task RT, awb/tcp/readonly.py: the readonly line of keys.conf) allows GET and HEAD only for
every role and caller, with its read key or with a token of its login that the service takes from IAM, holds in
memory and renews; a session reaches it only from a project the owner granted it (awb tenant grant). A call may
name a project of the tenant (`scope`: eu-de, eu-de_<suffix> or an id), checked against GET /v3/auth/projects (its
domain, its region one of the tenant's) and read with a token of that project; the op `projects` lists them.

Two sockets, one JSON object per line and one answer per connection:

    admin  <vault>/keys-admin.sock, mode 600, the owner only     ping, status, load, lock, reload, notify,
                                                                bucket_list, bucket_get, bucket_put
    call   /run/awb-keys/cloud.sock, mode 660, group awb         ping, tenants, call, projects, doc_get

`doc_get` (task DF) fetches one page of public documentation for a session (awb/tcp/doc.py says what it checks):
the service runs as the owner and has internet, the work user does not. It needs no unlock and reads no key.

A call names the tenant, the role (read or lab), the method, the service, the path (with {project_id} when
needed), the region, the query and a JSON body. Checks before anything is sent: the tenant is registered (awb
tenant add) and loaded, the role has a key and allows the method, the region is one of the tenant's, a write names
an active project (tcp-xxxx) of the project register, the path and service are plain, a write never reaches an
identity service such as iam. A body may carry a secret
only as a whole value `{{secret:NAME}}` in a password field (admin_pass, password, user_password and the like):
nowhere else, so that no secret is put into a field the session can read back. A password field may also carry
`{{random:password}}` for a resource that needs a password nobody logs in with later (fact-check design D3): the
service draws a throw-away value at call time that meets the platform's password rules (16 characters, upper and
lower case, a digit and one of !@%^-_=+), sends it, takes it out of the answer like a secret and forgets it; it is
never returned, never logged. The answer carries the status and
the parsed body with every key and secret of the tenant replaced by <secret>, also in base64 and JSON-escaped
form. Every call is logged to `<vault>/log/cloud-YYYY-MM.tsv` with time, peer uid, tenant, role, method, service,
region, status and project; never a path, a body or a value.

Known limits: a secret that the platform returns in a changed form (hashed, split or otherwise encoded) is not
recognised in the answer; that is why a secret may only fill a password field. A read key can list everything the
tenant's IAM rights allow; give it list rights only.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import re
import secrets as _secrets
import socket
import string
import subprocess
import sys
import tempfile
import threading
import time
from collections import deque
from datetime import datetime, timezone
from pathlib import Path

from awb import codes, config, obs
from awb.tcp import readonly

CALL_SOCKET_ENV = "AWB_KEYS_SOCKET"
ADMIN_SOCKET_ENV = "AWB_KEYS_ADMIN"
DEFAULT_CALL_SOCKET = Path("/run/awb-keys/cloud.sock")
STORE_PREFIX = "awb/tenant"
ROLES = {"read": ("GET", "HEAD"), "lab": ("GET", "HEAD", "POST", "PUT", "PATCH", "DELETE")}
WRITE_METHODS = ("POST", "PUT", "PATCH", "DELETE")
NO_WRITE_SERVICES = frozenset({"iam"})
_NOT_ASKED = object()
"""The caller's project was not looked up: the owner, the console or a direct call of a test (K4)."""
LAB_WRITE_LINE = "refused: lab write outside the lab_writes list of keys.conf, service %s"
"""The one line of a lab write the lab_writes list does not name: the class and the service, never the path."""
LAB_WRITES_MISSING_LINE = ("refused: keys.conf has no lab_writes line, so the lab key signs no write; the owner "
                           "adds the line of awb keys lab-writes --from-recipes and runs awb keys unlock")
"""The one line of a lab write when keys.conf has no lab_writes line (K9 of the review of 2026-10-20)."""
LAB_WRITES_OLD_LINE = ("lab_writes has the old form: no lab write is signed until `awb keys lab-writes "
                       "--from-recipes` writes the new line")
"""The one line of an old service:path-prefix lab_writes line: the unlock goes on, reads work, no lab write."""
# the lease (T12 part 3, Variant B): a temporary AK/SK of the lab key for the services a project's Terraform set may
# use (keys.conf tf_services), IAM denied, 15 to 60 minutes
TF_SERVICES = ("ecs", "evs", "vpc", "elb", "nat", "dns", "obs", "ims")
LEASE_MINUTES = (15, 60)
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
_PROJECT_RE = re.compile("^%s$" % codes.TCP_PROJECT_WIDE)
_BUCKET_RE = re.compile(r"^[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]$")
_REF_RE = re.compile(r"^\{\{secret:([a-z0-9][a-z0-9_.-]{0,40})\}\}$")
_SECRET_FIELD_RE = re.compile(r"(?i)^(?:admin_?pass(?:word)?|password|passwd|user_?password|root_?password"
                              r"|db_?password|login_?password)$")
MASK = "<secret>"
RANDOM_PASSWORD = "{{random:password}}"
_RANDOM_SPECIAL = "!@%^-_=+"
"""The special characters a drawn password takes: those both the ECS and the RDS password rules allow."""
LEAVE_WAIT = 70.0
"""Seconds a handed-over service waits for its calls in flight (a cloud call takes at most 60) before it exits."""
ADMIN_OPS = ("bucket_drop_in", "bucket_get", "bucket_list", "bucket_put", "load", "lock", "notify", "ping", "reload", "status")
EXCHANGE_METHODS = ("GET", "HEAD", "PUT", "DELETE", "LIST", "COPY", "MONTHS", "OBS", "OWNER_HAS", "TAKE_OWNER", "INBOX_FIND",
                    "WEB_READ", "TAKE_LAB", "OWNER_FOLDER", "WEB_PUT_IN", "BUCKET_GET", "BUCKET_LIST", "BUCKET_PUT",
                    "BUCKET_DROP_IN")
NOTIFY_MAX = 4000      # characters of an owner mail through the admin socket (awb ui watch --mail)
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
        elif len(parts) == 3 and parts[1] == "login" and parts[2] in readonly.LOGIN_PARTS:
            t.setdefault("login", {})[parts[2]] = reader(entry)
        else:
            raise KeysError("entry %d of awb/tenant does not follow the layout (ak, sk, lab/ak, lab/sk, secret/NAME, "
                            "login/domain|user|password)" % n)
    for alias, t in tenants.items():
        for role, pair in t["roles"].items():
            if set(pair) != {"ak", "sk"}:
                raise KeysError("the %s key of %s lacks its ak or sk" % (role, alias))
        if "login" in t and set(t["login"]) != set(readonly.LOGIN_PARTS):
            raise KeysError("the login of %s lacks its domain, user or password" % alias)
    return tenants


# --------------------------------------------------------------------------- the service


def _digest(tenants, settings) -> str:
    """sha256 over the canonical JSON of what a service holds: the hand-over compares it, so a successor that got
    masked, empty or summary-shaped values is refused before it serves. It travels on the socket pair only."""
    return hashlib.sha256(json.dumps([tenants, settings], sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _takeover_command(fd: int) -> list[str]:
    """The new key service of a reload, like vault._takeover_command; the tests replace it with a stand-in."""
    return [sys.executable, "-I", "-m", "awb", "keys", "serve", "--takeover", str(fd)]


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _variants(value: str) -> list[str]:
    raw = value.encode("utf-8")
    out = {value, json.dumps(value)[1:-1], base64.b64encode(raw).decode(), base64.urlsafe_b64encode(raw).decode()}
    return sorted((v for v in out if len(v) >= 4), key=len, reverse=True)


def random_password(length: int = 16) -> str:
    """A throw-away password: upper and lower case letters, digits and one special character at least, drawn with
    the secrets module."""
    alphabet = string.ascii_letters + string.digits + _RANDOM_SPECIAL
    while True:
        value = "".join(_secrets.choice(alphabet) for _ in range(length))
        if (any(c.islower() for c in value) and any(c.isupper() for c in value) and any(c.isdigit() for c in value)
                and any(c in _RANDOM_SPECIAL for c in value) and value[0].isalnum()):
            return value


def fill(obj, secrets: dict[str, str], key: str | None = None, drawn: list[str] | None = None):
    """The body with every `{{secret:NAME}}` replaced, allowed only as the whole value of a password field. A
    `{{random:password}}` there gets a drawn throw-away value, appended to `drawn` so the answer can be masked."""
    if isinstance(obj, dict):
        return {k: fill(v, secrets, str(k), drawn) for k, v in obj.items()}
    if isinstance(obj, list):
        return [fill(v, secrets, key, drawn) for v in obj]
    if isinstance(obj, str) and "{{random" in obj:
        if obj != RANDOM_PASSWORD:
            raise Refused("a random value must be the whole value: {{random:password}}")
        if key is None or not _SECRET_FIELD_RE.match(key):
            raise Refused("a random password may only fill a password field (admin_pass, password ...)")
        value = random_password()
        if drawn is not None:
            drawn.append(value)
        return value
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
        self._servers: list[tuple[socket.socket, Path, tuple[int, int]]] = []
        self._threads: list[threading.Thread] = []
        self._open = 0
        self._rate: dict[int, deque] = {}
        self.handing_over = threading.Event()
        self.handed_over = threading.Event()
        self._paused = threading.Event()
        self._idle = {True: threading.Event(), False: threading.Event()}
        self._reload_lock = threading.Lock()
        self.accepts = {"admin": 0, "call": 0}
        from awb.tcp import doc

        self.doc_limiter = doc.Limiter(self._doc_ledger_dir)
        self.doc_getter = doc.https_get
        self.doc_hosts = None   # None: rules/allow-domains.txt at each fetch
        self.tokens = readonly.Tokens()   # the login tokens of read-only tenants, in memory only, never handed over

    # sockets
    def start(self, inherited: dict | None = None, accept: bool = True) -> None:
        """Bind both sockets and serve. `inherited` ({True: admin, False: call} listening sockets of the service
        before) skips the bind; `accept=False` waits for accept_now() (the go of a hand-over)."""
        from awb import handover

        if self._servers:
            return
        for path, mode, folder, admin in ((self.admin_path, 0o600, 0o700, True), (self.call_path, 0o660, None, False)):
            if inherited is None:
                srv, ident = handover._bind(path, mode, folder)
            else:
                srv = inherited[admin]
                ident = handover._adopt(srv, path)
            self._servers.append((srv, path, ident))
            self._threads.append(threading.Thread(target=self._accept, args=(srv, admin), daemon=True,
                                                  name="awb-keys"))
        if accept:
            self.accept_now()

    def accept_now(self) -> None:
        for t in self._threads:
            if t.ident is None:
                t.start()

    def _close_servers(self, unlink: bool) -> None:
        """Close the listening sockets; remove a socket file only while it is still the one this service bound or
        took over (its identity), never after a hand-over."""
        unlink = unlink and not self.handed_over.is_set()
        for srv, path, ident in self._servers:
            srv.close()
            if not unlink:
                continue
            try:
                st = os.lstat(path)
                if (st.st_dev, st.st_ino) == ident:
                    path.unlink()
            except OSError:
                pass

    def stop(self) -> None:
        if self.handed_over.is_set():
            self.leave()
            return
        self.stopping.set()
        self._close_servers(unlink=True)
        for t in self._threads:
            if t.ident is not None:
                t.join(timeout=2)
        with self.lock:
            self.tenants = {}
        self.tokens.clear()

    def leave(self) -> None:
        """The hand-over exit: the successor serves on the same socket files, so they stay; the calls in flight
        finish (bounded by LEAVE_WAIT); the keys are forgotten."""
        self.handed_over.set()
        self.stopping.set()
        self._close_servers(unlink=False)
        for t in self._threads:
            if t.ident is not None:
                t.join(timeout=2)
        end = time.monotonic() + LEAVE_WAIT
        while time.monotonic() < end:
            with self.lock:
                if self._open <= 0:
                    break
            time.sleep(0.05)
        with self.lock:
            self.tenants = {}
            self.settings = {}
        self.tokens.clear()

    def serve_forever(self) -> None:
        import signal

        if threading.current_thread() is threading.main_thread():
            # the finally reads handed_over: after a hand-over a signal leaves the successor's files alone
            signal.signal(signal.SIGTERM, lambda *_: self.stopping.set())
        self.start()
        try:
            while not self.stopping.is_set():
                time.sleep(0.5)
        except KeyboardInterrupt:
            pass
        finally:
            if self.handed_over.is_set():
                self.leave()
            else:
                self.stop()

    def _pause(self, wait: float = 2.0) -> bool:
        for e in self._idle.values():
            e.clear()
        self._paused.set()
        end = time.monotonic() + wait
        return all(e.wait(max(0.0, end - time.monotonic())) for e in self._idle.values())

    def _resume(self) -> None:
        self._paused.clear()

    def _accept(self, srv: socket.socket, admin: bool) -> None:
        from awb import handover

        key = "admin" if admin else "call"
        while not self.stopping.is_set():
            if self._paused.is_set():
                self._idle[admin].set()
                time.sleep(0.02)
                continue
            self.accepts[key] += 1
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
                uid = handover._peer_uid(conn)
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
                    # F8: the call is authorised before a byte of the upload is stored
                    refusal = self._put_refusal(req, uid)
                    if refusal:
                        self._send(conn, {"ok": False, "error": refusal})
                        return
                    upload = self._receive(conn, buf[buf.index(b"\n") + 1:], req)
                elif not admin and req.get("op") == "web_put_in":
                    # the console's upload into a project's in/: the materials service runs as the owner, like
                    # this service; any other peer is refused before a byte of the upload is stored
                    if uid != os.getuid() or not self.tenants:
                        self._send(conn, {"ok": False, "kind": "refused" if self.tenants else "locked",
                                          "error": "refused"})
                        return
                    upload = self._receive(conn, buf[buf.index(b"\n") + 1:], req)
                if admin and req.get("op") == "bucket_put":
                    # the owner's CLI: authorised before a byte of the upload is stored, like any other put
                    refusal = self._bucket_refusal(req, uid, put=True)
                    if refusal:
                        self._send(conn, {"ok": False, "kind": "refused" if self.tenants else "locked",
                                          "error": refusal})
                        return
                    upload = self._receive(conn, buf[buf.index(b"\n") + 1:], req)
                if admin:
                    answer = self._admin(req, uid, upload)
                    if isinstance(answer, tuple):
                        answer, stream = answer
                else:
                    answer = self._call_socket(req, uid, upload, caller=self._caller_project(req, uid))
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
        from awb.tcp import keys_exchange

        return keys_exchange.Context(self.settings, self._lab_keys, self._obs_client, self.paths_fn, self._project_row,
                            self._notify)

    def _put_refusal(self, req: dict, uid: int) -> str | None:
        """Why a put is refused before its bytes are read (F8), or None; a refusal is logged like any exchange
        call."""
        from awb.tcp import keys_exchange

        if not self.tenants:
            return "the key service is locked: the owner runs awb keys unlock"
        try:
            keys_exchange.authorise_put(self._xchg(), req)
        except keys_exchange.XchgError as err:
            project = req.get("project")
            self._log(uid, self.settings.get("bucket_tenant") or "-", "lab", "PUT", "obs",
                      self.settings.get("region") or "eu-de", "refused",
                      project if isinstance(project, str) and codes.is_project_code(project) else "-")
            return str(err)
        return None

    def _receive(self, conn: socket.socket, head: bytes, req: dict) -> Path:
        """The bytes of a put, streamed into a private file of the owner, at most the limit of keys.conf."""
        length = req.get("length")
        limit = self._xchg().max_bytes()
        if not isinstance(length, int) or length < 0:
            raise KeysError("a put names its length")
        if length > limit:
            raise KeysError("the file is larger than the limit of keys.conf (max_mb)")
        from awb.tcp import keys_exchange

        fd, name = tempfile.mkstemp(prefix="awb-put-", dir=keys_exchange._tmp_dir(self._xchg()))
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
        if self._readonly(alias) is not None:
            return None         # RT: a read-only tenant never writes, not even the exchange
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
            return next((r for r in projects.load(self.paths_fn()) if r.code == code and r.state in projects.LIVE_STATES),
                        None)
        except Exception:
            return None

    def _notify(self, subject: str, message: str) -> bool:
        """A mail to the owner through the SMN topic of keys.conf; quietly nothing when none is set. True when
        the topic took it."""
        from awb.tcp import cloud

        topic = self.settings.get("notify_topic")
        alias = self.settings.get("bucket_tenant")
        keys_ = self._lab_keys(alias) if alias else None
        if not topic or keys_ is None:
            return False
        try:
            client = cloud.Client(keys_, self.settings.get("region") or "eu-de", endpoint=self.endpoint,
                                  label=alias, timeout=30.0)
            client.request("POST", "smn", "/v2/{project_id}/notifications/topics/%s/publish" % topic,
                           body={"subject": subject[:100], "message": message})
        except Exception:
            return False
        return True

    @staticmethod
    def _send(conn: socket.socket, answer: dict) -> None:
        conn.sendall(json.dumps(answer, ensure_ascii=False).encode("utf-8") + b"\n")

    # the admin socket
    def _admin(self, req: dict, uid: int, upload: Path | None = None):
        if uid != os.getuid():
            return {"ok": False, "error": "refused"}
        from awb import handover

        op = req.get("op")
        if op in ("bucket_list", "bucket_get", "bucket_put", "bucket_drop_in"):
            return self._bucket(op, req, uid, upload)
        if op == "ping":
            return {"ok": True, "ops": list(ADMIN_OPS), "release": handover.release_path()}
        if op == "status":
            return {"ok": True, "tenants": self._summary(), "ops": list(ADMIN_OPS), "release": handover.release_path(),
                    "pid": os.getpid()}
        if op == "lock":
            with self.lock:
                if self.handing_over.is_set():
                    return {"ok": False, "error": "busy"}
                self.tenants = {}
            self.tokens.clear()
            return {"ok": True}
        if op == "load":
            data = req.get("tenants")
            if not isinstance(data, dict):
                return {"ok": False, "error": "load needs tenants"}
            with self.lock:
                if self.handing_over.is_set():
                    return {"ok": False, "error": "busy"}
                settings = dict(req.get("settings") or {})
                self.tenants = _without_writes(data, settings)
                self.settings = settings
            self.tokens.clear()
            if _old_lab_writes(settings):
                print(LAB_WRITES_OLD_LINE, file=sys.stderr, flush=True)       # the journal of the unit
            return {"ok": True, "tenants": self._summary()}
        if op == "reload":
            return self._reload()
        if op == "notify":
            subject, message = req.get("subject"), req.get("message")
            if not isinstance(subject, str) or not isinstance(message, str) or len(message) > NOTIFY_MAX:
                return {"ok": False, "error": "notify needs a subject and a message of at most %d" % NOTIFY_MAX}
            return {"ok": True, "sent": self._notify(subject, message)}
        return {"ok": False, "error": "unknown operation"}

    def _bucket_refusal(self, req: dict, uid: int, put: bool = False) -> str | None:
        """Why a call of the owner's CLI to the owner bucket is refused before its upload is read, or None."""
        from awb.tcp import keys_exchange

        if uid != os.getuid():
            return "refused"
        if not self.tenants:
            return "the key service is locked: the owner runs awb keys unlock"
        return keys_exchange.owner_put_refusal(self._xchg(), req) if put else None

    def _bucket(self, op: str, req: dict, uid: int, upload: Path | None):
        """`bucket_list`, `bucket_get`, `bucket_put`: the owner's CLI at the owner bucket, logged like an exchange
        call."""
        from awb.tcp import keys_exchange

        if not self.tenants:
            return {"ok": False, "kind": "locked", "error": "the key service is locked: the owner runs awb keys unlock"}
        try:
            answer, stream = keys_exchange.serve_bucket(self._xchg(), req, upload)
        except Exception as err:
            answer, stream = {"ok": False, "error": "the bucket call failed (%s)" % type(err).__name__}, None
        self._log(uid, self.settings.get("bucket_tenant") or "-", "lab", op.upper(), op,
                  self.settings.get("region") or "eu-de", "ok" if answer.get("ok") else str(answer.get("kind") or "refused"),
                  "-")
        return (answer, stream) if stream is not None else answer

    def _reload(self) -> dict:
        """Hand the service to a new process of the installed code (design 8.4): the same order as the vault
        daemon's, plus the summary and the digest of the child compared before go."""
        from awb import handover

        deadline = time.monotonic() + handover.HANDOVER_DEADLINE
        if not self._reload_lock.acquire(blocking=False):
            return {"ok": False, "error": "busy"}
        try:
            self.handing_over.set()
            with self.lock:
                tenants, settings = self.tenants, self.settings
            summary = self._summary()
            digest = _digest(tenants, settings)
            (admin, admin_path, _), (call, call_path, _) = self._servers
            line = {"tenants": tenants, "settings": settings, "digest": digest, "admin_fd": admin.fileno(),
                    "call_fd": call.fileno(), "admin_path": str(admin_path), "call_path": str(call_path)}

            def check(reply: dict) -> str | None:
                if reply.get("tenants") != summary:
                    return "another summary"
                if reply.get("digest") != digest:
                    return "other values"
                return None

            reply, word = handover.hand_over(self, _takeover_command, (admin.fileno(), call.fileno()), line, deadline,
                                          check)
            line = tenants = settings = None
            if reply is None:
                return {"ok": False, "error": "failed", "detail": "the new key service did not come up (%s)" % word}
            self.handed_over.set()
            self.stopping.set()
            return {"ok": True, "pid": reply["pid"], "release": reply["release"], "tenants": summary}
        finally:
            if not self.handed_over.is_set():
                self.handing_over.clear()
            self._reload_lock.release()

    def _summary(self) -> dict:
        with self.lock:
            held = readonly.of_settings(self.settings)
            out = {}
            for a, t in sorted(self.tenants.items()):
                out[a] = {"roles": sorted(t.get("roles", {})), "secrets": sorted(t.get("secrets", {}))}
                if a in held:
                    # RT: the kind and whether a key or a login reads it, never a value
                    out[a].update(kind="readonly", auth=held[a]["auth"])
                elif "login" in t:
                    out[a].update(auth="login")
            return out

    def _readonly(self, alias) -> dict | None:
        """The entry of a read-only tenant in the settings (awb/tcp/readonly.py), None for any other tenant."""
        with self.lock:
            return readonly.of_settings(self.settings).get(alias) if isinstance(alias, str) else None

    def _readonly_granted(self, alias: str, code: str) -> bool:
        """The owner's grant of the read-only tenant `alias` to the project `code` (RT)."""
        try:
            return readonly.granted(self.paths_fn(), alias, code)
        except Exception:
            return False

    # the call socket
    def _call_socket(self, req: dict, uid: int, upload: Path | None = None, caller=_NOT_ASKED):
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
            with self.lock:
                for a, e in readonly.of_settings(self.settings).items():
                    regions.setdefault(a, e["regions"])
            return {"ok": True, "tenants": {a: dict(s, regions=list(regions.get(a, ())))
                                            for a, s in self._summary().items()}}
        if op == "call":
            try:
                return self._call(req, uid, caller)
            except Refused as err:
                return {"ok": False, "error": str(err)}
            except KeysError as err:
                return {"ok": False, "error": str(err)}
        if op == "projects":
            try:
                return self._readonly_projects(req, uid, caller)
            except Refused as err:
                return {"ok": False, "error": str(err)}
        if op == "lease":
            try:
                return self._lease(req, uid, caller)
            except Refused as err:
                return {"ok": False, "error": str(err)}
        if op == "doc_get":
            return self._doc_get(req, caller)
        if (op == "owner_has" or (op == "take_owner" and req.get("id") is None)) and uid != os.getuid():
            # a name of the owner inbox is asked by the owner alone: for a session an answer to a name it guessed
            # would tell whether the name is there (finding 2 of the review of 2026-10-10); a session takes by id
            return {"ok": False, "error": "a session takes a file of the owner inbox by its id (awb inbox list)"}
        if op in ("obs", "owner_has", "take_owner", "take_lab", "inbox_find", "owner_folder"):
            # owner_folder: spawn (the work user, the console's creation) asks for the project's folder in the
            # owner bucket; the service makes it with the bucket key, which the caller never holds
            return self._exchange(op, req, uid, upload)
        if op in ("web_read", "web_put_in"):
            # the materials service of the web console runs as the owner, like this service; nobody else reads
            # the owner bucket through it
            if uid != os.getuid():
                return {"ok": False, "kind": "refused", "error": "refused"}
            return self._exchange(op, req, uid, upload)
        return {"ok": False, "error": "unknown operation"}

    def _exchange(self, op: str, req: dict, uid: int, upload: Path | None):
        from awb.tcp import keys_exchange

        if not self.tenants:
            return {"ok": False, "kind": "locked", "error": "the key service is locked: the owner runs awb keys unlock"}
        ctx = self._xchg()
        status, stream = "ok", None
        try:
            if op == "obs":
                answer, stream = keys_exchange.serve_obs(ctx, req, upload)
            elif op == "owner_has":
                answer = keys_exchange.serve_owner_has(ctx, req)
            elif op == "take_lab":
                answer, stream = keys_exchange.serve_take_lab(ctx, req)
                status = answer.get("state", "ok")
            elif op == "inbox_find":
                answer = keys_exchange.serve_inbox_find(ctx, req)
            elif op == "owner_folder":
                answer = keys_exchange.serve_owner_folder(ctx, req)
            elif op == "web_put_in":
                answer = keys_exchange.serve_web_put_in(ctx, req, upload)
            elif op == "web_read":
                answer, stream = keys_exchange.serve_web_read(ctx, req)
                status = "ok" if answer.get("ok") else answer.get("kind", "refused")
            else:
                answer = keys_exchange.serve_take_owner(ctx, req)
                status = answer.get("state", "ok")
        except (keys_exchange.XchgError, obs.OBSError) as err:
            answer, status = {"ok": False, "error": str(err)}, "refused"
        except Exception as err:
            answer, status = {"ok": False, "error": "the exchange failed (%s)" % type(err).__name__}, "failed"
        # the log carries no string of the request as written: the method from a fixed set, the project only as a
        # project code, for every uid
        method = str(req.get("method") or req.get("what") or op).upper()
        method = method if method in EXCHANGE_METHODS else "-"
        project = req.get("project") or answer.get("project")
        project = project if isinstance(project, str) and codes.is_project_code(project) else "-"
        size = answer.get("size", "")
        self._log(uid, self.settings.get("bucket_tenant") or "-", "lab", method, "obs" if op == "obs" else op,
                  self.settings.get("region") or "eu-de", status if not answer.get("ok") else
                  "%s %s" % (status, size if isinstance(size, int) else ""), project)
        return (answer, stream) if stream is not None else answer

    def _doc_ledger_dir(self) -> Path | None:
        try:
            return self.log_dir or (self.paths_fn().vault / "log")
        except Exception:
            return None

    def _doc_get(self, req: dict, caller=_NOT_ASKED):
        """One page of public documentation (task DF). The project is the one the session's key token names; a
        session of a project without a grant (or the owner) names an active project of the register."""
        from awb.tcp import doc

        project = caller if isinstance(caller, str) else None
        named = req.get("project")
        if project is None and isinstance(named, str) and _PROJECT_RE.match(named) and self._active_project(named):
            project = named
        tmp = None
        try:
            tmp = self.paths_fn().vault / "tmp"
            tmp.mkdir(parents=True, exist_ok=True)
            os.chmod(tmp, 0o700)
        except Exception:
            tmp = None
        try:
            return doc.serve(req, project, self.doc_limiter, self._doc_ledger_dir(), getter=self.doc_getter,
                             hosts=self.doc_hosts, tmp_dir=str(tmp) if tmp else None)
        except Exception as err:
            return {"ok": False, "error": "failed: the reader stopped (%s)" % type(err).__name__}

    def _registered(self) -> dict[str, tuple[str, ...]]:
        from awb.tcp import tenants as _tenants

        try:
            return {t.alias: t.regions for t in _tenants.load(self.paths_fn())}
        except Exception:
            return {}

    def _granted(self, code: str) -> bool:
        """The owner's grant of tenant access for `code` (P0): never the kind of the work user's projects.tsv."""
        from awb import grants

        try:
            return grants.granted(self.paths_fn(), code)
        except Exception:
            return False

    def _customer_differs(self, row) -> bool:
        """True when the row of a granted project names another customer than the owner's grant recorded."""
        from awb import grants, projects

        own = getattr(row, "customer", None)
        if own is None:
            return False
        try:
            granted = grants.customer(self.paths_fn(), row.code)
        except Exception:
            return False   # an unreadable grant refused the call already (_granted)
        return granted is not None and granted != (own or projects.NO_CUSTOMER)

    def _active_project(self, code: str) -> bool:
        from awb import projects

        try:
            return any(r.code == code and r.state in projects.LIVE_STATES for r in projects.load(self.paths_fn()))
        except Exception:
            return False

    def _grant_tokens(self) -> dict[str, str]:
        from awb import grants

        try:
            return grants.tokens(self.paths_fn())
        except Exception:
            return {}

    def _caller_project(self, req: dict, uid: int):
        """K4: the project of a session (the work user) is the granted project whose key token the request
        presents, never the code it names: `_NOT_ASKED` for the owner and the console, None for no such token. It
        reads only the request and the owner's grants: the service runs as the owner and may not read the process of
        another user (PR 69, reverted)."""
        if not self._session_read(uid):
            return _NOT_ASKED
        token = req.get("token")
        if not (isinstance(token, str) and len(token) == 64):
            return None
        found = None
        for code, recorded in self._grant_tokens().items():
            if hmac.compare_digest(recorded.encode("ascii"), token.encode("ascii", "replace")):
                found = code
        return found

    def _unbound(self, project, caller, what: str) -> Refused:
        if caller is None and isinstance(project, str) and _PROJECT_RE.match(project):
            if self._granted(project) and project not in self._grant_tokens():
                return Refused("the grant of %s holds no key token: the owner runs awb projects kind %s project once "
                               "more" % (project, project))
        return Refused(what)

    def _call(self, req: dict, uid: int, caller=_NOT_ASKED) -> dict:
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
        entry = self._readonly(alias)
        if entry is not None:
            return self._call_readonly(req, uid, caller, alias, tenant, entry)
        if "login" in tenant:
            raise Refused(readonly.UNREGISTERED_LINE % alias)
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

            if caller is not _NOT_ASKED and caller != project:
                # K4: the project is the one whose key token the session presents, never the one the request names
                raise self._unbound(project, caller, "a call to a test tenant runs from the folder of an active "
                                                     "project")
            row = self._project_row(project)
            if row is None:
                raise Refused("a call to a test tenant runs from the folder of an active project")
            if not _projects.tenant_access(row.kind):
                raise Refused("%s is a query: it has no test tenant; the owner switches it with awb projects kind "
                              "%s project" % (project, project))
            if not self._granted(project):
                raise Refused("%s has no grant of the owner: he runs awb projects kind %s project in his own "
                              "terminal" % (project, project))
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
        if method in WRITE_METHODS:
            # K9: the write is one METHOD service path triple of the lab_writes line, matched whole with its query
            # keys; without the line no write is signed
            if self.settings.get("lab_writes") is None:
                raise Refused(LAB_WRITES_MISSING_LINE)
            if _old_lab_writes(self.settings):
                raise Refused(LAB_WRITES_OLD_LINE)
            if not self._lab_write_allowed(method, service, path, req.get("query")):
                raise Refused(LAB_WRITE_LINE % service)
        if service == "obs":
            # OBS is signed its own way and works: the exchange buckets stay with awb xchg, a write stays on the
            # buckets of the project (<code>-...), the rule of the lease policy
            bucket = path[1:].partition("/")[0]
            if bucket in self.exchange_buckets():
                raise Refused("the exchange buckets are reached through awb xchg only")
            if method in WRITE_METHODS and not bucket.startswith(project + "-"):
                raise Refused("an OBS write reaches only a bucket named after the project (%s-...)" % project)
            if method not in WRITE_METHODS and self._session_read(uid) and not self._obs_read_ok(
                    bucket, path[1:].partition("/")[2], req.get("query"), project):
                # K1: a session reads its own project's buckets, the bucket list (the sweep) and the tags of a bucket
                raise Refused("an OBS read reaches only a bucket named after the project (%s-...)" % project)
        query = req.get("query") or {}
        if not isinstance(query, dict) or not all(isinstance(k, str) and isinstance(v, (str, list))
                                                  for k, v in query.items()):
            raise Refused("a query is an object of strings")
        body = req.get("body")
        drawn: list[str] = []
        if body is not None:
            if not isinstance(body, (dict, list)) or len(json.dumps(body)) > MAX_BODY:
                raise Refused("a body is a JSON object of at most %d bytes" % MAX_BODY)
            body = fill(body, tenant.get("secrets", {}), drawn=drawn)
        keys = obs.Keys(pair["ak"], pair["sk"])
        client = cloud.Client(keys, region, endpoint=self.endpoint, label=alias, timeout=60.0)
        try:
            r = client.request(method, service, path, query=query or None, body=body)
            answer = {"ok": True, "status": r.status, "data": r.data, "text": r.text, "size": r.size,
                      "cut": r.cut}
        except cloud.CloudError as err:
            answer = {"ok": True, "status": err.status or 0, "data": None, "text": str(err)}
        self._log(uid, alias, role, method, service, region, answer["status"], project)
        return self._mask(answer, tenant, drawn)

    def _readonly_caller(self, uid: int, caller, alias: str, project: str) -> None:
        """Who may reach the read-only tenant `alias` (RT): the owner, never the console, a session only from a
        project the owner granted the kind project and this tenant."""
        if uid != os.getuid() and uid == self._console_uid():
            raise Refused(readonly.CONSOLE_LINE % alias)
        if uid != os.getuid():
            from awb import projects as _projects

            if caller is not _NOT_ASKED and caller != project:
                raise self._unbound(project, caller, "a call to a test tenant runs from the folder of an active "
                                                     "project")
            row = self._project_row(project)
            if row is None:
                raise Refused("a call to a test tenant runs from the folder of an active project")
            if not _projects.tenant_access(row.kind) or not self._granted(project):
                raise Refused("%s has no grant of the owner: he runs awb projects kind %s project in his own "
                              "terminal" % (project, project))
            if not self._readonly_granted(alias, project):
                raise Refused(readonly.GRANT_LINE % (project, alias, alias, project))

    def _readonly_makers(self, uid: int, alias: str, tenant: dict, entry: dict, region: str, project: str,
                         used: list[str], project_id: str | None = None) -> list:
        """The ways to a client of a read-only tenant in `region`, scoped to the region's project or to
        `project_id`: the read key, or a token of the login and once more a new one after a 401."""
        from awb.tcp import cloud

        if entry["auth"] == "key":
            pair = tenant.get("roles", {}).get("read")
            if not pair:
                raise Refused("the read-only tenant %s holds no read key: the owner puts awb/tenant/%s/ak and sk "
                              "into pass and runs awb keys unlock" % (alias, alias))
            return [lambda: cloud.Client(obs.Keys(pair["ak"], pair["sk"]), region, endpoint=self.endpoint,
                                         label=alias, timeout=60.0, project_id=project_id)]
        login = tenant.get("login")
        if not login:
            raise Refused("the read-only tenant %s holds no login: the owner puts awb/tenant/%s/login/domain, "
                          "user and password into pass and runs awb keys unlock" % (alias, alias))

        def by_login():
            try:
                token, got = self.tokens.get(alias, region, login, entry["domain_id"], self.endpoint,
                                             project_id=project_id)
            except readonly.ReadonlyError as err:
                self._log(uid, alias, "read", "LOGIN", "iam", region, "refused", project)
                raise Refused(str(err)) from None
            used.append(token)
            return cloud.Client(obs.Keys("", ""), region, endpoint=self.endpoint, label=alias, timeout=60.0,
                                token=token, project_id=got)

        def again():
            self.tokens.drop(alias, project_id or region)
            return by_login()

        return [by_login, again]

    def _readonly_send(self, makers: list, method: str, service: str, path: str, query: dict,
                       headers: dict | None = None) -> dict:
        from awb.tcp import cloud

        answer: dict = {}
        for n, make in enumerate(makers):
            client = make()
            try:
                r = client.request(method, service, path, query=query or None, headers=headers)
                answer = {"ok": True, "status": r.status, "data": r.data, "text": r.text, "size": r.size,
                          "cut": r.cut}
            except cloud.CloudError as err:
                answer = {"ok": True, "status": err.status or 0, "data": None, "text": client.clean(str(err))}
            if answer["status"] != 401 or n == len(makers) - 1:
                break        # a 401 of a token: IAM took it back (a change of the user), one new login
        return answer

    def _readonly_listing(self, uid: int, alias: str, tenant: dict, entry: dict, project: str,
                          used: list[str]) -> list[dict]:
        """Every project the login or key of `alias` can see (GET /v3/auth/projects in its first region), kept
        readonly.LIST_LIFE seconds."""
        held = self.tokens.listing(alias)
        if held is not None:
            return held
        region = entry["regions"][0]
        makers = self._readonly_makers(uid, alias, tenant, entry, region, project, used)
        answer = self._readonly_send(makers, "GET", "iam", readonly.PROJECTS_PATH, {})
        self._log(uid, alias, "read", "GET", "iam", region, answer["status"], project)
        if answer["status"] != 200:
            raise Refused("the projects of %s cannot be read: HTTP %d" % (alias, answer["status"]))
        found = readonly.projects_of(answer.get("data"))
        self.tokens.keep_listing(alias, found)
        return found

    def _readonly_projects(self, req: dict, uid: int, caller=_NOT_ASKED) -> dict:
        """The op projects: the projects a read-only tenant can be read in (its domain, its regions), GET only."""
        alias = req.get("tenant")
        with self.lock:
            if not self.tenants:
                raise Refused("the key service is locked: the owner runs awb keys unlock")
            tenant = self.tenants.get(alias) if isinstance(alias, str) else None
        if tenant is None:
            raise Refused("the key service holds no tenant of that alias")
        entry = self._readonly(alias)
        if entry is None:
            raise Refused("%s is not a read-only tenant: the projects are listed for read-only tenants only" % alias)
        project = req.get("project") or ""
        self._readonly_caller(uid, caller, alias, project)
        used: list[str] = []
        found = self._readonly_listing(uid, alias, tenant, entry, project, used)
        rows = [{"name": p["name"], "id": p["id"], "region": p["region"]} for p in readonly.readable(found, entry)]
        answer = {"ok": True, "status": 200, "tenant": alias, "regions": list(entry["regions"]), "projects": rows,
                  "other": len(found) - len(rows)}
        return self._mask(answer, tenant, used)

    def _call_readonly(self, req: dict, uid: int, caller, alias: str, tenant: dict, entry: dict) -> dict:
        """A call to a read-only tenant (RT): GET and HEAD only for every role and caller, no body, the read key or
        a token of the login; a session only from a project the owner granted this tenant. A `scope` names the
        project of the tenant to read (T2): its region decides the endpoints."""
        method = str(req.get("method") or "GET").upper()
        role = req.get("role") or "read"
        service, path = req.get("service"), req.get("path")
        if method not in readonly.METHODS:
            raise Refused(readonly.METHOD_LINE % alias)
        if role != "read":
            raise Refused(readonly.ROLE_LINE % alias)
        if req.get("body") is not None:
            raise Refused(readonly.BODY_LINE % alias)
        scope = req.get("scope")
        if scope is not None:
            try:
                readonly.check_scope(scope)
            except readonly.ReadonlyError as err:
                raise Refused(str(err)) from None
        regions = entry["regions"]
        region = req.get("region") or regions[0]
        if region not in regions:
            raise Refused("the region is not one of the tenant's")
        project = req.get("project") or ""
        self._readonly_caller(uid, caller, alias, project)
        if not isinstance(service, str) or not re.match(r"^[a-z][a-z0-9-]{1,30}$", service):
            raise Refused("a service name reads like ecs or vpc")
        if not isinstance(path, str) or not _PATH_RE.match(path) or ".." in path or "//" in path:
            raise Refused("a path is plain: /v1/{project_id}/cloudservers")
        query = req.get("query") or {}
        if not isinstance(query, dict) or not all(isinstance(k, str) and isinstance(v, (str, list))
                                                  for k, v in query.items()):
            raise Refused("a query is an object of strings")
        if service == "obs":
            if entry["auth"] == "login":
                raise Refused(readonly.OBS_LINE % alias)
            if scope is not None:
                raise Refused("refused: an OBS call names no project: the buckets are read by region")
            bucket = path[1:].partition("/")[0]
            if bucket in self.exchange_buckets():
                raise Refused("the exchange buckets are reached through awb xchg only")
            if self._session_read(uid) and not self._obs_read_ok(bucket, path[1:].partition("/")[2], query, project):
                raise Refused("an OBS read reaches only a bucket named after the project (%s-...)" % project)
        used: list[str] = []
        project_id, headers = None, None
        if scope is not None:
            listing = self._readonly_listing(uid, alias, tenant, entry, project, used)
            try:
                target = readonly.choose(listing, scope, alias, entry)
            except readonly.ReadonlyError as err:
                raise Refused(str(err)) from None
            if req.get("region") and req["region"] != target["region"]:
                raise Refused("refused: the project %s is in %s, not in %s" % (scope, target["region"], req["region"]))
            region, project_id = target["region"], target["id"]
            if entry["auth"] == "key":
                headers = {"X-Project-Id": project_id}
        makers = self._readonly_makers(uid, alias, tenant, entry, region, project, used, project_id)
        answer = self._readonly_send(makers, method, service, path, query, headers)
        self._log(uid, alias, "read", method, service, region, answer["status"], project)
        return self._mask(answer, tenant, used)

    def _session_read(self, uid: int) -> bool:
        """True for a call of a session (the work user), false for the owner's own and the console's."""
        return uid != os.getuid() and uid != self._console_uid()

    @staticmethod
    def _obs_read_ok(bucket: str, key: str, query, project) -> bool:
        """The OBS reads of a session: the list of the buckets, the tags of any bucket (what the sweep asks) and
        anything in a bucket named after its project. The exchange buckets are refused before this."""
        if bucket == "":
            return True
        if isinstance(query, dict) and list(query) == ["tagging"] and not key:
            return True
        return isinstance(project, str) and bool(_PROJECT_RE.match(project)) and bucket.startswith(project + "-")

    def _lab_write_allowed(self, method: str, service: str, path: str, query=None) -> bool:
        """True when one METHOD service path triple of the lab_writes line of keys.conf is this write, matched
        whole (xchg.lab_write_matches); False without the line or with a line that does not parse."""
        from awb.tcp import xchg

        value = self.settings.get("lab_writes")
        if value is None:
            return False
        triples = xchg.lab_writes_parse(str(value)) or []
        return any(xchg.lab_write_matches(t, method, service, path, query) for t in triples)

    def exchange_buckets(self) -> list[str]:
        """The lab bucket and the owner bucket of keys.conf (or their defaults): a leased key never reaches them."""
        from awb.tcp import xchg

        names = {str(self.settings.get(k) or xchg.DEFAULTS[k]) for k in ("lab_bucket", "owner_bucket")}
        return sorted(n for n in names if _BUCKET_RE.match(n))

    def lease_policy(self, project: str = "") -> dict:
        """The policy of a leased key: the tf_services of keys.conf (all their actions); OBS only on the buckets of
        the project, named <code>-..., and only with a project code; every IAM action denied and every action on
        the two exchange buckets denied."""
        # a tf_services line of keys.conf is the list, an empty one included (no service); no line is the default
        value = self.settings.get("tf_services")
        listed = [s.strip() for s in (str(value) if value is not None else ",".join(TF_SERVICES)).split(",")]
        services = [s for s in listed if re.match(r"^[a-z][a-z0-9]{1,20}$", s) and s != "iam"]
        statements = []
        plain = [s for s in services if s != "obs"]
        if plain:
            statements.append({"Effect": "Allow", "Action": ["%s:*:*" % s for s in plain]})
        if "obs" in services and isinstance(project, str) and _PROJECT_RE.match(project):
            statements.append({"Effect": "Allow", "Action": ["obs:*:*"],
                               "Resource": ["obs:*:*:bucket:%s-*" % project, "obs:*:*:object:%s-*/*" % project]})
        statements.append({"Effect": "Deny", "Action": ["iam:*:*"]})
        statements.append({"Effect": "Deny", "Action": ["obs:*:*"],
                           "Resource": [r for b in self.exchange_buckets()
                                        for r in ("obs:*:*:bucket:%s" % b, "obs:*:*:object:%s/*" % b)]})
        return {"Version": "1.1", "Statement": statements}

    def _bucket_pair(self, alias: str, pair: dict) -> bool:
        """True when `pair` is the lab key of the bucket tenant: that key reaches both exchange buckets, so no lease
        is ever minted from it (finding 1 of the review of 2026-10-10)."""
        tenant = self.settings.get("bucket_tenant")
        if not tenant:
            return False
        if alias == tenant:
            return True
        with self.lock:
            bucket = (self.tenants.get(tenant) or {}).get("roles", {}).get("lab") or {}
        return bool(bucket.get("ak")) and bucket.get("ak") == pair.get("ak")

    def _lease(self, req: dict, uid: int, caller=_NOT_ASKED) -> dict:
        """A temporary AK/SK and security token of the tenant's lab key (Variant B of DESIGN-terraform.md): for a
        session only from the folder of an active project of the kind project that the owner granted (P0), for the
        owner for any project; never from the lab key of the bucket tenant; 15 to 60 minutes; the policy of
        lease_policy for the project. The answer carries the key: the session reads it, by design, for at most an
        hour."""
        from awb.tcp import cloud

        alias, project = req.get("tenant"), req.get("project") or ""
        minutes = req.get("minutes")
        if not (isinstance(minutes, int) and not isinstance(minutes, bool) and
                LEASE_MINUTES[0] <= minutes <= LEASE_MINUTES[1]):
            raise Refused("a lease lasts 15 to 60 minutes")
        with self.lock:
            if not self.tenants:
                raise Refused("the key service is locked: the owner runs awb keys unlock")
            tenant = self.tenants.get(alias) if isinstance(alias, str) else None
        if tenant is None:
            raise Refused("the key service holds no tenant of that alias")
        if self._readonly(alias) is not None:
            raise Refused(readonly.LEASE_LINE % alias)
        pair = tenant.get("roles", {}).get("lab")
        if not pair:
            raise Refused("the tenant has no lab key")
        if self._bucket_pair(alias, pair):
            raise Refused("%s holds the exchange buckets: its lab key gives no lease" % alias)
        if uid != os.getuid():
            from awb import projects as _projects

            if uid == self._console_uid():
                raise Refused("the console takes no lease")
            if caller is not _NOT_ASKED and caller != project:
                # K4: a lease reaches the OBS buckets of its project, so it is bound like a call
                raise self._unbound(project, caller, "a lease is for the folder of an active project")
            row = self._project_row(project)
            if row is None:
                raise Refused("a lease is for the folder of an active project")
            if not _projects.tenant_access(row.kind) or not self._granted(project):
                raise Refused("%s has no grant of the owner: he runs awb projects kind %s project in his own "
                              "terminal" % (project, project))
            if self._customer_differs(row):
                from awb.tcp import xchg

                raise Refused(xchg.DIFFERS_LINE % project)
        regions = self._registered().get(alias)
        if not regions:
            raise Refused("the tenant is not registered (awb tenant add)")
        region = req.get("region") or regions[0]
        if region not in regions:
            raise Refused("the region is not one of the tenant's")
        body = {"auth": {"identity": {"methods": ["token"], "token": {"duration_seconds": minutes * 60},
                                      "policy": self.lease_policy(project)}}}
        client = cloud.Client(obs.Keys(pair["ak"], pair["sk"]), region, endpoint=self.endpoint, label=alias,
                              timeout=60.0)
        try:
            r = client.request("POST", "iam", "/v3.0/OS-CREDENTIAL/securitytokens", body=body)
        except cloud.CloudError as err:
            self._log(uid, alias, "lab", "LEASE", "iam", region, err.status or 0, project)
            raise Refused("the lease failed: no answer from IAM") from None
        cred = (r.data or {}).get("credential") if isinstance(r.data, dict) else None
        if not r.ok or not isinstance(cred, dict) or not all(isinstance(cred.get(k), str) and cred.get(k)
                                                             for k in ("access", "secret", "securitytoken")):
            self._log(uid, alias, "lab", "LEASE", "iam", region, r.status, project)
            code = (r.data or {}).get("error", {}).get("code") if isinstance(r.data, dict) and isinstance(
                (r.data or {}).get("error"), dict) else None
            raise Refused("the lease failed: IAM answered HTTP %d%s" % (
                r.status, " %s" % code if isinstance(code, str) and re.match(r"^[A-Za-z0-9._]{1,30}$", code) else ""))
        self._log(uid, alias, "lab", "LEASE", "iam", region, "%d %s %dmin" % (r.status, cred["access"][:4], minutes),
                  project)
        return {"ok": True, "tenant": alias, "region": region, "minutes": minutes, "ak": cred["access"],
                "sk": cred["secret"], "token": cred["securitytoken"], "expires_at": cred.get("expires_at")}

    def _mask(self, answer: dict, tenant: dict, drawn: list[str] = ()) -> dict:
        text = json.dumps(answer, ensure_ascii=False)
        values = [v for pair in tenant.get("roles", {}).values() for v in pair.values()]
        values += list(tenant.get("secrets", {}).values()) + list(drawn)
        values += [v for k, v in (tenant.get("login") or {}).items() if k == "password"]
        values += self.tokens.values()
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


def _old_lab_writes(settings: dict) -> bool:
    """True when the lab_writes line of the settings is the old service:path-prefix form (K9)."""
    from awb.tcp import xchg

    value = settings.get("lab_writes")
    return value is not None and xchg.lab_writes_parse(str(value)) is None and xchg.lab_writes_old_form(str(value))


def _without_writes(tenants: dict, settings: dict) -> dict:
    """The tenants with the lab key and the secrets of every read-only tenant left out (RT): what the store holds
    beside its read key or login is never used."""
    held = readonly.of_settings(settings)
    out = {}
    for alias, t in tenants.items():
        if alias in held and isinstance(t, dict):
            t = dict(t, roles={r: v for r, v in (t.get("roles") or {}).items() if r == "read"}, secrets={})
        out[alias] = t
    return out


def _hold(svc: Service, line: dict) -> None:
    """Put what the line carries into the new service (a stand-in child of the tests replaces it)."""
    with svc.lock:
        svc.tenants = line["tenants"]
        svc.settings = dict(line["settings"])


def _take_over(fd: int, endpoint: str | None) -> Service | None:
    """The child's side of a reload (design 8.4): takes the two listening sockets and what the service before
    held, answers the summary and the digest over what it holds, starts accepting on go. None on a failure."""
    from awb import handover

    deadline = time.monotonic() + handover.HANDOVER_DEADLINE
    try:
        pair = handover._Pair(socket.socket(fileno=fd))
    except (OSError, ValueError):
        return None
    line = pair.read(deadline)
    svc: Service | None = None
    servers: dict = {}
    done = False
    try:
        if not line or not isinstance(line.get("tenants"), dict) or not isinstance(line.get("settings"), dict):
            pair.send({"ok": False, "error": "failed"})
            return None
        try:
            servers = {True: handover._inherited(line.get("admin_fd")), False: handover._inherited(line.get("call_fd"))}
            svc = Service(Path(line["admin_path"]), Path(line["call_path"]), endpoint=endpoint)
        except (OSError, ValueError, TypeError, KeyError):
            pair.send({"ok": False, "error": "failed"})
            return None
        svc.start(inherited=servers, accept=False)
        _hold(svc, line)
        with svc.lock:
            digest = _digest(svc.tenants, svc.settings)
        if not pair.send({"ok": True, "pid": os.getpid(), "release": handover.release_path(),
                          "tenants": svc._summary(), "digest": digest}):
            return None
        digest = None
        if pair.read(deadline) != {"go": True}:
            return None
        svc.accept_now()
        if not pair.send({"serving": True}):
            return None
        done = True
    finally:
        line = None
        if not done:
            if svc is not None:
                svc.stopping.set()
                svc._close_servers(unlink=False)
            else:
                for srv in servers.values():
                    srv.close()
            pair.close()
    pair.wait_end(deadline)
    pair.close()
    handover.sd_notify("READY=1")
    return svc


# --------------------------------------------------------------------------- clients


def call_socket() -> Path:
    return Path(os.environ.get(CALL_SOCKET_ENV) or DEFAULT_CALL_SOCKET)


def admin_socket() -> Path:
    env = os.environ.get(ADMIN_SOCKET_ENV)
    return Path(env) if env else config.paths().vault / "keys-admin.sock"


def own_token(start: Path | None = None) -> str | None:
    """K4: the key token of the project folder this process works in (at or above `start`, default the working
    folder); None outside a project folder. The work user writes one into a project folder that has none yet (a
    project spawned before the token); the owner's next grant of the project records it."""
    from awb import projects

    try:
        folder = config.scope_root(Path(os.path.abspath(start or Path.cwd())))
        if folder is None:
            return None
        token = projects.read_key_token(folder)
        if token is None and config.is_work_user() and not os.path.lexists(folder / projects.BIND_FILE):
            token = projects.write_key_token(folder)
        return token
    except (OSError, ValueError):
        return None


def request(sock: Path, obj: dict, timeout: float = CALL_TIMEOUT + 10, *, upload: Path | None = None,
            download: Path | None = None) -> dict:
    """One request. `upload` sends a file's bytes after the request line (a put); `download` receives the bytes
    that follow an answer carrying a length (a get). A call, a lease, a doc_get and a projects carry the key token
    of the project folder this process works in (K4)."""
    if isinstance(obj, dict) and obj.get("op") in ("call", "lease", "doc_get", "projects") and "token" not in obj:
        token = own_token()
        if token is not None:
            obj = dict(obj, token=token)
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


def send_mail(subject: str, message: str) -> str:
    """A mail through the SMN topic of keys.conf, sent by the key service's notify (the admin socket admits the
    owner's uid). Returns a word for the log: "sent", or "not sent" and why. The one mail client of the owner's
    processes: awb ui watch and the build runner (F1 step 7)."""
    try:
        answer = request(admin_socket(), {"op": "notify", "subject": subject, "message": message}, timeout=60)
    except OSError:
        return "not sent: the key service is not reachable"
    if not answer.get("ok"):
        return "not sent: the key service refused (%s)" % ("deploy the notify operation" if answer.get("error")
                                                          == "unknown operation" else "refused")
    return "sent" if answer.get("sent") else "not sent: no notify_topic or no lab key of bucket_tenant"


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
    import argparse

    sub = ap.add_subparsers(dest="command")
    s = sub.add_parser("serve", help="run the service (systemd, as the owner)")
    s.add_argument("--takeover", type=int, default=None, metavar="FD", help=argparse.SUPPRESS)
    sub.add_parser("unlock", help="load the keys and secrets from the password store (owner)")
    sub.add_parser("lock", help="forget every key and secret (owner)")
    sub.add_parser("reload", help="hand the service to a new process of the installed code, keys kept (owner)")
    s = sub.add_parser("lab-writes", help="print the lab_writes line of keys.conf for the recipes (owner)")
    s.add_argument("--from-recipes", action="store_true", required=True,
                   help="the union of the write calls of every recipe and of the recheck watch")
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
            from awb import handover

            handover.no_dump()
            endpoint = os.environ.get("AWB_CLOUD_ENDPOINT") or None
            if args.takeover is not None:
                svc = _take_over(args.takeover, endpoint)
                if svc is None:
                    return 1
            else:
                svc = Service(admin_socket(), call_socket(), endpoint=endpoint)
                svc.start()
                handover.sd_notify("READY=1")
            svc.serve_forever()
            return 0
        if args.command in ("unlock", "lock", "reload") and config.is_work_user():
            print("awb keys: %s is for the owner" % args.command, file=sys.stderr)
            return 2
        if args.command == "unlock":
            summary = unlock()
            _print_summary(summary)
            from awb.tcp import xchg

            value = xchg.read_settings().get("lab_writes")
            if value is not None and _old_lab_writes({"lab_writes": value}):
                print(LAB_WRITES_OLD_LINE)
            elif value is not None:
                print("lab_writes: %d triples" % len(xchg.lab_writes_parse(value) or []))
            else:
                print("lab_writes: no line, the lab key signs no write (awb keys lab-writes --from-recipes)")
            return 0
        if args.command == "lab-writes":
            # a child process and no import: lab.py and recheckwatch.py belong to no unit, so a change of them must
            # not restart this service and with it forget the keys
            import subprocess

            done = subprocess.run([sys.executable, "-m", "awb.tcp.labwrites"], capture_output=True, text=True)
            sys.stdout.write(done.stdout)
            sys.stderr.write(done.stderr)
            return done.returncode
        if args.command == "reload":
            from awb import handover

            answer = request(admin_socket(), {"op": "reload"}, timeout=handover.RELOAD_CLIENT_TIMEOUT)
            if not answer.get("ok"):
                detail = answer.get("detail")
                print("awb keys: %s%s" % (answer.get("error"), ": %s" % detail if isinstance(detail, str) else ""),
                      file=sys.stderr)
                return 2
            release = answer.get("release")
            print("keys %s, pid %s, release %s" % (", ".join(sorted(answer.get("tenants") or {})) or "locked",
                                                    answer.get("pid"), os.path.basename(release)
                                                    if isinstance(release, str) else "unknown"))
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
        kind = "  kind: readonly (%s)" % t.get("auth") if t.get("kind") == "readonly" else ""
        print("%-12s keys: %-10s secrets: %s%s%s" % (alias, ", ".join(t["roles"]) or "-",
                                                     ", ".join(t["secrets"]) or "-",
                                                     "  regions: %s" % regions if regions else "", kind))


if __name__ == "__main__":
    sys.exit(main())
