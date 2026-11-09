"""Task LK (2026-11-09): a lab run may keep a create on purpose, so a project changes a stand it holds (a rule of a
listed security group) without the end of the run undoing it. Every id here is invented."""
from __future__ import annotations

import os
import time
from types import SimpleNamespace

import pytest

from awb import config, kb, labclient, labruns, ownerwait
from awb.tcp import labadmin
from tests.test_keys import calls_logged_here, no_waiting  # noqa: F401
from tests.test_lr_lab_runs import LAB, LAB2, OTHER, PROJECT, fake, lab_call, run_op, svc  # noqa: F401

SG = "-".join(("6a1b2c3d", "1111", "4222", "8333", "9444" + "5" * 8))
RULES = "/v1/{project_id}/security-group-rules"
HEAD = ("| handle | id | type | region | cost class | expiry | state | note |\n"
        "|---|---|---|---|---|---|---|---|\n")


def _rule(sg=SG):
    return {"security_group_rule": {"security_group_id": sg, "direction": "ingress", "protocol": "tcp",
                                    "port_range_min": 443, "port_range_max": 443, "remote_ip_prefix": "0.0.0.0/0"}}


def _folder(tmp_path, rows: str):
    folder = tmp_path / "projects" / PROJECT
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "SCOPE.md").write_text("# %s\n\n- code: %s\n" % (PROJECT, PROJECT), encoding="utf-8")
    (folder / "RESOURCES.md").write_text("# Resources of %s\n\n%s%s" % (PROJECT, HEAD, rows), encoding="utf-8")
    return folder


STAND = "| sg-1 | %s | security group | eu-de | - | - | live | taken over on %s |\n" % (SG, LAB)


@pytest.fixture
def stand(svc, tmp_path):
    """The service as a session of PROJECT sees it, the project folder listing the security group SG live on LAB."""
    folder = _folder(tmp_path, STAND)
    rows = {PROJECT: SimpleNamespace(code=PROJECT, kind="project", state="active", customer=None, path=str(folder)),
            OTHER: SimpleNamespace(code=OTHER, kind="project", state="active", customer=None, path="")}
    svc._project_row = lambda code: rows.get(code)
    svc._granted = lambda code: code in rows
    svc._console_uid = lambda: None
    return svc, os.getuid() + 1, folder


def test_lk_a_kept_rule_survives_the_end_and_shows_in_the_owner_status(stand, fake):
    svc, uid, _folder_ = stand
    run = run_op(svc, uid, "start")["run"]
    rule = lab_call(svc, uid, "POST", "vpc", RULES, run, body=_rule(), keep="https for the stand")
    assert rule["ok"] and rule["created"] == ["vpc security-group-rules"], rule
    rid = rule["data"]["security-group-rule"]["id"]
    assert rule["kept"] == [{"n": 1, "label": "vpc security-group-rules", "id": rid, "region": "eu-de",
                             "reason": "https for the stand"}]
    vpc = lab_call(svc, uid, "POST", "vpc", "/v1/{project_id}/vpcs", run, body={"vpc": {"name": "lk"}})
    status = run_op(svc, uid, "status")["runs"]
    assert status[0]["kept"] == 1 and [c["label"] for c in status[0]["left"]] == ["vpc vpcs"]
    ended = run_op(svc, uid, "end")
    assert ended["ok"] and ended["created"] == 2 and ended["failed"] == []
    assert [c["label"] for c in ended["removed"]] == ["vpc vpcs"]
    assert ended["kept"] == [{"label": "vpc security-group-rules", "id": rid, "reason": "https for the stand"}]
    assert fake.deleted == ["/v1/%s/vpcs/%s" % ("e" * 32, vpc["data"]["vpc"]["id"])]
    assert any(k.endswith("/security-group-rules/" + rid) for k in fake.store)      # the rule stays
    items = ownerwait.lab_run_items(svc.paths_fn(), time.time())
    assert [(i.label, i.text) for i in items] == [
        (PROJECT, "lab run on %s kept vpc security-group-rules %s: https for the stand" % (LAB, rid[:8]))]
    later = ownerwait.lab_run_items(svc.paths_fn(), time.time() + (labruns.KEPT_DAYS + 1) * 86400)
    assert later == []


def test_lk_a_run_without_keep_still_removes_the_rule(stand, fake):
    svc, uid, _f = stand
    run = run_op(svc, uid, "start")["run"]
    rule = lab_call(svc, uid, "POST", "vpc", RULES, run, body=_rule())
    assert "kept" not in rule
    ended = run_op(svc, uid, "end")
    assert [c["label"] for c in ended["removed"]] == ["vpc security-group-rules"] and ended["kept"] == []
    assert fake.deleted == ["/v1/%s/security-group-rules/%s" % ("e" * 32, rule["data"]["security-group-rule"]["id"])]


def test_lk_keep_outside_the_project_is_refused_with_one_line_and_makes_nothing(stand, fake):
    svc, uid, folder = stand
    run2 = run_op(svc, uid, "start", tenant=LAB2)["run"]
    before = fake.n
    other = lab_call(svc, uid, "POST", "vpc", RULES, run2, tenant=LAB2, body=_rule("7" * 8 + "-0000-4000-8000-" + "1" * 12),
                     keep="not ours")
    assert not other["ok"] and other["error"] == labadmin.KEEP_ANCHOR_LINE % (PROJECT, LAB2)
    assert "\n" not in other["error"] and fake.n == before
    short = lab_call(svc, uid, "POST", "vpc", RULES, run2, tenant=LAB2, body=_rule(), keep="x")
    assert not short["ok"] and short["error"] == labadmin.KEEP_REASON_LINE % labadmin.REASON_MAX
    ok = lab_call(svc, uid, "POST", "vpc", RULES, run2, tenant=LAB2, body=_rule(), keep="the listed group")
    assert ok["ok"] and ok["kept"]                       # the parent is listed: allowed on any lab tenant
    (folder / "RESOURCES.md").write_text(HEAD + STAND.replace("| live |", "| deleted |"), encoding="utf-8")
    run = run_op(svc, uid, "start")["run"]
    gone = lab_call(svc, uid, "POST", "vpc", RULES, run, body=_rule(), keep="the group was deleted")
    assert not gone["ok"] and gone["error"] == labadmin.KEEP_ANCHOR_LINE % (PROJECT, LAB)


def test_lk_keep_after_the_create_and_an_update_is_never_undone(stand, fake):
    svc, uid, _f = stand
    run = run_op(svc, uid, "start")["run"]
    rule = lab_call(svc, uid, "POST", "vpc", RULES, run, body=_rule())
    assert rule["ok"] and "kept" not in rule
    fake.store["/v1/%s/security-groups/%s" % ("e" * 32, SG)] = {"id": SG}
    put = lab_call(svc, uid, "PUT", "vpc", "/v1/{project_id}/security-groups/" + SG, run, body={"x": 1})
    assert put["ok"] and put["created"] == []
    wrong = run_op(svc, uid, "keep", item="9", reason="no such record")
    assert not wrong["ok"] and wrong["error"] == labadmin.KEEP_ITEM_LINE % (run, "9")
    kept = run_op(svc, uid, "keep", item="1", reason="https for the stand")
    assert kept["ok"] and kept["kept"][0]["id"] == rule["data"]["security-group-rule"]["id"]
    ended = run_op(svc, uid, "end")
    assert ended["removed"] == [] and len(ended["kept"]) == 1 and fake.deleted == []


def test_lk_a_delete_of_a_listed_resource_needs_the_flag(stand, fake):
    svc, uid, _f = stand
    run = run_op(svc, uid, "start")["run"]
    path = "/v1/{project_id}/security-groups/" + SG
    refused = lab_call(svc, uid, "DELETE", "vpc", path, run)
    assert not refused["ok"] and refused["error"] == labadmin.LISTED_DELETE_LINE % ("sg-1", PROJECT)
    batch = lab_call(svc, uid, "POST", "ecs", "/v1/{project_id}/cloudservers/delete", run,
                     body={"servers": [{"id": SG}]})
    assert not batch["ok"]
    assert lab_call(svc, uid, "DELETE", "vpc", path, run, delete_listed=True)["ok"]
    assert lab_call(svc, uid, "DELETE", "vpc", "/v1/{project_id}/security-group-rules/" + "2" * 32, run)["ok"]


def test_lk_labclient_keeps_writes_the_row_and_reports_it(svc, fake, tmp_path, monkeypatch, capsys):
    folder = _folder(tmp_path, STAND)
    svc._project_row = lambda code: SimpleNamespace(code=code, kind="project", state="active", customer=None,
                                                    path=str(folder)) if code == PROJECT else None
    monkeypatch.chdir(folder)
    monkeypatch.setattr(config, "is_work_user", lambda: False)
    assert labclient.run_main(["start", "--tenant", LAB]) == 0
    out = capsys.readouterr().out
    assert "plain python3 has no awb module" in out
    status, _h, body = labclient.call("POST", "vpc", RULES, body=_rule(), keep="https for the stand")
    rid = body["security-group-rule"]["id"]
    labclient.call("POST", "vpc", "/v1/{project_id}/security-group-rules", body=_rule())
    assert labclient.run_main(["keep", "2", "--reason", "second rule too"]) == 0
    assert "kept vpc security-group-rules" in capsys.readouterr().out
    text = (folder / "RESOURCES.md").read_text(encoding="utf-8")
    assert "| security-group-rule-1 | %s | vpc security-group-rules | eu-de | - | - | live | kept from lab run" % rid \
        in text and "| security-group-rule-2 |" in text
    assert labclient.run_main(["end"]) == 0
    out = capsys.readouterr().out
    assert "created 2, removed 0, failed 0, kept 2" in out and "  kept     vpc security-group-rules %s" % rid in out
    run = next((folder / "notes" / "lab-runs").glob("run-*.log"))
    log = run.read_text(encoding="utf-8")
    assert "keep: https for the stand" in log and "kept: vpc security-group-rules %s" % rid in log
    from awb import projects

    assert projects.live_resources(folder) == 3             # the kept rows count as live for awb close


def test_lk_kb_add_says_which_flags_are_required(capsys):
    with pytest.raises(SystemExit):
        kb._parser().parse_args(["add", "a statement"])
    err = capsys.readouterr().err
    assert "required: --scope, --tag, --grade, --class, --source and the statement" in err
