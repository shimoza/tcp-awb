"""Red team, dimension nontext: carriers that are not plain text (pictures, binary formats, formats without a
reader, office parts no reader renders). Same interface as smoke.py. Every value is an invented fixture form or
an invented structured value. Every case plants an innocent lowercase `visible` marker next to the value."""
from __future__ import annotations

import base64
import email.message
import io
import sqlite3
import struct
import sys
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))   # the repository that holds this pack
from tests import fixtures as fx  # noqa: E402

FULL, SHORT, ACRONYM, ENGLISH = fx.CUSTOMER_FORMS
PERSON = fx.PERSON_FORMS[0]
IBAN = "DE89 3704 0044 0532 0130 00"
PHONE = "+49 30 1234567"

# --------------------------------------------------------------------------- helpers


def _w(inbox, name, data):
    p = inbox / name
    p.write_bytes(data if isinstance(data, bytes) else data.encode("utf-8"))
    return p


def _zip(inbox, name, members, first_stored=None):
    """A zip with `members` (name -> bytes). `first_stored` is written first without compression (mimetype)."""
    p = inbox / name
    with zipfile.ZipFile(p, "w", zipfile.ZIP_DEFLATED) as z:
        if first_stored:
            z.writestr(zipfile.ZipInfo(first_stored[0]), first_stored[1], compress_type=zipfile.ZIP_STORED)
        for n, data in members.items():
            z.writestr(n, data)
    return p


_XML = '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
_REL_NS = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
_W_NS = (
    'xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main" '
    'xmlns:r="%s" ' % _REL_NS
    + 'xmlns:m="http://schemas.openxmlformats.org/officeDocument/2006/math" '
    'xmlns:mc="http://schemas.openxmlformats.org/markup-compatibility/2006" '
    'xmlns:wp="http://schemas.openxmlformats.org/drawingml/2006/wordprocessingDrawing" '
    'xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main" '
    'xmlns:pic="http://schemas.openxmlformats.org/drawingml/2006/picture" '
    'xmlns:c="http://schemas.openxmlformats.org/drawingml/2006/chart" '
    'xmlns:wps="http://schemas.microsoft.com/office/word/2010/wordprocessingShape" '
    'xmlns:asvg="http://schemas.microsoft.com/office/drawing/2016/SVG/main" '
    'xmlns:v="urn:schemas-microsoft-com:vml" xmlns:o="urn:schemas-microsoft-com:office:office"'
)


def wp(text):
    return '<w:p><w:r><w:t xml:space="preserve">%s</w:t></w:r></w:p>' % text


def make_docx(inbox, name, body, parts=None, rels=(), overrides=(), defaults=()):
    """A docx built by hand: `body` is the inner xml of w:body, `parts` extra members, `rels` relationships of
    the document part as (id, type suffix, target, external)."""
    parts = dict(parts or {})
    ct = [_XML, '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">',
          '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>',
          '<Default Extension="xml" ContentType="application/xml"/>']
    ct += ['<Default Extension="%s" ContentType="%s"/>' % d for d in defaults]
    ct += ['<Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.'
           'wordprocessingml.document.main+xml"/>']
    ct += ['<Override PartName="%s" ContentType="%s"/>' % o for o in overrides]
    ct += ["</Types>"]
    rel_lines = [_XML, '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">']
    for rid, typ, target, external in rels:
        rel_lines.append('<Relationship Id="%s" Type="%s/%s" Target="%s"%s/>'
                         % (rid, _REL_NS, typ, target, ' TargetMode="External"' if external else ""))
    rel_lines.append("</Relationships>")
    members = {
        "[Content_Types].xml": "".join(ct),
        "_rels/.rels": _XML + '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        '<Relationship Id="rId1" Type="%s/officeDocument" Target="word/document.xml"/></Relationships>' % _REL_NS,
        "word/document.xml": _XML + "<w:document %s><w:body>%s<w:sectPr/></w:body></w:document>" % (_W_NS, body),
        "word/_rels/document.xml.rels": "".join(rel_lines),
    }
    members.update(parts)
    return _zip(inbox, name, members)


def _png(text_chunks=None, itxt=None, size=(40, 30)):
    from PIL import Image, PngImagePlugin

    img = Image.new("RGB", size, (200, 30, 30))
    info = PngImagePlugin.PngInfo()
    for k, v in (text_chunks or {}).items():
        info.add_text(k, v)
    for k, v in (itxt or {}).items():
        info.add_itxt(k, v, lang="en", tkey=k)
    buf = io.BytesIO()
    img.save(buf, "PNG", pnginfo=info)
    return buf.getvalue()


def _jpeg(description=None, artist=None, comment=None, xmp=None, iptc=None, size=(40, 30)):
    """A JPEG with EXIF (ImageDescription, Artist), an XMP APP1 packet, an IPTC APP13 block and a COM segment."""
    from PIL import Image

    img = Image.new("RGB", size, (30, 30, 200))
    exif = Image.Exif()
    if description:
        exif[0x010E] = description
    if artist:
        exif[0x013B] = artist
    buf = io.BytesIO()
    if description or artist:
        img.save(buf, "JPEG", exif=exif.tobytes())
    else:
        img.save(buf, "JPEG")
    data = buf.getvalue()
    segments = b""
    if xmp:
        packet = ('<?xpacket begin="" id="W5M0MpCehiHzreSzNTczkc9d"?><x:xmpmeta xmlns:x="adobe:ns:meta/">'
                  '<rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#"><rdf:Description '
                  'xmlns:dc="http://purl.org/dc/elements/1.1/"><dc:description><rdf:Alt><rdf:li xml:lang="x-default">'
                  '%s</rdf:li></rdf:Alt></dc:description></rdf:Description></rdf:RDF></x:xmpmeta><?xpacket end="w"?>'
                  % xmp).encode("utf-8")
        payload = b"http://ns.adobe.com/xap/1.0/\x00" + packet
        segments += b"\xff\xe1" + struct.pack(">H", len(payload) + 2) + payload
    if iptc:
        rec = iptc.encode("utf-8")
        iptc_data = b"\x1c\x02\x78" + struct.pack(">H", len(rec)) + rec   # 2:120 caption
        block = b"8BIM\x04\x04\x00\x00" + struct.pack(">I", len(iptc_data)) + iptc_data
        if len(iptc_data) % 2:
            block += b"\x00"
        payload = b"Photoshop 3.0\x00" + block
        segments += b"\xff\xed" + struct.pack(">H", len(payload) + 2) + payload
    if comment:
        c = comment.encode("utf-8")
        segments += b"\xff\xfe" + struct.pack(">H", len(c) + 2) + c
    return data[:2] + segments + data[2:]


def _emf(text: str) -> bytes:
    """A minimal EMF: header record, one EMR_EXTTEXTOUTW record with `text` in UTF-16LE, EOF record."""
    s = text.encode("utf-16-le")
    emrtext = struct.pack("<iiIIIiiiiI", 10, 10, len(text), 76, 0, 0, 0, 200, 40, 76 + len(s) + (-len(s) % 4))
    rec = struct.pack("<IIiiiiIff", 84, 0, 0, 0, 200, 40, 1, 1.0, 1.0) + emrtext + s + b"\x00" * (-len(s) % 4)
    rec += b"\x00" * 4 * len(text)   # dx array
    rec = rec[:4] + struct.pack("<I", len(rec)) + rec[8:]
    eof = struct.pack("<IIIII", 14, 20, 0, 16, 20)
    header = struct.pack("<II", 1, 108) + struct.pack("<iiii", 0, 0, 200, 40) + struct.pack("<iiii", 0, 0, 2000, 400)
    header += b" EMF" + struct.pack("<IIIHHIII", 0x10000, 0, 3, 1, 0, 0, 0, 0)
    header += struct.pack("<iiii", 1024, 768, 320, 240) + b"\x00" * 20
    header = header[:4] + struct.pack("<I", len(header)) + header[8:]
    total = len(header) + len(rec) + len(eof)
    header = header[:48] + struct.pack("<I", total) + header[52:]
    return header + rec + eof


def _ovba_compress(source: bytes) -> bytes:
    """MS-OVBA compressed container of `source` with literal tokens only (what a module with no repeats gives):
    a flag byte before every eight literal bytes."""
    out = bytearray(b"\x01")
    pos = 0
    while pos < len(source):
        chunk = source[pos:pos + 4096]
        pos += 4096
        body = bytearray()
        for i in range(0, len(chunk), 8):
            body.append(0x00)
            body += chunk[i:i + 8]
        out += struct.pack("<H", (len(body) - 1) | 0xB000)
        out += body
    return bytes(out)


def _ole_header() -> bytes:
    """The 512 byte header of an OLE compound file, magic and a plausible shape, nothing else valid."""
    h = bytearray(512)
    h[0:8] = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"
    h[24:26] = b"\x3e\x00"
    h[26:28] = b"\x03\x00"
    h[28:30] = b"\xfe\xff"
    h[30:32] = b"\x09\x00"
    h[32:34] = b"\x06\x00"
    h[44:48] = b"\x01\x00\x00\x00"
    h[48:52] = b"\x01\x00\x00\x00"
    h[56:60] = b"\x00\x10\x00\x00"
    h[60:64] = b"\xfe\xff\xff\xff"
    h[68:72] = b"\xfe\xff\xff\xff"
    h[76:80] = b"\x00\x00\x00\x00"
    h[80:84] = b"\xff\xff\xff\xff"
    return bytes(h)


def _ole1_package(filename: str, data: bytes) -> bytes:
    """OLE1 embedded object stream of an OLE Package (what RTF \\objdata carries, hex encoded)."""
    fn = filename.encode("ascii") + b"\x00"
    native = b"\x02\x00" + fn + fn + b"\x00\x00\x03\x00" + struct.pack("<I", len(fn)) + fn
    native += struct.pack("<I", len(data)) + data
    cls = b"Package\x00"
    return (struct.pack("<II", 0x00000501, 2) + struct.pack("<I", len(cls)) + cls + struct.pack("<II", 0, 0)
            + struct.pack("<I", len(native)) + native)


def _pdf(content: str, prefix: bytes = b"") -> bytes:
    """A minimal valid PDF with one page and `content` as its content stream. `prefix` goes before the header."""
    objs = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 595 842] /Contents 4 0 R /Resources << /Font << /F1 5 0 R >> >> >>",
        b"<< /Length %d >>\nstream\n%s\nendstream" % (len(content.encode("latin-1")), content.encode("latin-1")),
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    out = bytearray(prefix + b"%PDF-1.4\n")
    offsets = []
    for i, body in enumerate(objs, start=1):
        offsets.append(len(out))
        out += b"%d 0 obj\n%s\nendobj\n" % (i, body)
    xref = len(out)
    out += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objs) + 1)
    for off in offsets:
        out += b"%010d 00000 n \n" % off
    out += b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (len(objs) + 1, xref)
    return bytes(out)


def _reportlab_pdf(lines, compress: int) -> bytes:
    from reportlab.pdfgen import canvas

    buf = io.BytesIO()
    c = canvas.Canvas(buf, pageCompression=compress)
    y = 750
    for line in lines:
        c.drawString(72, y, line)
        y -= 20
    c.showPage()
    c.save()
    return buf.getvalue()


def _hex(data: bytes) -> str:
    return data.hex()


# --------------------------------------------------------------------------- builders: office side parts

_CHART_XML = (
    _XML + '<c:chartSpace xmlns:c="http://schemas.openxmlformats.org/drawingml/2006/chart" '
    'xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main" xmlns:r="%s">' % _REL_NS
    + '<c:chart><c:title><c:tx><c:rich><a:bodyPr/><a:p><a:r><a:t>TITLE</a:t></a:r></a:p></c:rich></c:tx></c:title>'
    '<c:plotArea><c:barChart><c:barDir val="col"/><c:ser><c:idx val="0"/><c:order val="0"/><c:tx><c:strRef>'
    '<c:f>Sheet1!$B$1</c:f><c:strCache><c:ptCount val="1"/><c:pt idx="0"><c:v>SERIES</c:v></c:pt></c:strCache>'
    '</c:strRef></c:tx><c:dLbls><c:dLbl><c:idx val="0"/><c:tx><c:rich><a:bodyPr/><a:p><a:r><a:t>LABEL</a:t></a:r>'
    '</a:p></c:rich></c:tx><c:showVal val="1"/></c:dLbl></c:dLbls><c:cat><c:strRef><c:f>Sheet1!$A$2:$A$3</c:f>'
    '<c:strCache><c:ptCount val="2"/><c:pt idx="0"><c:v>north</c:v></c:pt><c:pt idx="1"><c:v>south</c:v></c:pt>'
    '</c:strCache></c:strRef></c:cat><c:val><c:numRef><c:f>Sheet1!$B$2:$B$3</c:f><c:numCache><c:formatCode>General'
    '</c:formatCode><c:ptCount val="2"/><c:pt idx="0"><c:v>3</c:v></c:pt><c:pt idx="1"><c:v>4</c:v></c:pt>'
    '</c:numCache></c:numRef></c:val></c:ser></c:barChart></c:plotArea></c:chart></c:chartSpace>'
)

_DRAWING = ('<w:p><w:r><w:drawing><wp:inline><wp:extent cx="5000000" cy="3000000"/><wp:docPr id="1" name="thing"/>'
            '<a:graphic><a:graphicData uri="%s">%s</a:graphicData></a:graphic></wp:inline></w:drawing></w:r></w:p>')


def docx_chart(inbox):
    chart = _CHART_XML.replace("TITLE", "%s, stage two of the invented chart plan" % FULL)
    chart = chart.replace("SERIES", "revenue with IBAN %s" % IBAN).replace("LABEL", "call %s" % PHONE)
    body = wp("stage one of the invented chart plan") + _DRAWING % (
        "http://schemas.openxmlformats.org/drawingml/2006/chart", '<c:chart r:id="rId2"/>')
    return make_docx(inbox, "chart.docx", body, parts={"word/charts/chart1.xml": chart},
                     rels=[("rId2", "chart", "charts/chart1.xml", False)],
                     overrides=[("/word/charts/chart1.xml",
                                 "application/vnd.openxmlformats-officedocument.drawingml.chart+xml")])


def docx_smartart(inbox):
    data = (_XML + '<dgm:dataModel xmlns:dgm="http://schemas.openxmlformats.org/drawingml/2006/diagram" '
            'xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main"><dgm:ptLst>'
            '<dgm:pt modelId="{1}" type="doc"><dgm:prSet/><dgm:spPr/><dgm:t><a:bodyPr/><a:p/></dgm:t></dgm:pt>'
            '<dgm:pt modelId="{2}"><dgm:prSet/><dgm:spPr/><dgm:t><a:bodyPr/><a:p><a:r><a:t>%s</a:t></a:r></a:p>'
            '</dgm:t></dgm:pt><dgm:pt modelId="{3}"><dgm:prSet/><dgm:spPr/><dgm:t><a:bodyPr/><a:p><a:r>'
            '<a:t>stage two of the invented smartart plan, IBAN %s</a:t></a:r></a:p></dgm:t></dgm:pt>'
            '</dgm:ptLst><dgm:cxnLst/></dgm:dataModel>' % (FULL, IBAN))
    body = wp("stage one of the invented smartart plan") + _DRAWING % (
        "http://schemas.openxmlformats.org/drawingml/2006/diagram",
        '<dgm:relIds xmlns:dgm="http://schemas.openxmlformats.org/drawingml/2006/diagram" r:dm="rId2" r:lo="rId3" '
        'r:qs="rId4" r:cs="rId5"/>')
    return make_docx(inbox, "smartart.docx", body, parts={"word/diagrams/data1.xml": data},
                     rels=[("rId2", "diagramData", "diagrams/data1.xml", False)],
                     overrides=[("/word/diagrams/data1.xml",
                                 "application/vnd.openxmlformats-officedocument.drawingml.diagramData+xml")])


def docx_altchunk(inbox):
    chunk = ("<html><head><meta charset='utf-8'></head><body><p>offer for %s</p><p>stage two of the invented "
             "chunk plan, IBAN %s, call %s</p></body></html>" % (FULL, IBAN, PHONE))
    body = wp("stage one of the invented chunk plan") + '<w:altChunk r:id="rId2"/>'
    return make_docx(inbox, "chunk.docx", body, parts={"word/afchunk.htm": chunk},
                     rels=[("rId2", "aFChunk", "afchunk.htm", False)],
                     overrides=[("/word/afchunk.htm", "text/html")])


def docx_wordart_vml(inbox):
    body = wp("stage one of the invented wordart plan") + (
        '<w:p><w:r><w:pict><v:shape id="WordArt1" type="#_x0000_t136" style="width:300pt;height:40pt" '
        'fillcolor="#3366ff"><v:textpath style="font-family:Arial" fitshape="t" string="%s, stage two of the '
        'invented wordart plan, IBAN %s"/></v:shape></w:pict></w:r></w:p>' % (FULL, IBAN))
    return make_docx(inbox, "wordart.docx", body)


def docx_textbox(inbox):
    inner = wp("offer for %s, stage two of the invented textbox plan, IBAN %s" % (FULL, IBAN))
    body = wp("stage one of the invented textbox plan") + (
        '<w:p><w:r><mc:AlternateContent><mc:Choice Requires="wps"><w:drawing><wp:anchor><wp:docPr id="2" '
        'name="box"/><a:graphic><a:graphicData uri="http://schemas.microsoft.com/office/word/2010/'
        'wordprocessingShape"><wps:wsp><wps:txbx><w:txbxContent>%s</w:txbxContent></wps:txbx></wps:wsp>'
        '</a:graphicData></a:graphic></wp:anchor></w:drawing></mc:Choice><mc:Fallback><w:pict><v:shape>'
        '<v:textbox><w:txbxContent>%s</w:txbxContent></v:textbox></v:shape></w:pict></mc:Fallback>'
        '</mc:AlternateContent></w:r></w:p>' % (inner, inner))
    return make_docx(inbox, "textbox.docx", body)


def docx_omml_inline(inbox):
    body = wp("stage one of the invented equation plan") + (
        '<w:p><m:oMathPara><m:oMath><m:r><m:t>%s = stage two of the invented equation plan</m:t></m:r>'
        '</m:oMath></m:oMathPara></w:p>' % FULL)
    return make_docx(inbox, "omml-inline.docx", body)


def docx_omml_block(inbox):
    body = wp("stage one of the invented equation plan") + (
        '<m:oMathPara><m:oMath><m:r><m:t>%s = stage two of the invented equation plan, IBAN %s</m:t></m:r>'
        '</m:oMath></m:oMathPara>' % (FULL, IBAN))
    return make_docx(inbox, "omml-block.docx", body)


_SVG = ('<?xml version="1.0" encoding="UTF-8"?>\n<svg xmlns="http://www.w3.org/2000/svg" width="400" height="120">'
        '<rect width="400" height="120" fill="#eee"/><text x="10" y="40">%s</text><text x="10" y="80">%s</text></svg>')


def _pic_drawing(rid, ext_svg=None):
    blip = '<a:blip r:embed="%s">' % rid
    if ext_svg:
        blip += ('<a:extLst><a:ext uri="{96DAC541-7B7A-43D3' '-8B79-37D633B846F1}"><asvg:svgBlip r:embed="%s"/></a:ext>'
                 '</a:extLst>' % ext_svg)
    blip += "</a:blip>"
    return _DRAWING % ("http://schemas.openxmlformats.org/drawingml/2006/picture",
                       '<pic:pic><pic:nvPicPr><pic:cNvPr id="3" name="picture"/><pic:cNvPicPr/></pic:nvPicPr>'
                       '<pic:blipFill>%s<a:stretch><a:fillRect/></a:stretch></pic:blipFill><pic:spPr/></pic:pic>' % blip)


def docx_media_svg(inbox):
    svg = _SVG % ("offer for %s" % FULL, "stage two of the invented svg plan, IBAN %s" % IBAN)
    body = wp("stage one of the invented svg plan") + _pic_drawing("rId2", "rId3")
    return make_docx(inbox, "svg-media.docx", body,
                     parts={"word/media/image1.png": _png(), "word/media/image2.svg": svg},
                     rels=[("rId2", "image", "media/image1.png", False), ("rId3", "image", "media/image2.svg", False)],
                     defaults=[("png", "image/png"), ("svg", "image/svg+xml")])


def docx_media_emf(inbox):
    emf = _emf("offer for %s, stage two of the invented emf plan" % FULL)
    body = wp("stage one of the invented emf plan") + _pic_drawing("rId2")
    return make_docx(inbox, "emf-media.docx", body, parts={"word/media/image1.emf": emf},
                     rels=[("rId2", "image", "media/image1.emf", False)], defaults=[("emf", "image/x-emf")])


def docx_media_emz(inbox):
    import gzip

    emz = gzip.compress(_emf("offer for %s, stage two of the invented emz plan" % FULL))
    body = wp("stage one of the invented emz plan") + _pic_drawing("rId2")
    return make_docx(inbox, "emz-media.docx", body, parts={"word/media/image1.emz": emz},
                     rels=[("rId2", "image", "media/image1.emz", False)], defaults=[("emz", "image/x-emz")])


def docx_ole_object(inbox):
    obj = _ole_header() + b"\x00" * 64 + ("offer for %s, stage two of the invented ole plan" % FULL).encode("cp1252")
    obj += b"\x00" * 200
    body = wp("stage one of the invented ole plan") + (
        '<w:p><w:r><w:object><v:shape id="obj1" type="#_x0000_t75" style="width:100pt;height:50pt"><v:imagedata '
        'r:id="rId3" o:title=""/></v:shape><o:OLEObject Type="Embed" ProgID="Package" ShapeID="obj1" '
        'DrawAspect="Icon" ObjectID="_1" r:id="rId2"/></w:object></w:r></w:p>')
    return make_docx(inbox, "ole.docx", body,
                     parts={"word/embeddings/oleObject1.bin": obj, "word/media/image1.png": _png()},
                     rels=[("rId2", "oleObject", "embeddings/oleObject1.bin", False),
                           ("rId3", "image", "media/image1.png", False)],
                     defaults=[("bin", "application/vnd.openxmlformats-officedocument.oleObject"), ("png", "image/png")])


def docm_vba(inbox):
    src = ("Attribute VB_Name = \"Module1\"\r\nSub AutoOpen()\r\n    MsgBox \"offer for %s, stage two of the "
           "invented macro plan\"\r\nEnd Sub\r\n" % FULL).encode("cp1252")
    vba = _ole_header() + b"\x00" * 128 + _ovba_compress(src) + b"\x00" * 256
    body = wp("stage one of the invented macro plan")
    return make_docx(inbox, "macro.docm", body, parts={"word/vbaProject.bin": vba},
                     rels=[("rId2", "vbaProject", "vbaProject.bin", False)],
                     defaults=[("bin", "application/vnd.ms-office.vbaProject")])


def docx_embedded_font(inbox):
    name = ("%s corporate, stage two of the invented font plan" % FULL).encode("utf-16-be")
    font = b"\x00\x01\x00\x00\x00\x01\x00\x10name" + struct.pack(">HHH", 0, 1, 6) + struct.pack(">HHHHHH", 3, 1, 0x409, 1, len(name), 0) + name
    font = bytes(b ^ 0x5A for b in font[:32]) + font[32:] + b"\x00" * 512
    table = (_XML + '<w:fonts %s><w:font w:name="corp font one"><w:embedRegular r:id="rId9" '
             'w:fontKey="{5A5A5A5A-5A5A-5A5A' '-5A5A-5A5A5A5A5A5A}"/></w:font></w:fonts>' % _W_NS)
    body = wp("stage one of the invented font plan")
    return make_docx(inbox, "font.docx", body,
                     parts={"word/fonts/font1.odttf": font, "word/fontTable.xml": table,
                            "word/_rels/fontTable.xml.rels": _XML + '<Relationships xmlns="http://schemas.'
                            'openxmlformats.org/package/2006/relationships"><Relationship Id="rId9" Type="%s/font" '
                            'Target="fonts/font1.odttf"/></Relationships>' % _REL_NS},
                     rels=[("rId2", "fontTable", "fontTable.xml", False)],
                     defaults=[("odttf", "application/vnd.openxmlformats-officedocument.obfuscatedFont")])


def docx_thumbnail(inbox):
    jpg = _jpeg(description="offer for %s" % FULL, comment="stage two of the invented thumbnail plan")
    body = wp("stage one of the invented thumbnail plan")
    p = make_docx(inbox, "thumb.docx", body, parts={"docProps/thumbnail.jpeg": jpg},
                  defaults=[("jpeg", "image/jpeg")])
    return p


def docx_picture_only(inbox):
    import docx

    d = docx.Document()
    png = _png({"Description": "offer for %s" % FULL, "Comment": "stage two of the invented picture plan"})
    d.add_picture(io.BytesIO(png))
    p = inbox / "picture-only.docx"
    d.save(str(p))
    return p


# --------------------------------------------------------------------------- builders: pptx and xlsx


def pptx_table(inbox):
    from pptx import Presentation
    from pptx.util import Inches

    prs = Presentation()
    slide = prs.slides.add_slide(prs.slide_layouts[6])
    tbl = slide.shapes.add_table(2, 2, Inches(1), Inches(1), Inches(6), Inches(2)).table
    tbl.cell(0, 0).text = "customer"
    tbl.cell(0, 1).text = FULL
    tbl.cell(1, 0).text = "stage two of the invented table plan"
    tbl.cell(1, 1).text = "IBAN %s" % IBAN
    p = inbox / "table.pptx"
    prs.save(str(p))
    return p


def pptx_master_only(inbox):
    from pptx import Presentation
    from pptx.util import Inches

    prs = Presentation()
    slide = prs.slides.add_slide(prs.slide_layouts[6])
    box = slide.shapes.add_textbox(Inches(1), Inches(1), Inches(6), Inches(1))
    box.text_frame.text = "stage one of the invented master plan"
    corner = slide.shapes.add_textbox(Inches(1), Inches(6), Inches(6), Inches(1))
    corner.text_frame.text = "%s, stage two of the invented master plan, IBAN %s" % (FULL, IBAN)
    el = corner._element
    el.getparent().remove(el)
    prs.slide_master.shapes._spTree.append(el)
    p = inbox / "master.pptx"
    prs.save(str(p))
    return p


def pptx_chart(inbox):
    from pptx import Presentation
    from pptx.chart.data import CategoryChartData
    from pptx.enum.chart import XL_CHART_TYPE
    from pptx.util import Inches

    prs = Presentation()
    slide = prs.slides.add_slide(prs.slide_layouts[6])
    box = slide.shapes.add_textbox(Inches(1), Inches(0.5), Inches(6), Inches(1))
    box.text_frame.text = "stage one of the invented deck chart plan"
    cd = CategoryChartData()
    cd.categories = ["north", "south"]
    cd.add_series("volume", (3, 4))
    gf = slide.shapes.add_chart(XL_CHART_TYPE.COLUMN_CLUSTERED, Inches(1), Inches(2), Inches(6), Inches(4), cd)
    gf.chart.has_title = True
    gf.chart.chart_title.text_frame.text = "%s, stage two of the invented deck chart plan, IBAN %s" % (FULL, IBAN)
    p = inbox / "chart.pptx"
    prs.save(str(p))
    return p


def xlsx_chart(inbox):
    from openpyxl import Workbook
    from openpyxl.chart import BarChart, Reference

    wb = Workbook()
    ws = wb.active
    ws.append(["region", "volume"])
    ws.append(["north", 3])
    ws.append(["south", 4])
    ws["D1"] = "stage one of the invented sheet chart plan"
    ch = BarChart()
    ch.title = "%s, stage two of the invented sheet chart plan, IBAN %s" % (FULL, IBAN)
    ch.add_data(Reference(ws, min_col=2, min_row=1, max_row=3), titles_from_data=True)
    ws.add_chart(ch, "E5")
    p = inbox / "chart.xlsx"
    wb.save(str(p))
    return p


def xlsx_validation_prompt(inbox):
    from openpyxl import Workbook
    from openpyxl.worksheet.datavalidation import DataValidation

    wb = Workbook()
    ws = wb.active
    ws["A1"] = "stage one of the invented prompt plan"
    dv = DataValidation(type="list", formula1='"yes,no"', allow_blank=True, showInputMessage=True,
                        showErrorMessage=True)
    dv.promptTitle = "customer"
    dv.prompt = "%s, stage two of the invented prompt plan, IBAN %s" % (FULL, IBAN)
    dv.errorTitle = "wrong"
    dv.error = "call %s" % PHONE
    ws.add_data_validation(dv)
    dv.add("A2")
    p = inbox / "prompt.xlsx"
    wb.save(str(p))
    return p


# --------------------------------------------------------------------------- builders: image files


def jpg_metadata(inbox):
    return _w(inbox, "photo.jpg", _jpeg(description="offer for %s" % FULL, artist=PERSON,
                                        comment="stage two of the invented photo plan",
                                        xmp="visit %s, IBAN %s" % (ENGLISH, IBAN), iptc="site plan of %s" % SHORT))


def png_text(inbox):
    return _w(inbox, "diagram.png", _png({"Description": "network plan of %s" % FULL},
                                         {"Comment": "stage two of the invented png plan, IBAN %s" % IBAN}))


def tiff_pages(inbox):
    from PIL import Image

    a = Image.new("RGB", (40, 30), (10, 200, 10))
    b = Image.new("RGB", (40, 30), (200, 200, 10))
    p = inbox / "scan.tiff"
    a.save(str(p), "TIFF", save_all=True, append_images=[b],
           description="scan of the offer for %s, stage two of the invented tiff plan" % FULL)
    return p


def webp_exif(inbox):
    from PIL import Image

    img = Image.new("RGB", (40, 30), (10, 10, 10))
    exif = Image.Exif()
    exif[0x010E] = "offer for %s, stage two of the invented webp plan" % FULL
    p = inbox / "logo.webp"
    img.save(str(p), "WEBP", exif=exif.tobytes())
    return p


def gif_comment(inbox):
    from PIL import Image

    img = Image.new("P", (40, 30))
    p = inbox / "anim.gif"
    img.save(str(p), "GIF", comment=("offer for %s, stage two of the invented gif plan" % FULL).encode("utf-8"))
    return p


def heic_file(inbox):
    ftyp = b"ftypheic\x00\x00\x00\x00mif1heic"
    ftyp = struct.pack(">I", len(ftyp) + 4) + ftyp
    text = ("offer for %s, stage two of the invented heic plan" % FULL).encode("utf-8")
    meta = b"meta\x00\x00\x00\x00" + b"\x00\x00\x00\x21hdlr\x00\x00\x00\x00\x00\x00\x00\x00pict" + b"\x00" * 13
    meta += b"\x00\x00\x00\x00" + struct.pack(">I", len(text) + 8) + b"udta" + text
    meta = struct.pack(">I", len(meta) + 4) + meta
    mdat = b"mdat" + bytes(range(256)) * 8
    mdat = struct.pack(">I", len(mdat) + 4) + mdat
    return _w(inbox, "photo.heic", ftyp + meta + mdat)


# --------------------------------------------------------------------------- builders: containers with pictures


def zip_with_jpg(inbox):
    jpg = _jpeg(description="offer for %s" % FULL, comment="stage two of the invented zipped photo plan")
    return _zip(inbox, "photos.zip", {"readme.txt": "stage one of the invented zipped photo plan\n", "site.jpg": jpg})


def eml_with_jpg(inbox):
    msg = email.message.EmailMessage()
    msg["From"] = "alice@example.org"
    msg["To"] = "bob@example.org"
    msg["Subject"] = "photo"
    msg.set_content("stage one of the invented mailed photo plan\n")
    jpg = _jpeg(description="offer for %s" % FULL, comment="stage two of the invented mailed photo plan")
    msg.add_attachment(jpg, maintype="image", subtype="jpeg", filename="site.jpg")
    return _w(inbox, "photo.eml", msg.as_bytes())


# --------------------------------------------------------------------------- builders: formats without a reader


def mp3_id3(inbox):
    def frame(fid, text):
        body = b"\x00" + text.encode("latin-1")
        return fid + struct.pack(">I", len(body)) + b"\x00\x00" + body

    frames = frame(b"TIT2", "offer for %s" % FULL) + frame(b"TPE1", "stage two of the invented audio plan")
    size = len(frames)
    sync = bytes([(size >> 21) & 0x7F, (size >> 14) & 0x7F, (size >> 7) & 0x7F, size & 0x7F])
    audio = (b"\xff\xfb\x90\x00" + bytes(range(0, 256, 3)) * 20)
    return _w(inbox, "memo.mp3", b"ID3\x03\x00\x00" + sync + frames + audio)


def onenote_file(inbox):
    magic = bytes.fromhex("e4525c7b8cd8a74d" "aeb15378d02996d3")
    body = ("offer for %s, stage two of the invented notebook plan" % FULL).encode("utf-16-le")
    return _w(inbox, "notes.one", magic + b"\x00" * 240 + bytes(range(64)) + body + b"\x00" * 300)


def pst_file(inbox):
    body = ("offer for %s, stage two of the invented pst plan" % FULL).encode("utf-16-le")
    return _w(inbox, "mail.pst", b"!BDN" + b"\x00" * 6 + b"\x17\x00" + bytes(range(200)) + body + b"\x00" * 300)


def parquet_file(inbox):
    text = ("offer for %s, stage two of the invented parquet plan" % FULL).encode("utf-8")
    page = struct.pack("<I", len(text)) + text
    footer = b"\x15\x02\x19\x3c\x15\x00\x15\x02\x00\x15\x0c\x25\x02\x18\x04body\x25\x00" + bytes(range(0, 60))
    return _w(inbox, "docs.parquet", b"PAR1" + page + footer + struct.pack("<I", len(footer)) + b"PAR1")


def sqlite_file(inbox):
    p = inbox / "crm.sqlite"
    con = sqlite3.connect(str(p))
    con.execute("create table customers (id integer, name text, note text)")
    con.execute("insert into customers values (1, ?, ?)", (FULL, "stage two of the invented database plan"))
    con.commit()
    con.close()
    return p


def doc_ole(inbox):
    text = ("offer for %s, stage two of the invented legacy word plan" % FULL).encode("cp1252")
    body = _ole_header() + b"\x00" * 512 + b"\xec\xa5\xc1\x00" + b"\x00" * 60 + text + b"\x00" * 512
    body += ("offer for %s" % FULL).encode("utf-16-le") + b"\x00" * 512
    return _w(inbox, "offer.doc", body)


def xlsb_file(inbox):
    strings = ("offer for %s, stage two of the invented binary sheet plan" % FULL).encode("utf-16-le")
    sst = b"\x9f\x01\x08\x01\x00\x00\x00\x01\x00\x00\x00\x13\x2a\x00" + struct.pack("<I", len(strings) // 2) + strings
    ct = (_XML + '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
          '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
          '<Default Extension="bin" ContentType="application/vnd.ms-excel.sheet.binary.macroEnabled.main"/>'
          '<Override PartName="/xl/workbook.bin" ContentType="application/vnd.ms-excel.sheet.binary.macroEnabled.main"/>'
          '<Override PartName="/xl/worksheets/sheet1.bin" ContentType="application/vnd.ms-excel.worksheet"/>'
          '<Override PartName="/xl/sharedStrings.bin" ContentType="application/vnd.ms-excel.sharedStrings"/></Types>')
    rels = (_XML + '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            '<Relationship Id="rId1" Type="%s/officeDocument" Target="xl/workbook.bin"/></Relationships>' % _REL_NS)
    wb_rels = (_XML + '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
               '<Relationship Id="rId1" Type="%s/worksheet" Target="worksheets/sheet1.bin"/>'
               '<Relationship Id="rId2" Type="%s/sharedStrings" Target="sharedStrings.bin"/></Relationships>'
               % (_REL_NS, _REL_NS))
    workbook = b"\x83\x01\x00\x80\x01\x00\x99\x01\x0c\x00" + bytes(range(12)) + b"\x84\x01\x00"
    sheet = b"\x81\x01\x00\x91\x01\x14\x00" + b"\x00" * 20 + b"\x92\x01\x08\x00" + b"\x00" * 8 + b"\x07\x0c\x00" + b"\x00" * 12 + b"\x82\x01\x00"
    return _zip(inbox, "sheet.xlsb", {"[Content_Types].xml": ct, "_rels/.rels": rels, "xl/_rels/workbook.bin.rels": wb_rels,
                                      "xl/workbook.bin": workbook, "xl/worksheets/sheet1.bin": sheet,
                                      "xl/sharedStrings.bin": sst})


def pages_iwork(inbox):
    text = ("offer for %s, stage two of the invented pages plan" % FULL).encode("utf-8")
    # snappy framed: stream identifier is absent in iWork; one compressed chunk with literal tokens
    literal = b"\xf0" + bytes([len(text) - 1]) + text if len(text) <= 256 else b""
    snappy = bytes([len(text)]) + literal
    iwa = b"\x00" + struct.pack("<I", len(snappy))[:3] + snappy
    header = b"\x08\x01\x12\x0e\x08\x01\x10\x00" + bytes(range(8))
    plist = b"bplist00" + b"\xd1\x01\x02" + b"\x5aBuildVersion" + b"\x50" + b"\x08\x0b\x18" + b"\x00" * 9
    from PIL import Image

    buf = io.BytesIO()
    Image.new("RGB", (40, 30), (200, 200, 200)).save(buf, "JPEG")
    return _zip(inbox, "offer.pages", {"Index/Document.iwa": header + iwa, "Index/Metadata.iwa": iwa,
                                       "Metadata/Properties.plist": plist, "preview.jpg": buf.getvalue()})


# --------------------------------------------------------------------------- builders: archive fallbacks


def odg_drawing(inbox):
    content = (_XML + '<office:document-content xmlns:office="urn:oasis:names:tc:opendocument:xmlns:office:1.0" '
               'xmlns:draw="urn:oasis:names:tc:opendocument:xmlns:drawing:1.0" '
               'xmlns:text="urn:oasis:names:tc:opendocument:xmlns:text:1.0" '
               'xmlns:svg="urn:oasis:names:tc:opendocument:xmlns:svg-compatible:1.0" office:version="1.3">'
               '<office:body><office:drawing><draw:page draw:name="page1"><draw:custom-shape svg:width="8cm" '
               'svg:height="2cm"><text:p>offer for %s</text:p></draw:custom-shape><draw:custom-shape svg:width="8cm" '
               'svg:height="2cm"><text:p>stage two of the invented drawing plan, IBAN %s</text:p></draw:custom-shape>'
               '</draw:page></office:drawing></office:body></office:document-content>' % (FULL, IBAN))
    return _zip(inbox, "network.odg", {"content.xml": content, "META-INF/manifest.xml": _XML + "<manifest/>"},
                first_stored=("mimetype", b"application/vnd.oasis.opendocument.graphics"))


def vsdx_file(inbox):
    ct = (_XML + '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
          '<Default Extension="xml" ContentType="application/xml"/>'
          '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
          '<Override PartName="/visio/document.xml" ContentType="application/vnd.ms-visio.drawing.main+xml"/>'
          '<Override PartName="/visio/pages/page1.xml" ContentType="application/vnd.ms-visio.page+xml"/></Types>')
    rels = (_XML + '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            '<Relationship Id="rId1" Type="http://schemas.microsoft.com/visio/2010/relationships/document" '
            'Target="visio/document.xml"/></Relationships>')
    doc = _XML + '<VisioDocument xmlns="http://schemas.microsoft.com/office/visio/2012/main"><DocumentSettings/></VisioDocument>'
    page = (_XML + '<PageContents xmlns="http://schemas.microsoft.com/office/visio/2012/main"><Shapes>'
            '<Shape ID="1" Type="Shape"><Text><cp IX="0"/>offer for %s</Text></Shape>'
            '<Shape ID="2" Type="Shape"><Text>stage two of the invented visio plan, IBAN %s</Text></Shape>'
            '</Shapes></PageContents>' % (FULL, IBAN))
    return _zip(inbox, "network.vsdx", {"[Content_Types].xml": ct, "_rels/.rels": rels, "visio/document.xml": doc,
                                        "visio/pages/page1.xml": page})


def epub_file(inbox):
    container = (_XML + '<container version="1.0" xmlns="urn:oasis:names:tc:opendocument:xmlns:container">'
                 '<rootfiles><rootfile full-path="OEBPS/content.opf" media-type="application/oebps-package+xml"/>'
                 '</rootfiles></container>')
    opf = (_XML + '<package xmlns="http://www.idpf.org/2007/opf" version="3.0" unique-identifier="id"><metadata '
           'xmlns:dc="http://purl.org/dc/elements/1.1/"><dc:title>handbook</dc:title><dc:identifier id="id">x1'
           '</dc:identifier></metadata><manifest><item id="c1" href="ch1.xhtml" media-type="application/xhtml+xml"/>'
           '</manifest><spine><itemref idref="c1"/></spine></package>')
    ch = ('<?xml version="1.0" encoding="UTF-8"?>\n<html xmlns="http://www.w3.org/1999/xhtml"><head><title>one</title>'
          '</head><body><p>offer for %s</p><p>stage two of the invented book plan, IBAN %s</p></body></html>' % (FULL, IBAN))
    return _zip(inbox, "handbook.epub", {"META-INF/container.xml": container, "OEBPS/content.opf": opf,
                                         "OEBPS/ch1.xhtml": ch}, first_stored=("mimetype", b"application/epub+zip"))


# --------------------------------------------------------------------------- builders: text-like carriers


def ics_file(inbox):
    text = ("BEGIN:VCALENDAR\r\nVERSION:2.0\r\nPRODID:-//invented//calendar//EN\r\nBEGIN:VEVENT\r\n"
            "UID:20260921-1@example.org\r\nDTSTAMP:20260921T100000Z\r\nDTSTART:20260922T090000Z\r\n"
            "SUMMARY:review of the offer for %s\r\nDESCRIPTION:stage two of the invented calendar plan\r\n"
            "LOCATION:%s\r\nEND:VEVENT\r\nEND:VCALENDAR\r\n" % (FULL, fx.PLACE_FORMS[0]))
    return _w(inbox, "review.ics", text)


def vcf_qp(inbox):
    def qp(s):
        return "".join("=%02X" % b for b in s.encode("utf-8"))

    photo = base64.b64encode(_jpeg()).decode("ascii")
    photo_lines = "\r\n ".join(photo[i:i + 72] for i in range(0, len(photo), 72))
    text = ("BEGIN:VCARD\r\nVERSION:2.1\r\nN;ENCODING=QUOTED-PRINTABLE;CHARSET=UTF-8:%s;%s\r\n"
            "FN;ENCODING=QUOTED-PRINTABLE;CHARSET=UTF-8:%s\r\nORG;ENCODING=QUOTED-PRINTABLE;CHARSET=UTF-8:%s\r\n"
            "NOTE:stage two of the invented card plan\r\nTEL;WORK:%s\r\nPHOTO;ENCODING=BASE64;TYPE=JPEG:%s\r\n\r\n"
            "END:VCARD\r\n" % (qp(fx.PERSON_FORMS[1]), qp(PERSON.split()[0]), qp(PERSON), qp(FULL), PHONE, photo_lines))
    return _w(inbox, "contact.vcf", text)


def sql_bytea(inbox):
    payload = ("offer for %s, stage three of the invented dump plan" % FULL).encode("utf-8")
    text = ("--\n-- PostgreSQL database dump\n--\nCREATE TABLE public.docs (id integer, body bytea);\n"
            "CREATE TABLE public.notes (id integer, note text);\n"
            "INSERT INTO public.notes VALUES (1, 'stage two of the invented dump plan');\n"
            "COPY public.docs (id, body) FROM stdin;\n1\t\\x%s\n\\.\n" % _hex(payload))
    return _w(inbox, "dump.sql", text)


def sql_mysql_hex(inbox):
    payload = ("offer for %s, stage three of the invented mysql plan" % FULL).encode("utf-8")
    text = ("-- MySQL dump\nCREATE TABLE `docs` (`id` int, `body` blob);\n"
            "INSERT INTO `notes` VALUES (1,'stage two of the invented mysql plan');\n"
            "INSERT INTO `docs` VALUES (1,0x%s);\n" % _hex(payload).upper())
    return _w(inbox, "dump-mysql.sql", text)


def rtf_objdata(inbox):
    inner = ("offer for %s, stage three of the invented rtf object plan\r\n" % FULL).encode("cp1252")
    obj = _ole1_package("notes.txt", inner)
    text = ("{\\rtf1\\ansi\\deff0{\\fonttbl{\\f0 Arial;}}\n\\pard stage one of the invented rtf object plan\\par\n"
            "stage two of the invented rtf object plan\\par\n"
            "{\\object\\objemb\\objw2000\\objh600{\\*\\objclass Package}{\\*\\objdata %s}"
            "{\\result{\\pict\\wmetafile8\\picw2000\\pich600 0100090000030000000000000000}}}\\par\n}\n" % _hex(obj))
    return _w(inbox, "embedded.rtf", text)


def rtf_pict_emf(inbox):
    emf = _emf("offer for %s, stage three of the invented rtf picture plan" % FULL)
    text = ("{\\rtf1\\ansi\\deff0{\\fonttbl{\\f0 Arial;}}\n\\pard stage one of the invented rtf picture plan\\par\n"
            "stage two of the invented rtf picture plan\\par\n"
            "{\\pict\\emfblip\\picw2000\\pich400\\picwgoal2000\\pichgoal400 %s}\\par\n}\n" % _hex(emf))
    return _w(inbox, "picture.rtf", text)


def mbox_two(inbox):
    text = ("From alice@example.org Mon Sep 21 10:00:00 2026\nFrom: alice@example.org\nTo: bob@example.org\n"
            "Subject: first\nDate: Mon, 21 Sep 2026 10:00:00 +0000\nMIME-Version: 1.0\n"
            "Content-Type: multipart/alternative; boundary=\"b1\"\n\n--b1\nContent-Type: text/plain; charset=utf-8\n\n"
            "stage one of the invented mailbox plan\n--b1\nContent-Type: text/html; charset=utf-8\n\n"
            "<html><body><p>stage one of the invented mailbox plan</p></body></html>\n--b1--\n\n"
            "From alice@example.org Mon Sep 21 11:00:00 2026\nFrom: alice@example.org\nTo: bob@example.org\n"
            "Subject: second\nDate: Mon, 21 Sep 2026 11:00:00 +0000\n\n"
            "stage two of the invented mailbox plan, offer for %s, IBAN %s\n\n" % (FULL, IBAN))
    return _w(inbox, "inbox.mbox", text)


# --------------------------------------------------------------------------- builders: sniffing


def pdf_text_prefix(inbox):
    content = ("BT /F1 12 Tf 72 700 Td [<5A7978776F> 40 <204C6F67697374696B20476D6248>] TJ ET\n"
               "BT /F1 12 Tf 72 680 Td (stage two of the invented prefix plan) Tj ET\n")
    return _w(inbox, "offer.pdf", _pdf(content, prefix=b"X-Generated-By: the invented tool\n\n"))


def pdf_no_prefix_control(inbox):
    content = ("BT /F1 12 Tf 72 700 Td [<5A7978776F> 40 <204C6F67697374696B20476D6248>] TJ ET\n"
               "BT /F1 12 Tf 72 680 Td (stage two of the invented control plan) Tj ET\n")
    return _w(inbox, "control.pdf", _pdf(content))


def pdf_compressed_prefix(inbox):
    data = _reportlab_pdf(["offer for %s" % FULL, "stage two of the invented compressed plan"], compress=1)
    return _w(inbox, "report.pdf", b"X-Generated-By: the invented tool\n\n" + data)


def pdf_zip_polyglot(inbox):
    pdf = _reportlab_pdf(["stage one of the invented polyglot plan"], compress=0)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("notes.txt", "offer for %s, stage two of the invented polyglot plan, IBAN %s\n" % (FULL, IBAN))
    return _w(inbox, "bundle.pdf", pdf + buf.getvalue())


def docx_renamed_zip(inbox):
    import docx

    d = docx.Document()
    d.add_paragraph("offer for %s, stage two of the invented rename plan" % FULL)
    p = inbox / "offer.zip"
    d.save(str(p))
    return p


def zip_renamed_docx(inbox):
    return _zip(inbox, "offer.docx", {"notes.txt": "offer for %s, stage two of the invented rename plan\n" % FULL})


def zip_xl_folder(inbox):
    return _zip(inbox, "exports.zip", {"xl/customers.csv": "name;note\n%s;stage two of the invented folder plan\n" % FULL,
                                       "xl/readme.txt": "stage one of the invented folder plan\n"})


def html_renamed_txt_late(inbox):
    comment = "<!-- " + ("generated block " * 400) + "-->\n"
    text = comment + "<html><body><p>offer for %s</p><p>stage two of the invented late html plan, IBAN %s</p></body></html>\n" % (FULL, IBAN)
    return _w(inbox, "notes.txt", text)


# --------------------------------------------------------------------------- cases

CASES = [
    # office parts no reader renders
    {"id": "docx-chart", "cls": "name", "carrier": "docx chart title, series name and data label (word/charts)",
     "values": [FULL, IBAN, PHONE], "visible": ["stage two of the invented chart plan"], "build": docx_chart},
    {"id": "docx-smartart", "cls": "name", "carrier": "docx SmartArt text (word/diagrams/data1.xml)",
     "values": [FULL, IBAN], "visible": ["stage two of the invented smartart plan"], "build": docx_smartart},
    {"id": "docx-altchunk", "cls": "name", "carrier": "docx altChunk html part rendered inline by Word",
     "values": [FULL, IBAN, PHONE], "visible": ["stage two of the invented chunk plan"], "build": docx_altchunk},
    {"id": "docx-wordart-vml", "cls": "name", "carrier": "docx legacy WordArt, v:textpath string attribute",
     "values": [FULL, IBAN], "visible": ["stage two of the invented wordart plan"], "build": docx_wordart_vml},
    {"id": "docx-textbox", "cls": "name", "carrier": "docx text box (wps:txbx in mc:Choice)",
     "values": [FULL, IBAN], "visible": ["stage two of the invented textbox plan"], "build": docx_textbox},
    {"id": "docx-omml-inline", "cls": "name", "carrier": "docx OMML equation inside a paragraph (m:t)",
     "values": [FULL], "visible": ["stage two of the invented equation plan"], "build": docx_omml_inline},
    {"id": "docx-omml-block", "cls": "name", "carrier": "docx OMML equation at body level (m:oMathPara under w:body)",
     "values": [FULL, IBAN], "visible": ["stage two of the invented equation plan"], "build": docx_omml_block},
    {"id": "docx-media-svg", "cls": "name", "carrier": "docx svg media text nodes", "values": [FULL, IBAN],
     "visible": ["stage two of the invented svg plan"], "build": docx_media_svg},
    {"id": "docx-media-emf", "cls": "name", "carrier": "docx emf media EXTTEXTOUTW record", "values": [FULL],
     "visible": ["stage two of the invented emf plan"], "build": docx_media_emf},
    {"id": "docx-media-emz", "cls": "name", "carrier": "docx emz (gzip emf) media text record", "values": [FULL],
     "visible": ["stage two of the invented emz plan"], "build": docx_media_emz},
    {"id": "docx-ole-object", "cls": "name", "carrier": "docx OLE object (word/embeddings/oleObject1.bin)",
     "values": [FULL], "visible": ["stage two of the invented ole plan"], "build": docx_ole_object},
    {"id": "docm-vba", "cls": "name", "carrier": "docm vbaProject.bin, MS-OVBA compressed module source",
     "values": [FULL], "visible": ["stage two of the invented macro plan"], "build": docm_vba},
    {"id": "docx-embedded-font", "cls": "name", "carrier": "docx embedded font name table (word/fonts/font1.odttf)",
     "values": [FULL], "visible": ["stage two of the invented font plan"], "build": docx_embedded_font},
    {"id": "docx-thumbnail", "cls": "name", "carrier": "docx docProps/thumbnail.jpeg EXIF and comment",
     "values": [FULL], "visible": ["stage two of the invented thumbnail plan"], "build": docx_thumbnail},
    {"id": "docx-picture-only", "cls": "name", "carrier": "docx whose body is one picture (png text chunks)",
     "values": [FULL], "visible": ["stage two of the invented picture plan"], "build": docx_picture_only},
    # pptx and xlsx
    {"id": "pptx-table", "cls": "name", "carrier": "pptx table cells", "values": [FULL, IBAN],
     "visible": ["stage two of the invented table plan"], "build": pptx_table},
    {"id": "pptx-master-only", "cls": "name", "carrier": "pptx text box on the slide master only",
     "values": [FULL, IBAN], "visible": ["stage two of the invented master plan"], "build": pptx_master_only},
    {"id": "pptx-chart", "cls": "name", "carrier": "pptx chart title (ppt/charts)", "values": [FULL, IBAN],
     "visible": ["stage two of the invented deck chart plan"], "build": pptx_chart},
    {"id": "xlsx-chart", "cls": "name", "carrier": "xlsx chart title (xl/charts)", "values": [FULL, IBAN],
     "visible": ["stage two of the invented sheet chart plan"], "build": xlsx_chart},
    {"id": "xlsx-validation-prompt", "cls": "name", "carrier": "xlsx data validation prompt and error (attributes)",
     "values": [FULL, IBAN, PHONE], "visible": ["stage two of the invented prompt plan"], "build": xlsx_validation_prompt},
    # image files with metadata
    {"id": "jpg-metadata", "cls": "name", "carrier": "jpg EXIF, XMP, IPTC and COM", "values": [FULL, PERSON, ENGLISH, IBAN, SHORT],
     "visible": ["stage two of the invented photo plan"], "build": jpg_metadata},
    {"id": "png-text", "cls": "name", "carrier": "png tEXt and iTXt chunks", "values": [FULL, IBAN],
     "visible": ["stage two of the invented png plan"], "build": png_text},
    {"id": "tiff-pages", "cls": "name", "carrier": "tiff with two pages, ImageDescription", "values": [FULL],
     "visible": ["stage two of the invented tiff plan"], "build": tiff_pages},
    {"id": "webp-exif", "cls": "name", "carrier": "webp EXIF chunk", "values": [FULL],
     "visible": ["stage two of the invented webp plan"], "build": webp_exif},
    {"id": "gif-comment", "cls": "name", "carrier": "gif comment extension", "values": [FULL],
     "visible": ["stage two of the invented gif plan"], "build": gif_comment},
    {"id": "heic", "cls": "name", "carrier": "heic (ftyp box) with a udta text box", "values": [FULL],
     "visible": ["stage two of the invented heic plan"], "build": heic_file},
    # containers with pictures
    {"id": "zip-with-jpg", "cls": "name", "carrier": "jpg EXIF inside a zip", "values": [FULL],
     "visible": ["stage two of the invented zipped photo plan"], "build": zip_with_jpg},
    {"id": "eml-with-jpg", "cls": "name", "carrier": "jpg EXIF attached to an eml", "values": [FULL],
     "visible": ["stage two of the invented mailed photo plan"], "build": eml_with_jpg},
    # formats without a reader
    {"id": "mp3-id3", "cls": "name", "carrier": "mp3 ID3v2 TIT2 and TPE1 frames", "values": [FULL],
     "visible": ["stage two of the invented audio plan"], "build": mp3_id3},
    {"id": "onenote", "cls": "name", "carrier": "OneNote .one, UTF-16 body", "values": [FULL],
     "visible": ["stage two of the invented notebook plan"], "build": onenote_file},
    {"id": "pst", "cls": "name", "carrier": "Outlook .pst, UTF-16 body", "values": [FULL],
     "visible": ["stage two of the invented pst plan"], "build": pst_file},
    {"id": "parquet", "cls": "name", "carrier": "parquet plain page", "values": [FULL],
     "visible": ["stage two of the invented parquet plan"], "build": parquet_file},
    {"id": "sqlite", "cls": "name", "carrier": "sqlite table row", "values": [FULL],
     "visible": ["stage two of the invented database plan"], "build": sqlite_file},
    {"id": "doc-ole", "cls": "name", "carrier": ".doc OLE, cp1252 and UTF-16 body", "values": [FULL],
     "visible": ["stage two of the invented legacy word plan"], "build": doc_ole},
    {"id": "xlsb", "cls": "name", "carrier": "xlsb sharedStrings.bin UTF-16", "values": [FULL],
     "visible": ["stage two of the invented binary sheet plan"], "build": xlsb_file},
    {"id": "pages-iwork", "cls": "name", "carrier": "Apple .pages Index/Document.iwa snappy literal", "values": [FULL],
     "visible": ["stage two of the invented pages plan"], "build": pages_iwork},
    # archive fallbacks
    {"id": "odg", "cls": "name", "carrier": "odg content.xml via the archive fallback", "values": [FULL, IBAN],
     "visible": ["stage two of the invented drawing plan"], "build": odg_drawing},
    {"id": "vsdx", "cls": "name", "carrier": "vsdx visio/pages/page1.xml via the archive fallback", "values": [FULL, IBAN],
     "visible": ["stage two of the invented visio plan"], "build": vsdx_file},
    {"id": "epub", "cls": "name", "carrier": "epub xhtml chapter via the archive fallback", "values": [FULL, IBAN],
     "visible": ["stage two of the invented book plan"], "build": epub_file},
    # text-like carriers
    {"id": "ics", "cls": "name", "carrier": "ics SUMMARY and LOCATION", "values": [FULL, fx.PLACE_FORMS[0]],
     "visible": ["stage two of the invented calendar plan"], "build": ics_file},
    {"id": "vcf-qp", "cls": "name", "carrier": "vcf 2.1 quoted-printable N, FN, ORG and a base64 PHOTO",
     "values": [FULL, PERSON, PHONE], "visible": ["stage two of the invented card plan"], "build": vcf_qp},
    {"id": "sql-bytea-hex", "cls": "name", "carrier": "sql dump bytea \\x hex literal", "values": [FULL],
     "visible": ["stage two of the invented dump plan"], "build": sql_bytea},
    {"id": "sql-mysql-hex", "cls": "name", "carrier": "sql dump 0x hex literal", "values": [FULL],
     "visible": ["stage two of the invented mysql plan"], "build": sql_mysql_hex},
    {"id": "rtf-objdata", "cls": "name", "carrier": "rtf embedded OLE Package (\\objdata hex) holding a text file",
     "values": [FULL], "visible": ["stage two of the invented rtf object plan"], "build": rtf_objdata},
    {"id": "rtf-pict-emf", "cls": "name", "carrier": "rtf \\pict emfblip hex with a UTF-16 text record",
     "values": [FULL], "visible": ["stage two of the invented rtf picture plan"], "build": rtf_pict_emf},
    {"id": "mbox-two", "cls": "name", "carrier": "mbox second message after a multipart first one (MIME epilogue)",
     "values": [FULL, IBAN], "visible": ["stage two of the invented mailbox plan"], "build": mbox_two},
    # sniffing
    {"id": "pdf-text-prefix", "cls": "name", "carrier": "pdf with a text prefix before %PDF, hex string chunks",
     "values": [SHORT, FULL], "visible": ["stage two of the invented prefix plan"], "build": pdf_text_prefix},
    {"id": "pdf-no-prefix-control", "cls": "name", "carrier": "the same pdf without the prefix (control)",
     "values": [SHORT, FULL], "visible": ["stage two of the invented control plan"], "build": pdf_no_prefix_control},
    {"id": "pdf-compressed-prefix", "cls": "name", "carrier": "compressed pdf with a text prefix before %PDF",
     "values": [FULL], "visible": ["stage two of the invented compressed plan"], "build": pdf_compressed_prefix},
    {"id": "pdf-zip-polyglot", "cls": "name", "carrier": "pdf with a zip appended (polyglot), text in the zip member",
     "values": [FULL, IBAN], "visible": ["stage two of the invented polyglot plan"], "build": pdf_zip_polyglot},
    {"id": "docx-renamed-zip", "cls": "name", "carrier": "docx renamed .zip", "values": [FULL],
     "visible": ["stage two of the invented rename plan"], "build": docx_renamed_zip},
    {"id": "zip-renamed-docx", "cls": "name", "carrier": "plain zip renamed .docx", "values": [FULL],
     "visible": ["stage two of the invented rename plan"], "build": zip_renamed_docx},
    {"id": "zip-xl-folder", "cls": "name", "carrier": "plain zip with an xl/ folder of csv files (sniffed as xlsx)",
     "values": [FULL], "visible": ["stage two of the invented folder plan"], "build": zip_xl_folder},
    {"id": "html-renamed-txt-late", "cls": "name", "carrier": "html renamed .txt with the html tag after 5 KB of comment",
     "values": [FULL, IBAN], "visible": ["stage two of the invented late html plan"], "build": html_renamed_txt_late},
]


# =========================================================================== round 2: mutations and boundaries


def svg_file_text(inbox):
    return _w(inbox, "plan.svg", _SVG % ("offer for %s" % FULL, "stage two of the invented svg file plan, IBAN %s" % IBAN))


def svg_file_logo(inbox):
    paths = "".join('<path d="M%d %d l 10 0 l 0 10 z" fill="#c33"/>' % (10 * i, 5 * i) for i in range(60))
    svg = ('<?xml version="1.0" encoding="UTF-8"?>\n<svg xmlns="http://www.w3.org/2000/svg" width="400" height="120">'
           '<desc>stage two of the invented logo plan</desc>%s</svg>' % paths)
    return _w(inbox, "logo.svg", svg)


def emf_file(inbox):
    return _w(inbox, "plan.emf", _emf("offer for %s, stage two of the invented emf file plan" % FULL))


def wmf_file(inbox):
    text = ("offer for %s, stage two of the invented wmf file plan" % FULL).encode("cp1252")
    placeable = b"\xd7\xcd\xc6\x9a\x00\x00" + struct.pack("<hhhh", 0, 0, 2000, 400) + struct.pack("<H", 1440) + b"\x00" * 4 + b"\x00\x00"
    header = struct.pack("<HHHIHIH", 1, 9, 0x300, 0, 0, 0, 0)
    rec = struct.pack("<IH", 0, 0x0A32) + struct.pack("<hhhh", 10, 10, len(text), 0) + text + (b"\x00" if len(text) % 2 else b"")
    rec = struct.pack("<I", len(rec) // 2) + rec[4:]
    eof = struct.pack("<IH", 3, 0)
    return _w(inbox, "plan.wmf", placeable + header + rec + eof)


def docx_media_svg_late(inbox):
    filler = "".join('<rect x="%d" y="1" width="1" height="1" fill="#ddd"/>' % i for i in range(1500))
    svg = ('<?xml version="1.0" encoding="UTF-8"?>\n<svg xmlns="http://www.w3.org/2000/svg" width="400" height="120">'
           '%s<text x="10" y="40">offer for %s</text><text x="10" y="80">stage two of the invented late svg plan</text>'
           '</svg>' % (filler, FULL))
    assert svg.find(FULL) > 70000
    body = wp("stage one of the invented late svg plan") + _pic_drawing("rId2", "rId3")
    return make_docx(inbox, "svg-late.docx", body,
                     parts={"word/media/image1.png": _png(), "word/media/image2.svg": svg},
                     rels=[("rId2", "image", "media/image1.png", False), ("rId3", "image", "media/image2.svg", False)],
                     defaults=[("png", "image/png"), ("svg", "image/svg+xml")])


def docx_altchunk_rtf(inbox):
    chunk = ("{\\rtf1\\ansi\\deff0{\\fonttbl{\\f0 Arial;}}\\pard offer for %s\\par stage two of the invented rtf chunk "
             "plan, IBAN %s\\par}" % (FULL, IBAN))
    body = wp("stage one of the invented rtf chunk plan") + '<w:altChunk r:id="rId2"/>'
    return make_docx(inbox, "chunk-rtf.docx", body, parts={"word/afchunk.rtf": chunk},
                     rels=[("rId2", "aFChunk", "afchunk.rtf", False)], overrides=[("/word/afchunk.rtf", "application/rtf")])


def docx_altchunk_txt(inbox):
    chunk = "offer for %s\nstage two of the invented txt chunk plan, IBAN %s\n" % (FULL, IBAN)
    body = wp("stage one of the invented txt chunk plan") + '<w:altChunk r:id="rId2"/>'
    return make_docx(inbox, "chunk-txt.docx", body, parts={"word/afchunk.txt": chunk},
                     rels=[("rId2", "aFChunk", "afchunk.txt", False)], overrides=[("/word/afchunk.txt", "text/plain")])


def docx_altchunk_docx(inbox):
    import docx

    d = docx.Document()
    d.add_paragraph("offer for %s, stage two of the invented docx chunk plan, IBAN %s" % (FULL, IBAN))
    buf = io.BytesIO()
    d.save(buf)
    body = wp("stage one of the invented docx chunk plan") + '<w:altChunk r:id="rId2"/>'
    return make_docx(inbox, "chunk-docx.docx", body, parts={"word/afchunk.docx": buf.getvalue()},
                     rels=[("rId2", "aFChunk", "afchunk.docx", False)],
                     overrides=[("/word/afchunk.docx",
                                 "application/vnd.openxmlformats-officedocument.wordprocessingml.document")])


def docx_omml_cell(inbox):
    body = wp("stage one of the invented cell equation plan") + (
        '<w:tbl><w:tr><w:tc>%s</w:tc><w:tc><m:oMathPara><m:oMath><m:r><m:t>%s = stage two of the invented cell '
        'equation plan</m:t></m:r></m:oMath></m:oMathPara></w:tc></w:tr></w:tbl>' % (wp("left cell"), FULL))
    return make_docx(inbox, "omml-cell.docx", body)


def _add_zip_members(path, members):
    """Rewrite a zip with extra members (python-pptx output plus hand-made parts)."""
    buf = io.BytesIO()
    with zipfile.ZipFile(path) as src, zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as dst:
        for info in src.infolist():
            dst.writestr(info, src.read(info))
        for n, data in members.items():
            dst.writestr(n, data)
    path.write_bytes(buf.getvalue())
    return path


def pptx_smartart(inbox):
    from pptx import Presentation
    from pptx.util import Inches

    prs = Presentation()
    slide = prs.slides.add_slide(prs.slide_layouts[6])
    slide.shapes.add_textbox(Inches(1), Inches(0.5), Inches(6), Inches(1)).text_frame.text = "stage one of the invented deck smartart plan"
    p = inbox / "smartart.pptx"
    prs.save(str(p))
    data = (_XML + '<dgm:dataModel xmlns:dgm="http://schemas.openxmlformats.org/drawingml/2006/diagram" '
            'xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main"><dgm:ptLst><dgm:pt modelId="{2}">'
            '<dgm:prSet/><dgm:spPr/><dgm:t><a:bodyPr/><a:p><a:r><a:t>%s, stage two of the invented deck smartart plan, '
            'IBAN %s</a:t></a:r></a:p></dgm:t></dgm:pt></dgm:ptLst></dgm:dataModel>' % (FULL, IBAN))
    return _add_zip_members(p, {"ppt/diagrams/data1.xml": data})


def pptx_picture_only(inbox):
    from pptx import Presentation
    from pptx.util import Inches

    prs = Presentation()
    slide = prs.slides.add_slide(prs.slide_layouts[6])
    png = _png({"Description": "offer for %s" % FULL, "Comment": "stage two of the invented deck picture plan"})
    slide.shapes.add_picture(io.BytesIO(png), Inches(1), Inches(1))
    p = inbox / "picture-only.pptx"
    prs.save(str(p))
    return p


def pdf_image_only(inbox):
    from reportlab.lib.utils import ImageReader
    from reportlab.pdfgen import canvas

    png = _png({"Description": "offer for %s" % FULL, "Comment": "stage two of the invented scan plan"}, size=(400, 300))
    buf = io.BytesIO()
    c = canvas.Canvas(buf)
    c.drawImage(ImageReader(io.BytesIO(png)), 72, 400, 400, 300)
    c.showPage()
    c.save()
    return _w(inbox, "scan.pdf", buf.getvalue())


def pdf_attachment(inbox):
    """A valid PDF with a readable page and an embedded file (EmbeddedFiles name tree) holding text."""
    attached = ("offer for %s, stage two of the invented attachment plan, IBAN %s\n" % (FULL, IBAN)).encode("latin-1")
    content = b"BT /F1 12 Tf 72 700 Td (stage one of the invented attachment plan, a page with enough words on it) Tj ET"
    objs = [
        b"<< /Type /Catalog /Pages 2 0 R /Names << /EmbeddedFiles << /Names [(notes.txt) 6 0 R] >> >> >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 595 842] /Contents 4 0 R /Resources << /Font << /F1 5 0 R >> >> >>",
        b"<< /Length %d >>\nstream\n%s\nendstream" % (len(content), content),
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
        b"<< /Type /Filespec /F (notes.txt) /UF (notes.txt) /EF << /F 7 0 R >> >>",
        b"<< /Type /EmbeddedFile /Subtype /text#2Fplain /Length %d >>\nstream\n%s\nendstream" % (len(attached), attached),
    ]
    out = bytearray(b"%PDF-1.7\n")
    offsets = []
    for i, body in enumerate(objs, start=1):
        offsets.append(len(out))
        out += b"%d 0 obj\n%s\nendobj\n" % (i, body)
    xref = len(out)
    out += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objs) + 1)
    for off in offsets:
        out += b"%010d 00000 n \n" % off
    out += b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (len(objs) + 1, xref)
    return _w(inbox, "with-attachment.pdf", bytes(out))


def pdf_zip_polyglot_ok(inbox):
    pdf = _reportlab_pdf(["stage one of the invented polyglot plan, a page with enough words to count as read",
                          "second line of the invented polyglot plan"], compress=0)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("notes.txt", "offer for %s, stage two of the invented polyglot plan, IBAN %s\n" % (FULL, IBAN))
    return _w(inbox, "bundle-ok.pdf", pdf + buf.getvalue())


def pdf_ws_prefix(inbox):
    content = ("BT /F1 12 Tf 72 700 Td [<5A7978776F> 40 <204C6F67697374696B20476D6248>] TJ ET\n"
               "BT /F1 12 Tf 72 680 Td (stage two of the invented whitespace plan) Tj ET\n")
    return _w(inbox, "ws.pdf", _pdf(content, prefix=b"\r\n"))


def pdf_prefix_literal(inbox):
    content = ("BT /F1 12 Tf 72 700 Td (offer for %s) Tj ET\n"
               "BT /F1 12 Tf 72 680 Td (stage two of the invented literal plan) Tj ET\n" % FULL)
    return _w(inbox, "literal.pdf", _pdf(content, prefix=b"X-Generated-By: the invented tool\n\n"))


def pdf_prefix_long_hex(inbox):
    hexname = ("offer for %s" % FULL).encode("latin-1").hex().upper()
    content = ("BT /F1 12 Tf 72 700 Td <%s> Tj ET\nBT /F1 12 Tf 72 680 Td (stage two of the invented long hex plan) Tj ET\n"
               % hexname)
    return _w(inbox, "longhex.pdf", _pdf(content, prefix=b"X-Generated-By: the invented tool\n\n"))


def pdf_compressed_prefix_big(inbox):
    lines = ["offer for %s" % FULL, "stage two of the invented big compressed plan"]
    lines += ["line %d of the invented big compressed plan with some more words in it" % i for i in range(120)]
    return _w(inbox, "big.pdf", b"X-Generated-By: the invented tool\n\n" + _reportlab_pdf(lines, compress=1))


def rtf_objdata_lines(inbox):
    inner = ("offer for %s, stage three of the invented rtf lines plan\r\n" % FULL).encode("cp1252")
    h = _hex(_ole1_package("notes.txt", inner))
    lines = "\n".join(h[i:i + 128] for i in range(0, len(h), 128))
    text = ("{\\rtf1\\ansi\\deff0{\\fonttbl{\\f0 Arial;}}\n\\pard stage two of the invented rtf lines plan\\par\n"
            "{\\object\\objemb\\objw2000\\objh600{\\*\\objclass Package}{\\*\\objdata \n%s\n}"
            "{\\result{\\pict\\wmetafile8\\picw2000\\pich600 0100090000030000000000000000}}}\\par\n}\n" % lines)
    return _w(inbox, "embedded-lines.rtf", text)


def rtf_objdata_zip(inbox):
    import docx

    d = docx.Document()
    d.add_paragraph("offer for %s, stage two of the invented rtf package plan, IBAN %s" % (FULL, IBAN))
    buf = io.BytesIO()
    d.save(buf)
    h = _hex(_ole1_package("offer.docx", buf.getvalue()))
    lines = "\n".join(h[i:i + 128] for i in range(0, len(h), 128))
    text = ("{\\rtf1\\ansi\\deff0{\\fonttbl{\\f0 Arial;}}\n\\pard stage one of the invented rtf package plan\\par\n"
            "{\\object\\objemb\\objw2000\\objh600{\\*\\objclass Package}{\\*\\objdata \n%s\n}"
            "{\\result{\\pict\\wmetafile8\\picw2000\\pich600 0100090000030000000000000000}}}\\par\n}\n" % lines)
    return _w(inbox, "embedded-zip.rtf", text)


def sql_x_quote_hex(inbox):
    payload = ("offer for %s, stage three of the invented xquote plan" % FULL).encode("utf-8")
    text = ("INSERT INTO notes VALUES (1, 'stage two of the invented xquote plan');\n"
            "INSERT INTO docs VALUES (1, X'%s');\n" % _hex(payload).upper())
    return _w(inbox, "dump-xquote.sql", text)


def sql_bytea_lines(inbox):
    payload = ("offer for %s, stage three of the invented split plan" % FULL).encode("utf-8")
    h = _hex(payload)
    text = ("INSERT INTO notes VALUES (1, 'stage two of the invented split plan');\n"
            "COPY docs (id, body) FROM stdin;\n1\t\\x%s\n\\.\n" % h)
    return _w(inbox, "dump-split.sql", text)


def vcf_qp_umlaut(inbox):
    def qp(s):
        return "".join("=%02X" % b for b in s.encode("utf-8"))

    org = fx.ORG_FORMS[0]
    text = ("BEGIN:VCARD\r\nVERSION:2.1\r\nFN:Anna\r\nORG;ENCODING=QUOTED-PRINTABLE;CHARSET=UTF-8:%s\r\n"
            "NOTE:stage two of the invented umlaut card plan\r\nEND:VCARD\r\n" % qp(org))
    return _w(inbox, "contact-umlaut.vcf", text)


def vcf_qp_ascii_split(inbox):
    """The QP run of an ASCII name with one encoded non-ASCII byte pair glued on: the whole run decodes."""
    def qp(s):
        return "".join("=%02X" % b for b in s.encode("utf-8"))

    text = ("BEGIN:VCARD\r\nVERSION:2.1\r\nFN:Anna\r\nORG;ENCODING=QUOTED-PRINTABLE;CHARSET=UTF-8:%s=C2=A0\r\n"
            "NOTE:stage two of the invented glued card plan\r\nEND:VCARD\r\n" % qp(FULL))
    return _w(inbox, "contact-glued.vcf", text)


def vcf_qp_soft_break(inbox):
    def qp(s):
        return "".join("=%02X" % b for b in s.encode("utf-8"))

    enc = qp(FULL)
    text = ("BEGIN:VCARD\r\nVERSION:2.1\r\nFN:Anna\r\nORG;ENCODING=QUOTED-PRINTABLE;CHARSET=UTF-8:%s=\r\n%s\r\n"
            "NOTE:stage two of the invented soft card plan\r\nEND:VCARD\r\n" % (enc[:21], enc[21:]))
    return _w(inbox, "contact-soft.vcf", text)


def ics_folded(inbox):
    summary = "SUMMARY:kickoff of the invented calendar plan with the customer %s and the site team" % FULL
    folded = summary[:75] + "\r\n " + summary[75:]
    text = ("BEGIN:VCALENDAR\r\nVERSION:2.0\r\nPRODID:-//invented//calendar//EN\r\nBEGIN:VEVENT\r\n"
            "UID:20260921-2@example.org\r\nDTSTAMP:20260921T100000Z\r\nDTSTART:20260922T090000Z\r\n%s\r\n"
            "DESCRIPTION:stage two of the invented folded plan\r\nEND:VEVENT\r\nEND:VCALENDAR\r\n" % folded)
    assert FULL not in text
    return _w(inbox, "folded.ics", text)


def mbox_two_plain(inbox):
    text = ("From alice@example.org Mon Sep 21 10:00:00 2026\nFrom: alice@example.org\nTo: bob@example.org\n"
            "Subject: first\nDate: Mon, 21 Sep 2026 10:00:00 +0000\n\nstage one of the invented plain mailbox plan\n\n"
            "From alice@example.org Mon Sep 21 11:00:00 2026\nFrom: alice@example.org\nTo: bob@example.org\n"
            "Subject: second\nDate: Mon, 21 Sep 2026 11:00:00 +0000\n\n"
            "stage two of the invented plain mailbox plan, offer for %s\n\n" % FULL)
    return _w(inbox, "inbox-plain.mbox", text)


def eml_epilogue(inbox):
    text = ("From: alice@example.org\nTo: bob@example.org\nSubject: epilogue\nDate: Mon, 21 Sep 2026 10:00:00 +0000\n"
            "MIME-Version: 1.0\nContent-Type: multipart/mixed; boundary=\"b1\"\n\n--b1\nContent-Type: text/plain; charset=utf-8\n\n"
            "stage one of the invented epilogue plan\n--b1--\n\nstage two of the invented epilogue plan, offer for %s, IBAN %s\n" % (FULL, IBAN))
    return _w(inbox, "epilogue.eml", text)


def eml_preamble(inbox):
    text = ("From: alice@example.org\nTo: bob@example.org\nSubject: preamble\nDate: Mon, 21 Sep 2026 10:00:00 +0000\n"
            "MIME-Version: 1.0\nContent-Type: multipart/mixed; boundary=\"b1\"\n\n"
            "stage two of the invented preamble plan, offer for %s, IBAN %s\n\n--b1\nContent-Type: text/plain; charset=utf-8\n\n"
            "stage one of the invented preamble plan\n--b1--\n" % (FULL, IBAN))
    return _w(inbox, "preamble.eml", text)


def odg_split_spans(inbox):
    letters = "".join('<text:span text:style-name="T%d">%s</text:span>' % (i % 3, ch) for i, ch in enumerate(FULL))
    content = (_XML + '<office:document-content xmlns:office="urn:oasis:names:tc:opendocument:xmlns:office:1.0" '
               'xmlns:draw="urn:oasis:names:tc:opendocument:xmlns:drawing:1.0" '
               'xmlns:text="urn:oasis:names:tc:opendocument:xmlns:text:1.0" '
               'xmlns:svg="urn:oasis:names:tc:opendocument:xmlns:svg-compatible:1.0" office:version="1.3">'
               '<office:body><office:drawing><draw:page draw:name="page1"><draw:custom-shape svg:width="8cm" '
               'svg:height="2cm"><text:p>offer for %s</text:p></draw:custom-shape><draw:custom-shape svg:width="8cm" '
               'svg:height="2cm"><text:p>stage two of the invented span plan</text:p></draw:custom-shape>'
               '</draw:page></office:drawing></office:body></office:document-content>' % letters)
    return _zip(inbox, "spans.odg", {"content.xml": content, "META-INF/manifest.xml": _XML + "<manifest/>"},
                first_stored=("mimetype", b"application/vnd.oasis.opendocument.graphics"))


CASES += [
    {"id": "svg-file-text", "cls": "name", "carrier": "svg file text nodes (sniffed as text)", "values": [FULL, IBAN],
     "visible": ["stage two of the invented svg file plan"], "build": svg_file_text},
    {"id": "svg-file-logo", "cls": "other", "carrier": "svg file of paths only (a logo), nothing to hold", "values": [],
     "visible": ["stage two of the invented logo plan"], "build": svg_file_logo},
    {"id": "emf-file", "cls": "name", "carrier": "emf file with a UTF-16 text record", "values": [FULL],
     "visible": ["stage two of the invented emf file plan"], "build": emf_file},
    {"id": "wmf-file", "cls": "name", "carrier": "wmf file with an EXTTEXTOUT record", "values": [FULL],
     "visible": ["stage two of the invented wmf file plan"], "build": wmf_file},
    {"id": "docx-media-svg-late", "cls": "name", "carrier": "docx svg media, text after the 64 KB probe", "values": [FULL],
     "visible": ["stage two of the invented late svg plan"], "build": docx_media_svg_late},
    {"id": "docx-altchunk-rtf", "cls": "name", "carrier": "docx altChunk rtf part", "values": [FULL, IBAN],
     "visible": ["stage two of the invented rtf chunk plan"], "build": docx_altchunk_rtf},
    {"id": "docx-altchunk-txt", "cls": "name", "carrier": "docx altChunk text/plain part", "values": [FULL, IBAN],
     "visible": ["stage two of the invented txt chunk plan"], "build": docx_altchunk_txt},
    {"id": "docx-altchunk-docx", "cls": "name", "carrier": "docx altChunk docx part (control, read as a child)",
     "values": [FULL, IBAN], "visible": ["stage two of the invented docx chunk plan"], "build": docx_altchunk_docx},
    {"id": "docx-omml-cell", "cls": "name", "carrier": "docx OMML at block level inside a table cell", "values": [FULL],
     "visible": ["stage two of the invented cell equation plan"], "build": docx_omml_cell},
    {"id": "pptx-smartart", "cls": "name", "carrier": "pptx SmartArt (ppt/diagrams/data1.xml)", "values": [FULL, IBAN],
     "visible": ["stage two of the invented deck smartart plan"], "build": pptx_smartart},
    {"id": "pptx-picture-only", "cls": "name", "carrier": "pptx whose only content is one picture", "values": [FULL],
     "visible": ["stage two of the invented deck picture plan"], "build": pptx_picture_only},
    {"id": "pdf-image-only", "cls": "name", "carrier": "pdf page that is one picture (png text chunks)", "values": [FULL],
     "visible": ["stage two of the invented scan plan"], "build": pdf_image_only},
    {"id": "pdf-attachment", "cls": "name", "carrier": "pdf embedded file (EmbeddedFiles name tree)", "values": [FULL, IBAN],
     "visible": ["stage two of the invented attachment plan"], "build": pdf_attachment},
    {"id": "pdf-zip-polyglot-ok", "cls": "name", "carrier": "pdf with a zip appended, page long enough to count as read",
     "values": [FULL, IBAN], "visible": ["stage two of the invented polyglot plan"], "build": pdf_zip_polyglot_ok},
    {"id": "pdf-ws-prefix", "cls": "name", "carrier": "pdf with CRLF before %PDF, hex string chunks", "values": [SHORT, FULL],
     "visible": ["stage two of the invented whitespace plan"], "build": pdf_ws_prefix},
    {"id": "pdf-prefix-literal", "cls": "name", "carrier": "pdf with a text prefix, name as a plain literal string",
     "values": [FULL], "visible": ["stage two of the invented literal plan"], "build": pdf_prefix_literal},
    {"id": "pdf-prefix-long-hex", "cls": "name", "carrier": "pdf with a text prefix, name as one long hex string",
     "values": [FULL], "visible": ["stage two of the invented long hex plan"], "build": pdf_prefix_long_hex},
    {"id": "pdf-compressed-prefix-big", "cls": "name", "carrier": "compressed pdf of 3 pages with a text prefix",
     "values": [FULL], "visible": ["stage two of the invented big compressed plan"], "build": pdf_compressed_prefix_big},
    {"id": "rtf-objdata-lines", "cls": "name", "carrier": "rtf objdata hex in 128 char lines (Word style)", "values": [FULL],
     "visible": ["stage two of the invented rtf lines plan"], "build": rtf_objdata_lines},
    {"id": "rtf-objdata-zip", "cls": "name", "carrier": "rtf objdata OLE Package holding a docx (compressed)",
     "values": [FULL, IBAN], "visible": ["stage two of the invented rtf package plan"], "build": rtf_objdata_zip},
    {"id": "sql-x-quote-hex", "cls": "name", "carrier": "sql X'..' hex literal (boundary: no prefix letter in the block)",
     "values": [FULL], "visible": ["stage two of the invented xquote plan"], "build": sql_x_quote_hex},
    {"id": "sql-bytea-lower", "cls": "name", "carrier": "sql bytea \\x lower-case hex literal", "values": [FULL],
     "visible": ["stage two of the invented split plan"], "build": sql_bytea_lines},
    {"id": "vcf-qp-umlaut", "cls": "name", "carrier": "vcf QP run with a non-ASCII byte (boundary)", "values": [fx.ORG_FORMS[0]],
     "visible": ["stage two of the invented umlaut card plan"], "build": vcf_qp_umlaut},
    {"id": "vcf-qp-ascii-glued", "cls": "name", "carrier": "vcf QP ASCII name with one non-ASCII pair glued on", "values": [FULL],
     "visible": ["stage two of the invented glued card plan"], "build": vcf_qp_ascii_split},
    {"id": "vcf-qp-soft-break", "cls": "name", "carrier": "vcf QP ASCII name with a soft line break", "values": [FULL],
     "visible": ["stage two of the invented soft card plan"], "build": vcf_qp_soft_break},
    {"id": "ics-folded", "cls": "name", "carrier": "ics SUMMARY folded (RFC 5545) inside the name", "values": [FULL],
     "visible": ["stage two of the invented folded plan"], "build": ics_folded},
    {"id": "mbox-two-plain", "cls": "name", "carrier": "mbox second message after a plain first one (control)", "values": [FULL],
     "visible": ["stage two of the invented plain mailbox plan"], "build": mbox_two_plain},
    {"id": "eml-epilogue", "cls": "name", "carrier": "eml text after the closing MIME boundary", "values": [FULL, IBAN],
     "visible": ["stage two of the invented epilogue plan"], "build": eml_epilogue},
    {"id": "eml-preamble", "cls": "name", "carrier": "eml text before the first MIME boundary", "values": [FULL, IBAN],
     "visible": ["stage two of the invented preamble plan"], "build": eml_preamble},
    {"id": "odg-split-spans", "cls": "name", "carrier": "odg name split into one text:span per letter (raw xml side path)",
     "values": [FULL], "visible": ["stage two of the invented span plan"], "build": odg_split_spans},
]


# =========================================================================== round 3: boundaries and more carriers


def _pdf_flate(content: bytes, prefix: bytes) -> bytes:
    import zlib

    comp = zlib.compress(content, 9)
    objs = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 595 842] /Contents 4 0 R /Resources << /Font << /F1 5 0 R >> >> >>",
        b"<< /Length %d /Filter /FlateDecode >>\nstream\n%s\nendstream" % (len(comp), comp),
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    out = bytearray(prefix + b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")
    offsets = []
    for i, body in enumerate(objs, start=1):
        offsets.append(len(out))
        out += b"%d 0 obj\n%s\nendobj\n" % (i, body)
    xref = len(out)
    out += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objs) + 1)
    for off in offsets:
        out += b"%010d 00000 n \n" % off
    out += b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (len(objs) + 1, xref)
    return bytes(out)


def pdf_flate_prefix_small(inbox):
    content = ("BT /F1 12 Tf 72 700 Td (offer for %s) Tj ET\nBT /F1 12 Tf 72 680 Td (stage two of the invented "
               "small flate plan) Tj ET\n" % FULL).encode("latin-1")
    return _w(inbox, "flate-small.pdf", _pdf_flate(content, b"X-Generated-By: the invented tool\n\n"))


def pdf_flate_prefix_big(inbox):
    lines = ["BT /F1 12 Tf 72 %d Td (line %d of the invented big flate plan, words words words %d) Tj ET" % (780 - 14 * i, i, i * 7919)
             for i in range(50)]
    lines.insert(0, "BT /F1 12 Tf 72 800 Td (offer for %s, stage two of the invented big flate plan) Tj ET" % FULL)
    return _w(inbox, "flate-big.pdf", _pdf_flate("\n".join(lines).encode("latin-1"), b"X-Generated-By: the invented tool\n\n"))


def pdf_nul_prefix(inbox):
    content = ("BT /F1 12 Tf 72 700 Td (offer for %s) Tj ET\nBT /F1 12 Tf 72 680 Td (stage two of the invented nul "
               "prefix plan) Tj ET\n" % FULL)
    return _w(inbox, "nul.pdf", _pdf(content, prefix=b"\x00\x00"))


def rtf_pict_png(inbox):
    png = _png({"Description": "offer for %s" % FULL, "Comment": "stage three of the invented rtf png plan"})
    text = ("{\\rtf1\\ansi\\deff0{\\fonttbl{\\f0 Arial;}}\n\\pard stage two of the invented rtf png plan\\par\n"
            "{\\pict\\pngblip\\picw40\\pich30\\picwgoal600\\pichgoal450 %s}\\par\n}\n" % _hex(png))
    return _w(inbox, "screenshot.rtf", text)


def docx_altchunk_mht(inbox):
    def qp_soft(s, at):
        return s[:at] + "=\r\n" + s[at:]

    body_html = ("<html><head><meta http-equiv=3D\"Content-Type\" content=3D\"text/html; charset=3Dutf-8\"></head>"
                 "<body><p>offer for %s</p><p>stage two of the invented mht chunk plan, IBAN %s</p></body></html>"
                 % (qp_soft(FULL, 8), IBAN))
    mht = ("MIME-Version: 1.0\r\nContent-Type: multipart/related; boundary=\"----=_NextPart_01\"\r\n\r\n"
           "------=_NextPart_01\r\nContent-Location: file:///C:/doc.htm\r\nContent-Transfer-Encoding: quoted-printable\r\n"
           "Content-Type: text/html; charset=\"utf-8\"\r\n\r\n%s\r\n\r\n------=_NextPart_01--\r\n" % body_html)
    body = wp("stage one of the invented mht chunk plan") + '<w:altChunk r:id="rId2"/>'
    return make_docx(inbox, "chunk-mht.docx", body, parts={"word/afchunk.mht": mht},
                     rels=[("rId2", "aFChunk", "afchunk.mht", False)], overrides=[("/word/afchunk.mht", "message/rfc822")])


def jpg_zip_polyglot(inbox):
    jpg = _jpeg(comment="stage one of the invented jpg polyglot plan")
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("notes.txt", "offer for %s, stage two of the invented jpg polyglot plan\n" % FULL)
    return _w(inbox, "photo-bundle.jpg", jpg + buf.getvalue())


def sevenz_file(inbox):
    name = ("offer for %s, stage two of the invented sevenz plan" % FULL).encode("utf-16-le")
    return _w(inbox, "bundle.7z", b"7z\xbc\xaf\x27\x1c\x00\x04" + bytes(range(24)) + b"\x17\x06" + name + b"\x00" * 64)


def ics_attach_docx(inbox):
    import docx

    d = docx.Document()
    d.add_paragraph("offer for %s, stage two of the invented attach plan, IBAN %s" % (FULL, IBAN))
    buf = io.BytesIO()
    d.save(buf)
    b64 = base64.b64encode(buf.getvalue()).decode("ascii")
    line = ("ATTACH;FMTTYPE=application/vnd.openxmlformats-officedocument.wordprocessingml.document;"
            "ENCODING=BASE64;VALUE=BINARY:%s" % b64)
    folded = "\r\n ".join(line[i:i + 74] for i in range(0, len(line), 74))
    text = ("BEGIN:VCALENDAR\r\nVERSION:2.0\r\nPRODID:-//invented//calendar//EN\r\nBEGIN:VEVENT\r\n"
            "UID:20260921-3@example.org\r\nDTSTAMP:20260921T100000Z\r\nDTSTART:20260922T090000Z\r\n"
            "SUMMARY:review, see the attached offer\r\n%s\r\nEND:VEVENT\r\nEND:VCALENDAR\r\n" % folded)
    return _w(inbox, "with-attach.ics", text)


def vcf_photo_exif(inbox):
    jpg = _jpeg(description="offer for %s" % FULL, comment="stage two of the invented photo card plan")
    b64 = base64.b64encode(jpg).decode("ascii")
    folded = "\r\n ".join(b64[i:i + 72] for i in range(0, len(b64), 72))
    text = ("BEGIN:VCARD\r\nVERSION:3.0\r\nFN:Anna\r\nNOTE:stage one of the invented photo card plan\r\n"
            "PHOTO;ENCODING=b;TYPE=JPEG:%s\r\nEND:VCARD\r\n" % folded)
    return _w(inbox, "contact-photo.vcf", text)


def docx_text_prefix(inbox):
    import docx

    d = docx.Document()
    d.add_paragraph("offer for %s, stage two of the invented docx prefix plan" % FULL)
    buf = io.BytesIO()
    d.save(buf)
    return _w(inbox, "offer-prefixed.docx", b"X-Generated-By: the invented tool\n\n" + buf.getvalue())


def eps_file(inbox):
    text = ("%%!PS-Adobe-3.0 EPSF-3.0\n%%%%BoundingBox: 0 0 300 100\n%%%%Title: plan\n/Helvetica findfont 12 scalefont setfont\n"
            "10 60 moveto (offer for %s) show\n10 30 moveto (stage two of the invented eps plan) show\nshowpage\n" % FULL)
    return _w(inbox, "plan.eps", text)


CASES += [
    {"id": "pdf-flate-prefix-small", "cls": "name", "carrier": "small pdf with a binary Flate stream and a text prefix",
     "values": [FULL], "visible": ["stage two of the invented small flate plan"], "build": pdf_flate_prefix_small},
    {"id": "pdf-flate-prefix-big", "cls": "name", "carrier": "bigger pdf with a binary Flate stream and a text prefix",
     "values": [FULL], "visible": ["stage two of the invented big flate plan"], "build": pdf_flate_prefix_big},
    {"id": "pdf-nul-prefix", "cls": "name", "carrier": "pdf with two NUL bytes before %PDF", "values": [FULL],
     "visible": ["stage two of the invented nul prefix plan"], "build": pdf_nul_prefix},
    {"id": "rtf-pict-png", "cls": "name", "carrier": "rtf \\pict pngblip hex with a tEXt chunk", "values": [FULL],
     "visible": ["stage two of the invented rtf png plan"], "build": rtf_pict_png},
    {"id": "docx-altchunk-mht", "cls": "name", "carrier": "docx altChunk mht, quoted-printable html with a soft break in the name",
     "values": [FULL, IBAN], "visible": ["stage two of the invented mht chunk plan"], "build": docx_altchunk_mht},
    {"id": "jpg-zip-polyglot", "cls": "name", "carrier": "jpg with a zip appended, text in the zip member", "values": [FULL],
     "visible": ["stage two of the invented jpg polyglot plan"], "build": jpg_zip_polyglot},
    {"id": "sevenz", "cls": "name", "carrier": "7z archive (no reader), UTF-16 header strings", "values": [FULL],
     "visible": ["stage two of the invented sevenz plan"], "build": sevenz_file},
    {"id": "ics-attach-docx", "cls": "name", "carrier": "ics ATTACH base64 docx (binary block)", "values": [FULL, IBAN],
     "visible": ["stage two of the invented attach plan"], "build": ics_attach_docx},
    {"id": "vcf-photo-exif", "cls": "name", "carrier": "vcf PHOTO base64 jpg with EXIF", "values": [FULL],
     "visible": ["stage two of the invented photo card plan"], "build": vcf_photo_exif},
    {"id": "docx-text-prefix", "cls": "name", "carrier": "docx with a text prefix before PK", "values": [FULL],
     "visible": ["stage two of the invented docx prefix plan"], "build": docx_text_prefix},
    {"id": "eps-file", "cls": "name", "carrier": "eps (PostScript) picture file, text in show operators", "values": [FULL],
     "visible": ["stage two of the invented eps plan"], "build": eps_file},
]


# =========================================================================== round 4: encoded binary blocks in text carriers


def _b64_lines(data: bytes, width: int = 76, sep: str = "\n") -> str:
    b = base64.b64encode(data).decode("ascii")
    return sep.join(b[i:i + width] for i in range(0, len(b), width))


def html_data_uri_jpg(inbox):
    jpg = _jpeg(description="offer for %s" % FULL, comment="stage two of the invented data uri plan")
    html = ("<html><body><p>stage one of the invented data uri plan</p><img alt=\"site\" src=\"data:image/jpeg;base64,%s\">"
            "</body></html>\n" % base64.b64encode(jpg).decode("ascii"))
    return _w(inbox, "page.html", html)


def txt_mime_b64_jpg(inbox):
    jpg = _jpeg(description="offer for %s" % FULL, comment="stage two of the invented mime block plan")
    text = "stage one of the invented mime block plan\n\nbegin-base64 644 site.jpg\n%s\n====\n" % _b64_lines(jpg)
    return _w(inbox, "pasted.txt", text)


def txt_b64_single_jpg(inbox):
    jpg = _jpeg(description="offer for %s" % FULL, comment="stage two of the invented single block plan")
    text = "stage one of the invented single block plan\n\nsite.jpg: %s\n" % base64.b64encode(jpg).decode("ascii")
    return _w(inbox, "pasted-single.txt", text)


def txt_xxd_jpg(inbox):
    jpg = _jpeg(description="offer for %s" % FULL, comment="stage two of the invented xxd plan")
    h = jpg.hex()
    text = "stage one of the invented xxd plan\n\n%s\n" % "\n".join(h[i:i + 60] for i in range(0, len(h), 60))
    return _w(inbox, "dump.txt", text)


def ics_attach_jpg(inbox):
    jpg = _jpeg(description="offer for %s" % FULL, comment="stage two of the invented calendar photo plan")
    line = "ATTACH;FMTTYPE=image/jpeg;ENCODING=BASE64;VALUE=BINARY:%s" % base64.b64encode(jpg).decode("ascii")
    folded = "\r\n ".join(line[i:i + 74] for i in range(0, len(line), 74))
    text = ("BEGIN:VCALENDAR\r\nVERSION:2.0\r\nPRODID:-//invented//calendar//EN\r\nBEGIN:VEVENT\r\n"
            "UID:20260921-4@example.org\r\nDTSTAMP:20260921T100000Z\r\nDTSTART:20260922T090000Z\r\n"
            "SUMMARY:site visit, photo attached\r\n%s\r\nEND:VEVENT\r\nEND:VCALENDAR\r\n" % folded)
    return _w(inbox, "with-photo.ics", text)


def odt_chart_object(inbox):
    content = (_XML + '<office:document-content xmlns:office="urn:oasis:names:tc:opendocument:xmlns:office:1.0" '
               'xmlns:draw="urn:oasis:names:tc:opendocument:xmlns:drawing:1.0" '
               'xmlns:text="urn:oasis:names:tc:opendocument:xmlns:text:1.0" xmlns:xlink="http://www.w3.org/1999/xlink" '
               'office:version="1.3"><office:body><office:text><text:p>stage one of the invented odt chart plan</text:p>'
               '<text:p><draw:frame draw:name="chart1"><draw:object xlink:href="./Object 1"/></draw:frame></text:p>'
               '</office:text></office:body></office:document-content>')
    chart = (_XML + '<office:document-content xmlns:office="urn:oasis:names:tc:opendocument:xmlns:office:1.0" '
             'xmlns:chart="urn:oasis:names:tc:opendocument:xmlns:chart:1.0" '
             'xmlns:text="urn:oasis:names:tc:opendocument:xmlns:text:1.0" office:version="1.3"><office:body>'
             '<office:chart><chart:chart chart:class="chart:bar"><chart:title><text:p>%s, stage two of the invented odt '
             'chart plan, IBAN %s</text:p></chart:title></chart:chart></office:chart></office:body>'
             '</office:document-content>' % (FULL, IBAN))
    return _zip(inbox, "report.odt", {"content.xml": content, "Object 1/content.xml": chart,
                                      "META-INF/manifest.xml": _XML + "<manifest/>"},
                first_stored=("mimetype", b"application/vnd.oasis.opendocument.text"))


def xlsx_image_exif(inbox):
    from openpyxl import Workbook
    from openpyxl.drawing.image import Image as XImage

    wb = Workbook()
    ws = wb.active
    ws["A1"] = "stage one of the invented sheet image plan"
    png = _png({"Description": "offer for %s" % FULL, "Comment": "stage two of the invented sheet image plan"})
    img = XImage(io.BytesIO(png))
    ws.add_image(img, "C3")
    p = inbox / "with-image.xlsx"
    wb.save(str(p))
    return p


def eml_svg_attachment(inbox):
    msg = email.message.EmailMessage()
    msg["From"] = "alice@example.org"
    msg["To"] = "bob@example.org"
    msg["Subject"] = "plan"
    msg.set_content("stage one of the invented mailed svg plan\n")
    svg = _SVG % ("offer for %s" % FULL, "stage two of the invented mailed svg plan")
    msg.add_attachment(svg.encode("utf-8"), maintype="image", subtype="svg+xml", filename="plan.svg")
    return _w(inbox, "plan.eml", msg.as_bytes())


CASES += [
    {"id": "html-data-uri-jpg", "cls": "name", "carrier": "html img data: uri, single-line base64 jpg with EXIF",
     "values": [FULL], "visible": ["stage two of the invented data uri plan"], "build": html_data_uri_jpg},
    {"id": "txt-mime-b64-jpg", "cls": "name", "carrier": "txt MIME-style multi-line base64 jpg with EXIF",
     "values": [FULL], "visible": ["stage two of the invented mime block plan"], "build": txt_mime_b64_jpg},
    {"id": "txt-b64-single-jpg", "cls": "name", "carrier": "txt single-line base64 jpg with EXIF (boundary)",
     "values": [FULL], "visible": ["stage two of the invented single block plan"], "build": txt_b64_single_jpg},
    {"id": "txt-xxd-jpg", "cls": "name", "carrier": "txt hex dump (xxd -p lines) of a jpg with EXIF",
     "values": [FULL], "visible": ["stage two of the invented xxd plan"], "build": txt_xxd_jpg},
    {"id": "ics-attach-jpg", "cls": "name", "carrier": "ics ATTACH base64 jpg with EXIF (folded lines)",
     "values": [FULL], "visible": ["stage two of the invented calendar photo plan"], "build": ics_attach_jpg},
    {"id": "odt-chart-object", "cls": "name", "carrier": "odt chart object title (Object 1/content.xml)",
     "values": [FULL, IBAN], "visible": ["stage two of the invented odt chart plan"], "build": odt_chart_object},
    {"id": "xlsx-image-exif", "cls": "name", "carrier": "xlsx picture with png text chunks", "values": [FULL],
     "visible": ["stage two of the invented sheet image plan"], "build": xlsx_image_exif},
    {"id": "eml-svg-attachment", "cls": "name", "carrier": "svg attached to an eml (picture as text child)", "values": [FULL],
     "visible": ["stage two of the invented mailed svg plan"], "build": eml_svg_attachment},
]


# =========================================================================== round 5: boundaries of the encoded block escapes


def _jpeg_name_in_one_line(comment_marker: str, width_bytes: int = 57) -> bytes:
    """A jpg with EXIF FULL padded (a leading COM segment) so that the name sits inside one decoded base64 line."""
    for pad in range(0, 80):
        jpg = _jpeg(description="offer for %s" % FULL, comment="%s%s" % (comment_marker, "." * pad))
        i = jpg.find(FULL.encode("latin-1"))
        if i >= 0 and (i % width_bytes) + len(FULL) <= width_bytes:
            return jpg
    raise RuntimeError("no alignment found")


def vcf_photo_oneline(inbox):
    jpg = _jpeg(description="offer for %s" % FULL, comment="stage two of the invented one line photo plan")
    text = ("BEGIN:VCARD\r\nVERSION:3.0\r\nFN:Anna\r\nNOTE:stage one of the invented one line photo plan\r\n"
            "PHOTO;ENCODING=b;TYPE=JPEG:%s\r\nEND:VCARD\r\n" % base64.b64encode(jpg).decode("ascii"))
    return _w(inbox, "contact-oneline.vcf", text)


def txt_mime_b64_jpg_aligned(inbox):
    jpg = _jpeg_name_in_one_line("stage two of the invented aligned block plan")
    text = "stage one of the invented aligned block plan\n\n%s\n" % _b64_lines(jpg)
    return _w(inbox, "pasted-aligned.txt", text)


def txt_hex_single_jpg(inbox):
    jpg = _jpeg(description="offer for %s" % FULL, comment="stage two of the invented single hex plan")
    return _w(inbox, "dump-single.txt", "stage one of the invented single hex plan\n\nblob: %s\n" % jpg.hex())


def txt_uuencode_jpg(inbox):
    import binascii

    jpg = _jpeg(description="offer for %s" % FULL, comment="stage two of the invented uuencode plan")
    lines = [binascii.b2a_uu(jpg[i:i + 45]).decode("ascii").rstrip("\n") for i in range(0, len(jpg), 45)]
    text = "stage one of the invented uuencode plan\n\nbegin 644 site.jpg\n%s\n`\nend\n" % "\n".join(lines)
    return _w(inbox, "uu.txt", text)


def _vcf_qp_bytes() -> bytes:
    def qp(s):
        return "".join("=%02X" % b for b in s.encode("utf-8"))

    return ("BEGIN:VCARD\r\nVERSION:2.1\r\nFN:Anna\r\nORG;ENCODING=QUOTED-PRINTABLE;CHARSET=UTF-8:%s\r\n"
            "NOTE:stage two of the invented contained card plan\r\nEND:VCARD\r\n" % qp(FULL)).encode("ascii")


def eml_vcf_attachment_qp(inbox):
    msg = email.message.EmailMessage()
    msg["From"] = "alice@example.org"
    msg["To"] = "bob@example.org"
    msg["Subject"] = "contact"
    msg.set_content("stage one of the invented contained card plan\n")
    msg.add_attachment(_vcf_qp_bytes(), maintype="text", subtype="vcard", filename="contact.vcf")
    return _w(inbox, "contact.eml", msg.as_bytes())


def zip_vcf_qp(inbox):
    return _zip(inbox, "contacts.zip", {"contact.vcf": _vcf_qp_bytes()})


def xlsm_vba(inbox):
    from openpyxl import Workbook

    wb = Workbook()
    ws = wb.active
    ws["A1"] = "stage one of the invented macro sheet plan"
    p = inbox / "macro.xlsm"
    wb.save(str(p))
    src = ("Attribute VB_Name = \"Module1\"\r\nSub Auto_Open()\r\n    MsgBox \"offer for %s, stage two of the invented "
           "macro sheet plan\"\r\nEnd Sub\r\n" % FULL).encode("cp1252")
    vba = _ole_header() + b"\x00" * 128 + _ovba_compress(src) + b"\x00" * 256
    return _add_zip_members(p, {"xl/vbaProject.bin": vba})


def mht_file(inbox):
    body_html = ("<html><head><meta http-equiv=3D\"Content-Type\" content=3D\"text/html; charset=3Dutf-8\"></head>"
                 "<body><p>offer for %s=\r\n%s</p><p>stage two of the invented web archive plan, IBAN %s</p></body></html>"
                 % (FULL[:8], FULL[8:], IBAN))
    mht = ("From: <Saved by the invented browser>\r\nSubject: plan\r\nDate: Mon, 21 Sep 2026 10:00:00 +0000\r\n"
           "MIME-Version: 1.0\r\nContent-Type: multipart/related; type=\"text/html\"; boundary=\"----=_NextPart_02\"\r\n\r\n"
           "------=_NextPart_02\r\nContent-Type: text/html; charset=\"utf-8\"\r\nContent-Transfer-Encoding: quoted-printable\r\n"
           "Content-Location: file:///C:/plan.htm\r\n\r\n%s\r\n\r\n------=_NextPart_02--\r\n" % body_html)
    return _w(inbox, "plan.mht", mht)


CASES += [
    {"id": "vcf-photo-oneline", "cls": "name", "carrier": "vcf PHOTO base64 jpg with EXIF on one line (boundary)",
     "values": [FULL], "visible": ["stage two of the invented one line photo plan"], "build": vcf_photo_oneline},
    {"id": "txt-mime-b64-jpg-aligned", "cls": "name", "carrier": "txt MIME-style base64 jpg, name inside one line",
     "values": [FULL], "visible": ["stage two of the invented aligned block plan"], "build": txt_mime_b64_jpg_aligned},
    {"id": "txt-hex-single-jpg", "cls": "name", "carrier": "txt one-line hex of a jpg with EXIF", "values": [FULL],
     "visible": ["stage two of the invented single hex plan"], "build": txt_hex_single_jpg},
    {"id": "txt-uuencode-jpg", "cls": "name", "carrier": "txt uuencoded jpg with EXIF (no decoder claimed)",
     "values": [FULL], "visible": ["stage two of the invented uuencode plan"], "build": txt_uuencode_jpg},
    {"id": "eml-vcf-attachment-qp", "cls": "name", "carrier": "vcf with QP ASCII name attached to an eml",
     "values": [FULL], "visible": ["stage two of the invented contained card plan"], "build": eml_vcf_attachment_qp},
    {"id": "zip-vcf-qp", "cls": "name", "carrier": "vcf with QP ASCII name inside a zip", "values": [FULL],
     "visible": ["stage two of the invented contained card plan"], "build": zip_vcf_qp},
    {"id": "xlsm-vba", "cls": "name", "carrier": "xlsm vbaProject.bin module source", "values": [FULL],
     "visible": ["stage two of the invented macro sheet plan"], "build": xlsm_vba},
    {"id": "mht-file", "cls": "name", "carrier": "mht web archive, QP html with a soft break inside the name",
     "values": [FULL, IBAN], "visible": ["stage two of the invented web archive plan"], "build": mht_file},
]


# What tests/test_redteam_pack.py accepts besides the fixture forms. DERIVED: strings built from a fixture form
# (a typo, an encoding, a transliteration). INVENTED: structured values this module builds, invented but valid
# in shape (documentation ranges, fake digit families). A value may carry one of them whole or in part.
DERIVED = ()
INVENTED = (IBAN, PHONE)
