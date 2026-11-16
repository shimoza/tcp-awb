"""fix/pdf-tables: `awb pdf FILE.md` draws a Markdown pipe table as a ruled grid, not as Courier text.

Read back with the poppler tools: `pdftotext -layout` gives the rows, `pdftotext -bbox` the place of every word, so
a long cell is seen to wrap inside its own column; the raw page streams show the drawn rules and no wrap arrow.
"""
from __future__ import annotations

import re
import subprocess
import textwrap
from pathlib import Path

import pytest

from awb import pdf

LONG_CELL = ("The flavor runs the database of tcp-q7m4 with enough memory for the nightly reports and the backup "
             "window of the project, so this text is far wider than its column")

TABLE = textwrap.dedent("""\
    # Sizing for CUST-Q7M4

    | Service | Purpose | Hours | Size |
    |:--|---|--:|:--:|
    | ECS | {long} | 730 | 2 |
    | RDS | Größe der Datenbank, Übergabe an der Straße | 24 | 1 |
    """).format(long=LONG_CELL)


def poppler(path: Path, *args: str) -> str:
    return subprocess.run(["pdftotext", *args, str(path), "-"], capture_output=True, text=True, check=True).stdout


def pages_of(path: Path) -> int:
    info = subprocess.run(["pdfinfo", str(path)], capture_output=True, text=True, check=True).stdout
    return int(re.search(r"^Pages:\s+(\d+)$", info, re.M).group(1))


def words(path: Path) -> list[tuple[float, float, float, str]]:
    """(xMin, yMin, xMax, word) of every word on the pages."""
    out = []
    for m in re.finditer(r'<word xMin="([\d.]+)" yMin="([\d.]+)" xMax="([\d.]+)" yMax="[\d.]+">([^<]*)</word>',
                         poppler(path, "-bbox")):
        out.append((float(m.group(1)), float(m.group(2)), float(m.group(3)), m.group(4)))
    return out


@pytest.fixture
def project(tmp_path, monkeypatch):
    p = tmp_path / "tcp-q7m4"
    (p / "deliverables").mkdir(parents=True)
    (p / "SCOPE.md").write_text("- code: tcp-q7m4\n", encoding="utf-8")
    monkeypatch.chdir(p)
    monkeypatch.setattr(pdf, "SCRATCH", str(tmp_path / "scratch-%d"))
    return p


def written(project: Path, text: str) -> Path:
    src = project / "deliverables" / "sizing-q7m4.md"
    src.write_text(text, encoding="utf-8")
    assert pdf.main([str(src)]) == 0
    return src.with_suffix(".pdf")


def test_a_pipe_table_becomes_a_grid_block_with_the_alignment_of_its_separator_row():
    blocks, _ = pdf.from_markdown(TABLE)
    grid = [b for b in blocks if b.kind == "grid"]
    assert len(grid) == 1 and grid[0].align == ["l", "l", "r", "c"]
    assert grid[0].lines[0] == ["Service", "Purpose", "Hours", "Size"] and len(grid[0].lines) == 3


def test_the_cells_read_back_and_the_grid_is_ruled_with_no_wrap_arrow(project):
    out = written(project, TABLE)
    text = poppler(out, "-layout")
    flat = " ".join(text.split())
    for cell in ("Service", "Purpose", "Hours", "Size", "ECS", "RDS", "730", "24"):
        assert cell in text.split(), cell
    assert " ".join(LONG_CELL.split()[:6]) in flat
    assert "|" not in text and "---" not in text
    for arrow in ("↵", "↪", "→", "⏎"):
        assert arrow not in text
    raw = out.read_bytes()
    assert raw.count(b" re S ") >= 4 * 3                       # one ruled box per cell
    assert b" l S " not in raw                                  # the wrap arrow of a code line is never drawn
    assert b"/BaseFont /Helvetica-Bold " in raw


def test_the_header_row_is_bold(project):
    out = written(project, TABLE)
    raw = out.read_bytes()
    bold = re.findall(rb"/F2 [\d.]+ Tf [\d.]+ [\d.]+ Td \(([^)]*)\) Tj", raw)
    assert {b"Service", b"Purpose", b"Hours", b"Size"} <= set(bold)
    assert b"ECS" not in bold


def test_a_long_cell_wraps_inside_its_own_column(project):
    out = written(project, TABLE)
    placed = words(out)
    purpose = next(w for w in placed if w[3] == "Purpose")
    hours = next(w for w in placed if w[3] == "Hours")
    long_words = set(LONG_CELL.split())
    cell = [w for w in placed if w[3] in long_words and w[1] > purpose[1] + 5 and w[0] >= purpose[0] - 1]
    assert len({round(w[1]) for w in cell}) >= 2                # more than one line
    assert all(w[2] < hours[0] for w in cell)                   # never into the next column
    assert all(w[2] <= pdf.PAGE_W - pdf.MARGIN + 1 for w in placed)


def test_a_table_longer_than_a_page_repeats_its_header_on_every_page(project):
    rows = "".join("| row-%03d | tcp-q7m4 item %d |\n" % (n, n) for n in range(1, 151))
    out = written(project, "# Long table\n\n| Item | Note |\n|---|---|\n" + rows)
    total = pages_of(out)
    assert total >= 3
    seen = []
    for n in range(1, total + 1):
        page = poppler(out, "-layout", "-f", str(n), "-l", str(n)).split()
        assert "Item" in page and "Note" in page, n
        seen += [w for w in page if w.startswith("row-")]
    assert seen == ["row-%03d" % n for n in range(1, 151)]     # every row once, in order


def test_umlauts_and_sharp_s_survive_in_cells(project):
    out = written(project, TABLE)
    flat = " ".join(poppler(out, "-layout").split())
    assert "Größe der Datenbank, Übergabe an der Straße" in flat


def test_alignment_right_and_centre_move_the_text_in_its_cell(project):
    out = written(project, "| Name | Amount | Mark |\n|:--|--:|:--:|\n| a | 1 | x |\n| bbbbbbbbbbbb | 1234567 | "
                           "xxxxxxx |\n")
    placed = words(out)
    one = next(w for w in placed if w[3] == "1")
    big = next(w for w in placed if w[3] == "1234567")
    assert abs(one[2] - big[2]) < 1 and one[0] > big[0] + 5   # right: the right edges line up
    x = next(w for w in placed if w[3] == "x")
    xs = next(w for w in placed if w[3] == "xxxxxxx")
    assert abs((x[0] + x[2]) / 2 - (xs[0] + xs[2]) / 2) < 1   # centre: the middles line up
    a = next(w for w in placed if w[3] == "a")
    b = next(w for w in placed if w[3] == "bbbbbbbbbbbb")
    assert abs(a[0] - b[0]) < 1                                 # left: the left edges line up


def test_an_html_table_stays_aligned_courier_text(project):
    src = project / "deliverables" / "page.html"
    src.write_text("<h1>Page</h1><table><tr><th>Field</th><th>Value</th></tr><tr><td>Name</td><td>widget-q7m4</td>"
                   "</tr></table>", encoding="utf-8")
    assert pdf.main([str(src)]) == 0
    text = poppler(src.with_suffix(".pdf"), "-layout")
    assert "Field | Value" in text and b" re S " not in src.with_suffix(".pdf").read_bytes()
