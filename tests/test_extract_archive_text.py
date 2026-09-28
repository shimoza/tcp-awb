"""Sniffing, dispatch, text files and archives.

Every file is built here from invented fixture names. Temporary extraction folders are checked by
pointing `tempfile` at a folder of the test.
"""
from __future__ import annotations

import base64
import re
import gzip
import io
import os
import struct
import sys
import tarfile
import tempfile
import types
import zipfile
import zlib
from pathlib import Path

import pytest

import awb.extract as ext
from awb.extract import IMAGE_NOTE, OLE_NOTE, Extraction, extract, sniff
from awb.extract import archive
from tests import fixtures as fx

CUSTOMER = fx.CUSTOMER_FORMS[0]
CUSTOMER_SHORT = fx.CUSTOMER_FORMS[1]
PERSON = fx.PERSON_FORMS[0]
PERSON_SHORT = fx.PERSON_FORMS[1]
ORG = fx.ORG_FORMS[0]
PLACE = fx.PLACE_FORMS[0]

SENTENCE = "ansprechpartner ist %s bei %s in %s." % (PERSON, CUSTOMER, PLACE)
UMLAUT_LINE = "grüße aus %s an %s, straße 1" % (PLACE, ORG)


def walk(ex: Extraction):
    yield ex
    for child in ex.children:
        yield from walk(child)


def assert_clean_notes(ex: Extraction) -> None:
    for e in walk(ex):
        fx.assert_no_fixture_name("\n".join(e.notes), "notes")


def make_zip(path: Path, members: dict[str, bytes]) -> Path:
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, data in members.items():
            zf.writestr(name, data)
    return path


def zip_bytes(members: dict[str, bytes]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, data in members.items():
            zf.writestr(name, data)
    return buf.getvalue()


def png_bytes() -> bytes:
    """A valid 1x1 grey PNG."""

    def chunk(kind: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)

    ihdr = struct.pack(">IIBBBBB", 1, 1, 8, 0, 0, 0, 0)
    idat = zlib.compress(b"\x00\x80")
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr) + chunk(b"IDAT", idat) + chunk(b"IEND", b"")


@pytest.fixture
def private_tmp(tmp_path, monkeypatch) -> Path:
    """Point tempfile at a folder of the test so that leftovers can be counted."""
    folder = tmp_path / "tmp"
    folder.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(folder))
    return folder


# --------------------------------------------------------------------------- text


def test_utf16_le_with_bom_decodes_umlauts(tmp_path):
    path = tmp_path / "brief.txt"
    path.write_bytes(b"\xff\xfe" + UMLAUT_LINE.encode("utf-16-le"))
    assert sniff(path) == "text"
    ex = extract(path)
    assert ex.state == "ok"
    assert ex.text == UMLAUT_LINE
    assert ex.meta["encoding"] == "utf-16-le"


def test_utf16_le_without_bom_decodes(tmp_path):
    path = tmp_path / "brief.txt"
    path.write_bytes(UMLAUT_LINE.encode("utf-16-le"))
    assert sniff(path) == "text"
    ex = extract(path)
    assert ex.text == UMLAUT_LINE
    # plain ASCII in UTF-16 is valid UTF-8 with NUL characters: must still be read as UTF-16
    ascii_path = tmp_path / "ascii.txt"
    ascii_path.write_bytes(("kunde %s" % CUSTOMER_SHORT).encode("utf-16-le"))
    ex = extract(ascii_path)
    assert ex.text == "kunde %s" % CUSTOMER_SHORT
    assert "\x00" not in ex.text


def test_latin1_decodes_umlauts(tmp_path):
    path = tmp_path / "alt.txt"
    path.write_bytes((UMLAUT_LINE + "\nmaß und größe\n").encode("latin-1"))
    assert sniff(path) == "text"
    ex = extract(path)
    assert ex.state == "ok"
    assert UMLAUT_LINE in ex.text
    assert "maß und größe" in ex.text
    assert ex.meta["encoding"] in ("cp1252", "latin-1")


def test_utf8_with_crlf(tmp_path):
    path = tmp_path / "win.txt"
    path.write_bytes(("zeile eins\r\n%s\r\n" % UMLAUT_LINE).encode("utf-8"))
    ex = extract(path)
    assert ex.text == "zeile eins\n%s\n" % UMLAUT_LINE


def test_base64_block_is_decoded_into_detect_text(tmp_path):
    block = base64.b64encode(SENTENCE.encode("utf-8")).decode("ascii")
    assert len(block) >= 40
    path = tmp_path / "log.txt"
    path.write_text("start\npayload=%s\nende\n" % block, encoding="utf-8")
    ex = extract(path)
    assert ex.state == "ok"
    assert SENTENCE in ex.detect_text
    assert "1 base64 block(s) decoded into detect text" in ex.notes
    # the output never keeps an encoded name: the decoded text takes the place of the block
    assert block not in ex.text
    assert SENTENCE in ex.text
    assert_clean_notes(ex)


def test_mime_style_base64_block_over_several_lines(tmp_path):
    long_text = (SENTENCE + "\n") * 6
    wrapped = base64.encodebytes(long_text.encode("utf-8")).decode("ascii")
    assert wrapped.count("\n") > 3
    path = tmp_path / "mail-teil.txt"
    path.write_text("content-transfer-encoding: base64\n\n" + wrapped + "\n--ende--\n", encoding="utf-8")
    ex = extract(path)
    assert ex.detect_text.count(SENTENCE) >= 6
    assert "[decoded base64 block 1]" in ex.text
    assert wrapped.splitlines()[0] not in ex.text


def test_binary_base64_block_is_noted_not_expanded(tmp_path):
    blob = base64.b64encode(bytes(range(256)) * 2).decode("ascii")
    path = tmp_path / "bin.txt"
    path.write_text("daten: %s\n" % blob, encoding="utf-8")
    ex = extract(path)
    assert "1 base64 block(s) decode to binary, not expanded" in ex.notes
    assert blob in ex.text


def test_hex_hash_is_not_treated_as_base64(tmp_path):
    path = tmp_path / "hash.txt"
    path.write_text("sha256 " + "ab12" * 16 + "\n", encoding="utf-8")
    ex = extract(path)
    assert not any("base64" in n for n in ex.notes)


def test_html_tags_are_stripped(tmp_path):
    page = (
        "<!doctype html><html><head><title>angebot</title>"
        '<meta name="author" content="%s"></head><body>'
        "<p>vertrag mit <b>%s</b></p>"
        '<p><a href="https://%s/portal">portal</a></p>'
        '<script>var kunde = "%s";</script>'
        "</body></html>"
    ) % (PERSON, CUSTOMER, fx.CUSTOMER_DOMAIN, PERSON_SHORT)
    path = tmp_path / "seite.html"
    path.write_text(page, encoding="utf-8")
    assert sniff(path) == "html"
    ex = extract(path)
    assert ex.kind == "html"
    assert "vertrag mit %s" % CUSTOMER in ex.text
    assert "<" not in ex.text and ">" not in ex.text
    assert "var kunde" not in ex.text
    assert ex.meta["author"] == PERSON
    assert "https://%s/portal" % fx.CUSTOMER_DOMAIN in ex.detect_text
    assert "author: %s" % PERSON in ex.detect_text


def test_html_without_suffix_is_found_by_content(tmp_path):
    path = tmp_path / "export"
    path.write_text("<html><body><div>%s</div></body></html>" % CUSTOMER, encoding="utf-8")
    assert sniff(path) == "html"
    assert extract(path).text == CUSTOMER


def test_prose_with_angle_brackets_stays_text(tmp_path):
    body = "wenn a < b und c > d, dann gilt <p nicht als tag\n"
    path = tmp_path / "notiz.txt"
    path.write_text(body, encoding="utf-8")
    ex = extract(path)
    assert ex.kind == "text"
    assert ex.text == body


def test_csv_becomes_a_markdown_table(tmp_path):
    path = tmp_path / "liste.csv"
    path.write_text("rolle;firma\nkunde;%s\n" % CUSTOMER, encoding="utf-8")
    ex = extract(path)
    assert ex.kind == "csv"
    assert "| kunde | %s |" % CUSTOMER in ex.text.splitlines()


def test_empty_file_is_unreadable(tmp_path):
    path = tmp_path / "leer.txt"
    path.write_bytes(b"")
    ex = extract(path)
    assert ex.state == "unreadable"
    assert ex.text == ""


# --------------------------------------------------------------------------- sniffing


def test_zip_renamed_txt_is_sniffed_as_zip(tmp_path):
    path = make_zip(tmp_path / "harmlos.txt", {"a.txt": b"eins"})
    assert sniff(path) == "zip"
    ex = extract(path)
    assert ex.kind == "zip"
    assert ex.children[0].text == "eins"


def test_text_renamed_png_is_sniffed_as_text(tmp_path):
    path = tmp_path / "bild.png"
    path.write_text("kunde: %s\n" % CUSTOMER, encoding="utf-8")
    assert sniff(path) == "text"
    ex = extract(path)
    assert ex.state == "ok"
    assert CUSTOMER in ex.text


def test_text_starting_with_bm_is_not_an_image(tmp_path):
    path = tmp_path / "notiz.txt"
    path.write_text("BM termin mit %s\n" % PERSON, encoding="utf-8")
    assert sniff(path) == "text"


def test_real_png_is_unreadable_with_the_image_note(tmp_path):
    path = tmp_path / "scan.png"
    path.write_bytes(png_bytes())
    assert sniff(path) == "image"
    ex = extract(path)
    assert ex.kind == "image"
    assert ex.state == "unreadable"
    assert ex.notes == [IMAGE_NOTE]
    assert ex.text == "" and ex.detect_text == ""
    # also when the name lies
    lying = tmp_path / "scan.txt"
    lying.write_bytes(png_bytes())
    assert extract(lying).state == "unreadable"


def test_ole_header_is_unsupported(tmp_path):
    ole = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1" + b"\x00" * 504
    doc = tmp_path / "alt.doc"
    doc.write_bytes(ole)
    ex = extract(doc)
    assert ex.state == "unsupported"
    assert ex.notes == [OLE_NOTE]
    msg = tmp_path / "nachricht.msg"
    msg.write_bytes(ole)
    assert sniff(msg) == "msg"
    ex = extract(msg)
    assert ex.kind == "msg"
    assert ex.state == "unsupported"


def test_unknown_binary_is_unsupported(tmp_path):
    path = tmp_path / "daten.bin"
    path.write_bytes(bytes(range(1, 32)) * 40)
    assert sniff(path) == "unknown"
    assert extract(path).state == "unsupported"


def test_missing_file_fails(tmp_path):
    ex = extract(tmp_path / "gibt-es-nicht.txt")
    assert ex.state == "failed"


# --------------------------------------------------------------------------- dispatch to pdf and mail


def test_missing_pdf_reader_gives_failed(tmp_path, monkeypatch):
    monkeypatch.setitem(sys.modules, "awb.extract.pdf", None)
    path = tmp_path / "angebot.pdf"
    path.write_bytes(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n1 0 obj\n<<>>\nendobj\ntrailer\n<<>>\n%%EOF\n")
    assert sniff(path) == "pdf"
    ex = extract(path)
    assert ex.kind == "pdf"
    assert ex.state == "failed"
    assert ex.notes == ["pdf reader is not available"]


def test_missing_mail_reader_gives_failed(tmp_path, monkeypatch):
    monkeypatch.setitem(sys.modules, "awb.extract.mail", None)
    path = tmp_path / "nachricht.eml"
    path.write_text(
        "From: a@%s\nTo: b@%s\nSubject: angebot\nDate: Tue, 22 Sep 2026 08:00:00 +0200\n\nhallo\n"
        % (fx.CUSTOMER_DOMAIN, fx.CUSTOMER_DOMAIN),
        encoding="utf-8",
    )
    assert sniff(path) == "eml"
    ex = extract(path)
    assert ex.kind == "eml"
    assert ex.state == "failed"
    assert ex.notes == ["mail reader is not available"]


def test_mail_headers_without_suffix_are_sniffed_as_eml(tmp_path):
    path = tmp_path / "weitergeleitet"
    path.write_text("Subject: angebot\nFrom: a@%s\nTo: b@%s\n\ntext\n" % ((fx.CUSTOMER_DOMAIN,) * 2), encoding="utf-8")
    assert sniff(path) == "eml"


def test_reader_is_called_with_depth(tmp_path, monkeypatch):
    calls = []
    fake = types.ModuleType("awb.extract.mail")

    def extract_mail(path, depth=0):
        calls.append(depth)
        return Extraction(Path(path), "eml", "ok", text="hallo", detect_text="hallo")

    extract_mail.__module__ = "awb.extract.mail"
    fake.extract_mail = extract_mail
    monkeypatch.setitem(sys.modules, "awb.extract.mail", fake)
    path = tmp_path / "nachricht.eml"
    path.write_text("From: a@b.example\nTo: c@d.example\nSubject: x\n\nhallo\n", encoding="utf-8")
    ex = extract(path, depth=2)
    assert ex.state == "ok"
    assert calls == [2]


def test_reader_that_imported_extract_is_not_called_back(tmp_path, monkeypatch):
    fake = types.ModuleType("awb.extract.mail")
    fake.extract = ext.extract        # imported for attachments, not an entry point
    monkeypatch.setitem(sys.modules, "awb.extract.mail", fake)
    path = tmp_path / "nachricht.eml"
    path.write_text("From: a@b.example\nTo: c@d.example\nSubject: x\n\nhallo\n", encoding="utf-8")
    ex = extract(path)
    assert ex.state == "failed"
    assert ex.notes == ["mail reader has no entry point"]


def test_reader_exception_gives_failed_without_values(tmp_path, monkeypatch):
    fake = types.ModuleType("awb.extract.pdf")

    def extract_pdf(path):
        raise ValueError("secret value %s" % CUSTOMER)

    extract_pdf.__module__ = "awb.extract.pdf"
    fake.extract_pdf = extract_pdf
    monkeypatch.setitem(sys.modules, "awb.extract.pdf", fake)
    path = tmp_path / "a.pdf"
    path.write_bytes(b"%PDF-1.4\n%%EOF\n")
    ex = extract(path)
    assert ex.state == "failed"
    assert ex.notes == ["ValueError while reading"]


# --------------------------------------------------------------------------- archives


def test_zip_member_names_in_detect_text(tmp_path, private_tmp):
    names = {
        "%s/angebot.txt" % CUSTOMER_SHORT: b"inhalt eins",
        "vertrag_%s.txt" % PERSON_SHORT: b"inhalt zwei",
    }
    ex = extract(make_zip(tmp_path / "paket.zip", names))
    assert ex.kind == "zip"
    assert ex.state == "ok"
    for name in names:
        assert name in ex.detect_text
        assert name in ex.text
    assert ex.meta["member_count"] == 2
    assert sorted(c.meta["member"] for c in ex.children) == sorted(names)
    assert sorted(c.text for c in ex.children) == ["inhalt eins", "inhalt zwei"]
    assert_clean_notes(ex)


def test_zip_inside_zip_gives_grandchildren(tmp_path, private_tmp):
    inner = zip_bytes({"innen/%s.txt" % PLACE: SENTENCE.encode("utf-8")})
    ex = extract(make_zip(tmp_path / "aussen.zip", {"innen.zip": inner, "readme.txt": b"hallo"}))
    zips = [c for c in ex.children if c.kind == "zip"]
    assert len(zips) == 1
    child = zips[0]
    assert child.meta["member"] == "innen.zip"
    assert "innen/%s.txt" % PLACE in child.detect_text
    assert len(child.children) == 1
    grandchild = child.children[0]
    assert grandchild.text == SENTENCE
    assert grandchild.meta["member"] == "innen/%s.txt" % PLACE
    assert os.listdir(private_tmp) == []


def test_nesting_stops_at_the_depth_limit(tmp_path, private_tmp):
    data = zip_bytes({"tief.txt": b"ganz unten"})
    for level in range(5):
        data = zip_bytes({"ebene%d.zip" % level: data})
    path = tmp_path / "tief.zip"
    path.write_bytes(data)
    ex = extract(path)
    chain = [ex]
    while chain[-1].children:
        chain.append(chain[-1].children[0])
    assert len(chain) == ext.MAX_ARCHIVE_DEPTH + 1
    assert any("nesting depth" in n for n in chain[-1].notes)
    assert chain[-1].detect_text  # members still listed


def test_tar_gz_works(tmp_path, private_tmp):
    path = tmp_path / "sicherung.tar.gz"
    with tarfile.open(path, "w:gz") as tf:
        for name, data in (("%s/liste.txt" % CUSTOMER_SHORT, SENTENCE.encode("utf-8")), ("b.txt", b"zwei")):
            info = tarfile.TarInfo(name)
            info.size = len(data)
            tf.addfile(info, io.BytesIO(data))
    assert sniff(path) == "tar"
    ex = extract(path)
    assert ex.state == "ok"
    assert "%s/liste.txt" % CUSTOMER_SHORT in ex.detect_text
    assert sorted(c.text for c in ex.children) == sorted([SENTENCE, "zwei"])
    assert os.listdir(private_tmp) == []


def test_single_gzip_file_is_read_as_one_member(tmp_path, private_tmp):
    path = tmp_path / "bericht.txt.gz"
    path.write_bytes(gzip.compress(SENTENCE.encode("utf-8")))
    ex = extract(path)
    assert ex.state == "ok"
    assert len(ex.children) == 1
    assert ex.children[0].text == SENTENCE


def test_more_than_200_members_stops_with_a_note(tmp_path, private_tmp):
    members = {"datei-%03d.txt" % i: b"x%d" % i for i in range(archive.MAX_MEMBERS + 5)}
    ex = extract(make_zip(tmp_path / "viele.zip", members))
    assert ex.meta["member_count"] == archive.MAX_MEMBERS
    assert len(ex.children) == archive.MAX_MEMBERS
    assert "member limit of 200 reached, 200 of 205 members listed" in ex.notes
    assert "datei-204.txt" not in ex.detect_text
    assert os.listdir(private_tmp) == []


def test_member_limit_counts_nested_archives_too(tmp_path, private_tmp):
    inner = zip_bytes({"f-%03d.txt" % i: b"y" for i in range(90)})
    ex = extract(make_zip(tmp_path / "bombe.zip", {"a.zip": inner, "b.zip": inner, "c.zip": inner}))
    total = sum(e.meta.get("member_count", 0) for e in walk(ex) if e.kind == "zip")
    assert total == archive.MAX_MEMBERS
    assert any("limit" in n for n in ex.notes)


def test_size_limit_stops_extraction(tmp_path, private_tmp, monkeypatch):
    monkeypatch.setattr(archive, "MAX_TOTAL_BYTES", 1000)
    ex = extract(make_zip(tmp_path / "gross.zip", {"a.txt": b"a" * 600, "b.txt": b"b" * 600, "c.txt": b"c"}))
    assert len(ex.children) == 1
    assert any("size limit" in n for n in ex.notes)
    assert ex.meta["member_count"] == 3
    assert os.listdir(private_tmp) == []


def test_temporary_folders_are_private_and_gone(tmp_path, private_tmp, monkeypatch):
    modes = []
    original = ext.extract

    def spy(path, depth=0):
        modes.append(os.stat(Path(path).parent).st_mode & 0o777)
        assert Path(path).is_relative_to(private_tmp)
        return original(path, depth)

    monkeypatch.setattr(ext, "extract", spy)
    inner = zip_bytes({"x.txt": b"x"})
    ex = original(make_zip(tmp_path / "p.zip", {"a.txt": b"a", "i.zip": inner}))
    assert modes and all(m == 0o700 for m in modes)
    assert os.listdir(private_tmp) == []
    for child in walk(ex):
        if child is not ex:
            assert not child.path.exists()


def test_member_names_cannot_escape_the_temporary_folder(tmp_path, private_tmp):
    path = tmp_path / "boese.zip"
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("../../ausbruch.txt", b"nein")
        zf.writestr("/abs/pfad.txt", b"auch nicht")
    ex = extract(path)
    assert "../../ausbruch.txt" in ex.detect_text
    assert len(ex.children) == 2
    assert not (tmp_path / "ausbruch.txt").exists()
    assert not (private_tmp.parent / "ausbruch.txt").exists()
    assert os.listdir(private_tmp) == []


def test_broken_zip_fails(tmp_path, private_tmp):
    path = tmp_path / "kaputt.zip"
    path.write_bytes(b"PK\x03\x04" + b"\x01" * 50)
    ex = extract(path)
    assert ex.state == "failed"
    assert os.listdir(private_tmp) == []


# --------------------------------------------------------------------------- review findings (2026-09-22)


def _cp1252_zip(path: Path, name: str, data: bytes = b"inhalt") -> Path:
    """A zip whose member name is written in cp1252 without the UTF-8 flag (as some Windows tools do)."""
    placeholder = "p" * len(name.encode("cp1252"))
    make_zip(path, {placeholder: data})
    raw = path.read_bytes().replace(placeholder.encode("ascii"), name.encode("cp1252"))
    path.write_bytes(raw)
    return path


def _set_encrypted_flag(path: Path) -> Path:
    """Mark every member as encrypted in the local and the central header (bit 0 of the flags)."""
    raw = bytearray(path.read_bytes())
    for sig, off in ((b"PK\x03\x04", 6), (b"PK\x01\x02", 8)):
        i = raw.find(sig)
        while i >= 0:
            raw[i + off] |= 0x01
            i = raw.find(sig, i + 4)
    path.write_bytes(bytes(raw))
    return path


def test_zip_name_in_cp1252_is_decoded_as_cp1252(tmp_path, private_tmp):
    name = "%s %s.txt" % (fx.PLANTED_CANDIDATE.split()[0], ORG.split()[1])
    ex = extract(_cp1252_zip(tmp_path / "a.zip", name))
    assert ex.children and ex.children[0].meta["member"] == name
    assert name in ex.text and name in ex.detect_text


def test_zip_in_cp437_keeps_cp437(tmp_path, private_tmp):
    path = make_zip(tmp_path / "b.zip", {"x": b"y"})
    raw = path.read_bytes().replace(b"x", "ö".encode("cp437"))
    path.write_bytes(raw)
    assert extract(path).children[0].meta["member"] == "ö"


def test_zip_comments_go_to_detection(tmp_path, private_tmp):
    path = tmp_path / "c.zip"
    with zipfile.ZipFile(path, "w") as zf:
        info = zipfile.ZipInfo("a.txt")
        info.comment = PERSON.encode("utf-8")
        zf.writestr(info, "x")
        zf.comment = CUSTOMER.encode("utf-8")
    ex = extract(path)
    assert CUSTOMER in ex.detect_text and PERSON in ex.detect_text
    assert CUSTOMER not in ex.text


def test_names_past_the_member_limit_are_scanned_and_the_walk_is_incomplete(tmp_path, private_tmp):
    members = {"datei-%03d.txt" % i: b"x" for i in range(archive.MAX_MEMBERS)}
    members["%s.txt" % CUSTOMER] = b"x"
    ex = extract(make_zip(tmp_path / "viele.zip", members))
    assert "%s.txt" % CUSTOMER not in ex.detect_text
    assert "%s.txt" % CUSTOMER in ex.scan_text
    assert ex.meta[ext.INCOMPLETE] is True and ext.is_incomplete(ex)


def test_encrypted_member_makes_the_walk_incomplete(tmp_path, private_tmp):
    path = _set_encrypted_flag(make_zip(tmp_path / "e.zip", {"a.txt": b"geheim"}))
    ex = extract(path)
    assert ex.state == "ok"
    assert ex.meta[ext.INCOMPLETE] is True


def test_nesting_past_the_depth_limit_is_incomplete(tmp_path, private_tmp):
    data = SENTENCE.encode()
    for level in range(ext.MAX_ARCHIVE_DEPTH + 2):
        data = zip_bytes({"level%d.%s" % (level, "zip" if level else "txt"): data})
    ex = extract(_write(tmp_path / "tief.zip", data))
    assert ext.is_incomplete(ex)
    assert not ex.meta.get(ext.INCOMPLETE), "the outer levels were read in full"


def _write(path: Path, data: bytes) -> Path:
    path.write_bytes(data)
    return path


def test_tar_owner_names_and_link_targets_go_to_detection(tmp_path, private_tmp):
    path = tmp_path / "a.tar"
    with tarfile.open(path, "w") as tf:
        info = tarfile.TarInfo("a.txt")
        info.size = 5
        info.uname = PERSON_SHORT.lower()
        info.gname = CUSTOMER_SHORT.lower()
        tf.addfile(info, io.BytesIO(b"hallo"))
        link = tarfile.TarInfo("verweis")
        link.type = tarfile.SYMTYPE
        link.linkname = "/srv/%s/daten" % CUSTOMER_SHORT
        tf.addfile(link)
    ex = extract(path)
    for value in (PERSON_SHORT.lower(), CUSTOMER_SHORT.lower(), "/srv/%s/daten" % CUSTOMER_SHORT):
        assert value in ex.detect_text
        assert value not in ex.text


def test_single_compressed_file_is_listed_without_its_name(tmp_path, private_tmp):
    path = tmp_path / ("%s-Angebot.txt.gz" % fx.PLANTED_CANDIDATE.split()[0])
    buf = io.BytesIO()
    with gzip.GzipFile(filename="%s.txt" % PERSON, mode="wb", fileobj=buf, mtime=0) as gz:
        gz.write(SENTENCE.encode())
    path.write_bytes(buf.getvalue())
    ex = extract(path)
    assert ex.children[0].meta["member"] == "member.txt"
    assert fx.PLANTED_CANDIDATE.split()[0] not in ex.text
    # the original name in the gzip header goes to detection only
    assert "%s.txt" % PERSON in ex.detect_text and PERSON not in ex.text


def test_tar_walk_stops_after_the_size_limit(tmp_path, private_tmp, monkeypatch):
    monkeypatch.setattr(archive, "MAX_TOTAL_BYTES", 1000)
    path = tmp_path / "a.tar.gz"
    with tarfile.open(path, "w:gz") as tf:
        for name, size in (("a.txt", 600), ("b.txt", 600), ("c.txt", 1), ("d.txt", 1)):
            info = tarfile.TarInfo(name)
            info.size = size
            tf.addfile(info, io.BytesIO(b"x" * size))
    ex = extract(path)
    assert ex.meta["member_count"] == 2
    assert ex.meta[ext.INCOMPLETE] is True


def test_tar_listing_at_the_depth_limit_has_a_read_budget(tmp_path, private_tmp, monkeypatch):
    monkeypatch.setattr(archive, "MAX_TOTAL_BYTES", 1000)
    path = tmp_path / "a.tar.gz"
    with tarfile.open(path, "w:gz") as tf:
        for i in range(5):
            info = tarfile.TarInfo("f%d.txt" % i)
            info.size = 400
            tf.addfile(info, io.BytesIO(b"z" * 400))
    ex = archive.extract_archive(path, "tar", ext.MAX_ARCHIVE_DEPTH)
    assert ex.meta["member_count"] == 3, "the walk stops once the declared data passes the read budget"
    assert any("read limit" in n for n in ex.notes)


def test_temporary_member_files_carry_an_index_only(tmp_path, private_tmp, monkeypatch):
    seen = []
    real = ext.extract

    def spy(path, depth=0):
        seen.append(Path(path).name)
        return real(path, depth)

    monkeypatch.setattr(ext, "extract", spy)
    make_zip(tmp_path / "n.zip", {"%s_Angebot.txt" % CUSTOMER_SHORT: b"x", "ordner/%s.csv" % PERSON: b"a;b"})
    real(tmp_path / "n.zip")
    assert seen and all(re.fullmatch(r"\d{4}(\.[A-Za-z0-9]+)?", n) for n in seen), seen


def test_temp_root_puts_reader_folders_under_it(tmp_path, private_tmp):
    root = tmp_path / "vault-tmp"
    with ext.temp_root(root):
        made = ext.make_temp_dir("awb-")
        assert made.parent == root
        made.rmdir()
    assert oct(root.stat().st_mode & 0o777) == "0o700"
    assert ext.make_temp_dir("awb-").parent == private_tmp


# text reader


def test_short_and_latin1_and_utf16_base64_blocks_are_decoded(tmp_path):
    blocks = [base64.b64encode(CUSTOMER.encode()).decode(),
              base64.b64encode(ORG.encode("latin-1")).decode(),
              base64.b64encode(CUSTOMER.encode("utf-16-le")).decode()]
    assert len(blocks[0]) < 40
    path = tmp_path / "b.txt"
    path.write_text("\n".join("tag %s" % b for b in blocks) + "\n", encoding="utf-8")
    ex = extract(path)
    assert ex.detect_text.count(CUSTOMER) >= 2 and ORG in ex.detect_text
    for b in blocks:
        assert b not in ex.text


def test_hex_block_of_text_is_decoded(tmp_path):
    hexed = CUSTOMER.encode().hex()
    path = tmp_path / "h.txt"
    path.write_text("wert %s ende\n" % hexed, encoding="utf-8")
    ex = extract(path)
    assert CUSTOMER in ex.detect_text and hexed not in ex.text
    assert "1 hex block(s) decoded into detect text" in ex.notes


def test_strings_of_a_binary_payload_reach_scan_text(tmp_path):
    blob = base64.b64encode(b"\x01\x02\x03\x04\x05" + CUSTOMER.encode()).decode()
    path = tmp_path / "bin.txt"
    path.write_text("daten %s\n" % blob, encoding="utf-8")
    ex = extract(path)
    assert CUSTOMER in ex.scan_text
    assert blob in ex.text


def test_ordinary_long_words_are_not_decoded(tmp_path):
    path = tmp_path / "w.txt"
    words = " ".join(("Leistungsbeschreibung", "Internationalisierung", "ConfigurationManagerFactory"))
    path.write_text(words + "\n", encoding="utf-8")
    ex = extract(path)
    assert ex.text == words + "\n"
    assert not any("block" in n for n in ex.notes)


def test_html_comments_attributes_and_scripts_reach_detection(tmp_path):
    path = tmp_path / "p.html"
    path.write_text("<html><body><!-- %s --><p data-kunde=\"%s\">x</p><script>var k = '%s';</script></body></html>"
                    % (PERSON, CUSTOMER_SHORT, ORG), encoding="utf-8")
    ex = extract(path)
    assert PERSON in ex.detect_text and PERSON not in ex.text
    assert CUSTOMER_SHORT in ex.scan_text and ORG in ex.scan_text
    assert "1 html comment(s) dropped from the output" in ex.notes


# --------------------------------------------------------------------------- red-team findings (2026-09-27)


def _hand_zip(entries: list[dict]) -> bytes:
    """A stored zip written by hand, so that the shape is exactly what the test says. Entry keys: name (bytes),
    data, extra."""
    out = b""
    central = b""
    for e in entries:
        name, data, extra = e["name"], e.get("data", b""), e.get("extra", b"")
        crc = zlib.crc32(data) & 0xFFFFFFFF
        offset = len(out)
        out += struct.pack("<IHHHHHIIIHH", 0x04034B50, 20, 0, 0, 0, 0x21, crc, len(data), len(data),
                           len(name), len(extra)) + name + extra + data
        central += struct.pack("<IHHHHHHIIIHHHHHII", 0x02014B50, 20, 20, 0, 0, 0, 0x21, crc, len(data),
                               len(data), len(name), len(extra), 0, 0, 0, 0, offset) + name + extra
    eocd = struct.pack("<IHHHHIIH", 0x06054B50, 0, 0, len(entries), len(entries), len(central), len(out), 0)
    return out + central + eocd


def _unicode_path_extra(raw_name: bytes, shown: str) -> bytes:
    """A 0x7075 unicode path extra field (version 1, crc of the raw name) that zipfile lets replace the name."""
    path = shown.encode("utf-8")
    return struct.pack("<HHBI", 0x7075, 5 + len(path), 1, zlib.crc32(raw_name)) + path


def _gz_bytes(data: bytes, fname: str, fcomment: str) -> bytes:
    """A gzip file with an original name and a comment in its header (RFC 1952)."""
    head = b"\x1f\x8b\x08" + bytes([0x08 | 0x10]) + struct.pack("<I", 0) + b"\x00\x03"
    head += fname.encode("latin-1") + b"\x00" + fcomment.encode("latin-1") + b"\x00"
    co = zlib.compressobj(9, zlib.DEFLATED, -15)
    return head + co.compress(data) + co.flush() + struct.pack("<II", zlib.crc32(data) & 0xFFFFFFFF, len(data))


def test_a_zip_member_named_with_a_trailing_slash_that_carries_data_is_a_file(tmp_path, private_tmp):
    data = ("offer for %s\n" % CUSTOMER).encode("utf-8")
    ex = extract(_write(tmp_path / "slash.zip", _hand_zip([{"name": b"notes.txt/", "data": data}])))
    assert ex.state == "ok" and ex.meta["member_count"] == 1
    assert len(ex.children) == 1
    assert ex.children[0].kind == "text" and ex.children[0].text.strip() == "offer for %s" % CUSTOMER
    assert ex.children[0].meta["member"] == "notes.txt/"
    assert ext.INCOMPLETE not in ex.meta
    # a folder entry without data stays a folder
    ex = extract(_write(tmp_path / "folder.zip", _hand_zip([{"name": b"ordner/"}, {"name": b"ordner/a.txt", "data": b"x"}])))
    assert [c.meta["member"] for c in ex.children] == ["ordner/a.txt"]
    assert ex.meta["member_count"] == 2
    assert os.listdir(private_tmp) == []


def test_the_raw_name_behind_a_unicode_path_extra_field_goes_to_detection(tmp_path, private_tmp):
    raw = ("%s plan.txt" % CUSTOMER).encode("cp437")
    entry = {"name": raw, "data": b"content\n", "extra": _unicode_path_extra(raw, "plan-0001.txt")}
    ex = extract(_write(tmp_path / "u.zip", _hand_zip([entry])))
    assert ex.state == "ok"
    assert ex.children[0].meta["member"] == "plan-0001.txt" and "plan-0001.txt" in ex.text
    assert "%s plan.txt" % CUSTOMER in ex.detect_text
    assert CUSTOMER not in ex.text
    # the other way round: the form in the extra field is listed, the innocent raw name still seen
    raw = b"plan-0002.txt"
    entry = {"name": raw, "data": b"content\n", "extra": _unicode_path_extra(raw, "%s plan.txt" % ORG)}
    ex = extract(_write(tmp_path / "v.zip", _hand_zip([entry])))
    assert ex.children[0].meta["member"] == "%s plan.txt" % ORG
    assert "plan-0002.txt" in ex.detect_text and "plan-0002.txt" not in ex.text
    assert os.listdir(private_tmp) == []


def test_the_gzip_header_of_a_tar_gz_goes_to_detection(tmp_path, private_tmp):
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tf:
        info = tarfile.TarInfo("plan.txt")
        info.size = 8
        tf.addfile(info, io.BytesIO(b"content\n"))
    data = _gz_bytes(buf.getvalue(), "%s pack.tar" % CUSTOMER, "notes of %s" % ORG)
    path = _write(tmp_path / "pack.tar.gz", data)
    assert sniff(path) == "tar"
    ex = extract(path)
    assert ex.state == "ok"
    assert [c.text for c in ex.children] == ["content\n"] and ex.children[0].meta["member"] == "plan.txt"
    assert "%s pack.tar" % CUSTOMER in ex.detect_text and "notes of %s" % ORG in ex.detect_text
    assert CUSTOMER not in ex.text and ORG not in ex.text
    assert "compressed_single_file" not in ex.meta
    assert os.listdir(private_tmp) == []
