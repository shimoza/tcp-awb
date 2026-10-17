"""The shape of the recipe rds-create-refusal after its live runs of 2026-10-17 (eu-de, two runs, nothing created): the
defaults, the storage-type preflight and the rule that a request shape error (400 with DBS.28xxxx) is a recipe error
and never the answer of the collation question. All on the fake key service."""
from __future__ import annotations

import copy

import pytest

from awb import cli
from awb.tcp import lab
from tests.lab_fake import FakeKeyService
from tests.test_lab import (PROJECT, REPO_RECIPES, TENANT, evidence, project,  # noqa: F401
                            run, write_recipe)

NAME = "rds-create-refusal"


def install(tmp_path):
    write_recipe(tmp_path / "recipes", lab.load(REPO_RECIPES / ("%s.json" % NAME)))


def shape(code, status=400):
    return lambda body: (status, {"error_code": code, "error_msg": "Invalid request"})


def test_the_dry_run_shows_the_new_defaults(monkeypatch, capsys):
    monkeypatch.delenv(lab.RECIPES_ENV, raising=False)
    assert cli.main(["lab", "dry-run", NAME, "--project", PROJECT, "--tenant", TENANT]) == 0
    out = capsys.readouterr().out
    assert '"flavor_ref": "rds.mssql.x1.large.2"' in out and '"volume": {"size": 40, "type": "CLOUDSSD"}' in out
    assert "rds.mssql.se." not in out and "ULTRAHIGH" not in out
    assert "0. preflight storage-type: GET rds /v3/{project_id}/storage-type/SQLServer ?version_name=2019_SE" in out
    assert "unless CLOUDSSD is normal in eu-de-01" in out


def test_the_dry_run_takes_the_volume_type_parameter(monkeypatch, capsys):
    monkeypatch.delenv(lab.RECIPES_ENV, raising=False)
    assert cli.main(["lab", "dry-run", NAME, "--project", PROJECT, "--tenant", TENANT,
                     "--param", "volume_type=GPSSD2"]) == 0
    assert '"type": "GPSSD2"' in capsys.readouterr().out


def test_the_default_flavor_passes_the_deny_list_and_the_volume_type_is_a_parameter():
    r = lab.load(REPO_RECIPES / ("%s.json" % NAME))
    params = lab.params_of(r, [])
    assert params["flavor"] == "rds.mssql.x1.large.2" and params["volume_type"] == "CLOUDSSD"
    assert lab.params_of(r, ["volume_type=GPSSD2"])["volume_type"] == "GPSSD2"
    with pytest.raises(lab.LabError):
        lab.params_of(r, ["volume_type=cloud ssd"])


@pytest.mark.parametrize("code", ["DBS.280434", "DBS.280325"])
@pytest.mark.parametrize("step", ["tested", "control"])
def test_a_shape_error_stops_the_run_as_a_recipe_error_and_creates_no_instance(project, tmp_path, code, step):
    install(tmp_path)
    fake = FakeKeyService()
    if step == "control":
        fake.rds_answer = lambda body: (500, {"error_code": "DBS.200005", "error_msg": "refused"}) \
            if body["collation"] != "Awb_No_Such_Collation_CI_AS" else shape(code)(body)
    else:
        fake.rds_answer = shape(code)
    code_, lines, errors = run(fake, recipe=NAME)
    assert code_ == lab.EXIT_STOPPED, (lines, errors)
    assert any("recipe error" in x and code in x for x in lines), lines
    verdict = {x.split(":")[0][6:]: x.split(":")[1].split(" (")[0].strip() for x in lines if x.startswith("check ")}
    assert verdict == ({"tested-refused": "not run", "control-refused": "not run", "nothing-made": "not run"}
                       if step == "tested" else
                       {"tested-refused": "passed", "control-refused": "not run", "nothing-made": "not run"})
    assert fake.live() == []
    assert len(fake.rds_bodies) == (1 if step == "tested" else 2)
    assert "No fact: the run did not finish its checks" in evidence(project)


def test_a_400_with_another_code_is_still_not_the_collation_answer(project, tmp_path):
    install(tmp_path)
    fake = FakeKeyService()
    fake.rds_answer = shape("DBS.200005", status=400)
    code, lines, errors = run(fake, recipe=NAME)
    assert code == lab.EXIT_CHECK and fake.live() == []


def test_only_the_500_of_the_collation_passes_both_refusal_checks(project, tmp_path):
    install(tmp_path)
    fake = FakeKeyService()
    code, lines, errors = run(fake, recipe=NAME)
    assert code == lab.EXIT_OK, (lines, errors)
    assert fake.live() == [] and len(fake.rds_bodies) == 2
    assert all(b["flavor_ref"] == "rds.mssql.x1.large.2" and b["volume"]["type"] == "CLOUDSSD" for b in fake.rds_bodies)


def test_an_unsupported_storage_type_stops_before_any_create(project, tmp_path):
    install(tmp_path)
    fake = FakeKeyService()
    code, lines, errors = run(fake, recipe=NAME, params=["volume_type=ULTRAHIGH"])
    assert code == lab.EXIT_STOPPED and fake.created() == [] and fake.rds_bodies == []
    assert errors == ["awb lab: ULTRAHIGH is not normal in eu-de-01 (unsupported), so no create was tried; "
                      "set another value in the recipe"]
    assert not any(c["method"] == "POST" for c in fake.calls)


@pytest.mark.parametrize("change", ["unlisted", "unreadable"])
def test_a_storage_type_that_is_not_listed_or_not_readable_stops_before_any_create(project, tmp_path, change):
    install(tmp_path)
    fake = FakeKeyService()
    if change == "unlisted":
        fake.storage_types = [t for t in fake.storage_types if t["name"] != "CLOUDSSD"]
    else:
        fake.storage_status = 500
    code, lines, errors = run(fake, recipe=NAME)
    assert code == lab.EXIT_STOPPED and fake.created() == [] and fake.rds_bodies == []
    assert len(errors) == 1 and "so no create was tried" in errors[0]


def test_the_az_must_be_normal_in_the_az_of_the_run(project, tmp_path):
    install(tmp_path)
    fake = FakeKeyService()
    for t in fake.storage_types:
        if t["name"] == "CLOUDSSD":
            t["az_status"]["eu-de-02"] = "sellout"
    code, lines, errors = run(fake, recipe=NAME, params=["az=eu-de-02"])
    assert code == lab.EXIT_STOPPED and fake.created() == []
    assert "CLOUDSSD is not normal in eu-de-02 (sellout)" in errors[0]


@pytest.mark.parametrize("bad", [
    {"shape_error": {"status": [400], "code": "("}},
    {"shape_error": {"status": [], "code": "x"}},
    {"shape_error": {"status": [400]}}])
def test_a_malformed_shape_error_is_refused(bad):
    r = copy.deepcopy(lab.load(REPO_RECIPES / ("%s.json" % NAME)))
    next(s for s in r["steps"] if s["id"] == "tested").update(bad)
    with pytest.raises(lab.LabError, match="shape_error"):
        lab.validate(r)


def test_a_malformed_preflight_is_refused():
    r = copy.deepcopy(lab.load(REPO_RECIPES / ("%s.json" % NAME)))
    r["preflight"][0]["call"]["method"] = "POST"
    with pytest.raises(lab.LabError, match="preflight"):
        lab.validate(r)
