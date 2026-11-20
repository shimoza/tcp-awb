"""Task WL part 1 (B-035): the registry of the owner's Windows lab machines and their windows (awb/winlab.py, awb
winlab, the open windows in awb owner status).

No network: nothing is reached. The registry is winlab.json beside a keys.conf in tmp_path; the clock is a fixed
datetime passed as `now` or put in place of winlab._now. Every machine name, code and fingerprint is invented.
"""
from __future__ import annotations

import datetime
import json
import stat
from pathlib import Path

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
    # UX10 (owner decision 2026-11-20): in a session of the owner user open and close run, add and remove stay
    # refused; the work user is refused all four
    session = who != "work user"
    for argv in (["add", OTHER_MACHINE, "--port", "2202", "--fingerprint", OTHER_FINGERPRINT, "--no-customer-data"],
                 ["open", MACHINE, "--minutes", "30", "--project", OTHER], ["close", MACHINE],
                 ["remove", MACHINE]):
        if session and argv[0] in ("open", "close"):
            continue
        assert cli.main(["winlab"] + argv) == 1, argv
        assert winlab.OWNER_LINE in capsys.readouterr().err
    calls = (lambda: winlab.add(OTHER_MACHINE, 2202, OTHER_FINGERPRINT, True),
             lambda: winlab.open_(MACHINE, 30, OTHER, now=minutes(1)), lambda: winlab.close(MACHINE, now=minutes(1)),
             lambda: winlab.remove(MACHINE, now=minutes(1)))
    for call in (calls[0], calls[3]) if session else calls:
        with pytest.raises(winlab.WinlabError) as err:
            call()
        assert str(err.value) == winlab.OWNER_LINE
    if session:
        # open of another project is refused by the open window, not by the side, and --public-image stays his
        with pytest.raises(winlab.WinlabError) as err:
            winlab.open_(MACHINE, 30, OTHER, now=minutes(1))
        assert "has an open window for %s" % PROJECT in str(err.value)
        with pytest.raises(winlab.WinlabError) as err:
            winlab.open_(MACHINE, 30, PROJECT, now=minutes(1), public_image=True)
        assert str(err.value) == winlab.PUBLIC_IMAGE_LINE
        assert registered.read_text(encoding="utf-8") == before
    assert registered.read_text(encoding="utf-8") == before
    assert cli.main(["winlab", "list"]) == 0                      # list is for anyone
    assert MACHINE in capsys.readouterr().out
    assert winlab.allowed(MACHINE, PROJECT, now=minutes(1))["name"] == MACHINE
    if session:
        # the allowed forms run in a session: close ends the window, open gives one to the other project
        assert winlab.close(MACHINE, now=minutes(2)) == "lab machine %s: window of %s closed" % (MACHINE, PROJECT)
        assert "window open for %s" % OTHER in winlab.open_(MACHINE, 30, OTHER, now=minutes(3))
        assert winlab.allowed(MACHINE, OTHER, now=minutes(4))["name"] == MACHINE


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


# Review regressions: no live registry, process, or repository is needed.
@pytest.mark.parametrize("field", ["name", "fingerprint", "account", "project"])
def test_wl_review_rejects_trailing_newline(owner, field):
    values = dict(name=MACHINE, fingerprint=FINGERPRINT, account=winlab.ACCOUNT)
    if field == "project":
        winlab.add(MACHINE, 2201, FINGERPRINT, True)
        with pytest.raises(winlab.WinlabError):
            winlab.open_(MACHINE, 30, PROJECT + "\n", now=T0)
    else:
        values[field] += "\n"
        with pytest.raises(winlab.WinlabError):
            winlab.add(port=2201, no_customer_data=True, **values)


@pytest.mark.parametrize("field,value", [("registered", "2026-99-99"), ("registered", 20261105),
                                         ("account", ["awblab"]), ("fingerprint", FINGERPRINT + "\n")])
def test_wl_review_malformed_registry_fields_fail_closed(registered, field, value):
    data = json.loads(registered.read_text())
    data["machines"][MACHINE][field] = value
    registered.write_text(json.dumps(data))
    with pytest.raises(winlab.WinlabError):
        winlab.load()
    assert winlab.windows() == []


@pytest.mark.parametrize("kind", ["link", "dangling", "fifo", "hardlink", "directory"])
def test_wl_review_registry_requires_regular_unlinked_file(owner, tmp_path, kind, monkeypatch):
    import os
    other = tmp_path / "outside"
    other.write_text(json.dumps({"machines": {}}))
    if kind == "link":
        owner.symlink_to(other)
    elif kind == "dangling":
        owner.symlink_to(tmp_path / "missing")
    elif kind == "fifo":
        os.mkfifo(owner)
        # A writer prevents the faulty reader from hanging; its bytes are a valid registry.
        fd = os.open(owner, os.O_RDWR | os.O_NONBLOCK)
        os.write(fd, b'{"machines": {}}')
        monkeypatch.setattr(Path, "read_text", lambda *a, **k: '{"machines": {}}')
    elif kind == "hardlink":
        os.link(other, owner)
    else:
        owner.mkdir()
    try:
        with pytest.raises(winlab.WinlabError):
            winlab.load()
    finally:
        if kind == "fifo":
            os.close(fd)
    assert other.read_bytes() == b'{"machines": {}}'


def test_wl_review_registry_duplicate_keys_are_refused(owner):
    owner.write_text('{"machines": {}, "machines": {}}')
    with pytest.raises(winlab.WinlabError):
        winlab.load()


def test_wl_review_registry_read_is_bounded(owner):
    owner.write_text('{"machines": {}}' + ' ' * (2 * 1024 * 1024))
    with pytest.raises(winlab.WinlabError):
        winlab.load()


def test_wl_review_future_window_is_not_authorized(registered):
    winlab.open_(MACHINE, 30, PROJECT, now=minutes(10))
    with pytest.raises(winlab.WinlabError):
        winlab.allowed(MACHINE, PROJECT, now=T0)
    assert winlab.windows(now=T0) == []


@pytest.mark.parametrize("operation", ["add", "open", "close", "remove", "allowed"])
def test_wl_review_invalid_values_never_reach_errors(owner, operation):
    value = "pass" + "word=" + "invented" + "-private\n"
    calls = {
        "add": lambda: winlab.add(value, 2201, FINGERPRINT, False),
        "open": lambda: winlab.open_(value, 30, PROJECT),
        "close": lambda: winlab.close(value),
        "remove": lambda: winlab.remove(value),
        "allowed": lambda: winlab.allowed(value, PROJECT),
    }
    with pytest.raises(winlab.WinlabError) as err:
        calls[operation]()
    assert value not in str(err.value) and "\n" not in str(err.value)


def test_wl_review_registry_error_hides_path(tmp_path):
    path = tmp_path / ("pass" + "word=" + "private-value")
    path.write_bytes(b"\xff")
    with pytest.raises(winlab.WinlabError) as err:
        winlab.load(path)
    assert path.name not in str(err.value)


def test_wl_review_concurrent_opens_have_one_winner(registered, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    import threading
    barrier = threading.Barrier(2)
    original = winlab.load

    def overlapping(path=None):
        result = original(path)
        try:
            barrier.wait(timeout=0.3)
        except threading.BrokenBarrierError:
            pass
        return result

    monkeypatch.setattr(winlab, "load", overlapping)

    def attempt(code):
        try:
            winlab.open_(MACHINE, 30, code, now=T0)
            return True
        except winlab.WinlabError:
            return False

    with ThreadPoolExecutor(2) as pool:
        assert sorted(pool.map(attempt, [PROJECT, OTHER], timeout=60)) == [False, True]


@pytest.mark.wallclock
def test_wl_review_a_held_registry_lock_refuses_within_the_wait(registered, monkeypatch):
    import fcntl
    import os
    import time
    monkeypatch.setattr(winlab, "LOCK_WAIT", 0.2)
    holder = os.open(registered.parent, os.O_RDONLY | os.O_DIRECTORY)
    try:
        fcntl.flock(holder, fcntl.LOCK_EX)
        start = time.monotonic()
        with pytest.raises(winlab.WinlabError, match="holds the lock"):
            winlab.open_(MACHINE, 30, PROJECT, now=T0)
        assert time.monotonic() - start < 10
    finally:
        os.close(holder)
    winlab.open_(MACHINE, 30, PROJECT, now=T0)


def test_wl_review_failed_save_keeps_registry_and_removes_temp(registered, monkeypatch, capsys):
    import os
    before = registered.read_bytes()
    value = "pass" + "word=" + "private-value"

    def fail(*args, **kwargs):
        raise OSError(value)

    monkeypatch.setattr(os, "replace", fail)
    assert cli.main(["winlab", "open", MACHINE, "--minutes", "30", "--project", PROJECT]) == 1
    out = capsys.readouterr()
    assert value not in out.out + out.err
    assert registered.read_bytes() == before
    assert sorted(p.name for p in registered.parent.iterdir()) == ["keys.conf", winlab.FILE_NAME]


def test_wl_review_unreadable_registry_fails_closed(registered, monkeypatch):
    import os
    value = "pass" + "word=" + "private-value"
    real_open, real_read = os.open, Path.read_text

    def denied(path, *args, **kwargs):
        if path == winlab.FILE_NAME or path == registered:
            raise PermissionError(value)
        return real_open(path, *args, **kwargs)

    def denied_text(path, *args, **kwargs):
        if path == registered:
            raise PermissionError(value)
        return real_read(path, *args, **kwargs)

    monkeypatch.setattr(os, "open", denied)
    monkeypatch.setattr(Path, "read_text", denied_text)
    with pytest.raises(winlab.WinlabError, match="cannot be read"):
        winlab.load()
    with pytest.raises(winlab.WinlabError) as err:
        winlab.allowed(MACHINE, PROJECT)
    assert value not in str(err.value)
    assert winlab.windows() == []
