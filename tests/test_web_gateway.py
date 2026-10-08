"""The gateway (awb/tcp/web/gateway.py): sign-in, sessions, the checks of every request and what reaches a backend.

Ported from the tests the web side wrote for the deployed gateway. The host name of the site is a setting now, so
these tests run with an invented one and also show that the setting is required and enforced.
"""
import base64
import hashlib
import http.client
import json
import re
import sys
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch
from urllib.parse import urlencode

import pytest

from awb.tcp.web import gateway
from awb.tcp.web.create_api import Server

DOMAIN = "awb.example.test"
PASSWORD = "short-test-pw"


def write_auth(path, username, password, salt=b"test-only-salt"):
    path.write_text(json.dumps({"username": username, "salt": salt.hex(), "rounds": 1000,
                                "hash": hashlib.pbkdf2_hmac("sha256", password.encode(), salt, 1000).hex()}))


class Backend(BaseHTTPRequestHandler):
    calls = []

    def do_GET(self):
        self.answer()

    def do_POST(self):
        self.answer()

    def answer(self):
        data = self.rfile.read(int(self.headers.get("Content-Length", 0)))
        self.calls.append((self.command, self.path, dict(self.headers), data))
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"backend response")

    def log_message(self, *args):
        pass


class LoginTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory()
        root = Path(cls.temp.name)
        write_auth(root / "auth.json", "awb", PASSWORD)
        (root / "index.html").write_text("<html><script>window.preview=true</script>sample dashboard</html>")
        cls.root = root
        cls.auth = gateway.Auth(root / "auth.json")
        cls.backend = ThreadingHTTPServer(("127.0.0.1", 0), Backend)
        port = cls.backend.server_port
        cls.servers = [gateway.make_server(0, cls.auth, root / "index.html", port, port, domain=DOMAIN)
                       for _ in range(2)]
        for s in [cls.backend, *cls.servers]:
            threading.Thread(target=s.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        for s in [cls.backend, *cls.servers]:
            s.shutdown()
            s.server_close()
        cls.temp.cleanup()

    def request(self, path="/", method="GET", headers=None, body=None, port=0, server=None):
        h = {"Host": DOMAIN}
        h.update(headers or {})
        target = server or self.servers[port]
        c = http.client.HTTPConnection("127.0.0.1", target.server_port, timeout=5)
        c.request(method, path, body=body, headers=h)
        r = c.getresponse()
        out = (r.status, dict(r.getheaders()), r.read())
        c.close()
        return out

    def session_headers(self):
        return {"Cookie": "__Host-awb-session=" + self.auth.new_session()}

    def form(self, **values):
        status, h, b = self.request("/login")
        self.assertEqual(status, 200)
        self.assertEqual(h["Referrer-Policy"], "same-origin")
        csrf = re.search(rb'name="csrf" value="([^"]+)"', b).group(1).decode()
        headers = {"Cookie": h["Set-Cookie"].split(";")[0], "Origin": "https://" + DOMAIN,
                   "Content-Type": "application/x-www-form-urlencoded"}
        data = {"csrf": csrf, "username": "awb", "password": PASSWORD, "next": "/"}
        data.update(values)
        return headers, data

    def test_anonymous_get_redirects_without_browser_auth_challenge(self):
        before = len(Backend.calls)
        for port in [0, 1]:
            for path in ["/", "/ask", "/kb", "/health", "/portal"]:
                status, h, b = self.request(path, port=port)
                self.assertEqual(status, 303)
                self.assertTrue(h["Location"].startswith("/login?"))
                self.assertNotIn("WWW-Authenticate", h)
        self.assertEqual(len(Backend.calls), before)

    def test_favicon_does_not_replace_login_challenge(self):
        status, h, b = self.request("/favicon.ico")
        self.assertEqual(status, 204)
        self.assertNotIn("Set-Cookie", h)
        self.assertNotIn("Location", h)

    def test_project_api_requires_session_and_limits_writes(self):
        before = len(Backend.calls)
        for path in ["/api/projects", "/api/projects/tcp-q7m4", "/api/tenants", "/api/tenants?refresh=1"]:
            status, h, b = self.request(path)
            self.assertEqual(status, 401)
            self.assertIn("application/json", h["Content-Type"])
            self.assertNotIn("WWW-Authenticate", h)
            self.assertEqual(json.loads(b), {"error": "Sign in to open the project data."})
            expected = 403 if path == "/api/projects" else 405
            self.assertEqual(self.request(path, "POST", self.session_headers(), body="")[0], expected)
        self.assertEqual(len(Backend.calls), before)

    def test_anonymous_chat_cannot_spend_tokens(self):
        before = len(Backend.calls)
        for port in [0, 1]:
            self.assertEqual(self.request("/ask", "POST", body="q=test", port=port)[0], 403)
        self.assertEqual(len(Backend.calls), before)

    def test_project_chat_auth_origin_and_json(self):
        path = "/api/projects/tcp-q7m4/chat"
        before = len(Backend.calls)
        for port in [0, 1]:
            self.assertEqual(self.request(path, "POST", body="{}", port=port)[0], 401)
            self.assertEqual(self.request(path, port=port)[0], 401)
        headers = self.session_headers()
        headers["Content-Type"] = "application/json"
        self.assertEqual(self.request(path, "POST", headers, "{}")[0], 403)
        headers["Origin"] = "https://evil.invalid"
        self.assertEqual(self.request(path, "POST", headers, "{}")[0], 403)
        headers["Origin"] = "https://" + DOMAIN
        headers["Content-Type"] = "application/x-www-form-urlencoded"
        self.assertEqual(self.request(path, "POST", headers, "{}")[0], 415)
        headers["Content-Type"] = "application/json"
        headers["Sec-Fetch-Site"] = "cross-site"
        self.assertEqual(self.request(path, "POST", headers, "{}")[0], 403)
        self.assertEqual(len(Backend.calls), before)
        headers["Sec-Fetch-Site"] = "same-origin"
        self.assertEqual(self.request(path, "POST", headers, "{}")[0], 200)
        method, route, forwarded, body = Backend.calls[-1]
        self.assertEqual(route, path)
        self.assertEqual(body, b"{}")
        self.assertEqual(forwarded["Content-Type"], "application/json")
        self.assertEqual(forwarded["Host"], DOMAIN)
        self.assertEqual(forwarded["Origin"], "https://" + DOMAIN)
        self.assertNotIn("Cookie", forwarded)
        self.assertNotIn("Authorization", forwarded)
        self.assertEqual(self.request(path, "DELETE", headers)[0], 405)

    def test_login_secure_cookie_session_and_safe_redirect(self):
        h, data = self.form(next="https://evil.invalid")
        status, out, _ = self.request("/login", "POST", h, urlencode(data))
        self.assertEqual(status, 303)
        self.assertEqual(out["Location"], "/")
        cookie = out["Set-Cookie"]
        for part in ("Secure", "HttpOnly", "SameSite=Lax"):
            self.assertIn(part, cookie)
        self.assertNotIn("Domain=", cookie)
        status, h, b = self.request(headers={"Cookie": cookie.split(";")[0]})
        self.assertEqual(status, 200)
        self.assertIn(b"sample dashboard", b)

    def test_wrong_password_and_legacy_basic_do_not_authenticate(self):
        h, data = self.form(password="wrong")
        status, out, b = self.request("/login", "POST", h, urlencode(data))
        self.assertEqual(status, 200)
        self.assertIn(b"incorrect", b)
        self.assertNotIn("WWW-Authenticate", out)
        self.assertNotIn("__Host-awb-session=", out.get("Set-Cookie", ""))
        basic = "Basic " + base64.b64encode(("awb:" + PASSWORD).encode()).decode()
        self.assertEqual(self.request(headers={"Authorization": basic})[0], 303)

    def test_login_rejects_csrf_and_cross_origin(self):
        h, data = self.form()
        data["csrf"] = "forged"
        self.assertEqual(self.request("/login", "POST", h, urlencode(data))[0], 403)
        h, data = self.form()
        h["Origin"] = "https://evil.invalid"
        self.assertEqual(self.request("/login", "POST", h, urlencode(data))[0], 403)
        h, data = self.form()
        data["csrf"] = "ю"
        self.assertEqual(self.request("/login", "POST", h, urlencode(data))[0], 403)

    def test_session_tampering_expiry_and_restart(self):
        token = self.auth.new_session()
        self.assertFalse(self.auth.session_valid(token + "a"))
        key = hashlib.sha256(token.encode()).digest()
        self.auth.sessions[key] = time.monotonic() - 1
        self.assertFalse(self.auth.session_valid(token))
        self.assertEqual(self.request(headers={"Cookie": "__Host-awb-session=forged"})[0], 303)

    def test_csrf_challenge_expires(self):
        with patch("awb.tcp.web.gateway.time.time", return_value=1000):
            token = self.auth.csrf_token()
        with patch("awb.tcp.web.gateway.time.time", return_value=1601):
            self.assertFalse(self.auth.valid_csrf(token))

    def test_logout_revokes_session(self):
        h = self.session_headers()
        old = dict(h)
        h.update({"Origin": "https://" + DOMAIN, "Content-Type": "application/x-www-form-urlencoded"})
        status, out, _ = self.request("/logout", "POST", h, "")
        self.assertEqual(status, 303)
        self.assertIn("Max-Age=0", out["Set-Cookie"])
        self.assertEqual(self.request(headers=old)[0], 303)

    def test_authenticated_chat_requires_exact_origin_and_no_credential_forwarding(self):
        before = len(Backend.calls)
        h = self.session_headers()
        h["Content-Type"] = "application/x-www-form-urlencoded"
        for origin in ["", "http://" + DOMAIN, "https://evil.invalid"]:
            h["Origin"] = origin
            self.assertEqual(self.request("/ask", "POST", h, "q=test")[0], 403)
        self.assertEqual(len(Backend.calls), before)
        h["Origin"] = "https://" + DOMAIN
        self.assertEqual(self.request("/ask", "POST", h, "q=test")[0], 200)
        _, _, forwarded, _ = Backend.calls[-1]
        self.assertNotIn("Cookie", forwarded)
        self.assertNotIn("Authorization", forwarded)

    def test_bruteforce_limit(self):
        for _ in range(8):
            h, d = self.form(password="wrong")
            h["CF-Connecting-IP"] = "failed-logins"
            self.assertEqual(self.request("/login", "POST", h, urlencode(d))[0], 200)
        h, d = self.form(password="wrong")
        h["CF-Connecting-IP"] = "failed-logins"
        self.assertEqual(self.request("/login", "POST", h, urlencode(d))[0], 429)

    def test_host_method_and_body_size_limits(self):
        self.assertEqual(self.request(headers={"Host": "evil.invalid"})[0], 400)
        self.assertEqual(self.request("/projects", "DELETE", self.session_headers())[0], 405)
        h, d = self.form()
        self.assertEqual(self.request("/login", "POST", h, "x" * 20000)[0], 413)

    def test_projects_and_tenants_reach_their_own_backends(self):
        class Marked(Backend):
            def answer(self):
                self.calls.append((self.command, self.path, dict(self.headers), b""))
                self.send_response(200)
                self.end_headers()
                self.wfile.write(self.server.mark)
        backends = []
        for mark in (b"projects", b"tenants"):
            b = ThreadingHTTPServer(("127.0.0.1", 0), Marked)
            b.mark = mark
            threading.Thread(target=b.serve_forever, daemon=True).start()
            backends.append(b)
        s = gateway.make_server(0, self.auth, self.root / "index.html", 1, 1, backends[0].server_port,
                                backends[1].server_port, domain=DOMAIN)
        threading.Thread(target=s.serve_forever, daemon=True).start()
        try:
            self.assertEqual(self.request("/api/projects", headers=self.session_headers(), server=s)[2], b"projects")
            self.assertEqual(self.request("/api/tenants", headers=self.session_headers(), server=s)[2], b"tenants")
            self.assertEqual(self.request("/api/tenants?refresh=1", headers=self.session_headers(), server=s)[2],
                             b"tenants")
        finally:
            for x in (s, *backends):
                x.shutdown()
                x.server_close()

    def test_the_host_of_the_site_is_a_setting(self):
        other = gateway.make_server(0, self.auth, self.root / "index.html", 1, 1, domain="Other.Example.Test")
        threading.Thread(target=other.serve_forever, daemon=True).start()
        try:
            self.assertEqual(self.request(server=other)[0], 400)
            self.assertEqual(self.request(server=other, headers={"Host": "other.example.test"})[0], 303)
        finally:
            other.shutdown()
            other.server_close()


def test_the_gateway_does_not_start_without_the_host(monkeypatch, tmp_path):
    monkeypatch.setattr(sys, "argv", ["gateway", "--auth", str(tmp_path / "auth.json"), "--index", "index.html"])
    with pytest.raises(SystemExit) as exc:
        gateway.main()
    assert exc.value.code == 2
    with pytest.raises(TypeError):
        gateway.make_server(0, None, "index.html", 1, 1)


class CreationRouteTests(unittest.TestCase):
    class Created(BaseHTTPRequestHandler):
        calls = []

        def do_GET(self):
            self.respond()

        def do_POST(self):
            self.respond()

        def respond(self):
            length = int(self.headers.get("Content-Length", 0))
            self.calls.append((self.command, self.path, dict(self.headers), self.rfile.read(length)))
            self.send_response(201)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(b'{"code":"tcp-q7m4"}')

        def log_message(self, *args):
            pass

    def test_private_routes_require_login_origin_json_and_never_forward_credentials(self):
        calls = self.Created.calls
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            write_auth(root / "auth", "test", "test", salt=b"test")
            (root / "index").write_text("test")
            backend = Server(str(root / "socket"), self.Created)
            auth = gateway.Auth(root / "auth")
            s = gateway.make_server(0, auth, root / "index", 1, 1, domain=DOMAIN)
            s.customer_socket = s.project_socket = s.materials_socket = str(root / "socket")
            for x in (backend, s):
                threading.Thread(target=x.serve_forever, daemon=True).start()

            def request(path, method="GET", headers=None, body=None):
                c = http.client.HTTPConnection("127.0.0.1", s.server_port)
                c.request(method, path, headers={"Host": DOMAIN, **(headers or {})}, body=body)
                r = c.getresponse()
                status = r.status
                r.read()
                c.close()
                return status

            try:
                for route in ["/api/customers", "/api/project-options", "/api/customer-operations/" + "a" * 32,
                              "/api/project-operations/" + "b" * 32, "/api/projects/tcp-q7m4/materials",
                              "/api/projects/tcp-q7m4/materials/sources?source=brief"]:
                    assert request(route) == 401
                headers = {"Cookie": "__Host-awb-session=" + auth.new_session(), "Content-Type": "application/json"}
                for route in ["/api/customers", "/api/projects", "/api/projects/tcp-q7m4/materials/imports"]:
                    assert request(route, "POST", body="{}") == 401
                    assert request(route, "POST", headers, "{}") == 403
                    assert request(route, "POST", {**headers, "Origin": "https://evil.invalid"}, "{}") == 403
                    valid = {**headers, "Origin": "https://" + DOMAIN}
                    assert request(route, "POST", {**valid, "Sec-Fetch-Site": "cross-site"}, "{}") == 403
                    assert request(route, "POST", {**valid, "Content-Type": "text/plain"}, "{}") == 415
                    assert request(route, "POST", valid, "x" * 20000) == 413
                    assert request(route, "POST", valid, "{}") == 201
                    sent = calls[-1]
                    assert sent[0:2] == ("POST", route) and sent[3] == b"{}"
                    assert "Cookie" not in sent[2] and "Authorization" not in sent[2]
                assert request("/api/project-options", "GET", headers) == 201
                assert request("/api/projects/tcp-q7m4/materials", "GET", headers) == 201
                assert request("/api/projects/tcp-q7m4/materials/sources?source=brief", "GET", headers) == 201
                assert request("/internal/customers/CUST-Q7M4", "POST",
                               {**headers, "Origin": "https://" + DOMAIN}, "{}") == 405
            finally:
                for x in (s, backend):
                    x.shutdown()
                    x.server_close()


# --------------------------------------------------------------------------- T9 step 1: the front door

import os
import socket as _socket
import subprocess
import pwd
from awb.tcp.web import users as _users


def _unix_request(path, raw, timeout=5):
    s = _socket.socket(_socket.AF_UNIX, _socket.SOCK_STREAM)
    s.settimeout(timeout)
    s.connect(str(path))
    data = b""
    try:
        s.sendall(raw)
        while True:
            chunk = s.recv(65536)
            if not chunk:
                break
            data += chunk
    except ConnectionResetError:
        pass
    s.close()
    return data


def _front(tmp_path, peer):
    write_auth(tmp_path / "auth.json", "awb", PASSWORD)
    (tmp_path / "index.html").write_text("<html>front page</html>")
    auth = gateway.Auth(tmp_path / "auth.json")
    sock = gateway.unix_listener(str(tmp_path / "front.sock"))
    server = gateway.make_front_server(sock, auth, tmp_path / "index.html", 1, 1, domain=DOMAIN, peer=peer)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, auth


def test_the_front_socket_refuses_another_peer_before_reading_a_byte(tmp_path, capsys):
    """Planted: a server whose one peer is another uid closes the connection with no answer; with this process's uid
    as the peer the same request gets the sign-in redirect. The refusal is logged as one word, no address."""
    other, _ = _front(tmp_path, os.getuid() + 1)
    try:
        assert _unix_request(tmp_path / "front.sock", b"GET / HTTP/1.0\r\nHost: " + DOMAIN.encode() + b"\r\n\r\n") == b""
    finally:
        other.shutdown()
        other.server_close()
    assert "- - main - peer" in capsys.readouterr().out
    mine, _ = _front(tmp_path, os.getuid())
    try:
        answer = _unix_request(tmp_path / "front.sock", b"GET / HTTP/1.0\r\nHost: " + DOMAIN.encode() + b"\r\n\r\n")
        assert answer.startswith(b"HTTP/1.0 303") and b"Location: /login?next=%2F" in answer
    finally:
        mine.shutdown()
        mine.server_close()
    with pytest.raises(TypeError):
        gateway.make_front_server(gateway.unix_listener(str(tmp_path / "x.sock")), None, "i", 1, 1, domain=DOMAIN,
                                  peer=None)


def test_the_gateway_without_ports_opens_no_tcp_listener_and_answers_health_on_the_status_socket(tmp_path):
    """The gateway as the unit starts it after the switch (no --ports): its sockets are all Unix sockets, none of
    them in /proc/net/tcp or tcp6, and the status socket answers /health and nothing else. Planted: with --ports 0
    the same check finds a TCP listener."""
    write_auth(tmp_path / "auth.json", "awb", PASSWORD)
    (tmp_path / "index.html").write_text("x")
    me = pwd.getpwuid(os.getuid()).pw_name
    base = [sys.executable, "-I", str(Path(gateway.__file__)), "--auth", str(tmp_path / "auth.json"), "--index",
            str(tmp_path / "index.html"), "--domain", DOMAIN, "--front-peer", me]

    def tcp_inodes_of(pid):
        inodes = set()
        for name in ("tcp", "tcp6"):
            for line in Path("/proc/net/" + name).read_text().splitlines()[1:]:
                inodes.add(line.split()[9])
        mine = set()
        for fd in Path("/proc/%d/fd" % pid).iterdir():
            try:
                target = os.readlink(fd)
            except OSError:
                continue
            if target.startswith("socket:["):
                mine.add(target[8:-1])
        return mine & inodes, mine

    def started(extra, front):
        proc = subprocess.Popen(base + ["--front-socket", str(front), "--status-socket", str(tmp_path / "s.sock")] +
                                extra, stdout=subprocess.PIPE, text=True)
        assert proc.stdout.readline().strip() == "AWB authenticated gateway ready"
        return proc

    proc = started([], tmp_path / "f1.sock")
    try:
        tcp, sockets = tcp_inodes_of(proc.pid)
        assert sockets and not tcp
        health = _unix_request(tmp_path / "s.sock", b"GET /health HTTP/1.0\r\n\r\n")
        assert health.startswith(b"HTTP/1.0 200") and health.endswith(b'{"status":"ok"}\n')
        assert _unix_request(tmp_path / "s.sock", b"GET / HTTP/1.0\r\n\r\n").startswith(b"HTTP/1.0 404")
        assert oct(os.stat(tmp_path / "f1.sock").st_mode & 0o777) == "0o660"
    finally:
        proc.terminate()
        proc.wait(5)
    proc = started(["--ports", "0"], tmp_path / "f2.sock")
    try:
        assert tcp_inodes_of(proc.pid)[0]
    finally:
        proc.terminate()
        proc.wait(5)
    # without a front socket and without ports it does not start; a front socket needs its peer
    assert subprocess.run(base[:-2], capture_output=True).returncode == 2
    assert subprocess.run(base[:-2] + ["--front-socket", str(tmp_path / "f3.sock")], capture_output=True).returncode == 2


def test_inherited_sockets_are_found_by_their_path(tmp_path, monkeypatch):
    sock = gateway.unix_listener(str(tmp_path / "a.sock"))
    fd = sock.fileno()
    monkeypatch.setattr(os, "getpid", lambda: 4242)
    assert gateway.inherited_sockets({"LISTEN_PID": "4241", "LISTEN_FDS": "1"}) == {}
    if fd == 3:
        assert set(gateway.inherited_sockets({"LISTEN_PID": "4242", "LISTEN_FDS": "1"})) == {str(tmp_path / "a.sock")}
    sock.close()


def test_the_address_key_is_the_slash_64_of_ipv6_and_the_whole_ipv4_address():
    assert gateway.address_key("2001:db8:1:2:aaaa::1") == gateway.address_key("2001:db8:1:2:ffff::9") == "2001:db8:1:2::/64"
    assert gateway.address_key("2001:db8:1:3::1") != gateway.address_key("2001:db8:1:2::1")
    assert gateway.address_key("192.0.2.7") == "192.0.2.7" != gateway.address_key("192.0.2.8")
    assert gateway.address_key("::ffff:192.0.2.7") == "192.0.2.7"
    assert gateway.address_key("not an address") == "not an address"


@pytest.fixture
def readers(tmp_path):
    write_auth(tmp_path / "auth.json", "awb", PASSWORD)
    path = tmp_path / "users.json"
    pw = {login: _users.change(path, "add", login, False, rounds=1000) for login in ("reader1", "reader2")}
    owner = _users.change(path, "add", "owner1", False, rounds=1000, level="owner")
    return gateway.Auth(tmp_path / "auth.json", path), pw, owner, path


def test_eight_failures_from_one_slash_64_limit_that_slash_64_only(readers):
    auth, pw, owner, _ = readers
    for i in range(8):
        assert auth.sign_in("junk%d" % i, "wrong", "2001:db8:5:6::%x" % (i + 1)) == "fail"
    assert auth.sign_in("reader2", pw["reader2"], "2001:db8:5:6::99") == "limited"
    assert auth.sign_in("reader2", pw["reader2"], "2001:db8:5:7::1") == "ok"


def test_a_sign_in_in_flight_limits_its_address_and_a_busy_slot_is_not_counted(readers, monkeypatch):
    """One pbkdf2 per address key at a time; no free slot within the wait answers busy and counts nothing."""
    auth, pw, owner, _ = readers
    auth.inflight.add("192.0.2.1")
    assert auth.sign_in("reader1", pw["reader1"], "192.0.2.1") == "limited"
    auth.inflight.clear()
    monkeypatch.setattr(gateway, "SLOT_WAIT", 0.05)
    for _ in range(4):
        auth.slots.acquire()
    try:
        for _ in range(12):
            assert auth.sign_in("reader1", "wrong", "192.0.2.2") == "busy"
    finally:
        for _ in range(4):
            auth.slots.release()
    assert auth.failures.get("192.0.2.2", []) == []
    assert auth.limiter.waits_until("reader1", auth.clock()) == 0
    assert auth.sign_in("reader1", pw["reader1"], "192.0.2.2") == "ok"


def test_200_parallel_bad_logins_from_one_slash_64_do_not_stop_the_owner(readers):
    """Acceptance of T9 step 1: 200 parallel bad logins from 200 addresses of one /64 (junk names and the owner's
    own name); a correct owner login from another address, sent again while it waits, goes through within 5
    seconds."""
    auth, pw, owner, _ = readers
    results = []

    def bad(i):
        results.append(auth.sign_in("junk%03d" % i if i % 2 else "owner1", "wrong", "2001:db8:9:9::%x" % (i + 1)))

    threads = [threading.Thread(target=bad, args=(i,)) for i in range(200)]
    for t in threads:
        t.start()
    started = time.monotonic()
    while auth.sign_in("owner1", owner, "198.51.100.4") != "ok":    # the owner sends the form again
        assert time.monotonic() - started < 5
        time.sleep(0.25)
    assert time.monotonic() - started < 5
    for t in threads:
        t.join()
    assert set(results) <= {"fail", "limited"} and len(auth.failures.get("2001:db8:9:9::/64", [])) <= 8


def test_the_owner_entry_waits_a_growing_delay_and_is_never_locked(readers):
    """An owner entry gets no cooldown and no site cap: after each failure its next try waits 1, 2, 4 ... 60
    seconds; a try inside the wait answers like a wrong password, uses no pbkdf2 and is not counted; the right
    password after the wait works however many failures came before."""
    auth, pw, owner, _ = readers
    clock = [1_800_000_000.0]
    auth.clock = lambda: clock[0]
    waits = []
    for i in range(10):
        assert auth.sign_in("owner1", "wrong", "198.51.100.%d" % i) == "fail"
        waits.append(auth.delays.until[auth.delays.cell("owner1")] - clock[0])
        assert auth.sign_in("owner1", owner, "198.51.100.%d" % i) == "fail"
        clock[0] += waits[-1]
    assert waits == [1, 2, 4, 8, 16, 32, 60, 60, 60, 60]
    for i in range(40):
        auth.sign_in("x%02d" % i, "wrong", "203.0.113.%d" % i)
        clock[0] += 1
    assert auth.limiter.site_closed_until(clock[0])
    assert auth.sign_in("reader1", pw["reader1"], "192.0.2.50") == "cooldown"
    assert auth.sign_in("owner1", owner, "192.0.2.51") == "ok"
    assert auth.delays.until[auth.delays.cell("owner1")] == 0


def test_a_login_token_is_taken_once(tmp_path):
    auth = gateway.Auth(write_auth(tmp_path / "a.json", "awb", PASSWORD) or tmp_path / "a.json")
    token = auth.csrf_token()
    assert auth.use_csrf(token) and not auth.use_csrf(token)
    assert not auth.use_csrf("forged")


class FrontLoginTests(LoginTests):
    """The sign-in over HTTP with the step 1 rules: a reused login form is refused, a known and an unknown name
    answer alike, a full session table and a busy door answer 503."""

    def test_a_reused_login_form_is_refused(self):
        h, data = self.form(password="wrong")
        self.assertEqual(self.request("/login", "POST", h, urlencode(data))[0], 200)
        self.assertEqual(self.request("/login", "POST", h, urlencode(data))[0], 403)

    def test_a_full_session_table_refuses_a_new_login_and_evicts_nobody(self):
        held = [self.auth.new_session() for _ in range(gateway.SESSION_TABLE)]
        held = [t for t in held if t]
        try:
            h, data = self.form()
            status, out, body = self.request("/login", "POST", h, urlencode(data))
            self.assertEqual(status, 503)
            self.assertIn(b"too many open sessions", body)
            self.assertTrue(all(self.auth.session_valid(t) for t in held))
        finally:
            for t in held:
                self.auth.revoke_session(t)

    def test_requests_without_a_session_beyond_the_cap_get_503(self):
        sem = self.auth.unauthenticated
        self.auth.unauthenticated = threading.BoundedSemaphore(1)
        self.auth.unauthenticated.acquire()
        try:
            status, out, body = self.request("/")
            self.assertEqual(status, 503)
            self.assertEqual(out.get("Retry-After"), "5")
            self.assertEqual(self.request(headers=self.session_headers())[0], 200)
        finally:
            self.auth.unauthenticated = sem

    def test_a_malformed_foreign_cookie_does_not_hide_the_session(self):
        token = self.auth.new_session()
        cookie = 'bad"cookie=x"y; other=\\x; __Host-awb-session=' + token + "; tail=1"
        self.assertEqual(self.request(headers={"Cookie": cookie})[0], 200)


def test_a_login_keeps_five_sessions_and_a_removed_or_reset_user_loses_them(readers):
    auth, pw, owner, path = readers
    tokens = [auth.new_session("reader1") for _ in range(6)]
    assert not auth.session_valid(tokens[0]) and all(auth.session_valid(t) for t in tokens[1:])
    other = auth.new_session("reader2")
    assert auth.session_user(other) == ("reader2", "reader") and auth.session_user(auth.new_session("owner1"))[1] == "owner"
    _users.change(path, "reset", "reader1", False, rounds=1000)
    assert not any(auth.session_valid(t) for t in tokens) and auth.session_valid(other)
    _users.change(path, "remove", "reader2", False)
    assert not auth.session_valid(other)


def test_the_log_line_names_host_level_and_refusal_but_no_address_or_user(tmp_path, capsys):
    server, auth = _front(tmp_path, os.getuid())
    try:
        _unix_request(tmp_path / "front.sock", b"GET /login HTTP/1.0\r\nHost: " + DOMAIN.encode() +
                      b"\r\nCF-Connecting-IP: 203.0.113.77\r\n\r\n")
        token = auth.new_session("awb")
        _unix_request(tmp_path / "front.sock", b"GET / HTTP/1.0\r\nHost: " + DOMAIN.encode() +
                      b"\r\nCookie: __Host-awb-session=" + token.encode() + b"\r\n\r\n")
    finally:
        server.shutdown()
        server.server_close()
    out = capsys.readouterr().out
    assert "GET 200 main - -" in out and "GET 200 main reader -" in out
    assert "203.0.113" not in out and "awb " not in out.replace("AWB ", "")


def test_v1_logs_whether_the_edge_kept_a_forged_client_address(tmp_path, capsys):
    """V1 (a test build only): with --v1-marker the gateway logs whether a CF-Connecting-IP the client sent arrived
    unchanged, never the address itself."""
    server, auth = _front(tmp_path, os.getuid())
    server.v1_marker = "v1-check-7f3a"
    try:
        for value in (b"v1-check-7f3a", b"198.51.100.200"):
            _unix_request(tmp_path / "front.sock", b"GET /login HTTP/1.0\r\nHost: " + DOMAIN.encode() +
                          b"\r\nX-AWB-V1: v1-check-7f3a\r\nCF-Connecting-IP: " + value + b"\r\n\r\n")
    finally:
        server.shutdown()
        server.server_close()
    out = capsys.readouterr().out
    assert "v1: CF-Connecting-IP arrived as the client sent it" in out
    assert "v1: CF-Connecting-IP was replaced by the edge" in out
    assert "198.51.100" not in out


def test_totp_codes_follow_rfc_6238():
    """The SHA1 test vectors of RFC 6238 appendix B, cut to six digits, and one step of skew."""
    secret = base64.b32encode(b"12345678901234567890").decode()
    for t, code in ((59, "287082"), (1111111109, "081804"), (1111111111, "050471"), (1234567890, "005924"),
                    (2000000000, "279037")):
        assert gateway.totp_code(secret, t // 30) == code
    assert gateway.totp_step(secret, "287082", 59 + 30) == 1
    assert gateway.totp_step(secret, "287082", 59 + 60) is None
    assert gateway.totp_step(secret, "28708", 59) is None and gateway.totp_step("", "287082", 59) is None
