"""Q-0009 (finding 6 of the UX review of 2026-11-18): `awb winlab open` extends the open window of the same project,
takes --until HH:MM beside --minutes and a window that ends within the hour is said once in the project session and
in the owner status.

No network and no lab machine: the registry is winlab.json beside a keys.conf in tmp_path, the clock is a fixed
datetime (passed as `now` or put in place of winlab._now), the zone of --until is a fixed one put in place of
winlab.LOCAL_TZ, and the broker of the last test is the fake one of tests/test_wl_broker.py. Every machine name,
code and fingerprint is invented.
"""
from __future__ import annotations

import datetime
import json

import pytest

from awb import cli, config, ownerwait, rulesync, winlab

from tests.test_wl_broker import MACHINE as BROKER_MACHINE, PROJECT as BROKER_PROJECT  # noqa: F401
from tests.test_wl_broker import lab, served  # noqa: F401  (fixtures)

MACHINE = "lab-1"
OTHER_MACHINE = "lab-2"
PROJECT = "tcp-ab2c"
OTHER = "tcp-zz99"
FINGERPRINT = "SHA256:" + ("invented" * 6)[:43]
OTHER_FINGERPRINT = "SHA256:" + ("fixture+" * 6)[:43]
T0 = datetime.datetime(2026, 11, 5, 10, 0, tzinfo=datetime.timezone.utc)
UTC = datetime.timezone.utc
CET = datetime.timezone(datetime.timedelta(hours=1))
SESSION = "sess-q0009"


def minutes(n: int) -> datetime.datetime:
    return T0 + datetime.timedelta(minutes=n)


@pytest.fixture
def owner(tmp_path, monkeypatch):
    """The owner's own terminal with lab-1 and lab-2 registered, the clock at T0 and --until in CET."""
    conf = tmp_path / "conf" / "keys.conf"
    conf.parent.mkdir()
    conf.write_text("region = eu-de\n", encoding="utf-8")
    monkeypatch.setenv("AWB_KEYS_CONF", str(conf))
    monkeypatch.delenv("CLAUDECODE", raising=False)
    monkeypatch.delenv("CLAUDE_CODE_ENTRYPOINT", raising=False)
    monkeypatch.setattr(config, "is_work_user", lambda: False)
    clock = [T0]
    monkeypatch.setattr(winlab, "_now", lambda: clock[0])
    monkeypatch.setattr(winlab, "LOCAL_TZ", CET)
    winlab.add(MACHINE, 2201, FINGERPRINT, True, today="2026-11-05")
    winlab.add(OTHER_MACHINE, 2202, OTHER_FINGERPRINT, True, today="2026-11-05")
    return clock


def _window(name=MACHINE) -> dict:
    return winlab.load()[name]["window"]


def _root(tmp_path, code=PROJECT):
    root = tmp_path / "projects" / code
    root.mkdir(parents=True, exist_ok=True)
    return root


# --------------------------------------------------------------------------- extend


def test_winlab_extend_open_for_the_same_project_extends_the_window(owner, capsys):
    assert cli.main(["winlab", "open", MACHINE, "--minutes", "60", "--project", PROJECT]) == 0
    assert "window open for %s until 2026-11-05 11:00 UTC (60 min)" % PROJECT in capsys.readouterr().out
    owner[0] = minutes(50)
    assert cli.main(["winlab", "open", MACHINE, "--minutes", "120", "--project", PROJECT]) == 0
    out = capsys.readouterr().out
    assert out.strip() == ("lab machine %s: window of %s extended until 2026-11-05 12:50 UTC (was 2026-11-05 11:00 "
                           "UTC)" % (MACHINE, PROJECT))
    w = _window()
    assert w["start"] == "2026-11-05T10:00:00Z" and w["end"] == "2026-11-05T12:50:00Z" and w["project"] == PROJECT
    assert winlab.allowed(MACHINE, PROJECT, now=minutes(169))["name"] == MACHINE


def test_winlab_extend_never_shortens_the_window(owner, capsys):
    assert cli.main(["winlab", "open", MACHINE, "--minutes", "240", "--project", PROJECT]) == 0
    capsys.readouterr()
    owner[0] = minutes(10)
    assert cli.main(["winlab", "open", MACHINE, "--minutes", "30", "--project", PROJECT]) == 0
    assert capsys.readouterr().out.strip() == ("lab machine %s: window of %s kept until 2026-11-05 14:00 UTC, it "
                                               "ended later already" % (MACHINE, PROJECT))
    assert _window()["end"] == "2026-11-05T14:00:00Z"


def test_winlab_extend_adds_public_image_and_never_takes_it_away(owner, capsys):
    """--public-image is gone (B-080: no content check in WinLab): the option is refused, a window carries no such
    field; extending a window changes only its end."""
    assert cli.main(["winlab", "open", MACHINE, "--minutes", "60", "--project", PROJECT]) == 0
    assert "public_image" not in _window()
    capsys.readouterr()
    owner[0] = minutes(5)
    before = _window()
    assert cli.main(["winlab", "open", MACHINE, "--minutes", "60", "--project", PROJECT, "--public-image"]) == 2
    assert "unrecognized arguments" in capsys.readouterr().err and _window() == before
    assert cli.main(["winlab", "open", MACHINE, "--minutes", "60", "--project", PROJECT]) == 0
    assert "public image" not in capsys.readouterr().out
    assert "public_image" not in _window() and _window()["end"] == "2026-11-05T11:05:00Z"
    owner[0] = minutes(6)
    assert cli.main(["winlab", "open", MACHINE, "--minutes", "90", "--project", PROJECT]) == 0
    assert "public_image" not in _window() and _window()["end"] == "2026-11-05T11:36:00Z"


def test_winlab_extend_still_refuses_the_window_of_another_project(owner, capsys):
    assert cli.main(["winlab", "open", MACHINE, "--minutes", "60", "--project", PROJECT]) == 0
    capsys.readouterr()
    before = _window()
    assert cli.main(["winlab", "open", MACHINE, "--minutes", "60", "--project", OTHER]) == 1
    assert ("has an open window for %s until 2026-11-05 11:00 UTC: awb winlab close %s first" % (PROJECT, MACHINE)
            in capsys.readouterr().err)
    assert _window() == before
    # the function without extend keeps its refusal for the same project too
    with pytest.raises(winlab.WinlabError, match="close %s first" % MACHINE):
        winlab.open_(MACHINE, 60, PROJECT, now=minutes(1))


def test_winlab_extend_keeps_the_limit_of_a_window(owner, capsys):
    assert cli.main(["winlab", "open", MACHINE, "--minutes", "60", "--project", PROJECT]) == 0
    capsys.readouterr()
    assert cli.main(["winlab", "open", MACHINE, "--minutes", str(winlab.MINUTES_MAX + 1), "--project", PROJECT]) == 1
    assert "a window lasts 1 to 1440 minutes" in capsys.readouterr().err
    assert _window()["end"] == "2026-11-05T11:00:00Z"


def test_winlab_extend_after_the_end_opens_a_new_window(owner, capsys):
    assert cli.main(["winlab", "open", MACHINE, "--minutes", "30", "--project", PROJECT]) == 0
    owner[0] = minutes(45)
    assert cli.main(["winlab", "open", MACHINE, "--minutes", "30", "--project", PROJECT]) == 0
    assert "window open for %s until 2026-11-05 11:15 UTC (30 min)" % PROJECT in capsys.readouterr().out
    assert _window()["start"] == "2026-11-05T10:45:00Z"


def test_winlab_extend_leaves_close_as_it_was(owner, capsys):
    assert cli.main(["winlab", "open", MACHINE, "--minutes", "60", "--project", PROJECT]) == 0
    assert cli.main(["winlab", "open", MACHINE, "--minutes", "90", "--project", PROJECT]) == 0
    capsys.readouterr()
    assert cli.main(["winlab", "close", MACHINE]) == 0
    assert capsys.readouterr().out.strip() == "lab machine %s: window of %s closed" % (MACHINE, PROJECT)
    assert "window" not in winlab.load()[MACHINE]


# --------------------------------------------------------------------------- --until


def test_winlab_until_takes_local_time_today(owner, capsys):
    # T0 is 11:00 in CET; 17:30 CET is 16:30 UTC
    assert cli.main(["winlab", "open", MACHINE, "--until", "17:30", "--project", PROJECT]) == 0
    assert "window open for %s until 2026-11-05 16:30 UTC (390 min)" % PROJECT in capsys.readouterr().out
    assert _window()["end"] == "2026-11-05T16:30:00Z"


def test_winlab_until_takes_tomorrow_when_the_time_has_passed(owner):
    assert winlab.until_end("09:15", T0, CET) == datetime.datetime(2026, 11, 6, 8, 15, tzinfo=UTC)
    assert winlab.until_end("11:00", T0, CET) == datetime.datetime(2026, 11, 6, 10, 0, tzinfo=UTC)
    assert winlab.until_end("11:01", T0, CET) == datetime.datetime(2026, 11, 5, 10, 1, tzinfo=UTC)
    assert winlab.until_end("0:30", T0, UTC) == datetime.datetime(2026, 11, 6, 0, 30, tzinfo=UTC)


def test_winlab_until_extends_an_open_window(owner, capsys):
    assert cli.main(["winlab", "open", MACHINE, "--minutes", "60", "--project", PROJECT]) == 0
    capsys.readouterr()
    owner[0] = minutes(40)
    assert cli.main(["winlab", "open", MACHINE, "--until", "20:00", "--project", PROJECT]) == 0
    assert "extended until 2026-11-05 19:00 UTC (was 2026-11-05 11:00 UTC)" in capsys.readouterr().out


@pytest.mark.parametrize("value", ["25:00", "7.30", "17:3", "1730", "", "noon"])
def test_winlab_until_refuses_a_bad_time(owner, capsys, value):
    assert cli.main(["winlab", "open", MACHINE, "--until", value, "--project", PROJECT]) == 1
    assert "--until takes a local time HH:MM (17:30)" in capsys.readouterr().err
    assert "window" not in winlab.load()[MACHINE]


def test_winlab_until_keeps_the_limit_and_excludes_minutes(owner, capsys):
    # a time of tomorrow more than 1440 minutes away cannot happen; a time under a minute away is refused
    owner[0] = datetime.datetime(2026, 11, 5, 10, 59, 30, tzinfo=UTC)
    with pytest.raises(winlab.WinlabError, match="a window lasts 1 to 1440 minutes: --until 12:00 is 0 min"):
        winlab.open_(MACHINE, None, PROJECT, until="12:00")
    with pytest.raises(winlab.WinlabError, match="give --minutes or --until, not both"):
        winlab.open_(MACHINE, 30, PROJECT, until="12:30")
    assert cli.main(["winlab", "open", MACHINE, "--minutes", "30", "--until", "12:30", "--project", PROJECT]) == 2
    assert cli.main(["winlab", "open", MACHINE, "--project", PROJECT]) == 2
    assert "window" not in winlab.load()[MACHINE]


# --------------------------------------------------------------------------- the notice and the session warning


def test_winlab_extend_open_and_extension_leave_one_notice(owner):
    winlab.open_(MACHINE, 60, PROJECT, extend=True)
    path = winlab.notice_path(MACHINE, PROJECT)
    assert json.loads(path.read_text(encoding="utf-8")) == {"machine": MACHINE, "project": PROJECT,
                                                            "end": "2026-11-05T11:00:00Z"}
    assert path.stat().st_mode & 0o777 == 0o640
    owner[0] = minutes(30)
    winlab.open_(MACHINE, 90, PROJECT, extend=True)
    assert json.loads(path.read_text(encoding="utf-8"))["end"] == "2026-11-05T12:00:00Z"
    assert sorted(f.name for f in path.parent.iterdir() if f.name.startswith("winlab-")) == [path.name]


def test_winlab_extend_warns_the_session_once_in_the_last_hour(owner, tmp_path):
    p = config.paths()
    root = _root(tmp_path)
    winlab.open_(MACHINE, 120, PROJECT, extend=True)
    owner[0] = minutes(59)                                         # 61 minutes left: no warning yet
    assert winlab.window_warnings(p, SESSION, root) == []
    owner[0] = minutes(65)
    lines = winlab.window_warnings(p, SESSION, root)
    assert lines == [winlab.WARNING % (MACHINE, PROJECT, "2026-11-05 12:00 UTC", 55, MACHINE, PROJECT)]
    assert "awb winlab open %s --minutes N --project %s (or --until HH:MM)" % (MACHINE, PROJECT) in lines[0]
    owner[0] = minutes(70)
    assert winlab.window_warnings(p, SESSION, root) == []          # once per session
    assert len(winlab.window_warnings(p, "sess-other", root)) == 1  # another session is told too
    # the told window survives the session start hook
    rulesync.remember(p, SESSION, root)
    assert winlab.window_warnings(p, SESSION, root) == []
    # an extension is a new end: warned again in its own last hour
    winlab.open_(MACHINE, 120, PROJECT, extend=True)
    assert winlab.window_warnings(p, SESSION, root) == []
    owner[0] = minutes(130)
    assert len(winlab.window_warnings(p, SESSION, root)) == 1
    owner[0] = minutes(200)                                        # ended: nothing
    assert winlab.window_warnings(p, "sess-late", root) == []


def test_winlab_extend_warning_is_for_its_own_project_only(owner, tmp_path):
    p = config.paths()
    winlab.open_(MACHINE, 30, PROJECT, extend=True)
    owner[0] = minutes(5)
    assert winlab.window_warnings(p, SESSION, _root(tmp_path, OTHER)) == []
    assert winlab.window_warnings(p, SESSION, None) == []
    assert winlab.window_warnings(p, None, _root(tmp_path)) == []
    assert len(winlab.window_warnings(p, SESSION, _root(tmp_path))) == 1


def test_winlab_extend_no_warning_after_close(owner, tmp_path):
    p = config.paths()
    winlab.open_(MACHINE, 30, PROJECT, extend=True)
    winlab.close(MACHINE)
    owner[0] = minutes(5)
    assert winlab.window_warnings(p, SESSION, _root(tmp_path)) == []


def test_winlab_extend_warning_waits_while_the_broker_is_away(owner, tmp_path, monkeypatch):
    p = config.paths()
    root = _root(tmp_path)
    winlab.open_(MACHINE, 30, PROJECT, extend=True)
    owner[0] = minutes(5)

    def away(*_a):
        raise OSError("the lab broker does not answer")

    real = winlab._window_end
    monkeypatch.setattr(winlab, "_window_end", away)
    assert winlab.window_warnings(p, SESSION, root) == []
    monkeypatch.setattr(winlab, "_window_end", real)
    owner[0] = minutes(6)
    assert len(winlab.window_warnings(p, SESSION, root)) == 1


def test_winlab_extend_hook_shows_the_warning(owner, tmp_path, monkeypatch):
    from awb import hooks

    root = _root(tmp_path)
    winlab.open_(MACHINE, 30, PROJECT, extend=True)
    owner[0] = minutes(5)
    monkeypatch.setattr(hooks, "project_root", lambda _folder: root)
    text = hooks._rules_text({"session_id": SESSION, "cwd": str(root)}) or ""
    assert "lab window of %s for %s ends at 2026-11-05 10:30 UTC (25 min left)" % (MACHINE, PROJECT) in text


def test_winlab_extend_broker_confirms_the_end(served, monkeypatch):
    lab = served
    winlab.open_(BROKER_MACHINE, 30, BROKER_PROJECT, path=lab.registry, now=lab.clock[0], extend=True)
    lab.clock[0] = minutes(10)
    monkeypatch.setattr(winlab, "_now", lambda: minutes(10))
    root = lab.paths.projects_root / BROKER_PROJECT
    lines = winlab.window_warnings(config.paths(), SESSION, root)
    assert lines == [winlab.WARNING % (BROKER_MACHINE, BROKER_PROJECT, "2026-11-05 10:30 UTC", 20, BROKER_MACHINE,
                                       BROKER_PROJECT)]


# --------------------------------------------------------------------------- the owner status


def test_winlab_extend_owner_status_lists_a_window_that_ends_within_the_hour(owner):
    winlab.open_(MACHINE, 30, PROJECT, extend=True)
    winlab.open_(OTHER_MACHINE, 180, OTHER, extend=True)
    owner[0] = minutes(10)
    items = ownerwait.winlab_ending_items(now=minutes(10))
    assert [(i.label, i.text, i.where, i.block) for i in items] == [
        (MACHINE, "lab window of %s for %s ends in 20 min (2026-11-05 10:30 UTC)" % (MACHINE, PROJECT),
         ownerwait.WHERE_TERMINAL, "awb winlab open %s --minutes 60 --project %s" % (MACHINE, PROJECT))]
    assert [i.label for i in ownerwait.winlab_ending_items(now=minutes(125))] == [OTHER_MACHINE]
    assert ownerwait.winlab_ending_items(now=minutes(181)) == []
    # the open window line stays as it was
    assert [i.text for i in ownerwait.winlab_items(now=minutes(10))][0] == (
        "lab machine window open for %s until 2026-11-05 10:30 UTC" % PROJECT)
