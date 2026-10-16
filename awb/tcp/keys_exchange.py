"""The service side of the exchange (awb/tcp/xchg.py): what runs inside the key service.

A part of the exchange (F1 step 7). The key service calls these with its settings, its tenants and its OBS client
(`Context`): `serve_obs` for the session's calls on the two buckets, `serve_inbox_find` and `serve_take_lab` and
`serve_take_owner` for a take, `serve_owner_has` and `serve_owner_folder` for the owner's folder `in/`,
`serve_web_put_in` and `serve_web_read` for the owner's console, `authorise_put` before the bytes of a put. The
docstring of awb/tcp/xchg.py says what each refuses. The session side and the owner's settings stay in xchg.py;
every name of this module is still reached through it (awb/parts.py).
"""
from __future__ import annotations

import datetime
import hashlib
import hmac
import os
import re
import shutil
import tempfile
from pathlib import Path

from awb import bucket_layout, codes, config, parts
from awb.tcp.xchg import (DIFFERS_LINE, FIND_MAX, FROM, IN, INBOX, STALE_LINE, XchgError, _CODE_RE, _CUST_RE, _ID_RE,
                          _ID_SECRET, check_problems, match, suffix, valid_name)


class Context:
    """What the key service hands the exchange: its settings, the tenants, a way to make an OBS client, the paths,
    a check of a project code and a notice to the owner."""

    def __init__(self, settings: dict, keys_of, obs_client, paths_fn, active_project, notify):
        self.settings = settings
        self.keys_of = keys_of              # alias -> obs.Keys of the lab role, or None
        self.obs_client = obs_client        # (bucket, keys) -> obs.Client
        self.paths_fn = paths_fn
        self.active_project = active_project  # code -> Project or None
        self.notify = notify                # (subject, message) -> None

    def client(self, which: str):
        tenant = self.settings.get("bucket_tenant")
        keys = self.keys_of(tenant) if tenant else None
        if keys is None:
            raise XchgError("the exchange is not set up: bucket_tenant in keys.conf names no tenant with a lab key")
        bucket = self.settings["lab_bucket" if which == "lab" else "owner_bucket"]
        return self.obs_client(bucket, keys)

    def max_bytes(self) -> int:
        try:
            return max(1, int(self.settings.get("max_mb") or bucket_layout.MAX_MB)) * 1024 * 1024
        except ValueError:
            return bucket_layout.MAX_MB * 1024 * 1024


def allowed_key(key: str, method: str, project: str | None) -> bool:
    """The object keys a session may touch: inbox/<name> to look at (head), <code>/in/ and <code>/from-session/ of
    its own active project. A put only into from-session/ (in/ is written by serve_take_lab with a copy of the
    service). A get or a delete of the lab inbox goes through serve_take_lab alone, so the name check of the service
    cannot be skipped (F6)."""
    if not isinstance(key, str) or not key or key.startswith("/") or ".." in key.split("/") or "//" in key:
        return False
    if key.startswith(INBOX):
        return method == "HEAD" and all(valid_name(part) for part in key[len(INBOX):].split("/"))
    if project and _CODE_RE.match(project):
        for sub in (IN, FROM):
            base = "%s/%s" % (project, sub)
            if key.startswith(base) and len(key) > len(base):
                return method in ("GET", "HEAD", "DELETE") or (method == "PUT" and sub == FROM)
    return False


def allowed_prefix(prefix: str, project: str | None) -> bool:
    if prefix == INBOX:
        return True
    return bool(project and _CODE_RE.match(project)) and prefix in ("%s/%s" % (project, IN), "%s/%s" % (project, FROM))


def serve_obs(ctx: Context, req: dict, body_path: Path | None) -> tuple[dict, Path | None]:
    """One object call of a session on the lab bucket. Returns the answer and, for a GET, a file to stream."""
    from awb import obs

    method = str(req.get("method") or "").upper()
    project = req.get("project")
    if project is not None and ctx.active_project(project) is None:
        raise XchgError("the project is not an active project")
    c = ctx.client("lab")
    if method == "LIST":
        prefix = req.get("prefix") or ""
        if not allowed_prefix(prefix, project):
            raise XchgError("a listing is allowed for inbox/ and the project's own folders only")
        objs = [(k, size) for k, size, _ in c.list(prefix).objects if not k.endswith("/")]
        names = [k[len(prefix):] for k, _ in objs]
        hidden = _name_hits(names, ctx.paths_fn()) if prefix == INBOX else set()
        rows = [{"name": n, "size": s} for i, (n, (_, s)) in enumerate(zip(names, objs)) if i not in hidden]
        return {"ok": True, "objects": rows, "withheld": len(hidden)}, None
    if method == "COPY":
        raise XchgError("a file of the lab inbox is taken with op take_lab, which checks it first")
    key = req.get("key")
    if method not in ("GET", "HEAD", "PUT", "DELETE") or not allowed_key(key, method, project):
        raise XchgError("the object call is outside inbox/ and the project's own folders")
    if method == "HEAD":
        h = c.head(key)
        return {"ok": True, "exists": h is not None, "size": int((h or {}).get("Content-Length", 0) or 0)}, None
    if method == "DELETE":
        c.delete(key)
        return {"ok": True}, None
    if method == "PUT":
        if body_path is None:
            raise XchgError("a put needs its bytes")
        # every put of a session: the key of put_key_ok, the name check and the send gate (finding 3)
        authorise_put(ctx, req)
        problems = put_problems(ctx, req, body_path)
        if problems:
            raise XchgError("; ".join(problems))
        c.put_file(key, body_path, "application/octet-stream")
        size = body_path.stat().st_size
        ident = key.rsplit("/", 1)[-1]
        # the mail names the file by its id alone: no text the session chose (finding 18 of the review of 2026-10-10)
        ctx.notify(*exchange_mail("put", project, ident=ident, size=size))
        return {"ok": True, "size": size}, None
    h = c.head(key)
    if h is None:
        return {"ok": True, "exists": False}, None
    size = int(h.get("Content-Length", 0) or 0)
    if size > ctx.max_bytes():
        raise XchgError("the object is larger than the limit of keys.conf (max_mb)")
    tmp = Path(tempfile.mkstemp(prefix="awb-xchg-", dir=_tmp_dir(ctx))[1])
    c.get(key, tmp)
    return {"ok": True, "exists": True, "size": tmp.stat().st_size}, tmp


_PUT_ID_RE = re.compile(r"^put-[0-9a-f]{12}(?:\.[a-z0-9]{1,5})?$")
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
HOLD_REASONS = {"intake": "the intake stopped", "withheld": "the final check withheld the copy",
                "unreadable": "the file cannot be read as text"}


def put_id(digest: str, name: str) -> str:
    """The object id of a put: put- and twelve hex digits of its bytes, and the extension when it is a plain one."""
    ext = os.path.splitext(name)[1].lower()
    return "put-%s%s" % (digest[:12], ext if re.fullmatch(r"\.[a-z0-9]{1,5}", ext) else "")


def put_key_ok(key: str, project: str) -> bool:
    """A put of a session lands at <code>/from-session/<date>/<file id> and nowhere else under from-session/."""
    parts = key.split("/")
    return (len(parts) == 4 and parts[0] == project and parts[1] + "/" == FROM and bool(_DATE_RE.match(parts[2]))
            and bool(_PUT_ID_RE.match(parts[3])))


def authorise_put(ctx: Context, req: dict) -> None:
    """Whether a put may be received at all (F8): an active project and a key of the form of put_key_ok under its
    from-session/, nowhere else. Run by the key service before it reads a byte of the upload."""
    project, key = req.get("project"), req.get("key")
    if not (isinstance(project, str) and ctx.active_project(project) is not None):
        raise XchgError("the project is not an active project")
    if not (allowed_key(key, "PUT", project) and put_key_ok(key, project)):
        raise XchgError("a put goes to <code>/from-session/<date>/<file id>")


def put_problems(ctx: Context, req: dict, body_path: Path) -> list[str]:
    """The checks of a put into from-session/, run by the key service on the bytes it received (F6): the name
    check of the content (a picture only with image and a reason), then the send gate on the project's folder
    under the projects root (never the path a row of projects.tsv names), in its strict form unless the owner's
    grant records no customer (send_gate_strict). A name the name check does not pass never travels (the mail names
    the id, TM0 item 1), so the bytes are judged under the id then."""
    from awb import review

    p = ctx.paths_fn()
    project = ctx.active_project(req["project"])
    ident = req["key"].rsplit("/", 1)[-1]
    name = req.get("name")
    if not (valid_name(name) and not _name_hits([name], p)):
        name = ident
    image = bool(req.get("image")) and isinstance(req.get("reason"), str) and len(req["reason"].strip()) >= 10
    with tempfile.TemporaryDirectory(prefix="awb-check-", dir=_tmp_dir(ctx)) as tmp:
        # the readers of the name check go by the extension: the received bytes are judged under their own name
        judged = Path(tmp) / ("upload" + os.path.splitext(name)[1].lower()[:6])
        os.link(body_path, judged)
        problems = check_problems(judged, image=image, register=p.register)
        if problems:
            return problems
        for_customer = send_gate_strict(ctx, project)
        verdict, message = review.send_check(p.projects_root / project.code, judged, for_customer,
                                             register_path=p.register, workbench=p, name=name)
    if verdict == review.SEND_REFUSE:
        return ["the send gate refused the file: %s" % message]
    return []


def exchange_mail(state: str, project: str, ident: str | None = None, size: int | None = None,
                  customer: str | None = None, why: str | None = None) -> tuple[str, str]:
    """The subject and the message of an exchange mail, built from fixed fields only: the project code, the object
    id, the size, the state (and for a hold the customer code and a reason of HOLD_REASONS). A field that does not
    read as its kind in full is sent as -; no key, no prefix, no error text and no file name (finding 18 of the
    review of 2026-10-10: the id stands for the file). The mail is printable ASCII: anything else becomes a space."""
    code = project if isinstance(project, str) and _CODE_RE.fullmatch(project) else "-"
    if state == "put":
        ident = ident if isinstance(ident, str) and _PUT_ID_RE.fullmatch(ident) else "-"
        subject, message = ("Workbench: %s put a file" % code,
                            "Project %s put the file %s (%d bytes) into its from-session folder of the lab bucket. "
                            "Fetch it in the OBS console." % (code, ident, int(size or 0)))
    else:
        cust = customer if isinstance(customer, str) and _CUST_RE.fullmatch(customer) else "-"
        reason = HOLD_REASONS.get(why or "", "held")
        subject, message = ("Workbench: a take for %s is held" % code,
                            "A take for %s (%s) is held: %s. The original is in the project's in/ folder of the owner "
                            "bucket and in the vault inbox. %s." % (code, cust, reason, hold_line(code)))
    return _MAIL_UNSAFE_RE.sub(" ", subject), _MAIL_UNSAFE_RE.sub(" ", message)


_MAIL_UNSAFE_RE = re.compile(r"[^\x20-\x7e]")
"""What never reaches a mail field: line breaks and separators, bidi and other controls, any letter outside ASCII."""


def _tmp_dir(ctx: Context) -> str:
    d = ctx.paths_fn().vault / "tmp"
    d.mkdir(parents=True, exist_ok=True)
    os.chmod(d, 0o700)
    return str(d)


def _name_hits(names: list[str], p) -> set[int]:
    """Indexes of the names the name check finds anything in (names of the lab inbox before a session sees them)."""
    from awb import check

    out = set()
    for i, n in enumerate(names):
        try:
            if check.check_text(n, p.register):
                out.add(i)
        except Exception:
            out.add(i)
    return out


def _inbox_files(ctx: Context, which: str) -> list[dict]:
    """The files under inbox/ of one bucket, subfolders included."""
    listing = ctx.client(which).list(INBOX)
    out = []
    for key, size, etag in listing.objects:
        rel = key[len(INBOX):]
        if key.endswith("/") or not rel or not all(valid_name(part) for part in rel.split("/")):
            continue
        out.append({"key": key, "rel": rel, "size": size, "etag": etag, "modified": listing.modified.get(key, "")})
    return out


def _file_id(which: str, key: str, etag: str) -> str:
    digest = hmac.new(_ID_SECRET, ("%s\0%s\0%s" % (which, key, etag)).encode(), hashlib.sha256).hexdigest()
    return "%s-%s" % ({"lab": "lab", "project": "prj"}.get(which, "own"), digest[:12])


def _resolve(ctx: Context, which: str, ident: str) -> dict | None:
    for f in _inbox_files(ctx, which):
        if _file_id(which, f["key"], f["etag"]) == ident:
            return f
    return None


def project_customer(ctx: Context, project) -> str:
    """The customer of a project for the service (the residual of F5 and F6): the one the owner's grant recorded, a
    CUST code or none; the work user's row never chooses it. A row that names another customer than the grant, or a
    customer under a grant written before customers were recorded, is refused in one line with the project code. A
    project the owner never granted has no record of his and so no customer (finding 5 of the review of
    2026-10-10): an owner-inbox take holds, a take from in/ runs under the project code."""
    from awb import grants, projects

    row = None if project.customer in (projects.NO_CUSTOMER, "", None) else project.customer
    recorded = _grant_of(ctx, project.code)
    if recorded is None:
        return projects.NO_CUSTOMER
    granted = grants.customer(ctx.paths_fn(), project.code)
    if granted is None:
        if row is None:
            return projects.NO_CUSTOMER
        raise XchgError(STALE_LINE % (project.code, project.code, recorded["kind"]))
    if (None if granted == projects.NO_CUSTOMER else granted) != row:
        raise XchgError(DIFFERS_LINE % project.code)
    return granted


def _grant_of(ctx: Context, code: str) -> dict | None:
    from awb import grants

    try:
        return grants.load(ctx.paths_fn()).get(code)
    except Exception:
        return None


def send_gate_strict(ctx: Context, project) -> bool:
    """Whether the send gate of a put runs in its strict form: always, unless the owner's grant records that the
    project has no customer. A project he never granted gets the strict form whatever its row says (finding 5)."""
    from awb import projects

    if _grant_of(ctx, project.code) is None:
        return True
    return project_customer(ctx, project) != projects.NO_CUSTOMER


def _project_files(ctx: Context, code: str) -> list[dict]:
    """The new files of the project's own folder in/ of the owner bucket (not yet taken by a take, an import or a
    pull). A project whose folder is missing gets it here: the first take makes it when spawn could not."""
    from awb import bucket

    c = ctx.client("owner")
    folder = bucket.find_folder(c, code)
    if folder is None:
        project = ctx.active_project(code)
        if project is None:
            return []
        folder, _ = bucket.ensure_folder(c, project)
    base = folder + IN
    done = bucket.pulled(ctx.paths_fn())
    out = []
    listing = c.list(base)
    for key, size, etag in listing.objects:
        rel = key[len(base):]
        if (key.endswith("/") or not rel or (key, etag) in done or "\t" in key or "\n" in key
                or not all(valid_name(part) for part in rel.split("/"))):
            continue
        out.append({"key": key, "rel": rel, "size": size, "etag": etag, "modified": listing.modified.get(key, "")})
    return out


def serve_inbox_find(ctx: Context, req: dict) -> dict:
    """Which inbox files the owner means: `words` (what he said), `id` (a file an earlier find showed), `all` or
    nothing (the list). Every file of both inboxes comes back with an id, its bucket, kind, size and time; the name
    only for a file of the lab inbox the name check passes, never for one of the owner inbox or of in/. His words
    meet only the names shown; a hidden one meets his kind words and the newest alone (match)."""
    project = req.get("project")
    if project is not None and ctx.active_project(project) is None:
        raise XchgError("the project is not an active project")
    words, ident = req.get("words"), req.get("id")
    if ident is not None and not (isinstance(ident, str) and _ID_RE.match(ident)):
        raise XchgError("an id reads lab- or own- and twelve characters, as awb inbox list shows it")
    if words is not None and not (isinstance(words, str) and words.strip() and len(words) <= 500):
        raise XchgError("say which file: what it is, a part of its name or a few words")
    files, entries = [], []
    for which in ("lab", "owner") + (("project",) if project is not None else ()):
        found = _inbox_files(ctx, which) if which != "project" else _project_files(ctx, project)
        hidden = _name_hits([f["rel"] for f in found], ctx.paths_fn()) if which == "lab" else set(range(len(found)))
        for i, f in enumerate(found):
            files.append(f)
            entries.append({"id": _file_id(which, f["key"], f["etag"]), "bucket": which,
                            "name": None if i in hidden else f["rel"], "kind": suffix(f["rel"]).upper() or "FILE",
                            "size": f["size"], "modified": f["modified"]})
    if len(entries) > FIND_MAX:
        raise XchgError("the inboxes hold %d files, more than %d; the owner clears them first" % (len(entries), FIND_MAX))
    if ident is not None:
        picked = [i for i, e in enumerate(entries) if e["id"] == ident]
    elif req.get("all"):
        picked = list(range(len(entries)))
    elif words is not None and from_in(words):
        picked = [i for i, e in enumerate(entries) if e["bucket"] == "project"]
    elif words is not None:
        visible = {i for i, e in enumerate(entries) if e["name"] is not None}
        picked = match(words, [f["rel"] for f in files], [f["modified"] for f in files], visible)
    else:
        picked = []
    return {"ok": True, "files": entries, "matches": [entries[i] for i in picked]}


_IN_WORDS = {"in", "in/", "from", "take", "the", "all", "files", "file", "folder", "every", "of", "my", "please"}


def from_in(words: str) -> bool:
    """His words name the project's folder in/ as a whole: "take the files from in", "all of in/", "in"."""
    tokens = re.findall(r"[^\s|,.;:!?\"'`]+", (words or "").lower())
    return bool(tokens) and ("in" in tokens or "in/" in tokens) and set(tokens) <= _IN_WORDS


def serve_owner_folder(ctx: Context, req: dict) -> dict:
    """The folder <YYYY-MM>/<code>/ with in/ and out/ of an active project in the owner bucket, made when missing
    (op owner_folder, asked by spawn). The caller never holds the bucket key; the service makes empty folder markers
    of a registered active project and nothing else."""
    from awb import bucket

    code = req.get("project")
    project = ctx.active_project(code) if isinstance(code, str) and _CODE_RE.match(code) else None
    if project is None:
        return {"ok": False, "kind": "refused", "error": "the project is not an active project"}
    folder, created = bucket.ensure_folder(ctx.client("owner"), project)
    return {"ok": True, "folder": folder, "created": created, "project": code}


_EXT_RE = re.compile(r"^\.[a-z0-9]{1,8}$")


def serve_web_put_in(ctx: Context, req: dict, upload: Path | None) -> dict:
    """A file of the owner's console into the project's folder in/ under an id and its extension (op web_put_in,
    the owner's processes only; the key service checks the peer). The file name never reaches this service."""
    from awb import bucket

    code, ext = req.get("project"), req.get("extension")
    project = ctx.active_project(code) if isinstance(code, str) and _CODE_RE.match(code) else None
    if project is None or not (isinstance(ext, str) and _EXT_RE.match(ext)) or upload is None:
        return {"ok": False, "kind": "refused", "error": "refused"}
    size = upload.stat().st_size
    if not 0 < size <= ctx.max_bytes():
        return {"ok": False, "kind": "too_large", "error": "the file is empty or larger than the limit"}
    c = ctx.client("owner")
    folder, _ = bucket.ensure_folder(c, project)
    ident = "upload-%s-%s%s" % (datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%d-%H%M%S"),
                                os.urandom(3).hex(), ext)
    c.put_file(folder + IN + ident, upload)
    return {"ok": True, "id": ident, "size": size, "project": code}


def serve_take_lab(ctx: Context, req: dict) -> tuple[dict, Path | None]:
    """The lab path of a take (F6): the key service fetches the file, runs the name check on its name and its
    content in its own process and only then copies it into the project's in/, deletes it from the inbox and
    streams the bytes to the session. A file with a hit, or one that cannot be checked, stays in the inbox and no
    byte of it leaves the service."""
    from awb import gate

    code, ident = req.get("project"), req.get("id")
    if not (isinstance(ident, str) and ident.startswith("lab-") and _ID_RE.match(ident)):
        raise XchgError("an id of the lab inbox reads lab- and twelve characters")
    if not (isinstance(code, str) and ctx.active_project(code) is not None):
        raise XchgError("the project is not an active project")
    f = _resolve(ctx, "lab", ident)
    if f is None:
        return {"ok": True, "state": "absent"}, None
    p = ctx.paths_fn()
    if _name_hits([f["rel"]], p):
        return {"ok": True, "state": "held", "problems": ["its name holds a hit of the name check"]}, None
    if f["size"] > ctx.max_bytes():
        raise XchgError("the object is larger than the limit of keys.conf (max_mb)")
    name = f["rel"].rsplit("/", 1)[-1]
    c = ctx.client("lab")
    tmp = Path(tempfile.mkdtemp(prefix="awb-take-", dir=_tmp_dir(ctx)))
    local = tmp / name
    try:
        c.get(f["key"], local)
        problems = check_problems(local, register=p.register)
        if problems:
            return {"ok": True, "state": "held", "problems": problems}, None
        opaque = gate.is_opaque(name, local.read_bytes())
        c.copy(f["key"], "%s/%s%s" % (code, IN, name))
        c.delete(f["key"])
        out = Path(tempfile.mkstemp(prefix="awb-xchg-", dir=_tmp_dir(ctx))[1])
        os.replace(local, out)
        return {"ok": True, "state": "taken", "name": name, "opaque": opaque}, out
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def serve_owner_has(ctx: Context, req: dict) -> dict:
    name = req.get("name")
    if not valid_name(name):
        raise XchgError("a file name without a folder")
    return {"ok": True, "exists": ctx.client("owner").head(INBOX + name) is not None}


def serve_take_owner(ctx: Context, req: dict) -> dict:
    """The owner path of a take: the original goes from the owner inbox into the vault and the project's folder of
    the owner bucket, the intake runs for the customer, the session gets only the ids of the sanitised copies. A file
    above max_mb (fetched at the version its listing showed) or in a folder that is not exactly one CUST-XXXX stays
    in the inbox (findings 19 and 6 of the review of 2026-10-10)."""
    from awb import bucket, intake, projects

    name, code, ident = req.get("name"), req.get("project"), req.get("id")
    if ident is not None:
        if not (isinstance(ident, str) and ident.startswith(("own-", "prj-")) and _ID_RE.match(ident)):
            raise XchgError("an id of the owner inbox reads own- and twelve characters, of the folder in/ prj-")
    elif not valid_name(name):
        raise XchgError("a file name without a folder")
    project = ctx.active_project(code)
    if project is None:
        raise XchgError("the project is not an active project")
    named = req.get("customer")
    if named is not None and not (isinstance(named, str) and _CUST_RE.match(named)):
        raise XchgError("a customer code reads CUST-XXXX")
    if isinstance(ident, str) and ident.startswith("prj-"):
        return _take_project(ctx, code, project, ident, named)
    p = ctx.paths_fn()
    c = ctx.client("owner")
    if ident is not None:
        f = _resolve(ctx, "owner", ident)
        if f is None:
            return {"ok": True, "state": "absent"}
        src, name, size, etag = f["key"], f["rel"].rsplit("/", 1)[-1], f["size"], f["etag"]
        if not owner_folder_ok(f["rel"]):
            raise XchgError(hold_line(code, "folder"))
        folder_customer = owner_folder_customer(f["rel"])
    else:
        src, folder_customer = INBOX + name, None
    own = project_customer(ctx, project)
    if own == projects.NO_CUSTOMER and project.customer not in (projects.NO_CUSTOMER, "", None) \
            and _grant_of(ctx, code) is None:
        # the row names a customer the owner never granted: his grant cures it, the import does not
        raise XchgError(hold_line(code, "grant", projects.LEGACY_KINDS.get(project.kind, project.kind)))
    customer = take_customer(own, folder_customer, named, code)
    if ident is None:
        h = c.head(src)
        if h is None:
            return {"ok": True, "state": "absent"}
        size, etag = int(h.get("Content-Length", 0) or 0), str(h.get("ETag", ""))
    if size > ctx.max_bytes():
        raise XchgError(hold_line(code, "size"))
    config.ensure_layout(p)
    dst = p.inbox / bucket._local_name(name, p.inbox)
    try:
        c.get(src, dst, if_match=etag.strip('"') or None)
        if dst.stat().st_size > ctx.max_bytes():
            raise XchgError(hold_line(code, "size"))
    except BaseException:
        dst.unlink(missing_ok=True)
        raise
    os.chmod(dst, 0o600)
    folder, _ = bucket.ensure_folder(c, project)
    target = folder + IN + name
    c.copy(src, target)
    if c.head(target) is None:
        raise XchgError("the copy into the project's folder did not arrive; the inbox keeps the file")
    c.delete(src)
    try:
        res = intake.run([dst], customer, p)
    except intake.IntakeError:
        return _hold(ctx, code, customer, "intake")
    return _taken(ctx, code, customer, res)


def _taken(ctx: Context, code: str, customer: str, res) -> dict:
    outputs = [o.name for o in res.outputs if o.name != "intake-report.md"]
    if not outputs:
        # wipe mode never holds for a name; a file that cannot be read as text, or one the final check withheld,
        # gives no copy, and the owner looks at it
        return _hold(ctx, code, customer, "withheld" if res.held else "unreadable")
    return {"ok": True, "state": "taken", "customer": customer, "outputs": outputs}


def _take_project(ctx: Context, code: str, project, ident: str, named: str | None) -> dict:
    """The take of a project from its own folder in/ of the owner bucket: the file stays there, a copy goes into
    the vault inbox and is marked taken (as a pull marks it), the intake runs for the project's customer or, without
    one, under the project code with no customer code, and the session gets only the ids of the sanitised copies."""
    from awb import bucket, intake, projects

    own = project_customer(ctx, project)
    customer = code if own == projects.NO_CUSTOMER else own
    if named is not None and named != customer:
        raise XchgError(hold_line(code))
    f = next((x for x in _project_files(ctx, code) if _file_id("project", x["key"], x["etag"]) == ident), None)
    if f is None:
        return {"ok": True, "state": "absent"}
    if f["size"] > ctx.max_bytes():
        raise XchgError(hold_line(code, "size"))
    p = ctx.paths_fn()
    config.ensure_layout(p)
    dst = p.inbox / bucket._local_name(f["rel"], p.inbox)
    ctx.client("owner").get(f["key"], dst)
    os.chmod(dst, 0o600)
    bucket._remember(p, f["key"], f["etag"])
    try:
        res = intake.run([dst], customer, p)
    except intake.IntakeError:
        return _hold(ctx, code, customer, "intake")
    return _taken(ctx, code, customer, res)


def owner_folder_ok(rel: str) -> bool:
    """A file of the owner inbox lies at its top or in exactly one folder that is a valid CUST-XXXX; any other
    folder (lower case, a suffix, a deeper level) holds the take instead of reading as the top (finding 6)."""
    parts = rel.split("/")
    return len(parts) == 1 or (len(parts) == 2 and bool(_CUST_RE.match(parts[0])))


def owner_folder_customer(rel: str) -> str | None:
    """The customer the owner named for a file by the folder he dropped it into: inbox/CUST-XXXX/<file>."""
    parts = rel.split("/")
    return parts[0] if len(parts) > 1 and _CUST_RE.match(parts[0]) else None


def take_customer(own: str, folder: str | None, named: str | None, code: str = "tcp-xxxx") -> str:
    """The customer of an owner-inbox take (F5): the project's own; the owner's folder, when he used one, has to be
    that customer's. A session's code never chooses it, it only has to agree. A project without a customer takes
    nothing from the owner inbox (its material comes from its own folder in/). Every refusal is the one line."""
    from awb import projects

    own = None if own in (projects.NO_CUSTOMER, "", None) else own
    if own is not None and folder and folder != own:
        raise XchgError(hold_line(code, "folder"))
    if own is None or (named and named != own):
        raise XchgError(hold_line(code))
    return own


def hold_line(code: str, cls: str | None = None, kind: str | None = None) -> str:
    """The one line of a hold: what the owner runs, in his own terminal (the work rules hand out nothing else).
    With a class word of codes.HOLD_CLASSES (P2 of F3) the line names the kind of refusal and no value; a missing
    grant names awb projects kind with the project's kind, which the import does not cure."""
    from awb import codes

    if cls == "grant":
        return codes.GRANT_HOLD_LINE % (code, kind if kind in ("query", "project") else "project|query")
    if cls in codes.HOLD_CLASSES:
        return codes.CLASS_HOLD_LINE % (cls, code)
    return codes.HOLD_LINE % code


HOLD_CLASS_OF = {"unreadable": "unreadable"}
"""The class word of a hold of the intake by its reason key of HOLD_REASONS; the others keep the plain line."""


def _hold(ctx: Context, code: str, customer: str, why: str) -> dict:
    ctx.notify(*exchange_mail("held", code, customer=customer, why=why))
    return {"ok": True, "state": "held", "why": HOLD_REASONS[why], "customer": customer,
            "class": HOLD_CLASS_OF.get(why)}


WEB_MONTH_RE = re.compile("^" + bucket_layout.WEB_MONTH + "/$")
_WEB_FOLDER_RE = re.compile("^" + bucket_layout.WEB_MONTH + "/(" + codes.TCP_PROJECT + ")/" + bucket_layout.IN)
WEB_MAX_OBJECTS = 2000
"""The most objects one listing of the web mode returns; more is refused as too many."""
WEB_MAX_ROOT = 1000
"""The most entries the top of the owner bucket may hold for the month folders to be named."""


def _web_folder(ctx: Context, which: str, path: str) -> tuple[str, str | None] | None:
    """The allowed folder that `path` (a prefix or a key) lies in and the project it belongs to, or None: inbox/
    of either bucket, <YYYY-MM>/<code>/in/ of an active project in the owner bucket."""
    if not isinstance(path, str) or path.startswith("/") or "//" in path or ".." in path.split("/") or \
            any(ord(c) < 32 or ord(c) == 127 for c in path):
        return None
    if path.startswith(INBOX):
        return INBOX, None
    m = _WEB_FOLDER_RE.match(path)
    if which == "owner" and m and ctx.active_project(m.group(1)) is not None:
        return m.group(0), m.group(1)
    return None


def _web_refusal(kind: str, error: str, status: int = 0) -> tuple[dict, None]:
    return {"ok": False, "kind": kind, "error": error, **({"status": status} if status else {})}, None


def serve_web_read(ctx: Context, req: dict) -> tuple[dict, Path | None]:
    """A read of the owner's materials service: `months` of the owner bucket, `list` of an allowed folder or `get`
    of one object at the version it names. A refusal carries a kind (setup, refused, too_many, inconsistent,
    too_large, changed, http) and, for an answer of the object storage, its status."""
    from awb import obs

    what, which = req.get("what"), req.get("bucket")
    if which not in ("lab", "owner"):
        return _web_refusal("refused", "the web mode reads the lab bucket or the owner bucket")
    try:
        c = ctx.client(which)
    except XchgError as err:
        return _web_refusal("setup", str(err))
    try:
        if what == "months":
            if which != "owner":
                return _web_refusal("refused", "month folders exist in the owner bucket only")
            listing = c.list("", "/")
            if len(listing.objects) + len(listing.prefixes) > WEB_MAX_ROOT:
                return _web_refusal("too_many", "the top of the owner bucket holds too many entries")
            return {"ok": True, "months": sorted(p for p in listing.prefixes if WEB_MONTH_RE.match(p))}, None
        if what == "list":
            prefix = req.get("prefix")
            folder = _web_folder(ctx, which, prefix)
            if folder is None or folder[0] != prefix:
                return _web_refusal("refused", "a listing is allowed for inbox/ and an active project's in/ only")
            listing = c.list(prefix)
            rows, seen = [], set()
            for key, size, etag in listing.objects:
                if not key.startswith(prefix) or key in seen:
                    return _web_refusal("inconsistent", "the listing named a key twice or outside its folder")
                seen.add(key)
                if key.endswith("/"):
                    continue
                rows.append({"key": key, "size": size, "etag": etag, "modified": listing.modified.get(key, "")})
                if len(rows) > WEB_MAX_OBJECTS:
                    return _web_refusal("too_many", "the folder holds too many objects")
            return {"ok": True, "objects": rows, "project": folder[1]}, None
        if what == "get":
            key, etag, limit = req.get("key"), req.get("etag"), req.get("limit")
            folder = _web_folder(ctx, which, key)
            if folder is None or len(key) <= len(folder[0]) or key.endswith("/"):
                return _web_refusal("refused", "a read is allowed inside inbox/ and an active project's in/ only")
            if not isinstance(etag, str) or not etag.strip('"'):
                return _web_refusal("refused", "a read names the version it wants")
            cap = ctx.max_bytes()
            if isinstance(limit, int) and not isinstance(limit, bool) and 0 < limit < cap:
                cap = limit
            head = c.head(key)
            if head is None or str(head.get("ETag", "")).strip('"') != etag.strip('"'):
                return _web_refusal("changed", "the object is gone or is another version now")
            if int(head.get("Content-Length", 0) or 0) > cap:
                return _web_refusal("too_large", "the object is larger than the limit")
            tmp = Path(tempfile.mkstemp(prefix="awb-web-", dir=_tmp_dir(ctx))[1])
            try:
                c.get(key, tmp, if_match=etag)
                if tmp.stat().st_size > cap:
                    raise XchgError("the object grew past the limit")
            except BaseException:
                tmp.unlink(missing_ok=True)
                raise
            return {"ok": True, "etag": etag.strip('"'), "size": tmp.stat().st_size, "project": folder[1]}, tmp
        return _web_refusal("refused", "the web mode knows months, list and get")
    except obs.OBSError as err:
        if err.status == 412:
            return _web_refusal("changed", "the object is another version now")
        return _web_refusal("http", "the object storage refused the read", err.status)
    except XchgError as err:
        return _web_refusal("too_large", str(err))


# xchg.py imports this module at its end and its session side calls from_in, hold_line and put_id. Whichever of the
# two loads first, the join runs once both hold their names.
parts.join("awb.tcp.xchg", __name__)
