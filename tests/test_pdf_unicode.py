"""awb pdf --unicode: Russian (Cyrillic) and German umlauts as selectable text with an embedded font of the host
(awb/pdffont.py). The owner decision of 2026-11-20: shared capabilities instead of a writer per project.

Read back with pdftotext and pdffonts of the poppler tools. Skips where the host has no Liberation or DejaVu file.
"""
from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest

from awb import pdf, pdffont

TEXT = ("# Отчёт для CUST-Q7M4\n\n"
        "Привет, мир. Größe, Übung, Maß und **жирный** текст, *курсив* и `код`.\n\n"
        "| Сервис | Größe |\n|---|---|\n| ECS | средний |\n\n"
        "```\nпечать(\"Привет\")  # Prüfung\n```\n")

needs_fonts = pytest.mark.skipif(pdffont.find() is None, reason="no Liberation or DejaVu font on this host")


def poppler(tool: str, path: Path) -> str:
    args = [tool, str(path), "-"] if tool == "pdftotext" else [tool, str(path)]
    return subprocess.run(args, capture_output=True, text=True, check=True).stdout


@pytest.fixture
def project(tmp_path, monkeypatch):
    p = tmp_path / "tcp-q7m4"
    (p / "deliverables").mkdir(parents=True)
    (p / "SCOPE.md").write_text("- code: tcp-q7m4\n", encoding="utf-8")
    monkeypatch.chdir(p)
    monkeypatch.setattr(pdf, "SCRATCH", str(tmp_path / "scratch-%d"))
    return p


@needs_fonts
def test_cyrillic_and_umlauts_round_trip_as_selectable_text(project, capsys):
    src = project / "deliverables" / "report-ru.md"
    src.write_text(TEXT, encoding="utf-8")
    assert pdf.main([str(src), "--unicode"]) == 0
    out = src.with_suffix(".pdf")
    assert "1 page(s)" in capsys.readouterr().out
    flat = " ".join(poppler("pdftotext", out).split())
    for words in ("Отчёт для CUST-Q7M4", "Привет, мир. Größe, Übung, Maß und жирный текст, курсив и код.",
                  "Сервис", "средний", 'печать("Привет")', "# Prüfung"):
        assert words in flat, words
    assert "?" not in flat
    fonts = poppler("pdffonts", out)
    rows = [line.split() for line in fonts.splitlines()[2:] if line.strip()]
    assert len(rows) == 4
    for row in rows:                      # embedded, a subset, with a ToUnicode map
        assert re.match(r"^[A-Z]{6}\+", row[0]) and row[-5:-2] == ["yes", "yes", "yes"], row
    assert re.search(r"^Title:\s+Отчёт для CUST-Q7M4$", poppler("pdfinfo", out), re.M)


@needs_fonts
def test_the_subset_keeps_the_file_small(project):
    src = project / "deliverables" / "short.md"
    src.write_text("# Кратко\n\nОдна строка.\n", encoding="utf-8")
    assert pdf.main([str(src), "--unicode"]) == 0
    assert src.with_suffix(".pdf").stat().st_size < 200_000


def test_without_unicode_the_refusal_names_both_ways_out(project, capsys):
    src = project / "deliverables" / "note.md"
    src.write_text("# Note\n\nСлово.\n", encoding="utf-8")
    assert pdf.main([str(src)]) == 1
    err = capsys.readouterr().err
    assert "--unicode" in err and "--replace" in err
    assert not src.with_suffix(".pdf").exists()


def test_a_latin_document_keeps_the_standard_fonts(project):
    src = project / "deliverables" / "plain.md"
    src.write_text("# Plain\n\nGröße and Übung.\n", encoding="utf-8")
    assert pdf.main([str(src)]) == 0
    raw = src.with_suffix(".pdf").read_bytes()
    assert b"/BaseFont /Helvetica " in raw and b"/FontFile2" not in raw


@needs_fonts
def test_a_character_no_embedded_font_has_is_refused_or_replaced(project, capsys):
    src = project / "deliverables" / "rare.md"
    src.write_text("# Note\n\nA sign: \U0001F600 here.\n", encoding="utf-8")
    assert pdf.main([str(src), "--unicode"]) == 1
    assert "embedded fonts cannot show" in capsys.readouterr().err
    assert pdf.main([str(src), "--unicode", "--replace"]) == 0
    assert "1 character(s) the embedded fonts cannot show written as ?" in capsys.readouterr().out
    assert "A sign: ? here." in poppler("pdftotext", src.with_suffix(".pdf"))


def test_no_font_file_on_the_host_is_an_error_not_a_silent_fallback(project, monkeypatch, capsys):
    empty = project / "no-fonts"
    empty.mkdir()
    assert pdffont.find((str(empty),)) is None
    monkeypatch.setattr(pdffont, "find", lambda: None)
    src = project / "deliverables" / "note.md"
    src.write_text("# Note\n\nСлово.\n", encoding="utf-8")
    assert pdf.main([str(src), "--unicode"]) == 2
    assert "no Liberation or DejaVu font file" in capsys.readouterr().err


@needs_fonts
def test_the_subset_keeps_the_parts_of_a_composite_glyph():
    """An accented letter may be built from other glyphs: their outlines stay in the subset."""
    files = pdffont.find()
    font = pdffont.TrueType(files["r"])
    fonts = pdffont.FontSet(files)
    gid = font.cmap[ord("ё")]
    program = font.subset({gid})
    sub = pdffont.TrueType.__new__(pdffont.TrueType)
    sub.data = program
    sub.tables = sub._directory()
    sub.glyphs, sub.long_loca = font.glyphs, True
    loca = sub._loca()
    assert loca[gid + 1] > loca[gid]
    assert fonts.width("r", "ё") > 0
