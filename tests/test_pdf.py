"""B-033: one shared PDF writer (awb/pdf.py) and `awb pdf FILE` for every project.

Read back with the poppler tools: pdfinfo counts the pages and reads title and author, pdftotext gives back every
heading, list item and code line; `pdftotext -layout -fixed 5.4` (the advance of Courier 9) gives the code lines
back with their indents, as they were written.
"""
from __future__ import annotations

import re
import subprocess
import textwrap
from pathlib import Path

import pytest

from awb import cli, pdf
from awb.tcp import dataset, howto
from tests.test_dataset import TODAY, facts, live  # noqa: F401
from tests.test_howto import make_mirror

COMMAND = "awb pdf deliverables/offer-q7m4.md --out notes/offer-q7m4.pdf"
INDENTED = "    for code in CUST-Q7M4 tcp-q7m4:   # keeps   its spaces"
TABBED = "\tprint(code)"
LONG = "awb calc " + " + ".join("%d.25" % n for n in range(1, 40))

MARKDOWN = textwrap.dedent("""\
    # Offer for CUST-Q7M4

    The project tcp-q7m4 needs a **bold** word, an *italic* one, `inline code` and a
    [price page](https://example.org/prices) in one paragraph that is long enough to wrap onto a second line of the page.

    ## Before you start

    - The first bullet
    - The second bullet
      - A nested bullet

    1. The first step
    2. The second step

    ### The commands

    ```bash
    {command}
    {indented}
    {tabbed}
    {long}
    ```

    | Service | Flavor | Hours |
    |---|---|---|
    | ECS | s3.large.2 | 730 |
    | RDS | rds.pg.x1 | 24 |

    See <https://example.org/docs> for more.
    """).format(command=COMMAND, indented=INDENTED, tabbed=TABBED, long=LONG)

HTML = textwrap.dedent("""\
    <!doctype html>
    <html lang="en"><head><meta charset="utf-8"><title>How to: create a widget</title>
    <style>body{{font:16px sans-serif}}</style></head><body>
    <h1>How to: create a widget</h1>
    <p>A paragraph with <strong>strong</strong>, <em>em</em>, <code>Create now</code> and
    <a href="https://example.org/console">the console</a>.</p>
    <h2>Before you start</h2>
    <ul><li>You are signed in.</li><li>Your account may use the service.</li></ul>
    <h3>Steps</h3>
    <ol><li><strong>Open the widget list</strong>
    <ul><li>Menu path: Compute &gt; Widgets</li></ul></li>
    <li><strong>Create the widget</strong><div>On the screen: a form</div></li></ol>
    <pre>{command}
    {indented}</pre>
    <table><tr><th>Field</th><th>Value</th></tr><tr><td>Name</td><td>widget-q7m4</td></tr></table>
    <section>Text of an element the writer does not know</section>
    <script>var hidden = 1;</script>
    </body></html>
    """).format(command=COMMAND, indented=INDENTED)


def poppler(tool: str, path: Path, *args: str) -> str:
    return subprocess.run([tool, *args, str(path), "-"] if tool == "pdftotext" else [tool, str(path)],
                          capture_output=True, text=True, check=True).stdout


def layout_lines(path: Path) -> list[str]:
    return textwrap.dedent(poppler("pdftotext", path, "-layout", "-fixed", "5.4").replace("\f", "")).splitlines()


@pytest.fixture
def project(tmp_path, monkeypatch):
    p = tmp_path / "tcp-q7m4"
    (p / "deliverables").mkdir(parents=True)
    (p / "notes").mkdir()
    (p / "SCOPE.md").write_text("- code: tcp-q7m4\n", encoding="utf-8")
    monkeypatch.chdir(p)
    monkeypatch.setattr(pdf, "SCRATCH", str(tmp_path / "scratch-%d"))
    return p


def test_a_markdown_sample_with_every_element_reads_back_with_the_poppler_tools(project, capsys):
    src = project / "deliverables" / "offer-q7m4.md"
    src.write_text(MARKDOWN, encoding="utf-8")
    assert cli.main(["pdf", str(src)]) == 0
    out = src.with_suffix(".pdf")
    assert "pdf: %s, 1 page(s)" % out in capsys.readouterr().out
    info = poppler("pdfinfo", out)
    assert re.search(r"^Pages:\s+1$", info, re.M) and re.search(r"^Page size:\s+595 x 842 pts \(A4\)$", info, re.M)
    assert re.search(r"^Title:\s+Offer for CUST-Q7M4$", info, re.M)
    assert re.search(r"^Author:\s+Architect Workbench$", info, re.M)
    text = poppler("pdftotext", out)
    lines = [" ".join(x.split()) for x in text.splitlines()]
    for heading in ("Offer for CUST-Q7M4", "Before you start", "The commands"):
        assert heading in lines, heading
    for item in ("• The first bullet", "• The second bullet", "• A nested bullet", "1. The first step",
                 "2. The second step"):
        assert item in lines, item
    flat = " ".join(text.split())
    assert "a bold word, an italic one, inline code and a price page (https://example.org/prices) in one" in flat
    assert "See https://example.org/docs for more." in flat
    # the command copies out as it was written, the indents and inner spaces too
    assert COMMAND in text.splitlines()
    code = layout_lines(out)
    assert COMMAND in code and INDENTED in code and TABBED.expandtabs(8) in code
    # the long line wraps and is never cut: its pieces, joined, are the line
    start = next(i for i, x in enumerate(code) if x.startswith("awb calc"))
    pieces = [code[start]]
    while len("".join(pieces)) < len(LONG):
        pieces.append(code[start + len(pieces)])
    assert len(pieces) > 1 and "".join(pieces) == LONG
    # the table as aligned Courier text
    table = [x for x in code if x.startswith(("Service", "ECS", "RDS"))]
    assert table == ["Service | Flavor     | Hours", "ECS     | s3.large.2 | 730", "RDS     | rds.pg.x1  | 24"]
    raw = out.read_bytes()
    assert b"/Subtype /Link" in raw and b"/URI (https://example.org/prices)" in raw
    assert b"/BaseFont /Courier " in raw and b"/BaseFont /Helvetica-Oblique " in raw
    assert b"/BaseFont /Helvetica-Bold " in raw and b"/BaseFont /Helvetica " in raw


def test_the_wrap_mark_is_drawn_and_never_copied(project):
    src = project / "deliverables" / "long.md"
    src.write_text("```\n%s\n```\n" % ("x" * 200), encoding="utf-8")
    assert pdf.main([str(src)]) == 0
    out = src.with_suffix(".pdf")
    code = [x for x in layout_lines(out) if x.startswith("x")]
    assert len(code) == 3 and "".join(code) == "x" * 200
    raw = out.read_bytes()
    assert raw.count(b" l S ") >= 4                 # two hooked arrows, two strokes each


def test_an_html_sample_with_every_element_reads_back(project, capsys):
    src = project / "deliverables" / "howto-widget.html"
    src.write_text(HTML, encoding="utf-8")
    assert pdf.main([str(src), "--title", "Widget how-to tcp-q7m4"]) == 0
    out = src.with_suffix(".pdf")
    info = poppler("pdfinfo", out)
    assert re.search(r"^Title:\s+Widget how-to tcp-q7m4$", info, re.M) and re.search(r"^Pages:\s+1$", info, re.M)
    text = poppler("pdftotext", out)
    lines = [" ".join(x.split()) for x in text.splitlines()]
    for heading in ("How to: create a widget", "Before you start", "Steps"):
        assert heading in lines, heading
    for item in ("• You are signed in.", "• Your account may use the service.", "1. Open the widget list",
                 "• Menu path: Compute > Widgets", "2. Create the widget", "On the screen: a form"):
        assert item in lines, item
    flat = " ".join(text.split())
    assert "A paragraph with strong, em, Create now and the console (https://example.org/console)." in flat
    assert "Text of an element the writer does not know" in flat and "hidden" not in flat
    code = layout_lines(out)
    assert COMMAND in code and INDENTED in code
    assert [x for x in code if x.startswith(("Field", "Name"))] == ["Field | Value", "Name  | widget-q7m4"]
    assert b"/URI (https://example.org/console)" in out.read_bytes()


def test_a_long_text_breaks_into_numbered_pages(project):
    src = project / "deliverables" / "long.md"
    src.write_text("# Long\n\n" + "\n\n".join("Paragraph %d of a text that is long enough to need more than one "
                                               "line on an A4 page of the shared writer." % n for n in range(1, 161)),
                   encoding="utf-8")
    assert pdf.main([str(src)]) == 0
    out = src.with_suffix(".pdf")
    pages = int(re.search(r"^Pages:\s+(\d+)$", poppler("pdfinfo", out), re.M).group(1))
    assert pages >= 4
    text = poppler("pdftotext", out)
    assert "Paragraph 160 of a text" in text and "Paragraph 1 of a text" in text
    for n in range(1, pages + 1):
        page = poppler("pdftotext", out, "-f", str(n), "-l", str(n))
        assert re.search(r"(?m)^%d\s*/\s*%d$" % (n, pages), page), n


def test_cyrillic_is_refused_with_its_line_and_count_and_replaced_with_replace(project, capsys):
    src = project / "deliverables" / "note.md"
    src.write_text("# Note\n\nPlain words first.\n\nThen a word: привет and more.\n",
                   encoding="utf-8")
    out = src.with_suffix(".pdf")
    assert pdf.main([str(src)]) == 1
    err = capsys.readouterr().err.strip()
    assert len(err.splitlines()) == 1 and "line 5: 6 character(s)" in err and "--replace" in err
    assert not out.exists()
    assert pdf.main([str(src), "--replace"]) == 0
    said = capsys.readouterr().out
    assert "6 character(s) the standard fonts cannot show written as ?" in said
    assert "Then a word: ?????? and more." in poppler("pdftotext", out)


def test_a_write_outside_the_project_or_the_scratch_folder_is_refused(project, tmp_path, capsys):
    src = project / "deliverables" / "offer-q7m4.md"
    src.write_text("# Offer\n\nText.\n", encoding="utf-8")
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    assert pdf.main([str(src), "--out", str(elsewhere / "offer.pdf")]) == 2
    assert "inside the project folder or the session scratch folder" in capsys.readouterr().err
    assert not (elsewhere / "offer.pdf").exists()
    (project / "deliverables" / "link.pdf").symlink_to(elsewhere / "target.pdf")
    assert pdf.main([str(src), "--out", str(project / "deliverables" / "link.pdf")]) == 2
    assert not (elsewhere / "target.pdf").exists()
    assert pdf.main([str(src), "--out", str(project / "notes" / "offer.txt")]) == 2
    # the session scratch folder is the other place
    scratch = Path(pdf.SCRATCH % __import__("os").getuid())
    scratch.mkdir()
    assert pdf.main([str(src), "--out", str(scratch / "offer.pdf")]) == 0 and (scratch / "offer.pdf").is_file()
    # outside a project the PDF next to the file is refused
    (elsewhere / "free.md").write_text("# Free\n", encoding="utf-8")
    assert pdf.main([str(elsewhere / "free.md")]) == 2
    assert not (elsewhere / "free.pdf").exists()
    assert pdf.main([str(project / "SCOPE.md").replace(".md", ".txt")]) == 2


def test_howto_render_pdf_writes_the_pdf_with_the_shared_writer(tmp_path, project, monkeypatch, capsys):
    """Successor of the --pdf assertion of test_the_command_line_drafts_renders_and_pdf_waits (B-033)."""
    monkeypatch.setenv("AWB_MIRRORS", str(make_mirror(tmp_path)))
    assert howto.main(["draft", "create widget", "--service", "zorbal", "--step", "widget list", "--name", "w"]) == 0
    md = project / "deliverables" / "howto-w.md"
    capsys.readouterr()
    assert howto.main(["render", str(md), "--pdf"]) == 0
    out = capsys.readouterr().out
    pdf_path = md.with_suffix(".pdf")
    assert "howto-w.html" in out and "%s, 1 page(s)" % pdf_path in out and "xchg put" in out
    text = poppler("pdftotext", pdf_path)
    assert "How to: create widget" in text and "Before you start" in text
    assert re.search(r"^Title:\s+How to: create widget$", poppler("pdfinfo", pdf_path), re.M)


def test_the_dataset_how_to_pdf_is_made_by_the_shared_writer(home, facts, tmp_path):
    built = dataset.build(home, out=tmp_path / "ds", today=TODAY)
    path = built.folder / "HOW-TO.pdf"
    info = poppler("pdfinfo", path)
    assert re.search(r"^Producer:\s+awb pdf$", info, re.M)
    assert re.search(r"^Title:\s+TCP Facts 2026-10-08: how to use it$", info, re.M)
    assert re.search(r"^Author:\s+TCP Facts, github.com/shimoza/tcp-facts$", info, re.M)
    text = poppler("pdftotext", path)
    assert dataset.ONE_LINER in text.splitlines()
    for line in dataset.LONG_PROMPT.split("\n"):
        assert line.split()[0] in text
    assert path.read_bytes() == dataset.pdf_bytes(*_blocks(home, tmp_path))


def _blocks(home, tmp_path):
    seen = {}
    real = dataset.pdf_bytes

    def spy(blocks, title):
        seen["args"] = (blocks, title)
        return real(blocks, title)
    dataset.pdf_bytes = spy
    try:
        dataset.build(home, out=tmp_path / "again", today=TODAY)
    finally:
        dataset.pdf_bytes = real
    return seen["args"]
