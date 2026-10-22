"""Public documentation for the sessions, read on the owner's side (task DF).

    awb doc fetch URL [--out FILE]     the page as text, with the line "source: URL, fetched YYYY-MM-DD HH:MM UTC"
    awb doc hosts                      the hosts a page may come from (rules/allow-domains.txt)

The egress filter of the work user lets a session reach the model API and a few hosts of the client, never a
documentation host: a session could send data out to it (seal/egress/ALLOW.md). So a session asks the key service,
which runs as the owner and has internet, with the op `doc_get` of the call socket. The service fetches one page by
GET and hands back its text; the session never opens a connection of its own.

What the service checks before anything is sent (`check_url`, the same function the client runs first):

- https only, the host exactly a line of rules/allow-domains.txt (no suffix match and no wildcard: the file
  defines none), no port, no user info, no query string and no fragment;
- a path of at most 300 characters made of URL-safe characters (letters, digits and `-._~/%+:@!$&'()*,;=`, a `%`
  only before two hex digits, no `..` segment).

Then: no cookie, no auth header, a fixed User-Agent naming the Workbench, `Accept-Encoding: identity`. A redirect
is followed only to a URL that passes the same checks, at most 3 times. A body of more than 5 MB is refused (by its
Content-Length before a byte is read, or when the read passes the cap), and only a text type is taken (HTML, XHTML,
Markdown, plain text, JSON). HTML is turned into readable text with the standard library: headings, paragraphs,
lists, code blocks, tables as rows of cells; scripts, styles and forms are left out.

Rate: at most 30 fetches a minute and 500 a day per project. Each fetch writes one line of the doc ledger,
`<vault>/log/doc-YYYY-MM.tsv`: the time, the project, the host, the path, the size and the status. Never page
content and never a query (there is none).

The project of a session is the one its key token names (K4) or, when the project has no grant, the active project
the request names; the client takes it from the SCOPE.md of the folder it runs in.
"""
from __future__ import annotations

import argparse
import html.parser
import os
import re
import socket
import ssl
import sys
import threading
import time
from collections import deque
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit

from awb import codes

RULES_DIR = Path(__file__).resolve().parent.parent.parent / "rules"
HOSTS_FILE = RULES_DIR / "allow-domains.txt"
USER_AGENT = "Architect-Workbench-doc-reader/1 (+https://github.com/shimoza/tcp-awb)"
MAX_PATH = 300
MAX_BYTES = 5 * 1024 * 1024
MAX_REDIRECTS = 3
PER_MINUTE = 30
PER_DAY = 500
TIMEOUT = 30.0
TEXT_TYPES = {
    "text/html": "html", "application/xhtml+xml": "html",
    "text/markdown": "markdown", "text/x-markdown": "markdown",
    "text/plain": "text", "application/json": "json",
}
REDIRECTS = (301, 302, 303, 307, 308)
_HOST_RE = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?(?:\.[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?)+$")
_PATH_RE = re.compile(r"^/[A-Za-z0-9._~/%+:@!$&'()*,;=-]*$")
_BAD_PERCENT_RE = re.compile(r"%(?![0-9A-Fa-f]{2})")
_PROJECT_RE = re.compile("^%s$" % codes.TCP_PROJECT_WIDE)


class DocError(Exception):
    """A fetch that is refused or failed. The message names the rule, never a value of the request or the page."""


# --------------------------------------------------------------------------- the checks (both sides)


def allowed_hosts(path: Path | None = None) -> list[str]:
    """The lines of rules/allow-domains.txt, read at each call so a change of the rules needs no restart."""
    try:
        text = Path(path or HOSTS_FILE).read_text(encoding="utf-8")
    except OSError:
        return []
    out = []
    for line in text.splitlines():
        line = line.strip().lower()
        if line and not line.startswith("#") and line not in out:
            out.append(line)
    return out


def check_url(url, hosts: list[str] | None = None) -> tuple[str, str]:
    """(host, path) of a URL the reader may fetch; DocError naming the rule otherwise."""
    if not isinstance(url, str) or not url or len(url) > 400 or any(ord(c) < 33 or ord(c) > 126 for c in url):
        raise DocError("refused: the URL is not plain ASCII without spaces")
    try:
        parts = urlsplit(url)
        port = parts.port
    except ValueError:
        raise DocError("refused: the URL cannot be read") from None
    if parts.scheme.lower() != "https" or not url.lower().startswith("https://"):
        raise DocError("refused: https only")
    if "@" in parts.netloc or parts.username is not None or parts.password is not None:
        raise DocError("refused: no user info in the URL")
    if port is not None or parts.netloc.endswith(":"):
        raise DocError("refused: no port in the URL")
    if "?" in url:
        raise DocError("refused: no query string")
    if "#" in url:
        raise DocError("refused: no fragment")
    host = (parts.hostname or "").lower()
    if not _HOST_RE.match(host) or host != parts.netloc.lower():
        raise DocError("refused: the host is not a plain host name")
    if host not in (allowed_hosts() if hosts is None else hosts):
        raise DocError("refused: the host is not on rules/allow-domains.txt (awb doc hosts)")
    path = parts.path or "/"
    if len(path) > MAX_PATH:
        raise DocError("refused: the path is longer than %d characters" % MAX_PATH)
    if not _PATH_RE.match(path) or _BAD_PERCENT_RE.search(path) or ".." in path.split("/") \
            or "%2e%2e" in path.lower():
        raise DocError("refused: the path has a character that is not URL-safe")
    return host, path


# --------------------------------------------------------------------------- HTML as text


class _Text(html.parser.HTMLParser):
    SKIP = frozenset({"script", "style", "noscript", "template", "svg", "head", "form", "button", "select",
                      "iframe", "object", "canvas", "nav"})
    BLOCK = frozenset({"p", "div", "section", "article", "main", "header", "footer", "aside", "blockquote",
                       "dl", "dt", "dd", "figure", "figcaption", "details", "summary"})
    VOID = frozenset({"br", "hr", "img", "input", "meta", "link", "wbr", "area", "base", "col", "embed", "source",
                      "track", "param"})

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.out: list[str] = []
        self.line: list[str] = []
        self.skip = 0
        self.pre = 0
        self.lists: list[list] = []
        self.row: list[str] | None = None
        self.cell: list[str] | None = None
        self.title: list[str] | None = None
        self.titled = ""
        self.indent = ""

    def _flush(self, blank: bool = True) -> None:
        text = " ".join("".join(self.line).split())
        self.line = []
        if text:
            self.out.append(self.indent + text)
        self.indent = ""
        if blank and self.out and self.out[-1] != "":
            self.out.append("")

    def handle_starttag(self, tag, attrs):
        if tag == "title" and not self.titled:
            self.title = []
            return
        if self.skip or tag in self.SKIP:
            if tag in self.SKIP and tag not in self.VOID:
                self.skip += 1
            return
        if self.pre:
            return
        if re.fullmatch(r"h[1-6]", tag):
            self._flush()
            self.line.append("#" * int(tag[1]) + " ")
        elif tag in self.BLOCK:
            self._flush()
        elif tag in ("ul", "ol"):
            self._flush(blank=not self.lists)
            self.lists.append([tag, 0])
        elif tag == "li":
            self._flush(blank=False)
            depth = max(0, len(self.lists) - 1)
            if self.lists and self.lists[-1][0] == "ol":
                self.lists[-1][1] += 1
                self.line.append("%d. " % self.lists[-1][1])
            else:
                self.line.append("- ")
            self.indent = "  " * depth
        elif tag == "pre":
            self._flush()
            self.out.append("```")
            self.pre = 1
        elif tag == "code":
            self._data("`")
        elif tag == "br":
            self._flush(blank=False)
        elif tag == "hr":
            self._flush()
        elif tag == "table":
            self._flush()
        elif tag == "tr":
            self.row = []
        elif tag in ("td", "th"):
            self.cell = []

    def handle_endtag(self, tag):
        if tag == "title" and self.title is not None:
            self.titled = " ".join("".join(self.title).split())
            self.title = None
            return
        if self.skip:
            if tag in self.SKIP:
                self.skip -= 1
            return
        if self.pre:
            if tag == "pre":
                text = "".join(self.line).rstrip("\n")
                self.line = []
                self.out.extend(text.split("\n") if text else [])
                self.out.extend(["```", ""])
                self.pre = 0
            return
        if re.fullmatch(r"h[1-6]", tag) or tag in self.BLOCK or tag == "table":
            self._flush()
        elif tag in ("ul", "ol"):
            self._flush(blank=False)
            if self.lists:
                self.lists.pop()
            if not self.lists:
                self._flush()
        elif tag == "li":
            self._flush(blank=False)
        elif tag == "code":
            self._data("`")
        elif tag in ("td", "th") and self.cell is not None and self.row is not None:
            self.row.append(" ".join("".join(self.cell).split()).replace("|", "/"))
            self.cell = None
        elif tag == "tr" and self.row is not None:
            if any(self.row):
                self.out.append("| " + " | ".join(self.row) + " |")
            self.row = None

    def handle_data(self, data):
        if self.title is not None:
            self.title.append(data)
            return
        if self.skip:
            return
        self._data(data)

    def _data(self, data):
        if self.cell is not None:
            self.cell.append(data)
        elif self.pre:
            self.line.append(data)
        else:
            self.line.append(data.replace("\n", " "))

    def text(self) -> str:
        self._flush()
        lines, blank = [], False
        for line in self.out:
            if line == "":
                if not blank and lines:
                    lines.append("")
                blank = True
            else:
                lines.append(line)
                blank = False
        body = "\n".join(lines).strip("\n")
        if self.titled and not body.startswith("# "):
            body = "# %s\n\n%s" % (self.titled, body)
        return body + "\n"


def html_text(page: str) -> str:
    """Readable text of an HTML page: headings, paragraphs, lists, code, tables as rows of cells."""
    p = _Text()
    p.feed(page)
    p.close()
    return p.text()


def _plain(text: str) -> str:
    """Text without control characters but tab and newline."""
    return re.sub(r"[\x00-\x08\x0b-\x1f\x7f]", "", text.replace("\r\n", "\n").replace("\r", "\n"))


# --------------------------------------------------------------------------- the service side


def https_get(host: str, path: str, timeout: float = TIMEOUT):
    """One GET on the host's port 443 with fixed headers. Returns (status, headers in lower case, response)."""
    import http.client

    conn = http.client.HTTPSConnection(host, 443, timeout=timeout, context=ssl.create_default_context())
    conn.request("GET", path, headers={"User-Agent": USER_AGENT, "Accept": "text/html, text/markdown, text/plain, "
                                       "application/json;q=0.9", "Accept-Encoding": "identity",
                                       "Connection": "close"})
    resp = conn.getresponse()
    return resp.status, {k.lower(): v for k, v in resp.getheaders()}, resp


class Limiter:
    """30 fetches a minute and 500 a day per project. The count of the day starts from the doc ledger, so a
    restart of the service does not reset it."""

    def __init__(self, ledger_dir_fn, per_minute: int = PER_MINUTE, per_day: int = PER_DAY):
        self.ledger_dir_fn = ledger_dir_fn
        self.per_minute = per_minute
        self.per_day = per_day
        self.lock = threading.Lock()
        self.minute: dict[str, deque] = {}
        self.day: dict[str, tuple[str, int]] = {}

    def take(self, project: str, now: float | None = None) -> str | None:
        """None when a fetch may go, or the rule that stops it."""
        now = time.monotonic() if now is None else now
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        with self.lock:
            q = self.minute.setdefault(project, deque())
            while q and now - q[0] >= 60.0:
                q.popleft()
            day, count = self.day.get(project, ("", 0))
            if day != today:
                day, count = today, _ledger_count(self.ledger_dir_fn(), project, today)
            if len(q) >= self.per_minute:
                self.day[project] = (day, count)
                return "refused: at most %d fetches a minute per project" % self.per_minute
            if count >= self.per_day:
                self.day[project] = (day, count)
                return "refused: at most %d fetches a day per project" % self.per_day
            q.append(now)
            self.day[project] = (day, count + 1)
            return None


def _ledger_path(folder: Path) -> Path:
    return Path(folder) / ("doc-%s.tsv" % datetime.now(timezone.utc).strftime("%Y-%m"))


def _ledger_count(folder, project: str, today: str) -> int:
    if folder is None:
        return 0
    try:
        with open(_ledger_path(folder), encoding="utf-8") as fh:
            rows = [line.rstrip("\n").split("\t") for line in fh if line.startswith(today)]
    except OSError:
        return 0
    # a line of a fetch that went out: its host is known and the rate did not stop it
    return sum(1 for r in rows if len(r) == 6 and r[1] == project and r[2] != "-" and r[5] != "rate")


def ledger_line(folder, project: str, host: str, path: str, size, status: str) -> None:
    """One line of the doc ledger: time, project, host, path, size, status. Never content."""
    if folder is None:
        return
    try:
        folder = Path(folder)
        folder.mkdir(parents=True, exist_ok=True)
        fd = os.open(_ledger_path(folder), os.O_WRONLY | os.O_CREAT | os.O_APPEND | os.O_NOFOLLOW, 0o600)
        fields = (datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"), project, host or "-", path or "-",
                  size if isinstance(size, int) else "-", status)
        with os.fdopen(fd, "a", encoding="utf-8") as fh:
            fh.write("\t".join(re.sub(r"[\t\r\n]+", " ", str(x)) for x in fields) + "\n")
    except OSError:
        pass


def _read_capped(resp, cap: int) -> bytes:
    buf = bytearray()
    while len(buf) <= cap:
        chunk = resp.read(min(1 << 16, cap + 1 - len(buf)))
        if not chunk:
            break
        buf += chunk
    if len(buf) > cap:
        raise DocError("refused: the page is larger than %d MB" % (cap // (1024 * 1024)))
    return bytes(buf)


def fetch(url: str, *, getter=https_get, hosts: list[str] | None = None, cap: int = MAX_BYTES) -> dict:
    """The page of a checked URL as text: {"url", "host", "path", "kind", "status", "size", "text"}. DocError
    naming the rule otherwise (with "host" and "path" of the last URL it got to, for the ledger)."""
    hosts = allowed_hosts() if hosts is None else hosts
    host, path = check_url(url, hosts)
    for hop in range(MAX_REDIRECTS + 1):
        try:
            status, headers, resp = getter(host, path)
        except DocError:
            raise
        except (OSError, socket.timeout, ssl.SSLError, ValueError) as err:
            raise _at(DocError("failed: no answer from the host (%s)" % type(err).__name__), host, path) from None
        except Exception as err:   # http.client raises its own classes
            raise _at(DocError("failed: no answer from the host (%s)" % type(err).__name__), host, path) from None
        try:
            if status in REDIRECTS:
                if hop == MAX_REDIRECTS:
                    raise DocError("refused: more than %d redirects" % MAX_REDIRECTS)
                where = headers.get("location") or ""
                if where.startswith("/") and not where.startswith("//"):
                    where = "https://%s%s" % (host, where)
                try:
                    host, path = check_url(where, hosts)
                except DocError as err:
                    raise DocError("refused: a redirect to a URL the rules refuse (%s)"
                                   % str(err).split(": ", 1)[-1]) from None
                url = "https://%s%s" % (host, path)
                continue
            if status != 200:
                raise DocError("failed: the host answered %d" % status if isinstance(status, int)
                               else "failed: the host gave no status")
            ctype = (headers.get("content-type") or "").split(";")[0].strip().lower()
            kind = TEXT_TYPES.get(ctype)
            if kind is None:
                raise DocError("refused: not a text type (HTML, Markdown, plain text or JSON)")
            if (headers.get("content-encoding") or "identity").strip().lower() != "identity":
                raise DocError("refused: an encoded body (the reader asks for identity)")
            length = headers.get("content-length")
            if length is not None and (not length.strip().isdigit() or int(length) > cap):
                raise DocError("refused: the page is larger than %d MB" % (cap // (1024 * 1024)))
            body = _read_capped(resp, cap)
            m = re.search(r"charset=([A-Za-z0-9_.:-]{1,40})", headers.get("content-type") or "")
            try:
                page = body.decode(m.group(1) if m else "utf-8", errors="replace")
            except LookupError:
                page = body.decode("utf-8", errors="replace")
            text = html_text(page) if kind == "html" else _plain(page)
            return {"url": url, "host": host, "path": path, "kind": kind, "status": status, "size": len(body),
                    "text": _plain(text)}
        except DocError as err:
            raise _at(err, host, path) from None
        finally:
            try:
                resp.close()
            except Exception:
                pass
    raise _at(DocError("refused: more than %d redirects" % MAX_REDIRECTS), host, path)


def _at(err: DocError, host: str, path: str) -> DocError:
    err.host, err.path = host, path
    return err


def serve(req: dict, project, limiter: Limiter, ledger_dir, *, getter=https_get, hosts=None, tmp_dir=None):
    """The op doc_get of the key service. Returns (answer, file of the text to stream) or (answer, None)."""
    import tempfile

    url = req.get("url")
    if not (isinstance(project, str) and _PROJECT_RE.match(project)):
        return {"ok": False, "error": "refused: a fetch runs for an active project (SCOPE.md of the folder)"}, None
    hosts = allowed_hosts() if hosts is None else hosts
    try:
        host, path = check_url(url, hosts)
    except DocError as err:
        ledger_line(ledger_dir, project, "-", "-", None, "refused")
        return {"ok": False, "error": str(err)}, None
    stop = limiter.take(project)
    if stop:
        ledger_line(ledger_dir, project, host, path, None, "rate")
        return {"ok": False, "error": stop}, None
    try:
        page = fetch(url, getter=getter, hosts=hosts)
    except DocError as err:
        word = str(err).split(":", 1)[0]
        ledger_line(ledger_dir, project, getattr(err, "host", host), getattr(err, "path", path), None, word)
        return {"ok": False, "error": str(err)}, None
    ledger_line(ledger_dir, project, page["host"], page["path"], page["size"], str(page["status"]))
    data = page["text"].encode("utf-8")
    fd, name = tempfile.mkstemp(prefix="awb-doc-", dir=tmp_dir)
    with os.fdopen(fd, "wb") as fh:
        fh.write(data)
    fetched = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    return {"ok": True, "url": page["url"], "kind": page["kind"], "size": page["size"], "fetched": fetched}, Path(name)


# --------------------------------------------------------------------------- the session side


STEERING = "Steering: allow documentation host %s (vendor, why)"
"""The owner item a session leaves in OPEN.md under the owner heading for a host the list lacks, never a question."""
OFF_LIST = "not on rules/allow-domains.txt"


def _refusal(line: str, url: str) -> str:
    """The one line of a refusal; for a host off the list it names the owner item to leave in OPEN.md."""
    if OFF_LIST not in line:
        return line
    try:
        host = (urlsplit(url).hostname or "").lower()
    except ValueError:
        host = ""
    host = host if _HOST_RE.match(host) else "HOST"
    return "%s; leave one owner item in OPEN.md under the owner heading: %s" % (line, STEERING % host)


def header(url: str, fetched: str) -> str:
    return "source: %s, fetched %s" % (url, fetched)


def _fetch_cmd(args) -> int:
    import tempfile

    from awb.tcp import keys, xchg

    try:
        check_url(args.url)
    except DocError as err:
        print(_refusal(str(err), args.url), file=sys.stderr)
        return 1
    try:
        root, code, _ = xchg._project()
    except xchg.XchgError as err:
        print("refused: %s" % err, file=sys.stderr)
        return 2
    out = None
    if args.out:
        out = Path(os.path.abspath(args.out if os.path.isabs(args.out) else Path.cwd() / args.out))
        real_root = Path(os.path.realpath(root))
        real_parent = Path(os.path.realpath(out.parent))
        if not (real_parent == real_root or real_root in real_parent.parents) or os.path.islink(out):
            print("refused: --out names a file inside the project folder", file=sys.stderr)
            return 2
    fd, tmp = tempfile.mkstemp(prefix="awb-doc-")
    os.close(fd)
    try:
        req = {"op": "doc_get", "url": args.url, "project": code}
        try:
            answer = keys.request(keys.call_socket(), req, timeout=TIMEOUT * (MAX_REDIRECTS + 1) + 10,
                                  download=Path(tmp))
        except keys.KeysError as err:
            print("failed: %s" % err, file=sys.stderr)
            return 2
        if not answer.get("ok"):
            print(_refusal(answer.get("error") or "refused: the key service said no", args.url), file=sys.stderr)
            return 1
        body = Path(tmp).read_text(encoding="utf-8", errors="replace")
    finally:
        try:
            os.unlink(tmp)
        except OSError:
            pass
    text = header(answer.get("url") or args.url, answer.get("fetched") or "?") + "\n\n" + body
    if out is None:
        sys.stdout.write(text)
        return 0
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(text, encoding="utf-8")
    print("%s: %d characters, %s" % (os.path.relpath(out, root), len(text), header(answer.get("url") or args.url,
                                                                                   answer.get("fetched") or "?")))
    return 0


def _hosts_cmd(args) -> int:
    hosts = allowed_hosts()
    if not hosts:
        print("no host: rules/allow-domains.txt is empty or missing", file=sys.stderr)
        return 1
    print("\n".join(hosts))
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="awb doc", description="public documentation of an allowed host, fetched by "
                                 "the key service on the owner's side and printed as text with its source and date")
    sub = ap.add_subparsers(dest="cmd")
    f = sub.add_parser("fetch", help="one page as text: https, a host of rules/allow-domains.txt, no query string; "
                                     "the first line names the source and the time of the fetch")
    f.add_argument("url", metavar="URL", help="https://HOST/PATH, the host exactly a line of awb doc hosts")
    f.add_argument("--out", metavar="FILE", help="write the text to FILE inside the project folder instead of "
                                                 "printing it")
    f.set_defaults(func=_fetch_cmd)
    h = sub.add_parser("hosts", help="the hosts a page may come from (rules/allow-domains.txt)")
    h.set_defaults(func=_hosts_cmd)
    args = ap.parse_args(argv)
    if not getattr(args, "func", None):
        ap.print_usage(sys.stderr)
        return 2
    return int(args.func(args))


if __name__ == "__main__":
    sys.exit(main())
