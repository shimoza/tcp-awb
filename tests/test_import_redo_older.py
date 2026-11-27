"""`awb import CODE --redo-list` and `--redo N`: a project with two imports can redo the older one (tcp-j5sy, the
first import after the finder fix of 2026-11-27). Every name is an invented fixture name."""
from __future__ import annotations

import time

from awb import importcmd, intake
from tests import fixtures as fx
from tests.test_import import _copies, _files, _lines, _no_name, owner_terminal, project  # noqa: F401  (fixtures)


def _two_imports(home, project, tmp_path):
    first = _files(home.inbox, 2)
    assert importcmd.run(home, project.code, []) == 0
    older = set(_copies(home))
    time.sleep(1.1)          # the private report is named by the second
    extra = tmp_path / "later"
    extra.mkdir()
    _files(extra, 1)
    assert importcmd.run(home, project.code, list(extra.iterdir())) == 0
    newer = set(_copies(home)) - older
    assert first and len(older) == 2 and len(newer) == 1
    return older, newer


def test_the_list_names_the_imports_newest_first_with_counts_and_no_name(home, project, tmp_path, capsys):
    _two_imports(home, project, tmp_path)
    capsys.readouterr()
    assert importcmd.main([project.code, "--redo-list"]) == 0
    out = _lines(capsys)
    lines = [l for l in out.splitlines() if l.strip()]
    assert lines[0].startswith("1  ") and "1 file(s)" in lines[0]
    assert lines[1].startswith("2  ") and "2 file(s)" in lines[1] and lines[1].endswith("folder import: no")
    _no_name(out)


def test_redo_2_takes_the_older_import_and_leaves_the_newer_copy(home, project, tmp_path, capsys):
    older, newer = _two_imports(home, project, tmp_path)
    before = _copies(home)
    intake.keep_phrase(home, fx.PLANTED_CANDIDATE)
    capsys.readouterr()
    assert importcmd.main([project.code, "--redo", "2"]) == 0
    out = _lines(capsys)
    assert "2 file(s) from the vault" in out and "copies written: 2 (replacing the earlier ones)" in out
    after = _copies(home)
    assert all(fx.PLANTED_CANDIDATE in after[f] for f in older)
    assert all(after[f] == before[f] for f in newer)
    _no_name(out)


def test_redo_alone_still_takes_the_newest(home, project, tmp_path, capsys):
    _two_imports(home, project, tmp_path)
    capsys.readouterr()
    assert importcmd.main([project.code, "--redo"]) == 0
    assert "1 file(s) from the vault" in _lines(capsys)


def test_a_number_outside_the_list_is_refused(home, project, tmp_path, capsys):
    _two_imports(home, project, tmp_path)
    capsys.readouterr()
    assert importcmd.main([project.code, "--redo", "3"]) != 0
    assert "no import number 3" in _lines(capsys)
    assert importcmd.main([project.code, "--redo", "0"]) != 0
    assert "counts from 1" in _lines(capsys)
