"""Red team of wipe mode, dimension disguised: the written shapes of the earlier red team (forms, derived, pdf)
applied to unregistered names and to technical terms: look-alike letters, spaced letters, a name split by bold or
markup, html entities, base64, hex, url and json escapes, zero width characters, soft hyphens, a hyphenation at a
line end, upper and lower case, CamelCase, snake and kebab case, a bidi override, a name in a file name, in a pdf
(plain, two columns, letter spaced, annotation, metadata), in a docx header, footer, comment, footnote and core
property, inside a quoted-printable and a base64 mail body. The losses: a product name spaced, bold split and with
entities, the column headings of a pdf table.

Every name is from tests/fixtures.py or from INVENTED below. Hosts end in .example or .test. Nothing here is real.
"""
import base64
import sys
import zipfile
from email import policy
from email.message import EmailMessage
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
from tests import fixtures as fx  # noqa: E402

# the fixture forms
CUSTOMER, CUSTOMER_SHORT, CUSTOMER_ACRONYM, CUSTOMER_EN = fx.CUSTOMER_FORMS
PERSON, SURNAME = fx.PERSON_FORMS            # a registered person (becomes a PERS code, not a token)
FIRST = fx.PLANTED_PERSON.split()[0]         # Xqarv
LAST = fx.PLANTED_PERSON.split()[1]          # Pomblet
PLANTED_PERSON = fx.PLANTED_PERSON           # Xqarv Pomblet, unregistered
PLANTED_COMPANY = fx.PLANTED_CANDIDATE       # Nrgtz Beratung GmbH, unregistered
BRAND = PLANTED_COMPANY.split()[0]           # Nrgtz
CONTROL = fx.CONTROL_UNREGISTERED            # Musterhausen, unregistered, a place word

# every invented name of this module, so that the owner can see nothing real slipped in
INVENTED = (
    "Anna Qwertzuio",        # an invented person (a common first name, an invented surname of nine letters)
    "Qwertzheim",            # an invented town
    "Musterweg 5",           # an invented street line
    "info@example.test",     # a neutral mail address that teaches no form
    "team@example.test",
)

MARK = "stage two of the invented plan"
P2 = "Anna Qwertzuio"
P2_LAST = P2.split()[1]
TOWN = "Qwertzheim"
STREET = "Musterweg 5"
CYR_O = "о"      # Cyrillic small o, a look-alike of the Latin o
CYR_E = "е"      # Cyrillic small e
GREEK_O = "ο"    # Greek small omicron
ZWSP = "​"
SHY = "­"
RLO, PDF = "‮", "‬"


def spaced(s):
    """Letters with one space between them, two spaces between words (a letter spaced heading)."""
    return "  ".join(" ".join(w) for w in s.split())


def txt(name, text):
    """A plain file with `text`; the marker line proves the carrier was read."""
    def build(inbox):
        p = inbox / name
        p.write_text(text + "\n" + MARK + "\n", encoding="utf-8")
        return p
    return build


def raw(name, data: bytes):
    def build(inbox):
        p = inbox / name
        p.write_bytes(data)
        return p
    return build


def html(name, body):
    return txt(name, "<html><body>%s<p>%s</p></body></html>" % (body, MARK))


def case(cid, carrier, values, keep, build, note="", code=None, expect=None):
    c = {"id": cid, "carrier": carrier, "values": list(values), "keep": list(keep), "build": build, "note": note}
    if code is not None:
        c["code"] = code
    if expect:
        c["expect"] = dict(expect)
    return c


# --------------------------------------------------------------------------- docx by hand (as calibration/redteam/office.py)

W_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
R_NS = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
REL_T = "http://schemas.openxmlformats.org/officeDocument/2006/relationships/"
PKG_REL = "http://schemas.openxmlformats.org/package/2006/relationships"
CORE_REL = "http://schemas.openxmlformats.org/package/2006/relationships/metadata/core-properties"
CT_CORE = "application/vnd.openxmlformats-package.core-properties+xml"
CT_APP = "application/vnd.openxmlformats-officedocument.extended-properties+xml"
CT_DOC = "application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"
CT_HDR = "application/vnd.openxmlformats-officedocument.wordprocessingml.header+xml"
CT_FTR = "application/vnd.openxmlformats-officedocument.wordprocessingml.footer+xml"
CT_WCM = "application/vnd.openxmlformats-officedocument.wordprocessingml.comments+xml"
CT_FN = "application/vnd.openxmlformats-officedocument.wordprocessingml.footnotes+xml"
XML_HEAD = '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'


def esc(s):
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")


def rels_xml(rels):
    return (XML_HEAD + '<Relationships xmlns="%s">%s</Relationships>' % (
        PKG_REL, "".join('<Relationship Id="%s" Type="%s" Target="%s"/>' % (rid, t, esc(target)) for rid, t, target in rels)))


def content_types(overrides):
    return (XML_HEAD + '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
            '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
            '<Default Extension="xml" ContentType="application/xml"/>'
            '%s</Types>' % "".join('<Override PartName="%s" ContentType="%s"/>' % (p, c) for p, c in overrides))


def core_xml(title="", creator="", modified_by=""):
    return (XML_HEAD + '<cp:coreProperties xmlns:cp="http://schemas.openxmlformats.org/package/2006/metadata/core-properties" '
            'xmlns:dc="http://purl.org/dc/elements/1.1/" xmlns:dcterms="http://purl.org/dc/terms/" '
            'xmlns:dcmitype="http://purl.org/dc/dcmitype/" xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">'
            '%s%s%s<dcterms:created xsi:type="dcterms:W3CDTF">2026-10-01T08:00:00Z</dcterms:created></cp:coreProperties>'
            % ("<dc:title>%s</dc:title>" % esc(title) if title else "",
               "<dc:creator>%s</dc:creator>" % esc(creator) if creator else "",
               "<cp:lastModifiedBy>%s</cp:lastModifiedBy>" % esc(modified_by) if modified_by else ""))


def app_xml(company="", manager="", application="Microsoft Office Word"):
    return (XML_HEAD + '<Properties xmlns="http://schemas.openxmlformats.org/officeDocument/2006/extended-properties" '
            'xmlns:vt="http://schemas.openxmlformats.org/officeDocument/2006/docPropsVTypes"><Application>%s</Application>%s%s</Properties>'
            % (esc(application), "<Company>%s</Company>" % esc(company) if company else "",
               "<Manager>%s</Manager>" % esc(manager) if manager else ""))


def write_zip(path, parts):
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, data in parts.items():
            zf.writestr(name, data)
    return path


def w_p(text):
    return '<w:p><w:r><w:t xml:space="preserve">%s</w:t></w:r></w:p>' % esc(text)


def w_commented(text):
    return ('<w:p><w:commentRangeStart w:id="0"/><w:r><w:t xml:space="preserve">%s</w:t></w:r><w:commentRangeEnd w:id="0"/>'
            '<w:r><w:commentReference w:id="0"/></w:r></w:p>' % esc(text))


def w_footnoted(text):
    return ('<w:p><w:r><w:t xml:space="preserve">%s</w:t></w:r><w:r><w:rPr><w:vertAlign w:val="superscript"/></w:rPr>'
            '<w:footnoteReference w:id="1"/></w:r></w:p>' % esc(text))


def docx_doc(name, body_xml, header=None, footer=None, comment=None, footnote=None, core=None, app=None):
    """A docx by hand: `body_xml` the blocks of the body, `header` and `footer` lists of paragraph texts (detection
    carriers), `comment` (author, initials, text) on the range marked with w_commented, `footnote` the text of
    footnote 1 (referenced with w_footnoted), `core` dict(title, creator, modified_by), `app` dict(company, manager)."""
    def build(inbox):
        parts = {}
        overrides = [("/word/document.xml", CT_DOC)]
        doc_rels = []
        sect = []
        if header:
            parts["word/header1.xml"] = (XML_HEAD + '<w:hdr xmlns:w="%s">%s</w:hdr>' % (W_NS, "".join(w_p(t) for t in header))).encode("utf-8")
            overrides.append(("/word/header1.xml", CT_HDR))
            doc_rels.append(("rIdHdr1", REL_T + "header", "header1.xml"))
            sect.append('<w:headerReference w:type="default" r:id="rIdHdr1"/>')
        if footer:
            parts["word/footer1.xml"] = (XML_HEAD + '<w:ftr xmlns:w="%s">%s</w:ftr>' % (W_NS, "".join(w_p(t) for t in footer))).encode("utf-8")
            overrides.append(("/word/footer1.xml", CT_FTR))
            doc_rels.append(("rIdFtr1", REL_T + "footer", "footer1.xml"))
            sect.append('<w:footerReference w:type="default" r:id="rIdFtr1"/>')
        if comment:
            author, initials, text = comment
            parts["word/comments.xml"] = (XML_HEAD + '<w:comments xmlns:w="%s"><w:comment w:id="0" w:author="%s" w:date="2026-10-01T10:00:00Z" w:initials="%s">%s</w:comment></w:comments>'
                                          % (W_NS, esc(author), esc(initials), w_p(text))).encode("utf-8")
            overrides.append(("/word/comments.xml", CT_WCM))
            doc_rels.append(("rIdCm", REL_T + "comments", "comments.xml"))
        if footnote:
            parts["word/footnotes.xml"] = (XML_HEAD + '<w:footnotes xmlns:w="%s"><w:footnote w:type="separator" w:id="-1"><w:p><w:r><w:separator/></w:r></w:p></w:footnote>'
                                           '<w:footnote w:type="continuationSeparator" w:id="0"><w:p><w:r><w:continuationSeparator/></w:r></w:p></w:footnote>'
                                           '<w:footnote w:id="1">%s</w:footnote></w:footnotes>' % (W_NS, w_p(footnote))).encode("utf-8")
            overrides.append(("/word/footnotes.xml", CT_FN))
            doc_rels.append(("rIdFn", REL_T + "footnotes", "footnotes.xml"))
        parts["word/document.xml"] = (XML_HEAD + '<w:document xmlns:w="%s" xmlns:r="%s"><w:body>%s<w:sectPr>%s</w:sectPr></w:body></w:document>'
                                      % (W_NS, R_NS, body_xml, "".join(sect))).encode("utf-8")
        if doc_rels:
            parts["word/_rels/document.xml.rels"] = rels_xml(doc_rels).encode("utf-8")
        root_rels = [("rId1", REL_T + "officeDocument", "word/document.xml")]
        if core:
            parts["docProps/core.xml"] = core_xml(**core).encode("utf-8")
            overrides.append(("/docProps/core.xml", CT_CORE))
            root_rels.append(("rId2", CORE_REL, "docProps/core.xml"))
        if app:
            parts["docProps/app.xml"] = app_xml(**app).encode("utf-8")
            overrides.append(("/docProps/app.xml", CT_APP))
            root_rels.append(("rId3", REL_T + "extended-properties", "docProps/app.xml"))
        parts["_rels/.rels"] = rels_xml(root_rels).encode("utf-8")
        parts["[Content_Types].xml"] = content_types(overrides).encode("utf-8")
        return write_zip(inbox / name, parts)
    return build


# --------------------------------------------------------------------------- pdf (reportlab and by hand, as calibration/redteam/pdf.py)

ECS_LINE = "Der Elastic Cloud Server steht bereit fuer die zweite Welle."


def rl(name, draw):
    def build(inbox):
        from reportlab.pdfgen import canvas

        p = inbox / name
        c = canvas.Canvas(str(p), pagesize=(595, 842))
        draw(c)
        c.save()
        return p
    return build


def draw_lines(c, lines, x=72, y=750, step=24):
    c.setFont("Helvetica", 12)
    for line in lines:
        c.drawString(x, y, line)
        y -= step


def draw_spaced(c, x, y, text, gap, size=12):
    """One glyph per drawString, `gap` points apart: a letter spaced heading as pdftotext -layout reads it."""
    c.setFont("Helvetica", size)
    for ch in text:
        if ch != " ":
            c.drawString(x, y, ch)
        x += gap


def pdf_assemble(objs):
    out = bytearray(b"%PDF-1.7\n%\xe2\xe3\xcf\xd3\n")
    offsets = []
    for i, body in enumerate(objs, start=1):
        offsets.append(len(out))
        out += b"%d 0 obj\n" % i + body + b"\nendobj\n"
    xref = len(out)
    out += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objs) + 1)
    for off in offsets:
        out += b"%010d 00000 n \n" % off
    out += b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (len(objs) + 1, xref)
    return bytes(out)


def pdf_stream(dict_body, data):
    return dict_body + b"\nstream\n" + data + b"\nendstream"


def pdf_with_annotation(name, lines, annotation):
    """A one page pdf with `lines` as page text and a FreeText annotation (no appearance stream) whose /Contents is
    `annotation`: the page text is the output, the annotation reaches detection only (keyed strings)."""
    def build(inbox):
        content = b""
        y = 750
        for line in lines:
            content += b"BT /F1 12 Tf 72 %d Td (" % y + line.encode("latin-1") + b") Tj ET\n"
            y -= 24
        objs = [
            b"<< /Type /Catalog /Pages 2 0 R >>",
            b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
            b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 595 842] /Contents 4 0 R"
            b" /Resources << /Font << /F1 5 0 R >> >> /Annots [6 0 R] >>",
            pdf_stream(b"<< /Length %d >>" % len(content), content),
            b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
            b"<< /Type /Annot /Subtype /FreeText /Rect [72 600 400 640] /Contents ("
            + annotation.encode("latin-1") + b") /DA (/F1 12 Tf 0 g) >>",
        ]
        p = inbox / name
        p.write_bytes(pdf_assemble(objs))
        return p
    return build


# --------------------------------------------------------------------------- mail by hand

def eml_raw(name, body_lines, cte, charset="utf-8"):
    """A plain mail without display names (the headers teach nothing); the body as given, with the transfer
    encoding header `cte` (the lines are written as they are, so a quoted-printable soft break can sit inside a word)."""
    head = ("From: info@example.test\r\nTo: team@example.test\r\nSubject: Freigabe\r\nMIME-Version: 1.0\r\n"
            "Content-Type: text/plain; charset=\"%s\"\r\nContent-Transfer-Encoding: %s\r\n\r\n" % (charset, cte))
    return raw(name, head.encode("ascii") + "\r\n".join(body_lines).encode("utf-8") + b"\r\n")


def eml_with_attachment(name, body, filename, data):
    def build(inbox):
        msg = EmailMessage(policy=policy.default)
        msg["From"] = "info@example.test"
        msg["To"] = "team@example.test"
        msg["Subject"] = "Unterlagen"
        msg.set_content(body)
        msg.add_attachment(data, maintype="text", subtype="plain", filename=filename)
        p = inbox / name
        p.write_bytes(msg.as_bytes())
        return p
    return build


B64_TITLE_NAME = base64.b64encode(("Herr %s" % PLANTED_PERSON).encode("utf-8")).decode("ascii")   # 24 characters, inline
HEX_TITLE_NAME = ("Frau %s" % PLANTED_PERSON).encode("utf-8").hex()
MAIL_BODY = ["Hallo zusammen,", "", "Frau %s hat das Angebot freigegeben. Der Load Balancer steht." % PLANTED_PERSON, "", MARK]
B64_MAIL_BODY = base64.encodebytes("\r\n".join(MAIL_BODY).encode("utf-8")).decode("ascii").splitlines()
LOWER_FILE = "angebot_%s.pdf" % PLANTED_COMPANY.lower().replace(" ", "_")    # angebot_nrgtz_beratung_gmbh.pdf
LOWER_ATTACHMENT = LOWER_FILE.replace(".pdf", ".txt")


CASES = [
    # ------------------------------------------------------------------ look-alike letters
    case("look-cyr-o-ident-filename", "txt, a file name with the surname between underscores, a Cyrillic o in it",
         [LAST], ["Load Balancer"],
         txt("note.txt", "siehe Angebot_%s_2026.pdf im Ordner. Der Load Balancer steht.\n" % LAST.replace("o", CYR_O, 1)),
         note="the identifier rule reads Latin letters only"),
    case("look-cyr-o-camel", "txt, a CamelCase login of first name and surname, a Cyrillic o in the surname",
         [PLANTED_PERSON], ["Load Balancer"],
         txt("note.txt", "Login %s%s angelegt. Der Load Balancer steht.\n" % (FIRST, LAST.replace("o", CYR_O, 1)))),
    case("look-cyr-e-postcode-town", "txt, an address block, the town after the postcode with a Cyrillic e",
         [PLANTED_COMPANY, TOWN], ["Load Balancer"],
         txt("brief.txt", "%s\n%s\n12345 %s\n\nDer Load Balancer steht.\n" % (PLANTED_COMPANY, STREET, TOWN.replace("e", CYR_E, 1))),
         expect={TOWN: "place"}),
    case("look-greek-o-title-then-latin", "txt, Frau plus the surname with a Greek omicron, later the Latin surname alone",
         [LAST], ["Load Balancer"],
         txt("note.txt", "Frau %s leitet das Projekt. Der Load Balancer steht.\n\n%s hat zugesagt.\n" % (LAST.replace("o", GREEK_O, 1), LAST)),
         expect={LAST: "person"}, note="the learned form is folded, so the Latin spelling must be wiped too"),
    # ------------------------------------------------------------------ spaced letters
    case("spaced-letterhead-company-then-brand", "txt, the company letter spaced in capitals as a letterhead, the brand alone in the body",
         [PLANTED_COMPANY, BRAND], ["Load Balancer"],
         txt("brief.txt", "%s\n\nAngebot\n\n%s hat zugesagt. Der Load Balancer steht.\n" % (spaced(PLANTED_COMPANY.upper()), BRAND)),
         expect={BRAND: "company"}, note="the spaced stretch is a candidate, but nothing is learned from it"),
    case("spaced-heading-ecs-kept", "txt, a service name letter spaced as a heading (a stop phrase)",
         [], ["Elastic Cloud Server"],
         txt("note.txt", "%s\n\nDie Server laufen in eu-de. Der Load Balancer steht.\n" % spaced("ELASTIC CLOUD SERVER"))),
    case("spaced-heading-avd-lost", "txt, a product name of three known words letter spaced as a heading (no stop phrase)",
         [], ["Azure Virtual Desktop"],
         txt("note.txt", "%s\n\nWir migrieren nach TCP. Der Load Balancer steht.\n" % spaced("AZURE VIRTUAL DESKTOP")),
         note="the exemption looks at the single letters, not at the joined words"),
    case("spaced-surname-after-title", "txt, Herr plus the surname letter spaced inside a sentence",
         [LAST], ["Load Balancer"],
         txt("note.txt", "Freigabe durch Herr %s am Montag. Der Load Balancer steht.\n" % " ".join(LAST)),
         note="the joined letters are checked without the title in front of them"),
    case("spaced-learned-surname-7", "txt, the person after Frau (learned), later the seven letter surname letter spaced on a line",
         [PLANTED_PERSON, LAST], ["Load Balancer"],
         txt("note.txt", "Frau %s leitet das Projekt.\n\n%s\n\nDer Load Balancer steht.\n" % (PLANTED_PERSON, " ".join(LAST))),
         note="the learned matcher reads a spread form only from eight letters on; the final check reads it"),
    case("pdf-spaced-wide-glyphs", "pdf, one glyph per draw 28 points apart (pdftotext gives four spaces between the letters)",
         [PLANTED_PERSON], ["Elastic Cloud Server"],
         rl("heading.pdf", lambda c: (draw_spaced(c, 72, 750, PLANTED_PERSON, 28), draw_lines(c, [ECS_LINE, MARK], y=700))),
         note="a run with three spaces or more inside is cut into pieces of one letter in proposed"),
    case("pdf-two-columns-first-last", "pdf, Vorname and Nachname columns, the first name and the surname in two columns of one line",
         [PLANTED_PERSON], ["Vorname", "Nachname", "Elastic Cloud Server"],
         rl("liste.pdf", lambda c: (draw_lines(c, ["Vorname", FIRST], x=72, y=750), draw_lines(c, ["Nachname", LAST], x=300, y=750),
                                   draw_lines(c, [ECS_LINE, MARK], y=690)))),
    # ------------------------------------------------------------------ split by bold or markup
    case("md-bold-each-word", "md, first name and surname each in their own bold", [PLANTED_PERSON], ["Load Balancer"],
         txt("note.md", "Termin mit **%s** **%s** am Montag. Der Load Balancer steht.\n" % (FIRST, LAST))),
    case("md-bold-split-term", "md, the first word of a service name in bold", [], ["Elastic Cloud Server"],
         txt("note.md", "Der **Elastic** Cloud Server steht bereit.\n")),
    case("md-inline-b-tag", "md, the first name inside an inline b tag (no reader flip)", [PLANTED_PERSON], ["Load Balancer"],
         txt("note.md", "Termin mit <b>%s</b> %s am Montag. Der Load Balancer steht.\n" % (FIRST, LAST))),
    case("html-spans-glued", "html, first name and surname in two spans without a space between them", [PLANTED_PERSON], ["Load Balancer"],
         html("note.html", "<p>Ansprechpartner: <span>%s</span><span>%s</span></p><p>Der Load Balancer steht.</p>" % (FIRST, LAST))),
    case("md-entity-nbsp-term", "md, a service name with &nbsp; between its words", [], ["Elastic Cloud Server"],
         txt("note.md", "Der Elastic&nbsp;Cloud&nbsp;Server steht bereit.\n")),
    # ------------------------------------------------------------------ encodings
    case("txt-base64-inline-title", "txt, base64 of Herr plus the person inline (24 characters)", [PLANTED_PERSON], ["Load Balancer"],
         txt("note.txt", "Freigabe: %s\n\nDer Load Balancer steht.\n" % B64_TITLE_NAME), expect={PLANTED_PERSON: "person"}),
    case("txt-hex-inline-label", "txt, hex of Frau plus the person inline", [PLANTED_PERSON], ["Load Balancer"],
         txt("note.txt", "Kontakt (hex): %s\n\nDer Load Balancer steht.\n" % HEX_TITLE_NAME), expect={PLANTED_PERSON: "person"}),
    case("json-unicode-escapes-contact", "json, the contact value with \\u escapes for the capitals", [PLANTED_PERSON], ["Load Balancer"],
         txt("contact.json", '{"contact": "\\u%04x%s \\u%04x%s", "service": "Load Balancer", "note": "%s"}'
             % (ord(FIRST[0]), FIRST[1:], ord(LAST[0]), LAST[1:], MARK)),
         expect={PLANTED_PERSON: "person"}),
    case("eml-qp-soft-break-in-surname", "eml, quoted-printable body with a soft line break inside the surname after Frau",
         [PLANTED_PERSON], ["Load Balancer"],
         eml_raw("freigabe.eml", ["Hallo zusammen,", "", "Frau %s %s=" % (FIRST, LAST[:3]), "%s hat das Angebot freigegeben. Der Load Balancer steht." % LAST[3:],
                                  "", MARK], "quoted-printable"),
         expect={PLANTED_PERSON: "person"}),
    case("eml-base64-body", "eml, base64 body with Frau plus the person", [PLANTED_PERSON], ["Load Balancer"],
         eml_raw("freigabe.eml", B64_MAIL_BODY, "base64"), expect={PLANTED_PERSON: "person"}),
    case("eml-attachment-lower-filename", "eml, an attachment named angebot_<company>_gmbh.txt in lower case, listed in the output",
         [PLANTED_COMPANY, BRAND], ["Load Balancer"],
         eml_with_attachment("unterlagen.eml", "Anbei die Unterlagen. Der Load Balancer steht.\n\n%s\n" % MARK, LOWER_ATTACHMENT,
                             b"Inhalt der Anlage, nichts weiter.\n"),
         note="the attachment list of the mail output carries the file name as written"),
    # ------------------------------------------------------------------ invisible characters, hyphens, bidi
    case("txt-zero-width-inside-name", "txt, a zero width space inside the first name and inside the surname after Frau",
         [PLANTED_PERSON], ["Load Balancer"],
         txt("note.txt", "Frau %s%s%s %s%s%s hat zugesagt. Der Load Balancer steht.\n"
             % (FIRST[:2], ZWSP, FIRST[2:], LAST[:3], ZWSP, LAST[3:])), expect={PLANTED_PERSON: "person"}),
    case("txt-soft-hyphen-inside-surname", "txt, a soft hyphen inside the surname after Frau", [PLANTED_PERSON], ["Load Balancer"],
         txt("note.txt", "Frau %s %s%s%s hat zugesagt. Der Load Balancer steht.\n" % (FIRST, LAST[:3], SHY, LAST[3:])),
         expect={PLANTED_PERSON: "person"}),
    case("txt-hyphenation-line-end-person", "txt, the surname hyphenated at a line end after Frau", [PLANTED_PERSON], ["Load Balancer"],
         txt("note.txt", "Frau %s %s-\n%s hat das Angebot freigegeben. Der Load Balancer steht.\n" % (FIRST, LAST[:3], LAST[3:])),
         expect={PLANTED_PERSON: "person"}),
    case("txt-hyphen-break-compound-company", "txt, a hyphenated company broken at its hyphen at a line end (the brand, a hyphen, the rest on the next line)",
         [PLANTED_COMPANY, BRAND], ["Load Balancer"],
         txt("brief.txt", "Angebot an die\n%s-\n%s\n\nDer Load Balancer steht.\n" % (BRAND, PLANTED_COMPANY.split(" ", 1)[1])),
         expect={BRAND: "company"}, note="the next line starts with a capital, so the hyphenation is not rejoined"),
    case("txt-bidi-override-reversed", "txt, the person reversed under a right-to-left override", [PLANTED_PERSON], ["Load Balancer"],
         txt("note.txt", "Termin mit %s%s%s am Montag. Der Load Balancer steht.\n" % (RLO, PLANTED_PERSON[::-1], PDF)),
         note="the override is stripped from the output, so a viewer shows the letters in stream order (hand check)"),
    # ------------------------------------------------------------------ case
    case("txt-upper-title-line-then-titlecase", "txt, HERR plus the person in an all capitals line, later Frau plus the surname",
         [PLANTED_PERSON, LAST], ["Load Balancer"],
         txt("note.txt", "HERR %s, GESCHAEFTSFUEHRER\n\nFrau %s hat zugesagt. Der Load Balancer steht.\n" % (PLANTED_PERSON.upper(), LAST)),
         expect={PLANTED_PERSON: "person"}),
    case("txt-lower-company-gmbh", "txt, the company in lower case including gmbh inside a sentence", [PLANTED_COMPANY, BRAND], ["Load Balancer"],
         txt("chat.txt", "die %s hat zugesagt. Der Load Balancer steht.\n" % PLANTED_COMPANY.lower()),
         note="the legal forms are matched case sensitive"),
    # ------------------------------------------------------------------ CamelCase, snake and kebab case
    case("txt-camel-person-login", "txt, a CamelCase login of first name and surname", [PLANTED_PERSON], ["Load Balancer"],
         txt("note.txt", "Login %s%s angelegt. Der Load Balancer steht.\n" % (FIRST, LAST))),
    case("txt-camel-company-glued-gmbh", "txt, the company CamelCased with its legal form glued (a folder name)", [PLANTED_COMPANY, BRAND], ["Load Balancer"],
         txt("note.txt", "Ordner %s kopiert. Der Load Balancer steht.\n" % PLANTED_COMPANY.replace(" ", "")),
         note="the CamelCase alternative of the identifier rule needs lower case letters after every capital"),
    case("txt-snake-lower-filename", "txt, a lower case file name with the company between underscores inside a sentence",
         [PLANTED_COMPANY, BRAND], ["Load Balancer"],
         txt("note.txt", "siehe %s im Ordner. Der Load Balancer steht.\n" % LOWER_FILE),
         note="the identifier rule reads capitalised parts only"),
    case("txt-kebab-brand-compound", "txt, the brand hyphenated to a known German noun", [BRAND], ["Load Balancer"],
         txt("note.txt", "Die %s-Lösung läuft seit Montag. Der Load Balancer steht.\n" % BRAND)),
    # ------------------------------------------------------------------ docx carriers that reach detection only, the name alone in the body
    case("docx-header-company-brand-in-body", "docx, the company in the page header, the brand alone in the body",
         [PLANTED_COMPANY, BRAND], ["Elastic Cloud Server"],
         docx_doc("angebot.docx", w_p("Angebot") + w_p("%s hat zugesagt. Der Elastic Cloud Server steht." % BRAND) + w_p(MARK),
                  header=["Angebot für %s" % PLANTED_COMPANY]),
         expect={BRAND: "company"}),
    case("docx-footer-byline-surname-in-body", "docx, Erstellt von plus the person in the page footer, the surname alone in the body",
         [PLANTED_PERSON, LAST], ["Elastic Cloud Server"],
         docx_doc("angebot.docx", w_p("Angebot") + w_p("%s hat zugesagt. Der Elastic Cloud Server steht." % LAST) + w_p(MARK),
                  footer=["Erstellt von %s" % PLANTED_PERSON, "Seite 1"]),
         expect={LAST: "person"}),
    case("docx-comment-author-surname-in-body", "docx, the person as the author of a comment, the surname alone in the body",
         [PLANTED_PERSON, LAST], ["Elastic Cloud Server"],
         docx_doc("angebot.docx", w_commented("Der Elastic Cloud Server steht bereit.") + w_p("%s hat zugesagt." % LAST) + w_p(MARK),
                  comment=(PLANTED_PERSON, "".join(w[0] for w in PLANTED_PERSON.split()), "bitte prüfen")),
         expect={LAST: "person"}),
    case("docx-footnote-title-person", "docx, Frau plus the person inside a footnote (the footnote is part of the output)",
         [PLANTED_PERSON], ["Elastic Cloud Server"],
         docx_doc("angebot.docx", w_footnoted("Der Elastic Cloud Server steht bereit.") + w_p(MARK),
                  footnote="Gespräch mit Frau %s am Montag." % PLANTED_PERSON),
         expect={PLANTED_PERSON: "person"}),
    case("docx-core-creator-surname-in-body", "docx, the person as dc:creator of the core properties, the surname alone in the body",
         [PLANTED_PERSON, LAST], ["Elastic Cloud Server"],
         docx_doc("angebot.docx", w_p("Angebot") + w_p("%s hat zugesagt. Der Elastic Cloud Server steht." % LAST) + w_p(MARK),
                  core={"title": "Angebot", "creator": PLANTED_PERSON}),
         expect={LAST: "person"}),
    # ------------------------------------------------------------------ pdf carriers
    case("pdf-annotation-title-surname-in-body", "pdf, Frau plus the person in a FreeText annotation, the surname alone on the page",
         [PLANTED_PERSON, LAST], ["Elastic Cloud Server"],
         pdf_with_annotation("angebot.pdf", ["%s hat zugesagt." % LAST, ECS_LINE, MARK], "Frau %s bitte pruefen" % PLANTED_PERSON),
         expect={LAST: "person"}),
    case("pdf-plain-label-person", "pdf, Ansprechpartner: plus the person as plain page text", [PLANTED_PERSON], ["Elastic Cloud Server"],
         rl("angebot.pdf", lambda c: draw_lines(c, ["Ansprechpartner: %s" % PLANTED_PERSON, ECS_LINE, MARK])),
         expect={PLANTED_PERSON: "person"}),
    # ------------------------------------------------------------------ round 2: the mechanisms of round 1 varied
    case("txt-three-spaces-between-names", "txt, first name and surname with three spaces between them (a tab stop expanded) inside a sentence",
         [PLANTED_PERSON], ["Load Balancer"],
         txt("note.txt", "Rueckfrage bei %s   %s am Montag. Der Load Balancer steht.\n" % (FIRST, LAST)),
         note="the cut of a run at three spaces or more, as in the two column pdf, in plain text"),
    case("spaced-letterhead-person-then-surname", "txt, the person letter spaced in capitals as a letterhead over a role line, the surname alone in the body",
         [PLANTED_PERSON, LAST], ["Load Balancer"],
         txt("brief.txt", "%s\nGeschaeftsfuehrung\n\n%s hat zugesagt. Der Load Balancer steht.\n" % (spaced(PLANTED_PERSON.upper()), LAST)),
         expect={LAST: "person"}, note="as spaced-letterhead-company-then-brand, for a person"),
    case("md-bold-surname-only", "md, the surname alone in bold after the plain first name", [PLANTED_PERSON], ["Load Balancer"],
         txt("note.md", "Termin mit %s **%s** am Montag. Der Load Balancer steht.\n" % (FIRST, LAST)),
         note="as md-bold-each-word with one bold span only"),
    case("txt-kebab-lower-slug", "txt, a lower case kebab slug with the company brand and its generic word inside a sentence",
         [BRAND], ["Load Balancer"],
         txt("note.txt", "siehe %s-angebot-2026.docx im Ordner. Der Load Balancer steht.\n" % "-".join(PLANTED_COMPANY.lower().split()[:2])),
         note="as txt-snake-lower-filename with hyphens"),
    case("look-cyr-e-place-date-line", "txt, the place and date line of a letter, the town with a Cyrillic e",
         [TOWN], ["Load Balancer"],
         txt("brief.txt", "%s, den 5. Oktober 2026\n\nSehr geehrte Damen und Herren,\n\nder Load Balancer steht.\n" % TOWN.replace("e", CYR_E, 1)),
         expect={TOWN: "place"}, note="as look-cyr-e-postcode-town for the place-date rule"),
]
