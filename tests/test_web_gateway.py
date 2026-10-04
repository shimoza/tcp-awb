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
