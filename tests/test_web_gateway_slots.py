"""The slow-body starvation of the gateway (n8): a request without a session holds at most PER_ADDRESS of the
UNAUTHENTICATED slots per address key and is cut when its headers or its body take longer than
UNAUTHENTICATED_TIMEOUT; a signed-in request keeps AUTHENTICATED_TIMEOUT. In the style of
tests/test_web_front_review.py: a real listener, the address key picked by the header the tunnel sets, a small
timeout patched in instead of a wait. Invented logins only."""
import hashlib
import http.client
import json
import os
import socket
import threading
import time
from urllib.parse import urlencode

import pytest

from awb.tcp.web import gateway

DOMAIN = "awb.example.test"
ROUNDS = 1000


def write_auth(path, username="awb", password="short-test-pw", salt=b"test-only-salt"):
    path.write_text(json.dumps({"username": username, "salt": salt.hex(), "rounds": ROUNDS,
                                "hash": hashlib.pbkdf2_hmac("sha256", password.encode(), salt, ROUNDS).hex()}))


@pytest.fixture
def site(tmp_path):
    """A listener whose one peer is this uid, so CF-Connecting-IP picks the address key as behind the tunnel."""
    write_auth(tmp_path / "auth.json")
    (tmp_path / "index.html").write_text("<html>x</html>")
    auth = gateway.Auth(tmp_path / "auth.json", log=tmp_path / "signin.log")
    server = gateway.make_server(0, auth, tmp_path / "index.html", 1, domain=DOMAIN, peer=os.getuid())
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        yield server, auth
    finally:
        server.shutdown()
        server.server_close()


def open_request(port, address, length=100, cookie=None, body=b""):
    """A POST /login whose headers are complete and whose body (`length` bytes announced) has `body` of it: the
    socket stays open, so the request holds whatever the gateway gave it."""
    s = socket.create_connection(("127.0.0.1", port), timeout=5)
    head = ("POST /login HTTP/1.0\r\nHost: %s\r\nOrigin: https://%s\r\nCF-Connecting-IP: %s\r\n"
            "Content-Type: application/x-www-form-urlencoded\r\nContent-Length: %d\r\n" % (DOMAIN, DOMAIN, address, length))
    if cookie:
        head += "Cookie: %s\r\n" % cookie
    s.sendall(head.encode() + b"\r\n" + body)
    return s


def answer(s):
    """The status of the answer on `s`, or None when the socket closed without one."""
    try:
        s.settimeout(5)
        data = s.recv(200)
    except OSError:
        return None
    finally:
        s.close()
    return int(data.split(b" ", 2)[1]) if data else None


def wait_for(condition, seconds=5):
    deadline = time.monotonic() + seconds
    while not condition():
        assert time.monotonic() < deadline, "the gateway did not get there in time"
        time.sleep(0.01)


def get(port, address, cookie=None):
    headers = {"Host": DOMAIN, "CF-Connecting-IP": address}
    if cookie:
        headers["Cookie"] = cookie
    c = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    c.request("GET", "/login", headers=headers)
    try:
        return c.getresponse().status
    finally:
        c.close()


def logged(auth):
    if not auth.log_path.exists():
        return []
    return [json.loads(line) for line in auth.log_path.read_text().splitlines()]


# the fifth open request of one address key is refused before its body ------------------------------------------

def test_five_open_requests_from_one_address_key_hold_four_slots(site):
    """five unauthenticated requests of one /64 send their headers and never their body: four hold a slot, the
    fifth answers 429 at once with the busy text before any body byte is read and is logged counted by the key."""
    server, auth = site
    key = gateway.address_key("2001:db8:7::1")
    held = []
    try:
        for i in range(gateway.PER_ADDRESS):
            held.append(open_request(server.server_port, "2001:db8:7::%x" % (i + 1)))
            wait_for(lambda: auth.holds.get(key, 0) == len(held))
        began = time.monotonic()
        fifth = open_request(server.server_port, "2001:db8:7::5")
        assert answer(fifth) == 429
        assert time.monotonic() - began < 2
        assert auth.holds.get(key) == gateway.PER_ADDRESS
        assert [e["result"] for e in logged(auth)] == ["slots"]
        assert all("login" not in e or e["login"] == gateway.NOT_A_LOGIN for e in logged(auth))
    finally:
        for s in held:
            s.close()
    wait_for(lambda: key not in auth.holds)
    assert auth.unauthenticated._value == gateway.UNAUTHENTICATED


def test_another_address_key_is_served_while_one_holds_its_four(site):
    """the cap is per address key: with four requests of one /64 holding, a request of another /64 and one of an
    IPv4 address are served at once."""
    server, auth = site
    key = gateway.address_key("2001:db8:7::1")
    held = []
    try:
        for i in range(gateway.PER_ADDRESS):
            held.append(open_request(server.server_port, "2001:db8:7::%x" % (i + 1)))
            wait_for(lambda: auth.holds.get(key, 0) == len(held))
        assert get(server.server_port, "2001:db8:8::1") == 200
        assert get(server.server_port, "198.51.100.7") == 200
        assert get(server.server_port, "2001:db8:7::9") == 429
    finally:
        for s in held:
            s.close()


# a body that never completes is cut at the unauthenticated timeout -----------------------------------------------

def test_a_body_that_never_completes_is_cut_at_the_timeout(site, monkeypatch):
    """an unauthenticated POST announces 100 bytes and sends 3: the read ends after UNAUTHENTICATED_TIMEOUT (made
    small here, the real value is 5 seconds) with 408, the slot is free again and the log has one counted line
    for the address key without a byte of the body."""
    monkeypatch.setattr(gateway, "UNAUTHENTICATED_TIMEOUT", 0.3)
    server, auth = site
    key = gateway.address_key("203.0.113.5")
    began = time.monotonic()
    s = open_request(server.server_port, "203.0.113.5", body=b"a=b")
    assert answer(s) == 408
    assert 0.2 < time.monotonic() - began < 3
    wait_for(lambda: key not in auth.holds)
    entries = logged(auth)
    assert [e["result"] for e in entries] == ["slow"]
    assert "a=b" not in auth.log_path.read_text()
    assert auth.unauthenticated._value == gateway.UNAUTHENTICATED


def test_headers_that_never_complete_are_cut_at_the_timeout(site, monkeypatch):
    """the request line arrives and the headers never end: the connection is closed after the timeout and the log
    has one counted line for the peer."""
    monkeypatch.setattr(gateway, "UNAUTHENTICATED_TIMEOUT", 0.3)
    server, auth = site
    s = socket.create_connection(("127.0.0.1", server.server_port), timeout=5)
    s.sendall(b"GET /login HTTP/1.0\r\nHost: " + DOMAIN.encode() + b"\r\n")
    assert answer(s) is None
    wait_for(lambda: [e["result"] for e in logged(auth)] == ["slow"])


# a signed-in request keeps the longer timeout ---------------------------------------------------------------------

def test_a_signed_in_request_keeps_the_authenticated_timeout(site, monkeypatch):
    """with the unauthenticated timeout made small, a signed-in POST whose body arrives later than that is still
    served and the handler reads it with AUTHENTICATED_TIMEOUT (20 seconds) on the connection."""
    monkeypatch.setattr(gateway, "UNAUTHENTICATED_TIMEOUT", 0.3)
    server, auth = site
    seen = []

    class Seen(gateway.Gateway):
        def route(self, session):
            seen.append((self.user is not None, self.connection.gettimeout()))
            super().route(session)

    server.RequestHandlerClass = Seen
    cookie = "__Host-awb-session=" + auth.new_session()
    assert get(server.server_port, "203.0.113.9", cookie) == 303
    assert get(server.server_port, "203.0.113.9") == 200
    assert seen == [(True, gateway.AUTHENTICATED_TIMEOUT), (False, 0.3)]
    assert gateway.AUTHENTICATED_TIMEOUT == 20
    body = urlencode({"x": "y"}).encode()
    s = socket.create_connection(("127.0.0.1", server.server_port), timeout=5)
    s.sendall(("POST /logout HTTP/1.0\r\nHost: %s\r\nOrigin: https://%s\r\nCookie: %s\r\n"
               "Content-Type: application/x-www-form-urlencoded\r\nContent-Length: %d\r\n\r\n"
               % (DOMAIN, DOMAIN, cookie, len(body))).encode())
    time.sleep(0.8)
    s.sendall(body)
    assert answer(s) == 303
    assert [e["result"] for e in logged(auth)] == []
