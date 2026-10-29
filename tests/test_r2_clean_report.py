"""R2 of the report design: `awb clean report` brings the codes file of the report workbook to the laptop from a
fake bucket; the report workbook never leaves the laptop (the laptop refusals by folder and by fingerprint, the
hold of `awb import` and its timer). Invented data only, temporary folders, workbooks built with openpyxl."""
from __future__ import annotations

import hashlib
import json
import os
from datetime import datetime, timezone
from pathlib import Path

import pytest

from awb import bucket, clean, cleanfetch, cleanreport, cleanserve, cli, importauto, importcmd, laptop, reportbook
from tests import fixtures as fx
from tests.obs_fake import FakeOBS
from tests.test_import_auto import own, owner_terminal, project, svc  # noqa: F401  (fixtures)

PROJECT = "tcp-q7m4"
HEADER = ",".join('"%s"' % h for h in cleanreport.HEADER)
WRITTEN = "2026-10-29T07:50:00Z"
NOW = datetime(2026, 10, 29, 9, 55, tzinfo=timezone.utc)


def _csv(rows: list[tuple[str, ...]]) -> bytes:
    lines = [HEADER] + [",".join('"%s"' % c for c in r) for r in rows]
    return ("\ufeff" + "\r\n".join(lines) + "\r\n").encode("utf-8")


ROWS = [("tcp-q7m4", fx.CUSTOMER_CODE, "move two app clusters to managed k8s", "price the target", "owner",
         "2026-10-28"),
        ("tcp-r2n5", "none", cleanreport.WITHHELD, "lab run of the vpn", "session", "2026-10-27")]


def _meta(data: bytes, **extra) -> bytes:
    meta = {"written": WRITTEN, "rows": 2, "withheld": 1, "sha256": hashlib.sha256(data).hexdigest(),
            "release": "test"}
    meta.update(extra)
    return json.dumps(meta).encode("utf-8")


def _workbook(path: Path, sheets=("Projects", "Names", "Status")) -> Path:
    openpyxl = pytest.importorskip("openpyxl")
    wb = openpyxl.Workbook()
    wb.active.title = sheets[0]
    for name in sheets[1:]:
        wb.create_sheet(name)
    if "Names" in sheets:
        wb["Names"].append([fx.CUSTOMER_CODE, fx.CUSTOMER_FORMS[0]])
    path.parent.mkdir(parents=True, exist_ok=True)
    wb.save(path)
    return path


@pytest.fixture
def lap(tmp_path, monkeypatch):
    monkeypatch.setenv(laptop.HOME_ENV, str(tmp_path / "laptop"))
    monkeypatch.setenv(laptop.WORK_ENV, str(tmp_path / "work"))
    for name in laptop.PUT_ENV + laptop.PUT_OPTIONAL_ENV:
        monkeypatch.delenv(name, raising=False)
    laptop.init()
    return tmp_path


@pytest.fixture
def bkt():
    with FakeOBS() as f:
        data = _csv(ROWS)
        f.objects["reports/projects.csv"] = data
        f.objects["reports/projects.meta.json"] = _meta(data)
        yield f


def _fetch(bkt, folder, **kw):
    lines = []
    kw.setdefault("now", NOW)
    kw.setdefault("sleep", lambda s: None)
    rows = cleanreport.fetch(folder, client=bkt.client(), say=lines.append, **kw)
    return rows, lines


def _register():
    it = iter([f + "\n" for f in fx.CUSTOMER_FORMS] + ["\n"])
    laptop.ask_forms(fx.CUSTOMER_CODE, read=lambda: next(it, ""), tty=True, say=lambda s: None)


# --------------------------------------------------------------------------- the fingerprint


def test_r2_the_fingerprint_is_both_sheets_names_and_projects_whatever_the_suffix(tmp_path):
    book = _workbook(tmp_path / "report.xlsx")
    assert reportbook.is_report_workbook(book)
    renamed = tmp_path / "copy.bin"
    renamed.write_bytes(book.read_bytes())
    assert reportbook.is_report_workbook(renamed), "a renamed copy keeps its fingerprint"
    # the check can fail: one of the two sheets alone, another workbook, a text, a missing file
    assert not reportbook.is_report_workbook(_workbook(tmp_path / "a.xlsx", ("Projects", "Status")))
    assert not reportbook.is_report_workbook(_workbook(tmp_path / "b.xlsx", ("Names", "Prices")))
    (tmp_path / "c.txt").write_text("names and projects\n", encoding="utf-8")
    assert not reportbook.is_report_workbook(tmp_path / "c.txt")
    assert not reportbook.is_report_workbook(tmp_path / "missing.xlsx")
    assert not reportbook.is_report_workbook(tmp_path)


# --------------------------------------------------------------------------- awb clean report


def test_r2_report_downloads_checks_and_prints_the_line(lap, bkt):
    folder = lap / "win" / "awb-report"
    rows, lines = _fetch(bkt, folder)
    assert rows == 2
    assert lines[0].startswith("projects.csv: 2 rows, 1 text withheld, written 2026-10-29 07:50 UTC (2 h old)")
    assert "report.xlsx" in lines[0] and len(lines) == 1
    assert (folder / "projects.csv").read_bytes() == bkt.objects["reports/projects.csv"]
    assert sorted(p.name for p in folder.iterdir()) == ["projects.csv"], "no part file, no sidecar, no previous yet"
    assert {m for m, _ in bkt.calls} <= {"GET", "HEAD"}, "it never uploads"
    assert all("reports/projects." in path for _, path in bkt.calls)


def test_r2_a_second_report_keeps_the_previous_and_replaces_the_file(lap, bkt):
    folder = lap / "awb-report"
    _fetch(bkt, folder)
    old = (folder / "projects.csv").read_bytes()
    _, lines = _fetch(bkt, folder)
    assert ", unchanged. " in lines[0]
    assert not (folder / "projects.previous.csv").exists(), "an unchanged file rotates nothing"
    new = _csv(ROWS[:1])
    bkt.objects["reports/projects.csv"] = new
    bkt.objects["reports/projects.meta.json"] = _meta(new)
    rows, lines = _fetch(bkt, folder)
    assert rows == 1 and "1 row, 0 texts withheld" in lines[0] and "replaced" in lines[0]
    assert (folder / "projects.csv").read_bytes() == new
    assert (folder / "projects.previous.csv").read_bytes() == old
    assert sorted(p.name for p in folder.iterdir()) == ["projects.csv", "projects.previous.csv"]


def test_r2_a_wrong_sha256_replaces_nothing(lap, bkt):
    folder = lap / "awb-report"
    _fetch(bkt, folder)
    old = (folder / "projects.csv").read_bytes()
    bkt.objects["reports/projects.csv"] = _csv(ROWS[:1])           # the sidecar still names the old sha256
    with pytest.raises(cleanreport.ReportError, match="does not match"):
        _fetch(bkt, folder)
    assert (folder / "projects.csv").read_bytes() == old
    assert sorted(p.name for p in folder.iterdir()) == ["projects.csv"]
    bkt.objects["reports/projects.meta.json"] = json.dumps({"written": WRITTEN}).encode()
    with pytest.raises(cleanreport.ReportError, match="no sha256"):
        _fetch(bkt, folder)


def test_r2_a_missing_codes_file_names_the_host_step(lap, bkt):
    del bkt.objects["reports/projects.meta.json"]
    with pytest.raises(cleanreport.ReportError, match="awb report --codes --put"):
        _fetch(bkt, lap / "awb-report")
    assert not list((lap / "awb-report").iterdir())


def test_r2_excel_holding_the_file_gets_three_tries_then_the_line(lap, bkt, monkeypatch):
    folder = lap / "awb-report"
    _fetch(bkt, folder)
    old = (folder / "projects.csv").read_bytes()
    new = _csv(ROWS[:1])
    bkt.objects["reports/projects.csv"] = new
    bkt.objects["reports/projects.meta.json"] = _meta(new)
    real, tries, slept = os.replace, [], []

    def held(src, dst):
        if Path(dst).name == "projects.csv":
            tries.append(dst)
            raise PermissionError(13, "held")
        return real(src, dst)

    monkeypatch.setattr(cleanreport.os, "replace", held)
    with pytest.raises(cleanreport.Held, match="Excel holds the file, run it again"):
        _fetch(bkt, folder, sleep=slept.append)
    assert len(tries) == 3 and slept == [2.0, 2.0]
    assert (folder / "projects.csv").read_bytes() == old
    assert sorted(p.name for p in folder.iterdir()) == ["projects.csv"], "no part file and no previous left"

    tries.clear()

    def once(src, dst):
        if Path(dst).name == "projects.csv" and not tries:
            tries.append(dst)
            raise PermissionError(13, "held")
        return real(src, dst)

    monkeypatch.setattr(cleanreport.os, "replace", once)
    _fetch(bkt, folder)
    assert (folder / "projects.csv").read_bytes() == new and (folder / "projects.previous.csv").read_bytes() == old


def test_r2_the_command_saves_the_folder_and_takes_a_windows_path(lap, bkt, monkeypatch, capsys):
    monkeypatch.setattr(clean, "_client", lambda what="--put": bkt.client())
    target = lap / "mnt-c" / "awb-report"
    monkeypatch.setattr(clean, "windows_path", lambda s: str(target) if s.startswith("C:") else s)
    assert cli.main(["clean", "report"]) == 2
    assert "--dir DIR --save" in capsys.readouterr().err
    assert cli.main(["clean", "report", "--save"]) == 2
    assert cli.main(["clean", "report", "--dir", "C:\\Users\\me\\awb-report", "--save"]) == 0
    assert "projects.csv: 2 rows" in capsys.readouterr().out
    assert cleanreport.saved_dir() == target and (target / "projects.csv").is_file()
    os.remove(target / "projects.csv")
    assert cli.main(["clean", "report"]) == 0 and (target / "projects.csv").is_file()


def test_r2_the_header_is_pinned_to_the_six_names():
    assert cleanreport.HEADER == ("project code", "customer code", "task description", "next step", "whose move",
                                  "date of last change")


# --------------------------------------------------------------------------- the laptop refusals


def test_r2_awb_clean_refuses_the_workbook_and_the_report_folder(lap, monkeypatch, capsys):
    _register()
    from awb import intake

    monkeypatch.setattr(intake, "selftest", lambda: pytest.fail("the refusal comes before the intake"))
    book = _workbook(lap / "elsewhere" / "status copy.xlsx")
    with pytest.raises(clean.CleanError, match=reportbook.REFUSED):
        clean.run([book], PROJECT, "docs", lap / "out")
    note = lap / "awb-report" / "note.txt"
    note.parent.mkdir(parents=True, exist_ok=True)
    note.write_text("plain note\n", encoding="utf-8")
    with pytest.raises(clean.CleanError, match=reportbook.REFUSED):
        clean.run([note], PROJECT, "logs", lap / "out")
    saved = lap / "Reports"
    saved.mkdir()
    cleanreport.save_dir(saved)
    inside = saved / "projects.csv"
    inside.write_text("x\n", encoding="utf-8")
    assert cleanreport.refused(inside) and cleanreport.refused(lap / "reports" / "projects.csv")
    assert cli.main(["clean", str(inside), "--project", PROJECT, "--out", str(lap / "out")]) == 2
    err = capsys.readouterr().err
    assert reportbook.REFUSED in err and "projects.csv" not in err
    # the refusal can fail: an ordinary workbook and a file outside the folder pass it
    plain = _workbook(lap / "elsewhere" / "plain.xlsx", ("Projects", "Status"))
    clean._refuse_report([plain, note.parent.parent / "elsewhere" / "plain.xlsx"])
    assert not cleanreport.refused(plain)


def test_r2_the_puts_refuse_the_workbook_and_the_report_folder(lap):
    class NoBucket:
        def __getattr__(self, name):
            pytest.fail("the refusal comes before the bucket")

    book = _workbook(lap / "work" / PROJECT / "clean" / "book.clean.md")      # a workbook under a clean name
    with pytest.raises(clean.CleanError, match=reportbook.REFUSED):
        clean.put_files(PROJECT, [book], client=NoBucket())
    copy = lap / "awb-report" / "x.clean.md"
    copy.parent.mkdir(parents=True, exist_ok=True)
    copy.write_text("clean\n", encoding="utf-8")
    with pytest.raises(clean.CleanError, match=reportbook.REFUSED):
        clean.put_files(PROJECT, [copy], client=NoBucket())


def test_r2_the_page_refuses_an_uploaded_workbook_and_deletes_the_upload(lap):
    _register()
    laptop.use(PROJECT)
    data = _workbook(lap / "src" / "r.xlsx").read_bytes()
    state = cleanserve.State()
    cleanserve.run_page(state, {"kind": "auto", "paths": ""}, [("anything.xlsx", data)])
    assert reportbook.REFUSED in state.message and not state.copies
    assert not [p for p in (lap / "laptop" / "tmp").iterdir() if p.name.startswith("upload-")]
    assert not list((lap / "work" / PROJECT / "clean").glob("*.clean.md"))


def test_r2_fetch_refuses_the_report_folder(lap):
    with pytest.raises(cleanfetch.FetchError, match="report folder"):
        cleanfetch.fetch(PROJECT, lap / "awb-report", client=object())


# --------------------------------------------------------------------------- the hold on the host


def _text(path: Path) -> Path:
    path.write_text("The pilot ran on KVM.\n", encoding="utf-8")
    return path


def test_r2_import_holds_the_workbook_mails_once_and_takes_the_rest(home, project, monkeypatch, capsys):
    monkeypatch.setattr(bucket, "client", lambda: (_ for _ in ()).throw(bucket.BucketError("no bucket key here")))
    mails = []
    monkeypatch.setattr(importcmd, "_send_mail", lambda s, m: mails.append((s, m)) or True)
    book = _workbook(home.inbox / ("status-%s.xlsx" % fx.CUSTOMER_FORMS[1].lower()))
    _text(home.inbox / "notes.txt")
    assert importcmd.run(home, project.code, []) == 0
    out = capsys.readouterr()
    text = out.out + out.err
    assert "held: %s" % reportbook.REFUSED in text and "files taken: 1" in text
    assert "status-" not in text and fx.CUSTOMER_FORMS[1].lower() not in text.lower()
    assert book.is_file(), "the workbook is not imported and not moved"
    assert len(mails) == 1 and project.code in mails[0][0] and fx.CUSTOMER_FORMS[0] not in mails[0][1]
    assert importcmd.run(home, project.code, []) == importcmd.EXIT_HELD, "only the workbook is left"
    assert len(mails) == 1, "one mail per file"
    assert book.is_file()


def test_r2_import_auto_leaves_the_workbook_in_in(svc, own, home, project, monkeypatch):
    mails = []
    monkeypatch.setattr(importcmd, "_send_mail", lambda s, m: mails.append(s) or True)
    base = "%s/%s/" % (project.created[:7], project.code)
    for key in (base, base + "in/", base + "out/"):
        own.objects[key] = b""
    book = _workbook(home.vault / "tmp-book.xlsx")
    own.objects[base + "in/report.xlsx"] = book.read_bytes()
    book.unlink()
    own.objects[base + "in/raw.md"] = b"The pilot ran on KVM.\n"
    assert importauto.run(home) == 0
    assert base + "in/report.xlsx" in own.objects, "the held workbook stays in in/"
    assert base + "in/raw.md" not in own.objects, "the other file was taken and dropped"
    assert len(mails) == 1


def test_r2_the_rule_file_names_the_line_and_its_walls():
    text = (Path(cleanreport.__file__).resolve().parent.parent / "rules" / "report-workbook.txt").read_text(
        encoding="utf-8")
    text = " ".join(text.split())
    assert reportbook.REFUSED in text
    for wall in ("awb clean FILE", "awb clean serve", "awb clean fetch", "awb import", "Names"):
        assert wall in text
