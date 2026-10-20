"""K1 K2 K3 of the attacker review of 2026-10-20 (docs/reviews/REVIEW-2026-10-20.md): the put of the dataset runs git
and the signing key as the owner on files the work user can write.

K1: no setting of the clone runs a program, and a clone that is not the owner's alone is refused.
K2: the review mark and RELEASED.jsonl count only when the owner wrote them; `awb kb review` is refused for the work
user.
K3: the review pins every release asset; the signed sums are refused when one changed since, and SHA256SUMS is never
written through a link.
"""
from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

from awb import cli, config, kbreview
from awb.tcp import dataset
from tests.test_dataset import TODAY, facts, github, live, signing  # noqa: F401


def _planted_programs(clone: Path, tmp_path: Path) -> Path:
    """Hooks and an fsmonitor in the clone's own config, each writing a marker file when git runs it."""
    marker = tmp_path / "ran"
    hooks = tmp_path / "planted-hooks"
    hooks.mkdir()
    script = "#!/bin/sh\necho \"$0\" >> %s\n" % marker
    for name in ("pre-commit", "post-commit", "pre-push", "post-merge", "reference-transaction", "fsmonitor"):
        (hooks / name).write_text(script)
        (hooks / name).chmod(0o755)
    for key, value in (("core.hooksPath", str(hooks)), ("core.fsmonitor", str(hooks / "fsmonitor"))):
        subprocess.run(["git", "config", key, value], cwd=clone, check=True)
    return marker


def test_k1_a_hook_or_fsmonitor_in_the_clone_config_never_runs(home, facts, tmp_path, github):
    built = dataset.build(home, out=tmp_path / "ds", today=TODAY)
    dataset.github_put(home, built.folder, clone=github.clone)
    marker = _planted_programs(github.clone, tmp_path)
    # the plant works on a plain git call: the marker proves the hooks would run without the fixed settings
    subprocess.run(["git", "tag", "probe"], cwd=github.clone, check=True)
    assert marker.exists()
    marker.unlink()
    (built.folder / "facts.md").write_text((built.folder / "facts.md").read_text() + "\n")   # a commit to make
    dataset.github_put(home, built.folder, replace=True, clone=github.clone)
    assert not marker.exists()


def test_k1_every_git_call_on_the_clone_carries_the_fixed_settings(home, facts, tmp_path, github, monkeypatch):
    """The settings go in the environment (GIT_CONFIG_COUNT, the scope of `git -c`), so the argv keeps its shape."""
    calls = []
    real = subprocess.run

    def spy(args, **kw):
        calls.append((list(args), kw.get("env") or {}))
        return real(args, **kw)
    monkeypatch.setattr(dataset.subprocess, "run", spy)
    built = dataset.build(home, out=tmp_path / "ds", today=TODAY)
    dataset.github_put(home, built.folder, clone=github.clone)
    on_clone = [env for args, env in calls if args[0] == "git" and "safe.directory=%s" % github.clone.resolve() in args]
    assert len(on_clone) > 5
    want = {"core.hooksPath": "/dev/null", "core.fsmonitor": "false", "gpg.ssh.program": "ssh-keygen"}
    for env in on_clone:
        got = {env["GIT_CONFIG_KEY_%d" % i]: env["GIT_CONFIG_VALUE_%d" % i] for i in range(int(env["GIT_CONFIG_COUNT"]))}
        assert got == want


@pytest.mark.parametrize("plant", ["group-writable config", "writable .git", "linked .git"])
def test_k1_a_clone_that_is_not_the_owners_alone_is_refused_and_nothing_is_published(home, facts, tmp_path, github,
                                                                                    plant):
    built = dataset.build(home, out=tmp_path / "ds", today=TODAY)
    dataset.github_put(home, built.folder, clone=github.clone)
    git = github.clone / ".git"
    if plant == "group-writable config":
        (git / "config").chmod(0o664)
    elif plant == "writable .git":
        git.chmod(0o777)
    else:
        git.rename(tmp_path / "elsewhere.git")
        git.symlink_to(tmp_path / "elsewhere.git")
    before = len(github.log())
    with pytest.raises(dataset.DatasetError, match="not yours alone") as err:
        dataset.github_put(home, built.folder, replace=True, clone=github.clone)
    assert github.log()[before:] == []
    assert str(tmp_path) not in str(err.value)


def test_k1_a_fresh_clone_under_a_group_umask_is_made_the_owners_alone(home, facts, tmp_path, github):
    built = dataset.build(home, out=tmp_path / "ds", today=TODAY)
    old = os.umask(0o002)
    try:
        dataset.github_put(home, built.folder, clone=github.clone)
    finally:
        os.umask(old)
    dataset.check_clone(github.clone)
    assert not (github.clone / ".git" / "config").stat().st_mode & 0o022
