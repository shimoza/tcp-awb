"""awb projects open without a code (P4 of F3): the active projects numbered, each with its Next: line and its owner
items of OPEN.md; he types the number and the session opens.

Every name is an invented fixture name; the session start is a stand-in that records its project.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from awb import cli, config, projects
from tests import fixtures as fx

GOAL = "move two app clusters to managed k8s"


@pytest.fixture
def two(home, register_path, monkeypatch):
    monkeypatch.setattr(config, "is_work_user", lambda: False)
    a = projects.spawn(home, "query", GOAL, fx.CUSTOMER_CODE, register_path)
    b = projects.spawn(home, "project", GOAL, None, register_path)
    state = Path(a.path) / "STATE.md"
    state.write_text(state.read_text(encoding="utf-8").replace(
        "Next: The first step of the goal in SCOPE.md.", "Next: Compare the retrieval fees."), encoding="utf-8")
    with open(Path(a.path) / "OPEN.md", "a", encoding="utf-8") as f:
        f.write("- Switch to a project for the live test: awb projects kind %s project (owner)\n" % a.code)
        f.write("\n## Questions\n\n- Which region holds the backups?\n")
    return a, b


def _opened(monkeypatch):
    seen = []
    monkeypatch.setattr(projects, "open_session", lambda p, code, run=None: seen.append(code) or 0)
    return seen


def test_the_list_numbers_the_active_projects_with_next_and_owner_items(home, two):
    a, b = two
    rows = projects.waiting(home)
    assert [r["code"] for r in rows] == [a.code, b.code]
    assert rows[0]["next"] == "Compare the retrieval fees."
    assert rows[0]["owner"] == ["Switch to a project for the live test: awb projects kind %s project (owner)" % a.code]
    assert rows[1]["owner"] == []
    lines = projects.waiting_lines(rows)
    assert lines[0].startswith("1  %s" % a.code) and any(l.strip() == "Next: Compare the retrieval fees."
                                                         for l in lines)
    assert not any("backups" in l for l in lines), "an item that does not wait on the owner is not listed"


def test_he_types_the_number_and_it_opens(home, two, monkeypatch, capsys):
    a, b = two
    seen = _opened(monkeypatch)
    assert projects.pick_session(home, read=lambda: "2\n", tty=True) == 0
    assert seen == [b.code]
    out = capsys.readouterr().out
    assert "1  %s" % a.code in out and "2  %s" % b.code in out
    fx.assert_no_fixture_name(out, "the list of awb projects open")


def test_an_empty_answer_or_a_wrong_number_opens_nothing(home, two, monkeypatch, capsys):
    seen = _opened(monkeypatch)
    assert projects.pick_session(home, read=lambda: "\n", tty=True) == 0
    assert projects.pick_session(home, read=lambda: "7\n", tty=True) == 2
    assert not seen and "no project 7" in capsys.readouterr().err


def test_without_a_terminal_the_list_is_printed_and_nothing_opens(home, two, monkeypatch, capsys):
    seen = _opened(monkeypatch)
    assert cli.main(["projects", "open"]) == 0
    assert not seen and "Next: Compare the retrieval fees." in capsys.readouterr().out


def test_a_next_line_with_a_registered_name_is_withheld(home, two, capsys):
    a, _ = two
    state = Path(a.path) / "STATE.md"
    state.write_text(state.read_text(encoding="utf-8").replace("Compare the retrieval fees.",
                                                               "Call %s." % fx.CUSTOMER_FORMS[0]), encoding="utf-8")
    rows = projects.waiting(home)
    assert fx.CUSTOMER_FORMS[0] not in "\n".join(projects.waiting_lines(rows))


def test_no_active_project_says_so(home, monkeypatch, capsys):
    monkeypatch.setattr(config, "is_work_user", lambda: False)
    assert projects.pick_session(home, read=lambda: "1\n", tty=True) == 0
    assert "no active project" in capsys.readouterr().out
