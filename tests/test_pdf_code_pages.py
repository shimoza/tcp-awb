"""tcp-bytg ask 2 (2026-11-22): awb pdf keeps a code block on one page when it fits a page, so the page number line
('21 / 32') no longer lands inside a block copied from the PDF. A block that does not fit where it starts moves to
the next page; a block longer than a page breaks between its lines and never inside a wrapped line. Read back with
pdftotext page by page.
"""
from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest

from awb import pdf

PARA = "A paragraph of the guide for CUST-Q7M4 that fills the page before the code block comes."


def page_texts(path: Path) -> list[str]:
    info = subprocess.run(["pdfinfo", str(path)], capture_output=True, text=True, check=True).stdout
    n = int(re.search(r"^Pages:\s+(\d+)$", info, re.M).group(1))
    return [subprocess.run(["pdftotext", "-f", str(k), "-l", str(k), str(path), "-"], capture_output=True, text=True,
                           check=True).stdout for k in range(1, n + 1)]


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


def code(lines: list[str]) -> str:
    return "```powershell\n" + "\n".join(lines) + "\n```\n"


def filler(count: int) -> str:
    return "\n\n".join([PARA] * count)


def paragraphs_leaving(lines: int) -> int:
    """The number of paragraphs after the title that leave room for fewer than `lines` code lines, but some, at the
    bottom of the first page."""
    lead = pdf.STYLES["code"][1]
    for count in range(1, 200):
        blocks, _ = pdf.from_markdown("# Guide for CUST-Q7M4\n\n" + filler(count) + "\n")
        pages = pdf.layout(blocks)
        room = (pages.y - pdf.MARGIN) / lead - 1
        if len(pages.ops) == 1 and 3 <= room < lines:
            return count
    raise AssertionError("no filler found")


def test_bytg_a_code_block_that_fits_a_page_moves_whole_to_the_next_page(project):
    block = ["Write-Output 'step %02d of the here-string'" % n for n in range(1, 31)]
    out = convert(project, filler(paragraphs_leaving(len(block))) + "\n\n" + code(block) + "\nThe end.\n")
    pages = page_texts(out)
    holding = [k for k, text in enumerate(pages) if any(line in text for line in block)]
    assert holding == [1], holding                            # the whole block on the second page
    assert all(line in pages[1].splitlines() for line in block)


def test_bytg_a_code_block_longer_than_a_page_breaks_between_its_lines(project):
    tail = "abcdefghij" * 23                                   # 230 characters without a space: three pieces each
    block = ["L%03d %s" % (n, tail) for n in range(1, 41)]
    out = convert(project, filler(paragraphs_leaving(10)) + "\n\n" + code(block))
    pages = page_texts(out)
    assert len(pages) >= 3
    for k, text in enumerate(pages, start=1):
        lines = [line.strip() for line in text.splitlines() if line.strip()]
        assert re.fullmatch(r"%d\s*/\s*%d" % (k, len(pages)), lines[-1]), lines[-3:]
        pieces = [line for line in lines if re.fullmatch(r"(?:L\d{3} )?[a-j]+", line)]
        assert len(pieces) % 3 == 0, (k, len(pieces))          # only whole wrapped lines on a page
        assert not pieces or pieces[0].startswith("L"), k
    flat = "".join("".join(t.split()) for t in pages)
    assert all("".join(line.split()) in flat for line in block)


def test_bytg_a_short_code_block_in_room_stays_where_it_is(project):
    out = convert(project, PARA + "\n\n" + code(["awb pdf deliverables/guide-q7m4.md"]) + "\n" + PARA + "\n")
    pages = page_texts(out)
    assert len(pages) == 1 and "awb pdf deliverables/guide-q7m4.md" in pages[0]
