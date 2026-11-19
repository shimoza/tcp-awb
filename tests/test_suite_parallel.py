"""Q-0013: the whole suite in the suite unit runs as one pytest per test file, SUITE_WORKERS at a time, and the
junit files become one; the suite unit has its own CPU share and the longer clock."""
from __future__ import annotations

import os
import sys
import tarfile
import tempfile
from pathlib import Path

from awb import build_unit_side as side, build_units


def _tar(base: Path, files: dict[str, str]) -> Path:
    tree = base / "src"
    for rel, text in files.items():
        (tree / rel).parent.mkdir(parents=True, exist_ok=True)
        (tree / rel).write_text(text, encoding="utf-8")
    tar = base / "tree.tar"
    with tarfile.open(tar, "w") as t:
        for rel in files:
            t.add(tree / rel, arcname=rel)
    return tar


def _run(tmp_path: Path, files: dict[str, str], mode: str = "suite", tokens: tuple[str, ...] = ()):
    root = tmp_path / "root"
    (root / "wt").mkdir(parents=True)
    with open(_tar(tmp_path, files), "rb") as fin:
        rc = side.unit_suite(str(root), "B-012", "1", mode, sys.executable, list(tokens), fin)
    return rc, root / "wt" / "B-012-1.out"


ENV_TEST = '''import os


def test_env():
    open("seen-%s.txt" % os.path.basename(__file__), "w").write(os.environ["HOME"] + "\\n" + os.environ["TMPDIR"])
    assert os.path.basename(os.environ["HOME"]).startswith("home-")
    assert os.path.basename(os.environ["TMPDIR"]).startswith("awb-w")


def test_tmp_path(tmp_path):
    assert str(tmp_path).startswith(os.environ["TMPDIR"])
'''


def test_the_whole_suite_runs_one_pytest_per_file_with_a_home_and_tmpdir_per_worker_and_one_junit(tmp_path):
    before = set(Path(tempfile.gettempdir()).glob("awb-w*"))
    files = {"tests/test_a%d.py" % i: ENV_TEST for i in range(6)}
    files["tests/test_two.py"] = "def test_one():\n    pass\n\n\ndef test_two():\n    pass\n"
    rc, out = _run(tmp_path, files)
    assert rc == 0
    got = side.outcomes((out / "suite.xml").read_bytes())
    assert len(got) == 14 and set(got.values()) == {"passed"}
    assert got["tests/test_a3.py::test_tmp_path"] == "passed" and got["tests/test_two.py::test_two"] == "passed"
    log = (out / "suite.log").read_text(encoding="utf-8")
    assert "=== tests/test_two.py (exit 0" in log
    assert log.splitlines()[-1].startswith("=== the whole suite: 7 files on %d workers, 14 passed, 0 failed"
                                           % side.SUITE_WORKERS)
    assert set(Path(tempfile.gettempdir()).glob("awb-w*")) <= before


def test_a_failed_file_comes_last_in_the_log_with_a_failed_line_and_its_nodes_get_the_one_rerun(tmp_path):
    flaky = ('import os\n\n\ndef test_flaky():\n    first = not os.path.exists("flaky.mark")\n'
             '    open("flaky.mark", "w").close()\n    assert not first\n')
    rc, out = _run(tmp_path, {"tests/test_flaky.py": flaky, "tests/test_ok.py": "def test_ok():\n    pass\n"})
    assert rc == 1
    assert side.outcomes((out / "suite.xml").read_bytes()) == {"tests/test_flaky.py::test_flaky": "failed",
                                                               "tests/test_ok.py::test_ok": "passed"}
    assert side.outcomes((out / "rerun.xml").read_bytes()) == {"tests/test_flaky.py::test_flaky": "passed"}
    lines = (out / "suite.log").read_text(encoding="utf-8").splitlines()
    assert lines[-1] == "FAILED tests/test_flaky.py (exit 1)"
    assert lines[-2].startswith("=== the whole suite: 2 files on")
    assert [ln for ln in lines if ln.startswith("=== tests/")][-1].startswith("=== tests/test_flaky.py (exit 1")


def test_a_file_that_leaves_no_report_is_an_error_case_never_passed_or_gone(tmp_path):
    rc, out = _run(tmp_path, {"tests/test_crash.py": "import os\nos._exit(3)\n",
                              "tests/test_ok.py": "def test_ok():\n    pass\n"})
    assert rc == 1
    got = side.outcomes((out / "suite.xml").read_bytes())
    assert got["tests/test_ok.py::test_ok"] == "passed"
    assert got["tests/test_crash.py::(no report, exit 3)"] == "error"
    assert "FAILED tests/test_crash.py (exit 3)" in (out / "suite.log").read_text(encoding="utf-8")


def test_the_task_check_with_tokens_and_a_tree_without_test_files_keep_the_one_process(tmp_path):
    files = {"tests/test_ok.py": "def test_ok():\n    pass\n", "tests/test_other.py": "def test_x():\n    pass\n"}
    rc, out = _run(tmp_path / "a", files, mode="check", tokens=("tests/test_ok.py",))
    assert rc == 0
    assert side.outcomes((out / "check.xml").read_bytes()) == {"tests/test_ok.py::test_ok": "passed"}
    assert "=== the whole suite" not in (out / "check.log").read_text(encoding="utf-8")
    rc, out = _run(tmp_path / "b", {"check_me.py": "def test_y():\n    pass\n"})
    assert rc == 5 and "=== the whole suite" not in (out / "suite.log").read_text(encoding="utf-8")


def test_suite_files_are_the_plain_test_files_largest_first(tmp_path):
    tests = tmp_path / "tests"
    tests.mkdir()
    (tests / "test_small.py").write_text("x = 1\n")
    (tests / "test_big.py").write_text("x = 1\n" * 50)
    (tests / "helper.py").write_text("x = 1\n" * 90)
    (tests / "test_link.py").symlink_to(tests / "test_big.py")
    (tests / "test_dir.py").mkdir()
    assert side.suite_files(tmp_path) == ["tests/test_big.py", "tests/test_small.py"]
    assert side.suite_files(tmp_path / "none") == []


def test_merge_junit_counts_the_cases_of_every_file_and_reads_no_doctype(tmp_path):
    good = tmp_path / "good.xml"
    good.write_text('<testsuites><testsuite><testcase classname="tests.test_g" name="test_a" file="tests/test_g.py"/>'
                    '<testcase classname="tests.test_g" name="test_b" file="tests/test_g.py"><failure/></testcase>'
                    '<testcase classname="tests.test_g" name="test_c" file="tests/test_g.py"><skipped/></testcase>'
                    '</testsuite></testsuites>')
    evil = tmp_path / "evil.xml"
    evil.write_text('<!DOCTYPE x [<!ENTITY a "b">]><testsuites/>')
    data = side.merge_junit([("tests/test_g.py", 1, good), ("tests/test_e.py", 1, evil),
                             ("tests/test_m.py", 124, tmp_path / "missing.xml")], 1.5)
    assert side.outcomes(data) == {"tests/test_g.py::test_a": "passed", "tests/test_g.py::test_b": "failed",
                                   "tests/test_g.py::test_c": "skipped",
                                   "tests/test_e.py::(no report, exit 1)": "error",
                                   "tests/test_m.py::(no report, exit 124)": "error"}
    assert b'tests="5"' in data and b'failures="1"' in data and b'errors="2"' in data and b'skipped="1"' in data


def test_the_suite_unit_has_its_own_cpu_share_and_the_longer_clock_and_the_other_units_keep_theirs():
    assert build_units.SUITE_SECONDS == 2700 and build_units.Host.suite_seconds == 2700
    assert build_units.CPU_QUOTA == "200%" and build_units.SUITE_CPU_QUOTA == "400%"
    units = build_units.SystemdUnits("awb-build")
    plain = units.argv(build_units.Spec("awb-task-B-012", ["x"], 60, "/tmp", "/tmp"))
    suite = units.argv(build_units.Spec("awb-task-B-012-suite", ["x"], 60, "/tmp", "/tmp",
                                        cpu=build_units.SUITE_CPU_QUOTA))
    assert "--property=CPUQuota=200%" in plain and "--property=CPUQuota=400%" in suite


def test_a_file_with_its_own_process_pool_runs_alone_before_the_queue(tmp_path):
    order = tmp_path / "order.txt"
    stamp = ('import os, time\nSTART = time.time()\n\n\ndef test_t():\n'
             '    open(%r, "a").write("%%s %%.3f %%.3f\\n" %% (os.path.basename(__file__), START, time.time()))\n'
             % str(order))
    files = {"tests/test_q%d.py" % i: stamp for i in range(4)}
    files["tests/test_pool.py"] = "# multiprocessing\nimport time\ntime.sleep(0.5)\n" + stamp
    rc, out = _run(tmp_path, files)
    assert rc == 0
    rows = {r.split()[0]: (float(r.split()[1]), float(r.split()[2])) for r in order.read_text().splitlines()}
    assert len(rows) == 5
    assert all(rows["test_pool.py"][1] < start for name, (start, _) in rows.items() if name != "test_pool.py")


def test_the_wallclock_tests_of_a_file_run_alone_before_the_queue_and_the_rest_of_the_file_in_the_queue(tmp_path):
    """Q-0028: a bound of 30 s took 32.7 s next to three other files (C-314); the marked tests go alone, the rest of
    their file still runs in the queue and both halves land in the one junit."""
    order = tmp_path / "order.txt"
    stamp = ('import os, time\nimport pytest\n\n\ndef _stamp(name):\n'
             '    open(%r, "a").write("%%s %%.3f %%.3f\\n" %% (name, time.time(), time.time()))\n\n\n'
             'def test_t():\n    time.sleep(0.2)\n    _stamp(os.path.basename(__file__))\n' % str(order))
    files = {"tests/test_q%d.py" % i: stamp for i in range(4)}
    files["tests/test_clock.py"] = stamp + ('\n\n@pytest.mark.wallclock\ndef test_bound():\n'
                                            '    time.sleep(0.5)\n    _stamp("bound")\n')
    files["pyproject.toml"] = '[tool.pytest.ini_options]\nmarkers = ["wallclock: alone"]\n'
    rc, out = _run(tmp_path, files)
    assert rc == 0
    rows = {r.split()[0]: float(r.split()[1]) for r in order.read_text().splitlines()}
    assert set(rows) == {"bound", "test_clock.py", "test_q0.py", "test_q1.py", "test_q2.py", "test_q3.py"}
    assert all(rows["bound"] < t for name, t in rows.items() if name != "bound")
    got = side.outcomes((out / "suite.xml").read_bytes())
    assert got["tests/test_clock.py::test_bound"] == "passed" and got["tests/test_clock.py::test_t"] == "passed"
    assert len(got) == 6
    log = (out / "suite.log").read_text(encoding="utf-8")
    assert "=== tests/test_clock.py (wall-clock tests, alone) (exit 0" in log
    assert log.splitlines()[-1].startswith("=== the whole suite: 5 files on")


def test_a_file_over_its_own_clock_is_a_red_file_and_the_others_still_report(tmp_path, monkeypatch):
    monkeypatch.setattr(side, "FILE_SECONDS", 3)
    rc, out = _run(tmp_path, {"tests/test_hang.py": "import time\n\n\ndef test_hang():\n    time.sleep(30)\n",
                              "tests/test_ok.py": "def test_ok():\n    pass\n"})
    assert rc == 1
    got = side.outcomes((out / "suite.xml").read_bytes())
    assert got["tests/test_ok.py::test_ok"] == "passed"
    assert got["tests/test_hang.py::(no report, exit 124)"] == "error"


def test_the_tmpdir_of_a_worker_is_shorter_than_the_suite_folder_so_a_unix_socket_fits(tmp_path):
    seen = tmp_path / "seen.txt"
    test = ('import os, socket\n\n\ndef test_sock():\n    open(%r, "w").write(os.environ["TMPDIR"])\n'
            '    s = socket.socket(socket.AF_UNIX)\n    s.bind(os.path.join(os.environ["TMPDIR"], "x.sock"))\n'
            '    s.close()\n' % str(seen))
    rc, out = _run(tmp_path, {"tests/test_sock.py": test})
    assert rc == 0
    worker = seen.read_text()
    assert len(worker) < len(os.path.join(tempfile.gettempdir(), "awb-suite-xxxxxxxx"))
    assert not os.path.exists(worker)
