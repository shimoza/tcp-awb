"""`awb build quick [REF]` (UX15): a short test run before the full one.

It runs, in this order and stopping at the first red stage: the import-rule tests, the gate selftest and the test
files of the changed modules. A changed module is an `awb/**.py` file that differs from the merge base with REF
(default `main`) in the commits, the working tree or as an untracked file. Its test files are `tests/test_<module>*.py`
by the name of the file and every test file that imports the module (found by reading the files). A changed test file
runs as itself. The files it ran and the time are printed. It starts no model session and writes nothing.
"""
from __future__ import annotations

import os
import re
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

IMPORT_RULES = ("tests/test_price.py::test_the_core_never_imports_the_platform_package",)
"""The node ids of the layering tests, first because a layering fault is the dearest to find late."""

STAGE_LIMIT = 900


def _git(root: Path, *args: str) -> list[str] | None:
    try:
        out = subprocess.run(["git", "-C", str(root), *args], capture_output=True, text=True, timeout=60,
                             stdin=subprocess.DEVNULL)
    except (OSError, subprocess.SubprocessError):
        return None
    if out.returncode != 0:
        return None
    return [ln for ln in out.stdout.splitlines() if ln]


def changed_files(root: Path, ref: str = "main") -> list[str] | None:
    """The paths changed since the merge base with REF, in the working tree and untracked; None when REF is unknown."""
    base = _git(root, "merge-base", ref, "HEAD")
    if not base:
        return None
    diff = _git(root, "diff", "--name-only", "--no-renames", base[0])
    new = _git(root, "ls-files", "--others", "--exclude-standard")
    if diff is None or new is None:
        return None
    return sorted(set(diff) | set(new))


def module_of(path: str) -> str | None:
    """`awb/tcp/board.py` gives `tcp.board`, `awb/tcp/__init__.py` gives `tcp`; anything else gives None."""
    p = Path(path)
    if p.suffix != ".py" or p.parts[:1] != ("awb",) or len(p.parts) < 2:
        return None
    parts = list(p.with_suffix("").parts[1:])
    if parts[-1] == "__init__":
        parts.pop()
    return ".".join(parts) or None


def _imports(module: str) -> re.Pattern:
    parent, _, leaf = module.rpartition(".")
    head = r"awb\." + re.escape(parent) if parent else "awb"
    dotted = re.escape(module)
    return re.compile(
        r"^[ \t]*(?:from[ \t]+awb\.%s(?:\.\w+)*[ \t]+import\b"
        r"|import[ \t]+awb\.%s\b"
        r"|from[ \t]+%s[ \t]+import\b[^\n]*?\b%s\b)" % (dotted, dotted, head, re.escape(leaf)), re.M)


def tests_for(root: Path, modules: list[str]) -> list[str]:
    """The test files of the modules: by name `tests/test_<last part>*.py` and by import."""
    tests = sorted((root / "tests").glob("test_*.py"))
    found: set[str] = set()
    for module in modules:
        leaf = module.rpartition(".")[2]
        pat = _imports(module)
        for t in tests:
            if t.name.startswith("test_" + leaf) or pat.search(t.read_text(encoding="utf-8", errors="replace")):
                found.add(t.relative_to(root).as_posix())
    return sorted(found)


def select(root: Path, files: list[str]) -> list[str]:
    modules = [m for m in map(module_of, files) if m]
    own = [f for f in files if re.fullmatch(r"tests/test_\w+\.py", f) and (root / f).is_file()]
    return sorted(set(own) | set(tests_for(root, modules)))


def _run(root: Path, argv: list[str]) -> tuple[int, str]:
    env = dict(os.environ, PYTHONPATH=str(root), PYTHONDONTWRITEBYTECODE="1")
    try:
        out = subprocess.run(argv, cwd=str(root), env=env, capture_output=True, text=True, timeout=STAGE_LIMIT,
                             stdin=subprocess.DEVNULL)
    except subprocess.TimeoutExpired:
        return 1, "timed out after %d s" % STAGE_LIMIT
    except OSError as err:
        return 1, "cannot start (%s)" % (err.strerror or "error").lower()
    return out.returncode, out.stdout + out.stderr


def _pytest(root: Path, targets: list[str]) -> tuple[int, str]:
    return _run(root, [sys.executable, "-B", "-m", "pytest", "-q", "-rf", "-p", "no:cacheprovider", *targets])


def _failed(text: str) -> list[str]:
    return [ln[len("FAILED "):].split(" - ")[0] for ln in text.splitlines() if ln.startswith("FAILED ")]


def quick(root: Path = ROOT, ref: str = "main", out=None) -> int:
    """Exit 0 green, 1 red, 2 REF unknown."""
    out = out or sys.stdout
    t0 = time.monotonic()
    files = changed_files(root, ref)
    if files is None:
        print("awb build quick: REF is not a branch or commit of this working copy", file=sys.stderr)
        return 2
    chosen = select(root, files)
    stages = [("import rules", lambda: _pytest(root, list(IMPORT_RULES)), list(IMPORT_RULES)),
              ("gate selftest", lambda: _run(root, [sys.executable, "-B", "-m", "awb", "gate", "--selftest"]),
               ["awb gate --selftest"])]
    if chosen:
        stages.append(("changed modules", lambda: _pytest(root, chosen), chosen))
    ran: list[str] = []
    code = 0
    for name, go, what in stages:
        code, text = go()
        ran += what
        if code != 0:
            named = _failed(text)
            last = (text.strip().splitlines() or ["no output"])[-1]
            print("quick: red at %s: %s" % (name, ", ".join(named) if named else last), file=out)
            break
    print("quick: ran %s" % " ".join(ran), file=out)
    if not chosen:
        print("quick: no changed module has a test file", file=out)
    print("quick: %s in %.1f s" % ("green" if code == 0 else "red", time.monotonic() - t0), file=out)
    return 0 if code == 0 else 1
