"""tcp-ui5o: TMS, a global service, answers a key-signed call only with X-Domain-Id and a login call only with a
domain token; a lab call that the service answers 401 reaches the session as HTTP 401 and the service's code."""
from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from awb import jobs, labclient, obs
from awb.tcp import cloud, labadmin
from tests import tcp_fake
from tests.tcp_fake import FakeGateway

AK, SK = "AKFAKE", "sk-fake-secret"
DOMAIN = "d" * 32


class TmsGateway(FakeGateway):
    """The fake gateway with the account listing of IAM and two TMS reads that need X-Domain-Id."""

    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self.domains = [{"id": DOMAIN, "name": "invented-account"}]
        self.seen: list[dict] = []

    def route(self, method, raw_path):
        path = raw_path.partition("?")[0]
        if path == "/v3/auth/domains":
            return 200, {"domains": self.domains}
        if path in ("/v1.0/predefine_tags", "/v1.0/tms/providers"):
            if self.seen[-1].get("x-domain-id") != DOMAIN:
                return 401, {"error": {"error_code": "TMS.0003", "error_msg": "Unauthorized user."}}
            return 200, {"tags": [], "total_count": 0}
        return super().route(method, raw_path)


@pytest.fixture
def gw(monkeypatch):
    monkeypatch.setattr(jobs.time, "sleep", lambda s: None)
    with TmsGateway(AK, SK) as g:
        real = tcp_fake.verify

        def verify(method, raw_path, headers, body, ak, sk):
            # the header must be one the signature covers, not only one that travels
            auth = headers.get("Authorization", "")
            signed = auth.partition("SignedHeaders=")[2].partition(",")[0].split(";")
            g.seen.append({"x-domain-id": headers.get("X-Domain-Id") if "x-domain-id" in signed else None})
            return real(method, raw_path, headers, body, ak, sk)

        monkeypatch.setattr(tcp_fake, "verify", verify)
        yield g


def client(gw) -> cloud.Client:
    return cloud.Client(obs.Keys(AK, SK), "eu-de", endpoint=gw.base)


def test_a_tms_read_carries_the_signed_account_header_read_once(gw):
    c = client(gw)
    assert c.get("tms", "/v1.0/predefine_tags").status == 200
    assert c.get("tms", "/v1.0/tms/providers").status == 200
    assert sum(1 for _, p in gw.calls if p.startswith("/v3/auth/domains")) == 1


def test_other_services_get_no_account_header(gw):
    c = client(gw)
    c.list("vpc", "/v1/{project_id}/vpcs", "vpcs")
    c.get("iam", "/v3/projects")
    assert not any(s.get("x-domain-id") for s in gw.seen)
    assert not any(p.startswith("/v3/auth/domains") for _, p in gw.calls)


def test_a_header_of_the_caller_is_kept(gw):
    r = client(gw).get("tms", "/v1.0/predefine_tags", headers={"X-Domain-Id": "e" * 32})
    assert r.status == 401 and r.data["error"]["error_code"] == "TMS.0003"
    assert not any(p.startswith("/v3/auth/domains") for _, p in gw.calls)


def test_an_unreadable_account_is_an_error_not_a_call_without_it(gw):
    gw.domains = []
    with pytest.raises(cloud.CloudError, match="0 accounts"):
        client(gw).get("tms", "/v1.0/predefine_tags")
    assert not any(p.startswith("/v1.0/") for _, p in gw.calls)


# --------------------------------------------------------------------------- lab calls (awb/tcp/labadmin.py)


class _LoginAuth:
    pair = None
    region = "eu-de"

    def __init__(self):
        self.scopes = []

    def token(self, scope):
        self.scopes.append(scope)
        return "token-of-" + scope, {"project_id": "p" * 32, "domain_id": DOMAIN}

    def credential(self):
        raise AssertionError("no OBS here")


class _KeyAuth(_LoginAuth):
    pair = {"ak": AK, "sk": SK}

    def token(self, scope):
        self.scopes.append(scope)
        return None, {"project_id": "p" * 32, "domain_id": DOMAIN}


def _capture(monkeypatch, status=200, body=b"{}"):
    sent = []

    def http(method, url, headers, data, **kw):
        sent.append((url, dict(headers)))
        return status, {}, body

    monkeypatch.setattr(labadmin, "_http", http)
    return sent


SVC = SimpleNamespace(endpoint="http://gateway.invalid")


def test_a_lab_login_call_to_tms_uses_the_domain_token(monkeypatch):
    sent = _capture(monkeypatch)
    auth = _LoginAuth()
    labadmin._send(SVC, auth, "run-1", "GET", "tms", "/v1.0/predefine_tags", {}, None, {}, None, None, None)
    assert sent[0][1]["X-Auth-Token"] == "token-of-domain"
    labadmin._send(SVC, auth, "run-1", "GET", "ecs", "/v1/x/cloudservers", {}, None, {}, None, None, None)
    assert sent[1][1]["X-Auth-Token"] == "token-of-eu-de"


def test_a_key_signed_lab_call_to_tms_names_the_account(monkeypatch):
    sent = _capture(monkeypatch)
    labadmin._send(SVC, _KeyAuth(), "run-1", "GET", "tms", "/v1.0/tms/providers", {}, None, {}, None, None, None)
    headers = sent[0][1]
    assert headers["X-Domain-Id"] == DOMAIN and "x-domain-id" in headers["Authorization"]
    labadmin._send(SVC, _KeyAuth(), "run-1", "GET", "vpc", "/v1/x/vpcs", {}, None, {}, None, None, None)
    assert "X-Domain-Id" not in sent[1][1]


def test_a_401_carries_the_service_code_and_never_its_message(monkeypatch):
    message = "Unauthorized " + "invented-secret-4711"
    _capture(monkeypatch, 401, json.dumps({"error": {"error_code": "TMS.0003", "error_msg": message}}).encode())
    with pytest.raises(labadmin.LabAdminError) as exc:
        labadmin._send(SVC, _LoginAuth(), "run-1", "GET", "tms", "/v1.0/predefine_tags", {}, None, {}, None, None,
                       None)
    assert str(exc.value).startswith("HTTP 401 TMS.0003; ")
    assert labadmin.STALE_LOGIN_LINE.partition("; ")[2] in str(exc.value) and message not in str(exc.value)


@pytest.mark.parametrize("body", [b"", b"not json", b'{"error": {"error_code": "free text here"}}', b"[1]"])
def test_a_401_without_a_plain_code_keeps_the_old_line(monkeypatch, body):
    _capture(monkeypatch, 401, body)
    with pytest.raises(labadmin.LabAdminError) as exc:
        labadmin._send(SVC, _LoginAuth(), "run-1", "GET", "tms", "/v1.0/predefine_tags", {}, None, {}, None, None,
                       None)
    assert str(exc.value) == labadmin.STALE_LOGIN_LINE


# --------------------------------------------------------------------------- what the session sees (labclient)


@pytest.mark.parametrize("error, shown", [
    ("HTTP 401 TMS.0003; the stored login may be stale: awb keys status", "HTTP 401 TMS.0003"),
    ("HTTP 401; the stored login may be stale: awb keys status", "HTTP 401"),
    ("HTTP 403 IAM.0002", "HTTP 403 IAM.0002"),
    ("HTTP 401 TMS.0003 " + "invented-secret-4711", "HTTP 401 TMS.0003"),
    ("HTTP 4011 TMS.0003", "the key service refused the request"),
    ("HTTP 401TMS.0003", "the key service refused the request"),
    ("rejected " + "invented-secret-4711", "the key service refused the request"),
    (None, "the key service refused the request"),
])
def test_the_session_sees_status_and_code_only(error, shown):
    assert labclient._service_error({"ok": False, "error": error}) == shown
