"""awb egress allow|revoke and the grants in awb egress load (awb/egressgrant.py, owner decision 2026-11-20).

One host for the work user, one project code, a last day at most 30 days ahead and a reason; the owner alone grants,
the root load reads only a private file of the owner and drops an expired grant. Every path is a tmp path, nft is a
stand-in (tests/test_egress.py), the ledger is a recorder.
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path

import pytest

from awb import egress, egressgrant, ledger
from tests.test_egress import EGRESS, ME, FakeNft, Clock, _conf, _resolve

DAY = 86400.0
NOW = datetime(2026, 11, 20, 12, 0, tzinfo=timezone.utc).timestamp()


class Ledger:
    def __init__(self, refuse: bool = False):
        self.entries: list[tuple] = []
        self.refuse = refuse

    def __call__(self, kind, done, *, project=None, outcome="", tags=()):
        if self.refuse:
            raise ledger.Refused("name")
        self.entries.append((kind, done, project, outcome))


@pytest.fixture
def owner(tmp_path):
    folder = tmp_path / "home" / ".config" / "awb"
    book = Ledger()
    clock = Clock(NOW)
    o = egressgrant.Owner(folder=folder, allow_md=EGRESS / "ALLOW.md",
                          conf_text=lambda: "owner = %s\nwork_user = awb-test-work\nbuild_user = awb-test-build\n" % ME,
                          user=lambda: ME, geteuid=lambda: 1000, cwd=lambda: tmp_path, ledger_add=book, now=clock)
    return o, book, clock


def _allow(o, host="docs.example.org", project="tcp-q7m4", until="2026-11-27", why="vendor docs for the PoC"):
    return egressgrant.allow(o, host, project, until, why)


def test_a_grant_is_written_logged_and_listed(owner):
    o, book, _ = owner
    said = _allow(o)
    assert "granted docs.example.org" in said and "sudo awb egress load" in said and "knows no project" in said
    path = o.dir() / egressgrant.GRANTS_FILE
    assert path.read_text() == "docs.example.org\ttcp-q7m4\t2026-11-27\t2026-11-20\tvendor docs for the PoC\n"
    assert path.stat().st_mode & 0o777 == 0o600
    log = (o.dir() / egressgrant.LOG_FILE).read_text().split("\t")
    assert log[1:5] == ["allow", "docs.example.org", "tcp-q7m4", "2026-11-27"]
    assert book.entries == [("tooling", "egress grant: one host for the work user until 2026-11-27 "
                                        "(awb egress allow --list)", "tcp-q7m4", "vendor docs for the PoC")]
    assert "docs.example.org" not in book.entries[0][1]          # the ledger never carries the host
    assert egressgrant.listing(o) == ["docs.example.org\ttcp-q7m4\tuntil 2026-11-27\tactive\tvendor docs for the PoC"]


@pytest.mark.parametrize("args, message", [
    (dict(host="10.0.0.1"), "one host name"),
    (dict(host="*.example.org"), "one host name"),
    (dict(project="CUST-Q7M4"), "project code"),
    (dict(until="27.11.2026"), "YYYY-MM-DD"),
    (dict(until="2026-11-19"), "in the past"),
    (dict(until="2026-12-21"), "at most 30 days"),
    (dict(why="  "), "needs a reason"),
    (dict(host="calculator.otc-service.com"), "ALLOW.md already"),
    (dict(host="github.com"), "ALLOW.md already"),
])
def test_a_bad_grant_is_refused_and_nothing_is_written(owner, args, message):
    o, book, _ = owner
    with pytest.raises(egressgrant.GrantError, match=message):
        _allow(o, **args)
    assert not (o.dir() / egressgrant.GRANTS_FILE).exists() and book.entries == []


def test_thirty_days_is_the_limit(owner):
    o, _, _ = owner
    _allow(o, until="2026-12-20")


@pytest.mark.parametrize("change, message", [
    (dict(geteuid=lambda: 0), "without sudo"),
    (dict(user=lambda: "awb-test-work"), "work user"),
    (dict(user=lambda: "awb-test-build"), "build user"),
    (dict(user=lambda: "someone-else"), "only the owner"),
])
def test_only_the_owner_grants(owner, change, message):
    o, _, _ = owner
    for k, v in change.items():
        setattr(o, k, v)
    with pytest.raises(egressgrant.GrantError, match=message):
        _allow(o)
    with pytest.raises(egressgrant.GrantError, match=message):
        egressgrant.listing(o)


def test_never_from_a_project_folder(owner, tmp_path):
    o, _, _ = owner
    project = tmp_path / "tcp-q7m4"
    project.mkdir()
    (project / "SCOPE.md").write_text("- code: tcp-q7m4\n", encoding="utf-8")
    o.cwd = lambda: project
    with pytest.raises(egressgrant.GrantError, match="project folder"):
        _allow(o)


def test_a_reason_with_a_name_is_refused_with_exit_one(owner, capsys):
    o, _, _ = owner
    o.ledger_add = Ledger(refuse=True)
    assert egress.main(["allow", "docs.example.org", "--project", "tcp-q7m4", "--until", "2026-11-27",
                        "--why", "for the PoC"], owner=o) == 1
    assert "name check" in capsys.readouterr().err
    assert not (o.dir() / egressgrant.GRANTS_FILE).exists()


def test_the_command_lines(owner, capsys):
    o, _, _ = owner
    assert egress.main(["allow", "docs.example.org", "--project", "tcp-q7m4", "--until", "2026-11-27",
                        "--why", "vendor docs"], owner=o) == 0
    assert egress.main(["allow", "--list"], owner=o) == 0
    assert "docs.example.org\ttcp-q7m4\tuntil 2026-11-27\tactive" in capsys.readouterr().out
    assert egress.main(["allow", "docs.example.org"], owner=o) == 2
    assert egress.main(["allow", "--list", "--project", "tcp-q7m4"], owner=o) == 2
    assert egress.main(["revoke", "docs.example.org", "--project", "tcp-q7m4"], owner=o) == 0
    assert "revoked docs.example.org" in capsys.readouterr().out
    assert egress.main(["revoke", "docs.example.org", "--project", "tcp-q7m4"], owner=o) == 2
    assert egressgrant.listing(o) == ["egress: no grant"]
    actions = [line.split("\t")[1] for line in (o.dir() / egressgrant.LOG_FILE).read_text().splitlines()]
    assert actions == ["allow", "revoke"]


def test_a_second_grant_of_the_same_host_and_project_replaces_the_first(owner):
    o, _, _ = owner
    _allow(o, until="2026-11-22")
    _allow(o, until="2026-11-30")
    _allow(o, project="tcp-k2ab")
    grants, bad = egressgrant.parse((o.dir() / egressgrant.GRANTS_FILE).read_text())
    assert bad == 0 and [(g.project, g.until) for g in grants] == [("tcp-q7m4", "2026-11-30"),
                                                                  ("tcp-k2ab", "2026-11-27")]


# --------------------------------------------------------------------------- the root load


OWN = {"docs.example.org": "198.51.100.7", "long.example.org": "198.51.100.8"}


def _addr(name: str) -> str:
    return OWN[name]


def _resolve_own(name: str) -> list[str]:
    return [OWN[name]] if name in OWN else _resolve(name)


@pytest.fixture
def world(tmp_path, owner):
    o, _, _ = owner
    conf = _conf(tmp_path / "paths.conf")
    nft = FakeNft()
    clock = Clock(NOW)
    host = egress.Host(conf=conf, state=tmp_path / "state", source=EGRESS, run=nft, now=clock, resolve=_resolve_own,
                       geteuid=lambda: 0, environ={"SUDO_USER": "owner-%s" % ME}, chown_root=False,
                       grants=o.dir() / egressgrant.GRANTS_FILE, grants_uid=os.getuid())
    return host, nft, clock, o


def _allowed(nft: FakeNft) -> str:
    text = nft.refreshes[-1] if nft.refreshes else nft.loaded
    return "\n".join(line for line in text.splitlines() if " allow4 " in line or " allow6 " in line)


def test_an_active_grant_adds_its_host_to_the_allowed_set_and_drops_when_it_expires(world):
    host, nft, clock, o = world
    _allow(o, host="docs.example.org")
    egress.load(host)
    assert _addr("docs.example.org") in _allowed(nft)
    st = json.loads((host.state_dir / "status.json").read_text())
    assert st["grants"] == {"active": 1, "expired": 0, "left_out": 0}
    assert any("1 host(s) granted for a limited time" in line for line in egress.status_lines(host))
    clock.t = datetime(2026, 11, 27, 23, 59, tzinfo=timezone.utc).timestamp()
    egress.load(host)
    assert _addr("docs.example.org") in _allowed(nft)
    clock.t = datetime(2026, 11, 28, 0, 0, 1, tzinfo=timezone.utc).timestamp()
    egress.load(host)
    assert _addr("docs.example.org") not in _allowed(nft)
    st = json.loads((host.state_dir / "status.json").read_text())
    assert st["grants"]["expired"] == 1 and st["grants"]["active"] == 0
    assert "docs.example.org" not in json.loads((host.state_dir / "addresses.json").read_text())


def test_a_revoke_drops_the_host_at_the_next_load(world):
    host, nft, clock, o = world
    _allow(o, host="docs.example.org")
    egress.load(host)
    egressgrant.revoke(o, "docs.example.org", "tcp-q7m4")
    clock.t += 300
    egress.load(host)
    assert _addr("docs.example.org") not in _allowed(nft)


@pytest.mark.parametrize("damage", ["group-writable", "link", "other-uid"])
def test_a_grants_file_that_is_not_private_to_the_owner_adds_nothing(world, tmp_path, damage):
    host, nft, _, o = world
    _allow(o, host="docs.example.org")
    path = o.dir() / egressgrant.GRANTS_FILE
    if damage == "group-writable":
        path.chmod(0o664)
    elif damage == "link":
        real = tmp_path / "elsewhere.tsv"
        real.write_text(path.read_text())
        path.unlink()
        path.symlink_to(real)
    else:
        host.grants_uid = os.getuid() + 1
    egress.load(host)
    assert _addr("docs.example.org") not in _allowed(nft)
    assert json.loads((host.state_dir / "status.json").read_text())["grants"]["refused"] == 1
    assert any("no grant is loaded" in line for line in egress.status_lines(host))


def test_malformed_long_or_listed_lines_are_left_out(world):
    host, nft, _, o = world
    o.dir().mkdir(parents=True)
    path = o.dir() / egressgrant.GRANTS_FILE
    path.write_text("".join(line + "\n" for line in [
        "docs.example.org\ttcp-q7m4\t2026-11-27\t2026-11-20\tok",
        "long.example.org\ttcp-q7m4\t2027-01-30\t2026-11-20\tmore than 30 days",
        "10.0.0.0/8\ttcp-q7m4\t2026-11-27\t2026-11-20\ta network",
        "github.com\ttcp-q7m4\t2026-11-27\t2026-11-20\ta watched host",
        "short.example.org\ttcp-q7m4\t2026-11-27",
    ]))
    path.chmod(0o600)
    egress.load(host)
    allowed = _allowed(nft)
    assert _addr("docs.example.org") in allowed and _addr("long.example.org") not in allowed
    st = json.loads((host.state_dir / "status.json").read_text())
    assert st["grants"] == {"active": 1, "expired": 0, "left_out": 4}


def test_no_grants_file_changes_nothing(world):
    host, nft, _, _ = world
    egress.load(host)
    st = json.loads((host.state_dir / "status.json").read_text())
    assert st["grants"] == {"active": 0, "expired": 0, "left_out": 0}
    assert not any("granted" in line for line in egress.status_lines(host))
