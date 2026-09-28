"""Red-team cases for the pdf reader (awb/extract/pdf.py).

The output a session reads is the sanitised `ex.text`: pdftotext -layout, one page per call, pages joined by a
blank line. `ex.detect_text` (reading order pass + Info/XMP/link lines) drives detection only. `ex.scan_text`
(every object string) is checked for registered forms only, no candidates and no structured patterns.

So a value escapes when it stays in `ex.text` in a shape the matcher or the structured patterns miss. A carrier
is unseen when its content reaches neither `ex.text` nor a place that detection renders into an output.

Interface: same as SCRATCH/rt/cases/smoke.py. build(inbox) writes ONE file and returns its path.
Registered forms come from tests/fixtures.py. Structured values are invented but valid in shape.
"""
from __future__ import annotations

import os
import struct
import sys
import zlib
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))   # the repository that holds this pack
from tests import fixtures as fx  # noqa: E402

FULL, SHORT, ACRONYM, ENGLISH = fx.CUSTOMER_FORMS
DOMAIN = fx.CUSTOMER_DOMAIN
PERSON = fx.PERSON_FORMS[0]
ORG = fx.ORG_FORMS[0]
PLACE = fx.PLACE_FORMS[0]
TENDER = fx.TENDER_ID

# invented structured values, valid in shape (mod-97 ok IBAN taken from awb/planted.py)
IBAN = "DE89 3704 0044 0532 0130 00"
IBAN_NS = "DE89370400440532013000"
PHONE = "030 1234555"
PHONE_INTL = "+49 30 1234555"
IPV4 = "203.0.113.45"
IPV6 = "2001:db8:1c1c:1234::1"
MAC = "02:00:5e:10:00:42"
VAT = "DE123456789"
MAILADDR = "%s@example.org" % PERSON.lower().replace(" ", ".")
URLADDR = "https://portal.%s/offer" % DOMAIN

MARK = "stage two of the invented plan"      # innocent marker, plain ascii
MARK2 = "review the second workload here"


# --------------------------------------------------------------------------- helpers


def _reportlab_canvas(path, draw):
    from reportlab.pdfgen import canvas

    c = canvas.Canvas(str(path), pagesize=(595, 842))
    draw(c)
    c.save()
    return path


def _rl(name, draw):
    def build(inbox):
        return _reportlab_canvas(inbox / name, draw)
    return build


def _spaced(c, x, y, text, step, size=12):
    c.setFont("Helvetica", size)
    for ch in text:
        if ch != " ":
            c.drawString(x, y, ch)
        x += step


def assemble(objs, root_ref=1, info_ref=None, prefix=b"%PDF-1.7\n%\xe2\xe3\xcf\xd3\n", extra_root=b""):
    """Assemble numbered objects (list of raw bodies, object 1 is objs[0]) into a PDF with a valid xref.

    Returns the bytes. root_ref points at the catalog. info_ref, when given, is put in the trailer.
    """
    out = bytearray(prefix)
    offsets = []
    for i, body in enumerate(objs, start=1):
        offsets.append(len(out))
        out += b"%d 0 obj\n" % i + body + b"\nendobj\n"
    xref = len(out)
    out += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objs) + 1)
    for off in offsets:
        out += b"%010d 00000 n \n" % off
    trailer = b"trailer\n<< /Size %d /Root %d 0 R" % (len(objs) + 1, root_ref)
    if info_ref is not None:
        trailer += b" /Info %d 0 R" % info_ref
    trailer += extra_root + b" >>\nstartxref\n%d\n%%%%EOF\n" % xref
    out += trailer
    return bytes(out)


def _stream_obj(dict_body, stream_bytes):
    return dict_body + b"\nstream\n" + stream_bytes + b"\nendstream"


def _page_content(text, x=72, y=750, size=12):
    body = b"BT /F1 %d Tf %d %d Td (" % (size, x, y) + text.encode("latin-1") + b") Tj ET"
    return body


def _simple_objs(content, extra_catalog=b"", page_extra=b"", extra_objs=b""):
    """Catalog(1) Pages(2) Page(3) Content(4) Font(5), plus optional trailing raw objects appended after 5."""
    objs = [
        b"<< /Type /Catalog /Pages 2 0 R" + extra_catalog + b" >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 595 842] /Contents 4 0 R"
        b" /Resources << /Font << /F1 5 0 R >> >>" + page_extra + b" >>",
        _stream_obj(b"<< /Length %d >>" % len(content), content),
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    return objs


def _write(inbox, name, data):
    p = inbox / name
    p.write_bytes(data)
    return p


# --------------------------------------------------------------------------- builders that hand-assemble


def build_tounicode(inbox):
    """The page draws the customer name with Helvetica glyphs, but a ToUnicode CMap maps every code to an
    innocent letter, so pdftotext extracts gibberish. The name shown on the page reaches no output."""
    shown = FULL  # the glyphs a viewer sees
    content = b"BT /F1 24 Tf 72 750 Td (" + shown.encode("latin-1") + b") Tj ET"
    # map every used byte code to letter 'x' (and space to space) in ToUnicode
    codes = sorted({c for c in shown.encode("latin-1")})
    lines = []
    for code in codes:
        target = 0x0020 if code == 0x20 else 0x0078  # space or 'x'
        lines.append(b"<%02X> <%04X>" % (code, target))
    cmap = (b"/CIDInit /ProcSet findresource begin 12 dict begin begincmap\n"
            b"/CMapName /Adobe-Identity-UCS def /CMapType 2 def\n"
            b"1 begincodespacerange <00> <FF> endcodespacerange\n"
            b"%d beginbfchar\n" % len(lines) + b"\n".join(lines) + b"\nendbfchar endcmap\n"
            b"CMapName currentdict /CMap defineresource pop end end")
    objs = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 595 842] /Contents 4 0 R"
        b" /Resources << /Font << /F1 5 0 R >> >> >>",
        _stream_obj(b"<< /Length %d >>" % len(content), content),
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica /ToUnicode 6 0 R >>",
        _stream_obj(b"<< /Length %d >>" % len(cmap), cmap),
    ]
    return _write(inbox, "remap.pdf", assemble(objs))


def build_type3(inbox):
    """A Type3 font whose glyphs are drawn as rectangles (vector paths), no ToUnicode, a non-standard
    encoding. pdftotext has no way to a Unicode text, the visible letters reach no output."""
    glyph = b"1000 0 0 0 700 700 d1 100 100 500 500 re f"
    content = b"BT /F1 40 Tf 72 750 Td <41424344> Tj ET"  # codes 0x41..0x44 -> four rectangle glyphs
    objs = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 595 842] /Contents 5 0 R"
        b" /Resources << /Font << /F1 4 0 R >> >> >>",
        b"<< /Type /Font /Subtype /Type3 /FontBBox [0 0 700 700] /FontMatrix [0.001 0 0 0.001 0 0]"
        b" /CharProcs 6 0 R /Encoding 7 0 R /FirstChar 65 /LastChar 68 /Widths [1000 1000 1000 1000] >>",
        _stream_obj(b"<< /Length %d >>" % len(content), content),
        b"<< /rect 8 0 R >>",
        b"<< /Type /Encoding /Differences [65 /rect /rect /rect /rect] >>",
        _stream_obj(b"<< /Length %d >>" % len(glyph), glyph),
    ]
    # marker of what a viewer would see: four block glyphs spelling nothing readable, so use FULL as the value
    return _write(inbox, "type3.pdf", assemble(objs))


def build_embedded_docx(inbox):
    """A docx with the customer name and a marker attached inside the pdf via the EmbeddedFiles name tree.
    The pdf reader does not open attachments, so the docx text reaches no output."""
    import docx

    tmp = inbox / "_inner.docx"
    doc = docx.Document()
    doc.add_paragraph("Kunde %s, %s" % (FULL, MARK))
    doc.save(str(tmp))
    payload = tmp.read_bytes()
    tmp.unlink()
    content = _page_content("cover sheet, see the attachment")
    objs = [
        b"<< /Type /Catalog /Pages 2 0 R /Names << /EmbeddedFiles 6 0 R >> >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 595 842] /Contents 4 0 R"
        b" /Resources << /Font << /F1 5 0 R >> >> >>",
        _stream_obj(b"<< /Length %d >>" % len(content), content),
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
        b"<< /Names [ (inner.docx) 7 0 R ] >>",
        b"<< /Type /Filespec /F (inner.docx) /EF << /F 8 0 R >> >>",
        _stream_obj(b"<< /Type /EmbeddedFile /Length %d >>" % len(payload), payload),
    ]
    return _write(inbox, "with-attachment.pdf", assemble(objs))


def build_portfolio(inbox):
    """A portfolio (collection) pdf. The cover carries neutral text, the customer material lives in an
    embedded child pdf that the reader never opens."""
    from reportlab.pdfgen import canvas
    import io

    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=(595, 842))
    c.drawString(72, 750, "Kunde %s" % FULL)
    c.drawString(72, 720, MARK)
    c.showPage()
    c.save()
    child = buf.getvalue()
    content = _page_content("portfolio cover, open the documents")
    objs = [
        b"<< /Type /Catalog /Pages 2 0 R /Collection << /View /D >> /Names << /EmbeddedFiles 6 0 R >> >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 595 842] /Contents 4 0 R"
        b" /Resources << /Font << /F1 5 0 R >> >> >>",
        _stream_obj(b"<< /Length %d >>" % len(content), content),
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
        b"<< /Names [ (angebot.pdf) 7 0 R ] >>",
        b"<< /Type /Filespec /F (angebot.pdf) /EF << /F 8 0 R >> >>",
        _stream_obj(b"<< /Type /EmbeddedFile /Subtype /application#2Fpdf /Length %d >>" % len(child), child),
    ]
    return _write(inbox, "portfolio.pdf", assemble(objs))


def build_outline(inbox):
    """The customer name and a marker only in bookmark titles (the outline). Not rendered into the page text."""
    content = _page_content("chapter one body text here for length padding one two three")
    objs = [
        b"<< /Type /Catalog /Pages 2 0 R /Outlines 6 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 595 842] /Contents 4 0 R"
        b" /Resources << /Font << /F1 5 0 R >> >> >>",
        _stream_obj(b"<< /Length %d >>" % len(content), content),
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
        b"<< /Type /Outlines /First 7 0 R /Last 7 0 R /Count 1 >>",
        b"<< /Title (" + ("Angebot %s %s" % (FULL, MARK)).encode("latin-1") + b") /Parent 6 0 R /Dest [3 0 R /Fit] >>",
    ]
    return _write(inbox, "bookmarks.pdf", assemble(objs))


def build_freetext_annot(inbox, value, name):
    """A FreeText annotation carrying `value`, with an appearance stream that draws it."""
    ap_content = b"BT /F1 12 Tf 2 8 Td (" + value.encode("latin-1") + b") Tj ET"
    content = _page_content("page body, see the sticky note")
    objs = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 595 842] /Contents 4 0 R"
        b" /Resources << /Font << /F1 5 0 R >> >> /Annots [6 0 R] >>",
        _stream_obj(b"<< /Length %d >>" % len(content), content),
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
        b"<< /Type /Annot /Subtype /FreeText /Rect [72 600 400 640] /Contents ("
        + value.encode("latin-1") + b") /DA (/F1 12 Tf 0 g) /AP << /N 7 0 R >> >>",
        _stream_obj(b"<< /Type /XObject /Subtype /Form /BBox [0 0 328 40]"
                    b" /Resources << /Font << /F1 5 0 R >> >> /Length %d >>" % len(ap_content), ap_content),
    ]
    return _write(inbox, name, assemble(objs))


def build_acroform(inbox, value, name, appearance=True):
    """An AcroForm text field with value `value`, optionally with an appearance stream."""
    ap_content = b"BT /F1 12 Tf 2 2 Td (" + value.encode("latin-1") + b") Tj ET"
    widget = (b"<< /Type /Annot /Subtype /Widget /FT /Tx /T (kunde) /V ("
              + value.encode("latin-1") + b") /Rect [72 600 400 630] /P 3 0 R")
    if appearance:
        widget += b" /AP << /N 7 0 R >>"
    widget += b" >>"
    content = _page_content("form page, one field below")
    objs = [
        b"<< /Type /Catalog /Pages 2 0 R /AcroForm << /Fields [6 0 R] /NeedAppearances "
        + (b"false" if appearance else b"true") + b" >> >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 595 842] /Contents 4 0 R"
        b" /Resources << /Font << /F1 5 0 R >> >> /Annots [6 0 R] >>",
        _stream_obj(b"<< /Length %d >>" % len(content), content),
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
        widget,
    ]
    if appearance:
        objs.append(_stream_obj(b"<< /Type /XObject /Subtype /Form /BBox [0 0 328 30]"
                                b" /Resources << /Font << /F1 5 0 R >> >> /Length %d >>" % len(ap_content), ap_content))
    return _write(inbox, name, assemble(objs))


def build_hidden_ocg(inbox):
    """A registered name inside an optional content group that is switched OFF (a hidden layer)."""
    content = (b"/OC /MC0 BDC BT /F1 14 Tf 72 750 Td (Kunde " + FULL.encode("latin-1")
               + b" " + MARK.encode("latin-1") + b") Tj ET EMC")
    objs = [
        b"<< /Type /Catalog /Pages 2 0 R /OCProperties << /OCGs [6 0 R] /D << /OFF [6 0 R] /ON [] >> >> >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 595 842] /Contents 4 0 R"
        b" /Resources << /Font << /F1 5 0 R >> /Properties << /MC0 6 0 R >> >> >>",
        _stream_obj(b"<< /Length %d >>" % len(content), content),
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
        b"<< /Type /OCG /Name (hidden layer) >>",
    ]
    return _write(inbox, "hidden-layer.pdf", assemble(objs))


def build_invisible_text(inbox, value, name):
    """Text in render mode 3 (invisible): does it reach the output and is it sanitised."""
    content = (b"BT /F1 12 Tf 3 Tr 72 750 Td (" + value.encode("latin-1") + b") Tj ET\n"
               b"BT /F1 12 Tf 0 Tr 72 700 Td (" + MARK.encode("latin-1") + b") Tj ET")
    objs = _simple_objs(content)
    return _write(inbox, name, assemble(objs))


def build_incremental(inbox):
    """A base revision shows the customer name and a structured value, an incremental update replaces the
    page content with innocent text. pdftotext reads the newer (visible) revision."""
    old = b"BT /F1 12 Tf 72 750 Td (Kunde " + FULL.encode("latin-1") + b", IBAN " + IBAN.encode("latin-1") + b") Tj ET"
    base_objs = _simple_objs(old)
    base = assemble(base_objs)
    # incremental update: new content stream as object 4 (same number), new page trailer /Prev
    new = b"BT /F1 12 Tf 72 750 Td (" + MARK.encode("latin-1") + b", nothing to see) Tj ET"
    add = bytearray(base)
    if not add.endswith(b"\n"):
        add += b"\n"
    off4 = len(add)
    add += b"4 0 obj\n" + _stream_obj(b"<< /Length %d >>" % len(new), new) + b"\nendobj\n"
    # find previous startxref
    prev = base.rfind(b"startxref")
    prev_off = int(base[prev + len(b"startxref"):].split()[0])
    xref = len(add)
    add += b"xref\n4 1\n%010d 00000 n \n" % off4
    add += b"trailer\n<< /Size 6 /Root 1 0 R /Prev %d >>\nstartxref\n%d\n%%%%EOF\n" % (prev_off, xref)
    return _write(inbox, "revised.pdf", bytes(add))


def build_junk_prefix(inbox):
    """A valid pdf (reportlab, compressed) with junk bytes before %PDF. sniff keys on %PDF at offset 0."""
    from reportlab.pdfgen import canvas
    import io

    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=(595, 842))
    c.drawString(72, 750, "Kunde %s" % FULL)
    c.drawString(72, 720, MARK)
    c.showPage()
    c.save()
    data = b"\x89 binary junk \x00\x01\x02 leading bytes before the header \x00\n" + buf.getvalue()
    return _write(inbox, "prefixed.pdf", data)


def build_broken_xref(inbox):
    """A valid set of objects but a corrupt xref (all offsets zero). Poppler reconstructs the xref."""
    content = _page_content("Kunde %s, %s" % (FULL, MARK))
    objs = _simple_objs(content)
    good = assemble(objs)
    # corrupt every offset line of the xref table to zeros
    import re

    broken = re.sub(rb"\d{10} 00000 n ", b"0000000000 00000 n ", good)
    return _write(inbox, "brokenxref.pdf", broken)


def build_many_pages(inbox, n=250):
    """More than MAX_PAGE_CALLS pages; the customer name only on a late page."""
    from reportlab.pdfgen import canvas

    p = inbox / "big.pdf"
    c = canvas.Canvas(str(p), pagesize=(595, 842))
    for i in range(1, n + 1):
        if i == 200:
            c.drawString(72, 750, "Kunde %s, %s" % (FULL, MARK))
        else:
            c.drawString(72, 750, "page %d body text padding padding padding padding" % i)
        c.showPage()
    c.save()
    return p


def build_image_scan(inbox):
    """An image-only pdf: the customer name is drawn into a raster image, no text layer."""
    from reportlab.pdfgen import canvas
    from reportlab.lib.utils import ImageReader
    from PIL import Image, ImageDraw
    import io

    img = Image.new("RGB", (900, 200), "white")
    draw = ImageDraw.Draw(img)
    draw.text((10, 40), "Kunde %s" % FULL, fill="black")
    draw.text((10, 90), MARK, fill="black")
    bio = io.BytesIO()
    img.save(bio, format="PNG")
    bio.seek(0)
    p = inbox / "scan.pdf"
    c = canvas.Canvas(str(p), pagesize=(595, 842))
    c.drawImage(ImageReader(bio), 40, 600, width=500, height=110)
    c.showPage()
    c.save()
    return p


def _draw_plain(c):
    c.setFont("Helvetica", 12)
    c.drawString(72, 750, "Angebot fuer %s" % FULL)
    c.drawString(72, 720, MARK)


def _two_columns(c):
    """FULL split so the two halves land in two columns on the same visual line (-layout keeps them on one
    line with spaces between)."""
    c.setFont("Helvetica", 12)
    c.drawString(72, 750, FULL.split(" ", 1)[0])
    c.drawString(360, 750, FULL.split(" ", 1)[1])
    c.drawString(72, 720, MARK)




def build_js(inbox):
    """A registered name and a marker inside document-level JavaScript. Object strings only (scan_text)."""
    js = ("var kunde = 'Kunde %s'; var note = '%s';" % (FULL, MARK)).encode("latin-1")
    content = _page_content("body text one two three four five six seven eight nine")
    objs = [
        b"<< /Type /Catalog /Pages 2 0 R /Names << /JavaScript 6 0 R >> >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 595 842] /Contents 4 0 R"
        b" /Resources << /Font << /F1 5 0 R >> >> >>",
        _stream_obj(b"<< /Length %d >>" % len(content), content),
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
        b"<< /Names [ (init) 7 0 R ] >>",
        b"<< /S /JavaScript /JS (" + js + b") >>",
    ]
    return _write(inbox, "withjs.pdf", assemble(objs))


def build_xmp_structured(inbox):
    """An IBAN inside the XMP metadata packet (dc:description). XMP reaches detect_text."""
    xmp = (
        b'<?xpacket begin="\xef\xbb\xbf"?><x:xmpmeta xmlns:x="adobe:ns:meta/">'
        b'<rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#">'
        b'<rdf:Description xmlns:dc="http://purl.org/dc/elements/1.1/">'
        b'<dc:description>Bankverbindung ' + IBAN.encode("latin-1") + b'</dc:description>'
        b'</rdf:Description></rdf:RDF></x:xmpmeta><?xpacket end="w"?>'
    )
    content = _page_content("body text one two three four five six seven eight nine ten")
    objs = [
        b"<< /Type /Catalog /Pages 2 0 R /Metadata 6 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 595 842] /Contents 4 0 R"
        b" /Resources << /Font << /F1 5 0 R >> >> >>",
        _stream_obj(b"<< /Length %d >>" % len(content), content),
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
        _stream_obj(b"<< /Type /Metadata /Subtype /XML /Length %d >>" % len(xmp), xmp),
    ]
    return _write(inbox, "xmpiban.pdf", assemble(objs))


def build_incremental_superseded(inbox):
    """The visible page is the newer revision (innocent). A structured value sits only in the superseded
    revision: not in the extracted text, and scan_text is registered-forms only."""
    old = b"BT /F1 12 Tf 72 750 Td (Bankverbindung " + IBAN.encode("latin-1") + b", " + MARK.encode("latin-1") + b") Tj ET"
    base = assemble(_simple_objs(old))
    new = b"BT /F1 12 Tf 72 750 Td (nothing to declare here, neutral cover) Tj ET"
    add = bytearray(base)
    if not add.endswith(b"\n"):
        add += b"\n"
    off4 = len(add)
    add += b"4 0 obj\n" + _stream_obj(b"<< /Length %d >>" % len(new), new) + b"\nendobj\n"
    prev = base.rfind(b"startxref")
    prev_off = int(base[prev + len(b"startxref"):].split()[0])
    xref = len(add)
    add += b"xref\n4 1\n%010d 00000 n \n" % off4
    add += b"trailer\n<< /Size 6 /Root 1 0 R /Prev %d >>\nstartxref\n%d\n%%%%EOF\n" % (prev_off, xref)
    return _write(inbox, "superseded.pdf", bytes(add))


def build_text_annot(inbox, value, name, subtype=b"Text"):
    """A Text (sticky note) or Popup annotation carrying `value` in /Contents, no appearance stream."""
    content = _page_content("page body with a sticky note attached to it here")
    objs = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 595 842] /Contents 4 0 R"
        b" /Resources << /Font << /F1 5 0 R >> >> /Annots [6 0 R] >>",
        _stream_obj(b"<< /Length %d >>" % len(content), content),
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
        b"<< /Type /Annot /Subtype /" + subtype + b" /Rect [72 600 92 620] /Contents ("
        + value.encode("latin-1") + b") >>",
    ]
    return _write(inbox, name, assemble(objs))


def build_link_uri(inbox):
    """A link annotation whose URI carries the customer domain; pdfinfo -url feeds detect_text."""
    content = _page_content("click the link in this document to open the portal page")
    objs = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 595 842] /Contents 4 0 R"
        b" /Resources << /Font << /F1 5 0 R >> >> /Annots [6 0 R] >>",
        _stream_obj(b"<< /Length %d >>" % len(content), content),
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
        b"<< /Type /Annot /Subtype /Link /Rect [72 700 300 720] /Border [0 0 0]"
        b" /A << /S /URI /URI (" + URLADDR.encode("ascii") + b") >> >>",
    ]
    return _write(inbox, "link.pdf", assemble(objs))


def build_rtl(inbox):
    """The customer short form written right to left, one glyph at a time from the right edge."""
    from reportlab.pdfgen import canvas

    p = inbox / "rtl.pdf"
    c = canvas.Canvas(str(p), pagesize=(595, 842))
    c.setFont("Helvetica", 14)
    x = 400
    for ch in SHORT:              # place the letters from right to left so the visual order is the short form
        c.drawString(x, 700, ch)
        x -= 22
    # actually emit in reverse so the stream order is o,w,x,y,Z but positions ascend left to right
    c.drawString(72, 660, MARK)
    c.save()
    return p


def build_rtl_stream(inbox):
    """Glyphs emitted right to left in the content stream but placed so the visual line reads left to right."""
    # positions ascend, but we emit the last letter first
    letters = list(SHORT)
    xs = [72 + 14 * i for i in range(len(letters))]
    ops = b""
    for ch, x in zip(reversed(letters), reversed(xs)):
        ops += b"BT /F1 14 Tf %d 700 Td (" % x + ch.encode("latin-1") + b") Tj ET\n"
    ops += b"BT /F1 12 Tf 72 660 Td (" + MARK.encode("latin-1") + b") Tj ET"
    return _write(inbox, "rtlstream.pdf", assemble(_simple_objs(ops)))


def build_ligature(inbox):
    """A name containing a typographic ligature glyph in the text layer."""
    from reportlab.pdfgen import canvas

    p = inbox / "liga.pdf"
    c = canvas.Canvas(str(p), pagesize=(595, 842))
    c.setFont("Helvetica", 12)
    # use a name that after folding is a registered form; insert an fi ligature into a padded name
    c.drawString(72, 750, "Angebot fuer %s" % FULL)
    c.drawString(72, 720, "oﬃce note: " + MARK)  # ligature in an innocent word, sanity for extraction
    c.save()
    return p


def _draw_short_only_iban(c):
    c.setFont("Helvetica", 12)
    c.drawString(72, 750, "IBAN DE 89 37 04 00 44 05 32 01 30 00")


def _info_pdf(inbox):
    """A pdf whose Info dictionary carries a custom key with a structured value."""
    content = _page_content("body text one two three four five six seven eight")
    objs = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 595 842] /Contents 4 0 R"
        b" /Resources << /Font << /F1 5 0 R >> >> >>",
        _stream_obj(b"<< /Length %d >>" % len(content), content),
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
        b"<< /Bankverbindung (" + IBAN.encode("latin-1") + b") /Title (Angebot) >>",
    ]
    return _write(inbox, "info.pdf", assemble(objs, info_ref=6))


MAILADDR2 = "angebot2026@example.org"


def build_invisible_variant(inbox, value, name, mode):
    """Structured value drawn as white text, off page, at size 0.1 or render mode 3, with a normal marker."""
    if mode == "white":
        draw = b"1 1 1 rg BT /F1 12 Tf 72 750 Td (" + value.encode("latin-1") + b") Tj ET 0 0 0 rg"
    elif mode == "offpage":
        draw = b"BT /F1 12 Tf -500 750 Td (" + value.encode("latin-1") + b") Tj ET"
    elif mode == "tiny":
        draw = b"BT /F1 0.1 Tf 72 750 Td (" + value.encode("latin-1") + b") Tj ET"
    else:
        draw = b"BT /F1 12 Tf 3 Tr 72 750 Td (" + value.encode("latin-1") + b") Tj ET"
    content = draw + b"\nBT /F1 12 Tf 72 700 Td (" + MARK.encode("latin-1") + b") Tj ET"
    return _write(inbox, name, assemble(_simple_objs(content)))


def build_iban_linebreak(inbox):
    """A grouped IBAN broken across two lines (two Td lines), the way a narrow column wraps it."""
    content = (b"BT /F1 12 Tf 72 750 Td (IBAN DE89 3704 0044) Tj ET\n"
               b"BT /F1 12 Tf 72 730 Td (0532 0130 00 fortlaufend) Tj ET")
    return _write(inbox, "ibanlb.pdf", assemble(_simple_objs(content)))


# --------------------------------------------------------------------------- the cases

CASES = [
    # baseline: a plain name and structured value must be caught
    {"id": "pdf-plain-name", "cls": "name", "carrier": "pdf page text", "values": [FULL],
     "visible": [MARK], "build": _rl("plain.pdf", _draw_plain)},
    {"id": "pdf-plain-iban", "cls": "iban", "carrier": "pdf page text", "values": [IBAN, IBAN_NS],
     "build": _rl("iban.pdf", lambda c: (c.setFont("Helvetica", 12), c.drawString(72, 750, "IBAN %s" % IBAN)))},

    # two columns / line / page splits of a registered form
    {"id": "pdf-two-columns", "cls": "name", "carrier": "pdf two columns same line", "values": [FULL],
     "visible": [MARK], "build": _rl("twocol.pdf", _two_columns)},

    # spread by per-glyph positioning: structured patterns have no spread search
    {"id": "pdf-iban-spaced", "cls": "iban", "carrier": "pdf per-glyph spaced digits", "values": [IBAN, IBAN_NS],
     "build": _rl("ibansp.pdf", lambda c: _spaced(c, 60, 700, IBAN_NS, 20))},
    {"id": "pdf-phone-spaced", "cls": "phone", "carrier": "pdf per-glyph spaced digits", "values": [PHONE, "0301234555"],
     "build": _rl("phonesp.pdf", lambda c: _spaced(c, 60, 700, "0301234555", 26))},
    {"id": "pdf-ipv4-spaced", "cls": "ip", "carrier": "pdf per-glyph spaced digits", "values": [IPV4],
     "build": _rl("ipsp.pdf", lambda c: _spaced(c, 60, 700, IPV4, 24))},
    {"id": "pdf-mac-spaced", "cls": "mac", "carrier": "pdf per-glyph spaced hex", "values": [MAC],
     "build": _rl("macsp.pdf", lambda c: _spaced(c, 60, 700, MAC, 22))},
    {"id": "pdf-vat-spaced", "cls": "vat", "carrier": "pdf per-glyph spaced", "values": [VAT],
     "build": _rl("vatsp.pdf", lambda c: _spaced(c, 60, 700, VAT, 24))},

    # a registered name spread wide (gap over the matcher's MAX_GAP of 8)
    {"id": "pdf-name-spaced-wide", "cls": "name", "carrier": "pdf per-glyph spaced letters", "values": [SHORT],
     "build": _rl("namesp.pdf", lambda c: _spaced(c, 60, 700, SHORT, 60))},
    {"id": "pdf-name-spaced-narrow", "cls": "name", "carrier": "pdf per-glyph spaced letters (small gap)", "values": [SHORT],
     "build": _rl("namesp2.pdf", lambda c: _spaced(c, 60, 700, SHORT, 22))},

    # font tricks: glyph shows one thing, text layer says another
    {"id": "pdf-tounicode-remap", "cls": "name", "carrier": "pdf ToUnicode maps glyphs to other letters",
     "values": [FULL], "visible": [FULL], "build": build_tounicode},
    {"id": "pdf-type3-vector", "cls": "name", "carrier": "pdf Type3 glyphs drawn as vector paths, no ToUnicode",
     "values": [FULL], "visible": [MARK], "build": build_type3},

    # carriers the reader does not render into the output
    {"id": "pdf-embedded-docx", "cls": "name", "carrier": "docx attached via EmbeddedFiles name tree",
     "values": [FULL], "visible": [MARK], "build": build_embedded_docx},
    {"id": "pdf-portfolio", "cls": "name", "carrier": "pdf portfolio, name in an embedded child pdf",
     "values": [FULL], "visible": [MARK], "build": build_portfolio},
    {"id": "pdf-outline", "cls": "name", "carrier": "pdf bookmark (outline) title", "values": [FULL],
     "visible": [MARK], "build": build_outline},

    # annotations and form fields with and without appearance streams
    {"id": "pdf-freetext-iban", "cls": "iban", "carrier": "pdf FreeText annotation with appearance",
     "values": [IBAN, IBAN_NS], "visible": [], "build": lambda inbox: build_freetext_annot(inbox, IBAN, "ftiban.pdf")},
    {"id": "pdf-freetext-name", "cls": "name", "carrier": "pdf FreeText annotation with appearance",
     "values": [FULL], "visible": [], "build": lambda inbox: build_freetext_annot(inbox, "Kunde %s" % FULL, "ftname.pdf")},
    {"id": "pdf-acroform-iban-ap", "cls": "iban", "carrier": "pdf AcroForm field value with appearance",
     "values": [IBAN, IBAN_NS], "build": lambda inbox: build_acroform(inbox, IBAN, "afiban.pdf", True)},
    {"id": "pdf-acroform-iban-noap", "cls": "iban", "carrier": "pdf AcroForm field value, no appearance",
     "values": [IBAN, IBAN_NS], "build": lambda inbox: build_acroform(inbox, IBAN, "afiban2.pdf", False)},
    {"id": "pdf-acroform-name-ap", "cls": "name", "carrier": "pdf AcroForm field value with appearance",
     "values": [FULL], "build": lambda inbox: build_acroform(inbox, "Kunde %s" % FULL, "afname.pdf", True)},

    # hidden layer, invisible text, incremental update
    {"id": "pdf-hidden-ocg", "cls": "name", "carrier": "pdf optional content group switched OFF", "values": [FULL],
     "visible": [MARK], "build": build_hidden_ocg},
    {"id": "pdf-invisible-name", "cls": "name", "carrier": "pdf render mode 3 invisible text", "values": [FULL],
     "visible": [MARK], "build": lambda inbox: build_invisible_text(inbox, "Kunde %s" % FULL, "invname.pdf")},
    {"id": "pdf-invisible-iban", "cls": "iban", "carrier": "pdf render mode 3 invisible text", "values": [IBAN, IBAN_NS],
     "visible": [MARK], "build": lambda inbox: build_invisible_text(inbox, "IBAN %s" % IBAN, "inviban.pdf")},
    {"id": "pdf-incremental", "cls": "name", "carrier": "pdf incremental update, name in the superseded revision",
     "values": [FULL, IBAN_NS], "visible": [MARK], "build": build_incremental},

    # metadata carriers
    {"id": "pdf-info-custom", "cls": "iban", "carrier": "pdf Info dictionary custom key", "values": [IBAN, IBAN_NS],
     "visible": [], "build": lambda inbox: _info_pdf(inbox)},

    # malformed containers
    {"id": "pdf-junk-prefix", "cls": "name", "carrier": "pdf with junk bytes before %PDF", "values": [FULL],
     "visible": [MARK], "build": build_junk_prefix},
    {"id": "pdf-broken-xref", "cls": "name", "carrier": "pdf with a corrupt xref table", "values": [FULL],
     "visible": [MARK], "build": build_broken_xref},

    # scale
    {"id": "pdf-many-pages", "cls": "name", "carrier": "pdf over MAX_PAGE_CALLS pages, name on page 200",
     "values": [FULL], "visible": [MARK], "build": build_many_pages},

    # image only
    {"id": "pdf-image-scan", "cls": "name", "carrier": "pdf image only, name in a raster picture", "values": [FULL],
     "visible": [MARK], "build": build_image_scan},

    # ---- round 2: natural-grouping structured escapes (no per-glyph trick) ----
    {"id": "pdf-iban-2digit-groups", "cls": "iban", "carrier": "pdf page, IBAN grouped in 2-digit blocks",
     "values": [IBAN_NS], "build": _rl("iban2.pdf", lambda c: (c.setFont("Helvetica", 12),
        c.drawString(72, 750, "IBAN DE 89 37 04 00 44 05 32 01 30 00")))},
    {"id": "pdf-iban-5char-groups", "cls": "iban", "carrier": "pdf page, IBAN grouped in 5-char blocks",
     "values": [IBAN_NS], "build": _rl("iban5.pdf", lambda c: (c.setFont("Helvetica", 12),
        c.drawString(72, 750, "IBAN DE893 70400 44053 20130 00")))},
    {"id": "pdf-vat-grouped", "cls": "vat", "carrier": "pdf page, VAT id grouped with spaces", "values": [VAT],
     "build": _rl("vatg.pdf", lambda c: (c.setFont("Helvetica", 12),
        c.drawString(72, 750, "USt-IdNr DE 123 456 789")))},
    {"id": "pdf-phone-dotted", "cls": "phone", "carrier": "pdf page, phone with dots between every digit",
     "values": [PHONE, "0301234555"], "build": _rl("phoned.pdf", lambda c: (c.setFont("Helvetica", 12),
        c.drawString(72, 750, "Tel 0.3.0.1.2.3.4.5.5.5")))},
    {"id": "pdf-hrb-spaced", "cls": "hrb", "carrier": "pdf page, register number spaced", "values": ["HRB 123456"],
     "build": _rl("hrb.pdf", lambda c: (c.setFont("Helvetica", 12),
        c.drawString(72, 750, "Handelsregister HR B 1 2 3 4 5 6")))},
    {"id": "pdf-mail-spaced", "cls": "mail", "carrier": "pdf per-glyph spaced mail address", "values": [MAILADDR],
     "build": _rl("mailsp.pdf", lambda c: _spaced(c, 40, 700, MAILADDR, 12))},
    {"id": "pdf-ipv6-spaced", "cls": "ip", "carrier": "pdf per-glyph spaced IPv6", "values": [IPV6],
     "build": _rl("ip6sp.pdf", lambda c: _spaced(c, 40, 700, IPV6, 16))},

    # near-empty page: only a spaced IBAN on the page (fewer than MIN_PAGE_CHARS)
    {"id": "pdf-iban-near-empty-page", "cls": "iban", "carrier": "pdf near-empty page, only a grouped IBAN",
     "values": [IBAN_NS], "build": _rl("nearempty.pdf", _draw_short_only_iban)},

    # ---- round 2: carriers scanned for registered forms only (structured + markers pass) ----
    {"id": "pdf-js", "cls": "name", "carrier": "pdf document-level JavaScript string", "values": [FULL],
     "visible": [MARK], "build": build_js},
    {"id": "pdf-freetext-iban-marked", "cls": "iban", "carrier": "pdf FreeText annotation, structured value",
     "values": [IBAN, IBAN_NS], "visible": [MARK],
     "build": lambda inbox: build_freetext_annot(inbox, "%s IBAN %s" % (MARK, IBAN), "ftm.pdf")},
    {"id": "pdf-text-annot-iban", "cls": "iban", "carrier": "pdf Text (sticky note) annotation, no appearance",
     "values": [IBAN, IBAN_NS], "visible": [MARK],
     "build": lambda inbox: build_text_annot(inbox, "%s IBAN %s" % (MARK, IBAN), "sticky.pdf")},
    {"id": "pdf-popup-annot", "cls": "name", "carrier": "pdf Popup annotation contents", "values": [FULL],
     "visible": [MARK], "build": lambda inbox: build_text_annot(inbox, "Kunde %s %s" % (FULL, MARK), "popup.pdf", b"Popup")},
    {"id": "pdf-acroform-iban-noap-marked", "cls": "iban", "carrier": "pdf AcroForm field value, no appearance",
     "values": [IBAN, IBAN_NS], "visible": [MARK],
     "build": lambda inbox: build_acroform(inbox, "%s IBAN %s" % (MARK, IBAN), "afm.pdf", False)},

    # metadata carriers reach detection (should be seen, not an escape)
    {"id": "pdf-xmp-iban", "cls": "iban", "carrier": "pdf XMP dc:description structured value",
     "values": [IBAN, IBAN_NS], "visible": [], "build": build_xmp_structured},
    {"id": "pdf-link-uri-domain", "cls": "url", "carrier": "pdf Link annotation URI with the customer domain",
     "values": [DOMAIN], "visible": [], "build": build_link_uri},

    # incremental update, structured value only in the superseded revision
    {"id": "pdf-incremental-superseded", "cls": "iban", "carrier": "pdf superseded revision, structured value",
     "values": [IBAN, IBAN_NS], "visible": [MARK], "build": build_incremental_superseded},

    # right to left and ligatures
    {"id": "pdf-rtl-stream", "cls": "name", "carrier": "pdf glyphs emitted right to left in the stream",
     "values": [SHORT], "visible": [MARK], "build": build_rtl_stream},
    {"id": "pdf-ligature", "cls": "name", "carrier": "pdf page text with a ligature glyph nearby", "values": [FULL],
     "visible": [MARK], "build": build_ligature},

    # an unregistered person name in a FreeText annotation (extracted -> candidate) vs no-appearance carrier
    {"id": "pdf-freetext-unregistered", "cls": "name", "carrier": "pdf FreeText annotation, unregistered person",
     "values": [fx.PLANTED_PERSON], "visible": [MARK],
     "build": lambda inbox: build_freetext_annot(inbox, "%s Ansprechpartner %s" % (MARK, fx.PLANTED_PERSON), "ftu.pdf")},
    {"id": "pdf-acroform-unregistered-noap", "cls": "name", "carrier": "pdf AcroForm value, unregistered person, no appearance",
     "values": [fx.PLANTED_PERSON], "visible": [MARK],
     "build": lambda inbox: build_acroform(inbox, "%s %s" % (MARK, fx.PLANTED_PERSON), "afu.pdf", False)},

    # ---- round 3: remaining brief angles ----
    {"id": "pdf-stamp-annot", "cls": "iban", "carrier": "pdf Stamp annotation contents, no appearance",
     "values": [IBAN, IBAN_NS], "visible": [MARK],
     "build": lambda inbox: build_text_annot(inbox, "%s IBAN %s" % (MARK, IBAN), "stamp.pdf", b"Stamp")},
    {"id": "pdf-mail-spaced-neutral", "cls": "mail", "carrier": "pdf per-glyph spaced mail, neutral local part",
     "values": [MAILADDR2], "build": _rl("mailn.pdf", lambda c: _spaced(c, 30, 700, MAILADDR2, 12))},
    {"id": "pdf-iban-linebreak", "cls": "iban", "carrier": "pdf grouped IBAN wrapped across two lines",
     "values": [IBAN_NS], "build": build_iban_linebreak},
    {"id": "pdf-invisible-white-iban", "cls": "iban", "carrier": "pdf white text, grouped IBAN", "values": [IBAN_NS],
     "visible": [MARK], "build": lambda inbox: build_invisible_variant(inbox, "IBAN DE 89 37 04 00 44 05 32 01 30 00", "white.pdf", "white")},
    {"id": "pdf-invisible-offpage-iban", "cls": "iban", "carrier": "pdf off-page text, grouped IBAN", "values": [IBAN_NS],
     "visible": [MARK], "build": lambda inbox: build_invisible_variant(inbox, "IBAN DE 89 37 04 00 44 05 32 01 30 00", "offpage.pdf", "offpage")},
    {"id": "pdf-invisible-tiny-name", "cls": "name", "carrier": "pdf size 0.1 text with the customer name", "values": [FULL],
     "visible": [MARK], "build": lambda inbox: build_invisible_variant(inbox, "Kunde %s" % FULL, "tiny.pdf", "tiny")},

    # ---- round 4: complete structured-class coverage and the boundary ----
    {"id": "pdf-tax-spaced", "cls": "tax", "carrier": "pdf per-glyph spaced tax number", "values": ["12/345/67890"],
     "build": _rl("taxsp.pdf", lambda c: _spaced(c, 60, 700, "12/345/67890", 22))},
    {"id": "pdf-bic-spaced", "cls": "bic", "carrier": "pdf per-glyph spaced BIC", "values": ["QAZWDEFFXXX"],
     "build": _rl("bicsp.pdf", lambda c: _spaced(c, 60, 700, "QAZWDEFFXXX", 22))},
    {"id": "pdf-phone-single-space", "cls": "phone", "carrier": "pdf phone with one space per digit", "values": ["0301234555"],
     "build": _rl("phone1.pdf", lambda c: (c.setFont("Helvetica", 12), c.drawString(72, 750, "Tel 0 3 0 1 2 3 4 5 5 5")))},
    # boundary: the canonical 4-char IBAN grouping is caught (baseline for the grouped escapes above)
    {"id": "pdf-iban-4groups-baseline", "cls": "iban", "carrier": "pdf page, IBAN in canonical 4-char groups", "values": [IBAN_NS],
     "build": _rl("iban4.pdf", lambda c: (c.setFont("Helvetica", 12), c.drawString(72, 750, "IBAN DE89 3704 0044 0532 0130 00")))},
]


# What tests/test_redteam_pack.py accepts besides the fixture forms. DERIVED: strings built from a fixture form
# (a typo, an encoding, a transliteration). INVENTED: structured values this module builds, invented but valid
# in shape (documentation ranges, fake digit families). A value may carry one of them whole or in part.
DERIVED = ()
INVENTED = (IBAN, IBAN_NS, PHONE, PHONE_INTL, "0301234555", IPV4, IPV6, MAC, VAT, MAILADDR2, "HRB 123456",
            "12/345/67890", "QAZWDEFFXXX")
