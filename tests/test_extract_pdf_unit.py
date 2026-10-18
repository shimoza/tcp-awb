"""The pdf tools in a transient unit of a throwaway user (task n18-extract-sandbox).

A fake stands in for `sudo -n systemd-run`: it records the argument list and the standard input and gives canned
output. No unit is ever started here."""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
from reportlab.lib.pagesizes import A4
from reportlab.pdfgen import canvas

from awb import intake_counts, report
from awb.extract import pdf

USER = "extract-test"
LINE = "the first page carries enough plain words to count as text"


@pytest.fixture
def pdf_file(tmp_path) -> Path:
    path = tmp_path / "doc.pdf"
    c = canvas.Canvas(str(path), pagesize=A4)
    c.drawString(72, 700, LINE)
    c.save()
    return path


@pytest.fixture
def conf(tmp_path, monkeypatch):
    def write(text: str) -> None:
        f = tmp_path / "paths.conf"
        f.write_text(text, encoding="utf-8")
        monkeypatch.setenv("AWB_CONF", str(f))
    return write


class FakeUnits:
    def __init__(self, fail: str | None = None):
        self.calls: list[tuple[list[str], bytes]] = []
        self.fail = fail

    def __call__(self, argv, input=None, **kw):
        self.calls.append((list(argv), input))
        tool = Path(argv[argv.index("--") + 1]).name
        if self.fail == "start":
            raise FileNotFoundError("sudo")
        if self.fail == "timeout":
            raise subprocess.TimeoutExpired(argv, 1)
        if self.fail == tool:
            return subprocess.CompletedProcess(argv, 1, b"", b"")
        if tool == "pdfinfo":
            out = b"Pages: 1\nEncrypted: no\n"
        else:
            out = LINE.encode() + b"\n"
        return subprocess.CompletedProcess(argv, 0, out, b"")


def _direct_forbidden(monkeypatch):
    def refuse(args):
        raise AssertionError("a direct call after a unit was chosen: %r" % (args,))
    monkeypatch.setattr(pdf, "_run", refuse)


def test_the_unit_argv_is_the_fixed_form_with_the_user_of_paths_conf(pdf_file, conf, monkeypatch):
    conf("extract_user = %s\n" % USER)
    fake = FakeUnits()
    monkeypatch.setattr(pdf.subprocess, "run", fake)
    _direct_forbidden(monkeypatch)
    ex = pdf.extract(pdf_file)
    assert ex.state == "ok" and ex.meta["extractor"] == "unit"
    head = ["sudo", "-n", "systemd-run", "--quiet", "--wait", "--pipe", "--collect",
            "--uid=" + USER, "--gid=" + USER,
            "--property=PrivateNetwork=yes", "--property=ProtectSystem=strict", "--property=ProtectHome=yes",
            "--property=PrivateTmp=yes", "--property=NoNewPrivileges=yes", "--property=MemoryMax=512M",
            "--property=RuntimeMaxSec=120", "--"]
    assert fake.calls
    for argv, _ in fake.calls:
        assert argv[:len(head)] == head
        tool, options = Path(argv[len(head)]).name, argv[len(head) + 1:]
        assert tool in ("pdftotext", "pdfinfo")
        if tool == "pdftotext":
            assert options[-2:] == ["-", "-"]
        else:
            assert options[-1] == "-" and "-" not in options[:-1]
    tools = {Path(a[len(head)]).name for a, _ in fake.calls}
    assert tools == {"pdftotext", "pdfinfo"}


def test_the_bytes_go_on_standard_input_and_the_path_is_in_no_argument(pdf_file, conf, monkeypatch):
    conf("extract_user = %s\n" % USER)
    fake = FakeUnits()
    monkeypatch.setattr(pdf.subprocess, "run", fake)
    pdf.extract(pdf_file)
    data = pdf_file.read_bytes()
    assert fake.calls
    for argv, stdin in fake.calls:
        assert stdin == data
        assert not [a for a in argv if str(pdf_file) in a or pdf_file.name in a or str(pdf_file.parent) in a]


@pytest.mark.parametrize("fail", ["pdfinfo", "pdftotext", "start", "timeout"])
def test_a_failed_unit_is_a_failed_file_and_never_a_direct_call(pdf_file, conf, monkeypatch, fail):
    conf("extract_user = %s\n" % USER)
    monkeypatch.setattr(pdf.subprocess, "run", FakeUnits(fail=fail))
    _direct_forbidden(monkeypatch)
    ex = pdf.extract(pdf_file)
    assert ex.state == "failed"
    assert ex.meta["extractor"] == "unit"
    assert ex.text == ""


def _run_record(ex) -> report.Run:
    rec = report.Run(date="2026-10-18", time="10:00:00", customer="CUST-Q7M4", new_customer=False,
                     register_forms=0)
    fr = report.FileRecord(file_id="F-AAAA", original_name="doc.pdf", kind=ex.kind, state=ex.state)
    fr.parts.append(report.Part(part_id="F-AAAA", label="doc.pdf", kind=ex.kind, state=ex.state,
                                meta=dict(ex.meta)))
    rec.files.append(fr)
    return rec


def test_the_report_and_the_counts_say_extractor_unit_after_a_failed_unit(pdf_file, conf, monkeypatch, tmp_path):
    conf("extract_user = %s\n" % USER)
    monkeypatch.setattr(pdf.subprocess, "run", FakeUnits(fail="pdfinfo"))
    ex = pdf.extract(pdf_file)
    rec = _run_record(ex)
    assert ex.state == "failed" and "- extractor: unit" in report.render_public(rec).splitlines()
    assert "- extractor: unit" in report.render_minimal(rec).splitlines()
    assert report.extractor_word(rec) == "unit"
    vault = tmp_path / "vault"
    vault.mkdir()
    paths = type("P", (), {"vault": vault})()
    intake_counts.record_intake(paths, "CUST-Q7M4", 0, extractor=report.extractor_word(rec))
    assert intake_counts.read_counts(paths)["CUST-Q7M4"]["extractor"] == "unit"


def test_without_extract_user_the_direct_path_is_taken(pdf_file, conf, monkeypatch):
    conf("shared = /nowhere\n")
    calls = []
    real = pdf._run

    def spy(args):
        calls.append(args)
        return real(args)

    monkeypatch.setattr(pdf, "_run", spy)
    ex = pdf.extract(pdf_file)
    assert ex.state == "ok" and ex.meta["extractor"] == "direct"
    assert LINE in ex.text and calls
    assert all(str(pdf_file.resolve()) in a for a in calls)
    assert "- extractor: direct" in report.render_public(_run_record(ex)).splitlines()


def test_a_bad_extract_user_fails_the_file_and_runs_nothing(pdf_file, conf, monkeypatch):
    conf("extract_user = bad user;x\n")
    fake = FakeUnits()
    monkeypatch.setattr(pdf.subprocess, "run", fake)
    _direct_forbidden(monkeypatch)
    ex = pdf.extract(pdf_file)
    assert ex.state == "failed" and not fake.calls
