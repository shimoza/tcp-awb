"""awb/tcp/lab.py: lab recipes raise resources on a test tenant, check, write evidence and always tear down.

The runs go to the stand-in key service of tests/lab_fake.py (an invented cloud in memory) with a clock that moves
only when the run sleeps; one run goes through the real key service of awb/tcp/keys.py and the signing stand-in
gateway of tests/tcp_fake.py. Every id, key and secret here is invented.
"""
from __future__ import annotations

import copy
import json
import os
import re
import signal
from pathlib import Path

import pytest

from awb import cli, grants, projects
from awb.tcp import lab
from tests import fixtures
from tests.lab_fake import Clock, FakeKeyService

PROJECT = "tcp-ab2c"
TENANT = "test-1"
REPO_RECIPES = Path(__file__).resolve().parent.parent / "recipes"
SECRET_LONG = "".join(("Zq8Rk2", "Lm9Vx4", "Tn7Pw3", "Hs6Jd1Fg5"))     # built from pieces: an invented value
TOKEN = "".join(("gAAAAAB", "plantedtoken", "0123456789"))              # an invented token, in pieces
IMAGE_ID = ("0b1c2d3e", "aaaa", "4bbb", "8ccc", "0d1e2f3a4b5c")          # an invented image id, in pieces

RECIPE = {
    "name": "widget-check",
    "question": "Does a widget of size ${size} reach ACTIVE in ${region} and refuse the forbidden path?",
    "params": {"size": {"default": "small", "pattern": "^(small|medium)$"},
               "region": {"default": "eu-de", "pattern": "^eu-(de|nl)$"}},
    "budget": {"minutes": 10, "teardown_minutes": 5},
    "guard": {"resources": 2, "lifetime_minutes": 20},
    "steps": [
        {"id": "widget", "do": "create", "says": "a widget",
         "call": {"method": "POST", "service": "vpc", "path": "/v1/{project_id}/widgets",
                  "body": {"widget": {"name": "${name}", "size": "${size}", "tags": "${tags:list}"}}},
         "id_field": "widget.id", "resource": {"kind": "wid", "type": "widget", "cost": "none"}, "tags": "body"},
        {"id": "widget-active", "do": "wait", "says": "the widget is ACTIVE",
         "call": {"method": "GET", "service": "vpc", "path": "/v1/{project_id}/widgets/${widget}"},
         "until": {"field": "widget.status", "equals": "ACTIVE"}, "fail_on": ["ERROR"], "within": 120,
         "every": 10},
        {"id": "gadget", "do": "create", "says": "a gadget",
         "call": {"method": "POST", "service": "vpc", "path": "/v1/{project_id}/gadgets",
                  "body": {"gadget": {"name": "${name}", "widget": "${widget}"}}},
         "id_field": "gadget.id", "resource": {"kind": "gad", "type": "gadget", "cost": "small"},
         "tags": {"method": "POST", "service": "vpc", "path": "/v1/{project_id}/gadgets/${id}/tags/action",
                  "body": {"action": "create", "tags": "${tags:kv}"}},
         "confirm": "the gadget shape"},
        {"id": "read", "do": "call", "says": "read the widget",
         "call": {"method": "GET", "service": "vpc", "path": "/v1/{project_id}/widgets/${widget}"}},
        {"id": "try", "do": "call", "says": "the forbidden path",
         "call": {"method": "GET", "service": "vpc", "path": "/v1/{project_id}/forbidden"}},
    ],
    "checks": [
        {"id": "active", "step": "widget-active", "says": "the widget reaches ACTIVE", "reached": True},
        {"id": "size", "step": "read", "says": "the widget has the size asked for", "field": "widget.size",
         "equals": "${size}"},
        {"id": "refused", "step": "try", "says": "the forbidden path is refused", "status": [403]},
    ],
    "teardown": {
        "widget": {"call": {"method": "DELETE", "service": "vpc", "path": "/v1/{project_id}/widgets/${id}"},
                   "gone": {"call": {"method": "GET", "service": "vpc", "path": "/v1/{project_id}/widgets/${id}"},
                            "status": [404]}},
        "gadget": {"call": {"method": "DELETE", "service": "vpc", "path": "/v1/{project_id}/gadgets/${id}"},
                   "gone": {"call": {"method": "GET", "service": "vpc", "path": "/v1/{project_id}/gadgets/${id}"},
                            "status": [404], "within": 60}},
    },
    "kb": {"scope": "tcp", "tags": ["vpc"], "class": "api",
           "statement": "A widget of size ${size} reaches ACTIVE in ${region}."},
}


def write_recipe(folder: Path, recipe: dict) -> Path:
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / ("%s.json" % recipe["name"])
    path.write_text(json.dumps(recipe), encoding="utf-8")
    return path


@pytest.fixture
def project(home, monkeypatch, tmp_path):
    """An active project of the kind project with the owner's grant and the session working in its folder; also the
    recipe folder of the test."""
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
    return folder


def run(fake, recipe="widget-check", params=(), clock=None, **kw):
    clock = clock or Clock()
    lines: list[str] = []
    errors: list[str] = []
    code = lab.run(recipe, PROJECT, TENANT, list(params), send=fake, clock=clock, sleep=clock.sleep,
                   out=lines.append, err=errors.append, **kw)
    return code, lines, errors


def evidence(folder: Path) -> str:
    files = sorted((folder / "evidence").glob("lab-*.md"))
    assert len(files) == 1, files
    return files[0].read_text(encoding="utf-8")


def rows(folder: Path) -> list[dict]:
    head, out = None, []
    for line in (folder / "RESOURCES.md").read_text(encoding="utf-8").splitlines():
        if not line.startswith("|"):
            continue
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if head is None:
            head = cells
        elif not all(set(c) <= set("-: ") for c in cells):
            out.append(dict(zip(head, cells)))
    return out


def writes(fake) -> list[tuple[str, str]]:
    return [(r["method"], r["path"]) for r in fake.calls if r["method"] != "GET"]


# --------------------------------------------------------------------------- a run that passes


def _recipe_lab_writes() -> dict:
    """The settings of keys.conf with the lab_writes line of the recipes of this test (K9: without the line the lab
    key signs no write), the line awb keys lab-writes --from-recipes prints."""
    from awb.tcp import labwrites, xchg

    return dict(xchg.read_settings(), lab_writes=", ".join(" ".join(t) for t in labwrites.triples()))

def test_a_recipe_that_passes_tags_every_resource_records_it_and_proves_it_gone(project):
    fake = FakeKeyService(polls_to_active=2)
    code, lines, errors = run(fake)
    assert code == lab.EXIT_OK, (lines, errors)
    assert len(fake.created()) == 2 and fake.live() == []
    # the grant probe first, with the read key; every other call with the lab key and the project code
    assert fake.calls[0]["role"] == "read" and fake.calls[0]["path"] == "/v1/{project_id}/vpcs"
    assert all(r["role"] == "lab" and r["project"] == PROJECT and r["tenant"] == TENANT for r in fake.calls[1:])
    widget, gadget = fake.items.values()
    assert widget["body"]["tags"] == ["awb-project=%s" % PROJECT, "awb-expiry=%s" % widget["body"]["tags"][1][11:]]
    assert re.match(r"^\d{4}-\d{2}-\d{2}$", widget["body"]["tags"][1][11:])
    assert gadget["tags"] == [{"key": "awb-project", "value": PROJECT},
                              {"key": "awb-expiry", "value": widget["body"]["tags"][1][11:]}]
    assert widget["body"]["name"].startswith(PROJECT + "-lab-")
    # torn down newest first
    deletes = [p for m, p in writes(fake) if m == "DELETE"]
    assert deletes[0].startswith("/v1/{project_id}/gadgets/") and deletes[1].startswith("/v1/{project_id}/widgets/")
    rs = rows(project)
    assert [r["state"] for r in rs] == ["deleted", "deleted"]
    assert {r["id"] for r in rs} == set(fake.created())
    assert [r["handle"].split("-")[0] for r in rs] == ["wid", "gad"]
    assert projects.live_resources(project) == 0
    text = evidence(project)
    assert "Question: Does a widget of size small reach ACTIVE in eu-de" in text
    assert "- result: passed" in text and "every resource deleted and proven gone" in text
    assert "| size | the widget has the size asked for | widget.size = small | small | passed |" in text
    assert "| refused | the forbidden path is refused | HTTP 403 | 403 | passed |" in text
    assert "2 resource(s) created" in text
    for rid in fake.created():          # ids appear as handles only
        assert rid not in text
    assert "gadget (ran): the gadget shape" in text
    kb_line = lines[-1]
    assert kb_line.startswith("awb kb add --scope tcp --tag vpc --grade live --class api --source ")
    assert "evidence/lab-widget-check-" in kb_line and PROJECT not in kb_line
    assert kb_line in text
    assert any(line.startswith("evidence: evidence/lab-widget-check-") and line.endswith("-1.md") for line in lines)


def test_a_second_run_of_the_day_gets_the_next_number_and_the_cli_reaches_it(project, monkeypatch, capsys):
    fake = FakeKeyService()
    assert run(fake)[0] == 0
    monkeypatch.setattr(lab, "_send_default", fake)
    monkeypatch.setattr(lab.time, "sleep", lambda s: None)
    assert cli.main(["lab", "run", "widget-check", "--project", PROJECT, "--tenant", TENANT]) == 0
    names = sorted(p.name for p in (project / "evidence").glob("lab-*.md"))
    assert [n[-5:] for n in names] == ["-1.md", "-2.md"]
    assert "awb kb add" in capsys.readouterr().out


# --------------------------------------------------------------------------- the teardown always runs


def test_a_failed_check_still_tears_down_and_proves_it(project):
    fake = FakeKeyService()
    code, lines, _ = run(fake, params=["size=medium"])      # the fake answers small whatever was asked
    assert code == lab.EXIT_CHECK
    assert fake.live() == [] and len(fake.created()) == 2
    assert [r["state"] for r in rows(project)] == ["deleted", "deleted"]
    text = evidence(project)
    assert "- result: check failed (size)" in text
    assert "| size | the widget has the size asked for | widget.size = medium | small | failed |" in text
    assert "| refused | the forbidden path is refused | HTTP 403 | - | not run |" in text
    assert "A negative needs two recorded attempts" in text
    assert not any("awb kb add" in line for line in lines)


def test_a_create_that_fails_halfway_tears_down_only_what_was_made(project):
    fake = FakeKeyService()
    fake.fail_create = {"gadgets": 400}
    code, lines, _ = run(fake)
    assert code == lab.EXIT_STOPPED
    assert len(fake.created()) == 1 and fake.live() == []
    deletes = [p for m, p in writes(fake) if m == "DELETE"]
    assert len(deletes) == 1 and deletes[0].startswith("/v1/{project_id}/widgets/")
    assert [r["state"] for r in rows(project)] == ["deleted"]
    text = evidence(project)
    assert "- result: stopped (the create gadget answered 400)" in text
    assert "| active | the widget reaches ACTIVE | reached within the wait | reached | passed |" in text
    assert "| size | the widget has the size asked for | widget.size = small | - | not run |" in text


def test_the_time_budget_runs_out_and_the_teardown_runs(project):
    recipe = copy.deepcopy(RECIPE)
    recipe["budget"]["minutes"] = 1
    recipe["steps"][1]["within"] = 600
    write_recipe(Path(os.environ[lab.RECIPES_ENV]), recipe)
    fake = FakeKeyService()
    fake.never_active = True
    code, lines, _ = run(fake)
    assert code == lab.EXIT_STOPPED
    assert fake.live() == [] and len(fake.created()) == 1
    assert [r["state"] for r in rows(project)] == ["deleted"]
    text = evidence(project)
    assert "- result: stopped (the time budget of 1 minutes ran out in widget-active)" in text
    assert "every resource deleted and proven gone" in text


def test_a_wait_that_never_reaches_its_state_fails_its_check(project):
    fake = FakeKeyService()
    fake.never_active = True
    code, _, _ = run(fake)
    assert code == lab.EXIT_CHECK and fake.live() == []
    text = evidence(project)
    assert "| active | the widget reaches ACTIVE | reached within the wait | not reached | failed |" in text


def test_ctrl_c_during_the_run_tears_down_and_a_second_one_does_not_stop_the_teardown(project):
    fake = FakeKeyService(polls_to_active=3)
    sent = {"run": 0, "teardown": 0}

    def on_call(req):
        if req["method"] == "GET" and "/widgets/" in req["path"] and not sent["run"]:
            sent["run"] = 1
            os.kill(os.getpid(), signal.SIGINT)
        if req["method"] == "DELETE" and not sent["teardown"]:
            sent["teardown"] = 1
            os.kill(os.getpid(), signal.SIGINT)
    fake.on_call = on_call
    before = signal.getsignal(signal.SIGINT)
    code, lines, errors = run(fake)
    assert sent == {"run": 1, "teardown": 1}
    assert code == lab.EXIT_STOPPED
    assert fake.live() == [] and len(fake.created()) == 1
    assert [r["state"] for r in rows(project)] == ["deleted"]
    assert "- result: stopped (interrupted (SIGINT))" in evidence(project)
    assert any("the teardown runs to its end" in e for e in errors)
    assert signal.getsignal(signal.SIGINT) is before          # the handler of before is back


def test_sigterm_during_a_create_waits_until_its_row_is_written(project):
    fake = FakeKeyService()

    def on_call(req):
        if req["method"] == "POST" and req["path"] == "/v1/{project_id}/widgets":
            os.kill(os.getpid(), signal.SIGTERM)
    fake.on_call = on_call
    code, _, _ = run(fake)
    assert code == lab.EXIT_STOPPED
    assert len(fake.created()) == 1 and fake.live() == []
    assert [r["state"] for r in rows(project)] == ["deleted"]
    assert "interrupted (SIGTERM)" in evidence(project)


def test_a_delete_that_fails_leaves_the_row_live_with_a_line_for_the_owner(project):
    fake = FakeKeyService()
    fake.fail_delete = {"gadgets"}
    code, lines, errors = run(fake)
    assert code == lab.EXIT_LEFT
    assert len(fake.live()) == 1 and fake.items[fake.live()[0]]["coll"] == "gadgets"
    by_kind = {r["handle"].split("-")[0]: r for r in rows(project)}
    assert by_kind["wid"]["state"] == "deleted"
    left = by_kind["gad"]
    assert left["state"] == "live" and left["id"] == fake.live()[0]
    assert "LEFT LIVE: delete it with awb cloud call DELETE vpc /v1/{project_id}/gadgets/%s --tenant %s --role lab " \
           "--project %s" % (fake.live()[0], TENANT, PROJECT) in left["note"]
    assert "the delete answered 500" in left["note"]
    assert projects.live_resources(project) == 1          # awb close refuses while it is there
    assert any("LEFT LIVE" in e for e in errors)
    text = evidence(project)
    assert "1 resource(s) LEFT LIVE" in text and "No fact" in text
    assert not any("awb kb add" in line for line in lines)


def test_a_create_that_answers_without_an_id_is_recorded_for_the_owner(project):
    recipe = copy.deepcopy(RECIPE)
    recipe["steps"][0]["id_field"] = "widget.missing"
    write_recipe(Path(os.environ[lab.RECIPES_ENV]), recipe)
    fake = FakeKeyService()
    code, _, _ = run(fake)
    assert code == lab.EXIT_LEFT
    (row,) = rows(project)
    assert row["state"] == "live" and row["id"] == "unknown"
    assert "find it by its name %s-lab-" % PROJECT in row["note"]


def test_a_create_without_an_id_is_found_by_its_name(project):
    recipe = copy.deepcopy(RECIPE)
    recipe["steps"][0]["id_field"] = "widget.missing"
    recipe["steps"][0]["find"] = {"call": {"method": "GET", "service": "vpc",
                                           "path": "/v1/{project_id}/widgets/${probe_id}"},
                                  "field": "widget.id", "within": 30}
    recipe["vars"] = {"probe_id": "none"}
    write_recipe(Path(os.environ[lab.RECIPES_ENV]), recipe)
    fake = FakeKeyService()
    ids = []
    real = fake.__call__

    def send(req):
        if req["path"] == "/v1/{project_id}/widgets/none" and fake.created():
            req = dict(req, path="/v1/{project_id}/widgets/%s" % fake.created()[0])
            ids.append(1)
        return real(req)
    code, _, _ = run(send)
    assert ids and code == lab.EXIT_OK and fake.live() == []
    assert [r["state"] for r in rows(project)] == ["deleted", "deleted"]


# --------------------------------------------------------------------------- refusals before the first write


def test_no_grant_of_the_owner_is_refused_before_any_call(project, home):
    fake = FakeKeyService()
    grants.grant(home, PROJECT, "query")
    code, _, errors = run(fake)
    assert code == lab.EXIT_STOPPED and fake.calls == []
    assert errors == ["awb lab: %s has no grant of the owner: he runs awb projects kind %s project in his own "
                      "terminal" % (PROJECT, PROJECT)]
    assert rows(project) == [] and not list((project / "evidence").glob("lab-*"))


def test_a_query_is_refused_and_so_is_another_projects_folder(project, home, monkeypatch, tmp_path):
    fake = FakeKeyService()
    folder = str(project)
    projects._save(home, [projects.Project(PROJECT, "query", projects.NO_CUSTOMER, "tcp", folder,
                                           projects.memory_key(folder), "active", "2026-10-11")])
    code, _, errors = run(fake)
    assert code == lab.EXIT_STOPPED and fake.calls == [] and "is a query" in errors[0]
    projects._save(home, [projects.Project(PROJECT, "project", projects.NO_CUSTOMER, "tcp", folder,
                                           projects.memory_key(folder), "active", "2026-10-11")])
    other = tmp_path / "other"
    other.mkdir()
    (other / "SCOPE.md").write_text("- code: tcp-zz9z\n", encoding="utf-8")
    monkeypatch.chdir(other)
    code, _, errors = run(fake)
    assert code == lab.EXIT_STOPPED and fake.calls == [] and "another project's folder" in errors[0]


def test_the_key_service_refusing_the_grant_stops_before_any_write(project):
    fake = FakeKeyService()
    fake.refuse = "%s has no grant of the owner: he runs awb projects kind %s project in his own terminal" % (
        PROJECT, PROJECT)
    code, _, errors = run(fake)
    assert code == lab.EXIT_STOPPED
    assert len(fake.calls) == 1 and fake.calls[0]["role"] == "read" and writes(fake) == []
    assert "the grant check through the key service failed" in errors[0] and "no grant of the owner" in errors[0]
    assert rows(project) == []


@pytest.mark.parametrize("change, said", [
    (lambda r: r["guard"].update(resources=1), "cost guard: the recipe creates 2 resources and its guard allows 1"),
    (lambda r: r["guard"].update(resources=lab.MAX_RESOURCES + 1), "cost guard: a recipe may create at most"),
    (lambda r: r["guard"].update(lifetime_minutes=lab.MAX_LIFETIME + 1), "cost guard: a recipe may create at most"),
    (lambda r: r["budget"].update(minutes=30), "cost guard: the time budget and the teardown (35 minutes) exceed"),
])
def test_the_cost_guard_refuses_before_any_call(project, change, said):
    recipe = copy.deepcopy(RECIPE)
    change(recipe)
    write_recipe(Path(os.environ[lab.RECIPES_ENV]), recipe)
    fake = FakeKeyService()
    code, _, errors = run(fake)
    assert code == lab.EXIT_STOPPED and fake.calls == []
    assert errors[0].startswith("awb lab: " + said)


def test_a_parameter_outside_its_range_is_refused_by_the_cost_guard(project):
    fake = FakeKeyService()
    code, _, errors = run(fake, params=["size=huge"])
    assert code == lab.EXIT_STOPPED and fake.calls == []
    assert errors == ["awb lab: cost guard: the value of size is outside what the recipe allows"]
    code, _, errors = run(fake, params=["colour=red"])
    assert code == lab.EXIT_STOPPED and fake.calls == [] and "has no parameter colour" in errors[0]


@pytest.mark.parametrize("change, said", [
    (lambda r: r["teardown"].pop("gadget"), "teardown has one entry for every create step"),
    (lambda r: r["steps"][0].update(tags="none"), "every resource carries the project and expiry tags"),
    (lambda r: r["steps"][0]["call"]["body"]["widget"].pop("tags"), "tags in the body need"),
    (lambda r: r["steps"][1]["call"].update(path="/v1/{project_id}/widgets/${later}"), "refers to later"),
    (lambda r: r["checks"].append({"id": "x", "step": "nowhere", "says": "x", "status": [200]}), "names a step"),
    (lambda r: r["teardown"]["widget"]["gone"]["call"].update(method="DELETE"), "the gone proof is a read"),
])
def test_a_malformed_recipe_is_refused(change, said):
    recipe = copy.deepcopy(RECIPE)
    change(recipe)
    with pytest.raises(lab.LabError, match=re.escape(said)):
        lab.validate(recipe)


# --------------------------------------------------------------------------- evidence and secrets


def test_the_evidence_holds_no_key_token_or_secret_planted_in_an_answer(project):
    planted = {"admin_pass": "Planted4711pw", "credential": {"access": "AKPLANTEDKEY1", "secret": "s3cr3tPl4nt3d"},
               "note": "s" + "k=" + SECRET_LONG, "security_" + "token": TOKEN}
    recipe = copy.deepcopy(RECIPE)
    recipe["steps"][3]["save"] = {"note": "widget.note"}
    recipe["checks"].append({"id": "note", "step": "read", "says": "the note is there", "field": "widget.note",
                             "present": True})
    recipe["checks"].append({"id": "cred", "step": "read", "says": "the credential is there",
                             "field": "widget.credential", "present": True})
    write_recipe(Path(os.environ[lab.RECIPES_ENV]), recipe)
    fake = FakeKeyService()
    fake.plant = planted
    code, lines, _ = run(fake)
    assert code == lab.EXIT_OK
    text = evidence(project)
    for value in ("Planted4711pw", "AKPLANTEDKEY1", "s3cr3tPl4nt3d", SECRET_LONG,
                  TOKEN):
        assert value not in text
        assert all(value not in line for line in lines)
        assert value not in (project / "RESOURCES.md").read_text(encoding="utf-8")
    assert "widget.credential = <withheld>" in text and "widget.note = <withheld>" in text
    from awb import gate

    for name in ("secret", "token", "private-key"):
        assert gate.DETECTORS[name](text) == []


def test_the_last_guard_withholds_a_line_the_gate_still_finds():
    text = lab.Run._scrub("| a | b |\n" + "pass" + "word: " + SECRET_LONG + "\n")
    assert SECRET_LONG not in text and "a line withheld" in text and "| a | b |" in text


# --------------------------------------------------------------------------- list and dry run


def test_the_dry_run_makes_no_call(project, monkeypatch, capsys):
    def no_call(*a, **k):
        raise AssertionError("a dry run made a call")
    from awb.tcp import keys

    monkeypatch.setattr(keys, "request", no_call)
    monkeypatch.setattr(lab, "_send_default", no_call)
    monkeypatch.delenv(lab.RECIPES_ENV)
    for name in ("ecs-boot-ssh", "obs-policy-deny", "elb-health"):
        assert cli.main(["lab", "dry-run", name, "--project", PROJECT, "--tenant", TENANT]) == 0
        out = capsys.readouterr().out
        assert out.rstrip().endswith("dry run: no call was made")
        assert "teardown, newest first" in out and "to confirm live" in out
    assert rows(project) == [] and not list((project / "evidence").glob("*"))


def test_the_list_shows_each_recipe_with_its_question_and_guard(monkeypatch, capsys):
    monkeypatch.delenv(lab.RECIPES_ENV, raising=False)
    assert cli.main(["lab", "list"]) == 0
    out = capsys.readouterr().out
    for name in ("ecs-boot-ssh", "obs-policy-deny", "elb-health"):
        assert name in out
    assert "Does an ECS of flavor s3.medium.1 boot the image" in out
    assert "guard: at most 5 resource(s) for 45 minutes" in out and "refused" not in out


# --------------------------------------------------------------------------- the three recipes


def test_the_three_recipes_are_valid_tag_every_resource_and_use_known_tags():
    tags = {line.strip() for line in (REPO_RECIPES.parent / "rules" / "tags.txt").read_text().splitlines()
            if line.strip() and not line.startswith("#")}
    names = sorted(p.stem for p in REPO_RECIPES.glob("*.json"))
    # the three first recipes and the three of the fact-check (F7); a new recipe is added here by name (P28)
    assert names == ["dns-private-zone", "ecs-boot-ssh", "ecs-flavor-boot", "eip-bandwidth-adjust", "elb-health",
                     "evs-snapshot-restore", "nat-snat-egress", "obs-lifecycle-rule", "obs-policy-deny",
                     "rds-create-refusal", "smn-http-confirm"]
    for name in names:
        r = lab.load(REPO_RECIPES / ("%s.json" % name))
        assert r["name"] == name and set(r["kb"]["tags"]) <= tags
        assert lab.confirm_notes(r), "a recipe of shapes not proven live says so"
        for s in r["steps"]:
            assert s["call"]["service"] != "iam"
        creates = [s for s in r["steps"] if s["do"] == "create"]
        assert len(creates) <= r["guard"]["resources"]


def test_the_example_of_the_format_is_a_valid_recipe():
    text = (REPO_RECIPES / "README.md").read_text(encoding="utf-8")
    block = text.split("## An example", 1)[1].split("```json", 1)[1].split("```", 1)[0]
    r = json.loads(block)
    lab.validate(r)
    assert lab.dry_run_lines(r, lab.params_of(r, []))[-1] == "dry run: no call was made"


def ecs_cloud(secrets=("ecs-admin",)):
    """A stand-in key service that answers the shapes of ecs-boot-ssh as the recipe expects them; the tenant holds
    the secrets named (their names only, as the key service lists them)."""
    state = {"n": 0, "live": {}, "made": [], "server_bodies": []}

    def new(kind):
        state["n"] += 1
        rid = "%08x-1111-4222-8333-%012x" % (state["n"], 0xfeed00 + state["n"])
        state["live"][rid] = kind
        state["made"].append(rid)
        return rid

    def ok(status, data=None):
        return {"ok": True, "status": status, "data": data, "text": ""}

    def send(req):
        if req.get("op") == "tenants":
            return {"ok": True, "tenants": {TENANT: {"roles": ["lab", "read"], "secrets": sorted(secrets)}}}
        m, p, b = req["method"], req["path"], req.get("body") or {}
        if p == "/v1/{project_id}/vpcs" and m == "GET":
            return ok(200, {"vpcs": []})
        if p == "/v2/{project_id}/types" and m == "GET":
            return ok(200, {"volume_types": [{"name": "SAS", "extra_specs": {
                "RESKEY:availability_zones": "eu-de-01,eu-de-02,eu-de-03"}}]})
        if p == "/v2/cloudimages":
            return ok(200, {"images": [{"id": "-".join(IMAGE_ID), "name": req["query"]["name"]}]})
        if m == "POST" and p == "/v1/{project_id}/vpcs":
            return ok(200, {"vpc": {"id": new("vpc")}})
        if m == "POST" and p.endswith("/tags/action"):
            return ok(204)
        if m == "POST" and p == "/v1/{project_id}/subnets":
            # the first-boot script of the probe needs DNS servers on the subnet (the live run of 2026-10-12)
            assert b["subnet"]["dnsList"] == ["100.125.4.25", "100.125.129.199"]
            assert b["subnet"]["dhcp_enable"] is True
            return ok(200, {"subnet": {"id": new("subnet")}})
        if m == "POST" and p == "/v1/{project_id}/security-groups":
            return ok(200, {"security_group": {"id": new("sg")}})
        if m == "POST" and p == "/v1/{project_id}/security-group-rules":
            return ok(201, {"security_group_rule": {"id": "rule"}})
        if m == "POST" and p == "/v1/{project_id}/cloudservers":
            # a public image needs a login (run 2 of 2026-10-12): the placeholder the key service fills, never a value
            assert b["server"]["adminPass"] == "{{secret:ecs-admin}}" and "key_name" not in b["server"]
            state["server_bodies"].append(b["server"])
            assert {"key": "awb-project", "value": PROJECT} in b["server"]["server_tags"]
            return ok(200, {"job_id": "job", "serverIds": [new("server")]})
        if m == "POST" and p == "/v1/{project_id}/cloudservers/delete":
            for s in b["servers"]:
                state["live"].pop(s["id"], None)
            return ok(200, {"job_id": "job"})
        if m == "POST" and p.endswith("/action"):
            assert p.startswith("/v2/{project_id}/servers/") and b == {"os-getConsoleOutput": {}}
            return ok(200, {"output": "cloud-init done\nAWB-SSH-OPEN\n"})
        if m == "DELETE":
            rid = p.rsplit("/", 1)[1]
            state["live"].pop(rid, None)
            return ok(204)
        if m == "GET":
            rid = p.rsplit("/", 1)[1]
            kind = state["live"].get(rid)
            if kind is None:
                return ok(404, {"error_msg": "not found"})
            if kind == "subnet":
                return ok(200, {"subnet": {"id": rid, "status": "ACTIVE"}})
            if kind == "server":
                vpc = next(k for k, v in state["live"].items() if v == "vpc")
                return ok(200, {"server": {"id": rid, "status": "ACTIVE",
                                           "addresses": {vpc: [{"addr": "192.168.10.23"}]}}})
            return ok(200, {kind: {"id": rid}})
        return ok(404, {"error_msg": "no such path"})
    send.state = state
    return send


def test_the_ecs_recipe_runs_end_to_end_with_the_secret_placeholder_and_dns(project, home, monkeypatch):
    monkeypatch.delenv(lab.RECIPES_ENV)
    send = ecs_cloud()
    code, lines, errors = run(send, recipe="ecs-boot-ssh", params=["flavor=s3.large.2"])
    assert code == lab.EXIT_OK, (lines, errors)
    assert send.state["live"] == {} and len(send.state["made"]) == 5
    # both servers carry the placeholder of the secret; the user_data of the probe is still there
    bodies = send.state["server_bodies"]
    assert [x["adminPass"] for x in bodies] == ["{{secret:ecs-admin}}"] * 2
    assert "user_data" not in bodies[0] and bodies[1]["user_data"]
    assert [r["state"] for r in rows(project)] == ["deleted"] * 5
    text = evidence(project)
    assert "| ssh-answers | port 22 of the server under test answers inside the VPC | output contains AWB-SSH-OPEN" \
           in text
    assert "output = contains AWB-SSH-: yes" in text
    assert "--tag ecs --tag ims --tag vpc --grade live --class availability" in lines[-1]
    assert "An ECS of flavor s3.large.2 boots the public image Standard_Ubuntu_22.04_latest in eu-de" in lines[-1]
    assert "## To confirm live" in text
    # the line is ready: the knowledge base takes it as it is
    import shlex

    argv = shlex.split(lines[-1])
    assert argv[:3] == ["awb", "kb", "add"]
    assert cli.main(argv[1:]) == 0
    entries = list((home.kb / "entries").glob("KB-*.md"))
    assert len(entries) == 1
    entry = entries[0].read_text(encoding="utf-8")
    assert "grade: live" in entry and "evidence/lab-ecs-boot-ssh-" in entry


# --------------------------------------------------------------------------- through the real key service


def test_a_run_goes_through_the_real_key_service_with_the_lab_key(project, tmp_path, monkeypatch):
    """The requests a run sends are ones the key service of awb/tcp/keys.py takes: it signs them with the lab key
    for an active project and the stand-in gateway checks every signature."""
    from tests.tcp_fake import FakeGateway
    from tests.test_keys import LAB_AK, LAB_SK, STORE, running_service
    from awb.tcp import keys, throttle

    monkeypatch.setattr(throttle, "log_call", lambda *a, **k: None)
    made: dict[str, bool] = {}

    class LabGateway(FakeGateway):
        def route(self, method, raw_path):
            path = raw_path.partition("?")[0]
            base = "/v1/%s/widgets" % self.PROJECT
            if method == "POST" and path == base:
                rid = "%08x-2222-4333-8444-%012x" % (len(made) + 7, 0xbead)
                made[rid] = True
                return 200, {"widget": {"id": rid}}
            if path.startswith(base + "/"):
                rid = path[len(base) + 1:]
                if method == "DELETE" and made.get(rid):
                    made[rid] = False
                    return 204, ""
                if method == "GET":
                    return (200, {"widget": {"id": rid, "status": "ACTIVE", "size": "small"}}) if made.get(rid) \
                        else (404, {"error_msg": "gone"})
            if method == "GET" and path == "/v1/%s/forbidden" % self.PROJECT:
                return 403, {"error_msg": "denied"}
            return super().route(method, raw_path)

    recipe = copy.deepcopy(RECIPE)
    recipe["steps"] = [s for s in recipe["steps"] if s["id"] != "gadget"]
    recipe["teardown"].pop("gadget")
    write_recipe(Path(os.environ[lab.RECIPES_ENV]), recipe)
    with LabGateway(LAB_AK, LAB_SK) as gw, running_service(gw, tmp_path / "log") as svc:
        svc._active_project = lambda code: code == PROJECT
        keys.unlock(svc.admin_path, sorted(STORE), reader=lambda e: STORE[e], settings=_recipe_lab_writes())
        monkeypatch.setenv(keys.CALL_SOCKET_ENV, str(svc.call_path))
        lines: list[str] = []
        errors: list[str] = []
        clock = Clock()
        # the grant probe reads with the read key, which this gateway does not take: the service still answered
        code = lab.run("widget-check", PROJECT, TENANT, [], clock=clock, sleep=clock.sleep, out=lines.append,
                       err=errors.append)
        assert code == lab.EXIT_OK, (lines, errors)
        assert list(made.values()) == [False]
        methods = [m for m, _ in gw.calls]
        assert "POST" in methods and "DELETE" in methods
    text = evidence(project)
    assert LAB_SK not in text and LAB_AK not in text
    assert "| 1 | grant | grant | GET | vpc | `/v1/{project_id}/vpcs` | 401 |" in text


# --------------------------------------------------------------------------- a create whose answer is lost


def finding_recipe() -> dict:
    """RECIPE with a find by name for both creates: the list of the collection, the item named like the resource."""
    recipe = copy.deepcopy(RECIPE)
    for step, coll in ((recipe["steps"][0], "widgets"), (recipe["steps"][2], "gadgets")):
        step["find"] = {"call": {"method": "GET", "service": "vpc", "path": "/v1/{project_id}/%s" % coll},
                        "list": coll, "field": "id", "within": 30}
    write_recipe(Path(os.environ[lab.RECIPES_ENV]), recipe)
    return recipe


def test_a_create_whose_answer_is_lost_is_found_by_name_recorded_torn_down_and_proven_gone(project):
    finding_recipe()
    fake = FakeKeyService()
    fake.made_but = {"widgets": "lost"}
    seen = []

    def on_call(req):
        # the row is there before the create call leaves, in the state creating, with the name
        if req["method"] == "POST" and req["path"] == "/v1/{project_id}/widgets":
            seen.extend(rows(project))
    fake.on_call = on_call
    code, lines, errors = run(fake)
    assert len(seen) == 1 and seen[0]["state"] == "creating" and seen[0]["id"] == "unknown"
    assert "creating, named %s-lab-" % PROJECT in seen[0]["note"]
    assert code == lab.EXIT_OK, (lines, errors)
    assert len(fake.created()) == 2 and fake.live() == []
    rs = rows(project)
    assert [r["state"] for r in rs] == ["deleted", "deleted"]
    assert {r["id"] for r in rs} == set(fake.created())
    assert rs[0]["handle"] == seen[0]["handle"]               # the same row, now with the id
    assert ("GET", "/v1/{project_id}/widgets") in [(r["method"], r["path"]) for r in fake.calls]
    text = evidence(project)
    assert "| 2 | run | widget | POST | vpc | `/v1/{project_id}/widgets` | no answer from the key service " \
           "(ConnectionResetError) |" in text
    assert "| widget find | GET | vpc | `/v1/{project_id}/widgets` | 200 | 1 | named like the resource = yes |" in text
    assert "every resource deleted and proven gone" in text
    for rid in fake.created():
        assert rid not in text


def test_a_create_whose_answer_is_lost_and_that_made_nothing_is_not_created_with_proof(project):
    finding_recipe()
    fake = FakeKeyService()
    fake.fail_create = {"gadgets": "lost"}
    code, lines, errors = run(fake)
    assert code == lab.EXIT_STOPPED, (lines, errors)
    assert len(fake.created()) == 1 and fake.live() == []
    by_kind = {r["handle"].split("-")[0]: r for r in rows(project)}
    assert by_kind["wid"]["state"] == "deleted"
    gad = by_kind["gad"]
    assert gad["state"] == "not created" and gad["id"] == "unknown"
    assert "the create got no answer from the key service (ConnectionResetError)" in gad["note"]
    assert "answered 200 without it after 30 s" in gad["note"]
    assert "at the teardown the find by name" in gad["note"]          # looked up again before the run ends
    assert projects.live_resources(project) == 0                      # awb close is not held by it
    assert [p for m, p in writes(fake) if m == "DELETE"] == ["/v1/{project_id}/widgets/%s" % fake.created()[0]]
    text = evidence(project)
    assert "the find by name proves nothing was made" in text
    assert "1 resource(s) created" in text and "1 create(s) proven not made" in text
    assert "| %s | gadget | not created |" % gad["handle"] in text


def test_a_5xx_then_a_successful_find_records_the_resource_and_tears_it_down(project):
    finding_recipe()
    fake = FakeKeyService()
    fake.made_but = {"gadgets": 503}
    code, lines, errors = run(fake)
    assert code == lab.EXIT_OK, (lines, errors)
    assert len(fake.created()) == 2 and fake.live() == []
    assert [r["state"] for r in rows(project)] == ["deleted", "deleted"]
    gadget = [i for i, it in fake.items.items() if it["coll"] == "gadgets"][0]
    assert fake.items[gadget]["tags"][0] == {"key": "awb-project", "value": PROJECT}   # the tag call followed
    assert "| 4 | run | gadget | POST | vpc | `/v1/{project_id}/gadgets` | 503 |" in evidence(project)


def test_a_lost_answer_the_find_cannot_tell_is_placed_by_the_find_of_the_teardown(project):
    finding_recipe()
    fake = FakeKeyService()
    fake.made_but = {"widgets": 502}
    fake.list_fail = 4                  # every read of the find within the run answers 500
    code, lines, errors = run(fake)
    assert code == lab.EXIT_STOPPED, (lines, errors)
    assert len(fake.created()) == 1 and fake.live() == []
    (row,) = rows(project)
    assert row["state"] == "deleted" and row["id"] == fake.created()[0]
    assert "the create widget got 502 and the find could not tell" in evidence(project)


def test_a_lost_answer_without_a_find_keeps_the_row_live_for_the_owner(project):
    fake = FakeKeyService()
    fake.made_but = {"widgets": "lost"}
    code, _, errors = run(fake)
    assert code == lab.EXIT_LEFT
    assert len(fake.live()) == 1                       # made; the run cannot know its id
    (row,) = rows(project)
    assert row["state"] == "live" and row["id"] == "unknown"
    assert "may have made it: find it by its name %s-lab-" % PROJECT in row["note"]
    assert projects.live_resources(project) == 1


def test_a_refused_create_takes_its_row_back(project):
    finding_recipe()
    fake = FakeKeyService()
    fake.fail_create = {"widgets": 409}
    code, _, _ = run(fake)
    assert code == lab.EXIT_STOPPED and fake.created() == []
    assert rows(project) == []
    assert not [r for r in fake.calls if r["path"] == "/v1/{project_id}/widgets" and r["method"] == "GET"]


@pytest.mark.parametrize("change, said", [
    (lambda r: r["steps"][0].update(name="widget-1"), "the name of the resource carries ${name} or ${project}"),
    (lambda r: r["steps"][0].update(name="${name}-z"), "the call gives the resource its name ${name}-z"),
    (lambda r: r["steps"][0].update(find={"call": {"method": "POST", "service": "vpc", "path": "/v1/x"},
                                          "field": "id"}), "a find reads"),
    (lambda r: r["steps"][0].update(find={"call": {"method": "GET", "service": "vpc", "path": "/v1/x"},
                                          "status": [200]}), "find has a call and a field"),
    (lambda r: r["steps"][0].update(find={"call": {"method": "GET", "service": "vpc", "path": "/v1/${gadget}"},
                                          "field": "id"}), "the find refers to a value it does not have"),
])
def test_a_malformed_name_or_find_is_refused(change, said):
    recipe = copy.deepcopy(RECIPE)
    change(recipe)
    with pytest.raises(lab.LabError, match=re.escape(said)):
        lab.validate(recipe)


def test_every_create_of_the_three_recipes_has_a_name_with_the_code_and_a_find_by_name():
    for path in sorted(REPO_RECIPES.glob("*.json")):
        r = lab.load(path)
        for s in (s for s in r["steps"] if s["do"] == "create"):
            assert "${name}" in s["name"], (path.name, s["id"])
            assert s["find"]["call"]["method"] in ("GET", "HEAD"), (path.name, s["id"])
            assert "list" in s["find"] or "status" in s["find"], (path.name, s["id"])
        lines = lab.dry_run_lines(r, lab.params_of(r, []))
        assert not any("no find by name" in line for line in lines)
        assert sum("before the call" in line for line in lines) == len([s for s in r["steps"] if s["do"] == "create"])


def test_the_ecs_recipe_places_a_vpc_whose_create_answer_was_lost_after_its_secret_check(project, monkeypatch):
    """The find of the VPC as the recipe writes it (the list under vpcs, the item named like the resource) against
    a cloud that made the VPC and lost the answer. The run asks for the names of the tenant's secrets first, a
    request without a method."""
    monkeypatch.delenv(lab.RECIPES_ENV)
    cloud = ecs_cloud()
    names: dict[str, str] = {}

    def send(req):
        if req.get("op") != "call":
            return cloud(req)
        if req["method"] == "POST" and req["path"] == "/v1/{project_id}/vpcs":
            answer = cloud(req)
            names[answer["data"]["vpc"]["id"]] = req["body"]["vpc"]["name"]
            raise TimeoutError("no answer")
        if req["method"] == "GET" and req["path"] == "/v1/{project_id}/vpcs" and names:
            vpcs = [{"id": i, "name": n} for i, n in names.items() if i in cloud.state["live"]]
            return {"ok": True, "status": 200, "data": {"vpcs": [{"id": "other", "name": "other"}] + vpcs},
                    "text": ""}
        return cloud(req)
    code, lines, errors = run(send, recipe="ecs-boot-ssh")
    assert code == lab.EXIT_OK, (lines, errors)
    assert cloud.state["live"] == {} and len(cloud.state["made"]) == 5
    assert [r["state"] for r in rows(project)] == ["deleted"] * 5


def test_a_full_page_without_the_name_proves_nothing(project):
    recipe = finding_recipe()
    recipe["steps"][0]["find"]["call"]["query"] = {"limit": "2"}
    write_recipe(Path(os.environ[lab.RECIPES_ENV]), recipe)
    fake = FakeKeyService()
    fake.items = {"%08x-aaaa-4bbb-8ccc-0d1e2f3a4b5c" % n: {"coll": "widgets", "deleted": False, "reads": 0,
                                                            "body": {"name": "other-%d" % n}} for n in (1, 2)}
    fake.fail_create = {"widgets": 504}
    code, _, _ = run(fake)
    assert code == lab.EXIT_LEFT                     # it cannot know: the row stays live for the owner
    (row,) = rows(project)
    assert row["state"] == "live" and row["id"] == "unknown"
    assert "the find could not tell" in row["note"]


# --------------------------------------------------------------------------- the first live run (2026-10-12)


def test_an_error_of_the_host_in_the_preflight_is_one_line_and_exit_2(project, monkeypatch):
    """The first live run died in the preflight with a traceback and exit 1, which reads as a failed check."""
    fake = FakeKeyService()

    def refused(code):
        raise PermissionError(13, "Permission denied", "/somewhere/grants.json")
    monkeypatch.setattr(lab, "project_folder", refused)
    code, lines, errors = run(fake)
    assert code == lab.EXIT_STOPPED and lines == []
    assert errors == ["awb lab: the preflight failed before any call (PermissionError); nothing was created"]
    assert fake.calls == [] and rows(project) == []


# --------------------------------------------------------------------------- run 2 (2026-10-12): the 400 and the secret


@pytest.mark.parametrize("body", [
    {"error_code": "Ecs.0005", "error_msg": "Invalid parameter: adminPass or key_name is required."},
    {"code": "Ecs.0005", "message": "Invalid parameter: adminPass or key_name is required."},
    {"error": {"code": "Ecs.0005", "message": "Invalid parameter: adminPass or key_name is required."}},
], ids=["error_code", "code", "error.code"])
def test_a_refused_create_keeps_its_error_code_and_message_in_the_evidence_and_the_stop_line(project, body):
    """Run 2 stopped with "the create server answered 400" and nothing else: the cause could not be told."""
    fake = FakeKeyService()
    fake.fail_create = {"gadgets": 400}
    fake.refused_body = body
    code, lines, errors = run(fake)
    assert code == lab.EXIT_STOPPED and fake.live() == []
    stop = "result: stopped (the create gadget answered 400, error_code Ecs.0005)"
    assert stop in lines
    text = evidence(project)
    assert "- " + stop in text
    assert "| 400 | 1 | error_code = Ecs.0005; error_msg = Invalid parameter: adminPass or key_name is required. |" \
        in text


@pytest.mark.parametrize("message, kept", [
    ("x" * 500, "x" * lab.ERROR_CHARS),
    ("the secret <secret> was refused", "<withheld>"),                       # what the key service masked
    ("quota of %s exceeded" % fixtures.CUSTOMER_FORMS[0], "<withheld by the name check>"),
    ("s" + "k=" + SECRET_LONG, "<withheld>"),
], ids=["long", "masked-secret", "name", "secret"])
def test_an_error_message_is_cut_to_200_characters_and_never_keeps_a_secret_or_a_name(project, message, kept):
    fake = FakeKeyService()
    fake.fail_create = {"gadgets": 400}
    fake.refused_body = {"error_code": "VPC.0101", "error_msg": message}
    code, lines, _ = run(fake)
    assert code == lab.EXIT_STOPPED
    text = evidence(project)
    assert "error_code = VPC.0101; error_msg = %s |" % kept in text
    for planted in (fixtures.CUSTOMER_FORMS[1], SECRET_LONG, "x" * (lab.ERROR_CHARS + 1)):
        assert planted not in text and all(planted not in line for line in lines)


def test_an_error_message_the_name_check_cannot_check_is_not_kept(project, monkeypatch):
    from awb import check

    def unavailable(text, reg):
        raise check.CheckUnavailable("no vault")
    monkeypatch.setattr(check, "check_text", unavailable)
    fake = FakeKeyService()
    fake.fail_create = {"gadgets": 409}
    fake.refused_body = {"error_code": "VPC.0102", "error_msg": "busy"}
    assert run(fake)[0] == lab.EXIT_STOPPED
    text = evidence(project)
    assert "error_code = <withheld>; error_msg = <withheld by the name check> |" in text
    assert "- result: stopped (the create gadget answered 409, error_code <withheld>)" in text


# --------------------------------------------------------------------------- run 3 (2026-10-12): OBS (B-003)


def test_an_obs_error_in_xml_keeps_its_code_and_message_in_the_evidence_and_the_stop_line(project):
    """The live run of obs-policy-deny stopped at the bucket create with 400 and no code: OBS answers in XML."""
    from tests.obs_fake import BAD_AUTHORIZATION, error_xml

    fake = FakeKeyService()
    fake.fail_create = {"gadgets": 400}
    fake.refused_body = None
    fake.refused_text = error_xml("InvalidArgument", BAD_AUTHORIZATION).decode()
    code, lines, _ = run(fake)
    assert code == lab.EXIT_STOPPED and fake.live() == []
    stop = "result: stopped (the create gadget answered 400, error_code InvalidArgument)"
    assert stop in lines
    text = evidence(project)
    assert "- " + stop in text
    assert "| 400 | 1 | error_code = InvalidArgument; error_msg = %s |" % BAD_AUTHORIZATION in text


def test_an_obs_error_cut_short_in_the_text_still_gives_its_code():
    from tests.obs_fake import error_xml

    text = error_xml("AccessDenied", "x" * 400).decode()[:300]          # the key service keeps 300 characters
    kept = text.split("<Message>", 1)[1]
    assert set(kept) == {"x"}
    assert lab.error_of({"ok": True, "status": 403, "data": None, "text": text}) == ("AccessDenied", kept)
    assert lab.error_of({"ok": True, "status": 200, "data": None, "text": "<ListAllMyBucketsResult/>"}) == ("", "")


def test_the_obs_recipe_runs_through_the_key_service_signed_the_obs_way(project, tmp_path):
    """obs-policy-deny end to end: the real key service signs every OBS call for a stand-in OBS service that takes
    the V2 signature only; the grant probe goes to the stand-in key service."""
    from types import SimpleNamespace

    from tests.obs_fake import ACCESS_DENIED, FakeOBSService
    from tests.test_keys import AK, LAB_AK, LAB_SK, SK, load, running_service

    write_recipe(tmp_path / "recipes", json.loads((REPO_RECIPES / "obs-policy-deny.json").read_text("utf-8")))
    others = FakeKeyService()
    with FakeOBSService({AK: SK, LAB_AK: LAB_SK}) as fake_obs:
        with running_service(SimpleNamespace(base=fake_obs.endpoint), tmp_path / "log") as svc:
            load(svc)

            def send(req):
                return svc._call_socket(req, os.getuid()) if req.get("service") == "obs" else others(req)
            code, lines, errors = run(send, recipe="obs-policy-deny")
    assert code == lab.EXIT_OK, (lines, errors)
    assert fake_obs.buckets == {}
    assert fake_obs.calls and {scheme for _, _, scheme in fake_obs.calls} == {"OBS"}
    made = [path for method, path, _ in fake_obs.calls if method == "PUT"]
    assert made[0].startswith("/%s-lab-" % PROJECT)
    text = evidence(project)
    assert "| 403 | 1 | error_code = AccessDenied; error_msg = %s |" % ACCESS_DENIED in text
    assert [r["state"] for r in rows(project)] == ["deleted"]


def test_the_404_that_proves_a_resource_gone_keeps_no_error(project):
    fake = FakeKeyService()
    assert run(fake)[0] == lab.EXIT_OK
    gone = [line for line in evidence(project).splitlines() if " gone | GET |" in line]
    assert len(gone) == 2 and all("| 404 |" in line and "error_" not in line for line in gone)


def secret_recipe(name="ecs-admin") -> dict:
    """RECIPE whose gadget fills a password field from the tenant secret named by the parameter admin_secret."""
    recipe = copy.deepcopy(RECIPE)
    recipe["params"]["admin_secret"] = {"default": name, "pattern": "^[a-z0-9][a-z0-9_.-]{0,40}$"}
    recipe["steps"][2]["call"]["body"]["gadget"]["admin_pass"] = "{{secret:${admin_secret}}}"
    write_recipe(Path(os.environ[lab.RECIPES_ENV]), recipe)
    return recipe


def test_a_missing_secret_stops_the_run_before_its_first_create(project):
    secret_recipe()
    fake = FakeKeyService()
    fake.secrets = {TENANT: ["other"], "test-2": ["ecs-admin"]}
    code, lines, errors = run(fake)
    assert code == lab.EXIT_STOPPED and lines == []
    assert errors == ["awb lab: the secret ecs-admin is missing for %s" % TENANT]
    assert [(r.get("op"), r.get("method")) for r in fake.calls] == [("call", "GET"), ("tenants", None)]
    assert fake.created() == []                        # the grant read, then the names; no write
    assert rows(project) == [] and not list((project / "evidence").glob("lab-*"))


def test_a_present_secret_lets_the_run_go_and_the_body_carries_only_its_placeholder(project):
    secret_recipe()
    fake = FakeKeyService()
    fake.secrets = {TENANT: ["ecs-admin"]}
    code, lines, errors = run(fake)
    assert code == lab.EXIT_OK, (lines, errors)
    gadget = next(it for it in fake.items.values() if it["coll"] == "gadgets")
    assert gadget["body"]["admin_pass"] == "{{secret:ecs-admin}}"
    assert sum(1 for r in fake.calls if r.get("op") == "tenants") == 1
    assert [r.get("op") for r in fake.calls].index("tenants") < next(
        i for i, r in enumerate(fake.calls) if r.get("method") == "POST")


def test_a_recipe_without_a_secret_does_not_ask_for_the_names(project):
    fake = FakeKeyService()
    assert run(fake)[0] == lab.EXIT_OK
    assert not any(r.get("op") == "tenants" for r in fake.calls)


def test_the_ecs_recipe_on_a_tenant_without_its_secret_makes_nothing(project, monkeypatch):
    monkeypatch.delenv(lab.RECIPES_ENV)
    send = ecs_cloud(secrets=())
    code, lines, errors = run(send, recipe="ecs-boot-ssh")
    assert code == lab.EXIT_STOPPED and lines == []
    assert errors == ["awb lab: the secret ecs-admin is missing for %s" % TENANT]
    assert send.state["made"] == [] and rows(project) == []


def test_the_secrets_of_the_recipes_and_the_dry_run_line():
    for name, want in (("ecs-boot-ssh", ["ecs-admin"]), ("elb-health", ["ecs-admin"]), ("obs-policy-deny", [])):
        r = lab.load(REPO_RECIPES / ("%s.json" % name))
        params = lab.params_of(r, [])
        assert lab.secrets_of(r, params) == want, name
        shown = [x for x in lab.dry_run_lines(r, params) if x.startswith("0. secrets:")]
        assert shown == (["0. secrets: the tenant holds ecs-admin in the key service (asked by name before the "
                          "first create)"] if want else [])
    r = lab.load(REPO_RECIPES / "ecs-boot-ssh.json")
    assert lab.secrets_of(r, lab.params_of(r, ["admin_secret=lab-pw"])) == ["lab-pw"]


def test_the_secret_check_goes_through_the_real_key_service(project, tmp_path, monkeypatch):
    """The names come from the operation tenants of awb/tcp/keys.py; the value never reaches the run."""
    from tests.tcp_fake import FakeGateway
    from tests.test_keys import ADMIN_PW, LAB_AK, LAB_SK, STORE, running_service
    from awb.tcp import keys, throttle

    monkeypatch.setattr(throttle, "log_call", lambda *a, **k: None)
    recipe = secret_recipe()
    with FakeGateway(LAB_AK, LAB_SK) as gw, running_service(gw, tmp_path / "log") as svc:
        svc._active_project = lambda code: code == PROJECT
        keys.unlock(svc.admin_path, sorted(STORE), reader=lambda e: STORE[e])
        monkeypatch.setenv(keys.CALL_SOCKET_ENV, str(svc.call_path))
        clock = Clock()
        job = lab.Run(recipe, lab.params_of(recipe, []), PROJECT, TENANT, project, None, clock=clock,
                      sleep=clock.sleep)
        assert job.missing_secret() is None
        job = lab.Run(recipe, lab.params_of(recipe, ["admin_secret=not-there"]), PROJECT, TENANT, project, None,
                      clock=clock, sleep=clock.sleep)
        assert job.missing_secret() == "the secret not-there is missing for %s" % TENANT
        assert ADMIN_PW not in repr(vars(job))


# --------------------------------------------------------------------------- the disk types before the first create


def disk_recipe() -> dict:
    """The widget recipe with a system disk of the type the parameter disk names, in the AZ of the parameter az."""
    recipe = copy.deepcopy(RECIPE)
    recipe["name"] = "disk-check"
    recipe["params"]["disk"] = {"default": "SAS", "pattern": "^[A-Za-z0-9-]{2,10}$"}
    recipe["params"]["az"] = {"default": "eu-de-01", "pattern": "^eu-de-0[1-3]$"}
    recipe["steps"][0]["call"]["body"]["widget"].update(
        {"availability_zone": "${az}", "root_volume": {"volumetype": "${disk}"}})
    write_recipe(Path(os.environ[lab.RECIPES_ENV]), recipe)
    return recipe


def test_a_sold_out_disk_type_stops_the_run_before_any_create(project):
    disk_recipe()
    fake = FakeKeyService()
    code, lines, errors = run(fake, recipe="disk-check", params=["disk=SATA"])
    assert code == lab.EXIT_STOPPED and lines == []
    assert errors == ["awb lab: the disk type SATA is sold out in eu-de-01, so no create was tried; set another "
                      "type in the recipe"]
    assert writes(fake) == [] and fake.created() == [] and rows(project) == []
    assert [(r["role"], r["service"], r["path"]) for r in fake.calls if r.get("op") == "call"][-1] == \
        ("read", "evs", "/v2/{project_id}/types")


def test_an_offered_disk_type_lets_the_run_go(project):
    disk_recipe()
    fake = FakeKeyService()
    code, lines, errors = run(fake, recipe="disk-check")
    assert code == lab.EXIT_OK, (lines, errors)
    paths = [r["path"] for r in fake.calls if r.get("op") == "call"]
    assert paths.index("/v2/{project_id}/types") < paths.index("/v1/{project_id}/widgets")
    assert "| preflight | disk types | GET | evs | `/v2/{project_id}/types` | 200 |" in evidence(project)


def test_an_unknown_an_unoffered_or_an_unread_disk_type_stops_the_run(project):
    disk_recipe()
    fake = FakeKeyService()
    assert run(fake, recipe="disk-check", params=["disk=GPSSD9"])[2] == [
        "awb lab: the disk type GPSSD9 is not in the list of disk types of the tenant, so no create was tried"]
    fake.volume_types[0]["extra_specs"]["RESKEY:availability_zones"] = "eu-de-02"
    assert run(fake, recipe="disk-check")[2] == [
        "awb lab: the disk type SAS is not offered in eu-de-01, so no create was tried"]
    fake.types_status = 500
    code, lines, errors = run(fake, recipe="disk-check", params=["disk=SSD"])
    assert code == lab.EXIT_STOPPED
    assert errors == ["awb lab: the disk types could not be read (status 500), so no create was tried"]
    assert writes(fake) == [] and fake.created() == [] and rows(project) == []


def test_the_disk_types_of_the_recipes_and_the_dry_run_line():
    for name, want in (("ecs-boot-ssh", [("SAS", "eu-de-01")]), ("elb-health", [("SAS", "eu-de-01")]),
                       ("obs-policy-deny", [])):
        r = lab.load(REPO_RECIPES / ("%s.json" % name))
        params = lab.params_of(r, [])
        assert lab.disks_of(r, params) == want, name
        shown = [x for x in lab.dry_run_lines(r, params) if x.startswith("0. disk types:")]
        assert shown == (["0. disk types: GET evs /v2/{project_id}/types (read key); stop before the first create "
                          "unless SAS is listed and offered in eu-de-01 and not sold out there"] if want else [])


# --------------------------------------------------------------------------- elb-health: the web server or the checks


def elb_cloud(console="cloud-init done\nAWB-WEB-UP\n", member="ONLINE"):
    """A stand-in key service that answers the shapes of elb-health as the recipe expects them: the backend's
    console holds `console` and the member reads `member`."""
    state = {"n": 0, "live": {}, "made": [], "order": []}

    def new(kind):
        state["n"] += 1
        rid = "%08x-1111-4222-8333-%012x" % (state["n"], 0xbeef00 + state["n"])
        state["live"][rid] = kind
        state["made"].append(rid)
        state["order"].append(kind)
        return rid

    def ok(status, data=None):
        return {"ok": True, "status": status, "data": data, "text": ""}

    made_by = {"/v1/{project_id}/vpcs": "vpc", "/v1/{project_id}/subnets": "subnet",
               "/v1/{project_id}/security-groups": "security_group", "/v2.0/lbaas/loadbalancers": "loadbalancer",
               "/v2.0/lbaas/listeners": "listener", "/v2.0/lbaas/pools": "pool",
               "/v2.0/lbaas/healthmonitors": "healthmonitor"}

    def send(req):
        if req.get("op") == "tenants":
            return {"ok": True, "tenants": {TENANT: {"roles": ["lab", "read"], "secrets": ["ecs-admin"]}}}
        m, p, b = req["method"], req["path"], req.get("body") or {}
        if p == "/v1/{project_id}/vpcs" and m == "GET":
            return ok(200, {"vpcs": []})
        if p == "/v2/{project_id}/types" and m == "GET":
            return ok(200, {"volume_types": [{"name": "SAS", "extra_specs": {
                "RESKEY:availability_zones": "eu-de-01,eu-de-02,eu-de-03"}}]})
        if p == "/v2/cloudimages":
            return ok(200, {"images": [{"id": "-".join(IMAGE_ID), "name": req["query"]["name"]}]})
        if m == "POST" and p in made_by:
            kind = made_by[p]
            return ok(201, {kind: {"id": new(kind)}})
        if m == "POST" and re.fullmatch(r"/v2\.0/lbaas/pools/[^/]+/members", p):
            return ok(201, {"member": {"id": new("member")}})
        if m == "POST" and p.endswith("/tags/action"):
            return ok(204)
        if m == "POST" and p == "/v1/{project_id}/security-group-rules":
            state["order"].append("rule %s" % b["security_group_rule"]["remote_ip_prefix"])
            return ok(201, {"security_group_rule": {"id": "rule"}})
        if m == "POST" and p == "/v1/{project_id}/cloudservers":
            state["user_data"] = b["server"]["user_data"]
            return ok(200, {"job_id": "job", "serverIds": [new("server")]})
        if m == "POST" and p == "/v1/{project_id}/cloudservers/delete":
            for s in b["servers"]:
                state["live"].pop(s["id"], None)
            return ok(200, {"job_id": "job"})
        if m == "POST" and p.endswith("/action"):
            assert p.startswith("/v2/{project_id}/servers/") and b == {"os-getConsoleOutput": {}}
            assert state["live"].get(p.split("/")[4]) == "server"
            state["order"].append("console")
            return ok(200, {"output": console})
        if m == "DELETE":
            state["live"].pop(p.rsplit("/", 1)[1], None)
            return ok(204)
        if m == "GET":
            rid = p.rsplit("/", 1)[1]
            kind = state["live"].get(rid)
            if kind is None:
                return ok(404, {"error_msg": "not found"})
            if kind == "subnet":
                return ok(200, {"subnet": {"id": rid, "status": "ACTIVE", "neutron_subnet_id": "n-" + rid[:8]}})
            if kind == "server":
                vpc = next(k for k, v in state["live"].items() if v == "vpc")
                return ok(200, {"server": {"id": rid, "status": "ACTIVE",
                                           "addresses": {vpc: [{"addr": "192.168.10.23"}]}}})
            if kind == "loadbalancer":
                return ok(200, {"loadbalancer": {"id": rid, "provisioning_status": "ACTIVE"}})
            if kind == "member":
                state["order"].append("member read")
                return ok(200, {"member": {"id": rid, "operating_status": member}})
            return ok(200, {kind: {"id": rid}})
        return ok(404, {"error_msg": "no such path"})
    send.state = state
    return send


def test_the_elb_recipe_reads_the_backends_console_before_the_load_balancer_and_the_health_wait():
    r = lab.load(REPO_RECIPES / "elb-health.json")
    ids = [s["id"] for s in r["steps"]]
    assert ids.index("boot") < ids.index("web") < ids.index("lb") < ids.index("monitor") < ids.index("healthy")
    web = r["steps"][ids.index("web")]
    assert web["do"] == "wait" and web["call"]["method"] == "POST"
    assert web["call"]["path"] == "/v2/{project_id}/servers/${backend}/action"
    assert web["call"]["body"] == {"os-getConsoleOutput": {}} and web["until"]["contains"] == "AWB-WEB-"
    checks = [c["id"] for c in r["checks"]]
    assert checks.index("backend-boots") < checks.index("web-serves") < checks.index("health-passes")
    serves = r["checks"][checks.index("web-serves")]
    assert serves["step"] == "web" and serves["field"] == "output" and serves["contains"] == "AWB-WEB-UP"
    # the web server is a systemd service; the check runs after cloud-init and writes the marker to the console
    script = lab.expand("${web_script}", {**lab.params_of(r, []), **r["vars"]})
    assert "nohup" not in script and "/etc/systemd/system/awb-web.service" in script and "Restart=always" in script
    assert "systemctl enable --now awb-web.service" in script and "systemd-run --unit=awb-web-check" in script
    assert "cloud-init status --wait" in script and "</dev/tcp/127.0.0.1/80'" in script
    assert "echo AWB-WEB-UP | tee /dev/console /dev/ttyS0" in script and "echo AWB-WEB-DOWN" in script
    # the rule for the health checks stays and stays to confirm live
    rule = r["steps"][ids.index("sg-checks")]
    assert rule["call"]["body"]["security_group_rule"]["remote_ip_prefix"] == "${hc_source}"
    assert r["params"]["hc_source"]["default"] == "100.125.0.0/16" and rule["confirm"]
    readme = (REPO_RECIPES / "README.md").read_text(encoding="utf-8")
    assert "100.125.0.0/16" in readme and "AWB-WEB-UP" in readme and "web-serves" in readme


def test_the_elb_recipe_runs_end_to_end_with_the_console_read_before_the_load_balancer(project, monkeypatch):
    monkeypatch.delenv(lab.RECIPES_ENV)
    send = elb_cloud()
    code, lines, errors = run(send, recipe="elb-health", params=["port=8080"])
    assert code == lab.EXIT_OK, (lines, errors)
    assert send.state["live"] == {} and len(send.state["made"]) == 9
    order = send.state["order"]
    assert order.index("server") < order.index("console") < order.index("loadbalancer")
    assert order.index("healthmonitor") < order.index("member read")
    assert "rule 100.125.0.0/16" in order
    import base64

    script = base64.b64decode(send.state["user_data"]).decode("utf-8")
    assert "ExecStart=/usr/bin/python3 -m http.server 8080" in script and "</dev/tcp/127.0.0.1/8080'" in script
    text = evidence(project)
    assert "| web-serves | the web server of the backend answers on the port locally after cloud-init | " \
           "output contains AWB-WEB-UP" in text
    assert "| health-passes |" in text and "- result: passed" in text


def test_a_dead_web_server_fails_web_serves_before_any_load_balancer(project, monkeypatch):
    monkeypatch.delenv(lab.RECIPES_ENV)
    send = elb_cloud(console="cloud-init done\nAWB-WEB-DOWN\n")
    code, lines, errors = run(send, recipe="elb-health")
    assert code == lab.EXIT_CHECK
    assert "loadbalancer" not in send.state["order"] and send.state["live"] == {}
    text = evidence(project)
    assert "- result: check failed (web-serves)" in text
    assert "| health-passes | the member reads ONLINE within 5 minutes | reached within the wait | - | not run |" \
           in text


def test_a_blocked_health_check_fails_health_passes_after_web_serves_passed(project, monkeypatch):
    monkeypatch.delenv(lab.RECIPES_ENV)
    send = elb_cloud(member="OFFLINE")
    code, lines, errors = run(send, recipe="elb-health")
    assert code == lab.EXIT_CHECK
    assert send.state["live"] == {} and len(send.state["made"]) == 9
    text = evidence(project)
    assert "- result: check failed (health-passes)" in text
    assert "| web-serves | the web server of the backend answers on the port locally after cloud-init | " \
           "output contains AWB-WEB-UP | contains it | passed |" in text


# --------------------------------------------------------------------------- the recipe guards of the fact check (F2)

GPU_FLAVOR = "p2s.2xlarge.8"
BIG_FLAVOR = "s3.4xlarge.2"


def flavor_recipe(default="s3.medium.2") -> dict:
    recipe = copy.deepcopy(RECIPE)
    recipe["params"]["flavor"] = {"default": default, "pattern": r"^[a-z0-9]+\.[a-z0-9]+\.[0-9]+$"}
    recipe["steps"][0]["call"]["body"]["widget"]["flavorRef"] = "${flavor}"
    return recipe


def deny_line(n: int) -> str:
    return lab.DENY_FILE.read_text(encoding="utf-8").splitlines()[n - 1]


def test_the_deny_list_names_the_families_sizes_and_physical_flavors_of_the_design():
    for value, starts in ((GPU_FLAVOR, "p"), ("pi2.2xlarge.4", "p"), ("g6.xlarge.4", "g"), ("ai1.large.4", "ai"),
                          ("kp1.2xlarge.4", "kp"), (BIG_FLAVOR, "(?:.+"), ("physical.s4.large", "(?:.+\\.)?physical"),
                          ("rds.pg.c2.large.8", "(?:rds")):
        n = lab.denied(value)
        assert n and deny_line(n).startswith(starts), value
    for value in ("s3.medium.2", "s3.2xlarge.8", "c4.xlarge.4", "rds.pg.c2.large.4", "eu-de-01", "100.125.0.0/16",
                  "Standard_Ubuntu_22.04_latest", "${flavor}"):
        assert lab.denied(value) is None, value


@pytest.mark.parametrize("change, where", [
    (lambda r: r["params"]["flavor"].update(default=GPU_FLAVOR), "the default of param flavor"),
    (lambda r: r.update(vars={"probe": BIG_FLAVOR}), "var probe"),
    (lambda r: r["steps"][0]["call"]["body"]["widget"].update(flavorRef="g6.xlarge.4"),
     "a flavor field of step widget"),
])
def test_a_recipe_that_carries_a_denied_flavor_even_as_a_default_is_refused(change, where):
    recipe = flavor_recipe()
    lab.validate(recipe)
    change(recipe)
    with pytest.raises(lab.LabError) as info:
        lab.validate(recipe)
    said = str(info.value)
    assert said.startswith("cost guard: %s matches line " % where) and "recipes/DENY.txt" in said
    assert GPU_FLAVOR not in said and BIG_FLAVOR not in said and "g6" not in said


@pytest.mark.parametrize("value", [GPU_FLAVOR, BIG_FLAVOR])
def test_a_denied_flavor_as_a_parameter_is_refused_by_line_number_before_any_call(project, value):
    write_recipe(Path(os.environ[lab.RECIPES_ENV]), flavor_recipe())
    fake = FakeKeyService()
    code, lines, errors = run(fake, params=["flavor=" + value])
    assert code == lab.EXIT_STOPPED and fake.calls == []
    assert errors == ["awb lab: cost guard: the value of flavor matches line %d of recipes/DENY.txt (a denied flavor)"
                      % lab.denied(value)]
    assert value not in " ".join(lines + errors)


def test_a_deny_list_that_cannot_be_read_refuses_every_recipe(tmp_path, monkeypatch):
    monkeypatch.setattr(lab, "DENY_FILE", tmp_path / "missing.txt")
    with pytest.raises(lab.LabError, match="cannot be read"):
        lab.validate(flavor_recipe())
    (tmp_path / "broken.txt").write_text("# a comment\n(unclosed\n", encoding="utf-8")
    monkeypatch.setattr(lab, "DENY_FILE", tmp_path / "broken.txt")
    with pytest.raises(lab.LabError, match="line 2 of recipes/DENY.txt is not a regular expression"):
        lab.validate(flavor_recipe())


def test_the_recheck_cap_cuts_the_lifetime_to_30_minutes_and_the_budget_with_it(project):
    long = copy.deepcopy(RECIPE)
    long["guard"]["lifetime_minutes"], long["budget"] = 60, {"minutes": 40, "teardown_minutes": 15}
    cut = lab.capped(long)
    assert cut["guard"]["lifetime_minutes"] == lab.RECHECK_LIFETIME == 30
    assert cut["budget"] == {"minutes": 15, "teardown_minutes": 15}
    assert long["guard"]["lifetime_minutes"] == 60          # the recipe itself is unchanged
    lab.validate(cut)
    assert lab.capped(RECIPE)["guard"] == RECIPE["guard"]   # a guard under the cap stays
    with pytest.raises(lab.LabError, match="the teardown \\(15 minutes\\) does not fit in the cap of 10 minutes"):
        lab.capped(long, 10)
    write_recipe(Path(os.environ[lab.RECIPES_ENV]), long)
    code, lines, errors = run(FakeKeyService(), cap_minutes=30)
    assert code == lab.EXIT_OK, (lines, errors)
    assert "budget 15 minutes" in lines[0]
    assert "for at most 30 minutes" in evidence(project)


def test_a_random_password_is_drawn_by_the_key_service_and_never_in_the_answer_the_evidence_or_a_log(
        project, tmp_path, monkeypatch):
    from tests.tcp_fake import FakeGateway
    from tests.test_keys import LAB_AK, LAB_SK, STORE, running_service
    from awb.tcp import keys, throttle

    monkeypatch.setattr(throttle, "log_call", lambda *a, **k: None)
    made: dict[str, bool] = {}

    class EchoLab(FakeGateway):
        def route(self, method, raw_path):
            path = raw_path.partition("?")[0]
            base = "/v1/%s/widgets" % self.PROJECT
            if method == "POST" and path == base:
                rid = "%08x-2222-4333-8444-%012x" % (len(made) + 7, 0xbead)
                made[rid] = True
                return 200, {"widget": dict(json.loads(self.bodies[-1])["widget"], id=rid)}   # echoes the body
            if path.startswith(base + "/"):
                rid = path[len(base) + 1:]
                if method == "DELETE" and made.get(rid):
                    made[rid] = False
                    return 204, ""
                if method == "GET":
                    return (200, {"widget": {"id": rid, "status": "ACTIVE", "size": "small"}}) if made.get(rid) \
                        else (404, {"error_msg": "gone"})
            if method == "GET" and path == "/v1/%s/forbidden" % self.PROJECT:
                return 403, {"error_msg": "denied"}
            return super().route(method, raw_path)

    recipe = copy.deepcopy(RECIPE)
    recipe["steps"] = [s for s in recipe["steps"] if s["id"] != "gadget"]
    recipe["teardown"].pop("gadget")
    recipe["steps"][0]["call"]["body"]["widget"]["admin_pass"] = keys.RANDOM_PASSWORD
    recipe["steps"][0]["save"] = {"echoed": "widget.admin_pass"}
    write_recipe(Path(os.environ[lab.RECIPES_ENV]), recipe)
    with EchoLab(LAB_AK, LAB_SK) as gw, running_service(gw, tmp_path / "log") as svc:
        svc._active_project = lambda code: code == PROJECT
        keys.unlock(svc.admin_path, sorted(STORE), reader=lambda e: STORE[e], settings=_recipe_lab_writes())
        monkeypatch.setenv(keys.CALL_SOCKET_ENV, str(svc.call_path))
        lines: list[str] = []
        errors: list[str] = []
        clock = Clock()
        code = lab.run("widget-check", PROJECT, TENANT, [], clock=clock, sleep=clock.sleep, out=lines.append,
                       err=errors.append)
        assert code == lab.EXIT_OK, (lines, errors)
        sent = [json.loads(b) for b in gw.bodies if b and b"admin_pass" in b]
    assert len(sent) == 1
    drawn = sent[0]["widget"]["admin_pass"]
    assert drawn != keys.RANDOM_PASSWORD and len(drawn) == 16
    assert any(c.isupper() for c in drawn) and any(c.islower() for c in drawn) and any(c.isdigit() for c in drawn)
    text = evidence(project)
    logs = "".join(p.read_text(encoding="utf-8") for p in (tmp_path / "log").glob("*"))
    for where in (text, logs, "\n".join(lines + errors), (project / "RESOURCES.md").read_text(encoding="utf-8")):
        assert drawn not in where
    # a second call draws another value
    assert keys.random_password() != keys.random_password()


# --------------------------------------------------------------------------- the deny list over expanded values (review of 2026-10-16, finding R4)


def pieces_recipe() -> dict:
    """A recipe whose flavor is put together from pieces: no value of the file matches the deny list as it stands."""
    recipe = copy.deepcopy(RECIPE)
    recipe["params"]["size"] = {"default": "xlarge.2", "pattern": r"^[a-z0-9]+\.[0-9]+$"}
    recipe["vars"] = {"flavor": "p2s.${size}"}
    recipe["steps"][0]["call"]["body"]["widget"]["flavorRef"] = "${flavor}"
    return recipe


def test_a_denied_flavor_put_together_from_pieces_is_refused_before_any_call(project):
    """The var holds `${`, the body holds `${` and the parameter value alone matches no line, so the reading of the
    file passes; the run judges the expanded var and stops before its first call, naming the line and never the
    value."""
    recipe = pieces_recipe()
    lab.validate(recipe)
    write_recipe(Path(os.environ[lab.RECIPES_ENV]), recipe)
    fake = FakeKeyService()
    code, lines, errors = run(fake)
    assert code == lab.EXIT_STOPPED and fake.calls == []
    n = lab.denied("p2s.xlarge.2")
    assert n and errors == ["awb lab: cost guard: var flavor expands to a value that matches line %d of "
                            "recipes/DENY.txt (a denied flavor)" % n]
    assert "p2s" not in " ".join(lines + errors)
    assert rows(project) == [] and not list((project / "evidence").glob("lab-*"))


def test_the_expanded_body_of_a_call_is_judged_before_the_send(project):
    """`Run._call` walks the body it is about to send: a flavor field whose value only the run holds (a value a step
    saved from an answer) and that matches the deny list stops the run; the key service sees nothing."""
    from awb.tcp import sweep

    fake = FakeKeyService()
    recipe = lab.load(write_recipe(Path(os.environ[lab.RECIPES_ENV]), RECIPE))
    handles = sweep.Handles(project / "handles.json")
    job = lab.Run(recipe, lab.params_of(recipe, []), PROJECT, TENANT, project, handles, send=fake, clock=Clock())
    job.env["size"] = GPU_FLAVOR
    call = {"method": "POST", "service": "ecs", "path": "/v1/{project_id}/cloudservers",
            "body": {"server": {"name": "${name}", "flavorRef": "${size}"}}}
    with pytest.raises(lab.Stop) as info:
        job._call("run", "server", call)
    said = str(info.value)
    assert said.startswith("cost guard: a flavor field of the call of server matches line %d of recipes/DENY.txt"
                           % lab.denied(GPU_FLAVOR))
    assert fake.calls == [] and job.calls == [] and GPU_FLAVOR not in said
    job.env["size"] = "s3.medium.2"
    answer, _ = job._call("run", "server", call)
    assert len(fake.calls) == 1 and fake.calls[0]["body"]["server"]["flavorRef"] == "s3.medium.2"


# --------------------------------------------------------------------------- the path column of the register (finding R5)


def test_a_register_row_whose_path_is_another_folder_is_refused_before_any_call(project, home, tmp_path):
    """The path column of the register is in the shared tree a session writes: a row that points the project at
    another folder of the owner's is refused before any call and that folder is never written."""
    other = tmp_path / "elsewhere" / PROJECT          # the register wants a path that ends in the code
    other.mkdir(parents=True)
    (other / "SCOPE.md").write_text("- code: %s\n" % PROJECT, encoding="utf-8")
    projects._save(home, [projects.Project(PROJECT, "project", projects.NO_CUSTOMER, "tcp", str(other),
                                           projects.memory_key(str(other)), "active", "2026-10-11")])
    fake = FakeKeyService()
    code, _, errors = run(fake)
    assert code == lab.EXIT_STOPPED and fake.calls == []
    assert errors == ["awb lab: the path of %s in the project register is not its folder under the projects root"
                      % PROJECT]
    assert sorted(p.name for p in other.iterdir()) == ["SCOPE.md"]
