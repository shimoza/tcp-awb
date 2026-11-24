"""awb docx keeps the level of h4 to h6 (ask 2026-11-24-tcp-ui5o-4) and awb xchg names a file it cannot read
(ask 2026-11-24-tcp-ui5o-5 and -6).

A sub-section 2.1.1 (h4 or ####) got the style Heading 3 like 2.1, so the navigation pane and the heading sizes
put both on one level. The PDF still draws h4 to h6 as h3. Invented content only.
"""
from __future__ import annotations

import os
import zipfile
from pathlib import Path

import pytest

from awb import docx, pdf
from awb.tcp import xchg

PAGE = """<html><head><title>guide q7m4</title><style>h4 { color: #aa0066 }</style></head><body>
<h1>1. start</h1><p>text one.</p>
<h2>1.1 part</h2><p>text two.</p>
<h3>1.1.1 piece</h3><p>text three.</p>
<h4>1.1.1.1 detail</h4><p>text four.</p>
</body></html>
"""


def _document(path: Path) -> str:
    with zipfile.ZipFile(path) as z:
        return z.read("word/document.xml").decode("utf-8"), z.read("word/styles.xml").decode("utf-8")


@pytest.fixture
def project(tmp_path, monkeypatch):
    (tmp_path / "SCOPE.md").write_text("# scope\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(pdf, "_screen", lambda text: None)
    return tmp_path


def test_ui5o_h4_is_heading_4_in_word_with_its_number_level(project):
    src = project / "guide.html"
    src.write_text(PAGE, encoding="utf-8")
    out = docx.convert(src)[0]
    body, styles = _document(out)
    assert '<w:pStyle w:val="Heading4"/>' in body
    assert 'w:styleId="Heading4"' in styles and "aa0066" in styles.lower()
    assert '<w:ilvl w:val="3"/>' in body


def test_ui5o_the_pdf_still_draws_h4_as_h3_and_keeps_the_source_level():
    blocks, _ = pdf.from_html(PAGE)
    heads = [(b.kind, b.level) for b in blocks if b.kind.startswith("h")]
    assert heads == [("h1", 1), ("h2", 2), ("h3", 3), ("h3", 4)]
    blocks, _ = pdf.from_markdown("# a\n\n#### b\n")
    assert [(b.kind, b.level) for b in blocks if b.kind.startswith("h")] == [("h1", 1), ("h3", 4)]


def test_ui5o_xchg_names_a_file_it_cannot_read(tmp_path, monkeypatch):
    folder = tmp_path / "out"
    folder.mkdir()
    (folder / "guide.pdf").write_bytes(b"x")
    real = os.open

    def refuse(name, flags, mode=0o777, *, dir_fd=None):
        if name == "guide.pdf":
            raise PermissionError(13, "denied")
        return real(name, flags, mode, dir_fd=dir_fd)

    monkeypatch.setattr(os, "open", refuse)
    with pytest.raises(xchg.XchgError) as err:
        with xchg._directory(folder) as d:
            with xchg._regular(d, "guide.pdf"):
                pass
    assert "guide.pdf cannot be read by this user" in str(err.value)
    assert "exchange folder cannot be opened" not in str(err.value)


def test_ui5o_pdf_and_docx_are_group_readable_in_a_project_and_private_in_scratch(project, tmp_path, monkeypatch):
    import stat

    src = project / "guide.html"
    src.write_text(PAGE, encoding="utf-8")
    assert stat.S_IMODE(pdf.convert(src)[0].stat().st_mode) == 0o664
    assert stat.S_IMODE(docx.convert(src)[0].stat().st_mode) == 0o664
    scratch = tmp_path / "scratch-%d"
    (tmp_path / ("scratch-%d" % os.getuid())).mkdir()
    monkeypatch.setattr(pdf, "SCRATCH", str(scratch))
    inner = Path(str(scratch) % os.getuid()) / "guide.html"
    inner.write_text(PAGE, encoding="utf-8")
    monkeypatch.chdir(inner.parent)
    assert stat.S_IMODE(pdf.convert(inner)[0].stat().st_mode) == 0o600
