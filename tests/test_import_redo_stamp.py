"""G12 of the attacker review of 2026-11-28: `awb import CODE --redo-list` prints the stamp of each import and
`--redo STAMP` redoes that import, also when another import came in between the list and the redo (the timer
`awb import --auto`); a number counts the list of now and moves. Every name is an invented fixture name."""
from __future__ import annotations

import re
import time

from awb import importcmd, intake
from tests import fixtures as fx
from tests.test_import import _copies, _files, _lines, _no_name, owner_terminal, project  # noqa: F401  (fixtures)

STAMP = re.compile(r"\d{4}-\d{2}-\d{2}-\d{6}")


def _import(home, project, folder, n):
    before = set(_copies(home))
    folder.mkdir()
    _files(folder, n)
    assert importcmd.run(home, project.code, list(folder.iterdir())) == 0
    time.sleep(1.1)          # the private report is named by the second
    return set(_copies(home)) - before


def _listed(project, capsys) -> list[list[str]]:
    capsys.readouterr()
    assert importcmd.main([project.code, "--redo-list"]) == 0
    out = _lines(capsys)
    _no_name(out)
    return [line.split("  ") for line in out.splitlines() if line.strip()]


def test_the_list_shows_a_stamp_for_every_import(home, project, tmp_path, capsys):
    _import(home, project, tmp_path / "a", 2)
    _import(home, project, tmp_path / "b", 1)
    rows = _listed(project, capsys)
    assert [r[0] for r in rows] == ["1", "2"]
    assert all(STAMP.fullmatch(r[1]) for r in rows) and rows[0][1] > rows[1][1]
    assert "1 file(s)" in rows[0] and "2 file(s)" in rows[1]


def test_a_stamp_redoes_the_same_import_after_another_one_came_in(home, project, tmp_path, capsys):
    first = _import(home, project, tmp_path / "a", 2)
    second = _import(home, project, tmp_path / "b", 1)
    rows = _listed(project, capsys)
    stamp = rows[1][1]                                   # number 2 of the list: the first import
    third = _import(home, project, tmp_path / "c", 3)    # the timer between the list and the redo
    assert len(first) == 2 and len(second) == 1 and len(third) == 3
    before = _copies(home)
    intake.keep_phrase(home, fx.PLANTED_CANDIDATE)
    capsys.readouterr()
    assert importcmd.main([project.code, "--redo", stamp]) == 0
    out = _lines(capsys)
    assert "2 file(s) from the vault" in out and "copies written: 2 (replacing the earlier ones)" in out
    after = _copies(home)
    assert all(fx.PLANTED_CANDIDATE in after[f] for f in first)
    assert all(after[f] == before[f] for f in second | third)
    _no_name(out)
    # the redo wrote a report of its own: the stamp still names the first import, its number moved
    now = {r[1]: r[0] for r in _listed(project, capsys)}
    assert now[stamp] not in ("1", "2")


def test_a_stamp_that_no_import_has_is_refused_and_nothing_is_redone(home, project, tmp_path, capsys):
    _import(home, project, tmp_path / "a", 1)
    before = _copies(home)
    capsys.readouterr()
    assert importcmd.main([project.code, "--redo", "2001-01-01-000000"]) != 0
    out = _lines(capsys)
    assert "no import with the stamp 2001-01-01-000000" in out
    assert _copies(home) == before


def test_a_value_that_is_neither_a_number_nor_a_stamp_is_refused(home, project, capsys):
    try:
        code = importcmd.main([project.code, "--redo", "2026-11-27"])
    except SystemExit as stop:
        code = stop.code
    assert code != 0
