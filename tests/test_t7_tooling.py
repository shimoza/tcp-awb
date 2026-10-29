"""T7 of 2026-10-28: seven defects a project session reported, each reproduced first.

A. A put rewrote a PDF whose streams are ASCII (ASCII85, hex): its bytes decode as UTF-8, so the secret scrub of the
   put took stream lines for tokens. The write hook had the same path. Binary files now go byte for byte.
B. The lab-log shortening of the write hook took code for a password: `getpass.getpass("...")` became [secret].
C. `awb check` called a lab log with more than 1000 ids opaque: every id counted as an encoded block.
D. The session guard read the API path `/` of `awb cloud call GET obs /` as the root folder.
E. The review took the field name "total" in a quoted raw API answer for a computed number.
F. Every PDF was opaque to `awb check` of the work user (the unit runner needs the owner's sudo) and to the gate.
G. A hex request id was a finding (a phone number, or a web address in the strings of its bytes).

Invented names only (tests/fixtures.py). Ids, keys and passwords are made at run time.
"""
from __future__ import annotations

import base64
import dataclasses
import hashlib
import random
import struct
import uuid
import zipfile
import zlib
from collections import Counter
from pathlib import Path

import pytest

from awb import check, config, gate, labsecrets, putcopy, review
from awb.extract import pdf
from awb.matcher import Matcher
from awb.tcp import keys
from tests import fixtures as fx
from tests.test_hooks import project, run_hook  # noqa: F401 (fixture)
from tests.test_pd23_guard import bash, world  # noqa: F401 (fixture)
from tests.test_xchg import buckets, cust_project, lab_project, quiet, run, svc  # noqa: F401 (fixtures)


def noise(n: int, seed: int) -> bytes:
    rnd = random.Random(seed)
    return bytes(rnd.randrange(256) for _ in range(n))


def png_bytes() -> bytes:
    def chunk(kind: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)
    raw = b"".join(b"\x00" + noise(60, row) for row in range(20))
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", 20, 20, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b""))


def binary_pdf(path: Path, text: str = "A plain page about block storage and its volumes in the region.") -> Path:
    """A PDF of reportlab with a compressed page and a picture: binary streams."""
    from reportlab.lib.utils import ImageReader
    from reportlab.pdfgen import canvas

    picture = path.with_suffix(".png")
    picture.write_bytes(png_bytes())
    c = canvas.Canvas(str(path))
    c.drawString(72, 750, text)
    c.drawImage(ImageReader(str(picture)), 72, 400, 200, 200)
    c.save()
    return path


def ascii_pdf(path: Path) -> Path:
    """A PDF whose streams are ASCII85 and hex: every byte is ASCII, so it decodes as UTF-8."""
    raw = noise(30000, 7)
    # a stream line that reads like a temporary key id to the secret rules (ASCII85 has these characters)
    keyish = ("AS" + "IA" + "".join(random.Random(3).choice("ABCDEFGHJKLMNPQRSTUVWXYZ234567") for _ in range(16)))
    a85 = base64.a85encode(zlib.compress(raw), wrapcol=75) + b"\n" + keyish.encode() + b"~>"
    hexed = raw[:6000].hex().encode()
    path.write_bytes(
        b"%PDF-1.4\n1 0 obj\n<< /Type /XObject /Subtype /Image /Filter [/ASCII85Decode /FlateDecode] /Length "
        + str(len(a85)).encode() + b" >>\nstream\n" + a85 + b"\nendstream\nendobj\n"
        b"2 0 obj\n<< /Filter /ASCIIHexDecode >>\nstream\n" + hexed + b"\nendstream\nendobj\n%%EOF\n")
    return path


def plain_docx(path: Path) -> Path:
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("[Content_Types].xml", "<Types/>")
        z.writestr("word/document.xml", "<w:document><w:t>The cluster runs 3 nodes.</w:t></w:document>")
        z.writestr("word/media/image1.png", png_bytes())
    return path


def binaries(folder: Path) -> list[Path]:
    picture = folder / "shot.png"
    picture.write_bytes(png_bytes())
    return [binary_pdf(folder / "calllog.pdf"), ascii_pdf(folder / "ascii.pdf"), picture,
            plain_docx(folder / "plain.docx")]


# --------------------------------------------------------------------------- A: binary files go byte for byte


def test_t7_a_scrub_copies_a_pdf_a_picture_and_a_docx_without_secrets_byte_for_byte(tmp_path):
    for f in binaries(tmp_path):
        out = tmp_path / ("out-" + f.name)
        assert putcopy.scrub(f, out) == 0, f.name
        assert out.read_bytes() == f.read_bytes(), f.name


def test_t7_a_the_ascii_pdf_reproduces_the_loss_of_the_text_scrub(tmp_path):
    f = ascii_pdf(tmp_path / "ascii.pdf")
    text = f.read_bytes().decode("utf-8")             # the bytes read as text: the old path of scrub
    new, n = putcopy.scrub_text(text)
    assert n and len(new) < len(text), "the text scrub of the put shortened the stream lines"


def test_t7_a_a_session_put_sends_binary_files_byte_for_byte(svc, buckets, cust_project, capsys):
    lab, _ = buckets
    folder = Path(cust_project.path) / "deliverables"
    for f in binaries(folder):
        args = ["xchg", "put", str(f)] + (["--image", "--reason", "the lab console, checked for names"]
                                          if f.suffix == ".png" else [])
        before = set(lab.objects)
        code, out, err = run(args, capsys)
        assert code == 0, err
        new = [k for k in set(lab.objects) - before if k.endswith(f.suffix)]
        assert len(new) == 1 and lab.objects[new[0]] == f.read_bytes(), f.name
        assert "secret" not in err


def test_t7_a_the_service_put_keeps_binary_files_byte_for_byte(svc, buckets, lab_project, tmp_path):
    lab, _ = buckets
    for n, f in enumerate(binaries(tmp_path)):
        key = "%s/from-session/2026-10-28/put-%012x%s" % (lab_project.code, n + 1, f.suffix)
        req = {"op": "obs", "method": "PUT", "project": lab_project.code, "key": key, "name": f.name}
        if f.suffix == ".png":
            req.update(image=True, reason="the lab console, checked for names")
        a = keys.request(svc.call_path, req, upload=f)
        assert a["ok"] and a["secrets"] == 0 and a["size"] == f.stat().st_size, (f.name, a)
        assert lab.objects[key] == f.read_bytes(), f.name


def test_t7_a_an_office_file_is_rewritten_only_for_a_real_secret(tmp_path):
    value = "Qx" + hashlib.sha256(b"t7").hexdigest()[:20] + "Zr7"
    office = tmp_path / "keyed.docx"
    with zipfile.ZipFile(office, "w") as z:
        z.writestr("word/document.xml", "<w:t>api_" + "key=" + value + "</w:t>")
        z.writestr("word/media/image1.png", png_bytes())
    out = tmp_path / "out.docx"
    assert putcopy.scrub(office, out) == 1
    with zipfile.ZipFile(out) as z:
        assert value not in z.read("word/document.xml").decode("utf-8")
        assert z.read("word/media/image1.png") == png_bytes()


def post_write(path: Path) -> dict:
    return {"hook_event_name": "PostToolUse", "tool_name": "Write", "tool_input": {"file_path": str(path)}}


def test_t7_a_the_write_hook_never_rewrites_a_pdf(project, monkeypatch, capsys):  # noqa: F811
    f = ascii_pdf(project / "deliverables" / "calllog.pdf")
    before = f.read_bytes()
    run_hook("post-write", post_write(f), monkeypatch, capsys)
    assert f.read_bytes() == before


# --------------------------------------------------------------------------- B: code is no password


CODE_LINES = (
    'pw = getpass.getpass("password: ")',
    'password = getpass.getpass("password: ")',
    'password = getpass.getpass()',
    'password = input("Password: ")',
    'password = os.environ["X"]',
    'login(user, password=args.password)',
    'password=password',
    'print("password: ")',
    'prompt = "password: "',
    'line = "password=%s" % value',
    'url = f"password={password}"',
)


def real_password() -> str:
    return "Winter" + str(2000 + 26) + "!x"


def test_t7_b_code_lines_keep_their_text_in_the_lab_shortening_and_the_put_scrub():
    for line in CODE_LINES:
        assert labsecrets.shorten(line + "\n")[0] == line + "\n", line
        assert putcopy.scrub_text(line + "\n") == (line + "\n", 0), line


def test_t7_b_a_real_password_is_still_masked():
    value = real_password()
    word = "pass" + "word"
    for line in (word + ": " + value, "the " + word + ": " + value, word + ' = "' + value + '"'):
        out, counts = labsecrets.shorten(line + "\n")
        assert value not in out and "[secret]" in out and counts["secret"] == 1, line
        new, n = putcopy.scrub_text(line + "\n")
        assert value not in new and n >= 1, line


def test_t7_b_the_write_hook_leaves_a_new_python_script_as_written(project, monkeypatch, capsys):  # noqa: F811
    f = project / "evidence" / "login.py"
    f.parent.mkdir(parents=True, exist_ok=True)
    script = "import getpass, os\n\n" + "\n".join(CODE_LINES) + "\n"
    f.write_text(script, encoding="utf-8")
    code, out, err = run_hook("post-write", post_write(f), monkeypatch, capsys)
    assert code == 0, err
    assert f.read_text(encoding="utf-8") == script


# --------------------------------------------------------------------------- C and G: ids are no opaque block


def ids(n: int) -> list[str]:
    return [hashlib.md5(str(i).encode()).hexdigest() for i in range(n)]


def scan(f: Path) -> Counter:
    return Counter(h["cls"] for h in check._check_file(f, check.LocalCheck(Matcher([])), code=True))


def test_t7_c_a_lab_log_of_2000_hex_ids_is_not_opaque(tmp_path):
    f = tmp_path / "run.log"
    f.write_text("".join("2026-10-28 GET /v3/projects 200 id %s\n" % h for h in ids(2000)), encoding="utf-8")
    assert scan(f) == Counter()


def test_t7_c_more_than_1000_base64_blocks_that_hide_text_stay_opaque(tmp_path):
    f = tmp_path / "payload.log"
    blocks = [base64.b64encode(("hidden text number %d of the payload" % i).encode()).decode()
              for i in range(1100)]
    f.write_text("".join("data %s\n" % b for b in blocks), encoding="utf-8")
    assert scan(f)["opaque"] == 1


def test_t7_g_request_ids_of_every_shape_are_no_finding(tmp_path):
    lines = []
    for i, h in enumerate(ids(300)):
        sha = hashlib.sha1(h.encode()).hexdigest()
        lines.append("x-obs-request-id: %s" % h.upper())
        lines.append('"request_id": "%s", "X-Request-Id": "%s"' % (sha, h[:16]))
        lines.append("trace %s" % uuid.UUID(h))
    f = tmp_path / "calls.log"
    f.write_text("\n".join(lines) + "\n", encoding="utf-8")
    assert scan(f) == Counter()


def test_t7_g_a_name_hidden_in_hex_is_still_found(tmp_path, register_path):
    f = tmp_path / "hidden.log"
    f.write_text("blob %s\n" % ("Notes for " + fx.CUSTOMER_FORMS[0]).encode().hex(), encoding="utf-8")
    hits = check.check_file(f, register_path)
    assert any(h["cls"] == "name" for h in hits)


# --------------------------------------------------------------------------- D: an API path is no file path


def test_t7_d_the_api_path_of_a_cloud_call_passes_and_the_root_stays_refused(world):  # noqa: F811
    assert bash(world, "awb cloud call GET obs / --tenant test-10491") is None
    assert bash(world, "awb cloud call --tenant test-10491 GET iam /v3/projects") is None
    assert bash(world, "awb lab run vpc --param path=/v1/vpcs") is None
    assert bash(world, "cat /") is not None
    assert bash(world, "awb cloud call PUT obs /b --body / --tenant test-10491") is not None, "--body is a file"
    assert bash(world, "awb cloud call GET obs %s --tenant test-10491" % world["other"]) is not None


# --------------------------------------------------------------------------- E: a quoted answer is data


ANSWER = """# Call log

The list answer of the run:

```json
{
  "total": 3,
  "projects": [{"id": "p1"}, {"id": "p2"}, {"id": "p3"}]
}
```

GET /v3/projects answered {"total": 3, "links": {}} for the domain.

The plan needs 24 vCPU in total.
"""


def test_t7_e_total_in_a_quoted_raw_answer_is_no_computed_claim(tmp_path):
    claims = review.find_claims(ANSWER)
    by_text = {c.sentence: c for c in claims}
    data = [c for c in claims if c.data]
    assert data and all(c.risk != "high" for c in data if c.kind == "number")
    inline = next(c for s, c in by_text.items() if s.startswith("GET /v3/projects"))
    plan = next(c for s, c in by_text.items() if s.startswith("The plan"))
    assert plan.risk == "high", "a total in prose stays a computed claim"
    project_dir = tmp_path / "p"
    (project_dir / "evidence").mkdir(parents=True)
    (project_dir / "evidence" / "call.log").write_text("GET /v3/projects 200\n", encoding="utf-8")
    deliverable = project_dir / "deliverables" / "log.md"
    for c in [inline] + data:
        row = dataclasses.replace(c, evidence="evidence/call.log", grade="live", verdict="supported")
        problems = review._claim_problems(c, row, project_dir, deliverable, 1)
        assert not any("computed number" in p for p in problems), (c.sentence, problems)
    row = dataclasses.replace(plan, evidence="evidence/call.log", grade="live", verdict="supported")
    assert any("computed number" in p for p in review._claim_problems(plan, row, project_dir, deliverable, 1))


# --------------------------------------------------------------------------- F: a PDF is read through pdftotext


TEXT_PAGE = "The call log of the lab run for %s covers the block storage of the region." % fx.CUSTOMER_FORMS[0]


@pytest.fixture
def direct(monkeypatch):
    monkeypatch.setattr(pdf, "runner_for", lambda p: pdf.Runner())


def test_t7_f_the_work_user_runs_pdftotext_direct(tmp_path, monkeypatch):
    conf = tmp_path / "paths.conf"
    conf.write_text("extract_user = awb-extract\n", encoding="utf-8")
    monkeypatch.setenv("AWB_CONF", str(conf))
    monkeypatch.setattr(config, "HOST_CONF", str(conf))      # the work user reads the host file only
    (tmp_path / "x.pdf").write_bytes(b"%PDF-1.4\n")
    monkeypatch.setattr(config, "is_work_user", lambda: True)
    assert type(pdf.runner_for(tmp_path / "x.pdf")) is pdf.Runner
    monkeypatch.setattr(config, "is_work_user", lambda: False)
    assert type(pdf.runner_for(tmp_path / "x.pdf")) is pdf.UnitRunner


def test_t7_f_awb_check_reads_the_text_of_a_pdf(tmp_path, register_path, direct):
    named = binary_pdf(tmp_path / "named.pdf", TEXT_PAGE)
    hits = check.check_file(named, register_path, code=True)
    assert "opaque" not in {h["cls"] for h in hits} and any(h["cls"] == "name" for h in hits)
    plain = binary_pdf(tmp_path / "plain.pdf")
    assert check.check_file(plain, register_path, code=True) == []


def test_t7_f_the_gate_reads_the_text_of_a_pdf_and_a_pdf_without_text_stays_opaque(tmp_path, register_path,
                                                                                   direct):
    plain = binary_pdf(tmp_path / "plain.pdf")
    assert gate.scan_files([plain], register_path) == []
    named = binary_pdf(tmp_path / "named.pdf", TEXT_PAGE)
    assert {f.cls for f in gate.scan_files([named], register_path)} == {"name"}
    empty = tmp_path / "empty.pdf"
    empty.write_bytes(b"%PDF-1.4\n1 0 obj << >> endobj\n")
    assert gate.scan_files([empty], register_path) == [gate.Finding(str(empty), 0, "opaque")]
