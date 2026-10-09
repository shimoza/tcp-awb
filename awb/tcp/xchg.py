"""The exchange between the owner and the sessions through OBS (T-102): two inboxes and a from-session folder.

    awb inbox take WORDS... [--project CODE] [--customer CUST-XXXX]   the file the owner describes, from an inbox
                                                                      or the project's own folder in/
    awb inbox take --all | --id ID...                                 every file of both inboxes, or the ones named
    awb inbox list                                                    both inboxes: id, kind, size, time
    awb xchg put FILE [--as NAME] [--image --reason TEXT]         a result into <code>/from-session/<date>/<id>
    awb xchg list                                                 what this project put
    awb xchg clean CODE [--older-than DATE] --dry-run|--go        delete what this project put, rows in RESOURCES.md

The owner drops a file into one of two inboxes in the OBS console, with no project code and no command:

    <lab bucket>/inbox/     material without customer content (vendor images, test data, public documents)
    <owner bucket>/inbox/   anything that may hold customer material; only the owner's IAM user reaches it

The owner never has to know a file's name: he says what it is ("the pdf", "the excel with the sizing", "the
newest", "all files") and the session passes his words. The key service matches them against the names of both
inboxes: the exact name, the same letters (case, dashes and the extension aside), then kind words in English,
German or Russian (pdf, excel, Tabelle, презентация), a word for the newest file and the other words inside the
name; several ways of saying it go in one call, separated by |. One file found: it is taken. None or several: the
session gets the list of both inboxes (an id, the kind, the size and the time, the name only for a file of the lab
inbox the name check passes, never for one of the owner inbox), picks by his description or asks him, and takes
by id. From the lab inbox the key service fetches it and runs the name check on it in its own process (F6); a
file that passes is copied to `<lab bucket>/<code>/in/`, removed from the inbox and handed to the session for
`input/`, a file with a hit or one that cannot be checked stays in the inbox and the session gets no byte of it.
From the owner inbox the session never sees the original: the key service (the owner's process) moves it into the
project's folder of the owner bucket, runs `awb intake` for the customer and hands the session only the sanitised
copies; a file the intake cannot read is held and the owner is told. The customer of an owner-inbox take is the
project's own; a folder `inbox/CUST-XXXX/` the owner drops a file into has to be that customer's (F5), and a
session's --customer only has to agree with it. A project without a customer takes nothing from the owner inbox: its
material comes from its own folder `in/` of the owner bucket (spawn makes it, op owner_folder; the first take makes
it when spawn could not), whose new files a find lists beside the two inboxes for every project; the key service
runs the same intake for them, for the project's customer or, without one, under the project code with no customer
code. "take the files from in" takes every new file of `in/`. Every refusal of a take is the one line of the work
rules (`hold_line`). Results go back
with `awb xchg put` into `<lab bucket>/<code>/from-session/<date>/`, after the name check and the send gate of `awb
bucket put`, both run again by the key service on the bytes it received (F6). The owner gets a mail through the SMN topic of the settings
when a session puts a file and when a take holds one.

The service side (`serve_obs`, `serve_take_lab`, `serve_take_owner`, `serve_owner_has`) runs inside the key service
and is refused everything outside the allowed prefixes before anything is signed: `inbox/` of the lab bucket (head
and list; a get, a copy and a delete only through `serve_take_lab`), `<code>/in/` and `<code>/from-session/` of an
active project code. A put is authorised before its bytes are received (F8). Never a policy, an ACL or another
bucket.

The owner's console puts a file into a project's `in/` under an id (`serve_web_put_in`, the owner's processes only);
the session's take is the one path from there.

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
import hashlib
import hmac
import json
import os
import re
import shutil
import sys
import tempfile
import unicodedata
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


# --------------------------------------------------------------------------- which file he means


FIND_MAX = 50
"""The most files the two inboxes may hold for a find; the owner keeps a handful there."""
_ID_RE = re.compile(r"^(lab|own|prj)-[0-9a-f]{12}$")
_ID_SECRET = os.urandom(16)
"""The ids of inbox files hold while the key service runs; after a restart the session finds the file again."""
_SHEET = {"xlsx", "xls", "xlsm", "ods", "csv"}
_DOC = {"docx", "doc", "odt", "rtf"}
_SLIDES = {"pptx", "ppt", "odp"}
_TEXT = {"txt", "md"}
_IMAGE = {"png", "jpg", "jpeg", "gif", "webp", "svg"}
_ARCHIVE = {"zip", "7z", "gz", "tgz", "tar"}
KIND_WORDS = {
    "pdf": {"pdf"}, "пдф": {"pdf"},
    "excel": _SHEET, "эксел": _SHEET, "таблиц": _SHEET, "spreadsheet": _SHEET, "sheet": _SHEET, "tabelle": _SHEET,
    "xlsx": _SHEET, "xls": _SHEET, "csv": {"csv"},
    "word": _DOC, "ворд": _DOC, "docx": _DOC, "doc": _DOC,
    "powerpoint": _SLIDES, "presentation": _SLIDES, "slides": _SLIDES, "deck": _SLIDES, "презентац": _SLIDES,
    "слайд": _SLIDES, "präsentation": _SLIDES, "folien": _SLIDES, "pptx": _SLIDES,
    "text": _TEXT, "текст": _TEXT, "txt": _TEXT, "markdown": _TEXT,
    "image": _IMAGE, "picture": _IMAGE, "screenshot": _IMAGE, "картинк": _IMAGE, "скрин": _IMAGE, "фото": _IMAGE,
    "bild": _IMAGE, "png": _IMAGE, "jpg": _IMAGE,
    "archive": _ARCHIVE, "архив": _ARCHIVE, "zip": _ARCHIVE,
}
"""Words for a kind of file and the extensions they mean; a word of four letters or more also takes its endings."""
_NEWEST = ("last", "latest", "newest", "new", "recent", "последн", "нов", "свеж", "neu", "letzt")
_FILLER = {"the", "a", "an", "file", "files", "my", "this", "that", "these", "those", "please", "of", "in", "from",
           "for", "to", "and", "or", "with", "inbox", "all", "both", "take", "get", "fetch",
           "файл", "файлы", "файла", "файлик", "в", "из", "и", "или", "для", "с", "мой", "мои", "этот", "эти", "тот",
           "все", "всё", "оба", "обе", "инбокс", "инбокса", "инбоксе", "забери", "забирай", "возьми",
           "die", "der", "das", "datei", "dateien", "und", "oder", "von", "aus", "im", "alle", "beide", "hol", "nimm"}


def _fold(text: str) -> str:
    """Letters and digits only, in one case and one Unicode form (a name typed on a Mac may arrive decomposed)."""
    return "".join(ch for ch in unicodedata.normalize("NFKC", text).casefold() if ch.isalnum())


def suffix(rel: str) -> str:
    base = rel.rsplit("/", 1)[-1]
    return base.rsplit(".", 1)[-1].lower() if "." in base[1:] else ""


def _forms(rel: str) -> set[str]:
    base = rel.rsplit("/", 1)[-1]
    stem = base.rsplit(".", 1)[0] if "." in base[1:] else base
    return {_fold(rel), _fold(base), _fold(stem)}


def _kinds_of(token: str) -> set[str] | None:
    for word, kinds in KIND_WORDS.items():
        w = _fold(word)
        if token == w or (len(w) >= 4 and token.startswith(w)):
            return kinds
    return None


def _is_newest(token: str) -> bool:
    """A word for the newest file; the Russian and German stems take their endings (последний, neueste)."""
    return any(token == w or (token.startswith(w) and (not w.isascii() or w in ("neu", "letzt"))) for w in _NEWEST)


def match(words: str, rels: list[str], modified: list[str] | None = None) -> list[int]:
    """The indexes of the files `words` means, `rels` being their names under inbox/. The exact name wins, then the
    same letters and digits, then each alternative of `words` (separated by |): its kind words pick the type, a word
    for the newest keeps the newest file, every other word must be inside the name."""
    wanted = words.strip()
    exact = [i for i, r in enumerate(rels) if wanted in (r, r.rsplit("/", 1)[-1])]
    if exact:
        return exact
    folded = _fold(wanted)
    same = [i for i, r in enumerate(rels) if folded and folded in _forms(r)]
    if same:
        return same
    found: list[int] = []
    for alternative in wanted.split("|"):
        kinds: set[str] = set()
        newest, rest = False, []
        for raw in re.findall(r"\w+", unicodedata.normalize("NFKC", alternative).casefold()):
            t = _fold(raw)
            if not t or t in _FILLER:
                continue
            k = _kinds_of(t)
            if k is not None:
                kinds |= k
            elif _is_newest(t):
                newest = True
            elif len(t) >= 2 or t.isdigit():
                rest.append(t)
        if not (kinds or newest or rest):
            continue
        hits = [i for i, r in enumerate(rels) if (not kinds or suffix(r) in kinds) and all(t in _fold(r) for t in rest)]
        if newest and len(hits) > 1 and modified:
            last = max(modified[i] for i in hits)
            hits = [i for i in hits if modified[i] == last]
        found += [i for i in hits if i not in found]
    return found


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
    """The object keys a session may touch: inbox/<name> to look at (head), <code>/in/ and <code>/from-session/ of
    its own active project. Writes only into the project's two folders. A get or a delete of the lab inbox goes
    through serve_take_lab alone, so the name check of the service cannot be skipped (F6)."""
    if not isinstance(key, str) or not key or key.startswith("/") or ".." in key.split("/") or "//" in key:
        return False
    if key.startswith(INBOX):
        return method == "HEAD" and all(valid_name(part) for part in key[len(INBOX):].split("/"))
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
        authorise_put(ctx, req)
        sent = key.startswith("%s/%s" % (project, FROM))
        if sent:
            problems = put_problems(ctx, req, body_path)
            if problems:
                raise XchgError("; ".join(problems))
        c.put_file(key, body_path, "application/octet-stream")
        size = body_path.stat().st_size
        if sent:
            ident = key.rsplit("/", 1)[-1]
            # the name travels only when the name check of the service passes it, else the id stands for it
            name = req.get("name")
            if not (valid_name(name) and not _name_hits([name], ctx.paths_fn())):
                name = ident
            ctx.notify(*exchange_mail("put", project, ident=ident, size=size, name=name))
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
    """Whether a put may be received at all (F8): an active project, a key inside its own folders, a key of the
    form of put_key_ok under from-session/. Run by the key service before it reads a byte of the upload."""
    project, key = req.get("project"), req.get("key")
    if not (isinstance(project, str) and ctx.active_project(project) is not None):
        raise XchgError("the project is not an active project")
    if not allowed_key(key, "PUT", project):
        raise XchgError("the object call is outside inbox/ and the project's own folders")
    if key.startswith("%s/%s" % (project, FROM)) and not put_key_ok(key, project):
        raise XchgError("a put goes to <code>/from-session/<date>/<file id>")


def put_problems(ctx: Context, req: dict, body_path: Path) -> list[str]:
    """The checks of a put into from-session/, run by the key service on the bytes it received (F6): the name
    check of the content (a picture only with image and a reason), then the send gate on the project's folder
    under the projects root (never the path a row of projects.tsv names), for a customer project in its strict
    form. A name the name check does not pass never travels (the mail names the id, TM0 item 1), so the bytes are
    judged under the id then."""
    from awb import projects, review

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
        for_customer = project.customer not in (projects.NO_CUSTOMER, "", None)
        verdict, message = review.send_check(p.projects_root / project.code, judged, for_customer,
                                             register_path=p.register, workbench=p, name=name)
    if verdict == review.SEND_REFUSE:
        return ["the send gate refused the file: %s" % message]
    return []


def exchange_mail(state: str, project: str, ident: str | None = None, size: int | None = None,
                  name: str | None = None, customer: str | None = None, why: str | None = None) -> tuple[str, str]:
    """The subject and the message of an exchange mail, built from fixed fields only: the project code, the object
    id, the size, the state (and for a hold the customer code and a reason of HOLD_REASONS). A field that does not
    read as its kind is sent as -; no key, no prefix, no error text, no file name the service did not pass."""
    code = project if isinstance(project, str) and _CODE_RE.match(project) else "-"
    if state == "put":
        ident = ident if isinstance(ident, str) and _PUT_ID_RE.match(ident) else "-"
        shown = name if isinstance(name, str) and name else ident
        return ("Workbench: %s put a file" % code,
                "Project %s put %s (id %s, %d bytes) into its from-session folder of the lab bucket. Fetch it in the "
                "OBS console." % (code, shown, ident, int(size or 0)))
    cust = customer if isinstance(customer, str) and _CUST_RE.match(customer) else "-"
    reason = HOLD_REASONS.get(why or "", "held")
    return ("Workbench: a take for %s is held" % code,
            "A take for %s (%s) is held: %s. The original is in the project's in/ folder of the owner bucket and in "
            "the vault inbox. %s." % (code, cust, reason, hold_line(code)))


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


def _no_customer(project) -> bool:
    from awb import projects

    return project is not None and project.customer in (projects.NO_CUSTOMER, "", None)


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
    only for a file of the lab inbox the name check passes, never for one of the owner inbox. His words meet those
    names here, inside his own process."""
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
        picked = match(words, [f["rel"] for f in files], [f["modified"] for f in files])
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
    the owner bucket, the intake runs for the customer, the session gets only the ids of the sanitised copies."""
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
        src, name = f["key"], f["rel"].rsplit("/", 1)[-1]
        folder_customer = owner_folder_customer(f["rel"])
    else:
        src, folder_customer = INBOX + name, None
    customer = take_customer(project.customer, folder_customer, named, code)
    if ident is None and c.head(src) is None:
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
    from awb import bucket, intake

    customer = code if _no_customer(project) else project.customer
    if named is not None and named != customer:
        raise XchgError(hold_line(code))
    f = next((x for x in _project_files(ctx, code) if _file_id("project", x["key"], x["etag"]) == ident), None)
    if f is None:
        return {"ok": True, "state": "absent"}
    if f["size"] > ctx.max_bytes():
        raise XchgError("the object is larger than the limit of keys.conf (max_mb)")
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
    if own is None or (folder and folder != own) or (named and named != own):
        raise XchgError(hold_line(code))
    return own


def hold_line(code: str) -> str:
    """The one line of a hold: what the owner runs, in his own terminal (the work rules hand out nothing else)."""
    return "held: run awb import %s as the owner in your own terminal" % code


def _hold(ctx: Context, code: str, customer: str, why: str) -> dict:
    ctx.notify(*exchange_mail("held", code, customer=customer, why=why))
    return {"ok": True, "state": "held", "why": HOLD_REASONS[why], "customer": customer}


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


def name_problems(name: str, register: Path | None = None) -> list[str]:
    """Why an object name must not cross: a hit of the name check in the name itself (the --as name or the
    source name), whatever kind of file it names."""
    from awb import check

    try:
        hits = check.check_text(name, register if register is not None else config.paths().register)
    except Exception as err:
        return ["the name check cannot run (%s)" % type(err).__name__]
    if hits:
        classes = sorted({h.get("cls", "unknown") for h in hits if isinstance(h, dict)})
        return ["the name check found %d hit(s) in the object name: %s; put it --as a plain name" % (
            len(hits), ", ".join(classes))]
    return []


def check_problems(path: Path, image: bool = False, register: Path | None = None) -> list[str]:
    """Why a file must not cross: a name check hit, a file that cannot be read as text."""
    from awb import check

    suffix = path.suffix.lower()
    if suffix in IMAGE_SUFFIXES:
        return [] if image else ["a picture crosses only with --image and a reason"]
    try:
        hits = check.check_file(path, register if register is not None else config.paths().register, code=True)
    except Exception as err:
        return ["the name check cannot run (%s)" % type(err).__name__]
    classes = sorted({h.get("cls", "unknown") for h in hits if isinstance(h, dict)})
    if hits:
        return ["the name check found %d hit(s): %s" % (len(hits), ", ".join(classes))]
    return []


def _size(n: int) -> str:
    if n < 1024:
        return "%d bytes" % n
    return "%.0f KB" % (n / 1024) if n < 1024 * 1024 else "%.1f MB" % (n / 1024 / 1024)


def describe(e: dict) -> str:
    """One inbox file for the session and for him: id, inbox, kind, size, time and, for the lab inbox, the name."""
    when = (e.get("modified") or "")[:16].replace("T", " ")
    where = {"lab": "lab inbox", "project": "folder in/"}.get(e["bucket"], "owner inbox")
    if e["bucket"] in ("owner", "project"):
        name = "name not shown"
    else:
        name = e.get("name") or "name withheld: the name check found a hit"
    return "%s  %-11s  %-4s  %8s  %s  %s" % (e["id"], where, e["kind"], _size(e["size"]),
                                            (when + " UTC") if when else "time unknown", name)


def _take_one(root: Path, code: str, e: dict, customer: str | None) -> str:
    from awb import projects

    what = "%s of %s" % (e["kind"], _size(e["size"]))
    if e["bucket"] in ("owner", "project"):
        whence = "the owner inbox" if e["bucket"] == "owner" else "the project's folder in/"
        a = _ok(_call({"op": "take_owner", "id": e["id"], "project": code, "customer": customer}))
        if a.get("state") == "held":
            return "%s from %s: held on the owner's side (%s); the owner has been told. %s" % (
                what, whence, a.get("why", "held"), hold_line(code))
        if a.get("state") != "taken":
            raise XchgError("the file left %s before the take" % whence)
        cust = a.get("customer")
        if not (isinstance(cust, str) and (_CUST_RE.match(cust) or cust == code)):
            raise XchgError("the key service named no customer code for the copies")
        outbox = config.paths().outbox / cust
        moved = []
        for out in a.get("outputs", []):
            src = outbox / out
            if src.is_file():
                shutil.move(str(src), str(root / "input" / out))
                moved.append(out)
        return "%s taken from %s through the intake: %d sanitised copy(ies) in input/ (%s)" % (
            what, whence, len(moved), ", ".join(moved) or "-")
    rel = e.get("name")
    if not rel:
        return "%s in the lab inbox: held, its name holds a hit of the name check; the owner renames it or moves " \
               "it to the owner inbox" % what
    name = rel.rsplit("/", 1)[-1]
    for where in ("input", projects.OPAQUE_DIR):
        if (root / where / name).exists():
            raise XchgError("%s/ already holds a file of that name" % where)
    with tempfile.TemporaryDirectory(prefix="awb-take-") as tmp:
        local = Path(tmp) / name
        # the key service checks the file and moves it in the bucket; a held file never reaches this process (F6)
        a = _ok(_call({"op": "take_lab", "id": e["id"], "project": code}, download=local))
        if a.get("state") == "held":
            return "held in the lab inbox by the key service: %s; the owner moves it to the owner inbox or releases " \
                   "it" % "; ".join(a.get("problems") or ["held"])
        if a.get("state") != "taken":
            raise XchgError("the file left the lab inbox before the take")
        # a file the commit gate cannot read (a pdf, a sheet, an archive) would block every commit that stages
        # it: it goes to input/opaque/, which .gitignore keeps out of git
        opaque = bool(a.get("opaque"))
        dest = root / (projects.OPAQUE_DIR if opaque else "input") / name
        if opaque:
            _ignore_opaque(root)
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(local, dest)
    if opaque:
        return ("taken from the lab inbox: %s/%s, a copy in the project's in/ folder; the commit gate cannot read "
                "it, so .gitignore keeps it out of git" % (projects.OPAQUE_DIR, name))
    return "taken from the lab inbox: input/%s, a copy in the project's in/ folder" % name


def _ignore_opaque(root: Path) -> None:
    """input/opaque/ in the project's .gitignore before anything lands there (a project spawned before TM0 has
    none)."""
    from awb import projects

    f = root / ".gitignore"
    try:
        text = f.read_text(encoding="utf-8")
    except FileNotFoundError:
        f.write_text(projects.GITIGNORE, encoding="utf-8")
        return
    if projects.OPAQUE_DIR + "/" not in text.splitlines():
        with open(f, "a", encoding="utf-8") as fh:
            fh.write(("" if text.endswith("\n") or not text else "\n") + projects.OPAQUE_DIR + "/\n")


def take(words: str | None = None, project: str | None = None, customer: str | None = None,
         ids: list[str] | None = None, take_all: bool = False) -> tuple[str, bool]:
    """The files he means, taken one by one. Returns the lines for him and whether every take went through."""
    root, code, _ = _project()
    code = project or code
    if ids:
        picked = []
        for ident in ids:
            a = _ok(_call({"op": "inbox_find", "id": ident, "project": code}))
            if not a.get("matches"):
                raise XchgError("%s is in neither inbox any more, or the file changed: run awb inbox list" % ident)
            picked += [m for m in a["matches"] if m["id"] not in {p["id"] for p in picked}]
    elif take_all:
        picked = _ok(_call({"op": "inbox_find", "all": True, "project": code})).get("matches", [])
        if not picked:
            raise XchgError("both inboxes are empty: the file is in neither inbox")
    else:
        if not (words and words.strip()):
            raise XchgError("say which file: his words for it, --all, or --id from awb inbox list")
        a = _ok(_call({"op": "inbox_find", "words": words, "project": code}))
        picked = a.get("matches", [])
        everything = "\n".join("  " + describe(e) for e in a.get("files", [])) or "  both inboxes are empty"
        if not picked and from_in(words):
            raise XchgError("the folder in/ of %s holds no new file" % code)
        if not picked:
            raise XchgError("the file is in neither inbox under these words. What the inboxes hold:\n%s\nPick the "
                            "one he described and take it with --id ID, or ask him." % everything)
        if len(picked) > 1:
            head = ("the file is in both inboxes" if {e["bucket"] for e in picked} == {"lab", "owner"}
                    else "%d files match" % len(picked))
            raise XchgError("%s: ask him which one he means, then take it with --id ID:\n%s" % (
                head, "\n".join("  " + describe(e) for e in picked)))
    if len(picked) == 1:
        return _take_one(root, code, picked[0], customer), True
    lines, ok = [], True
    for e in picked:
        try:
            lines.append(_take_one(root, code, e, customer))
        except XchgError as err:
            lines.append("%s not taken: %s" % (e["id"], err))
            ok = False
    return "\n".join(lines), ok


def inbox_list() -> list[str]:
    _, code, _ = _project()
    a = _ok(_call({"op": "inbox_find", "project": code}))
    files = a.get("files", [])
    lines = [describe(e) for e in files]
    withheld = sum(1 for e in files if e["bucket"] == "lab" and not e.get("name"))
    if withheld:
        lines.append("%d file(s) withheld: their names hold a hit of the name check" % withheld)
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
    problems = name_problems(name) or check_problems(path, image=image)
    if problems:
        raise XchgError("; ".join(problems))
    if customer not in (projects.NO_CUSTOMER, "", None):
        verdict, message = review.send_check(root, path, True, name=name)
        if verdict == review.SEND_REFUSE:
            raise XchgError("the send gate refused the file: %s" % message)
    with tempfile.TemporaryDirectory(prefix="awb-put-") as tmp:
        copy = Path(tmp) / "upload"
        shutil.copyfile(path, copy)
        ident = put_id(hashlib.sha256(copy.read_bytes()).hexdigest(), name)
        key = "%s/%s%s/%s" % (code, FROM, datetime.date.today().isoformat(), ident)
        req = {"op": "obs", "method": "PUT", "key": key, "project": code, "name": name}
        if image:
            req.update(image=True, reason=reason)
        a = _ok(_call(req, upload=copy))
    if image:
        _log_image(root, ident, reason)
    return "put: %s as %s (%d bytes); the owner gets a mail" % (name, key, a.get("size", 0))


def _log_image(root: Path, ident: str, reason: str) -> None:
    with open(root / "evidence" / "images-put.log", "a", encoding="utf-8") as f:
        f.write("%s\t%s\t%s\n" % (datetime.datetime.now().isoformat(timespec="seconds"), ident,
                                  " ".join(reason.split())))


def put_list() -> list[str]:
    _, code, _ = _project()
    a = _ok(_call({"op": "obs", "method": "LIST", "prefix": "%s/%s" % (code, FROM), "project": code}))
    return ["%s  %d bytes" % (o["name"], o["size"]) for o in a.get("objects", [])]


def clean(code: str, older_than: str | None = None, go: bool = False) -> list[str]:
    """The objects under <code>/from-session/ of the lab bucket, those of a date folder before `older_than` only
    when it is given; deleted through the key service with `go` (each one a row of state deleted in
    RESOURCES.md), else only listed. The service refuses every key outside the project's own folders."""
    root, own, _ = _project()
    if code != own:
        raise XchgError("clean names the code of this project (SCOPE.md), not another one")
    if older_than is not None:
        try:
            datetime.date.fromisoformat(older_than)
        except ValueError:
            raise XchgError("--older-than takes a date as YYYY-MM-DD") from None
    prefix = "%s/%s" % (code, FROM)
    objs = _ok(_call({"op": "obs", "method": "LIST", "prefix": prefix, "project": code})).get("objects", [])
    picked = []
    for o in objs:
        folder = o["name"].split("/", 1)[0]
        if older_than is not None:
            try:
                if datetime.date.fromisoformat(folder).isoformat() >= older_than:
                    continue
            except ValueError:
                continue            # not under a date folder: its age is unknown, an age filter keeps it
        picked.append(o)
    if not picked:
        return ["nothing to delete under %s" % prefix]
    if not go:
        return ["would delete %s%s (%d bytes)" % (prefix, o["name"], o["size"]) for o in picked] +             ["%d object(s); run again with --go to delete them" % len(picked)]
    lines, rows = [], []
    today = datetime.date.today().isoformat()
    try:
        for o in picked:
            key = prefix + o["name"]
            _ok(_call({"op": "obs", "method": "DELETE", "key": key, "project": code}))
            lines.append("deleted %s" % key)
            rows.append("| from-session | %s | obs object, lab bucket | eu-de | small | none | deleted | awb xchg "
                        "clean %s |\n" % (key.replace("|", "\\|"), today))
    finally:
        # a delete that fails half way still leaves a row for every object already gone
        if rows:
            with open(root / "RESOURCES.md", "a", encoding="utf-8") as f:
                f.writelines(rows)
    lines.append("%d object(s) deleted, %d row(s) in RESOURCES.md" % (len(picked), len(rows)))
    return lines


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
    t = sub.add_parser("take", help="take the file he describes from either inbox into this project")
    t.add_argument("words", nargs="*", help="his words for the file: what it is or a part of its name; | between "
                                            "several ways of saying it")
    t.add_argument("--id", dest="ids", action="append", default=[], help="a file awb inbox list showed (repeat)")
    t.add_argument("--all", dest="take_all", action="store_true", help="every file of both inboxes")
    t.add_argument("--project", default=None)
    t.add_argument("--customer", default=None, help="the customer code; it only has to agree with the project's "
                                                    "customer")
    sub.add_parser("list", help="both inboxes: id, kind, size and time")
    try:
        args = ap.parse_args(argv)
    except SystemExit as exc:
        return 0 if exc.code == 0 else 2
    if not args.command:
        ap.print_help()
        return 2
    try:
        if args.command == "take":
            text, ok = take(" ".join(args.words), args.project, args.customer, args.ids, args.take_all)
            print(text)
            return 0 if ok else 1
        for line in inbox_list() or ["both inboxes are empty"]:
            print(line)
        return 0
    except XchgError as err:
        text = str(err)
        print(text if text.startswith("held: ") else "awb inbox: %s" % text, file=sys.stderr)
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
    cl = sub.add_parser("clean", help="delete what this project put into <code>/from-session/")
    cl.add_argument("code")
    cl.add_argument("--older-than", default=None, help="only date folders before this date (YYYY-MM-DD)")
    mode = cl.add_mutually_exclusive_group(required=True)
    mode.add_argument("--dry-run", action="store_true", help="list what would be deleted")
    mode.add_argument("--go", action="store_true", help="delete and record the rows in RESOURCES.md")
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
        elif args.command == "clean":
            for line in clean(args.code, args.older_than, args.go):
                print(line)
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
