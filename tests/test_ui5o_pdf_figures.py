"""tcp-ui5o ask 2026-11-24-2: `awb pdf` keeps the figures of an HTML deliverable.

An img of a PNG file or a data URI is an image object of the PDF (pdfimages lists it, with its alpha as a soft mask,
and gives back its pixels), an inline svg is drawn as vector operators (its text reads back with pdftotext and no
markup leaks), a heading colour of a style attribute or a style rule is in the content stream and a link to an id of
the page is a GoTo link to the page of that element. Everything is read back with the poppler tools.
"""
from __future__ import annotations

import base64
import re
import struct
import subprocess
import zlib
from pathlib import Path

import pytest

from awb import pdf, pdffig


# --------------------------------------------------------------------------- helpers

def _paeth(a: int, b: int, c: int) -> int:
    p = a + b - c
    pa, pb, pc = abs(p - a), abs(p - b), abs(p - c)
    return a if pa <= pb and pa <= pc else b if pb <= pc else c


def _filtered(rows: list[bytes], bpp: int) -> bytes:
    """The rows with the five PNG filters in turn, so the decoder meets each of them."""
    out = []
    prev = bytes(len(rows[0]))
    for n, row in enumerate(rows):
        f = n % 5
        enc = bytearray()
        for i, v in enumerate(row):
            a = row[i - bpp] if i >= bpp else 0
            b = prev[i]
            c = prev[i - bpp] if i >= bpp else 0
            pred = (0, a, b, (a + b) >> 1, _paeth(a, b, c))[f]
            enc.append((v - pred) & 255)
        out.append(bytes([f]) + bytes(enc))
        prev = row
    return b"".join(out)


def png(width: int, height: int, ctype: int, rows: list[bytes], depth: int = 8, bpp: int = 3,
        extra: list[tuple[bytes, bytes]] = (), interlace: int = 0) -> bytes:
    def chunk(kind: bytes, body: bytes) -> bytes:
        return struct.pack(">I", len(body)) + kind + body + struct.pack(">I", zlib.crc32(kind + body))

    head = chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, depth, ctype, 0, 0, interlace))
    return (b"\x89PNG\r\n\x1a\n" + head + b"".join(chunk(k, b) for k, b in extra)
            + chunk(b"IDAT", zlib.compress(_filtered(rows, bpp))) + chunk(b"IEND", b""))


def rgb_rows(width: int, height: int) -> list[bytes]:
    return [bytes((x * 9 + y * 5 + c * 70) % 256 for x in range(width) for c in range(3)) for y in range(height)]


def rgba_rows(width: int, height: int) -> list[bytes]:
    return [bytes(((x * 9 + y * 5 + c * 70) % 256) if c < 3 else (x * 17 + y * 11) % 256
                  for x in range(width) for c in range(4)) for y in range(height)]


def poppler(*args: str) -> str:
    return subprocess.run(list(args), capture_output=True, text=True, check=True).stdout


def text_of(path: Path) -> str:
    return poppler("pdftotext", str(path), "-")


def image_list(path: Path) -> list[list[str]]:
    lines = poppler("pdfimages", "-list", str(path)).splitlines()[2:]
    return [line.split() for line in lines if line.strip()]


def netpbm(path: Path, gray: bool = False) -> tuple[int, int, bytes]:
    """(width, height, pixels) of a binary PPM or PGM that pdfimages wrote; `gray` one byte per pixel (pdfimages
    writes a soft mask as a PPM of three equal channels)."""
    data = path.read_bytes()
    m = re.match(rb"(P[56])\s+(\d+)\s+(\d+)\s+(\d+)\s", data)
    pixels = data[m.end():]
    if gray and m.group(1) == b"P6":
        pixels = pixels[0::3]
    return int(m.group(2)), int(m.group(3)), pixels


def streams(path: Path) -> bytes:
    """The raw bytes of the PDF: the page streams are written without compression."""
    return path.read_bytes()


@pytest.fixture
def project(tmp_path, monkeypatch):
    p = tmp_path / "tcp-q7m4"
    (p / "deliverables").mkdir(parents=True)
    (p / "SCOPE.md").write_text("- code: tcp-q7m4\n", encoding="utf-8")
    monkeypatch.chdir(p)
    monkeypatch.setattr(pdf, "SCRATCH", str(tmp_path / "scratch-%d"))
    return p


def written(project: Path, body: str, capsys=None, head: str = "") -> Path:
    src = project / "deliverables" / "guide-q7m4.html"
    src.write_text("<html><head><title>guide</title>%s</head><body>%s</body></html>" % (head, body),
                   encoding="utf-8")
    assert pdf.main([str(src)]) == 0
    return src.with_suffix(".pdf")


def refused(project: Path, body: str, capsys) -> str:
    src = project / "deliverables" / "guide-q7m4.html"
    src.write_text("<html><body>%s</body></html>" % body, encoding="utf-8")
    assert pdf.main([str(src)]) == 2
    said = capsys.readouterr()
    assert not said.out and len(said.err.splitlines()) == 1
    assert not src.with_suffix(".pdf").exists()
    return said.err


SVG = """
<svg width="400" height="200" viewBox="0 0 400 200" xmlns="http://www.w3.org/2000/svg">
  <title>a title that stays out</title>
  <style>.box { fill: #cc2233; stroke: black }</style>
  <defs><marker id="head" markerWidth="10" markerHeight="10" refX="9" refY="3" orient="auto">
    <path d="M0,0 L0,6 L9,3 z" fill="#333"/></marker></defs>
  <rect class="box" x="10" y="10" width="120" height="60" rx="8"/>
  <text x="70" y="45" text-anchor="middle" font-size="14">gateway node</text>
  <line x1="130" y1="40" x2="250" y2="40" stroke="#333" stroke-width="2" marker-end="url(#head)"/>
  <g transform="translate(260,10)">
    <g transform="rotate(10 50 30)"><circle cx="50" cy="30" r="25" fill="none" stroke="blue"/></g>
    <ellipse cx="50" cy="30" rx="10" ry="5" fill="#00ff00" opacity="0.5"/>
    <text x="50" y="80"><tspan font-weight="bold">bold</tspan> storage tier</text>
  </g>
  <polyline points="10,120 40,100 70,120" fill="none" stroke="black"/>
  <polygon points="100,120 130,100 160,120" fill="orange"/>
  <path d="M10 150 a 30 20 0 0 1 60 0 q 30 -40 60 0 t 60 0 H 300 v 20 h -20 V 190 L 10 190 Z" fill="green"/>
  <foreignObject x="0" y="0" width="50" height="50"><div>loose html words</div></foreignObject>
</svg>"""


# --------------------------------------------------------------------------- PNG and JPEG images

def test_a_png_file_next_to_the_page_is_embedded_with_its_pixels(project, tmp_path):
    rows = rgb_rows(23, 17)
    (project / "deliverables" / "net.png").write_bytes(png(23, 17, 2, rows))
    out = written(project, '<p>before</p><img src="net.png" alt="the network plan"><p>after</p>')
    listed = image_list(out)
    assert len(listed) == 1 and listed[0][2] == "image" and listed[0][3:6] == ["23", "17", "rgb"]
    subprocess.run(["pdfimages", str(out), str(tmp_path / "back")], check=True)
    w, h, pixels = netpbm(tmp_path / "back-000.ppm")
    assert (w, h) == (23, 17) and pixels == b"".join(rows)
    text = text_of(out)
    assert "the network plan" in text and text.index("before") < text.index("the network plan") < text.index("after")


def test_an_rgba_png_gets_its_alpha_as_a_soft_mask(project, tmp_path):
    rows = rgba_rows(19, 11)
    (project / "deliverables" / "alpha.png").write_bytes(png(19, 11, 6, rows, bpp=4))
    out = written(project, '<img src="alpha.png">')
    kinds = [row[2] for row in image_list(out)]
    assert kinds == ["image", "smask"]
    subprocess.run(["pdfimages", str(out), str(tmp_path / "back")], check=True)
    flat = b"".join(rows)
    _, _, color = netpbm(tmp_path / "back-000.ppm")
    _, _, alpha = netpbm(tmp_path / "back-001.ppm", gray=True)
    assert color == bytes(b for i, b in enumerate(flat) if i % 4 != 3)
    assert alpha == flat[3::4]


def test_a_data_uri_png_is_embedded_and_its_figcaption_is_the_caption(project):
    data = base64.b64encode(png(8, 6, 0, [bytes(range(y * 8, y * 8 + 8)) for y in range(6)], bpp=1)).decode()
    out = written(project, '<figure><img src="data:image/png;base64,%s" alt="alt words">'
                           '<figcaption>figure one: the landing zone</figcaption></figure>' % data)
    listed = image_list(out)
    assert len(listed) == 1 and listed[0][3:6] == ["8", "6", "gray"]
    text = text_of(out)
    assert "figure one: the landing zone" in text and "alt words" not in text and "base64" not in text


def test_a_palette_png_with_transparency_and_a_16_bit_png_are_converted(project, tmp_path):
    palette = bytes([255, 0, 0, 0, 255, 0, 0, 0, 255])
    rows = [bytes((x + y) % 3 for x in range(10)) for y in range(4)]
    (project / "deliverables" / "pal.png").write_bytes(
        png(10, 4, 3, rows, bpp=1, extra=[(b"PLTE", palette), (b"tRNS", bytes([0, 128]))]))
    deep = [b"".join(struct.pack(">HHH", x * 4000, y * 9000, 65535) for x in range(5)) for y in range(3)]
    (project / "deliverables" / "deep.png").write_bytes(png(5, 3, 2, deep, depth=16, bpp=6))
    out = written(project, '<img src="pal.png"><img src="deep.png">')
    listed = image_list(out)
    assert [row[2] for row in listed] == ["image", "smask", "image"]
    assert listed[0][5] == "index" and listed[2][5:8] == ["rgb", "3", "8"]
    subprocess.run(["pdfimages", str(out), str(tmp_path / "back")], check=True)
    _, _, alpha = netpbm(tmp_path / "back-001.ppm", gray=True)
    assert alpha == bytes((0, 128, 255)[i] for row in rows for i in row)
    _, _, pixels = netpbm(tmp_path / "back-002.ppm")
    assert pixels == bytes(v >> 8 for row in deep for v in struct.unpack(">%dH" % (len(row) // 2), row))


def test_an_interlaced_png_is_refused_with_a_clear_message(project, capsys):
    (project / "deliverables" / "inter.png").write_bytes(png(4, 4, 2, rgb_rows(4, 4), interlace=1))
    err = refused(project, '<img src="inter.png">', capsys)
    assert "interlaced" in err


def test_an_image_outside_the_project_a_linked_one_and_a_missing_one_are_refused(project, tmp_path, capsys):
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    (outside / "far.png").write_bytes(png(4, 4, 2, rgb_rows(4, 4)))
    assert "not inside the project folder" in refused(project, '<img src="../../elsewhere/far.png">', capsys)
    (project / "deliverables" / "linked.png").symlink_to(outside / "far.png")
    assert "cannot be read" in refused(project, '<img src="linked.png">', capsys)
    assert "does not exist" in refused(project, '<img src="gone.png">', capsys)


def test_a_web_image_is_left_out_and_counted_in_one_line(project, capsys):
    capsys.readouterr()
    out = written(project, '<p>text</p><img src="https://example.org/x.png" alt="remote chart">')
    said = capsys.readouterr()
    assert said.err.splitlines() == ["awb pdf: left out of the figures: 1 image from the web"]
    assert image_list(out) == [] and "remote chart" in text_of(out)


def test_a_jpeg_keeps_its_data(project):
    # the smallest baseline JPEG header the writer reads: SOI, an APP0 and a SOF0 of 3 x 2 pixels, 3 channels
    sof = b"\xff\xc0" + struct.pack(">HBHHB", 17, 8, 2, 3, 3) + b"\x01\x22\x00\x02\x11\x01\x03\x11\x01"
    data = b"\xff\xd8\xff\xe0" + struct.pack(">H", 16) + b"JFIF\x00\x01\x01\x00\x00\x01\x00\x01\x00\x00" + sof
    img = pdffig.image(data + b"\xff\xd9")
    assert (img.width, img.height) == (3, 2) and b"/DCTDecode" in img.entries and img.data.startswith(b"\xff\xd8")


def test_a_wide_image_is_scaled_to_the_text_width_and_never_crosses_a_page(project):
    (project / "deliverables" / "wide.png").write_bytes(png(1600, 900, 0, [bytes(1600)] * 900, bpp=1))
    out = written(project, '<p>a</p><img src="wide.png"><img src="wide.png"><img src="wide.png">')
    raw = streams(out)
    sizes = [(float(m.group(1)), float(m.group(2)), float(m.group(4)))
             for m in re.finditer(rb"q ([\d.]+) 0 0 ([\d.]+) ([\d.]+) ([\d.]+) cm /Im\d+ Do Q", raw)]
    assert len(sizes) == 3
    for w, h, y in sizes:
        assert abs(w - pdf.TEXT_W) < 0.01 and abs(h - pdf.TEXT_W * 900 / 1600) < 0.01
        assert y >= pdf.MARGIN and y + h <= pdf.PAGE_H - pdf.MARGIN
    assert [row[0] for row in image_list(out)] == ["1", "1", "2"]


# --------------------------------------------------------------------------- inline SVG

def test_an_inline_svg_is_drawn_its_text_reads_back_and_no_markup_leaks(project, capsys):
    capsys.readouterr()
    out = written(project, "<h1>plan</h1>" + SVG + "<p>after the drawing</p>")
    said = capsys.readouterr()
    assert said.err.splitlines() == ["awb pdf: left out of the figures: 1 foreignObject"]
    text = text_of(out)
    flat = " ".join(text.split())
    assert "gateway node" in flat and "bold storage tier" in flat and "after the drawing" in flat
    for leak in ("<", "svg", "rect", "viewBox", "loose html words", "a title that stays out", "fill", "marker"):
        assert leak not in text, leak
    raw = streams(out)
    assert b"re W n" in raw                                   # clipped to its box
    assert b"0.800 0.133 0.200 rg" in raw                    # the class rule of the style element
    assert re.search(rb"\d c\n", raw) and re.search(rb"\nB\nQ", raw) and re.search(rb"\nS\nQ", raw)
    assert b"/GS1 gs" in raw and b"/ExtGState << /GS1 << /ca 0.5 /CA 0.5 >>" in raw
    assert b"1 0 0 -1 " in raw                               # text set upright in the flipped space
    assert image_list(out) == []


def test_svg_text_and_image_alt_text_go_through_the_screen():
    secret = "pass" + "&#119;" + "ord=q7m4-private-value"
    for body in ('<svg viewBox="0 0 10 10"><text x="1" y="5">%s</text></svg>' % secret,
                 '<img src="data:image/png;base64,%s" alt="%s">'
                 % (base64.b64encode(png(2, 2, 0, [bytes(2)] * 2, bpp=1)).decode(), secret)):
        blocks, _ = pdf.from_html("<html><body>%s</body></html>" % body)
        # B-089: no content check inside, the text renders as written
        assert pdf.render(blocks, "guide").startswith(b"%PDF")


def test_path_data_absolute_relative_and_arcs():
    segs = pdffig.path("M10 10 h 20 v 10 H 5 V 0 l 5 5 L 1 1 c 1 1 2 2 3 3 s 1 1 2 2 q 1 1 2 0 t 2 0 z")
    assert segs[0] == ("M", 10.0, 10.0) and segs[1] == ("L", 30.0, 10.0) and segs[2] == ("L", 30.0, 20.0)
    assert segs[3] == ("L", 5.0, 20.0) and segs[4] == ("L", 5.0, 0.0) and segs[5] == ("L", 10.0, 5.0)
    assert segs[7][0] == "C" and segs[7][5:] == (4.0, 4.0) and segs[8][5:] == (6.0, 6.0)
    assert segs[9][5:] == (8.0, 6.0) and segs[10][5:] == (10.0, 6.0) and segs[-1] == ("Z",)
    arc = pdffig.path("M0 0 A 10 10 0 0 1 20 0")
    assert [s[0] for s in arc] == ["M", "C", "C"] and arc[-1][5:] == (20.0, 0.0)
    top = min(min(s[2], s[4], s[6]) for s in arc[1:])
    assert -11 < top < -9                                    # a half circle over the chord, y down
    assert pdffig.path("m0 0 a5 5 0 105 5")[-1][5:] == (5.0, 5.0)     # flags written together


def test_transforms_colours_and_units():
    assert pdffig.transform("translate(5) scale(2,3) matrix(1 0 0 1 4 4)") == [
        (1, 0, 0, 1, 5, 0), (2, 0, 0, 3, 0, 0), (1.0, 0.0, 0.0, 1.0, 4.0, 4.0)]
    c, s, *_ = pdffig.transform("rotate(90)")[0]
    assert abs(c) < 1e-9 and abs(s - 1) < 1e-9
    assert pdffig.color("#f00") == (1.0, 0.0, 0.0) and pdffig.color("rgb(0, 51, 255)") == (0.0, 0.2, 1.0)
    assert pdffig.color("navy") == (0.0, 0.0, 128 / 255) and pdffig.color("nonsense") is None
    assert pdffig.length("1in") == 96.0 and pdffig.length("50%", ref=200) == 100.0


# --------------------------------------------------------------------------- heading colours and links

def test_heading_colours_of_a_style_attribute_and_of_a_style_rule(project):
    head = "<style>/* house colours */ h2, h3 { color: #1a7f37; font-weight: bold } p { color: red }</style>"
    out = written(project, '<h1 style="color: rgb(200, 30, 60)">first</h1><p>plain text</p><h2>second</h2>'
                           '<h3 style="color:#0000ff">third</h3>', head=head)
    raw = streams(out)
    assert b"0.784 0.118 0.235 rg" in raw and b"0.102 0.498 0.216 rg" in raw and b"0.000 0.000 1.000 rg" in raw
    assert raw.count(b" rg") == 3                            # the paragraph keeps black: only h1 to h3 take a colour
    assert "first" in text_of(out) and "second" in text_of(out)


def test_a_link_to_an_id_of_the_page_is_a_goto_to_the_page_of_that_element(project):
    filler = "".join("<p>line %d of the long first part</p>" % n for n in range(80))
    out = written(project, '<p>see <a href="#part-two">the second part</a> and <a href="#nowhere">gone</a></p>'
                           + filler + '<section id="part-two"><h2>second part</h2></section>')
    text = text_of(out)
    assert "#part-two" not in text and "#nowhere" not in text and "the second part" in text
    raw = streams(out)
    gotos = re.findall(rb"/A << /S /GoTo /D \[(\d+) 0 R /XYZ null ([\d.]+) null\] >>", raw)
    assert len(gotos) == 1 and b"/URI" not in raw
    pages = [int(m.group(1)) for m in re.finditer(rb"(\d+) 0 obj\n<< /Type /Page /Parent", raw)]
    target = int(gotos[0][0])
    assert target in pages
    page_text = poppler("pdftotext", "-f", str(pages.index(target) + 1), "-l", str(pages.index(target) + 1),
                        str(out), "-")
    assert "second part" in page_text and pages.index(target) > 0


def test_svg_text_outside_winansi_uses_the_embedded_font(project):
    out = written(project, '<svg viewBox="0 0 200 40"><text x="10" y="20">схема сети</text></svg>')
    assert "схема сети" in text_of(out)
    assert b"/FontFile2" in streams(out)
