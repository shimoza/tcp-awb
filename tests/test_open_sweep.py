"""UX6: dated owner items, evidence, closure and the daily stop reminder."""
from datetime import date, timedelta
from pathlib import Path
import json
import subprocess

import pytest

from awb import cli, hooks, ledger
from tests import fixtures


def git(root, *args):
    return subprocess.run(["git", "-C", str(root), *args], check=True, capture_output=True,
                          text=True).stdout.strip()


@pytest.fixture
def project(home, monkeypatch):
    root = home.projects_root / "tcp-q7m4"
    root.mkdir(parents=True)
    (root / "SCOPE.md").write_text("# scope\n")
    git(root, "init", "-q")
    git(root, "config", "user.name", fixtures.PERSON_CODE)
    git(root, "config", "user.email", "fixture@example.org")
    (root / "OPEN.md").write_text("# open\n- owner: legacy. Default: keep ecs-0 stopped.\n")
    git(root, "add", ".")
    git(root, "commit", "-qm", "initial")
    monkeypatch.chdir(root)
    return root


def items(root, count):
    day = (date.today() - timedelta(days=2)).isoformat()
    text = "# open\n" + "".join(
        "- %s owner: decide ecs-%d. Default: keep ecs-%d stopped.\n" % (day, n, n)
        for n in range(1, count + 1))
    (root / "OPEN.md").write_text(text)
    return text


def test_sweep_lists_25_ages_and_six_proven_defaults(project, capsys):
    items(project, 25)
    for n in range(1, 4):
        git(project, "commit", "--allow-empty", "-qm", "Default taken: keep ecs-%d stopped." % n)
    (project / "RESOURCES.md").write_text("".join(
        "| ecs-%d | deleted | Default taken: keep ecs-%d stopped. |\n" % (n, n)
        for n in range(4, 7)))
    assert cli.main(["open", "sweep"]) == 0
    rows = [r for r in capsys.readouterr().out.splitlines() if r.startswith("| ")][1:]
    assert len(rows) == 25
    for n, row in enumerate(rows, 1):
        assert row.startswith("| %d | 2d | decide ecs-%d. | keep ecs-%d stopped. |" % (n, n, n))
    assert sum("taken" in r for r in rows) == 6
    assert "commit " in rows[0] and "RESOURCES.md:3" in rows[5]


def test_close_three_ticks_and_writes_exactly_one_ledger_line(project, home, capsys):
    original = items(project, 25)
    assert cli.main(["open", "close", "1", "2", "3", "--default"]) == 0
    changed = (project / "OPEN.md").read_text()
    assert changed.count("- [x] ") == 3
    assert all(line.replace("- ", "- [x] ", 1) in changed for line in original.splitlines()[1:4])
    from awb import projects
    assert projects.open_items(project) == 22
    records = [json.loads(line) for p in home.ledger.glob("*.jsonl") for line in p.read_text().splitlines()]
    assert len(records) == 1 and records[0]["project"] == project.name
    assert "1, 2, 3" in records[0]["done"] and "default" in records[0]["done"]
    capsys.readouterr()
    assert cli.main(["open", "sweep"]) == 0
    assert len([r for r in capsys.readouterr().out.splitlines() if r.startswith("| ")]) == 23


def test_gate_requires_date_only_for_new_items_in_index_and_files(project, capsys):
    path = project / "OPEN.md"
    old = path.read_text()
    path.write_text(old + "- owner: decide ecs-1. Default: keep ecs-1 stopped.\n")
    git(project, "add", "OPEN.md")
    # A dated working copy must not conceal the undated staged version.
    path.write_text(old + "- 2026-11-11 owner: decide ecs-1. Default: keep ecs-1 stopped.\n")
    assert cli.main(["gate", "--staged", "--no-register"]) == 1
    assert "owner-date  OPEN.md:3" in capsys.readouterr().out
    git(project, "add", "OPEN.md")
    assert cli.main(["gate", "--staged", "--no-register"]) == 0
    path.write_text(old + "- owner: decide ecs-2. Default: keep ecs-2 stopped.\n")
    assert cli.main(["gate", "--no-register", "OPEN.md"]) == 1


def test_gate_does_not_grandfather_duplicate_or_invalid_date(project, capsys):
    path = project / "OPEN.md"
    old = path.read_text()
    path.write_text(old + old.splitlines()[1] + "\n- 2026-02-30 owner: decide ecs-2.\n")
    git(project, "add", "OPEN.md")
    assert cli.main(["gate", "--staged", "--no-register"]) == 1
    out = capsys.readouterr().out
    assert "owner-date  OPEN.md:3" in out and "owner-date  OPEN.md:4" in out


def test_stop_reminder_at_21_once_per_day_and_project(project, home, monkeypatch, capsys):
    from awb import opensweep
    monkeypatch.setattr(hooks, "draft_ledger", lambda *a: False)
    monkeypatch.setattr(hooks, "draft_gate", lambda *a: [])
    monkeypatch.setattr(hooks, "_quietly", lambda *a: None)
    from awb import harvest, projects, status
    monkeypatch.setattr(harvest, "due", lambda *a: None)
    monkeypatch.setattr(projects, "mail_owner_commands", lambda *a: None)
    monkeypatch.setattr(status, "reminder", lambda *a: None)
    monkeypatch.setattr(opensweep, "today", lambda: date(2026, 11, 13))
    data = {"cwd": str(project)}
    items(project, 20)
    assert hooks.hook_stop(data) == 0
    assert capsys.readouterr().err == ""
    items(project, 21)
    assert hooks.hook_stop(data) == 2
    line = capsys.readouterr().err
    assert len(line.splitlines()) == 1 and "21" in line and "awb open sweep" in line
    assert hooks.hook_stop(data) == 0
    assert capsys.readouterr().err == ""
    monkeypatch.setattr(opensweep, "today", lambda: date(2026, 11, 14))
    assert hooks.hook_stop(data) == 2
    other = home.projects_root / "tcp-kx2a"
    other.mkdir()
    (other / "SCOPE.md").write_text("# scope\n")
    items(other, 21)
    assert hooks.hook_stop({"cwd": str(other)}) == 2


def test_legacy_sections_checked_items_and_unproven_defaults(project, capsys):
    (project / "OPEN.md").write_text(
        "## owner\n- legacy. Default: this holds.\n- [x] settled. Default: this holds.\n"
        "## done\n- owner: settled too.\n## work\n- session task\n"
        "- 2026-11-11 owner: decide ecs-8. Default: keep ecs-8 stopped.\n")
    git(project, "commit", "--allow-empty", "-qm", "Discuss default: keep ecs-8 stopped.")
    (project / "RESOURCES.md").write_text("| ecs-8 | planned | keep ecs-8 stopped. |\n")
    assert cli.main(["open", "sweep"]) == 0
    out = capsys.readouterr().out
    assert "unknown" in out and "taken" not in out
    assert len([r for r in out.splitlines() if r.startswith("| ")]) == 3


def test_sweep_withholds_names_secrets_and_unsafe_evidence(project, capsys):
    from awb import opensweep
    items(project, 1)
    text = (project / "OPEN.md").read_text()
    secret = "gh" + "p_" + "Zx8Kq3Lw7Rt2Vn5Bm9Yc4Hd6Jf1Gs0Pa3Ue7Wi"
    (project / "OPEN.md").write_text(text +
        "- owner: %s. Default: keep ecs-2 stopped.\n" % fixtures.PERSON_FORMS[0] +
        "- owner: decide ecs-3. Default: %s.\n" % secret)
    (project / "RESOURCES.md").write_text(
        "| ecs-1 | %s | Default taken: keep ecs-1 stopped. |\n" % fixtures.CUSTOMER_FORMS[0])
    assert cli.main(["open", "sweep"]) == 0
    out = capsys.readouterr().out
    fixtures.assert_no_fixture_name(out)
    assert secret not in out and out.count("[withheld]") == 4 and "RESOURCES.md:1" in out
    assert len(opensweep.parse((project / "OPEN.md").read_text())) == 3
    # A handle-shaped value still needs the name check before it leaves the file.
    (project / "RESOURCES.md").write_text(
        "| %s-1 | deleted | Default taken: keep ecs-1 stopped. |\n" % fixtures.PERSON_FORMS[1].lower())
    assert cli.main(["open", "sweep"]) == 0
    out = capsys.readouterr().out
    fixtures.assert_no_fixture_name(out)
    assert "taken" not in out


def test_sweep_refuses_when_the_name_check_is_unavailable(project, home, monkeypatch, capsys):
    items(project, 1)
    home.register.unlink()
    assert cli.main(["open", "sweep"]) == 2
    output = capsys.readouterr()
    assert output.out == "" and "no register" in output.err
    from awb import check
    def unavailable(*args, **kwargs):
        raise check.CheckUnavailable("name check unavailable")
    monkeypatch.setattr(check, "register_source", unavailable)
    assert cli.main(["open", "sweep"]) == 2
    output = capsys.readouterr()
    assert output.out == "" and "unavailable" in output.err


def test_close_refuses_bad_selection_missing_default_and_ledger_failure(project, monkeypatch):
    from awb import opensweep
    original = items(project, 3)
    for args in (["1", "4", "--default"], ["0", "--default"], ["1"]):
        assert cli.main(["open", "close", *args]) == 2
        assert (project / "OPEN.md").read_text() == original
    def refuse(*a, **k):
        raise ledger.Refused("fixture refusal")
    monkeypatch.setattr(ledger, "add", refuse)
    assert cli.main(["open", "close", "1", "--default"]) == 2
    assert (project / "OPEN.md").read_text() == original
    (project / "OPEN.md").write_text("- owner: no default\n")
    with pytest.raises(opensweep.OpenError):
        opensweep.close(project, [1])


def test_work_rules_require_date_closure_and_sweep():
    rules = (Path(__file__).resolve().parents[1] / "seal/work-claude/CLAUDE.md").read_text()
    assert "- YYYY-MM-DD owner: TEXT. Default: X." in rules
    assert "after a step that carries a default out, close its item" in rules
    assert "more than 20 owner items" in rules and "awb open sweep" in rules
