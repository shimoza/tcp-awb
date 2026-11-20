"""tcp-bytg: a run log that left git (Q-0021, notes/lab-runs/full/<run>.log) is still the run log. `awb lab obs put`
and get pass its full log to the key service, every append goes to the full log, a late file at the old place is
merged into it and a second move loses no line. Every project, run and value here is invented."""
from __future__ import annotations

import os

import pytest

from awb import lablog, labclient
from awb.tcp import labobs
from tests.test_keys import short_dir  # noqa: F401
from tests.test_lo_lab_obs import BUCKET, KEY, LAB, PROJECT, a_file, wait, world  # noqa: F401

SMALL = 4096
RUN = "run-20261120-101500-k7q2"


def _block(n: int) -> str:
    return "## 2026-11-20T10:%02d:%02dZ GET ecs /v1/{project_id}/cloudservers\nanswer: 200\n%s\n\n" % (
        n // 60 % 60, n % 60, "x" * 300)


def _moved_run(w, monkeypatch) -> tuple[str, os.PathLike, os.PathLike]:
    monkeypatch.chdir(w.folder)
    assert labclient.run_main(["start", "--tenant", LAB]) == 0
    status, _h, _b = labclient.call("PUT", "obs", "/" + BUCKET)
    assert status == 200
    run = next(w.folder.joinpath("notes", "lab-runs").glob("run-*.log")).name[:-4]
    monkeypatch.setattr(lablog, "LIMIT", SMALL)
    labclient.log(w.folder, run, ["filler " + "y" * 200] * 30)
    old = w.folder / "notes" / "lab-runs" / (run + ".log")
    full = lablog.full_path(old)
    assert full.is_file() and not old.exists()
    return run, old, full


# --------------------------------------------------------------------------- obs put and get after a move


def test_bytg_obs_put_and_get_work_after_the_run_log_moved(world, monkeypatch, capsys):
    w = world
    run, old, full = _moved_run(w, monkeypatch)
    path, data = a_file(w)
    capsys.readouterr()
    assert labclient.obs_main(["put", str(path), "obs://%s/%s" % (BUCKET, KEY)]) == 0
    job = capsys.readouterr().out.split()[1]
    assert labclient.obs_wait(job, every=0.01)["state"] == "done"
    back = w.folder / "back.vhdx"
    got = labclient.obs_get("obs://%s/%s" % (BUCKET, KEY), back)
    assert labclient.obs_wait(got["job"], every=0.01)["state"] == "done" and back.read_bytes() == data
    assert not old.exists()
    text = full.read_text(encoding="utf-8")
    assert "obs put %s obs://%s/%s" % (path, BUCKET, KEY) in text
    assert "obs job %s: put done" % job in text and "obs job %s: get done" % got["job"] in text


def test_bytg_obs_put_merges_a_late_file_at_the_old_place(world, monkeypatch, capsys):
    w = world
    run, old, full = _moved_run(w, monkeypatch)
    old.write_text("late line at the old place\n", encoding="utf-8")
    path, _data = a_file(w)
    capsys.readouterr()
    assert labclient.obs_main(["put", str(path), "obs://%s/%s" % (BUCKET, KEY)]) == 0
    job = capsys.readouterr().out.split()[1]
    assert labclient.obs_wait(job, every=0.01)["state"] == "done"
    assert not old.exists()
    text = full.read_text(encoding="utf-8")
    assert text.index("late line at the old place") < text.index("obs put %s" % path)


def test_bytg_the_key_service_takes_the_log_or_its_full_log_and_no_other_file(world, tmp_path):
    w = world
    p = w.svc.paths_fn()
    logs = w.folder / "notes" / "lab-runs"
    (logs / lablog.FULL).mkdir()
    for place, ok in ((logs / (RUN + ".log"), True), (logs / lablog.FULL / (RUN + ".log"), True),
                      (logs / lablog.FULL / "run-20261120-101500-zz11.log", False), (logs / "other.log", False)):
        fd = os.open(place, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
        try:
            if ok:
                assert labobs._log_fd([fd, fd], p, PROJECT, RUN) == fd
            else:
                with pytest.raises(Exception, match="second descriptor is not the run log"):
                    labobs._log_fd([fd, fd], p, PROJECT, RUN)
        finally:
            os.close(fd)


# --------------------------------------------------------------------------- appends and a second move


def _root(tmp_path):
    root = tmp_path / "projects" / PROJECT
    (root / "notes" / "lab-runs").mkdir(parents=True)
    return root, root / "notes" / "lab-runs" / (RUN + ".log")


def test_bytg_appends_after_a_move_go_to_the_full_log(tmp_path, monkeypatch):
    monkeypatch.setattr(lablog, "LIMIT", SMALL)
    root, log = _root(tmp_path)
    assert lablog.live_path(log) == log
    blocks = [_block(n) for n in range(40)]
    for b in blocks:
        lablog.append(root, log, b)
    full = lablog.full_path(log)
    assert lablog.live_path(log) == full and not log.exists()
    assert lablog.append(root, log, _block(99)) == full
    assert full.read_text(encoding="utf-8") == "".join(blocks) + _block(99)
    with lablog._directory(log.parent) as parent:
        fd = lablog.open_live(parent, log.name)
    try:
        os.write(fd, b"through open_live\n")
    finally:
        os.close(fd)
    assert full.read_text(encoding="utf-8").endswith("through open_live\n") and not log.exists()


def test_bytg_a_second_move_of_the_same_log_loses_no_line(tmp_path, monkeypatch):
    monkeypatch.setattr(lablog, "LIMIT", SMALL)
    root, log = _root(tmp_path)
    first = [_block(n) for n in range(40)]
    for b in first:
        lablog.append(root, log, b)
    full = lablog.full_path(log)
    assert full.is_file() and not log.exists()
    # a writer that looked before the move fills a new log at the old place over the limit
    second = "".join(_block(100 + n) for n in range(40))
    log.write_text(second, encoding="utf-8")
    assert log.stat().st_size > SMALL
    assert lablog.append(root, log, _block(200)) == full
    assert not log.exists()
    assert full.read_text(encoding="utf-8") == "".join(first) + second + _block(200)
    # and once more, closed by the end of the run
    log.write_text(_block(300), encoding="utf-8")
    assert lablog.refresh(root, log) == log.parent / lablog.summary_name(log.name)
    assert not log.exists()
    text = full.read_text(encoding="utf-8")
    assert text == "".join(first) + second + _block(200) + _block(300)
    summary = (log.parent / lablog.summary_name(log.name)).read_text(encoding="utf-8")
    assert "- size: %d bytes" % len(text.encode()) in summary


def test_bytg_the_move_onto_an_existing_full_log_appends_to_it(tmp_path, monkeypatch):
    monkeypatch.setattr(lablog, "LIMIT", SMALL)
    root, log = _root(tmp_path)
    full = lablog.full_path(log)
    full.parent.mkdir()
    full.write_text(_block(1), encoding="utf-8")
    log.write_text(_block(2) * 20, encoding="utf-8")
    with lablog._directory(log.parent) as parent:
        fd = os.open(log.name, os.O_RDWR | os.O_APPEND, dir_fd=parent)
        try:
            lablog._move(root, "notes/lab-runs", parent, log.name, fd)
        finally:
            os.close(fd)
    assert not log.exists() and full.read_text(encoding="utf-8") == _block(1) + _block(2) * 20
