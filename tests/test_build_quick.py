"""build quick (UX15 of 2026-11-11): a short run before the full one.

The layering fault is planted in a scratch copy of the tree, a core module that imports the platform package. Names
come from tests/fixtures.py; none is needed here.
"""
from __future__ import annotations

import io
import shutil
import subprocess
import time
from pathlib import Path

import pytest

from awb import buildquick

ROOT = Path(__file__).resolve().parent.parent
RULE = "test_the_core_never_imports_the_platform_package"


def _git(root: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(root), "-c", "user.name=t", "-c", "user.email=t@example.invalid", *args],
                   check=True, capture_output=True, stdin=subprocess.DEVNULL)


@pytest.fixture
def copy(tmp_path):
    """A scratch git repository holding awb/ and tests/ as committed, so that a change shows in its diff."""
    root = tmp_path / "tree"
    ignore = shutil.ignore_patterns("__pycache__", "*.pyc")
    for part in ("awb", "tests", "rules"):
        shutil.copytree(ROOT / part, root / part, ignore=ignore)
    _git(root, "init", "-q", "-b", "main")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "base")
    return root


def _quick(root: Path) -> tuple[int, str, float]:
    out = io.StringIO()
    t0 = time.monotonic()
    code = buildquick.quick(root, "main", out)
    return code, out.getvalue(), time.monotonic() - t0


def test_a_core_module_that_imports_the_platform_package_fails_quick_in_under_two_minutes_and_names_the_test(copy):
    target = copy / "awb" / "codes.py"
    target.write_text(target.read_text(encoding="utf-8") + "\nimport awb.tcp.board  # planted\n", encoding="utf-8")
    code, text, seconds = _quick(copy)
    assert code == 1
    assert seconds < 120
    assert "red at import rules" in text
    assert "tests/test_price.py::" + RULE in text
    assert "quick: red in" in text


def test_with_the_fault_removed_quick_is_green_and_prints_the_files_it_ran_and_the_time(copy):
    target = copy / "awb" / "handover.py"
    target.write_text(target.read_text(encoding="utf-8") + "\n# a harmless change\n", encoding="utf-8")
    code, text, _ = _quick(copy)
    assert code == 0, text
    assert "tests/test_price.py::" + RULE in text
    assert "awb gate --selftest" in text
    assert "tests/test_vault.py" in text
    assert text.rstrip().splitlines()[-1].startswith("quick: green in ")


def test_a_change_in_one_module_runs_its_test_file_by_name_and_the_files_that_import_it():
    picked = buildquick.select(ROOT, ["awb/calc.py"])
    assert "tests/test_calc.py" in picked
    picked = buildquick.select(ROOT, ["awb/tcp/board.py"])
    assert "tests/test_board.py" in picked
    assert "tests/test_platform_status.py" in picked  # imports it as `from awb.tcp import board, platformstatus`
    assert "tests/test_calc.py" not in picked


def test_a_change_outside_the_modules_runs_no_module_test_and_a_changed_test_file_runs_as_itself():
    assert buildquick.select(ROOT, ["README.md", "docs/x.md"]) == []
    assert buildquick.select(ROOT, ["tests/test_calc.py"]) == ["tests/test_calc.py"]
    assert buildquick.module_of("awb/tcp/board.py") == "tcp.board"
    assert buildquick.module_of("awb/tcp/__init__.py") == "tcp"
    assert buildquick.module_of("tests/test_calc.py") is None


def test_an_unknown_ref_is_refused_with_exit_2(copy, capsys):
    assert buildquick.quick(copy, "no-such-ref", io.StringIO()) == 2
    assert "REF is not a branch or commit" in capsys.readouterr().err


def test_the_command_takes_an_optional_ref(monkeypatch):
    from awb import build

    seen = []
    monkeypatch.setattr(buildquick, "quick", lambda ref="main", **kw: seen.append(ref) or 0)
    assert build.main(["quick"]) == 0
    assert build.main(["quick", "pr-12"]) == 0
    assert seen == ["main", "pr-12"]
