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


# The standalone driver is also used in exports with no installed awb package.
# These tests invoke its file directly or call its functions, never the build CLI.
@pytest.fixture
def versions(tmp_path, monkeypatch):
    from awb import mergedriver

    monkeypatch.chdir(tmp_path)
    paths = [tmp_path / name for name in ("base", "ours", "theirs")]
    for path, text in zip(paths, (rows("t1"), rows("t1", "t2"), rows("t1", "t3"))):
        path.write_text(text, encoding="utf-8")
    return mergedriver, paths


def driver_args(paths, kind="tests"):
    return [kind, *map(str, paths)]


def assert_refusal(driver, paths, capsys, kind="tests"):
    before = paths[1].read_bytes()
    assert driver.main(driver_args(paths, kind)) == 1
    assert paths[1].read_bytes() == before
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err.startswith("awb merge: ")
    assert len(captured.err.splitlines()) == 1
    return captured.err


def test_driver_conflict_diagnostics_withhold_ids_keys_names_and_values(versions, capsys):
    from tests import fixtures

    driver, paths = versions
    hidden = fixtures.PERSON_FORMS[0] + "\n" + "pass" + "word" + "=private-value"
    cases = [
        (rows(), rows(hidden, hidden), rows()),
        (rows(), rows(hidden), rows(hidden).replace('"pytest"', '"other"')),
        (rows(hidden), rows(hidden).replace('"pytest"', '"one"'),
         rows(hidden).replace('"pytest"', '"two"')),
        (json.dumps({"tests": [], hidden: 0}), json.dumps({"tests": [], hidden: 1}),
         json.dumps({"tests": [], hidden: 2})),
    ]
    for texts in cases:
        with pytest.raises(driver.Refused) as error:
            driver.merge_tests(*texts)
        fixtures.assert_no_fixture_name(str(error.value))
        assert "private-value" not in str(error.value)
        for path, text in zip(paths, texts):
            path.write_text(text, encoding="utf-8")
        err = assert_refusal(driver, paths, capsys)
        assert hidden not in err
        fixtures.assert_no_fixture_name(err)
        assert "private-value" not in err


def test_tests_json_rejects_duplicate_object_keys(versions, capsys):
    driver, paths = versions
    for text in ('{"tests": [], "tests": [{"id": "t1"}]}',
                 '{"tests": [{"id": "t1", "id": "t2"}]}',
                 '{"tests": [{"id": "t1", "extra": {"a": 1, "a": 2}}]}'):
        paths[2].write_text(text)
        assert_refusal(driver, paths, capsys)


def test_tests_json_rejects_nonfinite_numbers(versions, capsys):
    driver, paths = versions
    for number in ("NaN", "Infinity", "-Infinity", "1e999"):
        paths[2].write_text('{"tests": [{"id": "t4", "extra": ' + number + '}]}')
        assert_refusal(driver, paths, capsys)


def test_tests_json_rejects_deep_input_without_traceback(versions, capsys):
    driver, paths = versions
    # The C JSON decoder has a separate recursion budget on some Python versions.
    for depth in (101, 20000):
        paths[2].write_text('{"tests": [], "extra": ' + "[" * depth + "0" + "]" * depth + "}")
        with pytest.raises(driver.Refused):
            driver.merge_tests(*(p.read_text() for p in paths))
        assert_refusal(driver, paths, capsys)


def test_tests_json_rejects_invalid_checked_dates(versions, capsys):
    driver, paths = versions
    for checked in (None, {}, [], 123, "2026-02-30", "2026-1-02", "2026-11-15\n"):
        for path in paths:
            path.write_text(json.dumps({"tests": [], "checked": checked}))
        assert_refusal(driver, paths, capsys)


def test_tests_json_rejects_escaped_surrogates_before_writing(versions, capsys):
    driver, paths = versions
    paths[2].write_text('{"tests": [{"id": "t4", "name": "\\ud800"}]}')
    with pytest.raises(driver.Refused):
        driver.merge_tests(*(p.read_text() for p in paths))
    assert_refusal(driver, paths, capsys)


def test_tests_json_delete_modify_conflicts_in_both_directions(versions, capsys):
    driver, paths = versions
    changed = rows("t1").replace('"pytest"', '"changed"')
    for ours, theirs in ((rows(), changed), (changed, rows())):
        for path, text in zip(paths, (rows("t1"), ours, theirs)):
            path.write_text(text)
        assert_refusal(driver, paths, capsys)


def test_tests_json_metadata_uses_missing_distinct_from_null():
    from awb import mergedriver

    base = json.dumps({"tests": [], "extra": None})
    removed = rows()
    for ours, theirs in ((base, removed), (removed, base)):
        assert "extra" not in json.loads(mergedriver.merge_tests(base, ours, theirs))
    changed = json.dumps({"tests": [], "extra": 1})
    with pytest.raises(mergedriver.Refused):
        mergedriver.merge_tests(removed, base, changed)
    for ours, theirs in ((removed, changed), (changed, removed)):
        with pytest.raises(mergedriver.Refused):
            mergedriver.merge_tests(base, ours, theirs)


def test_changelog_refuses_conflicting_week_heading_edits():
    from awb import mergedriver

    with pytest.raises(mergedriver.Refused):
        mergedriver.merge_changelog(CHANGELOG, CHANGELOG.replace("(in progress)", "(closed)"),
                                   CHANGELOG.replace("(in progress)", "(reviewed)"))


def test_changelog_preserves_unilateral_item_formatting_and_section_tail():
    from awb import mergedriver

    base = "# Changelog\n\n## Week 46, 2026\n### Fixed\n- item\n  continuation\n"
    for changed in (base.replace("  continuation", "    continuation"), base + "\n"):
        for ours, theirs in ((base, changed), (changed, base)):
            assert mergedriver.merge_changelog(base, ours, theirs) == changed
    with pytest.raises(mergedriver.Refused):
        mergedriver.merge_changelog(base, base.replace("  continuation", "    continuation"),
                                   base.replace("  continuation", "\tcontinuation"))


def test_changelog_refuses_delete_modify_of_weeks_and_sections():
    from awb import mergedriver

    base = "# Changelog\n\n## Week 46, 2026\n### Fixed\n- item\n"
    changed = base.replace("- item", "- changed")
    for removed in ("# Changelog\n", "# Changelog\n\n## Week 46, 2026\n"):
        for ours, theirs in ((removed, changed), (changed, removed)):
            with pytest.raises(mergedriver.Refused):
                mergedriver.merge_changelog(base, ours, theirs)


def test_changelog_refuses_invalid_or_excessive_week_numbers(versions, capsys):
    driver, paths = versions
    for week in ("0", "54", "9" * 5000):
        for path in paths:
            path.write_text("## Week " + week + ", 2026\n### Fixed\n- item\n")
        assert_refusal(driver, paths, capsys, "changelog")


def test_driver_refuses_leaf_links_and_hardlinks(versions, capsys, tmp_path):
    driver, paths = versions
    target = tmp_path / "target"
    target.write_text(rows("external"))
    original = target.read_bytes()
    for link_type in ("symlink", "hardlink"):
        for path in paths:
            previous = path.read_bytes()
            path.unlink()
            if link_type == "symlink":
                path.symlink_to(target)
            else:
                os.link(target, path)
            assert_refusal(driver, paths, capsys)
            assert target.read_bytes() == original
            path.unlink()
            path.write_bytes(previous)


def test_driver_refuses_linked_ancestors(versions, capsys, tmp_path):
    driver, paths = versions
    linked = tmp_path / "linked"
    linked.symlink_to(tmp_path, target_is_directory=True)
    for index in range(3):
        args = paths.copy()
        args[index] = linked / paths[index].name
        assert_refusal(driver, args, capsys)


def test_driver_refuses_paths_outside_working_directory(versions, capsys, tmp_path):
    driver, paths = versions
    project = tmp_path / "project"
    project.mkdir()
    # All three roles must be confined; an absolute path is not authority to leave cwd.
    for index in range(3):
        local = []
        for path in paths:
            copy = project / path.name
            copy.write_bytes(path.read_bytes())
            local.append(copy)
        for spelling in (paths[index], Path("..") / paths[index].name):
            previous = Path.cwd()
            try:
                os.chdir(project)
                args = local.copy()
                args[index] = spelling
                assert_refusal(driver, args, capsys)
            finally:
                os.chdir(previous)


def test_driver_refuses_special_files_without_blocking(versions, tmp_path, monkeypatch, capsys):
    import sys

    driver, paths = versions
    before = paths[1].read_bytes()
    for special in (tmp_path / "pipe", tmp_path / "directory"):
        if special.name == "pipe":
            os.mkfifo(special)
        else:
            special.mkdir()
        for index in range(3):
            args = paths.copy()
            args[index] = special
            result = subprocess.run([sys.executable, "-I", driver.__file__, *driver_args(args)],
                                    capture_output=True, text=True, timeout=5, cwd=tmp_path)
            assert result.returncode == 1
            assert result.stdout == "" and len(result.stderr.splitlines()) == 1
            assert paths[1].read_bytes() == before
            reads = []
            real_read = os.read

            def observed_read(fd, size):
                if os.fstat(fd).st_ino == special.stat().st_ino:
                    reads.append(fd)
                return real_read(fd, size)

            with monkeypatch.context() as patch:
                patch.setattr(os, "read", observed_read)
                assert driver.main(driver_args(args)) == 1
            assert reads == []
            assert len(capsys.readouterr().err.splitlines()) == 1


def test_driver_missing_unreadable_and_invalid_utf8_inputs_are_private(versions, capsys, monkeypatch):
    driver, paths = versions
    secret = "pass" + "word" + "=hidden-path"
    for index in (0, 2):
        previous = paths[index].read_bytes()
        paths[index].unlink()
        err = assert_refusal(driver, paths, capsys)
        assert str(paths[index]) not in err
        paths[index].write_bytes(b"\xff")
        assert_refusal(driver, paths, capsys)
        paths[index].write_bytes(previous)
    real_open = os.open

    def denied(*args, **kwargs):
        if str(args[0]).endswith("theirs"):
            raise PermissionError(secret)
        return real_open(*args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(os, "open", denied)
        err = assert_refusal(driver, paths, capsys)
        assert secret not in err


def test_driver_bounds_actual_reads_even_if_stat_understates_size(versions, capsys, monkeypatch):
    driver, paths = versions
    monkeypatch.setattr(driver, "MAX_BYTES", 1024, raising=False)
    paths[2].write_text(rows("t4") + " " * 8192)
    real_fstat = os.fstat
    real_read = os.read
    target_inode = paths[2].stat().st_ino
    bytes_read = []

    def small_stat(fd):
        result = real_fstat(fd)
        values = {key: getattr(result, key) for key in dir(result) if key.startswith("st_")}
        values["st_size"] = 1
        return SimpleNamespace(**values)

    def observed_read(fd, size):
        data = real_read(fd, size)
        if real_fstat(fd).st_ino == target_inode:
            bytes_read.append(len(data))
        return data

    with monkeypatch.context() as patch:
        patch.setattr(os, "fstat", small_stat)
        patch.setattr(os, "read", observed_read)
        real_stat = os.stat

        def small_named_stat(*args, **kwargs):
            result = real_stat(*args, **kwargs)
            values = {key: getattr(result, key) for key in dir(result) if key.startswith("st_")}
            values["st_size"] = 1
            return SimpleNamespace(**values)

        patch.setattr(os, "stat", small_named_stat)
        assert_refusal(driver, paths, capsys)
    assert 0 < sum(bytes_read) <= 1025


def test_driver_refuses_unexpected_arguments_without_echoing_them(versions, capsys):
    driver, paths = versions
    before = paths[1].read_bytes()
    secret = "pass" + "word" + "=hidden-argument"
    assert driver.main([*driver_args(paths), secret]) == 2
    assert paths[1].read_bytes() == before
    captured = capsys.readouterr()
    assert not captured.out and secret not in captured.err


def test_driver_dependency_errors_and_refusals_never_echo_values(versions, capsys, monkeypatch):
    driver, paths = versions
    secret = "pass" + "word" + "=hidden-error"
    for error in (RuntimeError, ValueError, OSError, driver.Refused):
        def fail(*_args):
            raise error(secret)

        monkeypatch.setitem(driver.MERGES, "tests", fail)
        assert secret not in assert_refusal(driver, paths, capsys)


def test_driver_partial_write_failure_preserves_ours_and_cleans_stage(versions, capsys, monkeypatch):
    driver, paths = versions
    before_names = set(Path.cwd().iterdir())
    real_write = os.write
    calls = []

    def partial(fd, data):
        calls.append(len(data))
        if len(calls) == 1:
            return real_write(fd, data[:7])
        raise OSError("pass" + "word" + "=hidden-write")

    monkeypatch.setattr(os, "write", partial)
    err = assert_refusal(driver, paths, capsys)
    assert len(calls) == 2 and "hidden-write" not in err
    assert set(Path.cwd().iterdir()) == before_names


def test_driver_sync_and_publication_failures_preserve_ours(versions, capsys, monkeypatch):
    driver, paths = versions
    before_names = set(Path.cwd().iterdir())
    for operation in ("fsync", "replace"):
        def fail(*_args, **_kwargs):
            raise OSError("pass" + "word" + "=hidden-publication")

        with monkeypatch.context() as patch:
            patch.setattr(os, operation, fail)
            err = assert_refusal(driver, paths, capsys)
            assert "hidden-publication" not in err
        assert set(Path.cwd().iterdir()) == before_names


def test_driver_refuses_inputs_replaced_or_edited_during_merge(versions, capsys, monkeypatch):
    driver, paths = versions
    merge = driver.MERGES["tests"]
    for index in range(3):
        for replace in (False, True):
            for path, text in zip(paths, (rows("t1"), rows("t1", "t2"), rows("t1", "t3"))):
                path.write_text(text)
            before = paths[1].read_bytes()
            changed = rows("late")

            def edit(*texts):
                if replace:
                    paths[index].unlink()
                paths[index].write_text(changed)
                return merge(*texts)

            monkeypatch.setitem(driver.MERGES, "tests", edit)
            assert driver.main(driver_args(paths)) == 1
            assert paths[index].read_text() == changed
            if index != 1:
                assert paths[1].read_bytes() == before
            assert len(capsys.readouterr().err.splitlines()) == 1


def test_driver_link_swap_before_publication_never_writes_target(versions, capsys, monkeypatch, tmp_path):
    driver, paths = versions
    target = tmp_path / "external"
    target.write_text(rows("external"))
    before = target.read_bytes()
    merge = driver.MERGES["tests"]

    def swap(*texts):
        paths[1].unlink()
        paths[1].symlink_to(target)
        return merge(*texts)

    monkeypatch.setitem(driver.MERGES, "tests", swap)
    assert driver.main(driver_args(paths)) == 1
    assert target.read_bytes() == before and paths[1].is_symlink()
    assert len(capsys.readouterr().err.splitlines()) == 1


def test_keyed_merge_inserts_many_rows_with_linear_work():
    from awb import mergedriver

    probes = []

    class Key(str):
        def __hash__(self):
            probes.append(1)
            return super().__hash__()

    count = 1000
    base = {Key("base"): 0, Key("end"): 0}
    ours = {Key("base"): 0, Key("ours"): 1, Key("end"): 0}
    theirs = {Key("base"): 0, **{Key(str(i)): i for i in range(count)}, Key("end"): 0}
    probes.clear()
    result = mergedriver.merge_keyed(base, ours, theirs, lambda b, a, t: a)
    assert list(result) == ["base", "ours", *map(str, range(count)), "end"]
    assert len(probes) < 100 * count


def test_changelog_many_duplicate_items_finish_without_losing_occurrences(versions, tmp_path):
    import sys

    driver, paths = versions
    base = "# Changelog\n\n## Week 46, 2026\n### Fixed\n"
    for path in paths:
        path.write_text(base)
    paths[2].write_text(base + "- repeated item\n" * 30000)
    result = subprocess.run([sys.executable, "-I", driver.__file__, *driver_args(paths, "changelog")],
                            cwd=tmp_path, capture_output=True, text=True, timeout=5)
    assert result.returncode == 0 and result.stdout == "" and result.stderr == ""
    assert paths[1].read_text().count("- repeated item\n") == 30000


def test_driver_independent_outputs_can_merge_concurrently(versions, monkeypatch, tmp_path):
    import concurrent.futures
    import threading

    driver, paths = versions
    other = []
    for path in paths:
        copy = tmp_path / (path.name + "-second")
        copy.write_bytes(path.read_bytes())
        other.append(copy)
    barrier = threading.Barrier(2)
    merge = driver.MERGES["tests"]

    def synchronized(*texts):
        barrier.wait(timeout=5)
        return merge(*texts)

    monkeypatch.setitem(driver.MERGES, "tests", synchronized)
    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        jobs = [pool.submit(driver.main, driver_args(p)) for p in (paths, other)]
        assert [job.result(timeout=10) for job in jobs] == [0, 0]
    for output in (paths[1], other[1]):
        assert {r["id"] for r in json.loads(output.read_text())["tests"]} == {"t1", "t2", "t3"}
    assert not list(tmp_path.glob(".awb-merge-*"))


def test_existing_malformed_shapes_and_duplicate_headings_remain_refused(versions, capsys):
    driver, paths = versions
    for data in ([], None, {}, {"tests": {}}, {"tests": [None]}, {"tests": [{}]}, {"tests": [{"id": 1}]}):
        paths[2].write_text(json.dumps(data))
        assert_refusal(driver, paths, capsys)
    for text in ("## Week 46, 2026\n## Week 46, 2026\n",
                 "## Week 46, 2026\n### Added\n### Added\n"):
        for path in paths:
            path.write_text(text)
        assert_refusal(driver, paths, capsys, "changelog")


def test_driver_success_preserves_mode_newlines_count_and_later_date(versions, capsys):
    driver, paths = versions
    paths[1].chmod(0o640)
    paths[0].write_text("")
    paths[1].write_bytes(rows("t2").replace("\n", "\r\n").encode())
    paths[2].write_text(rows("t3").replace("2026-11-10", "2026-11-15"))
    assert driver.main(driver_args(paths)) == 0
    output = paths[1].read_bytes()
    data = json.loads(output)
    assert data["checked"] == "2026-11-15" and data["total_tests"] == len(data["tests"]) == 2
    assert [r["id"] for r in data["tests"]] == ["t2", "t3"]
    assert paths[1].stat().st_mode & 0o777 == 0o640
    assert b"\r" not in output and output.endswith(b"\n")
    assert capsys.readouterr() == ("", "")


def test_driver_parent_swap_cannot_redirect_publication(versions, monkeypatch, tmp_path):
    driver, paths = versions
    parent, outside = tmp_path / "inside", tmp_path / "outside"
    parent.mkdir()
    outside.mkdir()
    nested = []
    for path in paths:
        destination = parent / path.name
        destination.write_bytes(path.read_bytes())
        (outside / path.name).write_text(rows("external"))
        nested.append(destination)
    before = {p.name: p.read_bytes() for p in outside.iterdir()}
    merge = driver.MERGES["tests"]

    def swap(*texts):
        parent.rename(tmp_path / "moved")
        parent.symlink_to(outside, target_is_directory=True)
        return merge(*texts)

    monkeypatch.setitem(driver.MERGES, "tests", swap)
    assert driver.main(driver_args(nested)) in (0, 1)
    assert {p.name: p.read_bytes() for p in outside.iterdir()} == before


def test_driver_overlapping_runs_do_not_overwrite_each_other(versions, capsys, monkeypatch):
    import concurrent.futures
    import threading

    driver, paths = versions
    entered, release = threading.Event(), threading.Event()
    merge = driver.MERGES["tests"]

    def paused(*texts):
        entered.set()
        assert release.wait(5)
        return merge(*texts)

    monkeypatch.setitem(driver.MERGES, "tests", paused)
    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(driver.main, driver_args(paths))
        try:
            assert entered.wait(5)
            second = pool.submit(driver.main, driver_args(paths))
            assert second.result(timeout=2) == 1
        finally:
            release.set()
        assert first.result(timeout=5) == 0
    assert {r["id"] for r in json.loads(paths[1].read_text())["tests"]} == {"t1", "t2", "t3"}
    assert len(capsys.readouterr().err.splitlines()) == 1


def test_changelog_added_section_stays_after_week_introduction():
    from awb import mergedriver

    base = "## Week 46, 2026\nWeek introduction.\n### Added\n- existing\n"
    changed = base.replace("### Added", "### Fixed\n- repair\n### Added")
    for ours, theirs in ((base, changed), (changed, base)):
        assert mergedriver.merge_changelog(base, ours, theirs) == changed
