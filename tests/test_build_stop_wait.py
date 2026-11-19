"""Q-0027 and Q-0008: `awb build stop C-NNN` for a queued or running check, replace on queue for a newer head of the
same branch, and `awb build wait ID...`. Temporary run folders only; systemctl is never called (subprocess.run is
patched)."""
from __future__ import annotations

import subprocess
from datetime import datetime
from pathlib import Path

import pytest

from awb import build, buildrun


@pytest.fixture
def runs(tmp_path, monkeypatch):
    r = tmp_path / "runs"
    monkeypatch.setenv("AWB_BUILD_QUEUE", str(tmp_path / "queue"))
    monkeypatch.setenv("AWB_BUILD_RUNS", str(r))
    monkeypatch.delenv("CLAUDECODE", raising=False)
    monkeypatch.delenv("CLAUDE_CODE_ENTRYPOINT", raising=False)
    return r


@pytest.fixture
def calls(monkeypatch):
    """Every subprocess.run of the code under test, recorded and never run."""
    seen: list[list[str]] = []

    def fake(argv, **kw):
        seen.append(list(argv))
        return subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr(subprocess, "run", fake)
    return seen


def a_check(runs: Path, state: str = "queued", head: str = "a" * 40, ref: str | None = "pr-1") -> build.Run:
    run = build.new_check(runs)
    (run.folder / "head").write_text(head + "\n")
    if ref:
        (run.folder / build.CHECK_REF).write_text(ref + "\n")
    if state != "queued":
        build.write_state(run.folder, state)
    return build.latest(run.id, runs)


# --------------------------------------------------------------------------- awb build stop C-NNN

def test_stop_of_a_queued_check_writes_stopped_by_owner_and_stops_no_unit(runs, calls, capsys):
    run = a_check(runs)
    assert build.main(["stop", run.id]) == 0
    assert build.latest(run.id, runs).state == "stopped: by_owner"
    assert not (run.folder / "stop-wanted").exists() and calls == []
    assert "C-001 stopped: by_owner" in capsys.readouterr().out


def test_stop_of_a_running_check_stops_its_suite_unit_with_the_runner_stop_form(runs, calls, capsys):
    run = a_check(runs, "running")
    assert build.main(["stop", "C-001"]) == 0
    assert build.latest("C-001", runs).state == "stopped: by_owner"
    assert (run.folder / "stop-wanted").exists()
    assert calls == [["sudo", "-n", "systemctl", "stop", "awb-task-C-001-suite.service"]]
    assert "awb-task-C-001-suite.service stopped" in capsys.readouterr().out


@pytest.mark.parametrize("state", ["passed", "red: suite", "stopped: by_owner", "interrupted"])
def test_stop_of_an_ended_check_is_refused_and_writes_nothing(runs, calls, capsys, state):
    run = a_check(runs, state)
    assert build.main(["stop", run.id]) == 2
    assert "not queued or running" in capsys.readouterr().err
    assert build.latest(run.id, runs).state == state and calls == []


def test_stop_of_an_unknown_check_is_refused(runs, calls, capsys):
    assert build.main(["stop", "C-404"]) == 2
    assert "unknown" in capsys.readouterr().err and calls == []


def test_stop_of_a_task_id_keeps_its_old_form(runs, calls, capsys):
    run = build.new_run("B-012", runs, "running")
    assert build.main(["stop", "B-012"]) == 0
    assert (run.folder / "stop-wanted").exists() and calls == []
    assert build.latest("B-012", runs).state == "running"


def test_the_states_of_a_stop_and_a_replace_are_states():
    assert build.STATE_RE.fullmatch("stopped: by_owner")
    assert build.STATE_RE.fullmatch("stopped: replaced by C-012")
    assert not build.STATE_RE.fullmatch("stopped: replaced by B-012")
    assert not build.STATE_RE.fullmatch("stopped: replaced by C-01")


# --------------------------------------------------------------------------- replace on queue

def test_a_newer_head_of_the_same_branch_replaces_only_the_queued_check_of_an_older_head(runs):
    old = a_check(runs, head="a" * 40)
    running = a_check(runs, "running", head="b" * 40)
    other = a_check(runs, head="c" * 40, ref="pr-2")
    same = a_check(runs, head="d" * 40)
    ended = a_check(runs, "red: suite", head="e" * 40)
    new = a_check(runs, head="d" * 40)
    assert build.replace_older_checks(new, "pr-1", "d" * 40, runs) == [old.id]
    assert build.latest(old.id, runs).state == "stopped: replaced by %s" % new.id
    assert build.latest(running.id, runs).state == "running"
    assert build.latest(other.id, runs).state == "queued"
    assert build.latest(same.id, runs).state == "queued"
    assert build.latest(ended.id, runs).state == "red: suite"
    assert build.latest(new.id, runs).state == "queued"


@pytest.mark.parametrize("ref, kept", [
    ("pr-1", "pr-1"), ("feature/q-0027", "feature/q-0027"), ("HEAD", None), ("abc1234", None), ("a..b", None),
    ("-q", None), ("x" * 201, None),
])
def test_only_a_branch_name_is_recorded_as_the_ref_of_a_check(ref, kept):
    assert build.check_ref(ref, "abc1234" + "0" * 33) == kept


def test_check_main_records_the_ref_and_replaces_the_queued_check_of_the_older_head(tmp_path, capsys):
    """Through the real `awb build check` in a throw-away world: a runner holds the lock, so each check stays queued
    (the wait is broken off at its first poll). The second head of pr-1 replaces the first."""
    w = buildrun.World(tmp_path / "w")
    try:
        w.host.working = w.seed
        w.g("checkout", "-q", "-b", "pr-1", "main", cwd=w.seed)
        heads = []
        for value in ("2", "3"):
            (w.seed / "awb" / "x.py").write_text("VALUE = %s\n" % value, encoding="utf-8")
            w.g("-c", "user.name=a cloud session", "-c", "user.email=cloud@localhost", "commit", "-q", "-am",
                "a change from outside", cwd=w.seed)
            heads.append(w.g("rev-parse", "HEAD", cwd=w.seed).strip())
        w.g("checkout", "-q", "main", cwd=w.seed)
        held = buildrun.acquire_lock(w.runs)

        class Polled(Exception):
            pass

        def first_poll(seconds):
            raise Polled()

        w.host.sleep = first_poll
        try:
            for head in heads:
                w.g("update-ref", "refs/heads/pr-1", head, cwd=w.seed)
                with pytest.raises(Polled):
                    buildrun.check_main("pr-1", w.host)
        finally:
            import os
            os.close(held)
        first, second = (build.latest(c, w.runs) for c in build.check_ids(w.runs))
        assert (first.folder / build.CHECK_REF).read_text() == "pr-1\n"
        assert (second.folder / build.CHECK_REF).read_text() == "pr-1\n"
        assert first.state == "stopped: replaced by %s" % second.id and second.state == "queued"
        assert "check C-001 of an older head of pr-1: stopped: replaced by C-002" in capsys.readouterr().out
        assert build.line(first) == "build check C-001 stopped: replaced by C-002; nothing merged; runs/C-001/1/"
    finally:
        w.close()


def test_the_runner_ends_a_check_stopped_while_it_ran_as_stopped_by_owner_and_starts_no_suite(tmp_path, monkeypatch,
                                                                                               capsys):
    w = buildrun.World(tmp_path / "w")
    try:
        w.host.working = w.seed
        w.g("checkout", "-q", "-b", "pr-1", "main", cwd=w.seed)
        (w.seed / "awb" / "x.py").write_text("VALUE = 2\n", encoding="utf-8")
        w.g("-c", "user.name=a cloud session", "-c", "user.email=cloud@localhost", "commit", "-q", "-am",
            "a change from outside", cwd=w.seed)
        w.g("checkout", "-q", "main", cwd=w.seed)
        real = buildrun.check_outdir

        def outdir_then_stop(host, ctx):
            ok = real(host, ctx)
            assert build.cmd_stop(ctx.tid, w.runs) == 0
            return ok

        stopped: list[str] = []
        monkeypatch.setattr(build, "_stop_unit", stopped.append)      # the world's git needs the real subprocess
        monkeypatch.setattr(buildrun, "check_outdir", outdir_then_stop)
        assert buildrun.check_main("pr-1", w.host) == 1
        run = build.latest("C-001", w.runs)
        assert run.state == "stopped: by_owner"
        assert not [s for s in w.units.started if s.name.endswith("-suite")]
        assert stopped == ["awb-task-C-001-suite"]
        assert "state: stopped: by_owner" in (run.folder / "result.md").read_text(encoding="utf-8")
    finally:
        w.close()


# --------------------------------------------------------------------------- awb build wait

class Clock:
    """A clock the sleep moves: no real wait in a test."""

    def __init__(self, at: dict[int, callable] | None = None):
        self.t = 0.0
        self.sleeps: list[float] = []
        self.at = at or {}

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.t += seconds
        step = self.at.get(len(self.sleeps))
        if step:
            step()

    def now(self) -> float:
        return self.t


def wait(runs, ids, clock, every=False, timeout=None):
    return build.cmd_wait(ids, every, timeout, runs, sleep=clock.sleep, clock=clock.now)


def test_wait_returns_at_the_first_ended_id_with_one_line_and_its_result_path(runs, capsys):
    c = a_check(runs, "running")
    t = build.new_run("B-012", runs, "running")
    clock = Clock({2: lambda: (build.write_state(c.folder, "passed", datetime(2026, 11, 19, 14, 5)),
                               (c.folder / "result.md").write_text("state: passed\n"))})
    assert wait(runs, ["C-001", "B-012"], clock) == 0
    out = capsys.readouterr().out.splitlines()
    assert out == ["C-001  passed  2026-11-19 14:05  %s" % (c.folder / "result.md")]
    assert clock.sleeps == [build.WAIT_POLL, build.WAIT_POLL]
    assert build.latest(t.id, runs).state == "running"


def test_wait_all_waits_for_each_and_exits_one_when_one_did_not_pass(runs, capsys):
    c = a_check(runs, "queued")
    t = build.new_run("B-012", runs, "running")
    clock = Clock({1: lambda: build.write_state(t.folder, "done"),
                   3: lambda: build.write_state(c.folder, "red: suite")})
    assert wait(runs, ["C-001", "B-012"], clock, every=True) == 1
    out = capsys.readouterr().out.splitlines()
    assert [ln.split("  ")[:2] for ln in out] == [["B-012", "done"], ["C-001", "red: suite"]]
    assert out[1].endswith(str(c.folder))      # no result.md yet: the run folder
    assert len(clock.sleeps) == 3


def test_wait_all_exits_zero_when_every_one_passed_and_a_task_not_yet_run_is_waited_for(runs, capsys):
    c = a_check(runs, "passed")
    clock = Clock({2: lambda: build.new_run("B-013", runs, "running"),
                   4: lambda: build.write_state(build.latest("B-013", runs).folder, "done")})
    assert wait(runs, ["C-001", "B-013"], clock, every=True) == 0
    assert [ln.split("  ")[0] for ln in capsys.readouterr().out.splitlines()] == ["C-001", "B-013"]
    assert c.state == "passed"


@pytest.mark.parametrize("state", ["asked", "hung: suite", "held: push pending", "red: gate name"])
def test_a_task_end_that_is_not_done_exits_one(runs, capsys, state):
    build.new_run("B-012", runs, state)
    assert wait(runs, ["B-012"], Clock()) == 1
    assert "B-012  %s  " % state in capsys.readouterr().out


def test_a_replaced_check_has_ended_and_did_not_pass(runs, capsys):
    a_check(runs, "stopped: replaced by C-002")
    assert wait(runs, ["C-001"], Clock()) == 1
    assert "C-001  stopped: replaced by C-002  " in capsys.readouterr().out


def test_wait_times_out_with_exit_two_and_never_sleeps_past_the_timeout(runs, capsys):
    a_check(runs, "running")
    clock = Clock()
    assert wait(runs, ["C-001"], clock, timeout=12) == 2
    assert sum(clock.sleeps) == 12 and max(clock.sleeps) <= build.WAIT_POLL
    cap = capsys.readouterr()
    assert cap.out == "" and "timeout: C-001 still open" in cap.err


@pytest.mark.parametrize("ids", [["C-404"], ["X-001"], ["B-1"], ["c-001"]])
def test_wait_refuses_an_unknown_check_or_a_bad_id_at_once(runs, capsys, ids):
    clock = Clock()
    assert wait(runs, ids, clock) == 2 and clock.sleeps == []


def test_wait_through_the_cli_takes_all_and_timeout(runs, monkeypatch, capsys):
    a_check(runs, "running")
    seen = []
    monkeypatch.setattr(build, "cmd_wait", lambda ids, every, timeout: seen.append((ids, every, timeout)) or 0)
    assert build.main(["wait", "C-001", "B-012", "--all", "--timeout", "60"]) == 0
    assert build.main(["wait", "C-001"]) == 0
    assert seen == [(["C-001", "B-012"], True, 60.0), (["C-001"], False, None)]

