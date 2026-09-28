"""Extraction of text from incoming files, by content, never by trust in the file name.

`sniff` looks at the bytes of a file and names its kind. `extract` dispatches on that kind to the
reader modules of this package and always returns an `Extraction`, never raises. The pdf and mail
readers are imported lazily inside the dispatch so that the rest of the package works without them.

Nothing in here carries a value from the file into a note, a log line or an exception. Notes carry
counts, classes and positions only.
"""
from __future__ import annotations

import contextlib
import contextvars
import inspect
import os
import re
import tempfile
import zipfile
from dataclasses import dataclass, field
from pathlib import Path

MAX_ARCHIVE_DEPTH = 3
"""An archive nested deeper than this is listed but its members are not extracted."""

INCOMPLETE = "incomplete"
"""Key of `meta` that a reader sets to True when it skipped content it could not read (a member past a limit,
an encrypted member, an embedded object). The file is then never reported clean by a check."""

_TEMP_ROOT: contextvars.ContextVar[Path | None] = contextvars.ContextVar("awb_temp_root", default=None)


@contextlib.contextmanager
def temp_root(folder: Path):
    """Put every temporary folder of the readers under `folder` (mode 700) while the block runs.

    The intake points this into the vault, so that an unpacked member left behind by a killed process lies on
    the vault side and not in the system temporary folder.
    """
    folder = Path(folder)
    folder.mkdir(parents=True, exist_ok=True)
    os.chmod(folder, 0o700)
    token = _TEMP_ROOT.set(folder)
    try:
        yield folder
    finally:
        _TEMP_ROOT.reset(token)


def make_temp_dir(prefix: str) -> Path:
    """A private temporary folder (mode 700) under the current temp root, else in the system temporary folder."""
    tmp = Path(tempfile.mkdtemp(prefix=prefix, dir=_TEMP_ROOT.get()))
    os.chmod(tmp, 0o700)
    return tmp


IMAGE_NOTE = "image, no text layer, review the original"
OLE_NOTE = "ole compound file (msg, doc, xls or ppt): no reader, convert to a modern format"

OFFICE_KINDS = ("docx", "xlsx", "pptx", "odt", "ods", "odp")
TEXT_KINDS = ("text", "html", "csv", "json", "svg")
ARCHIVE_KINDS = ("zip", "tar")
MAIL_KINDS = ("eml", "mbox")
KINDS = TEXT_KINDS + OFFICE_KINDS + ("pdf",) + MAIL_KINDS + ("msg",) + ARCHIVE_KINDS + ("image", "unknown")

_SNIFF_BYTES = 65536

MBOX_SEPARATOR_RE = re.compile(rb"(?:\A|\r?\n\r?\n)From [^\r\n]*\r?\n")
"""A Unix mailbox separator: a "From " line at the start of the file or after a blank line (RFC 4155)."""


def looks_mbox(data: bytes) -> bool:
    """Mailbox shape: the file starts with a "From " line and a second one follows a blank line. A single mail
    saved with its envelope line has no second one and stays a mail."""
    if not data.startswith(b"From "):
        return False
    separators = MBOX_SEPARATOR_RE.finditer(data)
    next(separators, None)
    return next(separators, None) is not None


@dataclass
class Extraction:
    """What one file gave up. `text` goes into the sanitised output, `detect_text` into detection."""

    path: Path
    kind: str
    state: str
    text: str = ""
    detect_text: str = ""
    meta: dict = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)
    children: list["Extraction"] = field(default_factory=list)
    # raw text of places no reader renders (every xml part and attribute of an office file, strings of pdf
    # objects, html attributes and scripts): checked for registered forms only, because it is too noisy for
    # structured patterns and for candidates
    scan_text: str = ""


# --------------------------------------------------------------------------- sniffing


def _read_head(path: Path) -> bytes:
    with open(path, "rb") as fh:
        return fh.read(_SNIFF_BYTES)


def _is_ole(head: bytes) -> bool:
    return head.startswith(b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1")


def _is_image(head: bytes) -> bool:
    if head.startswith((b"\x89PNG\r\n\x1a\n", b"\xff\xd8\xff", b"GIF87a", b"GIF89a")):
        return True
    # BMP: "BM" alone is too weak (a text may start with it); the four reserved bytes must be zero
    if head.startswith(b"BM") and len(head) >= 26 and head[6:10] == b"\x00\x00\x00\x00":
        return True
    if head.startswith((b"II*\x00", b"MM\x00*")):
        return True
    if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return True
    # HEIF family: an ftyp box with a heic, heix, hevc, mif1 or avif brand
    if head[4:8] == b"ftyp" and head[8:12] in (b"heic", b"heix", b"hevc", b"hevx", b"mif1", b"msf1", b"avif"):
        return True
    # EMF (a header record of type 1 with the ENHMETA signature), WMF (placeable or standard header)
    if head[:4] == b"\x01\x00\x00\x00" and head[40:44] == b" EMF":
        return True
    if head.startswith((b"\xd7\xcd\xc6\x9a", b"\x01\x00\x09\x00", b"\x02\x00\x09\x00")):
        return True
    return False


def _is_tar(head: bytes, path: Path) -> bool:
    if len(head) > 262 and head[257:262] == b"ustar":
        return True
    if path.suffix.lower() == ".tar":
        import tarfile

        try:
            return tarfile.is_tarfile(path)
        except Exception:
            return False
    return False


def _utf16_shape(head: bytes) -> bool:
    """True when the bytes look like UTF-16 without a BOM: one byte of nearly every pair is NUL."""
    from awb.extract.text import utf16_shape

    return utf16_shape(head) is not None


_WHITESPACE_CONTROLS = frozenset(b"\t\n\r\x0b\x0c\x1b\x08")


def _control_ratio(data: bytes) -> float:
    if not data:
        return 0.0
    bad = sum(1 for b in data if b < 0x20 and b not in _WHITESPACE_CONTROLS)
    return bad / len(data)


def _looks_text(head: bytes) -> bool:
    if not head:
        return True
    if head.startswith((b"\xff\xfe", b"\xfe\xff", b"\xef\xbb\xbf")):
        return True
    if b"\x00" in head:
        return _utf16_shape(head)
    try:
        head.decode("utf-8")
    except UnicodeDecodeError as err:
        # a sample cut inside a multi-byte sequence is not a decoding failure
        if err.start < len(head) - 3:
            return _control_ratio(head) < 0.02
    return _control_ratio(head) < 0.02


def _decode_head(head: bytes) -> str:
    from awb.extract.text import decode_bytes

    return decode_bytes(head)[0]


def _text_kind(head: bytes, path: Path) -> str:
    """Refine a decodable file into text, html, csv, json, eml or mbox."""
    suffix = path.suffix.lower()
    sample = _decode_head(head).lstrip("﻿")
    low = sample[:4096].lower().lstrip()
    if suffix == ".mbox" or looks_mbox(head):
        return "mbox"
    if suffix == ".eml" or _looks_mail(sample):
        return "eml"
    if suffix in (".html", ".htm", ".xhtml") or low.startswith(("<!doctype html", "<html")) or "<html" in low[:2048]:
        return "html"
    if suffix == ".svg" or "<svg" in low[:2048]:
        return "svg"
    if suffix in (".csv", ".tsv"):
        return "csv"
    if suffix == ".json" or (low[:1] in ("{", "[") and _parses_json(sample)):
        return "json"
    return "text"


def _looks_mail(sample: str) -> bool:
    """RFC 822 shape: the first lines are header lines and From or Subject is among them."""
    lines = sample.splitlines()[:30]
    if not lines:
        return False
    seen = set()
    for line in lines:
        if not line.strip():
            break
        if line[:1] in (" ", "\t"):
            continue
        name, sep, _ = line.partition(":")
        if not sep or " " in name.strip() or not name.strip():
            return False
        seen.add(name.strip().lower())
    return ("from" in seen or "subject" in seen) and len(seen) >= 3


def _parses_json(sample: str) -> bool:
    """True when the whole sample is one JSON value. A truncated large file counts as plain text."""
    import json

    try:
        json.loads(sample)
        return True
    except ValueError:
        return False


def _zip_kind(path: Path) -> str:
    """docx, xlsx, pptx, odt, ods, odp or zip, by the member names of the archive."""
    try:
        with zipfile.ZipFile(path) as zf:
            names = zf.namelist()
            name_set = set(names)
            if "mimetype" in name_set:
                try:
                    mime = zf.read("mimetype")[:100].decode("ascii", errors="ignore").strip()
                except Exception:
                    mime = ""
                if mime.endswith("opendocument.text") or mime.endswith("opendocument.text-template"):
                    return "odt"
                if mime.endswith("opendocument.spreadsheet") or mime.endswith("opendocument.spreadsheet-template"):
                    return "ods"
                if mime.endswith("opendocument.presentation") or mime.endswith("opendocument.presentation-template"):
                    return "odp"
            if "word/document.xml" in name_set or any(n.startswith("word/") for n in names):
                return "docx"
            if "xl/workbook.xml" in name_set or any(n.startswith("xl/") for n in names):
                return "xlsx"
            if "ppt/presentation.xml" in name_set or any(n.startswith("ppt/") for n in names):
                return "pptx"
    except Exception:
        pass
    return "zip"


def sniff(path: Path) -> str:
    """Name the kind of a file by its content. The file name only refines a decodable text file."""
    path = Path(path)
    head = _read_head(path)
    # the header may sit behind up to 1024 bytes of junk (the PDF standard allows it, poppler reads it)
    if head.startswith(b"%PDF") or b"%PDF-" in head[:1024]:
        return "pdf"
    if head.startswith((b"PK\x03\x04", b"PK\x05\x06", b"PK\x07\x08")):
        return _zip_kind(path)
    if _is_ole(head):
        return "msg" if path.suffix.lower() == ".msg" else "unknown"
    if _is_image(head):
        return "image"
    if head.startswith((b"\x1f\x8b", b"BZh", b"\xfd7zXZ\x00")):
        return "tar"
    if _is_tar(head, path):
        return "tar"
    if _looks_text(head):
        return _text_kind(head, path)
    return "unknown"


# --------------------------------------------------------------------------- dispatch


def _call_reader(module_name: str, candidates: tuple[str, ...], path: Path, depth: int, kind: str) -> Extraction:
    """Import a reader module lazily and call the first of `candidates` it defines."""
    import importlib

    try:
        mod = importlib.import_module("awb.extract." + module_name)
    except ImportError:
        return Extraction(path, kind, "failed", notes=["%s reader is not available" % module_name])
    fn = None
    for name in candidates:
        fn = getattr(mod, name, None)
        # only a function defined in the reader itself: a reader that imported this module's `extract`
        # for its attachments must not be called back as its own entry point
        if callable(fn) and getattr(fn, "__module__", None) == mod.__name__:
            break
        fn = None
    if fn is None:
        return Extraction(path, kind, "failed", notes=["%s reader has no entry point" % module_name])
    kwargs = {}
    try:
        params = inspect.signature(fn).parameters
    except (TypeError, ValueError):
        params = {}
    if "depth" in params:
        kwargs["depth"] = depth
    if "kind" in params:
        kwargs["kind"] = kind
    return fn(path, **kwargs)


def extract(path: Path, depth: int = 0) -> Extraction:
    """Read one file. Archives recurse through their members up to MAX_ARCHIVE_DEPTH."""
    path = Path(path)
    try:
        if not path.is_file():
            return Extraction(path, "unknown", "failed", notes=["not a regular file"])
        if os.path.getsize(path) == 0:
            return Extraction(path, "text", "unreadable", notes=["empty file"])
        kind = sniff(path)
    except Exception as err:
        return Extraction(path, "unknown", "failed", notes=["%s while sniffing" % type(err).__name__])

    try:
        if kind in OFFICE_KINDS:
            from awb.extract import office

            result = office.extract_office(path, kind, depth)
        elif kind in ARCHIVE_KINDS:
            from awb.extract import archive

            result = archive.extract_archive(path, kind, depth)
        elif kind in TEXT_KINDS:
            from awb.extract import text as text_mod

            result = text_mod.extract_text(path, kind)
        elif kind == "pdf":
            result = _call_reader("pdf", ("extract_pdf", "extract", "read_pdf", "read"), path, depth, kind)
        elif kind in MAIL_KINDS:
            result = _call_reader(
                "mail", ("extract_mail", "extract_eml", "extract", "read_mail", "read"), path, depth, kind
            )
        elif kind == "msg":
            result = Extraction(path, kind, "unsupported", notes=[OLE_NOTE])
        elif kind == "image":
            result = Extraction(path, kind, "unreadable", notes=[IMAGE_NOTE])
        else:
            note = OLE_NOTE if _is_ole(_read_head(path)) else "unknown format, no reader"
            result = Extraction(path, kind, "unsupported", notes=[note])
    except Exception as err:
        return Extraction(path, kind, "failed", notes=["%s while reading" % type(err).__name__])

    if not isinstance(result, Extraction):
        return Extraction(path, kind, "failed", notes=["reader returned no extraction"])
    if result.state == "ok" and not result.text.strip() and not result.detect_text.strip():
        result.state = "unreadable"
        result.notes.append("no extractable text")
    return result


def is_incomplete(ex: Extraction) -> bool:
    """True when the extraction or any child was not read in full (state not ok or content skipped)."""
    if ex.state != "ok" or ex.meta.get(INCOMPLETE):
        return True
    return any(is_incomplete(c) for c in ex.children or [])


__all__ = [
    "Extraction",
    "INCOMPLETE",
    "is_incomplete",
    "make_temp_dir",
    "temp_root",
    "sniff",
    "extract",
    "looks_mbox",
    "MBOX_SEPARATOR_RE",
    "MAX_ARCHIVE_DEPTH",
    "IMAGE_NOTE",
    "OLE_NOTE",
    "OFFICE_KINDS",
    "TEXT_KINDS",
    "ARCHIVE_KINDS",
    "MAIL_KINDS",
    "KINDS",
]
