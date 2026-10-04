"""The exchange between the owner and the sessions through OBS (T-102): two inboxes and a from-session folder.

    awb inbox take FILE [--project CODE] [--customer CUST-XXXX]   take a file the owner dropped into an inbox
    awb inbox list                                                the lab inbox (names checked first)
    awb xchg put FILE [--as NAME] [--image --reason TEXT]         a result into <code>/from-session/<date>/
    awb xchg list                                                 what this project put

The owner drops a file into one of two inboxes in the OBS console, with no project code and no command:

    <lab bucket>/inbox/     material without customer content (vendor images, test data, public documents)
    <owner bucket>/inbox/   anything that may hold customer material; only the owner's IAM user reaches it

A session takes the file the owner names. From the lab inbox the key service fetches it; a file the name check
passes is copied to `<lab bucket>/<code>/in/`, saved to `input/` and removed from the inbox, a file with a hit or
one that cannot be checked stays in the inbox. From the owner inbox the session never sees the original: the key
service (the owner's process) moves it into the project's folder of the owner bucket, runs `awb intake` for the
project's customer and hands the session only the sanitised copies; with unknown name candidates the file is held
and the owner is told. Results go back with `awb xchg put` into `<lab bucket>/<code>/from-session/<date>/`, after
the name check and, for a project with a customer or partner, the send gate of `awb bucket put`. The owner gets a
mail through the SMN topic of the settings when a session puts a file and when a take holds one.

The service side (`serve_obs`, `serve_take_owner`, `serve_owner_has`) runs inside the key service and is refused
everything outside the allowed prefixes before anything is signed: `inbox/` of the lab bucket (get, head, delete,
copy into the project, list), `<code>/in/` and `<code>/from-session/` of an active project code. Never a policy, an
ACL or another bucket.

The web mode (`serve_web_read`) is the owner's materials service of the web console reading both buckets: a copy,
never a move. It lists `inbox/` of either bucket and `<YYYY-MM>/<code>/in/` of an active project in the owner
bucket, names the month folders of the owner bucket and reads one object at the version (ETag) its listing showed;
another version fails as changed. It never writes, deletes or copies, and only the owner's own processes reach it. The owner's settings live in `~/.config/awb/keys.conf` and travel with `awb keys unlock`:

    bucket_tenant = <alias whose lab key reaches both buckets>
    lab_bucket = awb-lab-eu-de        owner_bucket = awb        region = eu-de
    max_mb = 100                      notify_topic = <urn of an SMN topic>
"""
from __future__ import annotations

import datetime
import json
import os
import re
import shutil
import sys
import tempfile
from pathlib import Path

from awb import config

INBOX = "inbox/"
FROM = "from-session/"
IN = "in/"
DEFAULTS = {"lab_bucket": "awb-lab-eu-de", "owner_bucket": "awb", "region": "eu-de", "max_mb": "100"}
SETTING_KEYS = ("bucket_tenant", "lab_bucket", "owner_bucket", "region", "max_mb", "notify_topic")
_NAME_RE = re.compile(r"^[^/\\\x00-\x1f\x7f]{1,200}$")
_CODE_RE = re.compile(r"^tcp-[a-z0-9]{4}$")
_CUST_RE = re.compile(r"^CUST-[A-Z2-7]{4}$")
TEXT_SUFFIXES = (".md", ".txt", ".csv", ".tsv", ".json", ".yaml", ".yml", ".log", ".tf", ".py", ".sh", ".xml",
                 ".html", ".htm", ".ini", ".conf", ".cfg", ".toml", ".sql", ".ps1", ".rst")
IMAGE_SUFFIXES = (".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg")


class XchgError(Exception):
    """An exchange step that cannot be done. The message never carries a key or a file name of the owner."""


# --------------------------------------------------------------------------- settings (owner side)


def settings_file() -> Path:
    return Path(os.environ.get("AWB_KEYS_CONF") or "~/.config/awb/keys.conf").expanduser()


def read_settings(path: Path | None = None) -> dict:
    out = dict(DEFAULTS)
    try:
        text = (path or settings_file()).read_text(encoding="utf-8")
    except FileNotFoundError:
        return out
    for n, line in enumerate(text.splitlines(), start=1):
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        key, sep, value = line.partition("=")
        key, value = key.strip(), value.strip()
        if not sep or key not in SETTING_KEYS:
            raise XchgError("line %d of keys.conf is not a known setting" % n)
        out[key] = value
    return out


def valid_name(name: str) -> bool:
    return isinstance(name, str) and bool(_NAME_RE.match(name)) and name not in (".", "..") and \
        not name.startswith(".")


# --------------------------------------------------------------------------- service side (inside the key service)


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
            return max(1, int(self.settings.get("max_mb") or 100)) * 1024 * 1024
        except ValueError:
            return 100 * 1024 * 1024


def allowed_key(key: str, method: str, project: str | None) -> bool:
    """The object keys a session may touch: inbox/<name> for a take, <code>/in/ and <code>/from-session/ of its
    own active project. Writes only into the project's two folders."""
    if not isinstance(key, str) or not key or key.startswith("/") or ".." in key.split("/") or "//" in key:
        return False
    if key.startswith(INBOX):
        return method in ("GET", "HEAD", "DELETE") and valid_name(key[len(INBOX):])
    if project and _CODE_RE.match(project):
        for sub in (IN, FROM):
            base = "%s/%s" % (project, sub)
            if key.startswith(base) and len(key) > len(base):
                return method in ("GET", "HEAD", "PUT", "DELETE")
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
        src, dst = req.get("source"), req.get("key")
        if not (isinstance(src, str) and src.startswith(INBOX) and allowed_key(src, "GET", project)
                and allowed_key(dst, "PUT", project) and dst.startswith("%s/%s" % (project, IN))):
            raise XchgError("a copy goes from inbox/ into the project's in/ only")
        c.copy(src, dst)
        return {"ok": True}, None
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
        c.put_file(key, body_path, req.get("content_type") or "application/octet-stream")
        size = body_path.stat().st_size
        if key.startswith("%s/%s" % (project, FROM)):
            ctx.notify("Workbench: %s put a file" % project,
                       "Project %s put a file of %d bytes into %s. Fetch it in the OBS console, bucket %s."
                       % (project, size, "/".join(key.split("/")[:3]) + "/", ctx.settings["lab_bucket"]))
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


def serve_owner_has(ctx: Context, req: dict) -> dict:
    name = req.get("name")
    if not valid_name(name):
        raise XchgError("a file name without a folder")
    return {"ok": True, "exists": ctx.client("owner").head(INBOX + name) is not None}


def serve_take_owner(ctx: Context, req: dict) -> dict:
    """The owner path of a take: the original goes from the owner inbox into the vault and the project's folder of
    the owner bucket, the intake runs for the customer, the session gets only the ids of the sanitised copies."""
    from awb import bucket, intake, projects

    name, code = req.get("name"), req.get("project")
    if not valid_name(name):
        raise XchgError("a file name without a folder")
    project = ctx.active_project(code)
    if project is None:
        raise XchgError("the project is not an active project")
    customer = req.get("customer") or project.customer
    if customer == projects.NO_CUSTOMER or not customer:
        raise XchgError("the project has no customer: name the customer code (--customer CUST-XXXX)")
    if not _CUST_RE.match(customer):
        raise XchgError("a customer code reads CUST-XXXX")
    if project.customer not in (projects.NO_CUSTOMER, customer):
        raise XchgError("the customer code is not the customer of the project")
    p = ctx.paths_fn()
    c = ctx.client("owner")
    src = INBOX + name
    if c.head(src) is None:
        return {"ok": True, "state": "absent"}
    config.ensure_layout(p)
    dst = p.inbox / bucket._local_name(name, p.inbox)
    c.get(src, dst)
    os.chmod(dst, 0o600)
    folder, _ = bucket.ensure_folder(c, project)
    target = folder + IN + name
    c.copy(src, target)
    if c.head(target) is None:
        raise XchgError("the copy into the project's folder did not arrive; the inbox keeps the file")
    c.delete(src)
    try:
        res = intake.run([dst], customer, p)
    except intake.IntakeError as err:
        ctx.notify("Workbench: a take for %s stopped" % code,
                   "The intake for %s stopped: %s. The original is in the project's in/ folder of the owner "
                   "bucket and in the vault inbox." % (code, err))
        return {"ok": True, "state": "held", "why": "the intake stopped"}
    if res.blocked:
        ctx.notify("Workbench: a take for %s is held" % code,
                   "The intake for %s found %d name candidate(s). Read the private report with awb vault show, "
                   "register or keep the candidates, then run awb intake --customer %s in your own shell."
                   % (code, res.candidates, customer))
        return {"ok": True, "state": "held", "why": "name candidates for the owner", "customer": customer}
    return {"ok": True, "state": "taken", "customer": customer,
            "outputs": [o.name for o in res.outputs if o.name != "intake-report.md"]}


WEB_MONTH_RE = re.compile(r"^\d{4}-(?:0[1-9]|1[0-2])/$")
_WEB_FOLDER_RE = re.compile(r"^\d{4}-(?:0[1-9]|1[0-2])/(tcp-[a-z2-7]{4})/in/")
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


# --------------------------------------------------------------------------- session side


def _project() -> tuple[Path, str, str]:
    from awb import review

    root = review.find_project()
    try:
        lines = (root / "SCOPE.md").read_text(encoding="utf-8").splitlines()
    except OSError:
        raise XchgError("not inside a project (no SCOPE.md)") from None
    code = next((l.split(":", 1)[1].strip() for l in lines if l.startswith("- code:")), "")
    customer = next((l.split(":", 1)[1].strip() for l in lines if l.startswith("- customer:")), "none")
    if not _CODE_RE.match(code):
        raise XchgError("SCOPE.md names no project code")
    return root, code, customer


def _call(obj: dict, upload: Path | None = None, download: Path | None = None) -> dict:
    from awb.tcp import keys

    return keys.request(keys.call_socket(), obj, upload=upload, download=download)


def _ok(answer: dict) -> dict:
    if not answer.get("ok"):
        raise XchgError(answer.get("error") or "the key service said no")
    return answer


def check_problems(path: Path, image: bool = False) -> list[str]:
    """Why a file must not cross: a name check hit, a file that cannot be read as text."""
    from awb import check

    suffix = path.suffix.lower()
    if suffix in IMAGE_SUFFIXES:
        return [] if image else ["a picture crosses only with --image and a reason"]
    try:
        hits = check.check_file(path, config.paths().register)
    except Exception as err:
        return ["the name check cannot run (%s)" % type(err).__name__]
    classes = sorted({h.get("cls", "unknown") for h in hits if isinstance(h, dict)})
    if hits:
        return ["the name check found %d hit(s): %s" % (len(hits), ", ".join(classes))]
    return []


def take(name: str, project: str | None = None, customer: str | None = None) -> str:
    if not valid_name(name):
        raise XchgError("give the file name as the owner wrote it, without a folder")
    root, code, scope_customer = _project()
    code = project or code
    lab = _ok(_call({"op": "obs", "method": "HEAD", "key": INBOX + name, "project": code}))
    own = _ok(_call({"op": "owner_has", "name": name}))
    if lab.get("exists") and own.get("exists"):
        raise XchgError("the file is in both inboxes: ask the owner which one he means")
    if not lab.get("exists") and not own.get("exists"):
        raise XchgError("the file is in neither inbox")
    if own.get("exists"):
        a = _ok(_call({"op": "take_owner", "name": name, "project": code, "customer": customer}))
        if a.get("state") == "held":
            return "held on the owner's side (%s); the owner has been told" % a.get("why", "held")
        if a.get("state") != "taken":
            raise XchgError("the file left the owner inbox before the take")
        outbox = config.paths().outbox / a["customer"]
        moved = []
        for out in a.get("outputs", []):
            src = outbox / out
            if src.is_file():
                shutil.move(str(src), str(root / "input" / out))
                moved.append(out)
        return "taken from the owner inbox through the intake: %d sanitised copy(ies) in input/ (%s)" % (
            len(moved), ", ".join(moved) or "-")
    with tempfile.TemporaryDirectory(prefix="awb-take-") as tmp:
        local = Path(tmp) / name
        a = _ok(_call({"op": "obs", "method": "GET", "key": INBOX + name, "project": code}, download=local))
        if not a.get("exists"):
            raise XchgError("the file left the lab inbox before the take")
        problems = check_problems(local)
        if problems:
            return "held in the lab inbox: %s; the owner moves it to the owner inbox or releases it" % \
                "; ".join(problems)
        dest = root / "input" / name
        if dest.exists():
            raise XchgError("input/ already holds a file of that name")
        _ok(_call({"op": "obs", "method": "COPY", "source": INBOX + name, "key": "%s/%s%s" % (code, IN, name),
                   "project": code}))
        shutil.copyfile(local, dest)
        _ok(_call({"op": "obs", "method": "DELETE", "key": INBOX + name, "project": code}))
    return "taken from the lab inbox: input/%s, a copy in the project's in/ folder" % name


def inbox_list() -> list[str]:
    _, code, _ = _project()
    a = _ok(_call({"op": "obs", "method": "LIST", "prefix": INBOX, "project": code}))
    lines = ["%s  %d bytes" % (o["name"], o["size"]) for o in a.get("objects", [])]
    if a.get("withheld"):
        lines.append("%d file(s) withheld: their names hold a hit of the name check" % a["withheld"])
    return lines


def put(path: Path, as_name: str | None = None, image: bool = False, reason: str | None = None) -> str:
    from awb import projects, review

    root, code, customer = _project()
    path = Path(os.path.realpath(path))
    if not path.is_file():
        raise XchgError("the file to put is not there")
    name = as_name or path.name
    if not valid_name(name):
        raise XchgError("a name without a folder")
    if image and not (reason and len(reason.strip()) >= 10):
        raise XchgError("--image needs a reason of one line: what it shows and that you checked it for names")
    problems = check_problems(path, image=image)
    if problems:
        raise XchgError("; ".join(problems))
    if customer not in (projects.NO_CUSTOMER, "", None):
        verdict, message = review.send_check(root, path, True)
        if verdict == review.SEND_REFUSE:
            raise XchgError("the send gate refused the file: %s" % message)
    key = "%s/%s%s/%s" % (code, FROM, datetime.date.today().isoformat(), name)
    with tempfile.TemporaryDirectory(prefix="awb-put-") as tmp:
        copy = Path(tmp) / name
        shutil.copyfile(path, copy)
        a = _ok(_call({"op": "obs", "method": "PUT", "key": key, "project": code,
                       "content_type": "application/octet-stream"}, upload=copy))
    if image:
        _log_image(root, name, reason)
    return "put: %s (%d bytes); the owner gets a mail" % (key, a.get("size", 0))


def _log_image(root: Path, name: str, reason: str) -> None:
    with open(root / "evidence" / "images-put.log", "a", encoding="utf-8") as f:
        f.write("%s\t%s\t%s\n" % (datetime.datetime.now().isoformat(timespec="seconds"), name,
                                  " ".join(reason.split())))


def put_list() -> list[str]:
    _, code, _ = _project()
    a = _ok(_call({"op": "obs", "method": "LIST", "prefix": "%s/%s" % (code, FROM), "project": code}))
    return ["%s  %d bytes" % (o["name"], o["size"]) for o in a.get("objects", [])]


def leftovers(code: str) -> int | None:
    """Files left in the project's in/ and from-session/ of the lab bucket, None when the key service cannot say."""
    try:
        n = 0
        for sub in (IN, FROM):
            a = _call({"op": "obs", "method": "LIST", "prefix": "%s/%s" % (code, sub), "project": code})
            if not a.get("ok"):
                return None
            n += len(a.get("objects", []))
        return n
    except Exception:
        return None


# --------------------------------------------------------------------------- command line


def main_inbox(argv: list[str] | None = None) -> int:
    from awb.cli import SafeParser

    ap = SafeParser(prog="awb inbox", description="Take a file the owner dropped into an inbox.")
    sub = ap.add_subparsers(dest="command")
    t = sub.add_parser("take", help="take FILE from an inbox into this project")
    t.add_argument("file")
    t.add_argument("--project", default=None)
    t.add_argument("--customer", default=None, help="the customer code, for a project with customer none")
    sub.add_parser("list", help="the lab inbox")
    try:
        args = ap.parse_args(argv)
    except SystemExit as exc:
        return 0 if exc.code == 0 else 2
    if not args.command:
        ap.print_help()
        return 2
    try:
        if args.command == "take":
            print(take(args.file, args.project, args.customer))
        else:
            for line in inbox_list() or ["the lab inbox is empty"]:
                print(line)
        return 0
    except XchgError as err:
        print("awb inbox: %s" % err, file=sys.stderr)
        return 1
    except Exception as err:
        print("awb inbox: %s" % (str(err) if type(err).__name__ == "KeysError" else type(err).__name__),
              file=sys.stderr)
        return 2


def main_xchg(argv: list[str] | None = None) -> int:
    from awb.cli import SafeParser

    ap = SafeParser(prog="awb xchg", description="Results of this project into the owner's from-session folder.")
    sub = ap.add_subparsers(dest="command")
    pt = sub.add_parser("put", help="a file into <code>/from-session/<date>/")
    pt.add_argument("file")
    pt.add_argument("--as", dest="as_name", default=None)
    pt.add_argument("--image", action="store_true")
    pt.add_argument("--reason", default=None)
    sub.add_parser("list", help="what this project put")
    try:
        args = ap.parse_args(argv)
    except SystemExit as exc:
        return 0 if exc.code == 0 else 2
    if not args.command:
        ap.print_help()
        return 2
    try:
        if args.command == "put":
            print(put(Path(args.file), args.as_name, args.image, args.reason))
        else:
            for line in put_list() or ["nothing put yet"]:
                print(line)
        return 0
    except XchgError as err:
        print("awb xchg: %s" % err, file=sys.stderr)
        return 1
    except Exception as err:
        print("awb xchg: %s" % (str(err) if type(err).__name__ == "KeysError" else type(err).__name__),
              file=sys.stderr)
        return 2


main = main_xchg
