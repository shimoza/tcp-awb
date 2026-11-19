"""Q-0028: a test that asserts how long something took carries @pytest.mark.wallclock, and the suite unit runs those
tests alone before the queue of four files at a time (a bound of 30 s took 32.7 s next to three other files, C-314).
The bound itself is never loosened."""
from __future__ import annotations

import ast
import tomllib
from pathlib import Path

from awb import build_unit_side as side

ROOT = Path(__file__).resolve().parent.parent
CLOCKS = {"monotonic", "perf_counter", "time", "monotonic_ns", "perf_counter_ns"}
ORDER = (ast.Lt, ast.LtE, ast.Gt, ast.GtE)


def _clock_call(node: ast.AST) -> bool:
    for x in ast.walk(node):
        if isinstance(x, ast.Call):
            f = x.func
            if (isinstance(f, ast.Attribute) and f.attr in CLOCKS and isinstance(f.value, ast.Name)
                    and f.value.id == "time"):
                return True
            if isinstance(f, ast.Name) and f.id in ("monotonic", "perf_counter"):
                return True
    return False


def _functions(tree: ast.Module):
    """Every top-level function and every method of a top-level class, with its own name."""
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            yield node
        elif isinstance(node, ast.ClassDef):
            yield from (n for n in node.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)))


def _instants(fn: ast.AST) -> set[str]:
    """The names that hold a reading of the clock (t0, started, deadline)."""
    return {y.id for x in ast.walk(fn) if isinstance(x, ast.Assign) and _clock_call(x.value)
            for t in x.targets for y in ast.walk(t) if isinstance(y, ast.Name)}


def _duration(node: ast.AST, instants: set[str], durations: set[str], helpers: set[str]) -> bool:
    """The expression holds a measured time: a difference with a clock reading on one side, a name that holds one or
    a call of a helper of the file that returns one. A deadline check (now < deadline) holds none."""
    def clock(side: ast.AST) -> bool:
        return _clock_call(side) or any(isinstance(y, ast.Name) and y.id in instants for y in ast.walk(side))

    for x in ast.walk(node):
        if isinstance(x, ast.BinOp) and isinstance(x.op, ast.Sub) and (clock(x.left) or clock(x.right)):
            return True
        if isinstance(x, ast.Name) and x.id in durations:
            return True
        if isinstance(x, ast.Call) and isinstance(x.func, ast.Name) and x.func.id in helpers:
            return True
    return False


def _durations(fn: ast.AST, helpers: set[str]) -> tuple[set[str], set[str]]:
    instants, names = _instants(fn), set()
    for _ in range(3):
        for x in ast.walk(fn):
            if isinstance(x, ast.Assign) and _duration(x.value, instants, names, helpers):
                for t in x.targets:
                    names |= {y.id for y in ast.walk(t) if isinstance(y, ast.Name)}
    return instants, names


def _bounds(fn: ast.AST, helpers: set[str]) -> list[int]:
    """The lines of the asserts that compare a measured time with a bound."""
    instants, names = _durations(fn, helpers)
    found = []
    for x in ast.walk(fn):
        if isinstance(x, ast.Assert):
            for c in ast.walk(x.test):
                if (isinstance(c, ast.Compare) and any(isinstance(o, ORDER) for o in c.ops)
                        and any(_duration(p, instants, names, helpers) for p in [c.left, *c.comparators])):
                    found.append(x.lineno)
                    break
    return sorted(found)


def _marked(fn: ast.FunctionDef) -> bool:
    return any("wallclock" in ast.unparse(d) for d in fn.decorator_list)


def wallclock_tests(path: Path) -> tuple[dict[str, tuple[bool, list[int]]], list[str]]:
    """The tests of a file that assert a wall-clock bound ({name: (marked, lines)}) and the helpers that do it
    outside a test (they would hide the bound from the marker)."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    functions = list(_functions(tree))
    helpers: set[str] = set()
    for _ in range(3):
        for fn in functions:
            instants, names = _durations(fn, helpers)
            if any(isinstance(x, ast.Return) and x.value is not None
                   and _duration(x.value, instants, names, helpers) for x in ast.walk(fn)):
                helpers.add(fn.name)
    tests, loose = {}, []
    for fn in functions:
        lines = _bounds(fn, helpers)
        if not lines:
            continue
        if fn.name.startswith("test"):
            tests[fn.name] = (_marked(fn), lines)
        else:
            loose.append("%s:%d %s" % (path.name, lines[0], fn.name))
    return tests, loose


def test_every_test_with_a_wall_clock_bound_carries_the_wallclock_marker():
    unmarked, loose, seen = [], [], 0
    for path in sorted((ROOT / "tests").glob("test_*.py")):
        tests, bad = wallclock_tests(path)
        loose += bad
        for name, (marked, lines) in tests.items():
            seen += 1
            if not marked:
                unmarked.append("%s:%d %s" % (path.name, lines[0], name))
    assert seen >= 25, "the finder found only %d wall-clock tests: it went blind" % seen
    assert not unmarked, "add @pytest.mark.wallclock to: " + ", ".join(unmarked)
    assert not loose, "a helper asserts a wall-clock bound, move the assert into the test: " + ", ".join(loose)


def test_the_finder_sees_the_bound_of_c314_and_the_usual_forms_and_skips_a_deadline_check(tmp_path):
    path = tmp_path / "test_x.py"
    path.write_text('''import time
import pytest


def _run():
    t0 = time.monotonic()
    return 0, time.monotonic() - t0


@pytest.mark.wallclock
def test_elapsed():
    started = time.monotonic()
    elapsed = time.monotonic() - started
    assert elapsed < 30


def test_inline():
    t0 = time.perf_counter()
    assert time.perf_counter() - t0 < 2


def test_through_a_helper():
    code, took = _run()
    assert took < 120


def test_wait_loop():
    deadline = time.monotonic() + 5
    while True:
        assert time.monotonic() < deadline
        break


def test_in_a_loop():
    for _ in range(3):
        t0 = time.monotonic()
        assert time.monotonic() - t0 < 1


def test_no_clock():
    assert 1 < 2


def helper_bound():
    t0 = time.monotonic()
    assert time.monotonic() - t0 < 1
''', encoding="utf-8")
    tests, loose = wallclock_tests(path)
    assert {n: m for n, (m, _) in tests.items()} == {"test_elapsed": True, "test_inline": False,
                                                       "test_through_a_helper": False, "test_in_a_loop": False}
    assert loose == ["test_x.py:46 helper_bound"]


def test_the_marker_is_registered_so_a_typo_is_no_silent_new_marker():
    conf = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["tool"]["pytest"]["ini_options"]
    assert any(m.startswith("wallclock:") for m in conf["markers"])


def test_the_suite_unit_runs_the_wallclock_tests_of_every_such_file_alone_and_the_rest_in_the_queue():
    files = side.suite_files(ROOT)
    alone, queue = side.suite_jobs(ROOT, files)
    for path in sorted((ROOT / "tests").glob("test_*.py")):
        tests, _ = wallclock_tests(path)
        if not tests:
            continue
        rel = "tests/%s" % path.name
        whole = (rel, [rel], rel) in alone
        split = any(a[0] == rel and a[1] == [rel, "-m", "wallclock"] for a in alone)
        assert whole or split, rel
        assert whole or any(q[0] == rel and q[1] == [rel, "-m", "not wallclock"] for q in queue), rel
