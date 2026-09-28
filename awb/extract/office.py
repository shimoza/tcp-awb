"""Office documents read straight from the zip container, no third party library at runtime.

docx: paragraphs with runs joined, tables as Markdown rows, headings, footnotes and endnotes, equations,
legacy WordArt, chart text (title, series names, categories, data labels) and SmartArt text where they stand
in `text`; headers, footers, comments with author, tracked deletions and moves with their authors, field
codes, alternative text of pictures and tables, content control aliases and list items, numbering level
text, the other branches of alternate content (a Fallback the reader does not show), people and the core,
app and custom properties (a binary property decoded) in `detect_text`. Inserted text of a tracked change is
part of the body, so it is in `text` and in `detect_text`. Footnotes, endnotes, headers, footers, comments,
charts and diagrams are found through the relationships of the document part, then under their fixed names.
An alt chunk (html, rtf, text, mht, docx) is read as a child through the matching reader.

xlsx: every sheet including hidden ones, every cell including hidden rows and columns, shared and
inline strings (the table found through the workbook relationships), formula text and cached value, comments
with author, threaded comments, defined names, sheet names, headers and footers, text in drawings and charts
(rich text and string caches), legacy text boxes, phonetic runs, number format literals, data validation
prompts, errors and lists, conditional formatting formulas, hyperlink texts, cached cells of linked workbooks,
tables, query tables, slicers, data connections and pivot caches with who refreshed them. Visible sheets go to
`text`, everything else to `detect_text`.

pptx: slides in presentation order (through the relationships, by name when there are none) with their chart
and SmartArt text and notes in `text`; unlisted slide parts, comments with author, alternative text, layouts
and masters, tags, section names and the other branches of alternate content in `detect_text`.

odt, ods, odp: content.xml in `text`, with hidden and conditional text values, declared field values behind
empty fields and text boxes drawn on a sheet; styles.xml headers and footers, meta.xml, annotations, tracked
changes with their authors, field declarations and form control values in `detect_text`.

Every kind: external relationship targets (hyperlinks, attached templates, linked workbooks) go to
`detect_text`; embedded images are counted in a note. An embedded package (an xlsx inside a docx, a zip) is
read as a child. A macro project (vbaProject.bin), an embedded font and a container that repeats a part name
give a note and set `meta["incomplete"]`. `text` is always part of `detect_text`, so detection sees
everything the output can carry.

Every kind, second pass: every part of the container is read once more without knowing its schema. Every
text node and every attribute value of every xml part (tooltips, document variables, glossary, custom xml,
fields), the text of other text parts and the printable strings of binary parts (an OLE object, a macro
project, image metadata) go to `scan_text`, which is checked for registered forms only. A part that could
not be read and an OLE object set `meta["incomplete"]`.

XML is parsed with a cap on bytes per part, bytes per container and elements per part, so that a small
deflated part cannot grow into gigabytes of memory.
"""
from __future__ import annotations

import io
import os
import re
import shutil
import xml.etree.ElementTree as ET
import zipfile
from dataclasses import dataclass, field
from pathlib import Path

from awb.extract import INCOMPLETE, MAX_ARCHIVE_DEPTH, Extraction, make_temp_dir

MAX_PART_BYTES = 64 * 1024 * 1024
MAX_TOTAL_BYTES = 256 * 1024 * 1024
"""Unpacked XML read from one container, all parts together."""
MAX_ELEMENTS = 1_000_000
"""Elements one xml part may hold before it is refused (a tree of this size needs a few hundred MB)."""
MAX_RAW_TOTAL = 256 * 1024 * 1024
"""Bytes the second pass reads from one container, all parts together."""
MEDIA_PROBE = 64 * 1024
"""Bytes of an image read for the printable strings of its metadata."""
_MAX_REPEAT = 1024

_HEADING_STYLE_RE = re.compile(r"^(?:heading|.*berschrift|titre|t.tulo|kop)\s*(\d)$", re.IGNORECASE)
_TITLE_STYLE_RE = re.compile(r"^(?:title|titel)$", re.IGNORECASE)
_HF_CODE_RE = re.compile(r'&K[0-9A-Fa-f]{6}|&"[^"]*"|&\d+|&[LCRPNDTFABIUESXYZG]')
_COL_RE = re.compile(r"([A-Z]+)(\d*)")


# --------------------------------------------------------------------------- xml helpers


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _attr(el: ET.Element, name: str, default: str | None = None) -> str | None:
    """Attribute by local name, whatever the prefix."""
    for key, value in el.attrib.items():
        if _local(key) == name:
            return value
    return default


def _rid(el: ET.Element, name: str = "id") -> str | None:
    """A relationship attribute (r:id, r:dm) by local name, the namespaced one first: p:sldId carries a plain
    id (the slide number) next to r:id."""
    plain = None
    for key, value in el.attrib.items():
        if _local(key) == name:
            if "}" in key:
                return value
            plain = value
    return plain


def _child(el: ET.Element, name: str) -> ET.Element | None:
    for c in el:
        if _local(c.tag) == name:
            return c
    return None


def _all_text(el: ET.Element, tag: str = "t") -> str:
    """Concatenation of every descendant with local name `tag`."""
    return "".join((c.text or "") for c in el.iter() if _local(c.tag) == tag)


def _md_cell(value: str) -> str:
    return value.replace("\r", " ").replace("\n", " ").replace("|", "\\|").strip()


def _md_rows(rows: list[list[str]]) -> list[str]:
    """Markdown table lines from rows of cell strings. Empty input gives no lines."""
    rows = [r for r in rows if r]
    if not rows:
        return []
    width = max(len(r) for r in rows)
    lines = []
    for i, row in enumerate(rows):
        cells = [_md_cell(c) for c in row] + [""] * (width - len(row))
        lines.append("| " + " | ".join(cells) + " |")
        if i == 0:
            lines.append("|" + " --- |" * width)
    return lines


class _Parts:
    """The parts of one container. Malformed or oversized parts give None and a counted note."""

    def __init__(self, zf: zipfile.ZipFile, ex: Extraction):
        self.zf = zf
        self.ex = ex
        self.names = zf.namelist()
        self.name_set = set(self.names)
        self.bad = 0
        self.skipped = 0
        self.read_bytes = 0
        self._cache: dict[str, ET.Element | None] = {}
        # parts a reader wants read as children (alt chunks), in document order
        self.child_parts: list[str] = []
        if len(self.names) != len(self.name_set):
            # zipfile hands out the last copy, a reader's application may show the first
            ex.meta[INCOMPLETE] = True
            ex.notes.append("%d part name(s) repeated in the container, one copy read" % (len(self.names) - len(self.name_set)))

    def has(self, name: str) -> bool:
        return name in self.name_set

    def raw(self, name: str, limit: int = MAX_PART_BYTES) -> bytes | None:
        """The first `limit` bytes of a part, None when it is missing or cannot be read."""
        if name not in self.name_set:
            return None
        try:
            with self.zf.open(name) as fh:
                return fh.read(limit)
        except (zipfile.BadZipFile, ValueError, NotImplementedError, EOFError, OSError, RuntimeError):
            self.bad += 1
            self.ex.meta[INCOMPLETE] = True
            return None

    def matching(self, pattern: str) -> list[str]:
        rx = re.compile(pattern)
        return sorted((n for n in self.names if rx.fullmatch(n)), key=_natural_key)

    def get(self, name: str) -> ET.Element | None:
        if name in self._cache:
            return self._cache[name]
        root = self._parse(name)
        self._cache[name] = root
        return root

    def _parse(self, name: str) -> ET.Element | None:
        if name not in self.name_set:
            return None
        info = self.zf.getinfo(name)
        if info.file_size > MAX_PART_BYTES or self.read_bytes + info.file_size > MAX_TOTAL_BYTES:
            self.skipped += 1
            self.ex.meta[INCOMPLETE] = True
            return None
        self.read_bytes += info.file_size
        try:
            return _capped_xml(self.zf.read(name))
        except (ET.ParseError, zipfile.BadZipFile, ValueError, NotImplementedError, EOFError, OSError, _TooBig):
            self.bad += 1
            self.ex.meta[INCOMPLETE] = True
            return None

    def content_type(self, name: str) -> str:
        """The content type of a part from [Content_Types].xml, by override then by extension, else empty."""
        root = self.get("[Content_Types].xml")
        if root is None:
            return ""
        _, dot, ext = name.rpartition(".")
        by_ext = ""
        for node in root:
            tag = _local(node.tag)
            if tag == "Override" and (_attr(node, "PartName") or "").lstrip("/") == name:
                return (_attr(node, "ContentType") or "").strip().lower()
            if tag == "Default" and dot and (_attr(node, "Extension") or "").lower() == ext.lower():
                by_ext = (_attr(node, "ContentType") or "").strip().lower()
        return by_ext

    def main_part(self, default: str) -> str:
        """The target of the officeDocument relationship of the package or `default`."""
        root = self.get("_rels/.rels")
        if root is not None:
            for rel in root:
                if (_attr(rel, "Type") or "").endswith("/officeDocument"):
                    target = _resolve("", _attr(rel, "Target") or "")
                    if target in self.name_set:
                        return target
        return default

    def external_targets(self) -> list[str]:
        """External relationship targets of every part: hyperlinks, attached templates, linked files."""
        seen: dict[str, None] = {}
        for name in self.matching(r"(?:.*/)?_rels/[^/]*\.rels"):
            for target, external in self.rels(name).values():
                if external and target.strip():
                    seen.setdefault(target.strip(), None)
        return list(seen)

    def rel_rows(self, name: str) -> list[tuple[str, str, bool, str]]:
        """(id, target, external, type) of every relationship of a .rels part, in file order, internal targets
        resolved against the base of the part."""
        root = self.get(name)
        rows: list[tuple[str, str, bool, str]] = []
        if root is None:
            return rows
        base = name.split("_rels/")[0]
        for rel in root:
            rid = _attr(rel, "Id")
            target = _attr(rel, "Target") or ""
            external = (_attr(rel, "TargetMode") or "").lower() == "external"
            if not rid:
                continue
            if not external:
                target = _resolve(base, target)
            rows.append((rid, target, external, _attr(rel, "Type") or ""))
        return rows

    def rels(self, name: str) -> dict[str, tuple[str, bool]]:
        """Relationship id -> (target, external) from a .rels part, targets resolved against its base."""
        return {rid: (target, external) for rid, target, external, _ in self.rel_rows(name)}

    @staticmethod
    def rels_name(part: str) -> str:
        """The name of the .rels part that belongs to `part`."""
        base, _, leaf = part.rpartition("/")
        return "%s/_rels/%s.rels" % (base, leaf) if base else "_rels/%s.rels" % leaf

    def rels_for(self, part: str) -> dict[str, tuple[str, bool]]:
        return self.rels(self.rels_name(part))

    def targets(self, part: str, kind: str) -> list[str]:
        """Internal targets of `part` whose relationship type ends in `/kind`, present in the container, in
        file order and each once."""
        seen: dict[str, None] = {}
        for _, target, external, rtype in self.rel_rows(self.rels_name(part)):
            if not external and rtype.endswith("/" + kind) and target in self.name_set:
                seen.setdefault(target, None)
        return list(seen)

    def by_rels_or_name(self, part: str, kind: str, pattern: str) -> list[str]:
        """Parts reached from `part` by relationship type `kind`, then those matching the fixed-name `pattern`
        that the relationships did not name (a container without rels still works)."""
        found = self.targets(part, kind)
        for name in self.matching(pattern):
            if name not in found:
                found.append(name)
        return found

    def finish_notes(self) -> None:
        if self.bad:
            self.ex.notes.append("%d part(s) not well-formed, skipped" % self.bad)
        if self.skipped:
            self.ex.notes.append("%d part(s) above the size limit, skipped" % self.skipped)


class _TooBig(Exception):
    """An xml part holds more than MAX_ELEMENTS elements."""


class _CappedBuilder(ET.TreeBuilder):
    def __init__(self) -> None:
        super().__init__()
        self.count = 0

    def start(self, tag, attrs):
        self.count += 1
        if self.count > MAX_ELEMENTS:
            raise _TooBig()
        return super().start(tag, attrs)


def _capped_xml(data: bytes) -> ET.Element:
    """ET.fromstring with an element cap. The parser resolves no external entity (expat default)."""
    parser = ET.XMLParser(target=_CappedBuilder())
    parser.feed(data)
    return parser.close()


def _resolve(base: str, target: str) -> str:
    if target.startswith("/"):
        return target[1:]
    parts = [p for p in base.split("/") if p]
    for seg in target.split("/"):
        if seg == "..":
            if parts:
                parts.pop()
        elif seg and seg != ".":
            parts.append(seg)
    return "/".join(parts)


def _natural_key(name: str):
    return [int(p) if p.isdigit() else p for p in re.split(r"(\d+)", name)]


@dataclass
class _Ctx:
    """Side channels collected while walking a body: everything here goes to detect_text."""

    deleted: list[str] = field(default_factory=list)
    fields: list[str] = field(default_factory=list)
    comments: list[str] = field(default_factory=list)
    footnotes: list[str] = field(default_factory=list)
    links: list[str] = field(default_factory=list)
    hidden: list[str] = field(default_factory=list)
    formulas: list[str] = field(default_factory=list)
    alt: list[str] = field(default_factory=list)
    # True while consecutive deleted runs belong to one deleted stretch (a name split over two runs)
    del_open: bool = False
    # odf only: a table is a sheet (ods, name as a heading in text) or a table in a text (name kept out)
    sheets: bool = False
    # the container and the relationships of the part being walked, for charts, diagrams and alt chunks
    parts: "_Parts | None" = None
    rels: dict[str, tuple[str, bool]] = field(default_factory=dict)
    # chart and diagram parts already rendered where they stand in the body
    rendered: set[str] = field(default_factory=set)
    # string cells that point past the shared string table
    missing_strings: int = 0
    # odf only: user field and variable declarations by name, for an empty field in the body
    decls: dict[str, str] = field(default_factory=dict)

    def add_deleted(self, text: str) -> None:
        if not text:
            return
        if self.del_open and self.deleted:
            self.deleted[-1] += text
        else:
            self.deleted.append(text)
        self.del_open = True


_ALT_TAGS = ("docPr", "cNvPr")


def _alt_text(el: ET.Element, ctx: _Ctx) -> None:
    """Alternative text and title of a drawing or picture (they often carry names)."""
    for key in ("descr", "title"):
        value = (_attr(el, key) or "").strip()
        if value:
            ctx.alt.append("alt text: " + value)


def _attr_values(root: ET.Element | None, name: str) -> list[str]:
    """Every value of an attribute with local name `name` anywhere below `root`, in order, no repeats."""
    if root is None:
        return []
    seen: dict[str, None] = {}
    for el in root.iter():
        value = _attr(el, name)
        if value and value.strip():
            seen.setdefault(value.strip(), None)
    return list(seen)


# --------------------------------------------------------------------------- charts, diagrams, alternate content

# prefixes an Office application understands in mc:Choice Requires; a Choice that needs another one is shown
# as its Fallback by the reader
_MC_KNOWN_RE = re.compile(
    r"wps|wpg|wpi|wpc|wp1\d|w|w1\d\w*|wne|w10|a|a1\d|p|p1\d|x|x1\d\w*|xr\d*|c|c1\d\w*|cx\d*|v|o|mc|asvg|dgm1\d|sl1\d"
)


def _mc_known(requires: str | None) -> bool:
    return all(_MC_KNOWN_RE.fullmatch(p) for p in (requires or "").split())


def _mc_pick(el: ET.Element) -> tuple[ET.Element | None, list[ET.Element]]:
    """(the branch of an mc:AlternateContent the reader shows, the other branches). The first Choice whose
    Requires the reader knows is shown; when every Choice needs an unknown namespace the Fallback is shown; an
    element without any Choice shows nothing (the Fallback then reaches detection only)."""
    choices = [c for c in el if _local(c.tag) == "Choice"]
    fallback = _child(el, "Fallback")
    shown = next((c for c in choices if _mc_known(_attr(c, "Requires"))), None)
    if shown is None and choices and fallback is not None:
        shown = fallback
    others = [c for c in el if c is not shown]
    return shown, others


def _a_para(p: ET.Element) -> str:
    parts = []
    for node in p.iter():
        tag = _local(node.tag)
        if tag == "t":
            parts.append(node.text or "")
        elif tag == "br":
            parts.append("\n")
    return "".join(parts).strip()


def _chart_lines(root: ET.Element) -> list[str]:
    """What a chart shows as text: title, axis titles and data labels (rich text paragraphs), series names
    and category labels (string caches, a literal series name, a chartex text)."""
    lines: list[str] = []
    for el in root.iter():
        tag = _local(el.tag)
        if tag == "p":
            line = _a_para(el)
            if line:
                lines.append(line)
        elif tag == "strCache":
            values = [(v.text or "").strip() for v in el.iter() if _local(v.tag) == "v"]
            values = [v for v in values if v]
            if values:
                lines.append(" | ".join(values))
        elif tag in ("tx", "txData"):
            v = _child(el, "v")
            if v is not None and (v.text or "").strip():
                lines.append(v.text.strip())
    return lines


def _diagram_lines(root: ET.Element) -> list[str]:
    """The text of every node of a SmartArt data model (dgm:t holds drawingml paragraphs)."""
    lines: list[str] = []
    for el in root.iter():
        if _local(el.tag) == "p":
            line = _a_para(el)
            if line:
                lines.append(line)
    return lines


_CHART_RE = r"(?:word|ppt|xl)/charts/chart(?:[eE]x)?\d*\.xml"
_DIAGRAM_RE = r"(?:word|ppt|xl)/diagrams/data\d*\.xml"


def _graphic_lines(ctx: _Ctx, rid: str | None, kind: str) -> list[str]:
    """The lines of the chart or diagram part behind relationship `rid` of the part being walked, once per
    part. `kind` is "chart" or "diagram"."""
    if ctx.parts is None or not rid:
        return []
    target, external = ctx.rels.get(rid, ("", True))
    if external or not target or target in ctx.rendered:
        return []
    root = ctx.parts.get(target)
    if root is None:
        return []
    ctx.rendered.add(target)
    lines = _chart_lines(root) if kind == "chart" else _diagram_lines(root)
    return ["%s: %s" % (kind, line) for line in lines]


def _leftover_graphics(parts: _Parts, ctx: _Ctx, out: list[str], patterns: tuple[str, str]) -> None:
    """Chart and diagram parts no body referenced (or referenced without rels) still reach `out`."""
    for pattern, kind in zip(patterns, ("chart", "diagram")):
        for name in parts.matching(pattern):
            if name in ctx.rendered:
                continue
            root = parts.get(name)
            if root is None:
                continue
            ctx.rendered.add(name)
            lines = _chart_lines(root) if kind == "chart" else _diagram_lines(root)
            out.extend("%s: %s" % (kind, line) for line in lines)


# --------------------------------------------------------------------------- docx


def _w_hidden_runs(el: ET.Element, ctx: _Ctx, label: str) -> None:
    """Runs the reader does not see (the other branches of alternate content) go to detection."""
    buf: list[str] = []
    _w_runs(el, buf, ctx)
    text = " ".join("".join(buf).split())
    if text:
        ctx.hidden.append("%s: %s" % (label, text))


def _w_sdt_props(el: ET.Element, ctx: _Ctx) -> None:
    """Alias and drop-down list items of a content control: the reader sees them in the control."""
    for node in el.iter():
        tag = _local(node.tag)
        if tag == "alias" and (_attr(node, "val") or "").strip():
            ctx.hidden.append("content control: " + _attr(node, "val").strip())
        elif tag == "listItem" and (_attr(node, "displayText") or "").strip():
            ctx.hidden.append("list item: " + _attr(node, "displayText").strip())


def _w_runs(el: ET.Element, parts: list[str], ctx: _Ctx) -> None:
    for c in el:
        tag = _local(c.tag)
        if tag == "AlternateContent":
            shown, others = _mc_pick(c)
            if shown is not None:
                _w_runs(shown, parts, ctx)
            for other in others:
                _w_hidden_runs(other, ctx, "alternate content")
            continue
        if tag == "Fallback":
            _w_hidden_runs(c, ctx, "alternate content")
            continue
        if tag == "t":
            if c.text:
                parts.append(c.text)
                ctx.del_open = False
        elif tag == "textpath":
            # legacy WordArt keeps its text in an attribute
            value = (_attr(c, "string") or "").strip()
            if value:
                parts.append(value)
        elif tag == "chart":
            lines = _graphic_lines(ctx, _rid(c), "chart")
            if lines:
                parts.append("\n" + "\n".join(lines) + "\n")
        elif tag == "relIds":
            lines = _graphic_lines(ctx, _rid(c, "dm"), "diagram")
            if lines:
                parts.append("\n" + "\n".join(lines) + "\n")
        elif tag == "sdtPr":
            _w_sdt_props(c, ctx)
        elif tag == "delText":
            ctx.add_deleted(c.text or "")
        elif tag in ("instrText", "delInstrText"):
            ctx.fields.append((c.text or "").strip())
        elif tag == "moveFrom":
            # the source of a moved stretch: removed from the document, kept for detection only
            moved: list[str] = []
            _w_runs(c, moved, ctx)
            ctx.del_open = False
            ctx.add_deleted("".join(moved).strip())
            ctx.del_open = False
        elif tag == "fldSimple":
            ctx.fields.append((_attr(c, "instr") or "").strip())
            _w_runs(c, parts, ctx)
        elif tag in _ALT_TAGS:
            _alt_text(c, ctx)
        elif tag == "tab":
            parts.append("\t")
        elif tag in ("br", "cr"):
            parts.append("\n")
        elif tag == "noBreakHyphen":
            parts.append("-")
        elif tag in ("p", "tbl"):
            parts.append("\n")
            _w_runs(c, parts, ctx)
        elif tag in ("rPr", "pPr", "commentRangeStart", "commentRangeEnd"):
            continue
        else:
            _w_runs(c, parts, ctx)


def _w_para(p: ET.Element, ctx: _Ctx) -> str:
    parts: list[str] = []
    ctx.del_open = False
    _w_runs(p, parts, ctx)
    ctx.del_open = False
    body = "".join(parts).strip("\n")
    ppr = _child(p, "pPr")
    prefix = ""
    if ppr is not None:
        style = _child(ppr, "pStyle")
        val = _attr(style, "val") if style is not None else None
        if val:
            m = _HEADING_STYLE_RE.match(val)
            if m:
                prefix = "#" * min(int(m.group(1)), 6) + " "
            elif _TITLE_STYLE_RE.match(val):
                prefix = "# "
        if not prefix and _child(ppr, "numPr") is not None:
            prefix = "- "
    return prefix + body if body else ""


_W_WRAPPERS = ("sdt", "sdtContent", "customXml")


def _w_direct(el: ET.Element, want: str) -> list[ET.Element]:
    """Children with local name `want`, looking through content controls but not into nested tables."""
    found = []
    for c in el:
        tag = _local(c.tag)
        if tag == want:
            found.append(c)
        elif tag in _W_WRAPPERS:
            found.extend(_w_direct(c, want))
    return found


def _w_cells(tr: ET.Element) -> list[ET.Element]:
    return _w_direct(tr, "tc")


def _w_table(tbl: ET.Element, ctx: _Ctx) -> list[str]:
    tblpr = _child(tbl, "tblPr")
    if tblpr is not None:
        for node in tblpr:
            if _local(node.tag) in ("tblCaption", "tblDescription") and (_attr(node, "val") or "").strip():
                ctx.alt.append("alt text: " + _attr(node, "val").strip())
    rows: list[list[str]] = []
    for tr in _w_direct(tbl, "tr"):
        row = []
        for tc in _w_cells(tr):
            lines: list[str] = []
            _w_blocks(tc, lines, ctx)
            row.append(" ".join(line for line in lines if line))
        rows.append(row)
    return _md_rows(rows)


def _w_hidden_blocks(el: ET.Element, ctx: _Ctx, label: str) -> None:
    lines: list[str] = []
    _w_blocks(el, lines, ctx)
    ctx.hidden.extend("%s: %s" % (label, line) for line in lines if line)


def _w_blocks(el: ET.Element, out: list[str], ctx: _Ctx) -> None:
    for c in el:
        tag = _local(c.tag)
        if tag == "p":
            out.append(_w_para(c, ctx))
        elif tag == "tbl":
            out.extend(_w_table(c, ctx))
            out.append("")
        elif tag in ("oMathPara", "oMath"):
            # an equation at block level (Word wraps them in a paragraph, the schema does not require it)
            out.append(_w_para(c, ctx))
        elif tag == "AlternateContent":
            shown, others = _mc_pick(c)
            if shown is not None:
                _w_blocks(shown, out, ctx)
            for other in others:
                _w_hidden_blocks(other, ctx, "alternate content")
        elif tag == "Fallback":
            _w_hidden_blocks(c, ctx, "alternate content")
        elif tag == "altChunk":
            target, external = ctx.rels.get(_rid(c) or "", ("", True))
            if ctx.parts is not None and not external and target and ctx.parts.has(target):
                if target not in ctx.parts.child_parts:
                    ctx.parts.child_parts.append(target)
                out.append("[alt chunk, read as a separate part of this file]")
        elif tag == "sdtPr":
            _w_sdt_props(c, ctx)
        elif tag in ("sectPr", "tblPr", "tblGrid", "pPr", "tcPr", "trPr"):
            continue
        else:
            _w_blocks(c, out, ctx)


_W_FIXED = {
    "footnotes": r"word/footnotes\.xml",
    "endnotes": r"word/endnotes\.xml",
    "header": r"word/header\d*\.xml",
    "footer": r"word/footer\d*\.xml",
    "comments": r"word/comments\.xml",
    "people": r"word/people\.xml",
    "numbering": r"word/numbering\.xml",
}


def _docx(parts: _Parts, ex: Extraction, out: list[str], det: list[str]) -> None:
    main = parts.main_part("word/document.xml")
    root = parts.get(main)
    if root is None:
        ex.state = "failed"
        ex.notes.append("main document part missing or not well-formed")
        return
    body = _child(root, "body")
    ctx = _Ctx(parts=parts, rels=parts.rels_for(main))
    if body is not None:
        _w_blocks(body, out, ctx)
    # authors of tracked changes (w:ins, w:del, w:rPrChange ...) sit in attributes only
    revision_authors = _attr_values(root, "author")

    for kind, label in (("footnotes", "footnote"), ("endnotes", "endnote")):
        for part_name in parts.by_rels_or_name(main, kind, _W_FIXED[kind]):
            root = parts.get(part_name)
            if root is None:
                continue
            ctx.rels = parts.rels_for(part_name)
            revision_authors.extend(a for a in _attr_values(root, "author") if a not in revision_authors)
            notes = []
            for note in root:
                if _local(note.tag) != label:
                    continue
                if (_attr(note, "type") or "") in ("separator", "continuationSeparator"):
                    continue
                lines: list[str] = []
                _w_blocks(note, lines, ctx)
                joined = " ".join(line for line in lines if line).strip()
                if joined:
                    notes.append("[%s %s] %s" % (label, _attr(note, "id") or "", joined))
            out.extend(notes)

    hf_count = 0
    hf_parts = parts.by_rels_or_name(main, "header", _W_FIXED["header"])
    hf_parts += [n for n in parts.by_rels_or_name(main, "footer", _W_FIXED["footer"]) if n not in hf_parts]
    for name in hf_parts:
        root = parts.get(name)
        if root is None:
            continue
        ctx.rels = parts.rels_for(name)
        revision_authors.extend(a for a in _attr_values(root, "author") if a not in revision_authors)
        lines: list[str] = []
        _w_blocks(root, lines, ctx)
        lines = [line for line in lines if line]
        if lines:
            hf_count += 1
            det.extend(lines)
    if hf_count:
        ex.meta["header_footer_parts"] = hf_count

    comment_count = 0
    for name in parts.by_rels_or_name(main, "comments", _W_FIXED["comments"]):
        root = parts.get(name)
        if root is None:
            continue
        ctx.rels = parts.rels_for(name)
        for cm in root:
            if _local(cm.tag) != "comment":
                continue
            comment_count += 1
            lines = []
            _w_blocks(cm, lines, ctx)
            text = " ".join(line for line in lines if line)
            author = (_attr(cm, "author") or "").strip()
            initials = (_attr(cm, "initials") or "").strip()
            who = "%s, %s" % (author, initials) if initials else author
            det.append("comment by %s: %s" % (who, text))
    if comment_count:
        ex.meta["comment_count"] = comment_count

    for name in parts.by_rels_or_name(main, "people", _W_FIXED["people"]):
        root = parts.get(name)
        if root is None:
            continue
        for person in root.iter():
            if _local(person.tag) != "person":
                continue
            ids = [_attr(person, "author") or ""]
            ids.extend(_attr(info, "userId") or "" for info in person if _local(info.tag) == "presenceInfo")
            ids = [i.strip() for i in ids if i and i.strip()]
            if ids:
                det.append("person: " + " ".join(ids))

    # the level text of a numbering stands before every list item; "%1." carries nothing, words do
    for name in parts.by_rels_or_name(main, "numbering", _W_FIXED["numbering"]):
        root = parts.get(name)
        if root is None:
            continue
        for node in root.iter():
            if _local(node.tag) == "lvlText":
                value = (_attr(node, "val") or "").strip()
                if value and re.search(r"[^\W\d_]", value):
                    det.append("numbering: " + value)

    ctx.rels = parts.rels_for(main)
    _leftover_graphics(parts, ctx, out, (r"word/charts/chart(?:[eE]x)?\d*\.xml", r"word/diagrams/data\d*\.xml"))
    if revision_authors:
        ex.meta["revision_authors"] = len(revision_authors)
        det.extend("revision by " + a for a in revision_authors)
    _flush_ctx(ctx, ex, det)


def _flush_ctx(ctx: _Ctx, ex: Extraction, det: list[str]) -> None:
    deleted = [d.strip() for d in ctx.deleted if d.strip()]
    if deleted:
        ex.meta["deleted_count"] = len(deleted)
        det.extend("deleted: " + d for d in deleted)
    if ctx.alt:
        det.extend(ctx.alt)
    if ctx.fields:
        det.extend("field: " + f for f in ctx.fields if f)
    if ctx.comments:
        ex.meta["comment_count"] = ex.meta.get("comment_count", 0) + len(ctx.comments)
        det.extend(ctx.comments)
    if ctx.footnotes:
        det.extend(ctx.footnotes)
    if ctx.links:
        det.extend("link: " + link for link in ctx.links)
    if ctx.hidden:
        det.extend(ctx.hidden)
    if ctx.formulas:
        det.extend(ctx.formulas)


# --------------------------------------------------------------------------- xlsx


def _col_index(ref: str) -> int:
    m = _COL_RE.match(ref or "")
    if not m:
        return 0
    n = 0
    for ch in m.group(1):
        n = n * 26 + (ord(ch) - 64)
    return n


def _shared_strings(parts: _Parts, main: str, ctx: _Ctx) -> list[str]:
    """The shared string table, reached through the workbook relationships or under its fixed name. Phonetic
    runs (shown above the cell text when phonetic display is on) go to detection."""
    names = parts.targets(main, "sharedStrings") or ["xl/sharedStrings.xml"]
    root = parts.get(names[0])
    if root is None:
        return []
    strings = []
    for si in root:
        if _local(si.tag) != "si":
            continue
        buf = []
        phonetic = []
        for node in si.iter():
            if _local(node.tag) == "t":
                if _inside(node, si, "rPh"):
                    phonetic.append(node.text or "")
                else:
                    buf.append(node.text or "")
        strings.append("".join(buf))
        if "".join(phonetic).strip():
            ctx.hidden.append("phonetic: " + "".join(phonetic).strip())
    return strings


_NUMFMT_LITERAL_RE = re.compile(r'"([^"]*)"')


def _numfmt_texts(root: ET.Element | None) -> list[str]:
    """Literal text of number formats: the cell shows it, the value does not carry it."""
    out = []
    if root is None:
        return out
    for el in root.iter():
        if _local(el.tag) == "numFmt":
            code = _attr(el, "formatCode") or ""
            literals = [m.group(1) for m in _NUMFMT_LITERAL_RE.finditer(code) if re.search(r"[^\W\d_]", m.group(1))]
            if literals:
                out.append("number format: " + " ".join(literals))
    return out


def _xlsx_sheet_extras(root: ET.Element, ctx: _Ctx) -> None:
    """Validation prompts, errors and lists, conditional formatting formulas and hyperlink texts of one
    worksheet: the reader sees them, the cells do not carry them."""
    for el in root.iter():
        tag = _local(el.tag)
        if tag == "dataValidation":
            values = [(_attr(el, key) or "").strip() for key in ("promptTitle", "prompt", "errorTitle", "error")]
            values.extend((f.text or "").strip() for f in el.iter() if _local(f.tag) in ("formula1", "formula2", "f"))
            values = [v for v in values if v]
            if values:
                ctx.hidden.append("validation: " + " | ".join(values))
        elif tag == "cfRule":
            for f in el.iter():
                if _local(f.tag) in ("formula", "f") and (f.text or "").strip():
                    ctx.hidden.append("conditional format: =" + f.text.strip())
        elif tag == "hyperlink":
            values = [(_attr(el, key) or "").strip() for key in ("display", "tooltip", "location")]
            values = [v for v in values if v]
            if values:
                ctx.hidden.append("hyperlink: " + " | ".join(values))


_VML_TEXTBOX_RE = re.compile(r"(?is)<v:textbox\b[^>]*>(.*?)</v:textbox>")


def _vml_textboxes(parts: _Parts, det: list[str]) -> None:
    """Legacy text boxes (vml drawings are html-like, often not well-formed xml) go to detection."""
    from awb.extract.text import decode_bytes, strip_html

    for name in parts.matching(r"(?:xl|word|ppt)/drawings/vmlDrawing\d*\.vml"):
        data = parts.raw(name)
        if not data:
            continue
        text, _ = decode_bytes(data)
        for m in _VML_TEXTBOX_RE.finditer(text):
            inner = " ".join(strip_html(m.group(1)).split())
            if inner:
                det.append("text box: " + inner)


def _inside(node: ET.Element, root: ET.Element, tag: str) -> bool:
    """True when `node` sits under an element with local name `tag` below `root` (shallow check)."""
    for c in root:
        if _local(c.tag) == tag and any(n is node for n in c.iter()):
            return True
    return False


def _hf_clean(value: str) -> str:
    return _HF_CODE_RE.sub(" ", value.replace("&&", "\x00")).replace("\x00", "&").strip()


def _cell_value(c: ET.Element, shared: list[str], ctx: _Ctx) -> tuple[str, str | None]:
    """(display value, formula text or None) of one cell element."""
    ctype = _attr(c, "t") or "n"
    v = _child(c, "v")
    f = _child(c, "f")
    formula = None
    if f is not None:
        formula = (f.text or "").strip()
        if not formula:
            formula = "(shared formula)"
    if ctype == "s":
        try:
            value = shared[int((v.text or "").strip())] if v is not None else ""
        except (ValueError, IndexError):
            value = ""
            ctx.missing_strings += 1
    elif ctype == "inlineStr":
        is_ = _child(c, "is")
        value = _all_text(is_) if is_ is not None else ""
    elif ctype == "b":
        value = "TRUE" if v is not None and (v.text or "").strip() == "1" else "FALSE"
    else:
        value = (v.text or "") if v is not None else ""
    return value, formula


def _xlsx_sheet(root: ET.Element, shared: list[str], sheet_hidden: bool, out: list[str], ctx: _Ctx) -> int:
    """Render one worksheet. Returns the number of cells seen."""
    hidden_cols: set[int] = set()
    for col in root.iter():
        if _local(col.tag) == "col" and (_attr(col, "hidden") or "0") in ("1", "true"):
            try:
                lo, hi = int(_attr(col, "min") or 0), int(_attr(col, "max") or 0)
            except ValueError:
                continue
            hidden_cols.update(range(lo, min(hi, lo + _MAX_REPEAT) + 1))
    visible_rows: list[list[str]] = []
    hidden_rows: list[list[str]] = []
    cells_seen = 0
    sheet_data = None
    for node in root:
        if _local(node.tag) == "sheetData":
            sheet_data = node
            break
    if sheet_data is None:
        return 0
    for row in sheet_data:
        if _local(row.tag) != "row":
            continue
        row_hidden = (_attr(row, "hidden") or "0") in ("1", "true")
        visible: dict[int, str] = {}
        hidden: dict[int, str] = {}
        for c in row:
            if _local(c.tag) != "c":
                continue
            cells_seen += 1
            ref = _attr(c, "r") or ""
            idx = _col_index(ref) or (max(list(visible) + list(hidden) + [0]) + 1)
            value, formula = _cell_value(c, shared, ctx)
            if formula is not None:
                ctx.formulas.append("formula %s: =%s -> %s" % (ref, formula, value))
            if not value:
                continue
            if sheet_hidden or row_hidden or idx in hidden_cols:
                hidden[idx] = value
            else:
                visible[idx] = value
        if visible:
            visible_rows.append(_positional(visible))
        if hidden:
            hidden_rows.append(_positional(hidden))
    out.extend(_md_rows(visible_rows))
    if hidden_rows:
        ctx.hidden.extend(_md_rows(hidden_rows))
    hf = _child(root, "headerFooter")
    if hf is not None:
        for node in hf:
            if node.text and node.text.strip():
                ctx.hidden.append("%s: %s" % (_local(node.tag), _hf_clean(node.text)))
    _xlsx_sheet_extras(root, ctx)
    return cells_seen


def _positional(cells: dict[int, str]) -> list[str]:
    """Cells in column order. Columns beyond the width limit are appended, never dropped."""
    width = min(max(cells), _MAX_REPEAT)
    row = [cells.get(i, "") for i in range(1, width + 1)]
    row.extend(cells[i] for i in sorted(cells) if i > width)
    return row


def _xlsx(parts: _Parts, ex: Extraction, out: list[str], det: list[str]) -> None:
    main = parts.main_part("xl/workbook.xml")
    wb = parts.get(main)
    if wb is None:
        ex.state = "failed"
        ex.notes.append("workbook part missing or not well-formed")
        return
    rels = parts.rels_for(main)
    ctx = _Ctx(parts=parts, rels=rels)
    shared = _shared_strings(parts, main, ctx)
    sheets = []
    for node in wb.iter():
        if _local(node.tag) == "sheet":
            rid = _rid(node) or ""
            target = rels.get(rid, ("", False))[0]
            sheets.append((_attr(node, "name") or "", (_attr(node, "state") or "visible").lower(), target))
        elif _local(node.tag) == "definedName":
            ctx.formulas.append("defined name %s: %s" % (_attr(node, "name") or "", (node.text or "").strip()))
    hidden_sheets = 0
    cells = 0
    seen_targets = set()
    for name, state, target in sheets:
        hidden = state in ("hidden", "veryhidden")
        hidden_sheets += hidden
        det.append("sheet: %s" % name)
        root = parts.get(target) if target else None
        if root is None:
            ex.notes.append("a sheet part is missing or not well-formed")
            continue
        seen_targets.add(target)
        if hidden:
            ctx.hidden.append("hidden sheet: %s" % name)
        else:
            out.append("## %s" % name)
        cells += _xlsx_sheet(root, shared, hidden, out, ctx)
        out.append("")
    for name in parts.matching(r"xl/worksheets/sheet\d+\.xml"):
        if name not in seen_targets:
            root = parts.get(name)
            if root is not None:
                cells += _xlsx_sheet(root, shared, True, out, ctx)
    ex.meta["sheet_count"] = len(sheets)
    ex.meta["hidden_sheet_count"] = hidden_sheets
    ex.meta["cell_count"] = cells
    if ctx.missing_strings:
        ex.meta[INCOMPLETE] = True
        ex.notes.append("%d string cell(s) point past the shared string table, shown empty" % ctx.missing_strings)
    styles = parts.targets(main, "styles") or ["xl/styles.xml"]
    det.extend(_numfmt_texts(parts.get(styles[0])))

    comment_count = 0
    for name in parts.matching(r"xl/comments\d*\.xml|xl/comments/comment\d*\.xml"):
        root = parts.get(name)
        if root is None:
            continue
        authors = [a.text or "" for a in root.iter() if _local(a.tag) == "author"]
        for cm in root.iter():
            if _local(cm.tag) != "comment":
                continue
            comment_count += 1
            try:
                author = authors[int(_attr(cm, "authorId") or 0)]
            except (ValueError, IndexError):
                author = ""
            text = _all_text(cm)
            det.append("comment on %s by %s: %s" % (_attr(cm, "ref") or "", author, text))
    persons = {}
    for name in parts.matching(r"xl/persons/person\d*\.xml"):
        root = parts.get(name)
        if root is None:
            continue
        for p in root.iter():
            if _local(p.tag) == "person":
                persons[_attr(p, "id") or ""] = _attr(p, "displayName") or ""
                det.append("person: %s %s" % (_attr(p, "displayName") or "", _attr(p, "userId") or ""))
    for name in parts.matching(r"xl/threadedComments/threadedComment\d*\.xml"):
        root = parts.get(name)
        if root is None:
            continue
        for cm in root.iter():
            if _local(cm.tag) == "threadedComment":
                comment_count += 1
                who = persons.get(_attr(cm, "personId") or "", "")
                det.append("comment on %s by %s: %s" % (_attr(cm, "ref") or "", who, _all_text(cm, "text")))
    if comment_count:
        ex.meta["comment_count"] = comment_count

    for name in parts.matching(r"xl/drawings/[^/]+\.xml"):
        root = parts.get(name)
        if root is not None:
            text = _a_text_lines(root)
            if text:
                det.append("drawing: " + " ".join(text))
            for el in root.iter():
                if _local(el.tag) in _ALT_TAGS:
                    _alt_text(el, ctx)
    for name in parts.matching(r"xl/charts/[^/]+\.xml"):
        root = parts.get(name)
        if root is not None:
            det.extend("chart: " + line for line in _chart_lines(root))
            for el in root.iter():
                if _local(el.tag) in _ALT_TAGS:
                    _alt_text(el, ctx)
    _vml_textboxes(parts, det)
    for name in parts.matching(r"xl/tables/table\d*\.xml"):
        root = parts.get(name)
        if root is None:
            continue
        det.append("table: %s" % (_attr(root, "displayName") or _attr(root, "name") or ""))
        for col in root.iter():
            if _local(col.tag) == "tableColumn" and _attr(col, "name"):
                det.append("table column: %s" % _attr(col, "name"))
                if _attr(col, "totalsRowLabel"):
                    det.append("table totals: %s" % _attr(col, "totalsRowLabel"))
    for name in parts.matching(r"xl/queryTables/queryTable\d*\.xml"):
        root = parts.get(name)
        if root is None:
            continue
        values = [_attr(root, "name") or ""]
        values.extend(_attr(f, "name") or "" for f in root.iter() if _local(f.tag) == "queryTableField")
        det.append("query table: " + " | ".join(v for v in values if v))
    for name in parts.matching(r"xl/slicerCaches/slicerCache\d*\.xml|xl/slicers/slicer\d*\.xml"):
        root = parts.get(name)
        if root is None:
            continue
        values: dict[str, None] = {}
        for el in root.iter():
            for key in ("name", "sourceName", "caption"):
                value = (_attr(el, key) or "").strip()
                if value:
                    values.setdefault(value, None)
        if values:
            det.append("slicer: " + " | ".join(values))
    # the cached cells of a linked workbook are what the referencing cells show after the load-time refresh
    for name in parts.by_rels_or_name(main, "externalLink", r"xl/externalLinks/externalLink\d*\.xml"):
        root = parts.get(name)
        if root is None:
            continue
        values = []
        for el in root.iter():
            tag = _local(el.tag)
            if tag == "sheetName" and _attr(el, "val"):
                values.append(_attr(el, "val"))
            elif tag == "cell":
                v = _child(el, "v")
                if v is not None and (v.text or "").strip():
                    values.append(v.text.strip())
            elif tag == "definedName":
                values.extend(x for x in (_attr(el, "name"), _attr(el, "refersTo")) if x)
        if values:
            det.append("external link: " + " | ".join(values))
    # data connections carry server names, user ids and queries; pivot caches carry source data that
    # may appear in no sheet at all
    root = parts.get("xl/connections.xml")
    if root is not None:
        for el in root.iter():
            for key in ("name", "description", "connection", "command", "sourceFile", "url"):
                value = (_attr(el, key) or "").strip()
                if value:
                    det.append("connection %s: %s" % (key, value))
    for name in parts.matching(r"xl/pivotCache/[^/]+\.xml|xl/pivotTables/[^/]+\.xml"):
        root = parts.get(name)
        if root is None:
            continue
        values = []
        for el in root.iter():
            tag = _local(el.tag)
            if tag in ("s", "cacheField", "pivotTableDefinition", "pivotCacheDefinition", "dataField", "worksheetSource"):
                for key in ("v", "name", "sheet", "ref", "refreshedBy", "dataCaption"):
                    value = (_attr(el, key) or "").strip()
                    if value:
                        values.append(value)
        if values:
            det.append("pivot: " + "\n".join(dict.fromkeys(values)))
    _flush_ctx(ctx, ex, det)


# --------------------------------------------------------------------------- pptx


def _a_hidden(el: ET.Element, ctx: _Ctx, label: str = "alternate content") -> None:
    lines: list[str] = []
    _a_blocks(el, lines, ctx)
    ctx.hidden.extend("%s: %s" % (label, line) for line in lines if line)


def _a_blocks(el: ET.Element, out: list[str], ctx: _Ctx | None = None) -> None:
    for c in el:
        tag = _local(c.tag)
        if tag == "AlternateContent":
            shown, others = _mc_pick(c)
            if shown is not None:
                _a_blocks(shown, out, ctx)
            if ctx is not None:
                for other in others:
                    _a_hidden(other, ctx)
            continue
        if tag == "Fallback":
            if ctx is not None:
                _a_hidden(c, ctx)
            continue
        if tag == "tbl":
            rows = []
            for tr in c:
                if _local(tr.tag) != "tr":
                    continue
                row = []
                for tc in tr:
                    if _local(tc.tag) != "tc":
                        continue
                    lines: list[str] = []
                    _a_blocks(tc, lines, ctx)
                    row.append(" ".join(line for line in lines if line))
                rows.append(row)
            out.extend(_md_rows(rows))
            out.append("")
        elif tag == "p":
            out.append(_a_para(c))
        elif tag == "chart" and ctx is not None:
            out.extend(_graphic_lines(ctx, _rid(c), "chart"))
        elif tag == "relIds" and ctx is not None:
            out.extend(_graphic_lines(ctx, _rid(c, "dm"), "diagram"))
        else:
            _a_blocks(c, out, ctx)


def _a_text_lines(root: ET.Element) -> list[str]:
    lines: list[str] = []
    _a_blocks(root, lines)
    return [line for line in lines if line]


def _pptx_slides(parts: _Parts, main: str) -> tuple[list[str], list[str]]:
    """(slide parts in presentation order through the rels, slide-named parts the presentation does not list)."""
    pres = parts.get(main)
    rels = parts.rels_for(main)
    listed: list[str] = []
    if pres is not None:
        for node in pres.iter():
            if _local(node.tag) != "sldId":
                continue
            target, external = rels.get(_rid(node) or "", ("", True))
            if not external and target and parts.has(target) and target not in listed:
                listed.append(target)
    named = parts.matching(r"ppt/slides/slide\d+\.xml")
    if not listed:
        return named, []
    return listed, [n for n in named if n not in listed]


def _pptx(parts: _Parts, ex: Extraction, out: list[str], det: list[str]) -> None:
    main = parts.main_part("ppt/presentation.xml")
    slides, orphans = _pptx_slides(parts, main)
    if not slides and not parts.has(main):
        ex.state = "failed"
        ex.notes.append("presentation part missing")
        return
    notes_count = 0
    ctx = _Ctx(parts=parts)
    for i, name in enumerate(slides, 1):
        root = parts.get(name)
        if root is None:
            continue
        rels = parts.rels_for(name)
        ctx.rels = rels
        out.append("## Slide %d" % i)
        _a_blocks(root, out, ctx)
        for el in root.iter():
            if _local(el.tag) in _ALT_TAGS:
                _alt_text(el, ctx)
        notes_part = None
        for target, external in rels.values():
            if not external and "notesSlides/" in target:
                notes_part = target
                break
        if notes_part is None:
            candidate = name.replace("ppt/slides/slide", "ppt/notesSlides/notesSlide")
            notes_part = candidate if parts.has(candidate) else None
        if notes_part:
            nroot = parts.get(notes_part)
            if nroot is not None:
                lines = [line for line in _a_text_lines(nroot) if line and not line.isdigit()]
                if lines:
                    notes_count += 1
                    out.append("Notes:")
                    out.extend(lines)
        out.append("")
    # a slide part the presentation does not list is not shown, like an unlisted sheet
    for name in orphans:
        root = parts.get(name)
        if root is None:
            continue
        ctx.rels = parts.rels_for(name)
        _a_hidden(root, ctx, "unlisted slide")
    ex.meta["slide_count"] = len(slides)
    ex.meta["notes_count"] = notes_count
    pres = parts.get(main)
    if pres is not None:
        for node in pres.iter():
            if _local(node.tag) == "section" and (_attr(node, "name") or "").strip():
                det.append("section: " + _attr(node, "name").strip())
    for name in parts.matching(r"ppt/tags/tag\d*\.xml"):
        root = parts.get(name)
        if root is None:
            continue
        for tag in root.iter():
            if _local(tag.tag) == "tag" and (_attr(tag, "val") or "").strip():
                det.append("tag %s: %s" % (_attr(tag, "name") or "", _attr(tag, "val").strip()))
    _leftover_graphics(parts, ctx, out, (r"ppt/charts/chart(?:[eE]x)?\d*\.xml", r"ppt/diagrams/data\d*\.xml"))

    authors = {}
    for name in parts.matching(r"ppt/(commentAuthors|authors)\.xml"):
        root = parts.get(name)
        if root is None:
            continue
        for a in root.iter():
            if _local(a.tag) in ("cmAuthor", "author") and _attr(a, "name"):
                authors[_attr(a, "id") or ""] = _attr(a, "name") or ""
                det.append("comment author: %s" % _attr(a, "name"))
    comment_count = 0
    for name in parts.matching(r"ppt/comments/[^/]+\.xml"):
        root = parts.get(name)
        if root is None:
            continue
        for cm in root.iter():
            if _local(cm.tag) != "cm":
                continue
            comment_count += 1
            who = authors.get(_attr(cm, "authorId") or "", "")
            text = _all_text(cm, "text") or " ".join(_a_text_lines(cm))
            det.append("comment by %s: %s" % (who, text))
    if comment_count:
        ex.meta["comment_count"] = comment_count
    # layouts and masters repeat the same prompts many times: each line once is enough for detection
    template_lines: dict[str, None] = {}
    for name in parts.matching(r"ppt/(slideLayouts|slideMasters|notesMasters|handoutMasters)/[^/]+\.xml"):
        root = parts.get(name)
        if root is not None:
            for line in _a_text_lines(root):
                template_lines.setdefault(line, None)
    det.extend(template_lines)
    _flush_ctx(ctx, ex, det)


# --------------------------------------------------------------------------- odf


def _odf_text(el: ET.Element, ctx: _Ctx) -> str:
    parts = [el.text or ""]
    for c in el:
        tag = _local(c.tag)
        if tag == "s":
            try:
                parts.append(" " * int(_attr(c, "c") or 1))
            except ValueError:
                parts.append(" ")
        elif tag == "tab":
            parts.append("\t")
        elif tag == "line-break":
            parts.append("\n")
        elif tag == "note":
            body = _child(c, "note-body")
            if body is not None:
                lines: list[str] = []
                _odf_blocks(body, lines, ctx)
                ctx.footnotes.append("[note] " + " ".join(line for line in lines if line))
        elif tag == "annotation":
            _odf_annotation(c, ctx)
        elif tag in ("annotation-end", "change-start", "change-end", "change", "bookmark", "bookmark-start", "bookmark-end"):
            pass
        elif tag == "a":
            href = _attr(c, "href")
            if href:
                ctx.links.append(href)
            parts.append(_odf_text(c, ctx))
        elif tag == "hidden-text":
            # the value lives in an attribute; shown unless the condition hides it
            value = (c.text or "").strip() or (_attr(c, "string-value") or "").strip()
            if (_attr(c, "is-hidden") or "false").lower() == "true":
                if value:
                    ctx.hidden.append("hidden text: " + value)
            else:
                parts.append(value)
        elif tag == "conditional-text":
            on_true = (_attr(c, "string-value-if-true") or "").strip()
            on_false = (_attr(c, "string-value-if-false") or "").strip()
            current = (_attr(c, "current-value") or "true").lower()
            shown = (c.text or "").strip() or (on_true if current == "true" else on_false)
            parts.append(shown)
            ctx.hidden.extend("conditional text: " + v for v in (on_true, on_false) if v and v != shown)
        elif tag in ("user-field-get", "variable-get", "user-field-input"):
            # an empty field shows its declared value once the application evaluates it
            parts.append((c.text or "").strip() or ctx.decls.get(_attr(c, "name") or "", ""))
        elif tag in ("p", "h"):
            parts.append("\n" + _odf_text(c, ctx))
        else:
            parts.append(_odf_text(c, ctx))
        parts.append(c.tail or "")
    return "".join(parts)


def _odf_annotation(el: ET.Element, ctx: _Ctx) -> None:
    creator = ""
    lines: list[str] = []
    for c in el:
        tag = _local(c.tag)
        if tag == "creator":
            creator = (c.text or "").strip()
        elif tag in ("p", "list"):
            lines.append(_odf_text(c, ctx).strip())
    ctx.comments.append("comment by %s: %s" % (creator, " ".join(line for line in lines if line)))


def _odf_repeat(el: ET.Element, name: str) -> int:
    try:
        return max(1, min(int(_attr(el, name) or 1), _MAX_REPEAT))
    except ValueError:
        return 1


_ODF_COLUMN_GROUPS = ("table-columns", "table-header-columns", "table-column-group")
_ODF_ROW_GROUPS = ("table-rows", "table-header-rows", "table-row-group")


def _odf_direct(el: ET.Element, want: str, groups: tuple[str, ...]) -> list[ET.Element]:
    """Children named `want`, looking through row or column groups but not into nested tables."""
    found = []
    for c in el:
        tag = _local(c.tag)
        if tag == want:
            found.append(c)
        elif tag in groups:
            found.extend(_odf_direct(c, want, groups))
    return found


def _odf_table(tbl: ET.Element, out: list[str], ctx: _Ctx, det: list[str]) -> None:
    name = _attr(tbl, "name") or ""
    if name:
        if ctx.sheets:
            det.append("sheet: %s" % name)
            out.append("## %s" % name)
        else:
            det.append("table: %s" % name)
    shapes = _child(tbl, "shapes")
    if shapes is not None:
        # text boxes drawn on a sheet are body content for the reader
        lines: list[str] = []
        _odf_blocks(shapes, lines, ctx, det)
        out.extend(line for line in lines if line)
    hidden_cols: set[int] = set()
    col_index = 0
    for node in _odf_direct(tbl, "table-column", _ODF_COLUMN_GROUPS):
        n = _odf_repeat(node, "number-columns-repeated")
        if (_attr(node, "visibility") or "visible") != "visible":
            hidden_cols.update(range(col_index, col_index + n))
        col_index += n
    visible_rows: list[list[str]] = []
    hidden_rows: list[list[str]] = []
    for row in _odf_direct(tbl, "table-row", _ODF_ROW_GROUPS):
        row_hidden = (_attr(row, "visibility") or "visible") != "visible"
        visible: dict[int, str] = {}
        hidden: dict[int, str] = {}
        idx = 0
        for cell in row:
            tag = _local(cell.tag)
            if tag not in ("table-cell", "covered-table-cell"):
                continue
            n = _odf_repeat(cell, "number-columns-repeated")
            lines: list[str] = []
            if tag == "table-cell":
                _odf_blocks(cell, lines, ctx)
            value = " ".join(line for line in lines if line).strip()
            if not value:
                value = _attr(cell, "value") or _attr(cell, "string-value") or _attr(cell, "date-value") or ""
            formula = _attr(cell, "formula")
            if formula:
                ctx.formulas.append("formula: %s -> %s" % (formula, value))
            if value:
                for k in range(idx, idx + min(n, 8)):
                    if row_hidden or k in hidden_cols:
                        hidden[k + 1] = value
                    else:
                        visible[k + 1] = value
            idx += n
        if visible:
            visible_rows.append(_positional(visible))
        if hidden:
            hidden_rows.append(_positional(hidden))
    out.extend(_md_rows(visible_rows))
    out.append("")
    if hidden_rows:
        ctx.hidden.extend(_md_rows(hidden_rows))


def _odf_blocks(el: ET.Element, out: list[str], ctx: _Ctx, det: list[str] | None = None) -> None:
    det = det if det is not None else []
    for c in el:
        tag = _local(c.tag)
        if tag == "tracked-changes":
            for region in c.iter():
                rtag = _local(region.tag)
                if rtag == "change-info":
                    for info in region:
                        if _local(info.tag) == "creator" and (info.text or "").strip():
                            det.append("revision by " + info.text.strip())
                elif rtag in ("deletion", "insertion"):
                    lines: list[str] = []
                    _odf_blocks(region, lines, ctx, det)
                    text = " ".join(line for line in lines if line).strip()
                    if text:
                        ctx.del_open = False
                        ctx.add_deleted(text)
                        ctx.del_open = False
        elif tag == "h":
            try:
                level = min(int(_attr(c, "outline-level") or 1), 6)
            except ValueError:
                level = 1
            out.append("#" * level + " " + _odf_text(c, ctx).strip())
        elif tag == "p":
            out.append(_odf_text(c, ctx).strip())
        elif tag == "list":
            for item in c:
                if _local(item.tag) not in ("list-item", "list-header"):
                    continue
                lines = []
                _odf_blocks(item, lines, ctx, det)
                out.append("- " + " ".join(line for line in lines if line))
        elif tag == "table":
            _odf_table(c, out, ctx, det)
        elif tag == "page":
            out.append("## Slide %s" % (_attr(c, "name") or ""))
            _odf_blocks(c, out, ctx, det)
            out.append("")
        elif tag == "notes":
            lines = []
            _odf_blocks(c, lines, ctx, det)
            lines = [line for line in lines if line]
            if lines:
                out.append("Notes:")
                out.extend(lines)
        elif tag == "annotation":
            _odf_annotation(c, ctx)
        elif tag in ("title", "desc"):
            if (c.text or "").strip():
                ctx.alt.append("alt text: " + c.text.strip())
        elif tag == "named-expressions":
            for expr in c:
                det.append(
                    "defined name %s: %s"
                    % (_attr(expr, "name") or "", _attr(expr, "cell-range-address") or _attr(expr, "expression") or "")
                )
        elif tag in ("variable-decls", "user-field-decls"):
            # declared values show in the body wherever a field of that name stands
            for decl in c:
                name = _attr(decl, "name") or ""
                value = (_attr(decl, "string-value") or _attr(decl, "value") or "").strip()
                if value:
                    ctx.decls[name] = value
                    det.append("field %s: %s" % (name, value))
        elif tag == "forms":
            for control in c.iter():
                values = [(_attr(control, key) or "").strip() for key in ("label", "current-value", "value", "title")]
                values = [v for v in values if v]
                if values:
                    det.append("form control: " + " | ".join(values))
        elif tag in ("sequence-decls", "font-face-decls", "automatic-styles", "scripts", "settings"):
            continue
        else:
            _odf_blocks(c, out, ctx, det)


def _odf(parts: _Parts, ex: Extraction, out: list[str], det: list[str]) -> None:
    content = parts.get("content.xml")
    if content is None:
        ex.state = "failed"
        ex.notes.append("content part missing or not well-formed")
        return
    ctx = _Ctx(sheets=ex.kind == "ods")
    body = _child(content, "body")
    if body is not None:
        _odf_blocks(body, out, ctx, det)
    styles = parts.get("styles.xml")
    if styles is not None:
        for node in styles.iter():
            if _local(node.tag) in ("header", "footer", "header-left", "footer-left", "header-first", "footer-first"):
                lines: list[str] = []
                _odf_blocks(node, lines, ctx, det)
                det.extend(line for line in lines if line)
    meta = parts.get("meta.xml")
    if meta is not None:
        keys = {
            "title": "title",
            "subject": "subject",
            "description": "description",
            "keyword": "keywords",
            "initial-creator": "author",
            "creator": "last_modified_by",
            "creation-date": "created",
            "date": "modified",
            "generator": "application",
            "printed-by": "printed_by",
        }
        for node in meta.iter():
            tag = _local(node.tag)
            if tag in keys and node.text and node.text.strip():
                key = keys[tag]
                if key == "keywords" and ex.meta.get(key):
                    ex.meta[key] += ", " + node.text.strip()
                else:
                    ex.meta[key] = node.text.strip()
            elif tag == "user-defined" and node.text and node.text.strip():
                ex.meta.setdefault("custom", {})[_attr(node, "name") or ""] = node.text.strip()
            elif tag == "document-statistic":
                pages = _attr(node, "page-count")
                if pages and pages.isdigit():
                    ex.meta["page_count"] = int(pages)
    _flush_ctx(ctx, ex, det)


# --------------------------------------------------------------------------- ooxml properties

_CORE_KEYS = {
    "title": "title",
    "subject": "subject",
    "creator": "author",
    "keywords": "keywords",
    "description": "description",
    "lastModifiedBy": "last_modified_by",
    "revision": "revision",
    "created": "created",
    "modified": "modified",
    "category": "category",
    "contentStatus": "content_status",
    "lastPrinted": "last_printed",
}
_APP_KEYS = {
    "Company": "company",
    "Manager": "manager",
    "Application": "application",
    "Pages": "page_count",
    "Slides": "slide_count",
    "Words": "word_count",
    "Template": "template",
    "HyperlinkBase": "hyperlink_base",
}


def _ooxml_props(parts: _Parts, ex: Extraction, det: list[str]) -> None:
    core = parts.get("docProps/core.xml")
    if core is not None:
        for node in core:
            tag = _local(node.tag)
            key = _CORE_KEYS.get(tag)
            if key and node.text and node.text.strip():
                ex.meta[key] = node.text.strip()
                if tag == "creator":
                    ex.meta["creator"] = node.text.strip()
    app = parts.get("docProps/app.xml")
    if app is not None:
        for node in app:
            tag = _local(node.tag)
            key = _APP_KEYS.get(tag)
            if key and node.text and node.text.strip():
                value = node.text.strip()
                if key in ("page_count", "slide_count", "word_count"):
                    # a count taken from the container wins over the stored statistic, which can be stale
                    if value.isdigit() and key not in ex.meta:
                        ex.meta[key] = int(value)
                elif key not in ex.meta:
                    ex.meta[key] = value
            elif tag == "TitlesOfParts":
                titles = [n.text.strip() for n in node.iter() if _local(n.tag) == "lpstr" and n.text and n.text.strip()]
                if titles:
                    det.append("titles of parts: " + " | ".join(titles))
    custom = parts.get("docProps/custom.xml")
    if custom is not None:
        for prop in custom:
            if _local(prop.tag) != "property":
                continue
            name = _attr(prop, "name") or ""
            blob = _child(prop, "blob")
            if blob is not None:
                # a binary property is base64: its text or its printable strings go to detection
                text = _blob_text(blob.text or "")
                if text:
                    det.append("property %s: %s" % (name, text))
                continue
            value = "".join((n.text or "") for n in prop.iter() if n is not prop).strip()
            if name and value:
                ex.meta.setdefault("custom", {})[name] = value


def _blob_text(encoded: str) -> str:
    import base64
    import binascii

    from awb.extract.text import binary_strings, bytes_to_text

    try:
        raw = base64.b64decode("".join(encoded.split()), validate=True)
    except (binascii.Error, ValueError):
        return ""
    if not raw:
        return ""
    text = bytes_to_text(raw)
    if text is None:
        text = "\n".join(binary_strings(raw))
    return text.strip()


def _meta_lines(meta: dict) -> list[str]:
    lines = []
    for key, value in meta.items():
        if isinstance(value, str) and value:
            lines.append("%s: %s" % (key, value))
        elif isinstance(value, dict):
            lines.extend("%s: %s" % (k, v) for k, v in value.items() if isinstance(v, str) and v)
    return lines


_MEDIA_RE = r"(?:word|xl|ppt)/media/[^/]+|Pictures/[^/]+"
_EMBED_RE = r"(?:word|xl|ppt)/embeddings/[^/]+|Object \d+/content\.xml"
_MACRO_RE = r"(?:word|xl|ppt)/vbaProject\.bin"
_FONT_RE = r"(?:word|xl|ppt)/fonts/[^/]+"


def _container_notes(parts: _Parts, ex: Extraction, det: list[str], read_as_child: set[str], chunks: set[str]) -> None:
    """External targets to detect_text; counts of images, of embedded objects that are not read, of macro
    projects and embedded fonts that are not read, of alt chunks read as children."""
    for target in parts.external_targets():
        det.append("link: " + target)
    images = len(parts.matching(_MEDIA_RE))
    if images:
        ex.meta["image_count"] = images
        ex.notes.append("%d embedded image(s), no text layer, review the original" % images)
    embedded = [n for n in parts.matching(_EMBED_RE) if n not in read_as_child and n not in chunks]
    if embedded:
        ex.meta["embedded_count"] = len(embedded)
        ex.meta[INCOMPLETE] = True
        ex.notes.append("%d embedded object(s) not read, review the original" % len(embedded))
    if read_as_child:
        ex.notes.append("%d embedded package(s) read as parts of this file" % len(read_as_child))
    if chunks:
        ex.notes.append("%d alt chunk(s) read as parts of this file" % len(chunks))
    macros = set(parts.matching(_MACRO_RE))
    for name in parts.matching(r"(?:.*/)?_rels/[^/]*\.rels"):
        for _, target, external, rtype in parts.rel_rows(name):
            if not external and rtype.endswith("/vbaProject") and target in parts.name_set:
                macros.add(target)
    if macros:
        ex.meta["macro_count"] = len(macros)
        ex.meta[INCOMPLETE] = True
        ex.notes.append("macro project not read, review the original")
    fonts = len(parts.matching(_FONT_RE))
    if fonts:
        ex.meta["font_count"] = fonts
        ex.meta[INCOMPLETE] = True
        ex.notes.append("%d embedded font(s) not read, the document brings its own glyphs" % fonts)


# --------------------------------------------------------------------------- second pass: every part


_SKIP_RAW_RE = re.compile(r"(?i).*\.(?:ttf|otf|odttf|woff2?|fntdata)$")
_IMAGE_RE = re.compile(r"(?i).*\.(?:png|jpe?g|gif|bmp|tiff?|emf|wmf|wdp|svg|ico)$")
_PACKAGE_MAGIC = (b"PK\x03\x04",)


def _xml_strings(data: bytes) -> list[str] | None:
    """Every text node, tail and attribute value of an xml part, streamed with the element cap. Namespace
    declarations are not attributes to the parser. None when the part is not well-formed."""
    out: list[str] = []
    count = 0
    try:
        for event, el in ET.iterparse(io.BytesIO(data), events=("start", "end")):
            if event == "start":
                count += 1
                if count > MAX_ELEMENTS:
                    return None
                out.extend(v for v in el.attrib.values() if v and v.strip())
                continue
            if el.text and el.text.strip():
                out.append(el.text)
            if el.tail and el.tail.strip():
                out.append(el.tail)
            el.clear()
    except (ET.ParseError, ValueError):
        return None
    return out


def _child_of(data: bytes, name: str, index: int, depth: int, tmp: Path, default: str, mail: bool = False) -> Extraction:
    """Write the bytes of a part under an index and its suffix into `tmp` and read them as a child. `mail`
    sends the part to the mail reader (an mht chunk is a MIME archive that sniffing does not name)."""
    from awb.extract import extract

    _, dot, suffix = name.rpartition("/")[2].rpartition(".")
    dst = tmp / ("%04d.%s" % (index, re.sub(r"[^A-Za-z0-9]", "", suffix)[:8] if dot else default))
    fd = os.open(dst, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "wb") as fh:
        fh.write(data)
    if mail:
        from awb.extract import mail as mail_reader

        child = mail_reader.extract(dst, depth + 1)
    else:
        child = extract(dst, depth + 1)
    child.meta["member"] = name
    return child


_MAIL_HEAD_RE = re.compile(rb"(?i)^\s*(?:mime-version|content-type:\s*multipart)")


def _alt_chunks(parts: _Parts, ex: Extraction, depth: int) -> set[str]:
    """The alt chunk parts a reader collected are read as children (html, rtf, text, mht through the mail
    reader, a docx as an office file). Returns the names read."""
    done: set[str] = set()
    if not parts.child_parts:
        return done
    if depth >= MAX_ARCHIVE_DEPTH:
        ex.meta[INCOMPLETE] = True
        ex.notes.append("%d alt chunk(s) not read, nesting too deep" % len(parts.child_parts))
        return done
    tmp = make_temp_dir("awb-office-")
    try:
        for index, name in enumerate(parts.child_parts):
            data = parts.raw(name, MAX_PART_BYTES + 1)
            if data is None:
                continue
            if len(data) > MAX_PART_BYTES:
                parts.skipped += 1
                ex.meta[INCOMPLETE] = True
                continue
            mail = (name.lower().endswith((".mht", ".mhtml")) or parts.content_type(name) == "message/rfc822"
                    or bool(_MAIL_HEAD_RE.match(data[:256])))
            ex.children.append(_child_of(data, name, index, depth, tmp, "bin", mail=mail))
            done.add(name)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    return done


def _raw_parts(zf: zipfile.ZipFile, ex: Extraction, depth: int, skip: set[str]) -> tuple[list[str], set[str]]:
    """The second pass over every part not in `skip`. Returns the text for `scan_text` and the embedded
    packages that were read as children."""
    from awb.extract.text import binary_strings, decode_bytes, strip_html

    scan: list[str] = []
    as_child: set[str] = set()
    total = 0
    tmp: Path | None = None
    try:
        for index, info in enumerate(zf.infolist()):
            name = info.filename
            if info.is_dir() or _SKIP_RAW_RE.fullmatch(name) or name in skip:
                continue
            if info.flag_bits & 0x1:
                ex.meta[INCOMPLETE] = True
                continue
            limit = MEDIA_PROBE if _IMAGE_RE.fullmatch(name) else MAX_PART_BYTES
            if total >= MAX_RAW_TOTAL:
                ex.meta[INCOMPLETE] = True
                ex.notes.append("raw part limit reached, later parts not checked")
                break
            try:
                with zf.open(info) as fh:
                    data = fh.read(min(limit, MAX_RAW_TOTAL - total) + 1)
            except (zipfile.BadZipFile, ValueError, NotImplementedError, EOFError, OSError, RuntimeError):
                ex.meta[INCOMPLETE] = True
                continue
            total += len(data)
            if len(data) > limit and limit == MAX_PART_BYTES:
                ex.meta[INCOMPLETE] = True
                data = data[:limit]
            if _IMAGE_RE.fullmatch(name):
                scan.extend(binary_strings(data))
                continue
            if data.startswith(_PACKAGE_MAGIC):
                if depth >= MAX_ARCHIVE_DEPTH:
                    ex.meta[INCOMPLETE] = True
                    continue
                if tmp is None:
                    tmp = make_temp_dir("awb-office-")
                ex.children.append(_child_of(data, name, index, depth, tmp, "zip"))
                as_child.add(name)
                continue
            head = data[:512].lstrip()
            if head.startswith((b"<", b"\xef\xbb\xbf<", b"\xff\xfe<", b"\xfe\xff\x00<")):
                strings = _xml_strings(data)
                if strings is not None:
                    scan.append(" ".join(strings))
                    continue
                text, _ = decode_bytes(data)
                scan.append(strip_html(text) if "<" in text else text)
                continue
            text, _ = decode_bytes(data)
            printable = sum(1 for c in text[:4096] if c.isprintable() or c in "\n\r\t")
            if text and printable >= 0.95 * min(len(text), 4096):
                scan.append(strip_html(text) if "<html" in text[:4096].lower() else text)
            else:
                scan.extend(binary_strings(data))
    finally:
        if tmp is not None:
            shutil.rmtree(tmp, ignore_errors=True)
    return scan, as_child


# --------------------------------------------------------------------------- entry point

_READERS = {"docx": _docx, "xlsx": _xlsx, "pptx": _pptx, "odt": _odf, "ods": _odf, "odp": _odf}


def _join_lines(lines: list[str]) -> str:
    out: list[str] = []
    for line in lines:
        line = line.rstrip()
        if line:
            out.append(line)
        elif out and out[-1] != "":
            out.append("")
    return "\n".join(out).strip()


def extract_office(path: Path, kind: str, depth: int = 0) -> Extraction:
    """Read a docx, xlsx, pptx, odt, ods or odp file straight from its zip container. `depth` is the nesting
    level of `path`, as for archives: embedded packages are read as children up to MAX_ARCHIVE_DEPTH."""
    path = Path(path)
    if kind not in _READERS:
        return Extraction(path, kind, "unsupported", notes=["not an office kind"])
    ex = Extraction(path, kind, "ok")
    out: list[str] = []
    det: list[str] = []
    try:
        with zipfile.ZipFile(path) as zf:
            parts = _Parts(zf, ex)
            _READERS[kind](parts, ex, out, det)
            if ex.state == "failed":
                return ex
            if kind in ("docx", "xlsx", "pptx"):
                _ooxml_props(parts, ex, det)
            chunks = _alt_chunks(parts, ex, depth)
            scan, as_child = _raw_parts(zf, ex, depth, chunks)
            ex.scan_text = "\n".join(t for t in scan if t and t.strip())
            _container_notes(parts, ex, det, as_child, chunks)
            parts.finish_notes()
    except zipfile.BadZipFile:
        return Extraction(path, kind, "failed", notes=["container is not a readable zip"])
    ex.text = _join_lines(out)
    extra = [line for line in det if line and line.strip()]
    ex.detect_text = "\n".join([ex.text] + extra + _meta_lines(ex.meta)).strip()
    if ex.state == "ok" and not ex.text and not extra:
        ex.state = "unreadable"
        ex.notes.append("no extractable text")
    return ex
