"""The tenant key in the user data of a helper ECS, on the lab_call path of a lab run (tcp-bytg ask
2026-11-25-tcp-bytg-6, part 2; part 1 is tests/test_keys_userdata_secret.py).

`{{secret:obs-ak}}` and `{{secret:obs-sk}}` are filled with the lab key of the tenant only into the script that the key
service renders itself for the `server.user_data_script` of a server create (findings F1 and F2 of the attacker review
of 2026-11-26); no answer of the run carries the user data of a server, only a note of its length, and nothing the
service writes holds the key. The platform is a stand-in `_send` that keeps what it got. Every key here is invented.
"""
from __future__ import annotations

import base64
import json

import pytest

from awb import labclient
from awb.tcp import diskreader, keys, labadmin
from tests.test_keys import calls_logged_here, no_waiting  # noqa: F401
from tests.test_lr_lab_runs import (ADMIN_STORE, LAB, P_DE, SETTINGS, TENANT_STORE, _everything, fake,  # noqa: F401
                                    lab_call, run_op, session, svc)

LAB_AK, LAB_SK = "AKLABRUNFAKE", "sk-lab-run-fake-secret"
STORE = dict(TENANT_STORE, **{"%s/lab/ak" % LAB: LAB_AK, "%s/lab/sk" % LAB: LAB_SK})
SERVERS = "/v1/{project_id}/cloudservers"
VOLUME = "%08x-aaaa-4bbb-8ccc-%012x" % (0x9f8e7d6c, 0xddddeeeeffff)
SCRIPT = {"name": "diskreader", "params": {"bucket": "tcp-ab2c-lab-20261126-abcd", "region": "eu-de",
                                             "volume": VOLUME, "files": ""}}
TEXT = "#!/bin/sh\nAK={{secret:obs-ak}}\nSK={{secret:obs-sk}}\necho done\n"
FILLED = diskreader.from_params(SCRIPT["params"]).replace("{{secret:obs-ak}}", LAB_AK).replace("{{secret:obs-sk}}",
                                                                                             LAB_SK)


class Platform:
    """What the stand-in `_send` saw and what it answers: a server create keeps its user data, a read gives it back."""

    def __init__(self):
        self.seen: list[tuple] = []
        self.user_data = ""


@pytest.fixture
def platform(monkeypatch):
    p = Platform()

    def send(svc, signer, run_id, method, service, path, query, body, headers, scope, held_token, held_cred):
        p.seen.append((method, service, path, body))
        path = path.replace("{project_id}", P_DE)
        if method == "POST" and path.endswith("/cloudservers"):
            p.user_data = (body or {}).get("server", {}).get("user_data", "")
            return 200, {}, json.dumps({"serverIds": ["a" * 32]}).encode(), path
        if method == "GET" and "/cloudservers" in path:
            row = {"id": "a" * 32, "name": "reader", "OS-EXT-SRV-ATTR:user_data": p.user_data,
                   "metadata": {"user_data": p.user_data}}
            data = {"servers": [row]} if path.endswith("/detail") else {"server": row}
            return 200, {}, json.dumps(data).encode(), path
        return 404, {}, b"{}", path

    monkeypatch.setattr(labadmin, "_send", send)
    return p


@pytest.fixture
def run(session, fake, tmp_path):
    svc, uid = session
    keys.unlock(svc.admin_path, sorted(STORE), reader=lambda e: STORE[e], settings=dict(SETTINGS),
                admin_reader=lambda e: ADMIN_STORE[e])
    started = run_op(svc, uid, "start")
    assert started["ok"], started
    return svc, uid, started["run"], tmp_path


def create(run, server=None, path=SERVERS, service="ecs"):
    svc, uid, run_id, _ = run
    given = {"name": "lab-reader"}
    if "user_data_text" not in (server or {}):
        given["user_data_script"] = SCRIPT
    body = {"server": dict(given, **(server or {}))}
    return lab_call(svc, uid, "POST", service, path, run_id, body=body)


def test_a_server_create_of_a_run_sends_base64_user_data_with_the_lab_key(run, platform):
    a = create(run)
    assert a["ok"] and a["status"] == 200, a
    sent = platform.seen[-1][3]["server"]
    assert "user_data_text" not in sent and "user_data_script" not in sent and sent["name"] == "lab-reader"
    assert base64.b64decode(sent["user_data"]).decode("utf-8") == FILLED
    text = json.dumps(a)
    assert LAB_AK not in text and LAB_SK not in text and sent["user_data"] not in text


def test_an_answer_shows_a_note_of_the_length_in_place_of_the_user_data(run, platform):
    svc, uid, run_id, _ = run
    assert create(run)["ok"]
    coded = platform.user_data
    assert keys.USER_DATA_NOTE == labclient.BOOT_SCRIPT_NOTE
    for path in (SERVERS + "/detail", SERVERS + "/" + "a" * 32):
        a = lab_call(svc, uid, "GET", "ecs", path, run_id)
        assert a["ok"] and a["status"] == 200, a
        row = a["data"]["servers"][0] if "servers" in a["data"] else a["data"]["server"]
        assert row["name"] == "reader"
        shown = [row["OS-EXT-SRV-ATTR:user_data"], row["metadata"]["user_data"]]
        for value in shown:
            assert value.strip() == labclient.BOOT_SCRIPT_NOTE % len(coded) and len(value) == len(coded)
        text = json.dumps(a)
        assert coded not in text and LAB_AK not in text and LAB_SK not in text
        # the note stays what the run log of the client shows: the same note, not a note of the note
        assert labclient._withhold(row)["OS-EXT-SRV-ATTR:user_data"] == labclient.BOOT_SCRIPT_NOTE % len(coded)


def test_the_note_is_as_long_as_the_data_or_stars_when_the_data_is_short():
    assert keys.user_data_note("x" * 100) == (keys.USER_DATA_NOTE % 100).ljust(100)
    assert keys.user_data_note("abc") == "***"
    assert keys.text_without_user_data('{"a": {"user_data": "' + "y" * 60 + '"}}', keys.user_data_note) == \
        json.dumps({"a": {"user_data": (keys.USER_DATA_NOTE % 60).ljust(60)}})
    cut = '{"servers":[{"id":"1","OS-EXT-SRV-ATTR:user_data":"' + "z" * 70
    out = keys.text_without_user_data(cut, keys.user_data_note)
    assert "z" * 5 not in out and keys.USER_DATA_NOTE % 70 in out and '"id":"1"' in out


def test_the_log_says_it_and_nothing_the_service_wrote_holds_the_key(run, platform, tmp_path, calls_logged_here):
    svc, uid, run_id, _ = run
    assert create(run)["ok"]
    assert lab_call(svc, uid, "GET", "ecs", SERVERS + "/detail", run_id)["ok"]
    log = next((tmp_path / "log").glob("cloud-*.tsv")).read_text(encoding="utf-8")
    assert "200 %s" % keys.USER_DATA_LOG in log
    written = _everything(svc, tmp_path)
    for value in (LAB_AK, LAB_SK, platform.user_data, FILLED):
        assert value not in written and value not in json.dumps(calls_logged_here)


@pytest.mark.parametrize("server", [{"name": "{{secret:obs-ak}}"}, {"admin_pass": "{{secret:obs-sk}}"},
                                    {"user_data": "{{secret:obs-sk}}"}, {"metadata": {"n": "{{secret:obs-ak}}"}}])
def test_the_key_names_are_refused_in_any_other_field_of_a_run(run, platform, server):
    svc, uid, run_id, _ = run
    a = lab_call(svc, uid, "POST", "ecs", SERVERS, run_id, body={"server": server})
    assert not a["ok"] and ("obs-" in a["error"] or "whole value" in a["error"]), a
    assert platform.seen == []


def test_the_text_is_refused_in_another_service_and_for_another_reference(run, platform):
    svc, uid, run_id, _ = run
    a = lab_call(svc, uid, "POST", "vpc", "/v1/{project_id}/vpcs", run_id,
                 body={"vpc": {"name": "x", "user_data_text": TEXT}})
    assert not a["ok"] and "user_data_text" in a["error"], a
    a = create(run, {"user_data_text": "x {{secret:ecs-admin}}"})
    assert not a["ok"] and "obs-ak" in a["error"], a
    assert platform.seen == []


def test_a_tenant_without_a_lab_key_fills_nothing(session, fake, platform):
    svc, uid = session
    started = run_op(svc, uid, "start")
    assert started["ok"], started
    body = {"server": {"name": "x", "user_data_script": SCRIPT}}
    a = lab_call(svc, uid, "POST", "ecs", SERVERS, started["run"], body=body)
    assert not a["ok"] and "key of the call" in a["error"], a
    assert platform.seen == []
