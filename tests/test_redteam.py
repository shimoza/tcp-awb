"""What the red team of 2026-09-27 got through and what stops it now (calibration/redteam-2026-09-27.md).

The structured shapes live in awb/planted.py and run through the engine in tests/test_intake.py; here are the
readers' views, the normaliser's decodes, the matcher's glued halves, the encoded blocks of the text reader,
the sniffing and the pdf attachments. Every name comes from tests/fixtures.py.
"""
from __future__ import annotations

import base64
import zlib
from pathlib import Path

import pytest

from awb import images, intake, normalize, patterns
from awb.extract import pdf as pdf_reader
from awb.extract import sniff
from awb.extract import text as text_reader
from awb.matcher import Matcher
from awb import register
from tests import fixtures as fx

SHORT = fx.CUSTOMER_FORMS[1]
SURNAME = fx.PERSON_FORMS[1]
FULL = fx.CUSTOMER_FORMS[0]


def kinds(text: str) -> list[tuple[str, str]]:
    return [(s.cls, text[s.start:s.end]) for s in patterns.find_structured(text)]


# --------------------------------------------------------------------------- the view of the patterns


def test_a_value_broken_by_markup_is_found_whole():
    text = "IBAN DE89 3704<!-- x --> 0044 0532 0130 00 and mailhost<b></b>.example"
    got = kinds(text)
    assert ("iban", "DE89 3704<!-- x --> 0044 0532 0130 00") in got
    assert ("url", "mailhost<b></b>.example") in got


def test_an_address_in_angle_brackets_is_not_markup():
    text = "From: %s <tobias.beispielmann@%s>" % (fx.PERSON_FORMS[0], fx.CUSTOMER_DOMAIN)
    assert ("mail", "tobias.beispielmann@%s" % fx.CUSTOMER_DOMAIN) in kinds(text)


def test_cells_of_one_row_are_read_as_one_value():
    assert kinds("| 030 | 5551234 |") == [("phone", "030 | 5551234")]
    assert kinds("| 01 | 05 | 2026 |") == []


@pytest.mark.parametrize("text", [
    "files setup.py main.tf report.md libx.so archive.zip",
    "Tel.Nr and St.Nr and Bsp.AG in a form",
    "the host at 203.0.113.45 answered",
    "docs at docs.aws.amazon.com and learn.microsoft.com",
    "private 010.0.0.1 and 192.168.001.001 stay",
])
def test_the_new_hosts_keep_their_limits(text):
    assert [c for c, _ in kinds(text)] in ([], ["ip"])


def test_a_dotted_number_after_no_is_an_address_when_its_octets_are_large():
    assert ("ip", "203.0.113.9") in kinds("server no. 203.0.113.9 answers")
    assert kinds("item no. 3.1.4.2 of the contract") == []


# --------------------------------------------------------------------------- the normaliser


@pytest.mark.parametrize("text,plain", [
    ("&#81wxzv", "Qwxzv"),
    ("&#x00000051;wxzv", "Qwxzv"),
    ("\\u81?wxzv", "Qwxzv"),
    ("\\x51wxzv", "Qwxzv"),
    ("\x1b[01;31mQwxzv\x1b[m", "Qwxzv"),
    ("Qwx=\nzv =C3=A4", "Qwxzv ä"),
])
def test_new_decodes(text, plain):
    assert normalize.normalize(text).text == plain


def test_a_punycode_label_reads_as_its_letters():
    label = fx.ORG_FORMS[1].lower().encode("idna").decode("ascii")
    assert label.startswith("xn--")
    assert normalize.normalize("host %s.example" % label).text == "host %s.example" % fx.ORG_FORMS[1].lower()


def test_a_named_entity_without_semicolon_and_a_bare_soft_break_stay():
    assert normalize.normalize("&auml x").text == "&auml x"
    assert normalize.normalize("a =\nb").text == "a =\nb"


def test_strip_invisible_removes_what_a_viewer_acts_on():
    text = "kunde ‮" + SHORT[::-1] + "​ x\U000e0041\x1b[31m"
    assert normalize.strip_invisible(text) == "kunde " + SHORT[::-1] + " x"


def test_the_output_keeps_no_bidi_override_even_without_a_hit(home):
    f = home.inbox / "note.txt"
    f.write_text("kunde ‮nitsegoL owyxZ heute\n", encoding="utf-8")
    res = intake.run([f], fx.CUSTOMER_CODE, home)
    out = res.outputs[0].read_text(encoding="utf-8")
    assert "‮" not in out


# --------------------------------------------------------------------------- the matcher


@pytest.fixture
def m(register_path):
    return Matcher(register.forms_for_matching(register.load(register_path)))


@pytest.mark.parametrize("text", [
    "offer for %s<em\nclass=\"x\">%s</em> today" % (SHORT[:3], SHORT[3:]),
    "offer for %s<![CDATA[]]>%s today" % (SHORT[:3], SHORT[3:]),
    "offer for %s<?x?>%s today" % (SHORT[:3], SHORT[3:]),
    "offer for %s<span style=\"%s\">%s</span>" % (SHORT[:3], "a" * 300, SHORT[3:]),
    "host %s-%s01 is down" % (SHORT[:3].lower(), SHORT[3:].lower()),
    "code 01%s-%s today" % (SHORT[:3].lower(), SHORT[3:].lower()),
    "| %s | %sstr |" % (SURNAME[:8], SURNAME[8:]),
])
def test_split_forms_with_markup_or_glued_halves_are_found(m, text):
    assert m.find(text), text


def test_digits_of_another_script_and_look_alike_letters_fold(m):
    arabic = "".join(chr(0x660 + int(c)) if c.isdigit() else c for c in fx.FILE_NUMBER)
    text = "akte %s heute" % arabic
    assert [text[s.start:s.end] for s in m.find(text)] == [arabic]
    omega = SHORT.replace("w", "\u0461")
    assert m.find("kunde %s heute" % omega)


def test_a_short_form_is_not_read_into_the_next_word(m):
    text = "standort %s süd" % fx.PLACE_FORMS[0]
    assert [text[s.start:s.end] for s in m.find(text)] == [fx.PLACE_FORMS[0]]


# --------------------------------------------------------------------------- encoded blocks of the text reader


def test_hex_blocks_of_digits_and_binary_reach_detection():
    line = "call +49 30 5551234 now"
    assert text_reader.find_hex_blocks("blob %s end" % line.encode().hex()) == [line]
    assert text_reader.find_hex_blocks("blob 0x%s end" % FULL.encode().hex()) == [FULL]
    assert text_reader.find_hex_blocks("blob \\x%s end" % FULL.encode().hex()) == [FULL]
    binary = b"\x00\x01\x02\x03" + FULL.encode() + b"\x00\xff\xfe\x00"
    assert any(FULL in t for t in text_reader.encoded_texts("blob %s end" % binary.hex()))


def test_multiline_base64_of_binary_gives_its_strings():
    payload = zlib.compress(b"x" * 40) + b"\x00" + FULL.encode() + b"\x00" * 8
    b64 = base64.b64encode(payload).decode()
    lines = "\n".join(b64[i:i + 24] for i in range(0, len(b64), 24))
    assert any(FULL in t for t in text_reader.encoded_texts(lines))
    quoted = "\n".join("> " + l for l in lines.split("\n"))
    assert any(FULL in t for t in text_reader.encoded_texts(quoted))


def test_noscript_text_is_kept_and_svg_is_read_as_text_and_held(home):
    html = "<html><body><p>a</p><noscript>stage two of the plan</noscript></body></html>"
    assert "stage two of the plan" in text_reader.strip_html(html)
    svg = home.inbox / "logo.svg"
    svg.write_text('<svg xmlns="http://www.w3.org/2000/svg"><text><tspan>stage two of</tspan> the plan %s</text></svg>'
                   % SHORT, encoding="utf-8")
    assert sniff(svg) == "svg"
    res = intake.run([svg], fx.CUSTOMER_CODE, home)
    out = res.outputs[0].read_text(encoding="utf-8")
    assert "stage two of the plan" in out and SHORT not in out and "<svg" not in out
    assert images.held(home, fx.CUSTOMER_CODE)[0]["held"] == 1


# --------------------------------------------------------------------------- sniffing


def test_sniff_knows_a_prefixed_pdf_and_more_pictures(tmp_path):
    p = tmp_path / "a.pdf"
    p.write_bytes(b"junk\n" * 20 + b"%PDF-1.4\n1 0 obj << >> endobj\n%%EOF\n")
    assert sniff(p) == "pdf"
    heic = tmp_path / "a.heic"
    heic.write_bytes(b"\x00\x00\x00\x18ftypheic\x00\x00\x00\x00mif1heic" + b"\x00" * 64)
    assert sniff(heic) == "image"
    emf = tmp_path / "a.emf"
    emf.write_bytes(b"\x01\x00\x00\x00" + b"\x00" * 36 + b" EMF" + b"\x00" * 64)
    assert sniff(emf) == "image"


# --------------------------------------------------------------------------- pdf attachments and annotations


def _pdf(objects: list[bytes]) -> bytes:
    out = bytearray(b"%PDF-1.4\n")
    offsets = []
    for n, body in enumerate(objects, start=1):
        offsets.append(len(out))
        out += b"%d 0 obj\n" % n + body + b"\nendobj\n"
    xref = len(out)
    out += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objects) + 1)
    for off in offsets:
        out += b"%010d 00000 n \n" % off
    out += b"trailer\n<< /Root 1 0 R /Size %d >>\nstartxref\n%d\n%%%%EOF\n" % (len(objects) + 1, xref)
    return bytes(out)


def _stream(dictionary: bytes, data: bytes) -> bytes:
    return b"<< " + dictionary + b" /Length %d >>\nstream\n" % len(data) + data + b"\nendstream"


def test_embedded_files_are_read_as_children_and_annotations_reach_detection(tmp_path):
    note = ("stage two of the plan, customer %s\n" % SHORT).encode()
    page = b"BT /F1 12 Tf 20 100 Td (cover sheet, see the attachment) Tj ET"
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R /Names << /EmbeddedFiles << /Names [(note.txt) 5 0 R] >> >> >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 300 200] /Contents 4 0 R "
        b"/Resources << /Font << /F1 7 0 R >> >> /Annots [8 0 R] >>",
        _stream(b"", page),
        b"<< /Type /Filespec /F (note.txt) /UF (note.txt) /EF << /F 6 0 R >> >>",
        _stream(b"/Type /EmbeddedFile", note),
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
        b"<< /Type /Annot /Subtype /FreeText /Rect [10 10 100 30] /Contents (call +49 30 5551234) >>",
    ]
    path = tmp_path / "cover.pdf"
    path.write_bytes(_pdf(objects))
    ex = pdf_reader.extract(path)
    assert ex.state == "ok" and "cover sheet" in ex.text
    assert len(ex.children) == 1 and "stage two of the plan" in ex.children[0].text
    assert ex.children[0].meta["attachment"] == "note.txt"
    assert "contents: call +49 30 5551234" in ex.detect_text
    assert any("embedded file(s) read as attachments" in n for n in ex.notes)
    assert any("annotations" in n for n in ex.notes)


def test_an_embedded_file_whose_name_climbs_out_stays_inside_the_folder(tmp_path, monkeypatch):
    from awb.extract import temp_root

    note = b"stage two of the plan\n"
    page = b"BT /F1 12 Tf 20 100 Td (cover sheet) Tj ET"
    evil = b"../../escaped-attachment.txt"
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R /Names << /EmbeddedFiles << /Names [(" + evil + b") 5 0 R] >> >> >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 300 200] /Contents 4 0 R "
        b"/Resources << /Font << /F1 7 0 R >> >> >>",
        _stream(b"", page),
        b"<< /Type /Filespec /F (" + evil + b") /UF (" + evil + b") /EF << /F 6 0 R >> >>",
        _stream(b"/Type /EmbeddedFile", note),
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    path = tmp_path / "cover.pdf"
    path.write_bytes(_pdf(objects))
    vault = tmp_path / "vault"
    vault.mkdir()
    with temp_root(vault / "tmp"):
        ex = pdf_reader.extract(path)
    assert len(ex.children) == 1 and "stage two of the plan" in ex.children[0].text
    assert ex.children[0].meta["attachment"] == "../../escaped-attachment.txt"
    assert not list(tmp_path.rglob("escaped-attachment.txt"))


def test_pictures_of_one_file_have_a_total_budget(tmp_path, monkeypatch):
    import zipfile
    monkeypatch.setattr(images, "MAX_TOTAL_IMAGE_BYTES", 10_000)
    src = tmp_path / "many.docx"
    with zipfile.ZipFile(src, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("word/document.xml", "<w:document/>")
        for n in range(5):
            z.writestr("word/media/image%d.bmp" % n, b"\x00" * 4_000)
    found, more = images.pictures(src, "docx")
    assert len(found) == 2 and more == 3
