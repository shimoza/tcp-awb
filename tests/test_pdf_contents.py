"""tcp-bytg ask 15: awb pdf writes a contents table with page numbers and starts a new page for a level-1 heading.

A line `<!-- toc -->` puts the contents there: each h1 and h2 after it with a dot leader and its page, found in two
passes. `--h1-page` starts a new page before each h1 that is not at the top of a page already. Read back with
pdftotext page by page: each number in the contents is the page its heading is on.
"""
from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest

from awb import pdf

FILLER = " ".join(["The agent of CUST-Q7M4 reads its settings from the registry and writes one log line per run."] * 6)
ENTRY = re.compile(r"^(?P<title>\S.*?)\s*\.{3,}\s*(?P<page>\d+)$")


def pages_of(path: Path) -> list[list[str]]:
    text = subprocess.run(["pdftotext", str(path), "-"], capture_output=True, text=True, check=True).stdout
    return [[" ".join(x.split()) for x in page.splitlines() if x.strip()] for page in text.split("\f")[:-1]]


def contents(pages: list[list[str]]) -> dict[str, int]:
    out = {}
    for page in pages:
        for line in page:
            m = ENTRY.match(line)
            if m:
                out[m.group("title")] = int(m.group("page"))
    return out


def page_of(pages: list[list[str]], heading: str, after: int = 0) -> int:
    """The page (from 1) where the heading stands as a line of its own, outside the contents table."""
    return next(n for n, page in enumerate(pages, 1) if n > after and heading in page)


def guide(sections: int = 3, subs: int = 2, paras: int = 3) -> str:
    out = ["# Guide for CUST-Q7M4", "", "Intro of the guide tcp-q7m4.", "", "## Contents", "", "<!-- toc -->", ""]
    for s in range(1, sections + 1):
        out += ["# Part %d of the setup" % s, "", FILLER, ""]
        for t in range(1, subs + 1):
            out += ["## Step %d.%d of the setup" % (s, t), ""] + [FILLER, ""] * paras
    return "\n".join(out) + "\n"


@pytest.fixture
def project(tmp_path, monkeypatch):
    p = tmp_path / "tcp-q7m4"
    (p / "deliverables").mkdir(parents=True)
    (p / "SCOPE.md").write_text("- code: tcp-q7m4\n", encoding="utf-8")
    monkeypatch.chdir(p)
    monkeypatch.setattr(pdf, "SCRATCH", str(tmp_path / "scratch-%d"))
    return p


def convert(project: Path, text: str, *args: str, name: str = "guide-q7m4.md") -> Path:
    src = project / "deliverables" / name
    src.write_text(text, encoding="utf-8")
    assert pdf.main([str(src), *args]) == 0
    return src.with_suffix(".pdf")


def test_the_contents_numbers_are_the_pages_of_the_headings_and_each_part_starts_a_page(project):
    pages = pages_of(convert(project, guide(), "--h1-page"))
    toc = contents(pages)
    titles = ["Part %d of the setup" % s for s in (1, 2, 3)] + [
        "Step %d.%d of the setup" % (s, t) for s in (1, 2, 3) for t in (1, 2)]
    assert sorted(toc) == sorted(titles)                   # h1 and h2 after the marker; the title before it is left
    for title in titles:
        assert toc[title] == page_of(pages, title, after=1), title
    for s in (1, 2, 3):
        part = toc["Part %d of the setup" % s]
        assert pages[part - 1][0] == "Part %d of the setup" % s      # the first line of its page
    assert "Guide for CUST-Q7M4" in pages[0] and "Contents" in pages[0]


def test_without_the_option_the_parts_run_on(project):
    pages = pages_of(convert(project, guide(sections=2, subs=1, paras=0)))
    assert len(pages) == 1 and contents(pages) == {"Part 1 of the setup": 1, "Step 1.1 of the setup": 1,
                                                   "Part 2 of the setup": 1, "Step 2.1 of the setup": 1}


def test_a_contents_table_longer_than_a_page_still_carries_the_right_numbers(project):
    pages = pages_of(convert(project, guide(sections=12, subs=5, paras=1), "--h1-page"))
    toc = contents(pages)
    assert len(toc) == 72
    first_part = toc["Part 1 of the setup"]
    assert first_part > 2                                   # the contents ran over its first page
    for title, page in toc.items():
        assert page == page_of(pages, title, after=first_part - 1), title


def test_an_h1_at_the_top_of_a_page_gets_no_empty_page_before_it(project):
    text = "\n".join("# Part %d of the setup\n\nShort text %d.\n" % (n, n) for n in (1, 2, 3))
    pages = pages_of(convert(project, text, "--h1-page"))
    assert [p[0] for p in pages] == ["Part 1 of the setup", "Part 2 of the setup", "Part 3 of the setup"]


def test_the_marker_in_html_and_in_a_code_fence(project):
    html = ("<html><head><title>Guide tcp-q7m4</title></head><body><h1>Guide tcp-q7m4</h1><!-- toc -->"
            "<h1>Part one</h1><p>Text.</p><h2>Step one</h2><p>Text.</p><h1>Part two</h1><p>Text.</p></body></html>")
    pages = pages_of(convert(project, html, "--h1-page", name="guide-q7m4.html"))
    assert contents(pages) == {"Part one": 2, "Step one": 2, "Part two": 3}
    fenced = "# Guide\n\n```\n<!-- toc -->\n```\n\n## Step one\n\nText.\n"
    pages = pages_of(convert(project, fenced, name="fenced-q7m4.md"))
    assert "<!-- toc -->" in pages[0] and contents(pages) == {}


def test_the_layout_takes_the_page_map_of_its_last_pass(monkeypatch):
    blocks, _ = pdf.from_markdown(guide())
    assert [b.kind for b in blocks].count("toc") == 1
    passes = []
    real = pdf._layout

    def counted(*args):
        passes.append(args[2])
        return real(*args)

    monkeypatch.setattr(pdf, "_layout", counted)
    pages = pdf.layout(blocks, h1_page=True)
    assert len(passes) == 2 and passes[0] is None and passes[1] == pages.heads
    moving = iter(range(10))
    monkeypatch.setattr(pdf, "_layout", lambda b, h, heads: type("P", (), {"heads": {0: next(moving)}})())
    with pytest.raises(pdf.PdfError, match="do not settle"):
        pdf.layout(blocks)
