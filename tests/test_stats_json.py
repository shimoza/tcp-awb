"""B-114: STATS.json in both public repositories. The dataset build writes it next to facts.jsonl with the numbers of the
README first screen (each with counted_by); tools/stats.py writes the one of the repository, counts a temporary git
repository and names the method of every count; a number without counted_by is refused."""
from __future__ import annotations

import importlib.util
import json
import os
import re
import subprocess
from datetime import date
from pathlib import Path

import pytest

from awb.tcp import dataset
from tests.test_dataset import TODAY, facts, live, new  # noqa: F401  (fixtures)

TOOL = Path(__file__).resolve().parent.parent / "tools" / "stats.py"


def _tool():
    spec = importlib.util.spec_from_file_location("awb_tools_stats", TOOL)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _numbers(stats: dict) -> dict:
    return {k: v for k, v in stats.items() if not isinstance(v, str)}


# --------------------------------------------------------------------------- the dataset


@pytest.fixture
def built(home, facts):
    new("OBS is the object storage service of TCP and keeps objects in buckets", grade="docs", cls="stable",
        tags=["obs"])
    return dataset.build(home, out=home.shared / "ds-b114", today=TODAY)


def test_the_dataset_stats_has_every_field_with_counted_by_and_its_numbers_equal_the_readme(built):
    stats = json.loads((built.folder / "STATS.json").read_text(encoding="utf-8"))
    fields = _numbers(stats)
    assert set(fields) == {"released", "facts", "facts_tested_live", "services_covered", "services_orderable",
                           "facts_weekly_check", "facts_recheck_due", "licence_data", "licence_code"}
    for name, field in fields.items():
        assert set(field) == {"value", "counted_by"}, name
        assert field["counted_by"].strip(), name
    readme = (built.folder / "README.md").read_text(encoding="utf-8")
    v = {k: f["value"] for k, f in fields.items()}
    assert "**%d facts**" % v["facts"] in readme
    assert "**%d facts tested live**" % v["facts_tested_live"] in readme
    assert "**%d services** of the service description of %s" % (v["services_orderable"],
                                                                stats["service_description"]) in readme
    assert "%d of them with a plain fact" % v["services_covered"] in readme
    assert "Freshness: %d of the %d facts wait for a recheck" % (v["facts_recheck_due"], v["facts"]) in readme
    assert "%d of the %d facts have a weekly automated check" % (v["facts_weekly_check"], v["facts"]) in readme
    assert "Released %s." % v["released"] in readme and "STATS.json" in readme
    assert v["licence_data"] == "CC BY 4.0" and v["licence_code"] == "Apache-2.0"


def test_the_dataset_stats_equals_what_its_counted_by_counts_in_the_files(built):
    stats = json.loads((built.folder / "STATS.json").read_text(encoding="utf-8"))
    rows = [json.loads(x) for x in (built.folder / "facts.jsonl").read_text(encoding="utf-8").splitlines()]
    head, records = rows[0], [r for r in rows if r.get("id")]
    assert stats["facts"]["counted_by"] == dataset.JQ_FACTS and stats["facts"]["value"] == len(records) > 0
    assert stats["facts_tested_live"]["value"] == sum(1 for r in records if r["grade"] == "live") > 0
    assert stats["facts_tested_live"]["counted_by"] == dataset.JQ_LIVE
    assert stats["facts_weekly_check"]["value"] == sum(1 for r in records if r["level"] == "V4")
    assert stats["facts_recheck_due"]["value"] == sum(1 for r in records if r["recheck_due"])
    assert stats["released"]["value"] == head["date"] == built.day
    services = [x for x in (built.folder / "services.md").read_text(encoding="utf-8").splitlines()
                if re.match(r"- [0-9]", x)]
    assert stats["services_orderable"]["value"] == len(services) == built.services
    assert stats["services_covered"]["value"] <= stats["services_orderable"]["value"]
    manifest = json.loads((built.folder / "MANIFEST.json").read_text(encoding="utf-8"))
    assert "STATS.json" in manifest["files"] and manifest["facts"] == stats["facts"]["value"]
    assert "STATS.json" in dataset.GITHUB_FILES


def test_a_dataset_number_without_counted_by_is_refused():
    with pytest.raises(dataset.DatasetError, match="counted_by"):
        dataset._stat(3, " ")


# --------------------------------------------------------------------------- tools/stats.py


def _git(repo: Path, *args: str, when: str = "2026-11-01T12:00:00") -> None:
    env = dict(os.environ, GIT_AUTHOR_NAME="Owner", GIT_AUTHOR_EMAIL="owner@example.invalid",
               GIT_COMMITTER_NAME="Owner", GIT_COMMITTER_EMAIL="owner@example.invalid",
               GIT_AUTHOR_DATE=when, GIT_COMMITTER_DATE=when)
    subprocess.run(["git", "-C", str(repo), "-c", "core.hooksPath=/dev/null", "-c", "commit.gpgsign=false", *args],
                   check=True, capture_output=True, env=env)


def _put(repo: Path, name: str, text: str) -> None:
    (repo / name).parent.mkdir(parents=True, exist_ok=True)
    (repo / name).write_text(text, encoding="utf-8")


@pytest.fixture
def repo(tmp_path):
    r = tmp_path / "small-repo"
    r.mkdir()
    _git(r, "init", "-q")
    _put(r, "awb/__init__.py", "")
    _put(r, "awb/one.py", "a = 1\nb = 2\n")
    _put(r, "awb/sub/two.py", "c = 3\n")
    _git(r, "add", "-A")
    _git(r, "commit", "-q", "-m", "old", when="2026-09-01T12:00:00")
    _put(r, "tests/test_one.py", "def test_a():\n    pass\n\n\nclass TestT:\n    def test_b(self):\n        pass\n"
                                 "\n\ndef helper():\n    pass\n")
    _put(r, "plugin/skills/first/SKILL.md", "skill\n")
    _put(r, "plugin/agents/worker.md", "agent\n")
    _put(r, "COMMANDS.md", "# Commands\n\n### `awb hand written`\n\n## Reference of every command and option\n\n"
                           "### `awb one`\n\n### `awb two [--x]`\n")
    _git(r, "add", "-A")
    _git(r, "commit", "-q", "-m", "new one", when="2026-10-05T12:00:00")
    _put(r, "awb/three.py", "d = 4\n")
    _git(r, "add", "-A")
    _git(r, "commit", "-q", "-m", "new two", when="2026-11-02T12:00:00")
    return r


def test_the_tool_counts_a_temporary_git_repository_and_names_the_method_of_every_count(repo):
    tool = _tool()
    path = tool.write(repo, today=date(2026, 11, 27))
    assert path == repo / "STATS.json"
    stats = json.loads(path.read_text(encoding="utf-8"))
    v = {k: f["value"] for k, f in _numbers(stats).items()}
    assert v == {"date": "2026-11-27", "commits": 2, "lines_awb": 4, "modules": 3, "test_functions": 2, "skills": 1,
                 "agents": 1, "commands": 2}
    for name, field in _numbers(stats).items():
        assert field["counted_by"].strip(), name
    by = {k: f["counted_by"] for k, f in _numbers(stats).items()}
    assert by["commits"] == "git rev-list --count --since=2026-09-28 HEAD"
    assert "git grep" in by["test_functions"] and "wc -l" in by["lines_awb"]
    assert "awb build list" not in " ".join(by.values())       # a repository without awb/build.py has no queue


def test_the_tool_takes_the_build_numbers_from_awb_build_list(repo):
    lines = ["B-001  done  since 2d  waits on the push", "B-002  done  since 1d  waits on the push",
             "B-003  running  since 5m  waits on the runner", "C-001  passed  since 3d  a check of a branch, result in x",
             "C-002  red: a test failed  since 2d  a check of a branch, result in y",
             "C-003  queued  since 1m  a check of a branch, waits on the runner", "Q-1  refused: a fault"]
    stats = _tool().collect(repo, lines=lines, today=date(2026, 11, 27))
    assert stats["build_tasks_done"]["value"] == 2
    assert stats["branch_checks_passed"]["value"] == 1 and stats["branch_checks_refused"]["value"] == 1
    for name in ("build_tasks_done", "branch_checks_passed", "branch_checks_refused"):
        assert stats[name]["counted_by"].startswith("awb build list")
    omitted = _tool().collect(repo, lines=None, today=date(2026, 11, 27))
    assert "build_tasks_done" not in omitted and "branch_checks_passed" not in omitted


def test_the_tool_counts_test_functions_with_pytest_when_asked(repo):
    _put(repo, "tests/test_two.py", "import pytest\n\n\n@pytest.mark.parametrize('x', [1, 2, 3])\n"
                                    "def test_c(x):\n    assert x\n")
    _git(repo, "add", "-A")
    tool = _tool()
    grep = tool.collect(repo, today=date(2026, 11, 27))["test_functions"]
    collected = tool.collect(repo, pytest=True, today=date(2026, 11, 27))["test_functions"]
    assert grep["value"] == 3 and "git grep" in grep["counted_by"]
    assert collected["value"] == 5 and "--collect-only" in collected["counted_by"]


def test_a_number_without_counted_by_is_refused_and_nothing_is_written(repo, monkeypatch):
    tool = _tool()
    with pytest.raises(tool.StatsError, match="counted_by"):
        tool.validate({"repository": "x", "commits": {"value": 3}})
    with pytest.raises(tool.StatsError, match="counted_by"):
        tool.validate({"commits": {"value": 3, "counted_by": "  "}})
    monkeypatch.setattr(tool, "_tests", lambda root, pytest: {"value": 7})
    with pytest.raises(tool.StatsError, match="test_functions"):
        tool.write(repo, today=date(2026, 11, 27))
    assert not (repo / "STATS.json").exists()
    assert tool.main(["--repo", str(repo)]) == 2 and not (repo / "STATS.json").exists()
