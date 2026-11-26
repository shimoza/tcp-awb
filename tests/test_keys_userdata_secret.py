"""The tenant key as a secret in the user data of a helper ECS (tcp-bytg ask 2026-11-25-tcp-bytg-6).

`{{secret:obs-ak}}` and `{{secret:obs-sk}}` are filled by the key service from the key the call itself uses and only
into the script it renders itself for the `user_data_script` of a POST on ECS cloudservers (findings F1 and F2 of the
attacker review of 2026-11-26); the service sends the filled text as base64 `user_data`.
No answer carries the user data of a server and no log carries a key. Every key and name here is invented; the
fakes are those of tests/test_keys.py.
"""
from __future__ import annotations

import base64
import json

import pytest

from awb.tcp import diskreader, keys
from tests.test_keys import (LAB_AK, LAB_SK, PROJECT, STORE, EchoGateway, call, calls_logged_here,  # noqa: F401
                             lab_gateway, load, no_waiting, svc)

SERVERS = "/v1/{project_id}/cloudservers"
VOLUME = "%08x-aaaa-4bbb-8ccc-%012x" % (0x9f8e7d6c, 0xddddeeeeffff)
SCRIPT = {"name": "diskreader", "params": {"bucket": PROJECT + "-lab-20261126-abcd", "region": "eu-de",
                                             "volume": VOLUME, "files": ""}}
TEXT = "#!/bin/sh\nAK={{secret:obs-ak}}\nSK={{secret:obs-sk}}\necho done\n"
FILLED = diskreader.from_params(SCRIPT["params"]).replace("{{secret:obs-ak}}", LAB_AK).replace("{{secret:obs-sk}}",
                                                                                             LAB_SK)


class ServerGateway(EchoGateway):
    """The echo gateway that also answers a read of a server with the user data it got at the create."""

    user_data = ""

    def route(self, method, raw_path):
        path = raw_path.partition("?")[0]
        base = "/v1/%s/cloudservers" % self.PROJECT
        if method == "POST" and path == base:
            self.user_data = json.loads(self.bodies[-1] or b"{}").get("server", {}).get("user_data", "")
        if method == "GET" and path == base + "/detail":
            return 200, {"servers": [{"id": "srv-1", "name": "reader", "OS-EXT-SRV-ATTR:user_data": self.user_data}]}
        if method == "GET" and path == base + "/srv-1":
            return 200, {"server": {"id": "srv-1", "name": "reader", "OS-EXT-SRV-ATTR:user_data": self.user_data,
                                    "metadata": {"user_data": self.user_data}}}
        return super().route(method, raw_path)


@pytest.fixture
def gw():
    with ServerGateway("AKFAKE", "sk-fake-secret") as g:
        yield g


def create(svc, server=None, path=SERVERS, service="ecs"):
    given = {"name": "lab-reader"}
    if "user_data_text" not in (server or {}):
        given["user_data_script"] = SCRIPT
    body = {"server": dict(given, **(server or {}))}
    return call(svc, role="lab", method="POST", service=service, path=path, body=body, project=PROJECT)


def with_triples(svc, line: str):
    return keys.unlock(svc.admin_path, sorted(STORE), reader=lambda e: STORE[e], settings={"lab_writes": line})


def test_a_server_create_sends_base64_user_data_that_holds_the_lab_key(svc, gw):
    load(svc)
    lab_gateway(gw)
    a = create(svc)
    assert a["ok"] and a["status"] == 200, a
    sent = json.loads(gw.bodies[-1])["server"]
    assert "user_data_text" not in sent and "user_data_script" not in sent and sent["name"] == "lab-reader"
    assert base64.b64decode(sent["user_data"]).decode("utf-8") == FILLED
    assert LAB_AK in FILLED and LAB_SK in FILLED and "{{" not in FILLED


def test_the_v1_1_create_takes_the_text_too(svc, gw):
    lab_gateway(gw)
    with_triples(svc, "POST ecs /v1.1/{project_id}/cloudservers")
    a = create(svc, path="/v1.1/{project_id}/cloudservers")
    assert a["ok"], a
    assert base64.b64decode(json.loads(gw.bodies[-1])["server"]["user_data"]).decode("utf-8") == FILLED


def test_the_answer_of_the_create_carries_no_user_data_and_no_key(svc, gw):
    load(svc)
    lab_gateway(gw)
    a = create(svc)
    assert a["ok"], a
    text = json.dumps(a)
    assert LAB_AK not in text and LAB_SK not in text
    assert base64.b64encode(FILLED.encode()).decode() not in text
    assert "user_data" not in a["data"]["server"]["server"]


def test_a_get_of_a_server_answers_without_user_data(svc, gw):
    load(svc)
    lab_gateway(gw)
    assert create(svc)["ok"] and gw.user_data
    coded = gw.user_data
    for path, pick in ((SERVERS + "/srv-1", lambda d: d["server"]), (SERVERS + "/detail", lambda d: d["servers"][0])):
        a = call(svc, role="lab", method="GET", service="ecs", path=path, project=PROJECT)
        assert a["ok"] and a["status"] == 200, a
        row = pick(a["data"])
        assert row["name"] == "reader" and not [k for k in row if "user_data" in k.lower()], row
        assert "user_data" not in row.get("metadata", {})
        text = json.dumps(a)
        assert coded not in text and "user_data" not in text and LAB_AK not in text and LAB_SK not in text, text


def test_a_cut_answer_text_loses_its_user_data_too(svc):
    answer = {"ok": True, "status": 200, "data": None,
              "text": '{"servers":[{"id":"srv-1","OS-EXT-SRV-ATTR:user_data":"QUtB' + "x" * 50}
    masked = svc._mask(answer, {"roles": {}})
    assert "user_data" not in masked["text"] and "QUtB" not in masked["text"] and "srv-1" in masked["text"]


def test_the_key_service_log_says_it_and_holds_no_key(svc, gw, tmp_path, calls_logged_here):
    load(svc)
    lab_gateway(gw)
    assert create(svc)["ok"]
    assert create(svc, {"user_data_text": "#!/bin/sh\necho plain\n"})["ok"]
    log = next((tmp_path / "log").glob("cloud-*.tsv")).read_text(encoding="utf-8")
    lines = [line for line in log.splitlines() if "\tPOST\tecs\t" in line]
    assert len(lines) == 2
    assert keys.USER_DATA_LOG == "user data with the tenant key"
    assert keys.USER_DATA_LOG in lines[0] and keys.USER_DATA_LOG not in lines[1]
    for text in (log, json.dumps(calls_logged_here)):
        assert LAB_AK not in text and LAB_SK not in text and "{{secret" not in text
        assert base64.b64encode(FILLED.encode()).decode() not in text


@pytest.mark.parametrize("server", [
    {"name": "{{secret:obs-ak}}"},
    {"admin_pass": "{{secret:obs-sk}}"},
    {"user_data": "{{secret:obs-sk}}"},
    {"metadata": {"note": "{{secret:obs-ak}}"}},
    {"admin_pass": "x{{secret:obs-sk}}"},
])
def test_the_key_names_are_refused_in_any_other_field(svc, gw, server):
    load(svc)
    lab_gateway(gw)
    n = len(gw.calls)
    a = call(svc, role="lab", method="POST", service="ecs", path=SERVERS, project=PROJECT, body={"server": server})
    assert not a["ok"] and ("obs-" in a["error"] or "whole value" in a["error"]), a
    assert len(gw.calls) == n          # refused before anything was sent


def test_the_key_names_are_refused_in_the_body_of_another_service(svc, gw):
    lab_gateway(gw)
    with_triples(svc, "POST vpc /v1/{project_id}/vpcs")
    n = len(gw.calls)
    a = call(svc, role="lab", method="POST", service="vpc", path="/v1/{project_id}/vpcs", project=PROJECT,
             body={"vpc": {"name": "{{secret:obs-ak}}"}})
    assert not a["ok"] and "obs-ak" in a["error"], a
    a = call(svc, role="lab", method="POST", service="vpc", path="/v1/{project_id}/vpcs", project=PROJECT,
             body={"vpc": {"name": "x", "user_data_text": TEXT}})
    assert not a["ok"] and "user_data_text" in a["error"], a
    assert len(gw.calls) == n


def test_user_data_text_is_refused_outside_a_server_create(svc, gw):
    lab_gateway(gw)
    with_triples(svc, "POST ecs /v1/{project_id}/cloudservers, POST ecs /v1/{project_id}/cloudservers/action")
    n = len(gw.calls)
    other_path = create(svc, path="/v1/{project_id}/cloudservers/action")
    assert not other_path["ok"] and "user_data_text" in other_path["error"], other_path
    not_in_server = call(svc, role="lab", method="POST", service="ecs", path=SERVERS, project=PROJECT,
                         body={"user_data_text": TEXT, "server": {"name": "x"}})
    twice = create(svc, {"metadata": {"user_data_text": TEXT}})
    both = create(svc, {"user_data": "QUJD"})
    for a in (not_in_server, twice, both):
        assert not a["ok"] and "user_data_text" in a["error"], a
    assert len(gw.calls) == n


@pytest.mark.parametrize("text", ["x {{secret:ecs-admin}}", "x {{random:password}}", "x {{secret:obs-ak}} {{secret:x}}",
                                  "x {{secret:obs-AK}}"])
def test_the_text_takes_the_two_key_names_and_no_other_reference(svc, gw, text):
    load(svc)
    lab_gateway(gw)
    n = len(gw.calls)
    a = create(svc, {"user_data_text": text})
    assert not a["ok"] and "obs-ak" in a["error"] and "obs-sk" in a["error"], a
    assert len(gw.calls) == n


def test_the_read_key_fills_nothing_because_it_writes_nothing(svc, gw):
    load(svc)
    a = call(svc, role="read", method="POST", service="ecs", path=SERVERS, project=PROJECT,
             body={"server": {"user_data_script": SCRIPT}})
    assert not a["ok"] and "allows" in a["error"], a
