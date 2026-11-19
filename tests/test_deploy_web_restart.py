"""Q-0002: a deploy restarts a service that runs an older release, even when its closure did not change.

awb-web.service (socket activated, Type=simple) was left out of the restart set of most deploys, kept the code of an
older release and, once that release was pruned, needrestart flagged it and the host monitor saw the portal down. The
plan now restarts every active service whose main process started before the switch of the last deploy and that the
journal does not record on the live release (`deploy.older_release_units`). Nothing here runs as root or touches
systemd: the plan runs as the owner against a temporary repository and the fake systemctl of tests/test_deploy.py,
the root run in its temporary host. Every test names the planted fault it must catch.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

from awb import deploy
from tests.conftest import not_as_root
from tests.test_deploy import (World, commit, git, plan_host, synth_tree, units_of_repo,  # noqa: F401
                               world)

FLIP = 5_000_000            # the flip of the last deploy, monotonic microseconds
BEFORE = str(FLIP - 4_000_000)
AFTER = str(FLIP + 4_000_000)
OLDER = "c" * 40            # a release before the live one


def two_commits(world: World, tmp_path: Path, changed: str = "tests/x.txt") -> tuple[Path, str, str]:
    """A repository with c0 (live, src points at it) and c1 (changes `changed` only), the world moved onto them."""
    repo = tmp_path / "plan-repo"
    shutil.copytree(synth_tree(tmp_path).root, repo)
    git(repo, "init", "-q")
    c0 = commit(repo)
    path = repo / changed
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(path.read_text() + "X = 1\n" if path.exists() else "1\n")
    c1 = commit(repo)
    os.rename(world.root / "releases" / world.c0, world.root / "releases" / c0)
    world.c0, world.c1 = c0, c1
    (world.root / "src").unlink()
    (world.root / "src").symlink_to("releases/%s" % c0)
    return repo, c0, c1


def journal(world: World, c0: str, **kw) -> None:
    data = {"state": "done", "target": c0, "previous": OLDER, "units": {}, "pending": [], "running": {},
            "boot": deploy.boot_id(), "flip": FLIP}
    data.update(kw)
    deploy.write_journal(world.root / "DEPLOYED", data)


def started(world: World, **units: str) -> None:
    """Every unit of the repository started after the flip, the named ones at the given time."""
    for u in units_of_repo():
        world.runner.unit(u)["ExecMainStartTimestampMonotonic"] = AFTER
    for u, t in units.items():
        world.runner.unit(u.replace("_", "-").replace("-service", ".service").replace("-socket", ".socket"))[
            "ExecMainStartTimestampMonotonic"] = t


def row(plan: dict, unit: str) -> tuple[str, str]:
    r = next(u for u in plan["units"] if u["unit"] == unit)
    return r["action"], r["reason"]


def plan_of(world: World, repo: Path, opts: deploy.Options | None = None, show=None) -> dict:
    return deploy.owner_plan(opts or deploy.Options(), repo, plan_host(world, repo, show=show))


# --------------------------------------------------------------------------- the plan


def test_a_service_that_started_before_the_last_switch_restarts_with_its_closure_unchanged(world, tmp_path):
    """Planted: awb-web.service and awb-console-data.service started before the flip of the last deploy, the change
    touches no closure. Before Q-0002 both stayed unchanged; now both restart with the reason "runs an older
    release". A service started after the flip stays unchanged."""
    repo, c0, _ = two_commits(world, tmp_path)
    journal(world, c0)
    started(world, awb_web_service=BEFORE, awb_console_data_service=BEFORE)
    plan = plan_of(world, repo)
    for unit in ("awb-web.service", "awb-console-data.service"):
        action, reason = row(plan, unit)
        assert action == "restart", (unit, reason)
        assert reason == "runs an older release (started before the switch to %s)" % deploy.short(c0)
    assert row(plan, "awb-ask.service")[0] == "unchanged"
    assert row(plan, "awb-materials.service")[0] == "unchanged"
    # the control: everything started after the flip, nothing restarts
    started(world)
    plan = plan_of(world, repo)
    assert [u["unit"] for u in plan["units"] if u["action"] == "restart"] == []


def test_sockets_daemons_inactive_units_and_units_on_the_live_release_are_left_alone(world, tmp_path):
    """Planted: a socket, the vault and the key service, an inactive service and a service the journal records on
    the live release (a redeploy of the same commit flips to the release it already runs), each started before the
    flip. None of them restarts by this rule: a socket has no code, the daemons have their reload and their own
    line, an inactive service starts with the live code, the recorded one runs it."""
    repo, c0, _ = two_commits(world, tmp_path)
    journal(world, c0, running={"awb-console-data.service": c0, "awb-web.service": OLDER})
    started(world, awb_web_socket=BEFORE, awb_vaultd_service=BEFORE, awb_keyd_service=BEFORE,
            awb_materials_service=BEFORE, awb_console_data_service=BEFORE, awb_web_service=BEFORE)
    world.runner.unit("awb-materials.service")["ActiveState"] = "inactive"
    plan = plan_of(world, repo)
    for unit in ("awb-web.socket", "awb-vaultd.service", "awb-keyd.service", "awb-materials.service",
                 "awb-console-data.service"):
        assert row(plan, unit)[0] == "unchanged", (unit, row(plan, unit))
    assert row(plan, "awb-web.service")[0] == "restart"      # recorded on an older release: it restarts


def test_no_journal_flip_or_another_boot_restarts_nothing_by_this_rule(world, tmp_path):
    """Planted: the journal of another boot (every process started after the flip of that boot), a journal without
    a flip (a run that died before the switch) and a flip that is a boolean. The rule must not fire on any."""
    repo, c0, _ = two_commits(world, tmp_path)
    started(world, awb_web_service=BEFORE)
    for extra in ({"boot": "another-boot"}, {"flip": None}, {"flip": True}):
        journal(world, c0, **extra)
        plan = plan_of(world, repo)
        assert row(plan, "awb-web.service")[0] == "unchanged", extra


def test_a_change_of_its_closure_keeps_the_reason_of_the_change(world, tmp_path):
    """Planted: the gateway itself changed and awb-web.service started before the flip. The restart names the
    changed file, as before Q-0002, not the older release."""
    repo, c0, _ = two_commits(world, tmp_path, changed="awb/tcp/web/gateway.py")
    journal(world, c0)
    started(world, awb_web_service=BEFORE)
    action, reason = row(plan_of(world, repo), "awb-web.service")
    assert action == "restart" and "awb/tcp/web/gateway.py" in reason and "older release" not in reason


def test_only_still_limits_the_restart_to_the_named_units(world, tmp_path):
    """Planted: --only awb-ask.service while awb-web.service runs an older release. --only wins: awb-web.service
    stays unchanged, not named with --only."""
    repo, c0, _ = two_commits(world, tmp_path)
    journal(world, c0)
    started(world, awb_web_service=BEFORE)
    plan = plan_of(world, repo, deploy.Options(only=["awb-ask.service"]))
    assert row(plan, "awb-web.service") == ("unchanged", "not named with --only")
    assert row(plan, "awb-ask.service")[0] == "restart"


def test_the_build_runner_on_an_older_release_keeps_its_deferral(world, tmp_path):
    """Planted: the build runner started before the flip while a task unit runs. Its restart is deferred as for any
    other reason (unchanged, restart-wanted); with no task unit it restarts."""
    repo, c0, _ = two_commits(world, tmp_path)
    journal(world, c0)
    started(world, awb_buildd_service=BEFORE)
    show = {"awb-buildd.service": "/usr/local/bin/awb build serve"}
    world.runner.on = lambda cmd, kw: subprocess.CompletedProcess(
        cmd, 0, "awb-task-B-012.service loaded active running x\n", "") if cmd[:2] == ["systemctl", "list-units"] \
        else None
    plan = plan_of(world, repo, show=show)
    action, reason = row(plan, "awb-buildd.service")
    assert action == "unchanged" and "deferred" in reason and plan["build_restart_wanted"] is True
    world.runner.on = None
    plan = plan_of(world, repo, show=show)
    action, reason = row(plan, "awb-buildd.service")
    assert action == "restart" and "older release" in reason and plan["build_restart_wanted"] is False


# --------------------------------------------------------------------------- the dry run and the root run


@not_as_root
def test_the_dry_run_names_the_restart_and_the_run_restarts_waits_and_records_it(world, tmp_path, capsys):
    """Planted: the plan of an awb-web.service on an older release goes through root. The dry run prints its plan
    line and the install.sh step; the real run hands it to install.sh --only (the web side is live), waits for it,
    records it restarted on the target, so the next plan no longer keeps the older release."""
    repo, c0, c1 = two_commits(world, tmp_path)
    journal(world, c0, running={"awb-web.service": OLDER})
    (world.root / "releases" / OLDER).mkdir()
    started(world, awb_web_service=BEFORE)
    plan = plan_of(world, repo)
    assert plan["web"] is True
    world.runner.plan = plan
    assert world.main(["--dry-run"]) == 0
    out = capsys.readouterr().out.splitlines()
    line = "# %-28s %-14s %s" % ("awb-web.service", "restart",
                                  "runs an older release (started before the switch to %s)" % deploy.short(c0))
    assert line in out, out
    rel = world.root / "releases" / c1
    assert "+ %s --only awb-web.service" % (rel / "seal" / "web" / "install.sh") in out
    assert any(x.startswith("+ wait until active:") and "awb-web.service" in x for x in out)
    world.runner.calls.clear()
    assert world.main([]) == 0, capsys.readouterr()
    install = world.runner.named("install.sh")
    assert [c[1:] for c, _ in install] == [["--only", "awb-web.service"]]
    j = json.loads((world.root / "DEPLOYED").read_text())
    assert j["units"]["awb-web.service"]["result"] == "restarted" and j["pending"] == []
    assert j["running"]["awb-web.service"] == c1
    out = capsys.readouterr().out
    assert "wait until active:" in out and "awb-web.service" in out.split("wait until active:")[1].splitlines()[0]
    # the plan kept the older release for this run (its keep was read before the restart); the journal no longer
    # names it, so the next plan leaves it to the retention
    assert OLDER not in j["running"].values()
    assert OLDER not in plan_of(world, repo)["keep"]
