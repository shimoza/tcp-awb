"""awb/tcp/recheckwatch.py: the watchdog of the fact-check service (design F5 of 2026-10-14, sections 5 and 7).

The key service is a stand-in that answers the list paths of the sweep (awb/tcp/sweep.py SOURCES, NETWORK_SOURCES
and NAMED_SOURCES) from an invented cloud in memory, the deletes and the reads of one resource; the mail is a list.
The register, the project and the shared side are temporary folders of the `home` fixture. Every id and value here
is invented; no call leaves the process.
"""
from __future__ import annotations

import copy
import datetime
import itertools
import re
from pathlib import Path

import pytest

from awb import cli, projects
from awb.tcp import lab, recheck, recheckwatch
from tests.lab_fake import Clock
from tests.test_lab import RECIPE, write_recipe
from tests.test_seal import SETUP, USER, VERIFY, commands, fakebin, no_marks, run_script  # noqa: F401

PROJECT = recheck.SERVICE_PROJECT
OTHER = "tcp-zz2z"
TENANT = "test-1"
NOW = datetime.datetime(2026, 10, 15, 12, 0, tzinfo=datetime.timezone.utc)
PAST = "2026-10-14"
TODAY = "2026-10-15"
LISTS = {s[2]: (s[0], s[3]) for s in recheck._sources()}
PATH_OF = {kind: path for path, (kind, _) in LISTS.items()}


def rid(n: int) -> str:
    """An invented id, in pieces."""
    return "-".join(("0000aaaa", "0000", "4000", "8000", "%012d" % n))


def tagged(project: str, expiry: str) -> list[str]:
    return ["awb-project=%s" % project, "awb-expiry=%s" % expiry]


class Cloud:
    """The stand-in key service: `items` per list path, deletes that mark an item gone, `fail_delete` the ids whose
    delete answers 500, `tenants` what the operation tenants answers."""

    def __init__(self):
        self.items: dict[str, list[dict]] = {}
        self.gone: set[str] = set()
        self.fail_delete: set[str] = set()
        self.calls: list[dict] = []
        self.tenants = {TENANT: {"roles": ["lab", "read"], "secrets": [], "regions": ["eu-de"]}}

    def add(self, kind: str, item: dict) -> dict:
        self.items.setdefault(PATH_OF[kind], []).append(item)
        return item

    def _ids(self) -> set[str]:
        return {str(v) for items in self.items.values() for it in items for v in it.values() if isinstance(v, str)}

    def writes(self) -> list[dict]:
        return [c for c in self.calls if c.get("op") == "call" and c["method"] not in ("GET", "HEAD")]

    def __call__(self, req: dict) -> dict:
        self.calls.append(req)
        if req.get("op") == "tenants":
            return {"ok": True, "tenants": self.tenants}
        method, path = req["method"], req["path"]
        if method == "GET" and path in LISTS:
            kind, key = LISTS[path]
            items = [it for it in self.items.get(path, [])
                     if not any(v in self.gone for v in it.values() if isinstance(v, str))]
            return {"ok": True, "status": 200, "data": {key: items}, "text": ""}
        if method == "POST" and path == "/v1/{project_id}/cloudservers/delete":
            ids = [s["id"] for s in req["body"]["servers"]]
            if set(ids) & self.fail_delete:
                return {"ok": True, "status": 500, "data": {"error_msg": "internal"}, "text": ""}
            self.gone |= set(ids)
            return {"ok": True, "status": 200, "data": {"job_id": "job-1"}, "text": ""}
        last = path.rsplit("/", 1)[-1]
        if method == "DELETE":
            if last in self.fail_delete:
                return {"ok": True, "status": 500, "data": {"error_msg": "internal"}, "text": ""}
            if last not in self._ids() or last in self.gone:
                return {"ok": True, "status": 404, "data": {"error_msg": "not found"}, "text": ""}
            self.gone.add(last)
            return {"ok": True, "status": 204, "data": None, "text": ""}
        if method == "GET":
            if last in self._ids() and last not in self.gone:
                return {"ok": True, "status": 200, "data": {"item": {"id": last}}, "text": ""}
            return {"ok": True, "status": 404, "data": {"error_msg": "not found"}, "text": ""}
        return {"ok": True, "status": 405, "data": {"error_msg": "method"}, "text": ""}


@pytest.fixture(autouse=True)
def state(tmp_path, monkeypatch) -> Path:
    """The owner's state folder of the test, where the marks and the fallback rows go."""
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    return tmp_path / "state" / "awb"


@pytest.fixture
def project(home, monkeypatch, tmp_path):
    """The service project in the register with its folder and RESOURCES.md; the recipe folder of the test."""
    folder = home.projects_root / PROJECT
    folder.mkdir(parents=True)
    (folder / "SCOPE.md").write_text("# Scope\n\n- code: %s\n" % PROJECT, encoding="utf-8")
    (folder / "RESOURCES.md").write_text(projects._resources(PROJECT), encoding="utf-8")
    home.projects_register.parent.mkdir(parents=True, exist_ok=True)
    projects._save(home, [projects.Project(PROJECT, "project", projects.NO_CUSTOMER, "tcp", str(folder),
                                           projects.memory_key(str(folder)), "active", "2026-10-11")])
    monkeypatch.setenv(lab.RECIPES_ENV, str(tmp_path / "recipes"))
    return folder


class World:
    def __init__(self):
        self.mails: list[tuple[str, str]] = []
        self.sent = True

    def mail(self, subject: str, message: str) -> bool:
        if self.sent:
            self.mails.append((subject, message))
        return self.sent


def go(cloud, world, tenants=()):
    clock = Clock()
    lines: list[str] = []
    errors: list[str] = []
    code = recheckwatch.run(PROJECT, list(tenants), send=cloud, mail=world.mail, clock=clock, sleep=clock.sleep,
                            now=NOW, out=lines.append, err=errors.append)
    return code, lines, errors


def rows(folder: Path) -> list[dict]:
    return recheckwatch.table_rows((folder / "RESOURCES.md").read_text())


# --------------------------------------------------------------------------- the reaper


def test_an_expired_tagged_resource_is_deleted_and_proven_gone(project):
    """A server of the service project whose awb-expiry has passed and that no row names is deleted by the delete
    call of its kind and proven gone by the listing; its row in RESOURCES.md says deleted with the proof."""
    cloud, world = Cloud(), World()
    cloud.add("ecs", {"id": rid(1), "name": "probe", "status": "ACTIVE", "tags": tagged(PROJECT, PAST)})
    code, lines, errors = go(cloud, world)
    assert code == 0, errors
    assert [(c["method"], c["path"], c["role"], c["project"]) for c in cloud.writes()] == [
        ("POST", "/v1/{project_id}/cloudservers/delete", "lab", PROJECT)]
    assert rid(1) in cloud.gone
    row = next(r for r in rows(project) if r["id"] == rid(1))
    assert row["state"] == "deleted" and row["handle"].startswith("ecs-")
    assert "awb kb recheck-watch %s: deleted after its expiry by the delete call of the kind ecs" % TODAY in row["note"]
    assert "the listing of ecs no longer holds it" in row["note"]
    assert any("deleted and proven gone" in x for x in lines)
    assert world.mails == []
    assert not any(rid(1) in x for x in lines + errors)


def test_a_resource_a_lab_row_names_is_deleted_by_its_recipe_teardown(project, tmp_path):
    """A VPC left by a lab run: its row names the run (`awb lab run RECIPE DATE`), so the recipe's teardown call for
    its type deletes it and the recipe's read proves it gone; the row keeps its handle and type."""
    recipe = copy.deepcopy(RECIPE)
    recipe["name"] = "vpc-check"
    recipe["steps"][0]["resource"] = {"kind": "vpc", "type": "vpc under test", "cost": "none"}
    recipe["teardown"]["widget"] = {
        "call": {"method": "DELETE", "service": "vpc", "path": "/v1/{project_id}/vpcs/${id}"},
        "gone": {"call": {"method": "GET", "service": "vpc", "path": "/v1/{project_id}/vpcs/${id}"},
                 "status": [404]}}
    write_recipe(tmp_path / "recipes", recipe)
    with open(project / "RESOURCES.md", "a", encoding="utf-8") as fh:
        fh.write("| vpc-7 | %s | vpc under test | eu-de | none | %s | live | awb lab run vpc-check %s: LEFT LIVE |\n"
                 % (rid(2), PAST, PAST))
    cloud, world = Cloud(), World()
    cloud.add("vpc", {"id": rid(2), "name": "%s-lab-20261014-abcd" % PROJECT, "status": "OK"})
    code, lines, errors = go(cloud, world)
    assert code == 0, errors
    assert [(c["method"], c["path"]) for c in cloud.writes()] == [("DELETE", "/v1/{project_id}/vpcs/%s" % rid(2))]
    assert ("GET", "/v1/{project_id}/vpcs/%s" % rid(2)) in [(c.get("method"), c.get("path")) for c in cloud.calls]
    row = next(r for r in rows(project) if r["id"] == rid(2))
    assert (row["handle"], row["type"], row["state"]) == ("vpc-7", "vpc under test", "deleted")
    assert "by the teardown of recipe vpc-check, the read answered 404" in row["note"]
    assert any(x.startswith("recheck-watch: vpc-7 (vpc) on %s: deleted" % TENANT) for x in lines)


def test_a_resource_not_yet_expired_is_left(project):
    """An expiry of today has not passed: the resource is listed and nothing is sent but reads."""
    cloud, world = Cloud(), World()
    cloud.add("ecs", {"id": rid(3), "name": "probe", "status": "ACTIVE", "tags": tagged(PROJECT, TODAY)})
    cloud.add("evs", {"id": rid(4), "name": "disk", "status": "available", "tags": tagged(PROJECT, "2026-11-01")})
    code, lines, errors = go(cloud, world)
    assert code == 0, errors
    assert cloud.writes() == [] and cloud.gone == set()
    assert rows(project) == []
    assert "recheck-watch: no expired resource of %s on %s" % (PROJECT, TENANT) in lines


def test_a_resource_that_cannot_be_deleted_mails_once_and_not_again(project, state):
    """A delete that answers 500 leaves the resource live: exit 4, its row says LEFT LIVE with the call that deletes
    it, one mail with its handle and kind and never its id. The next call tries the delete again and mails nothing;
    a mail that was not sent is tried again at the next call."""
    cloud, world = Cloud(), World()
    cloud.add("evs", {"id": rid(5), "name": "disk", "status": "available", "tags": tagged(PROJECT, PAST)})
    cloud.fail_delete.add(rid(5))
    world.sent = False
    code, lines, errors = go(cloud, world)
    assert code == 4 and world.mails == []
    assert "recheck-watch: 1 resource(s) left live: mail not sent, tried again next time" in lines
    world.sent = True
    code, lines, errors = go(cloud, world)
    assert code == 4 and len(world.mails) == 1
    subject, message = world.mails[0]
    assert subject == "Workbench recheck-watch %s: 1 resource(s) left live" % PROJECT
    handle = next(r for r in rows(project) if r["id"] == rid(5))["handle"]
    assert re.search(r"^- %s \(evs, evs\) on %s: the delete answered 500$" % (handle, TENANT), message, re.M)
    assert rid(5) not in message and rid(5) not in subject
    row = next(r for r in rows(project) if r["id"] == rid(5))
    assert row["state"] == "live" and "LEFT LIVE after its expiry: the delete answered 500" in row["note"]
    assert "awb cloud call DELETE evs /v2/{project_id}/cloudvolumes/%s --tenant %s --role lab --project %s" % (
        rid(5), TENANT, PROJECT) in row["note"]
    mark = state / "recheck-watch.tsv"
    assert rid(5) not in mark.read_text() and recheckwatch.id_mark(rid(5)) in mark.read_text()
    deletes = len(cloud.writes())
    code, lines, errors = go(cloud, world)
    assert code == 4 and len(world.mails) == 1 and len(cloud.writes()) > deletes


def test_a_resource_of_another_project_is_never_touched(project):
    """Expired resources tagged or named with another project's code, an untagged one and a named policy of
    another project are never deleted: only reads go out."""
    cloud, world = Cloud(), World()
    cloud.add("ecs", {"id": rid(6), "name": "probe", "status": "ACTIVE", "tags": tagged(OTHER, PAST)})
    cloud.add("vpc", {"id": rid(7), "name": "%s-lab-20261001-wxyz" % OTHER, "status": "OK"})
    cloud.add("eip", {"id": rid(8), "status": "DOWN"})
    cloud.add("waf", {"id": rid(9), "name": "%s-lab-20261001-qrst" % OTHER})
    code, lines, errors = go(cloud, world)
    assert code == 0, errors
    assert cloud.writes() == [] and cloud.gone == set()
    assert all(c.get("op") == "tenants" or c["method"] == "GET" for c in cloud.calls)
    assert rows(project) == [] and world.mails == []


def test_a_named_resource_of_the_service_project_is_deleted_after_the_networks_order(project):
    """The named sources of FK3: an expired lab-named WAF policy of the service project is found by its name and
    deleted; a subnet goes before its VPC and takes the VPC id from its listing."""
    cloud, world = Cloud(), World()
    name = "%s-lab-20261014-abcd" % PROJECT
    cloud.add("vpc", {"id": rid(10), "name": name, "status": "OK"})
    cloud.add("sub", {"id": rid(11), "name": name, "status": "ACTIVE", "vpc_id": rid(10)})
    cloud.add("waf", {"id": rid(12), "name": name})
    code, lines, errors = go(cloud, world)
    assert code == 0, errors
    assert [(c["method"], c["path"]) for c in cloud.writes()] == [
        ("DELETE", "/v1/{project_id}/waf/policy/%s" % rid(12)),
        ("DELETE", "/v1/{project_id}/vpcs/%s/subnets/%s" % (rid(10), rid(11))),
        ("DELETE", "/v1/{project_id}/vpcs/%s" % rid(10))]
    assert {r["id"]: r["state"] for r in rows(project)} == {rid(10): "deleted", rid(11): "deleted",
                                                            rid(12): "deleted"}


def test_rows_go_to_the_owners_state_folder_when_the_project_folder_is_not_there(home, state):
    """Without a folder of the service project in the register the rows go to RESOURCES-watch.md in the owner's
    state folder in the same form; the output says so."""
    cloud, world = Cloud(), World()
    cloud.add("ecs", {"id": rid(13), "name": "probe", "status": "ACTIVE", "tags": tagged(PROJECT, PAST)})
    code, lines, errors = go(cloud, world)
    assert code == 0, errors
    got = recheckwatch.table_rows((state / recheckwatch.RECORD_MD).read_text())
    assert [(r["id"], r["state"]) for r in got] == [(rid(13), "deleted")]
    assert any("cannot be written here; the row of ecs-" in x and "owner's state folder" in x for x in lines)


def test_no_tenant_with_both_keys_stops_with_exit_2(project):
    cloud, world = Cloud(), World()
    cloud.tenants = {TENANT: {"roles": ["read"], "secrets": [], "regions": []}}
    code, lines, errors = go(cloud, world)
    assert code == 2 and errors == ["awb kb recheck-watch: the key service holds no tenant with a read and a lab key "
                                    "(locked?)"]
    assert cloud.writes() == []


# --------------------------------------------------------------------------- the run ends of state.tsv


def test_a_run_end_in_state_tsv_mails_once_per_line(project, home):
    """Every line of state.tsv gets one mail of its own with its counts, exit and inventory; the next call mails
    nothing, a new line gets one more mail; a line that does not match is passed over."""
    tsv = home.shared / "recheck" / "state.tsv"
    tsv.parent.mkdir(parents=True)
    tsv.write_text("2026-10-12\tread\t3\t1\t2\t4\t0\tnone\n"
                   "a line that is not a run\n"
                   "2026-10-13\twrite\t1\t0\t0\t0\t4\tdiffers\n", encoding="utf-8")
    cloud, world = Cloud(), World()
    code, lines, errors = go(cloud, world)
    assert code == 0, errors
    assert [s for s, _ in world.mails] == ["Workbench recheck: read run of 2026-10-12, exit 0",
                                           "Workbench recheck: write run of 2026-10-13, exit 4"]
    first, second = world.mails[0][1], world.mails[1][1]
    assert "verdicts: 3 confirmed, 1 refuted, 2 undecided" in first and "for the owner: 4" in first
    assert "next: the list for the owner in RESULT.md" in first and "inventory" not in first
    assert "exit: 4 (a resource left live or the inventory differs)" in second and "inventory: differs" in second
    assert "evidence: evidence/recheck-2026-10-13-<n>/RESULT.md of %s" % PROJECT in second
    go(cloud, world)
    assert len(world.mails) == 2
    with open(tsv, "a", encoding="utf-8") as fh:
        fh.write("2026-10-15\tread\t5\t0\t0\t0\t0\tnone\n")
    go(cloud, world)
    assert len(world.mails) == 3 and world.mails[2][0] == "Workbench recheck: read run of 2026-10-15, exit 0"


def test_a_run_end_mail_not_sent_is_tried_again(project, home):
    tsv = home.shared / "recheck" / "state.tsv"
    tsv.parent.mkdir(parents=True)
    tsv.write_text("2026-10-12\tread\t3\t1\t2\t0\t0\tnone\n", encoding="utf-8")
    cloud, world = Cloud(), World()
    world.sent = False
    code, lines, errors = go(cloud, world)
    assert "recheck-watch: run end of 2026-10-12 (read): mail not sent, tried again next time" in lines
    world.sent = True
    go(cloud, world)
    go(cloud, world)
    assert len(world.mails) == 1


# --------------------------------------------------------------------------- the command and the timer


def test_the_command_is_wired_and_refuses_a_bad_project(home, capsys):
    assert cli.main(["kb", "recheck-watch", "--project", "not-a-code"]) == 2
    assert "a project code reads like tcp-q7m4" in capsys.readouterr().err


def test_setup_installs_the_owner_timer_and_verify_proves_it_enabled(fakebin):
    """The dry run of seal/setup.sh writes awb-kb-recheck-watch.service (oneshot, as the owner, awb kb recheck-watch,
    unix sockets only, its own state folder as XDG_STATE_HOME and nothing else to write) and its timer every 15
    minutes, both root owned, then enables the timer; the dry run of seal/verify.sh lists the check that proves it
    enabled."""
    res = run_script(SETUP, ["--dry-run"], fakebin)
    assert res.returncode == 0, res.stderr
    no_marks(fakebin)
    out = res.stdout.splitlines()
    unit = out.index("+ write /etc/systemd/system/awb-kb-recheck-watch.service (mode 644, root:root)")
    body = [x[6:] for x in itertools.takewhile(lambda x: x.startswith("    | "), out[unit + 1:])]
    for line in ("Type=oneshot", "User=%s" % USER, "Group=awb", "ExecStart=/usr/local/bin/awb kb recheck-watch",
                 "RestrictAddressFamilies=AF_UNIX", "ProtectSystem=strict", "ProtectHome=read-only",
                 "StateDirectory=awb-kb-recheck-watch", "Environment=XDG_STATE_HOME=/var/lib/awb-kb-recheck-watch"):
        assert line in body, line
    assert not [x for x in body if x.startswith("ReadWritePaths=")]
    timer = out.index("+ write /etc/systemd/system/awb-kb-recheck-watch.timer (mode 644, root:root)")
    body = [x[6:] for x in itertools.takewhile(lambda x: x.startswith("    | "), out[timer + 1:])]
    assert "OnUnitActiveSec=15min" in body and "Unit=awb-kb-recheck-watch.service" in body
    assert "+ systemctl enable --now awb-kb-recheck-watch.timer" in commands(res.stdout)
    res = run_script(VERIFY, ["--dry-run"], fakebin)
    assert res.returncode == 0, res.stderr
    checks = [x[6:] for x in res.stdout.splitlines() if x.startswith("CHECK ")]
    assert "the timer awb-kb-recheck-watch.timer is enabled and runs awb kb recheck-watch as the owner" in checks
