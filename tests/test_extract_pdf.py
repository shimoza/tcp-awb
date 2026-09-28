"""The pdf reader: page text through pdftotext -layout, metadata through pdfinfo, empty pages noted.

Fixture PDFs are drawn with reportlab. Two are written by hand because reportlab cannot set a custom key
in the document information dictionary, an XMP packet or a link target in the way needed here.
"""
from __future__ import annotations

from pathlib import Path

import pytest
from reportlab.lib.pagesizes import A4
from reportlab.pdfgen import canvas

from awb import register
from awb.extract import extract as dispatch
from awb.extract import pdf
from awb.matcher import Matcher
from awb.normalize import normalize
from tests import fixtures

CUSTOMER = fixtures.CUSTOMER_FORMS[0]
LAWFIRM = fixtures.LAWFIRM_FORMS[0]
PERSON = fixtures.PERSON_FORMS[0]
ORG = fixtures.ORG_FORMS[0]


@pytest.fixture
def matcher(register_path) -> Matcher:
    return Matcher(register.forms_for_matching(register.load(register_path)))


def _full_hits(m: Matcher, text: str, form: str) -> list[str]:
    """Matched strings (of the normalized text) that are the full form, whatever whitespace sits inside."""
    n = normalize(text)
    want = " ".join(form.split()).lower()
    return [s.form for s in m.find(n.text) if " ".join(s.form.split()).lower() == want]


def _clean_notes(ex) -> None:
    fixtures.assert_no_fixture_name("\n".join(ex.notes), "notes")


def _pages(path: Path, pages: list[list[tuple[float, float, str]]], rect_pages=(), **info) -> Path:
    """One PDF page per entry: (x, y, string) triples. Pages listed in rect_pages get a drawn rectangle."""
    c = canvas.Canvas(str(path), pagesize=A4)
    if "title" in info:
        c.setTitle(info["title"])
    if "author" in info:
        c.setAuthor(info["author"])
    if "subject" in info:
        c.setSubject(info["subject"])
    for i, lines in enumerate(pages, start=1):
        for x, y, s in lines:
            c.drawString(x, y, s)
        if i in rect_pages:
            c.rect(100, 100, 200, 200)
            c.line(100, 100, 300, 300)
        c.showPage()
    c.save()
    return path


def _hand_pdf(path: Path, page_text: str, info: str, xmp: bytes, uri: str) -> Path:
    """A one page PDF with a free information dictionary, an XMP packet and one link annotation."""
    content = b"BT /F1 12 Tf 72 750 Td (" + page_text.encode("latin-1") + b") Tj ET"
    objs = [
        b"<< /Type /Catalog /Pages 2 0 R /Metadata 6 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 595 842] /Contents 4 0 R"
        b" /Resources << /Font << /F1 5 0 R >> >> /Annots [8 0 R] >>",
        b"<< /Length %d >>\nstream\n" % len(content) + content + b"\nendstream",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
        b"<< /Type /Metadata /Subtype /XML /Length %d >>\nstream\n" % len(xmp) + xmp + b"\nendstream",
        info.encode("latin-1"),
        b"<< /Type /Annot /Subtype /Link /Rect [72 700 300 720] /Border [0 0 0]"
        b" /A << /S /URI /URI (" + uri.encode("ascii") + b") >> >>",
    ]
    out = b"%PDF-1.4\n"
    offsets = []
    for i, body in enumerate(objs, start=1):
        offsets.append(len(out))
        out += b"%d 0 obj\n" % i + body + b"\nendobj\n"
    xref = len(out)
    out += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objs) + 1)
    for off in offsets:
        out += b"%010d 00000 n \n" % off
    out += b"trailer\n<< /Size %d /Root 1 0 R /Info 7 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (len(objs) + 1, xref)
    path.write_bytes(out)
    return path


def _pdf_octal(s: str) -> str:
    """A PDF literal string body in PDFDocEncoding (Latin-1 here) with octal escapes for non-ASCII."""
    return "".join(c if ord(c) < 128 else "\\%03o" % ord(c) for c in s)


# --------------------------------------------------------------------------- text and metadata


def test_page_text_and_document_information(tmp_path, matcher):
    path = _pages(
        tmp_path / "angebot.pdf",
        [
            [(72, 750, "Rahmenvertrag zwischen der %s und dem Anbieter." % CUSTOMER),
             (72, 730, "Ansprechpartner ist %s am Standort Beispielstadt." % PERSON)],
            [(72, 750, "Anlage 1: Leistungsbeschreibung mit Load Balancer und Terraform.")],
        ],
        title="Angebot Rahmenvertrag Zyxwo",
        author=LAWFIRM,
        subject="Vergabe %s" % fixtures.TENDER_ID,
    )
    ex = pdf.extract(path)

    assert ex.kind == "pdf"
    assert ex.state == "ok"
    assert ex.notes == []
    assert ex.meta["page_count"] == 2
    assert ex.meta["title"] == "Angebot Rahmenvertrag Zyxwo"
    assert ex.meta["author"] == LAWFIRM
    assert ex.meta["subject"] == "Vergabe %s" % fixtures.TENDER_ID
    assert ex.meta["encrypted"] is False

    # the body is in the output text, page 1 before page 2
    assert CUSTOMER in ex.text and PERSON in ex.text
    assert ex.text.index("Rahmenvertrag") < ex.text.index("Anlage 1")
    # metadata is for detection only, it never goes into the output text
    assert LAWFIRM not in ex.text
    assert fixtures.TENDER_ID not in ex.text
    assert "author: %s" % LAWFIRM in ex.detect_text
    assert "title: Angebot Rahmenvertrag Zyxwo" in ex.detect_text

    found = {s.code for s in matcher.find(normalize(ex.detect_text).text)}
    assert fixtures.LAWFIRM_CODE in found
    assert fixtures.CUSTOMER_CODE + "-REF-2" in found
    assert fixtures.PERSON_CODE in found


def test_custom_information_xmp_and_link_targets_reach_detection(tmp_path, matcher):
    info = "<< /Title (Konzept\\nfuer %s) /Company (%s) /Author (Planung) >>" % (
        fixtures.CUSTOMER_FORMS[1],
        _pdf_octal(ORG),
    )
    xmp = (
        '<?xpacket begin="" id="W5M0MpCehiHzreSzNTczkc9d"?>'
        '<x:xmpmeta xmlns:x="adobe:ns:meta/"><rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#">'
        '<rdf:Description rdf:about="" xmlns:dc="http://purl.org/dc/elements/1.1/"'
        ' xmlns:pdf="http://ns.adobe.com/pdf/1.3/" pdf:Keywords="Standort Beispielstadt">'
        "<dc:creator><rdf:Seq><rdf:li>%s</rdf:li></rdf:Seq></dc:creator>"
        '</rdf:Description></rdf:RDF></x:xmpmeta><?xpacket end="w"?>' % PERSON
    ).encode("utf-8")
    uri = "https://portal.%s/login" % fixtures.CUSTOMER_DOMAIN
    page = "Technisches Konzept fuer das Rechenzentrum der Region Nord"
    path = _hand_pdf(tmp_path / "konzept.pdf", page, info, xmp, uri)

    ex = pdf.extract(path)

    assert ex.state == "ok", ex.notes
    assert ex.meta["custom"]["Company"] == ORG
    assert ex.meta["title"].endswith("fuer %s" % fixtures.CUSTOMER_FORMS[1])
    assert "\n" in ex.meta["title"], "a title over two lines keeps both lines"
    assert "company: %s" % ORG in ex.detect_text
    assert "xmp: %s" % PERSON in ex.detect_text
    assert "xmp: Standort Beispielstadt" in ex.detect_text
    assert "link: %s" % uri in ex.detect_text
    assert ex.meta["link_count"] == 1
    # none of it goes into the output text
    for value in (ORG, PERSON, uri, "Beispielstadt"):
        assert value not in ex.text
    found = {s.code for s in matcher.find(normalize(ex.detect_text).text)}
    assert {fixtures.ORG_CODE, fixtures.PERSON_CODE, fixtures.PLACE_CODE, fixtures.CUSTOMER_CODE + "-DOM-1"} <= found
    _clean_notes(ex)


# --------------------------------------------------------------------------- layout


def test_two_columns_keep_the_halves_of_a_name_on_one_line(tmp_path, matcher):
    left = ["Linke Spalte Zeile eins", "Linke Spalte Zeile zwei", "Auftrag Zyxwo", "Linke Spalte Zeile vier"]
    right = ["Rechte Spalte Zeile eins", "Rechte Spalte Zeile zwei", "Logistik GmbH liefert",
             "Rechte Spalte Zeile vier"]
    lines = []
    for i, (a, b) in enumerate(zip(left, right)):
        lines.append((72, 750 - 14 * i, a))
        lines.append((400, 750 - 14 * i, b))
    ex = pdf.extract(_pages(tmp_path / "spalten.pdf", [lines]))

    assert ex.state == "ok"
    hits = _full_hits(matcher, ex.text, CUSTOMER)
    assert len(hits) == 1, "the full form must be found in the layout text"
    gap = hits[0][len("Zyxwo"):-len("Logistik GmbH")]
    assert "\n" not in gap and len(gap) >= 2 and not gap.strip(), "the halves sit on one line, far apart"
    # each column keeps its own lines: the left column text is not glued into the right one
    assert any(line.startswith("Linke Spalte Zeile eins") and "Rechte Spalte Zeile eins" in line
               for line in ex.text.splitlines())


def test_name_hyphenated_inside_a_column_is_found_in_reading_order(tmp_path, matcher):
    left = ["Dieser Vertrag wird mit der", "Zyxwo Logis-", "tik GmbH geschlossen und gilt", "ab dem ersten Oktober."]
    right = ["Rechte Spalte Zeile %s" % w for w in ("eins", "zwei", "drei", "vier")]
    lines = []
    for i, (a, b) in enumerate(zip(left, right)):
        lines.append((72, 750 - 14 * i, a))
        lines.append((360, 750 - 14 * i, b))
    ex = pdf.extract(_pages(tmp_path / "trennung.pdf", [lines]))

    assert ex.state == "ok"
    assert _full_hits(matcher, ex.text, CUSTOMER) == [], "layout text alone does not rejoin the name"
    assert len(_full_hits(matcher, ex.detect_text, CUSTOMER)) == 1


# --------------------------------------------------------------------------- empty pages


def test_page_with_only_a_drawing_is_noted(tmp_path):
    path = _pages(
        tmp_path / "zeichnung.pdf",
        [
            [(72, 750, "Seite eins beschreibt die Anbindung an die Netzwerkbrücke.")],
            [],
            [(72, 750, "Seite drei beschreibt den Betrieb mit PostgreSQL im Cluster.")],
            [(72, 40, "Seite 4")],
        ],
        rect_pages=(2,),
    )
    ex = pdf.extract(path)

    assert ex.state == "ok"
    assert ex.meta["page_count"] == 4
    assert pdf.PAGE_NOTE % 2 == "page 2 has no extractable text, review the original"
    assert pdf.PAGE_NOTE % 2 in ex.notes
    assert pdf.PAGE_NOTE % 4 in ex.notes, "a page under 40 characters counts as no text"
    assert pdf.PAGE_NOTE % 1 not in ex.notes and pdf.PAGE_NOTE % 3 not in ex.notes
    assert pdf.ALL_EMPTY_NOTE not in ex.notes
    assert "Seite 4" in ex.text, "short text is still kept"
    assert "Netzwerkbrücke" in ex.text and "PostgreSQL" in ex.text


def test_document_where_every_page_is_a_drawing_is_unreadable(tmp_path):
    path = _pages(tmp_path / "scan.pdf", [[], [], []], rect_pages=(1, 2, 3))
    ex = pdf.extract(path)

    assert ex.state == "unreadable"
    assert ex.meta["page_count"] == 3
    for n in (1, 2, 3):
        assert pdf.PAGE_NOTE % n in ex.notes
    assert pdf.ALL_EMPTY_NOTE in ex.notes
    assert ex.text == ""
    # the dispatcher says the same
    assert dispatch(path).state == "unreadable"


# --------------------------------------------------------------------------- failures


def test_missing_poppler_is_failed(tmp_path, monkeypatch):
    path = _pages(tmp_path / "angebot.pdf", [[(72, 750, "Rahmenvertrag mit der %s ueber den Betrieb." % CUSTOMER)]])
    empty = tmp_path / "no-tools"
    empty.mkdir()
    monkeypatch.setenv("PATH", str(empty))

    ex = pdf.extract(path)

    assert ex.state == "failed"
    assert ex.notes == [pdf.MISSING_NOTE]
    assert ex.text == "" and ex.detect_text == ""


def test_damaged_pdf_is_failed_and_notes_carry_no_file_name(tmp_path):
    path = tmp_path / ("%s-Angebot.pdf" % fixtures.CUSTOMER_FORMS[1])
    path.write_bytes(b"%PDF-1.4\n" + b"\x00garbage\x01" * 50)
    ex = pdf.extract(path)

    assert ex.state == "failed"
    assert ex.notes
    _clean_notes(ex)
    assert "Angebot" not in "\n".join(ex.notes)


def test_password_protected_pdf_is_failed_with_a_note(tmp_path):
    from reportlab.lib.pdfencrypt import StandardEncryption

    path = tmp_path / "geschuetzt.pdf"
    c = canvas.Canvas(str(path), pagesize=A4, encrypt=StandardEncryption("user-secret", "owner-secret"))
    c.drawString(72, 750, "Vertraulich: Rahmenvertrag mit der %s ueber den Betrieb." % CUSTOMER)
    c.showPage()
    c.save()

    ex = pdf.extract(path)

    assert ex.state == "failed"
    assert ex.notes == ["pdf is encrypted with a password, review the original"]
    assert CUSTOMER not in ex.detect_text


def test_dispatcher_uses_this_reader(tmp_path):
    path = _pages(tmp_path / "x.bin", [[(72, 750, "Betriebskonzept fuer die %s im Rechenzentrum." % CUSTOMER)]],
                  author=LAWFIRM)
    ex = dispatch(path)

    assert ex.kind == "pdf"
    assert ex.state == "ok"
    assert CUSTOMER in ex.text
    assert ex.meta["author"] == LAWFIRM


# --------------------------------------------------------------------------- review findings (2026-09-22)


def test_annotation_and_outline_strings_reach_scan_text(tmp_path):
    path = tmp_path / "a.pdf"
    c = canvas.Canvas(str(path), pagesize=A4)
    c.drawString(72, 760, "angebot fuer das rechenzentrum ohne namen, mit genug text auf der seite.")
    c.textAnnotation(CUSTOMER, Rect=(72, 600, 200, 650))
    c.bookmarkPage("p1")
    c.addOutlineEntry(PERSON, "p1", level=0)
    c.save()
    ex = pdf.extract(path)
    assert ex.state == "ok"
    assert CUSTOMER in ex.scan_text and PERSON in ex.scan_text
    assert CUSTOMER not in ex.text


def test_strings_in_compressed_streams_and_hex_strings_are_read():
    import zlib

    body = zlib.compress(b"<< /T (" + ORG.encode("latin-1") + b") /V <" + CUSTOMER.encode().hex().encode() + b"> >>")
    raw = b"%PDF-1.7\n1 0 obj\n<< /Length 9 /Filter /FlateDecode >>\nstream\n" + body + b"\nendstream\nendobj\n"
    strings = pdf._strings_in(raw) + pdf._strings_in(zlib.decompress(body))
    assert ORG in strings and CUSTOMER in strings


def test_long_documents_are_read_with_one_call(tmp_path, monkeypatch):
    path = _pages(tmp_path / "lang.pdf", [[(72, 760, "seite %d mit genug text fuer die erkennung der seite" % i)]
                                          for i in range(1, 5)])
    calls = []
    real = pdf._run

    def spy(args):
        calls.append(args)
        return real(args)

    monkeypatch.setattr(pdf, "_run", spy)
    monkeypatch.setattr(pdf, "MAX_PAGE_CALLS", 2)
    ex = pdf.extract(path)
    assert ex.meta["page_count"] == 4 and ex.state == "ok"
    assert not [a for a in calls if "-f" in a], "no call per page above the page cap"
    assert "seite 4 mit genug text" in ex.text


def test_pages_after_the_deadline_are_noted_not_read(tmp_path, monkeypatch):
    path = _pages(tmp_path / "zwei.pdf", [[(72, 760, "seite %d mit genug text fuer die erkennung der seite" % i)]
                                          for i in range(1, 3)])
    monkeypatch.setattr(pdf, "DEADLINE", -1)
    ex = pdf.extract(path)
    assert ex.meta["incomplete"] is True
    assert "page 1 could not be read, review the original" in ex.notes
