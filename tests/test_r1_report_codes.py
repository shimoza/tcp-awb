"""Task R1 of presentations/reports-design: `awb report --codes` writes the codes file of the owner's report workbook
(projects.csv and projects.meta.json), puts both under reports/ of the owner bucket through the key service and runs
every hour from the owner's timer awb-report-codes. Invented projects in a temporary Workbench (the `home` fixture)
and its temporary vault, the invented names of tests/fixtures.py only."""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
import subprocess
from pathlib import Path

import pytest

from awb import bucket, cli, ledger, ownerwait, period, projectfacts, projects, reportcodes
from awb.tcp import keys
from tests import fixtures
from tests.test_bucket_keys import AK, SK, STORE, quiet, short_dir  # noqa: F401
from tests.test_seal import SETUP, VERIFY, WORK_JOBS, _persist_run, commands, fakebin, no_marks, run_script  # noqa: F401
from tests.obs_fake import FakeOBS

TODAY = dt.date(2026, 10, 29)
NAME = fixtures.CUSTOMER_FORMS[0]
HEADER = '"project code","customer code","task description","next step","whose move","date of last change"'


def _git(folder: Path, *args: str, day: str | None = None) -> None:
    env = dict(os.environ)
    if day:
        env["GIT_AUTHOR_DATE"] = env["GIT_COMMITTER_DATE"] = "%sT10:00:00" % day
    subprocess.run(["git", "-c", "user.name=awb", "-c", "user.email=awb@localhost", "-c", "commit.gpgsign=false",
                    "-c", "core.hooksPath=/dev/null", "-C", str(folder), *args], check=True, capture_output=True,
                   env=env)


def _touch(path: Path, day: str) -> None:
    t = dt.datetime.fromisoformat(day + "T12:00:00").timestamp()
    os.utime(path, (t, t))


def plant(home, code: str, customer: str, state: str, goal: str, nxt: str, commit_day: str | None, state_day: str,
          owner_item: bool = False) -> projects.Project:
    folder = home.projects_root / code
    folder.mkdir(parents=True)
    (folder / "SCOPE.md").write_text("# Scope of %s\n\n- code: %s\n- goal: %s\n" % (code, code, goal), encoding="utf-8")
    (folder / "STATE.md").write_text("# State of %s\n\nStatus: work goes on.\nNext: %s\n" % (code, nxt),
                                     encoding="utf-8")
    (folder / "OPEN.md").write_text("# Open\n\n## For the owner\n\n%s" % ("- decide the retention\n" if owner_item else ""),
                                    encoding="utf-8")
    if commit_day:
        _git(folder, "init", "-q")
        _git(folder, "add", "-A")
        _git(folder, "commit", "-q", "-m", "step", day=commit_day)
    _touch(folder / "STATE.md", state_day)
    return projects.Project(code, "project", customer, "tcp", str(folder), projects.memory_key(folder), state,
                            commit_day or state_day)


@pytest.fixture
def planted(home, monkeypatch):
    monkeypatch.setattr(ledger, "today", lambda: TODAY)
    cust = fixtures.CUSTOMER_CODE
    rows = [
        plant(home, "tcp-q7m4", cust, "active", "lab run of the vendor image", "review the mapping draft",
              "2026-10-28", "2026-10-20", owner_item=True),
        plant(home, "tcp-b3np", cust, "active", "move of 40 virtual machines to TCP", "price estimate for phase 2",
              "2026-10-20", "2026-10-27"),
        plant(home, "tcp-c4rt", cust, "parked", "tender text for a managed database", "waits on the answer",
              "2026-10-14", "2026-10-14"),
        plant(home, "tcp-kx2a", projects.NO_CUSTOMER, "closed", "can a disk move between zones", "close the query",
              None, "2026-10-22"),
        plant(home, "tcp-grvz", cust, "closed", "an old proof", "nothing", "2026-09-01", "2026-09-01"),
        plant(home, "tcp-wqyz", cust, "deleted", "a deleted one", "nothing", "2026-10-28", "2026-10-28"),
    ]
    projects._save(home, rows)
    return home


def run(argv, capsys):
    code = cli.main(argv)
    out, err = capsys.readouterr()
    return code, out, err


def lines_of(path: Path) -> list[str]:
    raw = path.read_bytes()
    assert raw.startswith(b"\xef\xbb\xbf"), "no byte order mark"
    text = raw.decode("utf-8")[1:]
    assert text.endswith("\r\n") and "\n" not in text.replace("\r\n", "")
    return text.split("\r\n")[:-1]


def test_r1_the_six_headers_are_pinned():
    """The header is a contract with the workbook's query: these six names, this order, this spelling."""
    assert reportcodes.HEADERS == ("project code", "customer code", "task description", "next step", "whose move",
                                   "date of last change")
    assert reportcodes.render([]).decode("utf-8") == "﻿" + HEADER + "\r\n"


def test_r1_the_file_has_one_quoted_row_per_project_sorted_by_code(planted, tmp_path, capsys):
    out = tmp_path / "rep" / "projects.csv"
    out.parent.mkdir()
    code, stdout, err = run(["report", "--codes", "--out", str(out)], capsys)
    assert code == 0, err
    assert "4 rows, 0 texts withheld" in stdout
    rows = lines_of(out)
    assert rows[0] == HEADER
    cust = fixtures.CUSTOMER_CODE
    assert rows[1:] == [
        '"tcp-b3np","%s","move of 40 virtual machines to TCP","price estimate for phase 2","session","2026-10-27"' % cust,
        '"tcp-c4rt","%s","tender text for a managed database","waits on the answer","customer","2026-10-14"' % cust,
        '"tcp-kx2a","none","can a disk move between zones","close the query","closed","2026-10-22"',
        '"tcp-q7m4","%s","lab run of the vendor image","review the mapping draft","owner","2026-10-28"' % cust,
    ]
    meta = json.loads((tmp_path / "rep" / "projects.meta.json").read_text(encoding="utf-8"))
    assert meta["rows"] == 4 and meta["withheld"] == 0 and meta["file"] == "projects.csv"
    assert meta["sha256"] == hashlib.sha256(out.read_bytes()).hexdigest()
    assert dt.datetime.strptime(meta["written"], "%Y-%m-%dT%H:%M:%SZ") and meta["release"]
    fixtures.assert_no_fixture_name(out.read_text(encoding="utf-8") + stdout + err, "the codes file")


def test_r1_a_quote_in_a_text_is_doubled_and_a_cell_never_breaks_the_row():
    row = reportcodes.Row("tcp-q7m4", "none", 'the "fast" path,\tnow', "=1+1", "session", "2026-10-28")
    text = reportcodes.render([row]).decode("utf-8")
    assert text.split("\r\n")[1] == '"tcp-q7m4","none","the ""fast"" path, now","=1+1","session","2026-10-28"'


def test_r1_a_text_with_a_name_or_a_detector_hit_is_withheld(planted, tmp_path, capsys, monkeypatch):
    scope = planted.projects_root / "tcp-b3np" / "SCOPE.md"
    scope.write_text(scope.read_text(encoding="utf-8").replace("move of 40", "move for " + NAME), encoding="utf-8")
    real = ledger._detectors
    monkeypatch.setattr(ledger, "_detectors", lambda: dict(real(), planted=lambda t: ["x"] if "phase 2" in t else []))
    out = tmp_path / "projects.csv"
    code, stdout, err = run(["report", "--codes", "--out", str(out)], capsys)
    assert code == 0, err
    row = next(r for r in lines_of(out) if r.startswith('"tcp-b3np"'))
    assert row.split('","')[2:4] == [ledger.WITHHELD, ledger.WITHHELD]
    assert "2 texts withheld" in stdout
    assert json.loads((tmp_path / "projects.meta.json").read_text(encoding="utf-8"))["withheld"] == 2
    fixtures.assert_no_fixture_name(out.read_text(encoding="utf-8") + stdout + err, "the codes file")


def test_r1_a_check_that_cannot_run_writes_nothing(planted, tmp_path, capsys, monkeypatch):
    def broken(texts, reg):
        raise ledger.LedgerError("name check unavailable")

    monkeypatch.setattr(ledger, "hit_indexes", broken)
    out = tmp_path / "projects.csv"
    code, _, err = run(["report", "--codes", "--out", str(out)], capsys)
    assert code == 2 and "nothing is written" in err
    assert not out.exists() and not (tmp_path / "projects.meta.json").exists()


def test_r1_the_default_place_is_the_state_folder_and_without_it_the_command_says_so(planted, capsys):
    folder = ownerwait.report_dir()
    code, _, err = run(["report", "--codes"], capsys)
    assert code == 2 and "--out FILE" in err
    folder.mkdir(parents=True)
    code, _, err = run(["report", "--codes"], capsys)
    assert code == 0, err
    assert lines_of(folder / "projects.csv")[0] == HEADER and (folder / "projects.meta.json").is_file()


def test_r1_if_changed_writes_nothing_for_the_same_rows(planted, tmp_path, capsys):
    out = tmp_path / "projects.csv"
    assert run(["report", "--codes", "--out", str(out), "--if-changed"], capsys)[0] == 0
    old = 1_600_000_000_000_000_000
    os.utime(out, ns=(old, old))
    code, stdout, _ = run(["report", "--codes", "--out", str(out), "--if-changed"], capsys)
    assert code == 0 and "unchanged, 4 rows" in stdout and out.stat().st_mtime_ns == old
    state = planted.projects_root / "tcp-b3np" / "STATE.md"
    state.write_text(state.read_text(encoding="utf-8").replace("phase 2", "phase 3"), encoding="utf-8")
    code, stdout, _ = run(["report", "--codes", "--out", str(out), "--if-changed"], capsys)
    assert code == 0 and "written" in stdout and "phase 3" in out.read_text(encoding="utf-8")


@pytest.fixture
def own():
    with FakeOBS("awb", AK, SK) as o:
        yield o


@pytest.fixture
def svc(planted, own, tmp_path, monkeypatch):
    with short_dir() as d:
        s = keys.Service(d / "admin.sock", d / "call.sock", log_dir=tmp_path / "log",
                         obs_endpoints={own.bucket: own.endpoint})
        s.start()
        monkeypatch.setenv(keys.ADMIN_SOCKET_ENV, str(s.admin_path))
        monkeypatch.setenv(keys.CALL_SOCKET_ENV, str(s.call_path))
        keys.unlock(s.admin_path, sorted(STORE), reader=STORE.get,
                    settings={"bucket_tenant": "test-1", "lab_bucket": "awb-lab", "owner_bucket": own.bucket,
                              "max_mb": "1"})
        try:
            yield s
        finally:
            s.stop()


def test_r1_put_sends_both_files_to_reports_and_the_key_service_allows_nothing_else_there(svc, own, tmp_path,
                                                                                       capsys):
    folder = ownerwait.report_dir()
    folder.mkdir(parents=True)
    code, stdout, err = run(["report", "--codes", "--put", "--if-changed"], capsys)
    assert code == 0, err
    assert own.objects["reports/projects.csv"] == (folder / "projects.csv").read_bytes()
    assert json.loads(own.objects["reports/projects.meta.json"])["rows"] == 4
    assert "put reports/projects.csv" in stdout and "put reports/projects.meta.json" in stdout
    assert reportcodes.read_state()["state"] == "put" and ownerwait.report_items() == []
    code, stdout, _ = run(["report", "--codes", "--put", "--if-changed"], capsys)
    assert code == 0 and "unchanged" in stdout and "put " not in stdout
    f = tmp_path / "x.csv"
    f.write_bytes(b"x")
    for key in ("reports/other.csv", "reports/inventory/a.tsv", "reports/../datasets/x", "reports/"):
        assert keys.request(svc.admin_path, {"op": "bucket_put", "key": key}, upload=f)["ok"] is False, key
    assert keys.request(svc.admin_path, {"op": "bucket_get", "key": "reports/projects.csv"})["ok"] is False
    assert keys.request(svc.call_path, {"op": "bucket_put", "key": "reports/projects.csv"}, upload=f)["ok"] is False
    assert own.objects["reports/projects.csv"] != b"x"


def test_r1_a_failed_put_is_an_owner_item_until_a_put_succeeds(planted, tmp_path, capsys, monkeypatch):
    folder = ownerwait.report_dir()
    folder.mkdir(parents=True)
    code, stdout, _ = run(["report", "--codes", "--put"], capsys)          # no key service: locked
    assert code == 0 and "put skipped" in stdout
    items = ownerwait.report_items()
    assert [(i.label, i.text) for i in items] == [
        ("reports", "projects.csv older than 24 h, the last put failed: %s" % ownerwait._short(bucket.LOCKED_LINE))]

    def refused(self, key, path, content_type="application/octet-stream"):
        raise bucket.BucketError("the key service: refused")

    monkeypatch.setattr(bucket.KeyServiceClient, "put_file", refused)
    code, _, err = run(["report", "--codes", "--put", "--if-changed"], capsys)
    assert code == 1 and "put failed" in err
    now = dt.datetime.now(dt.timezone.utc)
    reportcodes.write_state({"state": "put", "sha256": "0"}, now=now - dt.timedelta(hours=2))
    reportcodes.write_state({"state": "failed", "reason": "the key service: refused"}, now=now)
    assert [i.text for i in ownerwait.report_items()] == ["the last put of projects.csv failed: the key service: refused"]
    assert [i.label for i in ownerwait.collect(planted, queue=tmp_path / "QUEUE.md")[0]].count("reports") == 1
    reportcodes.write_state({"state": "put", "sha256": "0"})
    assert ownerwait.report_items() == []


def test_r1_the_period_report_reads_the_shared_module():
    """The three helpers moved to awb/projectfacts.py; the period report uses them unchanged."""
    assert period.status_of is projectfacts.status_of and period.withhold is projectfacts.withhold
    assert period._git_dates is projectfacts.git_dates and period._mtime_date is projectfacts.mtime_date


def test_r1_the_hourly_unit_runs_as_the_owner_and_the_seal_and_the_update_install_it(fakebin, tmp_path):
    service = (WORK_JOBS / "awb-report-codes.service").read_text(encoding="utf-8").splitlines()
    timer = (WORK_JOBS / "awb-report-codes.timer").read_text(encoding="utf-8").splitlines()
    assert "User=@OWNER@" in service and "Type=oneshot" in service
    assert [x for x in service if x.startswith("ExecStart=")] == [
        "ExecStart=/usr/local/bin/awb report --codes --put --if-changed"]
    assert "StateDirectory=awb-report-codes" in service and "RestrictAddressFamilies=AF_UNIX" in service
    assert "OnCalendar=*-*-* *:50:00" in timer and "Unit=awb-report-codes.service" in timer
    res = run_script(SETUP, ["--dry-run"], fakebin)
    assert res.returncode == 0, res.stderr
    cmds = commands(res.stdout)
    for kind in ("service", "timer"):
        assert "+ write /etc/systemd/system/awb-report-codes.%s (mode 644, root:root)" % kind in cmds
    assert "+ systemctl enable --now awb-report-codes.timer" in cmds
    res = run_script(SETUP, ["--update", "--dry-run"], fakebin)
    assert res.returncode == 0, res.stderr
    no_marks(fakebin)
    cmds = commands(res.stdout)
    assert "+ install -o root -g root -m 0644 <rendered> /etc/systemd/system/awb-report-codes.service  # if it differs" \
        in cmds
    assert "+ systemctl try-restart awb-report-codes.timer  # if it changed" in cmds
    res, calls = _persist_run(tmp_path, 'OWNER_JOBS=awb-report-codes\nowner=ownr\nCONF_FILE=/etc/awb/paths.conf\n'
                                        'step_work_units\n')
    assert res.returncode == 0, res.stderr + res.stdout
    text = (tmp_path / "units" / "awb-report-codes.service").read_text()
    assert "User=ownr\n" in text and "Group=awb\n" in text and "@" not in text
    assert "systemctl enable --now awb-report-codes.timer" in calls
    res = run_script(VERIFY, ["--dry-run"], fakebin)
    assert res.returncode == 0, res.stderr
    checks = [line[6:] for line in res.stdout.splitlines() if line.startswith("CHECK ")]
    for must in ("the timer awb-report-codes.timer is enabled", "awb-report-codes.service runs as the owner",
                 "awb-report-codes.service runs awb report --codes --put --if-changed alone, every hour"):
        assert must in checks, must
