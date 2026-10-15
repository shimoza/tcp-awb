"""The recipes of fact-check task F7 (ecs-flavor-boot, smn-http-confirm, rds-create-refusal) and the three small
parts of the lab they need: a create whose refusal a check expects goes on when nothing was made, a check of
report is reported and never fails the run and a gone proof may name a second read under also."""
from __future__ import annotations

import copy

import pytest

from awb import cli
from awb.tcp import lab
from tests.lab_fake import FakeKeyService
from tests.test_lab import (PROJECT, RECIPE, REPO_RECIPES, TENANT, evidence, project,  # noqa: F401
                            rows, run, write_recipe)

FK7 = ("ecs-flavor-boot", "smn-http-confirm", "rds-create-refusal")


@pytest.mark.parametrize("name", FK7)
def test_each_fk7_recipe_passes_the_validator_and_the_dry_run(name, monkeypatch, capsys):
    monkeypatch.delenv(lab.RECIPES_ENV, raising=False)
    r = lab.load(REPO_RECIPES / ("%s.json" % name))
    assert r["name"] == name and lab.confirm_notes(r)
    assert r["guard"]["lifetime_minutes"] <= lab.RECHECK_LIFETIME
    assert lab.capped(r)["budget"] == r["budget"]
    assert cli.main(["lab", "dry-run", name, "--project", PROJECT, "--tenant", TENANT]) == 0
    out = capsys.readouterr().out
    assert out.rstrip().endswith("dry run: no call was made") and "teardown, newest first" in out


@pytest.mark.parametrize("value", ["p2s.xlarge.8", "g6.2xlarge.4", "pi2.2xlarge.4"])
def test_ecs_flavor_boot_refuses_a_gpu_flavor_parameter_before_any_call(project, tmp_path, value):
    write_recipe(tmp_path / "recipes", lab.load(REPO_RECIPES / "ecs-flavor-boot.json"))
    with pytest.raises(lab.LabError, match="matches line [0-9]+ of recipes/DENY.txt"):
        lab.params_of(lab.load(REPO_RECIPES / "ecs-flavor-boot.json"), ["flavor=" + value])
    fake = FakeKeyService()
    code, lines, errors = run(fake, recipe="ecs-flavor-boot", params=["flavor=" + value])
    assert code == lab.EXIT_STOPPED and fake.calls == []
    assert errors == ["awb lab: cost guard: the value of flavor matches line %d of recipes/DENY.txt (a denied flavor)"
                      % lab.denied(value)]


def test_ecs_flavor_boot_admits_the_s9_family_and_refuses_a_size_above_2xlarge():
    r = lab.load(REPO_RECIPES / "ecs-flavor-boot.json")
    for value in ("s9.medium.2", "s9.2xlarge.4", "c7n.large.2", "s3.medium.1"):
        assert lab.params_of(r, ["flavor=" + value])["flavor"] == value
    with pytest.raises(lab.LabError, match="cost guard"):
        lab.params_of(r, ["flavor=s9.4xlarge.2"])


def refusal_recipe(status: int = 500) -> dict:
    """The widget recipe whose gadget create is expected to be refused with `status` and has a find by name."""
    recipe = copy.deepcopy(RECIPE)
    gadget = next(s for s in recipe["steps"] if s["id"] == "gadget")
    gadget["find"] = {"call": {"method": "GET", "service": "vpc", "path": "/v1/{project_id}/gadgets"},
                      "list": "gadgets", "field": "id", "within": 20}
    recipe["checks"].append({"id": "gadget-refused", "step": "gadget", "says": "the gadget is refused",
                             "status": [status]})
    return recipe


@pytest.mark.parametrize("status", [500, 400])
def test_an_expected_refusal_of_a_create_goes_on_when_nothing_was_made(project, tmp_path, status):
    write_recipe(tmp_path / "recipes", refusal_recipe(status))
    fake = FakeKeyService()
    fake.fail_create = {"gadgets": status}
    code, lines, errors = run(fake)
    assert code == lab.EXIT_OK, (lines, errors)
    assert fake.live() == [] and len(fake.created()) == 1
    text = evidence(project)
    assert "| gadget-refused |" in text and "| refused |" in text
    states = [r for r in rows(project) if "gad" in str(r)]
    assert all("deleted" not in str(r) for r in states)
    if status == 500:
        assert any("not created" in str(r) for r in states)
    else:
        assert states == []


def test_an_accepted_create_where_a_refusal_was_expected_fails_its_check_and_is_torn_down(project, tmp_path):
    write_recipe(tmp_path / "recipes", refusal_recipe())
    fake = FakeKeyService()
    code, lines, errors = run(fake)
    assert code == lab.EXIT_CHECK
    assert len(fake.created()) == 2 and fake.live() == []
    assert "| gadget-refused |" in evidence(project)


def test_a_check_of_report_is_reported_and_does_not_fail_the_run(project, tmp_path):
    recipe = copy.deepcopy(RECIPE)
    recipe["checks"].append({"id": "colour", "step": "read", "says": "the widget names a colour",
                             "field": "widget.colour", "present": True, "report": True})
    write_recipe(tmp_path / "recipes", recipe)
    fake = FakeKeyService()
    code, lines, errors = run(fake)
    assert code == lab.EXIT_OK, (lines, errors)
    text = evidence(project)
    assert "| colour |" in text and "| absent | reported |" in text and "awb kb add" in text


def also_recipe(also_path: str) -> dict:
    recipe = copy.deepcopy(RECIPE)
    recipe["teardown"]["widget"]["gone"]["also"] = {
        "call": {"method": "GET", "service": "vpc", "path": also_path}, "status": [404]}
    return recipe


def test_a_gone_proof_with_also_reads_both_and_proves_the_resource_gone(project, tmp_path):
    write_recipe(tmp_path / "recipes", also_recipe("/v1/{project_id}/widgets/${id}"))
    fake = FakeKeyService()
    code, lines, errors = run(fake)
    assert code == lab.EXIT_OK, (lines, errors)
    assert fake.live() == []
    assert sum(1 for c in fake.calls if c["method"] == "GET" and c["path"].startswith("/v1/{project_id}/widgets/")) >= 3


def test_a_gone_proof_whose_also_does_not_hold_leaves_the_row_live(project, tmp_path):
    write_recipe(tmp_path / "recipes", also_recipe("/v1/{project_id}/forbidden"))
    fake = FakeKeyService()
    code, lines, errors = run(fake)
    assert code == lab.EXIT_LEFT
    assert any("LEFT LIVE" in str(r) for r in rows(project))


@pytest.mark.parametrize("change, said", [
    (lambda r: r["checks"][0].update(report="yes"), "report is true or false"),
    (lambda r: r["teardown"]["widget"]["gone"].update(also={"call": {"method": "GET", "service": "vpc",
                                                                     "path": "/v1/{project_id}/x"}}),
     "gone names the statuses"),
    (lambda r: r["teardown"]["widget"]["gone"].update(also={"call": {"method": "DELETE", "service": "vpc",
                                                                     "path": "/v1/{project_id}/x"},
                                                            "status": [404]}),
     "the gone proof is a read"),
    (lambda r: r["teardown"]["widget"]["gone"].update(also="twice"), "also of gone is a second read"),
])
def test_the_validator_refuses_a_malformed_report_or_also(change, said):
    recipe = copy.deepcopy(RECIPE)
    lab.validate(recipe)
    change(recipe)
    with pytest.raises(lab.LabError, match=said):
        lab.validate(recipe)


def test_a_find_by_another_field_places_a_create_whose_answer_was_lost(project, tmp_path):
    recipe = copy.deepcopy(RECIPE)
    gadget = next(s for s in recipe["steps"] if s["id"] == "gadget")
    gadget["call"]["body"]["gadget"]["remark"] = "${name}"
    gadget["find"] = {"call": {"method": "GET", "service": "vpc", "path": "/v1/{project_id}/gadgets"},
                      "list": "gadgets", "by": "remark", "field": "id"}
    lab.validate(recipe)
    write_recipe(tmp_path / "recipes", recipe)
    fake = FakeKeyService()
    fake.made_but = {"gadgets": 500}
    real = fake.__call__

    def with_remark(req):
        answer = real(req)
        if req.get("method") == "GET" and req.get("path") == "/v1/{project_id}/gadgets" and answer.get("ok"):
            for item in answer["data"]["gadgets"]:
                item["remark"], item["name"] = fake.items[item["id"]]["body"].get("remark"), None
        return answer
    code, lines, errors = run(with_remark)
    assert fake.live() == [] and len(fake.created()) == 2
    assert any("gad" in str(r) and "deleted" in str(r) for r in rows(project))


def test_by_of_a_find_needs_a_list():
    recipe = copy.deepcopy(RECIPE)
    recipe["steps"][0]["find"] = {"call": {"method": "GET", "service": "vpc", "path": "/v1/{project_id}/widgets"},
                                  "field": "widgets.0.id", "by": "remark"}
    with pytest.raises(lab.LabError, match="by of a find names the field of a list item"):
        lab.validate(recipe)
