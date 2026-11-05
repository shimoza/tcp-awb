"""Task WL part 1 (B-035): the registry of the owner's Windows lab machines and their windows (awb/winlab.py, awb
winlab, the open windows in awb owner status).

No network: nothing is reached. The registry is winlab.json beside a keys.conf in tmp_path; the clock is a fixed
datetime passed as `now` or put in place of winlab._now. Every machine name, code and fingerprint is invented.
"""
from __future__ import annotations

import datetime
import json
import stat

import pytest

from awb import cli, config, ownerwait, winlab

MACHINE = "lab-1"
OTHER_MACHINE = "lab-2"
PROJECT = "tcp-ab2c"
OTHER = "tcp-zz99"
FINGERPRINT = "SHA256:" + ("invented" * 6)[:43]
OTHER_FINGERPRINT = "SHA256:" + ("fixture+" * 6)[:43]
T0 = datetime.datetime(2026, 11, 5, 10, 0, tzinfo=datetime.timezone.utc)


def minutes(n: int) -> datetime.datetime:
    return T0 + datetime.timedelta(minutes=n)


@pytest.fixture
def owner(tmp_path, monkeypatch):
    """The owner's own terminal: keys.conf in tmp_path, no assistant session, not the work user."""
    conf = tmp_path / "conf" / "keys.conf"
    conf.parent.mkdir()
    conf.write_text("region = eu-de\n", encoding="utf-8")
    monkeypatch.setenv("AWB_KEYS_CONF", str(conf))
    monkeypatch.delenv("CLAUDECODE", raising=False)
    monkeypatch.delenv("CLAUDE_CODE_ENTRYPOINT", raising=False)
    monkeypatch.setattr(config, "is_work_user", lambda: False)
    return conf.parent / winlab.FILE_NAME


@pytest.fixture
def registered(owner):
    winlab.add(MACHINE, 2201, FINGERPRINT, True, today="2026-11-05")
    return owner


def test_wl_add_registers_a_machine_beside_keys_conf_mode_600(owner, capsys):
    assert cli.main(["winlab", "add", MACHINE, "--port", "2201", "--fingerprint", FINGERPRINT,
                     "--no-customer-data"]) == 0
    assert "lab machine %s registered: port 2201" % MACHINE in capsys.readouterr().out
    assert stat.S_IMODE(owner.stat().st_mode) == 0o600
    m = json.loads(owner.read_text(encoding="utf-8"))["machines"][MACHINE]
    assert m["port"] == 2201 and m["fingerprint"] == FINGERPRINT and m["no_customer_data"] is True
    assert m["registered"] == datetime.date.today().isoformat() and "window" not in m


# --------------------------------------------------------------------------- the refusals of add


def test_wl_add_is_refused_without_no_customer_data(owner, capsys):
    assert cli.main(["winlab", "add", MACHINE, "--port", "2201", "--fingerprint", FINGERPRINT]) == 1
    err = capsys.readouterr().err
    assert "add needs --no-customer-data" in err and len(err.strip().splitlines()) == 1
    with pytest.raises(winlab.WinlabError, match="--no-customer-data"):
        winlab.add(MACHINE, 2201, FINGERPRINT, False)
    assert not owner.exists()


@pytest.mark.parametrize("fingerprint", [
    "", "SHA256:", "sha256:" + ("invented" * 6)[:43], "MD5:" + ("invented" * 6)[:43], FINGERPRINT[:-1],
    FINGERPRINT + "a", FINGERPRINT + "=", FINGERPRINT[:-1] + "!", "SHA256: " + ("invented" * 6)[:42],
    ("invented" * 6)[:43]])
def test_wl_add_is_refused_with_a_malformed_fingerprint(owner, capsys, fingerprint):
    assert cli.main(["winlab", "add", MACHINE, "--port", "2201", "--fingerprint", fingerprint,
                     "--no-customer-data"]) == 1
    assert "a fingerprint reads SHA256: and 43 base64 characters" in capsys.readouterr().err
    assert not owner.exists()


@pytest.mark.parametrize("port", ["0", "22", "1023", "65536", "70000", "-1"])
def test_wl_add_is_refused_with_a_port_outside_1024_to_65535(owner, capsys, port):
    assert cli.main(["winlab", "add", MACHINE, "--port", port, "--fingerprint", FINGERPRINT,
                     "--no-customer-data"]) == 1
    assert "a loopback port from 1024 to 65535" in capsys.readouterr().err
    assert not owner.exists()


@pytest.mark.parametrize("port", [1024, 65535])
def test_wl_add_takes_the_ends_of_the_port_range(owner, port):
    winlab.add(MACHINE, port, FINGERPRINT, True)
    assert winlab.load()[MACHINE]["port"] == port


def test_wl_add_refuses_a_name_twice_a_port_twice_and_a_bad_name(registered):
    with pytest.raises(winlab.WinlabError, match="registered already"):
        winlab.add(MACHINE, 2202, OTHER_FINGERPRINT, True)
    with pytest.raises(winlab.WinlabError, match="port 2201 is the port of %s" % MACHINE):
        winlab.add(OTHER_MACHINE, 2201, OTHER_FINGERPRINT, True)
    for name in ("Lab-1", "1lab", "x", "lab_1", "lab 1", "../lab"):
        with pytest.raises(winlab.WinlabError, match="a machine name"):
            winlab.add(name, 2202, OTHER_FINGERPRINT, True)
    assert list(winlab.load()) == [MACHINE]


# --------------------------------------------------------------------------- the refusals of allowed()


def test_wl_allowed_returns_the_machine_inside_its_window(registered):
    winlab.open_(MACHINE, 30, PROJECT, now=T0)
    m = winlab.allowed(MACHINE, PROJECT, now=minutes(10))
    assert m["name"] == MACHINE and m["port"] == 2201 and m["fingerprint"] == FINGERPRINT


def _refusal(name: str, project: str, now: datetime.datetime) -> str:
    with pytest.raises(winlab.WinlabError) as err:
        winlab.allowed(name, project, now=now)
    text = str(err.value)
    assert text and "\n" not in text
    return text


def test_wl_allowed_refuses_an_unregistered_machine(registered):
    assert _refusal(OTHER_MACHINE, PROJECT, T0) == "%s is not a registered lab machine (awb winlab list)" % (
        OTHER_MACHINE)


def test_wl_allowed_refuses_a_machine_without_a_window(registered):
    assert _refusal(MACHINE, PROJECT, T0).startswith("%s has no open window" % MACHINE)
    winlab.open_(MACHINE, 30, PROJECT, now=T0)
    winlab.close(MACHINE, now=minutes(5))
    assert _refusal(MACHINE, PROJECT, minutes(6)).startswith("%s has no open window" % MACHINE)


def test_wl_allowed_refuses_an_expired_window(registered):
    winlab.open_(MACHINE, 30, PROJECT, now=T0)
    assert _refusal(MACHINE, PROJECT, minutes(30)) == "the window of %s for %s ended at 2026-11-05 10:30 UTC" % (
        MACHINE, PROJECT)


def test_wl_allowed_refuses_another_project(registered):
    winlab.open_(MACHINE, 30, PROJECT, now=T0)
    text = _refusal(MACHINE, OTHER, minutes(1))
    assert text == "the window of %s is open for another project, not for %s" % (MACHINE, OTHER)
    assert PROJECT not in text


def test_wl_a_broken_registry_allows_nothing(registered):
    registered.write_text("{not json", encoding="utf-8")
    assert "cannot be read" in _refusal(MACHINE, PROJECT, T0)
    registered.write_text(json.dumps({"machines": {MACHINE: {"port": 2201}}}), encoding="utf-8")
    assert "not a list of valid machines" in _refusal(MACHINE, PROJECT, T0)


# --------------------------------------------------------------------------- the window ends by itself


def test_wl_the_window_ends_by_itself_at_its_end(registered, monkeypatch, capsys):
    clock = [T0]
    monkeypatch.setattr(winlab, "_now", lambda: clock[0])
    assert cli.main(["winlab", "open", MACHINE, "--minutes", "30", "--project", PROJECT]) == 0
    assert "window open for %s until 2026-11-05 10:30 UTC (30 min)" % PROJECT in capsys.readouterr().out
    clock[0] = minutes(29)
    assert winlab.allowed(MACHINE, PROJECT)["name"] == MACHINE
    assert cli.main(["winlab", "list"]) == 0
    assert "open for %s  2026-11-05 10:30 UTC (1 min left)" % PROJECT in capsys.readouterr().out
    assert [n for n, _ in winlab.windows()] == [MACHINE]
    clock[0] = minutes(30)                      # nothing ran in between: the clock alone ends it
    with pytest.raises(winlab.WinlabError, match="ended at 2026-11-05 10:30 UTC"):
        winlab.allowed(MACHINE, PROJECT)
    assert winlab.windows() == []
    assert ownerwait.winlab_items(now=clock[0]) == []
    assert cli.main(["winlab", "list"]) == 0
    assert "ended for %s" % PROJECT in capsys.readouterr().out
    # an ended window is no open window: a new one opens without a close
    assert cli.main(["winlab", "open", MACHINE, "--minutes", "10", "--project", OTHER]) == 0
    assert winlab.allowed(MACHINE, OTHER)["name"] == MACHINE


def test_wl_at_most_one_open_window_per_machine(registered, capsys):
    winlab.add(OTHER_MACHINE, 2202, OTHER_FINGERPRINT, True)
    winlab.open_(MACHINE, 30, PROJECT, now=T0)
    for project in (PROJECT, OTHER):
        with pytest.raises(winlab.WinlabError, match="has an open window for %s until 2026-11-05 10:30 UTC: awb "
                                                     "winlab close %s first" % (PROJECT, MACHINE)):
            winlab.open_(MACHINE, 60, project, now=minutes(5))
    assert winlab.allowed(MACHINE, PROJECT, now=minutes(29))
    winlab.open_(OTHER_MACHINE, 30, OTHER, now=minutes(5))        # another machine has its own window
    assert winlab.close(MACHINE, now=minutes(6)) == "lab machine %s: window of %s closed" % (MACHINE, PROJECT)
    assert winlab.close(MACHINE, now=minutes(7)) == "lab machine %s: no open window" % MACHINE
    winlab.open_(MACHINE, 60, OTHER, now=minutes(7))
    assert winlab.allowed(MACHINE, OTHER, now=minutes(8))


def test_wl_open_refuses_a_bad_project_bad_minutes_and_an_unknown_machine(registered):
    for project in ("tcp-AB2C", "tcp-ab2", "CUST-Q7M4", ""):
        with pytest.raises(winlab.WinlabError, match="a project code reads like tcp-q7m4"):
            winlab.open_(MACHINE, 30, project, now=T0)
    for n in (0, -5, winlab.MINUTES_MAX + 1):
        with pytest.raises(winlab.WinlabError, match="a window lasts 1 to 1440 minutes"):
            winlab.open_(MACHINE, n, PROJECT, now=T0)
    with pytest.raises(winlab.WinlabError, match="not a registered lab machine"):
        winlab.open_(OTHER_MACHINE, 30, PROJECT, now=T0)
    assert "window" not in winlab.load()[MACHINE]


def test_wl_remove_forgets_the_machine_and_its_window(registered, capsys):
    winlab.open_(MACHINE, 30, PROJECT, now=T0)
    assert winlab.remove(MACHINE, now=minutes(1)) == "lab machine %s removed, its window of %s closed" % (
        MACHINE, PROJECT)
    assert winlab.load() == {}
    assert "not a registered lab machine" in _refusal(MACHINE, PROJECT, minutes(2))
    assert cli.main(["winlab", "remove", MACHINE]) == 1
    assert cli.main(["winlab", "list"]) == 0
    assert "no lab machine registered" in capsys.readouterr().out


# --------------------------------------------------------------------------- owner side only


@pytest.mark.parametrize("who", ["work user", "session", "entrypoint"])
def test_wl_owner_commands_are_refused_for_the_work_user_and_in_a_session(registered, monkeypatch, capsys, who):
    winlab.open_(MACHINE, 30, PROJECT, now=T0)
    before = registered.read_text(encoding="utf-8")
    if who == "work user":
        monkeypatch.setattr(config, "is_work_user", lambda: True)
    elif who == "session":
        monkeypatch.setenv("CLAUDECODE", "1")
    else:
        monkeypatch.setenv("CLAUDE_CODE_ENTRYPOINT", "cli")
    for argv in (["add", OTHER_MACHINE, "--port", "2202", "--fingerprint", OTHER_FINGERPRINT, "--no-customer-data"],
                 ["open", MACHINE, "--minutes", "30", "--project", OTHER], ["close", MACHINE],
                 ["remove", MACHINE]):
        assert cli.main(["winlab"] + argv) == 1, argv
        assert winlab.OWNER_LINE in capsys.readouterr().err
    calls = (lambda: winlab.add(OTHER_MACHINE, 2202, OTHER_FINGERPRINT, True),
             lambda: winlab.open_(MACHINE, 30, OTHER, now=minutes(1)), lambda: winlab.close(MACHINE, now=minutes(1)),
             lambda: winlab.remove(MACHINE, now=minutes(1)))
    for call in calls:
        with pytest.raises(winlab.WinlabError) as err:
            call()
        assert str(err.value) == winlab.OWNER_LINE
    assert registered.read_text(encoding="utf-8") == before
    assert cli.main(["winlab", "list"]) == 0                      # list is for anyone
    assert MACHINE in capsys.readouterr().out
    assert winlab.allowed(MACHINE, PROJECT, now=minutes(1))["name"] == MACHINE


# --------------------------------------------------------------------------- awb owner status


def test_wl_owner_status_lists_every_open_window(registered, home, tmp_path):
    winlab.add(OTHER_MACHINE, 2202, OTHER_FINGERPRINT, True)
    winlab.add("lab-3", 2203, FINGERPRINT, True)
    winlab.open_(MACHINE, 30, PROJECT, now=T0)
    winlab.open_(OTHER_MACHINE, 90, OTHER, now=T0)
    winlab.open_("lab-3", 5, PROJECT, now=T0)                     # ended at the time of the status below
    items = ownerwait.winlab_items(now=minutes(10))
    assert [(i.label, i.text, i.where, i.block) for i in items] == [
        (MACHINE, "lab machine window open for %s until 2026-11-05 10:30 UTC" % PROJECT, ownerwait.WHERE_TERMINAL,
         "awb winlab close %s" % MACHINE),
        (OTHER_MACHINE, "lab machine window open for %s until 2026-11-05 11:30 UTC" % OTHER,
         ownerwait.WHERE_TERMINAL, "awb winlab close %s" % OTHER_MACHINE)]
    text = "\n".join(ownerwait.render(items, 0))
    assert "%s  lab machine window open for %s until 2026-11-05 10:30 UTC  [his terminal]" % (MACHINE, PROJECT) in text
    assert "    awb winlab close %s" % OTHER_MACHINE in text
    assert ownerwait.winlab_items(now=minutes(31))[0].label == OTHER_MACHINE
    # the whole status, on the real clock
    for name in (MACHINE, OTHER_MACHINE, "lab-3"):
        winlab.close(name, now=minutes(31))
    winlab.open_("lab-3", 60, OTHER)
    listed, _ = ownerwait.collect(home, tmp_path / "none.md", tmp_path / "runs")
    assert [i.label for i in listed if i.text.startswith("lab machine window")] == ["lab-3"]


def test_wl_owner_status_lists_nothing_without_a_registry(owner):
    assert ownerwait.winlab_items(now=T0) == []
    owner.write_text("{broken", encoding="utf-8")
    assert ownerwait.winlab_items(now=T0) == []
