"""PDF files, read with the poppler command line tools pdftotext and pdfinfo.

`pdfinfo` gives the page count, the document information dictionary (standard and custom keys), the XMP
metadata stream and the link targets. `pdftotext -layout` gives the text, one call per page with -f and -l,
so that a page without a text layer can be named. Layout mode keeps the words of one printed line on one
line. A name whose halves sit in two columns stays together with only spaces between the halves. The
matcher treats any whitespace run inside a form as a match.

A second pass without -layout gives the reading order (one column after the other, hyphenation at a line
end rejoined by poppler). It goes into `detect_text` only, so that a name broken over two lines inside a
column is still seen.

A page with fewer than MIN_PAGE_CHARS characters of text is noted. When every page is like that the file
is `unreadable` (a scan without a text layer). Missing poppler tools give `failed`. The error output of
the tools is never kept, it can carry the file name. A document with more than MAX_PAGE_CALLS pages is read
with one pdftotext call split at the page breaks, because every call parses the whole file again.

A second pass reads the file itself: every string of every object, also inside compressed streams and
object streams, goes to `scan_text`, which is checked for registered forms only. The strings behind the keys
a viewer shows without rendering them as page text (annotation contents and subjects, form field values,
outline titles, JavaScript) go to `detect_text` as well, with a note.

Files embedded in the PDF (attachments, the documents of a portfolio) are saved with pdfdetach into a private
temporary folder and read as children through `awb.extract.extract`, like the members of an archive.
"""
from __future__ import annotations

import html
import os
import re
import shutil
import subprocess
import time
import zlib
from pathlib import Path

from awb import extract as _extract
from awb.extract import MAX_ARCHIVE_DEPTH, Extraction, make_temp_dir

MIN_PAGE_CHARS = 40
"""A page with fewer characters than this (whitespace not counted) has no extractable text."""

TIMEOUT = 120
"""Seconds one poppler call may take."""

MAX_PAGE_CALLS = 100
"""Up to this many pages, pdftotext runs once per page; above it once for the whole document."""
DEADLINE = 600
"""Seconds all calls per page may take together; pages after it are noted as not read."""
MAX_RAW_BYTES = 64 * 1024 * 1024
"""Bytes of the file and of decompressed streams the second pass reads, all together."""
MAX_STREAMS = 5000

PAGE_NOTE = "page %d has no extractable text, review the original"
ALL_EMPTY_NOTE = "no page has extractable text, review the original"
MISSING_NOTE = "poppler tools pdftotext and pdfinfo are missing, install poppler-utils"

_STANDARD_KEYS = {
    "Title": "title",
    "Author": "author",
    "Subject": "subject",
    "Keywords": "keywords",
    "Creator": "creator",
    "Producer": "producer",
    "CreationDate": "created",
    "ModDate": "modified",
}
_INFO_LINE_RE = re.compile(r"^([^\s:]+):(?:[ \t]+(.*))?$")
_XMP_ATTR_RE = re.compile(r"""([\w.-]+(?::[\w.-]+)?)\s*=\s*("([^"]*)"|'([^']*)')""")
_XMP_TAG_RE = re.compile(r"<[^>]*>")
_WS_RE = re.compile(r"\s+")
_BLANK_RUN_RE = re.compile(r"\n{3,}")


class _Missing(Exception):
    """A poppler tool is not installed."""


class _Tools:
    def __init__(self) -> None:
        self.pdftotext = shutil.which("pdftotext")
        self.pdfinfo = shutil.which("pdfinfo")
        self.pdfdetach = shutil.which("pdfdetach")

    @property
    def present(self) -> bool:
        return bool(self.pdftotext and self.pdfinfo)


def _run(args: list[str]) -> tuple[int, str, str]:
    """Run one tool. Returns (exit code, stdout, stderr). Stderr is only looked at, never kept."""
    try:
        proc = subprocess.run(
            args,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=TIMEOUT,
            check=False,
        )
    except FileNotFoundError:
        raise _Missing() from None
    out = proc.stdout.decode("utf-8", errors="replace")
    err = proc.stderr.decode("utf-8", errors="replace")
    return proc.returncode, out, err


# --------------------------------------------------------------------------- pdfinfo


def _parse_info(out: str) -> list[tuple[str, str]]:
    """Key and value pairs of pdfinfo output. A line that is not `Key: value` continues the value before it."""
    items: list[list[str]] = []
    for line in out.replace("\r\n", "\n").split("\n"):
        m = _INFO_LINE_RE.match(line)
        if m:
            items.append([m.group(1), m.group(2) or ""])
        elif items:
            items[-1][1] += "\n" + line
        elif line.strip():
            items.append(["", line])
    return [(k, v.strip()) for k, v in items]


def _last_field(out: str, key: str) -> str | None:
    """The value of the last `key:` line. Standard fields come after the information dictionary, so the
    last one is the real one even when a title carries a line that looks like a field."""
    found = re.findall(r"^%s:[ \t]+(.*)$" % re.escape(key), out, re.MULTILINE)
    return found[-1].strip() if found else None


def _xmp_values(xmp: str) -> list[str]:
    """Attribute values and text nodes of an XMP packet, without namespace declarations."""
    values: list[str] = []
    body = re.sub(r"<\?.*?\?>", " ", xmp, flags=re.DOTALL)
    for tag in re.findall(r"<[^>]*>", body):
        for m in _XMP_ATTR_RE.finditer(tag):
            name = m.group(1).lower()
            if name.startswith("xmlns") or name in ("rdf:about", "rdf:parsetype", "x:xmptk"):
                continue
            v = m.group(3) if m.group(3) is not None else m.group(4)
            v = html.unescape(v or "").strip()
            if v:
                values.append(v)
    for chunk in _XMP_TAG_RE.split(body):
        v = _WS_RE.sub(" ", html.unescape(chunk)).strip()
        if v:
            values.append(v)
    return values


def _links(out: str) -> list[str]:
    """Link targets from `pdfinfo -url`: rows of page number, type and URL under a header line."""
    urls: list[str] = []
    for line in out.splitlines():
        cols = line.split(None, 2)
        if len(cols) == 3 and cols[0].isdigit() and cols[2].strip():
            urls.append(cols[2].strip())
    return urls


# --------------------------------------------------------------------------- pdftotext


def _clean(raw: str) -> str:
    """Drop the page break, trailing spaces of every line and runs of blank lines."""
    lines = [line.rstrip() for line in raw.replace("\r\n", "\n").replace("\f", "\n").split("\n")]
    return _BLANK_RUN_RE.sub("\n\n", "\n".join(lines)).strip("\n")


def _chars(text: str) -> int:
    return len(_WS_RE.sub("", text))


def _page_text(tools: _Tools, target: str, n: int) -> str | None:
    """Layout text of page n. None when pdftotext failed on it."""
    try:
        code, out, _ = _run([tools.pdftotext, "-layout", "-enc", "UTF-8", "-f", str(n), "-l", str(n), target, "-"])
    except subprocess.TimeoutExpired:
        return None
    return _clean(out) if code == 0 else None


def _split_pages(tools: _Tools, target: str) -> list[str | None] | None:
    """Fallback when pdfinfo gave no page count: the whole document once, split at the page breaks."""
    try:
        code, out, _ = _run([tools.pdftotext, "-layout", "-enc", "UTF-8", target, "-"])
    except subprocess.TimeoutExpired:
        return None
    if code != 0:
        return None
    pages = out.split("\f")
    if pages and not pages[-1].strip():
        pages.pop()
    return [_clean(p) for p in pages]


# --------------------------------------------------------------------------- entry point


def extract(path: Path, depth: int = 0) -> Extraction:
    """Read a PDF file. Never raises, a problem becomes state failed or unreadable with a note."""
    path = Path(path)
    ex = Extraction(path, "pdf", "ok")
    tools = _Tools()
    if not tools.present:
        ex.state = "failed"
        ex.notes.append(MISSING_NOTE)
        return ex
    target = str(path.resolve())
    try:
        return _read(ex, tools, target, depth)
    except _Missing:
        return Extraction(path, "pdf", "failed", notes=[MISSING_NOTE])
    except subprocess.TimeoutExpired:
        return Extraction(path, "pdf", "failed", notes=["pdfinfo timed out, review the original"])
    except OSError as err:
        return Extraction(path, "pdf", "failed", notes=["%s while running poppler" % type(err).__name__])


def _read(ex: Extraction, tools: _Tools, target: str, depth: int = 0) -> Extraction:
    code, info_out, info_err = _run([tools.pdfinfo, "-enc", "UTF-8", target])
    if code != 0:
        ex.state = "failed"
        if "password" in info_err.lower():
            ex.notes.append("pdf is encrypted with a password, review the original")
        else:
            ex.notes.append("pdf could not be opened (exit code %d), review the original" % code)
        return ex

    det_meta: list[str] = []
    _read_metadata(ex, tools, target, det_meta)

    pages_value = _last_field(info_out, "Pages")
    page_count = int(pages_value) if pages_value and pages_value.isdigit() else 0
    encrypted = _last_field(info_out, "Encrypted") or ""
    ex.meta["encrypted"] = encrypted.lower().startswith("yes")
    form = (_last_field(info_out, "Form") or "none").lower()
    if form not in ("none", ""):
        ex.notes.append("pdf has form fields, their values are not extracted, review the original")

    if 0 < page_count <= MAX_PAGE_CALLS:
        pages = []
        started = time.monotonic()
        for n in range(1, page_count + 1):
            if time.monotonic() - started > DEADLINE:
                ex.meta["incomplete"] = True
                pages.append(None)
                continue
            pages.append(_page_text(tools, target, n))
    elif page_count > MAX_PAGE_CALLS:
        pages = _split_pages(tools, target) or [None] * page_count
        if len(pages) < page_count:
            pages += [None] * (page_count - len(pages))
    else:
        pages = _split_pages(tools, target) or []
        page_count = len(pages)
    ex.meta["page_count"] = page_count

    failed_pages = 0
    empty_pages = 0
    kept: list[str] = []
    for n, page in enumerate(pages, start=1):
        if page is None:
            failed_pages += 1
            empty_pages += 1
            ex.notes.append("page %d could not be read, review the original" % n)
            continue
        if _chars(page) < MIN_PAGE_CHARS:
            empty_pages += 1
            ex.notes.append(PAGE_NOTE % n)
        if page.strip():
            kept.append(page)
    ex.text = "\n\n".join(kept)

    reading = _reading_order(tools, target)
    if reading is None:
        ex.notes.append("reading order pass failed, detection sees the layout text only")

    parts = [ex.text]
    if reading:
        parts.append(reading)
    parts.extend(det_meta)
    try:
        plain, keyed = raw_strings(Path(target)), keyed_strings(Path(target))
    except (ValueError, MemoryError, OSError, zlib.error):
        plain, keyed = [], []
        ex.meta["incomplete"] = True
        ex.notes.append("the objects of the file could not be read for the raw check")
    if keyed:
        parts.append("annotations, fields and outline:\n" + "\n".join(keyed))
        shown = [k for k in keyed if k.split(":", 1)[0] in _NOTED_KEYS]
        if shown:
            ex.notes.append("%d string(s) of annotations, form fields or scripts seen by detection only"
                            % len(shown))
    ex.detect_text = "\n".join(p for p in parts if p and p.strip())
    ex.scan_text = "\n".join(plain)
    _attachments(ex, tools, target, depth)

    if page_count == 0:
        ex.state = "failed" if pages_value is None else "unreadable"
        ex.notes.append("pdf has no pages that could be read, review the original")
    elif failed_pages == page_count:
        ex.state = "failed"
    elif empty_pages == page_count:
        ex.state = "unreadable"
        ex.notes.append(ALL_EMPTY_NOTE)
    return ex


MAX_ATTACHMENTS = 200
"""Embedded files read from one PDF; the rest is counted in a note."""
_LIST_LINE_RE = re.compile(r"^(\d+):\s*(.*)$")


def _attachments(ex: Extraction, tools: _Tools, target: str, depth: int) -> None:
    """Embedded files saved with pdfdetach into a private temporary folder and read as children.

    Each file is saved by its number to a path this reader chooses (`-save N -o FILE`), never under the name the
    PDF gives it: pdfdetach honours a name such as ../../x in -saveall and would write outside the folder with
    the rights of the owner (found by the review of 2026-09-27). The name from the listing is kept in the
    child's meta only, on the vault side."""
    if not tools.pdfdetach:
        return
    try:
        code, out, _ = _run([tools.pdfdetach, "-list", target])
    except subprocess.TimeoutExpired:
        return
    if code != 0 or not out.strip():
        return
    names: list[tuple[int, str]] = []
    for line in out.splitlines():
        m = _LIST_LINE_RE.match(line.strip())
        if m:
            names.append((int(m.group(1)), m.group(2)))
    if not names:
        return
    if depth >= MAX_ARCHIVE_DEPTH:
        ex.meta["incomplete"] = True
        ex.notes.append("%d embedded file(s) not read, nested too deep" % len(names))
        return
    folder = make_temp_dir("awb-pdf-")
    saved = 0
    try:
        for n, name in names[:MAX_ATTACHMENTS]:
            suffix = Path(name).suffix.lower()
            neutral = folder / ("attachment-%d%s" % (n, suffix if re.fullmatch(r"\.[A-Za-z0-9]{1,8}", suffix) else ""))
            try:
                code, _, _ = _run([tools.pdfdetach, "-save", str(n), "-o", str(neutral), target])
            except subprocess.TimeoutExpired:
                code = -1
            # the file must be the one this reader named, inside its own folder, and no link
            if code != 0 or not neutral.is_file() or neutral.is_symlink() \
                    or Path(os.path.realpath(neutral)).parent != Path(os.path.realpath(folder)):
                continue
            child = _extract.extract(neutral, depth + 1)
            child.meta["attachment"] = name
            ex.children.append(child)
            saved += 1
        stray = [f for f in folder.iterdir() if not f.name.startswith("attachment-")]
        if stray:
            ex.meta["incomplete"] = True
            ex.notes.append("%d file(s) the tool wrote under other names were left unread" % len(stray))
        ex.notes.append("%d embedded file(s) read as attachments" % saved)
        if saved < len(names):
            ex.meta["incomplete"] = True
            ex.notes.append("%d embedded file(s) could not be saved or were over the limit" % (len(names) - saved))
    finally:
        shutil.rmtree(folder, ignore_errors=True)


def _reading_order(tools: _Tools, target: str) -> str | None:
    try:
        code, out, _ = _run([tools.pdftotext, "-enc", "UTF-8", target, "-"])
    except subprocess.TimeoutExpired:
        return None
    return _clean(out) if code == 0 else None


def _read_metadata(ex: Extraction, tools: _Tools, target: str, det: list[str]) -> None:
    """Information dictionary (every key), XMP packet and link targets into meta and detection lines."""
    try:
        code, out, _ = _run([tools.pdfinfo, "-custom", "-isodates", "-enc", "UTF-8", target])
    except subprocess.TimeoutExpired:
        code, out = -1, ""
    if code == 0:
        for key, value in _parse_info(out):
            if not value:
                continue
            std = _STANDARD_KEYS.get(key)
            if std:
                ex.meta[std] = value
            elif key:
                ex.meta.setdefault("custom", {})[key] = value
            det.append("%s: %s" % (std or key.lower() or "info", value))
    else:
        ex.notes.append("document information could not be read")

    try:
        code, out, _ = _run([tools.pdfinfo, "-meta", target])
    except subprocess.TimeoutExpired:
        code, out = -1, ""
    if code == 0 and out.strip():
        values = _xmp_values(out)
        ex.meta["xmp"] = True
        det.extend("xmp: %s" % v for v in values)
    elif code != 0:
        ex.notes.append("xmp metadata could not be read")

    try:
        code, out, _ = _run([tools.pdfinfo, "-url", target])
    except subprocess.TimeoutExpired:
        code, out = -1, ""
    if code == 0:
        urls = _links(out)
        if urls:
            ex.meta["link_count"] = len(urls)
            det.extend("link: %s" % u for u in urls)
    else:
        ex.notes.append("link targets could not be listed")


# --------------------------------------------------------------------------- second pass: strings of every object

_STREAM_RE = re.compile(rb"stream\r?\n")
_LITERAL_RE = re.compile(rb"\((?:\\.|[^\\()]|\((?:\\.|[^\\()])*\))*\)", re.S)
_HEXSTR_RE = re.compile(rb"(?<!<)<([0-9A-Fa-f\s]{4,})>(?!>)")
_NAME_RE = re.compile(rb"/([^\s/<>\[\]()%{}]*#[0-9A-Fa-f]{2}[^\s/<>\[\]()%{}]*)")
_ESCAPES = {b"n": b"\n", b"r": b"\r", b"t": b"\t", b"b": b"\b", b"f": b"\f", b"(": b"(", b")": b")", b"\\": b"\\"}


def _pdf_string(raw: bytes) -> str:
    """A PDF string's bytes as text: UTF-16 with a BOM, UTF-8 with a BOM, else PDFDocEncoding (Latin-1)."""
    if raw.startswith(b"\xfe\xff"):
        return raw[2:].decode("utf-16-be", errors="replace")
    if raw.startswith(b"\xff\xfe"):
        return raw[2:].decode("utf-16-le", errors="replace")
    if raw.startswith(b"\xef\xbb\xbf"):
        return raw[3:].decode("utf-8", errors="replace")
    return raw.decode("latin-1")


def _unescape_literal(body: bytes) -> bytes:
    out = bytearray()
    i = 0
    while i < len(body):
        c = body[i:i + 1]
        if c != b"\\" or i + 1 >= len(body):
            out += c
            i += 1
            continue
        nxt = body[i + 1:i + 2]
        if nxt in _ESCAPES:
            out += _ESCAPES[nxt]
            i += 2
        elif nxt and nxt in b"01234567":
            j = i + 1
            while j < len(body) and j < i + 4 and body[j:j + 1] in b"01234567":
                j += 1
            out.append(int(body[i + 1:j], 8) & 0xFF)
            i = j
        elif nxt in (b"\r", b"\n"):
            i += 2
        else:
            out += nxt
            i += 2
    return bytes(out)


def _strings_in(data: bytes) -> list[str]:
    out: list[str] = []
    for m in _LITERAL_RE.finditer(data):
        text = _pdf_string(_unescape_literal(m.group(0)[1:-1]))
        if sum(1 for c in text if c.isalpha()) >= 2:
            out.append(text)
    for m in _HEXSTR_RE.finditer(data):
        digits = re.sub(rb"\s", b"", m.group(1))
        if len(digits) % 2:
            digits += b"0"
        try:
            text = _pdf_string(bytes.fromhex(digits.decode("ascii")))
        except ValueError:
            continue
        if sum(1 for c in text if c.isalpha()) >= 2 and text.isprintable():
            out.append(text)
    for m in _NAME_RE.finditer(data):
        raw = re.sub(rb"#([0-9A-Fa-f]{2})", lambda h: bytes([int(h.group(1), 16)]), m.group(1))
        out.append(raw.decode("utf-8", errors="replace"))
    return out


_KEYED_RE = re.compile(
    rb"/(Contents|Subj|V|DV|Title|JS|TU|CA|Name|Dest)\s*(\((?:\\.|[^\\()]|\((?:\\.|[^\\()])*\))*\)|<[0-9A-Fa-f\s]{2,}>)")
_NOTED_KEYS = ("contents", "subj", "v", "dv", "js", "tu", "ca")
"""Keys whose strings a viewer shows as annotations, field values or scripts: worth a note. A title or a
name is metadata that the information dictionary already reports."""


def _keyed_in(data: bytes) -> list[str]:
    out: list[str] = []
    for m in _KEYED_RE.finditer(data):
        raw = m.group(2)
        if raw[:1] == b"(":
            text = _pdf_string(_unescape_literal(raw[1:-1]))
        else:
            digits = re.sub(rb"\s", b"", raw[1:-1])
            if len(digits) % 2:
                digits += b"0"
            try:
                text = _pdf_string(bytes.fromhex(digits.decode("ascii")))
            except ValueError:
                continue
        text = text.strip()
        if text and text.isprintable():
            out.append("%s: %s" % (m.group(1).decode("ascii").lower(), text))
    return out


def _chunks(path: Path) -> list[bytes]:
    try:
        with open(path, "rb") as fh:
            data = fh.read(MAX_RAW_BYTES)
    except OSError:
        return []
    budget = MAX_RAW_BYTES - len(data)
    chunks = [data]
    for n, m in enumerate(_STREAM_RE.finditer(data)):
        if n >= MAX_STREAMS or budget <= 0:
            break
        end = data.find(b"endstream", m.end())
        if end < 0:
            continue
        body = data[m.end():end]
        d = zlib.decompressobj()
        try:
            plain = d.decompress(body, budget)
        except zlib.error:
            continue
        budget -= len(plain)
        chunks.append(plain)
    return chunks


def keyed_strings(path: Path) -> list[str]:
    """The strings a viewer shows outside the page text: annotation contents and subjects, form field values,
    outline titles, scripts and names, as "key: value" lines for detection."""
    out: list[str] = []
    seen: set[str] = set()
    for chunk in _chunks(path):
        for line in _keyed_in(chunk):
            if line not in seen:
                seen.add(line)
                out.append(line)
    return out


def raw_strings(path: Path) -> list[str]:
    """Every string of every object of the file, also inside Flate compressed streams and object streams,
    within MAX_RAW_BYTES. Checked for registered forms only."""
    out: list[str] = []
    for chunk in _chunks(path):
        out.extend(_strings_in(chunk))
    return out
