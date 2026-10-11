"""A small Office Open XML workbook writer and reader with the standard library only (zipfile and xml).

The runtime of the Workbench does not use openpyxl (its tests do, to read what this module wrote). The writer makes
what a price workbook needs and nothing more: sheets of text, numbers and formulas with their stored results,
a few fixed cell styles, column widths and a frozen header. A formula cell carries the formula and the result the
caller computed (exact decimals), so a viewer that does not calculate still shows the right figure and Excel
calculates it again on opening (`fullCalcOnLoad`). A text is always written as a text, never as a formula, so a
cell that starts with "=" stays text.

    book = [Sheet("Lines")]
    book[0].put(1, 1, "Title", style="title")
    book[0].put(2, 3, Decimal("12.5"), formula="A2*2", style="money")
    write(path, book, title="...", created="2026-10-11T12:00:00Z")
    sheets = read(path)           # [SheetData(name, rows={row: {col: value}})], values str, Decimal or None

The output is deterministic: the same book gives the same bytes (fixed time stamps in the archive).
"""
from __future__ import annotations

import io
import os
import re
import tempfile
import zipfile
from dataclasses import dataclass, field
from decimal import Decimal, DecimalException
from pathlib import Path
from xml.etree import ElementTree as ET
from xml.sax.saxutils import escape

NS = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
REL_NS = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
PKG_REL_NS = "http://schemas.openxmlformats.org/package/2006/relationships"
ZIP_TIME = (1980, 1, 1, 0, 0, 0)
MAX_MEMBER = 40 * 1024 * 1024          # bytes of one part, uncompressed
MAX_MEMBERS = 200
MAX_CELLS = 400_000
SHEET_NAME_RE = re.compile(r"[^\[\]:*?/\\]{1,31}")
_CONTROL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\ufffe\uffff]")

# cell styles by name; the index is the position in cellXfs of styles.xml below
STYLES = {"default": 0, "title": 1, "header": 2, "money": 3, "price": 4, "label": 5, "total": 6, "wrap": 7,
          "note": 8, "number": 9, "money_good": 10, "money_bad": 11}


class XlsxError(Exception):
    """A workbook that cannot be written or read. The message says why, never what a cell holds."""


@dataclass
class Cell:
    value: object = None                 # str, Decimal, int or None
    formula: str | None = None           # without the leading "="
    style: str = "default"


@dataclass
class Sheet:
    name: str
    cells: dict = field(default_factory=dict)          # (row, col) -> Cell, both from 1
    widths: dict = field(default_factory=dict)         # col -> width in characters
    freeze_rows: int = 0                               # rows kept on top while scrolling

    def put(self, row: int, col: int, value=None, *, formula: str | None = None, style: str = "default") -> Cell:
        if row < 1 or col < 1 or row > 1_048_576 or col > 16_384:
            raise XlsxError("a cell is outside the sheet")
        if style not in STYLES:
            raise XlsxError("a cell style is unknown")
        cell = Cell(value, formula, style)
        self.cells[(row, col)] = cell
        return cell


def column_letter(col: int) -> str:
    out = ""
    while col:
        col, rem = divmod(col - 1, 26)
        out = chr(65 + rem) + out
    return out


def column_number(letters: str) -> int:
    n = 0
    for ch in letters:
        n = n * 26 + ord(ch) - 64
    return n


def ref(row: int, col: int, absolute: bool = False) -> str:
    return ("$%s$%d" if absolute else "%s%d") % (column_letter(col), row)


def decimal_text(value: Decimal | int) -> str:
    """Plain digits, no exponent: what a number cell stores."""
    d = Decimal(value)
    if not d.is_finite():
        raise XlsxError("a number cell holds a number")
    out = format(d, "f")
    if "." in out:
        out = out.rstrip("0").rstrip(".")
    return "0" if out in ("-0", "") else out


def _text(s: str) -> str:
    return escape(_CONTROL_RE.sub(" ", s))


# --------------------------------------------------------------------------- writer

_CONTENT_TYPES = (
    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
    '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
    '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
    '<Default Extension="xml" ContentType="application/xml"/>'
    '<Override PartName="/xl/workbook.xml" '
    'ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>'
    '%s'
    '<Override PartName="/xl/styles.xml" '
    'ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.styles+xml"/>'
    '<Override PartName="/xl/sharedStrings.xml" '
    'ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sharedStrings+xml"/>'
    '<Override PartName="/docProps/core.xml" '
    'ContentType="application/vnd.openxmlformats-package.core-properties+xml"/>'
    '<Override PartName="/docProps/app.xml" '
    'ContentType="application/vnd.openxmlformats-officedocument.extended-properties+xml"/>'
    '</Types>')

_ROOT_RELS = (
    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
    '<Relationships xmlns="%s">'
    '<Relationship Id="rId1" Type="%s/officeDocument" Target="xl/workbook.xml"/>'
    '<Relationship Id="rId2" Type="http://schemas.openxmlformats.org/package/2006/relationships/metadata/'
    'core-properties" Target="docProps/core.xml"/>'
    '<Relationship Id="rId3" Type="%s/extended-properties" Target="docProps/app.xml"/>'
    '</Relationships>' % (PKG_REL_NS, REL_NS, REL_NS))

# fonts: 0 normal, 1 title, 2 bold, 3 note, 4 green, 5 red; fills: 2 header; borders: 1 below, 2 above
_STYLES = (
    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
    '<styleSheet xmlns="%s">'
    '<numFmts count="2"><numFmt numFmtId="164" formatCode="#,##0.00####"/>'
    '<numFmt numFmtId="165" formatCode="0.######"/></numFmts>'
    '<fonts count="6">'
    '<font><sz val="11"/><name val="Calibri"/><family val="2"/></font>'
    '<font><b/><sz val="14"/><name val="Calibri"/><family val="2"/></font>'
    '<font><b/><sz val="11"/><name val="Calibri"/><family val="2"/></font>'
    '<font><i/><sz val="10"/><color rgb="FF595959"/><name val="Calibri"/><family val="2"/></font>'
    '<font><sz val="11"/><color rgb="FF1E6B2E"/><name val="Calibri"/><family val="2"/></font>'
    '<font><sz val="11"/><color rgb="FF9C1C1C"/><name val="Calibri"/><family val="2"/></font>'
    '</fonts>'
    '<fills count="3"><fill><patternFill patternType="none"/></fill><fill><patternFill patternType="gray125"/></fill>'
    '<fill><patternFill patternType="solid"><fgColor rgb="FFDDE6F0"/><bgColor indexed="64"/></patternFill></fill>'
    '</fills>'
    '<borders count="3"><border><left/><right/><top/><bottom/><diagonal/></border>'
    '<border><left/><right/><top/><bottom style="thin"><color auto="1"/></bottom><diagonal/></border>'
    '<border><left/><right/><top style="thin"><color auto="1"/></top><bottom/><diagonal/></border></borders>'
    '<cellStyleXfs count="1"><xf numFmtId="0" fontId="0" fillId="0" borderId="0"/></cellStyleXfs>'
    '<cellXfs count="12">'
    '<xf numFmtId="0" fontId="0" fillId="0" borderId="0" xfId="0"/>'
    '<xf numFmtId="0" fontId="1" fillId="0" borderId="0" xfId="0" applyFont="1"/>'
    '<xf numFmtId="0" fontId="2" fillId="2" borderId="1" xfId="0" applyFont="1" applyFill="1" applyBorder="1" '
    'applyAlignment="1"><alignment vertical="top" wrapText="1"/></xf>'
    '<xf numFmtId="4" fontId="0" fillId="0" borderId="0" xfId="0" applyNumberFormat="1"/>'
    '<xf numFmtId="164" fontId="0" fillId="0" borderId="0" xfId="0" applyNumberFormat="1"/>'
    '<xf numFmtId="0" fontId="2" fillId="0" borderId="0" xfId="0" applyFont="1"/>'
    '<xf numFmtId="4" fontId="2" fillId="0" borderId="2" xfId="0" applyNumberFormat="1" applyFont="1" '
    'applyBorder="1"/>'
    '<xf numFmtId="0" fontId="0" fillId="0" borderId="0" xfId="0" applyAlignment="1">'
    '<alignment vertical="top" wrapText="1"/></xf>'
    '<xf numFmtId="0" fontId="3" fillId="0" borderId="0" xfId="0" applyFont="1"/>'
    '<xf numFmtId="165" fontId="0" fillId="0" borderId="0" xfId="0" applyNumberFormat="1"/>'
    '<xf numFmtId="4" fontId="4" fillId="0" borderId="0" xfId="0" applyNumberFormat="1" applyFont="1"/>'
    '<xf numFmtId="4" fontId="5" fillId="0" borderId="0" xfId="0" applyNumberFormat="1" applyFont="1"/>'
    '</cellXfs>'
    '<cellStyles count="1"><cellStyle name="Normal" xfId="0" builtinId="0"/></cellStyles>'
    '</styleSheet>' % NS)


def _sheet_xml(sheet: Sheet, index: int, strings: dict[str, int]) -> str:
    rows: dict[int, list[tuple[int, Cell]]] = {}
    for (r, c), cell in sheet.cells.items():
        rows.setdefault(r, []).append((c, cell))
    max_row = max(rows, default=1)
    max_col = max((c for (_, c) in sheet.cells), default=1)
    out = ['<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n<worksheet xmlns="%s">' % NS,
           '<dimension ref="A1:%s"/>' % ref(max_row, max_col)]
    view = '<sheetView workbookViewId="0"%s>' % (' tabSelected="1"' if index == 0 else "")
    if sheet.freeze_rows:
        view += ('<pane ySplit="%d" topLeftCell="A%d" activePane="bottomLeft" state="frozen"/>'
                 '<selection pane="bottomLeft" activeCell="A%d" sqref="A%d"/>'
                 % ((sheet.freeze_rows, sheet.freeze_rows + 1) + (sheet.freeze_rows + 1,) * 2))
    out.append("<sheetViews>%s</sheetView></sheetViews>" % view)
    out.append('<sheetFormatPr defaultRowHeight="15"/>')
    if sheet.widths:
        out.append("<cols>%s</cols>" % "".join(
            '<col min="%d" max="%d" width="%s" customWidth="1"/>' % (c, c, w) for c, w in sorted(sheet.widths.items())))
    out.append("<sheetData>")
    for r in sorted(rows):
        out.append('<row r="%d">' % r)
        for c, cell in sorted(rows[r], key=lambda t: t[0]):
            a = ref(r, c)
            s = ' s="%d"' % STYLES[cell.style] if cell.style != "default" else ""
            v = cell.value
            if cell.formula is not None:
                if isinstance(v, str):
                    out.append('<c r="%s"%s t="str"><f>%s</f><v>%s</v></c>' % (a, s, _text(cell.formula), _text(v)))
                elif v is None:
                    out.append('<c r="%s"%s><f>%s</f></c>' % (a, s, _text(cell.formula)))
                else:
                    out.append('<c r="%s"%s><f>%s</f><v>%s</v></c>' % (a, s, _text(cell.formula), decimal_text(v)))
            elif v is None:
                out.append('<c r="%s"%s/>' % (a, s))
            elif isinstance(v, str):
                out.append('<c r="%s"%s t="s"><v>%d</v></c>' % (a, s, strings.setdefault(v, len(strings))))
            else:
                out.append('<c r="%s"%s><v>%s</v></c>' % (a, s, decimal_text(v)))
        out.append("</row>")
    out.append("</sheetData>")
    out.append('<pageMargins left="0.7" right="0.7" top="0.75" bottom="0.75" header="0.3" footer="0.3"/>')
    out.append("</worksheet>")
    return "".join(out)


def build(sheets: list[Sheet], *, title: str = "", created: str = "2026-01-01T00:00:00Z") -> bytes:
    """The bytes of the workbook."""
    if not sheets:
        raise XlsxError("a workbook needs a sheet")
    names = [s.name for s in sheets]
    for n in names:
        if not SHEET_NAME_RE.fullmatch(n) or n.startswith("'") or n.endswith("'"):
            raise XlsxError("a sheet name is not allowed")
    if len({n.lower() for n in names}) != len(names):
        raise XlsxError("two sheets have the same name")
    if not re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ", created):
        raise XlsxError("the creation time reads YYYY-MM-DDTHH:MM:SSZ")
    strings: dict[str, int] = {}
    parts: list[tuple[str, str]] = []
    for i, sheet in enumerate(sheets, start=1):
        parts.append(("xl/worksheets/sheet%d.xml" % i, _sheet_xml(sheet, i - 1, strings)))
    sst = ['<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n<sst xmlns="%s" count="%d" uniqueCount="%d">'
           % (NS, len(strings), len(strings))]
    for text in strings:        # insertion order is the index
        sst.append('<si><t xml:space="preserve">%s</t></si>' % _text(text))
    sst.append("</sst>")
    overrides = "".join(
        '<Override PartName="/xl/worksheets/sheet%d.xml" '
        'ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>' % i
        for i in range(1, len(sheets) + 1))
    workbook = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
                '<workbook xmlns="%s" xmlns:r="%s"><bookViews><workbookView activeTab="0"/></bookViews><sheets>%s'
                '</sheets><calcPr calcId="191029" fullCalcOnLoad="1"/></workbook>'
                % (NS, REL_NS, "".join('<sheet name="%s" sheetId="%d" r:id="rId%d"/>' % (escape(n, {'"': "&quot;"}),
                                                                                         i, i)
                                       for i, n in enumerate(names, start=1))))
    wb_rels = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n<Relationships xmlns="%s">%s'
               '<Relationship Id="rId%d" Type="%s/styles" Target="styles.xml"/>'
               '<Relationship Id="rId%d" Type="%s/sharedStrings" Target="sharedStrings.xml"/></Relationships>'
               % (PKG_REL_NS,
                  "".join('<Relationship Id="rId%d" Type="%s/worksheet" Target="worksheets/sheet%d.xml"/>'
                          % (i, REL_NS, i) for i in range(1, len(sheets) + 1)),
                  len(sheets) + 1, REL_NS, len(sheets) + 2, REL_NS))
    core = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
            '<cp:coreProperties xmlns:cp="http://schemas.openxmlformats.org/package/2006/metadata/core-properties" '
            'xmlns:dc="http://purl.org/dc/elements/1.1/" xmlns:dcterms="http://purl.org/dc/terms/" '
            'xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance"><dc:title>%s</dc:title>'
            '<dc:creator>awb pricebook</dc:creator>'
            '<dcterms:created xsi:type="dcterms:W3CDTF">%s</dcterms:created>'
            '<dcterms:modified xsi:type="dcterms:W3CDTF">%s</dcterms:modified></cp:coreProperties>'
            % (_text(title), created, created))
    app = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
           '<Properties xmlns="http://schemas.openxmlformats.org/officeDocument/2006/extended-properties">'
           '<Application>awb pricebook</Application></Properties>')
    members = ([("[Content_Types].xml", _CONTENT_TYPES % overrides), ("_rels/.rels", _ROOT_RELS),
                ("docProps/core.xml", core), ("docProps/app.xml", app), ("xl/workbook.xml", workbook),
                ("xl/_rels/workbook.xml.rels", wb_rels), ("xl/styles.xml", _STYLES),
                ("xl/sharedStrings.xml", "".join(sst))] + parts)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, text in members:
            info = zipfile.ZipInfo(name, ZIP_TIME)
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o644 << 16
            zf.writestr(info, text.encode("utf-8"))
    return buf.getvalue()


def write_bytes(path: Path, data: bytes) -> None:
    """Replace `path` with `data` in one step: a reader never sees half a file."""
    path = Path(path)
    fd, tmp = tempfile.mkstemp(prefix="." + path.name + ".", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
        os.chmod(tmp, 0o644)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def write(path: Path, sheets: list[Sheet], *, title: str = "", created: str = "2026-01-01T00:00:00Z") -> None:
    write_bytes(path, build(sheets, title=title, created=created))


# --------------------------------------------------------------------------- reader


@dataclass
class SheetData:
    name: str
    rows: dict = field(default_factory=dict)     # row -> {col: str | Decimal | None}


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _member(zf: zipfile.ZipFile, name: str) -> bytes:
    try:
        info = zf.getinfo(name)
    except KeyError:
        raise XlsxError("the workbook has no part %s" % name) from None
    if info.file_size > MAX_MEMBER:
        raise XlsxError("a part of the workbook is too large")
    data = zf.read(name)
    if b"<!DOCTYPE" in data[:4096].upper() or b"<!ENTITY" in data[:65536].upper():
        raise XlsxError("a part of the workbook carries a document type declaration")
    return data


def _xml(zf: zipfile.ZipFile, name: str) -> ET.Element:
    try:
        return ET.fromstring(_member(zf, name))
    except ET.ParseError:
        raise XlsxError("a part of the workbook is not valid XML") from None


def _all_text(el: ET.Element) -> str:
    """The text of a string item: every <t> below it, but not the phonetic runs (<rPh>)."""
    out = []

    def walk(e):
        for child in e:
            name = _local(child.tag)
            if name == "t":
                out.append(child.text or "")
            elif name in ("r", "is", "si"):
                walk(child)

    walk(el)
    return "".join(out)


def _resolve(base: str, target: str) -> str:
    if target.startswith("/"):
        return target.lstrip("/")
    parts = base.split("/")[:-1]
    for seg in target.split("/"):
        if seg == "..":
            if parts:
                parts.pop()
        elif seg and seg != ".":
            parts.append(seg)
    return "/".join(parts)


def read(path: Path) -> list[SheetData]:
    """Every sheet with the stored value of each cell (the result of a formula, not the formula)."""
    try:
        zf = zipfile.ZipFile(path)
    except (OSError, zipfile.BadZipFile):
        raise XlsxError("the file is not a workbook") from None
    with zf:
        if len(zf.namelist()) > MAX_MEMBERS:
            raise XlsxError("the workbook has too many parts")
        book = _xml(zf, "xl/workbook.xml")
        rels = {rel.get("Id"): _resolve("xl/workbook.xml", rel.get("Target", ""))
                for rel in _xml(zf, "xl/_rels/workbook.xml.rels")}
        strings: list[str] = []
        if "xl/sharedStrings.xml" in zf.namelist():
            strings = [_all_text(si) for si in _xml(zf, "xl/sharedStrings.xml") if _local(si.tag) == "si"]
        out: list[SheetData] = []
        cells = 0
        for sheet in book.iter():
            if _local(sheet.tag) != "sheet":
                continue
            target = rels.get(sheet.get("{%s}id" % REL_NS))
            if not target:
                raise XlsxError("a sheet has no part")
            data = SheetData(sheet.get("name", ""))
            for row in _xml(zf, target).iter():
                if _local(row.tag) != "row":
                    continue
                for c in row:
                    if _local(c.tag) != "c":
                        continue
                    cells += 1
                    if cells > MAX_CELLS:
                        raise XlsxError("the workbook has too many cells")
                    m = re.fullmatch(r"([A-Z]{1,3})([0-9]{1,7})", c.get("r", ""))
                    if not m:
                        raise XlsxError("a cell has no address")
                    kind = c.get("t", "n")
                    v = next((e for e in c if _local(e.tag) == "v"), None)
                    value: object = None
                    if kind == "inlineStr":
                        inline = next((e for e in c if _local(e.tag) == "is"), None)
                        value = _all_text(inline) if inline is not None else None
                    elif v is not None and v.text is not None:
                        if kind == "s":
                            try:
                                value = strings[int(v.text)]
                            except (ValueError, IndexError):
                                raise XlsxError("a cell points to a missing string") from None
                        elif kind in ("str", "e"):
                            value = v.text
                        elif kind == "b":
                            value = Decimal(1 if v.text.strip() == "1" else 0)
                        else:
                            try:
                                value = Decimal(v.text.strip())
                            except DecimalException:
                                raise XlsxError("a cell holds a number that cannot be read") from None
                    if value is not None:
                        data.rows.setdefault(int(m.group(2)), {})[column_number(m.group(1))] = value
            out.append(data)
        return out
