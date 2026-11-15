"""Review of `awb deploy` (awb/deploy.py): a malformed plan or ref, a link where a file or a folder is expected, a
corrupt archive, a failed step and a child's output that root relays. Each test names the planted fault it
catches. Nothing runs as root or touches systemd: the root path runs in the temporary host of tests/test_deploy.py.
"""
from __future__ import annotations

import errno
import io
import json
import os
import subprocess
import tarfile
from pathlib import Path

import pytest

from awb import deploy
from tests.conftest import not_as_root
from tests.test_deploy import BIN, World, units_of_repo, world  # noqa: F401 (the fixture)

PLANTED = "/planted/" + "pass" + "word" + "-store"


def run_of(world: World) -> deploy.Run:
    caller = deploy.Caller(world.owner, world.work, world.repo, {})
    host = deploy.Host(root=world.root, runner=world.runner, environ=world.environ, getgrouplist=lambda n, g: [g])
    return deploy.Run(host, caller, world.plan(), deploy.Options())


# --------------------------------------------------------------------------- the plan and the ref


def test_a_trailing_newline_does_not_pass_the_plan_validation(world):
    """Planted: an id or a unit name with a newline at its end. A pattern anchored with "$" matches before a
    trailing newline, so the deployed commit, a daemon's release and since, the install units, the task units, a
    unit the target adds and a probe path passed with it."""
    allowed = set(units_of_repo())
    deploy.validate_plan(world.plan(), allowed)                  # the control plan validates
    nl = "\n"
    bad = [world.plan(deployed=world.c0 + nl), world.plan(live=world.c0 + nl),
           world.plan(install_units=["awb-ask.service" + nl]),
           world.plan(build_busy=["awb-task-B-001.service" + nl])]
    p = world.plan()
    p["daemons"]["vault"]["release"] = world.c0 + nl
    bad.append(p)
    p = world.plan()
    p["daemons"]["keys"]["since"] = "2026-11-15T10:00:00Z" + nl
    bad.append(p)
    p = world.plan()
    p["daemons"]["keys"]["aliases"] = ["t1" + nl]
    bad.append(p)
    p = world.plan()
    p["units"].append({"unit": "awb-new.service" + nl, "kind": "service", "action": "not installed", "reason": "",
                       "installed": False})
    bad.append(p)
    p = world.plan()
    p["probes"] = {p["units"][0]["unit"]: "/run/awb/x.sock" + nl}
    bad.append(p)
    for plan in bad:
        with pytest.raises(deploy.Refused, match="does not validate"):
            deploy.validate_plan(plan, allowed)


def test_the_archive_child_refuses_a_commit_with_a_trailing_newline(capsys):
    """Planted: `awb deploy archive` with a commit id and a newline ran git archive."""
    calls = []
    assert deploy.owner_archive("a" * 40 + "\n", Path("."), runner=lambda *a, **k: calls.append(a)) == 2
    assert calls == []


def test_a_malformed_to_ref_never_reaches_git(tmp_path):
    """Planted: `--to` with a leading dash reached `git rev-parse` where git reads it as an option; a range or a
    control character is no single commit either. A plain ref still goes to git."""
    calls = []

    def runner(cmd, **kw):
        calls.append([str(c) for c in cmd])
        if "status" in cmd:
            return subprocess.CompletedProcess(cmd, 0, "", "")
        return subprocess.CompletedProcess(cmd, 1, "", "")

    host = deploy.Host(root=tmp_path / "opt", runner=runner, environ={}, cwd=tmp_path)
    for ref in ("--output=x", "-n", "a..b", "HEAD\nx", "HEAD\x1b[2J", " HEAD"):
        calls.clear()
        with pytest.raises(deploy.Refused) as err:
            deploy.owner_plan(deploy.Options(to=ref), tmp_path, host)
        assert str(err.value) == "--to names no commit of the repository"
        assert not [c for c in calls if "rev-parse" in c], ref
    calls.clear()
    with pytest.raises(deploy.Refused, match="--to names no commit"):
        deploy.owner_plan(deploy.Options(to="HEAD~1"), tmp_path, host)
    assert [c for c in calls if "rev-parse" in c and "HEAD~1^{commit}" in c]


def test_status_json_drops_a_probe_that_climbs_out_of_run(tmp_path, monkeypatch, capsys):
    """Planted: a probe path that starts with /run/ and climbs out (`/run/../x`) reached the socket probe."""
    seen = []
    monkeypatch.setattr(deploy, "owner_status_json", lambda probes: seen.append(list(probes)) or {})
    host = deploy.Host(root=tmp_path, geteuid=lambda: 1000, cwd=tmp_path)
    argv = ["status", "--json", "--probe", "/run/../tmp/x.sock", "--probe", "/run/awb/ok.sock",
            "--probe", "/run/awb/x.sock\n"]
    assert deploy._owner_main(argv, host) == 0
    assert seen == [["/run/awb/ok.sock"]]


# --------------------------------------------------------------------------- links where a file is expected


def test_the_journal_is_never_written_through_a_planted_link(tmp_path):
    """Planted: a link at the journal's temporary name `.DEPLOYED.next`; it was opened with O_TRUNC and written
    through, so root emptied and rewrote the file the link names."""
    victim = tmp_path / "victim"
    victim.write_text("keep\n")
    journal = tmp_path / deploy.JOURNAL_NAME
    (tmp_path / (".%s.next" % deploy.JOURNAL_NAME)).symlink_to(victim)
    deploy.write_journal(journal, {"state": "done"})
    assert victim.read_text() == "keep\n"
    assert json.loads(journal.read_text()) == {"state": "done"} and not journal.is_symlink()
    assert not (tmp_path / (".%s.next" % deploy.JOURNAL_NAME)).exists()


def test_the_lock_refuses_a_link_or_a_special_file(tmp_path):
    """Planted: a link and a fifo at the lock path. The link gave a raw OSError (a traceback, exit 1 and no
    refusal); the fifo was taken as the lock and ftruncate failed on it."""
    victim = tmp_path / "victim"
    victim.write_text("keep\n")
    lock = tmp_path / deploy.LOCK_NAME
    lock.symlink_to(victim)
    with pytest.raises(deploy.Refused, match="the deploy lock .* cannot be opened"):
        deploy.take_lock(tmp_path)
    assert victim.read_text() == "keep\n"
    lock.unlink()
    os.mkfifo(lock)
    with pytest.raises(deploy.Refused, match="not a regular file"):
        deploy.take_lock(tmp_path)


@not_as_root
def test_the_log_line_is_never_appended_through_a_link(world, capsys):
    """Planted: a link at /opt/tcp-awb/deploy.log; root appended the line of the run to the file it names."""
    victim = world.tmp / "victim"
    victim.write_text("keep\n")
    (world.root / deploy.LOG_NAME).symlink_to(victim)
    world.runner.plan = world.plan(actions={"awb-ask.service": "restart"})
    assert world.main([]) == 0, capsys.readouterr()
    assert victim.read_text() == "keep\n"
    assert "the line of this run is not logged" in capsys.readouterr().out


@not_as_root
def test_extract_refuses_a_linked_releases_folder_and_replaces_a_link_at_the_release(world, tmp_path):
    """Planted: the releases folder as a link (root extracted through it into another folder) and a link at
    releases/<target> (os.rename onto it raised a raw OSError after the work was done)."""
    run = run_of(world)
    releases = world.root / "releases"
    elsewhere = tmp_path / "elsewhere"
    releases.rename(elsewhere)
    releases.symlink_to(elsewhere)
    with pytest.raises(deploy.StepFailed, match="is a link or no folder"):
        run.extract()
    assert sorted(p.name for p in elsewhere.iterdir()) == [world.c0]
    releases.unlink()
    elsewhere.rename(releases)
    other = tmp_path / "other"
    other.mkdir()
    rel = releases / world.c1
    rel.symlink_to(other)
    run.extract()
    assert rel.is_dir() and not rel.is_symlink() and (rel / "seal" / "setup.sh").is_file()
    assert list(other.iterdir()) == []


def test_a_link_out_through_another_link_is_refused_after_extraction(tmp_path):
    """Planted: `s -> d/..` and `l -> s/..`. Each link's own text stays inside, so the archive check passes it,
    but `l` resolves to the folder above the release."""
    tar = tmp_path / "chain.tar"
    with tarfile.open(tar, "w") as tf:
        d = tarfile.TarInfo("d")
        d.type, d.mode = tarfile.DIRTYPE, 0o755
        tf.addfile(d)
        for name, target in (("s", "d/.."), ("l", "s/..")):
            info = tarfile.TarInfo(name)
            info.type, info.linkname = tarfile.SYMTYPE, target
            tf.addfile(info)
    deploy.check_archive(tar)
    folder = tmp_path / "rel"
    folder.mkdir()
    (folder / "d").mkdir()
    (folder / "s").symlink_to("d/..")
    (folder / "l").symlink_to("s/..")
    with pytest.raises(deploy.StepFailed, match="a link out of the release"):
        deploy.check_tree(folder)
    (folder / "l").unlink()
    deploy.check_tree(folder)                               # a link that stays inside is fine


# --------------------------------------------------------------------------- failed steps


def test_a_corrupt_archive_is_a_step_failure(tmp_path):
    """Planted: an archive child that exits 0 with bytes that are no tar; tarfile.ReadError escaped the step."""
    bad = tmp_path / "bad.tar"
    bad.write_bytes(b"no tar at all" * 100)
    with pytest.raises(deploy.StepFailed, match="no readable tar archive"):
        deploy.check_archive(bad)


@not_as_root
def test_an_os_error_in_a_step_fails_the_run_without_a_path(world, capsys):
    """Planted: setup.sh cannot be started (an OSError that names a path). The error escaped as a traceback with the
    path and the journal was not saved; now the run fails with exit 1, the errno text and the journal pending."""
    def fail(cmd, kw):
        if os.path.basename(cmd[0]) == "setup.sh":
            raise OSError(errno.EACCES, "Permission denied", PLANTED)
        return None

    world.runner.on = fail
    world.runner.plan = world.plan(actions={"awb-ask.service": "restart"})
    assert world.main([]) == 1
    out, err = capsys.readouterr()
    assert "# awb deploy: a step failed (Permission denied): the journal keeps what is pending" in out
    assert PLANTED not in out + err and "Traceback" not in err
    j = json.loads((world.root / deploy.JOURNAL_NAME).read_text())
    assert j["state"] == "running" and j["pending"] == ["awb-ask.service"]
    assert os.readlink(world.root / "src") == "releases/%s" % world.c0


def test_flip_replaces_a_leftover_folder_at_src_next(tmp_path):
    """Planted: a folder at src.next (an interrupted copy); os.symlink raised FileExistsError and nothing flipped."""
    (tmp_path / "releases" / "a").mkdir(parents=True)
    (tmp_path / "releases" / "b").mkdir()
    (tmp_path / "src").symlink_to("releases/a")
    (tmp_path / "src.next" / "x").mkdir(parents=True)
    deploy.flip(tmp_path, "b")
    assert os.readlink(tmp_path / "src") == "releases/b" and not (tmp_path / "src.next").exists()


@not_as_root
def test_the_plan_childs_error_is_relayed_without_control_characters(world, capsys):
    """Planted: a refused plan child whose standard error carries a terminal escape; root wrote it raw."""
    def refuse(cmd, kw):
        if cmd[:3] == [BIN, "deploy", "plan"]:
            return subprocess.CompletedProcess(cmd, 2, "", "awb deploy: refused\x1b[2J\x07 here\n")
        return None

    world.runner.on = refuse
    assert world.main([]) == 2
    err = capsys.readouterr().err
    assert "\x1b" not in err and "\x07" not in err
    assert "awb deploy: refused?[2J? here" in err and "the plan was refused, nothing was changed" in err
