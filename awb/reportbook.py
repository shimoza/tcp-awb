"""The fingerprint of the owner's report workbook (task R2, presentations/reports-design/DESIGN.md §2 and
rules/report-workbook.txt): a workbook with both a sheet Names and a sheet Projects is his `report.xlsx` or a copy
of it. It never leaves the laptop.

The sheet names are read from `xl/workbook.xml` of the zip with the standard library, nothing else of the file is
opened, whatever its suffix: a copy renamed to .zip or .bin keeps its fingerprint. A file that is not a zip, a zip
without that part or a part that cannot be read is no report workbook. Nothing here prints a sheet, a cell or a
file name.
"""
from __future__ import annotations

import html
import re
import zipfile
from pathlib import Path

SHEETS = ("Names", "Projects")
"""Both sheets together are the fingerprint; one of them alone is any other workbook."""
REFUSED = "the report workbook never leaves the laptop"
WORKBOOK_PART = "xl/workbook.xml"
MAX_PART = 8 * 1024 * 1024
_SHEET_RE = re.compile(r"<(?:[\w.-]+:)?sheet\b[^>]*?\bname\s*=\s*(\"[^\"]*\"|'[^']*')", re.S)


def sheet_names(path: Path) -> list[str]:
    """The sheet names of a workbook in `xl/workbook.xml`, empty for any other file."""
    try:
        if not zipfile.is_zipfile(path):
            return []
        with zipfile.ZipFile(path) as z:
            info = next((i for i in z.infolist() if i.filename.lstrip("/").lower() == WORKBOOK_PART), None)
            if info is None or info.file_size > MAX_PART:
                return []
            with z.open(info) as fh:
                text = fh.read(MAX_PART + 1).decode("utf-8", errors="replace")
    except (OSError, zipfile.BadZipFile, RuntimeError, ValueError, NotImplementedError):
        return []
    return [html.unescape(m.group(1)[1:-1]) for m in _SHEET_RE.finditer(text)]


def is_report_workbook(path: Path) -> bool:
    """True when the file is a workbook with both a sheet Names and a sheet Projects (case and spaces aside)."""
    names = {n.strip().casefold() for n in sheet_names(Path(path))}
    return all(s.casefold() in names for s in SHEETS)
