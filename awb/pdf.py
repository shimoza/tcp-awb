"""One PDF writer for every project, standard library only: a Markdown or HTML deliverable as an A4 PDF.

    awb pdf FILE [--out PDF] [--title T] [--replace]

The host has no office suite, so this is the one route from a text deliverable to a PDF; no project writes its own.
The writer uses the four standard fonts every reader carries (Helvetica, Helvetica-Bold, Helvetica-Oblique, Courier)
in WinAnsi, so it needs no font file and the text stays selectable: pdftotext gives back every heading, list item and
code line. It knows headings h1 to h3, paragraphs with bold, italic and inline code, bullet and numbered lists, code
blocks in Courier that keep their lines and indents (a line longer than the page wraps onto the next line with a drawn
arrow at the break, which is not text, so a copied line comes back as it was written), tables as aligned Courier
text, links as their text with the URL after it and a link annotation, a page number on every page and the title and
author in the document info.

Input: the Markdown the Workbench writes (headings, paragraphs, lists, fenced code, pipe tables, `code`, **bold**,
*italic*, [text](url)) or HTML such as `awb howto render` writes (h1 to h6, p, ul, ol, li, pre, code, a, table,
strong, em, br; any other element as its text). The PDF goes next to FILE unless --out names another file; either
way it must lie inside the project folder of the current folder (its SCOPE.md) or the session scratch folder
(/tmp/claude-UID). A character the standard fonts cannot show (outside WinAnsi, Cyrillic for example) is never
dropped silently: the command refuses with the first line that holds one and the count, unless --replace is given,
which writes "?" for each and says how many. Exit codes: 0 written, 1 refused for such characters, 2 usage or error.

Used by `awb howto render --pdf` and by `awb dataset build` for its HOW-TO.pdf (`render`).
"""
from __future__ import annotations

import html
import io
import os
import re
import sys
import tempfile
from dataclasses import dataclass, field
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import quote

from awb.labclient import SCRATCH

AUTHOR = "Architect Workbench"
PAGE_W, PAGE_H, MARGIN = 595, 842, 56          # A4 in points
TEXT_W = PAGE_W - 2 * MARGIN
MAX_INPUT = 16 * 1024 * 1024
SUFFIXES = {".md": "md", ".markdown": "md", ".html": "html", ".htm": "html"}
FONTS = {"r": ("F1", "Helvetica"), "b": ("F2", "Helvetica-Bold"), "i": ("F3", "Helvetica-Oblique"),
         "c": ("F4", "Courier")}
# kind: (size, leading, space before in leadings)
STYLES = {"h1": (17, 24, 0.5), "h2": (12, 17, 1.0), "h3": (11, 15, 0.6), "p": (10, 14, 0.0), "li": (10, 14, 0.0),
          "code": (9, 12, 0.2), "table": (9, 12, 0.2)}
LIST_INDENT = 16
TAB = 8

# Advance widths in 1/1000 em of the standard fonts (Adobe AFM) for the characters 32 to 126; Oblique is Helvetica.
_HELVETICA = ([278, 278, 355, 556, 556, 889, 667, 191, 333, 333, 389, 584, 278, 333, 278, 278] + [556] * 10
              + [278, 278, 584, 584, 584, 556, 1015, 667, 667, 722, 722, 667, 611, 778, 722, 278, 500, 667, 556, 833,
                 722, 778, 667, 778, 722, 667, 611, 722, 667, 944, 667, 667, 611, 278, 278, 278, 469, 556, 333, 556,
                 556, 500, 556, 556, 278, 556, 556, 222, 222, 500, 222, 833, 556, 556, 556, 556, 333, 500, 278, 556,
                 500, 722, 500, 500, 500, 334, 260, 334, 584])
_BOLD = ([278, 333, 474, 556, 556, 889, 722, 238, 333, 333, 389, 584, 278, 333, 278, 278] + [556] * 10
         + [333, 333, 584, 584, 584, 611, 975, 722, 722, 722, 722, 667, 611, 778, 722, 278, 556, 722, 611, 833, 722,
            778, 667, 778, 722, 667, 611, 722, 667, 944, 667, 667, 611, 333, 278, 333, 584, 556, 333, 556, 611, 556,
            611, 556, 333, 611, 611, 278, 278, 556, 278, 889, 611, 611, 611, 611, 389, 556, 333, 611, 556, 778, 556,
            556, 500, 389, 280, 389, 584])
# above 126 an upper bound, so a line wraps early rather than runs over the margin
_WIDE = {0x85: 1000, 0x89: 1000, 0x8C: 1000, 0x97: 1000, 0x99: 1000, 0xC6: 1000, 0xA0: 278}


class PdfError(Exception):
    """A file that cannot become a PDF: the one line of the refusal."""


class PdfRefused(PdfError):
    """Characters the standard fonts cannot show, without --replace."""


@dataclass
class Block:
    """One block of the document. `runs` are (text, font, url) with font r, b, i or c; `lines` are the lines of a
    code block or the rows (lists of cells) of a table; `marker` is the bullet or number of a list item."""
    kind: str
    runs: list = field(default_factory=list)
    lines: list = field(default_factory=list)
    marker: str = ""
    level: int = 0


def plain(kind: str, text: str) -> Block:
    """A block of one plain text: a code block keeps its lines."""
    if kind in ("code", "table"):
        return Block(kind, lines=text.split("\n"))
    return Block(kind, runs=[(text, "r", None)])


# --------------------------------------------------------------------------- characters

def _showable(ch: str) -> bool:
    if ch in "\t\n\r":
        return True
    if ord(ch) < 32 or ord(ch) == 127:
        return False
    try:
        ch.encode("cp1252")
    except UnicodeEncodeError:
        return False
    return True


def unshowable(text: str) -> list[tuple[int, int]]:
    """[(line number, count)] of the lines that hold characters outside WinAnsi, which the standard fonts lack."""
    out = []
    for n, line in enumerate(text.split("\n"), start=1):
        bad = sum(1 for ch in line if not _showable(ch))
        if bad:
            out.append((n, bad))
    return out


def _encode(text: str) -> bytes:
    return "".join(ch if _showable(ch) else "?" for ch in text).encode("cp1252", "replace")


def _width(text: str, font: str, size: float) -> float:
    if font == "c":
        return 0.6 * size * len(text)
    table = _BOLD if font == "b" else _HELVETICA
    total = 0
    for b in _encode(text):
        total += table[b - 32] if 32 <= b <= 126 else _WIDE.get(b, 667)
    return total * size / 1000.0


def _string(data: bytes) -> bytes:
    return b"(" + data.replace(b"\\", b"\\\\").replace(b"(", b"\\(").replace(b")", b"\\)") + b")"


def _info_string(text: str) -> bytes:
    if all(32 <= ord(ch) < 127 for ch in text):
        return _string(text.encode("ascii"))
    return b"<FEFF" + text.encode("utf-16-be").hex().upper().encode("ascii") + b">"


# --------------------------------------------------------------------------- layout

class _Pages:
    def __init__(self) -> None:
        self.ops: list[list[bytes]] = [[]]
        self.annots: list[list[bytes]] = [[]]
        self.y = PAGE_H - MARGIN

    def new_page(self) -> None:
        self.ops.append([])
        self.annots.append([])
        self.y = PAGE_H - MARGIN

    def need(self, height: float) -> None:
        if self.y - height < MARGIN and self.ops[-1]:
            self.new_page()

    def line(self, lead: float) -> float:
        """The baseline of the next line, on a new page when this one is full."""
        if self.y - lead < MARGIN:
            self.new_page()
        self.y -= lead
        return self.y

    def text(self, x: float, y: float, font: str, size: float, text: str) -> None:
        self.ops[-1].append(b"BT /%s %g Tf %.2f %.2f Td %s Tj ET" % (FONTS[font][0].encode(), size, x, y,
                                                                     _string(_encode(text))))

    def link(self, x1: float, y1: float, x2: float, y2: float, url: str) -> None:
        uri = quote(url, safe=":/?#[]@!$&'()*+,;=%~-._")
        self.annots[-1].append(b"<< /Type /Annot /Subtype /Link /Rect [%.2f %.2f %.2f %.2f] /Border [0 0 0] "
                               b"/A << /S /URI /URI %s >> >>" % (x1, y1, x2, y2, _string(uri.encode("ascii"))))

    def wrap_mark(self, x: float, y: float, size: float) -> None:
        """A drawn hooked arrow after a code line that goes on below: seen, never copied."""
        h = size * 0.7
        self.ops[-1].append(b"q 0.6 w 0.4 G %.2f %.2f m %.2f %.2f l %.2f %.2f l S %.2f %.2f m %.2f %.2f l %.2f %.2f l "
                            b"S Q" % (x + 1, y + h, x + 5, y + h, x + 5, y + 1, x + 2.5, y + 3.5, x + 5, y + 1,
                                      x + 7.5, y + 3.5))


_LINK_SCHEMES = ("http://", "https://", "mailto:")


def _groups(runs: list) -> list[tuple[bool, list]]:
    """The runs as word groups: (space before, [(text, font, url)]); a group never breaks unless it is wider than
    the line. A run "\\n" is a forced break, as (False, None)."""
    out: list[tuple[bool, list | None]] = []
    space, current = False, None
    for text, font, url in runs:
        if text == "\n":
            if current:
                out.append((space, current))
            out.append((False, None))
            space, current = False, None
            continue
        for part in re.split(r"(\s+)", text):
            if not part:
                continue
            if part.isspace():
                if current:
                    out.append((space, current))
                    current = None
                space = True
            else:
                if current is None:
                    current = []
                current.append((part, font, url))
    if current:
        out.append((space, current))
    return out


def _lines(runs: list, width: float, size: float, heading: bool) -> list[list[tuple[float, str, str, str | None]]]:
    """Word-wrapped lines of placed pieces (x, text, font, url)."""
    lines: list[list] = [[]]
    x = 0.0
    for space, group in _groups(runs):
        if group is None:
            lines.append([])
            x = 0.0
            continue
        group = [(t, "b" if heading and f in "ri" else f, u) for t, f, u in group]
        gap = _width(" ", group[0][1], size) if space and lines[-1] else 0.0
        gw = sum(_width(t, f, size) for t, f, _ in group)
        if lines[-1] and x + gap + gw > width:
            lines.append([])
            x, gap = 0.0, 0.0
        x += gap
        for text, font, url in group:
            while len(text) > 1 and x + _width(text, font, size) > width:   # wider than a line: split, never cut
                n = len(text) - 1
                while n > 0 and x + _width(text[:n], font, size) > width:
                    n -= 1
                if n == 0 and x > 0:
                    lines.append([])
                    x = 0.0
                    continue
                n = max(n, 1)
                lines[-1].append((x, text[:n], font, url))
                lines.append([])
                x, text = 0.0, text[n:]
            lines[-1].append((x, text, font, url))
            x += _width(text, font, size)
    return lines


def _draw_runs(pages: _Pages, block: Block, x0: float, width: float) -> None:
    size, lead, before = STYLES[block.kind]
    heading = block.kind in ("h1", "h2", "h3")
    lines = _lines(block.runs, width, size, heading)
    if heading:
        pages.need(lead * (1 + before) + 3 * STYLES["p"][1])      # a heading stays with what follows it
    if before and pages.ops[-1]:
        pages.y -= lead * before
    for i, pieces in enumerate(lines):
        y = pages.line(lead)
        if i == 0 and block.marker:
            mw = _width(block.marker, "r", size)
            pages.text(x0 - mw - 4, y, "r", size, block.marker)
        # pieces of one font and link next to each other become one string, with the space between them
        merged: list[list] = []
        for x, text, font, url in pieces:
            if merged and merged[-1][2] == font and merged[-1][3] == url:
                end = merged[-1][0] + _width(merged[-1][1], font, size)
                spaces = round((x - end) / _width(" ", font, size)) if x > end else 0
                merged[-1][1] += " " * spaces + text
            else:
                merged.append([x, text, font, url])
        for x, text, font, url in merged:
            pages.text(x0 + x, y, font, size, text)
            if url and url.startswith(_LINK_SCHEMES):
                pages.link(x0 + x, y - 2, x0 + x + _width(text, font, size), y + size, url)
    pages.y -= lead * (0.5 if block.kind in ("p", "h1", "h2", "h3") else 0.15)


def _chunks(line: str, cols: int) -> list[str]:
    """A fixed-width line in pieces of at most `cols` characters, broken before a space in the second half of the
    piece when there is one, else at `cols`. Joined, the pieces are the line: a piece after a break at a space starts
    with it, so no reader trims it away."""
    out = []
    while len(line) > cols:
        cut = line.rfind(" ", cols // 2, cols + 1)
        cut = cut if cut > 0 else cols
        out.append(line[:cut])
        line = line[cut:]
    return out + [line]


def _draw_fixed(pages: _Pages, lines: list[str], kind: str) -> None:
    size, lead, before = STYLES[kind]
    cols = max(int(TEXT_W / (0.6 * size)) - 2, 10)       # room for the wrap mark
    if pages.ops[-1]:
        pages.y -= lead * before
    for line in lines:
        line = line.expandtabs(TAB).rstrip()
        chunks = _chunks(line, cols)
        for n, chunk in enumerate(chunks):
            y = pages.line(lead)
            if chunk:
                pages.text(MARGIN, y, "c", size, chunk)
            if n < len(chunks) - 1:
                pages.wrap_mark(MARGIN + 0.6 * size * len(chunk) + 1, y, size)
    pages.y -= lead * 0.6


def _table_lines(rows: list[list[str]]) -> list[str]:
    rows = [[" ".join(c.split()) for c in r] for r in rows if r]
    if not rows:
        return []
    n = max(len(r) for r in rows)
    rows = [r + [""] * (n - len(r)) for r in rows]
    widths = [max(len(r[i]) for r in rows) for i in range(n)]
    out = [" | ".join(c.ljust(w) for c, w in zip(r, widths)).rstrip() for r in rows]
    out.insert(1, "-+-".join("-" * w for w in widths))
    return out


def layout(blocks: list[Block]) -> _Pages:
    pages = _Pages()
    for b in blocks:
        if b.kind == "code":
            _draw_fixed(pages, b.lines, "code")
        elif b.kind == "table":
            _draw_fixed(pages, _table_lines(b.lines), "table")
        elif b.kind == "li":
            indent = LIST_INDENT * (b.level + 1)
            _draw_runs(pages, b, MARGIN + indent, TEXT_W - indent)
        elif b.kind in STYLES:
            if any(t.strip() for t, _, _ in b.runs):
                _draw_runs(pages, b, MARGIN, TEXT_W)
        else:
            raise PdfError("unknown block %s" % b.kind)
    return pages


def render(blocks: list[Block], title: str, author: str = AUTHOR) -> bytes:
    """The PDF of the blocks: A4, the standard fonts, a page number on every page, title and author in the info."""
    pages = layout(blocks)
    total = len(pages.ops)
    for n, ops in enumerate(pages.ops, start=1):
        label = "%d / %d" % (n, total)
        ops.append(b"BT /F1 8 Tf %.2f %d Td %s Tj ET" % ((PAGE_W - _width(label, "r", 8)) / 2, MARGIN // 2,
                                                         _string(label.encode("ascii"))))
    objs: list[bytes] = []

    def add(body: bytes) -> int:
        objs.append(body)
        return len(objs)

    font_ids = {key: add(b"<< /Type /Font /Subtype /Type1 /BaseFont /%s /Encoding /WinAnsiEncoding >>"
                         % name.encode()) for key, name in FONTS.values()}
    res = b"<< /Font << " + b" ".join(b"/%s %d 0 R" % (k.encode(), v) for k, v in font_ids.items()) + b" >> >>"
    pages_id = len(objs) + 1 + sum(2 + len(a) for a in pages.annots)
    page_ids = []
    for ops, annots in zip(pages.ops, pages.annots):
        stream = b"\n".join(ops)
        cid = add(b"<< /Length %d >>\nstream\n" % len(stream) + stream + b"\nendstream")
        annot_ids = [add(a) for a in annots]
        extra = b" /Annots [%s]" % b" ".join(b"%d 0 R" % i for i in annot_ids) if annot_ids else b""
        page_ids.append(add(b"<< /Type /Page /Parent %d 0 R /MediaBox [0 0 %d %d] /Resources %s /Contents %d 0 R%s >>"
                            % (pages_id, PAGE_W, PAGE_H, res, cid, extra)))
    assert add(b"<< /Type /Pages /Kids [" + b" ".join(b"%d 0 R" % i for i in page_ids) + b"] /Count %d >>"
               % len(page_ids)) == pages_id
    catalog = add(b"<< /Type /Catalog /Pages %d 0 R >>" % pages_id)
    info = add(b"<< /Title %s /Author %s /Producer (awb pdf) >>" % (_info_string(title), _info_string(author)))
    out = io.BytesIO()
    out.write(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")
    offsets = []
    for i, body in enumerate(objs, 1):
        offsets.append(out.tell())
        out.write(b"%d 0 obj\n" % i + body + b"\nendobj\n")
    xref = out.tell()
    out.write(b"xref\n0 %d\n0000000000 65535 f \n" % (len(objs) + 1))
    for o in offsets:
        out.write(b"%010d 00000 n \n" % o)
    out.write(b"trailer\n<< /Size %d /Root %d 0 R /Info %d 0 R >>\nstartxref\n%d\n%%%%EOF\n"
              % (len(objs) + 1, catalog, info, xref))
    return out.getvalue()


# --------------------------------------------------------------------------- Markdown

_INLINE_RE = re.compile(
    r"\\(?P<esc>[!-/:-@\[-`{-~])"
    r"|!\[(?P<alt>[^\]]*)\]\([^)]*\)"
    r"|\[(?P<ltext>[^\]]+)\]\((?P<lurl>[^)\s]+)(?:\s+\"[^\"]*\")?\)"
    r"|<(?P<auto>(?:https?://|mailto:)[^>\s]+)>"
    r"|(?P<tick>`+)(?P<code>.+?)(?P=tick)"
    r"|\*\*(?P<bold>.+?)\*\*|(?<!\w)__(?P<bold2>.+?)__(?!\w)"
    r"|(?<![\w*])\*(?P<em>[^*\s](?:[^*]*[^*\s])?)\*(?![\w*])|(?<!\w)_(?P<em2>[^_\s](?:[^_]*[^_\s])?)_(?!\w)"
    r"|(?P<bare>https?://[^\s<>()]*[^\s<>().,;:!?'\"])")


def _runs(text: str, font: str = "r", url: str | None = None) -> list:
    out: list = []
    pos = 0
    for m in _INLINE_RE.finditer(text):
        if m.start() > pos:
            out.append((text[pos:m.start()], font, url))
        pos = m.end()
        g = m.groupdict()
        if g["esc"] is not None:
            out.append((g["esc"], font, url))
        elif g["alt"] is not None:
            if g["alt"]:
                out.append((g["alt"], font, url))
        elif g["ltext"] is not None:
            out += _runs(g["ltext"], font, g["lurl"])
            if g["ltext"].strip() != g["lurl"]:
                out.append((" (%s)" % g["lurl"], font, g["lurl"]))
        elif g["auto"] is not None or g["bare"] is not None:
            link = g["auto"] or g["bare"]
            out.append((link, font, link))
        elif g["code"] is not None:
            out.append((g["code"].strip() if g["code"].strip() else g["code"], "c", url))
        elif g["bold"] is not None or g["bold2"] is not None:
            out += _runs(g["bold"] or g["bold2"], "b" if font != "c" else font, url)
        else:
            out += _runs(g["em"] or g["em2"], "i" if font == "r" else font, url)
    if pos < len(text):
        out.append((text[pos:], font, url))
    return out


def _plain_text(runs: list) -> str:
    return "".join(t for t, _, _ in runs).strip()


_FENCE_RE = re.compile(r"^(\s*)(`{3,}|~{3,})(.*)$")
_HEAD_RE = re.compile(r"^\s{0,3}(#{1,6})\s+(.*?)(?:\s+#+)?\s*$")
_ITEM_RE = re.compile(r"^(\s*)([-*+]|\d{1,9}[.)])\s+(.*)$")
_RULE_RE = re.compile(r"^\s{0,3}([-*_])(\s*\1){2,}\s*$")
_TABLE_SEP_RE = re.compile(r"^\s*\|?\s*:?-{2,}:?\s*(\|\s*:?-{2,}:?\s*)*\|?\s*$")


def _cells(line: str) -> list[str]:
    line = line.strip()
    if line.startswith("|"):
        line = line[1:]
    if line.endswith("|") and not line.endswith("\\|"):
        line = line[:-1]
    cells = re.split(r"(?<!\\)\|", line)
    return [_plain_text(_runs(c.strip())) for c in cells]


def from_markdown(text: str) -> tuple[list[Block], str]:
    """(blocks, the text of the first h1 or "") of the Markdown the Workbench writes."""
    lines = text.split("\n")
    blocks: list[Block] = []
    para: list[str] = []
    indents: list[int] = []          # the indents of the open list levels
    in_list = False

    def flush() -> None:
        if para:
            blocks.append(Block("p", runs=_runs(" ".join(para))))
            para.clear()

    i = 0
    while i < len(lines):
        line = lines[i].rstrip("\r")
        fence = _FENCE_RE.match(line)
        if fence:
            flush()
            mark, cut = fence.group(2), len(fence.group(1))
            body = []
            i += 1
            while i < len(lines) and not re.match(r"^\s*%s%s*\s*$" % (re.escape(mark[0]), re.escape(mark)),
                                                  lines[i]):
                raw = lines[i].rstrip("\r")
                strip = len(raw) - len(raw.lstrip(" "))
                body.append(raw[min(cut, strip):])
                i += 1
            blocks.append(Block("code", lines=body))
            i += 1
            continue
        if line.lstrip().startswith("<!--"):
            flush()
            while i < len(lines) and "-->" not in lines[i]:
                i += 1
            i += 1
            continue
        head = _HEAD_RE.match(line)
        item = _ITEM_RE.match(line)
        if head:
            flush()
            in_list = False
            blocks.append(Block("h%d" % min(len(head.group(1)), 3), runs=_runs(head.group(2))))
        elif line.lstrip().startswith("|") and i + 1 < len(lines) and _TABLE_SEP_RE.match(lines[i + 1]):
            flush()
            in_list = False
            rows = [_cells(line)]
            i += 2
            while i < len(lines) and lines[i].strip().startswith("|"):
                rows.append(_cells(lines[i]))
                i += 1
            blocks.append(Block("table", lines=rows))
            continue
        elif _RULE_RE.match(line):
            flush()
            in_list = False
        elif item:
            flush()
            indent = len(item.group(1).expandtabs(4))
            if not in_list:
                indents = []
            while indents and indent < indents[-1]:
                indents.pop()
            if not indents or indent > indents[-1]:
                indents.append(indent)
            in_list = True
            marker = item.group(2)
            marker = "\u2022" if marker in "-*+" else marker.rstrip(")").rstrip(".") + "."
            blocks.append(Block("li", runs=_runs(item.group(3)), marker=marker, level=len(indents) - 1))
        elif not line.strip():
            flush()
        elif in_list and line[:1] in " \t" and blocks and blocks[-1].kind == "li":
            blocks[-1].runs += [("\n", "r", None)] + _runs(line.strip())        # a line that belongs to the item
        else:
            in_list = False
            stripped = line.strip()
            para.append(stripped[1:].strip() if stripped.startswith(">") else stripped)
        i += 1
    flush()
    title = next((_plain_text(b.runs) for b in blocks if b.kind == "h1"), "")
    return blocks, title


# --------------------------------------------------------------------------- HTML

class _Html(HTMLParser):
    SKIP = {"script", "style", "head"}
    HEADINGS = {"h1": "h1", "h2": "h2", "h3": "h3", "h4": "h3", "h5": "h3", "h6": "h3"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.blocks: list[Block] = []
        self.current: Block | None = None
        self.lists: list[list] = []          # [kind, next number]
        self.fonts: list[str] = []
        self.url: str | None = None
        self.link_text = ""
        self.skip = 0
        self.title: str | None = None
        self.in_title = False
        self.pre: list[str] | None = None
        self.rows: list[list[str]] | None = None
        self.cell: list[str] | None = None

    def font(self) -> str:
        return self.fonts[-1] if self.fonts else "r"

    def end_block(self) -> None:
        if self.current is not None and _plain_text(self.current.runs):
            self.blocks.append(self.current)
        self.current = None

    def block(self) -> Block:
        if self.current is None:
            if self.lists:
                self.current = Block("li", level=len(self.lists) - 1)
            else:
                self.current = Block("p")
        return self.current

    def handle_starttag(self, tag: str, attrs: list) -> None:
        a = dict(attrs)
        if tag in self.SKIP:
            self.skip += 1
        elif tag == "title":
            self.in_title, self.title = True, ""
        elif tag in self.HEADINGS:
            self.end_block()
            self.current = Block(self.HEADINGS[tag])
        elif tag in ("p", "div"):
            if self.current is not None and self.current.kind == "li":
                if _plain_text(self.current.runs):
                    self.current.runs.append(("\n", "r", None))
            else:
                self.end_block()
        elif tag == "br":
            if self.pre is not None:
                self.pre.append("\n")
            elif self.current is not None:
                self.current.runs.append(("\n", "r", None))
        elif tag in ("ul", "ol"):
            self.end_block()
            start = a.get("start") or "1"
            self.lists.append([tag, int(start) if start.isdigit() else 1])
        elif tag == "li":
            self.end_block()
            if not self.lists:
                self.lists.append(["ul", 1])
            kind = self.lists[-1]
            marker = "\u2022"
            if kind[0] == "ol":
                marker = "%d." % kind[1]
                kind[1] += 1
            self.current = Block("li", marker=marker, level=len(self.lists) - 1)
        elif tag == "pre":
            self.end_block()
            self.pre = []
        elif tag == "code" and self.pre is None:
            self.fonts.append("c")
        elif tag in ("strong", "b"):
            self.fonts.append("c" if self.font() == "c" else "b")
        elif tag in ("em", "i"):
            self.fonts.append(self.font() if self.font() in "bc" else "i")
        elif tag == "a":
            self.url, self.link_text = a.get("href") or None, ""
        elif tag == "table":
            self.end_block()
            self.rows = []
        elif tag == "tr" and self.rows is not None:
            self.rows.append([])
        elif tag in ("td", "th") and self.rows is not None:
            if not self.rows:
                self.rows.append([])
            self.cell = []

    def handle_endtag(self, tag: str) -> None:
        if tag in self.SKIP:
            self.skip = max(0, self.skip - 1)
        elif tag == "title":
            self.in_title = False
        elif tag in self.HEADINGS or tag == "li":
            self.end_block()
        elif tag == "p" and (self.current is None or self.current.kind != "li"):
            self.end_block()
        elif tag in ("ul", "ol"):
            self.end_block()
            if self.lists:
                self.lists.pop()
        elif tag == "pre" and self.pre is not None:
            body = "".join(self.pre)
            body = body[1:] if body.startswith("\n") else body
            self.blocks.append(Block("code", lines=body.rstrip("\n").split("\n")))
            self.pre = None
        elif tag == "code" and self.pre is None and self.fonts:
            self.fonts.pop()
        elif tag in ("strong", "b", "em", "i") and self.fonts:
            self.fonts.pop()
        elif tag == "a" and self.url:
            if self.cell is None and self.pre is None and self.link_text.strip() != self.url:
                self.block().runs.append((" (%s)" % self.url, self.font(), self.url))
            self.url = None
        elif tag in ("td", "th") and self.cell is not None and self.rows is not None:
            self.rows[-1].append(" ".join("".join(self.cell).split()))
            self.cell = None
        elif tag == "table" and self.rows is not None:
            self.blocks.append(Block("table", lines=[r for r in self.rows if r]))
            self.rows = None

    def handle_data(self, data: str) -> None:
        if self.skip:
            return
        if self.in_title:
            self.title = (self.title or "") + data
            return
        if self.pre is not None:
            self.pre.append(data)
            return
        if self.url:
            self.link_text += data
        if self.cell is not None:
            self.cell.append(data)
            return
        if self.rows is not None:
            return
        if self.current is None and not data.strip():
            return
        self.block().runs.append((data, self.font(), self.url))


def from_html(text: str) -> tuple[list[Block], str]:
    """(blocks, the <title> or the first h1 or "") of an HTML page such as `awb howto render` writes."""
    p = _Html()
    p.feed(text)
    p.close()
    p.end_block()
    title = " ".join((p.title or "").split()) or next((_plain_text(b.runs) for b in p.blocks if b.kind == "h1"), "")
    return p.blocks, title


# --------------------------------------------------------------------------- the file and the command

def allowed(target: Path, start: Path | None = None) -> bool:
    """True when the target lies inside the project folder of `start` (default: the current folder) or the session
    scratch folder."""
    from awb import config

    roots = []
    project = config.scope_root(Path(os.path.abspath(start or Path.cwd())))
    if project is not None:
        roots.append(os.path.realpath(project))
    roots.append(os.path.realpath(SCRATCH % os.getuid()))
    real = os.path.join(os.path.realpath(os.path.dirname(os.path.abspath(target))), os.path.basename(target))
    return any(real.startswith(r.rstrip("/") + "/") for r in roots)


def convert(path: Path, out: Path | None = None, title: str | None = None, replace: bool = False,
            start: Path | None = None, author: str = AUTHOR) -> tuple[Path, int, int]:
    """Write the PDF of a Markdown or HTML file; (the PDF, pages, characters written as "?")."""
    path = Path(path)
    kind = SUFFIXES.get(path.suffix.lower())
    if kind is None:
        raise PdfError("awb pdf takes a Markdown or HTML file (.md, .markdown, .html, .htm)")
    if os.path.islink(path) or not path.is_file():
        raise PdfError("not a regular file: %s" % path)
    if path.stat().st_size > MAX_INPUT:
        raise PdfError("larger than %d bytes" % MAX_INPUT)
    try:
        text = path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        raise PdfError("the file is not UTF-8") from None
    target = Path(out) if out else path.with_suffix(".pdf")
    if target.suffix.lower() != ".pdf":
        raise PdfError("the output is a .pdf file")
    if not allowed(target, start):
        raise PdfError("refused: the PDF goes inside the project folder or the session scratch folder (%s)"
                       % (SCRATCH % os.getuid()))
    if os.path.islink(target) or target.is_dir():
        raise PdfError("a link or a folder stands where the PDF should be")
    if not target.parent.is_dir():
        raise PdfError("no such folder: %s" % target.parent)
    bad = unshowable(html.unescape(text) if kind == "html" else text)
    count = sum(n for _, n in bad)
    if bad and not replace:
        raise PdfRefused("line %d: %d character(s) the standard fonts cannot show (outside WinAnsi), %d in %d line(s) "
                         "in all; nothing written; give --replace to write ? for them" % (bad[0][0], bad[0][1],
                                                                                         count, len(bad)))
    blocks, found = from_html(text) if kind == "html" else from_markdown(text)
    data = render(blocks, title or found or path.stem, author)
    fd, tmp = tempfile.mkstemp(prefix=".%s." % target.name, dir=target.parent)
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
        os.chmod(tmp, 0o644 & ~_umask())
        os.replace(tmp, target)
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise
    return target, data.count(b"/Type /Page "), count


def _umask() -> int:
    mask = os.umask(0)
    os.umask(mask)
    return mask


def main(argv: list[str] | None = None) -> int:
    from awb.cli import SafeParser

    ap = SafeParser(prog="awb pdf", description="A PDF of a Markdown or HTML deliverable, next to it, with the "
                                                "standard fonts and selectable text.")
    ap.add_argument("file", help="the Markdown (.md) or HTML (.html) file")
    ap.add_argument("--out", default=None, metavar="PDF", help="the PDF (default: FILE with .pdf, next to it); inside the project "
                                                "folder or the session scratch folder")
    ap.add_argument("--title", default=None, metavar="T", help="the title in the document info (default: the first heading)")
    ap.add_argument("--replace", action="store_true",
                    help="write ? for a character the standard fonts cannot show (default: refuse)")
    try:
        args = ap.parse_args(argv)
    except SystemExit as exc:
        return 0 if exc.code == 0 else 2
    try:
        out, pages, replaced = convert(Path(args.file), Path(args.out) if args.out else None, args.title,
                                       args.replace)
    except PdfRefused as err:
        print("awb pdf: %s" % err, file=sys.stderr)
        return 1
    except PdfError as err:
        print("awb pdf: %s" % err, file=sys.stderr)
        return 2
    except OSError as err:
        print("awb pdf: %s (operating system error)" % type(err).__name__, file=sys.stderr)
        return 2
    print("pdf: %s, %d page(s)" % (out, pages))
    if replaced:
        print("pdf: %d character(s) the standard fonts cannot show written as ?" % replaced)
    return 0
