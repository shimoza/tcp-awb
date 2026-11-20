"""Q-0014 phase 2, finding 1 of the UX review of 2026-11-18 (push, not poll): the kinds phase 1 left out (a lab run,
the runs of the timer jobs, the lock of the vault and the key service, the lapsed login of the build user), the push
of the owner's minute timer (one mail per red end, needs-you end, lock, lapsed login and answered blocking dev ask)
and the `ended` field of the owner status and of GET /api/owner/state.

Every place is a folder of tmp_path; the clocks are fixed datetimes; nothing is mailed, `send` is a list. Codes,
unit names and ids are invented.
"""
from __future__ import annotations

import datetime as dt
import json
import os
import stat
import types

import pytest

from awb import config, events, labclient, ownerwait, status_shape
from awb.tcp import owner
from awb.tcp.web import contract, owner_actions
from tests import fixtures as fx

UTC = dt.timezone.utc
T0 = dt.datetime(2026, 11, 20, 9, 0, tzinfo=UTC)
PROJECT = "tcp-ab2c"
HANDLE = "2026-11-20-%s-1" % PROJECT


def at(minutes: float) -> dt.datetime:
    return T0 + dt.timedelta(minutes=minutes)


@pytest.fixture
def board(tmp_path):
    b = tmp_path / "shared" / "build"
    b.mkdir(parents=True)
    return b


class Mail:
    """The send of a push: records (subject, body), answers what `ok` says."""

    def __init__(self, ok: bool = True):
        self.sent: list[tuple[str, str]] = []
        self.ok = ok

    def __call__(self, subject: str, body: str) -> bool:
        if self.ok:
            self.sent.append((subject, body))
        return self.ok


# --------------------------------------------------------------------------- the new kinds


def test_the_new_kinds_are_written_and_a_lock_or_login_id_is_a_fixed_word(board):
    assert events.start("labrun", "run-20261120-090000-ab2c", project=PROJECT, board=board, now=at(0))
    assert events.start("job", "awb-refresh-weekly", board=board, now=at(0))
    assert events.start("vault", "vault", board=board, now=at(0))
    assert events.start("keys", "keys", board=board, now=at(0))
    assert events.start("login", "build-user", board=board, now=at(0))
    assert not events.start("vault", "keys", board=board, now=at(0))
    assert not events.start("vault", "vault", project=PROJECT, board=board, now=at(0))
    assert not events.start("login", "someone", board=board, now=at(0))
    assert len(events.read(board)) == 5


def test_the_lock_of_the_vault_and_the_key_service_opens_once_and_ends_on_unlock(board):
    assert events.observe_locks({"vault": {"state": "locked"}, "keys": {"state": "unlocked"}}, board=board,
                                now=at(0)) == ["vault locked"]
    assert events.observe_locks({"vault": {"state": "down"}, "keys": {"state": "down"}}, board=board,
                                now=at(1)) == ["keys locked"]
    assert events.observe_locks({"vault": {"state": "unknown"}, "keys": {"state": "unknown"}}, board=board,
                                now=at(2)) == []
    run = events.running_now(events.read(board), at(3))
    assert [(r["kind"], r["id"], r["since"]) for r in run] == [("vault", "vault", events._z(at(0))),
                                                               ("keys", "keys", events._z(at(1)))]
    assert events.observe_locks({"vault": {"state": "unlocked"}, "keys": {"state": "plain"}}, board=board,
                                now=at(60)) == ["vault unlocked", "keys unlocked"]
    assert events.running_now(events.read(board), at(61)) == []
    assert events.observe_locks({"vault": None, "keys": {}}, board=board, now=at(62)) == []


def test_the_login_of_the_build_user_lapses_from_the_held_run_and_comes_back(board, tmp_path):
    runs = tmp_path / "runs"
    (runs / "B-012" / "1").mkdir(parents=True)
    (runs / "B-012" / "2").mkdir(parents=True)
    (runs / "B-012" / "1" / "state").write_text("red: suite\t2026-11-20T08:00:00\n")
    (runs / "B-012" / "2" / "state").write_text("held: login missing\t2026-11-20T09:00:00\n")
    assert owner.login_lapsed(runs) is True
    assert owner.login_lapsed(tmp_path / "none") is None
    assert events.observe_login(True, board=board, now=at(0)) == "lapsed"
    assert events.observe_login(True, board=board, now=at(1)) is None
    assert events.observe_login(None, board=board, now=at(2)) is None
    assert [(r["kind"], r["id"]) for r in events.running_now(events.read(board), at(3))] == [("login", "build-user")]
    (runs / "B-012" / "3").mkdir()
    (runs / "B-012" / "3" / "state").write_text("running\t2026-11-20T10:00:00\n")
    assert owner.login_lapsed(runs) is False
    assert events.observe_login(False, board=board, now=at(60)) == "back"
    assert events.running_now(events.read(board), at(61)) == []


TIMERS = """Fri 2026-11-20 10:00:00 UTC 50min Fri 2026-11-20 09:00:00 UTC 10min ago awb-import-auto.timer awb-import-auto.service
n/a n/a Sat 2026-11-14 17:00:00 UTC 6 days ago awb-refresh-weekly.timer awb-refresh-weekly.service
Fri 2026-11-20 09:11:00 UTC 1min Fri 2026-11-20 09:10:00 UTC 2s ago awb-owner-status.timer awb-owner-status.service
"""
SHOW = """Id=awb-import-auto.service
ActiveState=inactive
Result=success
ExecMainStartTimestamp=@1763629200
ExecMainExitTimestamp=@1763629205

Id=awb-refresh-weekly.service
ActiveState=failed
Result=exit-code
ExecMainStartTimestamp=@1763139600
ExecMainExitTimestamp=
"""


def test_the_timer_jobs_come_from_systemctl_without_the_owner_timer_itself():
    calls = []

    def run(argv, **kw):
        calls.append(argv)
        return types.SimpleNamespace(stdout=TIMERS if argv[1] == "list-timers" else SHOW)

    got = events.job_units(run)
    assert [u["unit"] for u in got] == ["awb-import-auto", "awb-refresh-weekly"]
    assert got[0] == {"unit": "awb-import-auto", "active": False, "result": "success",
                      "start": dt.datetime.fromtimestamp(1763629200, UTC),
                      "exit": dt.datetime.fromtimestamp(1763629205, UTC)}
    assert got[1]["exit"] is None and got[1]["result"] == "exit-code"
    assert "awb-owner-status.service" not in calls[1]
    assert events.job_units(lambda argv, **kw: (_ for _ in ()).throw(OSError("no systemctl"))) == []


def _unit(name, start, active, result="success", exit_=None):
    return {"unit": name, "active": active, "result": result, "start": start, "exit": exit_}


def test_a_timer_job_is_written_when_it_runs_long_or_ends_red_and_the_first_tick_only_learns(board):
    long_, short, red = "awb-refresh-weekly", "awb-import-auto", "awb-dataset-weekly"
    seen = events.observe_jobs([_unit(short, at(-30), False, exit_=at(-29)),
                                _unit(red, at(-30), False, "exit-code", at(-29))], None, board=board, now=at(0))
    assert events.read(board) == [] and seen[red]["done"] is True
    # the same finished runs on the next tick: nothing new
    seen = events.observe_jobs([_unit(short, at(-30), False, exit_=at(-29)),
                                _unit(red, at(-30), False, "exit-code", at(-29))], seen, board=board, now=at(1))
    assert events.read(board) == []
    # a long run: listed once it runs a minute, then its end; a short ok run never; a short red run with its times
    seen = events.observe_jobs([_unit(long_, at(1), True), _unit(short, at(1.5), True)], seen, board=board,
                               now=at(1.6))
    assert events.read(board) == []
    units = [_unit(long_, at(1), True), _unit(short, at(1.5), False, exit_=at(1.6))]
    seen = events.observe_jobs(units, seen, board=board, now=at(3))
    seen = events.observe_jobs(units, seen, board=board, now=at(4))
    run = events.running_now(events.read(board), at(4))
    assert [(r["kind"], r["id"], r["since"]) for r in run] == [("job", long_, events._z(at(1)))]
    units = [_unit(long_, at(1), False, exit_=at(20)), _unit(short, at(1.5), False, exit_=at(1.6)),
             _unit(red, at(10), False, "exit-code", at(10.2))]
    seen = events.observe_jobs(units, seen, board=board, now=at(21))
    seen = events.observe_jobs(units, seen, board=board, now=at(22))
    ended = events.ended_since(events.read(board), at(0), at(22))
    assert [(e["id"], e["outcome"], e["start"], e["end"]) for e in ended] == [
        (red, "red", events._z(at(10)), "2026-11-20T09:10:12Z"), (long_, "ok", events._z(at(1)), events._z(at(20)))]
    assert len(events.read(board)) == 4


def test_a_lab_run_writes_its_start_and_a_red_end_when_a_delete_failed(home, monkeypatch):
    run = "run-20261120-090000-ab2c"
    answers = iter([{"ok": True, "run": run, "started": "2026-11-20T09:00:00Z"},
                    {"ok": True, "run": run, "created": 2, "removed": [{"label": "vpc", "name": "v"}],
                     "kept": [], "failed": [{"label": "ecs", "name": "e", "error": "busy", "delete": "DELETE"}]}])
    monkeypatch.setattr(labclient, "_request", lambda obj, root=None, fds=(): next(answers))
    monkeypatch.setattr(config, "paths", lambda: home)
    assert labclient._start(None, PROJECT, "test-10491", "") == labclient.EXIT_OK
    board = home.shared / "build"
    assert [(r["kind"], r["id"], r["project"]) for r in events.running_now(events.read(board))] == [
        ("labrun", run, PROJECT)]
    assert labclient._end(None, PROJECT, "test-10491") == labclient.EXIT_LEFT
    got = events.read(board)
    assert [(e["ev"], e["kind"]) for e in got] == [("start", "labrun"), ("end", "labrun")]
    assert got[1]["outcome"] == "red" and got[1]["project"] == PROJECT


def test_the_owner_lines_name_the_lock_and_the_login_in_plain_words(board):
    events.start("vault", "vault", board=board, now=at(0))
    events.start("login", "build-user", board=board, now=at(1))
    events.start("keys", "keys", board=board, now=at(2))
    events.end("keys", "keys", "ok", board=board, now=at(10))
    events.start("job", "awb-refresh-weekly", board=board, now=at(3))
    events.end("job", "awb-refresh-weekly", "red", board=board, now=at(12))
    hm = lambda m: at(m).astimezone().strftime("%H:%M")   # noqa: E731
    assert events.status_lines(board, at(20)) == [
        "Running now",
        "  the vault locked since %s" % hm(0),
        "  the Claude login of the build user lapsed since %s" % hm(1),
        "Ended in the last 24 h",
        "  RED timer job awb-refresh-weekly at %s (started %s)" % (hm(12), hm(3)),
        "  ok the key service unlocked at %s (locked since %s)" % (hm(10), hm(2))]


# --------------------------------------------------------------------------- the push


def test_the_push_mails_each_event_that_needs_him_once_and_nothing_else(board):
    events.start("check", "C-301", board=board, now=at(0))
    events.end("check", "C-301", "red", board=board, now=at(30))
    events.start("task", "B-012", project=PROJECT, board=board, now=at(1))
    events.end("task", "B-012", "needs-you", board=board, now=at(31))
    events.start("deploy", "abc1234", board=board, now=at(2))
    events.end("deploy", "abc1234", "ok", board=board, now=at(32))
    events.start("task", "B-013", board=board, now=at(3))
    events.end("task", "B-013", "stopped", board=board, now=at(33))
    events.start("vault", "vault", board=board, now=at(34))
    events.start("ask", HANDLE, project=PROJECT, board=board, now=at(4))
    events.end("ask", HANDLE, "ok", project=PROJECT, board=board, now=at(35))
    events.start("ask", HANDLE[:-1] + "2", project=PROJECT, board=board, now=at(5))
    events.end("ask", HANDLE[:-1] + "2", "ok", project=PROJECT, board=board, now=at(36))
    mail = Mail()
    state, counts = events.push({"sent": []}, mail, blocking=lambda h: h == HANDLE, board=board, now=at(40))
    subjects = [s for s, _ in mail.sent]
    assert subjects == ["Workbench: RED check C-301", "Workbench: build task B-012 (%s) needs you" % PROJECT,
                        "Workbench: the vault locked", "Workbench: blocking dev ask %s answered" % HANDLE]
    assert counts == {"new": 4, "sent": 4, "held": 0, "withheld": 0}
    assert "ended red at 2026-11-20 09:30 UTC, started 2026-11-20 09:00 UTC" in mail.sent[0][1]
    # the next tick: nothing new
    again = Mail()
    state, counts = events.push(state, again, blocking=lambda h: h == HANDLE, board=board, now=at(41))
    assert again.sent == [] and counts["new"] == 0
    for subject, body in mail.sent:
        fx.assert_no_fixture_name(subject + "\n" + body)


def test_a_lock_that_is_gone_before_the_mail_is_never_pushed(board):
    events.start("keys", "keys", board=board, now=at(0))
    events.end("keys", "keys", "ok", board=board, now=at(1))
    mail = Mail()
    events.push({"sent": []}, mail, board=board, now=at(2))
    assert mail.sent == []


def test_the_first_tick_marks_what_is_there_and_mails_nothing(board):
    events.start("check", "C-301", board=board, now=at(0))
    events.end("check", "C-301", "red", board=board, now=at(1))
    mail = Mail()
    state, counts = events.push(None, mail, board=board, now=at(2))
    assert mail.sent == [] and counts["new"] == 1 and len(state["sent"]) == 1
    events.end("check", "C-302", "red", board=board, now=at(3))
    events.push(state, mail, board=board, now=at(4))
    assert [s for s, _ in mail.sent] == ["Workbench: RED check C-302"]


def test_a_mail_that_did_not_go_out_is_tried_again_and_an_old_event_never(board):
    events.end("check", "C-301", "red", board=board, now=at(0))
    events.end("check", "C-302", "red", board=board, now=at(1))
    down = Mail(ok=False)
    state, counts = events.push({"sent": []}, down, board=board, now=at(2))
    assert counts == {"new": 2, "sent": 0, "held": 2, "withheld": 0} and state["sent"] == []
    up = Mail()
    state, counts = events.push(state, up, board=board, now=at(3), limit=1)
    assert [s for s, _ in up.sent] == ["Workbench: RED check C-301"] and counts["held"] == 1
    state, counts = events.push(state, up, board=board, now=at(4), limit=1)
    assert [s for s, _ in up.sent][-1] == "Workbench: RED check C-302"
    late = Mail()
    events.push({"sent": []}, late, board=board, now=at(25 * 60))
    assert late.sent == []


def test_the_name_check_withholds_a_push_and_a_locked_vault_lets_only_fixed_words_out(board):
    events.end("check", "C-301", "red", board=board, now=at(0))
    events.end("job", "awb-refresh-weekly", "red", board=board, now=at(1))
    mail = Mail()
    state, counts = events.push({"sent": []}, mail, check=lambda text: "unavailable", board=board, now=at(2))
    assert [s for s, _ in mail.sent] == ["Workbench: RED timer job awb-refresh-weekly"]
    assert counts["held"] == 1
    state, counts = events.push(state, mail, check=lambda text: "found", board=board, now=at(3))
    assert counts["withheld"] == 1 and len(mail.sent) == 1
    state, counts = events.push(state, mail, check=lambda text: "passed", board=board, now=at(4))
    assert counts["new"] == 0 and len(mail.sent) == 1


def test_the_push_state_is_a_private_file_and_a_bad_one_is_an_empty_state(tmp_path):
    path = tmp_path / "push.json"
    assert events.read_state(path) is None
    assert events.write_state(path, {"sent": ["k%d" % i for i in range(events.SENT_MAX + 5)], "jobs": {"a": {}}})
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600
    got = events.read_state(path)
    assert len(got["sent"]) == events.SENT_MAX and got["jobs"] == {"a": {}}
    path.write_text("not json")
    assert events.read_state(path) == {"sent": [], "jobs": {}}


def test_the_minute_tick_writes_the_lock_mails_it_and_refreshes_the_status(home, tmp_path, monkeypatch):
    monkeypatch.setattr(owner, "login_lapsed", lambda runs=None: False)
    state_path = tmp_path / "owner-status" / owner.PUSH_STATE
    state_path.parent.mkdir()
    status = {"vault": {"state": "locked", "since": None}, "keys": {"state": "unlocked", "since": None},
              "running": [], "ended": []}
    mail = Mail()
    counts = owner.push_tick(status, home, state_path, send=mail, units=[], now=at(0))
    assert counts["sent"] == 0 and mail.sent == []          # the first tick only learns
    assert [r["kind"] for r in status["running"]] == ["vault"]
    status["vault"] = {"state": "unlocked", "since": None}
    owner.push_tick(status, home, state_path, send=mail, units=[], now=at(1))
    status["vault"] = {"state": "locked", "since": None}
    counts = owner.push_tick(status, home, state_path, send=mail, units=[], now=at(2))
    assert [s for s, _ in mail.sent] == ["Workbench: the vault locked"] and counts["sent"] == 1
    assert [r["kind"] for r in status["ended"]] == ["vault"]
    owner.push_tick(status, home, state_path, send=mail, units=[], now=at(3))
    assert len(mail.sent) == 1
    assert stat.S_IMODE(os.stat(state_path).st_mode) == 0o600
    ownerwait.validate_status({**_doc(), "running": status["running"], "ended": status["ended"]})


def test_a_blocking_ask_is_found_from_its_file(home, monkeypatch):
    from awb import devask

    folder = devask.folder(home)
    folder.mkdir(parents=True, exist_ok=True)
    head = {"project": PROJECT, "title": "a fix", "created": "2026-11-20T09:00:00+00:00", "blocks": True,
            "state": "closed"}
    (folder / (HANDLE + ".md")).write_text("<!-- %s -->\n# a fix\n" % json.dumps(head))
    blocking = owner.ask_blocks(home)
    assert blocking(HANDLE) is True
    assert blocking(HANDLE[:-1] + "9") is False


# --------------------------------------------------------------------------- the status file and the API


def _doc(**more) -> dict:
    return {"version": 1, "written": "2026-11-20T09:00:00Z", "vault": {"state": "unlocked", "since": None},
            "keys": {"state": "unlocked", "since": None}, "deployed": {"release": None, "journal": None},
            "page": {"size": None, "time": None, "backup_time": None, "backups": 0}, "ui_runs": [], "intake": [],
            **more}


ENDED = {"kind": "job", "id": "awb-refresh-weekly", "project": None, "start": "2026-11-20T06:00:00Z",
         "end": "2026-11-20T06:41:00Z", "outcome": "red"}
RUNNING = {"kind": "vault", "id": "vault", "project": None, "since": "2026-11-20T09:00:00Z", "eta": None,
           "overdue": False}


def test_the_status_carries_ended_and_the_new_kinds_and_owner_actions_serves_them(home, tmp_path):
    events.start("job", "awb-refresh-weekly", board=home.shared / "build", now=at(-180))
    events.end("job", "awb-refresh-weekly", "red", board=home.shared / "build", now=at(-139))
    assert events.ended_rows(home, at(0)) == [ENDED]
    status = _doc(running=[RUNNING], ended=[ENDED])
    ownerwait.validate_status(status)
    path = tmp_path / "status.json"
    owner.write(status, path, checker=lambda text: "passed")
    code, state = owner_actions.answer("/api/owner/state", owner_actions.read_status(path), at(1).timestamp())
    assert code == 200 and state["running"] == [RUNNING] and state["ended"] == [ENDED]
    assert owner_actions.answer("/api/owner/state", _doc(), at(1).timestamp())[1]["ended"] == []
    assert set(contract.EVENT_KINDS) == set(events.KINDS) == set(status_shape.RUNNING_KINDS)


@pytest.mark.parametrize("change", [
    lambda r: r.update(kind="timer"), lambda r: r.update(id="a b"), lambda r: r.update(project="someone"),
    lambda r: r.update(end=None), lambda r: r.update(start="soon"), lambda r: r.update(outcome="fine"),
    lambda r: r.update(note="x")])
def test_an_ended_row_of_another_shape_is_refused(change, tmp_path):
    row = dict(ENDED)
    change(row)
    with pytest.raises(status_shape.StatusError):
        ownerwait.validate_status(_doc(ended=[row]))
    path = tmp_path / "status.json"
    path.write_text(json.dumps(_doc(ended=[row])))
    assert owner_actions.read_status(path) is None
