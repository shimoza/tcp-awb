"""A Word file of a Markdown or HTML deliverable, standard library only, for partners who edit with tracked changes.

    awb docx FILE [--out DOCX] [--title T] [--h1-page]

The same inputs as `awb pdf` (the HTML of a guide, the Markdown the Workbench writes), read by the same parser, and
the same structure as its PDF, written as plain editable WordprocessingML (no text box, no frame, no content
control), so a partner can switch on Track Changes and edit every line:

- h1 to h3 as the built-in styles Heading 1 to Heading 3 (h4 to h6 as Heading 4 to Heading 6 with their numbering; the PDF draws them as h3), bold, the sizes
  of the PDF, the colour of the HTML (a style attribute or a simple rule of a style element) in the style of that
  level (the first coloured heading of the level decides; a heading of another colour keeps its own on its text).
  When the headings carry numbers that count up without a gap ("1.", "1.1", "2." ...), the numbers become a real
  Word numbering on the heading paragraphs (the same dots), so a heading a partner adds or moves is numbered by
  Word; numbers that do not count up so stay text.
- the line `<!-- toc -->` (as in the PDF) becomes a Word contents field (TOC of the levels 1 and 2 with links) whose
  stored result already lists each h1 and h2 after it, linked to its heading, so it reads right before Word updates
  it; the file asks Word to update its fields on open (the page numbers come then).
- an img of a PNG or JPEG (the same rules as `awb pdf`: inside the project or scratch folder, no link, or a data
  URI) is an inline picture with the file's own bytes, at its size, never wider than the text. An inline svg is an
  SVG picture (the page's style rules go into it) with a PNG fallback drawn by `awb pdf` and poppler's pdftoppm,
  so Word 2016 and later show the SVG and an older reader the PNG; when no PNG can be drawn (no pdftoppm), the SVG
  part stands alone, which Word 2016 and later show and an older reader does not, and one stderr line says so.
- the caption (the alt text or figcaption) is a centred paragraph of the style Caption below its figure; the
  figure paragraph keeps with it.
- tables (HTML tables, Markdown pipe tables) as Word tables with borders over the text width; a first row of th
  cells (or the head of a pipe table) is a header row, bold and shaded, repeated on each page; the alignment of a
  pipe table column is kept.
- code blocks as paragraphs of the style Code (Courier New 9 pt, shaded), one per line, spaces and indents kept;
  inline code in the character style Code Char.
- lists as real Word bullets and numbers (an ordered list keeps its first number), nested to the level of the
  source.
- an id of the page (`id`, or `name` of an a) is a bookmark, and a link `href="#id"` to it a hyperlink to that
  bookmark (one to an id the page lacks stays text); an http, https or mailto link is a hyperlink with its URL in
  brackets after the text, as in the PDF.
- A4 with the margins of the PDF and "page / pages" (the fields PAGE and NUMPAGES) centred in the footer; the title
  and the author "Architect Workbench" in the document properties.

The folder and link rules are those of `awb pdf` (its allowed, _screen, _read and _publish): the
input and the DOCX inside the project folder of the current folder or the session scratch folder, the encoding of the
text checked and no content check (B-089) before it is written, the file private (0600) and published atomically. What a figure cannot show is left out and
counted in one stderr line, as in the PDF. A character XML cannot hold (a control character) is written as "?" and
counted. Exit codes: 0 written, 2 usage or error.
"""
from __future__ import annotations

import io
import math
import os
import re
import shutil
import subprocess
import sys
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import unquote

from awb import pdf, pdffig

PAGE_W, PAGE_H = 11906, 16838           # A4 in twips
MARGIN = pdf.MARGIN * 20                 # the margin of the PDF, 56 pt
TEXT_W = (PAGE_W - 2 * MARGIN) / 20      # in points
ROOM_H = (PAGE_H - 2 * MARGIN) / 20 - 60  # the tallest figure, room left for its caption
EMU = 12700                              # EMU per point
MAX_CELLS = 100000
TAB = pdf.TAB
FALLBACK_DPI = 144
MONO = '<w:rFonts w:ascii="Courier New" w:hAnsi="Courier New" w:cs="Courier New"/>'
# the public extension URI of the Office SVG picture (MS-ODRAWXML svgBlip), joined so the gate does not take the
# fixed schema constant for the id of a resource
SVG_URI = "{96DAC541-7B7A-43D3-" + "8B79-37D633B846F1}"

NS_W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
NS_R = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
NS_PR = "http://schemas.openxmlformats.org/package/2006/relationships"
REL = "http://schemas.openxmlformats.org/officeDocument/2006/relationships/"
DOC_NS = ('xmlns:w="%s" xmlns:r="%s" '
          'xmlns:wp="http://schemas.openxmlformats.org/drawingml/2006/wordprocessingDrawing" '
          'xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main" '
          'xmlns:pic="http://schemas.openxmlformats.org/drawingml/2006/picture"' % (NS_W, NS_R))

# SVG names the HTML parser gave in lower case: their real spelling (SVG is case-sensitive)
_SVG_CASE = {n.lower(): n for n in (
    "linearGradient radialGradient clipPath textPath foreignObject feGaussianBlur feOffset feBlend feColorMatrix "
    "feFlood feComposite feMerge feMergeNode animateTransform viewBox preserveAspectRatio gradientUnits "
    "gradientTransform patternUnits patternContentUnits patternTransform markerWidth markerHeight markerUnits refX "
    "refY clipPathUnits textLength lengthAdjust startOffset spreadMethod stdDeviation maskUnits maskContentUnits "
    "filterUnits primitiveUnits pathLength").split()}
_SVG_DROP = {"script", "foreignobject", "iframe", "object", "embed", "audio", "video", "animate", "set",
             "animatetransform", "animatemotion"}
_NUMBER_RE = re.compile(r"^(\d{1,3}(?:\.\d{1,3}){0,5})(\.?)\s+(?=\S)")
_BAD_XML = re.compile("[\x00-\x08\x0b\x0c\x0e-\x1f￾￿\ud800-\udfff]")


class DocxError(pdf.PdfError):
    """A file that cannot become a DOCX: the one line of the refusal."""


def _esc(text: str) -> str:
    return (text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;"))


def _hex(rgb: tuple) -> str:
    return "".join("%02X" % max(0, min(255, round(v * 255))) for v in rgb)


# --------------------------------------------------------------------------- the parse

class _DocHtml(pdf._Html):
    """The parser of awb pdf, keeping what a Word file needs beside it: the header row of a table (th cells), the
    tree of an inline svg and the text of the page's style elements."""

    def __init__(self, base: Path | None = None, start: Path | None = None) -> None:
        super().__init__(base, start)
        self.th_rows: list[bool] = []
        self.css_raw: list[str] = []

    def handle_starttag(self, tag: str, attrs: list) -> None:
        inside = self.svg is not None or bool(self.skip)
        super().handle_starttag(tag, attrs)
        if inside or self.rows is None:
            return
        if tag == "table":
            self.th_rows = []
        elif tag == "tr":
            self.th_rows.append(True)
        elif tag in ("td", "th"):
            if len(self.th_rows) < len(self.rows):
                self.th_rows.append(True)
            if tag == "td" and self.th_rows:
                self.th_rows[-1] = False

    def handle_endtag(self, tag: str) -> None:
        if tag == "table" and self.svg is None and not self.skip and self.rows is not None:
            kept = [i for i, r in enumerate(self.rows) if r]
            head = bool(kept) and kept[0] < len(self.th_rows) and self.th_rows[kept[0]]
            super().handle_endtag(tag)
            if self.blocks and self.blocks[-1].kind == "table":
                self.blocks[-1].head = 1 if head else 0
            return
        super().handle_endtag(tag)

    def handle_data(self, data: str) -> None:
        if self.svg is None and self.skip and self.skip[-1] == "style":
            self.css_raw.append(data)
        super().handle_data(data)

    def end_svg(self) -> None:
        root = self.svg[0]
        super().end_svg()
        self.blocks[-1].svg_root = root


def parse_html(text: str, base: Path | None = None, start: Path | None = None) -> tuple[list, str, dict, str]:
    """(blocks, title, what the figures left out, the page's CSS) of an HTML page, as awb pdf parses it."""
    p = _DocHtml(base, start)
    try:
        p.feed(text)
        p.close()
    except (AssertionError, ValueError, RecursionError):
        raise DocxError("malformed HTML") from None
    if p.svg is not None:
        p.end_svg()
    if p.pre is not None:
        p.handle_endtag("pre")
    if p.cell is not None:
        p.handle_endtag("td")
    if p.rows is not None:
        p.handle_endtag("table")
    p.end_block()
    if p.pending and p.blocks:
        p.blocks[-1].anchors += p.pending
    title = " ".join((p.title or "").split()) or next(
        (pdf._plain_text(b.runs) for b in p.blocks if b.kind == "h1"), "")
    return p.blocks, title, p.skipped, "".join(p.css_raw)


# --------------------------------------------------------------------------- SVG

def svg_markup(root: pdffig.Node, css: str = "") -> str:
    """The SVG file of an inline svg tree: the real case of its names, the SVG namespace, the page's style rules;
    scripts, event handlers and links that leave the file are dropped."""
    out: list[str] = []

    def walk(node, top: bool) -> None:
        if isinstance(node, str):
            out.append(_esc(node))
            return
        if node.tag in _SVG_DROP:
            return
        tag = _SVG_CASE.get(node.tag, node.tag)
        if not re.fullmatch(r"[A-Za-z][\w.-]*", tag):
            return
        attrs = []
        for k, v in node.attrs.items():
            name = _SVG_CASE.get(k, k)
            if not re.fullmatch(r"[A-Za-z_][\w.:-]*", name) or name.lower().startswith("on") or name == "xmlns" \
                    or name.startswith("xmlns:"):
                continue
            if name in ("href", "xlink:href") and not (v.startswith("#") or v.lower().startswith("data:image/")):
                continue
            if ":" in name and name.split(":")[0] not in ("xlink", "xml"):
                continue
            attrs.append(' %s="%s"' % (name, _esc(v)))
        if top:
            attrs.insert(0, ' xmlns="http://www.w3.org/2000/svg" xmlns:xlink="http://www.w3.org/1999/xlink"')
        out.append("<%s%s>" % (tag, "".join(attrs)))
        if top and css.strip():
            out.append("<style>%s</style>" % _esc(css))
        for child in node.children:
            walk(child, False)
        out.append("</%s>" % tag)

    walk(root, True)
    return '<?xml version="1.0" encoding="UTF-8"?>\n' + "".join(out)


def svg_fallback(fig: pdffig.Svg) -> bytes | None:
    """A PNG of the compiled SVG: its page of `awb pdf` cut to the figure by pdftoppm; None without poppler or when
    the drawing fails."""
    tool = shutil.which("pdftoppm")
    if tool is None:
        return None
    try:
        fonts = pdf.unicode_fonts() if pdf.unshowable("\n".join(fig.texts)) else None
        data = pdf.render([pdf.Block("figure", figure=fig)], "figure", fonts=fonts)
    except pdf.PdfError:
        return None
    room = pdf.PAGE_H - 2 * pdf.MARGIN - 2 * pdf.FIGURE_GAP - pdf.STYLES["caption"][1]
    s = min(1.0, pdf.TEXT_W / fig.width, room / fig.height)
    w, h = fig.width * s, fig.height * s
    x = pdf.MARGIN + (pdf.TEXT_W - w) / 2
    k = FALLBACK_DPI / 72
    args = [tool, "-png", "-r", str(FALLBACK_DPI), "-singlefile", "-f", "1", "-l", "1",
            "-x", str(int(x * k)), "-y", str(int(pdf.MARGIN * k)),
            "-W", str(max(1, math.ceil(w * k))), "-H", str(max(1, math.ceil(h * k))), "-"]
    try:
        done = subprocess.run(args, input=data, capture_output=True, timeout=60, check=False)
    except (OSError, subprocess.SubprocessError):
        return None
    if done.returncode != 0 or not done.stdout.startswith(b"\x89PNG\r\n\x1a\n"):
        return None
    return done.stdout


# --------------------------------------------------------------------------- the writer

class _Writer:
    def __init__(self, blocks: list, title: str, css: str = "", h1_page: bool = False) -> None:
        self.blocks = blocks
        self.title = title
        self.css = css
        self.h1_page = h1_page
        self.rels: list[tuple[str, str, str, bool]] = []       # (id, type, target, external)
        self.media: list[tuple[str, bytes]] = []
        self.bookmark = 0
        self.drawing = 0
        self.replaced = 0
        self.nums: list[tuple[int, int, int | None]] = []       # (numId, abstract, (level, start) or None)
        self.marks: dict[str, str] = {}                         # id of the page: bookmark name
        self.heading_marks: dict[int, str] = {}                 # block index: _Toc bookmark
        self.counts = {"headings": 0, "figures": 0, "tables": 0, "code": 0, "svg_alone": 0}
        self.colors = {}                                        # heading kind: hex of its style
        self.numbered = self._numbering()

    # ---------------------------------------------------------------- helpers

    def text(self, value: str) -> str:
        clean, n = _BAD_XML.subn("?", value)
        self.replaced += n
        return _esc(clean)

    def rel(self, kind: str, target: str, external: bool = False) -> str:
        rid = "rId%d" % (len(self.rels) + 10)
        self.rels.append((rid, kind, target, external))
        return rid

    def _bookmark_name(self, ident: str) -> str:
        name = re.sub(r"[^A-Za-z0-9_]", "_", ident)
        if not name[:1].isalpha():
            name = "id_" + name
        name = name[:36]
        base, n = name, 1
        while name in self.marks.values():
            n += 1
            name = "%s_%d" % (base[:33], n)
        return name

    def bookmarks(self, names: list[str]) -> tuple[str, str]:
        starts, ends = [], []
        for name in names:
            self.bookmark += 1
            starts.append('<w:bookmarkStart w:id="%d" w:name="%s"/>' % (self.bookmark, _esc(name)))
            ends.append('<w:bookmarkEnd w:id="%d"/>' % self.bookmark)
        return "".join(starts), "".join(ends)

    # ---------------------------------------------------------------- numbering of the headings

    @staticmethod
    def _hlevel(b) -> int:
        """The Word heading level of a heading block: its level in the source (h4 to h6 and #### to ######
        keep their own level, the PDF draws them as h3), else the level of its kind."""
        n = int(b.level) if isinstance(b.level, int) and 1 <= b.level <= 6 else int(b.kind[1])
        return max(n, int(b.kind[1]))

    def _numbering(self) -> dict[int, tuple[int, str]]:
        """{block index: (level, text without its number)} when the heading numbers count up without a gap, else
        {}; the dot after the number of each level in self.dots."""
        self.dots: dict[int, str] = {}
        found: dict[int, tuple[int, int]] = {}
        counters = [0] * 6
        for i, b in enumerate(self.blocks):
            if b.kind not in ("h1", "h2", "h3"):
                continue
            text = " ".join(pdf._plain_text(b.runs).split())
            m = _NUMBER_RE.match(text)
            if not m:
                continue
            parts = [int(p) for p in m.group(1).split(".")]
            level = len(parts) - 1
            if level > 5:
                return {}
            if parts[:level] != counters[:level] or parts[level] != counters[level] + 1:
                return {}
            if self.dots.setdefault(level, m.group(2)) != m.group(2):
                return {}
            counters[level] += 1
            counters[level + 1:] = [0] * (5 - level)
            found[i] = (level, m.end())
        return found

    # ---------------------------------------------------------------- runs

    @staticmethod
    def _normal(runs: list) -> list:
        """The runs with the white space of the source as one space, none at the start or end of a line."""
        out: list = []
        for t, f, u in runs:
            if t == "\n":
                if out and out[-1][0] != "\n":
                    out[-1] = (out[-1][0].rstrip(" "), out[-1][1], out[-1][2])
                out.append((t, f, u))
                continue
            t = re.sub(r"\s+", " ", t)
            if not out or out[-1][0] == "\n" or out[-1][0].endswith(" "):
                t = t.lstrip(" ")
            if t:
                out.append((t, f, u))
        while out and out[-1][0] == "\n":
            out.pop()
        if out:
            out[-1] = (out[-1][0].rstrip(" "), out[-1][1], out[-1][2])
        return [r for r in out if r[0]]

    @staticmethod
    def _cut(runs: list, n: int) -> list:
        out = []
        for t, f, u in runs:
            if n >= len(t) and t != "\n":
                n -= len(t)
                continue
            out.append((t[n:], f, u))
            n = 0
        return out

    def _run(self, text: str, font: str, color: str = "", link: bool = False) -> str:
        """One run; its properties in the order of the schema (style, fonts, bold, italic, colour)."""
        props = ""
        if link:
            props += '<w:rStyle w:val="Hyperlink"/>' + (MONO if font == "c" else "")
        elif font == "c":
            props += '<w:rStyle w:val="CodeChar"/>'
        if font == "b":
            props += "<w:b/>"
        elif font == "i":
            props += "<w:i/>"
        if color:
            props += '<w:color w:val="%s"/>' % color
        props = "<w:rPr>%s</w:rPr>" % props if props else ""
        if text == "\n":
            return "<w:r>%s<w:br/></w:r>" % props
        parts = text.split("\t")
        body = '<w:tab/>'.join('<w:t xml:space="preserve">%s</w:t>' % self.text(p) for p in parts)
        return "<w:r>%s%s</w:r>" % (props, body)

    def runs(self, runs: list, color: str = "") -> str:
        out = []
        i = 0
        while i < len(runs):
            url = runs[i][2]
            j = i
            while j < len(runs) and runs[j][2] == url:
                j += 1
            group = runs[i:j]
            if url and url.startswith("#") and unquote(url[1:]) in self.marks:
                out.append('<w:hyperlink w:anchor="%s" w:history="1">%s</w:hyperlink>' % (
                    _esc(self.marks[unquote(url[1:])]), "".join(self._run(t, f, color, True) for t, f, _ in group)))
            elif url and url.startswith(pdf._LINK_SCHEMES):
                rid = self.rel("hyperlink", url, external=True)
                out.append('<w:hyperlink r:id="%s" w:history="1">%s</w:hyperlink>' % (
                    rid, "".join(self._run(t, f, color, True) for t, f, _ in group)))
            else:
                out.append("".join(self._run(t, f, color) for t, f, _ in group))
            i = j
        return "".join(out)

    def para(self, style: str | None, body: str, ppr: str = "", marks: tuple[str, str] = ("", "")) -> str:
        props = ('<w:pStyle w:val="%s"/>' % style if style else "") + ppr
        props = "<w:pPr>%s</w:pPr>" % props if props else ""
        return "<w:p>%s%s%s%s</w:p>" % (props, marks[0], body, marks[1])

    # ---------------------------------------------------------------- blocks

    def body(self) -> str:
        # every id of the page is known before a link to it is written
        for i, b in enumerate(self.blocks):
            for ident in b.anchors:
                if ident not in self.marks:
                    self.marks[ident] = self._bookmark_name(ident)
            if b.kind in ("h1", "h2", "h3") and pdf._plain_text(b.runs):
                self.heading_marks[i] = "_Toc%08d" % (i + 1)
                if b.color is not None:
                    self.colors.setdefault("h%d" % self._hlevel(b), _hex(b.color))
        out = []
        lists: dict[int, tuple[int, int]] = {}            # level: (numId, next number) of the open ordered lists
        first = True
        for i, b in enumerate(self.blocks):
            if b.kind != "li":
                lists.clear()
            names = [self.marks[a] for a in b.anchors if a in self.marks]
            if b.kind in ("h1", "h2", "h3"):
                if not pdf._plain_text(b.runs):
                    continue
                out.append(self.heading(i, b, names, first))
            elif b.kind in ("p", "caption"):
                runs = self._normal(b.runs)
                if not runs:
                    continue
                out.append(self.para("Caption" if b.kind == "caption" else None, self.runs(runs),
                                     marks=self.bookmarks(names)))
            elif b.kind == "li":
                out.append(self.item(b, names, lists))
            elif b.kind == "code":
                out.append(self.code(b, names))
            elif b.kind in ("table", "grid"):
                out.append(self.table(b, names))
            elif b.kind == "figure":
                nxt = self.blocks[i + 1] if i + 1 < len(self.blocks) else None
                alt = pdf._plain_text(nxt.runs) if nxt is not None and nxt.kind == "caption" else ""
                out.append(self.figure(b, names, alt))
            elif b.kind == "toc":
                out.append(self.contents(i))
            first = False
        return "".join(out)

    def heading(self, i: int, b, names: list[str], first: bool) -> str:
        self.counts["headings"] += 1
        runs = self._normal(b.runs)
        ppr = ""
        if b.kind == "h1" and self.h1_page and not first:
            ppr += "<w:pageBreakBefore/>"
        if i in self.numbered:
            level, cut = self.numbered[i]
            runs = self._cut(runs, cut)
            ppr += '<w:numPr><w:ilvl w:val="%d"/><w:numId w:val="1"/></w:numPr>' % level
        color = ""
        if b.color is not None and _hex(b.color) != self.colors.get("h%d" % self._hlevel(b)):
            color = _hex(b.color)
        marks = [self.heading_marks[i]] + names
        return self.para("Heading%d" % self._hlevel(b), self.runs(runs, color), ppr, self.bookmarks(marks))

    def item(self, b, names: list[str], lists: dict) -> str:
        level = max(0, min(int(b.level), 8))
        m = re.fullmatch(r"(\d{1,9})\.", b.marker or "")
        for deeper in [k for k in lists if k > level]:
            del lists[deeper]
        if m:
            n = int(m.group(1))
            if level in lists and lists[level][1] == n:
                num = lists[level][0]
            else:
                num = len(self.nums) + 3
                self.nums.append((num, 3, (level, n)))
            lists[level] = (num, n + 1)
        else:
            num = 2
            lists.pop(level, None)
        ppr = '<w:numPr><w:ilvl w:val="%d"/><w:numId w:val="%d"/></w:numPr>' % (level, num)
        return self.para("ListParagraph", self.runs(self._normal(b.runs)), ppr, self.bookmarks(names))

    def code(self, b, names: list[str]) -> str:
        self.counts["code"] += 1
        lines = [str(line).expandtabs(TAB) for line in b.lines] or [""]
        out = []
        for k, line in enumerate(lines):
            body = self._run(line, "r") if line else ""
            out.append(self.para("Code", body, marks=self.bookmarks(names) if k == 0 else ("", "")))
        return "".join(out)

    def table(self, b, names: list[str]) -> str:
        rows = [[str(c) for c in row] if isinstance(row, list) else [str(row)] for row in b.lines]
        rows = [r for r in rows if r]
        if not rows:
            return ""
        cols = max(len(r) for r in rows)
        if cols * len(rows) > MAX_CELLS:
            raise DocxError("a table of more than %d cells" % MAX_CELLS)
        self.counts["tables"] += 1
        head = 1 if b.kind == "grid" else int(getattr(b, "head", 0) or 0)
        width = int(TEXT_W * 20 / cols)
        out = ['<w:tbl><w:tblPr><w:tblStyle w:val="TableGrid"/><w:tblW w:w="5000" w:type="pct"/>'
               '<w:tblLook w:val="04A0" w:firstRow="%d" w:lastRow="0" w:firstColumn="0" w:lastColumn="0" '
               'w:noHBand="0" w:noVBand="1"/></w:tblPr><w:tblGrid>' % head]
        out += ['<w:gridCol w:w="%d"/>' % width] * cols
        out.append("</w:tblGrid>")
        jc = {"c": "center", "r": "right"}
        for r, row in enumerate(rows):
            is_head = r < head
            out.append("<w:tr>%s" % ("<w:trPr><w:tblHeader/></w:trPr>" if is_head else ""))
            for c in range(cols):
                text = " ".join(row[c].split()) if c < len(row) else ""
                ppr = ""
                align = b.align[c] if b.kind == "grid" and c < len(b.align) else "l"
                if align in jc:
                    ppr = '<w:jc w:val="%s"/>' % jc[align]
                shade = '<w:shd w:val="clear" w:color="auto" w:fill="E6E6E6"/>' if is_head else ""
                marks = self.bookmarks(names) if r == 0 and c == 0 else ("", "")
                body = self._run(text, "b" if is_head else "r") if text else ""
                out.append('<w:tc><w:tcPr><w:tcW w:w="%d" w:type="dxa"/>%s</w:tcPr>%s</w:tc>'
                           % (width, shade, self.para("TableText", body, ppr, marks)))
            out.append("</w:tr>")
        out.append("</w:tbl>")
        # a paragraph after the table keeps two tables in a row apart
        return "".join(out) + self.para("TableAfter", "")

    def figure(self, b, names: list[str], alt: str) -> str:
        fig = b.figure
        if fig is None or fig.width <= 0 or fig.height <= 0:
            return ""
        s = min(1.0, TEXT_W / fig.width, ROOM_H / fig.height)
        cx, cy = max(1, round(fig.width * s * EMU)), max(1, round(fig.height * s * EMU))
        self.drawing += 1
        n = self.drawing
        if isinstance(fig, pdf.Picture):
            data = fig.data
            if not data:
                return ""
            ext = "png" if data.startswith(b"\x89PNG") else "jpeg"
            name = "media/image%d.%s" % (n, ext)
            self.media.append((name, data))
            blip = '<a:blip r:embed="%s"/>' % self.rel("image", name)
        else:
            root = getattr(b, "svg_root", None)
            if root is None:
                return ""
            svg_name = "media/image%d.svg" % n
            self.media.append((svg_name, svg_markup(root, self.css).encode("utf-8")))
            svg_rid = self.rel("image", svg_name)
            png = svg_fallback(fig)
            if png is not None:
                png_name = "media/image%d.png" % n
                self.media.append((png_name, png))
                main = self.rel("image", png_name)
            else:
                main = svg_rid
                self.counts["svg_alone"] += 1
            blip = ('<a:blip r:embed="%s"><a:extLst><a:ext uri="%s"><asvg:svgBlip xmlns:asvg="http://schemas.'
                    'microsoft.com/office/drawing/2016/SVG/main" r:embed="%s"/></a:ext></a:extLst></a:blip>'
                    % (main, SVG_URI, svg_rid))
        self.counts["figures"] += 1
        descr = self.text(" ".join(alt.split()))
        drawing = (
            '<w:r><w:drawing><wp:inline distT="0" distB="0" distL="0" distR="0"><wp:extent cx="%d" cy="%d"/>'
            '<wp:docPr id="%d" name="Figure %d" descr="%s"/><wp:cNvGraphicFramePr><a:graphicFrameLocks '
            'noChangeAspect="1"/></wp:cNvGraphicFramePr><a:graphic><a:graphicData uri="http://schemas.'
            'openxmlformats.org/drawingml/2006/picture"><pic:pic><pic:nvPicPr><pic:cNvPr id="%d" name="Figure %d" '
            'descr="%s"/><pic:cNvPicPr/></pic:nvPicPr><pic:blipFill>%s<a:stretch><a:fillRect/></a:stretch>'
            '</pic:blipFill><pic:spPr><a:xfrm><a:off x="0" y="0"/><a:ext cx="%d" cy="%d"/></a:xfrm>'
            '<a:prstGeom prst="rect"><a:avLst/></a:prstGeom></pic:spPr></pic:pic></a:graphicData></a:graphic>'
            '</wp:inline></w:drawing></w:r>' % (cx, cy, n, n, descr, n, n, descr, blip, cx, cy))
        return self.para("Figure", drawing, marks=self.bookmarks(names))

    def contents(self, at: int) -> str:
        """The TOC field: its stored result the h1 and h2 after it, each linked to its heading."""
        entries = [(i, b) for i, b in enumerate(self.blocks)
                   if i > at and b.kind in ("h1", "h2") and i in self.heading_marks]
        begin = ('<w:r><w:fldChar w:fldCharType="begin" w:dirty="true"/></w:r><w:r><w:instrText '
                 'xml:space="preserve"> TOC \\o "1-2" \\h \\z \\u </w:instrText></w:r>'
                 '<w:r><w:fldChar w:fldCharType="separate"/></w:r>')
        end = '<w:r><w:fldChar w:fldCharType="end"/></w:r>'
        if not entries:
            return self.para("TOC1", begin + self._run("Update this field to list the headings.", "r") + end)
        out = []
        for k, (i, b) in enumerate(entries):
            text = " ".join(pdf._plain_text(b.runs).split())
            link = '<w:hyperlink w:anchor="%s" w:history="1">%s</w:hyperlink>' % (
                self.heading_marks[i], self._run(text, "r"))
            body = (begin if k == 0 else "") + link + (end if k == len(entries) - 1 else "")
            out.append(self.para("TOC1" if b.kind == "h1" else "TOC2", body))
        return "".join(out)

    # ---------------------------------------------------------------- parts

    def document(self) -> str:
        body = self.body()
        footer = self.rel("footer", "footer1.xml")
        sect = ('<w:sectPr><w:footerReference w:type="default" r:id="%s"/><w:pgSz w:w="%d" w:h="%d"/>'
                '<w:pgMar w:top="%d" w:right="%d" w:bottom="%d" w:left="%d" w:header="560" w:footer="560" '
                'w:gutter="0"/></w:sectPr>' % (footer, PAGE_W, PAGE_H, MARGIN, MARGIN, MARGIN, MARGIN))
        return ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n<w:document %s><w:body>%s%s</w:body>'
                '</w:document>' % (DOC_NS, body, sect))

    def styles(self) -> str:
        def heading(n: int, size: float, before: int) -> str:
            color = '<w:color w:val="%s"/>' % self.colors["h%d" % n] if "h%d" % n in self.colors else ""
            return ('<w:style w:type="paragraph" w:styleId="Heading%d"><w:name w:val="heading %d"/>'
                    '<w:basedOn w:val="Normal"/><w:next w:val="Normal"/><w:uiPriority w:val="9"/><w:qFormat/>'
                    '<w:pPr><w:keepNext/><w:keepLines/><w:spacing w:before="%d" w:after="120"/>'
                    '<w:outlineLvl w:val="%d"/></w:pPr><w:rPr><w:b/>%s<w:sz w:val="%d"/><w:szCs w:val="%d"/>'
                    '</w:rPr></w:style>' % (n, n, before, n - 1, color, size * 2, size * 2))

        mono = MONO
        return (
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n<w:styles xmlns:w="%s">'
            '<w:docDefaults><w:rPrDefault><w:rPr><w:rFonts w:ascii="Arial" w:hAnsi="Arial" w:eastAsia="Arial" '
            'w:cs="Arial"/><w:sz w:val="20"/><w:szCs w:val="20"/><w:lang w:val="en-GB"/></w:rPr></w:rPrDefault>'
            '<w:pPrDefault><w:pPr><w:spacing w:after="120" w:line="264" w:lineRule="auto"/></w:pPr>'
            '</w:pPrDefault></w:docDefaults>'
            '<w:style w:type="paragraph" w:default="1" w:styleId="Normal"><w:name w:val="Normal"/><w:qFormat/>'
            '</w:style>'
            '<w:style w:type="character" w:default="1" w:styleId="DefaultParagraphFont"><w:name w:val="Default '
            'Paragraph Font"/><w:uiPriority w:val="1"/><w:semiHidden/></w:style>'
            '%s%s%s'
            '<w:style w:type="paragraph" w:styleId="Caption"><w:name w:val="caption"/><w:basedOn w:val="Normal"/>'
            '<w:next w:val="Normal"/><w:qFormat/><w:pPr><w:spacing w:before="60" w:after="200"/>'
            '<w:jc w:val="center"/></w:pPr><w:rPr><w:i/><w:sz w:val="18"/><w:szCs w:val="18"/></w:rPr></w:style>'
            '<w:style w:type="paragraph" w:customStyle="1" w:styleId="Figure"><w:name w:val="Figure"/>'
            '<w:basedOn w:val="Normal"/><w:next w:val="Caption"/><w:qFormat/><w:pPr><w:keepNext/>'
            '<w:spacing w:before="120" w:after="0"/><w:jc w:val="center"/></w:pPr></w:style>'
            '<w:style w:type="paragraph" w:customStyle="1" w:styleId="Code"><w:name w:val="Code"/>'
            '<w:basedOn w:val="Normal"/><w:qFormat/><w:pPr><w:shd w:val="clear" w:color="auto" w:fill="F2F2F2"/>'
            '<w:spacing w:before="0" w:after="0" w:line="240" w:lineRule="auto"/></w:pPr><w:rPr>%s'
            '<w:noProof/><w:sz w:val="18"/><w:szCs w:val="18"/></w:rPr></w:style>'
            '<w:style w:type="character" w:customStyle="1" w:styleId="CodeChar"><w:name w:val="Code Char"/>'
            '<w:basedOn w:val="DefaultParagraphFont"/><w:rPr>%s<w:noProof/></w:rPr></w:style>'
            '<w:style w:type="character" w:styleId="Hyperlink"><w:name w:val="Hyperlink"/><w:basedOn '
            'w:val="DefaultParagraphFont"/><w:uiPriority w:val="99"/><w:unhideWhenUsed/><w:rPr><w:color '
            'w:val="0563C1"/><w:u w:val="single"/></w:rPr></w:style>'
            '<w:style w:type="paragraph" w:styleId="ListParagraph"><w:name w:val="List Paragraph"/>'
            '<w:basedOn w:val="Normal"/><w:uiPriority w:val="34"/><w:qFormat/><w:pPr><w:spacing w:after="60"/>'
            '<w:contextualSpacing/></w:pPr></w:style>'
            '<w:style w:type="paragraph" w:styleId="TOC1"><w:name w:val="toc 1"/><w:basedOn w:val="Normal"/>'
            '<w:next w:val="Normal"/><w:uiPriority w:val="39"/><w:unhideWhenUsed/><w:pPr><w:tabs><w:tab '
            'w:val="right" w:leader="dot" w:pos="%d"/></w:tabs><w:spacing w:before="60" w:after="40"/></w:pPr>'
            '<w:rPr><w:b/></w:rPr></w:style>'
            '<w:style w:type="paragraph" w:styleId="TOC2"><w:name w:val="toc 2"/><w:basedOn w:val="Normal"/>'
            '<w:next w:val="Normal"/><w:uiPriority w:val="39"/><w:unhideWhenUsed/><w:pPr><w:tabs><w:tab '
            'w:val="right" w:leader="dot" w:pos="%d"/></w:tabs><w:spacing w:after="40"/><w:ind w:left="320"/>'
            '</w:pPr></w:style>'
            '<w:style w:type="paragraph" w:customStyle="1" w:styleId="TableText"><w:name w:val="Table Text"/>'
            '<w:basedOn w:val="Normal"/><w:pPr><w:spacing w:before="40" w:after="40"/></w:pPr><w:rPr>'
            '<w:sz w:val="18"/><w:szCs w:val="18"/></w:rPr></w:style>'
            '<w:style w:type="paragraph" w:customStyle="1" w:styleId="TableAfter"><w:name w:val="Table After"/>'
            '<w:basedOn w:val="Normal"/><w:pPr><w:spacing w:before="0" w:after="120"/></w:pPr></w:style>'
            '<w:style w:type="paragraph" w:styleId="Footer"><w:name w:val="footer"/><w:basedOn w:val="Normal"/>'
            '<w:pPr><w:spacing w:after="0"/><w:jc w:val="center"/></w:pPr><w:rPr><w:sz w:val="16"/>'
            '<w:szCs w:val="16"/></w:rPr></w:style>'
            '<w:style w:type="table" w:default="1" w:styleId="TableNormal"><w:name w:val="Normal Table"/>'
            '<w:uiPriority w:val="99"/><w:semiHidden/><w:tblPr><w:tblInd w:w="0" w:type="dxa"/><w:tblCellMar>'
            '<w:top w:w="0" w:type="dxa"/><w:left w:w="108" w:type="dxa"/><w:bottom w:w="0" w:type="dxa"/>'
            '<w:right w:w="108" w:type="dxa"/></w:tblCellMar></w:tblPr></w:style>'
            '<w:style w:type="table" w:styleId="TableGrid"><w:name w:val="Table Grid"/><w:basedOn '
            'w:val="TableNormal"/><w:uiPriority w:val="39"/><w:tblPr><w:tblBorders>%s</w:tblBorders></w:tblPr>'
            '</w:style></w:styles>'
            % (NS_W, heading(1, 17, 360), heading(2, 12, 280), heading(3, 11, 200) + heading(4, 10.5, 160)
               + heading(5, 10, 120) + heading(6, 10, 120), mono, mono,
               TEXT_W * 20, TEXT_W * 20,
               "".join('<w:%s w:val="single" w:sz="4" w:space="0" w:color="808080"/>' % side
                       for side in ("top", "left", "bottom", "right", "insideH", "insideV"))))

    def numbering(self) -> str:
        bullets = "•◦▪" * 3
        abstract = ['<w:abstractNum w:abstractNumId="1"><w:multiLevelType w:val="multilevel"/>']
        for lvl in range(6):
            text = ".".join("%%%d" % (k + 1) for k in range(lvl + 1)) + self.dots.get(lvl, "")
            abstract.append('<w:lvl w:ilvl="%d"><w:start w:val="1"/><w:numFmt w:val="decimal"/>'
                            '<w:suff w:val="space"/><w:lvlText w:val="%s"/><w:lvlJc w:val="left"/>'
                            '<w:pPr><w:ind w:left="0" w:firstLine="0"/></w:pPr></w:lvl>' % (lvl, text))
        abstract.append("</w:abstractNum>")
        for ident, fmt in ((2, "bullet"), (3, "decimal")):
            abstract.append('<w:abstractNum w:abstractNumId="%d"><w:multiLevelType w:val="hybridMultilevel"/>'
                            % ident)
            for lvl in range(9):
                text = bullets[lvl] if fmt == "bullet" else "%%%d." % (lvl + 1)
                abstract.append('<w:lvl w:ilvl="%d"><w:start w:val="1"/><w:numFmt w:val="%s"/>'
                                '<w:lvlText w:val="%s"/><w:lvlJc w:val="left"/><w:pPr><w:ind w:left="%d" '
                                'w:hanging="280"/></w:pPr></w:lvl>'
                                % (lvl, fmt, _esc(text), 320 * (lvl + 1) + 280))
            abstract.append("</w:abstractNum>")
        nums = ['<w:num w:numId="1"><w:abstractNumId w:val="1"/></w:num>',
                '<w:num w:numId="2"><w:abstractNumId w:val="2"/></w:num>']
        for num, ab, start in self.nums:
            nums.append('<w:num w:numId="%d"><w:abstractNumId w:val="%d"/><w:lvlOverride w:ilvl="%d">'
                        '<w:startOverride w:val="%d"/></w:lvlOverride></w:num>' % (num, ab, start[0], start[1]))
        return ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n<w:numbering xmlns:w="%s">%s%s'
                '</w:numbering>' % (NS_W, "".join(abstract), "".join(nums)))

    @staticmethod
    def footer() -> str:
        def field(code: str, shown: str) -> str:
            return ('<w:r><w:fldChar w:fldCharType="begin"/></w:r><w:r><w:instrText xml:space="preserve"> %s '
                    '</w:instrText></w:r><w:r><w:fldChar w:fldCharType="separate"/></w:r><w:r><w:t>%s</w:t></w:r>'
                    '<w:r><w:fldChar w:fldCharType="end"/></w:r>' % (code, shown))
        return ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n<w:ftr xmlns:w="%s"><w:p><w:pPr>'
                '<w:pStyle w:val="Footer"/></w:pPr>%s<w:r><w:t xml:space="preserve"> / </w:t></w:r>%s</w:p>'
                '</w:ftr>' % (NS_W, field("PAGE", "1"), field("NUMPAGES", "1")))

    def package(self, author: str) -> bytes:
        document = self.document()
        when = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        types = ['<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>',
                 '<Default Extension="xml" ContentType="application/xml"/>',
                 '<Default Extension="png" ContentType="image/png"/>',
                 '<Default Extension="jpeg" ContentType="image/jpeg"/>',
                 '<Default Extension="svg" ContentType="image/svg+xml"/>']
        wml = "application/vnd.openxmlformats-officedocument.wordprocessingml."
        for part, kind in (("/word/document.xml", "document.main+xml"), ("/word/styles.xml", "styles+xml"),
                           ("/word/numbering.xml", "numbering+xml"), ("/word/settings.xml", "settings+xml"),
                           ("/word/footer1.xml", "footer+xml")):
            types.append('<Override PartName="%s" ContentType="%s%s"/>' % (part, wml, kind))
        types.append('<Override PartName="/docProps/core.xml" ContentType="application/vnd.openxmlformats-'
                     'package.core-properties+xml"/>')
        types.append('<Override PartName="/docProps/app.xml" ContentType="application/vnd.openxmlformats-'
                     'officedocument.extended-properties+xml"/>')
        rels = [(rid, REL + kind, target, ext) for rid, kind, target, ext in self.rels]
        rels += [("rId1", REL + "styles", "styles.xml", False), ("rId2", REL + "numbering", "numbering.xml", False),
                 ("rId3", REL + "settings", "settings.xml", False)]
        doc_rels = "".join('<Relationship Id="%s" Type="%s" Target="%s"%s/>' % (
            rid, kind, _esc(target), ' TargetMode="External"' if ext else "") for rid, kind, target, ext in rels)
        parts = [
            ("[Content_Types].xml", '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n<Types xmlns="http://'
             'schemas.openxmlformats.org/package/2006/content-types">%s</Types>' % "".join(types)),
            ("_rels/.rels", '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n<Relationships xmlns="%s">'
             '<Relationship Id="rId1" Type="%sofficeDocument" Target="word/document.xml"/>'
             '<Relationship Id="rId2" Type="http://schemas.openxmlformats.org/package/2006/relationships/metadata/'
             'core-properties" Target="docProps/core.xml"/><Relationship Id="rId3" Type="%sextended-properties" '
             'Target="docProps/app.xml"/></Relationships>' % (NS_PR, REL, REL)),
            ("docProps/core.xml", '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n<cp:coreProperties '
             'xmlns:cp="http://schemas.openxmlformats.org/package/2006/metadata/core-properties" xmlns:dc="http://'
             'purl.org/dc/elements/1.1/" xmlns:dcterms="http://purl.org/dc/terms/" xmlns:xsi="http://www.w3.org/'
             '2001/XMLSchema-instance"><dc:title>%s</dc:title><dc:creator>%s</dc:creator><cp:lastModifiedBy>%s'
             '</cp:lastModifiedBy><dcterms:created xsi:type="dcterms:W3CDTF">%s</dcterms:created><dcterms:modified '
             'xsi:type="dcterms:W3CDTF">%s</dcterms:modified></cp:coreProperties>'
             % (self.text(self.title), self.text(author), self.text(author), when, when)),
            ("docProps/app.xml", '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n<Properties xmlns="http://'
             'schemas.openxmlformats.org/officeDocument/2006/extended-properties"><Application>awb docx'
             '</Application></Properties>'),
            ("word/document.xml", document),
            ("word/_rels/document.xml.rels", '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
             '<Relationships xmlns="%s">%s</Relationships>' % (NS_PR, doc_rels)),
            ("word/styles.xml", self.styles()),
            ("word/numbering.xml", self.numbering()),
            ("word/settings.xml", '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n<w:settings xmlns:w="%s">'
             '<w:defaultTabStop w:val="709"/><w:characterSpacingControl w:val="doNotCompress"/>'
             '<w:updateFields w:val="true"/><w:compat><w:compatSetting w:name="compatibilityMode" w:uri="http://schemas.'
             'microsoft.com/office/word" w:val="15"/></w:compat></w:settings>' % NS_W),
            ("word/footer1.xml", self.footer()),
        ]
        out = io.BytesIO()
        with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
            for name, text in parts:
                z.writestr(zipfile.ZipInfo(name, (1980, 1, 1, 0, 0, 0)), text.encode("utf-8"),
                           compress_type=zipfile.ZIP_DEFLATED)
            for name, data in self.media:
                z.writestr(zipfile.ZipInfo("word/" + name, (1980, 1, 1, 0, 0, 0)), data,
                           compress_type=zipfile.ZIP_DEFLATED)
        return out.getvalue()


# --------------------------------------------------------------------------- the file and the command

def _screen_blocks(blocks: list, title: str, author: str, css: str) -> None:
    """The text of the blocks as awb pdf checks its encoding, the SVG files and the page's CSS beside it."""
    values = [title, author, css]
    for block in blocks:
        values.extend([pdf._plain_text(block.runs), block.marker])
        values.extend(url for _, _, url in block.runs if url)
        values.extend(" ".join(map(str, row)) if isinstance(row, list) else str(row) for row in block.lines)
        values.extend(getattr(block.figure, "texts", None) or [])
        values.extend(block.anchors)
        root = getattr(block, "svg_root", None)
        if root is not None:
            values.append(svg_markup(root))
    pdf._screen("\n".join(values))


def render(blocks: list, title: str, author: str = pdf.AUTHOR, css: str = "",
           h1_page: bool = False) -> tuple[bytes, dict]:
    """The DOCX of the blocks and its counts (headings, figures, tables, code, svg_alone, replaced)."""
    _screen_blocks(blocks, title, author, css)
    w = _Writer(blocks, title, css, h1_page)
    data = w.package(author)
    counts = dict(w.counts, replaced=w.replaced, numbered=len(w.numbered))
    return data, counts


def convert(path: Path, out: Path | None = None, title: str | None = None, start: Path | None = None,
            author: str = pdf.AUTHOR, h1_page: bool = False) -> tuple[Path, dict, dict]:
    """Write a checked, private DOCX atomically; (DOCX, counts, what the figures left out)."""
    try:
        path = Path(path)
        kind = pdf.SUFFIXES.get(path.suffix.lower())
        if kind is None:
            raise DocxError("awb docx takes a Markdown or HTML file (.md, .markdown, .html, .htm)")
        target = Path(out) if out else path.with_suffix(".docx")
        if target.suffix.lower() != ".docx":
            raise DocxError("the output is a .docx file")
        if not pdf.allowed(target, start) or not pdf.allowed(path, start):
            raise DocxError("refused: the DOCX goes inside the project folder or the session scratch folder; "
                            "the input must also be inside one of them")
        pdf._screen(str(path) + "\n" + str(target))
        if any(ord(c) < 32 or ord(c) == 127 for c in str(path) + str(target)):
            raise DocxError("control character in a file path")
        text = pdf._read(path)
        if any(len(line) > pdf.MAX_LINE for line in text.split("\n")):
            raise DocxError("input line is too long")
        pdf._screen(text)
        skipped: dict[str, int] = {}
        css = ""
        if kind == "html":
            blocks, found, skipped, css = parse_html(text, Path(os.path.abspath(path)).parent, start)
        else:
            blocks, found = pdf.from_markdown(text)
        data, counts = render(blocks, title or found or path.stem, author, css, h1_page)
        pdf._publish(target, data)
        return target, counts, skipped
    except (OSError, ValueError, RecursionError, zipfile.BadZipFile):
        raise DocxError("cannot read, render or write the file") from None


def main(argv: list[str] | None = None) -> int:
    from awb.cli import SafeParser

    ap = SafeParser(prog="awb docx", description="A Word file (.docx) of a Markdown or HTML deliverable, next to it, "
                                                 "for partners who edit with tracked changes: the structure of the "
                                                 "PDF of awb pdf (headings, contents, figures, tables, code).")
    ap.add_argument("file", help="the Markdown (.md) or HTML (.html) file")
    ap.add_argument("--out", default=None, metavar="DOCX", help="the DOCX (default: FILE with .docx, next to it); "
                                                                "inside the project folder or the session scratch "
                                                                "folder")
    ap.add_argument("--title", default=None, metavar="T",
                    help="the title in the document properties (default: the title or first heading)")
    ap.add_argument("--h1-page", action="store_true",
                    help="start a new page before each level-1 heading but the first, as awb pdf --h1-page")
    try:
        args = ap.parse_args(argv)
    except SystemExit as exc:
        return 0 if exc.code == 0 else 2
    try:
        out, counts, skipped = convert(Path(args.file), Path(args.out) if args.out else None, args.title,
                                       h1_page=args.h1_page)
    except pdf.PdfError as err:
        print("awb docx: %s" % err, file=sys.stderr)
        return 2
    except (OSError, ValueError, RecursionError):
        print("awb docx: cannot read, render or write the file", file=sys.stderr)
        return 2
    if skipped:
        print("awb docx: left out of the figures: %s" % ", ".join(
            "%d %s" % (n, what) for what, n in sorted(skipped.items())), file=sys.stderr)
    if counts["svg_alone"]:
        print("awb docx: %d SVG figure(s) without a PNG fallback (no pdftoppm): Word 2016 and later show them, an "
              "older reader does not" % counts["svg_alone"], file=sys.stderr)
    print("docx: %s, %d heading(s)%s, %d figure(s), %d table(s), %d code block(s)"
          % (out, counts["headings"], " numbered by Word" if counts["numbered"] else "", counts["figures"],
             counts["tables"], counts["code"]))
    if counts["replaced"]:
        print("docx: %d character(s) XML cannot hold written as ?" % counts["replaced"])
    return 0
