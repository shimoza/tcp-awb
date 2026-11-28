"""G1 of the attacker review of 2026-11-28: a lease taken before the reader server is made reads the key back.

B-092 refused a lease while a server with the key of the tenant in its user data lives. A lease that is out already
stays valid for an hour and calls ECS past the key service, where nothing strips the user data from the answer. So
every lease denies the reads of a server that answer its user data, and the list of noted servers no longer loses a
server whose create answered a job (v1) and whose name the platform gave a suffix or the caller changed. No loopback
but for the replaced lease test: the platform is a stand-in of cloud.Client.request. Every key and id is invented.
"""
from __future__ import annotations

import os

import pytest

from awb.tcp import keys
from tests.test_keys import (LAB_AK, PROJECT, _Minted, calls_logged_here, gw, load, no_waiting,  # noqa: F401
                             svc)

PATH = "/v1/{project_id}/cloudservers"
CODE = "tcp-abcd"
PAIR = {"ak": "AKGONEFAKE", "sk": "sk-g1-fake-secret"}
VOLUME = "%08x-aaaa-4bbb-8ccc-%012x" % (0x9f8e7d6c, 0xddddeeeeffff)
READER = "%08x-6666-4777-8888-%012x" % (0x1a2b3c4d, 0xbeef01)
JOB = "%032x" % 0xff8080814dbd65d7014dbe0d84db0013
NAME = CODE + "-reader"
SCRIPT = {"name": "diskreader", "params": {"bucket": CODE + "-lab-20261128-abcd", "region": "eu-de", "volume": VOLUME,
                                           "files": ""}}
READS = ["ecs:cloudServers:get", "ecs:cloudServers:list", "ecs:servers:get", "ecs:servers:list"]


class Platform:
    """IAM and ECS as the key service calls them. A v1 create answers a job; the job names the server once `job_ids`
    is set; the list shows the rows of `rows`."""

    def __init__(self):
        self.sent, self.rows, self.job_ids, self.gone = [], [], None, False

    def __call__(self, client, method, service, path, query=None, body=None, headers=None):
        from awb.tcp.cloud import Response

        self.sent.append((method, service, path, query, body))
        if method == "POST" and path == PATH:
            return Response(200, {"job_id": JOB}, "")
        if method == "GET" and path == "/v1/{project_id}/jobs/" + JOB:
            if self.job_ids is None:
                return Response(404, {}, "")
            return Response(200, {"status": "SUCCESS", "entities": {"sub_jobs": [
                {"entities": {"server_id": i}} for i in self.job_ids]}}, "")
        if method == "GET" and path == PATH + "/detail":
            return Response(200, {"servers": list(self.rows)}, "")
        if method == "GET" and path == PATH + "/" + READER:
            if self.gone:
                return Response(404, {}, "")
            return Response(200, {"server": {"id": READER, "status": "ACTIVE"}}, "")
        if service == "iam":
            return Response(201, {"credential": {"access": "TMPAKGONE", "secret": "tmp-secret-g1",
                                                 "securitytoken": "tmp-token-g1"}}, "")
        return Response(404, {}, "")


@pytest.fixture
def service(tmp_path, monkeypatch):
    from awb.tcp import cloud

    platform = Platform()
    monkeypatch.setattr(cloud.Client, "request", lambda c, *a, **k: platform(c, *a, **k))
    s = keys.Service(tmp_path / "admin.sock", tmp_path / "call.sock", log_dir=tmp_path / "log")
    s.tenants = {"test-1": {"roles": {"lab": dict(PAIR)}}}
    s.settings = {"lab_writes": "POST ecs " + PATH}
    s._registered = lambda: {"test-1": ("eu-de",)}
    s._active_project = lambda code: code == CODE
    return s, platform


def lease(s):
    return s._lease({"tenant": "test-1", "minutes": 60, "project": CODE}, os.getuid())


def denied(policy: dict) -> set:
    return {a for st in policy["Statement"] if st["Effect"] == "Deny" and "Resource" not in st for a in st["Action"]}


# --------------------------------------------------------------------------- the lease reads no server


def test_a_lease_taken_before_the_keyed_server_denies_the_reads_that_answer_user_data(service):
    s, platform = service
    assert lease(s)["ok"]
    policy = next(b for m, svc_, p, q, b in platform.sent if svc_ == "iam")["auth"]["identity"]["policy"]
    s.note_keyed("test-1", "eu-de", {"server": {"name": NAME}}, {"serverIds": [READER]})
    for held in (policy, s.lease_policy(CODE)):
        assert set(READS) <= denied(held), "the lease that is out already reads no server either"
        assert "ecs:cloudServers:get" in denied(held)


def test_the_denied_reads_are_in_every_lease_whatever_keys_conf_allows(service):
    s, _ = service
    for line in (None, "ecs, evs", "vpc", ""):
        if line is None:
            s.settings.pop("tf_services", None)
        else:
            s.settings["tf_services"] = line
        for project in (CODE, ""):
            assert {"Effect": "Deny", "Action": READS} in s.lease_policy(project)["Statement"], (line, project)


# --------------------------------------------------------------------------- the noted server is not lost


def test_a_v1_create_whose_name_got_a_suffix_stays_noted(service):
    """The test of the finding: a job answer carries no ids, the platform lists the server as NAME-0001."""
    s, platform = service
    s.note_keyed("test-1", "eu-de", {"server": {"name": NAME, "count": 2}}, {"job_id": JOB})
    platform.rows = [{"id": READER, "name": NAME + "-0001", "status": "ACTIVE"}]
    assert s.keyed_left("test-1", PAIR) == 1
    with pytest.raises(keys.Refused, match="still holds 1 server"):
        lease(s)
    platform.rows = [{"id": READER, "name": NAME + "-0001", "status": "DELETED"}]
    assert s.keyed_left("test-1", PAIR) == 0


def test_the_job_of_a_v1_create_names_the_server_so_a_rename_does_not_free_it(service):
    s, platform = service
    s.note_keyed("test-1", "eu-de", {"server": {"name": NAME}}, {"job_id": JOB})
    platform.job_ids = [READER]
    platform.rows = []          # renamed: no row carries the noted name any more
    assert s.keyed_left("test-1", PAIR) == 1
    assert s.keyed["test-1"][0]["ids"] == [READER]
    platform.gone = True
    assert s.keyed_left("test-1", PAIR) == 0


def test_a_keyed_create_through_the_service_notes_the_job(service):
    s, platform = service
    body = {"server": {"name": NAME, "user_data_script": SCRIPT}}
    a = s._call({"tenant": "test-1", "role": "lab", "method": "POST", "service": "ecs", "path": PATH,
                 "region": "eu-de", "body": body, "project": CODE}, os.getuid())
    assert a["ok"] and a["status"] == 200, a
    assert s.keyed["test-1"] == [{"region": "eu-de", "name": NAME, "ids": [], "job": JOB}]


def test_job_servers_reads_only_server_ids():
    data = {"entities": {"server_id": READER, "sub_jobs": [{"entities": {"server_id": "x; rm"}}, "odd",
                                                           {"entities": {"server_id": READER}}]}}
    assert keys._job_servers(data) == [READER]
    assert keys._job_servers(None) == [] and keys._job_servers({"entities": []}) == []


# --------------------------------------------------------------------------- the replaced lease test


def test_a_lease_mints_a_temporary_key_with_iam_the_server_reads_and_the_exchange_buckets_denied(svc, monkeypatch,  # noqa: F811
                                                                                                tmp_path):
    """Replaces test_a_lease_mints_a_temporary_key_with_iam_and_the_exchange_buckets_denied (G1 of the review of
    2026-11-28): the same lease, whose policy now also denies the reads of a server that answer its user data."""
    from awb.tcp import cloud

    load(svc)
    minted = _Minted()
    monkeypatch.setattr(cloud.Client, "request", minted.method())
    a = keys.request(svc.call_path, {"op": "lease", "tenant": "test-1", "minutes": 60})
    assert a["ok"] and (a["ak"], a["sk"], a["token"]) == ("TMPAKX7Q9", "tmp-secret-value", "tmp-token-value")
    ak, method, service, path, body = minted.sent[0]
    assert (ak, method, service, path) == (LAB_AK, "POST", "iam", "/v3.0/OS-CREDENTIAL/securitytokens")
    identity = body["auth"]["identity"]
    assert identity["methods"] == ["token"] and identity["token"] == {"duration_seconds": 3600}
    exchange = ["obs:*:*:bucket:awb", "obs:*:*:object:awb/*", "obs:*:*:bucket:awb-lab-eu-de",
                "obs:*:*:object:awb-lab-eu-de/*"]
    allow, iam, reads, buckets = identity["policy"]["Statement"]
    assert allow == {"Effect": "Allow", "Action": ["%s:*:*" % s for s in keys.TF_SERVICES if s != "obs"]}
    assert iam == {"Effect": "Deny", "Action": ["iam:*:*"]}
    assert reads == {"Effect": "Deny", "Action": READS}
    assert buckets == {"Effect": "Deny", "Action": ["obs:*:*"], "Resource": exchange}
    log = "".join(p.read_text() for p in (tmp_path / "log").glob("*.tsv"))
    assert "LEASE" in log and "TMPA" in log and "TMPAKX7Q9" not in log and "tmp-secret" not in log
    statements = svc.lease_policy(PROJECT)["Statement"]
    assert {"Effect": "Allow", "Action": ["obs:*:*"], "Resource": ["obs:*:*:bucket:%s-*" % PROJECT,
                                                                   "obs:*:*:object:%s-*/*" % PROJECT]} in statements
    assert statements[-1]["Effect"] == "Deny" and statements[-1]["Resource"] == exchange
    svc.settings["tf_services"] = "vpc, iam ,ecs"
    assert [s["Action"] for s in svc.lease_policy(PROJECT)["Statement"]] == [["vpc:*:*", "ecs:*:*"], ["iam:*:*"],
                                                                             READS, ["obs:*:*"]]
    svc.settings["bucket_tenant"] = "test-1"
    a = keys.request(svc.call_path, {"op": "lease", "tenant": "test-1", "minutes": 60})
    assert not a["ok"] and "exchange buckets" in a["error"] and len(minted.sent) == 1
