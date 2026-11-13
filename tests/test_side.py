"""Side sessions of a project (SS, 2026-11-13): awb side start|status|take|stop|drop.

A fake client stands in for the headless session: it reads the prompt on stdin, writes SIDE-RESULT.md and a
deliverable in its worktree, commits them and exits."""
from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from awb import cli, deploy, side

CODE = "tcp-q7m4"

FAKE = r"""#!%s
import os, subprocess, sys
prompt = sys.stdin.read()
args = sys.argv[1:]
open("argv.txt", "w").write(" ".join(args) + "\n")
assert "--model" in args and "--effort" in args and "--strict-mcp-config" in args
os.makedirs("deliverables", exist_ok=True)
open("deliverables/report.md", "w").write("the report\n")
open("SIDE-RESULT.md", "w").write("result of the side work\n" + ("brief seen\n" if "the brief text" in prompt else ""))
subprocess.run(["git", "add", "SIDE-RESULT.md", "deliverables/report.md", "argv.txt"], check=True)
subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@example.invalid", "commit", "-q", "-m", "side"],
               check=True)
print('{"result": "DONE"}')
"""


def _git(root: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(root), "-c", "user.name=t", "-c", "user.email=t@example.invalid",
                           *args], check=True, capture_output=True, text=True).stdout


@pytest.fixture
def proj(tmp_path, monkeypatch):
    root = tmp_path / CODE
    root.mkdir()
    _git(root, "init", "-q", "-b", "main")
    (root / "CLAUDE.md").write_text("# rules\n")
    (root / "STATE.md").write_text("Status: x\nNext: y\n")
    (root / "notes").mkdir()
    (root / "notes" / "brief-mail.md").write_text("the brief text: rework the plan mail\n")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "start")
    fake = tmp_path / "fake-client"
    fake.write_text(FAKE % sys.executable)
    fake.chmod(0o755)
    monkeypatch.setenv(side.CLIENT_ENV, str(fake))
    for k in ("GIT_AUTHOR_NAME", "GIT_COMMITTER_NAME"):
        monkeypatch.setenv(k, "t")
    for k in ("GIT_AUTHOR_EMAIL", "GIT_COMMITTER_EMAIL"):
        monkeypatch.setenv(k, "t@example.invalid")
    return root


def _wait(root: Path, name: str, want: str = "done", limit: float = 30.0) -> str:
    end = time.time() + limit
    while time.time() < end:
        line = side.status_lines(root)[0]
        if line.split()[1] == want:
            return line
        time.sleep(0.1)
    raise AssertionError(side.status_lines(root))


def test_start_runs_the_brief_in_a_worktree_and_take_copies_the_result(proj):
    msg = side.start("mail", "notes/brief-mail.md", "fable", "high", cwd=proj)
    assert "side session mail started (fable, effort high)" in msg
    line = _wait(proj, "mail")
    assert line.startswith("mail  done  fable/high  1 commit(s)  result: yes")
    wt = proj / ".side" / "mail"
    assert "--model fable --effort high" in (wt / "argv.txt").read_text()
    assert "/.side/" in (proj / ".git" / "info" / "exclude").read_text()
    assert _git(proj, "status", "--porcelain") == ""
    out = side.take("mail", cwd=proj)
    assert out[0] == "result: notes/side-mail.md" and "brief seen" in (proj / "notes" / "side-mail.md").read_text()
    assert "commits: 1" in out and any("deliverables/report.md" in x for x in out)
    assert not (proj / "deliverables" / "report.md").exists() and wt.is_dir()


def test_take_merge_brings_the_commits_and_removes_the_side(proj):
    side.start("rep", "notes/brief-mail.md", cwd=proj)
    _wait(proj, "rep")
    out = side.take("rep", merge=True, cwd=proj)
    assert "merged side/rep" in out and "side session rep removed" in out
    assert (proj / "deliverables" / "report.md").read_text() == "the report\n"
    assert not (proj / ".side" / "rep").exists()
    assert "side/rep" not in _git(proj, "branch", "--list")
    assert side.status_lines(proj) == ["no side sessions"]


def test_start_refuses_outside_a_project_a_bad_name_or_a_brief_outside(proj, tmp_path):
    other = tmp_path / "plain"
    other.mkdir()
    with pytest.raises(side.SideError, match="root of a project folder"):
        side.start("a", "x.md", cwd=other)
    with pytest.raises(side.SideError, match="NAME"):
        side.start("Bad_Name", "notes/brief-mail.md", cwd=proj)
    (tmp_path / "outside.md").write_text("x")
    with pytest.raises(side.SideError, match="file of this project"):
        side.start("a", "../outside.md", cwd=proj)
    with pytest.raises(side.SideError, match="LEVEL"):
        side.start("a", "notes/brief-mail.md", "sonnet", "huge", cwd=proj)
    assert not (proj / ".side").exists()


def test_a_second_start_of_the_same_name_is_refused_and_a_side_cannot_start_a_side(proj):
    side.start("mail", "notes/brief-mail.md", cwd=proj)
    with pytest.raises(side.SideError, match="exists"):
        side.start("mail", "notes/brief-mail.md", cwd=proj)
    _wait(proj, "mail")
    with pytest.raises(side.SideError):
        side.start("deeper", "notes/brief-mail.md", cwd=proj / ".side" / "mail")


def test_take_refuses_a_running_side_and_stop_ends_it(proj, tmp_path, monkeypatch):
    slow = tmp_path / "slow-client"
    slow.write_text("#!%s\nimport time, sys\nsys.stdin.read()\ntime.sleep(60)\n" % sys.executable)
    slow.chmod(0o755)
    monkeypatch.setenv(side.CLIENT_ENV, str(slow))
    side.start("long", "notes/brief-mail.md", cwd=proj)
    _wait(proj, "long", "running")
    with pytest.raises(side.SideError, match="still runs"):
        side.take("long", cwd=proj)
    assert side.stop("long", cwd=proj) == "side session long stopped"
    line = _wait(proj, "long", "failed")
    assert "result: no" in line
    assert side.drop("long", cwd=proj) == "side session long removed"


def test_the_client_runs_headless_without_mcp_and_with_the_chosen_model():
    argv = side.client_argv("/c", "opus", "max")
    assert argv[:2] == ["/c", "-p"] and argv[argv.index("--model") + 1] == "opus"
    assert argv[argv.index("--effort") + 1] == "max" and "--strict-mcp-config" in argv


def test_the_work_rules_tell_a_session_how_to_split_work_off():
    rules = (Path(__file__).resolve().parent.parent / "seal" / "work-claude" / "CLAUDE.md").read_text(encoding="utf-8")
    line = [ln for ln in rules.splitlines() if "awb side start NAME --brief notes/brief-NAME.md" in ln]
    assert len(line) == 1 and "awb side take NAME" in line[0] and "Never write in .side/" in line[0]


def test_the_command_is_wired_and_documented():
    root = Path(__file__).resolve().parent.parent
    assert "side" in cli.DELEGATED and "awb side" in cli.__doc__
    assert "awb/side.py" in deploy.COMMAND_ONLY
    assert "awb side" in (root / "README.md").read_text(encoding="utf-8")
    assert "### `awb side" in (root / "COMMANDS.md").read_text(encoding="utf-8")

