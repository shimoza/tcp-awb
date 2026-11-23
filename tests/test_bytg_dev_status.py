"""awb dev status (tcp-bytg ask 9, 2026-11-21): a session saw an answer of development only as a new note and could
not list its open asks. `awb dev status` lists the open asks of the project of the working folder, `--all` the
answered ones too with their note and release; another project's asks never show."""
from __future__ import annotations

from awb import cli, devask
from tests import fixtures as fx
from tests.test_close_the_loops import asks, new_ask  # noqa: F401


def _status(capsys, *options):
    capsys.readouterr()
    assert cli.main(["dev", "status", *options]) == 0
    out = capsys.readouterr().out
    fx.assert_no_fixture_name(out)
    return out


def test_bytg_status_lists_the_open_asks_and_all_the_answered_ones_too(asks, capsys):
    home, pr = asks
    first, second = new_ask(asks), new_ask(asks, "second request")
    out = _status(capsys)
    assert first.stem in out and second.stem in out and "%s: 2 open ask(s)" % pr.code in out
    devask.released(home, [first.stem], "c" * 40)
    out = _status(capsys)
    assert first.stem not in out and second.stem in out and "1 open ask(s)" in out
    out = _status(capsys, "--all")
    line = next(x for x in out.splitlines() if x.startswith(first.stem))
    assert "closed" in line and "notes/from-dev/" in line and "release ccccccc" in line


def test_bytg_status_shows_no_ask_of_another_project(asks, capsys):
    home, pr = asks
    new_ask(asks)
    other = "tcp-zz9z"
    assert devask.project_asks(home, other) == []
    assert len(devask.project_asks(home, pr.code)) == 1
