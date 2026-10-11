"""awb report --period: the period report in three blocks.

Every name here is an invented fixture name. A temporary register, ledger and project register (the `home`
fixture) with four planted projects: one per block and one whose activity lies before the period.
"""
from __future__ import annotations

import os
import subprocess
from datetime import date, datetime
from pathlib import Path

import pytest

from awb import cli, config, ledger, period, projects
from tests import fixtures

START, END = "2026-04-01", "2026-09-30"
ACTIVE, FINISHED, QUERY, BEFORE = "tcp-a2a2", "tcp-b2b2", "tcp-c2c2", "tcp-d2d2"
NAME = fixtures.CUSTOMER_FORMS[0]


def _git(folder: Path, *args: str, day: str | None = None) -> None:
    env = dict(os.environ)
    if day:
        env["GIT_AUTHOR_DATE"] = env["GIT_COMMITTER_DATE"] = "%sT10:00:00" % day
    subprocess.run(["git", "-c", "user.name=awb", "-c", "user.email=awb@localhost",
                    "-c", "commit.gpgsign=false", "-c", "core.hooksPath=/dev/null", "-C", str(folder), *args],
                   check=True, capture_output=True, env=env)


def _touch(path: Path, day: str) -> None:
    t = datetime.fromisoformat(day + "T12:00:00").timestamp()
    os.utime(path, (t, t))


def plant(home, code: str, kind: str, customer: str, state: str, goal: str, status: str, commit_days: list[str],
          state_day: str, resources: int = 0, deliverables: tuple[str, ...] = ()) -> projects.Project:
    folder = home.projects_root / code
    (folder / "deliverables").mkdir(parents=True)
    (folder / "SCOPE.md").write_text("# Scope of %s\n\n- code: %s\n- goal: %s\n" % (code, code, goal), encoding="utf-8")
    (folder / "STATE.md").write_text("# State of %s\n\nStatus: %s\nNext: more.\n" % (code, status), encoding="utf-8")
    rows = "".join("| ecs-%d | 0000-%d | ecs | eu-de | low | - | live | - |\n" % (n, n) for n in range(resources))
    (folder / "RESOURCES.md").write_text(
        "# Resources of %s\n\n| %s |\n|%s\n%s" % (code, " | ".join(projects.RESOURCE_COLUMNS),
                                                 "---|" * len(projects.RESOURCE_COLUMNS), rows), encoding="utf-8")
    for name in deliverables:
        (folder / "deliverables" / name).write_text("draft\n", encoding="utf-8")
    _git(folder, "init", "-q")
    for n, day in enumerate(commit_days):
        (folder / "STATE.md").write_text("# State of %s\n\nStatus: %s\nNext: step %d.\n" % (code, status, n),
                                         encoding="utf-8")
        _git(folder, "add", "-A")
        _git(folder, "commit", "-q", "-m", "step %d" % n, day=day)
    _touch(folder / "STATE.md", state_day)
    return projects.Project(code, kind, customer, "tcp", str(folder), projects.memory_key(folder), state,
                            commit_days[0] if commit_days else state_day)


@pytest.fixture
def planted(home):
    rows = [
        plant(home, ACTIVE, "project", fixtures.CUSTOMER_CODE, "active", "move the web shop to managed k8s",
              "sizing sent, waiting on the customer", ["2026-05-04", "2026-09-25"], "2026-09-25", resources=2,
              deliverables=("offer.md", "notes.md", "sizing.xlsx")),
        plant(home, FINISHED, "project", fixtures.CUSTOMER_CODE, "active", "price a backup target",
              "price sheet delivered", ["2026-06-10"], "2026-06-10", deliverables=("prices.csv",)),
        plant(home, QUERY, "query", projects.NO_CUSTOMER, "active", "can cce run gpu nodes",
              "answered from the docs", ["2026-07-01"], "2026-07-01"),
        plant(home, BEFORE, "project", fixtures.CUSTOMER_CODE, "closed", "an old proof", "closed",
              ["2026-03-15"], "2026-03-15"),
    ]
    projects._save(home, rows)
    ledger.add("proof", "first lab built", project=ACTIVE, customer=fixtures.CUSTOMER_CODE, tags=["ecs"],
               day="2026-05-04", p=home)
    ledger.add("pricing", "sizing sent", project=ACTIVE, customer=fixtures.CUSTOMER_CODE, tags=["ecs", "obs"],
               day="2026-09-20", p=home)
    ledger.add("pricing", "price sheet", project=FINISHED, customer=fixtures.CUSTOMER_CODE, tags=["cbr"],
               day="2026-06-10", p=home)
    ledger.add("inquiry", "gpu nodes answered", project=QUERY, tags=["cce"], day="2026-07-01", p=home)
    ledger.add("proof", "old proof", project=BEFORE, customer=fixtures.CUSTOMER_CODE, day="2026-03-15", p=home)
    ledger.add("tooling", "a tool fixed", tags=["iam"], day="2026-08-03", p=home)
    log = home.vault / "log"
    log.mkdir(parents=True, exist_ok=True)
    line = "%sT09:00:00Z\t1001\ttest-1\tlab\tGET\tecs\teu-de\t200\t%s\n"
    (log / "cloud-2026-09.tsv").write_text(
        line % ("2026-09-02", ACTIVE) * 3 + line % ("2026-09-03", "-"),
        encoding="utf-8")
    (log / "cloud-2026-03.tsv").write_text(line % ("2026-03-15", BEFORE), encoding="utf-8")
    return home


@pytest.fixture
def owner(monkeypatch):
    monkeypatch.setattr(config, "is_work_user", lambda: False)
    monkeypatch.delenv("CLAUDECODE", raising=False)
    monkeypatch.delenv("CLAUDE_CODE_ENTRYPOINT", raising=False)


def run(argv, capsys):
    code = cli.main(argv)
    out, err = capsys.readouterr()
    return code, out, err


def section(text: str, code: str) -> str:
    part = text.split("### %s\n" % code, 1)[1]
    return part.split("\n### ", 1)[0].split("\n## ", 1)[0]


def test_three_projects_land_in_their_blocks(planted, capsys):
    code, out, err = run(["report", "--period", "--from", START, "--to", END], capsys)
    assert code == 0, err
    b1, b2, b3, rules = (out.index("## 1 Active customer work (1)"), out.index("## 2 Finished customer work (1)"),
                         out.index("## 3 Technical questions (1)"), out.index("## Block rules"))
    assert b1 < out.index("### " + ACTIVE) < b2 < out.index("### " + FINISHED) < b3 < out.index("### " + QUERY) < rules
    assert BEFORE not in out
    assert "- projects: 3 (block 1: 1, block 2: 1, block 3: 1)" in out
    assert "- ledger entries: 5, by kind: pricing 2, inquiry 1, proof 1, tooling 1" in out
    assert "- ledger entries by technology: ecs 2, cbr 1, cce 1, iam 1, obs 1" in out


def test_one_entry_carries_every_field(planted, capsys):
    _, out, _ = run(["report", "--period", "--from", START, "--to", END], capsys)
    a = section(out, ACTIVE)
    assert "- customer: %s" % fixtures.CUSTOMER_CODE in a
    assert "- kind: project, state active" in a
    assert "- activity: 2026-05-04 to 2026-09-25 (STATE.md 2, commits 2, ledger 2)" in a
    assert "- goal: move the web shop to managed k8s" in a
    assert "- ledger kinds: pricing 1, proof 1" in a
    assert "- technologies: ecs 2, obs 1" in a
    assert "- deliverables: md 2, xlsx 1" in a
    assert "- test tenant: yes (RESOURCES.md 2 rows, 3 key service calls)" in a
    assert "- status: sizing sent, waiting on the customer" in a
    c = section(out, QUERY)
    assert "- customer: none" in c and "- test tenant: no (RESOURCES.md 0 rows, 0 key service calls)" in c


def test_the_block_rules_live_in_one_place_and_are_printed(planted, capsys):
    _, out, _ = run(["report", "--period", "--from", START, "--to", END], capsys)
    tail = out[out.index("## Block rules"):]
    for n, name, rule in period.BLOCKS:
        assert ledger.plain("%d %s: %s." % (n, name, rule)) in tail
    end = date(2026, 9, 30)
    cust = fixtures.CUSTOMER_CODE
    assert period.block_of("project", cust, "active", date(2026, 9, 1), end) == 1     # end - 29
    assert period.block_of("project", cust, "active", date(2026, 8, 31), end) == 2    # end - 30
    assert period.block_of("project", cust, "closed", end, end) == 2
    assert period.block_of("query", cust, "active", end, end) == 3
    assert period.block_of("engagement", cust, "active", end, end) == 3              # a legacy kind of query
    assert period.block_of("project", "none", "active", end, end) == 3


def test_codes_only_by_default_and_in_html(planted, capsys):
    for fmt in ("md", "html"):
        code, out, err = run(["report", "--period", "--from", START, "--to", END, "--format", fmt], capsys)
        assert code == 0, err
        assert fixtures.CUSTOMER_CODE in out
        fixtures.assert_no_fixture_name(out + err, "the period report")
    assert out.startswith("<!DOCTYPE html>") and "<h2>3 Technical questions (1)</h2>" in out


def test_a_text_that_carries_a_name_is_withheld(planted, capsys):
    scope = planted.projects_root / QUERY / "SCOPE.md"
    scope.write_text(scope.read_text(encoding="utf-8").replace("can cce run gpu nodes", "gpu nodes for " + NAME),
                     encoding="utf-8")
    code, out, err = run(["report", "--period", "--from", START, "--to", END], capsys)
    assert code == 0
    assert "- goal: %s" % ledger.WITHHELD in section(out, QUERY)
    assert "1 texts withheld" in err
    fixtures.assert_no_fixture_name(out + err, "the period report")


def test_the_call_log_not_readable_here_is_said(planted, capsys, monkeypatch):
    monkeypatch.setattr(period, "call_counts", lambda *a: None)
    _, out, _ = run(["report", "--period", "--from", START, "--to", END], capsys)
    assert "- test tenant: yes (RESOURCES.md 2 rows, call log not readable here)" in section(out, ACTIVE)


def test_reveal_puts_the_name_back_for_the_owner_only_into_his_file(planted, owner, tmp_path, capsys):
    target = tmp_path / "own" / "period.md"
    target.parent.mkdir()
    code, out, err = run(["report", "--period", "--from", START, "--to", END, "--reveal", "--out", str(target)],
                         capsys)
    assert code == 0, err
    named = target.read_text(encoding="utf-8")
    assert "- customer: %s" % NAME in section(named, ACTIVE)
    assert fixtures.CUSTOMER_CODE not in named
    assert (target.stat().st_mode & 0o777) == 0o600
    fixtures.assert_no_fixture_name(out + err, "the command output")
    code, _, err = run(["report", "--period", "--from", START, "--to", END, "--reveal", "--out", str(target)], capsys)
    assert code == 2 and "--replace" in err


@pytest.mark.parametrize("who", ["work user", "assistant session"])
def test_reveal_is_refused_for_the_work_user_and_inside_a_session(planted, owner, tmp_path, capsys, monkeypatch,
                                                                   who):
    if who == "work user":
        monkeypatch.setattr(config, "is_work_user", lambda: True)
    else:
        monkeypatch.setenv("CLAUDECODE", "1")
    target = tmp_path / "named.md"
    code, out, err = run(["report", "--period", "--from", START, "--to", END, "--reveal", "--out", str(target)],
                         capsys)
    assert code == 2 and not target.exists() and out == ""
    fixtures.assert_no_fixture_name(out + err, "the refusal")


def test_reveal_never_goes_to_a_pipe_a_project_or_a_git_working_copy(planted, owner, tmp_path, capsys):
    code, out, err = run(["report", "--period", "--reveal"], capsys)        # capsys is no terminal
    assert code == 2 and out == "" and "never to a pipe" in err
    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)
    for target in (repo / "sub" / "x.md", planted.projects_root / ACTIVE / "x.md", planted.shared / "x.md"):
        target.parent.mkdir(parents=True, exist_ok=True)
        code, out, err = run(["report", "--period", "--reveal", "--out", str(target)], capsys)
        assert code == 2 and not target.exists(), target.name
        fixtures.assert_no_fixture_name(out + err, "the refusal")
    code, _, err = run(["report", "--period", "--out", str(tmp_path / "x.md")], capsys)
    assert code == 2 and "go with --reveal" in err


def test_the_help_says_where_the_named_report_goes(capsys):
    code, out, _ = run(["report", "--period", "--help"], capsys)
    out = " ".join(out.split())
    assert code == 0 and "outside every git working copy" in out and "never to a pipe" in out


def test_from_after_to_is_refused(planted, capsys):
    code, _, err = run(["report", "--period", "--from", END, "--to", START], capsys)
    assert code == 2 and "--from is after --to" in err
