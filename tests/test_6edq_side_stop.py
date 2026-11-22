"""The stop hook asks no side session for STATE.md and OPEN.md (tcp-6edq asks 2 and 3 of 2026-11-21): in the
worktree .side/NAME of a project on the branch side/NAME it asked to update both after a commit, while the side
rules give both files to the main session. The main session is still asked."""
from __future__ import annotations

import os
from pathlib import Path

from awb import side
from tests.test_hook_notes_footer import STATE_TEXT, T0, commit, git
from tests.test_hooks import project, run_hook  # noqa: F401
from tests.test_hook_notes_footer import fake_review  # noqa: F401


def side_tree(project: Path, name: str) -> Path:
    """A project with STATE.md committed, a side worktree on side/NAME with one commit newer than STATE.md."""
    (project / "STATE.md").write_text(STATE_TEXT, encoding="utf-8")
    git(project, "init", "-q")
    git(project, "add", "-A")
    git(project, "commit", "-q", "-m", "start", when=T0)
    wt = project / side.SIDE_DIR / name
    git(project, "worktree", "add", "-q", "-b", "side/" + name, str(wt), "HEAD")
    os.utime(wt / "STATE.md", (T0, T0))
    commit(wt, "SIDE-RESULT.md", "the side result\n", T0 + 100)
    return wt


def test_6edq_the_stop_hook_asks_no_side_session_for_state_and_open(project, monkeypatch, capsys):  # noqa: F811
    wt = side_tree(project, "storage")
    fake_review(monkeypatch, [])
    code, _, err = run_hook("stop", {"cwd": str(wt)}, monkeypatch, capsys)
    assert "lag behind" not in err and "STATE.md" not in err and code == 0, err


def test_6edq_the_main_session_is_still_asked(project, monkeypatch, capsys):  # noqa: F811
    side_tree(project, "steps")
    commit(project, "notes.md", "a finding\n", T0 + 200)
    os.utime(project / "STATE.md", (T0, T0))
    fake_review(monkeypatch, [])
    code, _, err = run_hook("stop", {"cwd": str(project)}, monkeypatch, capsys)
    assert code == 2 and "lag behind 1 commit(s)" in err


def test_6edq_a_side_worktree_is_told_by_its_folder_or_its_branch(project, tmp_path):  # noqa: F811
    wt = side_tree(project, "entra")
    assert side.is_side_worktree(wt) and not side.is_side_worktree(project)
    elsewhere = tmp_path / "loose-steps"                     # a side branch outside .side/
    git(project, "worktree", "add", "-q", "-b", "side/loose", str(elsewhere), "HEAD")
    assert side.is_side_worktree(elsewhere)
    plain = tmp_path / "plain-tree"
    git(project, "worktree", "add", "-q", "-b", "work", str(plain), "HEAD")
    assert not side.is_side_worktree(plain) and not side.is_side_worktree(None)
    copy = project / side.SIDE_DIR / "copy"                   # a folder under .side/ that is no worktree
    copy.mkdir()
    assert not side.is_side_worktree(copy)
