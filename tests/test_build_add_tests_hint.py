"""build add (B-103): after the task is queued, one line names every test file that imports a module of `paths`, and
one more line asks for `tests_replaced` when it is none. A plain scan of the import lines, in a stand-in repository."""
from __future__ import annotations

from pathlib import Path

import pytest

from awb import build

TEMPLATE = """\
## B-012: the doc fetch keeps the query
model: opus
effort: medium
depends: none
paths: awb/tcp/doc.py
tests_replaced: {replaced}
check: tests/test_doc.py
budget: 10
clock: 90
---
Keep the query of a redirect.
"""

LINE = "tests that import the modules of paths:"
ASK = "if the task changes a behaviour these tests assert, name them in tests_replaced"


@pytest.fixture
def repo(tmp_path, monkeypatch):
    root = tmp_path / "repo"
    (root / "tests" / "sub").mkdir(parents=True)
    (root / "build" / "queue").mkdir(parents=True)
    (root / "tests" / "test_doc.py").write_text("from awb.tcp import doc\n", encoding="utf-8")
    (root / "tests" / "test_other.py").write_text("from awb.tcp import docs\nimport awb.tcp.cloud\n", encoding="utf-8")
    (root / "tests" / "test_plain.py").write_text("def test_x():\n    pass\n", encoding="utf-8")
    (root / "tests" / "sub" / "test_deep.py").write_text("import awb.tcp.doc as d\n", encoding="utf-8")
    monkeypatch.setenv("AWB_BUILD_QUEUE", str(root / "build" / "queue"))
    monkeypatch.setenv("AWB_BUILD_RUNS", str(tmp_path / "runs"))
    return root


def add(tmp_path: Path, replaced: str, register: Path, capsys) -> list[str]:
    f = tmp_path / "task.md"
    f.write_text(TEMPLATE.format(replaced=replaced), encoding="utf-8")
    assert build.cmd_add(str(f), register=register) == 0
    return capsys.readouterr().out.splitlines()


def test_add_lists_the_tests_that_import_the_modules_of_paths(repo, tmp_path, register_path, capsys):
    out = add(tmp_path, "none", register_path, capsys)
    line = next(ln for ln in out if ln.startswith(LINE))
    assert line.split(":", 1)[1].split() == ["tests/sub/test_deep.py", "tests/test_doc.py"]
    assert ASK in out


def test_add_with_tests_replaced_prints_no_second_line(repo, tmp_path, register_path, capsys):
    out = add(tmp_path, "tests/test_doc.py::test_redirect", register_path, capsys)
    assert any(ln.startswith(LINE) for ln in out)
    assert ASK not in out
