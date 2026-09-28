"""Plain text, html, csv and json files.

Encoding is taken from a BOM, then tried as UTF-8, then as UTF-16 when the bytes have that shape, then
read as cp1252 (a superset of Latin-1 for the printable range) with Latin-1 as the last fallback.
Bytes with NUL in them and the UTF-16 shape are read as UTF-16 before UTF-8 is tried.
Html tags are stripped; comments go to `detect_text`, every attribute value and the text of scripts and
styles go to `scan_text`. A base64 block that decodes to text (UTF-8, UTF-16 or Latin-1) and a hex block that
decodes to text are appended to `detect_text` with a note, so that a name hidden in an encoded attachment is
still seen. Inside a line a block needs 16 or more characters, a MIME style block 40 per line. In `text` the
decoded text takes the place of the block, so the sanitised output never carries an encoded name. A base64
block that decodes to binary stays. Its printable strings are available to detection.
"""
from __future__ import annotations

import base64
import binascii
import csv
import html
import io
import re
from pathlib import Path

from awb.extract import Extraction

MAX_BASE64_BLOCKS = 1000
MAX_BASE64_DECODED = 10 * 1024 * 1024
MIN_INLINE_BLOCK = 16
"""Characters of a base64 or hex block inside a line (a short encoded name is still a name)."""
MIN_STRING = 4
"""Printable characters in a row that count as a string inside a binary payload."""

_BASE64_LINE_RE = re.compile(r"^[A-Za-z0-9+/_-]+={0,2}$")
_HEX_GROUP = r"(?:0[xX])?(?:[0-9A-Fa-f]{2}){1,4}"
_HEX_DUMP_RE = re.compile(r"(?<![0-9A-Za-z])%s(?:(?:,[ \t]*|[ \t]+|:|(?=0[xX]))%s){2,}(?![0-9A-Za-z])"
                          % (_HEX_GROUP, _HEX_GROUP))
"""A hex dump: groups of one to four bytes with a space, a colon, a comma or a 0x prefix between them (xxd, od,
xxd -i, a C array, a wire capture), three groups or more, and only when the bytes read as text."""
_HEX_NOISE_RE = re.compile(r"0[xX]|[ \t,:]")
_PREFIX_CHARS = 12
"""How far into a base64 block a glued prefix (sha256- of an SRI value, a short word) is looked for."""
_QUOTE_PREFIX_RE = re.compile(r"^[ \t]*(?:>[ \t]?)+")
MIN_LINE_BLOCK = 24
"""Characters per line of a MIME style block (MIME wraps at 76, PEM at 64, other tools at 40 or 32)."""
_BASE64_INLINE_RE = re.compile(
    r"(?<![A-Za-z0-9+/_-])[A-Za-z0-9+/_-]{%d,}={0,2}(?![A-Za-z0-9+/_-])" % MIN_INLINE_BLOCK
)
_HEX_RE = re.compile(r"[0-9a-fA-F]+")
_LATIN_STRING_RE = re.compile(r"[\x20-\x7e\xa0-\xff]{%d,}" % MIN_STRING)

# --------------------------------------------------------------------------- decoding


def utf16_shape(data: bytes) -> str | None:
    """utf-16-le or utf-16-be when one byte of nearly every pair is NUL, else None."""
    head = data[:4096]
    if len(head) < 4:
        return None
    even = head[0::2]
    odd = head[1::2]
    n = min(len(even), len(odd))
    even_nul = even[:n].count(0) / n
    odd_nul = odd[:n].count(0) / n
    if odd_nul > 0.6 and even_nul < 0.1:
        return "utf-16-le"
    if even_nul > 0.6 and odd_nul < 0.1:
        return "utf-16-be"
    return None


def decode_bytes(data: bytes) -> tuple[str, str]:
    """Decode file bytes to text. Returns (text, encoding name)."""
    if data.startswith(b"\xef\xbb\xbf"):
        return data[3:].decode("utf-8", errors="replace"), "utf-8-sig"
    if data.startswith(b"\xff\xfe\x00\x00"):
        return data[4:].decode("utf-32-le", errors="replace"), "utf-32-le"
    if data.startswith(b"\x00\x00\xfe\xff"):
        return data[4:].decode("utf-32-be", errors="replace"), "utf-32-be"
    if data.startswith(b"\xff\xfe"):
        return data[2:].decode("utf-16-le", errors="replace"), "utf-16-le"
    if data.startswith(b"\xfe\xff"):
        return data[2:].decode("utf-16-be", errors="replace"), "utf-16-be"
    # plain ASCII in UTF-16 without a BOM is valid UTF-8 full of NUL characters, so the shape wins then
    shape = utf16_shape(data) if b"\x00" in data[:4096] else None
    if shape is None:
        try:
            return data.decode("utf-8"), "utf-8"
        except UnicodeDecodeError:
            shape = utf16_shape(data)
    if shape:
        return data[: len(data) - len(data) % 2].decode(shape, errors="replace"), shape
    try:
        return data.decode("cp1252"), "cp1252"
    except UnicodeDecodeError:
        return data.decode("latin-1"), "latin-1"


# --------------------------------------------------------------------------- html

_BLOCK_TAGS = (
    "p", "div", "br", "li", "ul", "ol", "tr", "td", "th", "table", "h1", "h2", "h3", "h4", "h5", "h6",
    "section", "article", "header", "footer", "blockquote", "pre", "hr", "title", "dt", "dd", "address",
    "form", "fieldset", "nav", "aside", "main", "figure", "figcaption", "option", "tbody", "thead",
)
_DROP_RE = re.compile(r"<(script|style|template)\b.*?</\1\s*>", re.IGNORECASE | re.DOTALL)
_COMMENT_RE = re.compile(r"<!--.*?-->", re.DOTALL)
_ATTRS = r"""(?:(?>"[^"<]*")|(?>'[^'<]*')|[^<>])"""
"""One character of a tag's attribute part: a quoted value is read whole, so a > inside it does not end the
tag (the way a browser reads it); atomic, so a stray quote costs no backtracking."""
_BLOCK_RE = re.compile(r"</?(%s)\b%s*+>" % ("|".join(_BLOCK_TAGS), _ATTRS), re.IGNORECASE)
_TAG_RE = re.compile(r"<%s++>" % _ATTRS)
_ATTR_RE = re.compile(
    r"""\b(href|src|alt|title|content|value|placeholder|data-[\w-]+)\s*=\s*("([^"]*)"|'([^']*)')""",
    re.IGNORECASE,
)
# a plain text file counts as html only with a real opening tag AND a closing tag, so that "a < b" and
# a stray "<p" in prose do not switch on tag stripping
_HTML_OPEN_RE = re.compile(r"<(html|body|p|div|table|span|a|br)(\s[^<>]*)?/?>", re.IGNORECASE)
_HTML_CLOSE_RE = re.compile(r"</(html|body|p|div|table|span|a)\s*>", re.IGNORECASE)


def looks_html(sample: str) -> bool:
    return bool(_HTML_OPEN_RE.search(sample) and _HTML_CLOSE_RE.search(sample))


def strip_html(source: str) -> str:
    """Visible text of an html document. Block tags become line breaks, other tags vanish."""
    s = _DROP_RE.sub(" ", source)
    s = _COMMENT_RE.sub(" ", s)
    s = _BLOCK_RE.sub("\n", s)
    s = _TAG_RE.sub("", s)
    s = html.unescape(s)
    lines = [re.sub(r"[ \t\r\f\v]+", " ", line).strip() for line in s.split("\n")]
    out: list[str] = []
    for line in lines:
        if line:
            out.append(line)
        elif out and out[-1] != "":
            out.append("")
    return "\n".join(out).strip()


_ANY_ATTR_RE = re.compile(r"""[\w:.-]+\s*=\s*("([^"]*)"|'([^']*)'|([^\s"'=<>`]+))""")
_ANY_TAG_RE = re.compile(r"<[A-Za-z][^<>]*>")


def html_hidden_parts(source: str) -> tuple[list[str], list[str]]:
    """What an html page carries that the stripped text does not show: (comments, other). `other` holds every
    attribute value of every tag and the text of scripts and styles."""
    comments = [html.unescape(m.group(0)[4:-3]).strip() for m in _COMMENT_RE.finditer(source)]
    other = [html.unescape(re.sub(r"<[^>]*>", " ", m.group(0))).strip() for m in _DROP_RE.finditer(source)]
    for tag in _ANY_TAG_RE.finditer(_COMMENT_RE.sub(" ", source)):
        for m in _ANY_ATTR_RE.finditer(tag.group(0)):
            v = next((g for g in m.groups()[1:] if g is not None), "")
            v = html.unescape(v).strip()
            if v:
                other.append(v)
    return [c for c in comments if c], [o for o in other if o]


def html_attribute_values(source: str) -> list[str]:
    """Values of href, src, alt, title, content and similar attributes, for detection."""
    values = []
    for m in _ATTR_RE.finditer(source):
        v = m.group(3) if m.group(3) is not None else m.group(4)
        v = html.unescape(v or "").strip()
        # data: URIs are left out, their base64 payload is decoded by the base64 pass over the raw file
        if v and not v.startswith("#") and not v[:5].lower() == "data:":
            values.append(v)
    return values


# --------------------------------------------------------------------------- base64


def _printable(text: str) -> bool:
    return bool(text) and sum(1 for ch in text if ch.isprintable() or ch in "\n\r\t") >= 0.95 * len(text)


def bytes_to_text(raw: bytes) -> str | None:
    """Decoded bytes when they are text: UTF-8, UTF-16 (with a BOM or with its shape) or Latin-1 prose. None
    for binary. Latin-1 needs printable bytes only and mostly letters, so that random bytes stay binary."""
    if not raw:
        return None
    if raw.startswith((b"\xff\xfe", b"\xfe\xff")) or (b"\x00" in raw and utf16_shape(raw)):
        text, _ = decode_bytes(raw)
        return text if _printable(text) else None
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        if any(b < 0x20 and b not in (9, 10, 13) or 0x7F <= b < 0xA0 for b in raw):
            return None
        text = raw.decode("latin-1")
        letters = sum(1 for ch in text if ch.isalpha() or ch == " ")
        ascii_ = sum(1 for ch in text if ch.isascii())
        if letters < 0.8 * len(text) or ascii_ < 0.7 * len(text):
            return None
    return text if _printable(text) else None


def binary_strings(raw: bytes) -> list[str]:
    """Printable strings of MIN_STRING or more characters in a binary payload, read as Latin-1 and as
    UTF-16 in both byte orders (what the strings tool would show)."""
    out = [m.group(0) for m in _LATIN_STRING_RE.finditer(raw.decode("latin-1"))]
    for start in (0, 1):
        for enc in ("utf-16-le", "utf-16-be"):
            chunk = raw[start: len(raw) - (len(raw) - start) % 2]
            text = chunk.decode(enc, errors="replace")
            out.extend(m.group(0) for m in re.finditer(r"[^\x00-\x1f\ufffd\ud800-\udfff]{%d,}" % MIN_STRING, text)
                       if any(c.isalpha() for c in m.group(0)) and m.group(0).isprintable())
    return out


def _base64_bytes(block: str) -> bytes | None:
    block = block.strip()
    alphabet = "-_" if ("-" in block or "_" in block) else "+/"
    padded = block + "=" * (-len(block) % 4)
    try:
        if alphabet == "-_":
            raw = base64.urlsafe_b64decode(padded)
        else:
            raw = base64.b64decode(padded, validate=True)
    except (binascii.Error, ValueError):
        return None
    return raw or None


def _unprefixed(block: str) -> list[str]:
    """The block without a glued prefix: after a dash or an underscore among its first characters (sha256- of an
    SRI value, which also switched the alphabet) and from the offsets 1 to 3 (a short word in front)."""
    starts = sorted({i + 1 for i, ch in enumerate(block[:_PREFIX_CHARS]) if ch in "-_"} | {1, 2, 3})
    return [block[i:] for i in starts if len(block) - i >= MIN_INLINE_BLOCK]


def _decode_block(block: str) -> str | None:
    """Decode one base64 block to text. None when it is not base64 or not text. A block that does not decode
    is tried again without a glued prefix."""
    block = block.strip()
    if len(block) < MIN_INLINE_BLOCK:
        return None
    for candidate in [block, *_unprefixed(block)]:
        raw = _base64_bytes(candidate)
        text = bytes_to_text(raw) if raw else None
        if text is not None:
            return text
    return None


def _decode_hex(block: str) -> str | None:
    """Decode one hex block (an even number of hex digits, not only digits, five bytes or more) to text. None
    when not text. Inline blocks arrive with MIN_INLINE_BLOCK digits at least; a spaced dump may be shorter."""
    if len(block) < 10 or len(block) % 2 or block.isdigit():
        return None
    try:
        raw = bytes.fromhex(block)
    except ValueError:
        return None
    text = bytes_to_text(raw)
    if text is None or not any(ch.isalnum() for ch in text):
        return None
    # letters, digits and spaces make text; a phone number or an IBAN in hex is mostly digits
    if sum(1 for ch in text if ch.isalnum() or ch.isspace()) < 0.8 * len(text):
        return None
    return text


def _base64_candidates(text: str) -> list[tuple[int, int, str, bool]]:
    """(start, end, block, multiline) for every base64-looking stretch of `text`.

    A run of lines that are all base64 (MIME style, each line 24 or more characters, the last one may
    be shorter, a mail quote prefix in front of each line allowed) is one block. Inside other lines, a run of
    16 or more base64 characters is a block.
    """
    out: list[tuple[int, int, str, bool]] = []
    run: list[str] = []
    run_start = run_end = 0
    pos = 0

    def close() -> None:
        if run:
            out.append((run_start, run_end, "".join(run), len(run) > 1))
            run.clear()

    for line in text.splitlines(keepends=True):
        body = line.rstrip("\r\n")
        stripped = _QUOTE_PREFIX_RE.sub("", body).strip()
        lead = body.find(stripped) if stripped else 0
        # a line of 0x bytes glued together is a hex dump, not a base64 line
        is_b64 = bool(stripped) and bool(_BASE64_LINE_RE.match(stripped)) and not _HEX_DUMP_RE.fullmatch(stripped)
        if run and run[-1].endswith("="):
            close()
        if is_b64 and len(stripped) >= MIN_LINE_BLOCK:
            if not run:
                run_start = pos + lead
            run.append(stripped)
            run_end = pos + lead + len(stripped)
        elif is_b64 and run and len(stripped) <= len(run[0]):
            # the shorter last line of a MIME block
            run.append(stripped)
            run_end = pos + lead + len(stripped)
            close()
        else:
            close()
            for m in _BASE64_INLINE_RE.finditer(body):
                out.append((pos + m.start(), pos + m.end(), m.group(0), False))
            for m in _HEX_DUMP_RE.finditer(body):
                out.append((pos + m.start(), pos + m.end(), _HEX_NOISE_RE.sub("", m.group(0)), False))
        pos += len(line)
    close()
    return sorted(out)


def _decoded_blocks(text: str, kinds: tuple[str, ...] = ("base64",), more: list | None = None):
    """Yield (start, end, decoded text or None, multiline, kind, block) per candidate, None for a binary
    block; `block` is the encoded text with line breaks and quote prefixes taken out.

    A hex string is decoded as hex when "hex" is in `kinds`; a 0x or \\x prefix in front of it is not part of
    it. A hex block that decodes to binary (an embedded object written as hex) is yielded as binary, so that
    its printable strings reach detection. Stops after MAX_BASE64_BLOCKS blocks or MAX_BASE64_DECODED
    characters of decoded text and then appends to `more`, when given, so that the caller knows the text was
    not read in full.
    """
    count = 0
    total = 0
    for start, end, block, multiline in _base64_candidates(text):
        if count >= MAX_BASE64_BLOCKS or total >= MAX_BASE64_DECODED:
            if more is not None:
                more.append(True)
            break
        if block[:2].lower() == "0x" and _HEX_RE.fullmatch(block[2:]):
            start, block = start + 2, block[2:]
        elif block[:1] in "xX" and _HEX_RE.fullmatch(block[1:]):
            start, block = start + 1, block[1:]
        if _HEX_RE.fullmatch(block):
            if "hex" not in kinds or len(block) % 2 or block.isdigit():
                continue
            result = _decode_hex(block)
            count += 1
            if result is not None:
                total += len(result)
            yield start, end, result, multiline, "hex", block
            continue
        result = _decode_block(block)
        if result is None:
            # count as binary only what really has the shape of base64: the alphabet and a decodable length
            # once the padding is completed (a pasted block may have lost a pad character)
            if re.search(r"[0-9+/_-]", block) and _base64_bytes(block) is not None:
                count += 1
                yield start, end, None, multiline, "base64", block
            continue
        count += 1
        total += len(result)
        yield start, end, result, multiline, "base64", block


def find_base64_blocks(text: str) -> tuple[list[str], int]:
    """Decoded texts of the base64 blocks in `text` and the count of blocks that were binary."""
    decoded: list[str] = []
    binary = 0
    for _, _, result, _, kind, _ in _decoded_blocks(text, ("base64", "hex")):
        if kind != "base64":
            continue
        if result is None:
            binary += 1
        else:
            decoded.append(result)
    return decoded, binary


def find_hex_blocks(text: str) -> list[str]:
    """Decoded texts of the hex blocks in `text` that give text, also of a spaced hex dump."""
    return [r for _, _, r, _, kind, _ in _decoded_blocks(text, ("base64", "hex")) if kind == "hex" and r]


def encoded_blocks(text: str, depth: int = 3, more: list | None = None) -> list[tuple[int, str]]:
    """(offset of the block in `text`, what detection must see behind it) for every base64 and hex block:
    the decoded text, the printable strings of a block that decodes to binary and the same again inside what
    was decoded (a block inside a block, at the offset of the outer block), up to `depth` levels. `more` gets
    an entry when a limit left blocks undecoded (see `_decoded_blocks`)."""
    out: list[tuple[int, str]] = []
    for start, end, result, _, kind, block in _decoded_blocks(text, ("base64", "hex"), more):
        if result is None:
            try:
                raw = bytes.fromhex(block) if kind == "hex" else _base64_bytes(block)
            except ValueError:
                raw = None
            if raw:
                strings = binary_strings(raw)
                if strings:
                    out.append((start, "\n".join(strings)))
            continue
        out.append((start, result))
        if depth > 1:
            out.extend((start, inner) for _, inner in encoded_blocks(result, depth - 1, more))
    return out


def encoded_texts(text: str, depth: int = 3, more: list | None = None) -> list[str]:
    """Everything detection must see behind encodings in `text` (see `encoded_blocks`), texts only."""
    return [decoded for _, decoded in encoded_blocks(text, depth, more)]


def expand_base64_blocks(text: str) -> tuple[str, int]:
    """`text` with every base64 or hex block that decodes to text replaced by the decoded text.

    The output must never carry an encoded name that detection cannot see, so the decoded text takes
    the place of the block and is sanitised like any other text. Binary blocks stay as they are.
    Returns the new text and the number of blocks replaced.
    """
    pieces: list[str] = []
    last = 0
    n = 0
    for start, end, result, multiline, kind, _ in _decoded_blocks(text, ("base64", "hex")):
        if result is None:
            continue
        n += 1
        if multiline:
            replacement = "[decoded %s block %d]\n%s\n[end of block %d]" % (kind, n, result.strip("\n"), n)
        else:
            replacement = "[decoded %s: %s]" % (kind, re.sub(r"\s+", " ", result).strip())
        pieces.append(text[last:start])
        pieces.append(replacement)
        last = end
    if not n:
        return text, 0
    pieces.append(text[last:])
    return "".join(pieces), n


# --------------------------------------------------------------------------- csv


def csv_to_markdown(source: str) -> str | None:
    """Render csv text as a Markdown table. None when it does not parse as a table."""
    sample = source[:8192]
    try:
        dialect: type[csv.Dialect] | csv.Dialect = csv.Sniffer().sniff(sample, delimiters=",;\t|")
        delimiter = dialect.delimiter
    except csv.Error:
        dialect = csv.excel
        delimiter = ";" if sample.count(";") > sample.count(",") else ","
    try:
        rows = list(csv.reader(io.StringIO(source), dialect, delimiter=delimiter))
    except csv.Error:
        return None
    rows = [r for r in rows if any(c.strip() for c in r)]
    if not rows:
        return None
    width = max(len(r) for r in rows)
    if width < 2:
        return None
    lines = []
    for i, row in enumerate(rows):
        cells = [_md_cell(c) for c in row] + [""] * (width - len(row))
        lines.append("| " + " | ".join(cells) + " |")
        if i == 0:
            lines.append("|" + " --- |" * width)
    return "\n".join(lines)


def _md_cell(value: str) -> str:
    return value.replace("\r", " ").replace("\n", " ").replace("|", "\\|").strip()


# --------------------------------------------------------------------------- entry point


def extract_text(path: Path, kind: str = "text") -> Extraction:
    """Read a text-like file. `kind` is one of text, html, csv, json."""
    path = Path(path)
    ex = Extraction(path, kind, "ok")
    data = path.read_bytes()
    raw, encoding = decode_bytes(data)
    raw = raw.replace("\r\n", "\n").replace("\r", "\n")
    ex.meta["encoding"] = encoding
    detect_extra: list[str] = []

    scan_extra: list[str] = []
    if kind in ("html", "svg") or (kind == "text" and looks_html(raw[:4096])):
        ex.kind = kind if kind == "svg" else "html"
        ex.text = strip_html(raw)
        detect_extra.extend(html_attribute_values(raw))
        comments, hidden = html_hidden_parts(raw)
        if comments:
            detect_extra.append("html comments:\n" + "\n".join(comments))
            ex.notes.append("%d html comment(s) dropped from the output" % len(comments))
        scan_extra.extend(hidden)
        title = re.search(r"<title[^>]*>(.*?)</title>", raw, re.IGNORECASE | re.DOTALL)
        if title:
            ex.meta["title"] = html.unescape(title.group(1)).strip()
        for m in re.finditer(r"<meta\b[^>]*>", raw, re.IGNORECASE):
            tag = m.group(0)
            name = re.search(r"""\bname\s*=\s*["']([^"']+)["']""", tag, re.IGNORECASE)
            content = re.search(r"""\bcontent\s*=\s*["']([^"']*)["']""", tag, re.IGNORECASE)
            if name and content and name.group(1).lower() in ("author", "description", "keywords", "generator"):
                ex.meta[name.group(1).lower()] = html.unescape(content.group(1)).strip()
    elif kind == "csv":
        table = csv_to_markdown(raw)
        ex.text = table if table is not None else raw
        if table is None:
            ex.notes.append("csv did not parse as a table, kept as text")
    else:
        ex.text = raw

    # detection sees every decoded block of the raw file (also those inside html attributes); the
    # output text carries the decoded text in place of the block, so nothing encoded slips through
    decoded, binary = find_base64_blocks(raw)
    hexed = find_hex_blocks(raw)
    ex.text, expanded = expand_base64_blocks(ex.text)
    if decoded:
        ex.meta["base64_decoded"] = len(decoded)
        ex.notes.append("%d base64 block(s) decoded into detect text" % len(decoded))
    if hexed:
        ex.notes.append("%d hex block(s) decoded into detect text" % len(hexed))
    if expanded:
        ex.notes.append("%d base64 block(s) replaced by their decoded text in the output" % expanded)
    if binary:
        ex.notes.append("%d base64 block(s) decode to binary, not expanded" % binary)
    if kind == "svg":
        ex.notes.append("svg picture, its text nodes are the output, the picture is held on the vault side")

    parts = [ex.text]
    parts.extend(detect_extra)
    for key in ("title", "author", "description", "keywords"):
        if ex.meta.get(key):
            parts.append("%s: %s" % (key, ex.meta[key]))
    for block in decoded:
        parts.append("[decoded base64]\n" + block)
    for block in hexed:
        parts.append("[decoded hex]\n" + block)
    ex.detect_text = "\n".join(p for p in parts if p)
    scan_extra.extend(t for t in encoded_texts(raw) if t not in decoded and t not in hexed)
    ex.scan_text = "\n".join(scan_extra)
    if not ex.text.strip() and not decoded:
        ex.state = "unreadable"
        ex.notes.append("no extractable text")
    return ex
