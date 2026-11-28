"""G5 of the attacker review of 2026-11-28: the ask material fence falls when the session sends no token.

B-093 bound the exchange ops to the folder token when one was presented. The token is a field the session writes:
without it the caller was nobody, the fence did nothing and the op ran for the project the request named. Now an
exchange op of a session is the project of its token or it is refused. The service knows the tokens of the grants;
for a project without a grant (a query project) it reads the token of the project's folder, written at spawn.
"""
from __future__ import annotations

import pytest

from awb import askkind, projects
from awb.tcp import keys, keys_exchange
from tests.test_ask_kind import granted, service, spawn  # noqa: F401 - the fixtures

SESSION_UID = 4242
OPS = ("take_owner", "take_lab", "inbox_find", "obs")


def _token(folder) -> str:
    return projects.read_key_token(folder)


def _socket_call(service, req: dict):  # noqa: F811
    """One request as `_serve` hands it on: the caller comes from the folder token, never from the request."""
    return service._call_socket(req, SESSION_UID, caller=service._caller_project(req, SESSION_UID))


@pytest.fixture
def reached(service, monkeypatch):  # noqa: F811
    """What reaches the exchange: (op, caller) per call, answered without a bucket."""
    seen: list = []

    def exchange(op, req, uid, upload, caller=None):
        seen.append((op, caller))
        return {"ok": True}

    monkeypatch.setattr(service, "_exchange", exchange)
    return seen


def test_an_exchange_op_without_a_token_is_refused_for_the_named_project(home, service, reached):  # noqa: F811
    """The test of the finding: a session of an ask project names another active project and sends no token."""
    granted(home)
    other = granted(home, "project")
    for op in OPS:
        req = {"op": op, "project": other.name, "id": "own-q7m4q7m4q7m4", "method": "GET",
               "key": "%s/in/notes.md" % other.name}
        for sent in (req, dict(req, token="0" * 64), dict(req, token=7), dict(req, token="short")):
            answer = service._call_socket(dict(sent), SESSION_UID, caller=None)
            assert answer["ok"] is False and "key token" in answer["error"], (op, answer)
            assert _socket_call(service, dict(sent))["ok"] is False
    assert reached == []


def test_a_query_project_without_a_token_is_refused_too(home, service, reached):  # noqa: F811
    other = spawn(home, "query")
    for op in OPS:
        answer = _socket_call(service, {"op": op, "project": other.name, "id": "own-q7m4q7m4q7m4", "method": "LIST"})
        assert answer == {"ok": False, "error": keys.TOKEN_LINE}, op
    assert reached == []


def test_the_folder_token_of_a_query_project_names_it_as_the_caller(home, service, reached):  # noqa: F811
    """The honest call of a project without a grant: its folder token, which only its folder holds."""
    query = spawn(home, "query")
    assert service._caller_project({"token": _token(query)}, SESSION_UID) is None, "no grant knows the token"
    for op in OPS:
        answer = _socket_call(service, {"op": op, "project": query.name, "id": "own-q7m4q7m4q7m4",
                                        "method": "LIST", "token": _token(query)})
        assert answer == {"ok": True}, op
    assert reached == [(op, query.name) for op in OPS]


def test_the_folder_token_of_one_project_never_takes_for_another(home, service):  # noqa: F811
    query, other = spawn(home, "query"), spawn(home, "query")
    service.tenants = {"test-1": {"roles": {"lab": {"ak": "c", "sk": "d"}}}}
    for op in ("take_owner", "take_lab", "inbox_find"):
        answer = _socket_call(service, {"op": op, "project": other.name, "id": "own-q7m4q7m4q7m4", "all": True,
                                        "token": _token(query)})
        assert answer["ok"] is False and answer["error"] == keys_exchange.CALLER_LINE % query.name, (op, answer)


def test_a_put_without_a_token_is_refused_before_its_bytes_are_read(home, service):  # noqa: F811
    query = spawn(home, "query")
    req = {"op": "obs", "method": "PUT", "project": query.name, "name": "x.md", "length": 3}
    assert service._put_refusal(dict(req), SESSION_UID, service._caller_project(req, SESSION_UID)) == keys.TOKEN_LINE
    assert _socket_call(service, dict(req)) == {"ok": False, "error": keys.TOKEN_LINE}


def test_an_ask_project_keeps_its_own_line_with_and_without_its_token(home, service, reached):  # noqa: F811
    ask = granted(home)
    for req in ({"op": "take_owner", "project": ask.name, "id": "own-q7m4q7m4q7m4"},
                {"op": "take_owner", "project": ask.name, "id": "own-q7m4q7m4q7m4", "token": _token(ask)}):
        assert _socket_call(service, req) == {"ok": False, "error": askkind.MATERIAL_LINE % ask.name}
    assert reached == []


def test_a_granted_project_is_named_by_the_token_of_its_grant_alone(home, service):  # noqa: F811
    """The folder look-up is for projects without a grant: a token put into the folder of a granted project after
    the grant names nobody, and a token two folders hold names nobody."""
    project = granted(home, "project")
    assert service._folder_caller({"token": _token(project)}) is None
    assert service._caller_project({"token": _token(project)}, SESSION_UID) == project.name
    first, second = spawn(home, "query"), spawn(home, "query")
    (second / projects.BIND_FILE).write_text(_token(first) + "\n", encoding="ascii")
    assert service._folder_caller({"token": _token(first)}) is None
    for odd in (None, 7, "0" * 64, "x" * 64, ""):
        assert service._folder_caller({"token": odd}) is None


def test_the_owner_and_the_console_are_not_asked_for_a_token(home, service, reached, monkeypatch):  # noqa: F811
    query = spawn(home, "query")
    monkeypatch.setattr(service, "_session_read", lambda uid: False)
    req = {"op": "take_owner", "project": query.name, "id": "own-q7m4q7m4q7m4"}
    assert service._call_socket(req, SESSION_UID, caller=service._caller_project(req, SESSION_UID)) == {"ok": True}
    assert reached == [("take_owner", keys._NOT_ASKED)]
