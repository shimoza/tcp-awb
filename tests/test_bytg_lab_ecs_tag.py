"""Every ECS a lab run creates carries the tag awb-project of its project (tcp-bytg ask 4, 2026-11-21): a POST of an
ECS went through the lab client without it. Every id, code and path here is invented."""
from __future__ import annotations

import pytest

from awb.tcp import labadmin, projecttags
from tests.test_keys import calls_logged_here, no_waiting  # noqa: F401
from tests.test_lr_lab_runs import OTHER, PROJECT, fake, lab_call, run_op, session, svc  # noqa: F401


@pytest.fixture
def sent(monkeypatch):
    """(method, path, body) of every call the key service sent to the stand-in API."""
    out = []
    real = labadmin._send

    def record(svc, signer, run_id, method, service, path, query, body, *rest):
        out.append((method, path, body))
        return real(svc, signer, run_id, method, service, path, query, body, *rest)

    monkeypatch.setattr(labadmin, "_send", record)
    return out


def test_bytg_an_ecs_of_the_native_api_gets_the_ecs_tag_call_with_its_project(session, sent):
    svc, uid = session
    run = run_op(svc, uid, "start")["run"]
    a = lab_call(svc, uid, "POST", "ecs", "/v2.1/{project_id}/servers", run, body={"server": {"name": "lr-nova"}})
    assert a["ok"] and a["status"] == 201, a
    rid = a["data"]["server"]["id"]
    method, path, body = sent[-1]
    assert (method, path) == ("POST", "/v1/{project_id}/cloudservers/%s/tags/action" % rid)   # filled in by _send
    assert body == {"action": "create", "tags": [projecttags.tag(PROJECT)]}
    assert projecttags.inject("POST", "ecs", "/v2/p9/servers", {}, PROJECT)[1] == "after"
    assert projecttags.inject("POST", "ecs", "/v2.1/p9/servers/x1/action", {}, PROJECT)[1] is None


def test_bytg_a_cloudservers_create_without_the_tag_gets_it_and_one_with_its_own_tag_keeps_it(session, sent):
    svc, uid = session
    run = run_op(svc, uid, "start")["run"]
    a = lab_call(svc, uid, "POST", "ecs", "/v1/{project_id}/cloudservers", run, body={"server": {"name": "lr-1"}})
    assert a["ok"], a
    assert sent[-1][2]["server"]["server_tags"] == [projecttags.tag(PROJECT)]
    own = {"server": {"name": "lr-2", "server_tags": [projecttags.tag(PROJECT),
                                                      {"key": "awb-expiry", "value": "2026-11-30"}]}}
    a = lab_call(svc, uid, "POST", "ecs", "/v1.1/{project_id}/cloudservers", run, body=own)
    assert a["ok"], a
    assert sent[-1][2]["server"]["server_tags"] == [{"key": "awb-expiry", "value": "2026-11-30"},
                                                     projecttags.tag(PROJECT)]


@pytest.mark.parametrize("path,body", [
    ("/v1/{project_id}/cloudservers", {"server": {"server_tags": [{"key": "awb-project", "value": OTHER}]}}),
    ("/v1/{project_id}/cloudservers", {"server": {"tags": ["awb-project.%s" % OTHER]}}),
    ("/v2/{project_id}/cloudvolumes", {"volume": {"tags": {"awb-project": OTHER}}}),
])
def test_bytg_a_create_with_the_tag_of_another_project_is_refused_and_nothing_is_sent(session, sent, path, body):
    svc, uid = session
    run = run_op(svc, uid, "start")["run"]
    before = len(sent)
    service = "evs" if "volumes" in path else "ecs"
    a = lab_call(svc, uid, "POST", service, path, run, body=body)
    assert not a["ok"] and a["error"] == labadmin.FOREIGN_TAG_LINE % (PROJECT, PROJECT), a
    assert len(sent) == before
    assert run_op(svc, uid, "status")["runs"][0]["left"] == []


def test_bytg_an_ecs_create_that_cannot_carry_the_tag_is_refused_before_it_is_sent(session, sent):
    svc, uid = session
    run = run_op(svc, uid, "start")["run"]
    before = len(sent)
    for body in ({"name": "lr-flat"}, ["lr"], {"server": "lr"}):
        a = lab_call(svc, uid, "POST", "ecs", "/v1/{project_id}/cloudservers", run, body=body)
        assert not a["ok"] and a["error"] == labadmin.ECS_UNTAGGED_LINE % (projecttags.KEY, PROJECT), a
    assert len(sent) == before
    # an action on an ECS creates nothing and passes as before
    a = lab_call(svc, uid, "POST", "ecs", "/v1/{project_id}/cloudservers/x1/metadata", run, body={"metadata": {}})
    assert a["ok"], a
