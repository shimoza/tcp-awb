"""`awb pr take N` (awb/prtake.py) and the merge drivers of build/tests.json and CHANGELOG.md (awb/mergedriver.py).

Every test builds a fake origin (a bare repository with a pull request head under refs/pull/N/head), the owner's
working copy cloned from it and a bare build repository. No network; `awb build check` and the gate of the merge
message are stand-ins that record what they were given."""
from __future__ import annotations

import json
import os
import stat
import subprocess
from pathlib import Path

import pytest

from awb import mergedriver, prtake

ENV = {"GIT_AUTHOR_NAME": "tester", "GIT_AUTHOR_EMAIL": "tester@example.invalid",
       "GIT_COMMITTER_NAME": "tester", "GIT_COMMITTER_EMAIL": "tester@example.invalid",
       "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_NOSYSTEM": "1"}

CHANGELOG = """# Changelog

What changed, newest first.

## Week 42, 2026-10-12 to 2026-10-18 (in progress)

### Added

- the first line of the week.

### Fixed

- a fix of the week.

## Week 41, 2026-10-05 to 2026-10-11

### Added

- a line of the week before.
"""


def git(cwd: Path, *args: str) -> str:
    r = subprocess.run(["git", "-C", str(cwd), *args], capture_output=True, text=True,
                       env=dict(os.environ, **ENV))
    assert r.returncode == 0, r.stderr
    return r.stdout.strip()


def rows_json(*ids: tuple[str, str]) -> str:
    rows = [{"id": i, "name": n, "run": "pytest", "status": "passing"} for i, n in ids]
    return json.dumps({"tests": rows, "total_tests": len(rows), "checked": "2026-10-14"}, indent=1) + "\n"


def commit(cwd: Path, files: dict[str, str], msg: str) -> str:
    for rel, text in files.items():
        p = cwd / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")
        git(cwd, "add", rel)
    git(cwd, "commit", "--quiet", "-m", msg)
    return git(cwd, "rev-parse", "HEAD")


class World:
    """origin.git, the working copy, the build repository and a cloud session's clone that opens pull requests."""

    def __init__(self, tmp: Path, monkeypatch):
        for k, v in ENV.items():
            monkeypatch.setenv(k, v)
        self.tmp = tmp
        self.origin = tmp / "origin.git"
        self.work = tmp / "work"
        self.cloud = tmp / "cloud"
        self.build = tmp / "build" / "main.git"
        seed = tmp / "seed"
        seed.mkdir()
        git(seed, "init", "--quiet", "-b", "main")
        commit(seed, {"README.md": "one\ntwo\nthree\n", "CHANGELOG.md": CHANGELOG,
                      "build/tests.json": rows_json(("A1", "the first test")),
                      ".gitattributes": "build/tests.json merge=awb-tests\nCHANGELOG.md merge=awb-changelog\n"},
               "the start")
        git(tmp, "clone", "--quiet", "--bare", str(seed), str(self.origin))
        git(tmp, "clone", "--quiet", "--bare", str(self.origin), str(self.build))
        git(tmp, "clone", "--quiet", str(self.origin), str(self.work))
        git(tmp, "clone", "--quiet", str(self.origin), str(self.cloud))
        self.checked: list[str] = []
        self.check_code = 0
        self.messages: list[str] = []

    def open_pr(self, n: int, files: dict[str, str], branch: str = "cloud-change") -> str:
        git(self.cloud, "switch", "--quiet", "-c", branch, "origin/main")
        head = commit(self.cloud, files, "a change from the cloud")
        git(self.cloud, "push", "--quiet", "origin", "%s:refs/heads/%s" % (branch, branch),
            "%s:refs/pull/%d/head" % (branch, n))
        return head

    def main_moves(self, files: dict[str, str]) -> str:
        """The owner's main gets a commit after the pull request branched off, pushed to origin and the build."""
        head = commit(self.work, files, "a change on main")
        git(self.work, "push", "--quiet", "origin", "main")
        git(self.work, "push", "--quiet", str(self.build), "main")
        return head

    def check(self, ref: str) -> int:
        self.checked.append(ref)
        return self.check_code

    def gate(self, path: Path) -> int:
        self.messages.append(path.read_text(encoding="utf-8"))
        return 0

    def take(self, n: int, gh: str | None = None) -> tuple[int, list[str]]:
        lines: list[str] = []
        t = prtake.Take(n, self.work, check=self.check, build_repo=lambda: str(self.build),
                        gate_message=self.gate, gh=gh, out=lines.append)
        return t.run(), lines

    def heads(self) -> tuple[str, str, str]:
        return (git(self.work, "rev-parse", "main"), git(self.origin, "rev-parse", "main"),
                git(self.build, "rev-parse", "main"))


@pytest.fixture
def world(tmp_path, monkeypatch):
    return World(tmp_path, monkeypatch)


def test_a_clean_take_rebases_checks_merges_with_no_ff_and_pushes_to_origin_and_the_build(world):
    world.main_moves({"README.md": "one\ntwo\nthree\nfour\n"})
    pr = world.open_pr(7, {"awb/new.py": "x = 1\n"}, branch="feature-x")
    before = git(world.work, "rev-parse", "main")

    code, out = world.take(7)

    assert code == 0, out
    assert world.checked == ["take/pr-7"]
    main, origin, build = world.heads()
    assert main == origin == build != before
    parents = git(world.work, "log", "-1", "--format=%P", "main").split()
    assert parents[0] == before and len(parents) == 2
    assert git(world.work, "log", "-1", "--format=%s", "main") == "Merge pull request #7: feature-x"
    assert world.messages == ["Merge pull request #7: feature-x\n"]
    rebased = parents[1]
    assert rebased != pr and git(world.work, "merge-base", "--is-ancestor", before, rebased) == ""
    assert (world.work / "awb" / "new.py").read_text() == "x = 1\n"
    assert git(world.work, "branch", "--list", "take/pr-7") == ""
    assert git(world.work, "worktree", "list").count("\n") == 0
    assert any("nothing was deployed" in ln for ln in out)


def test_a_take_with_gh_names_the_title_and_closes_the_pull_request_with_the_merge_commit(world, tmp_path):
    world.open_pr(8, {"docs/x.md": "a page\n"})
    log = tmp_path / "gh.log"
    gh = tmp_path / "gh"
    gh.write_text("#!/bin/sh\nprintf '%%s\\n' \"$*\" >> %s\n"
                  "if [ \"$1 $2\" = \"pr view\" ]; then echo 'add a page'; fi\n" % log)
    gh.chmod(gh.stat().st_mode | stat.S_IXUSR)

    code, out = world.take(8, gh=str(gh))

    assert code == 0, out
    merged = git(world.work, "rev-parse", "main")
    assert git(world.work, "log", "-1", "--format=%s", "main") == "Merge pull request #8: add a page"
    calls = log.read_text().splitlines()
    assert calls[0] == "pr view 8 --json title --jq .title"
    assert calls[1].startswith("pr close 8 --comment Merged into main as %s" % merged)


def test_a_tests_json_and_changelog_conflict_is_merged_by_id_and_by_week(world):
    world.main_moves({
        "build/tests.json": rows_json(("A1", "the first test"), ("M1", "a test of main")),
        "CHANGELOG.md": CHANGELOG.replace("- a fix of the week.\n", "- a fix of the week.\n- a fix on main.\n")})
    world.open_pr(9, {
        "build/tests.json": rows_json(("A1", "the first test"), ("PRT-1", "a test of the branch")),
        "CHANGELOG.md": CHANGELOG.replace("- a fix of the week.\n",
                                          "- a fix of the week.\n- a fix of the branch,\n  on two lines.\n")})

    code, out = world.take(9)

    assert code == 0, out
    data = json.loads((world.work / "build" / "tests.json").read_text())
    assert [r["id"] for r in data["tests"]] == ["A1", "M1", "PRT-1"]
    assert data["total_tests"] == 3
    text = (world.work / "CHANGELOG.md").read_text()
    week42 = text.split("## Week 41")[0]
    assert "- a fix on main.\n- a fix of the branch,\n  on two lines.\n" in week42
    assert text.count("## Week 42") == 1


def test_two_different_rows_with_one_id_are_refused_and_main_stays(world):
    world.main_moves({"build/tests.json": rows_json(("A1", "the first test"), ("D1", "one row"))})
    world.open_pr(10, {"build/tests.json": rows_json(("A1", "the first test"), ("D1", "another row"))})
    heads = world.heads()

    code, out = world.take(10)

    assert code == 1
    assert "awb merge: two different rows with the id D1" in out
    assert any("conflict in build/tests.json" in ln for ln in out)
    assert world.heads() == heads
    assert world.checked == []
    assert git(world.work, "status", "--porcelain") == ""
    assert git(world.work, "worktree", "list").count("\n") == 0


def test_another_conflict_stops_with_the_file_name(world):
    world.main_moves({"README.md": "one\nmain\nthree\n"})
    world.open_pr(11, {"README.md": "one\nbranch\nthree\n"})
    heads = world.heads()

    code, out = world.take(11)

    assert code == 1
    assert any("conflict in README.md" in ln for ln in out)
    assert world.heads() == heads and world.checked == []


def test_a_failed_check_leaves_main_alone_and_keeps_the_branch(world):
    world.open_pr(12, {"awb/new.py": "x = 2\n"})
    world.check_code = 1
    heads = world.heads()

    code, out = world.take(12)

    assert code == 1
    assert world.checked == ["take/pr-12"]
    assert world.heads() == heads
    assert world.messages == []
    assert git(world.work, "branch", "--list", "take/pr-12").strip() == "take/pr-12"
    assert any("the check did not pass" in ln for ln in out)


def test_a_dirty_tree_is_refused_before_the_fetch(world):
    world.open_pr(13, {"awb/new.py": "x = 3\n"})
    (world.work / "README.md").write_text("changed\n")
    heads = world.heads()

    code, out = world.take(13)

    assert code == 2
    assert any("the working tree has a change" in ln for ln in out)
    assert git(world.work, "branch", "--list", "take/pr-13") == ""
    assert world.heads() == heads and world.checked == []


def test_a_main_behind_the_build_repository_is_refused(world):
    world.open_pr(14, {"awb/new.py": "x = 4\n"})
    other = world.tmp / "other"
    git(world.tmp, "clone", "--quiet", str(world.build), str(other))
    commit(other, {"docs/y.md": "y\n"}, "a task of the runner")
    git(other, "push", "--quiet", "origin", "main")

    code, out = world.take(14)

    assert code == 2
    assert any("of the build repository is not in your main" in ln for ln in out)


def test_the_dry_run_prints_the_steps_and_changes_nothing(world, monkeypatch, capsys):
    world.open_pr(15, {"awb/new.py": "x = 5\n"})
    heads = world.heads()
    monkeypatch.chdir(world.work)
    monkeypatch.setenv("AWB_BUILD_ROOT", str(world.build.parent))
    monkeypatch.setenv("PATH", str(world.tmp / "nothing") + os.pathsep + "/usr/bin:/bin")

    assert prtake.main(["take", "15", "--dry-run"]) == 0

    out = capsys.readouterr().out
    assert "git fetch origin +pull/15/head:refs/heads/take/pr-15" in out
    assert "awb build check take/pr-15" in out
    assert "git merge --no-ff take/pr-15" in out
    assert "git push %s main" % world.build in out
    assert "5. on a failure main stays as it is; nothing is deployed" in out
    assert world.heads() == heads
    assert git(world.work, "branch", "--list", "take/pr-15") == ""


def test_the_changelog_driver_keeps_both_sides_lines_in_their_week():
    ours = CHANGELOG.replace("- the first line of the week.\n", "- the first line of the week.\n- ours.\n")
    theirs = CHANGELOG.replace("- a line of the week before.\n", "- a line of the week before.\n- theirs late.\n")
    theirs = theirs.replace("## Week 42", "## Week 43, 2026-10-19 to 2026-10-25\n\n### Added\n\n- next.\n\n"
                            "## Week 42", 1)
    out = mergedriver.merge_changelog(CHANGELOG, ours, theirs)
    assert out.index("## Week 43") < out.index("## Week 42") < out.index("- ours.") < out.index("## Week 41")
    assert out.index("## Week 41") < out.index("- theirs late.")
    assert mergedriver.merge_changelog(CHANGELOG, CHANGELOG, CHANGELOG) == CHANGELOG


def test_the_tests_driver_drops_a_row_one_side_dropped_and_refuses_a_file_with_one_id_twice(tmp_path):
    base = rows_json(("A1", "a"), ("A2", "b"))
    ours = rows_json(("A1", "a"))
    theirs = rows_json(("A1", "a"), ("A2", "b"), ("PRT-9", "c"))
    data = json.loads(mergedriver.merge_tests(base, ours, theirs))
    assert [r["id"] for r in data["tests"]] == ["A1", "PRT-9"] and data["total_tests"] == 2
    twice = rows_json(("A1", "a"), ("A1", "a"))
    files = []
    for name, text in (("o", base), ("a", twice), ("b", theirs)):
        (tmp_path / name).write_text(text)
        files.append(str(tmp_path / name))
    assert mergedriver.main(["tests", *files]) == 1
    assert (tmp_path / "a").read_text() == twice
