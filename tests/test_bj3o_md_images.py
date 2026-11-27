"""tcp-bj3o ask 2026-11-27-5: `awb docx` and `awb pdf` take a Markdown image `![alt](file)` as a figure.

The file is resolved relative to the .md and must lie inside the project folder of the .md; PNG and JPEG are taken,
the alt text is the caption. An absolute path, a path that leaves the project, a URL, another kind of file or a
missing file refuses the document with the path named and nothing is written. The DOCX is read back with python-docx
(inline shapes), the PDF with poppler (pdfimages -list, pdftotext).
"""
from __future__ import annotations

import struct
import subprocess
import zlib
from pathlib import Path

import docx as pydocx
import pytest

from awb import docx, pdf


def png(width: int, height: int) -> bytes:
    def chunk(kind: bytes, body: bytes) -> bytes:
        return struct.pack(">I", len(body)) + kind + body + struct.pack(">I", zlib.crc32(kind + body))

    rows = b"".join(b"\x00" + bytes((x * 9 + y * 5 + c * 70) % 256 for x in range(width) for c in range(3))
                    for y in range(height))
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(rows)) + chunk(b"IEND", b""))


def jpeg(width: int, height: int) -> bytes:
    """The smallest baseline JPEG header the writers read: SOI, APP0, SOF0 with 3 channels, EOI."""
    sof = b"\xff\xc0" + struct.pack(">HBHHB", 17, 8, height, width, 3) + b"\x01\x22\x00\x02\x11\x01\x03\x11\x01"
    return (b"\xff\xd8\xff\xe0" + struct.pack(">H", 16) + b"JFIF\x00\x01\x01\x00\x00\x01\x00\x01\x00\x00" + sof
            + b"\xff\xd9")


@pytest.fixture
def project(tmp_path, monkeypatch):
    p = tmp_path / "tcp-q7m4"
    (p / "deliverables" / "img").mkdir(parents=True)
    (p / "SCOPE.md").write_text("- code: tcp-q7m4\n", encoding="utf-8")
    (p / "deliverables" / "img" / "flow.png").write_bytes(png(40, 20))
    (p / "deliverables" / "img" / "rack.jpg").write_bytes(jpeg(30, 10))
    monkeypatch.chdir(p)
    monkeypatch.setattr(pdf, "SCRATCH", str(tmp_path / "scratch-%d"))
    return p


BODY = ("# Concept for CUST-Q7M4\n\nThe flow:\n\n![Data flow of tcp-q7m4](img/flow.png)\n\n"
        "Text before ![Rack view](img/rack.jpg \"rack\") and after.\n")


def source(project: Path, body: str = BODY) -> Path:
    src = project / "deliverables" / "concept-q7m4.md"
    src.write_text(body, encoding="utf-8")
    return src


def image_list(path: Path) -> list[list[str]]:
    out = subprocess.run(["pdfimages", "-list", str(path)], capture_output=True, text=True, check=True).stdout
    return [line.split() for line in out.splitlines()[2:] if line.strip()]


def test_the_docx_of_a_markdown_file_has_its_png_and_jpeg_as_pictures_with_captions(project, capsys):
    src = source(project)
    assert docx.main([str(src)]) == 0
    assert "2 figure(s)" in capsys.readouterr().out
    doc = pydocx.Document(str(src.with_suffix(".docx")))
    assert len(doc.inline_shapes) == 2
    blobs = [doc.part.related_parts[s._inline.graphic.graphicData.pic.blipFill.blip.embed].blob
             for s in doc.inline_shapes]
    assert blobs == [(project / "deliverables/img/flow.png").read_bytes(),
                     (project / "deliverables/img/rack.jpg").read_bytes()]
    captions = [p.text for p in doc.paragraphs if p.style.name == "Caption"]
    assert captions == ["Data flow of tcp-q7m4", "Rack view"]
    texts = [p.text for p in doc.paragraphs]
    assert "Text before" in texts and "and after." in texts


def test_the_pdf_of_a_markdown_file_has_its_png_and_jpeg_as_images_with_captions(project):
    src = source(project)
    assert pdf.main([str(src)]) == 0
    out = src.with_suffix(".pdf")
    rows = image_list(out)
    assert [(r[3], r[4], r[5]) for r in rows] == [("40", "20", "rgb"), ("30", "10", "rgb")]
    assert rows[1][8] == "jpeg"
    text = subprocess.run(["pdftotext", str(out), "-"], capture_output=True, text=True, check=True).stdout
    assert "Data flow of tcp-q7m4" in text and "Rack view" in text and "img/flow.png" not in text


@pytest.mark.parametrize("target, said", [
    ("../../outside.png", "outside the project folder"),
    ("/etc/flow.png", "absolute path"),
    ("https://host.example/flow.png", "is a URL"),
    ("img/notes.gif", "not a PNG or JPEG"),
])
def test_an_image_outside_the_project_a_url_or_another_kind_refuses_and_writes_nothing(project, capsys, target,
                                                                                      said):
    (project.parent / "outside.png").write_bytes(png(4, 4))
    src = source(project, "# T\n\n![x](%s)\n" % target)
    assert pdf.main([str(src)]) == 2
    err = capsys.readouterr().err
    assert said in err and target in err
    assert docx.main([str(src)]) == 2
    assert said in capsys.readouterr().err
    assert not src.with_suffix(".pdf").exists() and not src.with_suffix(".docx").exists()


def test_a_missing_image_is_an_error_naming_its_path(project, capsys):
    src = source(project, "# T\n\n![gone](img/none.png)\n")
    assert docx.main([str(src)]) == 2
    err = capsys.readouterr().err
    assert "img/none.png does not exist" in err and str(project / "deliverables" / "img" / "none.png") in err
    assert pdf.main([str(src)]) == 2
    assert "img/none.png does not exist" in capsys.readouterr().err


def test_an_image_in_inline_code_a_list_item_or_without_a_folder_stays_text(project):
    blocks, _ = pdf.from_markdown("a `![x](img/flow.png)` b\n\n- ![item](img/flow.png)\n",
                                  project / "deliverables")
    assert [b.kind for b in blocks] == ["p", "li"]
    blocks, _ = pdf.from_markdown("![alt only](img/none.png)\n")
    assert [(b.kind, pdf._plain_text(b.runs)) for b in blocks] == [("p", "alt only")]
