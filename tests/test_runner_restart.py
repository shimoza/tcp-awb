"""fix-runner-restart (2026-10-20): the build runner awb-buildd ran six days through about thirty deploys.

The cause: with the web side live a deploy handed every service it restarts to `seal/web/install.sh --only`, whose
restart lines never name awb-buildd; the deploy marked the runner restarted and its wait saw the same process
active. Now a service install.sh does not name goes to `systemctl restart`, the runner exits at its next idle moment
when the live release is not the one it runs. `awb deploy status` (and so
seal/verify.sh) names the runner's release and fails when an idle runner runs an older one.
"""
from __future__ import annotations

import ast
import os
from pathlib import Path

import pytest

from awb import build, buildrun, deploy
from tests.conftest import not_as_root
from tests.test_build_runner import seeded  # noqa: F401 (a fixture)
from tests.test_deploy import World

REPO = Path(__file__).resolve().parent.parent
OLD = "a" * 40
NEW = "b" * 40


@pytest.fixture
def dworld(tmp_path):
    return World(tmp_path / "d")


@pytest.fixture
def runner_world(tmp_path, seeded):  # noqa: F811
    w = buildrun.World(tmp_path / "w", like=seeded)
    yield w
    w.close()


# --------------------------------------------------------------------------- (a) the import map


def test_every_module_of_the_runner_restarts_awb_buildd():
    """Planted: each file the runner imports, directly, through parts.join or inside a function (the selftest)."""
    um = deploy.unit_map(deploy.DirTree(REPO))
    closure = um.closures["awb-buildd.service"]
    split = ["awb/buildrun.py", "awb/build_units.py", "awb/build_unit_side.py", "awb/build_checks.py",
             "awb/build_probe.py", "awb/build_selftest.py", "awb/parts.py"]
    assert closure is not None and set(split) <= closure
    eff = um.effects(split)
    assert all(rel in eff.units["awb-buildd.service"] for rel in split)
    assert eff.unmapped == []


def test_every_part_named_in_parts_join_is_in_the_closure_of_its_door():
    """A part reaches its units through an import of the door, not through its name in parts.join, which the map does
    not read (tests/test_deploy.py plants a buildrun.py without the selftest's import and wants it in no closure).
    So every part a door names must also sit in the closure of each unit that holds the door."""
    um = deploy.unit_map(deploy.DirTree(REPO))
    missing = []
    for rel in sorted(um.imap.files):
        for node in ast.walk(ast.parse((REPO / rel).read_text(encoding="utf-8"))):
            if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "join"
                    and isinstance(node.func.value, ast.Name) and node.func.value.id == "parts"):
                continue
            for arg in node.args[1:]:
                if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                    part = arg.value.replace(".", "/") + ".py"
                    for unit, closure in um.closures.items():
                        if closure and rel in closure and part not in closure:
                            missing.append("%s: %s" % (unit, part))
    assert missing == []
    assert "awb/build_selftest.py" in um.closures["awb-buildd.service"]


# --------------------------------------------------------------------------- (c) install.sh skipped the runner


def test_install_sh_names_no_build_runner_so_systemctl_restarts_it():
    named = deploy.install_sh_units((REPO / "seal" / "web" / "install.sh").read_text(encoding="utf-8"))
    assert "awb-ask.service" in named and "awb-buildd.service" not in named
    ask, buildd, web = ({"unit": "awb-ask.service"}, {"unit": "awb-buildd.service"}, {"unit": "awb-web.service"})
    assert deploy.split_services(True, [ask, buildd], [web], named) == ([ask, web], [buildd])
    assert deploy.split_services(False, [ask, buildd], [], named) == ([], [ask, buildd])
    assert deploy.split_services(True, [ask, buildd], [web], None) == ([ask, buildd, web], [])


@not_as_root
def test_a_live_web_side_still_restarts_the_build_runner(dworld, capsys):
    """2026-10-20: a plan that restarts awb-ask and awb-buildd with the web side live. Before the fix both went to
    install.sh --only, which restarts awb-ask only. The journal said awb-buildd restarted."""
    dworld.runner.plan = dworld.plan(actions={"awb-ask.service": "restart", "awb-buildd.service": "restart"})
    assert dworld.main([]) == 0, capsys.readouterr()
    install = dworld.runner.named("install.sh")
    assert [c[1:] for c, _ in install] == [["--only", "awb-ask.service"]]
    assert ["systemctl", "restart", "awb-buildd.service"] in dworld.runner.systemctl()


def test_the_plan_names_the_units_of_install_sh_and_validates_them(dworld):
    plan = dworld.plan(install_units=["awb-ask.service"])
    allowed = {u["unit"] for u in plan["units"]}
    assert deploy.validate_plan(plan, allowed)["install_units"] == ["awb-ask.service"]
    with pytest.raises(deploy.Refused):
        deploy.validate_plan(dworld.plan(install_units=["../etc/passwd"]), allowed)


# --------------------------------------------------------------------------- (b) the runner's own exit


def test_running_release_is_the_commit_folder_of_the_code(tmp_path):
    f = tmp_path / "opt" / "releases" / OLD / "awb" / "buildrun.py"
    f.parent.mkdir(parents=True)
    f.write_text("")
    (tmp_path / "opt" / "src").symlink_to("releases/%s" % OLD)
    assert buildrun.running_release(str(tmp_path / "opt" / "src" / "awb" / "buildrun.py")) == OLD
    assert buildrun.running_release(str(REPO / "awb" / "buildrun.py")) is None


def _live(tmp_path: Path, release: str) -> Path:
    opt = tmp_path / "opt"
    opt.mkdir(exist_ok=True)
    src = opt / "src"
    if src.is_symlink():
        src.unlink()
    src.symlink_to("releases/%s" % release)
    return opt / "DEPLOYED"


def test_an_idle_runner_exits_when_the_live_release_is_newer(runner_world, tmp_path):
    """No restart-wanted came (the deploy of 2026-10-20 never wrote one): the live link alone makes it exit."""
    runner_world.runs.mkdir(exist_ok=True)
    runner_world.host.deployed = _live(tmp_path, NEW)
    r = buildrun.Runner(runner_world.host)
    r.release = OLD
    assert r.restart_due() is True
    assert r.serve() == 75
    _live(tmp_path, OLD)
    assert r.restart_due() is False
    r.release = None                 # code that is no release (a working copy) never exits for this
    _live(tmp_path, NEW)
    assert r.restart_due() is False


def test_the_runner_exits_after_a_check_and_records_busy_then_removes_its_record(runner_world, tmp_path):
    """A deploy leaves restart-wanted while the runner judges a branch: the runner finishes the check, then exits
    before the next poll. While it works runs/runner.state says busy, with its pid and release."""
    runner_world.runs.mkdir(exist_ok=True)
    runner_world.host.deployed = _live(tmp_path, OLD)
    r = buildrun.Runner(runner_world.host)
    r.release = OLD
    seen = []

    def one_check():
        seen.append(deploy.read_runner_state(runner_world.runs / deploy.RUNNER_STATE))
        (runner_world.runs / deploy.RESTART_WANTED).write_text(NEW + "\n")

    def poll_once():
        if seen:
            raise AssertionError("a second poll after restart-wanted")
        return r._guarded("C-001", one_check)

    r.poll_once = poll_once
    assert r.serve() == 75
    assert seen[0]["pid"] == os.getpid() and seen[0]["release"] == OLD and seen[0]["state"] == "busy"
    assert not (runner_world.runs / deploy.RESTART_WANTED).exists()
    assert not (runner_world.runs / deploy.RUNNER_STATE).exists()


def test_a_check_in_its_own_process_writes_no_runner_record(runner_world):
    runner_world.runs.mkdir(exist_ok=True)
    r = buildrun.Runner(runner_world.host)
    r._guarded("C-001", lambda: None)
    assert not (runner_world.runs / deploy.RUNNER_STATE).exists()
    assert (runner_world.runs / build.STOP_FILE).exists() is False


# --------------------------------------------------------------------------- the status line


def test_the_status_names_the_runner_release_and_fails_for_an_old_idle_one(tmp_path):
    alive = {1: True, 2: False}.get
    idle_old = {"pid": 1, "release": OLD, "state": "idle", "since": "2026-10-14T07:32:00"}
    line, bad = deploy.build_runner_line(idle_old, NEW, flipped=0.0, now=1000.0, alive=alive)
    assert bad and "release %s" % OLD[:7] in line and "older than the live release %s" % NEW[:7] in line
    # just after the flip it gets its poll; a busy one exits after its step; the live release passes
    assert not deploy.build_runner_line(idle_old, NEW, flipped=990.0, now=1000.0, alive=alive)[1]
    assert not deploy.build_runner_line(dict(idle_old, state="busy"), NEW, 0.0, 1000.0, alive)[1]
    assert not deploy.build_runner_line(idle_old, OLD, 0.0, 1000.0, alive)[1]
    # a runner that is not running starts with the live release; no record says so
    line, bad = deploy.build_runner_line(dict(idle_old, pid=2), NEW, 0.0, 1000.0, alive)
    assert not bad and "not running" in line
    assert not deploy.build_runner_line(None, NEW, 0.0, 1000.0, alive)[1]
    # the record the runner writes is the one the status reads
    p = tmp_path / deploy.RUNNER_STATE
    p.write_text("%d\t%s\tidle\t2026-10-14T07:32:00\n" % (os.getpid(), OLD))
    assert deploy.read_runner_state(p) == {"pid": os.getpid(), "release": OLD, "state": "idle",
                                           "since": "2026-10-14T07:32:00"}
    p.write_text("not a record\n")
    assert deploy.read_runner_state(p) is None


def test_the_runner_record_round_trips(runner_world):
    runner_world.runs.mkdir(exist_ok=True)
    r = buildrun.Runner(runner_world.host)
    r.release, r.serving = OLD, True
    r.record("idle")
    got = deploy.read_runner_state(runner_world.runs / deploy.RUNNER_STATE)
    assert got["pid"] == os.getpid() and got["release"] == OLD and got["state"] == "idle"
    assert buildrun.RUNNER_STATE == deploy.RUNNER_STATE and buildrun.RESTART_WANTED == deploy.RESTART_WANTED


def test_verify_sh_runs_the_status_for_the_runner():
    text = (REPO / "seal" / "verify.sh").read_text(encoding="utf-8")
    assert "the build runner, while idle, runs the live release (awb deploy status)" in text
