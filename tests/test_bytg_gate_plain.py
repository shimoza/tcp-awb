"""awb gate with no argument (tcp-bytg ask 7, 2026-11-21): the project rules say "awb gate before every commit", but
the plain call only printed its usage. Inside a repository it now checks the staged files, as --staged does, and
says so in one line; outside a repository it still asks for an argument."""
from __future__ import annotations

from awb import gate
from tests.test_gate import PLANTED, git, new_repo


def test_bytg_the_plain_gate_checks_the_staged_files(tmp_path, register_path, capsys):
    repo = new_repo(tmp_path / "repo")
    (repo / "notes.txt").write_text("intro\n" + PLANTED["token"] + "\n", encoding="utf-8")
    assert git(repo, "add", "notes.txt").returncode == 0
    assert gate.main(["--repo", str(repo), "--register", str(register_path)]) == 1
    out, err = capsys.readouterr()
    assert out.splitlines() == ["token  notes.txt:2"] and "checking the staged files" in err
    (repo / "notes.txt").write_text("intro\nnothing here\n", encoding="utf-8")
    assert git(repo, "add", "notes.txt").returncode == 0
    assert gate.main(["--repo", str(repo), "--register", str(register_path)]) == 0


def test_bytg_outside_a_repository_the_plain_gate_asks_for_an_argument(tmp_path, register_path, capsys):
    assert gate.main(["--repo", str(tmp_path), "--register", str(register_path)]) == 2
    assert "give --staged" in capsys.readouterr().err
