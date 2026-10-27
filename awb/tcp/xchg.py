"""The exchange between the owner and the sessions through OBS (T-102): two inboxes and a from-session folder.

    awb inbox take WORDS... [--project CODE] [--customer CUST-XXXX]   the file the owner describes, from an inbox
                                                                      or the project's own folder in/
    awb inbox take --all | --id ID...                                 every file of both inboxes, or the ones named
    awb inbox list                                                    both inboxes: id, kind, size, time
    awb xchg put FILE [--as NAME] [--image --reason TEXT]         a result into <code>/from-session/<date>/<id>
                  [--internal-image]                              owner only: an HTML or Markdown file with an image
    awb xchg list                                                 what this project put
    awb xchg clean CODE [--older-than DATE] --dry-run|--go        delete what this project put, rows in RESOURCES.md

The owner drops a file into one of two inboxes in the OBS console, with no project code and no command:

    <lab bucket>/inbox/     material without customer content (vendor images, test data, public documents)
    <owner bucket>/inbox/   anything that may hold customer material; only the owner's IAM user reaches it

The owner never has to know a file's name: he says what it is ("the pdf", "the excel with the sizing", "the newest",
"all files") and the session passes his words. The key service matches them against the files of both inboxes: kind
words in English, German or Russian (pdf, excel, Tabelle, презентация) and a word for the newest file meet every
file; the exact name, the same letters (case, dashes and the extension aside) and the other words meet only a name
the session may see (a file of the lab inbox the name check passes), so no answer tells whether a hidden name holds
a word; several ways of saying it go in one call, separated by |. One file found: it is taken. None or several: the
session gets the list of both inboxes (an id, the kind, the size and the time, the name only for a file of the lab
inbox the name check passes, never for one of the owner inbox), picks by his description or asks him, and takes by
id. From the lab inbox the key service fetches it and runs the name check on it in its own process (F6); a file that
passes is copied to `<lab bucket>/<code>/in/`, removed from the inbox and handed to the session for `input/`, a file
with a hit or one that cannot be checked stays in the inbox and the session gets no byte of it. From the owner inbox
the session never sees the original: the key service (the owner's process) moves it into the project's folder of the
owner bucket, runs `awb intake` for the customer and hands the session only the sanitised copies; a file the intake
cannot read is held and the owner is told. The customer of an owner-inbox take is the project's own as the owner's
grant records it (a project he never granted has none); a folder `inbox/CUST-XXXX/` the owner drops a file into has
to be that customer's (F5), and a session's --customer only has to agree with it. A project without a customer takes
nothing from the owner inbox: its material comes from its own folder `in/` of the owner bucket (spawn makes it, op
owner_folder; the first take makes it when spawn could not), whose new files a find lists beside the two inboxes for
every project; the key service runs the same intake for them, for the project's customer or, without one, under the
project code with no customer code. "take the files from in" takes every new file of `in/`. Every refusal of a take
is the one line of the work rules (`hold_line`). Results go back
with `awb xchg put` into `<lab bucket>/<code>/from-session/<date>/`: never refused for a review state, a name or
structured data (task GT), its secrets replaced with [secret], again by the key service on the bytes it received (F6). The owner gets a mail through the SMN topic of the settings
when a session puts a file and when a take holds one.

The service side (`serve_obs`, `serve_take_lab`, `serve_take_owner`, `serve_owner_has`; awb/tcp/keys_exchange.py
since F1 step 7, still reached through this module) runs inside the key service and is refused everything outside
the allowed prefixes before anything is signed: `inbox/` of the lab bucket (head and list; a get, a copy and a delete
only through `serve_take_lab`), `<code>/in/` and `<code>/from-session/` of an active project code. A put goes only to
`<code>/from-session/<date>/<file id>`, is authorised before its bytes are received (F8), meets the picture rules and
has its secrets replaced by the service. Never a policy, an ACL or another bucket.

The owner's console puts a file into a project's `in/` under an id (`serve_web_put_in`, the owner's processes only);
the session's take is the one path from there.

The web mode (`serve_web_read`) is the owner's materials service of the web console reading both buckets: a copy,
never a move. It lists `inbox/` of either bucket and `<YYYY-MM>/<code>/in/` of an active project in the owner
bucket, names the month folders of the owner bucket and reads one object at the version (ETag) its listing showed;
another version fails as changed. It never writes, deletes or copies, and only the owner's own processes reach it. The owner's settings live in `~/.config/awb/keys.conf` and travel with `awb keys unlock`:

    bucket_tenant = <alias whose lab key reaches both buckets>
    lab_bucket = awb-lab-eu-de        owner_bucket = awb        region = eu-de
    max_mb = 100                      notify_topic = <urn of an SMN topic>
    readonly = <alias>:key|login:<domain id>:<region>[+<region>], ...   the read-only tenants (awb/tcp/readonly.py)
"""
from __future__ import annotations

import datetime
import hashlib
import os
import re
import shutil
import sys
import tempfile
import unicodedata
from pathlib import Path

from awb import bucket_layout, codes, config

INBOX = "inbox/"
FROM = "from-session/"
IN = bucket_layout.IN
DEFAULTS = {"lab_bucket": "awb-lab-eu-de", "owner_bucket": bucket_layout.OWNER_BUCKET,
            "region": bucket_layout.REGION, "max_mb": str(bucket_layout.MAX_MB)}
SETTING_KEYS = ("bucket_tenant", "lab_bucket", "owner_bucket", "region", "max_mb", "notify_topic", "tf_services",
                "lab_writes", "readonly")
_SERVICE_RE = re.compile(r"^[a-z][a-z0-9]{1,20}$")
"""One service of tf_services: the short name of the lease policy's actions (ecs, evs, vpc)."""
_NAME_RE = re.compile(r"^[^/\\\x00-\x1f\x7f]{1,200}$")
_CODE_RE = re.compile("^%s$" % codes.TCP_PROJECT_WIDE)
_CUST_RE = re.compile("^%s$" % codes.CUST)
DIFFERS_LINE = "%s: its row in projects.tsv names another customer than the owner's grant: refused"
"""The refusal of a row changed after the grant (the residual of F5 and F6): the project code, never a name."""
STALE_LINE = ("%s: the owner's grant records no customer: he runs awb projects kind %s %s in his own terminal "
              "once more")
"""The refusal of a customer row under a grant of before 2026-10-10: the owner's one-time re-run records it."""
TEXT_SUFFIXES = bucket_layout.TEXT_SUFFIXES
IMAGE_SUFFIXES = bucket_layout.IMAGE_SUFFIXES


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
        if key == "tf_services" and not tf_services_ok(value):
            raise XchgError("line %d of keys.conf: tf_services is a comma-separated list of service names, iam never"
                            % n)
        if key == "lab_writes" and lab_writes_parse(value) is None and not lab_writes_old_form(value):
            raise XchgError("line %d of keys.conf: lab_writes is a comma-separated list of METHOD service path, "
                            "a write method, a lower case service (iam never) and a path that starts with /, has no "
                            ".. and whole {name} segments; awb keys lab-writes --from-recipes prints it" % n)
        if key == "readonly":
            from awb.tcp import readonly

            if readonly.parse(value) is None:
                raise XchgError("line %d of keys.conf: readonly is a comma-separated list of "
                                "ALIAS:key|login:DOMAIN-ID:REGION[+REGION]" % n)
        out[key] = value
    return out


LAB_WRITE_METHODS = ("POST", "PUT", "PATCH", "DELETE")
"""The methods of a lab_writes triple: the write methods of the key service (keys.WRITE_METHODS)."""
_LAB_PATH_RE = re.compile(r"^/[A-Za-z0-9_{}.:/-]{0,500}$")
_LAB_SEGMENT_RE = re.compile(r"^(?:[A-Za-z0-9_.:-]+|\{[a-z_][a-z0-9_]{0,40}\})$")
"""One segment of a lab_writes path: plain, or a whole {name} that stands for exactly one segment."""
_LAB_QUERY_RE = re.compile(r"^[a-z][a-z0-9_-]{0,40}$")


def lab_writes_parse(value: str) -> list[tuple[str, str, str]] | None:
    """The lab_writes of keys.conf (K9 of the review of 2026-10-20): `METHOD service path` triples separated by
    commas, each matched whole. None when the line is not a list of valid triples (an empty list too). The method
    is a write method, the service lower case and never iam, the path starts with /, has no .., no // and no empty
    segment, a segment is plain or a whole {name}; the path may end in ?key or ?key&key, the query keys a write
    carries (without ? it carries none)."""
    out = []
    for item in value.split(","):
        parts = item.split()
        if len(parts) != 3:
            return None
        method, service, full = parts
        path, sep, query = full.partition("?")
        if method not in LAB_WRITE_METHODS or not _SERVICE_RE.match(service) or service == "iam" or \
                not _LAB_PATH_RE.match(path) or ".." in path or "//" in path:
            return None
        if path != "/" and not all(_LAB_SEGMENT_RE.match(s) for s in path[1:].split("/")):
            return None
        if sep and not all(_LAB_QUERY_RE.match(k) for k in query.split("&")):
            return None
        out.append((method, service, full))
    return out or None


def lab_writes_old_form(value: str) -> bool:
    """True when the line is the old service:path-prefix form of before K9: the unlock goes on (a deploy must not
    take TCP from every session), the key service reports it in one line and signs no lab write."""
    items = [item.strip() for item in value.split(",")]
    for item in items:
        service, sep, prefix = item.partition(":")
        if not sep or not _SERVICE_RE.match(service) or service == "iam" or not _LAB_PATH_RE.match(prefix) or \
                ".." in prefix or "//" in prefix:
            return False
    return bool(items)


def lab_write_matches(triple: tuple[str, str, str], method: str, service: str, path: str, query) -> bool:
    """True when the write `method service path` with the query object `query` is the triple: the same method,
    the same service, the same number of segments, each plain segment equal and each {name} one non-empty segment
    (never a prefix), and the query keys exactly the ones the triple names."""
    t_method, t_service, full = triple
    t_path, _sep, t_query = full.partition("?")
    if method != t_method or service != t_service or not isinstance(path, str):
        return False
    if query is None:
        query = {}
    if not isinstance(query, dict) or set(query) != (set(t_query.split("&")) if t_query else set()):
        return False
    want, got = t_path.split("/"), path.split("/")
    if len(want) != len(got):
        return False
    for w, g in zip(want, got):
        if w.startswith("{"):
            if not g:
                return False
        elif w != g:
            return False
    return True


def tf_services_ok(value: str) -> bool:
    """The tf_services of keys.conf: service names in lower case separated by commas, empty for none, never iam
    (finding 16 of the review of 2026-10-10: the owner narrows the lease with it)."""
    names = [s.strip() for s in value.split(",")] if value.strip() else []
    return all(_SERVICE_RE.match(s) and s != "iam" for s in names)


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


def match(words: str, rels: list[str], modified: list[str] | None = None, visible: set[int] | None = None) -> list[int]:
    """The indexes of the files `words` means, `rels` being their names under inbox/. The exact name wins, then the
    same letters and digits, then each alternative of `words` (separated by |): its kind words pick the type, a word
    for the newest keeps the newest file, every other word must be inside the name. `visible` (all when None) are
    the indexes whose names the session may see: only they meet a name, the same letters or a word; a hidden name
    meets kind words and the newest alone, which the listing shows anyway, so no answer tells whether a hidden name
    holds a word (finding 2 of the review of 2026-10-10)."""
    seen = set(range(len(rels))) if visible is None else set(visible)
    wanted = words.strip()
    exact = [i for i, r in enumerate(rels) if i in seen and wanted in (r, r.rsplit("/", 1)[-1])]
    if exact:
        return exact
    folded = _fold(wanted)
    same = [i for i, r in enumerate(rels) if i in seen and folded and folded in _forms(r)]
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
        hits = [i for i, r in enumerate(rels) if (not kinds or suffix(r) in kinds)
                and (not rest or (i in seen and all(t in _fold(r) for t in rest)))]
        if newest and len(hits) > 1 and modified:
            last = max(modified[i] for i in hits)
            hits = [i for i in hits if modified[i] == last]
        found += [i for i in hits if i not in found]
    return found


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


_PICTURE_RE = re.compile(rb"data:image/|<img[\s/>]|!\[[^\]]*\]\s*[(\[]", re.I)


def _embeds_picture(path: Path) -> bool:
    """A page or a text that embeds a picture as a data URI, an img tag or a Markdown image (`awb howto render`,
    `awb howto draft --internal`) or links one."""
    tail = b""
    try:
        with open(path, "rb") as fh:
            while chunk := fh.read(1 << 20):
                if _PICTURE_RE.search(tail + chunk):
                    return True
                tail = chunk[-64:]
    except OSError:
        return False
    return False


def check_problems(path: Path, image: bool = False, register: Path | None = None,
                   mirror_image: bool = False, names: bool = True) -> list[str]:
    """Why a file must not cross: a name check hit, a file that cannot be read as text, an HTML or Markdown file that
    embeds or links an image unless the owner passed `mirror_image` (--internal-image). A put passes `names` False
    (task GT): the picture rules alone."""
    from awb import check

    suffix = path.suffix.lower()
    if suffix in IMAGE_SUFFIXES:
        return [] if image else ["a picture crosses only with --image and a reason"]
    if suffix in (".html", ".htm", ".md", ".markdown") and not mirror_image and _embeds_picture(path):
        return ["a page or text that embeds or links an image never crosses; console images stay internal "
                "(only the owner passes --internal-image)"]
    if not names:
        return []
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
            cls = a.get("class") if a.get("class") in ("unreadable",) else None
            return "%s from %s: held on the owner's side (%s); the owner has been told. %s" % (
                what, whence, a.get("why", "held"), hold_line(code, cls))
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


def put(path: Path, as_name: str | None = None, image: bool = False, reason: str | None = None,
        kind_word: str | None = None, lang: str | None = None, mirror_image: bool = False) -> str:
    from awb import putcopy

    if mirror_image and (config.is_work_user() or os.environ.get("CLAUDECODE")
                         or os.environ.get("CLAUDE_CODE_ENTRYPOINT")):
        raise XchgError("--internal-image is for the owner's own terminal, never a session")
    root, code, _ = _project()
    path = Path(os.path.realpath(path))
    if not path.is_file():
        raise XchgError("the file to put is not there")
    name = as_name or path.name
    if not valid_name(name):
        raise XchgError("a name without a folder")
    if image and not (reason and len(reason.strip()) >= 10):
        raise XchgError("--image needs a reason of one line: what it shows and that you checked it for names")
    # task GT: no name check, no structured data, no review state; only the picture rule and the secrets
    problems = check_problems(path, image=image, mirror_image=mirror_image, names=False)
    if problems:
        raise XchgError("; ".join(problems))
    line = putcopy.review_line(root, path)
    with tempfile.TemporaryDirectory(prefix="awb-put-") as tmp:
        copy = Path(tmp) / "upload"
        secrets = putcopy.scrub(path, copy)
        ident = put_id(hashlib.sha256(copy.read_bytes()).hexdigest(), name)
        key = "%s/%s%s/%s" % (code, FROM, datetime.date.today().isoformat(), ident)
        req = {"op": "obs", "method": "PUT", "key": key, "project": code, "name": name}
        if image:
            req.update(image=True, reason=reason)
        a = _ok(_call(req, upload=copy))
    if image:
        _log_image(root, ident, reason)
    secrets += int(a.get("secrets", 0) or 0)
    lines = ["put: %s as %s (%d bytes); the owner gets a mail" % (name, key, a.get("size", 0))]
    page = a.get("page")
    if isinstance(page, str) and put_key_ok(page, code):
        lines.append("put: the same text as HTML as %s (open it, select all, paste into the mail)" % page)
    print(putcopy.put_line(line, secrets), file=sys.stderr)      # beside the lines of the objects
    lines.append(get_it_line(a.get("bucket"), key))
    return "\n".join(lines)


def get_it_line(bucket, key: str) -> str:
    """Where a put file is and how to get it, in one line (his rule of 2026-10-27: no link to a file). The laptop's
    awb clean fetch reads out/ of the owner bucket only, never from-session/ of the lab bucket: Total Commander."""
    name = bucket if isinstance(bucket, str) and re.fullmatch(r"[a-z0-9][a-z0-9.-]{1,62}", bucket) else "the lab bucket"
    return ("get it: Total Commander, bucket %s, folder %s (awb clean fetch on the laptop reads only out/ of the "
            "owner bucket)" % (name, key.rsplit("/", 1)[0] + "/"))


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
        print(text if codes.HOLD_LINE_RE.match(text) else "awb inbox: %s" % text, file=sys.stderr)
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
    pt.add_argument("--internal-image", dest="mirror_image", action="store_true",
                    help="owner only: let an HTML or Markdown file with an embedded or linked image cross")
    pt.add_argument("--kind", dest="kind_word", default=None, metavar="WORD",
                    help="the kind word of the neutral name proposed when the file name is refused (default doc)")
    pt.add_argument("--lang", default=None, metavar="XX",
                    help="the language of the neutral name proposed when the file name is refused (default: read "
                         "from the text)")
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
            print(put(Path(args.file), args.as_name, args.image, args.reason, args.kind_word, args.lang, args.mirror_image))
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

# The service side, awb/tcp/keys_exchange.py (F1 step 7): its names are reached through this module (awb/parts.py),
# the session side's from_in, hold_line and put_id too.
from awb.tcp import keys_exchange  # noqa: E402,F401
