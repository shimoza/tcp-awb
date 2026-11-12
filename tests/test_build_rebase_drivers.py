"""The runner's rebase onto a moved main (awb/build_checks.py check_onto_main) merges CHANGELOG.md and
build/tests.json with the drivers of awb/mergedriver.py, as `awb pr take` does. Every repository is a temporary
main.git with invented content."""
from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from awb import build_checks

ENV = {"GIT_AUTHOR_NAME": "tester", "GIT_AUTHOR_EMAIL": "tester@example.invalid",
       "GIT_COMMITTER_NAME": "tester", "GIT_COMMITTER_EMAIL": "tester@example.invalid",
       "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_NOSYSTEM": "1"}

CHANGELOG = """# Changelog

## Week 46, 2026-11-09 to 2026-11-15 (in progress)
### Added
- the first line of the week.

## Week 45, 2026-11-02 to 2026-11-08
### Added
- a line of the week before.
"""


def rows(*ids: str) -> str:
    data = [{"id": i, "name": "test " + i, "run": "pytest", "status": "passing"} for i in ids]
    return json.dumps({"tests": data, "total_tests": len(data), "checked": "2026-11-10"}, indent=1) + "\n"


class Repo:
    def __init__(self, tmp: Path):
        self.work = tmp / "work"
        self.bare = tmp / "main.git"
        self.env = dict(os.environ, **ENV)
        self.g("init", "-q", "-b", "main", str(self.work), cwd=tmp)
        self.commit({"CHANGELOG.md": CHANGELOG, "build/tests.json": rows("t1"), "awb/x.py": "VALUE = 1\n",
                     "awb/mergedriver.py": "# the driver\n"}, "start")
        self.start = self.rev("HEAD")

    def g(self, *args: str, cwd: Path | None = None) -> str:
        r = subprocess.run(["git", *args], cwd=cwd or self.work, capture_output=True, text=True, env=self.env)
        assert r.returncode == 0, r.stderr
        return r.stdout.strip()

    def rev(self, ref: str) -> str:
        return self.g("rev-parse", ref)

    def commit(self, files: dict[str, str], msg: str) -> str:
        for rel, text in files.items():
            p = self.work / rel
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(text, encoding="utf-8")
            self.g("add", rel)
        self.g("commit", "-q", "-m", msg)
        return self.rev("HEAD")

    def branch_and_main(self, branch: dict[str, str], main: dict[str, str]) -> tuple[str, str]:
        """The branch's tip and main's, both one commit after the start."""
        self.g("checkout", "-q", "-b", "task", self.start)
        tip = self.commit(branch, "the branch")
        self.g("checkout", "-q", "main")
        moved = self.commit(main, "main moves")
        self.g("clone", "-q", "--bare", str(self.work), str(self.bare), cwd=self.work.parent)
        self.g("--git-dir", str(self.bare), "fetch", "-q", str(self.work), "task:refs/heads/task")
        return tip, moved

    def run(self, tip: str, tmp: Path):
        host = SimpleNamespace(main_git=self.bare, git_run=subprocess.run)
        folder = tmp / "run"
        folder.mkdir()
        ctx = SimpleNamespace(start=self.start, tip=tip, lines=[], folder=folder, tid="B-900", n=1)
        try:
            build_checks.check_onto_main(host, ctx)
            state = None
        except build_checks.Stop as stop:
            state = stop.state
        return state, ctx

    def show(self, ref: str, path: str) -> str:
        return self.g("--git-dir", str(self.bare), "show", "%s:%s" % (ref, path))


@pytest.fixture
def repo(tmp_path):
    return Repo(tmp_path)


def test_a_changelog_line_on_both_sides_in_one_week_rebases_with_both_lines(repo, tmp_path):
    ours = CHANGELOG.replace("- the first line of the week.\n", "- the first line of the week.\n- a line of main.\n")
    theirs = CHANGELOG.replace("- the first line of the week.\n", "- the first line of the week.\n- a line of the task.\n")
    tip, moved = repo.branch_and_main({"CHANGELOG.md": theirs}, {"CHANGELOG.md": ours})
    state, ctx = repo.run(tip, tmp_path)
    assert state is None
    text = repo.show(ctx.tip, "CHANGELOG.md")
    assert "- a line of main." in text and "- a line of the task." in text
    assert repo.g("--git-dir", str(repo.bare), "rev-parse", ctx.tip + "^") == moved


def test_a_row_on_both_sides_of_tests_json_rebases_with_both_rows_and_the_count(repo, tmp_path):
    tip, _ = repo.branch_and_main({"build/tests.json": rows("t1", "t2")}, {"build/tests.json": rows("t1", "t3")})
    state, ctx = repo.run(tip, tmp_path)
    assert state is None
    data = json.loads(repo.show(ctx.tip, "build/tests.json"))
    assert sorted(r["id"] for r in data["tests"]) == ["t1", "t2", "t3"]
    assert data["total_tests"] == 3


def test_a_conflict_in_another_file_stays_red_and_names_the_file(repo, tmp_path):
    tip, _ = repo.branch_and_main({"awb/x.py": "VALUE = 2\n"}, {"awb/x.py": "VALUE = 3\n"})
    state, ctx = repo.run(tip, tmp_path)
    assert state == "red: rebase conflict"
    assert any("conflict in awb/x.py" in ln for ln in ctx.lines)


def test_a_conflict_in_another_file_next_to_a_changelog_line_names_only_that_file(repo, tmp_path):
    ours = CHANGELOG.replace("- the first line of the week.\n", "- the first line of the week.\n- a line of main.\n")
    theirs = CHANGELOG.replace("- the first line of the week.\n", "- the first line of the week.\n- a line of the task.\n")
    tip, _ = repo.branch_and_main({"CHANGELOG.md": theirs, "awb/x.py": "VALUE = 2\n"},
                                  {"CHANGELOG.md": ours, "awb/x.py": "VALUE = 3\n"})
    state, ctx = repo.run(tip, tmp_path)
    assert state == "red: rebase conflict"
    assert any(ln.endswith("conflict in awb/x.py") for ln in ctx.lines)


def test_a_branch_that_changes_the_driver_does_not_run_its_own_version(repo, tmp_path):
    evil = ("import sys\nfrom pathlib import Path\n"
            "Path(sys.argv[3]).write_text('TAKEN BY THE BRANCH DRIVER\\n')\n"
            "Path(%r).write_text('ran')\n" % str(tmp_path / "marker"))
    ours = CHANGELOG.replace("- the first line of the week.\n", "- the first line of the week.\n- a line of main.\n")
    theirs = CHANGELOG.replace("- the first line of the week.\n", "- the first line of the week.\n- a line of the task.\n")
    tip, _ = repo.branch_and_main({"CHANGELOG.md": theirs, "awb/mergedriver.py": evil,
                                   ".gitattributes": "CHANGELOG.md merge=ours\n"},
                                  {"CHANGELOG.md": ours})
    state, ctx = repo.run(tip, tmp_path)
    assert state is None
    assert not (tmp_path / "marker").exists()
    text = repo.show(ctx.tip, "CHANGELOG.md")
    assert "- a line of main." in text and "- a line of the task." in text
    assert repo.show(ctx.tip, "awb/mergedriver.py") == evil.strip()
