"""The suite runs from any path, read-only, with a shared venv (presentations/self-build/TASKS.md, SB0).

The runner of the self-build runs the whole suite as its build user on a `git archive` export it cannot write, from
a folder outside every home, with a venv by absolute path that has the test extras and not the package:
`<venv>/bin/python -I -B -m pytest` with the cache provider and bytecode writing off. These tests run small parts
of that in a subprocess: the two self-tests from a folder whose path holds `-home-`, three fast modules from a
read-only copy, the venv recipe of seal/setup.sh as a dry run."""
from __future__ import annotations

import os
import shutil
import stat
import subprocess
import sys
import tomllib
from pathlib import Path

import pytest

from tests.conftest import ROOT, SUITE_PYTHON

HOMELIKE = "-" + "home" + "-builder-wt"
"""A folder named the way the client names its project folders, which the gate reads as a home path."""
FAST_MODULES = ("tests/test_words.py", "tests/test_normalize.py", "tests/test_questions.py")
BOOT = "import sys; sys.path.insert(0, sys.argv[1]); from awb.cli import main; sys.exit(main(sys.argv[2:]))"
"""`awb ARGS` from the tree at argv[1] with an isolated interpreter: -I takes neither PYTHONPATH nor the folder."""


def tree_files() -> list[str]:
    """The files of the tree as an export holds them: what git would commit, or every file of an export."""
    if (ROOT / ".git").exists():
        out = subprocess.run(["git", "-C", str(ROOT), "ls-files", "-z", "--cached", "--others", "--exclude-standard"],
                             capture_output=True, check=True, timeout=60).stdout.decode("utf-8")
        return sorted(f for f in out.split("\0") if f and (ROOT / f).is_file())
    return sorted(p.relative_to(ROOT).as_posix() for p in ROOT.rglob("*")
                  if p.is_file() and "__pycache__" not in p.parts)


def read_only_copy(dst: Path) -> Path:
    """The tree copied to DST (no .venv, no .git) and every file and folder of it made read-only."""
    for rel in tree_files():
        (dst / rel).parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(ROOT / rel, dst / rel)
        os.chmod(dst / rel, os.stat(ROOT / rel).st_mode & 0o555)
    for folder, _, _ in os.walk(dst):
        os.chmod(folder, 0o555)
    return dst


def writable_again(top: Path) -> None:
    for folder, _, _ in os.walk(top):
        os.chmod(folder, os.stat(folder).st_mode | stat.S_IWUSR)


def snapshot(top: Path) -> dict[str, tuple[int, int]]:
    out = {}
    for folder, dirs, files in os.walk(top):
        for name in dirs + files:
            p = Path(folder) / name
            st = p.lstat()
            out[p.relative_to(top).as_posix()] = (st.st_size, st.st_mtime_ns)
    return out


def isolated_env(tmpdir: Path) -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if not k.startswith("PYTHON")}
    env.update(PYTHONDONTWRITEBYTECODE="1", TMPDIR=str(tmpdir))
    return env


@pytest.fixture
def ro_tree(tmp_path):
    made = []

    def make(parent: Path) -> Path:
        tree = read_only_copy(parent / "tcp-awb")
        made.append(parent)
        return tree

    yield make
    for parent in made:
        writable_again(parent)


def test_the_copy_is_read_only_and_holds_no_venv(ro_tree, tmp_path):
    tree = ro_tree(tmp_path / "ro")
    assert (tree / "awb" / "cli.py").is_file() and (tree / "tests" / "conftest.py").is_file()
    assert not (tree / ".venv").exists() and not (tree / ".git").exists()
    if os.geteuid() != 0:
        with pytest.raises(PermissionError):
            (tree / "planted.txt").write_text("x", encoding="utf-8")


@pytest.mark.timeout(300)
def test_the_gate_and_hook_selftests_pass_from_a_folder_whose_path_holds_home(ro_tree, tmp_path):
    base = tmp_path / HOMELIKE
    tree = ro_tree(base)
    tmpdir = base / "tmp-of-the-run"
    tmpdir.mkdir()
    for args, said in ((["gate", "--selftest"], "selftest passed"), (["hook", "selftest"], "PASS")):
        res = subprocess.run([SUITE_PYTHON, "-I", "-B", "-c", BOOT, str(tree), *args], capture_output=True, text=True,
                             cwd=tree, env=isolated_env(tmpdir), timeout=240, stdin=subprocess.DEVNULL)
        assert res.returncode == 0, (args, res.stdout[-2000:], res.stderr[-2000:])
        assert said in res.stdout, args


@pytest.mark.timeout(600)
def test_three_fast_modules_pass_from_a_read_only_copy_with_the_interpreter_by_absolute_path(ro_tree, tmp_path):
    tree = ro_tree(tmp_path / "export")
    tmpdir = tmp_path / "tmp-of-the-run"
    tmpdir.mkdir()
    assert os.path.isabs(SUITE_PYTHON)
    before = snapshot(tree)
    res = subprocess.run([SUITE_PYTHON, "-I", "-B", "-m", "pytest", "-p", "no:cacheprovider", *FAST_MODULES],
                         capture_output=True, text=True, cwd=tree, env=isolated_env(tmpdir), timeout=540,
                         stdin=subprocess.DEVNULL)
    assert res.returncode == 0, (res.stdout[-3000:], res.stderr[-2000:])
    assert " passed" in res.stdout and " failed" not in res.stdout
    assert snapshot(tree) == before, "the run wrote into the read-only copy"


def test_the_tree_interpreter_runs_the_code_under_test():
    """A child started as `sys.executable -I -m awb` imports this tree: through the editable install of a checkout's
    venv or through the tree interpreter of tests/conftest.py when the suite's venv has no package."""
    res = subprocess.run([sys.executable, "-I", "-B", "-c", "import awb, os; print(os.path.realpath(awb.__path__[0]))"],
                         capture_output=True, text=True, cwd="/", timeout=60, stdin=subprocess.DEVNULL)
    assert res.returncode == 0, res.stderr
    assert res.stdout.strip() == os.path.realpath(ROOT / "awb")


def test_the_venv_recipe_of_setup_takes_the_test_extras_and_no_package():
    setup = ROOT / "seal" / "setup.sh"
    with open(ROOT / "pyproject.toml", "rb") as f:
        extras = tomllib.load(f)["project"]["optional-dependencies"]["test"]
    # a fresh host (D-SB1-14): the venv may exist on the host the suite runs on
    res = subprocess.run(["bash", str(setup), "--dry-run", "--suite-venv", "/srv/awb-build/venv"], capture_output=True,
                         text=True, timeout=60, env=dict(os.environ, SUDO_USER="", AWB_SETUP_BUILD_HOST="fresh"),
                         stdin=subprocess.DEVNULL)
    assert res.returncode == 0, res.stderr
    lines = [line for line in res.stdout.splitlines() if line.startswith("+ ")]
    assert lines[0] == "+ python3 -m venv --system-site-packages /srv/awb-build/venv"
    assert lines[1] == ("+ /srv/awb-build/venv/bin/python -I -m pip install --no-cache-dir "
                        "--disable-pip-version-check " + " ".join(extras))
    assert len(lines) == 2 and "tcp-awb" not in res.stdout and " -e " not in res.stdout
    for name in ("pytest", "pytest-timeout", "python-docx", "python-pptx", "openpyxl", "reportlab"):
        assert name in extras


@pytest.mark.parametrize("where", ["relative/venv", "/ro" + "ot/venv", "/srv/../" + "ho" + "me/x/venv", "HOME", "REPO"])
def test_the_venv_recipe_refuses_a_home_the_repository_and_a_relative_path(where):
    target = {"HOME": str(Path.home() / "venv"), "REPO": str(ROOT / "venv")}.get(where, where)
    res = subprocess.run(["bash", str(ROOT / "seal" / "setup.sh"), "--dry-run", "--suite-venv", target],
                         capture_output=True, text=True, timeout=60, stdin=subprocess.DEVNULL)
    assert res.returncode != 0 and "+ python3" not in res.stdout, (where, res.stdout)
