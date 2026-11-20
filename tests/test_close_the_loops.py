"""Q-0007, finding 4 of the UX review of 2026-11-18: a development ask closes when a released commit names its handle,
the prompt hook shows an ask only while it is new, the hand-over keeps only checks of the last 48 hours that did not
pass and the build line of the board drops tasks and checks that ended long ago."""
from __future__ import annotations

import json
import subprocess
import types
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from awb import cli, config, deploy, devask, handover
from awb.tcp import board
from tests import fixtures as fx
from tests import test_dev_ask as da
from tests.test_deploy import BIN, World, commit, git


@pytest.fixture
def asks(home, monkeypatch, tmp_path):
    from awb import ownerwait, projects

    pr = projects.spawn(home, "query", "check the docs", fx.CUSTOMER_CODE, home.register)
    monkeypatch.setattr(config, "paths", lambda: home)
    monkeypatch.setattr(config, "is_work_user", lambda: True)
    monkeypatch.chdir(pr.path)
    monkeypatch.setattr(ownerwait, "export_items", lambda *a: [])
    return home, pr


def new_ask(world, text="missing docs filter") -> Path:
    home, _ = world
    assert cli.main(["dev", "ask", text]) == 0
    return sorted((home.shared / "notices/dev").glob("*.md"))[-1]


def age(path: Path, hours: int) -> None:
    text = path.read_text()
    old = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()
    path.write_text(text.replace(devask.read(path)["created"], old))


# --------------------------------------------------------------------------- a: the handle in a commit closes the ask


def test_a_handle_is_found_in_a_commit_message_and_never_inside_a_longer_one():
    msg = ("Q-0007: the docs filter (asked in 2026-11-19-tcp-q7m4-5 and notices/dev/2026-11-19-tcp-q7m4-6.md)\n"
           "not 2026-11-19-tcp-q7m4-50x, not x2026-11-19-tcp-q7m4-7, not 2026-11-19-tcp-q7m4-0")
    assert devask.handles_in(msg) == {"2026-11-19-tcp-q7m4-5", "2026-11-19-tcp-q7m4-6"}
    assert devask.handles_in("") == set()


def test_the_close_writes_the_live_note_and_closes_the_ask_with_the_release(asks, capsys):
    home, pr = asks
    first, second = new_ask(asks), new_ask(asks, "second request")
    assert devask.open_handles(home) == {first.stem, second.stem}
    release = "c" * 40
    capsys.readouterr()
    assert cli.main(["dev", "close", "--release", release, first.stem]) == 0
    out = capsys.readouterr().out
    assert out.strip() == "%s: development ask %s live in release ccccccc, note notes/from-dev/%s" % (
        pr.code, first.stem, sorted((Path(pr.path) / "notes/from-dev").glob("*.md"))[-1].name)
    note = sorted((Path(pr.path) / "notes/from-dev").glob("*.md"))[-1].read_text()
    assert note.startswith("# live in release ccccccc\n") and first.name in note and "missing docs filter" in note
    data = devask.read(first)
    assert data["state"] == "closed" and data["release"] == release and data["answer"].startswith("notes/from-dev/")
    assert devask.open_handles(home) == {second.stem}
    assert [d["title"] for _, d in devask.open_asks(home)] == ["second request"]
    # a second close of the same handle passes over it: nothing written, nothing said
    notes_before = sorted((Path(pr.path) / "notes/from-dev").iterdir())
    assert cli.main(["dev", "close", "--release", release, first.stem]) == 0
    assert capsys.readouterr().out == ""
    assert sorted((Path(pr.path) / "notes/from-dev").iterdir()) == notes_before
    fx.assert_no_fixture_name(out + note)


def test_the_close_refuses_a_short_release_and_a_foreign_handle(asks, capsys):
    path = new_ask(asks)
    assert cli.main(["dev", "close", "--release", "c" * 7, path.stem]) == 2
    assert "full commit id" in capsys.readouterr().err
    assert cli.main(["dev", "close", "--release", "c" * 40, "../" + path.stem]) == 2
    assert "not a development ask handle" in capsys.readouterr().out
    assert devask.read(path)["state"] == "open"


def test_the_plan_lists_the_open_asks_a_commit_of_the_target_names(asks, tmp_path):
    home, _ = asks
    named, other = new_ask(asks), new_ask(asks, "other request")
    closed = new_ask(asks, "closed request")
    devask.released(home, [closed.stem], "d" * 40)
    repo = tmp_path / "history"
    repo.mkdir()
    git(repo, "init", "-q")
    (repo / "f").write_text("1")
    commit(repo, "Q-0007: the filter (%s, %s)" % (named.stem, closed.stem))
    (repo / "f").write_text("2")
    target = commit(repo, "Q-0007: unrelated")
    assert deploy.released_asks(subprocess.run, repo, target) == [named.stem]
    assert other.stem not in deploy.released_asks(subprocess.run, repo, target)
    # no repository or no open ask: an empty list, never a refusal of the plan
    assert deploy.released_asks(subprocess.run, tmp_path / "none", target) == []


def test_root_validates_the_asks_of_the_plan():
    base = {"target": "a" * 40, "first": False, "interrupted": False, "rollback": False, "sync": False,
            "web": False, "changed": [], "unmapped": [], "notes": [], "pending_last": [], "keep": []}
    for bad in (["../etc/passwd"], "2026-11-19-tcp-q7m4-1", [1], ["2026-11-19-tcp-q7m4-1 --all"]):
        with pytest.raises(deploy.Refused, match=r"\(asks\)"):
            deploy.validate_plan(dict(base, asks=bad), set())
    with pytest.raises(deploy.Refused) as err:
        deploy.validate_plan(dict(base, asks=["2026-11-19-tcp-q7m4-1"]), set())
    assert "(asks)" not in str(err.value)


@pytest.fixture
def host(tmp_path):
    return World(tmp_path)


def test_a_deploy_that_ends_well_closes_the_asks_as_the_work_user(host, capsys):
    handle = "2026-11-19-tcp-q7m4-5"
    said = "tcp-q7m4: development ask %s live in release bbbbbbb, note notes/from-dev/2026-11-20-1.md" % handle

    def answer(cmd, kw):
        if cmd[:3] == [BIN, "dev", "close"]:
            return subprocess.CompletedProcess(cmd, 0, said + "\n", "")
        return None

    host.runner.on = answer
    host.runner.plan = host.plan(actions={"awb-ask.service": "restart"}, asks=[handle])
    assert host.main([]) == 0
    calls = [(c, k) for c, k in host.runner.calls if c[:3] == [BIN, "dev", "close"]]
    assert len(calls) == 1
    cmd, kw = calls[0]
    assert cmd == [BIN, "dev", "close", "--release", host.c1, handle]
    assert kw["user"] == host.work.pw_uid and kw["cwd"] == str(host.work_home)
    assert said in capsys.readouterr().out


def test_a_deploy_without_asks_or_that_failed_closes_nothing(host, capsys):
    host.runner.plan = host.plan(actions={"awb-ask.service": "restart"})
    assert host.main([]) == 0
    assert not [c for c, _ in host.runner.calls if c[:3] == [BIN, "dev", "close"]]
    host.runner.calls.clear()
    host.runner.plan = host.plan(actions={"awb-ask.service": "restart"}, asks=["2026-11-19-tcp-q7m4-5"])
    host.runner.on = lambda cmd, kw: (subprocess.CompletedProcess(cmd, 1, "", "")
                                      if cmd[0].endswith("/install.sh") else None)
    assert host.main([]) != 0
    assert not [c for c, _ in host.runner.calls if c[:3] == [BIN, "dev", "close"]]
    capsys.readouterr()


def test_a_failed_close_is_one_line_and_the_deploy_stays_ok(host, capsys):
    def answer(cmd, kw):
        if cmd[:3] == [BIN, "dev", "close"]:
            return subprocess.CompletedProcess(cmd, 2, "2026-11-19-tcp-q7m4-5: not closed (x)\x1b[2J\n", "")
        return None

    host.runner.on = answer
    host.runner.plan = host.plan(actions={"awb-ask.service": "restart"}, asks=["2026-11-19-tcp-q7m4-5"])
    assert host.main([]) == 0
    out = capsys.readouterr().out
    assert "awb dev close failed for some asks" in out and "\x1b" not in out


# --------------------------------------------------------------------------- b: the hook shows a new ask only


def test_the_prompt_hook_shows_a_new_ask_once_and_an_old_one_never(asks, monkeypatch, capsys, tmp_path):
    _, pr = asks
    old, new = new_ask(asks, "old request"), new_ask(asks, "new request")
    age(old, devask.NEW_HOURS + 1)
    monkeypatch.setattr(config, "is_work_user", lambda: False)
    checkout = da._checkout(tmp_path)
    out = json.loads(da.prompt(monkeypatch, capsys, checkout))["hookSpecificOutput"]["additionalContext"]
    assert out == "request from %s: new request (notices/dev/%s)" % (pr.code, new.name)
    assert da.prompt(monkeypatch, capsys, checkout) == ""
    assert "old request" not in da.prompt(monkeypatch, capsys, checkout, sid="another-session")
    # the old ask stays open for the owner status and the hand-over
    assert devask.read(old)["state"] == "open" and old.stem in devask.open_handles(asks[0])


# --------------------------------------------------------------------------- d: the hand-over and the board


def test_the_hand_over_keeps_checks_of_48_hours_that_did_not_pass():
    now = datetime(2026, 11, 20, 12, 0)
    lines = ["B-012  queued  since -  waits on the runner",
             "B-013  done  since 2026-11-19 10:00  waits on nothing",
             "B-014  stopped: by_owner  since 2026-11-01 10:00  waits on your word: awb build retry B-014",
             "B-015  red: suite  since 2026-11-01 10:00  waits on your word",
             "C-301  red: suite  since 2026-11-19 13:00  a check of a branch, result in C-301/1",
             "C-302  passed  since 2026-11-20 11:00  a check of a branch, result in C-302/1",
             "C-303  red: suite  since 2026-11-18 11:59  a check of a branch, result in C-303/1",
             "C-304  running  since 2026-11-20 11:30  a check of a branch, waits on the runner",
             "C-305  queued  since ?  a check of a branch, waits on the runner",
             "C-306  stopped: error  since 2026-11-20 11:00  a check of a branch, result in C-306/1",
             "a note of the queue"]
    kept = handover.open_build_lines(lines, now)
    assert [line.split("  ")[0] for line in kept] == ["B-012", "B-015", "C-301", "C-304", "C-305"]


def test_the_build_line_drops_what_ended_48_hours_before_the_newest_row(tmp_path):
    rows = ("task\tB-001\t1\tdone\t2026-11-10T10:00:00\tabcdef1\tsent\n"
            "task\tB-002\t1\tstopped: by_owner\t2026-11-10T11:00:00\t-\tsent\n"
            "task\tB-003\t1\tred: suite\t2026-11-10T12:00:00\t-\tsent\n"
            "task\tB-004\t1\tdone\t2026-11-19T09:00:00\tabcdef2\tsent\n"
            "task\tB-005\t1\trunning\t2026-11-20T10:00:00\t-\t-\n"
            "check\tC-001\tpassed\t2026-11-10T09:00:00\tabcdef3\n"
            "check\tC-002\tred: suite\t2026-11-20T09:30:00\t-\n"
            "live\teb928d2\nmain\t1a2b3c4\nnot_deployed\t0\n")
    (tmp_path / "build").mkdir()
    (tmp_path / "build" / "state.tsv").write_text(rows, encoding="utf-8")
    p = types.SimpleNamespace(shared=tmp_path)
    line = board.build_line(p)
    assert line == ("build: B-003 red: suite, B-004 done 09:00, B-005 running since 10:00, check C-002 red: suite; "
                    "live eb928d2, main 1a2b3c4, 0 not deployed")
    # the board's own data keeps every row and no full start of a check
    data = board.build_state(p)
    assert [t["id"] for t in data["tasks"]] == ["B-001", "B-002", "B-003", "B-004", "B-005"]
    assert all("stamp" not in c and "since" not in c for c in data["checks"])
