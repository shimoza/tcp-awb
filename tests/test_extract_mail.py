"""The mail reader: every header decoded, bodies decoded, html stripped, attachments as children.

The fixture mail is written by hand so that the transfer encodings are exactly what the test says:
an RFC 2047 base64 subject over two encoded words, a base64 body, a quoted-printable body with a soft line
break inside a name, an html alternative and two attachments (a .txt and a .docx made with python-docx).
"""
from __future__ import annotations

import base64
import binascii
import io
import stat
import tempfile
from pathlib import Path

import docx
import pytest

import awb.extract as extract_pkg
from awb import register
from awb.extract import INCOMPLETE, MAX_ARCHIVE_DEPTH
from awb.extract import mail
from awb.matcher import Matcher
from awb.normalize import normalize
from tests import fixtures

CUSTOMER = fixtures.CUSTOMER_FORMS[0]
SHORT = fixtures.CUSTOMER_FORMS[1]
PERSON = fixtures.PERSON_FORMS[0]
ORG = fixtures.ORG_FORMS[0]
LAWFIRM = fixtures.LAWFIRM_FORMS[0]
DOMAIN = fixtures.CUSTOMER_DOMAIN
PLACE = fixtures.PLACE_FORMS[0]

SUBJECT_1 = "Angebot für %s: " % SHORT
SUBJECT_2 = "Rückfragen zum Rahmenvertrag"
SUBJECT = SUBJECT_1 + SUBJECT_2

RECEIVED = (
    "from mail.%s (mail.%s [198.51.100.23])\r\n\tby mx.example.net with ESMTPS id 4F2B9C;"
    " Tue, 22 Sep 2026 09:12:03 +0200" % (DOMAIN, DOMAIN)
)
FROM_ADDR = "tobias.beispielmann@%s" % DOMAIN
MESSAGE_ID = "<20260922091200.4F2B9C@mail.%s>" % DOMAIN
DATE = "Tue, 22 Sep 2026 09:12:00 +0200"
ORIGIN_IP = "[203.0.113.45]"

PLAIN_BODY = (
    "Guten Tag,\n\nanbei das Angebot der %s für den Standort %s.\n"
    "Ansprechpartner ist %s, Telefon +49 40 123456-78.\n" % (CUSTOMER, PLACE, PERSON)
)
HTML_BODY = (
    "<html><head><style>p { color: red; }</style></head><body>"
    "<p>Guten Tag,</p><p>anbei das Angebot der <b>Zyxwo</b> Logistik GmbH.</p>"
    '<p><a href="https://portal.%s/angebot">Portal</a></p>'
    "<p>Gru&szlig;, <i>Beispielmann</i></p></body></html>" % DOMAIN
)
HTML_ONLY_TEXT = "Gruß, Beispielmann"
QP_BODY = "Lieferant ist die Qv=C3=B6r=\r\ntz Pr=C3=A4zision AG, Konto DE89 3704 0044 0532 0130 00.\r\n"
QP_DECODED = "Lieferant ist die %s, Konto DE89 3704 0044 0532 0130 00." % ORG

TXT_ATTACHMENT = "Protokoll: Termin mit %s in %s.\nAktenzeichen %s\n" % (PERSON, PLACE, fixtures.FILE_NUMBER)
TXT_NAME = "notizen-zyxwo.txt"
DOCX_NAME = "Angebot Qvörtz.docx"
DOCX_TEXT = "Leistungsbeschreibung für die %s" % ORG


def _b64(data: bytes) -> str:
    return base64.encodebytes(data).decode("ascii").replace("\n", "\r\n")


def _encoded_word(s: str) -> str:
    return "=?utf-8?b?%s?=" % base64.b64encode(s.encode("utf-8")).decode("ascii")


def _docx_bytes() -> bytes:
    d = docx.Document()
    d.add_paragraph(DOCX_TEXT)
    buf = io.BytesIO()
    d.save(buf)
    return buf.getvalue()


def _mail_bytes() -> bytes:
    head = (
        "Received: %s\r\n"
        "From: %s <%s>\r\n"
        "To: architektur@example.net, betrieb@example.net\r\n"
        "Cc: =?utf-8?q?Qv=C3=B6rtz_Pr=C3=A4zision_AG?= <einkauf@qvoertz.example>\r\n"
        'Reply-To: "%s" <kanzlei@vbnmq.example>\r\n'
        "Subject: %s\r\n %s\r\n"
        "Date: %s\r\n"
        "Message-ID: %s\r\n"
        "X-Originating-IP: %s\r\n"
        "X-Firma: %s\r\n"
        "MIME-Version: 1.0\r\n"
        'Content-Type: multipart/mixed; boundary="==outer=="\r\n'
        "\r\n"
    ) % (RECEIVED, PERSON, FROM_ADDR, LAWFIRM, _encoded_word(SUBJECT_1), _encoded_word(SUBJECT_2),
         DATE, MESSAGE_ID, ORIGIN_IP, ORG)
    body = (
        "This is a multi-part message in MIME format.\r\n"
        "--==outer==\r\n"
        'Content-Type: multipart/alternative; boundary="==alt=="\r\n\r\n'
        "--==alt==\r\n"
        "Content-Type: text/plain; charset=utf-8\r\n"
        "Content-Transfer-Encoding: base64\r\n\r\n"
        + _b64(PLAIN_BODY.encode("utf-8"))
        + "--==alt==\r\n"
        "Content-Type: text/html; charset=utf-8\r\n"
        "Content-Transfer-Encoding: base64\r\n\r\n"
        + _b64(HTML_BODY.encode("utf-8"))
        + "--==alt==--\r\n\r\n"
        "--==outer==\r\n"
        "Content-Type: text/plain; charset=utf-8\r\n"
        "Content-Transfer-Encoding: quoted-printable\r\n\r\n"
        + QP_BODY
        + "--==outer==\r\n"
        'Content-Type: text/plain; charset=utf-8; name="%s"\r\n'
        'Content-Disposition: attachment; filename="%s"\r\n'
        "Content-Transfer-Encoding: base64\r\n\r\n" % (TXT_NAME, TXT_NAME)
        + _b64(TXT_ATTACHMENT.encode("utf-8"))
        + "--==outer==\r\n"
        "Content-Type: application/vnd.openxmlformats-officedocument.wordprocessingml.document\r\n"
        "Content-Disposition: attachment; filename*=utf-8''Angebot%20Qv%C3%B6rtz.docx\r\n"
        "Content-Transfer-Encoding: base64\r\n\r\n"
        + _b64(_docx_bytes())
        + "--==outer==--\r\n"
    )
    return head.encode("utf-8") + body.encode("ascii")


@pytest.fixture
def eml(tmp_path) -> Path:
    path = tmp_path / "angebot.eml"
    path.write_bytes(_mail_bytes())
    return path


@pytest.fixture
def matcher(register_path) -> Matcher:
    return Matcher(register.forms_for_matching(register.load(register_path)))


@pytest.fixture
def private_tmp(tmp_path, monkeypatch) -> Path:
    """Point tempfile at a folder of the test, so that leftovers can be seen."""
    folder = tmp_path / "tmp"
    folder.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(folder))
    return folder


def _squash(s: str) -> str:
    return " ".join(s.split())


# --------------------------------------------------------------------------- headers


def test_every_header_value_is_decoded_into_detect_text(eml, matcher, private_tmp):
    ex = mail.extract(eml)

    assert ex.kind == "eml"
    assert ex.state == "ok"
    det = ex.detect_text
    expected = [
        # From with display name and address
        PERSON, FROM_ADDR,
        # To
        "architektur@example.net", "betrieb@example.net",
        # Cc with an RFC 2047 display name
        ORG, "einkauf@qvoertz.example",
        # Reply-To with a quoted display name
        LAWFIRM, "kanzlei@vbnmq.example",
        # Subject over two RFC 2047 base64 words
        SUBJECT,
        DATE, MESSAGE_ID, ORIGIN_IP,
    ]
    for value in expected:
        assert value in det, "a header value is missing from detect_text (index %d)" % expected.index(value)
    # Received is folded in the file and unfolded in detect_text
    assert _squash(RECEIVED) in _squash(det)
    assert "mail.%s" % DOMAIN in det
    # a raw UTF-8 header is decoded, not left as escapes
    assert "X-Firma: %s" % ORG in det
    assert "=?utf-8?" not in det and "=?utf-8?" not in ex.text
    assert ex.meta["subject"] == SUBJECT
    assert ex.meta["date"] == DATE

    found = {s.code for s in matcher.find(normalize(det).text)}
    assert {
        fixtures.CUSTOMER_CODE,
        fixtures.CUSTOMER_CODE + "-DOM-1",
        fixtures.PERSON_CODE,
        fixtures.ORG_CODE,
        fixtures.LAWFIRM_CODE,
        fixtures.PLACE_CODE,
    } <= found
    fixtures.assert_no_fixture_name("\n".join(ex.notes), "notes")


def test_reader_headers_open_the_output_and_routing_headers_stay_out(eml, private_tmp):
    ex = mail.extract(eml)

    lines = ex.text.splitlines()
    assert lines[0] == "From: %s <%s>" % (PERSON, FROM_ADDR)
    assert "Subject: %s" % SUBJECT in lines
    assert "Reply-To: %s <kanzlei@vbnmq.example>" % LAWFIRM in lines
    assert "Date: %s" % DATE in lines
    for routing in ("Received:", "Message-ID:", "X-Originating-IP:", "MIME-Version:"):
        assert routing not in ex.text
        assert routing in ex.detect_text


# --------------------------------------------------------------------------- bodies


def test_base64_and_quoted_printable_bodies_are_decoded(eml, matcher, private_tmp):
    ex = mail.extract(eml)

    for line in PLAIN_BODY.strip().splitlines():
        if line:
            assert line in ex.text
    # the soft line break inside the name is gone, =C3=B6 is an o umlaut
    assert QP_DECODED in ex.text
    assert QP_DECODED in ex.detect_text
    assert "=C3" not in ex.text and "Qv=" not in ex.detect_text
    hits = [s for s in matcher.find(normalize(ex.text).text) if s.code == fixtures.ORG_CODE]
    assert any(s.form == ORG for s in hits)
    # the base64 blocks themselves are not in the text
    assert _b64(PLAIN_BODY.encode("utf-8")).split()[0] not in ex.detect_text


def test_html_alternative_is_stripped_and_kept_for_detection_only(eml, private_tmp):
    ex = mail.extract(eml)

    assert HTML_ONLY_TEXT in ex.detect_text
    assert HTML_ONLY_TEXT not in ex.text, "only the plain alternative goes into the output"
    assert "anbei das Angebot der %s." % CUSTOMER in ex.detect_text, "tags inside a name are removed"
    assert "https://portal.%s/angebot" % DOMAIN in ex.detect_text, "link targets go to detection"
    for tag in ("<b>", "</p>", "<i>", "<html", "color: red"):
        assert tag not in ex.detect_text


def test_html_only_mail_gives_stripped_text(tmp_path, private_tmp):
    raw = (
        "From: %s <%s>\r\nTo: architektur@example.net\r\nSubject: Termin\r\n"
        "MIME-Version: 1.0\r\nContent-Type: text/html; charset=iso-8859-1\r\n"
        "Content-Transfer-Encoding: quoted-printable\r\n\r\n"
        "<p>Treffen in <b>Beispielstadt</b> bei der Qv=F6rtz Pr=E4zision AG.</p>\r\n" % (PERSON, FROM_ADDR)
    ).encode("ascii")
    path = tmp_path / "termin.eml"
    path.write_bytes(raw)

    ex = mail.extract(path)

    assert ex.state == "ok"
    assert "Treffen in Beispielstadt bei der %s." % ORG in ex.text
    assert "<p>" not in ex.text and "<b>" not in ex.detect_text


# --------------------------------------------------------------------------- attachments


def test_attachments_become_children_and_the_temporary_folder_is_removed(eml, monkeypatch, private_tmp):
    calls = []
    real = extract_pkg.extract

    def spy(path, depth=0):
        p = Path(path)
        calls.append((p.parent, stat.S_IMODE(p.parent.stat().st_mode), p.is_file(), p.name, depth))
        return real(path, depth)

    monkeypatch.setattr(extract_pkg, "extract", spy)
    ex = mail.extract(eml)

    assert [c.kind for c in ex.children] == ["text", "docx"]
    assert [c.state for c in ex.children] == ["ok", "ok"]
    txt, doc = ex.children
    assert PERSON in txt.text and fixtures.FILE_NUMBER in txt.text
    assert DOCX_TEXT in doc.text
    assert txt.meta["attachment"] == TXT_NAME
    assert doc.meta["attachment"] == DOCX_NAME, "an RFC 2231 file name is decoded"
    assert ex.meta["attachment_count"] == 2 and ex.meta["child_count"] == 2

    # attachment names are text: they go to detection and to the output list
    assert "attachment: %s" % DOCX_NAME in ex.detect_text
    assert "attachment: %s" % TXT_NAME in ex.detect_text
    assert "- %s" % DOCX_NAME in ex.text

    # every attachment went through extract from one private folder, at the next depth
    assert len(calls) == 2
    folders = {c[0] for c in calls}
    assert len(folders) == 1
    folder = folders.pop()
    assert folder.parent == private_tmp
    assert all(c[1] == 0o700 for c in calls), "the temporary folder is private"
    assert all(c[2] for c in calls), "the attachment existed while it was read"
    assert all(c[4] == 1 for c in calls)
    assert all("zyxwo" not in c[3].lower() and "qv" not in c[3].lower() for c in calls), \
        "temporary file names carry no name"
    # and the folder is gone again
    assert not folder.exists()
    assert list(private_tmp.iterdir()) == []
    assert not any(c.path.exists() for c in ex.children)


def test_attachments_at_the_depth_limit_are_listed_not_extracted(eml, private_tmp):
    ex = mail.extract(eml, depth=MAX_ARCHIVE_DEPTH)

    assert ex.state == "ok"
    assert ex.children == []
    assert any("not extracted" in n for n in ex.notes)
    assert "attachment: %s" % DOCX_NAME in ex.detect_text
    assert list(private_tmp.iterdir()) == []


def test_forwarded_mail_becomes_a_child_mail(tmp_path, private_tmp):
    inner = (
        "From: %s <kanzlei@vbnmq.example>\r\nTo: %s\r\nSubject: Vertragsentwurf\r\n"
        "Message-ID: <inner-1@vbnmq.example>\r\n\r\nEntwurf fuer die %s anbei.\r\n" % (LAWFIRM, FROM_ADDR, CUSTOMER)
    )
    outer = (
        "From: %s <%s>\r\nTo: architektur@example.net\r\nSubject: WG: Vertragsentwurf\r\n"
        "MIME-Version: 1.0\r\nContent-Type: multipart/mixed; boundary=\"b1\"\r\n\r\n"
        "--b1\r\nContent-Type: text/plain; charset=utf-8\r\n\r\nzur Info\r\n"
        "--b1\r\nContent-Type: message/rfc822\r\n\r\n%s\r\n--b1--\r\n" % (PERSON, FROM_ADDR, inner)
    )
    path = tmp_path / "weiter.eml"
    path.write_bytes(outer.encode("utf-8"))

    ex = mail.extract(path)

    assert ex.state == "ok"
    assert "zur Info" in ex.text
    assert CUSTOMER not in ex.text, "the forwarded mail is a child, not part of the body"
    assert len(ex.children) == 1
    child = ex.children[0]
    assert child.kind == "eml" and child.state == "ok"
    assert "From: %s <kanzlei@vbnmq.example>" % LAWFIRM in child.text
    assert CUSTOMER in child.text
    assert "Message-ID: <inner-1@vbnmq.example>" in child.detect_text
    assert list(private_tmp.iterdir()) == []


# --------------------------------------------------------------------------- msg and dispatch


def test_outlook_msg_is_unsupported(tmp_path):
    path = tmp_path / ("%s-Angebot.msg" % SHORT)
    path.write_bytes(bytes.fromhex("D0CF11E0A1B11AE1") + b"\x00" * 504)

    ex = mail.extract(path)

    assert ex.kind == "msg"
    assert ex.state == "unsupported"
    assert ex.notes == [mail.MSG_NOTE]
    assert ex.text == "" and ex.detect_text == "" and ex.children == []
    fixtures.assert_no_fixture_name("\n".join(ex.notes), "notes")


def test_dispatcher_uses_this_reader_with_depth(eml, private_tmp):
    ex = extract_pkg.extract(eml)

    assert ex.kind == "eml"
    assert ex.state == "ok"
    assert SUBJECT in ex.detect_text
    assert [c.kind for c in ex.children] == ["text", "docx"]
    # at the depth limit the dispatcher passes depth through and nothing is extracted
    assert extract_pkg.extract(eml, depth=MAX_ARCHIVE_DEPTH).children == []


# --------------------------------------------------------------------------- review findings (2026-09-22)


def _flowed_mail(body: str, delsp: str) -> bytes:
    return (
        "From: a@example.org\r\nTo: b@example.org\r\nSubject: termin\r\nMIME-Version: 1.0\r\n"
        "Content-Type: text/plain; charset=utf-8; format=flowed; delsp=%s\r\n\r\n%s" % (delsp, body)
    ).encode("utf-8")


def test_flowed_body_with_delsp_is_joined_like_a_mail_client_shows_it(tmp_path, private_tmp):
    brand, word, form = fixtures.PLANTED_CANDIDATE.split()
    body = "kunde %s \r\n%s fertig. und %s %s \r\n%s %s.\r\n" % (SHORT[:3], SHORT[3:], brand,
                                                               word[:4], word[4:], form)
    path = tmp_path / "flowed.eml"
    path.write_bytes(_flowed_mail(body, "yes"))
    ex = mail.extract(path)
    assert "kunde %s fertig." % SHORT in ex.text
    assert fixtures.PLANTED_CANDIDATE in ex.text


def test_flowed_body_without_delsp_keeps_the_space():
    assert mail.unflow("eins \r\nzwei\r\n-- \r\ngruss", False) == "eins zwei\n-- \ngruss"
    assert mail.unflow("eins \r\nzwei", True) == "einszwei"
    assert mail.unflow(" >nicht zitiert\r\n", True) == ">nicht zitiert\n"


def test_attachment_folders_go_under_the_temp_root(eml, tmp_path, private_tmp, monkeypatch):
    root = tmp_path / "vault-tmp"
    made = []
    real = extract_pkg.make_temp_dir

    def spy(prefix):
        folder = real(prefix)
        made.append(folder)
        return folder

    monkeypatch.setattr(mail, "make_temp_dir", spy)
    with extract_pkg.temp_root(root):
        mail.extract(eml)
    assert made and all(f.parent == root for f in made)
    assert list(root.iterdir()) == [] and list(private_tmp.iterdir()) == []


# --------------------------------------------------------------------------- red-team findings (2026-09-27)

ORG_SHORT = fixtures.ORG_FORMS[1]
IBAN = "DE89 3704 0044 0532 0130 00"
CRLF = "\r\n"
HEAD = "From: office@example.org" + CRLF + "To: sender@example.org" + CRLF


def _write(tmp_path: Path, name: str, data: bytes) -> Path:
    path = tmp_path / name
    path.write_bytes(data)
    return path


def _single_part(subject: str, ctype: str, body: bytes, cte: str | None = None, head: str = HEAD) -> bytes:
    """A single-part mail whose body bytes go in as they are."""
    lines = head + "Subject: %s" % subject + CRLF + "Content-Type: %s" % ctype + CRLF
    if cte:
        lines += "Content-Transfer-Encoding: %s" % cte + CRLF
    return lines.encode("utf-8") + CRLF.encode("ascii") + body


def _q_word(text: str, charset: str, label: str) -> str:
    """An RFC 2047 Q encoded word of `text` in `charset`, labelled `label`."""
    raw = text.encode(charset)
    payload = "".join(chr(b) if 33 <= b <= 126 and chr(b) not in "=?_" else "=%02X" % b for b in raw)
    return "=?%s?q?%s?=" % (label, payload.replace("=20", "_"))


def _b_word(text: str, charset: str, label: str) -> str:
    return "=?%s?b?%s?=" % (label, base64.b64encode(text.encode(charset)).decode("ascii"))


def _multipart(parts: list[str], boundary: str = "b1", preamble: str = "", epilogue: str = "") -> str:
    """The body block of a multipart/mixed, starting with its own Content-Type line."""
    out = 'Content-Type: multipart/mixed; boundary="%s"' % boundary + CRLF + CRLF
    if preamble:
        out += preamble + CRLF
    for p in parts:
        out += "--%s" % boundary + CRLF + p + CRLF
    out += "--%s--" % boundary + CRLF
    if epilogue:
        out += epilogue + CRLF
    return out


def _text_part(text: str, ctype: str = "text/plain; charset=utf-8") -> str:
    return "Content-Type: %s" % ctype + CRLF + CRLF + text + CRLF


def test_raw_cp1252_header_bytes_keep_their_umlaut(tmp_path, private_tmp):
    head = "From: %s team <office@example.org>" % ORG_SHORT + CRLF + "To: sender@example.org" + CRLF
    raw = _single_part("offer for %s" % ORG, "text/plain; charset=utf-8", b"body." + CRLF.encode("ascii"), head=head)
    ex = mail.extract(_write(tmp_path, "raw.eml", raw.decode("utf-8").encode("cp1252")))
    assert ex.state == "ok"
    assert "From: %s team <office@example.org>" % ORG_SHORT in ex.text
    assert "Subject: offer for %s" % ORG in ex.text
    assert ex.meta["subject"] == "offer for %s" % ORG
    assert "�" not in ex.text and "�" not in ex.detect_text


def test_a_single_byte_label_on_utf8_bytes_reads_as_utf8(tmp_path, private_tmp):
    mojibake = ORG.encode("utf-8").decode("latin-1")
    subject = _b_word("offer for %s" % ORG, "utf-8", "iso-8859-1")
    body = ("supplier is %s, see subject." % ORG).encode("utf-8") + CRLF.encode("ascii")
    raw = _single_part(subject, "text/plain; charset=iso-8859-1", body, "8bit")
    ex = mail.extract(_write(tmp_path, "label.eml", raw))
    assert "Subject: offer for %s" % ORG in ex.text
    assert "supplier is %s, see subject." % ORG in ex.text
    assert mojibake not in ex.text and mojibake not in ex.detect_text
    # a body that really is latin-1 under that label still reads right
    raw = _single_part("latin", "text/plain; charset=iso-8859-1", ORG.encode("latin-1") + CRLF.encode("ascii"), "8bit")
    assert ORG in mail.extract(_write(tmp_path, "latin.eml", raw)).text


def test_a_utf8_label_on_latin1_bytes_reads_as_cp1252(tmp_path, private_tmp):
    subject = _q_word("offer for %s" % ORG, "latin-1", "utf-8")
    body = ("supplier is %s." % ORG).encode("latin-1") + CRLF.encode("ascii")
    ex = mail.extract(_write(tmp_path, "broken.eml", _single_part(subject, "text/plain; charset=utf-8", body, "8bit")))
    assert "Subject: offer for %s" % ORG in ex.text
    assert "supplier is %s." % ORG in ex.text
    assert "�" not in ex.text


def test_an_unlabelled_utf7_body_is_decoded(tmp_path, private_tmp):
    body = ("offer for %s, see attachment" % ORG).encode("utf-7") + CRLF.encode("ascii")
    assert b"+A" in body
    ex = mail.extract(_write(tmp_path, "utf7.eml", _single_part("utf7", "text/plain", body, "7bit")))
    assert "offer for %s, see attachment" % ORG in ex.text
    assert "+A" not in ex.text
    # a plain body with plus signs is left alone
    plain = b"call +49 30 1234567 or the C++ team, budget +ABC- five\r\n"
    ex = mail.extract(_write(tmp_path, "plus.eml", _single_part("plus", "text/plain", plain, "7bit")))
    assert "call +49 30 1234567 or the C++ team, budget +ABC- five" in ex.text


def test_a_quoted_printable_body_labelled_7bit_is_decoded(tmp_path, private_tmp):
    body = ("offer for %s=" % CUSTOMER[:12] + CRLF + "%s, pay to %s=" % (CUSTOMER[12:], IBAN[:19]) + CRLF
            + IBAN[19:] + " now" + CRLF).encode("ascii")
    ex = mail.extract(_write(tmp_path, "qp.eml", _single_part("qp", "text/plain; charset=utf-8", body, "7bit")))
    assert "offer for %s, pay to %s now" % (CUSTOMER, IBAN) in ex.text
    assert "=" not in ex.text.split("now")[0].split(CRLF)[-1]
    # a 7bit body with a single =XX mark and no soft break stays as written
    plain = b"a=1 and b=2, color=FF0000\r\n"
    ex = mail.extract(_write(tmp_path, "plain.eml", _single_part("plain", "text/plain", plain, "7bit")))
    assert "a=1 and b=2, color=FF0000" in ex.text
    # the padding of a pasted base64 block at a line end is no soft break
    blob = base64.b64encode(b"x" * 20).decode("ascii")
    assert blob.endswith("=") and not blob.endswith("==")
    pasted = ("pasted: %s\r\nend of block\r\n" % blob).encode("ascii")
    ex = mail.extract(_write(tmp_path, "pasted.eml", _single_part("pasted", "text/plain", pasted, "7bit")))
    assert "pasted: %s\nend of block" % blob in ex.text


def test_preamble_and_epilogue_of_a_multipart_reach_the_output(tmp_path, private_tmp):
    block = _multipart([_text_part("the visible body.")], preamble="offer for %s in the preamble" % CUSTOMER,
                       epilogue="contract with %s in the epilogue" % ORG)
    ex = mail.extract(_write(tmp_path, "pre.eml", (HEAD + "Subject: structure" + CRLF + block).encode("utf-8")))
    assert ex.state == "ok"
    assert "offer for %s in the preamble" % CUSTOMER in ex.text
    assert "contract with %s in the epilogue" % ORG in ex.text
    assert ex.text.index("preamble") < ex.text.index("the visible body.") < ex.text.index("epilogue")
    assert "offer for %s in the preamble" % CUSTOMER in ex.detect_text


def test_the_preamble_inside_a_forwarded_mail_reaches_its_child(tmp_path, private_tmp):
    inner = ("From: office@example.org" + CRLF + "Subject: inner" + CRLF
             + _multipart([_text_part("inner body.")], boundary="b9", preamble="offer for %s in the inner preamble" % CUSTOMER))
    block = _multipart([_text_part("forwarded below."), "Content-Type: message/rfc822" + CRLF + CRLF + inner])
    ex = mail.extract(_write(tmp_path, "fwd.eml", (HEAD + "Subject: fwd" + CRLF + block).encode("utf-8")))
    assert len(ex.children) == 1
    assert "offer for %s in the inner preamble" % CUSTOMER in ex.children[0].text
    assert CUSTOMER not in ex.text


def _mbox(first_block: str, second_block: str) -> bytes:
    first = "From office@example.org 2026-09-22 10:00:00" + CRLF + HEAD + "Subject: first" + CRLF + first_block
    second = ("From t.b@%s 2026-09-22 11:00:00" % DOMAIN + CRLF + "From: %s <t.b@%s>" % (PERSON, DOMAIN) + CRLF
              + "To: sender@example.org" + CRLF + "Subject: offer for %s" % CUSTOMER + CRLF + second_block)
    return (first + CRLF + second).encode("utf-8")


def test_a_mailbox_is_split_into_one_child_per_message(tmp_path, private_tmp):
    data = _mbox(_text_part("the first message."), _text_part("pay to %s" % IBAN))
    path = _write(tmp_path, "box.mbox", data)
    assert extract_pkg.sniff(path) == "mbox"
    ex = mail.extract(path)
    assert ex.kind == "mbox" and ex.state == "ok"
    assert "mailbox with 2 message(s), each read as a child" in ex.notes
    assert ex.meta["message_count"] == 2 and ex.meta["child_count"] == 2
    assert [c.kind for c in ex.children] == ["eml", "eml"]
    assert [c.meta["member"] for c in ex.children] == ["message 1", "message 2"]
    assert "the first message." in ex.children[0].text
    assert "Subject: offer for %s" % CUSTOMER in ex.children[1].text
    assert "pay to %s" % IBAN in ex.children[1].text
    assert CUSTOMER not in ex.text and IBAN not in ex.text and "| 1 |" in ex.text
    assert "envelope sender: t.b@%s" % DOMAIN in ex.detect_text
    assert "2026-09-22" not in ex.detect_text, "the envelope date reads like a name and stays out"
    assert list(private_tmp.iterdir()) == []
    # the router reads it the same way, by content as well when the suffix says nothing
    assert extract_pkg.extract(path).kind == "mbox"
    bare = _write(tmp_path, "box", data)
    assert extract_pkg.sniff(bare) == "mbox" and extract_pkg.extract(bare).meta["child_count"] == 2
    # at the depth limit the messages are listed, not read
    deep = mail.extract(path, depth=MAX_ARCHIVE_DEPTH)
    assert deep.children == [] and deep.meta[INCOMPLETE] is True
    assert any("not extracted" in n for n in deep.notes)


def test_a_mailbox_whose_first_message_is_multipart_still_splits(tmp_path, private_tmp):
    attachment = ("Content-Type: application/vnd.openxmlformats-officedocument.wordprocessingml.document" + CRLF
                  + 'Content-Disposition: attachment; filename="offer.docx"' + CRLF
                  + "Content-Transfer-Encoding: base64" + CRLF + CRLF + _b64(_docx_bytes()))
    data = _mbox(_multipart([_text_part("the first message.")]),
                 _multipart([_text_part("docx attached"), attachment], boundary="b2"))
    ex = mail.extract(_write(tmp_path, "box2.mbox", data))
    assert ex.meta["message_count"] == 2 and len(ex.children) == 2
    assert "the first message." in ex.children[0].text
    grandchildren = ex.children[1].children
    assert [g.kind for g in grandchildren] == ["docx"]
    assert DOCX_TEXT in grandchildren[0].text
    assert list(private_tmp.iterdir()) == []


def test_a_single_mail_with_an_envelope_line_is_not_a_mailbox(tmp_path, private_tmp):
    data = ("From office@example.org 2026-09-22 10:00:00" + CRLF + HEAD + "Subject: only one" + CRLF
            + _text_part("the body says: From here on, nothing more.")).encode("utf-8")
    path = _write(tmp_path, "one.eml", data)
    assert extract_pkg.sniff(path) == "eml"
    ex = mail.extract(path)
    assert ex.kind == "eml" and ex.children == []
    assert "Subject: only one" in ex.text and "From here on" in ex.text


def _uuencode(name: str, data: bytes) -> str:
    lines = ["begin 644 %s" % name]
    for i in range(0, len(data), 45):
        lines.append(binascii.b2a_uu(data[i:i + 45]).decode("ascii").rstrip("\n"))
    lines += ["`", "end"]
    return CRLF.join(lines) + CRLF


def test_an_inline_uuencoded_block_becomes_a_child(tmp_path, private_tmp):
    inner = ("offer for %s, pay to %s\n" % (CUSTOMER, IBAN)).encode("utf-8")
    body = ("the file follows" + CRLF + _uuencode("plan.txt", inner) + "regards" + CRLF).encode("ascii")
    ex = mail.extract(_write(tmp_path, "uu.eml", _single_part("uu", "text/plain; charset=us-ascii", body)))
    assert ex.state == "ok"
    assert len(ex.children) == 1
    child = ex.children[0]
    assert child.kind == "text" and "offer for %s, pay to %s" % (CUSTOMER, IBAN) in child.text
    assert child.meta["attachment"] == "plan.txt"
    assert "the file follows\nregards" in ex.text, "the block leaves the text without a trace"
    assert "begin 644" not in ex.text and "`" not in ex.text
    assert binascii.b2a_uu(inner[:45]).decode("ascii").strip() not in ex.text
    assert CUSTOMER not in ex.detect_text
    assert "- plan.txt" in ex.text and "attachment: plan.txt" in ex.detect_text
    assert "1 uuencoded block(s) in the body decoded as attachment(s)" in ex.notes
    assert INCOMPLETE not in ex.meta
    assert list(private_tmp.iterdir()) == []


def test_binhex_and_yenc_blocks_are_noted_as_not_decoded(tmp_path, private_tmp):
    body = ("see below" + CRLF + "(This file must be converted with BinHex 4.0)" + CRLF + CRLF + ":#'peRT:" + CRLF
            + "=ybegin line=128 size=20 name=plan.txt" + CRLF + "abc" + CRLF + "=yend size=20" + CRLF
            + "begin 644 broken.txt" + CRLF + "not uuencoded at all" + CRLF + "end" + CRLF).encode("ascii")
    ex = mail.extract(_write(tmp_path, "hqx.eml", _single_part("hqx", "text/plain; charset=us-ascii", body)))
    assert ex.state == "ok" and ex.children == []
    assert "1 binhex block(s) in the body not decoded, review the original" in ex.notes
    assert "1 yenc block(s) in the body not decoded, review the original" in ex.notes
    assert "1 uuencoded block(s) in the body not decoded, review the original" in ex.notes
    assert ex.meta[INCOMPLETE] is True
    assert "see below" in ex.text and "not uuencoded at all" in ex.text
