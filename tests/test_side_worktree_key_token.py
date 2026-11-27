"""B-116 (ask 2026-11-27-tcp-6edq-1): a side session's worktree of a project (`<project>/.side/NAME`, awb side)
presents its project's key token, so a test tenant call from it reaches the project path of the key service.

Before: the token was read from `<folder>/.git/awb-key-token` only; in a git worktree `.git` is a file, so no token
went with the call and K4 refused it with "a call to a test tenant runs from the folder of an active project".
The service is a stand-in (tests/test_ask_kind.py), no request leaves the host."""
from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from awb import config, labclient, projects
from awb.tcp import keys
from tests.test_ask_kind import granted, service  # noqa: F401 - the fixtures

SESSION_UID = 4242


@pytest.fixture
def project(home, monkeypatch) -> Path:
    folder = granted(home, "project")
    monkeypatch.setattr(config, "paths", lambda: home)
    monkeypatch.setattr(config, "is_work_user", lambda: True)
    return folder


def _worktree(root: Path, name: str = "pocanswer") -> Path:
    wt = root / ".side" / name
    subprocess.run(["git", "-C", str(root), "worktree", "add", "-q", "-b", "side/" + name, str(wt)],
                   check=True, capture_output=True)
    return wt


def _call(token: str | None, project: str) -> dict:
    req = {"op": "call", "tenant": "test-1", "role": "read", "method": "GET", "service": "ecs",
           "path": "/v1/x/cloudservers", "region": "eu-nl", "project": project}
    return req if token is None else dict(req, token=token)


def test_a_side_worktree_presents_the_token_of_its_project(project, monkeypatch):
    token = projects.read_key_token(project)
    assert token is not None
    wt = _worktree(project)
    assert (wt / ".git").is_file()
    assert projects.read_key_token(wt) == token
    assert labclient._token(wt) == token
    (wt / "notes").mkdir(exist_ok=True)
    monkeypatch.chdir(wt / "notes")
    assert keys.own_token() == token


def test_a_call_from_a_side_worktree_reaches_the_project_path(project, service, monkeypatch):
    wt = _worktree(project)
    monkeypatch.chdir(wt)
    req = _call(keys.own_token(), project.name)
    assert service._caller_project(req, SESSION_UID) == project.name
    # past K4; stopped at the next check (a region the tenant lacks), so no request leaves the host
    with pytest.raises(keys.Refused, match="region is not one of the tenant"):
        service._call(req, SESSION_UID, caller=service._caller_project(req, SESSION_UID))


def test_without_the_token_the_call_is_refused_as_before(project, service):
    req = dict(_call(None, project.name), region="eu-de")
    with pytest.raises(keys.Refused, match="runs from the folder of an active project"):
        service._call(req, SESSION_UID, caller=service._caller_project(req, SESSION_UID))


def test_a_git_file_pointing_outside_the_projects_root_gives_no_token(project, home, tmp_path):
    # a project of the same code outside the projects root, with a real token in its .git
    outside = tmp_path / "elsewhere" / project.name
    subprocess.run(["git", "init", "-q", str(outside)], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(outside), "-c", "user.name=t", "-c", "user.email=t@example.invalid",
                    "commit", "-q", "--allow-empty", "-m", "x"], check=True, capture_output=True)
    projects.write_key_token(outside)
    wt = outside / ".side" / "x"
    subprocess.run(["git", "-C", str(outside), "worktree", "add", "-q", "-b", "side/x", str(wt)],
                   check=True, capture_output=True)
    assert not str(outside).startswith(str(home.projects_root))
    assert projects.read_key_token(wt) is None

    # a side folder of the real project whose .git file leads to that outside repository
    fake = project / ".side" / "fake"
    fake.mkdir(parents=True)
    (fake / ".git").write_text("gitdir: %s/.git/worktrees/x\n" % outside)
    assert projects.read_key_token(fake) is None
    assert labclient._token(fake) is None


def test_a_side_folder_of_a_project_that_is_not_live_gives_no_token(project, home):
    wt = _worktree(project)
    rows = [r if r.code != project.name else projects._replace(r, state="closed") for r in projects.load(home)]
    projects._save(home, rows)
    assert projects.read_key_token(wt) is None


def test_a_worktree_writes_no_token_file(home, monkeypatch):
    folder = granted(home, "project")
    monkeypatch.setattr(config, "paths", lambda: home)
    monkeypatch.setattr(config, "is_work_user", lambda: True)
    os.remove(folder / projects.BIND_FILE)
    wt = _worktree(folder)
    assert labclient._token(wt) is None
    monkeypatch.chdir(wt)
    assert keys.own_token() is None
    with pytest.raises(FileNotFoundError):
        projects.write_key_token(wt)
    assert not (folder / projects.BIND_FILE).exists()
    assert (wt / ".git").is_file()
    assert not list(wt.rglob("awb-key-token"))
    assert not list((folder / ".git" / "worktrees").rglob("awb-key-token"))
