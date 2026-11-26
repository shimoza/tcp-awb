"""The owner's bucket: one working folder per project, `<YYYY-MM>/<project-code>/` with `in/` for his originals
and `out/` for results (his decisions of 2026-09-24).

    awb bucket sync                         the folders of every active project; folders that match no project
    awb bucket folder CODE                  the folder of one project, created when missing
    awb bucket pull CODE [--again]          new files of in/ into the vault inbox, then the intake of its customer
    awb bucket put CODE FILE [--replace] [--reveal]   FILE into out/; --reveal puts the real names back first;
                                            a Markdown FILE goes as FILE.md and FILE.html (paste into a mail);
                                            it ends with the line "get it: ..." (Total Commander or the laptop);
                                            an older version of its group in out/ goes to _archive/<stamp>/
                                            first, a file the same as one there is not put again
    awb bucket move <YYYY-MM>/<NAME>/ CODE  a folder whose name drifted to <YYYY-MM>/<CODE>/: copy, check, delete

A folder is looked for under every month and used where it is. A new one goes under the month the project was
created. The project code is the fixed point: a drifted folder is renamed in the bucket, never the project.

Owner side only. The bucket takes the owner's IAM user alone, and that key stays in the key service: every call
goes through its admin socket (`awb keys unlock` loads it; F1 step 11). There is no bucket key in the environment or
in the host file. A working session must never see an original before the intake: as the work user of a sealed host
every command refuses, and inside an assistant session of the owner's own uid `pull` and `move` refuse and the
client gives no file of `in/` (review R6: the key service cannot tell the peer's session, so the check stays on the
client as it does for `awb fetch` and `awb vault show`). `pull` and `put --reveal` read the vault, so he runs them in
his own shell. The one way in from such a session is `awb import CODE` of the bucket folder (UX10, his yes
2026-11-12): it fetches in/ under `import_door()` straight into the vault inbox and the intake, and prints counts.

The key service takes list, get and put, inside the folders of the layout (awb/bucket_layout.py), and one delete:
`drop_in`, a file of in/ of an active project after `awb import --auto` took it into the vault, and one move:
`archive`, a file of out/ into `<YYYY-MM>/<code>/_archive/<stamp>/` of its folder, copied, compared (size and md5)
and only then deleted (task BK4). `put` uses it for an older version of the same group (the group rule of
awb/inventory.py: project, normalised stem, suffix): the newest stays in out/, as `awb files plan` would have it.
`move` needs a copy and a delete and stops with a line saying so. A locked or stopped key service stops every command with one
line that names `awb keys unlock`. Output carries codes, months, counts and sizes, never the name of a file of in/
and never the name of a folder that matches no project.
"""
from __future__ import annotations

import contextlib
import contextvars
import fcntl
import functools
import hashlib
import html
import json
import mimetypes
import os
import re
import secrets
import shutil
import socket
import stat
import sys
import tempfile
import unicodedata
from collections import Counter
from datetime import date
from pathlib import Path

from awb import config, obs
from awb.bucket_layout import IN, KEEP, KEEP_TEXT, MONTH, OUT, OWNER_BUCKET, REGION, TEXT_SUFFIXES, folder_of, is_keep

PULLED = "bucket-pulled.tsv"
ORIGINALS = ("pull", "move")
"""The commands that reach the originals of in/ (`move` carries a whole folder): the owner's own terminal only.
The fetch of `awb import` is refused by `awb import` itself, and the client gives no file of in/ to a session."""
_MONTH_RE = re.compile(MONTH + "/")
_UNSAFE_RE = re.compile(r"[\x00-\x1f/\\\x7f]")
_IN_SEGMENT = IN.rstrip("/")


class BucketError(Exception):
    """A bucket step cannot be done. The message carries codes, months and counts only."""


def _plain(value) -> bool:
    return isinstance(value, str) and not any(unicodedata.category(c).startswith("C") for c in value)


def _key(value) -> bool:
    return (_plain(value) and bool(value) and "\\" not in value
            and all(part not in (".", "..", "") for part in value.removesuffix("/").split("/")))


@contextlib.contextmanager
def _directory(path: Path, *, create: bool = False):
    """Pin all path components, including parents, before accessing a local file."""
    fd = os.open("/", os.O_RDONLY | os.O_DIRECTORY)
    try:
        for part in Path(os.path.abspath(path)).parts[1:]:
            if create:
                try:
                    os.mkdir(part, 0o700, dir_fd=fd)
                except FileExistsError:
                    pass
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            os.close(fd)
            fd = child
        yield fd
    except (OSError, ValueError, UnicodeError):
        raise BucketError("a bucket file or folder cannot be accessed safely") from None
    finally:
        os.close(fd)


@contextlib.contextmanager
def _regular(directory: int, name: str, flags: int = os.O_RDONLY):
    fd = os.open(name, flags | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600, dir_fd=directory)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise BucketError("a bucket file must be regular and have no links")
        with os.fdopen(fd, "r+b" if flags & os.O_RDWR else "rb", closefd=False) as stream:
            yield stream
    finally:
        os.close(fd)


@contextlib.contextmanager
def _stage(directory: int):
    name = ".bucket-" + secrets.token_hex(16)
    fd = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=directory)
    try:
        with os.fdopen(fd, "wb") as stream:
            yield name, stream
    finally:
        try:
            os.unlink(name, dir_fd=directory)
        except FileNotFoundError:
            pass


def _serialized(function):
    @functools.wraps(function)
    def run(p, *args, **kwargs):
        # The directory itself is the lock: no replaceable lock file and one lock across processes and threads.
        with _directory(p.vault, create=True) as directory:
            fcntl.flock(directory, fcntl.LOCK_EX)
            try:
                return function(p, *args, **kwargs)
            finally:
                fcntl.flock(directory, fcntl.LOCK_UN)
    return run


def _public(text: str, p: config.Paths) -> str:
    """Keep ordinary output readable: a text with a control character is withheld, a name or a secret in it is not
    (B-089: no content check of a key or an object name). `p` is kept for the callers."""
    return text if _plain(text) else "[withheld]"


# --------------------------------------------------------------------------- settings and client


ADMIN_SOCKET_ENV = "AWB_KEYS_ADMIN"
TIMEOUT = 190
MAX_ANSWER = 8 * 1024 * 1024
LOCKED_LINE = "the key service is locked or not running: the owner runs awb keys unlock"


def settings() -> dict[str, str | None]:
    if config.is_work_user():
        raise BucketError("the bucket is the owner's: run awb bucket in your own shell")
    return {"bucket": OWNER_BUCKET, "region": REGION}


_IMPORT_DOOR = contextvars.ContextVar("awb_bucket_import_door", default=False)


@contextlib.contextmanager
def import_door():
    """The fetch of in/ by `awb import CODE` (awb/importcmd.py `_from_bucket`): inside an assistant session of the
    owner user the originals go straight into the vault inbox and the intake, as UX10 allows. `awb bucket pull`,
    `move` and every other get of in/ stay refused there."""
    token = _IMPORT_DOOR.set(True)
    try:
        yield
    finally:
        _IMPORT_DOOR.reset(token)


def owner_terminal(what: str) -> None:
    """`pull` and `move` and a get of a file of in/ reach the originals: refused inside an assistant session, as
    `awb import` and `awb fetch` are (review R6). The uid alone is not enough: the steering session runs as the
    owner."""
    from awb import vault

    if vault.in_assistant_session() and not _IMPORT_DOOR.get():
        raise BucketError("%s never runs inside an assistant session; run it in your own terminal" % what)


def _is_original(key) -> bool:
    """A key under an in/ folder: `<month>/<code>/in/<file>` and any other key with an in/ segment."""
    return isinstance(key, str) and _IN_SEGMENT in key.split("/")[:-1]


class KeyServiceClient:
    """The calls of `obs.Client` that the owner's commands use, answered by the key service on its admin socket:
    list, head (a list of the one key), get, put_bytes and put_file. `copy` and `delete` are not offered."""

    can_copy = False

    def __init__(self, sock: Path | None = None):
        self.sock = sock

    def _ask(self, req: dict, *, upload: Path | None = None, download: Path | None = None) -> dict:
        """One bounded reply line; only a complete successful transfer can replace a local file."""
        with contextlib.ExitStack() as stack:
            source = None
            if upload is not None:
                directory = stack.enter_context(_directory(upload.parent))
                source = stack.enter_context(_regular(directory, upload.name))
                req = dict(req, length=os.fstat(source.fileno()).st_size)
            sock = self.sock or Path(os.environ.get(ADMIN_SOCKET_ENV) or config.paths().vault / "keys-admin.sock")
            try:
                s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                stack.callback(s.close)
                s.settimeout(TIMEOUT)
                s.connect(str(sock))
                s.sendall(json.dumps(req).encode("utf-8") + b"\n")
                if source is not None:
                    remaining = req["length"]
                    try:
                        while remaining:
                            chunk = source.read(min(1 << 16, remaining))
                            if not chunk:
                                raise BucketError("the upload changed while being read")
                            s.sendall(chunk)
                            remaining -= len(chunk)
                    except (BrokenPipeError, ConnectionResetError):
                        pass  # A refusal may arrive before all upload bytes have been sent.
                buf = bytearray()
                while b"\n" not in buf:
                    chunk = s.recv(min(1 << 16, MAX_ANSWER + 1 - len(buf)))
                    if not chunk:
                        raise BucketError("the key service gave no readable answer")
                    buf += chunk
                    if (buf.find(b"\n") if b"\n" in buf else len(buf)) > MAX_ANSWER:
                        raise BucketError("the answer of the key service is too large")
                head, _, rest = bytes(buf).partition(b"\n")
                try:
                    def pairs(items):
                        out = {}
                        for key, value in items:
                            if key in out:
                                raise ValueError()
                            out[key] = value
                        return out
                    def constant(value):
                        raise ValueError()
                    answer = json.loads(head, object_pairs_hook=pairs, parse_constant=constant)
                except (ValueError, UnicodeError, RecursionError):
                    raise BucketError("the key service gave no readable answer") from None
                if not isinstance(answer, dict) or type(answer.get("ok")) is not bool:
                    raise BucketError("the key service gave no readable answer")
                if not answer["ok"]:
                    if answer.get("kind") == "locked" or "locked" in str(answer.get("error")):
                        raise BucketError(LOCKED_LINE)
                    if answer.get("kind") == "http":
                        status = answer.get("status")
                        status = status if type(status) is int and 100 <= status <= 599 else 0
                        raise obs.OBSError("the key service: HTTP %d" % status, status)
                    # Retain the useful established archive failure hint without echoing the service text.
                    if "md5" in str(answer.get("error")):
                        raise BucketError("the key service: md5 comparison failed")
                    raise BucketError("the key service refused the request")
                if download is not None:
                    length = answer.get("length")
                    if type(length) is not int or length < 0:
                        raise BucketError("the key service gave no valid file length")
                    if req.get("if_match") is not None and answer.get("etag") != req["if_match"].strip('"'):
                        raise BucketError("the file changed since it was listed")
                    with _directory(download.parent) as directory:
                        try:
                            with _regular(directory, download.name):
                                pass
                        except FileNotFoundError:
                            pass
                        with _stage(directory) as (name, out):
                            digest = hashlib.md5()
                            digest.update(rest[:length])
                            out.write(rest[:length])
                            got = min(len(rest), length)
                            while got < length:
                                chunk = s.recv(min(1 << 16, length - got))
                                if not chunk:
                                    raise BucketError("the file from the key service ended early")
                                out.write(chunk)
                                digest.update(chunk)
                                got += len(chunk)
                            expected = (req.get("if_match") or "").strip('"')
                            if re.fullmatch(r"[a-fA-F0-9]{32}", expected) and digest.hexdigest() != expected.lower():
                                raise BucketError("the file changed since it was listed")
                            out.flush()
                            os.fsync(out.fileno())
                            # Replace a directory entry, never truncate through a newly planted link.
                            os.replace(name, download.name, src_dir_fd=directory, dst_dir_fd=directory)
                return answer
            except (FileNotFoundError, ConnectionRefusedError, PermissionError):
                raise BucketError(LOCKED_LINE) from None
            except (OSError, ValueError, UnicodeError) as err:
                raise BucketError("no answer from the key service (%s)" % type(err).__name__) from None

    def _listing(self, op: str, prefix: str, delimiter: str) -> obs.Listing:
        a = self._ask({"op": op, "prefix": prefix, "delimiter": delimiter})
        try:
            objects, prefixes, modified = a["objects"], a["prefixes"], a["modified"]
            if not isinstance(objects, list) or not isinstance(prefixes, list) or not isinstance(modified, dict):
                raise ValueError()
            seen = set()
            for row in objects:
                if not isinstance(row, (list, tuple)) or len(row) != 3:
                    raise ValueError()
                key, size, etag = row
                if (not _key(key) or not key.startswith(prefix) or key in seen or type(size) is not int
                        or size < 0 or not _plain(etag)):
                    raise ValueError()
                seen.add(key)
            if not all(_key(k) and k.startswith(prefix) and k.endswith("/") for k in prefixes):
                raise ValueError()
            if not all(_key(k) and k.startswith(prefix) and _plain(v) for k, v in modified.items()):
                raise ValueError()
            return obs.Listing([tuple(row) for row in objects], prefixes, modified)
        except (KeyError, TypeError, ValueError):
            raise BucketError("the key service gave no valid listing") from None

    def list(self, prefix: str = "", delimiter: str = "", page: int = 0) -> obs.Listing:
        return self._listing("bucket_list", prefix, delimiter)

    def lab_list(self, prefix: str = "", delimiter: str = "") -> obs.Listing:
        return self._listing("lab_list", prefix, delimiter)

    def head(self, key: str) -> dict | None:
        for k, size, etag in self.list(key).objects:
            if k == key:
                return {"ETag": '"%s"' % etag, "Content-Length": str(size)}
        return None

    def get(self, key: str, to: Path, if_match: str | None = None) -> Path:
        if _is_original(key):
            owner_terminal("a get of a file of in/")       # before the socket: an original never reaches a session
        self._ask({"op": "bucket_get", "key": key, "if_match": if_match}, download=Path(to))
        return Path(to)

    def put_file(self, key: str, path: Path, content_type: str = "application/octet-stream") -> None:
        self._ask({"op": "bucket_put", "key": key, "content_type": content_type}, upload=Path(path))

    def put_bytes(self, key: str, data: bytes, content_type: str = "application/octet-stream") -> None:
        with tempfile.TemporaryDirectory() as tmp:
            f = Path(tmp) / "put"
            f.write_bytes(data)
            self.put_file(key, f, content_type)

    def copy(self, source: str, target: str) -> None:
        raise BucketError("the key service has no copy: move a folder in the object storage console")

    def delete(self, key: str) -> None:
        raise BucketError("the key service has no delete: move a folder in the object storage console")

    def archive(self, key: str, target: str) -> dict:
        """Move a file of out/ to `<YYYY-MM>/<code>/_archive/<stamp>/<name>` of its folder (task BK4): the key service
        copies, compares the size and the md5, then deletes the source (`bucket_archive`). A move that failed or was
        refused raises BucketError and leaves the source."""
        return self._ask({"op": "bucket_archive", "key": key, "target": target})

    def drop_in(self, key: str) -> None:
        """Delete one file of a project's in/ after `awb import --auto` took it into the vault (the one delete the
        key service offers, `bucket_drop_in`: a file of in/ of an active project and nothing else)."""
        if not _is_original(key):
            raise BucketError("only a file of in/ is dropped after its import")
        owner_terminal("a drop of a file of in/")
        self._ask({"op": "bucket_drop_in", "key": key})


def client() -> KeyServiceClient:
    settings()      # the work user is refused here
    return KeyServiceClient()


# --------------------------------------------------------------------------- the layout


def months(c) -> list[str]:
    return [m for m in c.list("", "/").prefixes if _MONTH_RE.fullmatch(m)]


def project_folders(c) -> list[tuple[str, str]]:
    """(month folder, name) of every folder one level below a month."""
    out = []
    for month in months(c):
        for pref in c.list(month, "/").prefixes:
            out.append((month, pref[len(month):].rstrip("/")))
    return out


def find_folder(c, code: str) -> str | None:
    """The folder of a project under any month, None when there is none. Refused when two months hold one."""
    found = [month + code + "/" for month, name in project_folders(c) if name == code]
    if len(found) > 1:
        raise BucketError("%s has a folder under more than one month (%s): bring them together first"
                          % (code, ", ".join(f.split("/")[0] for f in found)))
    return found[0] if found else None


def _project(p: config.Paths, code: str):
    from awb import projects
    for row in projects.load(p):
        if row.code == code:
            return row
    raise BucketError("not a registered project")


def ensure_folder(c: obs.Client, project) -> tuple[str, bool]:
    """The folder of `project` with its in/ and out/ and a .keep file in each (some S3 clients cannot open an empty
    folder), created under the month of the project when missing. Returns (folder, created)."""
    folder = find_folder(c, project.code)
    created = folder is None
    if created:
        folder = folder_of(project.created[:7], project.code)
    for key in (folder, folder + IN, folder + OUT):
        if c.head(key) is None:
            c.put_bytes(key, b"")
    for key in (folder + IN + KEEP, folder + OUT + KEEP):
        if c.head(key) is None:
            c.put_bytes(key, KEEP_TEXT)
    return folder, created


def sync(p: config.Paths, c: obs.Client) -> list[str]:
    from awb import projects
    rows = projects.load(p)
    known = {r.code for r in rows}
    lines = []
    for r in rows:
        if r.state not in projects.LIVE_STATES:
            continue
        folder, created = ensure_folder(c, r)
        lines.append("%s  %s%s" % (r.code, folder, "  created" if created else ""))
    drift = Counter(month for month, name in project_folders(c) if name not in known)
    for month, n in sorted(drift.items()):
        lines.append("%s  %d folder(s) match no project; name them after their project with awb bucket move"
                     % (month.rstrip("/"), n))
    return lines


# --------------------------------------------------------------------------- pull


def _pulled_file(p: config.Paths) -> Path:
    return p.vault / PULLED


def pulled(p: config.Paths) -> set[tuple[str, str]]:
    with _directory(p.vault, create=True) as directory:
        try:
            with _regular(directory, PULLED) as source:
                raw = source.read(MAX_ANSWER + 1)
        except FileNotFoundError:
            return set()
        try:
            if len(raw) > MAX_ANSWER or (raw and not raw.endswith(b"\n")):
                raise ValueError()
            result = set()
            for line in raw.decode("utf-8").splitlines():
                key, etag, when = line.split("\t")
                if not _key(key) or not _plain(etag) or date.fromisoformat(when).isoformat() != when:
                    raise ValueError()
                result.add((key, etag))
            return result
        except (ValueError, UnicodeError):
            raise BucketError("the bucket pull history is malformed") from None


def _remember(p: config.Paths, key: str, etag: str) -> None:
    if not _key(key) or not _plain(etag):
        raise BucketError("the bucket pull history needs a valid key and etag")
    with _directory(p.vault) as directory, _regular(directory, PULLED, os.O_RDWR | os.O_CREAT) as out:
        fcntl.flock(out.fileno(), fcntl.LOCK_EX)
        os.fchmod(out.fileno(), 0o600)
        out.seek(0, os.SEEK_END)
        before = out.tell()
        try:
            row = ("%s\t%s\t%s\n" % (key, etag, date.today().isoformat())).encode("utf-8")
            if os.write(out.fileno(), row) != len(row):
                raise OSError("short history write")
            os.fsync(out.fileno())
        except OSError:
            os.ftruncate(out.fileno(), before)
            raise


def _local_name(rel: str, folder: Path | int) -> str:
    base = _UNSAFE_RE.sub(" ", rel.rsplit("/", 1)[-1]).strip() or "file"
    if base.startswith("."):
        base = "_" + base
    stem, dot, suffix = base.rpartition(".")
    if not dot:
        stem, suffix = base, ""
    def exists(name):
        try:
            if isinstance(folder, int):
                os.stat(name, dir_fd=folder, follow_symlinks=False)
            else:
                (folder / name).lstat()
            return True
        except FileNotFoundError:
            return False

    name, n = base, 1
    while exists(name):
        n += 1
        name = "%s (%d)%s" % (stem, n, "." + suffix if suffix else "")
    return name


def pull(p: config.Paths, c: obs.Client, code: str, *, again: bool = False) -> dict:
    """The new files of in/ into the vault inbox, then the intake for the project's customer (for a project without
    one under the project code, with no customer code). A file counts as new until it was downloaded once (object
    name and etag, kept on the vault side)."""
    from awb import intake, projects

    result = fetch(p, c, code, again=again)
    if result["files"]:
        customer = _project(p, code).customer
        result["intake"] = intake.run(result["files"], code if customer == projects.NO_CUSTOMER else customer, p)
    return result


@_serialized
def fetch(p: config.Paths, c: obs.Client, code: str, *, again: bool = False) -> dict:
    """The new files of in/ into the vault inbox, without the intake (`awb import` runs it): the counts and the
    downloaded files under "files"."""
    owner_terminal("awb bucket pull")
    _project(p, code)
    from awb import askkind

    if askkind.refuses_material(p, code):
        raise BucketError(askkind.MATERIAL_LINE % code)
    folder = find_folder(c, code)
    if folder is None:
        raise BucketError("%s has no folder in the bucket; awb bucket folder %s creates it" % (code, code))
    objects = [o for o in c.list(folder).objects if not o[0].endswith("/") and not is_keep(o[0])]
    incoming = [o for o in objects if o[0].startswith(folder + IN)]
    outside = [o for o in objects if not o[0].startswith((folder + IN, folder + OUT))]
    done = set() if again else pulled(p)
    fresh = [o for o in incoming if (o[0], o[2]) not in done and _key(o[0]) and _plain(o[2])]
    result = {"new": len(fresh), "known": len(incoming) - len(fresh), "outside": len(outside), "intake": None,
              "files": [], "keys": []}
    if not fresh:
        return result
    files = []
    with _directory(p.inbox, create=True) as directory:
        for key, size, etag in fresh:
            # A failed get never leaves an original or a partly written file in the inbox.
            with tempfile.TemporaryDirectory(prefix="awb-bucket-") as tmp:
                download = Path(tmp) / "download"
                c.get(key, download, if_match=etag or None)
                with _directory(download.parent) as source_dir, _regular(source_dir, download.name) as source:
                    digest = hashlib.md5()
                    count = 0
                    with _stage(directory) as (stage, target):
                        while chunk := source.read(1 << 16):
                            digest.update(chunk)
                            count += len(chunk)
                            target.write(chunk)
                        if count != size or (re.fullmatch(r"[a-fA-F0-9]{32}", etag)
                                             and digest.hexdigest() != etag.lower()):
                            raise BucketError("the file changed since it was listed")
                        target.flush()
                        os.fsync(target.fileno())
                        while True:
                            name = _local_name(key[len(folder) + len(IN):], directory)
                            try:
                                os.link(stage, name, src_dir_fd=directory, dst_dir_fd=directory,
                                        follow_symlinks=False)
                                break
                            except FileExistsError:
                                continue
                        try:
                            _remember(p, key, etag)
                        except BaseException:
                            os.unlink(name, dir_fd=directory)
                            raise
                files.append(p.inbox / name)
                result["keys"].append(key)
    result["files"] = files
    return result


# --------------------------------------------------------------------------- put and move


@_serialized
def put(p: config.Paths, c: obs.Client, code: str, file: Path, *, replace: bool = False,
        reveal: bool = False, report: list | None = None, outcome: dict | None = None) -> tuple[str, dict | None]:
    """`file` into out/ of the project's folder (created when missing). With `reveal` the real names are put back
    first, in a private temporary file on the vault side that is removed after the upload. A put never refuses a
    file for its review state, a name, structured data or a secret (task GT, B-089: no content check): the
    uploaded copy is the file and `report` receives the one line (`review: none` or `review: passed DATE`).

    Versions (task BK4, D-B1): when out/ holds a file of the same group (project, normalised stem, suffix: the rule
    of awb/inventory.py) with the size and md5 of what would go up, nothing is put and the key returned is that
    file's. Otherwise every other file of the group of the file or of its HTML page moves to
    `<YYYY-MM>/<code>/_archive/<stamp>/<name>` first (copy, compare, delete: `archive_out`), then the file is put.
    The same name stays the matter of `replace`. `outcome` receives {"same": key or None, "archived": [(key,
    target)], "failed": [(key, reason)]}."""
    project = _project(p, code)
    if reveal:
        owner_terminal("awb bucket put --reveal")
    root = Path(os.path.abspath(project.path))
    if root == p.projects_root or not root.is_relative_to(p.projects_root):
        raise BucketError("the registered project is outside the projects root")
    with _directory(root):
        pass
    file = Path(os.path.abspath(file))
    with _directory(file.parent) as directory, _regular(directory, file.name):
        pass
    from awb import putcopy
    folder, _ = ensure_folder(c, project)
    name = file.name
    if not _key(name) or "/" in name:
        raise BucketError("the upload needs a plain file name")
    key = folder + OUT + name
    page = html_key(key)
    if not replace and any(k and c.head(k) is not None for k in (key, page)):
        raise BucketError("out/ of %s already holds a file of that name; give --replace" % code)
    ctype = mimetypes.guess_type(file.name)[0] or "application/octet-stream"
    done = {} if outcome is None else outcome
    done.update(same=None, archived=[], failed=[])
    stats = None
    # what leaves is a private copy, never the path again
    with _directory(p.vault / "tmp", create=True):
        pass
    tmp_root = p.vault / "tmp"
    with tempfile.TemporaryDirectory(dir=tmp_root) as tmp:
        (Path(tmp) / "source").mkdir(mode=0o700)
        snapshot = Path(tmp) / "source" / name
        with _directory(file.parent) as directory, _regular(directory, file.name) as source:
            before = os.fstat(source.fileno())
            with open(snapshot, "xb") as target:
                os.chmod(snapshot, 0o600)
                shutil.copyfileobj(source, target)
            after = os.fstat(source.fileno())
            if (before.st_size, before.st_mtime_ns, before.st_ctime_ns) != (
                    after.st_size, after.st_mtime_ns, after.st_ctime_ns):
                raise BucketError("the upload changed while being read")
        line = putcopy.review_line(root, snapshot)
        if not isinstance(line, str) or not re.fullmatch(
                r"review: (?:none|passed(?: [0-9]{4}-[0-9]{2}-[0-9]{2})?)", line):
            line = "review: none"
        if file.suffix.lower() in TEXT_SUFFIXES + PAGE_SUFFIXES:
            try:
                text = snapshot.read_text(encoding="utf-8")
            except UnicodeDecodeError:
                raise BucketError("a text deliverable must be UTF-8 text") from None
            if "\x00" in text:
                raise BucketError("a text deliverable cannot contain NUL bytes")
        copy = Path(tmp) / name
        secrets = putcopy.scrub(snapshot, copy)
        os.chmod(copy, 0o600)
        upload = copy
        if reveal:
            from awb import reveal as _reveal
            try:
                text = copy.read_text(encoding="utf-8")
            except UnicodeDecodeError:
                raise BucketError("--reveal takes a text file") from None
            named = Path(tmp) / ("named-" + name)
            stats = _reveal.write_named(text, named, p)
            upload = Path(tmp) / ("scrubbed-" + name)
            secrets += putcopy.scrub(named, upload)
            os.chmod(upload, 0o600)
        page_bytes = None
        if page:
            try:
                text = upload.read_text(encoding="utf-8")
            except UnicodeDecodeError:
                raise BucketError("a Markdown deliverable must be UTF-8 text") from None
            page_bytes = md_to_html(text, md_title(text, "Document")).encode("utf-8")
        same, older = out_versions(c, folder, code, key, page, upload)
        if same is not None:
            done["same"] = same
            return same, stats
        if report is not None:
            report.append(putcopy.put_line(line, secrets))
        from awb import inventory
        stamp = inventory.stamp()
        for old in older:
            target = archive_target(folder, stamp, old)
            result, reason = archive_out(c, old, target)
            if result == "ok":
                done["archived"].append((old, target))
            else:
                done["failed"].append((old, reason))
        c.put_file(key, upload, ctype)
        if page_bytes is not None:
            c.put_bytes(page, page_bytes, "text/html")
    return key, stats


def archive_target(folder: str, stamp: str, key: str) -> str:
    """`<YYYY-MM>/<code>/_archive/<stamp>/<name>`: the place `awb files plan` gives an older version in out/."""
    from awb import filesplan

    return "%s%s/%s/%s" % (folder, filesplan.ARCHIVE, stamp, key.rsplit("/", 1)[-1])


def out_versions(c, folder: str, code: str, key: str, page: str | None, upload: Path) -> tuple[str | None, list[str]]:
    """(the file of out/ of the group of `key` with the size and md5 of `upload`, or None; the other files of out/ of
    the group of `key` or of `page`, which the put of `key` makes older versions). The file of the same name and its
    page are neither: --replace overwrites them. A multipart etag gives no md5, so never the same."""
    from awb import filesinv, inventory

    def group(k: str) -> str:
        return inventory.group_of(code, k, filesinv.suffix_of(k))

    size, md5 = upload.stat().st_size, inventory.hashes(upload)[1]
    mine = group(key)
    groups = {mine} | ({group(page)} if page else set())
    same, older = None, []
    for k, n, etag in c.list(folder + OUT).objects:
        if not _key(k) or not k.startswith(folder + OUT):
            raise BucketError("the bucket listed a file outside out/")
        name = k[len(folder + OUT):]
        if not name or "/" in name or is_keep(k):
            continue
        g = group(k)
        if g == mine and int(n) == size and filesinv.md5_of_etag(etag) == md5:
            same = same or k
        elif g in groups and k not in (key, page):
            older.append(k)
    return same, older


def archive_out(c, key: str, target: str) -> tuple[str, str]:
    """Move `key` to `target` inside the owner bucket: the key service's `bucket_archive` (it holds the key), or with a
    client that copies (obs.Client) the same steps here (filestidy.move_object). ("ok", how) or (result, reason);
    nothing is deleted without its compared copy."""
    from awb import filestidy

    try:
        if getattr(c, "can_copy", True):
            return filestidy.move_object(c, key, target)
        c.archive(key, target)
    except (BucketError, obs.OBSError, OSError) as err:
        return filestidy.FAILED, "md5 comparison failed" if "md5" in str(err) else "the archive failed"
    return filestidy.OK, "moved"


PAGE_SUFFIXES = (".md", ".markdown")
"""A deliverable in Markdown goes out twice (task DN, 2026-10-27): the md to edit and <name>.html beside it, rendered
by md_to_html below, to open in a browser and paste into a mail."""


def html_key(key: str) -> str | None:
    """The key of the HTML page that goes beside a Markdown deliverable, None for any other file."""
    stem, dot, ext = key.rpartition(".")
    return stem + ".html" if dot and "." + ext.lower() in PAGE_SUFFIXES and "/" not in ext else None


def _put_page(c, page: str | None, source: Path) -> None:
    if page is None:
        return
    try:
        text = source.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        raise BucketError("a Markdown deliverable must be UTF-8 text") from None
    c.put_bytes(page, md_to_html(text, md_title(text, Path(source).stem.removeprefix("named-"))).encode("utf-8"),
                "text/html")


def get_it_line(key: str, code: str) -> str:
    """The one line that says where a put file is and how to get it (his rule of 2026-10-27: no link to a file)."""
    return ("get it: Total Commander, bucket %s, folder %s, or on the laptop: awb clean fetch %s"
            % (OWNER_BUCKET, key.rsplit("/", 1)[0] + "/", code))


@_serialized
def move(p: config.Paths, c: obs.Client, old: str, code: str) -> dict:
    """Move a folder whose name drifted to `<its month>/<code>/`: copy every object inside the object storage,
    compare names and sizes, then delete the old objects. Nothing is deleted when the comparison fails."""
    owner_terminal("awb bucket move")
    _project(p, code)
    if not getattr(c, "can_copy", True):
        raise BucketError("the key service has no copy and no delete: move the folder in the object storage console")
    if not (_key(old) and _MONTH_RE.match(old) and old.endswith("/") and old.count("/") == 2):
        raise BucketError("give the folder as <YYYY-MM>/<name>/")
    target = folder_of(old[:7], code)
    if target == old:
        raise BucketError("the folder already carries the name of %s" % code)
    if find_folder(c, code) is not None:
        raise BucketError("%s already has a folder: bring the files together by hand" % code)
    objects = c.list(old).objects
    if not objects:
        raise BucketError("that folder is empty or not there")
    if any(not _key(k) or not k.startswith(old) for k, _, _ in objects):
        raise BucketError("the bucket listed a file outside the source folder")
    for key, _, _ in objects:
        c.copy(key, target + key[len(old):])
    want = {k[len(old):]: (s, e) for k, s, e in objects}
    got = {k[len(target):]: (s, e) for k, s, e in c.list(target).objects if k.startswith(target)}
    current = {k[len(old):]: (s, e) for k, s, e in c.list(old).objects}
    if got != want or current != want:
        raise BucketError("the copy does not match the folder (names or sizes differ); nothing was deleted")
    for key, _, _ in objects:
        c.delete(key)
    return {"moved": len(objects), "target": target}


# --------------------------------------------------------------------------- Markdown to a mail page
# A deliverable in Markdown goes out as md and as one HTML page that pastes into a mail (task DN,
# 2026-10-27): headings, paragraphs, bold, italic, inline code, links as text with the address in brackets,
# bullet and numbered lists (one level of nesting), tables with borders and fenced code blocks; one
# inline style block, no asset, no script, no image.

MD_CSS = ("body{font-family:Calibri,Arial,sans-serif;font-size:11pt;line-height:1.4;color:#000;max-width:60rem;"
          "margin:1rem}"
          "h1{font-size:16pt}h2{font-size:14pt}h3{font-size:12pt}h4{font-size:11pt}"
          "table{border-collapse:collapse;margin:.5rem 0}"
          "th,td{border:1px solid #808080;padding:3px 6px;text-align:left;vertical-align:top}"
          "th{background:#e7e6e6;font-weight:bold}"
          "code{font-family:Consolas,monospace;background:#f2f2f2}"
          "pre{font-family:Consolas,monospace;background:#f2f2f2;border:1px solid #d0d0d0;padding:6px;"
          "white-space:pre-wrap}")

_MD_LINK_RE = re.compile(r"\[([^\]]+)\]\(([^)\s]+)\)")
_MD_HEAD_RE = re.compile(r"^(#{1,6})\s+(.*?)\s*#*\s*$")
_MD_OL_RE = re.compile(r"^(\s*)(\d{1,4})[.)]\s+(.*)$")
_MD_UL_RE = re.compile(r"^(\s*)[-*+]\s+(.*)$")
_MD_FENCE_RE = re.compile(r"^\s*(```|~~~)")
_MD_RULE_RE = re.compile(r"^\s*([-*_])(\s*\1){2,}\s*$")
_MD_SEP_RE = re.compile(r"^\s*\|?\s*:?-{2,}:?\s*(\|\s*:?-{2,}:?\s*)*\|?\s*$")


def md_inline(text: str) -> str:
    """One line of markdown as escaped HTML: code first (its content stays literal), then links as text, bold and
    italic."""
    out = []
    for i, part in enumerate(re.split(r"`([^`]+)`", text)):
        if i % 2:
            out.append("<code>%s</code>" % html.escape(part, quote=False))
            continue
        part = _MD_LINK_RE.sub(lambda m: m.group(1) if m.group(1) == m.group(2) else "%s (%s)" % m.groups(), part)
        part = html.escape(part, quote=False)
        part = re.sub(r"\*\*([^*]+)\*\*|__([^_]+)__", lambda m: "<strong>%s</strong>" % (m.group(1) or m.group(2)),
                      part)
        part = re.sub(r"(?<![*\w])\*([^*\s][^*]*)\*(?!\w)", r"<em>\1</em>", part)
        part = re.sub(r"(?<![_\w])_([^_\s][^_]*)_(?!\w)", r"<em>\1</em>", part)
        out.append(part)
    return "".join(out)


def _md_cells(line: str) -> list[str]:
    s = line.strip()
    if s.startswith("|"):
        s = s[1:]
    if s.endswith("|") and not s.endswith("\\|"):
        s = s[:-1]
    return [c.strip().replace("\\|", "|") for c in re.split(r"(?<!\\)\|", s)]


def _md_table(rows: list[str]) -> str:
    head, rows_ = _md_cells(rows[0]), [_md_cells(r) for r in rows[2:]]
    width = len(head)
    out = ["<table border=\"1\" cellspacing=\"0\" cellpadding=\"4\">", "<thead><tr>%s</tr></thead>"
           % "".join("<th>%s</th>" % md_inline(c) for c in head), "<tbody>"]
    for cells in rows_:
        cells = (cells + [""] * width)[:width]
        out.append("<tr>%s</tr>" % "".join("<td>%s</td>" % md_inline(c) for c in cells))
    out.append("</tbody></table>")
    return "\n".join(out)


def md_body(markdown: str) -> str:
    """The HTML of the blocks of `markdown`, without the page around it."""
    lines = markdown.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    out: list[str] = []
    para: list[str] = []
    stack: list[tuple[str, int]] = []          # open lists: (tag, indent)

    def flush() -> None:
        if para:
            out.append("<p>%s</p>" % md_inline(" ".join(para)))
            para.clear()

    def close(to: int = 0) -> None:
        while len(stack) > to:
            out.append("</li></%s>" % stack.pop()[0])

    i = 0
    while i < len(lines):
        line = lines[i].rstrip()
        fence = _MD_FENCE_RE.match(line)
        if fence:
            flush()
            close()
            code = []
            i += 1
            while i < len(lines) and not lines[i].strip().startswith(fence.group(1)):
                code.append(lines[i])
                i += 1
            out.append("<pre><code>%s</code></pre>" % html.escape("\n".join(code), quote=False))
            i += 1
            continue
        if "|" in line and i + 1 < len(lines) and _MD_SEP_RE.match(lines[i + 1]) and "|" in lines[i + 1]:
            flush()
            close()
            rows = [line, lines[i + 1]]
            i += 2
            while i < len(lines) and "|" in lines[i] and lines[i].strip():
                rows.append(lines[i].rstrip())
                i += 1
            out.append(_md_table(rows))
            continue
        h = _MD_HEAD_RE.match(line)
        ol = _MD_OL_RE.match(line)
        ul = None if ol else _MD_UL_RE.match(line)
        if h:
            flush()
            close()
            n = len(h.group(1))
            out.append("<h%d>%s</h%d>" % (n, md_inline(h.group(2)), n))
        elif _MD_RULE_RE.match(line):
            flush()
            close()
            out.append("<hr>")
        elif ol or ul:
            flush()
            tag = "ol" if ol else "ul"
            indent = len((ol or ul).group(1).expandtabs(4))
            item = ol.group(3) if ol else ul.group(2)
            while stack and indent < stack[-1][1]:
                close(len(stack) - 1)
            if stack and indent > stack[-1][1] and len(stack) < 2:
                out.append("<%s>" % tag)
                stack.append((tag, indent))
            elif stack and stack[-1][0] != tag:
                close(len(stack) - 1)
                out.append("<%s>" % tag)
                stack.append((tag, indent))
            elif stack:
                out.append("</li>")
            else:
                out.append("<%s>" % tag)
                stack.append((tag, indent))
            out.append("<li>%s" % md_inline(item))
        elif not line.strip():
            flush()
            if stack and not (i + 1 < len(lines) and (_MD_OL_RE.match(lines[i + 1]) or _MD_UL_RE.match(lines[i + 1]))):
                close()
        elif stack and lines[i][:1] in " \t":
            out.append("<br>%s" % md_inline(line.strip()))
        else:
            close()
            para.append(line.strip())
        i += 1
    flush()
    close()
    return "\n".join(x for x in out if x)


def md_title(markdown: str, default: str = "Document") -> str:
    for line in markdown.splitlines():
        h = _MD_HEAD_RE.match(line.rstrip())
        if h:
            return re.sub(r"[*_`]", "", _MD_LINK_RE.sub(r"\1", h.group(2))).strip() or default
    return default


def md_to_html(markdown: str, title: str | None = None) -> str:
    """One self-contained HTML page of `markdown`."""
    name = title or md_title(markdown)
    return ('<!doctype html>\n<html><head><meta charset="utf-8"><title>%s</title>\n<style>%s</style></head>\n'
            '<body>\n%s\n</body></html>\n') % (html.escape(name), MD_CSS, md_body(markdown))


# --------------------------------------------------------------------------- command line


def main(argv: list[str] | None = None) -> int:
    """`awb bucket sync|folder|pull|put|move`. Exit 0 done, 1 the final check withheld a file, 2 refused or an
    error."""
    from awb import intake
    from awb.cli import SafeParser

    class BucketParser(SafeParser):
        def error(self, message):
            self.print_usage(sys.stderr)
            print("awb bucket: invalid command arguments", file=sys.stderr)
            raise SystemExit(2)

    ap = BucketParser(prog="awb bucket", description="The owner's bucket: one folder per project, in/ and out/.")
    sub = ap.add_subparsers(dest="command", parser_class=BucketParser)
    sub.add_parser("sync", help="the folders of every active project; folders that match no project")
    s = sub.add_parser("folder", help="the folder of one project, created when missing")
    s.add_argument("code")
    s = sub.add_parser("pull", help="new files of in/ into the vault inbox, then the intake (your own terminal, "
                                    "never an assistant session)")
    s.add_argument("code")
    s.add_argument("--again", action="store_true", help="take files again that were downloaded before")
    s = sub.add_parser("put", help="a file into out/; an older version of its group goes to _archive/ first")
    s.add_argument("code")
    s.add_argument("file", type=Path)
    s.add_argument("--replace", action="store_true", help="overwrite a file of the same name in out/")
    s.add_argument("--reveal", action="store_true", help="put the real names back first (your own shell)")
    s = sub.add_parser("move", help="rename a drifted folder to <month>/<code>/ (your own terminal, never an "
                                    "assistant session)")
    s.add_argument("folder")
    s.add_argument("code")
    try:
        args = ap.parse_args(argv)
    except SystemExit as exc:
        return exc.code if isinstance(exc.code, int) else 2
    if not args.command:
        ap.print_usage(sys.stderr)
        return 2
    from awb import projects, register
    from awb import reveal as _reveal
    try:
        p = config.paths()
        if args.command in ORIGINALS:
            owner_terminal("awb bucket %s" % args.command)      # before the key service is asked anything
        c = client()
        if args.command == "sync":
            for line in sync(p, c):
                print(line)
            return 0
        if args.command == "folder":
            folder, created = ensure_folder(c, _project(p, args.code))
            print("%s  %s%s" % (args.code, folder, "  created" if created else ""))
            return 0
        if args.command == "pull":
            r = pull(p, c, args.code, again=args.again)
            line = "%s: %d new files from in/, %d taken before" % (args.code, r["new"], r["known"])
            if r["outside"]:
                line += ", %d outside in/ and out/ left alone" % r["outside"]
            print(line)
            res = r["intake"]
            if res is None:
                return 0
            print("intake for %s: %d outputs in the outbox, wiped %s" % (res.customer, len(res.outputs),
                                                                       intake.wiped_text(res.wiped)))
            if res.held:
                print("withheld by the final check: %d file(s), the originals stay in the inbox" % len(res.held))
                return 1
            return 0
        if args.command == "put":
            said: list[str] = []
            done: dict = {}
            key, stats = put(p, c, args.code, args.file, replace=args.replace, reveal=args.reveal, report=said,
                             outcome=done)
            if done["same"] is not None:
                print("%s: %s is the same file (size and md5): not put again" % (args.code, _public(key, p)))
                print(get_it_line(key, args.code))
                return 0
            for old, target in done["archived"]:
                print("%s: %s archived to %s (an older version)" % (args.code, _public(old, p), _public(target, p)))
            for old, reason in done["failed"]:
                print("%s: %s not archived, it stays in out/: %s"
                      % (args.code, _public(old, p), _public(reason, p)), file=sys.stderr)
            line = "%s: %s" % (args.code, _public(key, p))
            if stats is not None:
                line += "  (%d codes replaced by names)" % stats["replaced"]
            print(line)
            if html_key(key):
                print("%s: %s  (the same text as HTML: open it, select all, paste into the mail)"
                      % (args.code, _public(html_key(key), p)))
            for one in said:
                print(one, file=sys.stderr)          # beside the lines of the objects, never between them
            print(get_it_line(key, args.code))
            return 0
        if args.command == "move":
            r = move(p, c, args.folder, args.code)
            print("%s: %d objects moved to %s" % (args.code, r["moved"], r["target"]))
            return 0
    except BucketError as err:
        message = str(err)
        print("awb bucket: %s" % (message if message == LOCKED_LINE else _public(message, p)), file=sys.stderr)
        return 2
    except (obs.OBSError, projects.ProjectError, intake.IntakeError, register.RegisterError,
            _reveal.RevealError, OSError, ValueError) as err:
        print("awb bucket: the operation failed (%s)" % type(err).__name__, file=sys.stderr)
        return 2
    return 2


if __name__ == "__main__":
    sys.exit(main())
