"""Red team of wipe mode, dimension decks: slide decks and documents built as office files by hand (zip plus XML,
the way calibration/redteam/office.py does it). pptx and odp decks with title slides ("Prepared for X by Y"),
footers and copyright lines, logos as pictures with alt text, speaker notes, agenda slides, org charts in shapes
and SmartArt, contact slides, participant tables, slide masters, comment authors and document properties; docx
documents with cover pages, key-value tables, document histories, headers, footers, comment authors, tracked
changes and footnotes. The losses: headings of agenda slides, product and vendor names on slides, service names
and flavors in tables, role phrases, the partner's own legal name, and the words a wiped heading teaches the run.

Every name is from tests/fixtures.py or from INVENTED below. Hosts end in .example. Nothing here is real.
"""
import sys
import zipfile
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
    "Anna Qwertzuio",            # an invented person (a common first name, an invented surname)
    "Markus Plmokn",             # an invented person on the partner's side
    "Zrbl Systemhaus GmbH",      # an invented company, short form "Zrbl Systemhaus", brand "Zrbl"
    "zrbl-systemhaus.example",   # its invented mail host
)

P2 = "Anna Qwertzuio"
P2_FIRST, P2_LAST = P2.split()
P3 = "Markus Plmokn"
P3_FIRST, P3_LAST = P3.split()
CO2 = "Zrbl Systemhaus GmbH"
CO2_SHORT = "Zrbl Systemhaus"
BRAND2 = "Zrbl"
DOMAIN2 = "zrbl-systemhaus.example"
MAIL2 = "xqarv.pomblet@" + DOMAIN2
PHONE2 = "+49 555 0101"
# the platform operator's legal name: a vendor name of the TCP scope, never anonymised (a keep term, no value)
PARTNER = "T-Systems International GmbH"

# --------------------------------------------------------------------------- xml helpers

A_NS = "http://schemas.openxmlformats.org/drawingml/2006/main"
P_NS = "http://schemas.openxmlformats.org/presentationml/2006/main"
R_NS = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
W_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
DGM_NS = "http://schemas.openxmlformats.org/drawingml/2006/diagram"
REL_T = "http://schemas.openxmlformats.org/officeDocument/2006/relationships/"
PKG_REL = "http://schemas.openxmlformats.org/package/2006/relationships"
CORE_REL = "http://schemas.openxmlformats.org/package/2006/relationships/metadata/core-properties"
CT_SLIDE = "application/vnd.openxmlformats-officedocument.presentationml.slide+xml"
CT_NOTES = "application/vnd.openxmlformats-officedocument.presentationml.notesSlide+xml"
CT_PRES = "application/vnd.openxmlformats-officedocument.presentationml.presentation.main+xml"
CT_MASTER = "application/vnd.openxmlformats-officedocument.presentationml.slideMaster+xml"
CT_DGM = "application/vnd.openxmlformats-officedocument.drawingml.diagramData+xml"
CT_CMAUTH = "application/vnd.openxmlformats-officedocument.presentationml.commentAuthors+xml"
CT_CM = "application/vnd.openxmlformats-officedocument.presentationml.comments+xml"
CT_CORE = "application/vnd.openxmlformats-package.core-properties+xml"
CT_APP = "application/vnd.openxmlformats-officedocument.extended-properties+xml"
CT_DOC = "application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"
CT_HDR = "application/vnd.openxmlformats-officedocument.wordprocessingml.header+xml"
CT_FTR = "application/vnd.openxmlformats-officedocument.wordprocessingml.footer+xml"
CT_WCM = "application/vnd.openxmlformats-officedocument.wordprocessingml.comments+xml"
CT_FN = "application/vnd.openxmlformats-officedocument.wordprocessingml.footnotes+xml"
XML_HEAD = '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
PNG = bytes.fromhex(
    "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c4890000000d4944415478da63f8ffff3f00"
    "05fe02fe0d5b52b10000000049454e44ae426082")


def esc(s):
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")


def rels_xml(rels):
    return (XML_HEAD + '<Relationships xmlns="%s">%s</Relationships>' % (
        PKG_REL, "".join('<Relationship Id="%s" Type="%s" Target="%s"/>' % (rid, t, esc(target)) for rid, t, target in rels)))


def content_types(overrides):
    return (XML_HEAD + '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
            '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
            '<Default Extension="xml" ContentType="application/xml"/><Default Extension="png" ContentType="image/png"/>'
            '%s</Types>' % "".join('<Override PartName="%s" ContentType="%s"/>' % (p, c) for p, c in overrides))


def core_xml(title="", creator="", modified_by=""):
    return (XML_HEAD + '<cp:coreProperties xmlns:cp="http://schemas.openxmlformats.org/package/2006/metadata/core-properties" '
            'xmlns:dc="http://purl.org/dc/elements/1.1/" xmlns:dcterms="http://purl.org/dc/terms/" '
            'xmlns:dcmitype="http://purl.org/dc/dcmitype/" xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">'
            '%s%s%s<dcterms:created xsi:type="dcterms:W3CDTF">2026-10-01T08:00:00Z</dcterms:created></cp:coreProperties>'
            % ("<dc:title>%s</dc:title>" % esc(title) if title else "",
               "<dc:creator>%s</dc:creator>" % esc(creator) if creator else "",
               "<cp:lastModifiedBy>%s</cp:lastModifiedBy>" % esc(modified_by) if modified_by else ""))


def app_xml(company="", manager="", application="Microsoft Office PowerPoint"):
    return (XML_HEAD + '<Properties xmlns="http://schemas.openxmlformats.org/officeDocument/2006/extended-properties" '
            'xmlns:vt="http://schemas.openxmlformats.org/officeDocument/2006/docPropsVTypes"><Application>%s</Application>%s%s</Properties>'
            % (esc(application), "<Company>%s</Company>" % esc(company) if company else "",
               "<Manager>%s</Manager>" % esc(manager) if manager else ""))


def write_zip(path, parts, first=None):
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as zf:
        if first:
            zf.writestr(zipfile.ZipInfo(first), parts[first], compress_type=zipfile.ZIP_STORED)
        for name, data in parts.items():
            if name != first:
                zf.writestr(name, data)
    return path


# --------------------------------------------------------------------------- pptx by hand


def a_p(text):
    """One drawingml paragraph; a newline in `text` is a line break inside the paragraph."""
    runs = []
    for i, piece in enumerate(text.split("\n")):
        if i:
            runs.append("<a:br/>")
        runs.append('<a:r><a:rPr lang="de-DE"/><a:t>%s</a:t></a:r>' % esc(piece))
    return "<a:p>%s</a:p>" % "".join(runs)


def sp(sid, paras, ph=None):
    nvpr = '<p:nvPr><p:ph type="%s"/></p:nvPr>' % ph if ph else "<p:nvPr/>"
    return ('<p:sp><p:nvSpPr><p:cNvPr id="%d" name="TextBox %d"/><p:cNvSpPr txBox="1"/>%s</p:nvSpPr><p:spPr/>'
            '<p:txBody><a:bodyPr/><a:lstStyle/>%s</p:txBody></p:sp>' % (sid, sid, nvpr, "".join(a_p(p) for p in paras)))


def pic(sid, descr, rid):
    return ('<p:pic><p:nvPicPr><p:cNvPr id="%d" name="Picture %d" descr="%s"/><p:cNvPicPr/><p:nvPr/></p:nvPicPr>'
            '<p:blipFill><a:blip r:embed="%s"/><a:stretch><a:fillRect/></a:stretch></p:blipFill><p:spPr/></p:pic>'
            % (sid, sid, esc(descr), rid))


def tbl(sid, rows):
    trs = []
    for row in rows:
        tcs = "".join('<a:tc><a:txBody><a:bodyPr/><a:lstStyle/>%s</a:txBody></a:tc>' % a_p(c) for c in row)
        trs.append('<a:tr h="370840">%s</a:tr>' % tcs)
    grid = "".join('<a:gridCol w="2000000"/>' for _ in rows[0])
    return ('<p:graphicFrame><p:nvGraphicFramePr><p:cNvPr id="%d" name="Table %d"/><p:cNvGraphicFramePr/><p:nvPr/></p:nvGraphicFramePr>'
            '<p:xfrm><a:off x="0" y="0"/><a:ext cx="6000000" cy="2000000"/></p:xfrm><a:graphic>'
            '<a:graphicData uri="http://schemas.openxmlformats.org/drawingml/2006/table"><a:tbl><a:tblPr/><a:tblGrid>%s</a:tblGrid>%s</a:tbl>'
            '</a:graphicData></a:graphic></p:graphicFrame>' % (sid, sid, grid, "".join(trs)))


def smartart_frame(sid, rid):
    return ('<p:graphicFrame><p:nvGraphicFramePr><p:cNvPr id="%d" name="Diagram %d"/><p:cNvGraphicFramePr/><p:nvPr/></p:nvGraphicFramePr>'
            '<p:xfrm><a:off x="0" y="0"/><a:ext cx="6000000" cy="4000000"/></p:xfrm><a:graphic><a:graphicData uri="%s">'
            '<dgm:relIds xmlns:dgm="%s" r:dm="%s" r:lo="" r:qs="" r:cs=""/></a:graphicData></a:graphic></p:graphicFrame>'
            % (sid, sid, DGM_NS, DGM_NS, rid))


def diagram_data(nodes):
    """A SmartArt data model; every node holds its paragraphs (name, role)."""
    pts = []
    for i, paras in enumerate(nodes):
        pts.append('<dgm:pt modelId="{%08d-0000-0000-0000-000000000000}" type="node"><dgm:prSet/><dgm:spPr/>'
                   '<dgm:t><a:bodyPr/><a:lstStyle/>%s</dgm:t></dgm:pt>' % (i + 1, "".join(a_p(p) for p in paras)))
    return (XML_HEAD + '<dgm:dataModel xmlns:dgm="%s" xmlns:a="%s"><dgm:ptLst>%s</dgm:ptLst><dgm:cxnLst/></dgm:dataModel>'
            % (DGM_NS, A_NS, "".join(pts)))


def sp_tree(inner):
    return ('<p:cSld><p:spTree><p:nvGrpSpPr><p:cNvPr id="1" name=""/><p:cNvGrpSpPr/><p:nvPr/></p:nvGrpSpPr><p:grpSpPr/>%s'
            '</p:spTree></p:cSld>' % inner)


def slide_xml(shapes_xml):
    return (XML_HEAD + '<p:sld xmlns:a="%s" xmlns:r="%s" xmlns:p="%s">%s<p:clrMapOvr><a:masterClrMapping/></p:clrMapOvr></p:sld>'
            % (A_NS, R_NS, P_NS, sp_tree(shapes_xml)))


def notes_xml(lines):
    body = ('<p:sp><p:nvSpPr><p:cNvPr id="2" name="Notes Placeholder 2"/><p:cNvSpPr><a:spLocks noGrp="1"/></p:cNvSpPr>'
            '<p:nvPr><p:ph type="body" idx="1"/></p:nvPr></p:nvSpPr><p:spPr/><p:txBody><a:bodyPr/><a:lstStyle/>%s</p:txBody></p:sp>'
            % "".join(a_p(l) for l in lines))
    return XML_HEAD + '<p:notes xmlns:a="%s" xmlns:r="%s" xmlns:p="%s">%s</p:notes>' % (A_NS, R_NS, P_NS, sp_tree(body))


def master_xml(lines):
    body = sp(2, lines, ph="ftr")
    return (XML_HEAD + '<p:sldMaster xmlns:a="%s" xmlns:r="%s" xmlns:p="%s">%s<p:clrMap bg1="lt1" tx1="dk1" bg2="lt2" tx2="dk2" '
            'accent1="accent1" accent2="accent2" accent3="accent3" accent4="accent4" accent5="accent5" accent6="accent6" '
            'hlink="hlink" folHlink="folHlink"/></p:sldMaster>' % (A_NS, R_NS, P_NS, sp_tree(body)))


def pptx_deck(name, slides, core=None, app=None, master=None, comment=None):
    """A pptx by hand. `slides` is a list of slides, each a list of items: a str (a text box with one paragraph),
    ("box", [paragraphs]), ("title", text), ("ftr", text) (a footer placeholder), ("table", rows), ("pic", alt text),
    ("smartart", [[paragraphs of node], ...]) or ("notes", [lines]). `core` is dict(title, creator, modified_by),
    `app` dict(company, manager), `master` a list of footer lines of the slide master, `comment` (author, initials,
    text) one comment on slide 1."""
    def build(inbox):
        parts = {}
        overrides = [("/ppt/presentation.xml", CT_PRES)]
        pres_rels = []
        sld_ids = []
        for n, items in enumerate(slides, 1):
            shapes, srels, notes = [], [], None
            sid = 2
            for item in items:
                if isinstance(item, str):
                    item = ("box", [item])
                kind = item[0]
                if kind == "box":
                    shapes.append(sp(sid, item[1]))
                elif kind == "title":
                    shapes.append(sp(sid, [item[1]], ph="title"))
                elif kind == "ftr":
                    shapes.append(sp(sid, [item[1]], ph="ftr"))
                elif kind == "table":
                    shapes.append(tbl(sid, item[1]))
                elif kind == "pic":
                    rid = "rIdPic%d" % sid
                    media = "ppt/media/image%d_%d.png" % (n, sid)
                    parts[media] = PNG
                    srels.append((rid, REL_T + "image", "../media/image%d_%d.png" % (n, sid)))
                    shapes.append(pic(sid, item[1], rid))
                elif kind == "smartart":
                    rid = "rIdDm%d" % sid
                    dname = "ppt/diagrams/data%d.xml" % n
                    parts[dname] = diagram_data(item[1]).encode("utf-8")
                    overrides.append(("/" + dname, CT_DGM))
                    srels.append((rid, REL_T + "diagramData", "../diagrams/data%d.xml" % n))
                    shapes.append(smartart_frame(sid, rid))
                elif kind == "notes":
                    notes = item[1]
                    continue
                sid += 1
            sname = "ppt/slides/slide%d.xml" % n
            parts[sname] = slide_xml("".join(shapes)).encode("utf-8")
            overrides.append(("/" + sname, CT_SLIDE))
            if notes:
                nname = "ppt/notesSlides/notesSlide%d.xml" % n
                parts[nname] = notes_xml(notes).encode("utf-8")
                overrides.append(("/" + nname, CT_NOTES))
                srels.append(("rIdNotes", REL_T + "notesSlide", "../notesSlides/notesSlide%d.xml" % n))
            if n == 1 and comment:
                srels.append(("rIdCm", REL_T + "comments", "../comments/comment1.xml"))
            if srels:
                parts["ppt/slides/_rels/slide%d.xml.rels" % n] = rels_xml(srels).encode("utf-8")
            pres_rels.append(("rId%d" % n, REL_T + "slide", "slides/slide%d.xml" % n))
            sld_ids.append('<p:sldId id="%d" r:id="rId%d"/>' % (255 + n, n))
        if master:
            parts["ppt/slideMasters/slideMaster1.xml"] = master_xml(master).encode("utf-8")
            overrides.append(("/ppt/slideMasters/slideMaster1.xml", CT_MASTER))
            pres_rels.append(("rIdMaster", REL_T + "slideMaster", "slideMasters/slideMaster1.xml"))
        if comment:
            author, initials, text = comment
            parts["ppt/commentAuthors.xml"] = (XML_HEAD + '<p:cmAuthorLst xmlns:p="%s"><p:cmAuthor id="0" name="%s" initials="%s" lastIdx="1" clrIdx="0"/></p:cmAuthorLst>'
                                               % (P_NS, esc(author), esc(initials))).encode("utf-8")
            parts["ppt/comments/comment1.xml"] = (XML_HEAD + '<p:cmLst xmlns:p="%s"><p:cm authorId="0" dt="2026-10-01T10:00:00.000" idx="1">'
                                                  '<p:pos x="10" y="10"/><p:text>%s</p:text></p:cm></p:cmLst>' % (P_NS, esc(text))).encode("utf-8")
            overrides += [("/ppt/commentAuthors.xml", CT_CMAUTH), ("/ppt/comments/comment1.xml", CT_CM)]
            pres_rels.append(("rIdCmAuth", REL_T + "commentAuthors", "commentAuthors.xml"))
        parts["ppt/presentation.xml"] = (XML_HEAD + '<p:presentation xmlns:a="%s" xmlns:r="%s" xmlns:p="%s"><p:sldIdLst>%s</p:sldIdLst>'
                                         '<p:sldSz cx="12192000" cy="6858000"/><p:notesSz cx="6858000" cy="9144000"/></p:presentation>'
                                         % (A_NS, R_NS, P_NS, "".join(sld_ids))).encode("utf-8")
        parts["ppt/_rels/presentation.xml.rels"] = rels_xml(pres_rels).encode("utf-8")
        root_rels = [("rId1", REL_T + "officeDocument", "ppt/presentation.xml")]
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


# --------------------------------------------------------------------------- docx by hand


def w_p(text, style=None):
    ppr = '<w:pPr><w:pStyle w:val="%s"/></w:pPr>' % style if style else ""
    return '<w:p>%s<w:r><w:t xml:space="preserve">%s</w:t></w:r></w:p>' % (ppr, esc(text))


def w_tbl(rows):
    trs = "".join("<w:tr>%s</w:tr>" % "".join('<w:tc><w:tcPr><w:tcW w:w="2500" w:type="dxa"/></w:tcPr>%s</w:tc>' % w_p(c) for c in row)
                  for row in rows)
    return ('<w:tbl><w:tblPr><w:tblW w:w="0" w:type="auto"/></w:tblPr><w:tblGrid>%s</w:tblGrid>%s</w:tbl>'
            % ("".join('<w:gridCol w="2500"/>' for _ in rows[0]), trs))


def w_tracked(before, inserted, deleted, after, author):
    return ('<w:p><w:r><w:t xml:space="preserve">%s</w:t></w:r>'
            '<w:ins w:id="1" w:author="%s" w:date="2026-10-01T10:00:00Z"><w:r><w:t xml:space="preserve">%s</w:t></w:r></w:ins>'
            '<w:del w:id="2" w:author="%s" w:date="2026-10-01T10:01:00Z"><w:r><w:delText xml:space="preserve">%s</w:delText></w:r></w:del>'
            '<w:r><w:t xml:space="preserve">%s</w:t></w:r></w:p>' % (esc(before), esc(author), esc(inserted), esc(author), esc(deleted), esc(after)))


def w_commented(text):
    return ('<w:p><w:commentRangeStart w:id="0"/><w:r><w:t xml:space="preserve">%s</w:t></w:r><w:commentRangeEnd w:id="0"/>'
            '<w:r><w:commentReference w:id="0"/></w:r></w:p>' % esc(text))


def w_footnoted(text):
    return ('<w:p><w:r><w:t xml:space="preserve">%s</w:t></w:r><w:r><w:rPr><w:vertAlign w:val="superscript"/></w:rPr>'
            '<w:footnoteReference w:id="1"/></w:r></w:p>' % esc(text))


def docx_doc(name, body_xml, header=None, footer=None, comment=None, footnote=None, core=None):
    """A docx by hand: `body_xml` the blocks of the body, `header` and `footer` lists of paragraph texts (detection
    carriers), `comment` (author, initials, text) on the range marked with w_commented, `footnote` the text of
    footnote 1 (referenced with w_footnoted), `core` dict(title, creator, modified_by)."""
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
        parts["_rels/.rels"] = rels_xml(root_rels).encode("utf-8")
        parts["[Content_Types].xml"] = content_types(overrides).encode("utf-8")
        return write_zip(inbox / name, parts)
    return build


# --------------------------------------------------------------------------- odp by hand

ODF_NS = (
    'xmlns:office="urn:oasis:names:tc:opendocument:xmlns:office:1.0" '
    'xmlns:text="urn:oasis:names:tc:opendocument:xmlns:text:1.0" '
    'xmlns:table="urn:oasis:names:tc:opendocument:xmlns:table:1.0" '
    'xmlns:draw="urn:oasis:names:tc:opendocument:xmlns:drawing:1.0" '
    'xmlns:presentation="urn:oasis:names:tc:opendocument:xmlns:presentation:1.0" '
    'xmlns:meta="urn:oasis:names:tc:opendocument:xmlns:meta:1.0" '
    'xmlns:dc="http://purl.org/dc/elements/1.1/" '
    'xmlns:xlink="http://www.w3.org/1999/xlink" office:version="1.2"'
)


def odp_deck(name, pages, meta=None):
    """An odp by hand: `pages` is a list of (lines, notes lines); the first line of a page is its title frame.
    `meta` is dict(creator, modified_by) for meta.xml (initial-creator, creator)."""
    def build(inbox):
        body = []
        for i, (lines, notes) in enumerate(pages, 1):
            frames = []
            for j, line in enumerate(lines):
                cls = "title" if j == 0 else "outline"
                frames.append('<draw:frame presentation:class="%s"><draw:text-box>%s</draw:text-box></draw:frame>'
                              % (cls, "".join("<text:p>%s</text:p>" % esc(p) for p in line.split("\n"))))
            if notes:
                frames.append('<presentation:notes><draw:page-thumbnail/><draw:frame presentation:class="notes"><draw:text-box>%s'
                              '</draw:text-box></draw:frame></presentation:notes>' % "".join("<text:p>%s</text:p>" % esc(p) for p in notes))
            body.append('<draw:page draw:name="page%d">%s</draw:page>' % (i, "".join(frames)))
        mime = "application/vnd.oasis.opendocument.presentation"
        content = ('<?xml version="1.0" encoding="UTF-8"?><office:document-content %s><office:body><office:presentation>%s'
                   '</office:presentation></office:body></office:document-content>' % (ODF_NS, "".join(body)))
        entries = ['<manifest:file-entry manifest:full-path="/" manifest:media-type="%s"/>' % mime,
                   '<manifest:file-entry manifest:full-path="content.xml" manifest:media-type="text/xml"/>']
        parts = {"mimetype": mime.encode("ascii"), "content.xml": content.encode("utf-8")}
        if meta:
            inner = ""
            if meta.get("creator"):
                inner += "<meta:initial-creator>%s</meta:initial-creator>" % esc(meta["creator"])
            if meta.get("modified_by"):
                inner += "<dc:creator>%s</dc:creator>" % esc(meta["modified_by"])
            inner += "<meta:generator>LibreOffice/7.6</meta:generator>"
            parts["meta.xml"] = ('<?xml version="1.0" encoding="UTF-8"?><office:document-meta %s><office:meta>%s</office:meta></office:document-meta>'
                                 % (ODF_NS, inner)).encode("utf-8")
            entries.append('<manifest:file-entry manifest:full-path="meta.xml" manifest:media-type="text/xml"/>')
        parts["META-INF/manifest.xml"] = ('<?xml version="1.0" encoding="UTF-8"?><manifest:manifest xmlns:manifest="urn:oasis:names:tc:opendocument:xmlns:manifest:1.0" '
                                          'manifest:version="1.2">%s</manifest:manifest>' % "".join(entries)).encode("utf-8")
        return write_zip(inbox / name, parts, first="mimetype")
    return build


# --------------------------------------------------------------------------- cases


def case(cid, carrier, values, keep, build, note="", code=None, expect=None):
    c = {"id": cid, "carrier": carrier, "values": list(values), "keep": list(keep), "build": build, "note": note}
    if code is not None:
        c["code"] = code
    if expect:
        c["expect"] = dict(expect)
    return c


def persons(*names):
    return {n: "person" for n in names}


FOOTER_PARTNER = PARTNER + " | Vertraulich"

CASES = [
    # ------------------------------------------------------------------ title slides, footers, copyright lines
    case("pptx-title-prepared-for-by", "pptx title slide: Prepared for <customer> by <company GmbH>; the brand alone in a footer",
         [CO2, BRAND2], ["Cloud Migration Workshop", "Zielbild und Vorgehen"],
         pptx_deck("workshop.pptx", [
             [("title", "Cloud Migration Workshop"), "Prepared for %s by %s" % (CUSTOMER, CO2), ("ftr", "7. Oktober 2026")],
             [("title", "Zielbild und Vorgehen"), ("box", ["Discovery, Zielbild, Migration in drei Wellen", "Betrieb ab Q2 2027"]),
              ("ftr", "© 2026 %s | Vertraulich" % BRAND2)],
         ]), code=True, expect={CO2: "company"}),
    case("pptx-title-prepared-by-noform", "pptx title slide: Prepared by <company without legal form>; the brand alone in the copyright footer and the notes",
         [CO2_SHORT, BRAND2], ["All rights reserved", "Zielbild und Vorgehen"],
         pptx_deck("workshop.pptx", [
             [("title", "Cloud Migration Workshop"), "Prepared by %s" % CO2_SHORT, ("ftr", "7. Oktober 2026")],
             [("title", "Zielbild und Vorgehen"), "Discovery, Zielbild, Migration in drei Wellen",
              ("ftr", "© 2026 %s. All rights reserved." % BRAND2), ("notes", ["%s liefert das Konzept bis Ende Oktober." % BRAND2])],
         ]), expect={CO2_SHORT: "company"}),
    case("pptx-footer-partner-legal", "pptx footer on every slide: the platform operator's legal name (a vendor name that must stay)",
         [], [PARTNER, "T-Systems betreibt"],
         pptx_deck("angebot.pptx", [
             [("title", "Angebot %s" % CUSTOMER), "Cloud-Migration nach TCP", ("ftr", FOOTER_PARTNER)],
             [("title", "Plattform"), "T-Systems betreibt die Plattform, der Betrieb ist nach ISO 27001 zertifiziert.", ("ftr", FOOTER_PARTNER)],
         ]), code=True),
    case("pptx-cover-erstellt-von", "pptx cover: Erstellt von <First Last> and Erstellt am <date>; the surname alone in the notes",
         [PLANTED_PERSON, LAST], ["Erstellt am"],
         pptx_deck("angebot.pptx", [
             [("title", "Angebot für %s" % CUSTOMER), ("box", ["Erstellt von %s" % PLANTED_PERSON, "Erstellt am 1. Oktober 2026"])],
             [("title", "Preise"), "Die Preise gelten bis Ende 2026.", ("notes", ["%s schickt die Preise nach." % LAST])],
         ]), code=True, expect=persons(PLANTED_PERSON)),
    case("pptx-title-customer-heading-merge", "pptx title line: the registered customer followed by a German heading on the same line",
         [], ["Technische Zielarchitektur"],
         pptx_deck("zielbild.pptx", [
             [("title", "%s Technische Zielarchitektur" % CUSTOMER), "Stand 7. Oktober 2026"],
             [("title", "Netzwerk"), "Zwei Availability Zones, ein Transit-VPC."],
         ]), code=True),
    # ------------------------------------------------------------------ contact slides
    case("pptx-contact-with-mail", "pptx contact slide: heading, name, role, mail, phone on separate lines; the surname alone in the notes of slide 2",
         [PLANTED_PERSON, LAST, MAIL2], ["Senior Cloud Architect", "Ihr Ansprechpartner"],
         pptx_deck("kontakt.pptx", [
             [("title", "Vielen Dank"), ("box", ["Ihr Ansprechpartner", PLANTED_PERSON, "Senior Cloud Architect", MAIL2, PHONE2])],
             [("title", "Flavors"), "s3.large.2 für die Web-Tier, m3.xlarge.8 für die Datenbank.",
              ("notes", ["%s meldet sich wegen der Flavors." % LAST])],
         ]), expect=persons(PLANTED_PERSON)),
    case("pptx-contact-dash-role", "pptx contact line: <First Last> – <role> – <company GmbH> on one line; the surname alone on slide 2",
         [PLANTED_PERSON, LAST, CO2], ["Senior Cloud Architect"],
         pptx_deck("kontakt.pptx", [
             [("title", "Kontakt"), "%s – Senior Cloud Architect – %s" % (PLANTED_PERSON, CO2)],
             [("title", "Unterlagen"), "%s bringt die Unterlagen zum Workshop mit." % LAST],
         ]), expect={PLANTED_PERSON: "person", CO2: "company"}),
    case("pptx-contact-label-bracket", "pptx label line: Ansprechpartner: <First Last> (<company GmbH>); the surname alone on slide 2",
         [PLANTED_PERSON, LAST, CO2], ["Freigabe"],
         pptx_deck("kontakt.pptx", [
             [("title", "Kontakt"), "Ansprechpartner: %s (%s)" % (PLANTED_PERSON, CO2)],
             [("title", "Freigabe"), "%s hat die Freigabe für die Bestellung." % LAST],
         ]), expect={PLANTED_PERSON: "person", CO2: "company"}),
    case("pptx-contact-hr-fr", "pptx contact slide: Hr. <Last>, <company GmbH> and Fr. <Last>, <company GmbH>; both surnames alone on slide 2",
         [LAST, P2_LAST, CO2, PLANTED_COMPANY], ["Workshop"],
         pptx_deck("kontakt.pptx", [
             [("title", "Kontakt"), ("box", ["Hr. %s, %s" % (LAST, CO2), "Fr. %s, %s" % (P2_LAST, PLANTED_COMPANY)])],
             [("title", "Teilnahme"), "%s und %s nehmen am Workshop teil." % (LAST, P2_LAST)],
         ]), expect=persons(LAST, P2_LAST)),
    case("pptx-contact-dipl-ing", "pptx contact slide: Dipl.-Ing. <First Last> above the role; the surname alone on slide 2",
         [PLANTED_PERSON, LAST], ["Projektleiter"],
         pptx_deck("kontakt.pptx", [
             [("title", "Kontakt"), ("box", ["Dipl.-Ing. %s" % PLANTED_PERSON, "Projektleiter"])],
             [("title", "Projekt"), "%s leitet das Projekt ab November." % LAST],
         ]), expect=persons(PLANTED_PERSON)),
    case("pptx-orgchart-firstname-newline", "pptx org chart boxes: first name and surname as two paragraphs of one shape, the role below",
         [FIRST, LAST, P2_FIRST, P2_LAST], ["Cloud Architect", "Head of IT"],
         pptx_deck("orga.pptx", [
             [("title", "Projektorganisation"), ("box", [FIRST, LAST, "Cloud Architect"]), ("box", [P2_FIRST, P2_LAST, "Head of IT"])],
         ])),
    # ------------------------------------------------------------------ agenda slides and headings (losses)
    case("pptx-agenda-naechste-schritte", "pptx agenda: Nächste Schritte and Offene Punkte as bullets; the nouns again in prose on slide 2",
         [], ["Nächste Schritte", "Offene Punkte", "Schritte sind klar", "Punkte auch"],
         pptx_deck("agenda.pptx", [
             [("title", "Agenda"), ("box", ["Begrüßung", "Ist-Situation", "Nächste Schritte", "Offene Punkte"])],
             [("title", "Zusammenfassung"), "Die Schritte sind klar, die Punkte auch."],
         ])),
    case("pptx-agenda-compound-known", "pptx agenda: a German compound noun next to a known word (Technische Zielarchitektur, Kostenvergleich Azure); the compound again in prose and in a file name",
         [], ["Technische Zielarchitektur", "Kostenvergleich Azure", "Zielarchitektur steht", "Kostenvergleich folgt", "Zielarchitektur_v2.pptx"],
         pptx_deck("agenda.pptx", [
             [("title", "Agenda"), ("box", ["Technische Zielarchitektur", "Kostenvergleich Azure", "Zeitplan"])],
             [("title", "Status"), "Die Zielarchitektur steht, der Kostenvergleich folgt. Siehe Zielarchitektur_v2.pptx im Teams-Kanal."],
         ])),
    case("pptx-agenda-english", "pptx agenda in English: headings whose words are rare in the public corpus (Assessment, Cutover, Steering, Wave) and a confidentiality footer",
         [], ["Current State Assessment", "Cutover Plan", "Steering Committee", "Wave Planning", "Strictly Confidential"],
         pptx_deck("agenda.pptx", [
             [("title", "Agenda"), ("box", ["Current State Assessment", "Cutover Plan", "Steering Committee", "Wave Planning"]),
              ("ftr", "Strictly Confidential")],
             [("title", "Summary"), "The assessment feeds the cutover plan and the wave planning.", ("ftr", "Strictly Confidential")],
         ])),
    case("pptx-bullets-vendor-products", "pptx bullets: vendor product names, some outside every word list (Zerto, Prism, Arctic Wolf)",
         [], ["Zerto Virtual Replication", "Nutanix Prism Central", "Arctic Wolf", "Cisco Meraki vMX", "Fortinet FortiGate"],
         pptx_deck("tools.pptx", [
             [("title", "Werkzeuge im Bestand"), ("box", ["Zerto Virtual Replication", "Nutanix Prism Central", "Arctic Wolf", "Cisco Meraki vMX", "Fortinet FortiGate"])],
         ])),
    case("pptx-table-services", "pptx table: TCP service names, a PostgreSQL version and flavor names in a Service | Zweck | Flavor table",
         [], ["Elastic Cloud Server", "Relational Database Service", "Secure Mail Gateway", "Enterprise VPN", "s3.large.2", "rds.pg.c6.large.4", "PostgreSQL 15"],
         pptx_deck("services.pptx", [
             [("title", "Services für %s" % CUSTOMER), ("table", [
                 ["Service", "Zweck", "Flavor"],
                 ["Elastic Cloud Server", "Web-Tier", "s3.large.2"],
                 ["Relational Database Service", "PostgreSQL 15", "rds.pg.c6.large.4"],
                 ["Secure Mail Gateway", "Mailrelay", "-"],
                 ["Enterprise VPN", "Site-to-Site", "-"],
             ])],
         ]), code=True),
    case("pptx-table-azure-mapping", "pptx table: Azure service names mapped to TCP service names",
         [], ["Azure Site Recovery", "Azure Front Door", "Microsoft Defender for Cloud", "Log Analytics Workspace",
              "Storage Disaster Recovery Service", "Log Tank Service"],
         pptx_deck("mapping.pptx", [
             [("title", "Service Mapping"), ("table", [
                 ["Azure", "TCP", "Hinweis"],
                 ["Azure Site Recovery", "Storage Disaster Recovery Service", "RPO 15 min"],
                 ["Azure Front Door", "Elastic Load Balancer", "ohne CDN"],
                 ["Microsoft Defender for Cloud", "Host Security Service", "Agent"],
                 ["Log Analytics Workspace", "Log Tank Service", "30 Tage"],
             ])],
         ])),
    case("pptx-table-ansprechpartner-teams", "pptx table: an Ansprechpartner column that holds team names next to a person",
         [PLANTED_PERSON], ["Cloud Operations Team", "TCP Service Desk"],
         pptx_deck("kontakte.pptx", [
             [("title", "Ansprechpartner je Thema"), ("table", [
                 ["Thema", "Ansprechpartner"],
                 ["Netzwerk", "Cloud Operations Team"],
                 ["Backup", PLANTED_PERSON],
                 ["Lizenzen", "TCP Service Desk"],
             ])],
         ]), expect=persons(PLANTED_PERSON)),
    # ------------------------------------------------------------------ participant tables and org charts
    case("pptx-participants-name-firma-rolle", "pptx participant table Name | Firma | Rolle with the customer, two companies and the partner's legal name; a surname alone in the notes",
         [PLANTED_PERSON, P2, P3, LAST, CO2, PLANTED_COMPANY], [PARTNER, "Projektleiter", "Senior Cloud Architect", "Architektin"],
         pptx_deck("teilnehmer.pptx", [
             [("title", "Teilnehmer"), ("table", [
                 ["Name", "Firma", "Rolle"],
                 [PLANTED_PERSON, CO2, "Projektleiter"],
                 [P2, PLANTED_COMPANY, "Architektin"],
                 [PERSON, CUSTOMER_SHORT, "CTO"],
                 [P3, PARTNER, "Senior Cloud Architect"],
             ]), ("notes", ["%s moderiert den zweiten Teil." % LAST])],
         ]), code=True, expect={PLANTED_PERSON: "person", P2: "person", P3: "person", CO2: "company", PLANTED_COMPANY: "company"}),
    case("pptx-participants-surname-name-col", "pptx participant table Name | Firma with surnames only, one with Hr.; both surnames alone on slide 2",
         [LAST, P2_LAST, CO2, PLANTED_COMPANY], ["Teilnehmer"],
         pptx_deck("teilnehmer.pptx", [
             [("title", "Teilnehmer"), ("table", [["Name", "Firma"], [LAST, CO2], ["Hr. " + P2_LAST, PLANTED_COMPANY]])],
             [("title", "Ablauf"), "%s und %s kommen um neun." % (LAST, P2_LAST)],
         ]), expect=persons(LAST, P2_LAST)),
    case("pptx-orgchart-smartart", "pptx SmartArt org chart: name and role as two paragraphs of one node; a surname alone in the notes",
         [P2, PLANTED_PERSON, P2_LAST], ["Head of IT", "Cloud Architect"],
         pptx_deck("orga.pptx", [
             [("title", "Projektorganisation"), ("smartart", [[P2, "Head of IT"], [PLANTED_PERSON, "Cloud Architect"], [PERSON, "CTO"]]),
              ("notes", ["%s entscheidet über das Budget." % P2_LAST])],
         ]), code=True, expect=persons(P2, PLANTED_PERSON)),
    case("pptx-orgchart-shapes-role", "pptx org chart in shapes: name above a German role phrase whose noun is outside the lists (Leiterin Infrastruktur); the noun again in prose",
         [P2, PLANTED_PERSON, P2_LAST], ["Leiterin Infrastruktur", "Infrastruktur wächst", "Cloud Architect"],
         pptx_deck("orga.pptx", [
             [("title", "Organisation"), ("box", [P2, "Leiterin Infrastruktur"]), ("box", [PLANTED_PERSON, "Cloud Architect"])],
             [("title", "Budget"), "%s hat das Budget. Die Infrastruktur wächst um zwei Standorte." % P2_LAST],
         ]), expect=persons(P2, PLANTED_PERSON)),
    # ------------------------------------------------------------------ speaker notes
    case("pptx-notes-surname-after-title", "pptx: Prepared by <First Last> on the title slide, the surname alone in the speaker notes",
         [PLANTED_PERSON, LAST], ["Zahlen bis Freitag"],
         pptx_deck("deck.pptx", [
             [("title", "Cloud-Migration"), "Prepared by %s" % PLANTED_PERSON, ("notes", ["%s will die Zahlen bis Freitag." % LAST])],
         ]), expect=persons(PLANTED_PERSON)),
    case("pptx-notes-label-learns", "pptx: Ansprechpartner: <First Last> only in the notes of slide 1, the surname alone on slide 2",
         [PLANTED_PERSON, LAST], ["Flavors"],
         pptx_deck("deck.pptx", [
             [("title", "Cloud-Migration"), "Stand 7. Oktober 2026", ("notes", ["Ansprechpartner: %s" % PLANTED_PERSON])],
             [("title", "Flavors"), "%s bestätigt die Flavors bis Freitag." % LAST],
         ]), expect=persons(PLANTED_PERSON)),
    # ------------------------------------------------------------------ carriers the reader keeps for detection only
    case("pptx-master-footer-company", "pptx slide master footer with <company GmbH> (detection only); the brand alone on a slide",
         [BRAND2], ["Konzept"],
         pptx_deck("deck.pptx", [[("title", "Konzept"), "%s liefert das Konzept bis Ende Oktober." % BRAND2]],
                   master=["%s | Vertraulich" % CO2])),
    case("pptx-alt-logo-vendor", "pptx pictures with vendor logos in the alt text (Cisco logo, Fortinet FortiGate logo); the vendors again on the slide",
         [], ["Cisco Meraki vMX", "Fortinet FortiGate"],
         pptx_deck("deck.pptx", [[("title", "Firewall"), ("pic", "Cisco logo"), ("pic", "Fortinet FortiGate logo"),
                                  "Cisco Meraki vMX und Fortinet FortiGate als Firewall-Optionen."]])),
    case("pptx-alt-logo-company", "pptx picture with Logo <company GmbH> in the alt text (detection only); the brand alone on the slide",
         [BRAND2], ["Firewall"],
         pptx_deck("deck.pptx", [[("title", "Firewall"), ("pic", "Logo %s" % CO2), "%s stellt die Firewall." % BRAND2]])),
    case("pptx-comment-author", "pptx comment author <First Last> (detection only); the surname alone on the slide",
         [PLANTED_PERSON, LAST], ["Preise"],
         pptx_deck("deck.pptx", [[("title", "Preise"), "%s prüft die Preise bis Freitag." % LAST]],
                   comment=(PLANTED_PERSON, "XP", "Bitte Preise prüfen")), expect=persons(PLANTED_PERSON, LAST)),
    case("pptx-core-creator", "pptx docProps dc:creator <First Last> (detection only); the surname alone on the slide",
         [PLANTED_PERSON, LAST], ["Preise"],
         pptx_deck("deck.pptx", [[("title", "Preise"), "%s prüft die Preise bis Freitag." % LAST]],
                   core={"title": "Preise", "creator": PLANTED_PERSON}), expect=persons(PLANTED_PERSON)),
    case("pptx-core-lastmodifiedby", "pptx docProps cp:lastModifiedBy <First Last>, the creator a generic office user (detection only); the surname alone on the slide",
         [P2, P2_LAST], ["Preise"],
         pptx_deck("deck.pptx", [[("title", "Preise"), "%s prüft die Preise bis Freitag." % P2_LAST]],
                   core={"title": "Preise", "creator": "Microsoft Office User", "modified_by": P2}), expect=persons(P2, P2_LAST)),
    # ------------------------------------------------------------------ odp
    case("odp-title-notes", "odp title page: Prepared for <customer> by <company GmbH>; the brand alone in the notes of page 2",
         [CO2, BRAND2], ["Zielbild und Vorgehen"],
         odp_deck("workshop.odp", [
             (["Cloud Migration Workshop", "Prepared for %s by %s" % (CUSTOMER, CO2)], None),
             (["Zielbild und Vorgehen", "Discovery, Zielbild, Migration in drei Wellen"], ["%s fragen, ob die dritte Welle hält." % BRAND2]),
         ]), code=True, expect={CO2: "company"}),
    case("odp-meta-creator", "odp meta.xml initial-creator <First Last> (detection only); the surname alone on a page under a Nächste Schritte heading",
         [PLANTED_PERSON, LAST], ["Nächste Schritte", "die Schritte"],
         odp_deck("deck.odp", [
             (["Nächste Schritte", "%s prüft die Schritte bis Freitag." % LAST], None),
         ], meta={"creator": PLANTED_PERSON}), expect=persons(PLANTED_PERSON)),
    # ------------------------------------------------------------------ docx
    case("docx-cover-labels", "docx cover page: Kunde:, Ansprechpartner:, Erstellt von: labels; both surnames alone in the body",
         [PLANTED_PERSON, P2, LAST, P2_LAST], ["Angebot Cloud-Migration", "Anforderungen abgestimmt"],
         docx_doc("angebot.docx",
                  w_p("Angebot Cloud-Migration", "Title") + w_p("Kunde: %s" % CUSTOMER) + w_p("Ansprechpartner: %s" % PLANTED_PERSON)
                  + w_p("Erstellt von: %s" % P2) + w_p("Version 1.2, Stand 1. Oktober 2026")
                  + w_p("Einleitung", "Heading1") + w_p("%s und %s haben die Anforderungen abgestimmt." % (LAST, P2_LAST))),
         code=True, expect=persons(PLANTED_PERSON, P2)),
    case("docx-cover-table-keyvalue", "docx cover table with label cells (Projekt, Kunde, Ansprechpartner, Autor) next to the values, no colon; both surnames alone in the body",
         [PLANTED_PERSON, P2, LAST, P2_LAST], ["Cloud-Migration", "Anforderungen abgestimmt"],
         docx_doc("angebot.docx",
                  w_tbl([["Projekt", "Cloud-Migration"], ["Kunde", CUSTOMER], ["Ansprechpartner", PLANTED_PERSON], ["Autor", P2]])
                  + w_p("Einleitung", "Heading1") + w_p("%s und %s haben die Anforderungen abgestimmt." % (LAST, P2_LAST))),
         code=True, expect=persons(PLANTED_PERSON, P2)),
    case("docx-history-table", "docx document history table Version | Datum | Autor | Änderung with an initial and a surname; both surnames alone in the body",
         ["X. " + LAST, LAST, P2_LAST], ["Entwurf", "Freigabe"],
         docx_doc("konzept.docx",
                  w_p("Dokumenthistorie", "Heading1")
                  + w_tbl([["Version", "Datum", "Autor", "Änderung"], ["V0.1", "1. September 2026", "X. " + LAST, "Entwurf"],
                           ["V1.0", "1. Oktober 2026", P2_LAST, "Freigabe"]])
                  + w_p("Einleitung", "Heading1") + w_p("%s und %s haben die Freigabe abgestimmt." % (LAST, P2_LAST))),
         expect=persons("X. " + LAST, P2_LAST)),
    case("docx-header-footer-learn", "docx header with <company GmbH>, footer with Erstellt von: <First Last> (detection only); the brand and the surname alone in the body",
         [BRAND2, LAST], ["Angebot abgestimmt"],
         docx_doc("angebot.docx", w_p("Angebot", "Title") + w_p("%s und %s haben das Angebot abgestimmt." % (BRAND2, LAST)),
                  header=["%s | Angebot 2026-17" % CO2], footer=["Erstellt von: %s" % PLANTED_PERSON])),
    case("docx-comment-author", "docx comment with author <First Last> and initials (detection only); the surname alone in the body",
         [P2, P2_LAST], ["Preis"],
         docx_doc("angebot.docx", w_commented("Der Preis gilt bis Ende 2026.") + w_p("%s prüft den Preis." % P2_LAST),
                  comment=(P2, "AQ", "Bitte Preis prüfen")), expect=persons(P2, P2_LAST)),
    case("docx-tracked-change-author", "docx tracked insertion and deletion by <First Last> (detection only); the surname alone in the body",
         [PLANTED_PERSON, LAST], ["Preis"],
         docx_doc("angebot.docx", w_tracked("Der Preis gilt ", "bis Ende 2026", "bis Ende 2025", ".", PLANTED_PERSON)
                  + w_p("%s hat den Absatz geändert." % LAST)), expect=persons(PLANTED_PERSON, LAST)),
    case("docx-footnote-company", "docx footnote Quelle: <company GmbH> (in the output); the brand alone in the body",
         [CO2, BRAND2], ["Erhebung 2026"],
         docx_doc("konzept.docx", w_footnoted("Die Zahlen stammen aus der Erhebung 2026.") + w_p("%s liefert die Zahlen." % BRAND2),
                  footnote="Quelle: %s, Erhebung 2026" % CO2), expect={CO2: "company"}),
    case("docx-prose-hr-fr", "docx body prose: Hr. <Last> und Fr. <Last> in a sentence",
         [LAST, P2_LAST], ["Workshop"],
         docx_doc("protokoll.docx", w_p("Hr. %s und Fr. %s nehmen am Workshop teil." % (LAST, P2_LAST))),
         expect=persons(LAST, P2_LAST)),
    # ------------------------------------------------------------------ round 2: the mechanisms of round 1 varied
    case("pptx-cover-vorgelegt-genehmigt-von", "pptx cover: Vorgelegt von <First Last> and Genehmigt von <First Last>, each with an <verb> am <date> line; both surnames alone on slide 2 (varies erstellt-von)",
         [P2, P2_LAST, P3, P3_LAST], ["Vorgelegt am", "Genehmigt am"],
         pptx_deck("angebot.pptx", [
             [("title", "Angebot für %s" % CUSTOMER), ("box", ["Vorgelegt von %s" % P2, "Vorgelegt am 1. Oktober 2026",
                                                                 "Genehmigt von %s" % P3, "Genehmigt am 2. Oktober 2026"])],
             [("title", "Workshop"), "%s und %s kommen zum Workshop." % (P2_LAST, P3_LAST)],
         ]), code=True, expect=persons(P2, P3)),
    case("pptx-agenda-hyphen-compounds", "pptx agenda: hyphenated German compounds as bullets (Ist-Situation, Soll-Konzept, Multi-Cloud-Strategie, Go-Live-Termin, Teams-Kanal, Backup-Konzept) (varies the identifier run rule)",
         [], ["Ist-Situation", "Soll-Konzept", "Multi-Cloud-Strategie", "Go-Live-Termin", "Teams-Kanal", "Backup-Konzept"],
         pptx_deck("agenda.pptx", [
             [("title", "Agenda"), ("box", ["Ist-Situation", "Soll-Konzept", "Multi-Cloud-Strategie", "Go-Live-Termin", "Backup-Konzept"])],
             [("title", "Kommunikation"), "Fragen bitte in den Teams-Kanal des Projekts."],
         ])),
    case("pptx-closing-vielen-dank", "pptx closing slide: Vielen Dank, Herzlichen Dank für Ihre Aufmerksamkeit, Noch Fragen? (varies the run rule on closing phrases)",
         [], ["Vielen Dank", "Herzlichen Dank", "Noch Fragen", "Fragen und Antworten"],
         pptx_deck("deck.pptx", [
             [("title", "Vielen Dank"), ("box", ["Herzlichen Dank für Ihre Aufmerksamkeit", "Noch Fragen?"])],
             [("title", "Fragen und Antworten"), "Offene Fragen bitte per Mail."],
         ])),
    case("pptx-orgchart-firstname-newline-roles", "pptx org chart boxes: first name, surname and role as three paragraphs, the role a single role word or a role with an acronym (varies firstname-newline)",
         [P3_FIRST, P3_LAST, P2_FIRST, P2_LAST], ["Projektleiter", "Leiterin IT"],
         pptx_deck("orga.pptx", [
             [("title", "Projektorganisation"), ("box", [P3_FIRST, P3_LAST, "Projektleiter"]), ("box", [P2_FIRST, P2_LAST, "Leiterin IT"])],
         ])),
    case("pptx-table-vendor-legal-names", "pptx table Hersteller | Produkt with vendor legal names (Microsoft Deutschland GmbH, Cisco Systems GmbH, Deutsche Telekom AG, Telekom Deutschland GmbH) (varies the partner legal name)",
         [], ["Microsoft Deutschland GmbH", "Cisco Systems GmbH", "Deutsche Telekom AG", "Telekom Deutschland GmbH", "Azure Arc", "Meraki vMX", "Magenta Cloud"],
         pptx_deck("hersteller.pptx", [
             [("title", "Hersteller im Bestand"), ("table", [
                 ["Hersteller", "Produkt"],
                 ["Microsoft Deutschland GmbH", "Azure Arc"],
                 ["Cisco Systems GmbH", "Meraki vMX"],
                 ["Deutsche Telekom AG", "Magenta Cloud"],
                 ["Telekom Deutschland GmbH", "Business VPN"],
             ])],
         ])),
]
