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
    assert re.search(r"^Author:\s+%s$" % re.escape(pdf.AUTHOR), info, re.M)
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
    # the pipe table as a ruled grid since fix/pdf-tables: its rows read back in order, cell by cell
    table = [x.split() for x in poppler("pdftotext", out, "-layout").splitlines()
             if x.split()[:1] in (["Service"], ["ECS"], ["RDS"])]
    assert table == [["Service", "Flavor", "Hours"], ["ECS", "s3.large.2", "730"], ["RDS", "rds.pg.x1", "24"]]
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


@pytest.mark.skipif(__import__("awb.pdffont").pdffont.find() is None, reason="no Liberation or DejaVu font")
def test_cyrillic_renders_by_default_and_is_replaced_with_replace(project, capsys):
    """Replaces test_cyrillic_is_refused_with_its_line_and_count_and_replaced_with_replace (owner's yes of
    2026-11-20, Unicode by default): Cyrillic now embeds a font of the host and comes back from pdftotext as
    written, where it was refused with its line and count. The refusal of a character no font has, with its line
    and count, is asserted in tests/test_pdf_unicode.py. Kept: --replace writes ? with the standard fonts."""
    src = project / "deliverables" / "note.md"
    src.write_text("# Note\n\nPlain words first.\n\nThen a word: привет and more.\n",
                   encoding="utf-8")
    out = src.with_suffix(".pdf")
    assert pdf.main([str(src)]) == 0
    assert capsys.readouterr().err == ""
    assert "Then a word: привет and more." in poppler("pdftotext", out)
    assert b"/FontFile2" in out.read_bytes()
    out.unlink()
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


# Hardening regressions use only temporary projects and call Python APIs in this process.
def _hardening_source(project, text="# Safe\n\nText.\n", suffix=".md"):
    src = project / "deliverables" / ("checked" + suffix)
    src.write_text(text, encoding="utf-8")
    return src


def _refused(src, capsys, *args):
    assert pdf.main([str(src), *args]) == 2
    said = capsys.readouterr()
    assert not said.out and len(said.err.splitlines()) == 1
    return said.err


def test_ph_missing_input_never_echoes_its_name(project, capsys):
    name = "pass" + "word" + "=q7m4-private-value"
    src = project / (name + ".md")
    assert name not in _refused(src, capsys)
    assert not src.with_suffix(".pdf").exists()


def test_ph_missing_output_folder_never_echoes_its_name(project, capsys):
    src = _hardening_source(project)
    name = "pass" + "word" + "=q7m4-private-value"
    assert name not in _refused(src, capsys, "--out", str(project / name / "out.pdf"))


def test_ph_unreadable_input_error_withholds_values(project, monkeypatch, capsys):
    import os

    src = _hardening_source(project)
    secret = "pass" + "word" + "=q7m4-private-value"
    real = os.open

    def denied(path, flags, *args, **kwargs):
        if str(path) in (str(src), src.name):
            raise PermissionError(secret)
        return real(path, flags, *args, **kwargs)

    monkeypatch.setattr(os, "open", denied)
    assert secret not in _refused(src, capsys)
    assert not src.with_suffix(".pdf").exists()


def test_ph_invalid_utf8_preserves_existing_output(project, capsys):
    src = _hardening_source(project)
    src.write_bytes(b"\xff")
    out = src.with_suffix(".pdf")
    out.write_bytes(b"previous")
    assert "UTF-8" in _refused(src, capsys)
    assert out.read_bytes() == b"previous"


def test_ph_symlink_input_is_refused(project, capsys):
    real = _hardening_source(project)
    src = project / "linked.md"
    src.symlink_to(real)
    _refused(src, capsys)
    assert not src.with_suffix(".pdf").exists()


def test_ph_fifo_input_is_refused_without_blocking(project, capsys):
    import os

    src = project / "pipe.md"
    os.mkfifo(src)
    _refused(src, capsys)
    assert not src.with_suffix(".pdf").exists()


def test_ph_input_swap_to_link_is_never_read(project, monkeypatch, capsys):
    import os

    src = _hardening_source(project)
    other = project / "other.md"
    other.write_text("must not be read", encoding="utf-8")
    real = os.open
    swapped = []

    def swap(path, flags, *args, **kwargs):
        if str(path) in (str(src), src.name) and not swapped:
            swapped.append(True)
            src.unlink()
            src.symlink_to(other)
        return real(path, flags, *args, **kwargs)

    monkeypatch.setattr(os, "open", swap)
    _refused(src, capsys)
    assert swapped and not src.with_suffix(".pdf").exists()


def test_ph_input_parent_link_cannot_escape(project, tmp_path, capsys):
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "source.md").write_text("# Outside", encoding="utf-8")
    (project / "linked").symlink_to(outside, target_is_directory=True)
    out = project / "out.pdf"
    _refused(project / "linked" / "source.md", capsys, "--out", str(out))
    assert not out.exists()


def test_ph_outside_input_cannot_be_copied_into_project(project, tmp_path, capsys):
    src = tmp_path / "outside.md"
    src.write_text("# Outside", encoding="utf-8")
    out = project / "out.pdf"
    _refused(src, capsys, "--out", str(out))
    assert not out.exists()


def test_ph_input_growth_is_bounded(project, monkeypatch, capsys):
    import os

    src = _hardening_source(project)
    monkeypatch.setattr(pdf, "MAX_INPUT", 64)
    real = os.fstat
    grown = []

    def grow(fd):
        info = real(fd)
        if info.st_ino == src.stat().st_ino and not grown:
            grown.append(True)
            src.write_bytes(b"x" * 65)
        return info

    monkeypatch.setattr(os, "fstat", grow)
    _refused(src, capsys)
    assert grown and not src.with_suffix(".pdf").exists()


def test_ph_input_changed_during_read_is_refused(project, monkeypatch, capsys):
    import os

    src = _hardening_source(project)
    real = os.fstat
    changed = []

    def change(fd):
        info = real(fd)
        if info.st_ino == src.stat().st_ino and not changed:
            changed.append(True)
            src.write_bytes(b"x" * info.st_size)
            os.utime(src, ns=(info.st_atime_ns, info.st_mtime_ns + 1000000))
        return info

    monkeypatch.setattr(os, "fstat", change)
    _refused(src, capsys)
    assert changed and not src.with_suffix(".pdf").exists()


def test_ph_fifo_output_is_not_replaced(project, capsys):
    import os
    import stat

    src = _hardening_source(project)
    out = src.with_suffix(".pdf")
    os.mkfifo(out)
    _refused(src, capsys)
    assert stat.S_ISFIFO(out.stat().st_mode)


def test_ph_linked_scratch_root_cannot_authorize_outside_write(project, tmp_path, capsys):
    import os

    src = _hardening_source(project)
    outside = tmp_path / "outside"
    outside.mkdir()
    scratch = Path(pdf.SCRATCH % os.getuid())
    scratch.symlink_to(outside, target_is_directory=True)
    _refused(src, capsys, "--out", str(scratch / "out.pdf"))
    assert not list(outside.iterdir())


def test_ph_linked_scope_does_not_authorize_a_project(project, tmp_path, capsys):
    src = _hardening_source(project)
    scope = project / "SCOPE.md"
    moved = tmp_path / "scope.md"
    scope.rename(moved)
    scope.symlink_to(moved)
    _refused(src, capsys)
    assert not src.with_suffix(".pdf").exists()


def test_ph_output_parent_swap_cannot_redirect_publication(project, tmp_path, monkeypatch, capsys):
    src = _hardening_source(project)
    out = project / "notes" / "out.pdf"
    outside = tmp_path / "outside"
    outside.mkdir()
    moved = project / "old-notes"
    real = pdf.render

    def swap(*args, **kwargs):
        data = real(*args, **kwargs)
        out.parent.rename(moved)
        out.parent.symlink_to(outside, target_is_directory=True)
        return data

    monkeypatch.setattr(pdf, "render", swap)
    _refused(src, capsys, "--out", str(out))
    assert not list(outside.iterdir()) and not list(moved.iterdir())


def test_ph_output_parent_moved_during_write_cleans_pinned_directory(project, tmp_path, monkeypatch, capsys):
    import os

    src = _hardening_source(project)
    out = project / "notes" / "out.pdf"
    moved = tmp_path / "moved-notes"
    real = os.fdopen
    swapped = []

    def swap(fd, mode, *args, **kwargs):
        if mode == "wb":
            out.parent.rename(moved)
            out.parent.mkdir()
            swapped.append(True)
        return real(fd, mode, *args, **kwargs)

    monkeypatch.setattr(os, "fdopen", swap)
    _refused(src, capsys, "--out", str(out))
    assert swapped and not list(moved.iterdir()) and not list(out.parent.iterdir())


def test_ph_output_link_planted_during_write_is_preserved(project, monkeypatch, capsys):
    import os

    src = _hardening_source(project)
    out = src.with_suffix(".pdf")
    victim = project / "victim"
    victim.write_bytes(b"previous")
    real = os.fdopen

    def plant(fd, mode, *args, **kwargs):
        if mode == "wb":
            out.symlink_to(victim)
        return real(fd, mode, *args, **kwargs)

    monkeypatch.setattr(os, "fdopen", plant)
    _refused(src, capsys)
    assert out.is_symlink() and victim.read_bytes() == b"previous"
    assert not list(src.parent.glob(".awb-pdf-*"))


def test_ph_replace_failure_preserves_old_pdf_and_removes_temporary_file(project, monkeypatch, capsys):
    import os

    src = _hardening_source(project)
    out = src.with_suffix(".pdf")
    out.write_bytes(b"previous")
    secret = "pass" + "word" + "=q7m4-private-value"

    def fail(*args, **kwargs):
        raise OSError(secret)

    monkeypatch.setattr(os, "replace", fail)
    assert secret not in _refused(src, capsys)
    assert out.read_bytes() == b"previous"
    assert sorted(p.name for p in src.parent.iterdir()) == [src.name, out.name]


def test_ph_pdf_permissions_are_private_without_changing_umask(project, monkeypatch):
    import os
    import stat

    src = _hardening_source(project)
    calls = []
    real = os.umask

    def mask(value):
        calls.append(value)
        return real(value)

    monkeypatch.setattr(os, "umask", mask)
    out, _, _ = pdf.convert(src)
    assert stat.S_IMODE(out.stat().st_mode) == 0o600
    assert calls == []


def test_ph_concurrent_writes_publish_complete_pdfs(project, monkeypatch):
    import os
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier

    src = _hardening_source(project)
    out = src.with_suffix(".pdf")
    expected = {pdf.render(*args) for args in [([pdf.plain("p", "first")], "first"),
                                              ([pdf.plain("p", "second")], "second")]}
    barrier = Barrier(2)
    real = os.replace
    snapshots = []

    def publish(*args, **kwargs):
        barrier.wait(timeout=10)
        result = real(*args, **kwargs)
        snapshots.append(out.read_bytes())
        return result

    monkeypatch.setattr(os, "replace", publish)
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(pdf._publish, out, data) for data in expected]
        for future in futures:
            future.result(timeout=15)
    assert len(snapshots) == 2 and all(data in expected for data in snapshots)
    assert out.read_bytes() in expected and not list(src.parent.glob(".awb-pdf-*"))


def test_ph_body_secret_is_refused_before_output(project, capsys):
    secret = "pass" + "word" + "=q7m4-private-value"
    src = _hardening_source(project, secret)
    assert secret not in _refused(src, capsys)
    assert not src.with_suffix(".pdf").exists()


def test_ph_registered_name_is_refused_in_body(project, home, capsys):
    from tests import fixtures

    name = fixtures.CUSTOMER_FORMS[0]
    src = _hardening_source(project, name)
    assert name not in _refused(src, capsys)
    assert not src.with_suffix(".pdf").exists()


def test_ph_parsed_html_cannot_join_a_private_value(project, home, capsys):
    from tests import fixtures

    name = fixtures.CUSTOMER_FORMS[1]
    src = _hardening_source(project, "<p>" + "<b></b>".join(name) + "</p>", ".html")
    assert name not in _refused(src, capsys)
    assert not src.with_suffix(".pdf").exists()


def test_ph_render_screens_all_metadata_and_link_carriers(home):
    from tests import fixtures

    name = fixtures.CUSTOMER_FORMS[1]
    secret = "pass" + "word" + "=q7m4-private-value"
    for blocks, title, author in [([pdf.plain("p", "safe")], name, pdf.AUTHOR),
                                   ([pdf.plain("p", "safe")], "safe", secret),
                                   ([pdf.Block("p", runs=[("safe", "r", "https://example.org/?" + secret)])],
                                    "safe", pdf.AUTHOR),
                                   ([pdf.Block("grid", lines=[["safe"], [name]])], "safe", pdf.AUTHOR)]:
        with pytest.raises(pdf.PdfError) as caught:
            pdf.render(blocks, title, author)
        assert name not in str(caught.value) and secret not in str(caught.value)


def test_ph_private_filename_is_not_published_or_printed(project, home, capsys):
    from tests import fixtures

    src = _hardening_source(project)
    name = fixtures.CUSTOMER_FORMS[1]
    out = project / (name + ".pdf")
    assert name not in _refused(src, capsys, "--out", str(out))
    assert not out.exists()


def test_ph_privacy_check_failure_is_closed_and_value_free(project, home, monkeypatch, capsys):
    from awb import check

    src = _hardening_source(project)
    secret = "pass" + "word" + "=q7m4-private-value"

    def fail(*args, **kwargs):
        raise RuntimeError(secret)

    monkeypatch.setattr(check, "check_text", fail)
    assert secret not in _refused(src, capsys)
    assert not src.with_suffix(".pdf").exists()


def test_ph_missing_register_in_existing_vault_is_refused(project, home, capsys):
    home.register.unlink()
    src = _hardening_source(project)
    _refused(src, capsys)
    assert not src.with_suffix(".pdf").exists()


def test_ph_unknown_block_error_never_echoes_kind():
    secret = "pass" + "word" + "=q7m4-private-value"
    with pytest.raises(pdf.PdfError) as caught:
        pdf.layout([pdf.Block(secret)])
    assert secret not in str(caught.value)


def test_ph_invalid_html_list_start_is_bounded(project, capsys):
    for value in ("²", "9" * 5000):
        src = _hardening_source(project, '<ol start="' + value + '"><li>Item</li></ol>', ".html")
        assert pdf.main([str(src)]) == 0
        assert "1. Item" in poppler("pdftotext", src.with_suffix(".pdf"))
        assert not capsys.readouterr().err


def test_ph_hidden_html_cannot_emit_link_attributes():
    text = '<head><a href="https://example.org/hidden">hidden</a></head><p>visible</p>'
    blocks, title = pdf.from_html(text)
    data = pdf.render(blocks, title)
    assert b"hidden" not in data and b"visible" in data


def test_ph_html_parser_error_never_echoes_values(project, monkeypatch, capsys):
    src = _hardening_source(project, "<p>safe</p>", ".html")
    secret = "pass" + "word" + "=q7m4-private-value"

    def fail(*args, **kwargs):
        raise AssertionError(secret)

    monkeypatch.setattr(pdf._Html, "feed", fail)
    assert secret not in _refused(src, capsys)
    assert not src.with_suffix(".pdf").exists()


def test_ph_nested_html_table_does_not_crash(project, capsys):
    src = _hardening_source(project, "<table><td><table></td></table>", ".html")
    assert pdf.main([str(src)]) == 0
    assert not capsys.readouterr().err
    assert "Pages:" in poppler("pdfinfo", src.with_suffix(".pdf"))


def test_ph_unclosed_html_pre_and_table_keep_text():
    for text, expected in [("<pre>code survives", b"code survives"),
                           ("<table><tr><td>cell survives", b"cell survives")]:
        blocks, title = pdf.from_html(text)
        assert expected in pdf.render(blocks, title)


def test_ph_html_head_title_is_preserved():
    _, title = pdf.from_html("<head><title>Document title</title></head><p>Body</p>")
    assert title == "Document title"


def test_ph_hostile_long_line_is_refused_before_parsing(project, capsys):
    src = _hardening_source(project, "`" * 16385)
    _refused(src, capsys)
    assert not src.with_suffix(".pdf").exists()


def test_ph_deep_lists_are_refused_before_drawing_outside_page(project, capsys):
    src = _hardening_source(project, "<ul><li>" * 30 + "deep" + "</li></ul>" * 30, ".html")
    _refused(src, capsys)
    assert not src.with_suffix(".pdf").exists()


def test_ph_too_narrow_grid_columns_are_refused(project, capsys):
    src = _hardening_source(project, "|" + "x|" * 100 + "\n|" + "--|" * 100 + "\n")
    _refused(src, capsys)
    assert not src.with_suffix(".pdf").exists()


def test_ph_oversized_table_header_is_refused(project, capsys):
    src = _hardening_source(project, "| " + "header " * 1500 + "|\n|---|\n|body|\n")
    _refused(src, capsys)
    assert not src.with_suffix(".pdf").exists()


def test_ph_long_word_wrapping_has_bounded_width_work(monkeypatch):
    real = pdf._width
    work = [0]

    def measured(text, *args):
        work[0] += len(text)
        assert work[0] < 25000
        return real(text, *args)

    monkeypatch.setattr(pdf, "_width", measured)
    word = "x" * 4000
    lines = pdf._lines([(word, "r", None)], pdf.TEXT_W, 10, False)
    assert "".join(t for line in lines for _, t, _, _ in line) == word
    assert all(max(x + real(t, f, 10) for x, t, f, _ in line) <= pdf.TEXT_W for line in lines)


def test_ph_page_count_cannot_be_spoofed_by_document_text(project):
    src = _hardening_source(project, "```\n/Type /Page /Parent 1 0 R /MediaBox\n```\n")
    out, pages, _ = pdf.convert(src)
    assert pages == 1
    assert re.search(r"^Pages:\s+1$", poppler("pdfinfo", out), re.M)


def test_ph_joined_paragraph_size_is_bounded(project, capsys):
    src = _hardening_source(project, ("text " * 2000 + "\n") * 2)
    _refused(src, capsys)
    assert not src.with_suffix(".pdf").exists()


def test_ph_markup_run_cannot_exhaust_inline_parser():
    with pytest.raises(pdf.PdfError):
        pdf.from_markdown("text " + "`" * 1000)


def test_ph_ragged_table_expansion_is_bounded():
    rows = [["x"] * 1000] + [["x"] for _ in range(100)]
    for kind in ("grid", "table"):
        with pytest.raises(pdf.PdfError):
            pdf.layout([pdf.Block(kind, lines=rows)])


def test_ph_pdf_page_count_is_bounded():
    pages = pdf._Pages()
    for _ in range(999):
        pages.new_page()
    with pytest.raises(pdf.PdfError):
        pages.new_page()


def test_ph_invalid_title_surrogate_is_refused(project, capsys):
    src = _hardening_source(project)
    _refused(src, capsys, "--title", "bad\ud800title")
    assert not src.with_suffix(".pdf").exists()


def test_ph_control_character_in_output_path_is_refused(project, capsys):
    src = _hardening_source(project)
    out = project / "split\nline.pdf"
    _refused(src, capsys, "--out", str(out))
    assert not out.exists()


def test_ph_failed_write_removes_temporary_file(project, monkeypatch, capsys):
    import os

    src = _hardening_source(project)
    out = src.with_suffix(".pdf")
    out.write_bytes(b"previous")
    real = os.fdopen

    class FailedWrite:
        def __init__(self, stream):
            self.stream = stream

        def __enter__(self):
            return self

        def __exit__(self, *args):
            self.stream.close()

        def write(self, data):
            self.stream.write(data[:50])
            raise OSError("disk full")

    def fail(fd, mode, *args, **kwargs):
        stream = real(fd, mode, *args, **kwargs)
        return FailedWrite(stream) if mode == "wb" else stream

    monkeypatch.setattr(os, "fdopen", fail)
    _refused(src, capsys)
    assert out.read_bytes() == b"previous"
    assert sorted(p.name for p in src.parent.iterdir()) == [src.name, out.name]


def test_ph_temporary_file_swap_cannot_publish_a_link(project, monkeypatch, capsys):
    import os

    src = _hardening_source(project)
    out = src.with_suffix(".pdf")
    out.write_bytes(b"previous")
    victim = project / "victim"
    victim.write_bytes(b"untouched")
    real = os.fdopen

    def swap(fd, mode, *args, **kwargs):
        if mode == "wb":
            temporary = next(p for p in src.parent.iterdir() if p.name.startswith("."))
            temporary.unlink()
            temporary.symlink_to(victim)
        return real(fd, mode, *args, **kwargs)

    monkeypatch.setattr(os, "fdopen", swap)
    _refused(src, capsys)
    assert not out.is_symlink() and out.read_bytes() == b"previous"
    assert victim.read_bytes() == b"untouched"
    assert not [p for p in src.parent.iterdir() if p.name.startswith(".")]


def test_ph_missing_plain_input_is_refused(project, capsys):
    src = project / "missing.md"
    _refused(src, capsys)
    assert not src.with_suffix(".pdf").exists()


def test_ph_missing_plain_output_directory_is_refused(project, capsys):
    src = _hardening_source(project)
    out = project / "missing" / "out.pdf"
    _refused(src, capsys, "--out", str(out))
    assert not out.parent.exists()


def test_ph_oversized_input_is_refused_before_reading(project, monkeypatch, capsys):
    src = _hardening_source(project, "x" * 65)
    monkeypatch.setattr(pdf, "MAX_INPUT", 64)
    _refused(src, capsys)
    assert not src.with_suffix(".pdf").exists()


def test_ph_convert_io_errors_are_value_free(project, monkeypatch):
    import os

    src = _hardening_source(project)
    secret = "pass" + "word" + "=q7m4-private-value"
    real = os.open

    def fail(path, flags, *args, **kwargs):
        if str(path) in (str(src), src.name):
            raise PermissionError(secret)
        return real(path, flags, *args, **kwargs)

    monkeypatch.setattr(os, "open", fail)
    with pytest.raises(pdf.PdfError) as caught:
        pdf.convert(src)
    assert secret not in str(caught.value)
