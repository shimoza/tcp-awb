"""E-mail files (.eml) and Unix mailboxes (.mbox), read with the standard library `email` package.

Every header goes to `detect_text`, decoded (RFC 2047 encoded words, RFC 2231 parameters, raw 8-bit bytes as
UTF-8, else as cp1252) and unfolded: Received, Message-ID, X-Originating-IP, DKIM and list headers carry host
and domain names too. The headers a reader needs (From, To, Cc, Bcc, Reply-To, Date, Subject) also open the
output `text`.

Body parts are decoded (base64, quoted-printable, 8bit) with their charset. A charset label that does not fit
its bytes is corrected the way a browser does it, in bodies and in encoded words: a single-byte label on bytes
that are UTF-8 reads as UTF-8, a utf-8 label on bytes that are not reads as cp1252. A part without a charset
(or labelled ascii) whose ASCII text carries utf-7 runs (+A...-, the shape of a Latin letter) is read as utf-7.
A 7bit, 8bit or unlabelled part whose ASCII text carries quoted-printable marks (two =XX escapes or a soft
break, which is = at a line end that is not base64 padding) is decoded as quoted-printable first. A plain text part sent as format=flowed
(RFC 3676) is joined the way a mail client shows it, with DelSp honoured, so that a word split over a soft line
break is one word again. Html is stripped to text and its link and alt attributes go to detection. Of a
multipart/alternative only one version goes into the output (plain text when there is one), every version goes
to detection. The preamble and the epilogue of a multipart (text outside its parts, which a mail client hides)
go into the output like a body part.

Attachments, forwarded mails (message/rfc822) and non-text parts are written to a private temporary
folder (mode 700, under `awb.extract.temp_root` when set) under a name made of an index and the suffix,
passed through `awb.extract.extract` as children and the folder is removed again. Their names go to detection
and to `meta["attachment"]` of the child. An inline uuencoded block in a plain text body (begin 644 name, the
data lines, end) is decoded and read as an attachment under its name; the block itself leaves the output. An
inline BinHex or yEnc block is noted as not decoded and the extraction is marked incomplete. An Outlook .msg
file (OLE compound file) is unsupported.

A mailbox (.mbox by suffix or by content: a "From " line at the start and another after a blank line) is split
at its "From " lines and every message is read as a child mail, at most MAX_MESSAGES of them. The extraction is
of kind mbox, its text lists the messages by number and size, the sender address of every envelope line goes to
detection and every child carries its number in `meta["member"]`.
"""
from __future__ import annotations

import base64
import binascii
import codecs
import email
import email.policy
import email.utils
import functools
import mimetypes
import os
import quopri
import re
import shutil
from email.header import decode_header, make_header
from email.message import Message
from pathlib import Path

from awb.extract import INCOMPLETE, MAX_ARCHIVE_DEPTH, MBOX_SEPARATOR_RE, Extraction, looks_mbox, make_temp_dir
from awb.extract.text import decode_bytes, html_attribute_values, strip_html, unfold_calendar

OLE_MAGIC = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"
MSG_NOTE = "outlook msg file: no reader, save the mail as eml and run intake again"

MAX_ATTACHMENTS = 200
MAX_ATTACHMENT_BYTES = 200 * 1024 * 1024
MAX_MIME_DEPTH = 50
MAX_MESSAGES = 200
"""Messages of one mailbox that are read as children."""

READER_HEADERS = ("From", "To", "Cc", "Bcc", "Reply-To", "Date", "Subject")
_PART_PARAMS = ("filename", "name")
_PART_HEADERS = ("content-description", "content-id", "content-location")
_SIGNATURE_TYPES = ("application/pkcs7-signature", "application/x-pkcs7-signature", "application/pgp-signature")

_SAFE_NAME_RE = re.compile(r"[^A-Za-z0-9._-]+")
_LINEBREAK_RE = re.compile(r"\r?\n")
_ENCODED_WORD_RE = re.compile(r"=\?([^?\s]+)\?([bBqQ])\?([^?\s]*)\?=")
_QP_MARK_RE = re.compile(rb"=[0-9A-F]{2}")
_QP_SOFT_BREAK_RE = re.compile(rb"([A-Za-z0-9+/]*)(=+)\r?$", re.M)
_UTF7_RUN_RE = re.compile(rb"\+A[A-Za-z0-9+/]{2,}-")
_UU_BEGIN_RE = re.compile(r"begin [0-7]{3,4} (\S.*)")
_BINHEX_MARK = "(This file must be converted with BinHex"
_YENC_RE = re.compile(r"^=ybegin ", re.M)


# --------------------------------------------------------------------------- helpers


def _fix_surrogates(value: str) -> str:
    """Raw 8-bit header bytes arrive as surrogate escapes. Decode them as UTF-8, else as cp1252."""
    if not any("\udc80" <= c <= "\udcff" for c in value):
        return value
    raw = value.encode("utf-8", "surrogateescape")
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        return raw.decode("cp1252", errors="replace")


def _unfold(value: str) -> str:
    return _LINEBREAK_RE.sub("", value)


def _has_control(text: str) -> bool:
    return any(ord(c) < 32 and c not in "\t\n\r" for c in text)


def unflow(text: str, delsp: bool) -> str:
    """Join the soft line breaks of a format=flowed body (RFC 3676): a line that ends in a space continues on
    the next line; with DelSp=yes that space is removed. A space-stuffed line loses its first space. The
    signature separator "-- " is a hard break."""
    out: list[str] = []
    buf = ""
    for line in text.replace("\r\n", "\n").split("\n"):
        if line.startswith(" "):
            line = line[1:]
        if line.endswith(" ") and line != "-- ":
            buf += line[:-1] if delsp else line
            continue
        out.append(buf + line)
        buf = ""
    if buf:
        out.append(buf)
    return "\n".join(out)


# --------------------------------------------------------------------------- charsets


def _codec_name(charset: str) -> str | None:
    try:
        return codecs.lookup(charset).name
    except LookupError:
        return None


@functools.lru_cache(maxsize=None)
def _single_byte(charset: str) -> bool:
    """True for a code page whose high bytes decode one by one (the Latin, Greek and Cyrillic code pages), false
    for UTF-8, UTF-16, utf-7 and the East Asian multi-byte encodings."""
    try:
        ones = sum(len(bytes([b]).decode(charset, "ignore")) == 1 for b in range(0x80, 0x100))
    except LookupError:
        return False
    return ones >= 96


def _is_utf8(data: bytes) -> bool:
    try:
        data.decode("utf-8")
    except UnicodeDecodeError:
        return False
    return True


def _better_charset(data: bytes, charset: str) -> str | None:
    """The browser heuristic for a label that does not fit its bytes: a single-byte (or ascii) label on bytes
    that are UTF-8 with non-ASCII content gives utf-8, a utf-8 label on bytes that are not UTF-8 gives cp1252.
    None when the label stands."""
    if data.isascii():
        return None
    name = _codec_name(charset)
    if name is None:
        return None
    if name == "utf-8":
        return None if _is_utf8(data) else "cp1252"
    if (name == "ascii" or _single_byte(name)) and _is_utf8(data):
        return "utf-8"
    return None


def _relabel_encoded_words(value: str) -> str:
    """An encoded word whose charset label does not fit its bytes gets the label `_better_charset` gives, so
    that the header parser decodes it right."""

    def fix(m: re.Match) -> str:
        label, enc, payload = m.group(1), m.group(2), m.group(3)
        charset, star, lang = label.partition("*")
        try:
            if enc in "bB":
                raw = base64.b64decode(payload + "=" * (-len(payload) % 4))
            else:
                raw = quopri.decodestring(payload.encode("ascii"), header=True)
        except (binascii.Error, ValueError, UnicodeEncodeError):
            return m.group(0)
        better = _better_charset(raw, charset)
        if better is None:
            return m.group(0)
        return "=?%s%s%s?%s?%s?=" % (better, star, lang, enc, payload)

    return _ENCODED_WORD_RE.sub(fix, value)


def _base64_padding(run: bytes, pads: bytes) -> bool:
    """A = that closes a run of 16 or more base64 characters to a multiple of four is base64 padding."""
    return len(run) >= 16 and len(pads) <= 2 and (len(run) + len(pads)) % 4 == 0


def _looks_quoted_printable(data: bytes) -> bool:
    """Two or more =XX escapes or a soft break: = at a line end that is not the padding of a base64 block."""
    if len(_QP_MARK_RE.findall(data)) >= 2:
        return True
    return any(not _base64_padding(m.group(1), m.group(2)) for m in _QP_SOFT_BREAK_RE.finditer(data))


def _utf7(data: bytes) -> str | None:
    try:
        text = data.decode("utf-7")
    except UnicodeDecodeError:
        return None
    return None if _has_control(text) else text


def _decode_text(data: bytes, charset: str | None) -> str:
    """The bytes of a text part as text: by the charset label, corrected by the browser heuristic; a part
    without a label (or labelled ascii) whose ASCII text carries utf-7 runs is tried as utf-7 first; a part
    whose label fails is decoded by `decode_bytes`."""
    name = _codec_name(charset) if charset else None
    if name in (None, "ascii") and data.isascii() and _UTF7_RUN_RE.search(data):
        text = _utf7(data)
        if text is not None:
            return text
    if charset:
        charset = _better_charset(data, charset) or charset
        try:
            return data.decode(charset)
        except (LookupError, UnicodeDecodeError):
            pass
    return decode_bytes(data)[0]


# --------------------------------------------------------------------------- headers and parts


def _decode_raw(value: str) -> str:
    """RFC 2047 decoding without the header policy, for a value the policy could not parse."""
    try:
        return str(make_header(decode_header(value)))
    except Exception:
        return value


def _headers(msg: Message, notes: list[str]) -> list[tuple[str, str]]:
    """Every header of `msg` as (name, decoded and unfolded value), in file order. Raw 8-bit bytes are decoded
    before the policy parses the value, because the policy would replace them by U+FFFD."""
    out: list[tuple[str, str]] = []
    failed = 0
    for name, raw in msg.raw_items():
        raw = _relabel_encoded_words(_fix_surrogates(str(raw)))
        try:
            value = str(msg.policy.header_fetch_parse(name, raw))
        except Exception:
            failed += 1
            value = _decode_raw(_unfold(raw))
        out.append((name, _fix_surrogates(_unfold(value)).strip()))
    if failed:
        notes.append("%d header(s) could not be parsed, kept as written" % failed)
    return out


def _payload_text(part: Message) -> str:
    """The decoded body of a leaf part as text."""
    data = part.get_payload(decode=True)
    if data is None:
        payload = part.get_payload()
        return payload if isinstance(payload, str) else ""
    cte = str(part.get("content-transfer-encoding") or "").strip().lower()
    if cte in ("", "7bit", "8bit") and data.isascii() and _looks_quoted_printable(data):
        data = quopri.decodestring(data)
    return _decode_text(data, part.get_content_charset())


def _filename(part: Message) -> str | None:
    try:
        name = part.get_filename()
    except Exception:
        name = part.get_param("filename", header="content-disposition") or part.get_param("name")
        name = str(name) if name else None
    if name:
        name = _fix_surrogates(_unfold(str(name))).strip()
    return name or None


def _is_attachment(part: Message) -> bool:
    try:
        disposition = part.get_content_disposition()
    except Exception:
        disposition = None
    if disposition == "attachment" or _filename(part):
        return True
    return part.get_content_maintype() not in ("text", "multipart")


def _temp_path(folder: Path, index: int, name: str | None, ctype: str) -> Path:
    """A file name made of the index and the suffix only, so that the temporary path carries no name.
    The suffix survives because sniffing uses it (eml, csv, html)."""
    base = os.path.basename((name or "").replace("\\", "/"))
    _, dot, suffix = base.rpartition(".")
    suffix = _SAFE_NAME_RE.sub("", suffix)[:16] if dot else ""
    if not suffix:
        guessed = ".eml" if ctype == "message/rfc822" else (mimetypes.guess_extension(ctype) or ".bin")
        suffix = guessed.lstrip(".")
    return folder / ("%04d.%s" % (index, suffix))


def _attachment_bytes(part: Message) -> bytes:
    if part.get_content_type() == "message/rfc822" or part.is_multipart():
        inner = part.get_payload()
        if isinstance(inner, list) and len(inner) == 1 and isinstance(inner[0], Message):
            inner = inner[0]
        target = inner if isinstance(inner, Message) else part
        try:
            return target.as_bytes()
        except Exception:
            return target.as_bytes(policy=email.policy.compat32)
    data = part.get_payload(decode=True)
    if data is None:
        payload = part.get_payload()
        data = payload.encode("utf-8", "surrogateescape") if isinstance(payload, str) else b""
    return data


def _attachment_payload(item: Message | bytes) -> tuple[bytes, str]:
    """The bytes to write for an attachment and its content type; a file from an inline uuencoded block has
    neither header."""
    if isinstance(item, bytes):
        return item, "application/octet-stream"
    return _attachment_bytes(item), item.get_content_type()


# --------------------------------------------------------------------------- inline encoded blocks


def _uudecode(lines: list[str]) -> bytes | None:
    """The data lines of a uuencoded block as bytes, None when a line is not uuencoded. An empty line is
    skipped: some encoders write it for a line of no bytes, which is "`" in the standard."""
    chunks: list[bytes] = []
    for line in lines:
        if not line.strip():
            continue
        try:
            chunks.append(binascii.a2b_uu(line))
        except binascii.Error:
            return None
    return b"".join(chunks)


def uudecode_blocks(text: str) -> tuple[str, list[tuple[str, bytes]], int]:
    """Inline uuencoded blocks of a text (begin 644 name, the data lines, end): the text without the blocks
    that decoded, the decoded (name, bytes) pairs and the count of blocks that did not decode and stay."""
    lines = text.split("\n")
    out: list[str] = []
    files: list[tuple[str, bytes]] = []
    failed = 0
    i = 0
    while i < len(lines):
        m = _UU_BEGIN_RE.fullmatch(lines[i].rstrip())
        end = next((j for j in range(i + 1, len(lines)) if lines[j].strip() == "end"), None) if m else None
        data = _uudecode(lines[i + 1:end]) if end is not None else None
        if data is None:
            if m:
                failed += 1
            out.append(lines[i])
            i += 1
            continue
        files.append((m.group(1).strip(), data))
        i = end + 1
    return "\n".join(out), files, failed


# --------------------------------------------------------------------------- walking the parts


class _Body:
    """Text pieces of one branch of the MIME tree: (text, goes to output)."""

    def __init__(self) -> None:
        self.pieces: list[tuple[str, bool]] = []

    def has_text(self) -> bool:
        return any(t.strip() for t, out in self.pieces if out)


class _Walk:
    def __init__(self) -> None:
        self.detect: list[str] = []
        self.attachments: list[tuple[str | None, Message | bytes]] = []
        self.defects = 0
        self.skipped_signatures = 0
        self.too_deep = False
        self.uu_blocks = 0
        self.uu_failed = 0
        self.binhex_blocks = 0
        self.yenc_blocks = 0

    def walk(self, part: Message, body: _Body, level: int = 0) -> None:
        if level > MAX_MIME_DEPTH:
            self.too_deep = True
            return
        self.defects += len(getattr(part, "defects", ()) or ())
        ctype = part.get_content_type()
        if level > 0:
            self._part_headers(part)
        if part.is_multipart() and ctype != "message/rfc822" and part.get_content_maintype() == "multipart":
            self._outside(part.preamble, body)
            subparts = [p for p in part.get_payload() if isinstance(p, Message)]
            if ctype == "multipart/alternative":
                self._alternative(subparts, body, level)
            else:
                for sub in subparts:
                    self.walk(sub, body, level + 1)
            self._outside(part.epilogue, body)
            return
        if ctype in _SIGNATURE_TYPES:
            self.skipped_signatures += 1
            return
        if _is_attachment(part) and (level > 0 or part.get_content_maintype() not in ("text", "multipart")):
            self.attachments.append((_filename(part), part))
            return
        text = _payload_text(part)
        if ctype == "text/plain" and str(part.get_param("format") or "").lower() == "flowed":
            text = unflow(text, str(part.get_param("delsp") or "").lower() == "yes")
        if ctype == "text/html":
            self.detect.extend(html_attribute_values(text))
            text = strip_html(text)
        text = text.replace("\r\n", "\n").replace("\r", "\n").strip()
        if ctype == "text/calendar":
            text = unfold_calendar(text)
        if ctype == "text/plain":
            text = self._inline_blocks(text)
        if text:
            body.pieces.append((text, True))

    def _outside(self, chunk: str | None, body: _Body) -> None:
        """The preamble or the epilogue of a multipart: text outside its parts, which a mail client hides."""
        text = _fix_surrogates(chunk or "").replace("\r\n", "\n").replace("\r", "\n").strip()
        if text:
            body.pieces.append((text, True))

    def _inline_blocks(self, text: str) -> str:
        """Decode inline uuencoded blocks into attachments and take them out of the text; count BinHex and
        yEnc blocks, which stay as they are."""
        text, files, failed = uudecode_blocks(text)
        for name, data in files:
            self.attachments.append((name, data))
        self.uu_blocks += len(files)
        self.uu_failed += failed
        if _BINHEX_MARK in text:
            self.binhex_blocks += 1
        self.yenc_blocks += len(_YENC_RE.findall(text))
        return text

    def _alternative(self, subparts: list[Message], body: _Body, level: int) -> None:
        branches: list[tuple[Message, _Body]] = []
        for sub in subparts:
            b = _Body()
            self.walk(sub, b, level + 1)
            branches.append((sub, b))
        chosen = None
        for sub, b in branches:
            if sub.get_content_type() == "text/plain" and b.has_text():
                chosen = b
                break
        if chosen is None:
            for sub, b in reversed(branches):
                if b.has_text():
                    chosen = b
                    break
        for _, b in branches:
            for text, out in b.pieces:
                body.pieces.append((text, out and b is chosen))

    def _part_headers(self, part: Message) -> None:
        name = _filename(part)
        if name:
            self.detect.append("part name: %s" % name)
        for param in _PART_PARAMS:
            try:
                value = part.get_param(param)
            except Exception:
                value = None
            if isinstance(value, tuple):
                value = email.utils.collapse_rfc2231_value(value)
            if value:
                value = _fix_surrogates(str(value)).strip()
                if value and value != name:
                    self.detect.append("part name: %s" % value)
        for header in _PART_HEADERS:
            value = part.get(header)
            if value:
                self.detect.append("%s: %s" % (header, _fix_surrogates(_unfold(str(value))).strip()))


# --------------------------------------------------------------------------- entry point


def extract(path: Path, depth: int = 0) -> Extraction:
    """Read an .eml or .mbox file. `depth` is the nesting level of `path`, as for archives. Never raises."""
    path = Path(path)
    try:
        data = path.read_bytes()
    except OSError as err:
        return Extraction(path, "eml", "failed", notes=["%s while reading the file" % type(err).__name__])
    if data.startswith(OLE_MAGIC):
        return Extraction(path, "msg", "unsupported", notes=[MSG_NOTE])
    messages = split_mbox(data) if path.suffix.lower() == ".mbox" or looks_mbox(data) else []
    if messages:
        try:
            return _read_mbox(path, messages, depth)
        except Exception as err:
            return Extraction(path, "mbox", "failed", notes=["%s while reading the mailbox" % type(err).__name__])
    ex = Extraction(path, "eml", "ok")
    try:
        msg = email.message_from_bytes(data, policy=email.policy.default)
    except Exception as err:
        ex.state = "failed"
        ex.notes.append("%s while parsing the mail" % type(err).__name__)
        return ex
    try:
        return _read(ex, msg, depth)
    except Exception as err:
        return Extraction(path, "eml", "failed", notes=["%s while reading the mail" % type(err).__name__])


def _read(ex: Extraction, msg: Message, depth: int) -> Extraction:
    headers = _headers(msg, ex.notes)
    walker = _Walk()
    body = _Body()
    walker.walk(msg, body)

    reader_lines: list[str] = []
    for want in READER_HEADERS:
        for name, value in headers:
            if name.lower() == want.lower() and value:
                reader_lines.append("%s: %s" % (want, value))
    first = {name.lower(): value for name, value in reversed(headers)}
    for key, meta_key in (("subject", "subject"), ("from", "from"), ("date", "date")):
        if first.get(key):
            ex.meta[meta_key] = first[key]

    names = [n or "" for n, _ in walker.attachments]
    ex.meta["attachment_count"] = len(walker.attachments)
    if walker.attachments:
        _extract_attachments(ex, walker.attachments, depth)

    out_pieces = [t for t, out in body.pieces if out]
    text_parts = ["\n".join(reader_lines)] if reader_lines else []
    text_parts.extend(out_pieces)
    listed = [n for n in names if n]
    if listed:
        text_parts.append("Attachments:\n" + "\n".join("- %s" % n for n in listed))
    ex.text = "\n\n".join(p for p in text_parts if p.strip())

    detect = [ex.text]
    detect.extend("%s: %s" % (name, value) for name, value in headers if value)
    detect.extend(t for t, out in body.pieces if not out)
    detect.extend(walker.detect)
    detect.extend("attachment: %s" % n for n in listed)
    ex.detect_text = "\n".join(p for p in detect if p and p.strip())

    if walker.defects:
        ex.notes.append("%d mime defect(s) found while parsing" % walker.defects)
    if walker.skipped_signatures:
        ex.notes.append("%d signature part(s) skipped" % walker.skipped_signatures)
    if walker.too_deep:
        ex.notes.append("mime nesting deeper than %d, inner parts skipped" % MAX_MIME_DEPTH)
    if walker.uu_blocks:
        ex.notes.append("%d uuencoded block(s) in the body decoded as attachment(s)" % walker.uu_blocks)
    for count, what in ((walker.uu_failed, "uuencoded"), (walker.binhex_blocks, "binhex"), (walker.yenc_blocks, "yenc")):
        if count:
            ex.notes.append("%d %s block(s) in the body not decoded, review the original" % (count, what))
            ex.meta[INCOMPLETE] = True
    if not headers and not out_pieces and not walker.attachments:
        ex.state = "unreadable"
        ex.notes.append("no headers, no body and no attachments found")
    return ex


def _extract_attachments(ex: Extraction, attachments: list[tuple[str | None, Message | bytes]], depth: int) -> None:
    """Write attachments to a private temporary folder, extract each as a child, remove the folder."""
    if depth >= MAX_ARCHIVE_DEPTH:
        note = "nesting depth %d reached, %d attachment(s) listed but not extracted"
        ex.notes.append(note % (MAX_ARCHIVE_DEPTH, len(attachments)))
        return
    from awb.extract import extract as extract_file

    tmp = make_temp_dir("awb-mail-")
    try:
        total = 0
        for index, (name, part) in enumerate(attachments):
            if index >= MAX_ATTACHMENTS:
                ex.notes.append("attachment limit reached, %d of %d attachments extracted" % (index, len(attachments)))
                break
            try:
                data, ctype = _attachment_payload(part)
            except Exception as err:
                ex.notes.append("attachment %d could not be decoded (%s)" % (index + 1, type(err).__name__))
                continue
            if total + len(data) > MAX_ATTACHMENT_BYTES:
                note = "size limit reached at attachment %d, remaining attachments not extracted"
                ex.notes.append(note % (index + 1))
                break
            total += len(data)
            dst = _temp_path(tmp, index, name, ctype)
            fd = os.open(dst, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "wb") as fh:
                fh.write(data)
            child = extract_file(dst, depth + 1)
            if name:
                child.meta["attachment"] = name
            ex.children.append(child)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    ex.meta["child_count"] = len(ex.children)


# --------------------------------------------------------------------------- mailboxes


def split_mbox(data: bytes) -> list[tuple[str, bytes]]:
    """The messages of a mailbox as (envelope line, message bytes): every "From " line at the start of the file
    or after a blank line opens one; the line itself is the envelope and not part of the message. Text before
    the first envelope line is a message without one."""
    separators = list(MBOX_SEPARATOR_RE.finditer(data))
    out: list[tuple[str, bytes]] = []
    if separators and data[: separators[0].start()].strip():
        out.append(("", data[: separators[0].start()]))
    for i, m in enumerate(separators):
        end = separators[i + 1].start() if i + 1 < len(separators) else len(data)
        envelope = _fix_surrogates(m.group(0).strip().decode("utf-8", "surrogateescape"))
        out.append((envelope, data[m.end():end]))
    return out


def _read_mbox(path: Path, messages: list[tuple[str, bytes]], depth: int) -> Extraction:
    """Read every message of a mailbox as a child mail, from a private temporary folder that is removed again."""
    ex = Extraction(path, "mbox", "ok")
    ex.meta["message_count"] = len(messages)
    ex.meta["child_count"] = 0
    ex.notes.append("mailbox with %d message(s), each read as a child" % len(messages))
    lines = ["| message | bytes |", "| --- | --- |"]
    lines.extend("| %d | %d |" % (i + 1, len(body)) for i, (_, body) in enumerate(messages))
    ex.text = "\n".join(lines)
    # of an envelope line ("From " sender date) only the sender carries anything: the date would read as a name
    senders = [envelope.split(None, 2)[1] for envelope, _ in messages if len(envelope.split(None, 2)) > 1]
    ex.detect_text = "\n".join("envelope sender: %s" % sender for sender in senders)
    if depth >= MAX_ARCHIVE_DEPTH:
        ex.notes.append("nesting depth %d reached, messages listed but not extracted" % MAX_ARCHIVE_DEPTH)
        ex.meta[INCOMPLETE] = True
        return ex
    from awb.extract import extract as extract_file

    tmp = make_temp_dir("awb-mbox-")
    try:
        for index, (_, body) in enumerate(messages):
            if index >= MAX_MESSAGES:
                ex.notes.append("message limit reached, %d of %d messages extracted" % (index, len(messages)))
                ex.meta[INCOMPLETE] = True
                break
            dst = tmp / ("%04d.eml" % index)
            fd = os.open(dst, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "wb") as fh:
                fh.write(body)
            child = extract_file(dst, depth + 1)
            child.meta["member"] = "message %d" % (index + 1)
            ex.children.append(child)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    ex.meta["child_count"] = len(ex.children)
    return ex
