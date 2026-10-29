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
from datetime import timedelta
from pathlib import Path

import pytest

from awb import cli, config, kbreview
from awb.tcp import dataset
from tests.test_dataset import TODAY, facts, github, live, signing  # noqa: F401
from tests.test_kb_review import bucket  # noqa: F401


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


# --------------------------------------------------------------------------- K2: the mark and RELEASED.jsonl

def _review(ds, capsys) -> None:
    assert cli.main(["kb", "review", "--out", str(ds)]) == 0
    capsys.readouterr()


def test_k2_the_work_user_may_not_review(home, facts, tmp_path, capsys, monkeypatch):
    ds = tmp_path / "ds"
    built = dataset.build(home, out=ds, today=TODAY)
    monkeypatch.setattr(config, "is_work_user", lambda: True)
    assert cli.main(["kb", "review", "--out", str(ds)]) == 2
    assert "awb kb review is the owner's" in capsys.readouterr().err
    assert not kbreview.review_path(built.folder).exists()


@pytest.mark.parametrize("plant", ["writable by others", "a link"])
def test_k2_a_mark_the_owner_did_not_write_alone_is_refused(home, facts, tmp_path, bucket, capsys, plant):
    ds = tmp_path / "ds"
    built = dataset.build(home, out=ds, today=TODAY)
    _review(ds, capsys)
    mark = kbreview.review_path(built.folder)
    if plant == "writable by others":
        mark.chmod(0o666)
    else:
        copy = tmp_path / "copy.json"
        copy.write_bytes(mark.read_bytes())
        mark.unlink()
        mark.symlink_to(copy)
    assert cli.main(["dataset", "put", "--out", str(ds)]) == 1
    err = capsys.readouterr().err
    assert "is not yours alone" in err and "run awb kb review again" in err and str(tmp_path) not in err
    assert not bucket.objects and not kbreview.released_path(ds).exists()


def test_k2_a_released_file_writable_by_others_stops_the_review_and_the_put(home, facts, tmp_path, bucket, capsys):
    ds = tmp_path / "ds"
    dataset.build(home, out=ds, today=TODAY)
    _review(ds, capsys)
    assert cli.main(["dataset", "put", "--out", str(ds)]) == 0
    capsys.readouterr()
    released = kbreview.released_path(ds)
    assert not released.stat().st_mode & 0o022
    released.chmod(0o666)
    assert cli.main(["kb", "review", "--out", str(ds)]) == 2
    assert "RELEASED.jsonl beside the folders is not yours alone" in capsys.readouterr().err
    assert cli.main(["dataset", "put", "--out", str(ds), "--replace"]) == 1
    assert "RELEASED.jsonl beside the folders is not yours alone" in capsys.readouterr().err


def test_k2_the_mark_of_another_folder_does_not_count(home, facts, tmp_path, bucket, capsys):
    ds = tmp_path / "ds"
    first = dataset.build(home, out=ds, today=TODAY)
    _review(ds, capsys)
    second = dataset.build(home, out=ds, today=TODAY + timedelta(days=7))
    (second.folder / "facts.jsonl").write_bytes((first.folder / "facts.jsonl").read_bytes())
    kbreview.review_path(second.folder).write_bytes(kbreview.review_path(first.folder).read_bytes())
    assert cli.main(["dataset", "put", "--out", str(ds)]) == 1
    assert "run awb kb review again" in capsys.readouterr().err and not bucket.objects


def test_k2_the_mark_is_never_written_through_a_planted_link(home, facts, tmp_path, capsys):
    ds = tmp_path / "ds"
    built = dataset.build(home, out=ds, today=TODAY)
    victim = tmp_path / "victim.txt"
    victim.write_text("untouched\n")
    mark = kbreview.review_path(built.folder)
    mark.with_name(".%s.new" % mark.name).symlink_to(victim)
    _review(ds, capsys)
    assert victim.read_text() == "untouched\n"
    assert not mark.is_symlink() and json.loads(mark.read_text())["dataset"] == built.folder.name
    assert mark.stat().st_mode & 0o777 == 0o644


# --------------------------------------------------------------------------- K3: the signed sums

def test_k3_the_review_pins_every_release_asset(home, facts, tmp_path, capsys):
    ds = tmp_path / "ds"
    built = dataset.build(home, out=ds, today=TODAY)
    _review(ds, capsys)
    mark = json.loads(kbreview.review_path(built.folder).read_text())
    names = ["tcp-facts-2026-10-08.zip", "HOW-TO.pdf", "MANIFEST.json", "tcp-facts-2026-10-08.mcpb", "tcp-facts.mcpb"]
    assert sorted(mark["assets"]) == sorted(names)
    assert mark["assets"]["HOW-TO.pdf"] == kbreview.sha256_of(built.folder / "HOW-TO.pdf")
    assert mark["assets"] == kbreview.asset_sums(built.folder)


@pytest.mark.parametrize("asset", ["zip", "HOW-TO.pdf", "MANIFEST.json", "mcpb"])
def test_k3_an_asset_changed_after_the_review_stops_the_put(home, facts, tmp_path, bucket, capsys, asset):
    ds = tmp_path / "ds"
    built = dataset.build(home, out=ds, today=TODAY)
    _review(ds, capsys)
    path = built.folder / asset if "." in asset else built.folder.parent / (built.folder.name + "." + asset)
    with path.open("ab") as f:
        f.write(b"\n")
    assert cli.main(["dataset", "put", "--out", str(ds)]) == 1
    assert "run awb kb review again" in capsys.readouterr().err and not bucket.objects


def test_k3_signing_refuses_an_asset_the_review_did_not_pin_and_signs_nothing(home, facts, tmp_path, signing):
    built = dataset.build(home, out=tmp_path / "ds", today=TODAY)
    pinned = dict(kbreview.asset_sums(built.folder), **{"HOW-TO.pdf": "0" * 64})
    with pytest.raises(dataset.DatasetError, match="changed since the review; nothing was signed") as err:
        dataset.github_put(home, built.folder, clone=signing.github.clone, pinned=pinned)
    assert "run awb kb review again" in str(err.value)
    assert signing.keygen() == [] and not (built.folder / "SHA256SUMS").exists()
    assert not [a for a in signing.github.log() if a[:2] == ["release", "create"]]


def test_k3_signing_passes_with_the_pins_of_the_review(home, facts, tmp_path, signing):
    built = dataset.build(home, out=tmp_path / "ds", today=TODAY)
    dataset.github_put(home, built.folder, clone=signing.github.clone, pinned=kbreview.asset_sums(built.folder))
    assert len(signing.keygen()) == 1
    lines = (built.folder / "SHA256SUMS").read_text().splitlines()
    assert {l.split("  ")[1]: l.split("  ")[0] for l in lines} == kbreview.asset_sums(built.folder)


def test_k3_the_sums_file_is_never_written_through_a_planted_link(home, facts, tmp_path, signing):
    built = dataset.build(home, out=tmp_path / "ds", today=TODAY)
    victim = tmp_path / "victim.txt"
    victim.write_text("untouched\n")
    (built.folder / "SHA256SUMS").symlink_to(victim)
    dataset.github_put(home, built.folder, clone=signing.github.clone)
    assert victim.read_text() == "untouched\n"
    assert not (built.folder / "SHA256SUMS").is_symlink() and "HOW-TO.pdf" in (built.folder / "SHA256SUMS").read_text()


def test_k3_the_put_command_hands_the_pins_of_the_review_to_the_signing(home, facts, tmp_path, signing, capsys,
                                                                     monkeypatch):
    ds = tmp_path / "ds"
    built = dataset.build(home, out=ds, today=TODAY)
    _review(ds, capsys)
    seen = []
    real = dataset.github_put

    def spy(p, folder, **kw):
        seen.append(kw.get("pinned"))
        return real(p, folder, **{**kw, "clone": signing.github.clone})
    monkeypatch.setattr(dataset, "github_put", spy)
    assert cli.main(["dataset", "put", "--out", str(ds), "--github-only"]) == 0
    assert seen == [kbreview.asset_sums(built.folder)]
