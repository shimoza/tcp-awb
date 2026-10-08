"""T7: `awb ui` submits a checked task to the UI queue, lists the runs and writes a result note when a run ends.

A fake bridge script plays the queue's bridge: it inserts the row the real one inserts. The queue folder, the
settings, the tasks folder and the state folder are temporary (AWB_UI_QUEUE, AWB_UI_CONF, AWB_UI_STATE).
"""
from __future__ import annotations

import contextlib
import json
import shutil
import sqlite3
import tempfile
from pathlib import Path

import pytest

from awb import cli, gate
from awb.tcp import keys, ui
from tests import fixtures

FAKE_BRIDGE = '''import json, os, sqlite3, sys
args = sys.argv[1:]
assert args[0] == "submit"
job_id = args[args.index("--id") + 1]
text = open(args[args.index("--file") + 1], encoding="utf-8").read()
con = sqlite3.connect(os.path.join(os.environ["AWB_UI_QUEUE"], "queue.sqlite3"))
con.execute("INSERT INTO jobs(id,text,hash,state,created,apply_changes) VALUES(?,?,?,?,?,?)",
            (job_id, text, "h", "queued", "2026-10-08T09:00:00+00:00", int("--candidate-only" not in args)))
con.commit()
print(json.dumps({"id": job_id, "state": "queued", "created": "2026-10-08T09:00:00+00:00"}))
'''

TASK = """UI task %s: a UI tasks page for the owner

From: Claude (AWB backend). For: the UI worker. Scope: architect-workbench.html only. Status: ready for work.

1. Why

%s
"""


@pytest.fixture
def queue(tmp_path, monkeypatch, home):
    q = tmp_path / "queue"
    (q / "runs").mkdir(parents=True)
    con = sqlite3.connect(q / "queue.sqlite3")
    con.execute("CREATE TABLE jobs (id TEXT PRIMARY KEY, text TEXT NOT NULL, hash TEXT NOT NULL, state TEXT NOT "
                "NULL, created TEXT NOT NULL, started TEXT, finished TEXT, detail TEXT NOT NULL DEFAULT '', "
                "apply_changes INTEGER NOT NULL DEFAULT 1)")
    con.commit()
    con.close()
    bridge = tmp_path / "bridge.py"
    bridge.write_text(FAKE_BRIDGE, encoding="utf-8")
    tasks = tmp_path / "ui-tasks"
    tasks.mkdir()
    conf = tmp_path / "ui.conf"
    conf.write_text("bridge = %s\ntasks = %s\n" % (bridge, tasks), encoding="utf-8")
    monkeypatch.setenv("AWB_UI_QUEUE", str(q))
    monkeypatch.setenv("AWB_UI_CONF", str(conf))
    monkeypatch.setenv("AWB_UI_STATE", str(tmp_path / "state"))
    return q


def _task(job_id: str, body: str = "The owner wants to see the runs of the queue.") -> Path:
    path = ui.tasks_dir() / ("%s.txt" % job_id)
    path.write_text(TASK % (job_id, body), encoding="utf-8")
    return path


def _rows(q: Path) -> list[tuple]:
    con = sqlite3.connect(q / "queue.sqlite3")
    try:
        return con.execute("SELECT id, state FROM jobs").fetchall()
    finally:
        con.close()


def _set(q: Path, job_id: str, **cols) -> None:
    con = sqlite3.connect(q / "queue.sqlite3")
    con.execute("UPDATE jobs SET %s WHERE id=?" % ",".join("%s=?" % k for k in cols), (*cols.values(), job_id))
    con.commit()
    con.close()


def _insert(q: Path, job_id: str, state: str, finished: str | None = None, detail: str = "",
            applies: bool = True) -> None:
    con = sqlite3.connect(q / "queue.sqlite3")
    con.execute("INSERT INTO jobs(id,text,hash,state,created,finished,detail,apply_changes) VALUES(?,?,?,?,?,?,?,?)",
                (job_id, "task", "h", state, "2026-10-08T09:00:00+00:00", finished, detail, int(applies)))
    con.commit()
    con.close()


def _run_folder(q: Path, job_id: str, validation: str = "PASS: inline JS syntax; six pages",
                summary: str = "Added the UI tasks page.", limitations=("Browser layout needs the runner.",)) -> None:
    run = q / "runs" / job_id
    run.mkdir(parents=True)
    (run / "validation.txt").write_text(validation + "\n", encoding="utf-8")
    (run / "result.json").write_text(json.dumps({"status": "completed", "summary": summary,
                                                 "limitations": list(limitations)}), encoding="utf-8")
    (run / "change.diff").write_text("--- before\n+++ after\n@@ -1,2 +1,3 @@\n-old\n+new\n+more\n same\n",
                                     encoding="utf-8")


# --------------------------------------------------------------------------- submit


def test_submit_refuses_a_task_file_with_a_registered_form_with_the_class_only(queue, capsys):
    form = fixtures.CUSTOMER_FORMS[0]
    path = _task("ui-20261008-001", "The page shows the offer for %s." % form)
    assert ui.main(["submit", str(path), "--no-watch"]) == 1
    captured = capsys.readouterr()
    assert "name: 1 line" in captured.err
    for text in (captured.err, captured.out):
        for f in fixtures.ALL_REGISTERED:
            assert f not in text
    assert _rows(queue) == []


@pytest.mark.parametrize("cls", ["secret", "token", "private-key"])
def test_submit_refuses_a_task_file_with_a_planted_secret(queue, capsys, cls):
    planted = gate._selftest_cases()[cls].decode("utf-8")
    path = _task("ui-20261008-002", planted)
    assert ui.main(["submit", str(path), "--no-watch"]) == 1
    err = capsys.readouterr().err
    assert "%s: 1 line" % cls in err
    assert planted.splitlines()[1] not in err
    assert _rows(queue) == []


def test_submit_refuses_a_task_file_out_of_the_form(queue, capsys):
    path = ui.tasks_dir() / "ui-20261008-003.txt"
    path.write_text("UI task ui-20261008-009: another id\n\nScope: architect-workbench.html only.\n")
    assert ui.main(["submit", str(path), "--no-watch"]) == 1
    assert "first line" in capsys.readouterr().err
    path.write_text("UI task ui-20261008-003: no scope line\n\nChange the backend too.\n")
    assert ui.main(["submit", str(path), "--no-watch"]) == 1
    assert "Scope: architect-workbench.html only" in capsys.readouterr().err
    assert _rows(queue) == []


def test_submit_hands_a_clean_task_to_the_bridge_and_starts_the_watch(queue, capsys, monkeypatch):
    started = []
    monkeypatch.setattr(ui, "spawn_watch", lambda job_id, mail: started.append((job_id, mail)) or Path("x.log"))
    path = _task("ui-20261008-004")
    assert ui.main(["submit", str(path), "--mail"]) == 0
    assert _rows(queue) == [("ui-20261008-004", "queued")]
    assert started == [("ui-20261008-004", True)]
    assert "UI run ui-20261008-004 submitted: queued." in capsys.readouterr().out


def test_submit_without_a_bridge_in_the_settings_is_refused(queue, capsys, tmp_path, monkeypatch):
    conf = tmp_path / "empty.conf"
    conf.write_text("# nothing\n")
    monkeypatch.setenv("AWB_UI_CONF", str(conf))
    path = Path(tempfile.mkdtemp(dir=tmp_path)) / "ui-20261008-005.txt"
    path.write_text(TASK % ("ui-20261008-005", "Clean."), encoding="utf-8")
    assert ui.main(["submit", str(path), "--no-watch"]) == 1
    assert "no bridge in ui.conf" in capsys.readouterr().err


# --------------------------------------------------------------------------- watch


def test_watch_writes_the_result_note_when_the_run_flips_to_completed(queue, monkeypatch, capsys):
    _insert(queue, "ui-20261008-006", "running")
    _run_folder(queue, "ui-20261008-006")
    sleeps = []

    def flip(seconds):
        sleeps.append(seconds)
        _set(queue, "ui-20261008-006", state="completed", finished="2026-10-08T09:04:00+00:00",
             detail="Local UI updated; not deployed. Added the UI tasks page.")
    monkeypatch.setattr(ui.time, "sleep", flip)
    assert ui.main(["watch", "ui-20261008-006", "--interval", "1"]) == 0
    assert len(sleeps) == 1
    note = (ui.tasks_dir() / "ui-20261008-006.result.md").read_text(encoding="utf-8")
    assert note.startswith("# UI run ui-20261008-006: completed")
    assert "finished 2026-10-08 09:04 UTC" in note
    assert "PASS: inline JS syntax; six pages" in note
    assert "Added the UI tasks page." in note
    assert "- Browser layout needs the runner." in note
    assert "change.diff: " in note and "(+2 -1)" in note
    assert "sudo awb web publish --from-queue ui-20261008-006" in note
    assert "ended: completed" in capsys.readouterr().out


def test_watch_names_a_run_that_cannot_be_published_and_withholds_a_text_with_a_finding(queue):
    form = fixtures.CUSTOMER_FORMS[0]
    _insert(queue, "ui-20261008-007", "completed", "2026-10-08T09:05:00+00:00")
    _run_folder(queue, "ui-20261008-007", validation="FAIL: overflow at 390px",
                summary="Built the page for %s." % form)
    assert ui.main(["watch", "ui-20261008-007"]) == 0
    note = (ui.tasks_dir() / "ui-20261008-007.result.md").read_text(encoding="utf-8")
    assert "Not publishable: the validation is not PASS." in note
    assert "(withheld: name: 1 line; read it in the run folder)" in note
    for f in fixtures.ALL_REGISTERED:
        assert f not in note
    _insert(queue, "ui-20261008-008", "blocked", "2026-10-08T09:06:00+00:00", detail="A missing contract.")
    assert ui.main(["watch", "ui-20261008-008"]) == 0
    note = (ui.tasks_dir() / "ui-20261008-008.result.md").read_text(encoding="utf-8")
    assert "Not publishable: the run is in state blocked." in note
    assert "None: the run wrote no validation.txt." in note and "no change.diff" in note


def test_watch_with_mail_sends_codes_only(queue, monkeypatch, capsys):
    sent = []
    monkeypatch.setattr(ui, "send_mail", lambda subject, message: sent.append((subject, message)) or "sent")
    _insert(queue, "ui-20261008-009", "ready", "2026-10-08T09:07:00+00:00", applies=False,
            detail="Validated candidate only. Built the page for %s." % fixtures.CUSTOMER_FORMS[0])
    _run_folder(queue, "ui-20261008-009")
    assert ui.main(["watch", "ui-20261008-009", "--mail"]) == 0
    (subject, message), = sent
    assert subject == "Workbench: UI run ui-20261008-009 ready"
    assert "Validation: PASS." in message and "sudo awb web publish --from-queue ui-20261008-009" in message
    for f in fixtures.ALL_REGISTERED:
        assert f not in message
    assert "Mail sent." in capsys.readouterr().out


def test_watch_gives_up_after_its_timeout_without_a_note(queue, monkeypatch):
    _insert(queue, "ui-20261008-010", "queued")
    monkeypatch.setattr(ui.time, "sleep", lambda s: None)
    assert ui.main(["watch", "ui-20261008-010", "--timeout", "0"]) == 1
    assert not (ui.tasks_dir() / "ui-20261008-010.result.md").exists()


# --------------------------------------------------------------------------- status


def test_status_prints_one_line_per_run_with_id_state_and_finish_time_codes_only(queue, capsys):
    _insert(queue, "ui-20261008-011", "completed", "2026-10-08T09:10:00+00:00",
            detail="Built the page for %s." % fixtures.CUSTOMER_FORMS[0])
    _insert(queue, "ui-20261008-012", "running")
    assert cli.main(["ui", "status"]) == 0
    out = capsys.readouterr().out
    lines = out.splitlines()
    assert len(lines) == 2
    assert any(l.split()[:2] == ["ui-20261008-011", "completed"] and "2026-10-08 09:10 UTC" in l for l in lines)
    assert any(l.split()[:2] == ["ui-20261008-012", "running"] and l.endswith("finished -") for l in lines)
    for f in fixtures.ALL_REGISTERED:
        assert f not in out


def test_status_ended_names_a_run_once_after_it_ended(queue, capsys):
    _insert(queue, "ui-20261008-013", "running")
    assert ui.main(["status", "--ended"]) == 0
    assert capsys.readouterr().out == ""
    _set(queue, "ui-20261008-013", state="completed", finished="2099-01-01T00:00:00+00:00")
    assert ui.main(["status", "--ended"]) == 0
    assert capsys.readouterr().out.startswith("UI run ui-20261008-013 ended: completed")
    assert ui.main(["status", "--ended"]) == 0
    assert capsys.readouterr().out == ""


# --------------------------------------------------------------------------- the mail through the key service


@contextlib.contextmanager
def _service(tmp_path):
    d = Path(tempfile.mkdtemp(prefix="awbu", dir="/tmp"))
    s = keys.Service(d / "admin.sock", d / "call.sock", log_dir=tmp_path / "log")
    notes = []
    s._notify = lambda subject, message: notes.append((subject, message)) or True
    s.start()
    try:
        yield s, notes
    finally:
        s.stop()
        shutil.rmtree(d, ignore_errors=True)


def test_the_key_service_sends_an_owner_mail_through_its_notify_operation(home, tmp_path):
    with _service(tmp_path) as (s, notes):
        answer = keys.request(s.admin_path, {"op": "notify", "subject": "Workbench: UI run ui-1 completed",
                                             "message": "UI run ui-1 ended."})
        assert answer == {"ok": True, "sent": True}
        assert notes == [("Workbench: UI run ui-1 completed", "UI run ui-1 ended.")]
        bad = keys.request(s.admin_path, {"op": "notify", "subject": "x", "message": "y" * (keys.NOTIFY_MAX + 1)})
        assert bad["ok"] is False and len(notes) == 1
