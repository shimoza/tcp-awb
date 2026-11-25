"""tcp-bj3o ask 1 of 2026-11-25: the lab client reads EPS with a domain scoped token, eu-nl stays closed with a plain
line, and a temporary AK/SK with a security token is signed with X-Security-Token.

No socket of the network: the HTTP of the key service is a function here. Every login, token and key is invented.
"""
from __future__ import annotations

import json
import os
from types import SimpleNamespace

import pytest

from awb.tcp import keys, labadmin
from tests.test_keys import no_waiting, short_dir  # noqa: F401
from tests.test_lr_lab_runs import (ADMIN_STORE, DOMAIN_ID, LAB, LAB2, NOT_LAB, OTHER, P_DE, PROJECT, SETTINGS,
                                    TENANT_STORE, lab_call, run_op)

TOKEN = "MII" + "x" * 90
AK = "TMPAK000000000000001"
SK = "tmpsk" + "q" * 35
ST = "gQ" + "z" * 300
BASE = "http://platform.invalid"


class Wire:
    """The calls the key service made: (method, url, headers), and the answers of a stand-in platform."""

    def __init__(self):
        self.calls: list[tuple[str, str, dict]] = []
        self.logins = 0

    def __call__(self, method, url, headers, data, timeout=60.0):
        self.calls.append((method, url, dict(headers)))
        if url.endswith("/v3/auth/tokens?nocatalog=true"):
            self.logins += 1
            scope = json.loads(data)["auth"]["scope"]
            body = ({"expires_at": "2099-01-01T00:00:00.000000Z", "domain": {"id": DOMAIN_ID, "name": "d"}}
                    if "domain" in scope else
                    {"expires_at": "2099-01-01T00:00:00.000000Z",
                     "project": {"id": P_DE, "name": "eu-de", "domain": {"id": DOMAIN_ID}}})
            return 201, {"X-Subject-Token": TOKEN + str(self.logins)}, json.dumps({"token": body}).encode()
        if url.endswith("/v3.0/OS-CREDENTIAL/securitytokens"):
            return 201, {}, json.dumps({"credential": {"access": AK, "secret": SK, "securitytoken": ST,
                                                       "expires_at": "2099-01-01T00:00:00.000000Z"}}).encode()
        return 200, {}, b"{}"

    def api(self):
        return [c for c in self.calls if "/v3/auth/tokens" not in c[1] and "securitytokens" not in c[1]]

    def logged_in(self):
        return [c for c in self.calls if "/v3/auth/tokens" in c[1]]


@pytest.fixture
def wire(monkeypatch):
    w = Wire()
    monkeypatch.setattr(labadmin, "_http", w)
    return w


@pytest.fixture
def session(wire, tmp_path, monkeypatch):
    with short_dir() as d:
        s = keys.Service(d / "admin.sock", d / "call.sock", endpoint=BASE, log_dir=tmp_path / "log",
                         paths_fn=lambda: SimpleNamespace(vault=tmp_path / "vault"))
        s._registered = lambda: {LAB: ("eu-de",), LAB2: ("eu-de",), NOT_LAB: ("eu-de",)}
        s._active_project = lambda code: code in (PROJECT, OTHER)
        rows = {c: SimpleNamespace(code=c, kind="project", state="active", customer=None) for c in (PROJECT, OTHER)}
        s._project_row = lambda code: rows.get(code)
        s._granted = lambda code: code in rows
        s._console_uid = lambda: None
        s.start()
        monkeypatch.setenv(keys.CALL_SOCKET_ENV, str(s.call_path))
        monkeypatch.setenv(keys.ADMIN_SOCKET_ENV, str(s.admin_path))
        monkeypatch.setattr(labadmin, "END_PAUSE", 0.0)
        try:
            keys.unlock(s.admin_path, sorted(TENANT_STORE), reader=lambda e: TENANT_STORE[e],
                        settings=dict(SETTINGS), admin_reader=lambda e: ADMIN_STORE[e])
            yield s, os.getuid() + 1
        finally:
            s.stop()


def test_domain_scope_reads_eps_and_nothing_else(session, wire):
    svc, uid = session
    run = run_op(svc, uid, "start")["run"]
    wire.calls.clear()
    ok = lab_call(svc, uid, "GET", "eps", "/v1.0/enterprise-projects", run, scope="domain")
    assert ok["ok"] and ok["status"] == 200
    (method, url, headers), = wire.api()
    assert (method, url) == ("GET", BASE + "/v1.0/enterprise-projects")
    assert headers["X-Auth-Token"].startswith(TOKEN)
    assert len(wire.logged_in()) == 1
    # the domain token is not kept: a second call makes a new one
    lab_call(svc, uid, "GET", "eps", "/v1.0/enterprise-projects", run, scope="domain")
    assert len(wire.logged_in()) == 2
    wire.calls.clear()
    allowed = " and ".join(labadmin.DOMAIN_READ_SERVICES)
    for method, service in (("POST", "eps"), ("DELETE", "eps"), ("GET", "iam"), ("GET", "vpc")):
        bad = lab_call(svc, uid, method, service, "/v1.0/enterprise-projects", run, scope="domain",
                       body={} if method == "POST" else None)
        assert not bad["ok"] and bad["error"] == labadmin.DOMAIN_SCOPE_LINE % allowed, (method, service)
        assert "eps" in bad["error"]
    assert wire.api() == []


def test_eu_nl_is_closed_with_the_owners_rule(session, wire):
    svc, uid = session
    run = run_op(svc, uid, "start")["run"]
    wire.calls.clear()
    answer = lab_call(svc, uid, "GET", "vpc", "/v1/{project_id}/vpcs", run, region="eu-nl")
    assert not answer["ok"]
    assert "eu-nl is closed on the test tenants by the owner's rule" in answer["error"]
    assert wire.api() == []


def test_a_temporary_credential_is_signed_with_the_security_token(session, wire):
    svc, uid = session
    run = run_op(svc, uid, "start")["run"]
    cred = lab_call(svc, uid, "POST", "iam", "/v3.0/OS-CREDENTIAL/securitytokens", run, body={"auth": {}})
    access = cred["data"]["credential"]["access"]
    wire.calls.clear()
    got = lab_call(svc, uid, "GET", "vpc", "/v1/{project_id}/vpcs", run, credential=access)
    assert got["ok"] and got["status"] == 200
    (method, url, headers), = wire.api()
    assert headers["X-Security-Token"] == ST
    signed = headers["Authorization"].partition("SignedHeaders=")[2].partition(",")[0]
    assert "x-security-token" in signed.split(";")
    assert headers["Authorization"].startswith("SDK-HMAC-SHA256 Access=%s," % AK)


def test_a_key_with_a_security_token_is_signed_with_it_on_every_service(monkeypatch):
    from awb import obs
    from awb.tcp import cloud, throttle
    from tests.test_cloud import _Wire

    sent = _Wire()
    monkeypatch.setattr(cloud.urllib.request, "urlopen", sent)
    monkeypatch.setattr(throttle, "log_call", lambda *a, **k: None)
    monkeypatch.setattr(throttle, "wait_turn", lambda host: 0.0)
    c = cloud.Client(obs.Keys(AK, SK, ST))
    assert c.request("GET", "vpc", "/v1/x/vpcs").status == 200
    req = sent.sent[-1]
    assert req.get_header("X-security-token") == ST
    auth = req.get_header("Authorization")
    assert auth.startswith("SDK-HMAC-SHA256 Access=%s," % AK)
    assert "x-security-token" in auth.partition("SignedHeaders=")[2].partition(",")[0].split(";")
    c.request("GET", "obs", "/tcp-ab2c-lab-1")
    assert sent.sent[-1].get_header("X-obs-security-token") == ST
    cloud.Client(obs.Keys(AK, SK)).request("GET", "vpc", "/v1/x/vpcs")
    assert sent.sent[-1].get_header("X-security-token") is None
    assert ST not in c.clean("answer with %s in it" % ST)
