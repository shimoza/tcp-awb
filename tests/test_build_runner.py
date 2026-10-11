"""build runner (SB3 of presentations/self-build): `awb build serve`, its units, its eleven checks, the merge, the
push, the records, the board, the mail and `awb build selftest`.

Every task runs through the runner in a throw-away world (awb.buildrun.World): a bare main.git with a tiny project, a
queue, LocalUnits that run the unit steps as this uid and a stand-in client that plays a scenario named in the prompt
(it commits, writes the one JSON object and exits). Planted values are built from pieces so that this file passes the
gate itself.
"""
from __future__ import annotations

import json
import os
import re
import signal
import subprocess
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from awb import build, buildrun


@pytest.fixture
def world(tmp_path):
    w = buildrun.World(tmp_path / "w")
    yield w
    w.close()


def main_of(w) -> str:
    return w.g("--git-dir", str(w.root / "main.git"), "rev-parse", "refs/heads/main").strip()


def origin_of(w) -> str:
    return w.g("--git-dir", str(w.origin), "rev-parse", "refs/heads/main").strip()


def move_main(w, path: str, text: str) -> str:
    """A commit of his pushed into main.git while a task runs."""
    w.g("pull", "-q", "--rebase", str(w.root / "main.git"), "main", cwd=w.seed)
    (w.seed / path).write_text(text, encoding="utf-8")
    w.g("commit", "-q", "-am", "his own change", cwd=w.seed)
    w.g("push", "-q", str(w.root / "main.git"), "main:main", cwd=w.seed)
    return w.g("rev-parse", "HEAD", cwd=w.seed).strip()


def checks_of(run) -> str:
    return (run.folder / "checks.log").read_text(encoding="utf-8")


# --------------------------------------------------------------------------- green, done, asked

def test_a_done_task_inside_its_paths_goes_green_then_done_and_main_moves(world):
    start = main_of(world)
    run = world.run("good")
    assert run.state == "done"
    head = main_of(world)
    assert head != start and origin_of(world) == head
    assert (run.folder / "commit").read_text().strip() == head
    log = checks_of(run)
    assert [ln.split()[0:2] for ln in log.splitlines() if ln[:1].isdigit()] == [[str(i), "ok"] for i in range(1, 12)]
    result = (run.folder / "result.md").read_text(encoding="utf-8")
    assert "state: done" in result and "the value is two" in result and "Changed the value" not in result
    assert "next: deploy: sudo awb deploy --task %s" % run.id in result
    assert (run.folder / "message.txt").read_text().startswith("Changed the value.")
    # the clone, the out folder and the home are gone; the quarantine ref stays as the review object
    assert not world.host.clone(run.id, run.n).exists() and not world.host.home(run.id, run.n).exists()
    refs = world.g("--git-dir", str(world.root / "main.git"), "for-each-ref", "refs/quarantine/")
    assert "refs/quarantine/%s/1/task/%s" % (run.id, run.id) in refs


@pytest.mark.parametrize("scenario", ["ask", "blocked"])
def test_a_question_or_blocked_goes_asked_and_main_does_not_move(world, scenario):
    start = main_of(world)
    run = world.run(scenario)
    assert run.state == "asked"
    assert main_of(world) == start
    assert build.line(run) == "build %s asked; awb build show %s" % (run.id, run.id)
    # the home stays for the resume, its model key does not
    home = world.host.home(run.id, run.n)
    assert home.is_dir() and not (home / buildrun.KEY_IN_HOME).exists()
    assert "should i change" not in (run.folder / "result.md").read_text().lower()


def test_an_answer_resumes_the_same_session_and_a_lost_transcript_is_session_gone(world):
    run = world.run("ask")
    session = (run.folder / "session").read_text().strip()
    assert buildrun.UUID_RE.fullmatch(session)
    assert build.cmd_answer(run.id, "SCENARIO: good", world.runs, world.register) == 0
    world.task("good", tid=run.id)
    assert world.runner().poll_once()
    after = build.latest(run.id, world.runs)
    assert (after.n, after.state) == (run.n, "done")
    agent = [s for s in world.units.started if s.name == "awb-task-%s" % run.id]
    assert len(agent) == 2 and agent[1].argv[-2:] == ["--resume", session]
    assert (run.folder / "result.asked-1.json").exists()
    # a second task asks, its transcript is gone before the answer: no fresh session, stopped: session_gone
    other = world.run("ask")
    for f in (world.host.home(other.id, other.n) / ".claude" / "projects").rglob("*.jsonl"):
        f.unlink()
    assert build.cmd_answer(other.id, "SCENARIO: good", world.runs, world.register) == 0
    world.task("good", tid=other.id)
    world.runner().poll_once()
    assert build.latest(other.id, world.runs).state == "stopped: session_gone"


# --------------------------------------------------------------------------- the reds

@pytest.mark.parametrize("scenario, paths, state", [
    ("dirty", "awb/ tests/", "red: dirty tree"),
    ("branch", "awb/ tests/", "red: second branch"),
    ("outside", "awb/ tests/", "red: outside paths"),
    ("coauthor", "awb/ tests/", "red: gate attribution"),
    ("identity", "awb/ tests/", "red: identity"),
    ("deleted", "awb/ tests/", "red: tests lost"),
    ("skipped", "awb/ tests/", "red: tests lost"),
    ("testsjson", "awb/ tests/", "red: tests.json"),
    ("changelogcut", "awb/ tests/", "red: changelog"),
    ("nochangelog", "awb/ tests/", "red: changelog"),
    ("claudelocal", "awb/ tests/", "red: outside paths"),
    ("hook", "awb/ tests/", "red: outside paths"),
    ("rules", "awb/ tests/ rules/", "red: rules list lost lines"),
    ("newline", "awb/ tests/", "red: outside paths"),
])
def test_each_planted_fault_is_its_named_red_and_main_stays(world, scenario, paths, state):
    start = main_of(world)
    run = world.run(scenario, paths=paths)
    assert run.state == state
    assert main_of(world) == start and origin_of(world) == start
    assert build.line(run) == "build %s %s; branch task/%s kept" % (run.id, state, run.id)
    assert world.host.clone(run.id, run.n).is_dir()            # kept for his eyes or the retry
    assert not world.host.home(run.id, run.n).exists()         # the home and its key are gone
    assert "FAIL" in checks_of(run)


def test_a_replaced_test_named_in_tests_replaced_passes_check_ten(world):
    run = world.run("deleted", replaced="tests/test_x.py::test_three")
    assert run.state == "done"
    assert "1 test line(s) removed" in checks_of(run)


def test_a_conftest_only_in_the_clone_never_reaches_the_export(world):
    run = world.run("conftest")
    assert run.state == "done"
    names = world.g("--git-dir", str(world.root / "main.git"), "ls-tree", "-r", "--name-only", "main").split()
    assert "conftest.py" not in names


def test_the_task_check_runs_in_the_export_and_a_failing_one_is_red(world):
    assert world.run("good", check="tests/test_x.py -k one").state == "done"
    assert world.run("good", check="tests/test_x.py::test_none").state == "red: check"


def test_a_widened_path_reruns_the_checks_on_the_kept_branch_without_a_session(world):
    run = world.run("outside")
    assert run.state == "red: outside paths"
    assert build.cmd_widen(run.id, ["pyproject.toml"], world.runs) == 0
    sessions = len([s for s in world.units.started if s.name == "awb-task-%s" % run.id])
    assert world.runner().poll_once()
    assert build.latest(run.id, world.runs).state == "done"
    assert len([s for s in world.units.started if s.name == "awb-task-%s" % run.id]) == sessions


# --------------------------------------------------------------------------- hung, stopped, the retry

def test_a_session_that_never_returns_under_a_2_s_clock_is_hung_within_5_s_and_no_child_is_left(world):
    world.host.minute = 0.2
    t0 = time.monotonic()
    run = world.run("hang")
    assert run.state == "hung" and time.monotonic() - t0 < 5
    pid = int((world.base / ("mark-%s" % run.id)).read_text())
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)
    assert build.line(run).startswith("build %s hung after 10 min" % run.id)


@pytest.mark.parametrize("scenario", ["twoobjects", "noobject"])
def test_a_result_with_two_objects_or_none_is_stopped(world, scenario):
    run = world.run(scenario)
    assert run.state == "stopped: no_result"


def test_an_api_error_before_the_first_tool_call_is_retried_once_after_ten_minutes(world):
    clock = [time.time()]
    world.host.wall = lambda: clock[0]
    run = world.run("apierror")
    runs = build.runs_of(run.id, world.runs)
    assert [r.state for r in runs] == ["stopped: error_during_execution", "queued"]
    assert "one retry in 10 min" in (runs[0].folder / "result.md").read_text()
    world.task("apierror", tid=run.id)
    assert world.runner().pick(build.read_queue(world.queue)[0]) is None      # not before ten minutes
    clock[0] += 601
    assert world.runner().poll_once()
    runs = build.runs_of(run.id, world.runs)
    assert [r.state for r in runs] == ["stopped: error_during_execution"] * 2      # once, never a third run


def test_stop_id_stops_the_session_with_the_one_stop_form(world, monkeypatch):
    tid = world.task("slow", extra="WAIT: 30\n")
    sleep = world.host.sleep
    calls = []

    def planted(sec):
        r = build.latest(tid, world.runs)
        if r is not None and r.state == "running" and not calls and world.units.handles.get("awb-task-%s" % tid):
            calls.append(build.cmd_stop(tid, world.runs))
        sleep(sec)

    world.host.sleep = planted
    world.runner().poll_once()
    assert calls == [0]
    assert build.latest(tid, world.runs).state == "stopped: by_owner"
    assert world.units.stopped == ["awb-task-%s" % tid]


# --------------------------------------------------------------------------- main moves, the merge, the push

def test_main_that_moved_means_a_rebase_and_a_second_round_and_a_conflict_is_stale(world, monkeypatch):
    real = buildrun.Runner.judge
    moved = []

    def judge(self, ctx):
        if not moved:
            moved.append(move_main(world, "README.md", "# tiny\n\nhis line\n"))
        return real(self, ctx)

    monkeypatch.setattr(buildrun.Runner, "judge", judge)
    run = world.run("good")
    assert run.state == "done"
    assert "main moved" in checks_of(run)
    log = world.g("--git-dir", str(world.root / "main.git"), "log", "--format=%s", "main")
    assert "his own change" in log and "the value is two" in log
    # the same with a commit of his on the task's file: the rebase conflicts, the task is stale and main stays his
    moved.clear()

    def judge2(self, ctx):
        if not moved:
            moved.append(move_main(world, "awb/x.py", "VALUE = 9\n"))
        return real(self, ctx)

    monkeypatch.setattr(buildrun.Runner, "judge", judge2)
    run = world.run("outside2")
    assert run.state == "stale"
    assert main_of(world) == moved[0]


def test_update_ref_refuses_when_main_moved_between_the_check_and_the_merge(world, monkeypatch):
    real = buildrun.git
    moved = []

    def planted(host, *args, **kw):
        if args[:1] == ("update-ref",) and "refs/heads/main" in args and not moved:
            moved.append(move_main(world, "README.md", "# tiny\n\nmoved under the merge\n"))
        return real(host, *args, **kw)

    monkeypatch.setattr(buildrun, "git", planted)
    run = world.run("good")
    assert moved and run.state == "done"
    log = world.g("--git-dir", str(world.root / "main.git"), "log", "--format=%H", "main").split()
    assert moved[0] in log                                     # never rewound
    assert "main moved" in checks_of(run)


def test_a_refused_push_is_held_never_done_and_main_git_keeps_the_task(world):
    other = world.base / "other"
    world.g("clone", "-q", str(world.origin), str(other))
    (other / "README.md").write_text("# tiny\n\non the remote only\n", encoding="utf-8")
    world.g("commit", "-q", "-am", "on the remote", cwd=other)
    world.g("push", "-q", "origin", "main", cwd=other)
    run = world.run("good")
    assert run.state == "held: push refused"
    assert (run.folder / "commit").read_text().strip() == main_of(world)
    assert origin_of(world) != main_of(world)
    assert build.line(run) == "build %s held: push refused" % run.id
    # not tried again within ten minutes; then tried again, still held, mailed once
    mails = len(world.mails)
    assert world.runner().poll_once() is False
    later = datetime.now() + timedelta(minutes=11)
    world.host.now = lambda: later
    assert world.runner().poll_once() is True
    assert build.latest(run.id, world.runs).state == "held: push refused" and len(world.mails) == mails


def test_without_the_push_done_is_main_git(world):
    world.host.push = False
    run = world.run("good")
    assert run.state == "done" and origin_of(world) != main_of(world)


# --------------------------------------------------------------------------- the owner never enters the clone

def test_the_runner_never_runs_git_in_the_clone_and_never_reads_its_fsmonitor(world):
    calls = []
    real = world.host.git_run

    def record(cmd, **kw):
        calls.append((list(cmd), kw.get("cwd")))
        return real(cmd, **kw)

    world.host.git_run = record
    run = world.run("fsmonitor")
    assert run.state == "done"
    assert not (world.base / ("mark-%s" % run.id)).exists()
    assert calls
    wt = str(world.root / "wt")
    for cmd, cwd in calls:
        assert cmd[:3] == ["git", "-C", str(world.root / "main.git")], cmd[:3]
        assert cwd is None and not any(a.startswith(wt) and not a.endswith(".bundle") for a in cmd), cmd


# --------------------------------------------------------------------------- what he reads

MAIL_LINE = re.compile(r"(state: [a-z:. -]+|commits: \d+|suite: (\d+ passed, \d+ failed, \d+ skipped( \(\d+ min\))?|"
                       r"not run)|note: runs/B-\d{3,4}/\d+/result\.md|live \S+, main \S+, \d+ not deployed|"
                       r"next: [A-Za-z0-9 :./-]+|note: one retry in \d+ min)")


@pytest.mark.parametrize("scenario", ["good", "ask", "deleted"])
def test_the_mail_has_fixed_fields_only(world, scenario):
    world.run(scenario)
    subject, body = world.mails[-1]
    assert re.fullmatch(r"Workbench build B-\d{3,4}: [a-z]+", subject)
    for line in body.strip().split("\n"):
        assert MAIL_LINE.fullmatch(line), line
    assert "value" not in body.lower() and "?" not in body


def test_state_tsv_has_ids_states_hashes_and_times_only(world):
    for sc in ("good", "ask", "outside"):
        world.run(sc)
    rows = (world.board / "state.tsv").read_text().strip().split("\n")
    assert rows[0].startswith("# ")
    kinds = set()
    for row in rows[1:]:
        f = row.split("\t")
        kinds.add(f[0])
        if f[0] == "task":
            assert len(f) == 7 and re.fullmatch(r"B-\d{3,4}", f[1]) and f[2].isdigit()
            assert build.STATE_RE.fullmatch(f[3])
            assert re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d", f[4])
            assert re.fullmatch(r"[0-9a-f]{7}|-", f[5]) and f[6] in ("sent", "not_sent", "-")
        elif f[0] == "queue":
            assert f[1:] == ["ok", "-"]
        else:
            assert f[0] in ("live", "main", "not_deployed", "daemons") and len(f) == 2
            assert re.fullmatch(r"[0-9a-f]{7}|unknown|\d+|current|[a-z,]+", f[1])
    assert kinds == {"queue", "task", "live", "main", "not_deployed", "daemons"}


def test_live_main_and_not_deployed_come_from_the_deploy_record(world):
    first = world.run("good")
    live = (first.folder / "commit").read_text().strip()
    second = world.run("good")
    (world.base / "DEPLOYED").write_text(json.dumps({
        "target": live, "daemons": {"vault": "0" * 40, "keys": live},
        "daemon_code": {"awb-vaultd.service": {"release": "0" * 40, "code": "changed"}}}), encoding="utf-8")
    info = buildrun.deployed_info(world.host)
    assert info == {"live": live[:7], "main": main_of(world)[:7], "not_deployed": "1", "behind": ["vault"]}
    buildrun.write_result(world.host, buildrun.Ctx(build.parse((second.folder / "task.md").read_text())[0][0],
                                                   second), "done", info)
    text = (second.folder / "result.md").read_text()
    assert "live %s / main %s / 1 tasks not deployed; daemon code behind: vault" % (live[:7], main_of(world)[:7]) \
        in text


def test_a_subject_with_a_registered_form_is_withheld_from_result_md(world):
    from awb import gate

    run = world.run("subject")
    assert run.state == "red: gate name"
    text = (run.folder / "result.md").read_text()
    assert gate._SELFTEST_FORM not in text and "subject withheld: name" in text


def test_the_model_key_is_in_the_home_for_the_session_and_gone_with_it(world, tmp_path):
    key = tmp_path / "model.key"
    key.write_text("fake-model-key-for-tests\n")
    key.chmod(0o400)
    world.host.model_key = key
    run = world.run("key", extra="KEY: fake-model-key-for-tests\n")
    assert run.state == "done"
    assert not world.host.home(run.id, run.n).exists()
    for f in run.folder.iterdir():
        if f.is_file():
            assert b"fake-model-key" not in f.read_bytes() or f.name == "prompt.txt", f.name
    assert not any("fake-model-key" in m for _, m in world.mails)
    assert "fake-model-key" not in (world.board / "state.tsv").read_text()


def test_a_prompt_that_fails_the_name_check_is_held_and_starts_once_it_is_clean(world):
    from awb import gate

    tid = world.task("good", extra="for %s\n" % gate._SELFTEST_FORM)
    assert world.runner().poll_once()
    run = build.latest(tid, world.runs)
    assert run.state == "held: prompt" and not world.units.started
    (world.queue / ("%s.md" % tid)).write_text("SCENARIO: good\n", encoding="utf-8")
    world.host.poll = 0.0
    assert world.runner().poll_once()
    after = build.latest(tid, world.runs)
    assert (after.n, after.state) == (run.n, "done")


# --------------------------------------------------------------------------- the runner itself

def test_a_second_serve_is_refused_with_one_line(world, capsys):
    fd = buildrun.acquire_lock(world.runs)
    try:
        assert buildrun.Runner(world.host).serve() == 1
    finally:
        os.close(fd)
    assert capsys.readouterr().err.strip().count("\n") == 0


def test_serve_ends_on_the_stop_file_and_exits_for_a_restart_wanted(world):
    (world.runs).mkdir(exist_ok=True)
    (world.runs / build.STOP_FILE).write_text("stop\n")
    assert buildrun.Runner(world.host).serve() == 0
    (world.runs / "restart-wanted").write_text("x\n")
    assert buildrun.Runner(world.host).serve() == 75
    assert not (world.runs / "restart-wanted").exists()


def test_a_restart_reattaches_to_a_running_session(world):
    tid = world.task("slow", extra="WAIT: 1\n")
    started = world.runner().start(build.read_queue(world.queue)[0][0], None)
    assert started and build.latest(tid, world.runs).state == "running"
    world.runner().reconcile()
    assert build.latest(tid, world.runs).state == "done"


def test_a_restart_after_the_session_died_with_the_runner_is_interrupted(world):
    tid = world.task("slow", extra="WAIT: 30\n")
    ctx, handle, _ = world.runner().start(build.read_queue(world.queue)[0][0], None)
    os.killpg(handle.proc.pid, signal.SIGKILL)
    handle.proc.wait()
    world.units.handles.clear()                                    # a reboot: systemd knows the unit no more
    world.runner().reconcile()
    run = build.latest(tid, world.runs)
    assert run.state == "interrupted"
    assert build.line(run) == "build %s interrupted; awb build retry %s" % (tid, tid)


def test_the_preconditions_hold_the_queue_without_a_spawn_and_mail_once(world):
    world.host.precheck = lambda: "vault locked"
    world.task("good")
    r = world.runner()
    assert r.poll_once() is False and r.poll_once() is False
    assert world.units.started == [] and build.ids_with_runs(world.runs) == []
    assert len(world.mails) == 1 and "held: vault locked" in world.mails[0][1]
    assert "queue\theld\tvault locked" in (world.board / "state.tsv").read_text()
    world.host.precheck = lambda: None
    assert r.poll_once() is True


def test_a_name_check_that_cannot_run_after_a_run_is_held_and_reruns(world, monkeypatch):
    register = world.host.register
    tid = world.task("good")                 # the prompt's check at the start passes ...
    real = buildrun.check_gate

    def unavailable(host, ctx, identity):
        host.register = world.base / "no-register.tsv"        # ... and the vault locks before check 6
        try:
            return real(host, ctx, identity)
        finally:
            host.register = register

    monkeypatch.setattr(buildrun, "check_gate", unavailable)
    world.runner().poll_once()
    monkeypatch.setattr(buildrun, "check_gate", real)
    run = build.latest(tid, world.runs)
    assert run.state == "held: checks pending" and build.line(run) == "build %s held: checks pending" % tid
    assert world.runner().poll_once()
    assert build.latest(tid, world.runs).state == "done"


def test_the_reserved_paths_and_the_depends_order_the_pick(world):
    queue = ""
    for tid, paths, dep in (("B-201", "awb/", "none"), ("B-202", "awb/x.py", "none"), ("B-203", "tests/", "B-201")):
        (world.queue / ("%s.md" % tid)).write_text("SCENARIO: ask\n")
        queue += ("## %s: t\ndepends: %s\npaths: %s\ntests_replaced: none\ncheck: none\nprompt: build/queue/%s.md\n"
                  "end: %s\n" % (tid, dep, paths, tid, tid))
    (world.queue / "QUEUE.md").write_text(queue)
    r = world.runner()
    assert r.pick(build.read_queue(world.queue)[0])[0].id == "B-201"
    r.poll_once()
    assert build.latest("B-201", world.runs).state == "asked"
    # B-201 asked keeps awb/ reserved: B-202 waits; B-203 waits on B-201 to be done
    assert r.pick(build.read_queue(world.queue)[0]) is None


# --------------------------------------------------------------------------- the units

def test_the_real_spawn_is_one_fixed_systemd_run_form():
    units = buildrun.SystemdUnits("awb-build")
    spec = buildrun.Spec("awb-task-B-012", ["/opt/claude-build/bin/claude", "-p"], 5400, "/srv/awb-build/wt/B-012-1",
                         "/srv/awb-build/home/B-012-1", rw=["/srv/awb-build/wt/B-012-1", "/srv/awb-build/home/B-012-1"],
                         hidden=["/srv/awb-build/runs", "/srv/awb-build/wt/B-011-1"],
                         binds=[("/srv/awb-build/home-template/.gitconfig", "/srv/awb-build/home/B-012-1/.gitconfig")],
                         allow=["127.0.0.53", "192.0.2.10"], env={"DISABLE_AUTOUPDATER": "1"})
    assert units.argv(spec) == [
        "sudo", "-n", "systemd-run", "--quiet", "--wait", "--pipe", "--unit=awb-task-B-012.service",
        "--uid=awb-build", "--gid=awb-build", "--working-directory=/srv/awb-build/wt/B-012-1",
        "--setenv=HOME=/srv/awb-build/home/B-012-1", "--setenv=DISABLE_AUTOUPDATER=1",
        "--property=RuntimeMaxSec=5400", "--property=CPUQuota=200%", "--property=MemoryMax=6G",
        "--property=TasksMax=512", "--property=ProtectSystem=strict", "--property=ProtectHome=yes",
        "--property=PrivateTmp=yes", "--property=NoNewPrivileges=yes",
        "--property=ReadWritePaths=/srv/awb-build/wt/B-012-1 /srv/awb-build/home/B-012-1",
        "--property=InaccessiblePaths=-/etc/awb -/srv/awb-build/runs -/srv/awb-build/wt/B-011-1",
        "--property=BindReadOnlyPaths=/srv/awb-build/home-template/.gitconfig:/srv/awb-build/home/B-012-1/.gitconfig",
        "--property=IPAddressDeny=any", "--property=IPAddressAllow=127.0.0.53 192.0.2.10",
        "--", "/opt/claude-build/bin/claude", "-p"]
    # the suite unit allows no address; a unit name from the agent's side is refused
    assert "--property=IPAddressAllow" not in " ".join(units.argv(buildrun.Spec("awb-task-B-012-suite", ["x"], 1800,
                                                                                 "/tmp", "/tmp")))
    with pytest.raises(ValueError):
        units.argv(buildrun.Spec("awb-task-B-012; reboot", ["x"], 1, "/tmp", "/tmp"))


def test_the_client_argv_and_the_session_units_of_a_task(world):
    run = world.run("good")
    agent = [s for s in world.units.started if s.name == "awb-task-%s" % run.id][0]
    assert agent.argv == [buildrun.CLIENT, "-p", "--model", "opus", "--effort", "medium", "--max-budget-usd", "10",
                          "--permission-mode", "bypassPermissions", "--dangerously-skip-permissions",
                          "--permission-prompts", "none", "--setting-sources", "user", "--strict-mcp-config",
                          "--output-format", "json"]
    assert agent.seconds == 10 * world.host.minute
    assert agent.rw == [str(world.host.clone(run.id, 1)), str(world.host.home(run.id, 1))]
    assert str(world.runs) in agent.hidden
    assert {s.name for s in world.units.started} == {"awb-task-%s%s" % (run.id, k) for k in ("", "-prep", "-hand",
                                                                                              "-suite")}
    suite = [s for s in world.units.started if s.name.endswith("-suite")]
    assert all(s.allow == [] and s.rw == [str(world.host.out(run.id, 1))] for s in suite)
    assert all(str(world.host.clone(run.id, 1)) in s.hidden for s in suite)


def test_systemd_handle_reads_the_result_then_resets_the_unit():
    calls = []

    class Proc:
        pid = 1

        def poll(self):
            return 1

    def run(cmd, **kw):
        calls.append(cmd)
        out = "ActiveState=failed\nResult=timeout\n" if cmd[1] == "show" else ""
        return subprocess.CompletedProcess(cmd, 0, out, "")

    units = buildrun.SystemdUnits("awb-build", run=run, popen=lambda *a, **k: Proc())
    h = units.start(buildrun.Spec("awb-task-B-012", ["x"], 60, "/tmp", "/tmp"))
    assert h.poll() == buildrun.Ended("timeout", 1)
    assert calls == [["systemctl", "show", "-p", "ActiveState", "-p", "Result", "awb-task-B-012.service"],
                     ["sudo", "-n", "systemctl", "reset-failed", "awb-task-B-012.service"]]
    units.stop("awb-task-B-012")
    assert calls[-1] == ["sudo", "-n", "systemctl", "stop", "awb-task-B-012.service"]


def test_junit_outcomes_name_each_test_by_its_node_id():
    xml = (b'<testsuites><testsuite><testcase classname="tests.test_x" name="test_one" file="tests/test_x.py"/>'
           b'<testcase classname="tests.test_x.TestA" name="test_b[1]" file="tests/test_x.py"><skipped/></testcase>'
           b'<testcase classname="tests.test_x" name="test_c" file="tests/test_x.py"><failure/></testcase>'
           b'</testsuite></testsuites>')
    assert buildrun.outcomes(xml) == {"tests/test_x.py::test_one": "passed",
                                      "tests/test_x.py::TestA::test_b[1]": "skipped",
                                      "tests/test_x.py::test_c": "failed"}
    assert buildrun.outcomes(b'<!DOCTYPE x [<!ENTITY a "b">]><x/>') is None


def test_the_unit_helpers_never_run_as_the_owner_or_the_work_user(monkeypatch):
    from awb import config

    monkeypatch.setattr(config, "host_conf", lambda: {"owner": __import__("pwd").getpwuid(os.geteuid()).pw_name})
    assert buildrun.unit_allowed() is not None
    assert build.main(["unit", "handover", "/srv/awb-build", "B-001", "1"]) == 2


# --------------------------------------------------------------------------- the selftest

def test_the_selftest_refuses_or_classifies_every_planted_case():
    assert buildrun.selftest(python=sys.executable) == []


def _no_check(*a, **k):
    return None


def _clone_export(host, ctx, commit, name):
    """Planted: the suite runs on the agent's clone instead of the export of the commit."""
    import tarfile

    path = ctx.folder / name
    with tarfile.open(path, "w") as tar:
        tar.add(host.clone(ctx.tid, ctx.n), arcname=".")
    return path


def _shared_home(root, tid, n):
    r, clone, out, _ = buildrun._HELPER_PLACES(root, tid, n)
    return r, clone, out, Path(root) / "home" / "shared"


def _hgit_with_fsmonitor(env, *args, timeout=300, stdin=None):
    return subprocess.run(["git", *args], env=env, capture_output=True, timeout=timeout, check=False, stdin=stdin)


def _lax_snapshot(host, ctx, prompt):
    os.symlink(host.queue / ctx.task.prompt_name, ctx.folder / "prompt.txt")


PLANTED_AWAY = [
    ("n6-prompt", buildrun, "snapshot_prompt", _lax_snapshot),
    ("n6-home", buildrun, "_helper_places", _shared_home),
    ("n7-clock", buildrun, "check_unit", _no_check),
    ("n9-deleted", buildrun, "check_hollow", _no_check),
    ("n9-skipped", buildrun, "check_hollow", _no_check),
    ("n9-conftest", buildrun, "export", _clone_export),
    ("n9-outside", buildrun, "check_paths", _no_check),
    ("n10-coauthor", buildrun, "check_gate", _no_check),
    ("n10-author", buildrun, "check_identity", _no_check),
    ("n11-newline", buildrun, "check_paths", _no_check),
    ("n11-subject", buildrun, "check_gate", _no_check),
    ("n11-check", build, "_check_tokens", lambda value: tuple(value.split())),
    ("n11-fsmonitor", buildrun, "_hgit", _hgit_with_fsmonitor),
    ("n12-lock", buildrun, "acquire_lock", lambda runs: os.open(os.devnull, os.O_RDONLY)),
    ("n12-reattach", buildrun.LocalUnits, "attach", lambda self, name: None),
    ("push-nonff", buildrun, "push_main", lambda host, register: "pushed"),
]


def test_every_planted_case_is_named():
    assert sorted(c for c, *_ in PLANTED_AWAY) == sorted(name for name, _ in buildrun.SELFTEST_CASES)


@pytest.mark.parametrize("case, module, attr, planted", PLANTED_AWAY, ids=[c for c, *_ in PLANTED_AWAY])
def test_each_selftest_case_fails_when_its_check_is_taken_away(monkeypatch, case, module, attr, planted):
    assert buildrun.selftest(only=[case], python=sys.executable) == []
    if attr == "_helper_places":
        monkeypatch.setattr(buildrun, "_HELPER_PLACES", buildrun._helper_places, raising=False)
    monkeypatch.setattr(module, attr, planted)
    failures = buildrun.selftest(only=[case], python=sys.executable)
    assert len(failures) == 1 and failures[0].startswith(case + ": "), failures


# --------------------------------------------------------------------------- the login mode (D-S7 changed)

TOKEN = "fake-" + "login-token-one"


def plant_login(w, token: str = TOKEN, expires: int = 1_900_000_000_000) -> Path:
    """A fake login of the client in the build user's own home (<root>/home/.claude/.credentials.json, 600)."""
    d = w.root / "home" / ".claude"
    d.mkdir(mode=0o700, exist_ok=True)
    f = d / ".credentials.json"
    f.write_text(json.dumps({"claudeAiOauth": {"accessToken": token, "refreshToken": token + "-r",
                                               "expiresAt": expires, "scopes": ["user:inference"]}}))
    f.chmod(0o600)
    return f


def cred(login: Path) -> str:
    """The prompt line the stand-in client checks the task home's copy against: a digest, never the token."""
    import hashlib

    return "CREDSHA: %s\n" % hashlib.sha256(login.read_bytes()).hexdigest()


def nowhere(w, run, needle: str) -> None:
    """The needle is in no file of runs/ (every run, every record), no board file and no mail."""
    for f in w.runs.rglob("*"):
        if f.is_file() and not f.is_symlink():
            assert needle.encode() not in f.read_bytes(), f
    for f in w.board.rglob("*"):
        if f.is_file():
            assert needle.encode() not in f.read_bytes(), f
    assert not any(needle in s + m for s, m in w.mails)


def test_login_mode_copies_the_login_into_the_task_home_for_the_session_and_never_into_runs(world):
    """D-S7 changed: with build_model = login the prepare unit copies the build user's login into the task home
    (600, read by the stand-in client there), the session and the other units do not see the build user's own client
    state, the home goes with its copy when the task is done, the login stays where it was and its bytes are in no
    record, log, board file or mail."""
    world.host.model = "login"
    login = plant_login(world)
    before = login.read_bytes()
    run = world.run("login", extra=cred(login))
    assert run.state == "done"
    assert not world.host.home(run.id, run.n).exists()
    assert login.read_bytes() == before and oct(login.stat().st_mode & 0o777) == "0o600"
    nowhere(world, run, TOKEN)
    specs = {s.name: s for s in world.units.started}
    prep = [s for s in world.units.started if s.name.endswith("-prep")][0]
    assert prep.argv[-1] == "login" and prep.argv[-6] == "prepare"
    own = str(world.root / "home" / ".claude")
    assert own in specs["awb-task-%s" % run.id].hidden
    assert own in specs["awb-task-%s-suite" % run.id].hidden
    assert own not in prep.hidden
    cleans = [s for s in world.units.started if s.argv[3:4] == ["clean"]]
    assert cleans and all(s.argv[-1] == "login" and own not in s.hidden for s in cleans)
    hands = [s for s in world.units.started if s.argv[3:4] == ["handover"]]
    assert hands and all(own in s.hidden for s in hands)


def test_login_mode_without_a_login_holds_the_task_at_once_names_the_command_and_retries_nothing(world):
    """No login in the build user's home: the task ends held: login missing before a session (nothing made, no
    session unit), the mail and result.md name the one login command, later polls start neither this task nor the
    next one; after his login and awb build retry the task runs and is done."""
    world.host.model = "login"
    run = world.run("login")
    assert run.state == buildrun.LOGIN_HELD
    assert not any(s.name == "awb-task-%s" % run.id for s in world.units.started)
    assert not world.host.home(run.id, run.n).exists() and not world.host.clone(run.id, run.n).exists()
    mail = [m for s, m in world.mails if run.id in s]
    assert len(mail) == 1 and mail[0].count(buildrun.LOGIN_COMMAND) == 1
    assert "awb build retry %s" % run.id in mail[0]
    assert buildrun.LOGIN_COMMAND in (run.folder / "result.md").read_text()
    assert buildrun.LOGIN_COMMAND == "sudo -u awb-build -H /opt/claude-build/bin/claude login"
    started = len(world.units.started)
    world.task("login", tid=run.id)
    other = world.task("good")
    (world.queue / "QUEUE.md").write_text((world.queue / "QUEUE.md").read_text())
    r = world.runner()
    for _ in range(3):
        r.poll_once()
    assert len(world.units.started) == started
    assert build.latest(run.id, world.runs).n == run.n and build.runs_of(other, world.runs) == []
    assert r.held_queue == "login missing"
    world.task("login", tid=run.id, extra=cred(plant_login(world)))
    assert build.cmd_retry(run.id, world.queue, world.runs) == 0
    for _ in range(5):
        if not r.poll_once():
            break
    after = build.latest(run.id, world.runs)
    assert (after.n, after.state) == (run.n + 1, "done")


def test_login_mode_asked_keeps_the_home_without_the_login_and_the_resume_copies_it_again(world):
    world.host.model = "login"
    login = plant_login(world)
    run = world.run("ask")
    assert run.state == "asked"
    home = world.host.home(run.id, run.n)
    assert home.is_dir() and not (home / buildrun.LOGIN_IN_HOME).exists()
    assert build.cmd_answer(run.id, "SCENARIO: login\n" + cred(login), world.runs, world.register) == 0
    world.task("login", tid=run.id)
    world.runner().poll_once()
    after = build.latest(run.id, world.runs)
    assert (after.n, after.state) == (run.n, "done")
    rekey = [s for s in world.units.started if s.argv[3:4] == ["rekey"]]
    assert len(rekey) == 1 and rekey[0].argv[-1] == "login"
    nowhere(world, run, TOKEN)


def test_login_mode_a_refreshed_token_goes_back_and_nothing_else_does(world, tmp_path):
    """A refresh may rotate the refresh token: a newer login of the task goes back to the build user's home
    before the task home goes. A file that is not the login's shape, an older one and a link never do, and a login
    that is not there is never made."""
    world.host.model = "login"
    login = plant_login(world)
    new = TOKEN + "-refreshed"
    run = world.run("refresh", extra=cred(login))
    assert run.state == "done"
    back = json.loads(login.read_text())["claudeAiOauth"]
    assert back["accessToken"] == new and back["expiresAt"] == 1_900_000_000_000 + 3600000
    assert oct(login.stat().st_mode & 0o777) == "0o600"
    nowhere(world, run, new)
    # the clean unit on planted task homes
    root = world.root
    home = root / "home" / "B-900-1"

    def task_home(content: str | None, link: bool = False):
        import shutil

        shutil.rmtree(home, ignore_errors=True)
        (home / ".claude").mkdir(parents=True)
        f = home / buildrun.LOGIN_IN_HOME
        if link:
            f.symlink_to(tmp_path / "elsewhere.json")
            (tmp_path / "elsewhere.json").write_text(content or "")
        elif content is not None:
            f.write_text(content)

    good = login.read_bytes()
    exp = back["expiresAt"]
    for content, link in ((json.dumps({"claudeAiOauth": {"accessToken": "x", "refreshToken": "y",
                                                         "expiresAt": exp - 1}}), False),
                          (json.dumps({"other": {"expiresAt": exp + 9}}), False),
                          (json.dumps({"claudeAiOauth": {"accessToken": "x", "expiresAt": exp + 9}}), False),
                          ("not json", False),
                          (json.dumps({"claudeAiOauth": {"accessToken": "x", "refreshToken": "y",
                                                         "expiresAt": exp + 9}}), True)):
        task_home(content, link)
        assert buildrun.unit_clean(str(root), "B-900", "1", "home", "login") == 0
        assert login.read_bytes() == good and not home.exists()
    # key mode never sends anything back
    task_home(json.dumps({"claudeAiOauth": {"accessToken": "x", "refreshToken": "y", "expiresAt": exp + 9}}))
    assert buildrun.unit_clean(str(root), "B-900", "1", "home") == 0
    assert login.read_bytes() == good
    # no login there: none is made
    login.unlink()
    task_home(json.dumps({"claudeAiOauth": {"accessToken": "x", "refreshToken": "y", "expiresAt": exp + 9}}))
    assert buildrun.unit_clean(str(root), "B-900", "1", "home", "login") == 0
    assert not login.exists()


def test_key_mode_still_hands_the_key_and_ignores_a_login_that_is_there(world, tmp_path):
    """build_model = key (7fac409): the key goes into the task home as before and the login of the build user's home
    is not copied; the stand-in that wants a login asks."""
    key = tmp_path / "model.key"
    key.write_text("fake-model-key-for-tests\n")
    key.chmod(0o400)
    world.host.model, world.host.model_key = "key", key
    login = plant_login(world)
    run = world.run("key", extra="KEY: fake-model-key-for-tests\n")
    assert run.state == "done"
    prep = [s for s in world.units.started if s.name.endswith("-prep")][0]
    assert prep.argv[-1] != "login"
    run = world.run("login", extra=cred(login))
    assert run.state == "asked"
    assert not (world.host.home(run.id, run.n) / buildrun.LOGIN_IN_HOME).exists()
    nowhere(world, run, TOKEN)


def test_a_build_model_outside_login_and_key_holds_the_queue_and_the_host_file_defaults_to_login(world, monkeypatch):
    world.host.model = "both"
    world.task("good")
    r = world.runner()
    assert not r.poll_once() and r.held_queue == "build model" and not world.units.started
    from awb import config

    for conf, model, key in (({}, "login", None),
                             ({"build_model_key": "/etc/awb/build-model.key"}, "login", None),
                             ({"build_model": "key", "build_model_key": "/k"}, "key", Path("/k"))):
        monkeypatch.setattr(config, "host_conf", lambda conf=conf: dict(conf))
        h = buildrun.Host.from_config()
        assert (h.model, h.model_key) == (model, key)
