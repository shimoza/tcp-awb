"""Public documentation for the sessions, read on the owner's side (task DF).

    awb doc fetch URL [--out FILE]     the page as text, with the line "source: URL, fetched YYYY-MM-DD HH:MM UTC"
    awb doc fetch URL --links          the same, then the links of the page as absolute URLs with their texts
    awb doc find URL WORDS             the pages of a help system whose title or path holds every word (task DL)
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

Then: no cookie, no auth header, a fixed User-Agent naming the Workbench, `Accept-Encoding: identity`. A host that
sends gzip all the same (the docs host does since 2026-11) is read: the gzip body is unpacked under the same cap, a
broken one and every other encoding are refused. A redirect
is followed only to a URL that passes the same checks, at most 3 times. A body of more than 5 MB is refused (by its
Content-Length before a byte is read, or when the read passes the cap), and only a text type is taken (HTML, XHTML,
Markdown, plain text, JSON) or a PDF (task DF-PDF, 2026-11-07): `application/pdf`, or an octet stream whose body starts
with `%PDF-`, up to 32 MB. A PDF becomes text through `pdftotext -layout` of the poppler tools on a private temporary
file (removed in every case), with an argument list and a timeout of 60 s; each page starts with a line
`--- page N ---`. An encrypted PDF, one pdftotext cannot read and one without a text layer (a scan: fewer than 20
characters a page) are refused with the reason. HTML is turned into readable text with the standard library: headings, paragraphs,
lists, code blocks, tables as rows of cells; scripts, styles and forms are left out.

Rate: at most 30 fetches a minute and 500 a day per project. Each fetch writes one line of the doc ledger,
`<vault>/log/doc-YYYY-MM.tsv`: the time, the project, the host, the path, the size and the status. Never page
content and never a query (there is none).

Links and help indexes (task DL, 2026-11-11): `--links` appends the links of an HTML page (absolute, without the
fragment, at most MAX_LINKS, a host off the list marked), so a session can walk from page to page. A help system built
with MadCap Flare draws its table of contents with JavaScript, so its pages carry no links to each other: the page
names its help root (`data-mc-path-to-help-system`), the root holds Data/HelpSystem.xml, that names the Toc file and the
Toc file its chunks, which map every page path to its title. `awb doc find URL WORDS` makes the service read that
chain (each file one fetch of the rate and one ledger line, each from an allowed host) and hand back only the
matching titles with their URLs, never the script itself. A plain fetch still takes no script.

The project of a session is the one its key token names (K4) or, when the project has no grant, the active project
the request names; the client takes it from the SCOPE.md of the folder it runs in.
"""
from __future__ import annotations

import argparse
import contextlib
import fcntl
import html.parser
import os
import re
import socket
import ssl
import stat
import sys
import threading
import time
import unicodedata
import zlib
from collections import deque
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import unquote, urldefrag, urljoin, urlsplit

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
PDF_TYPES = {"application/pdf"}
OCTET_TYPES = {"application/octet-stream", "binary/octet-stream"}
MAX_PDF_BYTES = 32 * 1024 * 1024
"""A PDF may be larger than a page of text: service descriptions and price lists are PDFs of several MB."""
PDF_TEXT_MAX = 8 * 1024 * 1024
PDF_TIMEOUT = 60.0
PDFTOTEXT = "/usr/bin/pdftotext"
PDF_MIN_CHARS = 20
"""Fewer characters a page than this on average: a scan without a text layer."""
REDIRECTS = (301, 302, 303, 307, 308)
MAX_LINKS = 400
MAX_FOUND = 60
INDEX_TYPES = {"application/javascript": "script", "text/javascript": "script", "application/x-javascript": "script",
               "text/xml": "xml", "application/xml": "xml"}
"""Types a help index comes in; taken only by `find`, never by a fetch of a page."""
_WORDS_RE = re.compile(r"^[A-Za-z0-9 ._/&+-]{2,120}$")
_HOST_RE = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?(?:\.[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?)+$")
_PATH_RE = re.compile(r"^/[A-Za-z0-9._~/%+:@!$&'()*,;=-]*$")
_BAD_PERCENT_RE = re.compile(r"%(?![0-9A-Fa-f]{2})")
_PROJECT_RE = re.compile("^%s$" % codes.TCP_PROJECT_WIDE)


class DocError(Exception):
    """A fetch that is refused or failed. The message names the rule, never a value of the request or the page."""


@contextlib.contextmanager
def _directory(path: Path, create: bool = False):
    """Walk without following links and keep operations relative to the opened directory."""
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
    finally:
        os.close(fd)


@contextlib.contextmanager
def _regular(directory: int, name: str, flags: int = os.O_RDONLY, mode: int = 0o600):
    fd = os.open(name, flags | os.O_NOFOLLOW | os.O_NONBLOCK, mode, dir_fd=directory)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise OSError("unsafe file")
        yield fd
    finally:
        os.close(fd)


def _read_file(path: Path, cap: int) -> str:
    with _directory(path.parent) as directory, _regular(directory, path.name) as fd:
        fcntl.flock(fd, fcntl.LOCK_SH)
        with os.fdopen(os.dup(fd), "rb") as stream:
            data = stream.read(cap + 1)
        if len(data) > cap:
            raise OSError("file too large")
        return data.decode("utf-8")


def _append(fd: int, data: bytes) -> None:
    """Called under an exclusive lock; a failed append must not leave half a row."""
    before = os.lseek(fd, 0, os.SEEK_END)
    try:
        while data:
            count = os.write(fd, data)
            if count <= 0:
                raise OSError("short write")
            data = data[count:]
    except OSError:
        os.ftruncate(fd, before)
        raise


def _line_of(text: str, at: int) -> str:
    return "line %d of %d" % (text.count("\n", 0, max(at, 0)) + 1, text.count("\n") + 1)


MASKED_LINE = "[a line withheld: a secret-like value of the page]"


def mask_secret_lines(text: str) -> tuple[str, int]:
    """`text` with each line that holds a secret, a token or a private key replaced by MASKED_LINE, and the count.
    A public vendor page shows sample credentials in its examples (tcp-bytg ask 2026-11-24-4: the Veeam cmdlet
    pages); the rest of the page is still useful. The caller checks the result again and withholds the whole page
    when anything is left, so a value split over lines never passes."""
    from awb import check, gate

    out, n = [], 0
    for line in text.split("\n"):
        if any(gate.DETECTORS[cls](line) for cls in check.SECRET_CLASSES if cls != "homepath"):
            out.append(MASKED_LINE)
            n += 1
        else:
            out.append(line)
    return "\n".join(out), n


def private_reason(text: str) -> str | None:
    """Why `text` is withheld, without its value: the class of the first hit and its line; None when it is public.
    The same decision as `_public`."""
    from awb import check, config

    try:
        text.encode("utf-8")
        for i, c in enumerate(text):
            if unicodedata.category(c).startswith("C") and c not in "\n\t":
                return "a control character at %s" % _line_of(text, i)
        secrets_found = check._secret_classes(text)
        if secrets_found:
            return "a secret (%s)" % ", ".join(sorted(secrets_found))
        register = config.paths().register
        try:
            hits = check.check_text(text, register)
        except check.CheckUnavailable:
            if config.is_work_user():
                return "the name check is unavailable"
            try:
                register.parent.stat()
            except FileNotFoundError:
                hits = []
            else:
                return "the name check is unavailable"
        names = sorted(h.get("start", 0) for h in hits if h.get("cls") == "name")
        if names:
            return "%d form(s) of the name register, the first at %s" % (len(names), _line_of(text, names[0]))
        return None
    except Exception:
        return "the check failed"


def _public(text: str, fallback: str) -> str:
    """Withhold private output and checker failures, with an unconfigured development fallback."""
    return text if private_reason(text) is None else fallback


# --------------------------------------------------------------------------- the checks (both sides)


def allowed_hosts(path: Path | None = None) -> list[str]:
    """The lines of rules/allow-domains.txt, read at each call so a change of the rules needs no restart."""
    try:
        text = _read_file(Path(path or HOSTS_FILE), 1024 * 1024)
    except (OSError, UnicodeError):
        return []
    out = []
    for line in text.splitlines():
        line = line.strip().lower()
        if _HOST_RE.fullmatch(line) and line not in out:
            out.append(line)
    return out


EXTRA_KEY = "doc_hosts"
"""The key of the host file (paths.conf) that names the owner-side file of the hosts `awb doc hosts add` writes."""


def extra_file() -> Path | None:
    """The file the key `doc_hosts` names; None without the key. The owner writes it, the work user only reads it."""
    from awb import config

    value = config.host_conf().get(EXTRA_KEY, "").strip()
    return Path(value) if value.startswith("/") else None


def extra_hosts(path: Path | None = None) -> list[str]:
    """The hosts of the owner-side file, read at each call: a line is `HOST` or `HOST  # why`."""
    try:
        f = path or extra_file()
        text = _read_file(Path(f), 1024 * 1024) if f else ""
    except (OSError, UnicodeDecodeError):
        return []
    out = []
    for line in text.splitlines():
        line = line.split("#", 1)[0].strip().lower()
        if _HOST_RE.fullmatch(line) and line not in out:
            out.append(line)
    return out


def current_hosts() -> list[str]:
    """The repository list and then the owner-side list, each read at each call."""
    hosts = allowed_hosts()
    return hosts + [h for h in extra_hosts() if h not in hosts]


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
    if host not in (current_hosts() if hosts is None else hosts):
        raise DocError("refused: the host is not on rules/allow-domains.txt (awb doc hosts)")
    path = parts.path or "/"
    if len(path) > MAX_PATH:
        raise DocError("refused: the path is longer than %d characters" % MAX_PATH)
    if not _PATH_RE.match(path) or _BAD_PERCENT_RE.search(path) or ".." in path.split("/") \
            or "%2e%2e" in path.lower():
        raise DocError("refused: the path has a character that is not URL-safe")
    decoded = path
    while True:
        if ".." in decoded.split("/") or "\\" in decoded or any(ord(c) < 32 or ord(c) == 127 for c in decoded):
            raise DocError("refused: the path has a character that is not URL-safe")
        next_path = unquote(decoded)
        if next_path == decoded:
            break
        decoded = next_path
    if not _public(url, ""):
        raise DocError("refused: private URL withheld")
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
        self.skip: list[str] = []
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
        if tag == "title" and not self.titled and (not self.skip or self.skip == ["head"]):
            self.title = []
            return
        if self.skip or tag in self.SKIP:
            if tag in self.SKIP and tag not in self.VOID:
                self.skip.append(tag)
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
            if tag == self.skip[-1]:
                self.skip.pop()
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

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        if tag not in self.VOID:
            self.handle_endtag(tag)

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


class _Links(html.parser.HTMLParser):
    """The links of a page as (href, text) and the help root a MadCap Flare page names."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.links: list[tuple[str, str]] = []
        self.open: list[str] | None = None
        self.href = ""
        self.help_root: str | None = None
        self.frame: list[str] = []
        self.frame_links: set[int] = set()

    FRAME = ("nav", "header", "footer", "aside")

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag in self.FRAME:
            self.frame.append(tag)
        if tag == "html" and a.get("data-mc-path-to-help-system") is not None:
            self.help_root = a["data-mc-path-to-help-system"] or "./"
        if tag == "a" and a.get("href"):
            self.href, self.open = a["href"], []

    def handle_endtag(self, tag):
        if tag == "a" and self.open is not None:
            if self.frame:
                self.frame_links.add(len(self.links))
            self.links.append((self.href, " ".join("".join(self.open).split())))
            self.open = None
        elif tag in self.FRAME and tag in self.frame:
            del self.frame[len(self.frame) - 1 - self.frame[::-1].index(tag):]

    def handle_data(self, data):
        if self.open is not None:
            self.open.append(data)


def page_links(page: str, base: str, hosts: list[str]) -> tuple[list[str], str | None]:
    """Lines "- TEXT: URL" of the links of a page (http and https, absolute, no fragment, each URL once, a host off
    the list marked) and the help root URL of a MadCap Flare page, or None."""
    p = _Links()
    p.feed(page)
    p.close()
    seen, lines = set(), []
    # the links of the page body first, then those of the navigation, header, aside and footer (tcp-bytg ask
    # 2026-11-24-3: a help tree of thousands of entries came first and the cap cut every body link)
    order = [i for i in range(len(p.links)) if i not in p.frame_links] + sorted(p.frame_links)
    for href, text in (p.links[i] for i in order):
        try:
            if any(ord(c) < 32 for c in href):
                continue
            url = urldefrag(urljoin(base, href.strip()))[0]
            parts = urlsplit(url)
            if parts.username is not None or parts.password is not None or not _public(url, ""):
                continue
        except ValueError:
            continue
        if parts.scheme not in ("http", "https") or not parts.hostname or url in seen or url == base:
            continue
        seen.add(url)
        mark = "" if parts.scheme == "https" and parts.hostname.lower() in hosts else " (not an allowed host)"
        lines.append("- %s: %s%s" % (text or "(no text)", url, mark))
        if len(lines) == MAX_LINKS:
            break
    root = None
    if p.help_root is not None:
        try:
            root = urljoin(base, p.help_root)
            check_url(root, hosts)
        except (ValueError, DocError):
            root = None
    return lines, root


def html_text(page: str) -> str:
    """Readable text of an HTML page: headings, paragraphs, lists, code, tables as rows of cells."""
    p = _Text()
    p.feed(page)
    p.close()
    return p.text()


def _plain(text: str) -> str:
    """Text without control characters but tab and newline, and without the invisible marks a page carries
    harmlessly: a byte order mark, zero-width spaces and joiners, the word joiner and the soft hyphen (tcp-s2z5
    2026-11-25: the archive help pages of a vendor carry a byte order mark in an included part)."""
    return re.sub(r"[\x00-\x08\x0b-\x1f\x7f\ufeff\u200b-\u200d\u2060\u00ad]", "",
                  text.replace("\r\n", "\n").replace("\r", "\n"))


# --------------------------------------------------------------------------- the service side


def https_get(host: str, path: str, timeout: float = TIMEOUT):
    """One GET on the host's port 443 with fixed headers. Returns (status, headers in lower case, response)."""
    import http.client

    conn = http.client.HTTPSConnection(host, 443, timeout=timeout, context=ssl.create_default_context())
    try:
        conn.request("GET", path, headers={"User-Agent": USER_AGENT, "Accept": "text/html, text/markdown, text/plain, "
                                           "application/json;q=0.9, application/pdf;q=0.8, */*;q=0.1",
                                           "Accept-Encoding": "identity",
                                           "Connection": "close"})
        resp = conn.getresponse()
    except Exception:
        with contextlib.suppress(Exception):
            conn.close()
        raise
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
        text = _read_file(_ledger_path(folder), 64 * 1024 * 1024)
        rows = [line.split("\t") for line in text.splitlines() if line.startswith(today)]
    except FileNotFoundError:
        return 0
    except (OSError, UnicodeError):
        raise DocError("refused: the documentation rate ledger cannot be read") from None
    # a line of a fetch that went out: its host is known and the rate did not stop it
    return sum(1 for r in rows if len(r) == 6 and r[1] == project and r[2] != "-" and r[5] != "rate")


def ledger_line(folder, project: str, host: str, path: str, size, status: str) -> None:
    """One line of the doc ledger: time, project, host, path, size, status. Never content."""
    if folder is None:
        return
    try:
        folder = Path(folder)
        project = project if isinstance(project, str) and _PROJECT_RE.fullmatch(project) else "-"
        try:
            check_url("https://%s%s" % (host, path), [host])
        except DocError:
            host, path = "-", "-"
        status = status if re.fullmatch(r"[1-5][0-9]{2}|refused|failed|rate", str(status)) else "failed"
        fields = (datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"), project, host or "-", path or "-",
                  size if isinstance(size, int) else "-", status)
        with _directory(folder, create=True) as directory, _regular(
                directory, _ledger_path(folder).name, os.O_WRONLY | os.O_CREAT | os.O_APPEND) as fd:
            fcntl.flock(fd, fcntl.LOCK_EX)
            _append(fd, ("\t".join(str(x) for x in fields) + "\n").encode("utf-8"))
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


def _gunzip_capped(data: bytes, cap: int) -> bytes:
    """A gzip body unpacked, refused when it unpacks to more than CAP bytes or is not whole gzip."""
    unpack = zlib.decompressobj(16 + zlib.MAX_WBITS)
    try:
        out = unpack.decompress(data, cap + 1)
    except zlib.error:
        raise DocError("refused: an encoded body that is not valid gzip") from None
    if len(out) > cap:
        raise DocError("refused: the page is larger than %d MB" % (cap // (1024 * 1024)))
    if not unpack.eof or unpack.unused_data:
        raise DocError("refused: an encoded body that is not valid gzip")
    return out


def _pdf_run(cmd, *, timeout, **kwargs):
    """Drain both pipes under byte limits, killing and reaping the reader on every failure."""
    import selectors
    import subprocess

    with subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE) as proc:
        end = time.monotonic() + timeout
        output, errors = bytearray(), bytearray()
        try:
            with selectors.DefaultSelector() as selector:
                for stream, buffer, limit in ((proc.stdout, output, PDF_TEXT_MAX), (proc.stderr, errors, 65536)):
                    os.set_blocking(stream.fileno(), False)
                    selector.register(stream, selectors.EVENT_READ, (buffer, limit))
                while selector.get_map():
                    left = end - time.monotonic()
                    if left <= 0:
                        raise subprocess.TimeoutExpired(cmd, timeout)
                    for key, _ in selector.select(left):
                        buffer, limit = key.data
                        data = os.read(key.fd, min(65536, limit + 1 - len(buffer)))
                        if not data:
                            selector.unregister(key.fileobj)
                            continue
                        buffer.extend(data)
                        if len(buffer) > limit:
                            raise DocError("refused: the PDF reader output is larger than its limit")
            proc.wait(timeout=max(0, end - time.monotonic()))
            return subprocess.CompletedProcess(cmd, proc.returncode, bytes(output), bytes(errors))
        finally:
            if proc.poll() is None:
                proc.kill()
            proc.wait()


def pdf_text(data: bytes, *, runner=None, tool: str = PDFTOTEXT, timeout: float = PDF_TIMEOUT) -> tuple[str, int]:
    """The text of a PDF and its page count, each page after a line `--- page N ---`. The bytes go to a file of a
    private temporary folder (700, the file 600) that is removed in every case; pdftotext gets an argument list,
    never a shell, no standard input and a timeout. DocError for an encrypted PDF, one it cannot read, a text larger
    than PDF_TEXT_MAX and a scan without a text layer."""
    import subprocess
    import tempfile

    run = runner or _pdf_run
    with tempfile.TemporaryDirectory(prefix="awb-doc-pdf-") as folder:
        os.chmod(folder, 0o700)
        name = os.path.join(folder, "page.pdf")
        fd = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
        try:
            res = run([tool, "-layout", "-enc", "UTF-8", name, "-"], stdin=subprocess.DEVNULL, capture_output=True,
                      timeout=timeout, check=False)
        except subprocess.TimeoutExpired:
            raise DocError("refused: the PDF took longer than %d s to read" % int(timeout)) from None
        except OSError:
            raise DocError("failed: the PDF reader (pdftotext of the poppler tools) cannot be run") from None
    err = (res.stderr or b"").decode("utf-8", "replace").lower()
    if res.returncode != 0:
        if "password" in err or "encrypt" in err:
            raise DocError("refused: an encrypted PDF")
        raise DocError("refused: a PDF the reader cannot read")
    out = res.stdout or b""
    if len(out) > PDF_TEXT_MAX:
        raise DocError("refused: the text of the PDF is larger than %d MB" % (PDF_TEXT_MAX // (1024 * 1024)))
    pages = out.decode("utf-8", "replace").split("\f")      # pdftotext ends every page with a form feed
    if pages and not pages[-1].strip():
        pages = pages[:-1]
    count = len(pages)
    chars = sum(len(re.sub(r"\s", "", p)) for p in pages)
    if count == 0 or chars < PDF_MIN_CHARS * count:
        raise DocError("refused: a PDF without a text layer (a scan); it has no text to read")
    text = "\n\n".join("--- page %d ---\n%s" % (n, _plain(p).strip("\n")) for n, p in enumerate(pages, 1))
    return text + "\n", count


def fetch(url: str, *, getter=https_get, hosts: list[str] | None = None, cap: int = MAX_BYTES,
          pdf_cap: int = MAX_PDF_BYTES, pdf_runner=None, links: bool = False, index: bool = False) -> dict:
    """The page of a checked URL as text: {"url", "host", "path", "kind", "status", "size", "text", "pages"} (pages
    for a PDF, else None). DocError naming the rule otherwise (with "host" and "path" of the last URL it got to, for
    the ledger)."""
    hosts = current_hosts() if hosts is None else hosts
    host, path = check_url(url, hosts)
    for hop in range(MAX_REDIRECTS + 1):
        try:
            status, headers, resp = getter(host, path)
        except Exception:   # A getter's exceptions can contain request or remote values, including DocError.
            raise _at(DocError("failed: no answer from the host"), host, path) from None
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
            kind = TEXT_TYPES.get(ctype) or ("pdf" if ctype in PDF_TYPES else "octet" if ctype in OCTET_TYPES else None)
            if index:
                kind = INDEX_TYPES.get(ctype) or kind
            if kind is None:
                raise DocError("refused: not a text type (HTML, Markdown, plain text, JSON or PDF)")
            encoding = (headers.get("content-encoding") or "identity").strip().lower()
            if encoding not in ("identity", "gzip"):
                raise DocError("refused: an encoded body (the reader asks for identity)")
            limit = pdf_cap if kind in ("pdf", "octet") else cap
            length = headers.get("content-length")
            if length is not None and (not re.fullmatch(r"[0-9]{1,20}", length.strip()) or int(length) > limit):
                raise DocError("refused: the page is larger than %d MB" % (limit // (1024 * 1024)))
            body = _read_capped(resp, limit)
            if encoding == "gzip":
                body = _gunzip_capped(body, limit)
            if kind in ("pdf", "octet"):
                if not body.startswith(b"%PDF-"):
                    raise DocError("refused: not a text type (HTML, Markdown, plain text, JSON or PDF)"
                                   if kind == "octet" else "refused: the body is not a PDF")
                text, pages = pdf_text(body, runner=pdf_runner)
                return {"url": url, "host": host, "path": path, "kind": "pdf", "status": status, "size": len(body),
                        "text": text, "pages": pages}
            m = re.search(r"charset=([A-Za-z0-9_.:-]{1,40})", headers.get("content-type") or "")
            try:
                page = body.decode(m.group(1) if m else "utf-8", errors="replace")
            except LookupError:
                page = body.decode("utf-8", errors="replace")
            if index:
                return {"url": url, "host": host, "path": path, "kind": kind, "status": status, "size": len(body),
                        "text": page, "pages": None}
            text = html_text(page) if kind == "html" else _plain(page)
            root = None
            if links and kind == "html":
                lines, root = page_links(page, url, hosts)
                text = text.rstrip("\n") + "\n\n## Links (%d)\n\n%s\n" % (len(lines), "\n".join(lines) or "(none)")
                if root:
                    text += ("\nThis page belongs to a MadCap Flare help system (root %s). Its pages do not link each "
                             "other: awb doc find %s WORDS lists every page whose title holds the words.\n" % (root, url))
            return {"url": url, "host": host, "path": path, "kind": kind, "status": status, "size": len(body),
                    "text": _plain(text), "pages": None, "help_root": root}
        except DocError as err:
            raise _at(err, host, path) from None
        except Exception:
            raise _at(DocError("failed: the documentation response cannot be read"), host, path) from None
        finally:
            try:
                resp.close()
            except Exception:
                pass
    raise _at(DocError("refused: more than %d redirects" % MAX_REDIRECTS), host, path)


_TOC_ATTR_RE = re.compile(r'\bToc="([^"]+)"')
_NUMCHUNKS_RE = re.compile(r"numchunks:\s*(\d+)")
_PREFIX_RE = re.compile(r"prefix:\s*'([A-Za-z0-9_.-]+)'")
_ENTRY_RE = re.compile(r"'(/[^'\\]+)':\{i:\[[0-9,]*\],t:\['((?:[^'\\]|\\.)*)'")
MAX_CHUNKS = 20


def find(url: str, words: str, *, fetcher) -> dict:
    """The pages of the MadCap Flare help system of the page at `url` whose title or path holds every word.
    `fetcher(url, **kw)` is fetch with the getter, the hosts, the rate and the ledger of the caller. Returns
    {"url", "root", "text", "count"}; DocError when the page names no help index or the index cannot be read."""
    if not isinstance(words, str) or not _WORDS_RE.match(words.strip()):
        raise DocError("refused: WORDS are 2 to 120 letters, digits, spaces and . _ / & + -")
    terms = [w.lower() for w in words.split()]
    page = fetcher(url, links=True)
    root = page.get("help_root")
    if not root:
        raise DocError("refused: the page names no help index (only a MadCap Flare help system has one); "
                       "use awb doc fetch URL --links")
    root = root if root.endswith("/") else root + "/"
    system = fetcher(root + "Data/HelpSystem.xml", index=True)["text"]
    m = _TOC_ATTR_RE.search(system)
    if not m:
        raise DocError("failed: the help system names no table of contents")
    toc_url = urljoin(root, m.group(1))
    toc = fetcher(toc_url, index=True)["text"]
    n, prefix = _NUMCHUNKS_RE.search(toc), _PREFIX_RE.search(toc)
    if not (n and prefix):
        raise DocError("failed: the table of contents has no chunk list")
    found, seen = [], set()
    for k in range(min(int(n.group(1).lstrip("0")[:8] or "0"), MAX_CHUNKS)):
        chunk = fetcher(urljoin(toc_url, "%s%d.js" % (prefix.group(1), k)), index=True)["text"]
        for e in _ENTRY_RE.finditer(chunk):
            path, title = e.group(1), re.sub(r"\\(.)", r"\1", e.group(2)).strip()
            hay = (title + " " + path).lower()
            if path not in seen and all(t in hay for t in terms):
                seen.add(path)
                found.append("- %s: %s" % (title or "(no title)", root.rstrip("/") + path))
    head = "%d page%s of the help at %s match \"%s\"" % (len(found), "" if len(found) == 1 else "s", root,
                                                       " ".join(words.split()))
    if len(found) > MAX_FOUND:
        head += " (the first %d shown; add a word)" % MAX_FOUND
    body = "\n".join(found[:MAX_FOUND]) or "(none: try fewer or other words)"
    return {"url": page["url"], "root": root, "text": "# %s\n\n%s\n" % (head, body), "count": len(found)}


def _at(err: DocError, host: str, path: str) -> DocError:
    err.host, err.path = host, path
    return err


def serve(req: dict, project, limiter: Limiter, ledger_dir, *, getter=https_get, hosts=None, tmp_dir=None):
    """The op doc_get of the key service. Returns (answer, file of the text to stream) or (answer, None)."""
    import tempfile

    if not isinstance(req, dict) or type(req.get("links", False)) is not bool:
        return {"ok": False, "error": "refused: invalid documentation request"}, None
    url = req.get("url")
    if not (isinstance(project, str) and _PROJECT_RE.fullmatch(project)):
        return {"ok": False, "error": "refused: a fetch runs for an active project (SCOPE.md of the folder)"}, None
    hosts = current_hosts() if hosts is None else hosts
    try:
        host, path = check_url(url, hosts)
    except DocError as err:
        ledger_line(ledger_dir, project, "-", "-", None, "refused")
        return {"ok": False, "error": str(err)}, None
    try:
        stop = limiter.take(project)
    except DocError as err:
        return {"ok": False, "error": str(err)}, None
    if stop:
        ledger_line(ledger_dir, project, host, path, None, "rate")
        return {"ok": False, "error": stop}, None
    words = req.get("find")

    def one(u, **kw):
        """A fetch of the request: the first is counted above, every further file of a find takes the rate too."""
        if one.n:
            stop = limiter.take(project)
            if stop:
                raise DocError(stop)
        one.n += 1
        try:
            got = fetch(u, getter=getter, hosts=hosts, **kw)
        except DocError as err:
            ledger_line(ledger_dir, project, getattr(err, "host", host), getattr(err, "path", path), None,
                        str(err).split(":", 1)[0])
            raise
        ledger_line(ledger_dir, project, got["host"], got["path"], got["size"], str(got["status"]))
        return got
    one.n = 0

    try:
        if words is not None:
            res = find(url, words, fetcher=one)
            page = {"url": res["url"], "kind": "find", "size": res["count"], "text": res["text"]}
        else:
            page = one(url, links=bool(req.get("links")))
    except DocError as err:
        return {"ok": False, "error": str(err)}, None
    why = private_reason(page["text"])
    if why is not None and why.startswith("a secret"):
        masked, n = mask_secret_lines(page["text"])
        lines = sum(1 for x in page["text"].split("\n") if x.strip())
        # a page that is mostly secret-like values stays withheld whole: masking is for a sample line in a long page
        if n and n * 3 <= lines and private_reason(masked) is None:
            page = {**page, "text": masked + "\n\n%d line(s) withheld: a secret-like value of the page\n" % n}
            why = None
    if why is not None:
        return {"ok": False, "error": "refused: private documentation text withheld (%s)" % why}, None
    text = page["text"]
    data = text.encode("utf-8")
    name = None
    try:
        fd, name = tempfile.mkstemp(prefix="awb-doc-", dir=tmp_dir)
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
    except OSError:
        if name is not None:
            with contextlib.suppress(OSError):
                os.unlink(name)
        return {"ok": False, "error": "failed: the documentation stream cannot be written"}, None
    fetched = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    return {"ok": True, "url": page["url"], "kind": page["kind"], "size": page["size"], "fetched": fetched,
            "pages": page.get("pages")}, Path(name)


# --------------------------------------------------------------------------- the session side


STEERING = "Steering: allow documentation host %s (vendor, why)"
"""The owner item a session leaves in OPEN.md under the owner heading for a host the list lacks, never a question."""
OFF_LIST = "not on rules/allow-domains.txt"


def _refusal(line: str, url: str) -> str:
    """The one line of a refusal; for a host off the list it names the owner item to leave in OPEN.md."""
    line = (_public(line, "refused: the key service said no")
            if isinstance(line, str) and not any(c in line for c in "\n\r\t")
            else "refused: the key service said no")
    if OFF_LIST not in line:
        return line
    try:
        host = (urlsplit(url).hostname or "").lower()
    except ValueError:
        host = ""
    host = _public(host, "HOST") if _HOST_RE.fullmatch(host) else "HOST"
    return "%s; leave one owner item in OPEN.md under the owner heading: %s" % (line, STEERING % host)


def header(url: str, fetched: str, kind: str | None = None, pages=None) -> str:
    line = "source: %s, fetched %s" % (url, fetched)
    if kind == "pdf" and isinstance(pages, int):
        line += ", pdf, %d page%s" % (pages, "" if pages == 1 else "s")
    return line


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
    except (xchg.XchgError, OSError):
        print("refused: the project cannot be read", file=sys.stderr)
        return 2
    out = None
    if args.out:
        out = Path(os.path.abspath(args.out if os.path.isabs(args.out) else Path.cwd() / args.out))
        real_root = Path(os.path.realpath(root))
        real_parent = Path(os.path.realpath(out.parent))
        if not (real_parent == real_root or real_root in real_parent.parents) or os.path.islink(out):
            print("refused: --out names a file inside the project folder", file=sys.stderr)
            return 2
    try:
        fd, tmp = tempfile.mkstemp(prefix="awb-doc-")
        os.close(fd)
    except OSError:
        print("failed: the documentation stream cannot be created", file=sys.stderr)
        return 2
    try:
        req = {"op": "doc_get", "url": args.url, "project": code}
        if getattr(args, "links", False):
            req["links"] = True
        if getattr(args, "words", None):
            req["find"] = " ".join(args.words)
        try:
            answer = keys.request(keys.call_socket(), req, timeout=TIMEOUT * (MAX_REDIRECTS + 1) + 10,
                                  download=Path(tmp))
        except (keys.KeysError, OSError):
            print("failed: the key service request failed", file=sys.stderr)
            return 2
        if not isinstance(answer, dict) or type(answer.get("ok")) is not bool:
            print("failed: the key service gave an invalid answer", file=sys.stderr)
            return 2
        if not answer.get("ok"):
            print(_refusal(answer.get("error") or "refused: the key service said no", args.url), file=sys.stderr)
            return 1
        body = _read_file(Path(tmp), PDF_TEXT_MAX + MAX_BYTES)
    except (OSError, UnicodeError):
        print("failed: the documentation stream cannot be read", file=sys.stderr)
        return 2
    finally:
        try:
            os.unlink(tmp)
        except OSError:
            pass
    source, fetched = answer.get("url") or args.url, answer.get("fetched") or "?"
    try:
        check_url(source)
        if not isinstance(fetched, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2} \d{2}:\d{2} UTC|\?", fetched):
            raise DocError("invalid date")
        if answer.get("kind") == "pdf" and (type(answer.get("pages")) is not int or
                                             not 1 <= answer["pages"] <= 1000000):
            raise DocError("invalid page count")
    except DocError:
        print("failed: the key service gave invalid source metadata", file=sys.stderr)
        return 2
    head = header(source, fetched, answer.get("kind"), answer.get("pages"))
    text = head + "\n\n" + body
    why = private_reason(text)
    if why is not None:
        print("refused: private documentation text withheld (%s)" % why, file=sys.stderr)
        return 1
    if out is None:
        sys.stdout.write(text)
        return 0
    try:
        _publish(root, out, text)
    except OSError:
        print("failed: the output file cannot be written safely", file=sys.stderr)
        return 2
    label = _public(os.path.relpath(out, root), "[file]")
    if any(c in label for c in "\n\r\t"):
        label = "[file]"
    print("%s: %d characters, %s" % (label, len(text), head))
    return 0


def _publish(root: Path, out: Path, text: str) -> None:
    """Publish a complete private file in the project, never following an output link."""
    import secrets

    # Recheck after the download, then walk from the root using directory descriptors.
    relative = out.relative_to(Path(os.path.abspath(root)))
    if ".." in relative.parts:
        raise OSError("outside project")
    with _directory(root) as directory:
        parent = os.dup(directory)
        tmp = ".awb-doc-" + secrets.token_hex(16)
        try:
            for part in relative.parts[:-1]:
                try:
                    os.mkdir(part, 0o700, dir_fd=parent)
                except FileExistsError:
                    pass
                child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent)
                os.close(parent)
                parent = child
            try:
                with _regular(parent, relative.name):
                    pass
            except FileNotFoundError:
                pass
            with _regular(parent, tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL) as fd:
                with os.fdopen(os.dup(fd), "w", encoding="utf-8") as stream:
                    stream.write(text)
                    stream.flush()
                    os.fsync(stream.fileno())
            os.replace(tmp, relative.name, src_dir_fd=parent, dst_dir_fd=parent)
        finally:
            with contextlib.suppress(FileNotFoundError):
                os.unlink(tmp, dir_fd=parent)
            os.close(parent)


def _hosts_cmd(args) -> int:
    rest = list(getattr(args, "rest", None) or [])
    if rest:
        if rest[0] != "add" or len(rest) != 2:
            print("usage: awb doc hosts [add HOST [--why TEXT]]", file=sys.stderr)
            return 2
        return _add_cmd(rest[1], getattr(args, "why", None))
    hosts = allowed_hosts()
    extra = [h for h in extra_hosts() if h not in hosts]
    if not hosts and not extra:
        print("no host: rules/allow-domains.txt is empty or missing", file=sys.stderr)
        return 1
    print("\n".join(_public(h, "[host withheld]") for h in
                    hosts + ["%s  (added on the host, not in the repository)" % h for h in extra]))
    return 0


WHY_MAX = 120


def check_new_host(host: str, listed: list[str] | None = None) -> str:
    """The host to add, lower case, after the checks the repository list has; DocError naming the rule otherwise.
    Takes a bare host or an https URL of one."""
    import ipaddress

    from awb import vendorhosts

    if not isinstance(host, str):
        raise DocError("refused: the host is not a plain host name")
    text = host.strip().lower()
    if "://" in text:
        if not text.startswith("https://"):
            raise DocError("refused: https only")
        text = text[len("https://"):].rstrip("/")
    if "*" in text:
        raise DocError("refused: no wildcard, one exact host")
    try:
        ipaddress.ip_address(text.strip("[]"))
    except ValueError:
        pass
    else:
        raise DocError("refused: an address is not a host name")
    if re.fullmatch(r"[0-9.]+", text):
        raise DocError("refused: an address is not a host name")
    if not _HOST_RE.fullmatch(text):
        raise DocError("refused: the host is not a plain host name (no path, port, user info or space)")
    # Every TCP API host ends in .otc.t-systems.com (the probe host of build_probe too); importing build_probe here
    # would tie every service that reads this module to the build runner in the deploy map.
    if text.endswith(".otc.t-systems.com") and not text.startswith("docs"):
        raise DocError("refused: a TCP API host is never a documentation host")
    if text in vendorhosts.read()[1]:
        raise DocError("refused: a portal host of rules/vendor-hosts.txt")
    if text in (current_hosts() if listed is None else listed):
        raise DocError("refused: the host is already on the list")
    if not _public(text, ""):
        raise DocError("refused: private host withheld")
    return text


def _resolves(host: str) -> bool:
    try:
        socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)
    except (OSError, UnicodeError):
        return False
    return True


def sibling_hosts(host: str, *, resolves=None) -> list[str]:
    """The www. and docs. hosts of the same domain that pass the checks and resolve."""
    resolves = resolves or _resolves
    domain = host.split(".", 1)[1] if host.split(".", 1)[0] in ("www", "docs") else host
    out = []
    for prefix in ("www", "docs"):
        name = "%s.%s" % (prefix, domain)
        if name == host:
            continue
        try:
            check_new_host(name)
        except DocError:
            continue
        if resolves(name):
            out.append(name)
    return out


def add_host(host: str, why: str | None = None, *, path: Path | None = None) -> str:
    """Append HOST to the owner-side file after the checks; returns the host. DocError when refused."""
    from awb import config

    if config.is_work_user():
        raise DocError("refused: the work user cannot add a host; leave the owner item (awb doc fetch names it)")
    f = path or extra_file()
    if f is None:
        raise DocError("refused: paths.conf has no line %s = /ABSOLUTE/FILE" % EXTRA_KEY)
    name = check_new_host(host, allowed_hosts())
    if why is not None and not isinstance(why, str):
        raise DocError("refused: --why must be text")
    note = " ".join((why or "").split())
    if len(note) > WHY_MAX or any(ord(c) < 32 or ord(c) > 126 for c in note):
        raise DocError("refused: --why is plain ASCII of at most %d characters" % WHY_MAX)
    if _public(note, "[withheld]") != note:
        raise DocError("refused: private host note withheld")
    line = "%s  # %s%s\n" % (name, datetime.now(timezone.utc).strftime("%Y-%m-%d"), " " + note if note else "")
    try:
        f = Path(f)
        with _directory(f.parent) as directory, _regular(directory, f.name, os.O_RDWR | os.O_CREAT, 0o644) as fd:
            fcntl.flock(fd, fcntl.LOCK_EX)
            with os.fdopen(os.dup(fd), "rb") as fh:
                old = fh.read(1024 * 1024 + 1)
            addition = (("\n" if old and not old.endswith(b"\n") else "") + line).encode("utf-8")
            if len(old) + len(addition) > 1024 * 1024:
                raise OSError("file too large")
            listed = [row.split("#", 1)[0].strip().lower() for row in old.decode("utf-8").splitlines()]
            check_new_host(host, allowed_hosts() + listed)
            _append(fd, addition)
    except (OSError, UnicodeError):
        raise DocError("refused: the file of %s cannot be written" % EXTRA_KEY) from None
    return name


def _add_cmd(host: str, why: str | None) -> int:
    try:
        name = add_host(host, why)
    except DocError as err:
        print(str(err), file=sys.stderr)
        return 1
    print("added %s; the key service reads it at the next fetch" % name)
    for other in sibling_hosts(name):
        print("also resolves: %s (awb doc hosts add %s)" % (other, other))
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
    f.add_argument("--links", action="store_true", help="append the links of the page as absolute URLs with their "
                                                        "texts (a host off the list is marked)")
    f.set_defaults(func=_fetch_cmd)
    d = sub.add_parser("find", help="the pages of the help system of a page (MadCap Flare) whose title or path holds "
                                    "every word, as titles with their URLs")
    d.add_argument("url", metavar="URL", help="any page of the help, https://HOST/PATH on an allowed host")
    d.add_argument("words", metavar="WORDS", nargs="+", help="words that must all be in the title or the path")
    d.add_argument("--out", metavar="FILE", help="write the list to FILE inside the project folder")
    d.set_defaults(func=_fetch_cmd)
    h = sub.add_parser("hosts", help="the hosts a page may come from (rules/allow-domains.txt and the owner-side "
                                     "file); `add HOST [--why TEXT]` allows one more, owner side")
    h.add_argument("rest", nargs="*", metavar="add HOST")
    h.add_argument("--why", metavar="TEXT", help="a short reason written beside the host")
    h.set_defaults(func=_hosts_cmd)
    args = ap.parse_args(argv)
    if not getattr(args, "func", None):
        ap.print_usage(sys.stderr)
        return 2
    return int(args.func(args))


if __name__ == "__main__":
    sys.exit(main())
