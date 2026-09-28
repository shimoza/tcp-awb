"""awb/sign.py: SDK-HMAC-SHA256 against a verifier written from the algorithm (tests/tcp_fake.py)."""
from __future__ import annotations

import datetime
import urllib.error
import urllib.request

import pytest

from awb import sign
from tests.tcp_fake import FakeGateway, verify

AK, SK = "AKFAKE", "sk-fake-secret"
NOW = datetime.datetime(2026, 9, 24, 12, 0, 0, tzinfo=datetime.timezone.utc)


def test_the_canonical_uri_ends_in_a_slash_and_encodes_every_segment_once():
    assert sign.canonical_uri("/v3/projects") == "/v3/projects/"
    assert sign.canonical_uri("") == "/"
    assert sign.canonical_uri("/a b/c%2Fd/") == "/a%20b/c%2Fd/"
    assert sign.canonical_uri("/v1/x~y_z.-") == "/v1/x~y_z.-/"


def test_the_canonical_query_is_sorted_by_key_and_value():
    assert sign.canonical_query("b=2&a=1&a=0&c=") == "a=0&a=1&b=2&c="
    assert sign.canonical_query("name=eu de&x=%2F") == "name=eu%20de&x=%2F"
    assert sign.canonical_query("") == ""


def test_sign_sets_host_date_and_authorization():
    h = sign.sign("GET", "https://iam.eu-de.example/v3/projects?name=eu-de", {"Content-Type": "application/json"},
                  b"", AK, SK, now=NOW)
    assert h["Host"] == "iam.eu-de.example" and h["X-Sdk-Date"] == "20260924T120000Z"
    assert h["Authorization"].startswith("SDK-HMAC-SHA256 Access=AKFAKE, SignedHeaders=content-type;host;x-sdk-date, "
                                         "Signature=")
    assert len(h["Authorization"].rsplit("=", 1)[1]) == 64


def test_the_signature_is_pinned():
    """Pinned after the live proof against the identity service, so that a change of the algorithm shows."""
    h = sign.sign("GET", "https://iam.eu-de.example/v3/projects?name=eu-de", {"Content-Type": "application/json"},
                  b"", AK, SK, now=NOW)
    assert h["Authorization"].rsplit("=", 1)[1] == PINNED


PINNED = "60300c77ae39f6bd1637a7648cd31472b547b77b337f6e9804481954c1e0ad91"


def _send(gw: FakeGateway, headers: dict, path: str = "/v3/projects?name=eu-de") -> int:
    req = urllib.request.Request(gw.base + path, method="GET")
    for k, v in headers.items():
        req.add_header(k, v)
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status
    except urllib.error.HTTPError as exc:
        return exc.code


def test_the_gateway_accepts_the_signature_and_refuses_a_wrong_one():
    with FakeGateway(AK, SK) as gw:
        url = gw.base + "/v3/projects?name=eu-de"
        good = sign.sign("GET", url, {"Content-Type": "application/json"}, b"", AK, SK)
        assert _send(gw, good) == 200
        other_secret = sign.sign("GET", url, {"Content-Type": "application/json"}, b"", AK, "another-secret")
        assert _send(gw, other_secret) == 401
        other_path = sign.sign("GET", gw.base + "/v3/projects?name=eu-nl", {"Content-Type": "application/json"}, b"",
                               AK, SK)
        assert _send(gw, other_path) == 401                 # signed for another query, sent for this one
        tampered = dict(good, **{"Content-Type": "text/plain"})
        assert _send(gw, tampered) == 401


def test_the_verifier_can_fail():
    """The stand-in proves nothing unless it refuses: a request without a signature and one with a wrong key."""
    class H(dict):
        def get(self, k, d=None):
            return super().get(k, super().get(k.lower(), d))

    signed = sign.sign("GET", "http://127.0.0.1:1/v3/projects", {"Content-Type": "application/json"}, b"", AK, SK)
    lower = H({k.lower(): v for k, v in signed.items()}, **signed)
    assert verify("GET", "/v3/projects", lower, b"", AK, SK)
    assert not verify("GET", "/v3/projects", lower, b"x", AK, SK)
    assert not verify("GET", "/v3/projects", lower, b"", "AKOTHER", SK)
    assert not verify("GET", "/v3/projects", H(), b"", AK, SK)
