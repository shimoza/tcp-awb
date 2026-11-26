"""B-093: the caller fences of the key service, findings F3, F4 and F8 of the attacker review of 2026-11-26.

F3: the material ops decide from the caller of the folder token, never from the project field a session writes.
F4: the folder token of an ask project runs the ask refusals of op call.
F8: the rate counts per caller project, so one looping session does not starve the others.
"""
from __future__ import annotations

import pytest

from awb import askkind, projects
from awb.tcp import keys, keys_exchange
from tests.test_ask_kind import granted, service  # noqa: F401 - the fixtures

SESSION_UID = 4242
MATERIAL_OPS = ("take_lab", "take_owner", "inbox_find", "obs")


def _token(folder) -> str:
    return projects.read_key_token(folder)


def _socket_call(service, req: dict) -> dict:
    """One request as `_serve` hands it on: the caller comes from the folder token, never from the request."""
    return service._call_socket(req, SESSION_UID, caller=service._caller_project(req, SESSION_UID))


# --------------------------------------------------------------------------- F3: material by the caller


def test_an_ask_token_taking_material_for_another_project_gets_the_ask_material_line(home, service):
    ask = granted(home)
    other = granted(home, "query")
    for op in MATERIAL_OPS:
        req = {"op": op, "project": other.name, "id": "lab-q7m4q7m4q7m4", "method": "GET",
               "key": "%s/in/notes.md" % other.name, "token": _token(ask)}
        answer = _socket_call(service, req)
        assert answer == {"ok": False, "error": askkind.MATERIAL_LINE % ask.name}, op


def test_an_ask_token_naming_no_project_gets_the_ask_material_line(home, service):
    ask = granted(home)
    answer = _socket_call(service, {"op": "inbox_find", "all": True, "token": _token(ask)})
    assert answer == {"ok": False, "error": askkind.MATERIAL_LINE % ask.name}


def test_an_exchange_op_of_a_caller_that_names_another_project_is_refused(home):
    caller, other = "tcp-q7m4", "tcp-z2p9"
    calls = (lambda req: keys_exchange.serve_obs(None, dict(req, method="LIST", prefix="%s/in/" % other), None,
                                                 caller=caller),
             lambda req: keys_exchange.serve_take_owner(None, dict(req, id="own-q7m4q7m4q7m4"), caller=caller),
             lambda req: keys_exchange.serve_take_lab(None, dict(req, id="lab-q7m4q7m4q7m4"), caller=caller),
             lambda req: keys_exchange.serve_inbox_find(None, dict(req, all=True), caller=caller))
    for call in calls:
        for req in ({"project": other}, {}):
            with pytest.raises(keys_exchange.XchgError) as err:
                call(req)
            assert str(err.value) == keys_exchange.CALLER_LINE % caller
            assert other not in str(err.value)


def test_the_fence_leaves_a_call_without_a_caller_and_the_own_code_alone():
    keys_exchange.caller_fence({"project": "tcp-z2p9"}, None)
    keys_exchange.caller_fence({"project": "tcp-q7m4"}, "tcp-q7m4")


def test_a_put_of_an_ask_token_for_another_project_is_refused_before_its_bytes(home, service):
    ask = granted(home)
    other = granted(home, "query")
    req = {"op": "obs", "method": "PUT", "project": other.name, "name": "result.md", "length": 10,
           "token": _token(ask)}
    refusal = service._put_refusal(req, SESSION_UID, service._caller_project(req, SESSION_UID))
    assert refusal == keys_exchange.CALLER_LINE % ask.name


# --------------------------------------------------------------------------- F4: the ask refusals of op call


def test_an_ask_project_with_a_read_only_tenant_granted_gets_the_tenant_refusal(home, service, monkeypatch):
    ask = granted(home)
    monkeypatch.setattr(service, "_readonly_granted", lambda alias, code: True)
    # a path no check passes: on any code no request leaves the host
    req = {"op": "call", "tenant": "cust-1", "role": "read", "method": "GET", "service": "ecs", "path": "no path",
           "region": "eu-de", "project": ask.name, "token": _token(ask)}
    answer = _socket_call(service, req)
    assert answer == {"ok": False, "error": askkind.TENANT_LINE % ask.name}


def test_an_ask_token_naming_another_code_on_a_test_tenant_gets_the_tenant_refusal(home, service):
    ask = granted(home)
    req = {"op": "call", "tenant": "test-1", "role": "read", "method": "GET", "service": "ecs",
           "path": "/v1/x/cloudservers", "region": "eu-de", "project": "tcp-z2p9", "token": _token(ask)}
    assert _socket_call(service, req) == {"ok": False, "error": askkind.TENANT_LINE % ask.name}


def test_an_ask_token_on_a_test_tenant_passes_the_ask_check(home, service):
    ask = granted(home)
    for role, method in (("read", "GET"), ("lab", "POST")):
        # past the ask check; stopped at the next one (a region the tenant lacks), so no request leaves the host
        req = {"op": "call", "tenant": "test-1", "role": role, "method": method, "service": "ecs",
               "path": "/v1/x/cloudservers", "region": "eu-nl", "token": _token(ask)}
        with pytest.raises(keys.Refused, match="region is not one of the tenant"):
            service._call(req, SESSION_UID, caller=service._caller_project(req, SESSION_UID))


# --------------------------------------------------------------------------- F8: the rate per caller


def test_one_looping_caller_does_not_make_another_caller_answer_rate(home, service):
    first, second = granted(home), granted(home)
    for _ in range(keys.RATE_LIMIT):
        assert _socket_call(service, {"op": "ping", "token": _token(first)})["ok"] is True
    assert _socket_call(service, {"op": "ping", "token": _token(first)}) == {"ok": False, "error": "rate"}
    assert _socket_call(service, {"op": "ping", "token": _token(second)})["ok"] is True
    assert _socket_call(service, {"op": "ping"})["ok"] is True


def test_calls_without_a_token_keep_a_ceiling_per_uid(home, service):
    caller = granted(home)
    assert keys.RATE_LIMIT == 240
    for _ in range(keys.RATE_LIMIT):
        assert _socket_call(service, {"op": "ping"})["ok"] is True
    assert _socket_call(service, {"op": "ping"}) == {"ok": False, "error": "rate"}
    assert _socket_call(service, {"op": "ping", "token": "0" * 64}) == {"ok": False, "error": "rate"}
    assert service._call_socket({"op": "ping"}, SESSION_UID + 1, caller=None)["ok"] is True
    assert _socket_call(service, {"op": "ping", "token": _token(caller)})["ok"] is True
