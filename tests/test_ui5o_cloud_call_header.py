"""An extra request header on `awb cloud call` (tcp-ui5o ask 1, 2026-11-23): RFS GET /v1/{project_id}/stacks answers
APIGW.0106 without Client-Request-Id and the call had no way to send one. The key service passes a plain header on
and refuses a header of the signature or the scope. Every key and id here is invented."""
from __future__ import annotations

import pytest

from awb.tcp import cloud, keys
from tests.test_keys import calls_logged_here, call, gw, load, no_waiting, svc  # noqa: F401

REQ_ID = "req-invented-0001"


def test_ui5o_a_plain_header_reaches_the_gateway_signed(svc, gw):
    load(svc)
    a = call(svc, role="read", method="GET", service="iam", path="/v3/projects",
             headers={"Client-Request-Id": REQ_ID})
    assert a["ok"] and a["status"] == 200, a
    assert gw.headers_seen[-1].get("client-request-id") == REQ_ID
    assert call(svc, role="read", method="GET", service="iam", path="/v3/projects")["status"] == 200
    assert "client-request-id" not in gw.headers_seen[-1]


@pytest.mark.parametrize("headers", [
    {"Authorization": "x"}, {"X-Auth-Token": "x"}, {"x-project-id": "x"}, {"Host": "x"}, {"X-Sdk-Date": "x"},
    {"Bad Name": "x"}, {"Client-Request-Id": "a\r\nX-Other: b"}, {"Client-Request-Id": ""},
    {"H%d" % n: "v" for n in range(keys.MAX_HEADERS + 1)}, ["Client-Request-Id"],
])
def test_ui5o_a_header_of_the_signature_or_a_broken_one_is_refused(svc, gw, headers):
    load(svc)
    sent = len(gw.calls)
    a = call(svc, role="read", method="GET", service="iam", path="/v3/projects", headers=headers)
    assert not a["ok"], a
    assert len(gw.calls) == sent


def test_ui5o_no_header_on_an_obs_call(svc, gw):
    load(svc)
    a = call(svc, role="read", method="GET", service="obs", path="/", headers={"Client-Request-Id": REQ_ID})
    assert not a["ok"] and "OBS" in a["error"], a


def test_ui5o_the_command_line_reads_k_v_and_k_colon_v():
    assert cloud._header_pairs(["Client-Request-Id=a=b", "X-Trace: t1"]) == {"Client-Request-Id": "a=b",
                                                                              "X-Trace": "t1"}
    with pytest.raises(cloud.CloudError):
        cloud._header_pairs(["Client-Request-Id"])
