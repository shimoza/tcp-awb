"""The budget of the build runner (awb/buildbudget.py, owner's yes of 2026-11-22): a subscription limit holds a task
as `held: usage limit` with its reset, `build_model = auto` runs it again and the next ones on the model key until
the reset (sonnet unless the block is an opus security review), the weekly key cap holds the key runs, each switch
is one event and one mail with the spend of the week. `awb build list` and `awb owner status` name the mode.

The runner runs in the throw-away world of awb.buildrun.World with a stand-in client of this file in front of the
world's own: its scenario `limitonce` answers the first session with the limit text and plays `good` after that.
"""
from __future__ import annotations

import datetime as dt
import json
import sys
import time
from pathlib import Path

import pytest

from awb import build, buildbudget, buildrun, events

UTC = dt.timezone.utc


def at(text: str) -> float:
    return dt.datetime.strptime(text, "%Y-%m-%d %H:%M").replace(tzinfo=UTC).timestamp()


# --------------------------------------------------------------------------- the limit texts

@pytest.mark.parametrize("text, wall, reset", [
    ("You've hit your weekly limit · resets 12am (UTC)", "2026-11-22 15:00", "2026-11-23 00:00"),
    ("5-hour limit reached ∙ resets 3pm (Europe/Berlin)", "2026-11-22 10:00", "2026-11-22 14:00"),
    ("You've hit your limit · resets Nov 24, 5pm (UTC)", "2026-11-22 10:00", "2026-11-24 17:00"),
    ("You've hit your 5-hour limit · resets 10:30pm", "2026-11-22 23:00", "2026-11-23 22:30"),
    ("Claude AI usage limit reached|%d" % at("2026-11-22 18:00"), "2026-11-22 10:00", "2026-11-22 18:00"),
    ('API Error: 429 {"type":"error","error":{"type":"rate_limit_error","message":"x"}}', "2026-11-22 10:00", None),
], ids=["weekly", "five-hour", "date", "minutes", "epoch", "rate-limit"])
def test_the_limit_texts_are_found_and_their_reset_is_parsed(text, wall, reset):
    found, epoch = buildbudget.find_limit([None, json.dumps({"result": text}).encode()], at(wall))
    assert found and epoch == (at(reset) if reset else None)
    assert buildbudget.find_limit([b'{"result": "API Error: overloaded"}', "DONE: the value"], at(wall)) == (
        False, None)


def test_the_fallback_model_is_sonnet_unless_the_block_is_an_opus_security_review():
    assert buildbudget.fallback_model("opus", "B-120: the budget fallback") == "sonnet"
    assert buildbudget.fallback_model("opus", "B-121: security review of the key path") == "opus"
    assert buildbudget.fallback_model("sonnet", "B-122: Security review") == "sonnet"
    assert buildbudget.fallback_model("haiku", "B-123: small fix") == "haiku"


# --------------------------------------------------------------------------- the switches, the ledger, the cap

def test_each_switch_is_one_event_and_one_mail_with_the_spend_and_the_cap_holds_until_the_next_week(tmp_path):
    runs, board = tmp_path / "runs", tmp_path / "board"
    board.mkdir()
    mails: list[tuple[str, list[str]]] = []

    def mail(subject, lines):
        mails.append((subject, lines))
        return True

    wall = at("2026-11-20 10:00")                       # a Friday of week 47
    until = buildbudget.hit(runs, wall, int(at("2026-11-24 12:00")))
    assert oct((runs / buildbudget.STATE_FILE).stat().st_mode & 0o777) == "0o600"
    assert buildbudget.choose("auto", runs, wall, 25) == ("key", None)
    assert buildbudget.choose("login", runs, wall, 25) == ("login", buildbudget.HOLD_LIMIT)
    assert buildbudget.tick("auto", runs, wall, 25, board, mail) == ["key"]
    assert buildbudget.tick("auto", runs, wall + 60, 25, board, mail) == []
    assert [s for s, _ in mails] == ["Workbench build: on the API key"]
    assert "key spend: 0.00 of 25.00 USD this week" in mails[0][1]
    assert "until 2026-11-24 12:00 UTC" in buildbudget.mode_line("auto", runs, wall, 25)
    # the ledger: a reported cost, a run that reports none counts its budget, a second look adds nothing
    assert buildbudget.cost_of({"total_cost_usd": 4.5}, 10) == (4.5, "reported")
    assert buildbudget.cost_of({"result": "DONE: x"}, 10) == (10.0, "budget")
    assert buildbudget.record(runs, wall, "B-101", 1, "r0", 4.5, "reported")
    assert not buildbudget.record(runs, wall, "B-101", 1, "r0", 4.5, "reported")
    assert buildbudget.record(runs, wall, "B-102", 1, "r0", 21.0, "budget")
    assert oct((runs / buildbudget.LEDGER_FILE).stat().st_mode & 0o777) == "0o600"
    assert buildbudget.spent(runs, wall) == 25.5
    assert buildbudget.choose("auto", runs, wall, 25) == ("key", buildbudget.HOLD_CAP)
    assert buildbudget.tick("auto", runs, wall + 120, 25, board, mail) == ["cap"]
    assert buildbudget.tick("auto", runs, wall + 180, 25, board, mail) == []
    assert mails[-1][0] == "Workbench build: key cap reached"
    assert "Build tasks wait until 2026-11-23 00:00 UTC." in mails[-1][1]
    assert "key spend: 25.50 of 25.00 USD this week" in mails[-1][1]
    assert buildbudget.mode_line("auto", runs, wall, 25).startswith("build model: key cap reached")
    # the next week: no spend yet, the key again, then the reset: the login
    monday = at("2026-11-23 01:00")
    assert buildbudget.choose("auto", runs, monday, 25) == ("key", None)
    assert buildbudget.tick("auto", runs, monday, 25, board, mail) == ["key"]
    assert buildbudget.tick("auto", runs, until, 25, board, mail) == ["login"]
    assert mails[-1][0] == "Workbench build: back on the login" and len(mails) == 4
    assert buildbudget.load(runs) == {"until": None, "announced": None}
    assert buildbudget.mode_line("auto", runs, until, 25) == (
        "build model: login (build_model = auto), 0.00 of 25.00 USD this week")
    got = [(e["ev"], e["kind"], e["id"]) for e in events.read(board)]
    assert got == [("start", "budget", "key-fallback"), ("start", "budget", "key-cap"), ("end", "budget", "key-cap"),
                   ("end", "budget", "key-fallback")]
    assert events.read(board)[0]["eta"] == "2026-11-24T12:00:00Z"
    assert not events.push_candidates(events.read(board), dt.datetime.fromtimestamp(until, UTC))
    text = "\n".join(s + "\n" + "\n".join(lines) for s, lines in mails)
    assert "B-10" not in text


def test_a_hit_without_a_reset_waits_an_hour_and_the_host_file_reads_auto_and_the_cap(monkeypatch, tmp_path):
    runs = tmp_path / "runs"
    wall = at("2026-11-22 10:00")
    assert buildbudget.hit(runs, wall, None) == int(wall) + buildbudget.DEFAULT_WAIT
    assert buildbudget.cap_of({}) == 25.0 and buildbudget.cap_of({"key_week_cap_usd": "40"}) == 40.0
    assert buildbudget.cap_of({"key_week_cap_usd": "lots"}) == 25.0
    from awb import config

    monkeypatch.setattr(config, "host_conf", lambda: {"build_model": "auto", "build_model_key": "/k",
                                                      "key_week_cap_usd": "30"})
    h = buildrun.Host.from_config()
    assert (h.model, h.mode, h.build_mode, h.model_key, h.key_cap) == ("login", "auto", "auto", Path("/k"), 30.0)
    assert buildbudget.host_line(runs, wall) == ("build model: key fallback until 2026-11-22 11:00 UTC, 0.00 of "
                                                 "30.00 USD this week")
    monkeypatch.setattr(config, "host_conf", lambda: {})
    assert buildbudget.host_line(runs, wall) == "build model: login, usage limit until 2026-11-22 11:00 UTC, the " \
                                                "queue waits"


# --------------------------------------------------------------------------- the runner

_WRAP = r'''
import json, os, subprocess, sys, time, uuid
prompt = sys.stdin.read()
flag = os.path.join(%(base)r, "limit-seen")
if "SCENARIO: limitonce" in prompt and not os.path.exists(flag):
    open(flag, "w").write("x")
    print(json.dumps({"type": "result", "subtype": "success", "is_error": True, "num_turns": 2,
                      "result": "Claude AI usage limit reached|%%d" %% (int(time.time()) + 3600),
                      "session_id": str(uuid.uuid4()), "total_cost_usd": 0}))
    sys.exit(1)
key = os.path.join(os.environ["HOME"], ".config", "awb-build", "model.key")
open(os.path.join(%(base)r, "argv-%%d" %% len(os.listdir(%(base)r))), "w").write(
    json.dumps({"argv": sys.argv[1:], "key": os.path.exists(key)}))
r = subprocess.run([sys.executable, "-I", %(agent)r] + sys.argv[1:],
                   input=prompt.replace("SCENARIO: limitonce", "SCENARIO: good"), text=True)
sys.exit(r.returncode)
'''


@pytest.fixture
def world(tmp_path):
    w = buildrun.World(tmp_path / "w")
    rec = tmp_path / "rec"
    rec.mkdir()
    wrap = tmp_path / "wrap.py"
    wrap.write_text(_WRAP % {"base": str(rec), "agent": str(w.base / "agent.py")}, encoding="utf-8")
    w.units.commands[buildrun.CLIENT] = [sys.executable, "-I", str(wrap)]
    d = w.root / "home" / ".claude"
    d.mkdir(mode=0o700, exist_ok=True)
    login = d / ".credentials.json"
    login.write_text(json.dumps({"claudeAiOauth": {"accessToken": "fake-login-a", "refreshToken": "fake-login-r",
                                                   "expiresAt": 1_900_000_000_000, "subscriptionType": "max"}}))
    login.chmod(0o600)
    key = tmp_path / "model.key"
    key.write_text("fake-model-key-for-tests\n")
    key.chmod(0o400)
    w.host.model, w.host.model_key = "login", key
    w.rec = rec
    yield w
    w.close()


def polls(w, n: int = 8) -> None:
    r = w.runner()
    for _ in range(n):
        if not r.poll_once():
            break
    return r


def agent_runs(w) -> list[dict]:
    return [json.loads(p.read_text()) for p in sorted(w.rec.glob("argv-*"))]


def test_auto_holds_the_task_on_a_limit_then_runs_it_on_the_key_with_sonnet_and_goes_back_after_the_reset(world):
    world.host.mode = "auto"
    tid = world.task("limitonce")
    polls(world)
    runs = build.runs_of(tid, world.runs)
    assert [r.state for r in runs] == [buildbudget.HELD_LIMIT, "done"]
    held = runs[0]
    assert "note: the subscription limit resets " in (held.folder / "result.md").read_text()
    assert (held.folder / "model").read_text() == "login\n" and (runs[1].folder / "model").read_text() == "key\n"
    (one,) = agent_runs(world)
    assert one["key"] and one["argv"][one["argv"].index("--model") + 1] == "sonnet"
    spec = [s for s in world.units.started if s.name == "awb-task-%s" % tid][-1]
    settings = [src for src, dst in spec.binds if dst.endswith("/.claude/settings.json")]
    assert settings == [str(runs[1].folder / "settings.key.json")]
    assert json.loads(Path(settings[0]).read_text())["apiKeyHelper"] == buildrun.KEY_HELPER
    first = [s for s in world.units.started if s.name == "awb-task-%s" % tid][0]
    assert first.binds[0][0] == str(world.root / "home-template" / ".claude" / "settings.json")
    subjects = [s for s, _ in world.mails]
    assert subjects.count("Workbench build: on the API key") == 1
    assert not any(s.startswith("Workbench build %s: held" % tid) for s in subjects)
    assert not any(s == "Workbench build queue: held" for s in subjects)
    ledger = (world.runs / buildbudget.LEDGER_FILE).read_text().split("\t")
    assert ledger[2:4] == [tid, "2"] and ledger[5:] == ["0.0100", "reported\n"]
    assert "fake-model-key" not in "".join(m for _, m in world.mails)
    # the reset passes: back on the login, one mail
    real = time.time
    world.host.wall = lambda: real() + 7200
    world.task("good", tid=tid)
    polls(world, 2)
    assert world.host.model == "login"
    assert [s for s, _ in world.mails].count("Workbench build: back on the login") == 1
    assert buildbudget.load(world.runs)["until"] is None


def test_auto_at_the_key_cap_holds_the_queue_with_one_mail_and_list_names_it(world, monkeypatch, capsys):
    world.host.mode, world.host.key_cap = "auto", 5.0
    buildbudget.record(world.runs, time.time(), "B-099", 1, "r0", 6.0, "budget")
    tid = world.task("limitonce")
    r = polls(world)
    assert [x.state for x in build.runs_of(tid, world.runs)] == [buildbudget.HELD_LIMIT]
    assert r.model_hold() == buildbudget.HOLD_CAP
    subjects = [s for s, _ in world.mails]
    assert subjects == ["Workbench build: key cap reached"]
    assert "key spend: 6.00 of 5.00 USD this week" in world.mails[0][1]
    assert agent_runs(world) == []
    assert buildbudget.mode_line("auto", world.runs, time.time(), 5.0).startswith("build model: key cap reached")
    from awb import config

    monkeypatch.setattr(config, "host_conf", lambda: {"build_model": "auto", "key_week_cap_usd": "5"})
    monkeypatch.setenv("AWB_BUILD_RUNS", str(world.runs))
    monkeypatch.setenv("AWB_BUILD_QUEUE", str(world.queue))
    assert build.main(["list"]) == 0
    out = capsys.readouterr().out.splitlines()
    assert out[-1].startswith("build model: key cap reached, the queue waits until ")
    assert any(ln.startswith("%s  %s" % (tid, buildbudget.HELD_LIMIT)) for ln in out)


def test_login_mode_holds_the_task_and_the_queue_until_the_reset_then_runs_it_on_the_login(world):
    tid = world.task("limitonce")
    r = polls(world)
    assert [x.state for x in build.runs_of(tid, world.runs)] == [buildbudget.HELD_LIMIT]
    assert r.model_hold() == buildbudget.HOLD_LIMIT
    subjects = [s for s, _ in world.mails]
    assert subjects == ["Workbench build %s: held" % tid]
    assert not buildrun.nudge_due(world.host, build.latest(tid, world.runs))
    assert not (world.runs / buildbudget.LEDGER_FILE).exists()
    real = time.time
    world.host.wall = lambda: real() + 7200
    polls(world)
    runs = build.runs_of(tid, world.runs)
    assert [x.state for x in runs] == [buildbudget.HELD_LIMIT, "done"]
    (one,) = agent_runs(world)
    assert not one["key"] and one["argv"][one["argv"].index("--model") + 1] == "opus"
    assert len(world.mails) == 2 and world.mails[1][0] == "Workbench build %s: done" % tid
