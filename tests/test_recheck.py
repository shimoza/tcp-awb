"""awb/tcp/recheck.py: the recheck run of the fact-check service (design F4 of 2026-10-14, section 3).

The key service is the stand-in of tests/lab_fake.py (an invented cloud in memory) with a read answer for the
flavor list put in front of it; the knowledge base, the project and the shared side are temporary folders of the
`home` fixture. Every id and value here is invented; no call leaves the process.
"""
from __future__ import annotations

import copy
import datetime
from pathlib import Path

import pytest

from awb import cli, grants, kb, projects
from awb.tcp import lab, recheck
from tests.lab_fake import Clock, FakeKeyService
from tests.test_lab import RECIPE, write_recipe

PROJECT = "tcp-ab2c"
TENANT = "test-1"
TODAY = datetime.date.today()
OLD = (TODAY - datetime.timedelta(days=400)).isoformat()
FLAVORS = "/v1/{project_id}/cloudservers/flavors"
STRAY = "-".join(("0000aaaa", "0000", "4000", "8000", "000000000001"))     # an invented id, in pieces

READ_BLOCK = ("tier: read\n"
              "calls: GET ecs %s\n"
              "confirms: flavors[name=s9.medium.1].os-extra_specs.cond:operation:az has eu-de-01(normal)\n"
              "refutes: flavors[name=s9.medium.1].os-extra_specs.cond:operation:az lacks eu-de-01(normal)\n"
              "control: flavors[name=s2.medium.1].os-extra_specs.cond:operation:az has sellout\n"
              "needs: none\n" % FLAVORS)


def write_block(recipe="widget-check", params="size=small") -> str:
    return ("tier: write\n"
            "calls: recipe %s %s\n"
            "confirms: the widget reaches the state active with the size asked for\n"
            "refutes: the widget has another size than the one asked for\n"
            "control: the forbidden path is refused, so the key reaches the project at all\n"
            "needs: none\n" % (recipe, params))


def flavor(name: str, state: str) -> dict:
    return {"name": name, "os-extra_specs": {"cond:operation:az": "eu-de-01(%s)" % state}}


class Cloud(FakeKeyService):
    """The lab stand-in with the flavor list of the read tier in front of it."""

    def __init__(self, s9="normal", s2="sellout", status=200):
        super().__init__()
        self.flavors = [flavor("s9.medium.1", s9), flavor("s2.medium.1", s2)]
        self.flavor_status = status

    def __call__(self, req: dict) -> dict:
        if req.get("op") == "call" and req.get("path") == FLAVORS:
            self.calls.append(req)
            if self.flavor_status != 200:
                return {"ok": True, "status": self.flavor_status,
                        "data": {"error_code": "Ecs.0001", "error_msg": "internal"}, "text": ""}
            return {"ok": True, "status": 200, "data": {"flavors": self.flavors}, "text": ""}
        return super().__call__(req)


@pytest.fixture
def project(home, monkeypatch, tmp_path):
    """An active project of the kind project with the owner's grant, the session in its folder, the recipe folder
    of the test and an inventory over the widgets of the stand-in."""
    folder = home.projects_root / PROJECT
    (folder / "evidence").mkdir(parents=True)
    (folder / "SCOPE.md").write_text("# Scope\n\n- code: %s\n" % PROJECT, encoding="utf-8")
    (folder / "RESOURCES.md").write_text(projects._resources(PROJECT), encoding="utf-8")
    home.projects_register.parent.mkdir(parents=True, exist_ok=True)
    projects._save(home, [projects.Project(PROJECT, "project", projects.NO_CUSTOMER, "tcp", str(folder),
                                           projects.memory_key(str(folder)), "active", "2026-10-11")])
    grants.grant(home, PROJECT, "project")
    monkeypatch.chdir(folder)
    monkeypatch.setenv(lab.RECIPES_ENV, str(tmp_path / "recipes"))
    write_recipe(tmp_path / "recipes", RECIPE)
    with_control = copy.deepcopy(RECIPE)
    with_control["name"] = "widget-control"
    for c in with_control["checks"]:
        if c["id"] == "size":
            c["id"] = "control"
    write_recipe(tmp_path / "recipes", with_control)
    monkeypatch.setattr(recheck, "_sources",
                        lambda: (("wid", "vpc", "/v1/{project_id}/widgets", "widgets", "marker", {}, False),))
    return folder


def entry(statement: str, block: str | None, **kw) -> kb.Entry:
    args = dict(scope="tcp", tags=["ecs"], grade="live", cls="availability", source="live API call, eu-de",
                checked=OLD)
    args.update(kw)
    e = kb.add(statement, **args)
    if block is not None:
        kb.how(e.id, block)
    return kb.get(e.id)


def go(cloud, tier="read", ids=(), dry_run=False):
    clock = Clock()
    lines: list[str] = []
    errors: list[str] = []
    code = recheck.run(tier, list(ids), PROJECT, TENANT, dry_run=dry_run, send=cloud, clock=clock,
                       sleep=clock.sleep, out=lines.append, err=errors.append)
    return code, lines, errors


def run_dir(folder: Path) -> Path:
    dirs = sorted((folder / "evidence").glob("recheck-*"))
    assert len(dirs) == 1, dirs
    return dirs[0]


def state(home) -> list[list[str]]:
    return [line.split("\t") for line in (home.shared / "recheck" / "state.tsv").read_text().splitlines()]


# --------------------------------------------------------------------------- the read tier


def test_a_confirmed_read_tier_rechecks_the_entry_and_the_rest_is_listed(project, home):
    e = entry("ECS flavor s9.medium.1 is offered in eu-de-01", READ_BLOCK)
    bare = entry("ECS flavor s3.large.2 is offered in eu-de-02", None)
    owner = entry("A GPU flavor boots with its driver in eu-de", READ_BLOCK.replace("tier: read", "tier: decision")
                  .replace("calls: GET ecs %s\n" % FLAVORS, "").replace("needs: none", "needs: a GPU flavor"))
    cloud = Cloud()
    code, lines, errors = go(cloud)
    assert code == 0, errors
    got = kb.get(e.id)
    assert not got.is_expired() and got.grade == "live" and got.checked == TODAY.isoformat()
    folder = run_dir(project)
    assert got.source.endswith("%s/%s.md" % (folder.relative_to(project).as_posix(), e.id))
    assert kb.get(bare.id).is_expired() and kb.get(owner.id).is_expired()
    result = (folder / "RESULT.md").read_text()
    assert "%s: confirmed" % e.id in result and "no block: 1 (%s)" % bare.id in result
    assert "for the owner: 1" in result and "%s (decision): needs a GPU flavor" % owner.id in result
    assert "2 inventory before: not taken (a read run creates nothing)" in result
    assert "s9.medium.1" in (folder / "calls.log").read_text()
    assert (folder / ("%s.md" % e.id)).is_file()
    assert [r["method"] for r in cloud.calls] == ["GET"] and cloud.calls[0]["role"] == "read"
    assert state(home) == [[TODAY.isoformat(), "read", "1", "0", "0", "1", "0", "none"]]


def test_a_refuted_read_tier_leaves_the_entry_expired_with_the_answers(project, home):
    e = entry("ECS flavor s9.medium.1 is offered in eu-de-01", READ_BLOCK)
    code, lines, errors = go(Cloud(s9="sellout"))
    assert code == 0, errors
    got = kb.get(e.id)
    assert got.is_expired() and got.statement == e.statement and got.source == e.source
    result = (run_dir(project) / "RESULT.md").read_text()
    assert "%s: refuted (refutes holds, confirms does not); stays expired" % e.id in result
    assert state(home)[0][2:5] == ["0", "1", "0"]


@pytest.mark.parametrize("cloud, said", [
    (dict(s2="normal"), "the control does not hold"),
    (dict(status=500), "call 1 answered 500"),
])
def test_an_undecided_read_tier_writes_nothing(project, home, cloud, said):
    e = entry("ECS flavor s9.medium.1 is offered in eu-de-01", READ_BLOCK)
    code, lines, errors = go(Cloud(**cloud))
    assert code == 0, errors
    assert kb.get(e.id) == e
    folder = run_dir(project)
    assert said in (folder / "RESULT.md").read_text()
    if cloud.get("status"):
        assert "error_code Ecs.0001, message internal" in (folder / ("%s.md" % e.id)).read_text()
    assert state(home)[0][2:5] == ["0", "0", "1"]


def test_a_prose_line_is_not_a_test_and_stays_undecided():
    block = {"confirms": "the size reads normal", "refutes": "status is 404", "control": "status is 200"}
    answers = [{"ok": True, "status": 200, "data": {}}]
    assert recheck.read_verdict(block, answers) == ("undecided", "the confirms line is not a test the run reads")
    assert recheck.dig({"a": [{"n": "x.1", "v": [1, 2]}]}, "a[n=x.1].v") == [1, 2]
    assert recheck.holds((1, "status", "is", "200"), answers) is True


def test_an_event_is_amended_with_the_state_of_the_day(project, home):
    e = entry("On 2026-07-20 a create of ECS flavor s9.medium.1 in eu-de-01 answered sold out",
              READ_BLOCK.replace("needs: none", "event: 2026-07-20\nneeds: none"))
    code, lines, errors = go(Cloud())
    assert code == 0, errors
    got = kb.get(e.id)
    assert got.checked == TODAY.isoformat() and not got.is_expired()
    assert got.statement.startswith(e.statement)
    assert got.statement.endswith("\nOn %s the state is: %s." % (TODAY.isoformat(), got.block["confirms"]))
    assert got.grade == e.grade and got.source == e.source
    assert "amended with the state of %s" % TODAY.isoformat() in (run_dir(project) / "RESULT.md").read_text()


def test_an_event_keeps_at_most_three_state_lines():
    text = "An event of a day."
    for day in range(1, 5):
        text = recheck.state_sentence(text, datetime.date(2026, 11, day), "the state of day %d" % day)
    lines = text.split("\n")
    assert lines[0] == "An event of a day." and len(lines) == 4
    assert lines[1].startswith("On 2026-11-02") and lines[-1] == "On 2026-11-04 the state is: the state of day 4."


# --------------------------------------------------------------------------- the write tier


def test_a_confirmed_write_tier_runs_the_recipe_with_an_equal_inventory(project, home):
    e = entry("A widget of size small reaches the state active in eu-de", write_block())
    cloud = Cloud()
    code, lines, errors = go(cloud, tier="write")
    assert code == 0, errors
    assert not kb.get(e.id).is_expired()
    folder = run_dir(project)
    result = (folder / "RESULT.md").read_text()
    assert "5 inventory after: inventory equal" in result and "%s: confirmed" % e.id in result
    assert (folder / "inventory-before.txt").read_text() == (folder / "inventory-after.txt").read_text()
    assert "# Lab run widget-check" in (folder / ("%s.md" % e.id)).read_text()
    assert cloud.created() and not cloud.live()
    assert state(home) == [[TODAY.isoformat(), "write", "1", "0", "0", "0", "0", "equal"]]


def test_a_refuted_write_tier_leaves_the_entry_expired(project):
    e = entry("A widget of size medium reaches the state active in eu-de", write_block(params="size=medium"))
    code, lines, errors = go(Cloud(), tier="write")
    assert code == 0, errors
    assert kb.get(e.id) == e
    assert "%s: refuted (the check size failed)" % e.id in (run_dir(project) / "RESULT.md").read_text()


def test_a_failed_control_check_is_undecided_and_writes_nothing(project):
    e = entry("A widget of size medium reaches the state active in eu-de",
              write_block(recipe="widget-control", params="size=medium"))
    code, lines, errors = go(Cloud(), tier="write")
    assert code == 0, errors
    assert kb.get(e.id) == e
    assert "%s: undecided (the control check failed" % e.id in (run_dir(project) / "RESULT.md").read_text()


def test_an_unequal_inventory_skips_the_write_back_and_exits_4(project, home):
    e = entry("A widget of size small reaches the state active in eu-de", write_block())
    cloud = Cloud()

    def stray(req):
        # another actor makes a widget while the recipe tears its own down
        if req.get("method") == "DELETE" and not any(it["body"].get("name") == "stray" for it in cloud.items.values()):
            cloud.items[STRAY] = {"coll": "widgets", "deleted": False, "reads": 0,
                                                                   "body": {"name": "stray"}}
    cloud.on_call = stray
    code, lines, errors = go(cloud, tier="write")
    assert code == 4
    assert kb.get(e.id) == e
    result = (run_dir(project) / "RESULT.md").read_text()
    assert "inventory differs: wid +1/-0; no write-back" in result
    assert "%s: confirmed" % e.id in result and "not written: the inventory differs" in result
    assert "0000aaaa" not in result
    assert state(home)[0][6:] == ["4", "differs"]


def test_a_resource_left_live_skips_every_further_entry(project, home):
    first = entry("A widget of size small reaches the state active in eu-de", write_block())
    # a day younger, so it comes second: the pick takes the oldest expiry first
    second = entry("A widget of size medium reaches the state active in eu-nl", write_block(params="size=medium"),
                   checked=(TODAY - datetime.timedelta(days=399)).isoformat())
    cloud = Cloud()
    cloud.fail_delete = {"widgets"}
    code, lines, errors = go(cloud, tier="write")
    assert code == 4
    result = (run_dir(project) / "RESULT.md").read_text()
    assert "4 delete: a resource is LEFT LIVE" in result
    assert "%s: skipped (a resource of an earlier entry is left live)" % second.id in result
    assert kb.get(first.id) == first and kb.get(second.id) == second
    assert len(cloud.created()) == 2


def test_an_unreadable_inventory_stops_the_write_run_before_any_create(project, home):
    e = entry("A widget of size small reaches the state active in eu-de", write_block())
    cloud = Cloud()
    cloud.list_fail = 1
    code, lines, errors = go(cloud, tier="write")
    assert code == 2 and cloud.created() == []
    assert "%s: skipped (the inventory before cannot be read)" % e.id in (run_dir(project) / "RESULT.md").read_text()


# --------------------------------------------------------------------------- pick, dry run and the command


def test_the_dry_run_makes_no_call_and_prints_the_recipe(project, home):
    w = entry("A widget of size small reaches the state active in eu-de", write_block())
    r = entry("ECS flavor s9.medium.1 is offered in eu-de-01", READ_BLOCK)
    cloud = Cloud()
    code, lines, errors = go(cloud, tier="write", dry_run=True)
    assert code == 0 and cloud.calls == []
    assert "%s: recipe widget-check with size=small" % w.id in lines and "other tier: 1 (%s)" % r.id in lines
    assert list((project / "evidence").glob("recheck-*")) == []
    code, lines, errors = go(cloud, ids=[r.id], dry_run=True)
    assert lines[0] == "%s: %s" % (r.id, r.block["calls"])


def test_an_id_that_is_not_expired_is_listed_and_an_unknown_id_refused(project, home):
    fresh = entry("ECS flavor s9.medium.1 is offered in eu-de-01", READ_BLOCK, checked=TODAY.isoformat())
    code, lines, errors = go(Cloud(), ids=[fresh.id], dry_run=True)
    assert code == 0 and "not expired, not checked: %s" % fresh.id in lines
    code, lines, errors = go(Cloud(), ids=["KB-ZZZZ"], dry_run=True)
    assert code == 2 and errors


def test_the_command_reaches_the_run_and_a_run_needs_a_tenant(project, home, capsys):
    entry("ECS flavor s9.medium.1 is offered in eu-de-01", READ_BLOCK)
    assert cli.main(["kb", "recheck-run", "--dry-run"]) == 0
    assert "GET ecs" in capsys.readouterr().out
    assert cli.main(["kb", "recheck-run", "--project", PROJECT]) == 2
    assert "needs --tenant" in capsys.readouterr().err
    assert cli.main(["kb", "recheck-run", "--tier", "decision", "--dry-run"]) == 2


def test_the_inventory_reads_every_source_the_key_service_can_send():
    kinds = {s[0] for s in recheck._sources()}
    assert {"ecs", "evs", "eip", "vpc", "sub", "sg", "waf", "smn", "smns", "rds"} <= kinds
    assert not kinds & {"elb", "lsn", "pool", "nat"}
