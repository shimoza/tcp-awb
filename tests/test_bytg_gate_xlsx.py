"""tcp-bytg ask 2026-11-21-tcp-bytg-18: the commit gate reads a workbook (.xlsx) and checks its text like any text
file, and an evidence script of a project may name the home of the work user.

Workbooks are built with openpyxl from invented content; every planted value is built from pieces so that this file
passes the gate itself. Names come from tests/fixtures.py.
"""
from __future__ import annotations

import io
import os
import subprocess
import zipfile
from pathlib import Path

import openpyxl
import pytest
from openpyxl.comments import Comment
from openpyxl.workbook.defined_name import DefinedName

from awb import config, gate, xlsxlite
from tests import fixtures

CUSTOMER = fixtures.CUSTOMER_FORMS[0]
PERSON = fixtures.PERSON_FORMS[0]
HOME = "/ho" + "me/builder/notes.txt"
PLANTED_VALUE = "t7Kq2Zp9" + "Lw4Rx8Vn3Bm6Yc"
GH_BODY = "Zx8Kq3Lw7Rt2Vn5Bm9Yc4Hd6Jf1Gs0Pa3Ue7Wi"
HEX32 = "4f1c9a7e2b6d" + "40f8a3c5e9b1d7f2a6c0"
WORK = "labwork"


@pytest.fixture(autouse=True)
def isolated_git(monkeypatch):
    for key in list(os.environ):
        if key.startswith("GIT_"):
            monkeypatch.delenv(key)
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", os.devnull)
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")


def book(edit=None) -> bytes:
    """A small register as a session builds it: a header row, two rows, a number and a formula."""
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "register"
    ws.append(["handle", "state", "cost"])
    ws.append(["ecs-1", "live", 3])
    ws.append(["evs-2", "deleted", 4])
    ws["D2"] = "=C2*2"
    if edit is not None:
        edit(wb)
    out = io.BytesIO()
    wb.save(out)
    return out.getvalue()


def put(tmp_path: Path, data: bytes, name: str = "register.xlsx") -> Path:
    f = tmp_path / name
    f.write_bytes(data)
    return f


def classes(tmp_path: Path, register_path: Path, data: bytes, name: str = "register.xlsx") -> set[str]:
    return {f.cls for f in gate.scan_files([put(tmp_path, data, name)], register_path)}


def rezip(data: bytes, add: dict | None = None, drop: tuple = (), change=None, comment: bytes = b"") -> bytes:
    """The workbook `data` written again with parts added, dropped or changed."""
    out = io.BytesIO()
    with zipfile.ZipFile(io.BytesIO(data)) as src, zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as dst:
        for info in src.infolist():
            if info.filename in drop:
                continue
            raw = src.read(info.filename)
            if change is not None:
                raw = change(info.filename, raw)
            dst.writestr(info.filename, raw)
        for name, raw in (add or {}).items():
            dst.writestr(name, raw)
        dst.comment = comment
    return out.getvalue()


# --------------------------------------------------------------------------- read and checked like text


def test_a_clean_register_passes(tmp_path, register_path):
    assert gate.scan_files([put(tmp_path, book())], register_path) == []


def test_a_home_path_in_a_cell_is_found_on_a_line(tmp_path, register_path):
    data = book(lambda wb: wb.active.append(["ecs-3", HOME, 1]))
    found = gate.scan_files([put(tmp_path, data)], register_path)
    assert {f.cls for f in found} == {"homepath"}
    assert all(f.line > 0 for f in found)


@pytest.mark.parametrize("where", ["cell", "sheet", "comment", "creator", "title", "defined", "inline"])
def test_a_registered_name_anywhere_in_the_text_is_found(tmp_path, register_path, where):
    def edit(wb):
        ws = wb.active
        if where == "cell":
            ws["A5"] = "offer for %s" % CUSTOMER
        elif where == "sheet":
            wb.create_sheet(PERSON)
        elif where == "comment":
            ws["A2"].comment = Comment("asked by %s" % PERSON, "bot")
        elif where == "creator":
            wb.properties.creator = PERSON
        elif where == "title":
            wb.properties.title = "plan for %s" % CUSTOMER
        elif where == "defined":
            wb.defined_names["offer_text"] = DefinedName("offer_text", attr_text='"offer for %s"' % CUSTOMER)
        elif where == "inline":
            ws["A6"] = "=CONCAT(\"for \", \"%s\")" % CUSTOMER
    assert "name" in classes(tmp_path, register_path, book(edit))


def test_a_token_in_a_formula_and_an_id_in_a_cell_are_found(tmp_path, register_path):
    def edit(wb):
        wb.active["E2"] = "=LEN(\"" + "gh" + "p_" + GH_BODY + "\")"
        wb.active["A7"] = "project " + HEX32
    assert classes(tmp_path, register_path, book(edit)) == {"token", "identifier"}


def test_a_credential_table_across_two_cells_is_a_secret(tmp_path, register_path):
    def edit(wb):
        ws = wb.create_sheet("access")
        ws.append(["user", "pass" + "word"])
        ws.append(["builder", PLANTED_VALUE])
    assert classes(tmp_path, register_path, book(edit)) == {"secret"}


def test_a_home_path_in_an_xml_comment_of_a_part_is_found(tmp_path, register_path):
    def change(name, raw):
        if name == "xl/worksheets/sheet1.xml":
            return raw.replace(b"<sheetData>", ("<!-- built in " + HOME + " --><sheetData>").encode())
        return raw
    found = gate.scan_files([put(tmp_path, rezip(book(), change=change))], register_path)
    assert {f.cls for f in found} == {"homepath"}


def test_office_guids_in_structural_attributes_are_no_id(tmp_path, register_path):
    guid = "{3F2B8C1E-9A4D-" + "4E7B-8C2F-1A5D6E7F8A9B}"

    def change(name, raw):
        if name == "xl/worksheets/sheet1.xml":
            return raw.replace(b"<worksheet ", ('<worksheet uid="%s" ' % guid).encode(), 1)
        return raw
    assert classes(tmp_path, register_path, rezip(book(), change=change)) == set()


def test_the_staged_workbook_is_read_and_a_clean_one_commits(tmp_path, register_path, capsys):
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    (repo / "register.xlsx").write_bytes(book())
    subprocess.run(["git", "-C", str(repo), "add", "register.xlsx"], check=True)
    assert gate.main(["--register", str(register_path), "--staged", "--repo", str(repo)]) == 0
    (repo / "register.xlsx").write_bytes(book(lambda wb: wb.active.append([HOME])))
    subprocess.run(["git", "-C", str(repo), "add", "register.xlsx"], check=True)
    assert gate.main(["--register", str(register_path), "--staged", "--repo", str(repo)]) == 1
    out = capsys.readouterr()
    assert "homepath" in out.out + out.err and "builder" not in out.out + out.err


# --------------------------------------------------------------------------- refused when it cannot be read safely


def bomb() -> bytes:
    return rezip(book(), add={"xl/padding.xml": b"<a>" + b" " * (xlsxlite.MAX_MEMBER + 1) + b"</a>"})


@pytest.mark.parametrize("case", [
    "macro", "external-link", "connection", "external-relation", "picture", "too-many-parts", "bomb",
    "bytes-after", "archive-comment", "doctype", "not-a-zip", "broken-xml", "twice",
])
def test_a_workbook_that_cannot_be_read_safely_stays_opaque(tmp_path, register_path, case):
    clean = book()
    rels = ('<?xml version="1.0"?><Relationships xmlns="http://schemas.openxmlformats.org/package/2006/'
            'relationships"><Relationship Id="rId9" Type="http://schemas.openxmlformats.org/officeDocument/2006/'
            'relationships/oleObject" Target="file:///x/y.bin" TargetMode="External"/></Relationships>')
    if case == "macro":
        data = rezip(clean, add={"xl/vbaProject.bin": b"\x00macro"})
    elif case == "external-link":
        data = rezip(clean, add={"xl/externalLinks/externalLink1.xml": b"<externalLink/>"})
    elif case == "connection":
        data = rezip(clean, add={"xl/connections.xml": b"<connections/>"})
    elif case == "external-relation":
        data = rezip(clean, add={"xl/worksheets/_rels/sheet9.xml.rels": rels.encode()})
    elif case == "picture":
        data = rezip(clean, add={"xl/media/image1.png": b"\x89PNG\r\n\x1a\n"})
    elif case == "too-many-parts":
        data = rezip(clean, add={"xl/extra/p%d.xml" % i: b"<p/>" for i in range(xlsxlite.MAX_MEMBERS)})
    elif case == "bomb":
        data = bomb()
    elif case == "bytes-after":
        data = clean + b"hidden text after the archive"
    elif case == "archive-comment":
        data = rezip(clean, comment=b"a comment")
    elif case == "doctype":
        data = rezip(clean, change=lambda n, r: b"<!DOCTYPE x [<!ENTITY a \"b\">]>" + r
                     if n == "xl/workbook.xml" else r)
    elif case == "not-a-zip":
        data = b"PK\x03\x04" + b"not really an archive" * 10
    elif case == "broken-xml":
        data = rezip(clean, change=lambda n, r: r[:-5] if n == "xl/workbook.xml" else r)
    else:
        out = io.BytesIO()
        with zipfile.ZipFile(io.BytesIO(clean)) as src, zipfile.ZipFile(out, "w") as dst:
            for info in src.infolist():
                dst.writestr(info.filename, src.read(info.filename))
            import warnings
            with warnings.catch_warnings():
                warnings.simplefilter("ignore")
                dst.writestr("xl/workbook.xml", b"<workbook/>")
        data = out.getvalue()
    found = gate.scan_files([put(tmp_path, data)], register_path)
    assert found == [gate.Finding(str(tmp_path / "register.xlsx"), 0, "opaque")]


def test_a_macro_workbook_stays_opaque_by_its_suffix(tmp_path, register_path):
    assert classes(tmp_path, register_path, book(), "register.xlsm") == {"opaque"}


def test_a_macro_content_type_in_an_xlsx_stays_opaque(tmp_path, register_path):
    def change(name, raw):
        if name == "[Content_Types].xml":
            return raw.replace(b"sheet.main+xml", b"sheet.macroEnabled.main+xml")
        return raw
    assert classes(tmp_path, register_path, rezip(book(), change=change)) == {"opaque"}


def test_a_web_hyperlink_is_read_not_refused(tmp_path, register_path):
    def edit(wb):
        wb.active["A9"] = "docs"
        wb.active["A9"].hyperlink = "https://example.org/docs/register"
    assert classes(tmp_path, register_path, book(edit)) == set()
    assert "homepath" in classes(tmp_path, register_path, book(
        lambda wb: setattr(wb.active["A9"], "hyperlink", "file://" + HOME)))


def test_other_opaque_kinds_keep_their_rule(tmp_path, register_path):
    assert classes(tmp_path, register_path, book(), "register.docx") == {"opaque"}
    assert classes(tmp_path, register_path, book(), "register.zip") == {"opaque"}


# --------------------------------------------------------------------------- the work user's home in evidence scripts


@pytest.fixture
def project(tmp_path, monkeypatch):
    root = tmp_path / "tcp-q7m4"
    (root / "evidence" / "lab").mkdir(parents=True)
    (root / "deliverables").mkdir()
    (root / "SCOPE.md").write_text("goal: a lab\n", encoding="utf-8")
    monkeypatch.setattr(config, "work_user", lambda: WORK)
    return root


def script(path: Path, line: str) -> Path:
    path.write_text("import json\n\n" + line + "\nprint('done')\n", encoding="utf-8")
    return path


def work_home(rest: str) -> str:
    return "/ho" + "me/" + WORK + rest


@pytest.mark.parametrize("line", [
    "OUT = '%s'" % work_home("/tcp-q7m4/evidence/lab/out.json"),
    "cd %s" % work_home(""),
    "LOG = '~" + WORK + "/tcp-q7m4/notes/x.log'",
    "folder -ho" + "me-" + WORK + "-tcp-q7m4",
])
def test_an_evidence_script_may_name_the_work_users_home(project, register_path, line):
    f = script(project / "evidence" / "lab" / "poc.py", line)
    assert gate.scan_files([f], register_path) == []


@pytest.mark.parametrize("line", [
    "OUT = '" + HOME + "'",
    "OUT = '%s'" % ("/ho" + "me/" + WORK + ".old/x"),
    "OUT = '/Us" + "ers/" + WORK + "/x'",
    "OUT = '/ro" + "ot/x'",
])
def test_an_evidence_script_still_refuses_any_other_home(project, register_path, line):
    f = script(project / "evidence" / "lab" / "poc.py", line)
    assert gate.scan_files([f], register_path) == [gate.Finding(str(f), 3, "homepath")]


@pytest.mark.parametrize("where", ["deliverables/plan.md", "evidence/lab/notes.md", "evidence/lab/out.json",
                                   "poc.py"])
def test_every_other_file_keeps_the_rule(project, register_path, where):
    f = script(project / where, "OUT = '%s'" % work_home("/tcp-q7m4/x"))
    assert gate.scan_files([f], register_path) == [gate.Finding(str(f), 3, "homepath")]


def test_an_evidence_folder_outside_a_project_keeps_the_rule(tmp_path, register_path, monkeypatch):
    monkeypatch.setattr(config, "work_user", lambda: WORK)
    (tmp_path / "evidence").mkdir()
    f = script(tmp_path / "evidence" / "poc.py", "OUT = '%s'" % work_home("/x"))
    assert gate.scan_files([f], register_path) == [gate.Finding(str(f), 3, "homepath")]


def test_without_a_work_user_the_home_is_refused(project, register_path, monkeypatch):
    monkeypatch.setattr(config, "work_user", lambda: None)
    f = script(project / "evidence" / "lab" / "poc.py", "OUT = '%s'" % work_home("/x"))
    assert gate.scan_files([f], register_path) == [gate.Finding(str(f), 3, "homepath")]


def test_an_evidence_script_keeps_every_other_class(project, register_path):
    f = script(project / "evidence" / "lab" / "poc.py",
               "OUT = '%s'  # for %s" % (work_home("/x"), CUSTOMER))
    assert gate.scan_files([f], register_path) == [gate.Finding(str(f), 3, "name")]


def test_a_staged_evidence_script_with_the_work_home_commits(project, register_path):
    subprocess.run(["git", "init", "-q", str(project)], check=True)
    script(project / "evidence" / "lab" / "poc.py", "OUT = '%s'" % work_home("/tcp-q7m4/evidence/out.json"))
    subprocess.run(["git", "-C", str(project), "add", "evidence/lab/poc.py", "SCOPE.md"], check=True)
    assert gate.main(["--register", str(register_path), "--staged", "--repo", str(project)]) == 0
