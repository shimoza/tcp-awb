"""tcp-ui5o ask 2026-11-24-3: `awb docx` writes a Word file of a guide with the structure of its PDF.

The file is read back with python-docx and zipfile: headings in the styles Heading 1 to 3 with the colour of the
page and a Word numbering when the page numbers them, the contents field with a result that lists the headings, the
image parts (the PNG bytes as they were, the SVG with a PNG fallback), captions, tables with a header row, the code
style, the footer page fields, the bookmarks and the links to them.
"""
from __future__ import annotations

import base64
import re
import struct
import zipfile
import zlib
from pathlib import Path

import docx as pydocx
import pytest
from docx.shared import RGBColor

from awb import docx, pdf

W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"


def png(width: int, height: int) -> bytes:
    def chunk(kind: bytes, body: bytes) -> bytes:
        return struct.pack(">I", len(body)) + kind + body + struct.pack(">I", zlib.crc32(kind + body))

    rows = b"".join(b"\0" + bytes((x * 7 + y * 3) % 256 for x in range(width * 3)) for y in range(height))
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(rows)) + chunk(b"IEND", b""))


@pytest.fixture
def project(tmp_path, monkeypatch):
    p = tmp_path / "tcp-q7m4"
    (p / "deliverables").mkdir(parents=True)
    (p / "SCOPE.md").write_text("- code: tcp-q7m4\n", encoding="utf-8")
    monkeypatch.chdir(p)
    monkeypatch.setattr(pdf, "SCRATCH", str(tmp_path / "scratch-%d"))
    return p


def written(project: Path, body: str, head: str = "", argv: tuple = ()) -> Path:
    src = project / "deliverables" / "guide-q7m4.html"
    src.write_text("<html><head><title>guide q7m4</title>%s</head><body>%s</body></html>" % (head, body),
                   encoding="utf-8")
    assert docx.main([str(src), *argv]) == 0
    return src.with_suffix(".docx")


def part(path: Path, name: str) -> str:
    with zipfile.ZipFile(path) as z:
        return z.read(name).decode("utf-8")


def paragraphs(path: Path) -> list[tuple[str, str]]:
    return [(p.style.name, p.text) for p in pydocx.Document(str(path)).paragraphs]


GUIDE = """
<h1>migration guide</h1>
<!-- toc -->
<h2 id="prep">1. Preparation</h2>
<p>read <a href="#net">the network part</a> first, then <a href="#nowhere">a lost link</a>
and <a href="https://example.org/docs">the public docs</a>.</p>
<h3>1.1 Accounts</h3>
<h3 style="color: rgb(0, 128, 0)">1.2 Keys</h3>
<h2 id="net">2. Network</h2>
<p>plain text</p>
"""
STYLE = "<style>h1, h2 { color: #e20074 } h3 { color: #e20074 }</style>"


# --------------------------------------------------------------------------- headings

def test_headings_carry_their_styles_colour_and_a_word_numbering(project):
    out = written(project, GUIDE, STYLE)
    doc = pydocx.Document(str(out))
    heads = [(p.style.name, p.text) for p in doc.paragraphs if p.style.name.startswith("Heading")]
    assert heads == [("Heading 1", "migration guide"), ("Heading 2", "Preparation"), ("Heading 3", "Accounts"),
                     ("Heading 3", "Keys"), ("Heading 2", "Network")]
    for name in ("Heading 1", "Heading 2", "Heading 3"):
        assert doc.styles[name].font.color.rgb == RGBColor(0xE2, 0x00, 0x74)
        assert doc.styles[name].font.bold
    keys = next(p for p in doc.paragraphs if p.text == "Keys")
    assert keys.runs[0].font.color.rgb == RGBColor(0x00, 0x80, 0x00)
    levels = {p.text: p._p.pPr.numPr for p in doc.paragraphs if p.style.name.startswith("Heading")}
    assert levels["migration guide"] is None
    assert [(levels[t].numId.val, levels[t].ilvl.val) for t in ("Preparation", "Accounts", "Keys", "Network")] == \
        [(1, 0), (1, 1), (1, 1), (1, 0)]
    numbering = part(out, "word/numbering.xml")
    assert '<w:lvlText w:val="%1."/>' in numbering and '<w:lvlText w:val="%1.%2"/>' in numbering
    assert doc.styles["Heading 2"].font.size.pt == 12 and doc.styles["Heading 1"].font.size.pt == 17


def test_numbers_with_a_gap_stay_text_and_no_numbering_is_set(project):
    out = written(project, "<h2>1. first</h2><h2>3. third</h2><h3>3.1 inner</h3>")
    doc = pydocx.Document(str(out))
    heads = [p for p in doc.paragraphs if p.style.name.startswith("Heading")]
    assert [p.text for p in heads] == ["1. first", "3. third", "3.1 inner"]
    assert all(p._p.pPr.numPr is None for p in heads)


def test_a_new_page_before_each_level_one_heading_but_the_first(project):
    out = written(project, "<h1>one</h1><p>a</p><h1>two</h1>", argv=("--h1-page",))
    doc = part(out, "word/document.xml")
    assert doc.count("<w:pageBreakBefore/>") == 1 and doc.index("<w:pageBreakBefore/>") > doc.index(">one<")


# --------------------------------------------------------------------------- contents, bookmarks, links

def test_the_contents_field_lists_the_headings_and_links_them_before_an_update(project):
    out = written(project, GUIDE, STYLE)
    doc = part(out, "word/document.xml")
    assert re.search(r'<w:instrText xml:space="preserve"> TOC \\o "1-2" \\h \\z \\u </w:instrText>', doc)
    begin, sep, end = (doc.index('w:fldCharType="%s"' % k) for k in ("begin", "separate", "end"))
    assert begin < sep < end
    result = doc[sep:end]
    entries = re.findall(r'<w:hyperlink w:anchor="(_Toc\d+)"[^>]*>.*?<w:t[^>]*>([^<]*)</w:t>', result)
    assert [t for _, t in entries] == ["1. Preparation", "2. Network"]
    for anchor, _ in entries:
        assert '<w:bookmarkStart w:id="' in doc and 'w:name="%s"/>' % anchor in doc
    styles = [s for s, t in paragraphs(out) if t in ("1. Preparation", "2. Network")]
    assert styles == ["toc 2", "toc 2"]
    assert '<w:updateFields w:val="true"/>' in part(out, "word/settings.xml")


def test_ids_are_bookmarks_and_links_to_them_are_hyperlinks(project):
    out = written(project, GUIDE, STYLE)
    doc = part(out, "word/document.xml")
    assert 'w:name="net"/>' in doc and 'w:name="prep"/>' in doc
    link = re.search(r'<w:hyperlink w:anchor="net"[^>]*>(.*?)</w:hyperlink>', doc).group(1)
    assert "the network part" in link and 'w:val="Hyperlink"' in link
    assert "a lost link" in doc and 'w:anchor="nowhere"' not in doc
    rid = re.search(r'<w:hyperlink r:id="(rId\d+)"[^>]*>.*?the public docs', doc).group(1)
    rels = part(out, "word/_rels/document.xml.rels")
    assert re.search(r'Id="%s" Type="[^"]+/hyperlink" Target="https://example.org/docs" TargetMode="External"'
                     % rid, rels)
    text = " ".join(t for _, t in paragraphs(out))
    assert "(https://example.org/docs)" in text and "#net" not in text


# --------------------------------------------------------------------------- figures

def test_a_png_is_an_inline_picture_with_its_bytes_and_a_caption_below(project):
    data = png(30, 20)
    (project / "deliverables" / "plan.png").write_bytes(data)
    out = written(project, '<p>before</p><figure><img src="plan.png" alt="the plan"><figcaption>figure 1: the '
                           'plan of the network</figcaption></figure><p>after</p>')
    with zipfile.ZipFile(out) as z:
        media = [n for n in z.namelist() if n.startswith("word/media/")]
        assert media == ["word/media/image1.png"] and z.read(media[0]) == data
    doc = pydocx.Document(str(out))
    assert len(doc.inline_shapes) == 1
    shape = doc.inline_shapes[0]
    assert (shape.width, shape.height) == (30 * 9525, 20 * 9525)          # 1 px = 0.75 pt = 9525 EMU
    styles = paragraphs(out)
    i = [s for s, _ in styles].index("Figure")
    assert styles[i + 1] == ("Caption", "figure 1: the plan of the network")
    assert doc.styles["Caption"].font.italic
    assert 'descr="figure 1: the plan of the network"' in part(out, "word/document.xml")


def test_a_wide_image_and_a_data_uri_are_scaled_to_the_text_width(project):
    (project / "deliverables" / "wide.png").write_bytes(png(1600, 400))
    uri = base64.b64encode(png(8, 8)).decode()
    out = written(project, '<img src="wide.png" alt="wide"><p>x <img src="data:image/png;base64,%s" alt="dot"></p>'
                  % uri)
    shapes = pydocx.Document(str(out)).inline_shapes
    assert len(shapes) == 2
    text_width = round(docx.TEXT_W * docx.EMU)
    assert abs(shapes[0].width - text_width) <= 1 and abs(shapes[0].height - text_width / 4) <= 1
    assert shapes[1].width == 8 * 9525
    assert "image/png" in part(out, "[Content_Types].xml")


SVG = ('<svg width="200" height="100" viewBox="0 0 200 100" preserveAspectRatio="xMidYMid meet" onload="x()">'
       '<script>bad()</script><rect class="box" x="10" y="10" width="180" height="80"/>'
       '<a href="https://example.org/far"><text x="20" y="50">edge router</text></a></svg>')


def test_an_inline_svg_is_an_svg_picture_with_a_png_fallback(project):
    out = written(project, "<h2>drawing</h2>" + SVG + "<p>after</p>", "<style>.box { fill: #e20074 }</style>")
    with zipfile.ZipFile(out) as z:
        svg = z.read("word/media/image1.svg").decode("utf-8")
        fallback = z.read("word/media/image1.png")
    assert svg.startswith('<?xml') and 'xmlns="http://www.w3.org/2000/svg"' in svg
    assert 'viewBox="0 0 200 100"' in svg and 'preserveAspectRatio=' in svg and "edge router" in svg
    assert ".box { fill: #e20074 }" in svg
    for gone in ("script", "bad()", "onload", "example.org/far"):
        assert gone not in svg, gone
    assert fallback.startswith(b"\x89PNG\r\n\x1a\n")
    doc = part(out, "word/document.xml")
    assert "svgBlip" in doc and docx.SVG_URI in doc
    assert "image/svg+xml" in part(out, "[Content_Types].xml")
    assert "edge router" not in " ".join(t for _, t in paragraphs(out))         # no markup or text leaks


def test_an_svg_without_pdftoppm_stands_alone_and_one_line_says_so(project, monkeypatch, capsys):
    monkeypatch.setattr(docx.shutil, "which", lambda name: None)
    capsys.readouterr()
    out = written(project, SVG)
    said = capsys.readouterr()
    assert [line for line in said.err.splitlines() if "fallback" in line] == [
        "awb docx: 1 SVG figure(s) without a PNG fallback (no pdftoppm): Word 2016 and later show them, an older "
        "reader does not"]
    with zipfile.ZipFile(out) as z:
        assert [n for n in z.namelist() if n.startswith("word/media/")] == ["word/media/image1.svg"]
    assert len(pydocx.Document(str(out)).inline_shapes) == 1


def test_a_web_image_is_left_out_and_counted_in_one_line(project, capsys):
    capsys.readouterr()
    out = written(project, '<p>text</p><img src="https://example.org/x.png" alt="remote chart">')
    assert capsys.readouterr().err.splitlines() == ["awb docx: left out of the figures: 1 image from the web"]
    assert ("Caption", "remote chart") in paragraphs(out)
    assert len(pydocx.Document(str(out)).inline_shapes) == 0


# --------------------------------------------------------------------------- tables, code, lists, footer

def test_a_table_has_a_header_row_repeated_on_each_page(project):
    out = written(project, "<table><tr><th>flavor</th><th>vcpu</th></tr><tr><td>s3.large</td><td>2</td></tr>"
                           "<tr><td>s3.xlarge</td><td>4</td></tr></table><table><tr><td>a</td><td>b</td></tr></table>")
    doc = pydocx.Document(str(out))
    assert len(doc.tables) == 2
    first, second = doc.tables
    assert first.style.name == "Table Grid"
    assert [[c.text for c in r.cells] for r in first.rows] == [["flavor", "vcpu"], ["s3.large", "2"],
                                                                 ["s3.xlarge", "4"]]
    head = first.rows[0]._tr
    assert head.trPr is not None and head.trPr.find(W + "tblHeader") is not None
    assert first.rows[0].cells[0].paragraphs[0].runs[0].bold
    assert first.rows[1]._tr.trPr is None
    assert second.rows[0]._tr.trPr is None                       # no th, no header row


def test_markdown_pipe_tables_code_lists_and_alignment(project):
    src = project / "deliverables" / "notes-q7m4.md"
    src.write_text("# notes\n\n| step | time |\n|:--|--:|\n| copy | 5 min |\n\n```\nawb pdf a.md\n\tindented\n```\n\n"
                   "- one\n- two\n  - deeper\n\n3. third\n4. fourth\n\ntext with `inline` code\n", encoding="utf-8")
    assert docx.main([str(src)]) == 0
    out = src.with_suffix(".docx")
    doc = pydocx.Document(str(out))
    table = doc.tables[0]
    assert [[c.text for c in r.cells] for r in table.rows] == [["step", "time"], ["copy", "5 min"]]
    assert table.rows[0]._tr.trPr.find(W + "tblHeader") is not None
    assert table.rows[1].cells[1].paragraphs[0].alignment == 2      # right
    code = [p.text for p in doc.paragraphs if p.style.name == "Code"]
    assert code == ["awb pdf a.md", "        indented"]
    assert doc.styles["Code"].font.name == "Courier New"
    items = [(p.text, p._p.pPr.numPr.numId.val, p._p.pPr.numPr.ilvl.val) for p in doc.paragraphs
             if p.style.name == "List Paragraph"]
    assert [(t, lvl) for t, _, lvl in items] == [("one", 0), ("two", 0), ("deeper", 1), ("third", 0),
                                                 ("fourth", 0)]
    assert items[0][1] == items[1][1] == items[2][1] == 2 and items[3][1] == items[4][1] > 2
    numbering = part(out, "word/numbering.xml")
    assert re.search(r'<w:num w:numId="%d">.*?<w:startOverride w:val="3"/>' % items[3][1], numbering)
    assert 'w:val="CodeChar"' in part(out, "word/document.xml")


def test_the_footer_carries_page_and_numpages_fields(project):
    out = written(project, "<p>one page</p>")
    footer = part(out, "word/footer1.xml")
    assert re.search(r"<w:instrText[^>]*> PAGE </w:instrText>.*/.*<w:instrText[^>]*> NUMPAGES </w:instrText>",
                     footer)
    doc = part(out, "word/document.xml")
    rid = re.search(r'<w:footerReference w:type="default" r:id="(rId\d+)"/>', doc).group(1)
    assert re.search(r'Id="%s" Type="[^"]+/footer" Target="footer1.xml"' % rid,
                     part(out, "word/_rels/document.xml.rels"))
    assert '<w:pgSz w:w="11906" w:h="16838"/>' in doc
    props = pydocx.Document(str(out)).core_properties
    assert props.title == "guide q7m4" and props.author == "Architect Workbench"


def test_every_xml_part_is_well_formed_and_holds_no_text_box(project):
    import xml.dom.minidom

    (project / "deliverables" / "plan.png").write_bytes(png(4, 4))
    out = written(project, GUIDE + '<img src="plan.png" alt="a">' + SVG + "<pre>x &lt; y</pre>", STYLE)
    with zipfile.ZipFile(out) as z:
        for name in z.namelist():
            if name.endswith((".xml", ".rels", ".svg")):
                xml.dom.minidom.parseString(z.read(name))
        body = z.read("word/document.xml").decode("utf-8")
    for frame in ("w:txbxContent", "w:framePr", "w:sdt", "v:textbox", "wps:"):
        assert frame not in body
    assert "x &lt; y" in body


# --------------------------------------------------------------------------- refusals

def test_the_folder_suffix_and_screen_rules_of_awb_pdf(project, tmp_path, capsys):
    src = project / "deliverables" / "guide-q7m4.html"
    src.write_text("<p>ok</p>", encoding="utf-8")
    assert docx.main([str(src), "--out", str(tmp_path / "outside.docx")]) == 2
    assert "inside the project folder" in capsys.readouterr().err
    assert docx.main([str(src), "--out", str(project / "x.pdf")]) == 2
    assert "the output is a .docx file" in capsys.readouterr().err
    txt = project / "deliverables" / "plain.txt"
    txt.write_text("x", encoding="utf-8")
    assert docx.main([str(txt)]) == 2
    capsys.readouterr()
    secret = "pass" + "&#119;" + "ord=q7m4-private-value"
    for body in ("<p>%s</p>" % secret, '<svg viewBox="0 0 10 10"><text x="1" y="5">%s</text></svg>' % secret):
        src.write_text("<html><body>%s</body></html>" % body, encoding="utf-8")
        assert docx.main([str(src)]) == 2
        assert "private content refused" in capsys.readouterr().err
        assert not src.with_suffix(".docx").exists()


def test_a_control_character_is_written_as_a_question_mark_and_counted(project, capsys):
    capsys.readouterr()
    out = written(project, "<p>bell\x07here</p>")
    assert "docx: 1 character(s) XML cannot hold written as ?" in capsys.readouterr().out
    assert ("Normal", "bell?here") in paragraphs(out)
