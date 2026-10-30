"""Task R1b: on the sealed host the hourly owner unit of `awb report --codes` failed every hour with "PermissionError
(operating system error)": the project folders are the work user's (mode 700) and the owner can read none of them.
Now the work side reads the folders (`--to-shared`, its timer awb-report-codes-work) and hands the file over on the
shared side; the owner side (`--from-shared`) checks it again and only puts. Invented projects in a temporary
Workbench, the invented names of tests/fixtures.py only."""
from __future__ import annotations

import datetime as dt
import json
import os
import stat
from contextlib import contextmanager
from pathlib import Path

import pytest

from awb import ledger, ownerwait, projectfacts, reportcodes
from tests import fixtures
from tests.conftest import AS_ROOT
from tests.test_r1_report_codes import HEADER, NAME, lines_of, own, planted, run, svc  # noqa: F401
from tests.test_seal import SETUP, VERIFY, WORK_JOBS, _persist_run, _service_keys, commands, fakebin, no_marks, \
    run_script  # noqa: F401

not_as_root = pytest.mark.skipif(AS_ROOT, reason="root reads a folder of mode 000 anyway")
CUST = fixtures.CUSTOMER_CODE
ROWS = [
    '"tcp-b3np","%s","move of 40 virtual machines to TCP","price estimate for phase 2","session","2026-10-27"' % CUST,
    '"tcp-c4rt","%s","tender text for a managed database","waits on the answer","customer","2026-10-14"' % CUST,
    '"tcp-kx2a","none","can a disk move between zones","close the query","closed","2026-10-22"',
    '"tcp-q7m4","%s","lab run of the vendor image","review the mapping draft","owner","2026-10-28"' % CUST,
]


@contextmanager
def unreadable(*folders: Path):
    """The folders at mode 000, as a project folder of the work user is for the owner; back to 700 afterwards."""
    for f in folders:
        os.chmod(f, 0)
    try:
        yield
    finally:
        for f in folders:
            os.chmod(f, 0o700)


def folders_of(home) -> list[Path]:
    return sorted(f for f in home.projects_root.iterdir() if f.is_dir())


def shared_file(home) -> Path:
    return Path(home.shared) / "report-codes" / "projects.csv"


@not_as_root
def test_r1b_an_unreadable_project_folder_is_no_crash_and_never_a_row_with_empty_texts(planted, tmp_path, capsys):
    """The bug of 2026-10-29: git_dates looked for .git in a folder of mode 700 of another user and raised. Now that
    is no date; and a folder this user cannot read makes the command refuse, since its texts would be empty and
    whose move wrong."""
    folder = planted.projects_root / "tcp-b3np"
    out = tmp_path / "projects.csv"
    with unreadable(folder):
        assert projectfacts.git_dates(folder) == []
        assert reportcodes.last_change(folder, "2026-10-01") == dt.date(2026, 10, 1)
        code, _, err = run(["report", "--codes", "--out", str(out)], capsys)
    assert code == 2 and "1 project folder cannot be read by this user" in err and "--from-shared" in err
    assert "operating system error" not in err
    assert not out.exists()


@not_as_root
def test_r1b_the_sealed_split_keeps_the_task_descriptions_and_whose_move(planted, tmp_path, capsys):
    """The work side reads the folders and writes report-codes/ of the shared side (640); the owner, who can read no
    folder, takes the rows from there: every task description, next step and whose move as the folders say."""
    code, stdout, err = run(["report", "--codes", "--to-shared"], capsys)
    assert code == 0, err
    handed = shared_file(planted)
    assert "4 rows" in stdout and lines_of(handed)[1:] == ROWS
    for f in (handed, handed.with_name("projects.meta.json")):
        assert stat.S_IMODE(f.stat().st_mode) == 0o640, f
    out = tmp_path / "own" / "projects.csv"
    out.parent.mkdir()
    with unreadable(*folders_of(planted)):
        code, stdout, err = run(["report", "--codes", "--from-shared", "--out", str(out)], capsys)
    assert code == 0, err
    rows = lines_of(out)
    assert rows[0] == HEADER and rows[1:] == ROWS
    assert all(r.split('","')[2] and r.split('","')[3] for r in rows[1:])
    meta = json.loads(out.with_name("projects.meta.json").read_text(encoding="utf-8"))
    assert meta["rows"] == 4 and meta["sha256"] and meta["written"].endswith("Z")
    fixtures.assert_no_fixture_name(out.read_text(encoding="utf-8") + stdout + err, "the codes file")


@not_as_root
def test_r1b_the_owner_side_puts_what_the_work_side_handed_over(planted, svc, own, capsys):
    folder = ownerwait.report_dir()
    folder.mkdir(parents=True)
    assert run(["report", "--codes", "--to-shared"], capsys)[0] == 0
    with unreadable(*folders_of(planted)):
        code, stdout, err = run(["report", "--codes", "--from-shared", "--put", "--if-changed"], capsys)
        assert code == 0, err
        assert own.objects["reports/projects.csv"] == (folder / "projects.csv").read_bytes()
        assert reportcodes.read_state()["state"] == "put"
        code, stdout, _ = run(["report", "--codes", "--from-shared", "--put", "--if-changed"], capsys)
    assert code == 0 and "unchanged" in stdout and "put " not in stdout


def _rewrite(path: Path, fn) -> None:
    text = path.read_bytes().decode("utf-8")
    path.write_bytes(fn(text).encode("utf-8"))


def test_r1b_the_owner_side_checks_the_texts_again_and_takes_the_customer_from_the_register(planted, tmp_path,
                                                                                          capsys):
    """A session can write the shared side: a name in a text is withheld on the owner side, a customer code that is
    not the register's is replaced by it, a code the register does not know is left out."""
    assert run(["report", "--codes", "--to-shared"], capsys)[0] == 0
    handed = shared_file(planted)
    _rewrite(handed, lambda t: t.replace("move of 40 virtual machines", "move for " + NAME)
             .replace('"tcp-c4rt","%s"' % CUST, '"tcp-c4rt","none"')
             + '"tcp-h6tw","none","a planted row","nothing","session","2026-10-28"\r\n')
    out = tmp_path / "projects.csv"
    code, stdout, err = run(["report", "--codes", "--from-shared", "--out", str(out)], capsys)
    assert code == 0, err
    rows = lines_of(out)[1:]
    assert [r.split('","')[0].strip('"') for r in rows] == ["tcp-b3np", "tcp-c4rt", "tcp-kx2a", "tcp-q7m4"]
    assert rows[0].split('","')[2] == ledger.WITHHELD and "1 text withheld" in stdout
    assert rows[1] == ROWS[1]
    fixtures.assert_no_fixture_name(out.read_text(encoding="utf-8") + stdout + err, "the codes file")


@pytest.mark.parametrize("change, words", [
    (lambda t: t.replace("whose move", "whose turn"), "another header"),
    (lambda t: t.replace('"session"', '"boss"'), "no word of whose move"),
    (lambda t: t.replace('"2026-10-27"', '"27.10.2026"'), "no ISO date"),
    (lambda t: t.replace('"tcp-b3np"', '"tcp-q7m4"'), "a second one"),
    (lambda t: t.replace(',"session",', ',"session","x",'), "has 7 cells"),
])
def test_r1b_a_file_the_owner_side_cannot_trust_is_refused(planted, tmp_path, capsys, change, words):
    assert run(["report", "--codes", "--to-shared"], capsys)[0] == 0
    _rewrite(shared_file(planted), change)
    out = tmp_path / "projects.csv"
    code, _, err = run(["report", "--codes", "--from-shared", "--out", str(out)], capsys)
    assert code == 2 and words in err and not out.exists()


def test_r1b_a_missing_old_or_linked_file_is_refused_and_with_put_an_owner_item(planted, tmp_path, capsys):
    ownerwait.report_dir().mkdir(parents=True)
    code, _, err = run(["report", "--codes", "--from-shared", "--put", "--if-changed"], capsys)
    assert code == 2 and "no codes file yet" in err
    assert [i.label for i in ownerwait.report_items()] == ["reports"]
    assert run(["report", "--codes", "--to-shared"], capsys)[0] == 0
    handed = shared_file(planted)
    old = handed.stat().st_mtime - 4 * 3600
    os.utime(handed, (old, old))
    code, _, err = run(["report", "--codes", "--from-shared", "--put"], capsys)
    assert code == 2 and "is 4 h old" in err and "awb-report-codes-work" in err
    assert "is 4 h old" in ownerwait.report_items()[0].text
    real = tmp_path / "planted.csv"
    real.write_bytes(handed.read_bytes())
    handed.unlink()
    handed.symlink_to(real)
    code, _, err = run(["report", "--codes", "--from-shared", "--out", str(tmp_path / "o.csv")], capsys)
    assert code == 2 and "cannot be opened" in err


def test_r1b_to_shared_writes_every_time_and_takes_no_out_put_or_if_changed(planted, tmp_path, capsys):
    for extra in (["--out", str(tmp_path / "x.csv")], ["--put"], ["--if-changed"]):
        code, _, err = run(["report", "--codes", "--to-shared"] + extra, capsys)
        assert code == 2 and "--to-shared writes the file of the shared side every time" in err, extra
    assert run(["report", "--codes", "--to-shared"], capsys)[0] == 0
    handed = shared_file(planted)
    old = 1_600_000_000_000_000_000
    os.utime(handed, ns=(old, old))
    code, stdout, _ = run(["report", "--codes", "--to-shared"], capsys)
    assert code == 0 and "written" in stdout and handed.stat().st_mtime_ns != old
    assert run(["report", "--codes", "--to-shared", "--from-shared"], capsys)[0] == 2


def test_r1b_the_work_unit_runs_as_the_work_user_and_the_seal_the_update_and_verify_carry_it(fakebin, tmp_path):
    keys = _service_keys(WORK_JOBS / "awb-report-codes-work.service")
    assert keys["User"] == ["awb"] and keys["Group"] == ["awb"] and keys["Type"] == ["oneshot"]
    assert keys["ExecStart"] == ["/usr/local/bin/awb report --codes --to-shared"]
    assert keys["ReadWritePaths"] == ["@WORK_HOME@/tcp-shared"] and keys["ProtectHome"] == ["read-only"]
    assert keys["RestrictAddressFamilies"] == ["AF_UNIX"] and keys["UMask"] == ["0027"]
    timer = (WORK_JOBS / "awb-report-codes-work.timer").read_text(encoding="utf-8").splitlines()
    assert "OnCalendar=*-*-* *:40:00" in timer and "Unit=awb-report-codes-work.service" in timer
    res = run_script(SETUP, ["--dry-run"], fakebin)
    assert res.returncode == 0, res.stderr
    cmds = commands(res.stdout)
    for kind in ("service", "timer"):
        assert "+ write /etc/systemd/system/awb-report-codes-work.%s (mode 644, root:root)" % kind in cmds
    assert "+ systemctl enable --now awb-report-codes-work.timer" in cmds
    res = run_script(SETUP, ["--update", "--dry-run"], fakebin)
    assert res.returncode == 0, res.stderr
    no_marks(fakebin)
    assert "+ install -o root -g root -m 0644 <rendered> /etc/systemd/system/awb-report-codes-work.service  # if " \
           "it differs" in commands(res.stdout)
    res, calls = _persist_run(tmp_path, "step_work_units\n")
    assert res.returncode == 0, res.stderr + res.stdout
    text = (tmp_path / "units" / "awb-report-codes-work.service").read_text()
    assert "ReadWritePaths=%s/work/tcp-shared\n" % tmp_path in text and "@" not in text
    assert "systemctl enable --now awb-report-codes-work.timer" in calls
    res = run_script(VERIFY, ["--dry-run"], fakebin)
    assert res.returncode == 0, res.stderr
    checks = [line[6:] for line in res.stdout.splitlines() if line.startswith("CHECK ")]
    for must in ("the timer awb-report-codes-work.timer is enabled", "awb-report-codes-work.service runs as the "
                 "work user", "awb-report-codes-work.service runs awb report --codes --to-shared alone, every hour"):
        assert must in checks, must
