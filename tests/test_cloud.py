"""awb/tcp/cloud.py against a stand-in API gateway that checks every signature (tests/tcp_fake.py)."""
from __future__ import annotations

import socket

import pytest

from awb import cli, config, jobs, obs
from awb.tcp import cloud
from tests.tcp_fake import FakeGateway
from tests.test_obs import fake_pass

AK, SK = "AKFAKE", "sk-fake-secret"


@pytest.fixture
def gw():
    with FakeGateway(AK, SK) as g:
        yield g


@pytest.fixture(autouse=True)
def no_waiting(monkeypatch):
    monkeypatch.setattr(jobs.time, "sleep", lambda s: None)


def client(gw, **kw) -> cloud.Client:
    return cloud.Client(obs.Keys(kw.pop("ak", AK), kw.pop("sk", SK)), "eu-de", endpoint=gw.base, **kw)


def test_the_project_is_the_one_named_like_the_region(gw):
    c = client(gw)
    assert c.project_id() == FakeGateway.PROJECT
    assert c.expand("/v1/{project_id}/vpcs") == "/v1/%s/vpcs" % FakeGateway.PROJECT
    c.project_id()
    assert sum(1 for _, p in gw.calls if p.startswith("/v3/projects")) == 1      # asked once


def test_a_paged_list_by_marker_and_by_offset(gw):
    c = client(gw)
    vpcs = c.list("vpc", "/v1/{project_id}/vpcs", "vpcs", limit=3)
    assert vpcs.state == jobs.LIST and [v["id"] for v in vpcs.items] == ["vpc-%02d" % i for i in range(7)]
    ports = c.list("vpc", "/v1/{project_id}/ports", "ports", paging="offset", limit=2)
    assert ports.state == jobs.LIST and len(ports.items) == 5


def test_a_limit_in_the_query_is_the_page_size(gw):
    c = client(gw)
    vpcs = c.list("vpc", "/v1/{project_id}/vpcs", "vpcs", query={"limit": ["2"]})
    assert vpcs.state == jobs.LIST and len(vpcs.items) == 7
    pages = [p for m, p in gw.calls if "/vpcs" in p]
    assert len(pages) == 4 and all("limit=2" in p for p in pages)
    with pytest.raises(cloud.CloudError, match="is a number"):
        c.list("vpc", "/v1/{project_id}/vpcs", "vpcs", query={"limit": "many"})


def test_empty_and_unknown_are_kept_apart(gw):
    c = client(gw)
    assert c.list("vpc", "/v1/{project_id}/empty", "items").state == jobs.EMPTY
    odd = c.list("vpc", "/v1/{project_id}/odd", "items")
    assert odd.state == jobs.UNKNOWN and "no list under items" in odd.reason
    boom = c.list("vpc", "/v1/{project_id}/boom", "items")
    assert boom.state == jobs.UNKNOWN and boom.reason == "HTTP 500"
    gw.vpcs = [{"name": "no id"}] * 3
    assert c.list("vpc", "/v1/{project_id}/vpcs", "vpcs", limit=3).state == jobs.UNKNOWN


def test_a_wrong_key_is_an_answer_not_a_crash_and_the_key_never_shows(gw):
    gw.echo_key = True
    c = client(gw, sk="wrong-secret")
    r = c.get("iam", "/v3/projects")
    assert r.status == 401 and not r.ok
    assert AK not in r.text and "<ak>" in r.text
    with pytest.raises(cloud.CloudError, match="HTTP 401"):
        c.project_id()


def test_a_busy_service_is_asked_again_within_the_budget(gw):
    gw.busy = 2
    c = client(gw)
    listing = c.list("vpc", "/v1/{project_id}/busy", "items", paging="none")
    assert listing.state == jobs.LIST
    gw.busy = 5
    assert c.list("vpc", "/v1/{project_id}/busy", "items", paging="none").reason == "HTTP 503"


def test_no_answer_at_all_raises_without_the_key():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    c = cloud.Client(obs.Keys(AK, SK), endpoint="http://127.0.0.1:%d" % port, timeout=2)
    with pytest.raises(cloud.CloudError, match="no answer") as ei:
        c.get("iam", "/v3/projects")
    assert SK not in str(ei.value) and AK not in str(ei.value)


def test_names_and_paths_are_checked():
    c = cloud.Client(obs.Keys(AK, SK))
    with pytest.raises(cloud.CloudError):
        c.url("ECS!", "/v1")
    with pytest.raises(cloud.CloudError):
        c.url("ecs", "v1/no-slash")
    with pytest.raises(cloud.CloudError):
        cloud.Client(obs.Keys(AK, SK), "Frankfurt")
    assert c.url("ecs", "/v1/x", {"a": ["1", "2"]}) == "https://ecs.eu-de.otc.t-systems.com/v1/x?a=1&a=2"


def test_the_work_user_and_a_missing_key_setting_are_refused(monkeypatch):
    monkeypatch.delenv("AWB_CLOUD_KEYS", raising=False)
    with pytest.raises(cloud.CloudError, match="AWB_CLOUD_KEYS"):
        cloud.settings()
    monkeypatch.setattr(config, "is_work_user", lambda: True)
    with pytest.raises(cloud.CloudError, match="T-100"):
        cloud.settings()


def test_the_command_line_reads_and_never_writes(gw, tmp_path, monkeypatch, capsys):
    fake_pass(tmp_path, monkeypatch, {"cloud/owner/ak": AK, "cloud/owner/sk": SK})
    monkeypatch.setenv("AWB_CLOUD_KEYS", "pass:cloud/owner")
    monkeypatch.setenv("AWB_CLOUD_ENDPOINT", gw.base)
    assert cli.main(["cloud", "get", "vpc", "/v1/{project_id}/vpcs", "--list", "vpcs", "--limit", "3"]) == 0
    out = capsys.readouterr().out
    assert out.startswith("awb cloud: list (7)") and out.count('"id": "vpc-') == 7
    assert cli.main(["cloud", "get", "vpc", "/v1/{project_id}/odd", "--list", "items"]) == 2
    assert "unknown" in capsys.readouterr().out
    assert cli.main(["cloud", "get", "vpc", "/v1/{project_id}/boom"]) == 1
    capsys.readouterr()
    assert cli.main(["cloud", "projects"]) == 0
    out = capsys.readouterr().out
    assert "eu-de" in out and FakeGateway.PROJECT not in out and "3 project(s)" in out
    assert cli.main(["cloud", "post", "vpc", "/v1/x"]) == 2
    assert all(method == "GET" for method, _ in gw.calls)
