"""The owner's bucket: one working folder per project, `<YYYY-MM>/<project-code>/` with `in/` for his originals
and `out/` for results (his decisions of 2026-09-24).

    awb bucket sync                         the folders of every active project; folders that match no project
    awb bucket folder CODE                  the folder of one project, created when missing
    awb bucket pull CODE [--again]          new files of in/ into the vault inbox, then the intake of its customer
    awb bucket put CODE FILE [--replace] [--reveal]   FILE into out/; --reveal puts the real names back first;
                                            a Markdown FILE goes as FILE.md and FILE.html (paste into a mail);
                                            it ends with the line "get it: ..." (Total Commander or the laptop)
    awb bucket move <YYYY-MM>/<NAME>/ CODE  a folder whose name drifted to <YYYY-MM>/<CODE>/: copy, check, delete

A folder is looked for under every month and used where it is. A new one goes under the month the project was
created. The project code is the fixed point: a drifted folder is renamed in the bucket, never the project.

Owner side only. The bucket takes the owner's IAM user alone, and that key stays in the key service: every call
goes through its admin socket (`awb keys unlock` loads it; F1 step 11). There is no bucket key in the environment or
in the host file. A working session must never see an original before the intake: as the work user of a sealed host
every command refuses, and inside an assistant session of the owner's own uid `pull` and `move` refuse and the
client gives no file of `in/` (review R6: the key service cannot tell the peer's session, so the check stays on the
client as it does for `awb import`, `awb fetch` and `awb vault show`). `pull` and `put --reveal` read the vault, so
he runs them in his own shell.

The key service takes list, get and put, inside the folders of the layout (awb/bucket_layout.py), and one delete:
`drop_in`, a file of in/ of an active project after `awb import --auto` took it into the vault. `move` needs a copy
and a delete and stops with a line saying so. A locked or stopped key service stops every command with one
line that names `awb keys unlock`. Output carries codes, months, counts and sizes, never the name of a file of in/
and never the name of a folder that matches no project.
"""
from __future__ import annotations

import html
import json
import mimetypes
import os
import re
import socket
import sys
import tempfile
from collections import Counter
from datetime import date
from pathlib import Path

from awb import config, obs
from awb.bucket_layout import IN, KEEP, KEEP_TEXT, MONTH, OUT, OWNER_BUCKET, REGION, folder_of, is_keep

PULLED = "bucket-pulled.tsv"
ORIGINALS = ("pull", "move")
"""The commands that reach the originals of in/ (`move` carries a whole folder): the owner's own terminal only.
The fetch of `awb import` is refused by `awb import` itself, and the client gives no file of in/ to a session."""
_MONTH_RE = re.compile(MONTH + "/")
_UNSAFE_RE = re.compile(r"[\x00-\x1f/\\\x7f]")
_IN_SEGMENT = IN.rstrip("/")


class BucketError(Exception):
    """A bucket step cannot be done. The message carries codes, months and counts only."""


# --------------------------------------------------------------------------- settings and client


ADMIN_SOCKET_ENV = "AWB_KEYS_ADMIN"
TIMEOUT = 190
MAX_ANSWER = 8 * 1024 * 1024
LOCKED_LINE = "the key service is locked or not running: the owner runs awb keys unlock"


def settings() -> dict[str, str | None]:
    if config.is_work_user():
        raise BucketError("the bucket is the owner's: run awb bucket in your own shell")
    return {"bucket": OWNER_BUCKET, "region": REGION}


def owner_terminal(what: str) -> None:
    """`pull` and `move` and a get of a file of in/ reach the originals: refused inside an assistant session, as
    `awb import` and `awb fetch` are (review R6). The uid alone is not enough: the steering session runs as the
    owner."""
    from awb import vault

    if vault.in_assistant_session():
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
        """One request line on the admin socket, the bytes of a put after it and the bytes of a get after the answer
        (the wire of awb.tcp.keys.request, written out here: the core imports no awb.tcp)."""
        sock = self.sock or Path(os.environ.get(ADMIN_SOCKET_ENV) or config.paths().vault / "keys-admin.sock")
        if upload is not None:
            req = dict(req, length=upload.stat().st_size)
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.settimeout(TIMEOUT)
        try:
            s.connect(str(sock))
            s.sendall(json.dumps(req).encode("utf-8") + b"\n")
            if upload is not None:
                try:
                    with open(upload, "rb") as fh:
                        while chunk := fh.read(1 << 16):
                            s.sendall(chunk)
                except (BrokenPipeError, ConnectionResetError):
                    pass            # the service refused before the bytes were all sent: its answer is waiting
            buf = bytearray()
            while b"\n" not in buf:
                chunk = s.recv(1 << 16)
                if not chunk:
                    break
                buf += chunk
                if len(buf) > MAX_ANSWER and b"\n" not in buf:
                    raise BucketError("the answer of the key service is too large")
            head, _, rest = bytes(buf).partition(b"\n")
            try:
                answer = json.loads(head)
            except ValueError:
                raise BucketError("the key service gave no readable answer") from None
            length = answer.get("length") if isinstance(answer, dict) else None
            if download is not None and isinstance(length, int):
                with open(download, "wb") as out:
                    out.write(rest[:length])
                    got = min(len(rest), length)
                    while got < length:
                        chunk = s.recv(min(1 << 16, length - got))
                        if not chunk:
                            raise BucketError("the file from the key service ended early")
                        out.write(chunk)
                        got += len(chunk)
        except (FileNotFoundError, ConnectionRefusedError, PermissionError):
            raise BucketError(LOCKED_LINE) from None
        except (OSError, socket.timeout) as err:
            raise BucketError("no answer from the key service (%s)" % type(err).__name__) from None
        finally:
            s.close()
        if not isinstance(answer, dict):
            raise BucketError("the key service gave no readable answer")
        if answer.get("ok"):
            return answer
        if answer.get("kind") == "locked" or "locked" in str(answer.get("error")):
            raise BucketError(LOCKED_LINE)
        if answer.get("kind") == "http":
            raise obs.OBSError(str(answer.get("error")), int(answer.get("status") or 0))
        raise BucketError("the key service: %s" % (answer.get("error") or "no answer"))

    def list(self, prefix: str = "", delimiter: str = "", page: int = 0) -> obs.Listing:
        a = self._ask({"op": "bucket_list", "prefix": prefix, "delimiter": delimiter})
        return obs.Listing([(k, int(n), e) for k, n, e in a["objects"]], list(a["prefixes"]), dict(a["modified"]))

    def head(self, key: str) -> dict | None:
        for k, size, etag in self.list(key).objects:
            if k == key:
                return {"ETag": '"%s"' % etag, "Content-Length": str(size)}
        return None

    def get(self, key: str, to: Path, if_match: str | None = None) -> Path:
        if _is_original(key):
            owner_terminal("a get of a file of in/")       # before the socket: an original never reaches a session
        self._ask({"op": "bucket_get", "key": key}, download=Path(to))
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
    raise BucketError("%s is not a registered project" % code)


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
    try:
        text = _pulled_file(p).read_text(encoding="utf-8")
    except FileNotFoundError:
        return set()
    return {tuple(line.split("\t")[:2]) for line in text.splitlines() if line.count("\t") >= 2}


def _remember(p: config.Paths, key: str, etag: str) -> None:
    f = _pulled_file(p)
    fd = os.open(f, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    with os.fdopen(fd, "a", encoding="utf-8") as out:
        out.write("%s\t%s\t%s\n" % (key, etag, date.today().isoformat()))


def _local_name(rel: str, folder: Path) -> str:
    base = _UNSAFE_RE.sub(" ", rel.rsplit("/", 1)[-1]).strip() or "file"
    if base.startswith("."):
        base = "_" + base
    stem, dot, suffix = base.rpartition(".")
    if not dot:
        stem, suffix = base, ""
    name, n = base, 1
    while (folder / name).exists():
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


def fetch(p: config.Paths, c: obs.Client, code: str, *, again: bool = False) -> dict:
    """The new files of in/ into the vault inbox, without the intake (`awb import` runs it): the counts and the
    downloaded files under "files"."""
    _project(p, code)
    folder = find_folder(c, code)
    if folder is None:
        raise BucketError("%s has no folder in the bucket; awb bucket folder %s creates it" % (code, code))
    objects = [o for o in c.list(folder).objects if not o[0].endswith("/") and not is_keep(o[0])]
    incoming = [o for o in objects if o[0].startswith(folder + IN)]
    outside = [o for o in objects if not o[0].startswith((folder + IN, folder + OUT))]
    done = set() if again else pulled(p)
    fresh = [o for o in incoming if (o[0], o[2]) not in done and "\t" not in o[0] and "\n" not in o[0]]
    result = {"new": len(fresh), "known": len(incoming) - len(fresh), "outside": len(outside), "intake": None,
              "files": [], "keys": []}
    if not fresh:
        return result
    config.ensure_layout(p)
    files = []
    for key, _, etag in fresh:
        dst = p.inbox / _local_name(key[len(folder) + len(IN):], p.inbox)
        c.get(key, dst)
        os.chmod(dst, 0o600)
        files.append(dst)
        result["keys"].append(key)
        _remember(p, key, etag)
    result["files"] = files
    return result


# --------------------------------------------------------------------------- put and move


def put(p: config.Paths, c: obs.Client, code: str, file: Path, *, replace: bool = False,
        reveal: bool = False, report: list | None = None) -> tuple[str, dict | None]:
    """`file` into out/ of the project's folder (created when missing). With `reveal` the real names are put back
    first, in a private temporary file on the vault side that is removed after the upload. A put never refuses a
    file for its review state, a name or structured data (task GT): a secret is replaced with [secret] in the
    uploaded copy and `report` receives the one line (`review: none` or `review: passed DATE`, the secrets
    replaced)."""
    project = _project(p, code)
    file = Path(os.path.realpath(file))
    if not file.is_file():
        raise BucketError("the file to put is not there")
    from awb import putcopy
    folder, _ = ensure_folder(c, project)
    key = folder + OUT + file.name
    page = html_key(key)
    if not replace and any(k and c.head(k) is not None for k in (key, page)):
        raise BucketError("out/ of %s already holds a file of that name; give --replace" % code)
    ctype = mimetypes.guess_type(file.name)[0] or "application/octet-stream"
    line = putcopy.review_line(Path(project.path), file)
    # what leaves is a private copy, its secrets replaced, never the path again
    tmp_root = config.make_dir(p.vault / "tmp", 0o700)
    with tempfile.TemporaryDirectory(dir=tmp_root) as tmp:
        copy = Path(tmp) / file.name
        secrets = putcopy.scrub(file, copy)
        os.chmod(copy, 0o600)
        if report is not None:
            report.append(putcopy.put_line(line, secrets))
        if not reveal:
            c.put_file(key, copy, ctype)
            _put_page(c, page, copy)
            return key, None
        from awb import reveal as _reveal
        try:
            text = copy.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            raise BucketError("--reveal takes a text file") from None
        named = Path(tmp) / ("named-" + file.name)
        stats = _reveal.write_named(text, named, p)
        c.put_file(key, named, ctype)
        _put_page(c, page, named)
    return key, stats


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


def move(p: config.Paths, c: obs.Client, old: str, code: str) -> dict:
    """Move a folder whose name drifted to `<its month>/<code>/`: copy every object inside the object storage,
    compare names and sizes, then delete the old objects. Nothing is deleted when the comparison fails."""
    _project(p, code)
    if not getattr(c, "can_copy", True):
        raise BucketError("the key service has no copy and no delete: move the folder in the object storage console")
    if not (_MONTH_RE.match(old or "") and old.endswith("/") and old.count("/") == 2):
        raise BucketError("give the folder as <YYYY-MM>/<name>/")
    target = folder_of(old[:7], code)
    if target == old:
        raise BucketError("the folder already carries the name of %s" % code)
    if find_folder(c, code) is not None:
        raise BucketError("%s already has a folder: bring the files together by hand" % code)
    objects = c.list(old).objects
    if not objects:
        raise BucketError("that folder is empty or not there")
    for key, _, _ in objects:
        c.copy(key, target + key[len(old):])
    want = {k[len(old):]: s for k, s, _ in objects}
    got = {k[len(target):]: s for k, s, _ in c.list(target).objects}
    if got != want:
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

    ap = SafeParser(prog="awb bucket", description="The owner's bucket: one folder per project, in/ and out/.")
    sub = ap.add_subparsers(dest="command", parser_class=SafeParser)
    sub.add_parser("sync", help="the folders of every active project; folders that match no project")
    s = sub.add_parser("folder", help="the folder of one project, created when missing")
    s.add_argument("code")
    s = sub.add_parser("pull", help="new files of in/ into the vault inbox, then the intake (your own terminal, "
                                    "never an assistant session)")
    s.add_argument("code")
    s.add_argument("--again", action="store_true", help="take files again that were downloaded before")
    s = sub.add_parser("put", help="a file into out/")
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
            key, stats = put(p, c, args.code, args.file, replace=args.replace, reveal=args.reveal, report=said)
            line = "%s: %s" % (args.code, key)
            if stats is not None:
                line += "  (%d codes replaced by names)" % stats["replaced"]
            print(line)
            if html_key(key):
                print("%s: %s  (the same text as HTML: open it, select all, paste into the mail)"
                      % (args.code, html_key(key)))
            for one in said:
                print(one, file=sys.stderr)          # beside the lines of the objects, never between them
            print(get_it_line(key, args.code))
            return 0
        if args.command == "move":
            r = move(p, c, args.folder, args.code)
            print("%s: %d objects moved to %s" % (args.code, r["moved"], r["target"]))
            return 0
    except (BucketError, obs.OBSError, projects.ProjectError, intake.IntakeError, register.RegisterError,
            _reveal.RevealError) as err:
        print("awb bucket: %s" % err, file=sys.stderr)
        return 2
    return 2


if __name__ == "__main__":
    sys.exit(main())
