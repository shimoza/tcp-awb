"""Embedded TrueType fonts for `awb pdf --unicode`, standard library only.

The standard PDF fonts know WinAnsi alone, so Russian (Cyrillic) or any other script outside it needs a font file in
the PDF. This module reads the TrueType fonts of the host (Liberation first, DejaVu next), keeps only the glyphs the
document uses (the glyph numbers stay as they are, the outlines of the others are dropped) and writes it as a Type0
font with Identity-H encoding, its widths and a ToUnicode map, so the text stays selectable and pdftotext gives it
back as it was written. German umlauts, Cyrillic and Latin share one font.
"""
from __future__ import annotations

import hashlib
import struct
import zlib
from pathlib import Path

FONT_DIRS = ("/usr/share/fonts/truetype/liberation", "/usr/share/fonts/truetype/liberation2",
             "/usr/share/fonts/truetype/dejavu")
# families in order of preference, each as the files of the style keys of awb.pdf (r, b, i, c): Liberation has the
# metrics of Helvetica and Courier, so a document lays out as with the standard fonts; DejaVu has no Sans Oblique
# on every host, so it may borrow the oblique of the other family
FAMILIES = (
    {"r": "LiberationSans-Regular.ttf", "b": "LiberationSans-Bold.ttf", "i": "LiberationSans-Italic.ttf",
     "c": "LiberationMono-Regular.ttf"},
    {"r": "DejaVuSans.ttf", "b": "DejaVuSans-Bold.ttf", "i": "DejaVuSans-Oblique.ttf", "c": "DejaVuSansMono.ttf"},
)
# tables a PDF reader needs of an embedded TrueType program; the rest (layout, kerning, glyph names) is left out
KEEP = (b"OS/2", b"cmap", b"cvt ", b"fpgm", b"glyf", b"head", b"hhea", b"hmtx", b"loca", b"maxp", b"prep")
MAX_FONT = 32 * 1024 * 1024


class FontError(Exception):
    """A font file that cannot be read."""


class TrueType:
    """One TrueType font: its character map, advance widths and the parts the PDF needs."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        data = self.path.read_bytes()
        if len(data) > MAX_FONT or len(data) < 12:
            raise FontError("font file size")
        self.data = data
        self.name = "".join(ch for ch in self.path.stem if ch.isalnum() or ch == "-")[:60] or "Font"
        try:
            self.tables = self._directory()
            head = self.table(b"head")
            self.upem = struct.unpack(">H", head[18:20])[0] or 1000
            self.bbox = struct.unpack(">4h", head[36:44])
            self.long_loca = struct.unpack(">h", head[50:52])[0] == 1
            hhea = self.table(b"hhea")
            self.ascent, self.descent = struct.unpack(">2h", hhea[4:8])
            n_metrics = struct.unpack(">H", hhea[34:36])[0]
            self.glyphs = struct.unpack(">H", self.table(b"maxp")[4:6])[0]
            hmtx = self.table(b"hmtx")
            self.advances = [struct.unpack(">H", hmtx[4 * i:4 * i + 2])[0] for i in range(n_metrics)]
            os2 = self.tables.get(b"OS/2")
            cap = None
            if os2 and os2[1] >= 90 and struct.unpack(">H", data[os2[0]:os2[0] + 2])[0] >= 2:
                cap = struct.unpack(">h", data[os2[0] + 88:os2[0] + 90])[0]
            self.cap = cap if cap else int(self.ascent * 0.7)
            self.cmap = self._cmap()
        except (struct.error, KeyError, IndexError) as exc:
            raise FontError("font file layout") from exc
        self.mono = self.path.stem.lower().find("mono") >= 0
        self.italic = any(w in self.path.stem.lower() for w in ("oblique", "italic"))
        self.bold = "bold" in self.path.stem.lower()

    # ------------------------------------------------------------------ reading

    def _directory(self) -> dict[bytes, tuple[int, int]]:
        count = struct.unpack(">H", self.data[4:6])[0]
        out = {}
        for i in range(count):
            tag, _, off, length = struct.unpack(">4sLLL", self.data[12 + 16 * i:28 + 16 * i])
            if off + length > len(self.data):
                raise FontError("table outside the file")
            out[tag] = (off, length)
        return out

    def table(self, tag: bytes) -> bytes:
        off, length = self.tables[tag]
        return self.data[off:off + length]

    def _cmap(self) -> dict[int, int]:
        cmap = self.table(b"cmap")
        n = struct.unpack(">H", cmap[2:4])[0]
        subs = {}
        for i in range(n):
            plat, enc, off = struct.unpack(">HHL", cmap[4 + 8 * i:12 + 8 * i])
            subs[(plat, enc)] = off
        for key in ((3, 10), (0, 4), (3, 1), (0, 3)):
            if key in subs:
                off = subs[key]
                fmt = struct.unpack(">H", cmap[off:off + 2])[0]
                if fmt == 12:
                    return self._cmap12(cmap, off)
                if fmt == 4:
                    return self._cmap4(cmap, off)
        raise FontError("no Unicode character map")

    @staticmethod
    def _cmap4(cmap: bytes, off: int) -> dict[int, int]:
        segs = struct.unpack(">H", cmap[off + 6:off + 8])[0] // 2
        ends = struct.unpack(">%dH" % segs, cmap[off + 14:off + 14 + 2 * segs])
        p = off + 16 + 2 * segs
        starts = struct.unpack(">%dH" % segs, cmap[p:p + 2 * segs])
        deltas = struct.unpack(">%dh" % segs, cmap[p + 2 * segs:p + 4 * segs])
        ro_at = p + 4 * segs
        ranges = struct.unpack(">%dH" % segs, cmap[ro_at:ro_at + 2 * segs])
        out = {}
        for i in range(segs):
            for code in range(starts[i], ends[i] + 1):
                if code == 0xFFFF:
                    continue
                if ranges[i] == 0:
                    gid = (code + deltas[i]) & 0xFFFF
                else:
                    at = ro_at + 2 * i + ranges[i] + 2 * (code - starts[i])
                    gid = struct.unpack(">H", cmap[at:at + 2])[0]
                    if gid:
                        gid = (gid + deltas[i]) & 0xFFFF
                if gid:
                    out[code] = gid
        return out

    @staticmethod
    def _cmap12(cmap: bytes, off: int) -> dict[int, int]:
        groups = struct.unpack(">L", cmap[off + 12:off + 16])[0]
        out = {}
        for i in range(groups):
            start, end, gid = struct.unpack(">LLL", cmap[off + 16 + 12 * i:off + 28 + 12 * i])
            if end - start > 0x10FFFF:
                raise FontError("character map group")
            for k in range(end - start + 1):
                out[start + k] = gid + k
        return out

    def advance(self, gid: int) -> int:
        """The advance width of a glyph in 1/1000 em."""
        a = self.advances[gid] if gid < len(self.advances) else self.advances[-1]
        return round(a * 1000 / self.upem)

    def scaled(self, v: int) -> int:
        return round(v * 1000 / self.upem)

    # ------------------------------------------------------------------ subsetting

    def _loca(self) -> list[int]:
        loca = self.table(b"loca")
        if self.long_loca:
            return list(struct.unpack(">%dL" % (self.glyphs + 1), loca[:4 * (self.glyphs + 1)]))
        return [2 * x for x in struct.unpack(">%dH" % (self.glyphs + 1), loca[:2 * (self.glyphs + 1)])]

    def subset(self, used: set[int]) -> bytes:
        """The font program with the outlines of `used` glyphs (and their components) and glyph 0; glyph numbers
        unchanged, so the PDF maps character codes to glyphs one to one."""
        loca, glyf = self._loca(), self.table(b"glyf")
        keep, todo = set(), [0] + sorted(g for g in used if 0 <= g < self.glyphs)
        while todo:
            gid = todo.pop()
            if gid in keep or not 0 <= gid < self.glyphs:
                continue
            keep.add(gid)
            g = glyf[loca[gid]:loca[gid + 1]]
            if len(g) >= 10 and struct.unpack(">h", g[:2])[0] < 0:     # a composite glyph: keep its parts
                p = 10
                while p + 4 <= len(g):
                    flags, part = struct.unpack(">HH", g[p:p + 4])
                    todo.append(part)
                    p += 4 + (4 if flags & 0x0001 else 2)
                    p += 2 if flags & 0x0008 else 4 if flags & 0x0040 else 8 if flags & 0x0080 else 0
                    if not flags & 0x0020:
                        break
        new_glyf, new_loca = bytearray(), []
        for gid in range(self.glyphs):
            new_loca.append(len(new_glyf))
            if gid in keep:
                new_glyf += glyf[loca[gid]:loca[gid + 1]]
                new_glyf += b"\0" * (-len(new_glyf) % 4)
        new_loca.append(len(new_glyf))
        head = bytearray(self.table(b"head"))
        head[8:12] = b"\0\0\0\0"                          # checksum adjustment: not checked by readers
        head[50:52] = struct.pack(">h", 1)
        tables = {tag: self.table(tag) for tag in KEEP if tag in self.tables}
        tables.update({b"glyf": bytes(new_glyf), b"loca": struct.pack(">%dL" % len(new_loca), *new_loca),
                       b"head": bytes(head)})
        return _sfnt(tables)


def _checksum(data: bytes) -> int:
    data += b"\0" * (-len(data) % 4)
    return sum(struct.unpack(">%dL" % (len(data) // 4), data)) & 0xFFFFFFFF


def _sfnt(tables: dict[bytes, bytes]) -> bytes:
    tags = sorted(tables)
    n = len(tags)
    power = 1
    while power * 2 <= n:
        power *= 2
    header = struct.pack(">LHHHH", 0x00010000, n, power * 16, power.bit_length() - 1, n * 16 - power * 16)
    offset = 12 + 16 * n
    records, body = bytearray(), bytearray()
    for tag in tags:
        data = tables[tag]
        records += struct.pack(">4sLLL", tag, _checksum(data), offset + len(body), len(data))
        body += data + b"\0" * (-len(data) % 4)
    return header + bytes(records) + bytes(body)


def find(dirs: tuple[str, ...] = FONT_DIRS) -> dict[str, Path] | None:
    """The four font files (r, b, i, c) of the host: the first family found whole, else each style from the first
    family that has it; None when a style has no file at all."""
    def where(name: str) -> Path | None:
        return next((Path(d) / name for d in dirs if (Path(d) / name).is_file()), None)

    for family in FAMILIES:
        files = {key: where(name) for key, name in family.items()}
        if all(files.values()):
            return files
    out = {}
    for key in FAMILIES[0]:
        hit = next((f for f in (where(fam[key]) for fam in FAMILIES) if f is not None), None)
        if hit is None:
            return None
        out[key] = hit
    return out


class FontSet:
    """The fonts of one document: character to glyph, widths, and the used glyphs for the PDF objects."""

    def __init__(self, files: dict[str, Path]) -> None:
        self.fonts = {key: TrueType(path) for key, path in files.items()}
        self.used: dict[str, dict[int, str]] = {key: {} for key in files}
        self.covered = set.intersection(*(set(f.cmap) for f in self.fonts.values()))

    def showable(self, ch: str) -> bool:
        return ord(ch) in self.covered

    def glyphs(self, key: str, text: str) -> list[int]:
        font = self.fonts[key]
        out = []
        for ch in text:
            gid = font.cmap.get(ord(ch)) or font.cmap.get(ord("?"), 0)
            out.append(gid)
            self.used[key].setdefault(gid, ch if gid == font.cmap.get(ord(ch)) else "?")
        return out

    def width(self, key: str, text: str) -> int:
        """Advance of the text in 1/1000 em, without recording the glyphs as used."""
        font = self.fonts[key]
        q = font.cmap.get(ord("?"), 0)
        return sum(font.advance(font.cmap.get(ord(ch)) or q) for ch in text)

    def show(self, key: str, text: str) -> bytes:
        return b"<" + "".join("%04X" % g for g in self.glyphs(key, text)).encode("ascii") + b">"

    def objects(self, add) -> dict[str, int]:
        """Write the PDF objects of every font through `add(body) -> id`; {resource name key: Type0 font id}."""
        out = {}
        for key, font in self.fonts.items():
            used = self.used[key]
            tag = "".join(chr(65 + b % 26) for b in hashlib.sha256(
                (font.name + ",".join(map(str, sorted(used)))).encode()).digest()[:6])
            base = ("%s+%s" % (tag, font.name)).encode("ascii")
            program = font.subset(set(used))
            packed = zlib.compress(program, 9)
            file_id = add(b"<< /Length %d /Length1 %d /Filter /FlateDecode >>\nstream\n" % (len(packed), len(program))
                          + packed + b"\nendstream")
            flags = 4 | (1 if font.mono else 0) | (64 if font.italic else 0)
            bbox = b" ".join(b"%d" % font.scaled(v) for v in font.bbox)
            desc = add(b"<< /Type /FontDescriptor /FontName /%s /Flags %d /FontBBox [%s] /ItalicAngle %d /Ascent %d "
                       b"/Descent %d /CapHeight %d /StemV %d /FontFile2 %d 0 R >>"
                       % (base, flags, bbox, -12 if font.italic else 0, font.scaled(font.ascent),
                          font.scaled(font.descent), font.scaled(font.cap), 140 if font.bold else 80, file_id))
            widths = b" ".join(b"%d [%d]" % (g, font.advance(g)) for g in sorted(used))
            cid = add(b"<< /Type /Font /Subtype /CIDFontType2 /BaseFont /%s /CIDSystemInfo << /Registry (Adobe) "
                      b"/Ordering (Identity) /Supplement 0 >> /FontDescriptor %d 0 R /DW %d /W [%s] "
                      b"/CIDToGIDMap /Identity >>" % (base, desc, font.advance(0), widths))
            cmap = _to_unicode(used)
            tu = add(b"<< /Length %d >>\nstream\n" % len(cmap) + cmap + b"\nendstream")
            out[key] = add(b"<< /Type /Font /Subtype /Type0 /BaseFont /%s /Encoding /Identity-H "
                           b"/DescendantFonts [%d 0 R] /ToUnicode %d 0 R >>" % (base, cid, tu))
        return out


def _to_unicode(used: dict[int, str]) -> bytes:
    lines = [b"/CIDInit /ProcSet findresource begin", b"12 dict begin", b"begincmap",
             b"/CIDSystemInfo << /Registry (Adobe) /Ordering (UCS) /Supplement 0 >> def",
             b"/CMapName /Adobe-Identity-UCS def", b"/CMapType 2 def",
             b"1 begincodespacerange", b"<0000> <FFFF>", b"endcodespacerange"]
    pairs = sorted(used.items())
    for i in range(0, len(pairs), 100):
        chunk = pairs[i:i + 100]
        lines.append(b"%d beginbfchar" % len(chunk))
        for gid, ch in chunk:
            lines.append(b"<%04X> <%s>" % (gid, ch.encode("utf-16-be").hex().upper().encode("ascii")))
        lines.append(b"endbfchar")
    lines += [b"endcmap", b"CMapName currentdict /CMap defineresource pop", b"end", b"end"]
    return b"\n".join(lines)
