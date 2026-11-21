"""tcp-bytg ask 1: awb pdf keeps a code line whole, so a command copied from the PDF is the command.

A code block whose longest line is wider than the page gets a smaller Courier, down to pdf.CODE_FLOOR, until that line
fits; only a line too long even at the floor wraps with the drawn arrow. Read back with pdftotext: the line comes back
as one line, as it was written.
"""
from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest

from awb import pdf, pdffont

PIPE = ('Get-ItemProperty -Path "HKLM:\\SOFTWARE\\Microsoft\\Windows NT\\CurrentVersion\\Winlogon" | '
        'Select-Object -Property DefaultUserName')
SHORT = "awb pdf deliverables/guide-q7m4.md"
HUGE = "awb calc " + " + ".join("%d.25" % n for n in range(1, 60))


def text_of(path: Path, *args: str) -> str:
    return subprocess.run(["pdftotext", *args, str(path), "-"], capture_output=True, text=True, check=True).stdout


def code_sizes(raw: bytes) -> list[float]:
    return [float(x) for x in re.findall(rb"/F4 ([0-9.]+) Tf", raw)]


@pytest.fixture
def project(tmp_path, monkeypatch):
    p = tmp_path / "tcp-q7m4"
    (p / "deliverables").mkdir(parents=True)
    (p / "SCOPE.md").write_text("- code: tcp-q7m4\n", encoding="utf-8")
    monkeypatch.chdir(p)
    monkeypatch.setattr(pdf, "SCRATCH", str(tmp_path / "scratch-%d"))
    return p


def convert(project: Path, body: str) -> Path:
    src = project / "deliverables" / "guide-q7m4.md"
    src.write_text("# Guide for CUST-Q7M4\n\n" + body, encoding="utf-8")
    assert pdf.main([str(src)]) == 0
    return src.with_suffix(".pdf")


def test_a_long_powershell_line_copies_out_as_one_line_in_a_smaller_font(project):
    assert pdf._width(PIPE, "c", 9) > pdf.TEXT_W                   # too wide at the code size of 9 pt
    out = convert(project, "```powershell\n%s\n%s\n```\n" % (SHORT, PIPE))
    lines = text_of(out).splitlines()
    assert PIPE in lines and SHORT in lines
    raw = out.read_bytes()
    assert b" l S " not in raw                                      # no wrap arrow
    sizes = set(code_sizes(raw))
    assert len(sizes) == 1 and pdf.CODE_FLOOR <= sizes.pop() < 9   # the whole block in one smaller size


def test_a_block_of_short_lines_keeps_the_code_size(project):
    out = convert(project, "```\n%s\n```\n" % SHORT)
    assert code_sizes(out.read_bytes()) == [9.0]


def test_only_a_line_too_long_at_the_floor_wraps_and_the_block_takes_the_size_the_others_need(project):
    assert pdf._width(HUGE, "c", pdf.CODE_FLOOR) > pdf.TEXT_W
    out = convert(project, "```\n%s\n%s\n```\n" % (PIPE, HUGE))
    lines = text_of(out).splitlines()
    assert PIPE in lines and HUGE not in lines
    # wrapped, never cut: the pieces joined are the line (plain pdftotext drops the space a piece starts with)
    bare = HUGE.replace(" ", "")
    start = next(i for i, x in enumerate(lines) if x.startswith("awb calc"))
    pieces = [lines[start].replace(" ", "")]
    while len("".join(pieces)) < len(bare):
        pieces.append(lines[start + len(pieces)].replace(" ", ""))
    assert len(pieces) > 1 and "".join(pieces) == bare
    raw = out.read_bytes()
    assert b" l S " in raw                                           # the arrow at the break
    size = pdf._fixed_size([pdf._width(PIPE, "c", 9)], 9)
    assert set(code_sizes(raw)) == {size} and size > pdf.CODE_FLOOR


def test_the_size_rule_fits_the_longest_line_and_stops_at_the_floor():
    assert pdf._fixed_size([100.0, pdf.TEXT_W], 9) == 9
    size = pdf._fixed_size([pdf.TEXT_W * 1.2], 9)
    assert size == int(9 / 1.2 * 10) / 10 and pdf.TEXT_W * 1.2 * size / 9 <= pdf.TEXT_W
    assert pdf._fixed_size([pdf.TEXT_W * 9 / pdf.CODE_FLOOR], 9) == pdf.CODE_FLOOR
    assert pdf._fixed_size([pdf.TEXT_W * 2], 9) == 9                     # beyond the floor: no size saves it
    assert pdf._fixed_size([], 9) == 9


@pytest.mark.skipif(pdffont.find() is None, reason="no Liberation or DejaVu font on this host")
def test_a_long_code_line_with_cyrillic_stays_one_line_in_the_embedded_font(project):
    line = PIPE + " # ключ"
    out = convert(project, "```\n%s\n```\n" % line)
    assert line in text_of(out).splitlines()
    raw = out.read_bytes()
    assert b" l S " not in raw and b"/FontFile2" in raw
