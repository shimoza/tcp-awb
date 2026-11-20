"""Q-0014 phase 1, finding 1 of the UX review of 2026-11-18 (push, not poll): the events file of the long things
(awb/events.py), its writers (the build runner, the deploy, awb winlab, the dev asks) and its readers (`awb owner
status` and the `running` field of the owner status file).

Every place is a folder of tmp_path; the clocks are fixed datetimes. Codes, machine names and ids are invented.
"""
from __future__ import annotations

import datetime as dt
import json
import os
import stat
import subprocess
import sys
import types

import pytest

from awb import buildrun, cli, config, deploy, devask, events, ownerwait, status_shape, winlab
from awb.tcp import owner
from awb.tcp.web import owner_actions
from tests import fixtures as fx
from tests import test_close_the_loops as loops
from tests import test_winlab_extend as wx

UTC = dt.timezone.utc
T0 = dt.datetime(2026, 11, 20, 9, 0, tzinfo=UTC)
PROJECT = "tcp-ab2c"


def at(minutes: int) -> dt.datetime:
    return T0 + dt.timedelta(minutes=minutes)


@pytest.fixture
def board(tmp_path):
    b = tmp_path / "shared" / "build"
    b.mkdir(parents=True)
    return b


# --------------------------------------------------------------------------- the file


def test_start_and_end_append_one_checked_line_each_to_a_group_file(board):
    assert events.start("check", "C-301", board=board, now=at(0))
    assert events.end("check", "C-301", "red", board=board, now=at(30))
    path = board / events.FILE
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o660
    lines = path.read_text().splitlines()
    assert [json.loads(x)["ev"] for x in lines] == ["start", "end"]
    assert json.loads(lines[0]) == {"t": "2026-11-20T09:00:00Z", "ev": "start", "kind": "check", "id": "C-301",
                                    "project": None, "eta": None, "auto_end": False}
    assert events.read(board)[1]["outcome"] == "red"


def test_a_writer_refuses_free_text_and_never_raises(board, tmp_path):
    assert not events.start("task", "B-012 with words", board=board)
    assert not events.start("timer", "B-012", board=board)
    assert not events.start("task", "B-012", project="someone", board=board)
    assert not events.end("task", "B-012", "fine", board=board)
    assert not events.start("task", "B-012", eta="tomorrow", board=board)
    assert not os.path.exists(board / events.FILE)
    # a link where the file goes is never followed, a missing board with create=False is skipped
    target = tmp_path / "elsewhere"
    target.write_text("")
    (board / events.FILE).symlink_to(target)
    assert not events.start("task", "B-012", board=board)
    assert target.read_text() == ""
    assert not events.start("task", "B-012", board=tmp_path / "none", create=False)
    assert not (tmp_path / "none").exists()


def test_lines_of_several_writers_never_mix(board):
    code = ("import sys\nfrom awb import events\nn = sys.argv[1]\nok = [events.start('task', 'B-%s%03d' % (n, i), "
            "board=sys.argv[2]) for i in range(100)]\nsys.exit(0 if all(ok) else 1)\n")
    children = [subprocess.Popen([sys.executable, "-I", "-c", code, str(n), str(board)]) for n in range(1, 5)]
    assert [c.wait(timeout=60) for c in children] == [0, 0, 0, 0]
    raw = (board / events.FILE).read_text().splitlines()
    assert len(raw) == 400 and len(events.read(board)) == 400


def test_a_bad_line_is_skipped_and_only_the_tail_of_a_large_file_is_read(board, monkeypatch):
    path = board / events.FILE
    events.start("task", "B-001", board=board, now=at(0))
    with open(path, "a") as fh:
        fh.write("not json\n" + json.dumps({"t": "x", "ev": "start"}) + "\n")
        fh.write(json.dumps({"t": "2026-11-20T09:00:00Z", "ev": "end", "kind": "task", "id": "B-001",
                             "project": None, "outcome": "ok", "note": "free text"}) + "\n")
    assert [e["id"] for e in events.read(board)] == ["B-001"]
    monkeypatch.setattr(events, "READ_MAX", 300)
    for i in range(10):
        events.start("task", "B-1%02d" % i, board=board, now=at(i))
    got = events.read(board)
    assert 0 < len(got) < 11 and got[-1]["id"] == "B-109"


# --------------------------------------------------------------------------- running now and ended


def test_running_now_has_the_open_items_with_eta_and_overdue_and_ended_has_the_rest(board):
    events.start("deploy", "abc1234", board=board, now=at(0), eta=at(5))
    events.start("check", "C-301", board=board, now=at(1), eta=at(60))
    events.end("deploy", "abc1234", "ok", board=board, now=at(4))
    events.start("task", "B-012", project=PROJECT, board=board, now=at(2))
    events.end("task", "B-012", "red", board=board, now=at(20))
    events.start("task", "B-013", board=board, now=at(3), eta=at(10))
    run = events.running_now(events.read(board), at(30))
    assert [(r["kind"], r["id"], r["since"], r["overdue"]) for r in run] == [
        ("check", "C-301", "2026-11-20T09:01:00Z", False), ("task", "B-013", "2026-11-20T09:03:00Z", True)]
    ended = events.ended_since(events.read(board), at(0), at(30))
    assert [(e["id"], e["outcome"], e["start"], e["project"]) for e in ended] == [
        ("abc1234", "ok", "2026-11-20T09:00:00Z", None), ("B-012", "red", "2026-11-20T09:02:00Z", PROJECT)]
    assert events.ended_since(events.read(board), at(10), at(30))[0]["id"] == "B-012"


def test_a_lab_window_ends_by_itself_at_its_eta_and_a_new_start_renews_the_eta(board):
    events.start("lab", "lab-1", project=PROJECT, eta=at(60), auto_end=True, board=board, now=at(0))
    events.start("lab", "lab-1", project=PROJECT, eta=at(90), auto_end=True, board=board, now=at(30))
    run = events.running_now(events.read(board), at(70))
    assert [(r["id"], r["since"], r["eta"]) for r in run] == [("lab-1", "2026-11-20T09:00:00Z",
                                                               "2026-11-20T10:30:00Z")]
    assert events.running_now(events.read(board), at(91)) == []
    ended = events.ended_since(events.read(board), at(0), at(91))
    assert [(e["id"], e["outcome"], e["end"]) for e in ended] == [("lab-1", "ok", "2026-11-20T10:30:00Z")]
    # opened again after the window ended by itself: a new item, not an extension of the old one
    events.start("lab", "lab-1", project=PROJECT, eta=at(200), auto_end=True, board=board, now=at(120))
    assert events.running_now(events.read(board), at(121))[0]["since"] == "2026-11-20T11:00:00Z"


def test_the_eta_of_history_is_the_median_of_the_last_of_its_kind(board):
    for i, minutes in enumerate((10, 30, 20, 500)):
        start = at(i * 1000)
        events.start("check", "C-%03d" % (i + 1), board=board, now=start)
        events.end("check", "C-%03d" % (i + 1), "ok", board=board, now=start + dt.timedelta(minutes=minutes))
    assert events.median_seconds(events.read(board), "check") == 25 * 60
    assert events.median_seconds(events.read(board), "task") is None
    events.start("check", "C-010", history=True, board=board, now=at(9000))
    assert events.running_now(events.read(board), at(9001))[0]["eta"] == events._z(at(9025))
    events.start("task", "B-001", history=True, board=board, now=at(9000))
    assert events.running_now(events.read(board), at(9001))[1]["eta"] is None


def test_the_outcome_of_a_build_state():
    assert [events.build_outcome(s) for s in ("done", "passed", "red: suite", "hung: suite", "stale", "interrupted",
                                              "stopped: by_owner", "asked", "held: push pending", "running",
                                              "green", "answered", "queued")] == [
        "ok", "ok", "red", "red", "red", "red", "stopped", "needs-you", "needs-you", None, None, None, None]


def test_the_owner_lines_show_running_now_first_and_mark_red(board):
    events.start("check", "C-301", board=board, now=at(0), eta=at(40))
    events.start("task", "B-012", project=PROJECT, board=board, now=at(1))
    events.end("task", "B-012", "red", board=board, now=at(20))
    events.start("deploy", "abc1234", board=board, now=at(21))
    events.end("deploy", "abc1234", "ok", board=board, now=at(23))
    events.start("ask", "2026-11-19-%s-1" % PROJECT, project=PROJECT, board=board, now=at(-60 * 30))
    events.end("ask", "2026-11-19-%s-1" % PROJECT, "ok", board=board, now=at(-60 * 26))
    lines = events.status_lines(board, at(50))
    hm = lambda m: at(m).astimezone().strftime("%H:%M")   # noqa: E731
    assert lines == [
        "Running now",
        "  check C-301 since %s, ETA %s (overdue)" % (hm(0), hm(40)),
        "Ended in the last 24 h",
        "  ok deploy abc1234 at %s (started %s)" % (hm(23), hm(21)),
        "  RED build task B-012 (%s) at %s (started %s)" % (PROJECT, hm(20), hm(1))]
    assert events.status_lines(board / "none", at(0)) == ["Running now", "  nothing runs",
                                                          "Ended in the last 24 h", "  nothing ended"]


# --------------------------------------------------------------------------- the owner status


def test_owner_status_prints_running_now_before_the_list(home, monkeypatch, capsys):
    monkeypatch.setattr(config, "paths", lambda: home)
    monkeypatch.setattr(config, "is_work_user", lambda: False)
    monkeypatch.setattr(ownerwait, "collect", lambda **k: ([], []))
    monkeypatch.setattr(ownerwait, "render", lambda *a: ["Nothing waits on you."])
    monkeypatch.setattr(ownerwait, "notes", lambda: [])
    events.start("check", "C-301", board=home.shared / "build")
    assert cli.main(["owner", "status"]) == 0
    out = capsys.readouterr().out.splitlines()
    assert out[0] == "Running now" and out[1].startswith("  check C-301 since ")
    assert out.index("Ended in the last 24 h") < out.index("Nothing waits on you.")
    fx.assert_no_fixture_name("\n".join(out))


def _doc(**more) -> dict:
    return {"version": 1, "written": "2026-11-20T09:00:00Z", "vault": {"state": "unlocked", "since": None},
            "keys": {"state": "unlocked", "since": None}, "deployed": {"release": None, "journal": None},
            "page": {"size": None, "time": None, "backup_time": None, "backups": 0}, "ui_runs": [], "intake": [],
            **more}


ROW = {"kind": "lab", "id": "lab-1", "project": PROJECT, "since": "2026-11-20T09:00:00Z",
       "eta": "2026-11-20T10:00:00Z", "overdue": False}


def test_the_status_file_carries_running_and_owner_actions_serves_it(home, tmp_path, monkeypatch):
    events.start("lab", "lab-1", project=PROJECT, eta=at(60), auto_end=True, board=home.shared / "build",
                 now=at(0))
    assert events.running_rows(home, at(1)) == [ROW]
    status = _doc(running=[ROW])
    ownerwait.validate_status(status)
    path = tmp_path / "status.json"
    owner.write(status, path, checker=lambda text: "passed")
    data = owner_actions.read_status(path)
    code, state = owner_actions.answer("/api/owner/state", data, at(1).timestamp())
    assert code == 200 and state["running"] == [ROW]
    assert owner_actions.answer("/api/owner/state", _doc(), at(1).timestamp())[1]["running"] == []
    # collect fills it from the events file of the shared side
    for name, value in (("daemons", {"vault": _doc()["vault"], "keys": _doc()["keys"]}),
                        ("deployed", _doc()["deployed"]), ("page", _doc()["page"]), ("ui_runs", [])):
        monkeypatch.setattr(owner, name, lambda *a, _value=value, **k: _value)
    monkeypatch.setattr(owner, "_decisions", lambda p, path=None: ([], 0))
    monkeypatch.setattr(owner, "_lists", lambda p, path=None: ([], {"readable": True, "rows": []}, 0))
    monkeypatch.setattr(events, "running_rows", lambda p: [ROW])
    assert owner.collect(home)["running"] == [ROW]


@pytest.mark.parametrize("change", [
    lambda r: r.update(kind="timer"), lambda r: r.update(id="B 1"), lambda r: r.update(project="someone"),
    lambda r: r.update(since=None), lambda r: r.update(eta="soon"), lambda r: r.update(overdue="no"),
    lambda r: r.update(note="x"), lambda r: r.update(id="B-1\n")])
def test_a_running_row_of_another_shape_is_refused(change, tmp_path):
    row = dict(ROW)
    change(row)
    with pytest.raises(status_shape.StatusError):
        ownerwait.validate_status(_doc(running=[row]))
    path = tmp_path / "status.json"
    path.write_text(json.dumps(_doc(running=[row])))
    assert owner_actions.read_status(path) is None


# --------------------------------------------------------------------------- the writers


def test_the_runner_writes_the_start_and_the_end_of_a_task_and_a_check(board):
    clock = [dt.datetime(2026, 11, 20, 10, 0)]           # the runner's clock: host local time, naive
    run = buildrun.Runner(types.SimpleNamespace(board=board, now=lambda: clock[0], quiet=True))
    run.event_start("B-012")
    run.event_start("C-301")
    clock[0] += dt.timedelta(minutes=20)
    run.event_end("C-301", "red: suite")
    run.event_end("B-012", "green")                     # still open work: no end
    got = events.read(board)
    assert [(e["ev"], e["kind"], e["id"]) for e in got] == [("start", "task", "B-012"), ("start", "check", "C-301"),
                                                            ("end", "check", "C-301")]
    assert got[2]["outcome"] == "red"
    assert got[0]["t"] == events._z(dt.datetime(2026, 11, 20, 10, 0))
    # without a board (the tests of the runner, the selftest) nothing is written
    buildrun.Runner(types.SimpleNamespace(board=None, now=lambda: clock[0], quiet=True)).event_start("B-013")
    assert len(events.read(board)) == 3


@pytest.fixture
def lab_owner(home, tmp_path, monkeypatch):
    """The owner's terminal with lab-1 registered and the clock of tests/test_winlab_extend.py."""
    conf = tmp_path / "conf" / "keys.conf"
    conf.parent.mkdir()
    conf.write_text("region = eu-de\n", encoding="utf-8")
    monkeypatch.setenv("AWB_KEYS_CONF", str(conf))
    monkeypatch.setattr(config, "paths", lambda: home)
    monkeypatch.setattr(config, "is_work_user", lambda: False)
    clock = [wx.T0]
    monkeypatch.setattr(winlab, "_now", lambda: clock[0])
    winlab.add(wx.MACHINE, 2201, wx.FINGERPRINT, True, today="2026-11-05")
    return clock


def test_winlab_open_extend_and_close_write_the_window(lab_owner, home):
    clock = lab_owner
    winlab.open_(wx.MACHINE, 60, PROJECT)
    clock[0] = wx.minutes(30)
    winlab.open_(wx.MACHINE, 60, PROJECT, extend=True)   # extends to 10:30 + 60
    run = events.running_now(events.read(home.shared / "build"), wx.minutes(31))
    assert [(r["kind"], r["id"], r["project"], r["eta"]) for r in run] == [
        ("lab", wx.MACHINE, PROJECT, events._z(wx.minutes(90)))]
    winlab.close(wx.MACHINE)
    assert events.running_now(events.read(home.shared / "build"), wx.minutes(31)) == []
    assert events.read(home.shared / "build")[-1]["outcome"] == "ok"
    winlab.close(wx.MACHINE)                             # no open window: no second end
    assert len(events.read(home.shared / "build")) == 3


def test_a_dev_ask_starts_when_written_and_ends_when_closed(home, monkeypatch, tmp_path):
    from awb import projects

    rules = tmp_path / "work-rules.md"
    rules.write_text("# Work rules\n", encoding="utf-8")
    monkeypatch.setattr(projects, "RULES_FILE", rules)
    pr = projects.spawn(home, "query", "check the docs", fx.CUSTOMER_CODE, home.register)
    monkeypatch.setattr(config, "paths", lambda: home)
    monkeypatch.setattr(config, "is_work_user", lambda: True)
    monkeypatch.chdir(pr.path)
    world = (home, pr)
    path = loops.new_ask(world)
    run = events.running_now(events.read(home.shared / "build"))
    assert [(r["kind"], r["id"], r["project"]) for r in run] == [("ask", path.stem, world[1].code)]
    assert cli.main(["dev", "close", "--release", "c" * 40, path.stem]) == 0
    assert events.running_now(events.read(home.shared / "build")) == []
    assert events.read(home.shared / "build")[-1]["outcome"] == "ok"
    assert devask.read(path)["state"] == "closed"


def test_a_deploy_writes_its_start_and_end_only_on_a_host_with_a_shared_side(tmp_path, capsys):
    if not (loops.ROOT / "seal").is_dir():
        pytest.skip("the deploy host copies seal/, which a quick copy of the tree does not hold")
    host = loops.World(tmp_path)
    conf = host.etc / "awb" / "paths.conf"
    assert deploy.deploy_board(types.SimpleNamespace(conf={})) is None
    shared = tmp_path / "shared-side"
    (shared / "build").mkdir(parents=True)
    conf.write_text(conf.read_text() + "shared = %s\n" % shared)
    host.runner.plan = host.plan(actions={"awb-ask.service": "restart"})
    assert host.main([]) == 0
    got = events.read(shared / "build")
    assert [(e["ev"], e["kind"], e["id"]) for e in got] == [("start", "deploy", host.c1[:7]),
                                                            ("end", "deploy", host.c1[:7])]
    assert got[1]["outcome"] == "ok"
    capsys.readouterr()
