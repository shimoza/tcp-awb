"""The relative path of a folder import (tcp-7c7b ask 2026-11-27-tcp-7c7b-1).

`awb import CODE DIR` of a code tree wrote copies that carried only the file id and the kind, so the project could not
rebuild the tree. The copy now carries the path below the folder (`- path:`), wiped like the content: a piece that is a
name becomes its token, a registered form its code. The public report lists it, the private report keeps the path as
written, and `--redo` writes the same path again. The tree and its names are invented (tests/fixtures.py).
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

import awb
from awb import bucket, importcmd, intake, projects, start
from tests import fixtures as fx

ROOT = Path(awb.__file__).resolve().parent.parent
GOAL = "move two app clusters to managed k8s"
PERSON_DIR = fx.PLANTED_PERSON.replace(" ", "_")      # a folder named after a person
FORM_DIR = fx.CUSTOMER_FORMS[1]                        # a folder named like the customer
CODE = "def main():\n    return 42\n"


@pytest.fixture(autouse=True)
def owner_terminal(monkeypatch):
    monkeypatch.delenv("CLAUDECODE", raising=False)
    monkeypatch.delenv("CLAUDE_CODE_ENTRYPOINT", raising=False)
    monkeypatch.setattr(bucket, "client", lambda: (_ for _ in ()).throw(bucket.BucketError("no bucket key here")))


@pytest.fixture
def project(home, register_path, monkeypatch):
    monkeypatch.setenv("PYTHONPATH", str(ROOT))
    return projects.spawn(home, "engagement", GOAL, fx.CUSTOMER_CODE, register_path)


def _tree(root: Path) -> Path:
    """An invented code tree: a plain path, a folder named after a person and one named like the customer."""
    for rel in ("src/app/main.py", "docs/%s/notes.md" % PERSON_DIR, "%s/deploy/run.py" % FORM_DIR):
        f = root / rel
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(CODE if rel.endswith(".py") else "Notes on the build.\n", encoding="utf-8")
    return root


def _paths(home) -> dict[str, str]:
    """file id -> the path line of its copy."""
    out = {}
    for f in sorted((home.outbox / fx.CUSTOMER_CODE).glob("F-*.md")):
        m = re.search(r"(?m)^- path: (.*)$", f.read_text(encoding="utf-8"))
        out[f.stem] = m.group(1) if m else None
    return out


def test_a_folder_import_carries_the_wiped_relative_path_in_each_copy(home, project, tmp_path, capsys):
    tree = _tree(tmp_path / "repo")
    files, paths = importcmd._expand_paths([str(tree)])
    assert sorted(paths.values()) == sorted(["src/app/main.py", "docs/%s/notes.md" % PERSON_DIR,
                                             "%s/deploy/run.py" % FORM_DIR])
    assert importcmd.run(home, project.code, files, paths=paths) == 0
    lines = sorted(_paths(home).values())
    assert len(lines) == 3 and None not in lines
    assert "src/app/main.py" in lines
    assert "%s/deploy/run.py" % fx.CUSTOMER_CODE in lines, "a registered form in a folder becomes the code"
    person = [x for x in lines if x.startswith("docs/")]
    assert person and re.fullmatch(r"docs/\[(?:person|name) \d+\]/notes\.md", person[0]), person
    every = "\n".join((home.outbox / fx.CUSTOMER_CODE / f).read_text(encoding="utf-8")
                      for f in ("intake-report.md",)) + "\n".join(lines)
    for word in fx.PLANTED_PERSON.split() + [FORM_DIR]:
        assert word not in every, "a name of the tree reached a copy or the public report"
    out = capsys.readouterr()
    assert PERSON_DIR not in out.out + out.err


def test_the_public_report_lists_the_wiped_path_of_each_file(home, project, tmp_path):
    tree = _tree(tmp_path / "repo")
    files, paths = importcmd._expand_paths([str(tree)])
    importcmd.run(home, project.code, files, paths=paths)
    public = (home.outbox / fx.CUSTOMER_CODE / "intake-report.md").read_text(encoding="utf-8")
    assert "## Paths" in public
    for fid, line in _paths(home).items():
        assert "| %s | %s |" % (fid, line) in public


def test_the_private_report_keeps_the_path_as_written(home, project, tmp_path):
    tree = _tree(tmp_path / "repo")
    files, paths = importcmd._expand_paths([str(tree)])
    importcmd.run(home, project.code, files, paths=paths)
    rows, kept = importcmd._last_report(home, fx.CUSTOMER_CODE)
    assert len(rows) == 3
    assert sorted(kept.values()) == sorted(paths.values())


def test_redo_writes_the_same_path_again(home, project, tmp_path):
    tree = _tree(tmp_path / "repo")
    files, paths = importcmd._expand_paths([str(tree)])
    importcmd.run(home, project.code, files, paths=paths)
    before = _paths(home)
    assert importcmd.run(home, project.code, [], redo=True) == 0
    assert _paths(home) == before
    # and a second redo reads the path from the report of the first one
    assert importcmd.run(home, project.code, [], redo=True) == 0
    assert _paths(home) == before


def test_a_file_given_alone_carries_no_path(home, project, tmp_path):
    f = tmp_path / "alone.txt"
    f.write_text("Notes on the build.\n", encoding="utf-8")
    files, paths = importcmd._expand_paths([str(f)])
    assert paths == {}
    importcmd.run(home, project.code, files, paths=paths)
    assert list(_paths(home).values()) == [None]
    public = (home.outbox / fx.CUSTOMER_CODE / "intake-report.md").read_text(encoding="utf-8")
    assert "## Paths" not in public


def test_a_path_with_a_dot_dot_piece_is_no_path():
    assert intake._relative_path("src/../etc/x") == ""
    assert intake._relative_path("a\nb") == ""
    assert intake._relative_path("src\\app\\x.py") == "src/app/x.py"


def test_awb_start_from_passes_the_paths_of_its_folder(tmp_path):
    tree = _tree(tmp_path / "repo")
    found, _ = start.walk_from(str(tree))
    paths = importcmd.folder_files(tree, found)
    assert "src/app/main.py" in paths.values()
    assert all(not v.startswith("/") for v in paths.values())
