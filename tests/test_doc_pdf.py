"""awb doc fetch reads a PDF (task DF-PDF, 2026-11-07): pdftotext of the poppler tools on a private temporary file.
A fake getter, never the network; small PDFs written here with reportlab; the real pdftotext of the host."""
from __future__ import annotations

import io
import os
import subprocess
from pathlib import Path

import pytest

from awb.tcp import doc
from tests.test_doc import HOSTS, Getter

needs_pdftotext = pytest.mark.skipif(not os.path.exists(doc.PDFTOTEXT), reason="pdftotext of poppler is missing")


def make_pdf(pages: list[str], *, encrypt: str | None = None) -> bytes:
    from reportlab.lib.pagesizes import A4
    from reportlab.pdfgen import canvas

    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=A4, encrypt=encrypt)
    for text in pages:
        y = 800
        for line in text.splitlines():
            c.drawString(60, y, line)
            y -= 16
        c.showPage()
    c.save()
    return buf.getvalue()


def scan_pdf() -> bytes:
    """A page with a drawing and no text: what a scan without a text layer reads as."""
    from reportlab.lib.pagesizes import A4
    from reportlab.pdfgen import canvas

    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=A4)
    c.rect(50, 50, 400, 600, fill=1)
    c.showPage()
    c.save()
    return buf.getvalue()


PAGE_ONE = "Leistungsbeschreibung T Cloud Public\nReserved und Reserved Upfront mit Mindestabnahme."
PAGE_TWO = "Elastic Cloud Server Flavors\nDie Abrechnung erfolgt im Sekundentakt."


def getter_for(body: bytes, ctype: str = "application/pdf", **headers) -> Getter:
    return Getter({("example.com", "/lb.pdf"): (200, dict({"content-type": ctype}, **headers), body)})


@needs_pdftotext
def test_a_pdf_answer_becomes_text_with_page_markers_and_the_header_names_pdf_and_pages():
    page = doc.fetch("https://example.com/lb.pdf", getter=getter_for(make_pdf([PAGE_ONE, PAGE_TWO])), hosts=HOSTS)
    assert page["kind"] == "pdf" and page["pages"] == 2
    text = page["text"]
    assert text.index("--- page 1 ---") < text.index("Mindestabnahme") < text.index("--- page 2 ---") \
        < text.index("Sekundentakt")
    assert doc.header("https://example.com/lb.pdf", "2026-11-07 20:00 UTC", "pdf", 2) == \
        "source: https://example.com/lb.pdf, fetched 2026-11-07 20:00 UTC, pdf, 2 pages"
    assert doc.header("https://example.com/a", "2026-11-07 20:00 UTC", "html", None) == \
        "source: https://example.com/a, fetched 2026-11-07 20:00 UTC"


@needs_pdftotext
def test_an_octet_stream_is_read_only_when_its_body_is_a_pdf():
    page = doc.fetch("https://example.com/lb.pdf", getter=getter_for(make_pdf([PAGE_ONE]), "application/octet-stream"),
                     hosts=HOSTS)
    assert page["kind"] == "pdf" and "Mindestabnahme" in page["text"]
    with pytest.raises(doc.DocError, match="not a text type"):
        doc.fetch("https://example.com/lb.pdf", getter=getter_for(b"\x89PNG\r\n", "application/octet-stream"),
                  hosts=HOSTS)
    with pytest.raises(doc.DocError, match="the body is not a PDF"):
        doc.fetch("https://example.com/lb.pdf", getter=getter_for(b"<html></html>"), hosts=HOSTS)


def test_a_pdf_has_its_own_cap_and_html_keeps_the_five_mb_cap():
    big = str(doc.MAX_PDF_BYTES + 1)
    with pytest.raises(doc.DocError, match="larger than 25 MB"):
        doc.fetch("https://example.com/lb.pdf", getter=getter_for(b"%PDF-1.7", **{"content-length": big}),
                  hosts=HOSTS)
    with pytest.raises(doc.DocError, match="larger than"):
        doc.fetch("https://example.com/lb.pdf", getter=getter_for(b"%PDF-" + b"x" * 64), hosts=HOSTS, pdf_cap=32)
    six = str(6 * 1024 * 1024)
    g = Getter({("example.com", "/p"): (200, {"content-type": "text/html", "content-length": six}, b"<p>x</p>")})
    with pytest.raises(doc.DocError, match="larger than 5 MB"):
        doc.fetch("https://example.com/p", getter=g, hosts=HOSTS)
    # a PDF under 25 MB but over 5 MB passes the cap check (the reader is a fake here)
    fake = lambda cmd, **kw: subprocess.CompletedProcess(cmd, 0, (PAGE_ONE + "\f").encode(), b"")
    page = doc.fetch("https://example.com/lb.pdf", getter=getter_for(b"%PDF-1.7" + b" " * (6 * 1024 * 1024)),
                     hosts=HOSTS, pdf_runner=fake)
    assert page["pages"] == 1


@needs_pdftotext
def test_an_encrypted_a_broken_and_a_scanned_pdf_are_refused_with_their_reason():
    with pytest.raises(doc.DocError, match="an encrypted PDF"):
        doc.fetch("https://example.com/lb.pdf", getter=getter_for(make_pdf([PAGE_ONE], encrypt="secret-pass")),
                  hosts=HOSTS)
    with pytest.raises(doc.DocError, match="a PDF the reader cannot read"):
        doc.fetch("https://example.com/lb.pdf", getter=getter_for(b"%PDF-1.7\nnot really a pdf\n"), hosts=HOSTS)
    with pytest.raises(doc.DocError, match="without a text layer"):
        doc.fetch("https://example.com/lb.pdf", getter=getter_for(scan_pdf()), hosts=HOSTS)


class Recorder:
    """A runner that records the call and where the PDF lay, then answers, times out or fails."""

    def __init__(self, mode: str = "ok"):
        self.mode = mode
        self.calls: list[tuple[list, dict]] = []
        self.folder: Path | None = None
        self.file_mode = self.folder_mode = None

    def __call__(self, cmd, **kw):
        self.calls.append((cmd, kw))
        pdf = Path(cmd[4])
        self.folder = pdf.parent
        self.file_mode, self.folder_mode = pdf.stat().st_mode & 0o777, self.folder.stat().st_mode & 0o777
        if self.mode == "timeout":
            raise subprocess.TimeoutExpired(cmd, kw.get("timeout"))
        if self.mode == "fail":
            return subprocess.CompletedProcess(cmd, 1, b"", b"Syntax Error: broken")
        return subprocess.CompletedProcess(cmd, 0, (PAGE_ONE + "\f").encode(), b"")


@pytest.mark.parametrize("mode, error", [("ok", None), ("fail", "cannot read"), ("timeout", "longer than")])
def test_the_temporary_file_and_folder_are_private_and_gone_after_success_failure_and_timeout(mode, error):
    run = Recorder(mode)
    if error:
        with pytest.raises(doc.DocError, match=error):
            doc.pdf_text(b"%PDF-1.7", runner=run)
    else:
        assert doc.pdf_text(b"%PDF-1.7", runner=run)[1] == 1
    assert run.file_mode == 0o600 and run.folder_mode == 0o700
    assert run.folder is not None and not run.folder.exists()


def test_pdftotext_gets_an_argument_list_no_shell_no_stdin_and_a_timeout():
    run = Recorder()
    doc.pdf_text(b"%PDF-1.7", runner=run)
    [(cmd, kw)] = run.calls
    assert isinstance(cmd, list) and cmd[:4] == [doc.PDFTOTEXT, "-layout", "-enc", "UTF-8"] and cmd[5] == "-"
    assert not kw.get("shell") and kw["stdin"] == subprocess.DEVNULL and kw["timeout"] == doc.PDF_TIMEOUT


def test_the_service_answer_carries_the_kind_and_the_pages_and_the_ledger_no_content(tmp_path, monkeypatch):
    fake = lambda cmd, **kw: subprocess.CompletedProcess(cmd, 0, (PAGE_ONE + "\f" + PAGE_TWO + "\f").encode(), b"")
    real = doc.fetch
    monkeypatch.setattr(doc, "fetch", lambda url, **kw: real(url, pdf_runner=fake, **kw))
    ledger = tmp_path / "log"
    ledger.mkdir()
    answer, text = doc.serve({"url": "https://example.com/lb.pdf"}, "tcp-ab2c", doc.Limiter(lambda: ledger), ledger,
                             getter=getter_for(b"%PDF-1.7"), hosts=HOSTS, tmp_dir=tmp_path)
    try:
        assert answer["ok"] and (answer["kind"], answer["pages"]) == ("pdf", 2)
        assert "--- page 2 ---" in text.read_text(encoding="utf-8")
    finally:
        text.unlink()
    logged = "".join(p.read_text(encoding="utf-8") for p in ledger.iterdir())
    assert "example.com" in logged and "Mindestabnahme" not in logged and "Sekundentakt" not in logged
