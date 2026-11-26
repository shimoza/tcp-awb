"""B-105: a deploy waits for a running winlab transfer.

On 2026-11-25 a deploy replaced the installed tree under a 7.86 GB `awb winlab get` and killed it. `awb winlab get` and
`put` now write a start (events kind `transfer`, id MACHINE@PID) and an end, and the root run of `sudo awb deploy`
reads the events file before the plan: a transfer of the last 6 hours with no end and a live pid refuses the deploy
(exit 2, nothing changed), `--force` goes on, an ended transfer and a transfer whose pid is gone do not hold it (the
second says it is stale). Nothing here touches systemd: the root run goes through the fake host of tests/test_deploy.py.
Every test names the planted fault it must catch.
"""
from __future__ import annotations

import datetime as dt
import os
import subprocess
import sys

import pytest

from awb import events, winlabsession
from tests.conftest import not_as_root
from tests.test_deploy import World, world  # noqa: F401

NOW = dt.datetime(2026, 11, 26, 12, 0, 0, tzinfo=dt.timezone.utc)
CODE = "tcp-q7m4"
HOLD = "a winlab transfer runs for %s since %s; deploy after it ends or with --force"


def minutes_ago(n: int) -> dt.datetime:
    return NOW - dt.timedelta(minutes=n)


def hm(t: dt.datetime) -> str:
    return t.astimezone().strftime("%H:%M")


def dead_pid() -> int:
    """The pid of a process that ran and was reaped."""
    proc = subprocess.Popen([sys.executable, "-c", "pass"])
    proc.wait()
    return proc.pid


@pytest.fixture
def board(world):
    """The host file names a shared side and its board folder exists, as on a sealed host."""
    shared = world.tmp / "shared"
    folder = shared / "build"
    folder.mkdir(parents=True)
    conf = world.etc / "awb" / "paths.conf"
    conf.write_text(conf.read_text() + "shared = %s\n" % shared)
    world.runner.plan = world.plan()
    return folder


def deploy(world, argv=None, alive=(os.getpid(),)):
    return world.main(argv or [], wall=lambda: NOW, pid_alive=lambda pid: pid in alive)


def started(board, pid: int, ago: int = 30, machine: str = "lab-1") -> str:
    eid = "%s@%d" % (machine, pid)
    assert events.start("transfer", eid, project=CODE, board=board, now=minutes_ago(ago))
    return eid


@not_as_root
def test_an_open_transfer_with_a_live_pid_refuses_the_deploy(world, board, capsys):
    """Planted: a deploy that does not read the events file goes on (it did before B-105) and installs a release."""
    started(board, os.getpid(), ago=30)
    assert deploy(world) == 2
    err = capsys.readouterr().err
    assert err.strip() == "awb deploy: " + HOLD % (CODE, hm(minutes_ago(30)))
    assert world.runner.calls == [] and not (world.root / "releases" / world.c1).exists()


@not_as_root
def test_force_deploys_anyway(world, board, capsys):
    """Planted: a --force that is not read refuses again; one that skips the line hides what it cuts off."""
    started(board, os.getpid())
    assert deploy(world, ["--force"]) == 0, capsys.readouterr()
    out = capsys.readouterr().out
    assert "--force deploys anyway" in out and CODE in out
    assert os.readlink(world.root / "src").endswith(world.c1)


@not_as_root
def test_an_ended_transfer_does_not_hold_the_deploy(world, board, capsys):
    """Planted: a check that looks at the starts and ignores the ends refuses a finished transfer."""
    eid = started(board, os.getpid())
    assert events.end("transfer", eid, "ok", project=CODE, board=board, now=minutes_ago(5))
    assert deploy(world) == 0
    assert "winlab transfer" not in capsys.readouterr().out


@not_as_root
def test_a_dead_pid_goes_on_and_says_the_transfer_is_stale(world, board, capsys):
    """Planted: a transfer that was killed (SIGKILL leaves its start) holds every later deploy."""
    started(board, dead_pid(), ago=45)
    assert deploy(world) == 0, capsys.readouterr()
    out = capsys.readouterr().out
    assert "the winlab transfer for %s since %s is stale" % (CODE, hm(minutes_ago(45))) in out


@not_as_root
def test_a_transfer_older_than_six_hours_does_not_hold_it(world, board, capsys):
    """Planted: a window that is not applied lets a start of last week hold the deploy for a pid that was reused."""
    started(board, os.getpid(), ago=6 * 60 + 5)
    assert deploy(world) == 0, capsys.readouterr()


@not_as_root
def test_a_host_without_a_board_has_no_check(world, capsys):
    """Planted: a check that needs the board fails a plain host (no shared side in paths.conf)."""
    world.runner.plan = world.plan()
    assert deploy(world) == 0, capsys.readouterr()


@not_as_root
def test_the_dry_run_is_not_held(world, board, capsys):
    """Planted: a dry run that refuses cannot show what the deploy would do."""
    started(board, os.getpid())
    assert deploy(world, ["--dry-run"]) == 0, capsys.readouterr()


@not_as_root
def test_other_events_of_the_board_do_not_hold_the_deploy(world, board, capsys):
    """Planted: a check on any open item (a lab window, a task) instead of kind transfer."""
    assert events.start("lab", "lab-1", project=CODE, board=board, now=minutes_ago(10))
    assert events.start("task", "B-105", board=board, now=minutes_ago(10))
    assert deploy(world) == 0, capsys.readouterr()


# --- the writers ------------------------------------------------------------------------------------------------


@pytest.fixture
def writer(tmp_path, monkeypatch):
    """The board of the session: awb.events writes to it, the project code is fixed."""
    folder = tmp_path / "board"
    folder.mkdir()
    monkeypatch.setattr(events, "folder", lambda p=None: folder)
    monkeypatch.setattr(winlabsession, "_code", lambda root: CODE)
    return folder


def ended_of(folder):
    far = dt.timedelta(days=3650)
    return [(i["id"], i["outcome"]) for i in events.ended_since(events.read(folder), NOW - far, NOW + far)]


def test_a_transfer_that_succeeds_writes_a_start_and_an_ok_end(writer, tmp_path):
    """Planted: a transfer without events, or an end that is never written, leaves the deploy holding forever."""
    eid = "lab-1@%d" % os.getpid()
    with winlabsession._transfer(tmp_path, "lab-1"):
        shown = events.running_now(events.read(writer))
        assert [(i["kind"], i["id"], i["project"]) for i in shown] == [("transfer", eid, CODE)]
    assert events.running_now(events.read(writer)) == []
    assert ended_of(writer) == [(eid, "ok")]


def test_a_transfer_that_fails_or_is_stopped_ends_red_or_stopped(writer, tmp_path):
    """Planted: an end written only on success leaves a failed transfer open."""
    eid = "lab-1@%d" % os.getpid()
    with pytest.raises(winlabsession.WinlabError):
        with winlabsession._transfer(tmp_path, "lab-1"):
            raise winlabsession.WinlabError("x")
    assert ended_of(writer) == [(eid, "red")]
    with pytest.raises(winlabsession.Stopped):
        with winlabsession._transfer(tmp_path, "lab-1"):
            raise winlabsession.Stopped()
    assert sorted(o for _, o in ended_of(writer)) == ["red", "stopped"]
    assert events.running_now(events.read(writer)) == []


def test_a_transfer_that_dies_leaves_a_start_that_running_now_shows(writer):
    """Planted: a writer that records start and end together hides a kill (SIGKILL runs no finally)."""
    events.start("transfer", "lab-1@%d" % dead_pid(), project=CODE, board=writer)
    shown = events.running_now(events.read(writer))
    assert [i["kind"] for i in shown] == ["transfer"]
    assert "winlab transfer" in events._what(shown[0])


def test_get_and_put_run_inside_a_transfer():
    """Planted: a put or get that calls the transfer without the events (the source holds both calls in a with)."""
    source = open(winlabsession.__file__, encoding="utf-8").read()
    assert 'with _transfer(root, found["name"]):\n            return _put(' in source
    assert 'with _transfer(root, found["name"]):\n            return _get(' in source
