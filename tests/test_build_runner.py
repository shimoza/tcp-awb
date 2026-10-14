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
import types
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


@pytest.mark.parametrize("scenario, state", [
    ("bigblob", "red: blob too big"),
    ("bigmessage", "red: message too big"),
])
def test_a_blob_or_message_above_the_bound_is_red_before_any_read_and_main_stays(world, scenario, state):
    """B3 of docs/review-2026-10-13.md: every changed blob and every commit is sized with `git cat-file
    --batch-check` before any read, and one above BLOB_MAX (16 MB) is red at check 4. Planted: a blob of 16 MB and
    one byte of zeros (a few KB in the bundle) and a message of the same size; neither is ever read whole."""
    calls = []
    real = world.host.git_run

    def record(cmd, **kw):
        calls.append(list(cmd))
        return real(cmd, **kw)

    world.host.git_run = record
    start = main_of(world)
    run = world.run(scenario)
    assert run.state == state
    assert main_of(world) == start and origin_of(world) == start
    assert build.line(run) == "build %s %s; branch task/%s kept" % (run.id, state, run.id)
    assert "FAIL" in checks_of(run)
    assert any("--batch-check" in " ".join(cmd) for cmd in calls)
    for cmd in calls:                                          # main's own files only, no message
        if "cat-file" in cmd and "blob" in cmd:
            assert cmd[-1].startswith(start + ":"), cmd[-3:]
        assert not any("%B" in a for a in cmd), cmd[-3:]


def test_the_runner_unit_has_a_memory_limit_that_fits_the_fetch_of_a_full_bundle():
    """B3: seal/awb-buildd.service caps the runner's memory, so a read that grows cannot reach the vault daemon or
    the key service, with room for the fetch of a bundle of BUNDLE_MAX and the gate over blobs of BLOB_MAX."""
    unit = (Path(buildrun.__file__).parents[1] / "seal" / "awb-buildd.service").read_text(encoding="utf-8")
    values = re.findall(r"^MemoryMax=(\d+)([MG])$", unit, re.M)
    assert len(values) == 1
    size = int(values[0][0]) << (20 if values[0][1] == "M" else 30)
    assert buildrun.BLOB_MAX == 16 << 20
    assert 4 * buildrun.BUNDLE_MAX <= size < 6 << 30


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

def _gone_within(pid, seconds):
    """True once the process is gone or only a zombie. SIGKILL ends it at once but a dead orphan stays a zombie
    until its new parent reaps it (a container's pid 1 may take seconds), and os.kill(pid, 0) still succeeds on a
    zombie. A zombie runs nothing, so it is not a child left."""
    end = time.monotonic() + seconds
    while True:
        try:
            state = (Path("/proc/%d/stat" % pid).read_text().rsplit(")", 1)[1].split()[0])
        except (OSError, IndexError):
            return True
        if state in ("Z", "X"):
            return True
        if time.monotonic() > end:
            return False
        time.sleep(0.02)


def test_a_session_that_never_returns_under_a_2_s_clock_is_hung_within_5_s_and_no_child_is_left(world):
    world.host.minute = 0.2
    t0 = time.monotonic()
    run = world.run("hang")
    assert run.state == "hung" and time.monotonic() - t0 < 5
    pid = int((world.base / ("mark-%s" % run.id)).read_text())
    assert _gone_within(pid, 2)
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

def test_the_real_spawn_is_one_fixed_form_and_the_suite_unit_has_its_own_loopback_alone():
    """Replaces test_the_real_spawn_is_one_fixed_systemd_run_form_with_its_failure_oneshot (B-001: the suite unit
    allowed no address, so every test that talks to a stand-in server on 127.0.0.1 waited for its timeout and the
    suite hit the clock; it now gets a network namespace of its own and loopback alone)."""
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
        "--property=OnFailure=awb-task-failed@awb-task-B-012.service",
        "--", "/opt/claude-build/bin/claude", "-p"]
    assert "--property=OnFailure=awb-task-failed@awb-task-B-012-suite.service" in units.argv(
        buildrun.Spec("awb-task-B-012-suite", ["x"], 1800, "/tmp", "/tmp"))
    assert not [a for a in units.argv(buildrun.Spec("awb-task-probe", ["x"], 60, "/", "/tmp")) if "OnFailure" in a]
    # the suite unit: its own network namespace, everything denied but loopback; the session unit has no namespace
    suite = units.argv(buildrun.Spec("awb-task-B-012-suite", ["x"], 1800, "/tmp", "/tmp",
                                     allow=list(buildrun.SUITE_ALLOW), netns=True))
    net = [a for a in suite if a.startswith(("--property=PrivateNetwork", "--property=IPAddress"))]
    assert net == ["--property=PrivateNetwork=yes", "--property=IPAddressDeny=any",
                   "--property=IPAddressAllow=localhost"]
    assert suite.index("--property=PrivateNetwork=yes") < suite.index("--")
    assert not [a for a in units.argv(spec) if "PrivateNetwork" in a]
    # a unit name from the agent's side is refused
    with pytest.raises(ValueError):
        units.argv(buildrun.Spec("awb-task-B-012; reboot", ["x"], 1, "/tmp", "/tmp"))


def test_the_client_argv_the_session_units_and_the_suite_on_its_own_loopback(world):
    """Replaces test_the_client_argv_and_the_session_units_of_a_task (B-001: the suite unit pinned allow == [];
    it now allows loopback alone, in a namespace of its own, and the session unit keeps its own allow list)."""
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
    assert all(s.allow == ["localhost"] and s.netns and s.rw == [str(world.host.out(run.id, 1))] for s in suite)
    assert not agent.netns and "localhost" not in agent.allow
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

@pytest.mark.long   # the self-test runs every planted case in a real runner world and that count is the proof
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


# --------------------------------------------------------------------------- SB5: the probe unit and a failed task unit

ALLOW = ["127.0.0.53", "192.0.2.10", "192.0.2.11"]


def probe_host(w, tmp_path, *, reach=("api.anthropic.com", "platform.claude.com"), who=None, model="login",
               unresolved=()):
    """The world's host with LocalUnits whose `id -un` prints `who` and whose python connects to the hosts of
    `reach` only (exit 0), finds no address for the hosts of `unresolved` (exit 2), every other host refused (exit
    1): no network is touched."""
    import dataclasses
    import getpass

    fake = tmp_path / "fake"
    fake.mkdir(exist_ok=True)
    (fake / "python").write_text("#!/bin/sh\nfor a; do last=\"$a\"; done\ncase \" %s \" in *\" $last \"*) exit 0 ;; "
                                 "esac\ncase \" %s \" in *\" $last \"*) exit 2 ;; esac\nexit 1\n"
                                 % (" ".join(reach), " ".join(unresolved)))
    (fake / "id").write_text("#!/bin/sh\necho %s\n" % (who or getpass.getuser()))
    for f in fake.iterdir():
        f.chmod(0o755)
    units = buildrun.LocalUnits({"/usr/bin/python3": [str(fake / "python")], "/usr/bin/id": [str(fake / "id")]})
    return dataclasses.replace(w.host, units=units, model=model, resolve=lambda: list(ALLOW))


def test_the_probe_script_tells_no_name_resolution_from_no_connection(tmp_path):
    """The probe's exit: 2 when the name does not resolve, 1 when it resolves and no connection opens."""
    import socket

    run = [sys.executable, "-I", "-c", buildrun.PROBE_PY]
    assert subprocess.run(run + ["awb-probe-nowhere.invalid"], cwd=tmp_path, timeout=60).returncode == 2
    try:
        socket.create_connection(("127.0.0.1", 443), timeout=2).close()
        pytest.skip("something listens on port 443 here")
    except OSError:
        pass
    assert subprocess.run(run + ["127.0.0.1"], cwd=tmp_path, timeout=60).returncode == 1
    assert buildrun.PROBE_EXIT == {1: "no connection", 2: "no name resolution"}


def resolv_world(tmp_path, monkeypatch, text="nameserver 127.0.0.53\noptions edns0\n"):
    """/etc/resolv.conf as on the host: a relative link to systemd-resolved's stub file under /run, both planted in
    a temporary folder; RESOLV and RESOLV_PLACES point there and the model endpoint resolves to fixed addresses."""
    etc, stub = tmp_path / "etc", tmp_path / "run" / "systemd" / "resolve" / "stub-resolv.conf"
    etc.mkdir()
    stub.parent.mkdir(parents=True)
    stub.write_text(text)
    stub.chmod(0o644)
    (etc / "resolv.conf").symlink_to("../run/systemd/resolve/stub-resolv.conf")
    monkeypatch.setattr(buildrun, "RESOLV", etc / "resolv.conf")
    monkeypatch.setattr(buildrun, "RESOLV_PLACES", (str(etc), str(tmp_path / "run")))
    fake = {"api.anthropic.com": "192.0.2.10", "platform.claude.com": "192.0.2.11"}
    monkeypatch.setattr(buildrun.socket, "getaddrinfo",
                        lambda name, *a, **k: [(2, 1, 6, "", (fake[name], 443))])
    return etc / "resolv.conf", stub


def test_no_resolver_holds_the_queue_and_the_probe_starts_no_unit(world, tmp_path, monkeypatch):
    """Fix of 2026-10-11: without a nameserver a unit with IPAddressDeny=any can resolve no name. model_allow is
    None, the runner holds the queue with one line before a unit starts, the probe fails every line with it."""
    import dataclasses
    import getpass

    resolv_world(tmp_path, monkeypatch, text="# no nameserver here\n")
    assert buildrun.resolvers() == []
    assert buildrun.model_allow(dataclasses.replace(world.host, resolve=None)) is None
    host = dataclasses.replace(probe_host(world, tmp_path), resolve=None)
    res = buildrun.run_probe(host, getpass.getuser())
    assert not any(ok for _, ok, _ in res) and not host.units.started
    assert all(ln.endswith(" (no resolver for the task unit)") for _, _, ln in res)
    world.task("good")
    world.host.resolve = None
    r = world.runner()
    assert not r.poll_once() and r.held_queue == "no resolver for the task unit" and not world.units.started


def upstream_world(tmp_path, monkeypatch, upstream="nameserver 192.0.2.53\nnameserver 198.51.100.53\n"):
    """The host of the finding of 2026-10-12: /etc/resolv.conf links to systemd-resolved's stub file (127.0.0.53) and
    /run/systemd/resolve/resolv.conf names the upstream servers (none when `upstream` is None), planted in a
    temporary folder; the model endpoint and the token host resolve to fixed addresses."""
    link, stub = resolv_world(tmp_path, monkeypatch)
    up = stub.parent / "resolv.conf"
    if upstream is not None:
        up.write_text(upstream)
        up.chmod(0o644)
    monkeypatch.setattr(buildrun, "UPSTREAM_RESOLV", up)
    return link, stub, up


def test_the_allow_list_names_the_endpoints_alone_and_never_a_nameserver(world, tmp_path, monkeypatch):
    """Replaces test_the_task_units_resolve_through_the_upstream_nameservers_and_never_the_stub (review of 2026-10-13,
    finding B4: IPAddressAllow= takes no port, so an upstream nameserver in the list was open on every port). The
    allow list names the endpoints' addresses alone, each endpoint with its own addresses for the hosts file; the
    queue is still held without an upstream nameserver. Planted: the stub alone, no upstream file, a link out of /etc
    and /run, a file others may write, a loop."""
    import dataclasses

    link, stub, up = upstream_world(tmp_path, monkeypatch)
    host = dataclasses.replace(world.host, resolve=None, model="login")
    assert buildrun.resolvers() == ["192.0.2.53", "198.51.100.53"]
    assert buildrun.model_allow(host) == (["192.0.2.10", "192.0.2.11"], {"api.anthropic.com": ["192.0.2.10"],
                                                                         "platform.claude.com": ["192.0.2.11"]})
    assert buildrun.model_allow(dataclasses.replace(host, model="key")) == (["192.0.2.10"],
                                                                            {"api.anthropic.com": ["192.0.2.10"]})
    assert buildrun.model_allow(dataclasses.replace(host, resolve=lambda: ["192.0.2.30"])) == (["192.0.2.30"], {})
    # the stub alone, a loopback or a scoped address in the upstream file: no nameserver
    up.write_text("nameserver 127.0.0.53\nnameserver ::1\nnameserver fe80::1%eth0\nnameserver 0.0.0.0\n")
    assert buildrun.resolvers() == [] and buildrun.model_allow(host) is None
    up.write_text("nameserver 127.0.0.53\nnameserver 192.0.2.53\n")
    assert buildrun.resolvers() == ["192.0.2.53"]
    # no upstream file: the stub of /etc/resolv.conf is never taken instead
    up.unlink()
    assert buildrun.resolvers() == [] and buildrun.model_allow(host) is None
    # a link out of /etc and /run, a file others may write, a loop
    elsewhere = tmp_path / "home" / "resolv.conf"
    elsewhere.parent.mkdir()
    elsewhere.write_text("nameserver 192.0.2.99\n")
    elsewhere.chmod(0o644)
    up.symlink_to(elsewhere)
    assert buildrun.resolvers() == []
    up.unlink()
    up.symlink_to("../../../etc/resolv.conf")
    assert buildrun.resolvers() == []
    up.unlink()
    up.write_text("nameserver 192.0.2.53\n")
    up.chmod(0o666)
    assert buildrun.resolvers() == []
    up.unlink()
    up.symlink_to("resolv.conf")
    assert buildrun.resolvers() == []
    # a link inside /run to a system file is followed
    up.unlink()
    real = stub.parent / "upstream.conf"
    real.write_text("nameserver 192.0.2.54\n")
    real.chmod(0o644)
    up.symlink_to("upstream.conf")
    assert buildrun.resolvers() == ["192.0.2.54"]
    # an endpoint that resolves to the host itself holds the task
    monkeypatch.setattr(buildrun.socket, "getaddrinfo", lambda name, *a, **k: [(2, 1, 6, "", ("127.0.0.1", 443))])
    assert buildrun.model_allow(host) is None


def test_no_upstream_nameserver_holds_the_queue_with_one_line(world, tmp_path, monkeypatch):
    """Finding of 2026-10-12: a host whose /etc/resolv.conf names the stub alone and that has no upstream file holds
    the queue as no resolver for the task unit before a unit starts; the probe fails every line with it."""
    import dataclasses
    import getpass

    upstream_world(tmp_path, monkeypatch, upstream=None)
    host = dataclasses.replace(probe_host(world, tmp_path), resolve=None)
    res = buildrun.run_probe(host, getpass.getuser())
    assert not any(ok for _, ok, _ in res) and not host.units.started
    assert all(ln.endswith(" (no resolver for the task unit)") for _, _, ln in res)
    world.task("good")
    world.host.resolve = None
    r = world.runner()
    assert not r.poll_once() and r.held_queue == "no resolver for the task unit" and not world.units.started


def test_the_session_unit_binds_a_hosts_file_and_a_resolv_conf_without_a_nameserver(world, tmp_path, monkeypatch):
    """Replaces test_the_session_unit_binds_a_resolv_conf_of_the_upstream_nameservers (review of 2026-10-13, finding
    B4: the upstream nameservers in the allow list were open on every port and protocol, a channel through DNS and on
    some clouds the metadata API). The session unit allows the endpoints' addresses alone, no nameserver; it binds a
    hosts file of loopback and the endpoints the runner resolved over /etc/hosts and a resolv.conf without a
    nameserver over /etc/resolv.conf, both in the run folder, mode 644. The suite unit keeps loopback alone."""
    upstream_world(tmp_path, monkeypatch)
    world.host.resolve = None
    run = world.run("good")
    agent = [s for s in world.units.started if s.name == "awb-task-%s" % run.id][0]
    login = world.host.model == "login"
    allow = ["192.0.2.10"] + (["192.0.2.11"] if login else [])
    assert agent.allow == allow
    assert not {"192.0.2.53", "198.51.100.53", "127.0.0.53"} & set(agent.allow)
    hosts, resolv = run.folder / "hosts", run.folder / "resolv.conf"
    assert [(s, d) for s, d in agent.binds if d in ("/etc/hosts", "/etc/resolv.conf")] == [
        (str(hosts), "/etc/hosts"), (str(resolv), "/etc/resolv.conf")]
    assert hosts.read_text() == "127.0.0.1\tlocalhost\n::1\tlocalhost\n192.0.2.10\tapi.anthropic.com\n" + (
        "192.0.2.11\tplatform.claude.com\n" if login else "")
    assert not re.search(r"(?m)^\s*nameserver", resolv.read_text())
    assert hosts.stat().st_mode & 0o777 == 0o644 and resolv.stat().st_mode & 0o777 == 0o644
    argv = buildrun.SystemdUnits("awb-build").argv(agent)
    assert "--property=BindReadOnlyPaths=%s:/etc/hosts" % hosts in argv
    assert "--property=BindReadOnlyPaths=%s:/etc/resolv.conf" % resolv in argv
    allowed = "--property=IPAddressAllow=%s" % " ".join(allow)
    assert allowed in argv and argv.index("--property=IPAddressDeny=any") < argv.index(allowed)
    for s in world.units.started:
        if s.name.endswith("-suite"):
            assert s.allow == ["localhost"] and s.netns and not [d for _, d in s.binds if d.startswith("/etc/")]


HOST_LINES = ["the probe unit cannot connect to the host's loopback (127.0.0.1:22)",
              "the probe unit cannot connect to the resolver stub's address (127.0.0.53:22)",
              "the probe unit cannot connect to the host's own address on port 22"]


def test_the_probe_runs_with_a_task_s_own_network_and_names_and_cannot_reach_the_host_itself(world, tmp_path,
                                                                                             monkeypatch):
    """Replaces test_the_probe_runs_with_a_task_s_own_network_and_cannot_reach_the_host_itself (review of 2026-10-13,
    finding B4: the probe binds the hosts file and the resolv.conf of a task's session). Each probe is a unit of the
    runner's one form with the address list of a task's session, the session's hidden places, environment and the
    binds over /etc/hosts and /etc/resolv.conf; the control shows the build user first. Planted: GitHub, the
    loopback, the stub and the host's address reachable each fail their line; another user in the control fails
    every line; the key mode has no token line."""
    import getpass

    monkeypatch.setattr(buildrun, "host_address", lambda: "192.0.2.20")
    user = getpass.getuser()
    host = probe_host(world, tmp_path)
    res = buildrun.run_probe(host, user)
    keys = ["unit", "model", "token", "tcp", "github", "loopback", "stub", "host"]
    assert [(k, ok) for k, ok, _ in res] == [(k, True) for k in keys]
    assert [ln for _, _, ln in res] == [ln for _, ln in buildrun.probe_lines("login")]
    assert [ln for _, ln in buildrun.probe_lines("login")][-3:] == HOST_LINES
    specs = host.units.started
    assert len(specs) == 8 and {s.name for s in specs} == {"awb-task-probe"}
    assert all(s.allow == ALLOW and s.hidden == buildrun._hidden(host, None) for s in specs)
    assert all(s.env == {"CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1", "DISABLE_AUTOUPDATER": "1"} for s in specs)
    assert all([d for _, d in s.binds] == ["/etc/hosts", "/etc/resolv.conf"] for s in specs)
    assert [s.argv[4:] for s in specs[1:]] == [["api.anthropic.com"], ["platform.claude.com"],
                                               ["iam.eu-de.otc.t-systems.com"], ["github.com"], ["22", "127.0.0.1"],
                                               ["22", "127.0.0.53"], ["22", "192.0.2.20"]]
    assert buildrun.write_probe(host, res)
    ok, what, note = buildrun.probe_check(host.board)
    assert ok and "passed its 8 checks" in what and note == ""
    for target, key in (("github.com", "github"), ("127.0.0.1", "loopback"), ("127.0.0.53", "stub"),
                        ("192.0.2.20", "host")):
        bad = buildrun.run_probe(probe_host(world, tmp_path, reach=("api.anthropic.com", "platform.claude.com",
                                                                     target)), user)
        assert [(k, ln) for k, ok, ln in bad if not ok] == [(key, dict(buildrun.probe_lines("login"))[key]
                                                             + " (connected)")]
        buildrun.write_probe(host, bad)
        ok, _, note = buildrun.probe_check(host.board)
        assert not ok and note == "failed: %s" % key
    # no address of the host found: the line fails, the others stand
    monkeypatch.setattr(buildrun, "host_address", lambda: None)
    lost = buildrun.run_probe(probe_host(world, tmp_path), user)
    assert [(k, ln) for k, ok, ln in lost if not ok] == [("host", HOST_LINES[2] + " (no address of the host found)")]
    monkeypatch.setattr(buildrun, "host_address", lambda: "192.0.2.20")
    # planted: the unit does not run as the build user
    other = buildrun.run_probe(probe_host(world, tmp_path, who="someone"), user)
    assert not any(ok for _, ok, _ in other) and all(ln.endswith("(the probe unit did not run)")
                                                    for _, _, ln in other[1:])
    # the key mode: no token host
    key = buildrun.run_probe(probe_host(world, tmp_path, model="key"), user)
    assert [k for k, _, _ in key] == ["unit", "model", "tcp", "github", "loopback", "stub", "host"]
    assert all(ok for _, ok, _ in key)
    # the endpoint does not resolve: every line fails
    import dataclasses

    none = buildrun.run_probe(dataclasses.replace(host, resolve=lambda: None), user)
    assert not any(ok for _, ok, _ in none)


def test_the_probe_binds_a_hosts_file_and_never_allows_a_nameserver(world, tmp_path, monkeypatch):
    """Replaces test_the_probe_binds_the_upstream_resolv_conf_and_never_allows_the_stub (review of 2026-10-13,
    finding B4). With the real address list (no stand-in) every probe unit allows the endpoints alone (no upstream
    nameserver, never 127.0.0.53) and binds a resolv.conf without a nameserver and a hosts file of the endpoints and
    of the hosts it must not reach, so their refusal is the address filter. Planted: a host that does not resolve is
    left out of the hosts file."""
    import dataclasses
    import getpass
    import socket

    upstream_world(tmp_path, monkeypatch)
    fake = {"api.anthropic.com": "192.0.2.10", "platform.claude.com": "192.0.2.11",
            "iam.eu-de.otc.t-systems.com": "198.51.100.40", "github.com": "198.51.100.41"}

    def lookup(name, *a, **k):
        if name not in fake:
            raise socket.gaierror("no such name")
        return [(2, 1, 6, "", (fake[name], 443))]

    monkeypatch.setattr(buildrun.socket, "getaddrinfo", lookup)
    monkeypatch.setattr(buildrun, "host_address", lambda: "192.0.2.20")

    def watched():
        seen = []
        host = dataclasses.replace(probe_host(world, tmp_path), resolve=None)
        start = host.units.start

        def keep(spec):
            seen.append([(d, Path(s).read_text()) for s, d in spec.binds])
            return start(spec)

        host.units.start = keep
        return host, seen

    host, seen = watched()
    res = buildrun.run_probe(host, getpass.getuser())
    assert all(ok for _, ok, _ in res)
    specs = host.units.started
    assert all(s.allow == ["192.0.2.10", "192.0.2.11"] for s in specs)
    hosts = ("127.0.0.1\tlocalhost\n::1\tlocalhost\n192.0.2.10\tapi.anthropic.com\n198.51.100.41\tgithub.com\n"
             "198.51.100.40\tiam.eu-de.otc.t-systems.com\n192.0.2.11\tplatform.claude.com\n")
    assert seen == [[("/etc/hosts", hosts), ("/etc/resolv.conf", buildrun.UNIT_NO_RESOLVER)]] * len(specs)
    assert not re.search(r"(?m)^\s*nameserver", buildrun.UNIT_NO_RESOLVER)
    # GitHub does not resolve for the runner: the hosts file has no line of it
    del fake["github.com"]
    host, seen = watched()
    buildrun.run_probe(host, getpass.getuser())
    assert seen and all("github.com" not in binds[0][1] and "iam.eu-de" in binds[0][1] for binds in seen)


def test_a_refusal_of_any_kind_counts_only_after_the_model_endpoint_connected(world, tmp_path, monkeypatch):
    """Replaces test_a_refusal_counts_only_after_the_model_endpoint_connected_in_the_same_run (finding of 2026-10-12:
    the three checks of the host itself are refusals too). A unit that resolves no name refuses every host, so the
    TCP API host, GitHub and the host's own ports count only after the model check passed in the same run; a failed
    line names the probe's exit (2 no name resolution, 1 no connection)."""
    import getpass

    monkeypatch.setattr(buildrun, "host_address", lambda: "192.0.2.20")
    user = getpass.getuser()
    refusals = ("tcp", "github", "loopback", "stub", "host")
    dns = buildrun.run_probe(probe_host(world, tmp_path, reach=(), unresolved=(
        "api.anthropic.com", "platform.claude.com", "iam.eu-de.otc.t-systems.com", "github.com")), user)
    assert [(k, ok) for k, ok, _ in dns] == [("unit", True), ("model", False), ("token", False)] + [
        (k, False) for k in refusals]
    lines = dict(buildrun.probe_lines("login"))
    got = {k: ln for k, _, ln in dns}
    assert got["model"] == lines["model"] + " (no name resolution)"
    assert got["token"] == lines["token"] + " (no name resolution)"
    for k in refusals:
        assert got[k] == lines[k] + " (not proven: the unit could not reach the model endpoint)"
    refused = buildrun.run_probe(probe_host(world, tmp_path, reach=()), user)
    assert {k: ln for k, _, ln in refused}["model"] == lines["model"] + " (no connection)"
    assert [k for k, ok, _ in refused if ok] == ["unit"]
    # the model connects, the token host does not: the refusals still count
    half = buildrun.run_probe(probe_host(world, tmp_path, reach=("api.anthropic.com",)), user)
    assert [k for k, ok, _ in half if not ok] == ["token"]
    # the model connects and GitHub does not resolve: no refusal by the probe's own exit 1, not a pass
    odd = buildrun.run_probe(probe_host(world, tmp_path, unresolved=("github.com",)), user)
    assert [(k, ln) for k, ok, ln in odd if not ok] == [("github", lines["github"] + " (no name resolution)")]


def test_the_probe_script_takes_a_port_before_the_host(tmp_path):
    """Finding of 2026-10-12: `PROBE_PY 22 127.0.0.1` tries port 22 (the host's own checks); one argument stays
    port 443. A closed port of loopback is exit 1, an open one exit 0."""
    import socket

    run = [sys.executable, "-I", "-c", buildrun.PROBE_PY]
    with socket.socket() as srv:
        srv.bind(("127.0.0.1", 0))
        srv.listen(1)
        port = srv.getsockname()[1]
        assert subprocess.run(run + [str(port), "127.0.0.1"], cwd=tmp_path, timeout=60).returncode == 0
    assert subprocess.run(run + [str(port), "127.0.0.1"], cwd=tmp_path, timeout=60).returncode == 1


def test_seal_check_reads_only_a_full_probe_record_with_the_host_checks(world):
    """Replaces test_seal_check_reads_only_a_full_probe_record_of_its_build_model (finding of 2026-10-12: a record
    passes only with the three checks of the host itself). No record, a login record without its token check, a
    record without a host check, a failed line, a planted free text: never a pass."""
    board = world.host.board
    assert buildrun.probe_check(board) == (False, "the probe unit with a task's own properties passed its checks",
                                           "no record yet: sudo seal/verify.sh runs it")
    rows = "probe\t2026-10-12T21:00:00\tlogin\t8\t0\n" + "".join(
        "check\t%s\tpass\n" % k for k in ("unit", "model", "tcp", "github", "loopback", "stub", "host"))
    (board / "probe.tsv").write_text(rows)
    assert buildrun.probe_check(board)[0] is False and buildrun.probe_check(board)[2] == "failed: token"
    (board / "probe.tsv").write_text(rows + "check\ttoken\tpass\n")
    assert buildrun.probe_check(board)[0] is True
    for k in ("loopback", "stub", "host"):
        (board / "probe.tsv").write_text(rows.replace("check\t%s\tpass\n" % k, "") + "check\ttoken\tpass\n")
        assert buildrun.probe_check(board)[2] == "failed: %s" % k
    (board / "probe.tsv").write_text(rows.replace("login", "zyxwo") + "check\ttoken\tpass\n")
    assert buildrun.probe_check(board)[0] is False
    (board / "probe.tsv").write_text(rows.replace("stub\tpass", "stub\tzyxwo") + "check\ttoken\tpass\n")
    assert buildrun.probe_check(board)[2] == "failed: stub"


def test_a_failed_task_unit_writes_one_board_line_only_while_no_runner_waits(world, monkeypatch, capsys):
    """SB5: the oneshot of OnFailure= (`awb build failed UNIT`) adds one `failed` row to state.tsv when no runner
    holds its lock; the board line names the unit, the time and the result; the runner's next write drops it.
    Planted: a held runner lock writes nothing; a unit name from elsewhere and the probe unit are refused; a result
    outside systemd's words reads as unknown."""
    import types

    from awb.tcp import board as boardmod

    h = world.host
    h.board = world.base / "shared" / "build"
    h.board.mkdir(parents=True)
    shared = types.SimpleNamespace(shared=world.base / "shared")
    r = buildrun.Runner(h)
    r.write_board()
    before = (h.board / "state.tsv").read_text()
    fd = buildrun.acquire_lock(h.runs)
    assert buildrun.runner_alive(h.runs)
    assert buildrun.record_failed(h, "awb-task-B-012", "timeout") == "watched"
    assert (h.board / "state.tsv").read_text() == before
    os.close(fd)
    assert not buildrun.runner_alive(h.runs)
    assert buildrun.record_failed(h, "awb-task-B-012", "timeout") == "written"
    text = (h.board / "state.tsv").read_text()
    assert text.startswith(before) and len(text.splitlines()) == len(before.splitlines()) + 1
    line = boardmod.build_line(shared)
    assert re.search(r"; no runner watched: B-012 failed \d\d:\d\d \(timeout\)$", line), line
    for i in range(7):
        buildrun.record_failed(h, "awb-task-B-%03d-suite" % (100 + i), "exit-code")
    rows = [x for x in (h.board / "state.tsv").read_text().splitlines() if x.startswith("failed\t")]
    assert len(rows) == buildrun.FAILED_ROWS and rows[-1].startswith("failed\tawb-task-B-106-suite\texit-code\t")
    assert "B-106-suite failed" in boardmod.build_line(shared) and "B-012 failed" not in boardmod.build_line(shared)
    r.write_board()
    assert "failed" not in (h.board / "state.tsv").read_text()
    assert "no runner watched" not in boardmod.build_line(shared)
    # the command: the vocabulary of the unit name and of the result
    monkeypatch.setattr(buildrun.Host, "from_config", classmethod(lambda cls: h))
    monkeypatch.setenv("MONITOR_SERVICE_RESULT", "zyxwo")
    assert buildrun.failed_main("awb-task-B-012-prep") == 0
    assert "failed\tawb-task-B-012-prep\tunknown\t" in (h.board / "state.tsv").read_text()
    for planted in ("awb-task-probe", "awb-task-B-012; reboot", "sshd"):
        assert buildrun.failed_main(planted) == 2
    assert "not a task unit" in capsys.readouterr().err
    # a link at the place of the board's folder: nothing written through it
    os.rename(h.board, world.base / "elsewhere")
    os.symlink(world.base / "elsewhere", h.board)
    assert buildrun.record_failed(h, "awb-task-B-013", "timeout") == "not written"
    assert "B-013" not in (world.base / "elsewhere" / "state.tsv").read_text()


# --------------------------------------------------------------------------- B-001 follow-ups 2 and 3

def test_the_session_unit_gets_no_loopback_and_the_header_leaves_the_suite_to_the_runner(world):
    """B-001 follow-up 2: loopback for the session unit is refused (in the host's namespace 127.0.0.0/8 reaches sshd
    and the web units, proved live on 2026-10-12; a namespace of its own cuts the model endpoint). The session's allow
    list holds no loopback range, it has no namespace and the header tells the agent to run only the module tests
    that need no network with the venv's interpreter while the runner runs the whole suite."""
    world.host.resolve = lambda: ["127.0.0.53", "192.0.2.10"]
    run = world.run("good")
    agent = [s for s in world.units.started if s.name == "awb-task-%s" % run.id][0]
    assert not agent.netns
    assert agent.allow == ["127.0.0.53", "192.0.2.10"]
    assert not {"localhost", "127.0.0.0/8", "127.0.0.1", "::1", "::1/128"} & set(agent.allow)
    header = " ".join(build.HEADER.split())
    assert "Run only the tests of the modules you change that need no network" in header
    assert "`/srv/awb-build/venv/bin/python -I -B -m pytest -q tests/test_<module>.py`" in header
    assert "This session has no loopback" in header
    assert "The runner runs the whole suite on your commit" in header
    assert "The whole suite must pass (" not in header
    sent = (run.folder / "prompt.txt").read_text(encoding="utf-8")
    assert "This session has no loopback" in " ".join(sent.split())


def _monday_after(day: datetime) -> datetime:
    y, w, _ = day.isocalendar()
    return datetime.fromisocalendar(y, w, 1) + timedelta(days=7, minutes=5)


def test_the_runner_opens_the_week_before_the_first_task_of_a_monday(world):
    """B-001 follow-up 3, fake clock: the seed's newest CHANGELOG entry is this week; the clock says next Monday. The
    runner commits the new week's heading with an empty Added on main of main.git as the owner, the old week loses
    (in progress), the first task of the week goes done with its line under the new week and a second task of the
    same week adds no second rollover."""
    monday = _monday_after(datetime.now())
    world.host.now = lambda: monday
    start = main_of(world)
    run = world.run("good")
    assert run.state == "done", checks_of(run)
    git_dir = str(world.root / "main.git")
    roll = world.g("--git-dir", git_dir, "rev-parse", "%s^" % (run.folder / "commit").read_text().strip()).strip()
    assert world.g("--git-dir", git_dir, "rev-parse", "%s^" % roll).strip() == start
    y, w, _ = monday.isocalendar()
    old_week = (monday - timedelta(days=7)).isocalendar()[1]
    assert world.g("--git-dir", git_dir, "log", "-1", "--format=%an <%ae>|%cn <%ce>|%s", roll).strip() == \
        "%s <%s>|%s <%s>|CHANGELOG.md opens week %d and closes week %d" % (*world.IDENTITY, *world.IDENTITY, w,
                                                                           old_week)
    assert world.g("--git-dir", git_dir, "diff", "--name-only", start, roll).split() == ["CHANGELOG.md"]
    text = world.g("--git-dir", git_dir, "show", "%s:CHANGELOG.md" % main_of(world))
    heads = [ln for ln in text.splitlines() if ln.startswith("## ")]
    assert heads[0] == buildrun._week_heading(monday)
    assert heads[1] == buildrun._week_heading(monday - timedelta(days=7))[:-len(" (in progress)")]
    new_block = text.split(heads[0], 1)[1].split(heads[1], 1)[0]
    # the stand-in agent writes its line two lines under the newest heading, so it lands inside the new week
    assert "- the value is two" in new_block and "### Added" in new_block and "seed entry" not in new_block
    assert origin_of(world) == main_of(world)
    # the second task of the week: no new rollover commit
    second = world.run("good")
    assert second.state == "done"
    tip = (second.folder / "commit").read_text().strip()
    assert world.g("--git-dir", git_dir, "rev-parse", "%s^" % tip).strip() == (run.folder / "commit").read_text().strip()


def test_without_the_rollover_the_first_task_of_a_monday_is_red_changelog(world, monkeypatch):
    """B-001 follow-up 3, planted: the rollover switched off, the same Monday and the same task are red: changelog
    (what check 8 did to the first task of week 42) and main does not move."""
    monkeypatch.setattr(buildrun, "roll_week", lambda host: None)
    world.host.now = lambda: _monday_after(datetime.now())
    start = main_of(world)
    run = world.run("good")
    assert run.state == "red: changelog"
    assert main_of(world) == start


def test_a_rollover_that_cannot_be_written_holds_the_task_before_a_session(world, monkeypatch):
    """B-001 follow-up 3: no owner identity in the home template means the week cannot be opened as him; the task is
    held: changelog week before any session unit starts, main does not move and the hold restarts once it clears."""
    world.host.now = lambda: _monday_after(datetime.now())
    gitconfig = world.root / "home-template" / ".gitconfig"
    saved = gitconfig.read_text(encoding="utf-8")
    gitconfig.write_text("[core]\n\tbare = false\n", encoding="utf-8")
    start = main_of(world)
    tid = world.task("good")
    r = world.runner()
    r.poll_once()
    run = build.latest(tid, world.runs)
    assert run.state == "held: changelog week"
    assert "held: changelog week" in buildrun.HELD_RESTART
    assert main_of(world) == start
    assert not [s for s in world.units.started if s.name == "awb-task-%s" % tid]
    gitconfig.write_text(saved, encoding="utf-8")
    world.drop()


@pytest.mark.parametrize("today,newest,want", [
    (datetime(2026, 10, 12, 0, 5), "## Week 41, 2026-10-05 to 2026-10-11 (in progress)",
     ("## Week 42, 2026-10-12 to 2026-10-18 (in progress)", "## Week 41, 2026-10-05 to 2026-10-11")),
    (datetime(2027, 1, 4, 8, 0), "## Week 53, 2026-12-28 to 2027-01-03 (in progress)",
     ("## Week 1, 2027-01-04 to 2027-01-10 (in progress)", "## Week 53, 2026-12-28 to 2027-01-03")),
    (datetime(2026, 10, 26), "## Week 41, 2026-10-05 to 2026-10-11",
     ("## Week 44, 2026-10-26 to 2026-11-01 (in progress)", "## Week 41, 2026-10-05 to 2026-10-11")),
    (datetime(2026, 10, 18, 23, 59), "## Week 42, 2026-10-12 to 2026-10-18 (in progress)", None),
    (datetime(2026, 10, 12), "## Week 43, 2026-10-19 to 2026-10-25 (in progress)", None),
    (datetime(2026, 10, 12), "## Unreleased", None),
])
def test_the_week_rollover_of_the_changelog_text(today, newest, want):
    """B-001 follow-up 3, fake clock: the Monday of a new week, the turn of a year with week 53 and a skipped week
    open the clock's week and close the newest; the same week, a later week and a heading that is not a week change
    nothing."""
    text = "# Changelog\n\nIntro.\n\n%s\n\n### Added\n\n- a line\n\n## Week 1, 2020-01-01 to 2020-01-07\n" % newest
    got = buildrun.rolled_changelog(text, today)
    if want is None:
        assert got is None
        return
    new, week, closed = got
    assert new == text.replace(newest, "%s\n\n### Added\n\n%s" % want)
    assert week == today.isocalendar()[1] and str(closed) == newest.split()[2].rstrip(",")


def test_a_folder_in_wt_with_a_name_the_runner_never_makes_never_reaches_a_unit_property(tmp_path):
    """Review of 2026-10-13: the names of wt/ went unquoted into InaccessiblePaths=, which systemd splits on blanks and
    quotes: a folder `A"` and a folder `Z"` made of every clone between them one path that does not exist (the
    leading - lets systemd skip it), so the clones of other tasks were no longer hidden."""
    host = buildrun.Host(tmp_path, tmp_path / "runs", tmp_path / "queue", units=None)
    (tmp_path / "runs").mkdir()
    for name in ("A\"", "B-011-1", "B-011-1.out", "B-012-1", "x y", "Z\"", "B-1-1", "B-011-1\\"):
        (host.wt / name).mkdir(parents=True)
    ctx = types.SimpleNamespace(tid="B-012", n=1)
    hidden = buildrun._hidden(host, ctx)
    wt = [p for p in hidden if p.startswith(str(host.wt))]
    assert sorted(wt) == [str(host.wt / "B-011-1"), str(host.wt / "B-011-1.out")]
    assert not any(c in p for p in hidden for c in " \"'\\")


def test_the_export_runs_no_filter_and_keeps_the_blobs_the_gate_read(tmp_path, monkeypatch):
    """Review of 2026-10-13: a task's .gitattributes turned a filter of the owner's own git configuration on for
    the export (a command of the owner's run as the owner) and ident or eol made the export differ from the blobs
    the gate had read."""
    import tarfile

    home = tmp_path / "owner"
    home.mkdir()
    mark = tmp_path / "MARK"
    (home / ".gitconfig").write_text('[filter "evil"]\n\tsmudge = "touch %s; cat"\n' % mark, encoding="utf-8")
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("GIT_CONFIG_GLOBAL", raising=False)
    root = tmp_path / "b"
    root.mkdir()
    subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(root / "main.git")], check=True)
    seed = tmp_path / "seed"
    g = ["git", "-C", str(seed), "-c", "user.name=t", "-c", "user.email=t@example.invalid"]
    subprocess.run(["git", "init", "-q", "-b", "main", str(seed)], check=True)
    (seed / "awb").mkdir()
    (seed / "awb" / ".gitattributes").write_text("* filter=evil ident eol=crlf\n", encoding="utf-8")
    (seed / "awb" / "a.txt").write_bytes(b"$Id$\none\n")
    subprocess.run(g + ["add", "-A"], check=True)
    subprocess.run(g + ["commit", "-q", "-m", "seed"], check=True)
    subprocess.run(g + ["push", "-q", str(root / "main.git"), "main:main"], check=True)
    host = buildrun.Host(root, tmp_path / "runs", tmp_path / "queue", units=None, push=False)
    buildrun.ensure_main(host)
    folder = tmp_path / "run"
    folder.mkdir()
    tar = buildrun.export(host, types.SimpleNamespace(folder=folder), "main", "x.tar")
    with tarfile.open(tar) as t:
        assert t.extractfile("awb/a.txt").read() == b"$Id$\none\n"
    assert not mark.exists()


def test_the_handover_and_rebase_units_write_their_clone_alone_and_have_no_network(tmp_path):
    """Review of 2026-10-13, B1: every helper unit had wt/ and home/ writable and a network, so a command a session
    planted in its clone's git configuration ran in the handover or the rebase with every other clone and home in
    reach. Only the prepare and the clean unit keep wt/ and home/."""
    started = []

    class Units:
        def start(self, spec):
            started.append(spec)
            return types.SimpleNamespace(poll=lambda: buildrun.Ended("success", 0))

    host = buildrun.Host(tmp_path, tmp_path / "runs", tmp_path / "queue", units=Units())
    (tmp_path / "runs").mkdir()
    other, mine = ("B-011", 1), ("B-012", 1)
    for c in (other, mine):
        host.clone(*c).mkdir(parents=True)
        host.out(*c).mkdir()
    folder = tmp_path / "run"
    folder.mkdir()
    ctx = types.SimpleNamespace(tid="B-012", n=1, folder=folder)
    for action, extra in (("handover", []), ("rebase", ["0" * 40])):
        buildrun.unit_run(host, ctx, "hand", [action, str(tmp_path), "B-012", "1", *extra])
        spec = started[-1]
        assert spec.rw == [str(host.clone(*mine)), str(host.out(*mine))], action
        assert spec.netns, action
        assert spec.hidden == buildrun._hidden(host, ctx), action
        assert set(buildrun._login_paths(host)) <= set(spec.hidden), action
        assert {str(host.clone(*other)), str(host.out(*other)), str(host.runs)} <= set(spec.hidden), action
        assert not {str(host.clone(*mine)), str(host.out(*mine))} & set(spec.hidden), action
    for action, kind in (("prepare", "prep"), ("rekey", "prep"), ("clean", "hand")):
        buildrun.unit_run(host, ctx, kind, [action, str(tmp_path), "B-012", "1", "x"])
        spec = started[-1]
        assert spec.rw == [str(host.wt), str(host.homes)] and not spec.netns, action
        assert spec.hidden == [str(host.runs)], action


def _plain_git(*args, cwd=None):
    env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": str(cwd), "GIT_CONFIG_NOSYSTEM": "1",
           "GIT_CONFIG_GLOBAL": os.devnull, "LANG": "C.UTF-8"}
    r = subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@example.invalid", *args], cwd=cwd, env=env,
                       capture_output=True, check=True)
    return r.stdout.decode().strip()


def _helper_root(base: Path) -> tuple[Path, Path, Path, str]:
    """A build root with the clone of task B-012 run 1 as the prepare unit leaves it and a commit of the task on its
    branch, and base/main.bundle: a main that moved one commit past the clone's start."""
    root = base / "root"
    (root / "home-template").mkdir(parents=True)
    (root / "home-template" / ".gitconfig").write_text("[user]\n\tname = t\n\temail = t@example.invalid\n")
    seed = base / "seed"
    seed.mkdir()
    _plain_git("init", "-q", "-b", "main", cwd=seed)
    (seed / "a.txt").write_text("one\n")
    _plain_git("add", "a.txt", cwd=seed)
    _plain_git("commit", "-q", "-m", "seed", cwd=seed)
    start = _plain_git("rev-parse", "HEAD", cwd=seed)
    _, clone, out, _ = buildrun._helper_places(str(root), "B-012", "1")
    _plain_git("clone", "-q", str(seed), str(clone), cwd=base)
    _plain_git("remote", "remove", "origin", cwd=clone)
    _plain_git("checkout", "-q", "-b", "task/B-012", cwd=clone)
    _plain_git("config", "core.hooksPath", str(base / "no-hooks"), cwd=clone)
    _plain_git("config", "core.fsmonitor", "false", cwd=clone)
    (clone / "b.txt").write_text("two\n")
    _plain_git("add", "b.txt", cwd=clone)
    _plain_git("commit", "-q", "-m", "task", cwd=clone)
    out.mkdir()
    (seed / "c.txt").write_text("three\n")
    _plain_git("add", "c.txt", cwd=seed)
    _plain_git("commit", "-q", "-m", "moved", cwd=seed)
    _plain_git("bundle", "create", "-q", str(base / "main.bundle"), "main", cwd=seed)
    return root, clone, out, start


def test_a_helper_unit_refuses_a_clone_whose_git_would_run_a_command_of_the_session(tmp_path):
    """Review of 2026-10-13, B1: the handover and the rebase ran git with the clone's own .git/config, overriding
    only fsmonitor and the hooks: a filter with an in-tree .gitattributes, gpg.program with commit.gpgSign, an
    include, info/attributes, commondir or config.worktree of the session's choice ran or steered git in the unit.
    Such a clone is refused (exit 4) before git runs in it; a clone as the prepare unit leaves it is handed over and
    rebased."""
    mark = tmp_path / "mark"
    script = tmp_path / "plant.sh"
    script.write_text("#!/bin/sh\ntouch %s\ncat\n" % mark)
    script.chmod(0o755)

    def filter_in_tree(clone):
        (clone / ".gitattributes").write_text("* filter=x\n")
        _plain_git("config", "filter.x.clean", str(script), cwd=clone)
        (clone / "a.txt").write_text("changed\n")

    def filter_by_include(clone):
        (clone / ".gitattributes").write_text("* filter=x\n")
        (clone / ".git" / "more").write_text("[filter \"x\"]\n\tclean = %s\n" % script)
        _plain_git("config", "include.path", "more", cwd=clone)
        (clone / "a.txt").write_text("changed\n")

    def info_attributes(clone):
        (clone / ".git" / "info").mkdir(exist_ok=True)
        (clone / ".git" / "info" / "attributes").write_text("* -diff\n")

    def commondir(clone):
        (clone / ".git" / "commondir").write_text(".\n")

    def config_worktree(clone):
        (clone / ".git" / "config.worktree").write_text("[core]\n\tbare = false\n")

    def gitfile(clone):
        moved = clone.parent / "elsewhere.git"
        (clone / ".git").rename(moved)
        (clone / ".git").write_text("gitdir: %s\n" % moved)

    def gpg(clone):
        _plain_git("config", "commit.gpgSign", "true", cwd=clone)
        _plain_git("config", "gpg.program", str(script), cwd=clone)

    for i, plant in enumerate([filter_in_tree, filter_by_include, info_attributes, commondir, config_worktree, gitfile,
                               gpg]):
        for action in ("handover", "rebase"):
            base = tmp_path / ("%d-%s" % (i, action))
            root, clone, out, start = _helper_root(base)
            plant(clone)
            if action == "handover":
                rc = buildrun.unit_handover(str(root), "B-012", "1")
            else:
                with open(base / "main.bundle", "rb") as fin:
                    rc = buildrun.unit_rebase(str(root), "B-012", "1", start, fin)
            assert rc == 4, (plant.__name__, action, rc)
            assert not mark.exists(), (plant.__name__, action)
            assert not (out / "task.bundle").exists(), (plant.__name__, action)
    # a clone as the prepare unit leaves it: handed over, and rebased onto the moved main
    base = tmp_path / "clean"
    root, clone, out, start = _helper_root(base)
    assert buildrun.unit_handover(str(root), "B-012", "1") == 0
    assert (out / "task.bundle").is_file() and (out / "status.txt").read_bytes() == b""
    with open(base / "main.bundle", "rb") as fin:
        assert buildrun.unit_rebase(str(root), "B-012", "1", start, fin) == 0
    assert _plain_git("log", "--format=%s", "task/B-012", cwd=clone).splitlines() == ["task", "moved", "seed"]


# --------------------------------------------------------------------------- awb build check: a branch from outside

def outside_branch(w, name: str = "pr-1", files: dict | None = None, message: str = "a change from outside") -> str:
    """A branch of the working copy as a cloud session pushes it (its own author), fetched by the owner."""
    w.g("checkout", "-q", "-b", name, "main", cwd=w.seed)
    for rel, text in (files or {"awb/x.py": "VALUE = 2\n"}).items():
        (w.seed / rel).write_text(text, encoding="utf-8")
    w.g("add", "-A", cwd=w.seed)
    w.g("-c", "user.name=a cloud session", "-c", "user.email=cloud@localhost", "commit", "-q", "-m", message,
        cwd=w.seed)
    tip = w.g("rev-parse", "HEAD", cwd=w.seed).strip()
    w.g("checkout", "-q", "main", cwd=w.seed)
    return tip


def check(w, ref: str, capsys) -> tuple[int, str, build.Run | None]:
    w.host.working = w.seed
    rc = buildrun.check_main(ref, w.host)
    ids = build.check_ids(w.runs)
    return rc, capsys.readouterr().out, build.latest(ids[-1], w.runs) if ids else None


def suite_specs(w) -> list:
    return [s for s in w.units.started if s.name.endswith("-suite")]


def test_a_branch_from_outside_is_checked_in_the_suite_unit_and_nothing_moves(world, capsys, tmp_path):
    from awb.tcp import board

    start, pushed = main_of(world), origin_of(world)
    tip = outside_branch(world)
    rc, out, run = check(world, "pr-1", capsys)
    assert rc == 0 and run.id == "C-001" and run.state == "passed"
    assert main_of(world) == start and origin_of(world) == pushed
    assert world.g("--git-dir", str(world.root / "main.git"), "rev-parse",
                   "refs/quarantine/C-001/1/head").strip() == tip
    log = checks_of(run)
    assert [ln.split()[0:2] for ln in log.splitlines() if ln[:1].isdigit()] == [["4", "ok"], ["6", "ok"],
                                                                               ["9", "ok"], ["10", "ok"]]
    assert out.splitlines()[0] == "build check C-001 passed; nothing merged; runs/C-001/1/"
    assert "9 ok    the whole suite is green in a fresh export" in out
    # the suite and the baseline ran in the task's suite unit: its own loopback alone, the records hidden
    specs = suite_specs(world)
    assert [s.argv[7] for s in specs] == ["suite", "base"]
    for s in specs:
        assert s.name == "awb-task-C-001-suite" and s.netns and s.allow == list(buildrun.SUITE_ALLOW)
        assert s.rw == [str(world.host.out("C-001", 1))] and str(world.runs) in s.hidden
    assert any(s.argv[3:4] == ["outdir"] for s in world.units.started)
    assert not world.host.out("C-001", 1).exists()
    assert not any(s.name == "awb-task-C-001" or s.name.endswith("-prep") for s in world.units.started)
    result = (run.folder / "result.md").read_text(encoding="utf-8")
    assert "state: passed" in result and "next: read the diff, then merge it yourself" in result
    assert "awb/x.py +1 -1" in result
    # awb build list, awb build status and the board show it
    assert any(ln.startswith("C-001  passed  since ") and ln.endswith("result in runs/C-001/1/")
               for ln in build.list_lines(world.queue, world.runs))
    assert build.board_line(world.queue, world.runs).endswith("check C-001 passed")
    tsv = (world.board / "state.tsv").read_text()
    assert "\ncheck\tC-001\tpassed\t" in tsv and ("\t%s\n" % tip[:7]) in tsv
    (tmp_path / "build").mkdir()
    (tmp_path / "build" / "state.tsv").write_text(tsv, encoding="utf-8")
    assert board.build_line(types.SimpleNamespace(shared=tmp_path)).startswith("build: check C-001 passed; live ")


@pytest.mark.parametrize("text, state", [
    ("def test_one():\n    assert 1 + 1 == 2\n", "red: tests lost"),
    ("def test_one():\n    assert 1 + 1 == 3\n", "red: suite"),
])
def test_a_branch_that_breaks_or_drops_a_test_is_red_and_main_stays(world, capsys, text, state):
    start = main_of(world)
    outside_branch(world, files={"tests/test_x.py": text})
    rc, out, run = check(world, "pr-1", capsys)
    assert rc == 1 and run.state == state and main_of(world) == start
    assert out.splitlines()[0] == "build check C-001 %s; nothing merged; runs/C-001/1/" % state
    assert suite_specs(world) and not world.host.out("C-001", 1).exists()


def test_a_registered_form_in_a_commit_of_the_branch_is_red_gate_before_any_code_runs(world, capsys):
    from awb import gate

    outside_branch(world, message="mention %s" % gate._SELFTEST_FORM)
    rc, out, run = check(world, "pr-1", capsys)
    assert rc == 1 and run.state == "red: gate name"
    assert not suite_specs(world) and gate._SELFTEST_FORM not in out
    assert gate._SELFTEST_FORM not in (run.folder / "result.md").read_text(encoding="utf-8")


def test_a_ref_that_is_an_option_a_range_or_unknown_is_refused_and_nothing_is_written(world, capsys):
    outside_branch(world)
    for ref in ("--upload-pack=touch", "main..pr-1", "no-such-branch", "-q"):
        rc, out, run = check(world, ref, capsys)
        assert rc == 2 and run is None, ref
    assert not world.units.started


def test_the_check_goes_through_the_runner_lock_and_the_runner_takes_it_between_tasks(world, capsys):
    """While a runner holds runs/.runner.lock the check waits queued; the runner's next poll takes it, never beside a
    task. A check found running at the reconcile lost its runner and is interrupted."""
    outside_branch(world)
    held = buildrun.acquire_lock(world.runs)
    polls = []

    def runner_polls(seconds):
        if seconds != buildrun.CHECK_WAIT:      # the tick of the runner's own wait for a unit
            time.sleep(seconds)
            return
        polls.append(build.latest("C-001", world.runs).state)
        world.runner().poll_once()

    world.host.sleep = runner_polls
    try:
        rc, out, run = check(world, "pr-1", capsys)
    finally:
        os.close(held)
    assert polls == ["queued"] and rc == 0 and run.state == "passed"
    assert "queued: the runner takes it after its current task" in out
    second = build.new_check(world.runs)
    build.write_state(second.folder, "running")
    world.runner().reconcile()
    assert build.latest(second.id, world.runs).state == "interrupted"
    assert build.latest("C-001", world.runs).state == "passed"


def test_awb_build_check_reaches_the_runner_and_the_unit_helper_takes_a_check_id(monkeypatch, tmp_path):
    monkeypatch.setattr(buildrun, "check_main", lambda ref: 7 if ref == "pr-1" else 0)
    assert build.main(["check", "pr-1"]) == 7
    root = tmp_path / "root"
    (root / "wt").mkdir(parents=True)
    assert buildrun.unit_main(["outdir", str(root), "C-001", "1"]) == 0
    assert (root / "wt" / "C-001-1.out").is_dir()
    assert buildrun.unit_main(["outdir", str(root), "C-001", "1"]) == 3
    assert buildrun.unit_main(["outdir", str(root), "B-001", "1"]) == 2
    assert buildrun.UNIT_RE.fullmatch("awb-task-C-001-suite")


# --------------------------------------------------------------------------- A2: onto the main of now before the checks

def _yesterday_baseline(w, start: str) -> None:
    """The cached baseline of a start commit as an earlier day left it: a parametrized test whose id held that day's
    date passed then; the suite of today names the same test with today's date."""
    cache = w.runs / ".baseline"
    cache.mkdir(parents=True, exist_ok=True)
    nodes = {"tests/test_x.py::test_%s" % n: "passed" for n in ("one", "two", "three")}
    nodes["tests/test_x.py::test_day[2026-10-13]"] = "passed"
    (cache / ("%s.json" % start)).write_text(json.dumps(nodes), encoding="utf-8")


def test_a_retry_whose_main_moved_during_the_session_is_rebased_before_the_first_check(world, monkeypatch):
    """A stale baseline never causes a false red: the start commit's cached baseline names a node today's suite does
    not have; the checks judge the branch on the main of now, never on that start commit."""
    first = world.run("outside")
    assert first.state == "red: outside paths"
    world.task("good", tid=first.id)
    assert build.cmd_retry(first.id, world.queue, world.runs) == 0
    real_jm, real_judge = buildrun.Runner.judge_and_merge, buildrun.Runner.judge
    moved, starts = [], []

    def judge_and_merge(self, ctx):
        _yesterday_baseline(world, ctx.start)
        moved.append(move_main(world, "README.md", "# tiny\n\nhis line\n"))
        return real_jm(self, ctx)

    def judge(self, ctx):
        starts.append(ctx.start)
        return real_judge(self, ctx)

    monkeypatch.setattr(buildrun.Runner, "judge_and_merge", judge_and_merge)
    monkeypatch.setattr(buildrun.Runner, "judge", judge)
    assert world.runner().poll_once()
    run = build.latest(first.id, world.runs)
    assert (run.n, run.state) == (2, "done")
    assert starts == moved                                     # one round, on the main of now
    assert "- main moved: the branch was rebased onto %s before the checks" % moved[0][:7] in checks_of(run)
    log = world.g("--git-dir", str(world.root / "main.git"), "log", "--format=%s", "main")
    assert "his own change" in log and "the value is two" in log


def test_a_widened_task_whose_main_moved_is_rebased_before_the_checks_run_again(world):
    run = world.run("outside")
    assert run.state == "red: outside paths"
    moved = move_main(world, "README.md", "# tiny\n\nhis line\n")
    _yesterday_baseline(world, (run.folder / "start").read_text().strip())
    assert build.cmd_widen(run.id, ["pyproject.toml"], world.runs) == 0
    assert world.runner().poll_once()
    after = build.latest(run.id, world.runs)
    assert (after.n, after.state) == (run.n, "done")
    assert (run.folder / "start").read_text().strip() == moved
    assert "rebased onto %s before the checks" % moved[:7] in checks_of(after)


def test_a_rebase_that_conflicts_before_the_checks_is_red_with_its_reason_and_main_stays(world, monkeypatch):
    real = buildrun.Runner.judge_and_merge
    moved = []

    def judge_and_merge(self, ctx):
        moved.append(move_main(world, "awb/x.py", "VALUE = 9\n"))
        return real(self, ctx)

    monkeypatch.setattr(buildrun.Runner, "judge_and_merge", judge_and_merge)
    run = world.run("good")
    assert run.state == "red: rebase conflict"
    assert main_of(world) == moved[0]
    assert build.line(run) == "build %s red: rebase conflict; branch task/%s kept" % (run.id, run.id)
    log = checks_of(run)
    assert "4 ok" in log and "does not rebase onto it without a conflict" in log
    assert not [s for s in world.units.started if s.name.endswith("-suite")]
    assert world.host.clone(run.id, run.n).is_dir()


def test_a_dirty_tree_is_named_before_a_rebase_could_call_it_a_conflict(world, monkeypatch):
    real = buildrun.Runner.judge_and_merge

    def judge_and_merge(self, ctx):
        move_main(world, "README.md", "# tiny\n\nhis line\n")
        return real(self, ctx)

    monkeypatch.setattr(buildrun.Runner, "judge_and_merge", judge_and_merge)
    assert world.run("dirty").state == "red: dirty tree"


def test_a_check_of_a_branch_whose_main_moved_runs_on_the_main_of_now(world, capsys):
    tip = outside_branch(world)
    base = main_of(world)
    _yesterday_baseline(world, base)
    moved = move_main(world, "README.md", "# tiny\n\nhis line\n")
    rc, out, run = check(world, "pr-1", capsys)
    assert rc == 0 and run.state == "passed"
    assert main_of(world) == moved
    onto = (run.folder / "onto").read_text().strip()
    assert (run.folder / "start").read_text().strip() == moved and (run.folder / "head").read_text().strip() == tip
    g = ["--git-dir", str(world.root / "main.git")]
    assert world.g(*g, "rev-parse", onto + "^").strip() == moved
    assert world.g(*g, "show", onto + ":awb/x.py") == "VALUE = 2\n"
    assert world.g(*g, "show", onto + ":README.md") == "# tiny\n\nhis line\n"
    assert world.g(*g, "rev-parse", "refs/quarantine/C-001/1/onto").strip() == onto
    assert "rebased onto %s before the checks" % moved[:7] in checks_of(run)
    assert [s.argv[7] for s in suite_specs(world)] == ["suite", "base"]


def test_a_check_of_a_branch_that_conflicts_with_the_main_of_now_is_red_before_any_code_runs(world, capsys):
    outside_branch(world)
    move_main(world, "awb/x.py", "VALUE = 9\n")
    rc, out, run = check(world, "pr-1", capsys)
    assert rc == 1 and run.state == "red: rebase conflict"
    assert out.splitlines()[0] == "build check C-001 red: rebase conflict; nothing merged; runs/C-001/1/"
    assert not suite_specs(world)


# --------------------------------------------------------------------------- A3: the always allowed files reserve nothing

def test_a_task_waiting_on_a_question_keeps_no_task_waiting_for_an_always_allowed_file(world):
    queue = ""
    for tid, paths in (("B-301", "awb/ README.md CHANGELOG.md build/tests.json"),
                       ("B-302", "tests/ README.md CHANGELOG.md build/tests.json"),
                       ("B-303", "awb/x.py")):
        (world.queue / ("%s.md" % tid)).write_text("SCENARIO: ask\n")
        queue += ("## %s: t\ndepends: none\npaths: %s\ntests_replaced: none\ncheck: none\nprompt: build/queue/%s.md\n"
                  "end: %s\n" % (tid, paths, tid, tid))
    (world.queue / "QUEUE.md").write_text(queue)
    r = world.runner()
    r.poll_once()
    assert build.latest("B-301", world.runs).state == "asked"
    # B-301 asked: README.md, CHANGELOG.md and build/tests.json stay free, awb/ stays reserved
    assert r.pick(build.read_queue(world.queue)[0])[0].id == "B-302"
    r.poll_once()
    assert build.latest("B-302", world.runs).state == "asked"
    assert r.pick(build.read_queue(world.queue)[0]) is None


def test_an_always_allowed_file_meets_no_reserved_folder(world):
    queue = ""
    for tid, paths in (("B-311", "build/"), ("B-312", "awb/ build/tests.json")):
        (world.queue / ("%s.md" % tid)).write_text("SCENARIO: ask\n")
        queue += ("## %s: t\ndepends: none\npaths: %s\ntests_replaced: none\ncheck: none\nprompt: build/queue/%s.md\n"
                  "end: %s\n" % (tid, paths, tid, tid))
    (world.queue / "QUEUE.md").write_text(queue)
    r = world.runner()
    r.poll_once()
    assert build.latest("B-311", world.runs).state == "asked"
    assert r.pick(build.read_queue(world.queue)[0])[0].id == "B-312"


# --------------------------------------------------------------------------- A5: widen --tests-replaced

def test_a_test_id_widened_into_tests_replaced_reaches_the_rejudge_without_a_session(world):
    run = world.run("deleted")
    assert run.state == "red: tests lost"
    # an edit of the queue never reaches the checks: they read the run's own task.md
    world.task("deleted", tid=run.id, replaced="tests/test_x.py::test_three")
    assert build.cmd_widen(run.id, ["awb/"], world.runs) == 0
    assert world.runner().poll_once()
    assert build.latest(run.id, world.runs).state == "red: tests lost"
    sessions = len([s for s in world.units.started if s.name == "awb-task-%s" % run.id])
    assert build.cmd_widen(run.id, [], world.runs, tests_replaced=["tests/test_x.py::test_three"]) == 0
    assert world.runner().poll_once()
    after = build.latest(run.id, world.runs)
    assert (after.n, after.state) == (run.n, "done")
    assert len([s for s in world.units.started if s.name == "awb-task-%s" % run.id]) == sessions
    assert "tests_replaced: tests/test_x.py::test_three" in (run.folder / "task.md").read_text()
    assert len((run.folder / "widen.log").read_text().splitlines()) == 2


def test_an_id_of_tests_json_widened_into_tests_replaced_lets_its_entry_change(world):
    run = world.run("testsjson")
    assert run.state == "red: tests.json"
    assert build.cmd_widen(run.id, [], world.runs, tests_replaced=["S1"]) == 0
    assert world.runner().poll_once()
    assert build.latest(run.id, world.runs).state == "done"
