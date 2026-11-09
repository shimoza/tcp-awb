"""The project tag on every create of a project (PT, 2026-11-09): the owner runs into quotas on the test tenants and
has to see which project made what. Every code here is invented; the project code comes from tests/test_keys.py."""
from __future__ import annotations

import datetime
import json

import pytest

from awb.tcp import keys, projecttags, tenanttags
from tests.test_keys import (PROJECT, STORE, EchoGateway, call, calls_logged_here, gw, lab_gateway,  # noqa: F401
                             no_waiting, svc)

SERVERS = "/v1/{project_id}/cloudservers"


@pytest.mark.parametrize("service,path,body,where", [
    ("ecs", SERVERS, {"server": {"name": "x"}}, ("server", "server_tags")),
    ("ecs", "/v1.1/abc/cloudservers", {"server": {"name": "x"}}, ("server", "server_tags")),
    ("evs", "/v2/{project_id}/cloudvolumes", {"volume": {"size": 10}}, ("volume", "tags")),
    ("elb", "/v3/{project_id}/elb/loadbalancers", {"loadbalancer": {"name": "x"}}, ("loadbalancer", "tags")),
    ("cce", "/api/v3/projects/{project_id}/clusters/c1/nodes", {"spec": {"flavor": "x"}}, ("spec", "userTags")),
    ("dcs", "/v2/{project_id}/instances", {"name": "x"}, (None, "tags")),
    ("cbr", "/v3/{project_id}/vaults", {"vault": {"name": "x"}}, ("vault", "tags")),
])
def test_a_create_of_a_tag_capable_service_carries_the_project_tag_in_its_own_field(service, path, body, where):
    new, how = projecttags.inject("POST", service, path, body, PROJECT)
    assert how == "inline"
    parent_new, parent_old = (new, body) if where[0] is None else (new[where[0]], body[where[0]])
    tags = parent_new[where[1]]
    assert tags == {"awb-project": PROJECT} if isinstance(tags, dict) else tags == [projecttags.tag(PROJECT)]
    assert where[1] not in parent_old              # the body given is never changed in place


def test_a_tag_of_another_project_is_set_to_the_project_of_the_call_and_other_tags_stay():
    body = {"server": {"server_tags": [{"key": "awb-project", "value": "tcp-zz99"},
                                       {"key": "awb-expiry", "value": "2026-11-10"}]}}
    new, _ = projecttags.inject("POST", "ecs", SERVERS, body, PROJECT)
    assert new["server"]["server_tags"] == [{"key": "awb-expiry", "value": "2026-11-10"}, projecttags.tag(PROJECT)]
    vol = {"volume": {"tags": {"awb-project": "tcp-zz99", "awb-expiry": "2026-11-10"}}}
    new, _ = projecttags.inject("POST", "evs", "/v2/{project_id}/cloudvolumes", vol, PROJECT)
    assert new["volume"]["tags"] == {"awb-project": PROJECT, "awb-expiry": "2026-11-10"}


@pytest.mark.parametrize("method,service,path", [
    ("GET", "ecs", SERVERS), ("DELETE", "ecs", SERVERS + "/x"), ("POST", "ecs", SERVERS + "/delete"),
    ("POST", "vpc", "/v2.0/{project_id}/vpcs/x/tags/action"), ("POST", "ecs", SERVERS + "/action"),
    ("POST", "iam", "/v3/auth/tokens"), ("PUT", "obs", "/tcp-ab2c-x"),
])
def test_a_call_that_creates_nothing_is_left_alone(method, service, path):
    body = {"a": 1}
    assert projecttags.inject(method, service, path, body, PROJECT) == (body, None)


def test_a_vpc_subnet_or_eip_create_gets_a_tag_call_with_the_new_id():
    for path, answer, tpath in (("/v1/{project_id}/vpcs", {"vpc": {"id": "v-1"}}, "/v2.0/{project_id}/vpcs/v-1/"),
                                ("/v1/p9/subnets", {"subnet": {"id": "s-1"}}, "/v2.0/p9/subnets/s-1/"),
                                ("/v1/p9/publicips", {"publicip": {"id": "e-1"}}, "/v2.0/p9/publicips/e-1/")):
        assert projecttags.inject("POST", "vpc", path, {}, PROJECT)[1] == "after"
        method, service, call_path, body = projecttags.after_call("vpc", path, answer, PROJECT)
        assert (method, service, call_path) == ("POST", "vpc", tpath + "tags/action")
        assert body == {"action": "create", "tags": [projecttags.tag(PROJECT)]}
    assert projecttags.after_call("vpc", "/v1/p9/vpcs", {"vpc": {"id": "../x"}}, PROJECT) is None


def test_an_rds_create_gets_the_tag_call_its_body_takes_no_tags():
    body = {"name": "x"}
    assert projecttags.inject("POST", "rds", "/v3/p9/instances", body, PROJECT) == (body, "after")
    assert projecttags.after_call("rds", "/v3/p9/instances", {"instance": {"id": "r1in03"}}, PROJECT)[2] == \
        "/v3/p9/instances/r1in03/tags/action"


def test_a_cce_cluster_gets_tags_create_without_action_and_a_nat_gateway_its_project_placeholder():
    _m, _s, path, body = projecttags.after_call("cce", "/api/v3/projects/p9/clusters", {"metadata": {"uid": "c-1"}},
                                                PROJECT)
    assert path == "/api/v3/projects/p9/clusters/c-1/tags/create" and body == {"tags": [projecttags.tag(PROJECT)]}
    _m, _s, path, _b = projecttags.after_call("nat", "/v2.0/nat_gateways", {"nat_gateway": {"id": "n-1"}}, PROJECT)
    assert path == "/v2.0/{project_id}/nat_gateways/n-1/tags/action"


def test_any_other_create_is_untagged_and_its_line_is_read_back(tmp_path):
    assert projecttags.inject("POST", "nat", "/v2.0/snat_rules", {}, PROJECT)[1] == "untagged"
    assert projecttags.inject("POST", "vpc", "/v1/p9/security-groups", {}, PROJECT)[1] == "untagged"
    now = datetime.datetime(2026, 11, 9, 10, 0, tzinfo=datetime.timezone.utc)
    projecttags.note_untagged("test-1", PROJECT, "dcs", "/v2/p9/instances", "untagged", shared=tmp_path, now=now)
    assert projecttags.untagged(tmp_path, "2026-11") == [
        {"at": "2026-11-09T10:00:00Z", "tenant": "test-1", "project": PROJECT, "service": "dcs",
         "path": "/v2/p9/instances", "result": "untagged"}]


def test_the_key_service_sends_the_tag_in_the_create_body_and_says_so(svc, gw):
    from tests.test_keys import load

    load(svc)
    lab_gateway(gw)
    a = call(svc, role="lab", method="POST", service="ecs", path=SERVERS, body={"server": {"name": "x"}},
             project=PROJECT)
    assert a["ok"], a
    assert json.loads(gw.bodies[-1])["server"]["server_tags"] == [projecttags.tag(PROJECT)]
    assert a.get("tagged") == "inline"


def test_a_read_is_never_tagged(svc, gw):
    from tests.test_keys import load

    load(svc)
    a = call(svc, role="read", method="GET", service="ecs", path=SERVERS)
    assert a["ok"] and "tagged" not in a


def _snap(p, alias, items):
    folder = p.shared / "tenants" / alias / "snapshots"
    folder.mkdir(parents=True)
    (folder / "2026-11-08.json").write_text(json.dumps({"date": "2026-11-08", "items": items}))


def test_the_report_counts_per_project_tag_and_marks_full_and_near_quotas(tmp_path):
    class P:
        shared = tmp_path

    items = [{"handle": "ecs-1", "kind": "ecs", "project": PROJECT, "created": "2026-11-01"},
             {"handle": "eip-1", "kind": "eip", "project": PROJECT, "created": "2026-11-01"},
             {"handle": "ecs-2", "kind": "ecs", "project": "", "created": "2026-11-01"}]
    _snap(P, "test-1", items)

    def read(alias, service, path):
        if service == "ecs":
            return {"absolute": {"maxTotalInstances": 10, "totalInstancesUsed": 8}}
        return {"quotas": {"resources": [{"type": "publicIp", "used": 10, "quota": 10},
                                         {"type": "vpc", "used": 1, "quota": 10}]}}

    out = "\n".join(tenanttags.lines(P, "test-1", datetime.date(2026, 11, 9), read))
    assert PROJECT in out and "\n  -" in out
    rows = {line.split()[0]: line.split() for line in out.splitlines() if line.startswith("  ")}
    assert rows[PROJECT][1:] == ["1", "1", "2"] and rows["-"][1:] == ["1", "0", "1"]
    assert rows["publicIp"][-1] == "full" and rows["servers"][-1] == "near" and rows["vpc"][-1] == "10"


def test_a_quota_that_cannot_be_read_is_one_line_and_the_report_stands(tmp_path):
    class P:
        shared = tmp_path

    def read(alias, service, path):
        raise RuntimeError("refused")

    out = tenanttags.lines(P, "test-1", datetime.date(2026, 11, 9), read)
    assert out[0].startswith("test-1: no snapshot yet")
    assert "test-1: ECS limits not readable now" in out and "test-1: VPC quotas not readable now" in out
