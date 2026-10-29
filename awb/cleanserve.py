"""`awb clean serve [--port 8765]` (task LC5): the laptop flow of `awb clean` as one local page.

It listens on 127.0.0.1 inside WSL only; WSL forwards localhost to Windows, so the owner opens the printed URL
http://localhost:8765/?t=TOKEN in the Windows browser. The token is random per start (like Jupyter) and every
request must carry it (the query field `t`). A request whose Host header is not localhost or 127.0.0.1 (with or
without the port) is refused, against DNS rebinding.

The page: the project (prefilled with the current project of `awb clean use`, a button "use"), the files (uploaded
in a multipart form, or Windows or WSL paths typed one per line), the kind (auto: logs for a .log file, docs for any
other; or docs or logs), a button "run" that cleans into ~/work/tcp-xxxx/clean/ and shows the report (kinds and
counts only, the report file itself) with a link per clean copy that opens it as text, and a button "put" that sends
the copies of that run with the key of AWB_OBS_AK and AWB_OBS_SK in the environment of the serve process (without
them the page says so and how to load them). No marking of names, no editor, no review step.

Uploaded originals go into a temporary folder (mode 700) under the laptop's tmp/ and are deleted when the run ends,
also when it fails. Nothing logs a file content, a file name or the token: a request line is the method and the path.
Standard library only, no external asset.
"""
from __future__ import annotations

import email.parser
import email.policy
import hmac
import html
import os
import secrets
import shutil
import sys
import tempfile
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from awb import clean, laptop

DEFAULT_PORT = 8765
BIND = "127.0.0.1"
ALLOWED_HOSTS = ("localhost", "127.0.0.1")
MAX_BODY = 200 * 1024 * 1024
OBS_ENV_HINT = ("put needs AWB_OBS_AK and AWB_OBS_SK in the environment of awb clean serve: stop it (Ctrl-C), run "
                "set -a; . ~/awb-laptop/obs.env; set +a (the file holds the two lines AWB_OBS_AK=... and "
                "AWB_OBS_SK=..., mode 600) and start awb clean serve again")

CSS = """
:root { --bg: #f6f7f9; --fg: #1d2430; --muted: #5b6573; --line: #d5dae1; --accent: #b0136e; --box: #ffffff; }
@media (prefers-color-scheme: dark) {
  :root { --bg: #14181f; --fg: #e3e7ed; --muted: #9aa4b2; --line: #2c3440; --accent: #e0579f; --box: #1b2029; }
}
body { background: var(--bg); color: var(--fg); font: 15px/1.45 system-ui, sans-serif; margin: 0; padding: 16px; }
main { max-width: 860px; margin: 0 auto; }
h1 { font-size: 20px; margin: 0 0 12px; }
section { background: var(--box); border: 1px solid var(--line); border-radius: 6px; padding: 12px 14px;
          margin: 0 0 12px; }
h2 { font-size: 15px; margin: 0 0 8px; color: var(--muted); }
input[type=text], textarea, select { width: 100%; box-sizing: border-box; background: var(--bg); color: var(--fg);
  border: 1px solid var(--line); border-radius: 4px; padding: 6px; font: inherit; }
button { background: var(--accent); color: #fff; border: 0; border-radius: 4px; padding: 7px 14px; font: inherit;
         cursor: pointer; margin-top: 8px; }
pre { white-space: pre-wrap; background: var(--bg); border: 1px solid var(--line); padding: 8px; overflow-x: auto; }
.msg { border-left: 3px solid var(--accent); padding-left: 8px; }
.muted { color: var(--muted); }
"""


def host_ok(value: str | None) -> bool:
    """localhost or 127.0.0.1, with or without a port; anything else (or nothing) is refused."""
    if not value:
        return False
    host = value.strip().lower()
    if host.count(":") == 1:
        host, port = host.split(":")
        if not port.isdigit():
            return False
    return host in ALLOWED_HOSTS


class State:
    """What the page keeps while it runs: the token, the last message and the copies of the last run."""

    def __init__(self, token: str | None = None):
        self.token = token or secrets.token_urlsafe(24)
        self.message = ""
        self.report = ""
        self.copies: list[Path] = []
        self.run_project = ""


def _project() -> tuple[str, str]:
    try:
        return laptop.resolve_project(None)
    except laptop.LaptopError:
        return "", ""


def _parse_multipart(ctype: str, body: bytes) -> tuple[dict[str, str], list[tuple[str, bytes]]]:
    """The fields and the uploaded files of a multipart/form-data body (the email parser of the standard library)."""
    msg = email.parser.BytesParser(policy=email.policy.HTTP).parsebytes(
        b"Content-Type: " + ctype.encode("latin-1") + b"\r\nMIME-Version: 1.0\r\n\r\n" + body)
    fields: dict[str, str] = {}
    files: list[tuple[str, bytes]] = []
    if not msg.is_multipart():
        return fields, files
    for part in msg.iter_parts():
        name = part.get_param("name", header="content-disposition")
        filename = part.get_filename()
        data = part.get_payload(decode=True) or b""
        if filename:
            if data:
                files.append((filename, data))
        elif name:
            fields[name] = data.decode("utf-8", errors="replace")
    return fields, files


def _safe_upload_name(name: str, taken: set[str]) -> str:
    base = name.replace("\\", "/").rsplit("/", 1)[-1].strip().lstrip(".") or "upload"
    base = "".join(c if c.isalnum() or c in "._- " else "_" for c in base)[:120] or "upload"
    out, n = base, 1
    while out.casefold() in taken:
        n += 1
        stem, dot, suf = base.rpartition(".")
        out = "%s-%d.%s" % (stem, n, suf) if dot and stem else "%s-%d" % (base, n)
    taken.add(out.casefold())
    return out


def run_page(state: State, fields: dict[str, str], uploads: list[tuple[str, bytes]]) -> None:
    """One run of the page: the uploads into a temporary folder (700) that is deleted afterwards, the typed paths
    as they are, then the clean run into clean/ of the work folder."""
    project, _ = _project()
    if not project:
        state.message = "no project: type its code and press use first"
        return
    kind = fields.get("kind") or "auto"
    kind = kind if kind in clean.KINDS else None
    customer = laptop.customer_of(project)
    if not laptop.known(customer):
        state.message = ("%s is not registered on this laptop yet: run awb clean register %s once in the terminal "
                         "(Enter on an empty first line: no forms), then run again" % (customer, customer))
        return
    tmp = Path(tempfile.mkdtemp(prefix="upload-", dir=laptop.tmp_dir()))
    try:
        os.chmod(tmp, 0o700)
        files: list[Path] = []
        taken: set[str] = set()
        for name, data in uploads:
            path = tmp / _safe_upload_name(name, taken)
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "wb") as fh:
                fh.write(data)
            files.append(path)
        for line in (fields.get("paths") or "").splitlines():
            if line.strip():
                files.append(Path(clean.windows_path(line)))
        if not files:
            state.message = "no file: upload one or type a path"
            return
        lines: list[str] = []
        runs = clean.run_files(files, project, kind, laptop.clean_dir(project), say=lines.append)
    except (laptop.LaptopError, clean.CleanError) as err:
        state.message = "the run failed: %s" % err
        return
    except OSError as err:
        state.message = "the run failed: %s (operating system error)" % type(err).__name__
        return
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    reports = []
    copies: list[Path] = []
    for k, how, res in runs:
        try:
            reports.append(res.report.read_text(encoding="utf-8") if res.report else "")
        except OSError:
            reports.append("")
        copies += res.outputs
    state.run_project, state.copies = project, copies
    state.report = "\n".join(reports)
    state.message = "%d clean cop%s of %d file%s in %s" % (len(copies), "y" if len(copies) == 1 else "ies",
                                                          len(files), "" if len(files) == 1 else "s",
                                                          laptop.clean_dir(project))


def put_page(state: State, client=None) -> None:
    project, _ = _project()
    if not clean.keys_present() and client is None:
        state.message = OBS_ENV_HINT
        return
    if not project:
        state.message = "no project: type its code and press use first"
        return
    lines: list[str] = []
    try:
        clean.put_last(project, client=client, say=lines.append)
    except (laptop.LaptopError, clean.CleanError) as err:
        state.message = "put failed: %s" % err
        return
    state.message = "; ".join(lines)


def page(state: State) -> str:
    project, source = _project()
    t = urllib.parse.quote(state.token)
    esc = html.escape
    put_note = ("" if clean.keys_present() else
                '<p class="muted">%s</p>' % esc(OBS_ENV_HINT))
    links = ""
    if state.copies:
        items = "".join('<li><a href="/view?t=%s&amp;f=%s" target="_blank">%s</a></li>'
                        % (t, urllib.parse.quote(p.name), esc(p.name)) for p in state.copies)
        links = "<h2>clean copies (open as text)</h2><ul>%s</ul>" % items
    report = "<h2>report</h2><pre>%s</pre>" % esc(state.report) if state.report else ""
    msg = '<p class="msg">%s</p>' % esc(state.message) if state.message else ""
    where = ("current: %s (from %s), customer %s" % (project, source, laptop.customer_of(project))
             if project else "no project yet")
    return """<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>awb clean</title><style>%(css)s</style></head><body><main>
<h1>awb clean</h1>%(msg)s
<section><h2>project</h2>
<form method="post" action="/use?t=%(t)s">
<input type="text" name="project" value="%(project)s" placeholder="tcp-xxxx" autocomplete="off">
<button type="submit">use</button></form><p class="muted">%(where)s</p></section>
<section><h2>files</h2>
<form method="post" action="/run?t=%(t)s" enctype="multipart/form-data">
<input type="file" name="files" multiple>
<p class="muted">or type paths, one per line (D:\\Documents\\x.docx, D:/Documents/x.docx or /mnt/d/...)</p>
<textarea name="paths" rows="3"></textarea>
<p class="muted">kind</p>
<select name="kind"><option value="auto">auto: logs for a .log file, docs for any other</option>
<option value="docs">docs</option><option value="logs">logs</option></select>
<button type="submit">run</button></form></section>
<section><h2>result</h2>%(report)s%(links)s
<form method="post" action="/put?t=%(t)s"><button type="submit">put</button></form>%(put_note)s</section>
</main></body></html>
""" % {"css": CSS, "msg": msg, "t": t, "project": esc(project), "where": esc(where), "report": report,
       "links": links, "put_note": put_note}


def make_handler(state: State, client=None):
    class Handler(BaseHTTPRequestHandler):
        server_version = "awb-clean"
        sys_version = ""

        def log_message(self, fmt, *args):  # noqa: D401 - the method and the path only, never the query
            sys.stderr.write("%s %s\n" % (self.command, urllib.parse.urlsplit(self.path).path))

        def _send(self, code: int, body: str, ctype: str = "text/html; charset=utf-8", extra=()) -> None:
            data = body.encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("Content-Security-Policy", "default-src 'none'; style-src 'unsafe-inline'; "
                             "form-action 'self'; frame-ancestors 'none'")
            for k, v in extra:
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(data)

        def _guard(self) -> dict[str, list[str]] | None:
            if not host_ok(self.headers.get("Host")):
                self._send(403, "refused: the Host header is not localhost or 127.0.0.1\n", "text/plain; charset=utf-8")
                return None
            query = urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query)
            got = (query.get("t") or [""])[0]
            if not hmac.compare_digest(got.encode("utf-8"), state.token.encode("utf-8")):
                self._send(403, "refused: open the URL with the token that awb clean serve printed\n",
                           "text/plain; charset=utf-8")
                return None
            return query

        def _back(self) -> None:
            self._send(303, "", extra=(("Location", "/?t=%s" % urllib.parse.quote(state.token)),))

        def do_GET(self):  # noqa: N802
            query = self._guard()
            if query is None:
                return
            route = urllib.parse.urlsplit(self.path).path
            if route == "/":
                self._send(200, page(state))
            elif route == "/view":
                self._view((query.get("f") or [""])[0])
            else:
                self._send(404, "not found\n", "text/plain; charset=utf-8")

        def _view(self, name: str) -> None:
            path = next((p for p in state.copies if p.name == name), None)
            if path is None or not clean.is_clean_name(name) or not path.is_file() or path.is_symlink():
                self._send(404, "not found\n", "text/plain; charset=utf-8")
                return
            self._send(200, path.read_text(encoding="utf-8", errors="replace"), "text/plain; charset=utf-8")

        def do_POST(self):  # noqa: N802
            if self._guard() is None:
                return
            route = urllib.parse.urlsplit(self.path).path
            try:
                length = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                length = -1
            if length < 0 or length > MAX_BODY:
                self._send(413, "refused: the upload is larger than %d MB\n" % (MAX_BODY // (1024 * 1024)),
                           "text/plain; charset=utf-8")
                return
            body = self.rfile.read(length) if length else b""
            ctype = self.headers.get("Content-Type") or ""
            if route == "/use":
                fields = urllib.parse.parse_qs(body.decode("utf-8", errors="replace"))
                code = (fields.get("project") or [""])[0].strip()
                try:
                    folder = laptop.use(code)
                    state.message = "project %s, folder %s" % (code, folder)
                except laptop.LaptopError as err:
                    state.message = str(err)
                self._back()
            elif route == "/run":
                if not ctype.startswith("multipart/form-data"):
                    self._send(400, "refused: the run form is multipart\n", "text/plain; charset=utf-8")
                    return
                fields, uploads = _parse_multipart(ctype, body)
                del body
                run_page(state, fields, uploads)
                self._back()
            elif route == "/put":
                put_page(state, client=client)
                self._back()
            else:
                self._send(404, "not found\n", "text/plain; charset=utf-8")

    return Handler


def serve(port: int = DEFAULT_PORT, *, state: State | None = None) -> int:
    state = state or State()
    laptop.init()
    try:
        httpd = ThreadingHTTPServer((BIND, port), make_handler(state))
    except OSError as err:
        print("awb clean serve: port %d cannot be opened (%s)" % (port, type(err).__name__), file=sys.stderr)
        return clean.EXIT_ERROR
    project, source = _project()
    print("project: %s" % ("%s (from %s), customer %s" % (project, source, laptop.customer_of(project))
                           if project else "none yet, type it on the page"))
    print("open in the Windows browser: http://localhost:%d/?t=%s" % (port, state.token))
    print("listening on %s:%d only; Ctrl-C ends it" % (BIND, port))
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()
    return clean.EXIT_OK
