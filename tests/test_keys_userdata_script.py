"""The key service renders the reader script itself (findings F1 and F2 of the attacker review of 2026-11-26).

F1: `{{secret:obs-ak}}` and `{{secret:obs-sk}}` are never filled into a text the caller wrote. A server create names
the script and its parameters (`server.user_data_script`), the service renders awb/tcp/diskreader.py with them, each
checked by its pattern, fills the key of the call and sends base64 user_data. F2: a lease of a tenant that still holds
such a server is refused, because a leased key reads the user data of a server past the key service. No loopback: the
service is called in this process and the platform is a stand-in of cloud.Client.request. Every key and id here is
invented.
"""
from __future__ import annotations

import base64
import json
import os
from pathlib import Path

import pytest

from awb.tcp import diskreader, keys, lab
from tests.test_keys import calls_logged_here, no_waiting  # noqa: F401

PATH = "/v1/{project_id}/cloudservers"
PROJECT = "tcp-ab2c"
AK, SK = "AKSCRIPTFAKE", "sk-script-fake-secret"
PAIR = {"ak": AK, "sk": SK}
VOLUME = "%08x-aaaa-4bbb-8ccc-%012x" % (0x9f8e7d6c, 0xddddeeeeffff)
READER = "%08x-6666-4777-8888-%012x" % (0x1a2b3c4d, 0xbeef01)
PARAMS = {"bucket": PROJECT + "-lab-20261126-abcd", "region": "eu-de", "volume": VOLUME,
          "files": "Windows/Panther/setupact.log"}


def script(**change) -> dict:
    return {"name": "diskreader", "params": dict(PARAMS, **change)}


def create(server: dict, pair=PAIR, **kw):
    return keys.with_user_data({"server": dict({"name": "lab-reader"}, **server)}, "POST", "ecs", PATH, pair, **kw)


# --------------------------------------------------------------------------- F1: no caller text is filled


def test_a_caller_text_with_the_key_name_is_refused_and_never_filled():
    with pytest.raises(keys.Refused, match="user_data_script"):
        create({"user_data_text": "echo {{secret:obs-ak}} | rev"})
    with pytest.raises(keys.Refused):
        create({"user_data_text": "#!/bin/sh\nput {{secret:obs-sk}} somewhere\n"})


def test_a_user_data_that_carries_a_key_name_is_refused_plain_or_in_base64():
    for value in ("{{secret:obs-ak}}", base64.b64encode(b"echo {{secret:obs-sk}} | rev").decode()):
        with pytest.raises(keys.Refused, match="obs-ak"):
            create({"user_data": value})
        with pytest.raises(keys.Refused):
            keys.with_user_data({"server": {"metadata": {"user_data": value}}}, "POST", "ecs", PATH, PAIR)


def test_the_diskreader_script_is_rendered_by_the_service_with_the_key_filled():
    body, filled = create({"user_data_script": script()}, project=PROJECT, region="eu-de")
    server = body["server"]
    assert filled and "user_data_script" not in server and server["name"] == "lab-reader"
    env = {"name": PARAMS["bucket"], "region": "eu-de", "sys_volume": VOLUME, "files": PARAMS["files"]}
    want = diskreader.usertext(env).replace("{{secret:obs-ak}}", AK).replace("{{secret:obs-sk}}", SK)
    assert base64.b64decode(server["user_data"]).decode("utf-8") == want
    assert AK in want and SK in want and "{{secret" not in want and VOLUME in want


@pytest.mark.parametrize("change", [
    {"bucket": PROJECT + "-lab-x;curl"}, {"bucket": PROJECT + "-lab-$(id)"}, {"bucket": PROJECT + "-lab-x\n"},
    {"region": "eu-de`id`"}, {"region": "eu-de\n"}, {"volume": VOLUME + ";rev"}, {"volume": "$(cat key)"},
    {"files": "Windows/x.log;rev"}, {"files": "Windows/$(id).log"}, {"files": "a|b"}, {"files": "a&b"},
    {"files": "a\n"}, {"files": "a>b"}, {"bucket": 7},
])
def test_a_parameter_with_a_shell_metacharacter_is_refused(change):
    with pytest.raises(keys.Refused, match="user_data_script diskreader"):
        create({"user_data_script": script(**change)})


@pytest.mark.parametrize("name", ["other", "diskreader2", "", "../diskreader", None])
def test_a_script_name_other_than_diskreader_is_refused(name):
    with pytest.raises(keys.Refused, match="names a script of the key service: diskreader"):
        create({"user_data_script": {"name": name, "params": dict(PARAMS)}})


@pytest.mark.parametrize("spec", [
    {"name": "diskreader"}, {"name": "diskreader", "params": dict(PARAMS), "text": "echo"},
    {"name": "diskreader", "params": dict(PARAMS, device="/dev/vdb")}, {"name": "diskreader", "params": "x"},
    {"name": "diskreader", "params": {"region": "eu-de", "volume": VOLUME}}, "diskreader",
])
def test_a_script_takes_its_name_and_its_own_parameters_only(spec):
    with pytest.raises(keys.Refused, match="user_data_script"):
        create({"user_data_script": spec})


def test_the_script_puts_only_into_a_bucket_of_the_project_and_the_region_of_the_call():
    with pytest.raises(keys.Refused, match="named after the project"):
        create({"user_data_script": script(bucket="tcp-zz9z-lab-20261126-abcd")}, project=PROJECT)
    with pytest.raises(keys.Refused, match="region of the call"):
        create({"user_data_script": script(region="eu-nl")}, project=PROJECT, region="eu-de")


def test_the_script_belongs_once_in_a_server_create_without_user_data():
    for body, path in (({"server": {"user_data_script": script()}}, "/v1/{project_id}/cloudservers/action"),
                       ({"user_data_script": script(), "server": {"name": "x"}}, PATH),
                       ({"server": {"user_data_script": script(), "user_data": "QUJD"}}, PATH),
                       ({"server": {"user_data_script": script(), "user_data_text": "echo"}}, PATH)):
        with pytest.raises(keys.Refused, match="belongs once in the server of a server create"):
            keys.with_user_data(body, "POST", "ecs", path, PAIR)


def test_a_plain_caller_text_still_goes_out_unfilled():
    body, filled = create({"user_data_text": "#!/bin/sh\necho plain\n"})
    assert not filled and base64.b64decode(body["server"]["user_data"]) == b"#!/bin/sh\necho plain\n"


# --------------------------------------------------------------------------- the recipe and the engine


def test_the_recipe_names_the_script_and_the_engine_sends_its_parameters():
    r = lab.load(Path(__file__).resolve().parent.parent / "recipes" / "disk-reader.json")
    reader = next(s for s in r["steps"] if s["id"] == "reader")
    assert reader["call"]["body"]["server"]["user_data_script"] == "${script:diskreader}"
    assert "usertext" not in json.dumps(r) and "user_data_text" not in json.dumps(reader)
    env = {"name": PARAMS["bucket"], "region": "eu-de", "sys_volume": VOLUME, "files": ""}
    assert lab.expand("${script:diskreader}", env) == {"name": "diskreader", "params": dict(PARAMS, files="")}
    assert lab.expand("${script:diskreader}", {}, loose=True) == "<user_data of diskreader>"
    with pytest.raises(lab.Stop):
        lab.expand("${script:other}", env)
    with pytest.raises(lab.Stop):
        lab.expand("${script:diskreader}", dict(env, sys_volume="x;rev"))
    # the teardown deletes the newest create first: the reader, right after the restore gave the volume back
    creates = [s["id"] for s in r["steps"] if s["do"] == "create" and s["id"] in r["teardown"]]
    assert creates[-1] == "reader"


# --------------------------------------------------------------------------- F2: no lease while a reader lives


class Platform:
    """A stand-in of cloud.Client.request: the server create answers the id of the reader (502 with `lose_create`),
    a read of the reader answers it until `gone`, the lease answers a temporary key."""

    def __init__(self):
        self.gone = False
        self.lose_create = False
        self.sent: list[tuple] = []

    def __call__(self, client, method, service, path, query=None, body=None, headers=None):
        from awb.tcp.cloud import Response

        self.sent.append((method, service, path, body))
        if method == "POST" and path == PATH:
            if self.lose_create:
                return Response(502, None, "bad gateway")
            return Response(200, {"serverIds": [READER]}, "")
        if method == "GET" and path == PATH + "/detail":
            rows = [] if self.gone else [{"id": READER, "name": PROJECT + "-reader", "status": "ACTIVE"}]
            return Response(200, {"servers": rows}, "")
        if method == "GET" and path == PATH + "/" + READER:
            if self.gone:
                return Response(404, {"itemNotFound": {"message": "gone"}}, "")
            return Response(200, {"server": {"id": READER, "status": "ACTIVE",
                                             "OS-EXT-SRV-ATTR:user_data": "c2VjcmV0"}}, "")
        if service == "iam":
            return Response(201, {"credential": {"access": "TMPAKSCRIPT", "secret": "tmp-secret-script",
                                                 "securitytoken": "tmp-token-script"}}, "")
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
    s._active_project = lambda code: code == PROJECT
    return s, platform


def lease(s):
    return s._lease({"tenant": "test-1", "minutes": 15, "project": PROJECT}, os.getuid())


def make_reader(s):
    body = {"server": {"name": PROJECT + "-reader", "user_data_script": script()}}
    return s._call({"tenant": "test-1", "role": "lab", "method": "POST", "service": "ecs", "path": PATH,
                    "region": "eu-de", "body": body, "project": PROJECT}, os.getuid())


def test_a_lease_is_refused_while_a_server_with_the_key_in_its_user_data_lives(service):
    s, platform = service
    assert lease(s)["ok"]
    a = make_reader(s)
    assert a["ok"] and a["status"] == 200, a
    sent = platform.sent[-1][3]["server"]
    assert AK in base64.b64decode(sent["user_data"]).decode("utf-8")
    with pytest.raises(keys.Refused, match="still holds 1 server"):
        lease(s)
    platform.gone = True
    assert lease(s)["ok"] and s.keyed == {"test-1": []}


def test_a_lost_create_answer_still_holds_the_lease_back_by_the_name(service):
    s, platform = service
    platform.lose_create = True
    assert make_reader(s)["status"] == 502
    with pytest.raises(keys.Refused, match="still holds 1 server"):
        lease(s)
    platform.gone = True
    assert lease(s)["ok"]
