"""PD4: a project can be parked (waits on the customer or rests). The board counts it apart, leaves its OPEN.md
items out, and an active project's items under a closed or done heading are out of its counts. Synthetic folders."""
from __future__ import annotations

from datetime import date
from pathlib import Path
from unittest.mock import patch

import pytest

from awb import cli, projects
from awb.tcp import board as board_file
from awb.tcp.web import projects_api as api
from tests import fixtures
from tests.test_hooks import context_of, missing_module, run_hook

A, B, C = "tcp-q7m4", "tcp-m3fx", "tcp-k2wd"

OPEN_WITH_CLOSED = ("# Open items\n\n- first open question\n- second open question\n\n## Closed 2026-10-01\n\n"
                    "- settled one\n- settled two\n- settled three\n\n### Details\n\n- settled four\n\n"
                    "## Next steps\n\n- a new step\n")


def plant(home, code, state="active", opened="# Open items\n\n- one\n- two\n", customer=projects.NO_CUSTOMER):
    folder = home.projects_root / code
    folder.mkdir(parents=True)
    (folder / "SCOPE.md").write_text("# Scope of %s\n\n- code: %s\n- goal: a test goal\n" % (code, code),
                                     encoding="utf-8")
    (folder / "STATE.md").write_text("# State of %s\n\nStatus: nothing for the owner.\nNext: wait.\n" % code,
                                     encoding="utf-8")
    (folder / "OPEN.md").write_text(opened, encoding="utf-8")
    (folder / "RESOURCES.md").write_text("# Resources\n", encoding="utf-8")
    return projects.Project(code, "query", customer, "tcp", str(folder), projects.memory_key(folder), state,
                            "2026-10-01")


@pytest.fixture
def three(home):
    rows = [plant(home, A, customer=fixtures.CUSTOMER_CODE, opened="# Open items\n\n" + "- item\n" * 54),
            plant(home, B), plant(home, C, opened=OPEN_WITH_CLOSED)]
    projects._save(home, rows)
    return rows


def board(home):
    with patch.object(api, "checked", side_effect=lambda text, paths: (text, False)):
        return api.board(home)


def states(home):
    return {r.code: r.state for r in projects.load(home)}


def test_park_and_unpark_change_the_register_and_keep_everything_else(home, three):
    row, before = projects.park(home, A, "waits on the customer", today=date(2026, 10, 20))
    assert (row.state, before) == ("parked", "active")
    assert states(home) == {A: "parked", B: "active", C: "active"}
    assert projects.park_info(home, A) == ("2026-10-20", "waits on the customer")
    assert Path(row.path).is_dir() and row.customer == fixtures.CUSTOMER_CODE and row.kind == "query"
    again, before = projects.park(home, A)
    assert before == "parked" and projects.park_info(home, A) == ("2026-10-20", "waits on the customer")
    back, before = projects.unpark(home, A)
    assert (back.state, before) == ("active", "parked") and states(home)[A] == "active"
    assert projects.park_info(home, A) is None
    assert projects.unpark(home, A)[1] == "active"


def test_a_closed_or_unknown_project_is_not_parked_and_the_reason_is_checked(home, three):
    projects.close(home, B, force=True)
    with pytest.raises(projects.ProjectError, match="closed"):
        projects.park(home, B)
    with pytest.raises(projects.ProjectError, match="closed"):
        projects.unpark(home, B)
    with pytest.raises(projects.ProjectError, match="not registered"):
        projects.park(home, "tcp-zzzz")
    for bad in ("x" * 81, "a\tb\u0000", "https://x.example/y?z=1"):
        with pytest.raises(projects.ProjectError):
            projects.park(home, A, bad)
    assert states(home)[A] == "active"


def test_the_board_counts_a_parked_project_apart_and_leaves_its_items_out(home, three):
    before = board(home)
    assert len(before["projects"]) == 3
    assert before["parked"] == [] and {p["code"]: p["open"]["total"] for p in before["projects"]}[A] == 54
    projects.park(home, A, "waits on the customer")
    data = board(home)
    assert sorted(p["code"] for p in data["projects"]) == sorted([B, C]) and data["summary"]["projects"] == 2
    assert data["parked"] == [{"code": A, "kind": "query", "customer": fixtures.CUSTOMER_CODE,
                               "reason": "waits on the customer", "since": data["parked"][0]["since"],
                               "board_redacted": False}]
    md = board_file.markdown(data)
    assert "2 active, 1 parked project(s)" in md
    assert "## Parked" in md and "waits on the customer" in md
    assert md.index("## Parked") > md.index("## %s" % B)
    assert "54" not in md and "## %s" % A not in md
    page = board_file.page(data)
    assert "2 active, 1 parked project(s)" in page and "<h2>Parked</h2>" in page
    projects.unpark(home, A)
    assert board(home)["parked"] == [] and "## Parked" not in board_file.markdown(board(home))


def test_the_board_header_without_parked_projects_says_zero(home, three):
    assert "3 active, 0 parked project(s)" in board_file.markdown(board(home))


def test_items_under_a_closed_heading_are_out_of_an_active_projects_counts(home, three):
    data = board(home)
    c = next(p for p in data["projects"] if p["code"] == C)
    assert c["open"]["total"] == 3 and c["open"]["items"] == ["first open question", "second open question",
                                                               "a new step"]
    assert c["open_items"] == 3
    assert projects.open_items(Path(three[2].path)) == 3
    assert [l for l in projects.open_item_lines(Path(three[2].path))] == [
        "open: first open question", "open: second open question", "open: a new step"]


@pytest.mark.parametrize("heading", ["Closed", "DONE", "Erledigt", "abgeschlossen", "What is done so far",
                                     "Items closed in week 42"])
def test_the_closed_heading_words_in_any_case(heading):
    text = "- keep\n## %s\n- gone\n## Next\n- back\n" % heading
    assert [l[2:] for l in projects.open_bullets(text)] == ["keep", "back"]


def test_a_closed_section_ends_at_a_heading_of_the_same_or_a_higher_level():
    text = "# Open\n- a\n## Done\n- b\n### Sub\n- c\n## Todo\n- d\n#### Deep done\n- e\n# Last\n- f\n"
    assert [l[2:] for l in projects.open_bullets(text)] == ["a", "d", "f"]


def test_close_still_works_on_a_parked_project(home, three, capsys):
    projects.park(home, B, "rests")
    assert projects.close(home, B, force=True).state == "closed"
    assert states(home)[B] == "closed" and projects.park_info(home, B) is None
    projects.park(home, A, "waits")
    with pytest.raises(projects.ProjectError, match="open item"):
        projects.close(home, A)
    assert projects.close(home, A, force=True).state == "closed"
    assert (home.projects_root / A).is_dir()


def test_the_commands_park_and_unpark(home, three, capsys):
    assert cli.main(["projects", "park", A, "--reason", "waits on the customer"]) == 0
    assert "parked %s" % A in capsys.readouterr().out and states(home)[A] == "parked"
    assert cli.main(["projects", "list"]) == 0
    assert "parked" in capsys.readouterr().out
    assert cli.main(["projects", "park", "tcp-zzzz"]) == cli.EXIT_ERROR
    assert "not registered" in capsys.readouterr().err
    assert cli.main(["projects", "unpark", A]) == 0
    assert "unparked %s" % A in capsys.readouterr().out and states(home)[A] == "active"
    assert cli.main(["projects", "unpark", A]) == 0
    assert "active already" in capsys.readouterr().out
    assert cli.main(["close", B, "--force"]) == 0


def test_material_and_rules_still_reach_a_parked_project(home, three):
    from awb import rulesync

    projects.park(home, A)
    assert A in [code for code, _ in rulesync.sync(home)]
    assert A in [code for code, _ in rulesync.sync(home, [A])]
    projects.close(home, B, force=True)
    assert B not in [code for code, _ in rulesync.sync(home)]


def test_the_session_start_line_says_the_project_is_parked(home, three, monkeypatch, capsys):
    missing_module(monkeypatch, "kb")
    missing_module(monkeypatch, "career")
    root = Path(three[0].path)
    _, out, _ = run_hook("session-start", {"cwd": str(root)}, monkeypatch, capsys)
    assert "is parked" not in context_of(out, "SessionStart")["hookSpecificOutput"]["additionalContext"]
    projects.park(home, A, "waits on the customer")
    _, out, _ = run_hook("session-start", {"cwd": str(root)}, monkeypatch, capsys)
    text = context_of(out, "SessionStart")["hookSpecificOutput"]["additionalContext"]
    assert "This project is parked since" in text and "(waits on the customer)" in text
    assert "awb projects unpark %s" % A in text
