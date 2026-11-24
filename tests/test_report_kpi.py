"""Task 7 of presentations/reports-design: `awb report --codes` writes the KPI file projects-kpi.csv and its sidecar
beside projects.csv, by the same route (work side to the shared side, owner side checks again and puts to
reports/), and `awb clean report` brings it to the laptop. projects.csv stays six columns. Invented projects in a
temporary Workbench and its temporary vault, a temporary ledger, the invented names of tests/fixtures.py only."""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
import stat
from datetime import datetime, timezone
from pathlib import Path

import pytest

from awb import cleanreport, cli, ledger, ownerwait, projects, reportcodes, reportkpi, review
from awb.tcp import keys
from tests import fixtures
from tests.obs_fake import FakeOBS
from tests.test_r1_report_codes import HEADER as CODES_HEADER, NAME, lines_of, own, plant, planted, run, svc  # noqa: F401
from tests.test_r1b_codes_reader import not_as_root, folders_of, unreadable

CUST = fixtures.CUSTOMER_CODE
KPI_HEADER = ('"project code","customer code","kind","goal","start date","close date","state",'
              '"tags and technologies","ledger entries","main deliverables"')


def _touch(path: Path, day: str) -> None:
    t = dt.datetime.fromisoformat(day + "T12:00:00").timestamp()
    os.utime(path, (t, t))


@pytest.fixture
def kpi(planted):
    """The six projects of test_r1 plus one closed before START, tags in a scope, deliverables, ledger entries and
    the fingerprint of a close."""
    home = planted
    rows = projects.load(home)
    old = plant(home, "tcp-m7vd", projects.NO_CUSTOMER, "closed", "an older question", "nothing", "2026-08-20",
                "2026-08-20")
    old = projects.Project(old.code, "query", old.customer, old.platform, old.path, old.memory_key, old.state,
                           "2026-08-10")
    projects._save(home, rows + [old])
    scope = home.projects_root / "tcp-q7m4" / "SCOPE.md"
    scope.write_text(scope.read_text(encoding="utf-8") + "- tags: ecs, cbr\n", encoding="utf-8")
    made = home.projects_root / "tcp-q7m4" / "deliverables"
    made.mkdir()
    for name in ("plan.pdf", "sizing.xlsx", "notes.md", "answer.md"):
        (made / name).write_text("x\n", encoding="utf-8")
    ledger.add("proof", "first lab built", project="tcp-q7m4", customer=CUST, tags=["ecs", "obs"], day="2026-10-02",
               p=home)
    ledger.add("pricing", "sizing sent", project="tcp-q7m4", customer=CUST, tags=["cce"], day="2026-10-20", p=home)
    ledger.add("inquiry", "disk move answered", project="tcp-kx2a", tags=["evs"], day="2026-10-22", p=home)
    ledger.add("inquiry", "an answer before the start", project="tcp-m7vd", day="2026-08-15", p=home)
    fp = review.fingerprint_path(home, "tcp-kx2a")
    fp.parent.mkdir(parents=True, exist_ok=True)
    fp.write_text("{}\n", encoding="utf-8")
    _touch(fp, "2026-10-23")
    return home


ROWS = [
    '"tcp-b3np","%s","project","move of 40 virtual machines to TCP","2026-10-20","","active","","0","none"' % CUST,
    '"tcp-c4rt","%s","project","tender text for a managed database","2026-10-14","","parked","","0","none"' % CUST,
    '"tcp-grvz","%s","project","an old proof","2026-09-01","2026-09-01","closed","","0","none"' % CUST,
    '"tcp-kx2a","none","project","can a disk move between zones","2026-10-22","2026-10-23","closed","evs","1",'
    '"none"',
    '"tcp-q7m4","%s","project","lab run of the vendor image","2026-10-28","","active","cbr, cce, ecs, obs","2",'
    '"md 2, pdf 1, xlsx 1"' % CUST,
    '"tcp-wqyz","%s","project","a deleted one","2026-10-28","2026-10-28","deleted","","0","none"' % CUST,
]


def test_kpi_the_ten_headers_are_pinned_and_the_six_stay_as_they_are():
    assert reportkpi.HEADERS == ("project code", "customer code", "kind", "goal", "start date", "close date",
                                 "state", "tags and technologies", "ledger entries", "main deliverables")
    assert cleanreport.KPI_HEADER == reportkpi.HEADERS, "the laptop checks the same ten names"
    assert reportkpi.render([]).decode("utf-8") == "﻿" + KPI_HEADER + "\r\n"
    assert reportcodes.HEADERS == ("project code", "customer code", "task description", "next step", "whose move",
                                   "date of last change")


def test_kpi_one_row_per_project_since_the_start_closed_and_deleted_too(kpi, tmp_path, capsys):
    out = tmp_path / "rep" / "projects.csv"
    out.parent.mkdir()
    code, stdout, err = run(["report", "--codes", "--out", str(out)], capsys)
    assert code == 0, err
    assert len(lines_of(out)) == 5 and lines_of(out)[0] == CODES_HEADER, "projects.csv keeps its four rows"
    kfile = out.with_name("projects-kpi.csv")
    rows = lines_of(kfile)
    assert rows[0] == KPI_HEADER and rows[1:] == ROWS, "the project closed before the start is left out"
    assert "projects-kpi.csv written, 6 rows, 0 texts withheld" in stdout
    meta = json.loads(out.with_name("projects-kpi.meta.json").read_text(encoding="utf-8"))
    assert meta["file"] == "projects-kpi.csv" and meta["rows"] == 6 and meta["withheld"] == 0
    assert meta["sha256"] == hashlib.sha256(kfile.read_bytes()).hexdigest() and meta["release"]
    assert meta["written"].endswith("Z")
    assert json.loads(out.with_name("projects.meta.json").read_text(encoding="utf-8"))["file"] == "projects.csv"
    fixtures.assert_no_fixture_name(kfile.read_text(encoding="utf-8") + stdout + err, "the KPI file")


def test_kpi_a_text_with_a_name_or_a_detector_hit_is_withheld(kpi, tmp_path, capsys, monkeypatch):
    scope = kpi.projects_root / "tcp-b3np" / "SCOPE.md"
    scope.write_text(scope.read_text(encoding="utf-8").replace("move of 40", "move for " + NAME), encoding="utf-8")
    real = ledger._detectors
    monkeypatch.setattr(ledger, "_detectors", lambda: dict(real(), planted=lambda t: ["x"] if "xlsx" in t else []))
    out = tmp_path / "projects.csv"
    code, stdout, err = run(["report", "--codes", "--out", str(out)], capsys)
    assert code == 0, err
    rows = {r.split('","')[0].strip('"'): r.split('","') for r in lines_of(tmp_path / "projects-kpi.csv")[1:]}
    assert rows["tcp-b3np"][3] == ledger.WITHHELD
    assert rows["tcp-q7m4"][9].rstrip('"') == ledger.WITHHELD and rows["tcp-q7m4"][7] == "cbr, cce, ecs, obs"
    assert "projects-kpi.csv written, 6 rows, 2 texts withheld" in stdout
    assert json.loads((tmp_path / "projects-kpi.meta.json").read_text(encoding="utf-8"))["withheld"] == 2
    fixtures.assert_no_fixture_name((tmp_path / "projects-kpi.csv").read_text(encoding="utf-8") + stdout + err,
                                    "the KPI file")


def test_kpi_out_named_like_the_kpi_file_is_refused(kpi, tmp_path, capsys):
    code, _, err = run(["report", "--codes", "--out", str(tmp_path / "projects-kpi.csv")], capsys)
    assert code == 2 and "--out names projects-kpi.csv itself" in err


@not_as_root
def test_kpi_the_sealed_split_hands_the_file_over_and_the_owner_side_checks_it(kpi, tmp_path, capsys):
    code, stdout, err = run(["report", "--codes", "--to-shared"], capsys)
    assert code == 0, err
    handed = Path(kpi.shared) / "report-codes" / "projects-kpi.csv"
    assert lines_of(handed)[1:] == ROWS
    for f in (handed, handed.with_name("projects-kpi.meta.json")):
        assert stat.S_IMODE(f.stat().st_mode) == 0o640, f
    out = tmp_path / "own" / "projects.csv"
    out.parent.mkdir()
    with unreadable(*folders_of(kpi)):
        code, stdout, err = run(["report", "--codes", "--from-shared", "--out", str(out)], capsys)
    assert code == 0, err
    assert lines_of(out.with_name("projects-kpi.csv"))[1:] == ROWS

    text = handed.read_bytes().decode("utf-8")
    text = (text.replace("lab run of the vendor image", "lab run for " + NAME)
            .replace('"tcp-c4rt","%s","project"' % CUST, '"tcp-c4rt","none","ask"')
            + '"tcp-h6tw","none","query","a planted row","2026-10-01","","active","","0","none"\r\n')
    handed.write_bytes(text.encode("utf-8"))
    code, stdout, err = run(["report", "--codes", "--from-shared", "--out", str(out)], capsys)
    assert code == 0, err
    rows = lines_of(out.with_name("projects-kpi.csv"))[1:]
    assert [r.split('","')[0].strip('"') for r in rows] == [r.split('","')[0].strip('"') for r in ROWS]
    assert rows[1] == ROWS[1], "customer code and kind come from the register"
    assert rows[4].split('","')[3] == ledger.WITHHELD and "projects-kpi.csv written, 6 rows, 1 text withheld" in stdout
    fixtures.assert_no_fixture_name(out.with_name("projects-kpi.csv").read_text(encoding="utf-8") + stdout + err,
                                    "the KPI file")


@pytest.mark.parametrize("change, words", [
    (lambda t: t.replace("main deliverables", "main results"), "another header"),
    (lambda t: t.replace('"evs","1"', '"evs","one"'), "no count of ledger entries"),
    (lambda t: t.replace('"2026-10-23"', '"23.10.2026"'), "no ISO close date"),
    (lambda t: t.replace('"md 2, pdf 1, xlsx 1"', '"two reports"'), "deliverables of another form"),
    (lambda t: t.replace('"cbr, cce, ecs, obs"', '"cbr; cce"'), "tags of another form"),
    (lambda t: t.replace('"tcp-b3np"', '"tcp-q7m4"'), "a second one"),
])
def test_kpi_a_file_the_owner_side_cannot_trust_is_refused_and_projects_csv_still_goes(kpi, tmp_path, capsys,
                                                                                       change, words):
    assert run(["report", "--codes", "--to-shared"], capsys)[0] == 0
    handed = Path(kpi.shared) / "report-codes" / "projects-kpi.csv"
    handed.write_bytes(change(handed.read_bytes().decode("utf-8")).encode("utf-8"))
    out = tmp_path / "projects.csv"
    code, _, err = run(["report", "--codes", "--from-shared", "--out", str(out)], capsys)
    assert code == 2 and words in err and "projects-kpi.csv" in err
    assert out.is_file() and not (tmp_path / "projects-kpi.csv").exists()


def test_kpi_no_file_of_the_work_side_yet_is_one_line_and_no_failure(kpi, tmp_path, capsys):
    assert run(["report", "--codes", "--to-shared"], capsys)[0] == 0
    (Path(kpi.shared) / "report-codes" / "projects-kpi.csv").unlink()
    code, stdout, err = run(["report", "--codes", "--from-shared", "--out", str(tmp_path / "projects.csv")], capsys)
    assert code == 0, err
    assert "projects-kpi.csv skipped, the work side has written none yet" in stdout
    assert (tmp_path / "projects.csv").is_file() and not (tmp_path / "projects-kpi.csv").exists()


def test_kpi_put_sends_both_kpi_files_to_reports_and_if_changed_keeps_them(kpi, svc, own, tmp_path, capsys):
    folder = ownerwait.report_dir()
    folder.mkdir(parents=True)
    code, stdout, err = run(["report", "--codes", "--put", "--if-changed"], capsys)
    assert code == 0, err
    assert own.objects["reports/projects-kpi.csv"] == (folder / "projects-kpi.csv").read_bytes()
    assert json.loads(own.objects["reports/projects-kpi.meta.json"])["rows"] == 6
    assert "put reports/projects-kpi.csv" in stdout and "put reports/projects-kpi.meta.json" in stdout
    assert reportcodes.read_state(folder / "kpi-last.json")["state"] == "put"
    assert reportcodes.read_state()["state"] == "put" and ownerwait.report_items() == []
    code, stdout, _ = run(["report", "--codes", "--put", "--if-changed"], capsys)
    assert code == 0 and "projects-kpi.csv unchanged, 6 rows" in stdout and "put " not in stdout
    f = tmp_path / "x.csv"
    f.write_bytes(b"x")
    for key in ("reports/projects-kpi.txt", "reports/kpi.csv"):
        assert keys.request(svc.admin_path, {"op": "bucket_put", "key": key}, upload=f)["ok"] is False, key
    assert keys.request(svc.call_path, {"op": "bucket_put", "key": "reports/projects-kpi.csv"},
                        upload=f)["ok"] is False, "a session's call never reaches reports/"


# --------------------------------------------------------------------------- the laptop


NOW = datetime(2026, 10, 29, 9, 55, tzinfo=timezone.utc)


def _bucket_csv(rows: list[str]) -> bytes:
    return ("﻿" + "\r\n".join([KPI_HEADER] + rows) + "\r\n").encode("utf-8")


def _bucket_meta(data: bytes) -> bytes:
    return json.dumps({"file": "projects-kpi.csv", "written": "2026-10-29T07:50:00Z", "rows": 2, "withheld": 0,
                       "sha256": hashlib.sha256(data).hexdigest(), "release": "test"}).encode("utf-8")


@pytest.fixture
def lap(tmp_path, monkeypatch):
    from awb import laptop

    monkeypatch.setenv(laptop.HOME_ENV, str(tmp_path / "laptop"))
    monkeypatch.setenv(laptop.WORK_ENV, str(tmp_path / "work"))
    for name in laptop.PUT_ENV + laptop.PUT_OPTIONAL_ENV:
        monkeypatch.delenv(name, raising=False)
    laptop.init()
    return tmp_path


def test_kpi_the_laptop_brings_the_file_keeps_the_previous_and_says_when_there_is_none(lap):
    folder = lap / "awb-report"
    lines: list[str] = []
    kw = dict(now=NOW, sleep=lambda s: None, say=lines.append)
    with FakeOBS() as bkt:
        assert cleanreport.fetch_kpi(folder, client=bkt.client(), **kw) is None
        assert lines == ["projects-kpi.csv is not in the bucket yet: the host puts it with awb report --codes --put"]
        first = _bucket_csv(ROWS[:2])
        bkt.objects["reports/projects-kpi.csv"] = first
        bkt.objects["reports/projects-kpi.meta.json"] = _bucket_meta(first)
        lines.clear()
        assert cleanreport.fetch_kpi(folder, client=bkt.client(), **kw) == 2
        assert lines[0].startswith("projects-kpi.csv: 2 rows, 0 texts withheld, written 2026-10-29 07:50 UTC")
        assert len(lines) == 1 and (folder / "projects-kpi.csv").read_bytes() == first
        second = _bucket_csv(ROWS)
        bkt.objects["reports/projects-kpi.csv"] = second
        bkt.objects["reports/projects-kpi.meta.json"] = _bucket_meta(second)
        assert cleanreport.fetch_kpi(folder, client=bkt.client(), **kw) == 6
        assert (folder / "projects-kpi.previous.csv").read_bytes() == first
        bkt.objects["reports/projects-kpi.csv"] = first
        with pytest.raises(cleanreport.ReportError, match="sha256 of projects-kpi.csv"):
            cleanreport.fetch_kpi(folder, client=bkt.client(), **kw)
        assert (folder / "projects-kpi.csv").read_bytes() == second
        assert {m for m, _ in bkt.calls} <= {"GET", "HEAD"}, "it never uploads"
    assert sorted(p.name for p in folder.iterdir()) == ["projects-kpi.csv", "projects-kpi.previous.csv"]


def test_kpi_awb_clean_report_brings_both_files(lap, monkeypatch, capsys):
    from awb import clean

    six = ("﻿" + CODES_HEADER + "\r\n").encode("utf-8")
    ten = _bucket_csv(ROWS)
    with FakeOBS() as bkt:
        bkt.objects["reports/projects.csv"] = six
        bkt.objects["reports/projects.meta.json"] = json.dumps(
            {"written": "2026-10-29T07:50:00Z", "sha256": hashlib.sha256(six).hexdigest()}).encode("utf-8")
        bkt.objects["reports/projects-kpi.csv"] = ten
        bkt.objects["reports/projects-kpi.meta.json"] = _bucket_meta(ten)
        monkeypatch.setattr(clean, "_client", lambda what="--put": bkt.client())
        target = lap / "awb-report"
        assert cli.main(["clean", "report", "--dir", str(target), "--save"]) == 0
        out = capsys.readouterr().out
    assert "projects.csv: 0 rows" in out and "projects-kpi.csv: 6 rows" in out
    assert (target / "projects-kpi.csv").read_bytes() == ten and (target / "projects.csv").read_bytes() == six
