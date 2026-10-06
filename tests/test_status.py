"""The recorded status of a project (awb/status.py): the Status: and Next: lines of STATE.md, the commits newer than
STATE.md, the stop hook that holds a reply while the status lags, the projects API and the chat that show it.

2026-10-06: STATE.md of a lab lagged five days and more than ten commits behind the work, and the portal showed the
old status as the current one.
"""
from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from awb import status
from tests.test_hooks import fake_review, project, run_hook  # noqa: F401  (fixtures of the hook tests)

T0 = 1_790_000_000          # 2026-09-21, any fixed second will do
STATE_TEXT = "# State of tcp-q7m4\n\nStatus: The appliance image is imported.\nNext: The boot test.\n\n## Now\n"


def git(root: Path, *args: str, when: int | None = None) -> None:
    env = dict(os.environ, GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@example.org", GIT_COMMITTER_NAME="t",
               GIT_COMMITTER_EMAIL="t@example.org")
    if when is not None:
        env.update(GIT_AUTHOR_DATE="@%d +0000" % when, GIT_COMMITTER_DATE="@%d +0000" % when)
    subprocess.run(["git", "-C", str(root), *args], check=True, capture_output=True, env=env)


def commit(root: Path, name: str, text: str, when: int) -> None:
    (root / name).write_text(text, encoding="utf-8")
    if name == "STATE.md":
        os.utime(root / name, (when, when))
    git(root, "add", name)
    git(root, "commit", "-q", "-m", "change %s" % name, when=when)


@pytest.fixture
def repo(tmp_path) -> Path:
    root = tmp_path / "tcp-q7m4"
    root.mkdir()
    git(root, "init", "-q")
    commit(root, "STATE.md", STATE_TEXT, T0)
    return root


def test_the_two_lines_are_read_from_the_head_of_state():
    rec = status.lines(STATE_TEXT)
    assert (rec.summary, rec.next) == ("The appliance image is imported.", "The boot test.")
    assert status.lines("# State\n\n## Now\n- work\n") == status.Recorded(None, None)
    late = "# State\n" + "line\n" * status.HEAD_LINES + "Status: too far down\nNext: too\n"
    assert status.lines(late) == status.Recorded(None, None)
    assert status.lines("Status:   \nNext: x\n").summary is None


def test_behind_counts_the_commits_newer_than_state(repo):
    assert status.behind(repo) == 0
    commit(repo, "notes.md", "a finding\n", T0 + 100)
    commit(repo, "notes.md", "a second finding\n", T0 + 200)
    assert status.behind(repo) == 2
    os.utime(repo / "STATE.md", (T0 + 300, T0 + 300))          # updated on disk, not yet committed
    assert status.behind(repo) == 0
    commit(repo, "STATE.md", STATE_TEXT + "- the findings\n", T0 + 400)
    assert status.behind(repo) == 0


def test_without_history_or_state_nothing_is_judged(tmp_path, repo):
    plain = tmp_path / "plain"
    plain.mkdir()
    (plain / "STATE.md").write_text("# State\n", encoding="utf-8")
    assert status.behind(plain) is None and status.reminder(plain) is None
    (repo / "STATE.md").unlink()
    assert status.behind(repo) is None and status.reminder(repo) is None


def test_the_reminder_asks_for_the_update_and_for_the_two_lines(repo):
    assert status.reminder(repo) is None
    commit(repo, "notes.md", "a finding\n", T0 + 100)
    said = status.reminder(repo)
    assert "lag behind 1 commit(s)" in said and "Status: and Next:" not in said and said.endswith("then commit.")
    commit(repo, "STATE.md", "# State of tcp-q7m4\n\n## Now\n- the finding\n", T0 + 200)
    said = status.reminder(repo)
    assert "lag behind" not in said and "Status: and Next:" in said


def test_the_stop_hook_holds_a_reply_while_the_status_lags(project, monkeypatch, capsys):  # noqa: F811
    (project / "STATE.md").write_text(STATE_TEXT, encoding="utf-8")
    git(project, "init", "-q")
    commit(project, "STATE.md", STATE_TEXT, T0)
    fake_review(monkeypatch, [])
    assert run_hook("stop", {"cwd": str(project)}, monkeypatch, capsys) == (0, "", "")
    commit(project, "notes.md", "a finding\n", T0 + 100)
    code, _, err = run_hook("stop", {"cwd": str(project)}, monkeypatch, capsys)
    assert code == 2 and "lag behind 1 commit(s)" in err
    assert run_hook("stop", {"cwd": str(project), "stop_hook_active": True}, monkeypatch, capsys) == (0, "", "")
    commit(project, "STATE.md", STATE_TEXT + "- the finding\n", T0 + 200)
    assert run_hook("stop", {"cwd": str(project)}, monkeypatch, capsys) == (0, "", "")


def test_the_portal_gets_the_status_and_how_far_it_lags():
    from tests.test_web_projects_api import Tests

    from awb.tcp.web import contract
    from awb.tcp.web import projects_api as api

    t = Tests()
    t.setUp()
    try:
        git(t.folder, "init", "-q")
        commit(t.folder, "STATE.md", STATE_TEXT, T0)
        commit(t.folder, "notes.md", "a finding\n", T0 + 100)
        row = api.project_list(t.paths)["projects"][0]
        assert row["status"]["summary"] == "The appliance image is imported."
        assert row["status"]["next"] == "The boot test." and row["status"]["behind"] == 1
        detail = api.project_detail("tcp-q7m4", t.paths)
        assert detail["status"] == row["status"]
        doc = contract.build()
        assert contract.validate(doc, detail, contract.ref("ProjectDetail")) == []
        assert contract.validate(doc, api.project_list(t.paths), contract.ref("ProjectList")) == []
    finally:
        t.tearDown()


def test_the_chat_says_when_the_status_lags():
    from awb.tcp.web import chat_service

    project = {"code": "tcp-q7m4", "goal": "Test a sample deployment", "state": "active",
               "fetched_at": "2026-10-06T10:00:00+00:00", "documents": {},
               "status": {"summary": "x", "next": "y", "updated": None, "behind": 3, "redacted": False}}
    assert "older than 3 later commit(s)" in chat_service.snapshot(project)["status_note"]
    project["status"]["behind"] = 0
    assert "status_note" not in chat_service.snapshot(project)
