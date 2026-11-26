"""B-101: `awb dev reopen HANDLE --error TEXT` takes a closed ask back to open and the live note names the command."""
from __future__ import annotations

from pathlib import Path

from awb import cli, devask, events, projects
from tests import fixtures as fx
from tests.test_close_the_loops import asks  # noqa: F401

RELEASE = "c" * 40


def _ask_and_close(world, command="awb doc find --kind x") -> Path:
    home, pr = world
    assert cli.main(["dev", "ask", "missing docs filter", "--command", command, "--error", "unsupported filter"]) == 0
    path = sorted((home.shared / "notices/dev").glob("*.md"))[-1]
    lines, ok = devask.released(home, [path.stem], RELEASE)
    assert ok and len(lines) == 1 and "live in release ccccccc" in lines[0]
    return path


def test_a_reopen_after_the_release_leaves_the_ask_open_with_the_count(asks, capsys):
    home, pr = asks
    path = _ask_and_close(asks)
    assert devask.open_asks(home) == []
    capsys.readouterr()
    assert cli.main(["dev", "reopen", path.stem, "--error", "still the same filter error"]) == 0
    assert "reopened" in capsys.readouterr().out
    data = devask.read(path)
    assert data["state"] == "open" and data["reopened"] == 1 and data["reopened_release"] == RELEASE
    assert data["reopened_at"] and "answer" not in data and "release" not in data
    assert [(p.name, d["reopened"]) for p, d in devask.open_asks(home)] == [(path.name, 1)]
    body = path.read_text()
    assert "## Reopened 1" in body and "still the same filter error" in body and "ccccccc" in body
    assert any(e.get("id") == path.stem and e.get("ev") == "start" for e in events.read(home.shared / "build"))
    capsys.readouterr()
    assert cli.main(["dev", "status"]) == 0
    out = capsys.readouterr().out
    assert "reopened 1" in next(x for x in out.splitlines() if x.startswith(path.stem))
    fx.assert_no_fixture_name(out + body)
    # a second failure counts again
    devask.released(home, [path.stem], RELEASE)
    assert cli.main(["dev", "reopen", path.stem, "--error", "and again"]) == 0
    assert devask.read(path)["reopened"] == 2 and "## Reopened 2" in path.read_text()


def test_a_reopen_from_another_project_is_refused(asks, monkeypatch, capsys):
    home, pr = asks
    path = _ask_and_close(asks)
    other = projects.spawn(home, "lab", "another goal", None, home.register)
    monkeypatch.chdir(other.path)
    capsys.readouterr()
    assert cli.main(["dev", "reopen", path.stem, "--error", "not mine"]) == 2
    err = capsys.readouterr().err
    assert len(err.strip().splitlines()) == 1 and "another project" in err
    assert devask.read(path)["state"] == "closed" and "reopened" not in devask.read(path)


def test_a_reopen_of_an_open_ask_is_refused(asks, capsys):
    home, _ = asks
    assert cli.main(["dev", "ask", "still open"]) == 0
    path = sorted((home.shared / "notices/dev").glob("*.md"))[-1]
    capsys.readouterr()
    assert cli.main(["dev", "reopen", path.stem, "--error", "nothing"]) == 2
    err = capsys.readouterr().err
    assert len(err.strip().splitlines()) == 1 and "open" in err
    assert "reopened" not in devask.read(path)


def test_the_live_note_names_the_recorded_command(asks):
    _, pr = asks
    _ask_and_close(asks)
    note = sorted((Path(pr.path) / "notes/from-dev").glob("*.md"))[-1].read_text()
    assert "Run the command you recorded again: awb doc find --kind x" in note


def test_the_live_note_has_no_command_line_without_a_recorded_command(asks):
    home, pr = asks
    assert cli.main(["dev", "ask", "no command here"]) == 0
    path = sorted((home.shared / "notices/dev").glob("*.md"))[-1]
    devask.released(home, [path.stem], RELEASE)
    note = sorted((Path(pr.path) / "notes/from-dev").glob("*.md"))[-1].read_text()
    assert "Run the command" not in note
