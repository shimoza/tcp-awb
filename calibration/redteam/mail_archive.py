"""Red-team cases, dimension mail-archive: .eml (awb/extract/mail.py) and archives (awb/extract/archive.py).

Every value is an invented fixture form or an invented structured value. Nothing here is real.
"""
from __future__ import annotations

import base64
import binascii
import gzip
import io
import bz2
import lzma
import os
import struct
import sys
import tarfile
import zipfile
import zlib
from pathlib import Path
from urllib.parse import quote

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))   # the repository that holds this pack
from tests import fixtures as fx  # noqa: E402

FULL, SHORT, ACRONYM, ENGLISH = fx.CUSTOMER_FORMS
DOMAIN = fx.CUSTOMER_DOMAIN
PERSON = fx.PERSON_FORMS[0]
ORG = fx.ORG_FORMS[0]
PLACE = fx.PLACE_FORMS[0]
CAND = fx.PLANTED_CANDIDATE          # unregistered: a detection probe (blocks the run when detection reads it)
IBAN = "DE89 3704 0044 0532 0130 00"  # from awb/planted.py, passes mod 97
PHONE = "030 1234567"
PHONE_INT = "+49 30 1234567"
IP = "203.0.113.7"
MOJI_ORG = ORG.encode("utf-8").decode("latin-1")     # the mojibake a wrong charset label produces
UTF7_ORG = ORG.encode("utf-7").decode("ascii")       # the utf-7 spelling without a charset label
FULL_HEAD, FULL_TAIL = FULL.split(" ", 1)             # the first word and the rest of the full form
FIRST, LAST = PERSON.split(" ", 1)                     # the person's first name and surname
FULL_CUT = len(FULL_HEAD) + 6                          # a cut inside the second word of the full form
Q_WORD = SHORT[:3] + "=%02X" % ord(SHORT[3]) + SHORT[4:] + "_" + FULL_TAIL.replace(" ", "_")   # Q encoding
PERSON_Q = quote(PERSON)                               # the person percent encoded, for RFC 2231 names

CRLF = "\r\n"
BASE = ("From: office@example.org" + CRLF + "To: sender@example.org" + CRLF
        + "Date: Tue, 22 Sep 2026 10:00:00 +0200" + CRLF)


def mk(word: str) -> str:
    """An innocent marker: lower case, unique, never a name."""
    return "invented marker %s here" % word


# --------------------------------------------------------------------------- builders


def file_builder(name: str, data: bytes):
    def build(inbox):
        p = inbox / name
        p.write_bytes(data)
        return p
    return build


def b64lines(data: bytes) -> str:
    return base64.encodebytes(data).decode("ascii").replace("\n", CRLF)


def part(ctype: str, body: str | bytes, extra_headers: str = "", cte: str | None = None) -> str:
    h = "Content-Type: %s" % ctype + CRLF
    if cte:
        h += "Content-Transfer-Encoding: %s" % cte + CRLF
    h += extra_headers
    if isinstance(body, bytes):
        body = body.decode("latin-1")
    else:
        body = body.encode("utf-8").decode("latin-1")
    return h + CRLF + body


def multipart(subtype: str, parts: list[str], boundary: str = "B1", preamble: str = "", epilogue: str = "",
              params: str = "") -> str:
    out = ""
    if preamble:
        out += preamble + CRLF
    for p in parts:
        out += "--%s" % boundary + CRLF + p + CRLF
    out += "--%s--" % boundary + CRLF
    if epilogue:
        out += epilogue + CRLF
    return "Content-Type: multipart/%s; boundary=\"%s\"%s" % (subtype, boundary, params) + CRLF + CRLF + out


def mail(subject: str, body_and_ctype: str, extra: str = "", head: str = BASE) -> bytes:
    """A whole mail: base headers, extra raw header lines, Subject, then the body block (which starts with its
    own Content-Type line)."""
    text = head + extra + "Subject: %s" % subject + CRLF + body_and_ctype
    return text.encode("latin-1", "replace")


def eml(name: str, subject: str, body_and_ctype: str, extra: str = "", head: str = BASE):
    return file_builder(name, mail(subject, body_and_ctype, extra, head))


def ew(text: str, charset: str, enc: str = "b", label: str | None = None) -> str:
    raw = text.encode(charset)
    if enc == "b":
        payload = base64.b64encode(raw).decode("ascii")
    else:
        payload = "".join("=%02X" % b if (b > 126 or b < 33 or chr(b) in "=?_") else chr(b) for b in raw)
        payload = payload.replace("=20", "_")
    return "=?%s?%s?%s?=" % (label or charset, enc, payload)


def docx_bytes(text: str, compression=zipfile.ZIP_DEFLATED) -> bytes:
    """A minimal docx by hand (zipfile), so that compression can be chosen."""
    ct = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
          '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
          '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
          '<Default Extension="xml" ContentType="application/xml"/>'
          '<Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.'
          'wordprocessingml.document.main+xml"/></Types>')
    rels = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/'
            'officeDocument" Target="word/document.xml"/></Relationships>')
    doc = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
           '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:body>'
           + "".join("<w:p><w:r><w:t xml:space=\"preserve\">%s</w:t></w:r></w:p>" % line for line in text.split("\n"))
           + '</w:body></w:document>')
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", compression) as zf:
        zf.writestr("[Content_Types].xml", ct)
        zf.writestr("_rels/.rels", rels)
        zf.writestr("word/document.xml", doc)
    return buf.getvalue()


def zip_bytes(members: list[tuple[str, bytes]], compression=zipfile.ZIP_DEFLATED, comment: bytes = b"") -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", compression) as zf:
        for name, data in members:
            zf.writestr(name, data)
        if comment:
            zf.comment = comment
    return buf.getvalue()


def hand_zip(entries: list[dict]) -> bytes:
    """A zip written by hand (stored). Entry keys: name (bytes), data, flags, extra, comment, local_name."""
    out = b""
    central = b""
    for e in entries:
        name = e["name"]
        data = e.get("data", b"")
        flags = e.get("flags", 0)
        extra = e.get("extra", b"")
        comment = e.get("comment", b"")
        local_name = e.get("local_name", name)
        crc = zlib.crc32(data) & 0xFFFFFFFF
        offset = len(out)
        local = struct.pack("<IHHHHHIIIHH", 0x04034B50, 20, flags, 0, 0, 0x21, crc, len(data), len(data),
                            len(local_name), len(extra)) + local_name + extra
        out += local + data
        central += struct.pack("<IHHHHHHIIIHHHHHII", 0x02014B50, 20, 20, flags, 0, 0, 0x21, crc, len(data),
                               len(data), len(name), len(extra), len(comment), 0, 0, 0, offset)
        central += name + extra + comment
    zcomment = b""
    eocd = struct.pack("<IHHHHIIH", 0x06054B50, 0, 0, len(entries), len(entries), len(central), len(out),
                       len(zcomment)) + zcomment
    return out + central + eocd


def tar_bytes(members: list, fmt=tarfile.PAX_FORMAT, mode: str = "w") -> bytes:
    """members: TarInfo objects, (TarInfo, data) tuples or (name, data) tuples."""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode=mode, format=fmt) as tf:
        for m in members:
            if isinstance(m, tuple) and isinstance(m[0], tarfile.TarInfo):
                m[0].size = len(m[1])
                tf.addfile(m[0], io.BytesIO(m[1]))
            elif isinstance(m, tuple):
                ti = tarfile.TarInfo(m[0])
                ti.size = len(m[1])
                tf.addfile(ti, io.BytesIO(m[1]))
            else:
                tf.addfile(m)
    return buf.getvalue()


def gz_bytes(data: bytes, fname: str | None = None, fcomment: str | None = None) -> bytes:
    flags = (0x08 if fname else 0) | (0x10 if fcomment else 0)
    head = b"\x1f\x8b\x08" + bytes([flags]) + struct.pack("<I", 0) + b"\x00\x03"
    if fname:
        head += fname.encode("latin-1") + b"\x00"
    if fcomment:
        head += fcomment.encode("latin-1") + b"\x00"
    co = zlib.compressobj(9, zlib.DEFLATED, -15)
    body = co.compress(data) + co.flush()
    return head + body + struct.pack("<II", zlib.crc32(data) & 0xFFFFFFFF, len(data) & 0xFFFFFFFF)


def uuencode(name: str, data: bytes) -> str:
    lines = ["begin 644 %s" % name]
    for i in range(0, len(data), 45):
        lines.append(binascii.b2a_uu(data[i:i + 45]).decode("ascii").rstrip("\n"))
    lines += ["`", "end"]
    return CRLF.join(lines) + CRLF


def yenc(name: str, data: bytes) -> bytes:
    out = bytearray()
    for b in data:
        c = (b + 42) % 256
        if c in (0x00, 0x0A, 0x0D, 0x3D):
            out += b"=" + bytes([(c + 64) % 256])
        else:
            out.append(c)
    body = bytes(out)
    lines = [body[i:i + 128] for i in range(0, len(body), 128)]
    head = ("=ybegin line=128 size=%d name=%s" % (len(data), name)).encode("latin-1")
    tail = ("=yend size=%d crc32=%08x" % (len(data), zlib.crc32(data) & 0xFFFFFFFF)).encode("latin-1")
    return head + b"\r\n" + b"\r\n".join(lines) + b"\r\n" + tail + b"\r\n"


_HQX = b"!\"#$%&'()*+,-012345689@ABCDEFGHIJKLMNPQRSTUVXYZ[`abcdefhijklmpqr"


def binhex(name: str, data: bytes) -> str:
    """BinHex 4.0 without run-length compression: header, data fork, no resource fork."""
    nm = name.encode("ascii")
    payload = bytes([len(nm)]) + nm + b"\x00" + b"TEXTttxt" + b"\x00\x00" + struct.pack(">II", len(data), 0)
    payload += struct.pack(">H", binascii.crc_hqx(payload, 0))
    payload += data + struct.pack(">H", binascii.crc_hqx(data, 0)) + b"\x00\x00"
    bits = "".join("{:08b}".format(b) for b in payload)
    bits += "0" * (-len(bits) % 6)
    enc = "".join(chr(_HQX[int(bits[i:i + 6], 2)]) for i in range(0, len(bits), 6))
    lines = [enc[i:i + 64] for i in range(0, len(enc), 64)]
    return "(This file must be converted with BinHex 4.0)" + CRLF + CRLF + ":" + CRLF.join(lines) + ":" + CRLF


def tnef_bytes(title: bytes, data: bytes) -> bytes:
    def attr(level: int, aid: int, payload: bytes) -> bytes:
        return (bytes([level]) + struct.pack("<I", aid) + struct.pack("<I", len(payload)) + payload
                + struct.pack("<H", sum(payload) & 0xFFFF))
    out = struct.pack("<I", 0x223E9F78) + struct.pack("<H", 0x1234)
    out += attr(1, 0x00089006, struct.pack("<I", 0x10000))
    out += attr(2, 0x00069002, b"\x01\x00" + b"\x00" * 12)
    out += attr(2, 0x00018010, title + b"\x00")
    out += attr(2, 0x0006800F, data)
    return out


class Zeros:
    def __init__(self, n: int):
        self.left = n

    def read(self, k: int = -1) -> bytes:
        if k < 0 or k > self.left:
            k = self.left
        self.left -= k
        return b"\x00" * k


def build_big_tar(inbox):
    p = inbox / "big.tar.gz"
    big = 201 * 1024 * 1024 + 1
    with tarfile.open(str(p), "w:gz", format=tarfile.PAX_FORMAT) as tf:
        ti = tarfile.TarInfo("zeros.bin")
        ti.size = big
        tf.addfile(ti, Zeros(big))
        data = ("after the big one: %s, %s\n" % (FULL, mk("tar-past-bytes"))).encode()
        ti2 = tarfile.TarInfo("late.txt")
        ti2.size = len(data)
        tf.addfile(ti2, io.BytesIO(data))
    return p


def rand(n: int, seed: int = 7) -> bytes:
    """Deterministic pseudo random bytes (no secrets, no real key)."""
    out = bytearray()
    x = seed
    while len(out) < n:
        x = (x * 1103515245 + 12345) & 0x7FFFFFFF
        out.append((x >> 16) & 0xFF)
    return bytes(out)


# --------------------------------------------------------------------------- mail cases

M = []

# headers outside READER_HEADERS carrying the domain and forms; the candidate probes whether detection reads them
_routing = (
    "Message-ID: <20260922.1001@mail.%s>" % DOMAIN + CRLF
    + "In-Reply-To: <20260921.0900@%s>" % DOMAIN + CRLF
    + "References: <20260920.0800@%s>" % DOMAIN + CRLF
    + "Received: from mx.%s (mx.%s [%s])" % (DOMAIN, DOMAIN, IP) + CRLF
    + "\tby mail.example.org with ESMTP id 1234; Tue, 22 Sep 2026 10:00:00 +0200" + CRLF
    + "X-Originating-IP: [%s]" % IP + CRLF
    + "X-Sender: info@%s" % DOMAIN + CRLF
    + "Organization: %s" % FULL + CRLF
    + "Thread-Topic: offer for %s and %s" % (FULL, CAND) + CRLF
    + "List-Id: staff <staff.%s>" % DOMAIN + CRLF
    + "Return-Path: <bounce@%s>" % DOMAIN + CRLF
    + "Sender: info@%s" % DOMAIN + CRLF
    + "Disposition-Notification-To: %s <read@%s>" % (PERSON, DOMAIN) + CRLF
    + "X-Mailer: %s Mailer 2.0" % SHORT + CRLF
    + "Comments: %s, %s" % (FULL, mk("comments-header")) + CRLF
    + "Keywords: %s, %s, %s" % (SHORT, ACRONYM, mk("keywords-header")) + CRLF
)
M.append({"id": "m-hdr-routing", "cls": "name", "carrier": "eml headers outside READER_HEADERS (Message-ID, Received, Organization, Comments ...)",
          "values": [FULL, SHORT, DOMAIN, IP, PERSON, CAND], "visible": [mk("comments-header"), mk("keywords-header")],
          "build": eml("routing.eml", "status update", part("text/plain; charset=utf-8", "plain body, nothing here." + CRLF), _routing),
          "note": "expected: detection reads them (blocked by the candidate), nothing reaches the output (detect-only by design)"})

# header folding with trailing white space or a tab continuation inside a structured value (Subject is an output header)
M.append({"id": "m-fold-iban-space", "cls": "iban", "carrier": "eml Subject folded with a trailing space inside the IBAN",
          "values": [IBAN], "visible": [mk("fold-iban-space")],
          "build": eml("fold1.eml", "payment to %s %s " % (mk("fold-iban-space"), IBAN[:19]) + CRLF + " " + IBAN[19:] + " please",
                       part("text/plain; charset=utf-8", "see subject." + CRLF))})
M.append({"id": "m-fold-iban-tab", "cls": "iban", "carrier": "eml Subject folded with a tab inside the IBAN",
          "values": [IBAN], "visible": [mk("fold-iban-tab")],
          "build": eml("fold2.eml", "payment %s to %s" % (mk("fold-iban-tab"), IBAN[:19]) + CRLF + "\t" + IBAN[19:] + " please",
                       part("text/plain; charset=utf-8", "see subject." + CRLF))})
M.append({"id": "m-fold-phone-space", "cls": "phone", "carrier": "eml Subject folded with a trailing space inside a phone number",
          "values": [PHONE], "visible": [mk("fold-phone-space")],
          "build": eml("fold3.eml", "call back %s on 030 " % mk("fold-phone-space") + CRLF + " 1234567 today",
                       part("text/plain; charset=utf-8", "see subject." + CRLF))})
M.append({"id": "m-fold-phone-tab", "cls": "phone", "carrier": "eml Subject folded with a tab inside a phone number",
          "values": [PHONE], "visible": [mk("fold-phone-tab")],
          "build": eml("fold4.eml", "call back %s on 030" % mk("fold-phone-tab") + CRLF + "\t1234567 today",
                       part("text/plain; charset=utf-8", "see subject." + CRLF))})
M.append({"id": "m-fold-inside-form", "cls": "name", "carrier": "eml Subject and From display name folded inside the form",
          "values": [FULL, PERSON], "visible": [mk("fold-form")],
          "build": eml("fold5.eml", "offer for %s %s" % (mk("fold-form"), FULL_HEAD) + CRLF + "\t%s today" % FULL_TAIL,
                       part("text/plain; charset=utf-8", "see subject." + CRLF),
                       head="From: %s" % FIRST + CRLF + " %s <office@example.org>" % LAST + CRLF + "To: sender@example.org" + CRLF)})
M.append({"id": "m-dup-subject", "cls": "name", "carrier": "eml second Subject header (duplicate)",
          "values": [FULL], "visible": [mk("dup-subject")],
          "build": eml("dup.eml", "first subject", part("text/plain; charset=utf-8", "body." + CRLF),
                       extra="Subject: second subject %s for %s" % (mk("dup-subject"), FULL) + CRLF)})

# encoded words
M.append({"id": "m-ew-utf7", "cls": "name", "carrier": "eml Subject as RFC 2047 encoded word, charset utf-7",
          "values": [ORG], "visible": [mk("ew-utf7")],
          "build": eml("ew1.eml", ew("%s %s" % (mk("ew-utf7"), ORG), "utf-7"), part("text/plain", "body." + CRLF))})
M.append({"id": "m-ew-2022jp-gb2312", "cls": "name", "carrier": "eml Subject (iso-2022-jp) and From display name (gb2312) encoded words",
          "values": [FULL, ENGLISH], "visible": [mk("ew-cjk")],
          "build": eml("ew2.eml", ew("%s %s" % (mk("ew-cjk"), FULL), "iso-2022-jp"), part("text/plain", "body." + CRLF),
                       head="From: %s <office@example.org>" % ew(ENGLISH, "gb2312") + CRLF + "To: sender@example.org" + CRLF)})
M.append({"id": "m-ew-wrong-label", "cls": "name", "carrier": "eml Subject encoded word: utf-8 bytes labelled iso-8859-1 (mojibake)",
          "values": [ORG, MOJI_ORG], "visible": [mk("ew-wrong")],
          "build": eml("ew3.eml", ew("%s %s" % (mk("ew-wrong"), ORG), "utf-8", label="iso-8859-1"), part("text/plain", "body." + CRLF))})
M.append({"id": "m-ew-latin1-as-utf8", "cls": "name", "carrier": "eml Subject encoded word: latin-1 bytes labelled utf-8 (broken decoding)",
          "values": [ORG], "visible": [mk("ew-broken")],
          "build": eml("ew4.eml", ew("%s %s" % (mk("ew-broken"), ORG), "iso-8859-1", label="utf-8"), part("text/plain", "body." + CRLF))})
M.append({"id": "m-ew-bogus-charset-q", "cls": "name", "carrier": "eml Subject encoded word with an unknown charset label, Q encoding with hex letters",
          "values": [FULL], "visible": [mk("ew-bogus")],
          "build": eml("ew5.eml", "=?x-bogus?q?%s_%s?=" % (mk("ew-bogus").replace(" ", "_"), Q_WORD), part("text/plain", "body." + CRLF))})

# attachment names
_att_names = multipart("mixed", [
    part("text/plain; charset=utf-8", mk("att-names") + CRLF),
    part("application/octet-stream; name*=utf-8''%s" % (quote(FULL) + "%20plan.bin"), "AAAA" + CRLF,
         "Content-Disposition: attachment" + CRLF, "base64"),
    part("application/octet-stream", "AAAA" + CRLF,
         "Content-Disposition: attachment;" + CRLF + " filename*0*=utf-8''%s;" % PERSON_Q[:12] + CRLF
         + " filename*1*=%s%%20notes.bin" % PERSON_Q[12:] + CRLF, "base64"),
    part("application/octet-stream; name=\"%s\"" % ew("%s offer.bin" % ORG, "utf-8"), "AAAA" + CRLF,
         "Content-Disposition: attachment; filename=\"%s\"" % ew("%s offer.bin" % ORG, "utf-8") + CRLF, "base64"),
    part("application/octet-stream", "AAAA" + CRLF,
         "Content-Disposition: attachment; filename=\"%s\"" % ("%s-site.bin" % PLACE) + CRLF
         + "Content-Description: notes of %s" % ENGLISH + CRLF, "base64"),
])
M.append({"id": "m-att-names", "cls": "name", "carrier": "eml attachment names: RFC 2231 charset, RFC 2231 continuation, RFC 2047 in filename, Content-Description",
          "values": [FULL, PERSON, ORG, PLACE, ENGLISH], "visible": [mk("att-names")],
          "build": eml("attnames.eml", "attachments", _att_names)})

# bodies in other encodings
M.append({"id": "m-body-utf7", "cls": "name", "carrier": "eml text/plain body, charset utf-7",
          "values": [ORG], "visible": [mk("body-utf7")],
          "build": eml("b1.eml", "utf7", part("text/plain; charset=utf-7", ("%s for %s" % (mk("body-utf7"), ORG)).encode("utf-7") + b"\r\n", cte="7bit"))})
M.append({"id": "m-body-utf7-unlabelled", "cls": "name", "carrier": "eml text/plain body in utf-7 without a charset label",
          "values": [ORG, UTF7_ORG], "visible": [mk("body-utf7-nolabel")],
          "build": eml("b2.eml", "utf7", part("text/plain", ("%s for %s" % (mk("body-utf7-nolabel"), ORG)).encode("utf-7") + b"\r\n", cte="7bit"))})
M.append({"id": "m-body-utf16", "cls": "name", "carrier": "eml text/plain body, charset utf-16 with BOM, base64",
          "values": [FULL], "visible": [mk("body-utf16")],
          "build": eml("b3.eml", "utf16", part("text/plain; charset=utf-16", b64lines(("%s for %s\r\n" % (mk("body-utf16"), FULL)).encode("utf-16")), cte="base64"))})
M.append({"id": "m-body-utf16-unlabelled", "cls": "name", "carrier": "eml text/plain body in utf-16 (no BOM) without a charset label, 8bit",
          "values": [FULL], "visible": [mk("body-utf16-nolabel")],
          "build": eml("b4.eml", "utf16", part("text/plain", ("%s for %s\r\n" % (mk("body-utf16-nolabel"), FULL)).encode("utf-16-le"), cte="8bit"))})
M.append({"id": "m-body-8859-15", "cls": "name", "carrier": "eml text/plain body, charset iso-8859-15, 8bit",
          "values": [ORG], "visible": [mk("body-latin9")],
          "build": eml("b5.eml", "latin9", part("text/plain; charset=iso-8859-15", ("%s: 5 € for %s\r\n" % (mk("body-latin9"), ORG)).encode("iso-8859-15"), cte="8bit"))})
M.append({"id": "m-body-wrong-charset", "cls": "name", "carrier": "eml text/plain body: utf-8 bytes labelled iso-8859-1 (mojibake)",
          "values": [ORG, MOJI_ORG], "visible": [mk("body-mojibake")],
          "build": eml("b6.eml", "wrong label", part("text/plain; charset=iso-8859-1", ("%s for %s\r\n" % (mk("body-mojibake"), ORG)).encode("utf-8"), cte="8bit"))})
M.append({"id": "m-body-qp-softbreak", "cls": "iban", "carrier": "eml quoted-printable body with soft breaks inside the form and inside the IBAN",
          "values": [FULL, IBAN], "visible": [mk("qp-soft")],
          "build": eml("b7.eml", "qp", part("text/plain; charset=utf-8",
                                            "%s offer for %s=" % (mk("qp-soft"), FULL[:FULL_CUT]) + CRLF + "%s, pay to %s=" % (FULL[FULL_CUT:], IBAN[:-3]) + CRLF + " %s now" % IBAN[-2:] + CRLF, cte="quoted-printable"))})
M.append({"id": "m-body-qp-unlabelled", "cls": "iban", "carrier": "eml body written quoted-printable but labelled 7bit: soft breaks stay",
          "values": [FULL, IBAN], "visible": [mk("qp-nolabel")],
          "build": eml("b8.eml", "qp", part("text/plain; charset=utf-8",
                                            "%s offer for %s=" % (mk("qp-nolabel"), FULL[:FULL_CUT]) + CRLF + "%s, pay to %s=" % (FULL[FULL_CUT:], IBAN[:-3]) + CRLF + " %s now" % IBAN[-2:] + CRLF, cte="7bit"))})
M.append({"id": "m-body-flowed", "cls": "name", "carrier": "eml format=flowed body, form and IBAN across soft breaks, DelSp=yes, quoted lines",
          "values": [FULL, IBAN, PERSON], "visible": [mk("flowed")],
          "build": eml("b9.eml", "flowed", part("text/plain; charset=utf-8; format=flowed; delsp=yes",
                                                "%s offer for %s " % (mk("flowed"), FULL_HEAD) + CRLF + "%s, pay to %s " % (FULL_TAIL, IBAN[:14]) + CRLF + " %s now" % IBAN[15:] + CRLF
                                                + "> contact %s " % FIRST + CRLF + "> %s" % LAST + CRLF + "-- " + CRLF + "sig" + CRLF, cte="8bit"))})

# structure
M.append({"id": "m-preamble-epilogue", "cls": "name", "carrier": "eml multipart preamble and epilogue",
          "values": [FULL, ORG], "visible": [mk("preamble"), mk("epilogue")],
          "build": eml("pre.eml", "structure", multipart("mixed", [part("text/plain; charset=utf-8", "the visible body." + CRLF)],
                                                          preamble="%s: offer for %s" % (mk("preamble"), FULL),
                                                          epilogue="%s: contract with %s" % (mk("epilogue"), ORG)))})
M.append({"id": "m-rfc822-forward", "cls": "name", "carrier": "eml message/rfc822 attachment: inner headers and body",
          "values": [FULL, PERSON, DOMAIN, IBAN], "visible": [mk("fwd-inner")],
          "build": eml("fwd.eml", "Fwd: something", multipart("mixed", [
              part("text/plain; charset=utf-8", "forwarded below." + CRLF),
              part("message/rfc822", "From: %s <t.b@%s>" % (PERSON, DOMAIN) + CRLF + "To: sender@example.org" + CRLF
                   + "Message-ID: <x@%s>" % DOMAIN + CRLF + "Subject: offer for %s" % FULL + CRLF + CRLF
                   + "%s pay to %s" % (mk("fwd-inner"), IBAN) + CRLF, "Content-Disposition: attachment; filename=\"fwd.eml\"" + CRLF)]))})
M.append({"id": "m-rfc822-base64", "cls": "name", "carrier": "eml message/rfc822 attachment with Content-Transfer-Encoding base64",
          "values": [FULL, PERSON], "visible": [mk("fwd-b64")],
          "build": eml("fwdb64.eml", "Fwd: something", multipart("mixed", [
              part("text/plain; charset=utf-8", "forwarded below." + CRLF),
              part("message/rfc822", b64lines(("From: %s <office@example.org>" % PERSON + CRLF + "To: sender@example.org" + CRLF
                                              + "Subject: offer for %s" % FULL + CRLF + CRLF + mk("fwd-b64") + CRLF).encode()),
                   "Content-Disposition: attachment; filename=\"fwd.eml\"" + CRLF, cte="base64")]))})
M.append({"id": "m-alt-html-form", "cls": "name", "carrier": "eml multipart/alternative: text/plain clean, text/html carries the form and the domain",
          "values": [FULL, DOMAIN], "visible": [mk("alt-html")],
          "build": eml("alt1.eml", "alternative", multipart("alternative", [
              part("text/plain; charset=utf-8", "plain version, nothing here." + CRLF),
              part("text/html; charset=utf-8", "<html><body><p>%s offer for <b>%s</b> <a href=\"https://www.%s/x\">link</a></p></body></html>" % (mk("alt-html"), FULL, DOMAIN) + CRLF)]))})
M.append({"id": "m-alt-plain-form", "cls": "name", "carrier": "eml multipart/alternative: text/plain carries the form, text/html clean",
          "values": [FULL], "visible": [mk("alt-plain")],
          "build": eml("alt2.eml", "alternative", multipart("alternative", [
              part("text/plain; charset=utf-8", "%s offer for %s" % (mk("alt-plain"), FULL) + CRLF),
              part("text/html; charset=utf-8", "<html><body><p>html version, nothing here.</p></body></html>" + CRLF)]))})
_ics = ("BEGIN:VCALENDAR" + CRLF + "BEGIN:VEVENT" + CRLF + "ORGANIZER;CN=%s:mailto:info@%s" % (FULL, DOMAIN) + CRLF
        + "LOCATION:%s" % PLACE + CRLF + "DESCRIPTION:%s" % mk("ics") + CRLF + "END:VEVENT" + CRLF + "END:VCALENDAR" + CRLF)
M.append({"id": "m-calendar", "cls": "name", "carrier": "eml text/calendar part inline and as an .ics attachment",
          "values": [FULL, DOMAIN, PLACE], "visible": [mk("ics")],
          "build": eml("cal.eml", "invitation", multipart("mixed", [
              part("text/plain; charset=utf-8", "invitation attached." + CRLF),
              part("text/calendar; charset=utf-8; method=REQUEST", _ics),
              part("application/ics; name=\"invite.ics\"", b64lines(_ics.encode()), "Content-Disposition: attachment; filename=\"invite.ics\"" + CRLF, "base64")]))})
M.append({"id": "m-smime-signed", "cls": "name", "carrier": "eml multipart/signed: signed text part plus pkcs7-signature",
          "values": [FULL], "visible": [mk("smime-signed")],
          "build": eml("sig.eml", "signed", multipart("signed", [
              part("text/plain; charset=utf-8", "%s offer for %s" % (mk("smime-signed"), FULL) + CRLF),
              part("application/pkcs7-signature; name=\"smime.p7s\"", b64lines(rand(300)), "Content-Disposition: attachment; filename=\"smime.p7s\"" + CRLF, "base64")],
              params="; protocol=\"application/pkcs7-signature\"; micalg=sha-256"))})
_der = b"\x30\x82\x04\x00\x06\x09\x2a\x86\x48\x86\xf7\x0d\x01\x07\x02\xa0\x82" + rand(700, 3) + ("Content-Type: text/plain\r\n\r\n%s offer for %s\r\n" % (mk("smime-opaque"), FULL)).encode() + rand(400, 5)
M.append({"id": "m-smime-opaque", "cls": "name", "carrier": "eml application/pkcs7-mime opaque signed-data (content inside the DER)",
          "values": [FULL], "visible": [mk("smime-opaque")],
          "build": eml("p7m.eml", "signed opaque", part("application/pkcs7-mime; smime-type=signed-data; name=\"smime.p7m\"", b64lines(_der),
                                                         "Content-Disposition: attachment; filename=\"smime.p7m\"" + CRLF, "base64"))})
_pgp = ("-----BEGIN PGP MESSAGE-----" + CRLF + "Comment: %s for %s" % (mk("pgp-armor"), FULL) + CRLF + CRLF
        + b64lines(rand(200, 9)) + "=abcd" + CRLF + "-----END PGP MESSAGE-----" + CRLF)
M.append({"id": "m-pgp-mime", "cls": "name", "carrier": "eml multipart/encrypted (PGP/MIME), form in an armor Comment header",
          "values": [FULL], "visible": [mk("pgp-armor")],
          "build": eml("pgp.eml", "encrypted", multipart("encrypted", [
              part("application/pgp-encrypted", "Version: 1" + CRLF),
              part("application/octet-stream; name=\"encrypted.asc\"", _pgp, "Content-Disposition: inline; filename=\"encrypted.asc\"" + CRLF)],
              params="; protocol=\"application/pgp-encrypted\""))})
M.append({"id": "m-pgp-clearsigned", "cls": "name", "carrier": "eml text/plain body clear-signed with PGP",
          "values": [FULL], "visible": [mk("pgp-clear")],
          "build": eml("clear.eml", "clear signed", part("text/plain; charset=utf-8",
                                                          "-----BEGIN PGP SIGNED MESSAGE-----" + CRLF + "Hash: SHA256" + CRLF + CRLF
                                                          + "%s offer for %s" % (mk("pgp-clear"), FULL) + CRLF + CRLF + "-----BEGIN PGP SIGNATURE-----" + CRLF + CRLF
                                                          + b64lines(rand(120, 11)) + "-----END PGP SIGNATURE-----" + CRLF))})

# legacy encodings inline in the body
_uu_text = ("%s offer for %s, pay to %s\n" % (mk("uu-inner"), FULL, IBAN)).encode()
M.append({"id": "m-uu-inline", "cls": "name", "carrier": "eml 7bit body with an inline uuencoded file",
          "values": [FULL, IBAN], "visible": [mk("uu-outer"), mk("uu-inner")],
          "build": eml("uu.eml", "uu", part("text/plain; charset=us-ascii", "%s, file follows" % mk("uu-outer") + CRLF + uuencode("plan.txt", _uu_text)))})
M.append({"id": "m-uu-cte", "cls": "name", "carrier": "eml part with Content-Transfer-Encoding x-uuencode",
          "values": [FULL, IBAN], "visible": [mk("uu-inner")],
          "build": eml("uucte.eml", "uu", multipart("mixed", [
              part("text/plain; charset=us-ascii", "file attached" + CRLF),
              part("text/plain; name=\"plan.txt\"", uuencode("plan.txt", _uu_text), "Content-Disposition: attachment; filename=\"plan.txt\"" + CRLF, "x-uuencode")]))})
_yenc_text = ("%s offer for %s\n" % (mk("yenc-inner"), FULL)).encode()
M.append({"id": "m-yenc-inline", "cls": "name", "carrier": "eml 8bit body with an inline yEnc block",
          "values": [FULL], "visible": [mk("yenc-outer"), mk("yenc-inner")],
          "build": eml("yenc.eml", "yenc", part("text/plain; charset=iso-8859-1", ("%s, file follows" % mk("yenc-outer")).encode() + b"\r\n" + yenc("plan.txt", _yenc_text), cte="8bit"))})
_hqx_text = ("%s offer for %s\n" % (mk("hqx-inner"), FULL)).encode()
M.append({"id": "m-binhex-inline", "cls": "name", "carrier": "eml 7bit body with an inline BinHex 4.0 block",
          "values": [FULL], "visible": [mk("hqx-outer"), mk("hqx-inner")],
          "build": eml("hqx.eml", "binhex", part("text/plain; charset=us-ascii", "%s, file follows" % mk("hqx-outer") + CRLF + binhex("plan.txt", _hqx_text)))})
_tnef = tnef_bytes(("%s plan.txt" % FULL).encode(), ("%s offer for %s\n" % (mk("tnef-inner"), FULL)).encode())
M.append({"id": "m-tnef", "cls": "name", "carrier": "eml winmail.dat (application/ms-tnef): attachment title and data inside TNEF",
          "values": [FULL], "visible": [mk("tnef-inner")],
          "build": eml("tnef.eml", "tnef", multipart("mixed", [
              part("text/plain; charset=utf-8", "see attachment" + CRLF),
              part("application/ms-tnef; name=\"winmail.dat\"", b64lines(_tnef), "Content-Disposition: attachment; filename=\"winmail.dat\"" + CRLF, "base64")]))})

# attachments: containers
_docx = docx_bytes("%s offer for %s" % (mk("zip-docx"), FULL))
M.append({"id": "m-att-zip-docx", "cls": "name", "carrier": "eml attached zip holding a docx with the form",
          "values": [FULL], "visible": [mk("zip-docx")],
          "build": eml("zipdocx.eml", "zip", multipart("mixed", [
              part("text/plain; charset=utf-8", "see attachment" + CRLF),
              part("application/zip; name=\"pack.zip\"", b64lines(zip_bytes([("offer.docx", _docx)])), "Content-Disposition: attachment; filename=\"pack.zip\"" + CRLF, "base64")]))})
_msg = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1" + b"\x00" * 16 + b"\x3e\x00\x03\x00\xfe\xff\x09\x00\x06\x00" + b"\x00" * 470 + ("%s offer for %s" % (mk("msg-inner"), FULL)).encode("utf-16-le") + b"\x00" * 64
M.append({"id": "m-att-msg", "cls": "name", "carrier": "eml attached Outlook .msg (OLE) with the form inside",
          "values": [FULL], "visible": [mk("msg-inner")],
          "build": eml("msg.eml", "msg", multipart("mixed", [
              part("text/plain; charset=utf-8", "see attachment" + CRLF),
              part("application/vnd.ms-outlook; name=\"forwarded.msg\"", b64lines(_msg), "Content-Disposition: attachment; filename=\"forwarded.msg\"" + CRLF, "base64")]))})


def _many_attachments():
    parts = [part("text/plain; charset=utf-8", "many attachments" + CRLF)]
    for i in range(203):
        if i == 201:
            body = "%s offer for %s" % (mk("att-late"), FULL)
            name = "%s-late.txt" % SHORT
        else:
            body = "attachment %d" % i
            name = "a%03d.txt" % i
        parts.append(part("text/plain; charset=utf-8; name=\"%s\"" % name, body + CRLF, "Content-Disposition: attachment; filename=\"%s\"" % name + CRLF))
    return multipart("mixed", parts)


M.append({"id": "m-att-over-limit", "cls": "name", "carrier": "eml attachment 202 of 203 (past MAX_ATTACHMENTS): its name and its content",
          "values": [FULL, SHORT], "visible": [mk("att-late")],
          "build": eml("many.eml", "many", _many_attachments())})

# mbox
_mail2 = ("From: %s <t.b@%s>" % (PERSON, DOMAIN) + CRLF + "To: sender@example.org" + CRLF + "Subject: offer for %s" % FULL + CRLF
          + "Content-Type: text/plain; charset=utf-8" + CRLF + CRLF + "%s pay to %s" % (mk("mbox-second"), IBAN) + CRLF)
M.append({"id": "m-mbox-two", "cls": "name", "carrier": ".mbox with two mails, the second carries the forms",
          "values": [FULL, PERSON, DOMAIN, IBAN], "visible": [mk("mbox-first"), mk("mbox-second")],
          "build": file_builder("box.mbox", ("From office@example.org Tue Sep 22 10:00:00 2026" + CRLF + BASE + "Subject: first" + CRLF
                                              + "Content-Type: text/plain; charset=utf-8" + CRLF + CRLF + mk("mbox-first") + CRLF + CRLF
                                              + "From t.b@example.org Tue Sep 22 11:00:00 2026" + CRLF + _mail2).encode())})
M.append({"id": "m-mbox-multipart-first", "cls": "name", "carrier": ".mbox whose first mail is multipart: the second mail lands in its epilogue",
          "values": [FULL, PERSON, DOMAIN, IBAN], "visible": [mk("mbox-first"), mk("mbox-second")],
          "build": file_builder("box2.mbox", ("From office@example.org Tue Sep 22 10:00:00 2026" + CRLF + BASE + "Subject: first" + CRLF
                                               + multipart("mixed", [part("text/plain; charset=utf-8", mk("mbox-first") + CRLF)]) + CRLF
                                               + "From t.b@example.org Tue Sep 22 11:00:00 2026" + CRLF + _mail2).encode())})
_docx_deflated = docx_bytes("%s offer for %s" % (mk("mbox-docx"), FULL), zipfile.ZIP_DEFLATED)
_mail3 = ("From: office@example.org" + CRLF + "To: sender@example.org" + CRLF + "Subject: second with docx" + CRLF
          + multipart("mixed", [part("text/plain; charset=utf-8", "docx attached" + CRLF),
                                part("application/vnd.openxmlformats-officedocument.wordprocessingml.document; name=\"offer.docx\"",
                                     b64lines(_docx_deflated), "Content-Disposition: attachment; filename=\"offer.docx\"" + CRLF, "base64")], boundary="B2"))
M.append({"id": "m-mbox-docx", "cls": "name", "carrier": ".mbox, plain first mail, second mail carries a deflated docx attachment (base64 lands in the first body)",
          "values": [FULL], "visible": [mk("mbox-first"), mk("mbox-docx")],
          "build": file_builder("box3.mbox", ("From office@example.org Tue Sep 22 10:00:00 2026" + CRLF + BASE + "Subject: first" + CRLF
                                               + "Content-Type: text/plain; charset=utf-8" + CRLF + CRLF + mk("mbox-first") + CRLF + CRLF
                                               + "From office@example.org Tue Sep 22 11:00:00 2026" + CRLF + _mail3).encode())})
_docx_stored = docx_bytes("%s offer for %s" % (mk("body-docx"), FULL), zipfile.ZIP_STORED)
M.append({"id": "m-body-b64-docx-stored", "cls": "name", "carrier": "eml text/plain body holding base64 of a stored (uncompressed) docx, no CTE",
          "values": [FULL], "visible": [mk("body-docx")],
          "build": eml("bdocx.eml", "docx in body", part("text/plain; charset=us-ascii", "pasted:" + CRLF + b64lines(_docx_stored)))})
M.append({"id": "m-body-b64-docx-deflated", "cls": "name", "carrier": "eml text/plain body holding base64 of a deflated docx, no CTE",
          "values": [FULL], "visible": [mk("mbox-docx")],
          "build": eml("bdocx2.eml", "docx in body", part("text/plain; charset=us-ascii", "pasted:" + CRLF + b64lines(_docx_deflated)))})

M.append({"id": "m-body-utf16-8bit-labelled", "cls": "name", "carrier": "eml text/plain body, charset utf-16 declared, sent 8bit (NUL bytes in the file)",
          "values": [FULL], "visible": [mk("body-utf16-8bit")],
          "build": eml("b10.eml", "utf16 8bit", part("text/plain; charset=utf-16", ("%s for %s\r\n" % (mk("body-utf16-8bit"), FULL)).encode("utf-16"), cte="8bit"))})
M.append({"id": "m-att-binary-cte", "cls": "name", "carrier": "eml attachment with Content-Transfer-Encoding binary (raw bytes with NUL) next to a plain body with the form",
          "values": [FULL], "visible": [mk("binary-cte-body")],
          "build": eml("bin.eml", "binary cte", multipart("mixed", [
              part("text/plain; charset=utf-8", "%s offer for %s" % (mk("binary-cte-body"), FULL) + CRLF),
              part("application/octet-stream; name=\"blob.bin\"", b"\x00\x01\x02" + rand(300, 29) + b"\x00\x00", "Content-Disposition: attachment; filename=\"blob.bin\"" + CRLF, "binary")]))})

# round 2 mutations
_stored_oneline = base64.b64encode(_docx_stored).decode("ascii")
M.append({"id": "m-body-b64-docx-stored-oneline", "cls": "name", "carrier": "eml text/plain body holding base64 of a stored docx on ONE line (boundary of the multi-line bug)",
          "values": [FULL], "visible": [mk("body-docx")],
          "build": eml("bdocx3.eml", "docx in body", part("text/plain; charset=us-ascii", "pasted: " + _stored_oneline + CRLF))})
_zip_stored = zip_bytes([("inner.txt", ("%s offer for %s\n" % (mk("zip-stored"), FULL)).encode())], zipfile.ZIP_STORED)
M.append({"id": "m-body-b64-zip-stored", "cls": "name", "carrier": "eml text/plain body holding multi-line base64 of a stored zip with a txt member",
          "values": [FULL], "visible": [mk("zip-stored")],
          "build": eml("bzip.eml", "zip in body", part("text/plain; charset=us-ascii", "pasted:" + CRLF + b64lines(_zip_stored)))})
M.append({"id": "m-body-b64-text-multiline", "cls": "name", "carrier": "eml text/plain body holding multi-line base64 of plain text (control)",
          "values": [FULL], "visible": [mk("b64-text")],
          "build": eml("btext.eml", "text in body", part("text/plain; charset=us-ascii", "pasted:" + CRLF + b64lines(("%s offer for %s\n" % (mk("b64-text"), FULL)).encode() * 3)))})
M.append({"id": "m-fold-phone-int-space", "cls": "phone", "carrier": "eml Subject folded with a trailing space inside an international phone number",
          "values": [PHONE_INT], "visible": [mk("fold-phone-int")],
          "build": eml("fold6.eml", "call back %s on +49 30 " % mk("fold-phone-int") + CRLF + " 1234567 today",
                       part("text/plain; charset=utf-8", "see subject." + CRLF))})
M.append({"id": "m-fold-vat-space", "cls": "vat", "carrier": "eml Subject folded with a trailing space inside a VAT id",
          "values": ["DE123456789"], "visible": [mk("fold-vat")],
          "build": eml("fold7.eml", "invoice %s USt-IdNr. DE " % mk("fold-vat") + CRLF + " 123456789 please",
                       part("text/plain; charset=utf-8", "see subject." + CRLF))})
M.append({"id": "m-fold-mail-at", "cls": "mail", "carrier": "eml To header folded after the @ of an address (FWS around @ is legal)",
          "values": ["pomblet.x@example.org"], "visible": [mk("fold-mail")],
          "build": eml("fold8.eml", "hello %s" % mk("fold-mail"), part("text/plain; charset=utf-8", "see headers." + CRLF),
                       head="From: office@example.org" + CRLF + "To: pomblet.x@" + CRLF + " example.org" + CRLF)})
M.append({"id": "m-fold-iban-space-last", "cls": "iban", "carrier": "eml Subject folded with a trailing space before the last IBAN group",
          "values": [IBAN], "visible": [mk("fold-iban-last")],
          "build": eml("fold9.eml", "payment %s to %s " % (mk("fold-iban-last"), IBAN[:24]) + CRLF + " " + IBAN[24:] + " please",
                       part("text/plain; charset=utf-8", "see subject." + CRLF))})
M.append({"id": "m-fold-iban-body-flowed", "cls": "iban", "carrier": "eml format=flowed body: IBAN split at a soft break with DelSp=no and a space-stuffed next line",
          "values": [IBAN], "visible": [mk("flowed-iban")],
          "build": eml("flow2.eml", "flowed iban", part("text/plain; charset=utf-8; format=flowed",
                                                         "%s pay to %s " % (mk("flowed-iban"), IBAN[:19]) + CRLF + "  " + IBAN[19:] + " now" + CRLF, cte="8bit"))})
_dsn = ("Reporting-MTA: dns; mail.example.org" + CRLF + CRLF + "Final-Recipient: rfc822; info@%s" % DOMAIN + CRLF
        + "Original-Recipient: rfc822; %s <t.b@%s>" % (PERSON, DOMAIN) + CRLF + "Action: failed" + CRLF + "Status: 5.1.1" + CRLF)
M.append({"id": "m-dsn-bounce", "cls": "mail", "carrier": "eml multipart/report bounce: message/delivery-status fields and the returned headers",
          "values": [DOMAIN, PERSON, FULL], "visible": [mk("dsn-human"), mk("dsn-fields")],
          "build": eml("dsn.eml", "Undelivered", multipart("report", [
              part("text/plain; charset=utf-8", "%s: delivery failed" % mk("dsn-human") + CRLF),
              part("message/delivery-status", _dsn.replace("Action: failed", "Action: failed" + CRLF + "X-Note: %s" % mk("dsn-fields"))),
              part("text/rfc822-headers", "From: office@example.org" + CRLF + "To: info@%s" % DOMAIN + CRLF + "Subject: offer for %s" % FULL + CRLF)],
              params="; report-type=delivery-status"))})
M.append({"id": "m-rfc822-preamble", "cls": "name", "carrier": "eml message/rfc822 attachment whose inner multipart preamble carries the form",
          "values": [FULL], "visible": [mk("inner-preamble")],
          "build": eml("fwd2.eml", "Fwd: inner", multipart("mixed", [
              part("text/plain; charset=utf-8", "forwarded below." + CRLF),
              part("message/rfc822", "From: office@example.org" + CRLF + "To: sender@example.org" + CRLF + "Subject: inner" + CRLF
                   + multipart("mixed", [part("text/plain; charset=utf-8", "inner body." + CRLF)], boundary="B9",
                               preamble="%s: offer for %s" % (mk("inner-preamble"), FULL)))]))})

# --------------------------------------------------------------------------- archive cases

A = []
_plain = ("%s offer for %s\n" % (mk("archive-content"), FULL)).encode()

A.append({"id": "a-zip-cp437-name", "cls": "name", "carrier": "zip member name in cp437 without the utf-8 flag (umlaut form)",
          "values": [ORG], "visible": [mk("cp437")],
          "build": file_builder("n1.zip", hand_zip([{"name": ("%s plan.txt" % ORG).encode("cp437"), "data": mk("cp437").encode()}]))})
A.append({"id": "a-zip-cp1252-name", "cls": "name", "carrier": "zip member name in cp1252 bytes without the utf-8 flag (umlaut form)",
          "values": [ORG], "visible": [mk("cp1252")],
          "build": file_builder("n2.zip", hand_zip([{"name": ("%s plan.txt" % ORG).encode("cp1252"), "data": mk("cp1252").encode()}]))})
A.append({"id": "a-zip-utf8-flag", "cls": "name", "carrier": "zip member name utf-8 with the flag, and the form as a folder name",
          "values": [ORG, FULL], "visible": [mk("utf8flag")],
          "build": file_builder("n3.zip", zip_bytes([("%s/%s plan.txt" % (FULL, ORG), mk("utf8flag").encode())]))})
_uextra = ("%s.txt" % mk("unicode-extra")).encode()
A.append({"id": "a-zip-7075-form-in-raw", "cls": "name", "carrier": "zip: form in the cp437 name, innocent name in the 0x7075 unicode path extra",
          "values": [FULL], "visible": [mk("unicode-extra")],
          "build": file_builder("n4.zip", hand_zip([{"name": ("%s plan.txt" % FULL).encode("cp437"), "data": b"content\n",
                                                     "extra": struct.pack("<HHBI", 0x7075, 5 + len(_uextra), 1, zlib.crc32(("%s plan.txt" % FULL).encode("cp437"))) + _uextra}]))})
_uextra2 = ("%s plan.txt" % FULL).encode()
A.append({"id": "a-zip-7075-form-in-extra", "cls": "name", "carrier": "zip: innocent cp437 name, form in the 0x7075 unicode path extra",
          "values": [FULL], "visible": [mk("raw-name")],
          "build": file_builder("n5.zip", hand_zip([{"name": ("%s.txt" % mk("raw-name")).encode("cp437"), "data": b"content\n",
                                                     "extra": struct.pack("<HHBI", 0x7075, 5 + len(_uextra2), 1, zlib.crc32(("%s.txt" % mk("raw-name")).encode("cp437"))) + _uextra2}]))})
A.append({"id": "a-zip-comments", "cls": "name", "carrier": "zip archive comment and member comment",
          "values": [FULL, ORG, CAND], "visible": [mk("zip-comment"), mk("member-comment")],
          "build": file_builder("n6.zip", hand_zip([{"name": b"plan.txt", "data": b"content\n",
                                                     "comment": ("%s: %s" % (mk("member-comment"), ORG)).encode()}])
                                [:-2] + struct.pack("<H", len(("%s: %s, %s" % (mk("zip-comment"), FULL, CAND)).encode()))
                                + ("%s: %s, %s" % (mk("zip-comment"), FULL, CAND)).encode())})


def _zip64(inbox):
    p = inbox / "n7.zip"
    with zipfile.ZipFile(str(p), "w") as zf:
        with zf.open(zipfile.ZipInfo("%s plan.txt" % FULL), "w", force_zip64=True) as fh:
            fh.write(("%s offer for %s\n" % (mk("zip64"), ORG)).encode())
    return p


A.append({"id": "a-zip64", "cls": "name", "carrier": "zip64 member: name and content", "values": [FULL, ORG], "visible": [mk("zip64")], "build": _zip64})
A.append({"id": "a-zip-trailing-slash-file", "cls": "name", "carrier": "zip member named with a trailing slash but carrying data",
          "values": [FULL], "visible": [mk("slash-data")],
          "build": file_builder("n8.zip", hand_zip([{"name": b"notes.txt/", "data": ("%s offer for %s\n" % (mk("slash-data"), FULL)).encode()}]))})
A.append({"id": "a-zip-encrypted", "cls": "name", "carrier": "zip member flagged encrypted (content in plain, flag set)",
          "values": [FULL], "visible": [mk("encrypted")],
          "build": file_builder("n9.zip", hand_zip([{"name": b"secret.txt", "flags": 0x1, "data": ("%s offer for %s\n" % (mk("encrypted"), FULL)).encode()}]))})
A.append({"id": "a-zip-local-name-differs", "cls": "name", "carrier": "zip: local header name differs from the central directory name",
          "values": [FULL], "visible": [mk("local-name")],
          "build": file_builder("n10.zip", hand_zip([{"name": b"plan-innocent-0000000.txt", "local_name": ("%s.txt" % FULL).encode()[:25].ljust(25, b"x"),
                                                      "data": ("%s offer for %s\n" % (mk("local-name"), FULL)).encode()}]))})
A.append({"id": "a-zip-traversal", "cls": "name", "carrier": "zip member name with path traversal and the form",
          "values": [FULL], "visible": [mk("traversal")],
          "build": file_builder("n11.zip", zip_bytes([("../../../tmp/%s.txt" % FULL, ("%s offer\n" % mk("traversal")).encode())]))})


def _many_members(inbox):
    p = inbox / "n12.zip"
    with zipfile.ZipFile(str(p), "w") as zf:
        for i in range(240):
            if i == 230:
                zf.writestr("%s late.txt" % FULL, ("%s offer for %s, %s\n" % (mk("late-member"), ORG, IBAN)).encode())
            else:
                zf.writestr("m%03d.txt" % i, b"member\n")
    return p


A.append({"id": "a-zip-over-members", "cls": "name", "carrier": "zip member 231 of 240 (past MAX_MEMBERS): its name and its content",
          "values": [FULL, ORG, IBAN], "visible": [mk("late-member")], "build": _many_members})

# tar
def _tar_names(inbox):
    p = inbox / "t1.tar"
    long_name = "a/" + "b" * 90 + "/%s plan.txt" % FULL
    sym = tarfile.TarInfo("link-to-plan"); sym.type = tarfile.SYMTYPE; sym.linkname = "%s/plan.txt" % ORG
    hard = tarfile.TarInfo("hard-link"); hard.type = tarfile.LNKTYPE; hard.linkname = long_name
    pax = tarfile.TarInfo("with-pax.txt"); pax.pax_headers = {"comment": "%s: %s, %s" % (mk("pax-comment"), ENGLISH, CAND)}
    pax.uname = PERSON; pax.gname = PLACE
    data = ("%s offer\n" % mk("tar-content")).encode()
    p.write_bytes(tar_bytes([(long_name, data), sym, hard, (pax, data)], fmt=tarfile.PAX_FORMAT))
    return p


A.append({"id": "a-tar-names-links-pax", "cls": "name", "carrier": "tar: pax long path with the form, symlink target, hard link target, pax comment, uname, gname",
          "values": [FULL, ORG, ENGLISH, PERSON, PLACE, CAND], "visible": [mk("tar-content"), mk("pax-comment")], "build": _tar_names})


def _tar_gnu(inbox):
    p = inbox / "t2.tar"
    long_name = "x/" + "y" * 120 + "/%s plan.txt" % FULL
    sym = tarfile.TarInfo("gnu-link"); sym.type = tarfile.SYMTYPE; sym.linkname = "z/" + "w" * 120 + "/%s.txt" % ORG
    p.write_bytes(tar_bytes([(long_name, ("%s offer\n" % mk("gnu-content")).encode()), sym], fmt=tarfile.GNU_FORMAT))
    return p


A.append({"id": "a-tar-gnu-longname", "cls": "name", "carrier": "tar GNU long name and GNU long link target with the form",
          "values": [FULL, ORG], "visible": [mk("gnu-content")], "build": _tar_gnu})
A.append({"id": "a-gz-single-header", "cls": "name", "carrier": ".txt.gz: gzip header original name and comment",
          "values": [FULL, ORG, CAND], "visible": [mk("gz-content"), mk("gz-comment")],
          "build": file_builder("report.txt.gz", gz_bytes(("%s plain content\n" % mk("gz-content")).encode(), fname="%s report.txt" % FULL,
                                                          fcomment="%s: %s, %s" % (mk("gz-comment"), ORG, CAND)))})
A.append({"id": "a-targz-header", "cls": "name", "carrier": ".tar.gz: gzip header original name and comment around a tar",
          "values": [FULL, ORG, CAND], "visible": [mk("targz-content"), mk("targz-comment")],
          "build": file_builder("pack.tar.gz", gz_bytes(tar_bytes([("plan.txt", ("%s content\n" % mk("targz-content")).encode())], mode="w"),
                                                        fname="%s pack.tar" % FULL, fcomment="%s: %s, %s" % (mk("targz-comment"), ORG, CAND)))})
A.append({"id": "a-bz2-single", "cls": "name", "carrier": ".txt.bz2 single file content", "values": [FULL], "visible": [mk("bz2")],
          "build": file_builder("note.txt.bz2", bz2.compress(("%s offer for %s\n" % (mk("bz2"), FULL)).encode()))})
A.append({"id": "a-xz-single", "cls": "name", "carrier": ".txt.xz single file content", "values": [FULL], "visible": [mk("xz")],
          "build": file_builder("note.txt.xz", lzma.compress(("%s offer for %s\n" % (mk("xz"), FULL)).encode()))})
A.append({"id": "a-7z", "cls": "name", "carrier": "7z archive (no reader): form after the signature",
          "values": [FULL], "visible": [mk("sevenz")],
          "build": file_builder("pack.7z", b"7z\xbc\xaf\x27\x1c\x00\x04" + rand(200, 13) + ("%s offer for %s\n" % (mk("sevenz"), FULL)).encode() + rand(100, 17))})
A.append({"id": "a-rar", "cls": "name", "carrier": "rar archive (no reader): form after the signature",
          "values": [FULL], "visible": [mk("rar")],
          "build": file_builder("pack.rar", b"Rar!\x1a\x07\x01\x00" + rand(200, 19) + ("%s offer for %s\n" % (mk("rar"), FULL)).encode() + rand(100, 23))})
_z4 = zip_bytes([("%s deep.txt" % FULL, ("%s offer for %s\n" % (mk("depth4"), ORG)).encode())])
_z3 = zip_bytes([("l3.zip", _z4)]); _z2 = zip_bytes([("l2.zip", _z3)]); _z1 = zip_bytes([("l1.zip", _z2)])
A.append({"id": "a-nested-depth4", "cls": "name", "carrier": "zip nested four deep: member name at depth 3 listing, content at depth 4",
          "values": [FULL, ORG], "visible": [mk("depth4")], "build": file_builder("deep.zip", _z1)})


def _docx_with_zip(text_zip: bytes) -> bytes:
    """A docx that carries a zip as an embedded package part."""
    base = docx_bytes("docx body, nothing here")
    buf = io.BytesIO()
    with zipfile.ZipFile(io.BytesIO(base)) as src, zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as dst:
        for info in src.infolist():
            dst.writestr(info, src.read(info))
        dst.writestr("word/embeddings/package.zip", text_zip)
    return buf.getvalue()


A.append({"id": "a-zip-docx-zip", "cls": "name", "carrier": "zip > docx > embedded zip > txt with the form",
          "values": [FULL], "visible": [mk("docx-zip")],
          "build": file_builder("outer.zip", zip_bytes([("report.docx", _docx_with_zip(zip_bytes([("inner.txt", ("%s offer for %s\n" % (mk("docx-zip"), FULL)).encode())])))]))})
_eml_with_docx = mail("mail in zip", multipart("mixed", [
    part("text/plain; charset=utf-8", "docx attached" + CRLF),
    part("application/vnd.openxmlformats-officedocument.wordprocessingml.document; name=\"offer.docx\"",
         b64lines(docx_bytes("%s offer for %s" % (mk("zip-eml-docx"), FULL))), "Content-Disposition: attachment; filename=\"offer.docx\"" + CRLF, "base64")]))
A.append({"id": "a-zip-eml-attachment", "cls": "name", "carrier": "zip > eml > docx attachment with the form",
          "values": [FULL], "visible": [mk("zip-eml-docx")], "build": file_builder("mails.zip", zip_bytes([("one.eml", _eml_with_docx)]))})
A.append({"id": "a-tar-past-bytes", "cls": "name", "carrier": "tar.gz with a 201 MB member before a member with the form",
          "values": [FULL], "visible": [mk("tar-past-bytes")], "build": build_big_tar})

# round 3: controls and boundaries
M.append({"id": "m-fold-iban-plain", "cls": "iban", "carrier": "eml Subject folded at a space without trailing white space (control: one space after unfolding)",
          "values": [IBAN], "visible": [mk("fold-iban-plain")],
          "build": eml("fold10.eml", "payment %s to %s" % (mk("fold-iban-plain"), IBAN[:19]) + CRLF + " " + IBAN[20:] + " please",
                       part("text/plain; charset=utf-8", "see subject." + CRLF))})
M.append({"id": "m-body-iban-double-space", "cls": "iban", "carrier": "eml plain body: IBAN written with two spaces between two groups (boundary: not a fold)",
          "values": [IBAN], "visible": [mk("body-double")],
          "build": eml("dbl.eml", "double", part("text/plain; charset=utf-8", "%s pay to %s  %s now" % (mk("body-double"), IBAN[:19], IBAN[20:]) + CRLF))})
M.append({"id": "m-body-phone-double-space", "cls": "phone", "carrier": "eml plain body: phone number with two spaces after the area code (boundary: not a fold)",
          "values": [PHONE], "visible": [mk("body-double-phone")],
          "build": eml("dbl2.eml", "double", part("text/plain; charset=utf-8", "%s call 030  1234567 now" % mk("body-double-phone") + CRLF))})
M.append({"id": "m-body-single-nul", "cls": "name", "carrier": "eml plain mail with one NUL byte in the body (sniff boundary), form in the Subject",
          "values": [FULL], "visible": [mk("single-nul")],
          "build": eml("nul.eml", "offer for %s" % FULL, part("text/plain; charset=utf-8", ("%s before" % mk("single-nul")).encode() + b"\x00" + b" after" + b"\r\n", cte="8bit"))})
M.append({"id": "m-body-nul-late", "cls": "name", "carrier": "eml plain mail with a NUL byte after the first 64 KB (sniff boundary), form in the Subject",
          "values": [FULL], "visible": [mk("late-nul")],
          "build": eml("nul2.eml", "offer for %s" % FULL, part("text/plain; charset=utf-8", ("%s before " % mk("late-nul")).encode() + b"x" * 70000 + b"\x00" + b" after" + b"\r\n", cte="8bit"))})


def _nested_rfc822(depth_left: int, marker: str) -> str:
    if depth_left == 0:
        return "From: office@example.org" + CRLF + "To: sender@example.org" + CRLF + "Subject: deepest for %s" % FULL + CRLF + CRLF + marker + CRLF
    inner = _nested_rfc822(depth_left - 1, marker)
    return ("From: office@example.org" + CRLF + "To: sender@example.org" + CRLF + "Subject: level %d" % depth_left + CRLF
            + multipart("mixed", [part("text/plain; charset=utf-8", "level %d body" % depth_left + CRLF),
                                  part("message/rfc822", inner, "Content-Disposition: attachment; filename=\"fwd%d.eml\"" % depth_left + CRLF)],
                        boundary="N%d" % depth_left))


M.append({"id": "m-rfc822-depth4", "cls": "name", "carrier": "eml message/rfc822 nested four deep: the deepest mail's Subject and body",
          "values": [FULL], "visible": [mk("rfc822-deep")],
          "build": file_builder("deep.eml", _nested_rfc822(4, mk("rfc822-deep")).encode("latin-1"))})
M.append({"id": "m-att-suffix-form", "cls": "name", "carrier": "eml attachment whose file suffix is the short form",
          "values": [SHORT], "visible": [mk("suffix-form")],
          "build": eml("suf.eml", "suffix", multipart("mixed", [
              part("text/plain; charset=utf-8", mk("suffix-form") + CRLF),
              part("application/octet-stream; name=\"offer.%s\"" % SHORT, "AAAA" + CRLF, "Content-Disposition: attachment; filename=\"offer.%s\"" % SHORT + CRLF, "base64")]))})
M.append({"id": "m-hdr-fold-tab-inside-form", "cls": "name", "carrier": "eml Subject folded with a tab inside a word of the form",
          "values": [FULL], "visible": [mk("fold-tab-word")],
          "build": eml("fold11.eml", "offer %s for %s" % (mk("fold-tab-word"), FULL[:FULL_CUT]) + CRLF + "\t%s today" % FULL[FULL_CUT:],
                       part("text/plain; charset=utf-8", "see subject." + CRLF))})
A.append({"id": "a-zip-name-newline", "cls": "name", "carrier": "zip member name with a line break inside the form",
          "values": [FULL], "visible": [mk("name-newline")],
          "build": file_builder("nl.zip", hand_zip([{"name": ("%s\n%s plan.txt" % (FULL[:5], FULL[5:])).encode(), "flags": 0x800, "data": mk("name-newline").encode()}]))})

# round 4: nearby shapes
_b64name = base64.b64encode(FULL.encode()).decode("ascii")
M.append({"id": "m-att-name-b64-blob", "cls": "name", "carrier": "eml attachment file name that is the base64 of the form",
          "values": [FULL], "visible": [mk("name-b64")],
          "build": eml("nb64.eml", "name", multipart("mixed", [
              part("text/plain; charset=utf-8", mk("name-b64") + CRLF),
              part("application/octet-stream; name=\"%s.pdf\"" % _b64name, "AAAA" + CRLF, "Content-Disposition: attachment; filename=\"%s.pdf\"" % _b64name + CRLF, "base64")]))})
M.append({"id": "m-alt-plain-empty", "cls": "name", "carrier": "eml multipart/alternative with an empty text/plain: the html version is chosen",
          "values": [FULL], "visible": [mk("alt-empty")],
          "build": eml("alt3.eml", "alternative", multipart("alternative", [
              part("text/plain; charset=utf-8", CRLF),
              part("text/html; charset=utf-8", "<html><body><p>%s offer for %s</p></body></html>" % (mk("alt-empty"), FULL) + CRLF)]))})
M.append({"id": "m-att-eml-octet-nosuffix", "cls": "name", "carrier": "eml forwarded mail attached as application/octet-stream without a file suffix",
          "values": [FULL, PERSON], "visible": [mk("octet-eml")],
          "build": eml("oct.eml", "fwd", multipart("mixed", [
              part("text/plain; charset=utf-8", "attached" + CRLF),
              part("application/octet-stream", b64lines(("From: %s <office@example.org>" % PERSON + CRLF + "To: sender@example.org" + CRLF
                                                          + "Subject: offer for %s" % FULL + CRLF + CRLF + mk("octet-eml") + CRLF).encode()),
                   "Content-Disposition: attachment; filename=\"forwarded\"" + CRLF, "base64")]))})
_mixed_docx = multipart("mixed", [part("text/plain; charset=utf-8", "%s docx attached" % mk("broken-boundary") + CRLF),
                                  part("application/vnd.openxmlformats-officedocument.wordprocessingml.document; name=\"offer.docx\"",
                                       b64lines(_docx_deflated), "Content-Disposition: attachment; filename=\"offer.docx\"" + CRLF, "base64")], boundary="REAL")
M.append({"id": "m-broken-boundary-docx", "cls": "name", "carrier": "eml multipart whose declared boundary does not match the body (mangled): the docx base64 becomes body text",
          "values": [FULL], "visible": [mk("broken-boundary"), mk("mbox-docx")],
          "build": eml("bb.eml", "broken", _mixed_docx.replace("boundary=\"REAL\"", "boundary=\"WRONG\""))})
M.append({"id": "m-multipart-cte-base64", "cls": "name", "carrier": "eml multipart part carrying Content-Transfer-Encoding base64 (illegal): the whole body is one base64 block",
          "values": [FULL], "visible": [mk("multipart-b64")],
          "build": eml("mpb64.eml", "mp", "Content-Type: multipart/mixed; boundary=\"B7\"" + CRLF + "Content-Transfer-Encoding: base64" + CRLF + CRLF
                       + b64lines(("--B7" + CRLF + "Content-Type: text/plain" + CRLF + CRLF + "%s offer for %s" % (mk("multipart-b64"), FULL) + CRLF + "--B7--" + CRLF).encode()))})
M.append({"id": "m-hdr-raw-cp1252", "cls": "name", "carrier": "eml Subject as raw 8-bit cp1252 bytes (no encoded word), full umlaut form, plus a From display name",
          "values": [ORG, PERSON], "visible": [mk("raw-utf8")],
          "build": eml("raw8.eml", "%s offer for %s" % (mk("raw-utf8"), ORG), part("text/plain; charset=utf-8", "body." + CRLF),
                       head="From: %s <office@example.org>" % PERSON + CRLF + "To: sender@example.org" + CRLF)})
A.append({"id": "a-zip-member-name-structured", "cls": "iban", "carrier": "zip member names carrying an IBAN and a phone number",
          "values": [IBAN, PHONE], "visible": [mk("name-structured")],
          "build": file_builder("st.zip", zip_bytes([("Rechnung %s.pdf" % IBAN, mk("name-structured").encode()), ("Anruf %s.txt" % PHONE, b"x\n")]))})
A.append({"id": "a-zip-utf8-name-no-flag", "cls": "name", "carrier": "zip member name in utf-8 bytes without the utf-8 flag",
          "values": [ORG], "visible": [mk("utf8-noflag")],
          "build": file_builder("u8.zip", hand_zip([{"name": ("%s plan.txt" % ORG).encode("utf-8"), "data": mk("utf8-noflag").encode()}]))})
A.append({"id": "a-zstd", "cls": "name", "carrier": "zstd compressed single file (no reader)",
          "values": [FULL], "visible": [mk("zstd")],
          "build": file_builder("note.txt.zst", b"\x28\xb5\x2f\xfd" + rand(120, 31) + ("%s offer for %s\n" % (mk("zstd"), FULL)).encode() + rand(60, 37))})

# round 5: raw 8-bit utf-8 headers, the spelling a reader gets when the umlaut is lost
ORG_SHORT = fx.ORG_FORMS[1]
FFFD_ORG = "".join("\ufffd" if c in "\u00e4\u00f6\u00fc\u00c4\u00d6\u00dc\u00df" else c for c in ORG)
FFFD_SHORT = "".join("\ufffd" if c in "\u00e4\u00f6\u00fc\u00c4\u00d6\u00dc\u00df" else c for c in ORG_SHORT)
M.append({"id": "m-hdr-raw-cp1252-short", "cls": "name", "carrier": "eml Subject as raw 8-bit cp1252 bytes carrying the short umlaut form",
          "values": [ORG_SHORT, FFFD_SHORT], "visible": [mk("raw-utf8-short")],
          "build": eml("raw8b.eml", "%s: the %s team" % (mk("raw-utf8-short"), ORG_SHORT), part("text/plain; charset=utf-8", "body." + CRLF))})
M.append({"id": "m-hdr-raw-cp1252-full", "cls": "name", "carrier": "eml Subject and To display name as raw 8-bit cp1252 bytes carrying the full umlaut form",
          "values": [ORG, FFFD_ORG], "visible": [mk("raw-utf8-full")],
          "build": eml("raw8c.eml", "%s: offer for %s" % (mk("raw-utf8-full"), ORG), part("text/plain; charset=utf-8", "body." + CRLF),
                       head="From: office@example.org" + CRLF + "To: %s <sender@example.org>" % ORG + CRLF)})
M.append({"id": "m-hdr-raw-cp1252-ascii-forms", "cls": "name", "carrier": "eml raw 8-bit cp1252 Subject carrying forms without umlauts next to an umlaut word (control)",
          "values": [PERSON, PLACE], "visible": [mk("raw-utf8-ascii")],
          "build": eml("raw8d.eml", "%s: Gru\u00df an %s in %s" % (mk("raw-utf8-ascii"), PERSON, PLACE), part("text/plain; charset=utf-8", "body." + CRLF))})

# true raw utf-8 headers (RFC 6532), built as bytes so the umlaut is utf-8 on the wire
_u8 = lambda t: t.encode("utf-8")
M.append({"id": "m-hdr-raw-utf8-short", "cls": "name", "carrier": "eml Subject as raw 8-bit utf-8 bytes (RFC 6532) carrying the short umlaut form (control)",
          "values": [ORG_SHORT, FFFD_SHORT], "visible": [mk("raw-utf8-short")],
          "build": file_builder("raw8e.eml", _u8(BASE + "Subject: %s: the %s team" % (mk("raw-utf8-short"), ORG_SHORT) + CRLF + "Content-Type: text/plain; charset=utf-8" + CRLF + CRLF + "body." + CRLF))})
M.append({"id": "m-hdr-raw-utf8-full", "cls": "name", "carrier": "eml Subject and To display name as raw 8-bit utf-8 bytes carrying the full umlaut form (control)",
          "values": [ORG, FFFD_ORG], "visible": [mk("raw-utf8-full")],
          "build": file_builder("raw8f.eml", _u8("From: office@example.org" + CRLF + "To: %s <sender@example.org>" % ORG + CRLF + "Subject: %s: offer for %s" % (mk("raw-utf8-full"), ORG) + CRLF
                                              + "Content-Type: text/plain; charset=utf-8" + CRLF + CRLF + "body." + CRLF))})
M.append({"id": "m-hdr-raw-cp1252-from", "cls": "name", "carrier": "eml From display name as raw 8-bit cp1252 bytes carrying the short umlaut form",
          "values": [ORG_SHORT, FFFD_SHORT], "visible": [mk("raw-cp1252-from")],
          "build": file_builder("raw8g.eml", ("From: %s Team <office@example.org>" % ORG_SHORT + CRLF + "To: sender@example.org" + CRLF + "Subject: %s" % mk("raw-cp1252-from") + CRLF
                                              + "Content-Type: text/plain; charset=utf-8" + CRLF + CRLF + "body." + CRLF).encode("cp1252"))})

CASES = M + A


# What tests/test_redteam_pack.py accepts besides the fixture forms. DERIVED: strings built from a fixture form
# (a typo, an encoding, a transliteration). INVENTED: structured values this module builds, invented but valid
# in shape (documentation ranges, fake digit families). A value may carry one of them whole or in part.
DERIVED = (MOJI_ORG, UTF7_ORG, FFFD_ORG, FFFD_SHORT, Q_WORD, PERSON_Q)
INVENTED = (IBAN, PHONE, PHONE_INT, IP, "DE123456789")
