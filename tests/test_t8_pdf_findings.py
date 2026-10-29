"""T8: false findings of `awb check` on a PDF of our own call log, read through pdftotext since T7.

A web address of a TCP endpoint cut by the line wrap of the PDF was reported by its cut head, and digits of
separate table cells (`018 | 018 | 3`, or the same digits apart by the gap of two columns) read as a phone number.
A real phone number in a sentence and a foreign web address are still found.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from awb import check, gate, patterns
from awb.extract import pdf
from tests import fixtures as fx

FOREIGN_HOST = "files." + fx.CUSTOMER_DOMAIN.split(".")[0] + ".de"


def found(text: str) -> list[tuple[str, str]]:
    return [(text[s.start:s.end], s.cls) for s in patterns.find_structured(text)]


def call_log_pdf(path: Path, url_lines: tuple[str, str] = ("GET https://obs.eu-de.otc.t-systems.c",
                                                           "om/lab-bucket/run.log 200")) -> Path:
    """A call log as our lab runs print it: a web address wrapped at the line end and a table with numbers."""
    from reportlab.pdfgen import canvas

    c = canvas.Canvas(str(path))
    c.drawString(72, 750, "Call log of the lab run, page 1")
    c.drawString(72, 730, url_lines[0])
    c.drawString(72, 716, url_lines[1])
    c.drawString(72, 690, "| 018 | 018 | 3 |")
    for x, cell in ((72, "volumes"), (200, "018"), (330, "018"), (460, "3")):
        c.drawString(x, 670, cell)
    c.save()
    return path


@pytest.fixture
def direct(monkeypatch):
    monkeypatch.setattr(pdf, "runner_for", lambda p: pdf.Runner())


# --------------------------------------------------------------------------- the PDF as awb check reads it


def test_t8_awb_check_of_the_call_log_pdf_has_no_finding(tmp_path, register_path, direct):
    log = call_log_pdf(tmp_path / "calllog.pdf")
    assert check.check_file(log, register_path, code=True) == []
    assert gate.scan_files([log], register_path) == []


def test_t8_a_foreign_web_address_cut_in_a_pdf_is_still_found(tmp_path, register_path, direct):
    log = call_log_pdf(tmp_path / "foreign.pdf", ("GET https://" + FOREIGN_HOST[:-1], FOREIGN_HOST[-1:] + "/x 200"))
    assert "url" in {h["cls"] for h in check.check_file(log, register_path, code=True)}


# --------------------------------------------------------------------------- 1: a TCP host cut by the wrap


@pytest.mark.parametrize("text", [
    "GET http://obs.otc.t-systems.c\nom/bucket/a.txt 200",
    "GET https://ecs.eu-de.otc.t-\nsystems.com/v1/servers 200",
    "GET https://ecs.eu-de.otc.t-\n      systems.com/v1/servers 200",
    "host obs.eu-de.otc.t-systems.c\nom/x",
])
def test_t8_a_tcp_host_cut_at_the_line_end_is_no_finding(text):
    assert found(text) == []


def test_t8_a_foreign_web_address_is_still_found_whole_or_cut():
    assert [c for _, c in found("GET https://%s/x 200" % FOREIGN_HOST)] == ["url"]
    cut = "GET https://%s\n%s/x 200" % (FOREIGN_HOST[:-1], FOREIGN_HOST[-1:])
    assert "url" in {c for _, c in found(cut)}


def test_t8_a_whole_tcp_host_before_a_line_break_is_not_joined_to_the_next_word():
    text = "see obs.eu-de.otc.t-systems.com\nfiles.%s for the rest" % FOREIGN_HOST.split(".", 1)[1]
    assert [c for _, c in found(text)] == ["url"]


# --------------------------------------------------------------------------- 2: digits of separate cells


@pytest.mark.parametrize("text", [
    "| volumes | 018 | 018 | 3 |",
    "volumes   018   018   3",
    "volumes\t018\t018\t3",
    "next\t0180\t2026\t17",
    "| next | 0180 | 018 |",
])
def test_t8_digits_of_separate_cells_are_no_phone(text):
    assert found(text) == []


def test_t8_a_real_phone_in_a_sentence_is_still_found():
    for text in ("please call 030 5551234 today", "please call +49 30 1234567 today",
                 "call +49 30 - 5551234 today", "call 0171 1234567 today"):
        assert [c for _, c in found(text)] == ["phone"], text


def test_t8_a_phone_over_two_cells_is_still_found_inside_a_longer_row():
    assert found("| Tel | 030 | 5551234 | 7 |") == [("030 | 5551234", "phone")]
    assert found("| 030 | 5551234 |") == [("030 | 5551234", "phone")]
