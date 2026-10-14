"""awb/builddraft.py: `awb build draft FINDING` writes an add file from a row of a findings table, in a throw-away
working copy with a small findings file; `awb build add` takes it as it is."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from awb import build, builddraft

FINDINGS = """\
# review of the test tree

## summary

| ID | Severity | Place | Finding | State |
|----|----------|-------|---------|-------|
| X1 | low-medium | `awb/obs.py:104` | an answer with a doctype was parsed | open |
| X2 | medium | `awb/tcp/cloud.py:175,178` and `README.md` | the key service read an answer of any size | open |
| D1 | note | checks 9 to 11 of the runner | a task's own test code can forge its green | open, design |

## open

### X1, low-medium: an answer with a doctype was parsed

The parser took a doctype and expanded its entities.
Fix: refuse a doctype before the parse.

#### a note inside the section

It stays with the section.

### X2, medium: the key service read an answer of any size

Fix: read at most a bound and refuse the rest.
"""

TABLE_ONLY = """\
| # | gap | impact | likelihood | control | host |
|---|---|---|---|---|---|
| G5 | a parser runs under the owner's uid | high | low | a transient unit | yes |
"""

ROWS = [
    {"id": "X1-OLD", "phase": "X", "name": "the parser refuses a doctype",
     "run": ".venv/bin/pytest tests/test_obs.py::test_doctype", "status": "passing"},
    {"id": "X1-ALL", "phase": "X", "name": "the whole file",
     "run": ".venv/bin/pytest tests/test_obs.py", "status": "passing"},
    {"id": "OTHER", "phase": "X", "name": "another file",
     "run": ".venv/bin/pytest tests/test_obsolete.py", "status": "passing"},
]


@pytest.fixture
def tree(tmp_path, monkeypatch):
    root = tmp_path / "wc"
    for rel in ("awb/obs.py", "awb/tcp/cloud.py", "tests/test_obs.py", "tests/test_obsolete.py", "README.md"):
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_text("# stand-in\n", encoding="utf-8")
    (root / "docs").mkdir()
    (root / "docs/review.md").write_text(FINDINGS, encoding="utf-8")
    (root / "docs/gaps.md").write_text(TABLE_ONLY, encoding="utf-8")
    (root / "build").mkdir()
    (root / "build/tests.json").write_text(json.dumps({"tests": ROWS, "total_tests": 3}), encoding="utf-8")
    q, r = tmp_path / "queue", tmp_path / "runs"
    q.mkdir()
    monkeypatch.setenv("AWB_BUILD_QUEUE", str(q))
    monkeypatch.setenv("AWB_BUILD_RUNS", str(r))
    monkeypatch.delenv("CLAUDECODE", raising=False)
    monkeypatch.delenv("CLAUDE_CODE_ENTRYPOINT", raising=False)
    monkeypatch.chdir(root)
    return root, q, r


def _split(text: str) -> tuple[list[str], str]:
    head, prompt = build.split_add_file(text)
    return head.split("\n"), prompt


def test_the_draft_takes_the_row_and_its_section_into_the_block_and_the_prompt(tree):
    text = builddraft.make("X1", "docs/review.md", tid="B-031")
    head, prompt = _split(text)
    assert head[0] == "## B-031: an answer with a doctype was parsed (X1)"
    assert "paths: awb/obs.py tests/test_obs.py docs/review.md" in head
    assert "check: tests/test_obs.py" in head
    assert "tests_replaced: none" in head
    assert "model: opus" in head and "depends: none" in head
    assert not any(ln.startswith(("prompt:", "end:")) for ln in head)
    assert "| X1 | low-medium | `awb/obs.py:104` | an answer with a doctype was parsed | open |" in prompt
    assert "Fix: refuse a doctype before the parse." in prompt and "It stays with the section." in prompt
    assert "Fix: read at most a bound" not in prompt          # the next finding's section is not taken
    assert ("Defaults, do not ask: where the task leaves a choice take the default named here or else the smallest "
            "change and name the choice in your result.") in prompt
    for rule in ("fail before the fix", "fixed, <short hash of the fix commit>", "CHANGELOG.md", "build/tests.json",
                 "B031-1", "tests/fixtures.py", "no co-author line"):
        assert rule in prompt, rule


def test_the_comment_lines_list_the_rows_whose_run_names_a_touched_test_file(tree):
    head, _ = _split(builddraft.make("X1", "docs/review.md", tid="B-031"))
    comments = [ln for ln in head if ln.startswith("# ")]
    assert "# X1-OLD  .venv/bin/pytest tests/test_obs.py::test_doctype" in comments
    assert "# X1-ALL  .venv/bin/pytest tests/test_obs.py" in comments
    assert not any("OTHER" in ln for ln in comments)               # test_obsolete.py is another file
    assert head.index(comments[-1]) < head.index("tests_replaced: none")


def test_the_draft_never_lists_an_always_allowed_file_and_cuts_the_line_numbers(tree):
    head, _ = _split(builddraft.make("X2", "docs/review.md", tid="B-032"))
    paths = next(ln for ln in head if ln.startswith("paths:")).split()[1:]
    assert paths == ["awb/tcp/cloud.py", "docs/review.md"]           # README.md left out, no test_cloud.py here
    assert "check: none" in head
    assert "# tests_replaced: no row of build/tests.json names a test file of paths." in head


def test_a_place_without_a_path_keeps_the_document_and_a_finding_without_a_section_says_so(tree):
    head, prompt = _split(builddraft.make("D1", "docs/review.md", tid="B-033"))
    assert "paths: docs/review.md" in head
    head, prompt = _split(builddraft.make("G5", "docs/gaps.md", tid="B-034"))
    assert head[0] == "## B-034: a parser runs under the owner's uid (G5)"
    assert "paths: docs/gaps.md" in head
    assert "no section for G5 below the table" in prompt


def test_awb_build_add_takes_the_draft_as_it_is_and_skips_the_comment_lines(tree, register_path, capsys):
    root, q, _ = tree
    f = root.parent / "draft.md"
    f.write_text(builddraft.make("X1", "docs/review.md", tid="B-031"), encoding="utf-8")
    assert build.cmd_add(str(f), register=register_path) == 0
    (task,), notes = build.read_queue(q)
    assert notes == [] and task.id == "B-031"
    assert task.paths == ("awb/obs.py", "tests/test_obs.py", "docs/review.md")
    assert task.check == ("tests/test_obs.py",) and task.tests_replaced == ()
    assert "X1-OLD" not in (q / "QUEUE.md").read_text(encoding="utf-8")


def test_the_id_defaults_to_the_one_after_the_queue_and_the_records(tree):
    _, q, r = tree
    assert builddraft.next_id() == "B-001"
    (q / "QUEUE.md").write_text("## B-007: a task\nend: B-007\n", encoding="utf-8")
    (r / "B-012" / "1").mkdir(parents=True)
    assert builddraft.next_id() == "B-013"
    assert _split(builddraft.make("X1", "docs/review.md"))[0][0].startswith("## B-013: ")


def test_the_command_writes_to_standard_output_or_a_new_file_and_never_to_the_queue(tree, capsys):
    root, q, _ = tree
    assert build.main(["draft", "X1", "--from", "docs/review.md", "--id", "B-031"]) == 0
    assert capsys.readouterr().out.startswith("## B-031: ")
    out = root.parent / "x1.md"
    assert build.main(["draft", "X1", "--from", "docs/review.md", "--id", "B-031", "--out", str(out)]) == 0
    assert out.read_text(encoding="utf-8").startswith("## B-031: ")
    assert build.main(["draft", "X1", "--from", "docs/review.md", "--out", str(out)]) == 2
    assert "exists" in capsys.readouterr().err
    assert list(q.iterdir()) == []


def test_the_default_findings_file_is_the_review_and_a_missing_row_is_refused(tree, capsys):
    root, _, _ = tree
    assert builddraft.DEFAULT_FROM == "docs/review-2026-10-13.md"
    assert build.main(["draft", "X1"]) == 2
    assert "docs/review-2026-10-13.md cannot be read" in capsys.readouterr().err
    assert build.main(["draft", "Z9", "--from", "docs/review.md"]) == 2
    assert "names no row Z9" in capsys.readouterr().err
    assert build.main(["draft", "X1", "--from", "docs/review.md", "--id", "nine"]) == 2
    assert "the id is not B-NNN" in capsys.readouterr().err


def test_the_review_of_the_repository_drafts_every_finding_of_its_table():
    root = Path(__file__).resolve().parents[1]
    for fid in ("E1", "B7", "T4", "D1"):
        head, prompt = _split(builddraft.make(fid, root=root, tid="B-900"))
        build.parse("\n".join(ln for ln in head if not ln.startswith("# ")) + "\nprompt: build/queue/B-900.md\n"
                    "end: B-900\n")[0][0]
        assert "docs/review-2026-10-13.md" in next(ln for ln in head if ln.startswith("paths:"))
        assert prompt.count("### %s, " % fid) == 1
