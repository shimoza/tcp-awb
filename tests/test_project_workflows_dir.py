"""B-118 (ask 2026-11-27-tcp-bj3o-3): the Workflow tool takes a scriptPath only in a folder the session can read,
so a project session and a side session get the installed workflows folder in permissions.additionalDirectories
of their client settings. A spawn writes it, `awb projects sync` adds it to an older project without losing a
hook, and a side worktree carries it even when its project was not synced yet."""
from __future__ import annotations

import json
import subprocess
import sys
import time
from pathlib import Path

import pytest

from awb import projects, rulesync, side
from tests import fixtures

GOAL = "size two app clusters for the next phase"


def _settings(folder: Path) -> dict:
    return json.loads((Path(folder) / ".claude" / "settings.json").read_text(encoding="utf-8"))


def _spawn(home, register_path):
    return projects.spawn(home, "engagement", GOAL, fixtures.CUSTOMER_CODE, register_path)


def test_the_folder_is_the_installed_release_of_the_workflows():
    assert projects.WORKFLOWS_INSTALLED == "/opt/tcp-awb/src/workflows"


def test_a_spawned_project_carries_the_installed_workflows_folder(home, register_path):
    pr = _spawn(home, register_path)
    data = _settings(pr.path)
    assert data["permissions"] == {"additionalDirectories": [projects.WORKFLOWS_INSTALLED]}
    assert data["hooks"]
    assert projects.add_workflows_dir(Path(pr.path)) is False


def test_sync_adds_the_folder_to_an_old_project_and_keeps_every_hook(home, register_path):
    pr = _spawn(home, register_path)
    path = Path(pr.path) / ".claude" / "settings.json"
    old = _settings(pr.path)
    hooks = old["hooks"]
    del old["permissions"]
    old["permissions"] = {"deny": ["WebFetch"], "additionalDirectories": ["/srv/other"]}
    old["model"] = "sonnet"
    path.write_text(json.dumps(old, indent=2) + "\n", encoding="utf-8")
    assert dict(rulesync.sync_settings(home))[pr.code] == "added"
    data = _settings(pr.path)
    assert data["hooks"] == hooks
    assert data["model"] == "sonnet"
    assert data["permissions"] == {"deny": ["WebFetch"],
                                   "additionalDirectories": ["/srv/other", projects.WORKFLOWS_INSTALLED]}
    assert dict(rulesync.sync_settings(home))[pr.code] == "current"
    assert dict(rulesync.sync_settings(home, ["tcp-zzzz"])) == {}


def test_sync_adds_permissions_to_settings_with_hooks_only(home, register_path):
    pr = _spawn(home, register_path)
    path = Path(pr.path) / ".claude" / "settings.json"
    hooks = _settings(pr.path)["hooks"]
    path.write_text(json.dumps({"hooks": hooks}, indent=2) + "\n", encoding="utf-8")
    assert dict(rulesync.sync_settings(home))[pr.code] == "added"
    assert _settings(pr.path) == {"hooks": hooks,
                                  "permissions": {"additionalDirectories": [projects.WORKFLOWS_INSTALLED]}}


def test_sync_reports_settings_it_cannot_write_and_leaves_them(home, register_path):
    pr = _spawn(home, register_path)
    path = Path(pr.path) / ".claude" / "settings.json"
    path.write_text("[1, 2]\n", encoding="utf-8")
    assert dict(rulesync.sync_settings(home))[pr.code] == "cannot be written"
    assert path.read_text(encoding="utf-8") == "[1, 2]\n"
    path.unlink()
    path.symlink_to(Path(pr.path) / "STATE.md")
    assert dict(rulesync.sync_settings(home))[pr.code] == "cannot be written"


FAKE = r"""#!%s
import subprocess, sys
sys.stdin.read()
open("SIDE-RESULT.md", "w").write("done\n")
subprocess.run(["git", "add", "SIDE-RESULT.md"], check=True)
subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@example.invalid", "commit", "-q", "-m", "side"],
               check=True)
print('{"result": "DONE", "num_turns": 1, "is_error": false}')
"""


def _git(root: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(root), "-c", "user.name=t", "-c", "user.email=t@example.invalid",
                           *args], check=True, capture_output=True, text=True).stdout


def test_a_side_worktree_carries_the_folder_though_its_project_was_not_synced(tmp_path, monkeypatch):
    root = tmp_path / "tcp-q7m4"
    (root / ".claude").mkdir(parents=True)
    _git(root, "init", "-q", "-b", "main")
    (root / "CLAUDE.md").write_text("# rules\n")
    (root / "STATE.md").write_text("Status: x\nNext: y\n")
    (root / "notes").mkdir()
    (root / "notes" / "brief.md").write_text("the brief\n")
    hooks = {"Stop": [{"hooks": [{"type": "command", "command": "awb hook stop"}]}]}
    (root / ".claude" / "settings.json").write_text(json.dumps({"hooks": hooks}) + "\n")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "start")
    fake = tmp_path / "fake-client"
    fake.write_text(FAKE % sys.executable)
    fake.chmod(0o755)
    monkeypatch.setenv(side.CLIENT_ENV, str(fake))
    side.start("one", "notes/brief.md", "sonnet", "low", cwd=root)
    data = _settings(root / ".side" / "one")
    assert data["hooks"] == hooks
    assert data["permissions"]["additionalDirectories"] == [projects.WORKFLOWS_INSTALLED]
    end = time.time() + 30
    while side.status_lines(root)[0].split()[1] == "running" and time.time() < end:
        time.sleep(0.1)
    side.drop("one", cwd=root)
