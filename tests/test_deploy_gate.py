"""The deploy gate (threat model G2): before a release is installed, the owner's plan child runs the commit gate
over the commits from the deployed release to the target and refuses on any finding, naming the commit and the
class and never a value. The tests use a temporary repository, the fixture register and a fake gate."""
from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

from awb import deploy
from tests import fixtures as fx
from tests.conftest import not_as_root
from tests.test_deploy import BIN, World, commit, git, plan_host, synth_tree, world  # noqa: F401 (the fixture)


def repo_with_history(tmp_path: Path) -> tuple[Path, list[str]]:
    """Three commits: a clean one, one that adds a file with a registered name, one whose message carries a home
    path. The planted strings are split so that the self-clean scan of the repository does not see them here."""
    repo = tmp_path / "repo"
    repo.mkdir()
    git(repo, "init", "-q")
    (repo / "a.txt").write_text("clean\n", encoding="utf-8")
    c0 = commit(repo, "first")
    (repo / "b.txt").write_text("offer for %s\n" % fx.CUSTOMER_FORMS[0], encoding="utf-8")
    c1 = commit(repo, "second")
    (repo / "c.txt").write_text("still clean\n", encoding="utf-8")
    c2 = commit(repo, "third\n\nlog under /ho" + "me/builder/x")
    return repo, [c0, c1, c2]


def test_the_gate_scans_the_range_and_names_commit_and_class_only(tmp_path, register_path):
    """Planted: a registered name in a file of one commit and a home path in the message of the next. The verdict
    carries the commit, the class and where, never the value, the path or the author."""
    repo, (c0, c1, c2) = repo_with_history(tmp_path)
    v = deploy.owner_gate(repo, c0, c2, register_path)
    assert v["from"] == c0 and v["to"] == c2 and v["commits"] == 2
    assert sorted(v["findings"]) == sorted([(c1[:10], "name", "file", 1), (c2[:10], "homepath", "message", 1)])
    note, refusal = deploy.gate_note(v), deploy.gate_refusal(v)
    assert note == "gate: 2 commits %s..%s, 2 findings" % (c0[:7], c2[:7])
    assert "commit %s: name x1" % c1[:7] in refusal
    assert "commit %s: homepath in the message x1" % c2[:7] in refusal
    assert refusal.startswith("the gate refuses the target %s: " % c2[:7]) and refusal.endswith("nothing was changed")
    for text in (note, refusal, json.dumps(v)):
        assert fx.CUSTOMER_FORMS[0] not in text and "builder" not in text and "b.txt" not in text
    # one commit, clean: the note says so
    one = deploy.owner_gate(repo, c1, c2, register_path)
    assert one["commits"] == 1 and len(one["findings"]) == 1
    assert deploy.gate_note(deploy.owner_gate(repo, c0, c1, register_path)) == \
        "gate: 1 commit %s..%s, 1 finding" % (c0[:7], c1[:7])
    # the deployed commit itself, and a rollback to an ancestor: nothing new is gated
    assert deploy.owner_gate(repo, c2, c2, register_path) == {"from": c2, "to": c2, "commits": 0, "findings": []}
    assert deploy.owner_gate(repo, c2, c0, register_path)["commits"] == 0
    assert deploy.gate_note(deploy.owner_gate(repo, c2, c0, register_path)) == \
        "gate: 0 commits %s..%s, clean" % (c2[:7], c0[:7])
    # no deployed commit: the whole history of the target
    whole = deploy.owner_gate(repo, None, c2, register_path)
    assert whole["commits"] == 3 and sorted(whole["findings"]) == sorted(v["findings"])
    assert deploy.gate_note(whole) == \
        "gate: 3 commits up to %s (no deployed commit: the whole history), 2 findings" % c2[:7]


def test_a_gate_that_cannot_run_refuses(tmp_path, register_path):
    """Planted: no register at the path, and a target that is no commit: both are Refused, nothing is scanned."""
    repo, (c0, _c1, c2) = repo_with_history(tmp_path)
    with pytest.raises(deploy.Refused, match="finds no register"):
        deploy.owner_gate(repo, c0, c2, tmp_path / "no-register.tsv")
    with pytest.raises(deploy.Refused, match="could not run"):
        deploy.owner_gate(repo, c0, "f" * 40, register_path)


def gated_repo(world: World, tmp_path: Path) -> tuple[Path, str, str]:
    """A repository with the unit templates, two commits, and the world's live release renamed to the first."""
    repo = tmp_path / "plan-repo"
    shutil.copytree(synth_tree(tmp_path).root, repo)
    git(repo, "init", "-q")
    c0 = commit(repo)
    (repo / "tests").mkdir()
    (repo / "tests" / "x.txt").write_text("1\n")
    c1 = commit(repo)
    world.c0, world.c1 = c0, c1
    os.rename(world.root / "releases" / ("a" * 40), world.root / "releases" / c0)
    (world.root / "src").unlink()
    (world.root / "src").symlink_to("releases/%s" % c0)
    deploy.write_journal(world.root / "DEPLOYED", {"state": "done", "target": c0, "previous": None, "units": {},
                                                   "pending": [], "running": {}})
    return repo, c0, c1


def verdict(deployed, target, findings=()) -> dict:
    return {"from": deployed, "to": target, "commits": 1, "findings": list(findings)}


@not_as_root
def test_the_plan_carries_the_verdict_and_a_finding_refuses_it(world, tmp_path, capsys):
    """Planted: a fake gate that records its range and answers clean, then one that answers two findings. The
    clean verdict is a note of the plan that root validates; the findings refuse the plan with the commit and the
    class, so no plan reaches root and the dry run prints no step."""
    repo, c0, c1 = gated_repo(world, tmp_path)
    calls = []

    def clean(repo_, deployed, target):
        calls.append((Path(repo_), deployed, target))
        return verdict(deployed, target)

    host = plan_host(world, repo)
    host.gate = clean
    assert deploy._owner_main(["plan", "--json"], host) == 0
    plan = json.loads(capsys.readouterr().out)
    assert calls == [(repo, c0, c1)]
    note = "gate: 1 commit %s..%s, clean" % (c0[:7], c1[:7])
    assert note in plan["notes"]
    assert deploy.validate_plan(plan, {u["unit"] for u in plan["units"]}) is plan
    # the dry run as the owner shows the verdict and every step
    assert deploy._owner_main(["dry-run"], host, deploy.Options(dry_run=True)) == 0
    out = capsys.readouterr().out.splitlines()
    assert "# " + note in out and "# dry run done, nothing was changed" in out
    assert any(line.startswith("+ write ") for line in out)

    def dirty(repo_, deployed, target):
        return verdict(deployed, target, [(target[:10], "secret", "file", 2), (target[:10], "name", "message", 1)])

    host.gate = dirty
    assert deploy._owner_main(["plan", "--json"], host) == 2
    cap = capsys.readouterr()
    refusal = "awb deploy: the gate refuses the target %s: commit %s: name in the message x1, secret x2; " \
              "nothing was changed" % (c1[:7], c1[:7])
    assert cap.out == "" and refusal in cap.err.splitlines()
    assert deploy._owner_main(["dry-run"], host, deploy.Options(dry_run=True)) == 2
    cap = capsys.readouterr()
    out = cap.out.splitlines()
    assert "# gate: 1 commit %s..%s, 3 findings" % (c0[:7], c1[:7]) in out
    assert refusal in cap.err.splitlines()
    assert not [line for line in out if line.startswith("+ ")] and "# dry run done, nothing was changed" not in out


@not_as_root
def test_without_a_working_gate_the_plan_is_refused(world, tmp_path, capsys):
    """Planted: the test environment has no readable register and no vault daemon, so the real gate cannot run:
    the plan child must refuse rather than hand root a plan without a verdict."""
    repo, _c0, _c1 = gated_repo(world, tmp_path)
    host = plan_host(world, repo)
    assert host.gate is None
    assert deploy._owner_main(["plan", "--json"], host) == 2
    cap = capsys.readouterr()
    assert cap.out == ""
    assert "awb deploy: the gate finds no register" in cap.err or "awb deploy: the gate could not run" in cap.err
    assert deploy._owner_main(["dry-run"], host, deploy.Options(dry_run=True)) == 2
    assert "awb deploy: the gate " in capsys.readouterr().err


@not_as_root
def test_root_installs_nothing_when_the_gate_refuses_the_plan(world, capsys):
    """Planted: the plan child exits 2 with the gate's refusal; root must show it and change nothing, with and
    without --dry-run. A clean verdict reaches the owner as a note of the plan."""
    refusal = "awb deploy: the gate refuses the target %s: commit %s: secret x1; nothing was changed\n" % (
        world.c1[:7], world.c1[:7])

    def refuse(cmd, kw):
        if cmd[:3] == [BIN, "deploy", "plan"]:
            return subprocess.CompletedProcess(cmd, 2, "", refusal)
        return None

    world.runner.on = refuse
    for argv in ([], ["--dry-run"]):
        assert world.main(argv) == 2
        err = capsys.readouterr().err
        assert refusal.strip() in err and "the plan was refused, nothing was changed" in err
    assert not (world.root / "DEPLOYED").exists()
    assert sorted(p.name for p in (world.root / "releases").iterdir()) == [world.c0]
    assert world.runner.systemctl() == [] and world.runner.named("setup.sh") == []
    assert [c[:3] for c, _ in world.runner.calls] == [[BIN, "deploy", "plan"]] * 2
    world.runner.on = None
    note = "gate: 1 commit %s..%s, clean" % (world.c0[:7], world.c1[:7])
    world.runner.plan = world.plan(notes=[note])
    assert world.main(["--dry-run"]) == 0
    assert "# " + note in capsys.readouterr().out.splitlines()
