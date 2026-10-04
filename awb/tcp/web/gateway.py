"""Authenticated front door for AWB. No application imports or model credentials."""
# Moved from the web adapters of 2026-10-02 into the repository on 2026-10-04, behaviour unchanged (tests/test_web_*.py).
# The host name of the site is a setting (--domain), so no host of the owner is written into the repository.

import argparse
import base64
import hashlib
import hmac
import http.client
import html
import secrets
from http.cookies import SimpleCookie, CookieError
import json
import re
import socket
import sys
import threading
import time
from collections import OrderedDict
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit, parse_qs, urlencode

MAX_BODY = 16_384
MAX_RESPONSE = 12 * 1024 * 1024
BACKEND_ROUTES = {'/kb', '/price', '/projects', '/reviews', '/tenants', '/health', '/portal', '/ask'}


class UnixConnection(http.client.HTTPConnection):
    def __init__(self, path, timeout=150):
        super().__init__('localhost', timeout=timeout)
        self.socket_path = path

    def connect(self):
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(self.timeout)
        self.sock.connect(self.socket_path)


class Auth:
    def __init__(self, path):
        data = json.loads(Path(path).read_text())
        self.username = data['username']
        self.salt = bytes.fromhex(data['salt'])
        self.digest = bytes.fromhex(data['hash'])
        self.rounds = data['rounds']
        self.lock = threading.Lock()
        self.failures = OrderedDict()
        self.valid = OrderedDict()
        self.cache_key = __import__('secrets').token_bytes(32)
        self.slots = threading.BoundedSemaphore(4)
        self.sessions = OrderedDict()

    def check(self, header, client):
        if not header:
            return 401
        if len(header) > 1024:
            return 401
        now = time.monotonic()
        cache_id = hmac.digest(self.cache_key, header.encode(), 'sha256')
        with self.lock:
            recent = self.failures.get(client, [])
            recent = [t for t in recent if now-t < 60]
            if len(recent) >= 8:
                return 429
            if self.valid.get(cache_id, 0) > now:
                return 200
        valid = False
        if self.slots.acquire(blocking=False):
            try:
                scheme, encoded = header.split(' ', 1)
                if scheme.lower() == 'basic':
                    raw = base64.b64decode(encoded, validate=True).decode('utf-8')
                    username, password = raw.split(':', 1)
                    computed = hashlib.pbkdf2_hmac('sha256', password.encode(), self.salt, self.rounds)
                    valid = hmac.compare_digest(username.encode(), self.username.encode()) and hmac.compare_digest(computed, self.digest)
            except (ValueError, UnicodeError):
                pass
            finally:
                self.slots.release()
        else:
            return 429
        with self.lock:
            if valid:
                self.valid[cache_id] = now+300
                while len(self.valid) > 64:
                    self.valid.popitem(last=False)
                self.failures.pop(client, None)
            else:
                self.failures[client] = recent + [now]
                self.failures.move_to_end(client)
                while len(self.failures) > 2048:
                    self.failures.popitem(last=False)
        return 200 if valid else 401

    def csrf_token(self):
        payload = str(int(time.time())) + '.' + secrets.token_urlsafe(24)
        return payload + '.' + hmac.new(self.cache_key, payload.encode(), 'sha256').hexdigest()

    def valid_csrf(self, token):
        try:
            stamp, nonce, sig = token.split('.')
            payload = stamp + '.' + nonce
            return 0 <= time.time()-int(stamp) < 600 and hmac.compare_digest(sig, hmac.new(self.cache_key, payload.encode(), 'sha256').hexdigest())
        except (ValueError, TypeError):
            return False

    def new_session(self):
        token = secrets.token_urlsafe(32)
        key = hashlib.sha256(token.encode()).digest()
        with self.lock:
            now = time.monotonic()
            self.sessions = OrderedDict((k,v) for k,v in self.sessions.items() if v > now)
            self.sessions[key] = now + 8*3600
            while len(self.sessions) > 128:
                self.sessions.popitem(last=False)
        return token

    def session_valid(self, token):
        if not token or len(token) > 100:
            return False
        key = hashlib.sha256(token.encode()).digest()
        with self.lock:
            expires = self.sessions.get(key, 0)
            if expires <= time.monotonic():
                self.sessions.pop(key, None)
                return False
            return True

    def revoke_session(self, token):
        with self.lock:
            self.sessions.pop(hashlib.sha256(token.encode()).digest(), None)


def document_csp(data):
    text = data.decode('utf-8')
    def hashes(tag):
        return ' '.join("'sha256-"+base64.b64encode(hashlib.sha256(x.encode()).digest()).decode()+"'" for x in re.findall(r'<'+tag+r'\b[^>]*>(.*?)</'+tag+r'>', text, re.S | re.I))
    return "default-src 'none'; script-src " + (hashes('script') or "'none'") + "; style-src 'unsafe-inline'; img-src 'self' data:; font-src 'self'; connect-src 'self'; form-action 'self'; base-uri 'none'; frame-ancestors 'none'"


class Gateway(BaseHTTPRequestHandler):
    server_version = 'AWB'
    sys_version = ''
    protocol_version = 'HTTP/1.0'

    def setup(self):
        super().setup()
        self.connection.settimeout(20)

    def reply(self, status, body=b'', headers=None):
        self.close_connection = True
        self.send_response(status)
        baseline = {
            'Content-Type': 'text/html; charset=utf-8',
            'Cache-Control': 'no-store, private',
            'X-Content-Type-Options': 'nosniff',
            'X-Frame-Options': 'DENY',
            'Referrer-Policy': 'same-origin',
            'Strict-Transport-Security': 'max-age=31536000',
            'Content-Security-Policy': "default-src 'none'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'",
            'Connection': 'close',
        }
        baseline.update(headers or {})
        baseline['Content-Length'] = str(len(body))
        for name, value in baseline.items():
            self.send_header(name, value)
        self.end_headers()
        if self.command != 'HEAD':
            self.wfile.write(body)

    def cookie(self, name):
        try:
            cookies = SimpleCookie()
            cookies.load(self.headers.get('Cookie', ''))
            return cookies[name].value if name in cookies else ''
        except CookieError:
            return ''

    def safe_next(self, value):
        return value if value in BACKEND_ROUTES | {'/'} else '/'

    def login_page(self, next_path='/', error='', status=200):
        token = self.server.auth.csrf_token()
        next_path = self.safe_next(next_path)
        message = '<p role="alert" class="error">'+html.escape(error)+'</p>' if error else ''
        content = ('''<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Sign in · AWB</title><style>
*{box-sizing:border-box}body{margin:0;background:#f5f7fa;color:#121a24;font:15px/1.5 -apple-system,BlinkMacSystemFont,"Segoe UI",Arial,sans-serif;min-height:100vh;display:grid;place-items:center;padding:24px}.box{width:100%;max-width:400px;background:white;border:1px solid #d8dfe8;border-radius:12px;padding:34px}.brand{display:inline-grid;place-items:center;width:48px;height:44px;background:#c4006a;color:white;border-radius:8px;font-weight:750;font-size:14px}h1{font-size:25px;margin:22px 0 6px;letter-spacing:-.7px}.muted{color:#636d78;font-size:13px;margin:0 0 24px}label{display:block;font-size:13px;margin:16px 0 6px}input{width:100%;padding:11px 12px;border:1px solid #d8dfe8;border-radius:7px;font:inherit}button{width:100%;padding:12px;border:0;border-radius:7px;background:#c4006a;color:white;font:600 14px inherit;margin-top:24px;cursor:pointer}button:hover{background:#a8005c}input:focus-visible,button:focus-visible{outline:2px solid #c4006a;outline-offset:3px}.error{color:#b33a31;background:#f8e7e5;border-radius:7px;padding:12px;font-size:13px}.foot{margin:22px 0 0;color:#636d78;font-size:11px}a{color:#c4006a}</style></head><body><main class="box"><div class="brand">AWB</div><h1>Welcome to your workspace</h1><p class="muted">Sign in to Architect Workbench.</p>''' + message +
            '<form action="/login" method="post"><input type="hidden" name="csrf" value="'+html.escape(token)+'"><input type="hidden" name="next" value="'+html.escape(next_path)+'"><label for="username">Username</label><input id="username" name="username" autocomplete="username" required maxlength="100" autofocus><label for="password">Password</label><input id="password" name="password" type="password" autocomplete="current-password" required maxlength="200"><button type="submit">Sign in</button></form><p class="foot">Private access · Your session lasts up to 8 hours.</p></main></body></html>').encode()
        self.reply(status, content, {'Content-Security-Policy': document_csp(content), 'Set-Cookie': '__Host-awb-login='+token+'; Path=/; Secure; HttpOnly; SameSite=Strict; Max-Age=600'})

    def read_form(self):
        if self.headers.get('Origin') != 'https://' + self.server.domain or self.headers.get('Sec-Fetch-Site') not in {None, 'same-origin', 'none'}:
            self.reply(403, b'Open this form on the AWB site.\n')
            return None
        lengths = self.headers.get_all('Content-Length', [])
        if self.headers.get('Transfer-Encoding') or len(lengths) != 1:
            self.reply(400, b'A single content length is required.\n')
            return None
        try:
            length = int(lengths[0])
        except ValueError:
            length = -1
        if not 0 <= length <= MAX_BODY:
            self.reply(413, b'Request is too large.\n')
            return None
        if self.headers.get('Content-Type', '').split(';')[0].lower() != 'application/x-www-form-urlencoded':
            self.reply(415, b'Use the sign-in form.\n')
            return None
        try:
            data = self.rfile.read(length)
            if len(data) != length:
                raise ValueError()
            return parse_qs(data.decode('utf-8'), max_num_fields=10)
        except (OSError, ValueError, UnicodeError):
            self.reply(400, b'Invalid form.\n')
            return None

    def dispatch(self):
        if self.headers.get('Host', '').lower() != self.server.domain:
            self.reply(400, b'Invalid host.\n')
            return
        if self.command not in {'GET', 'HEAD', 'POST'}:
            self.reply(405, b'Method not allowed.\n', {'Allow': 'GET, HEAD, POST'})
            return
        url = urlsplit(self.path)
        if url.scheme or url.netloc or self.path.startswith('//'):
            self.reply(400, b'Invalid request target.\n')
            return
        path = url.path
        if path == '/favicon.ico':
            self.reply(204)
            return
        peer = self.headers.get('CF-Connecting-IP', self.client_address[0])[:100]
        session = self.cookie('__Host-awb-session')
        authenticated = self.server.auth.session_valid(session)
        if path == '/login':
            if self.command in {'GET', 'HEAD'}:
                try:
                    query = parse_qs(url.query, max_num_fields=10)
                except ValueError:
                    self.reply(400, b'Invalid query.\n')
                    return
                next_path = self.safe_next((query.get('next') or ['/'])[0])
                if authenticated:
                    self.reply(303, headers={'Location': next_path})
                else:
                    self.login_page(next_path)
                return
            form = self.read_form()
            if form is None:
                return
            csrf = (form.get('csrf') or [''])[0]
            if not csrf or not hmac.compare_digest(csrf.encode(), self.cookie('__Host-awb-login').encode()) or not self.server.auth.valid_csrf(csrf):
                self.login_page(error='The sign-in form expired. Please try again.', status=403)
                return
            username = (form.get('username') or [''])[0]
            password = (form.get('password') or [''])[0]
            next_path = self.safe_next((form.get('next') or ['/'])[0])
            header = 'Basic '+base64.b64encode((username+':'+password).encode()).decode()
            status = self.server.auth.check(header, peer)
            if status != 200:
                self.login_page(next_path, 'Too many attempts. Try again in one minute.' if status == 429 else 'The username or password is incorrect.', 429 if status == 429 else 200)
                return
            token = self.server.auth.new_session()
            self.reply(303, headers={'Location': next_path, 'Set-Cookie': '__Host-awb-session='+token+'; Path=/; Secure; HttpOnly; SameSite=Lax; Max-Age=28800'})
            return
        if not authenticated:
            if path.startswith('/api/'):
                self.reply(401, b'{"error":"Sign in to open the project data."}', {'Content-Type':'application/json; charset=utf-8'})
            elif self.command in {'GET', 'HEAD'}:
                self.reply(303, headers={'Location': '/login?'+urlencode({'next': self.safe_next(path)})})
            else:
                self.reply(403, b'Sign in before submitting a question.\n')
            return
        if path == '/logout':
            if self.command in {'GET', 'HEAD'}:
                content = b'<html><head><title>Sign out - AWB</title></head><body><h1>Sign out of AWB?</h1><form method="post" action="/logout"><button>Sign out</button></form><a href="/">Return to workspace</a></body></html>'
                self.reply(200, content)
            elif self.read_form() is not None:
                self.server.auth.revoke_session(session)
                self.reply(303, headers={'Location': '/login', 'Set-Cookie': '__Host-awb-session=; Path=/; Secure; HttpOnly; SameSite=Lax; Max-Age=0'})
            return
        if self.command == 'POST':
            # Cookie sessions require the exact HTTPS origin for submitted questions.
            is_chat = bool(re.fullmatch(r'/api/projects/tcp-[a-z0-9]{4}/chat', path))
            is_creation = path in {'/api/projects', '/api/customers'} or bool(re.fullmatch(r'/api/projects/tcp-[a-z2-7]{4}/materials/imports',path))
            if path != '/ask' and not is_chat and not is_creation:
                self.reply(405, b'Method not allowed.\n')
                return
            if self.headers.get('Origin') != 'https://' + self.server.domain:
                self.reply(403, b'Open Ask on the AWB site to submit a question.\n')
                return
            if self.headers.get('Sec-Fetch-Site') not in {None, 'same-origin', 'none'}:
                self.reply(403, b'Cross-site requests are not allowed.\n')
                return
            lengths = self.headers.get_all('Content-Length', [])
            if self.headers.get('Transfer-Encoding') or len(lengths) != 1:
                self.reply(400, b'A single content length is required.\n')
                return
            try:
                length = int(lengths[0])
            except ValueError:
                length = -1
            if not 0 <= length <= MAX_BODY:
                self.reply(413, b'Request is too large.\n')
                return
            expected_type = 'application/json' if is_chat or is_creation else 'application/x-www-form-urlencoded'
            if self.headers.get('Content-Type', '').split(';')[0].lower() != expected_type:
                self.reply(415, b'Use the Ask form.\n')
                return
            try:
                body = self.rfile.read(length)
            except (socket.timeout, OSError):
                self.reply(408, b'Request timed out.\n')
                return
            if len(body) != length:
                self.reply(400, b'Incomplete request.\n')
                return
        else:
            body = None
        if path in {'/', '/architect-workbench.html'} and self.command in {'GET', 'HEAD'}:
            try:
                content = self.server.index.read_bytes()
            except OSError:
                self.reply(503, b'The workspace is temporarily unavailable.\n')
                return
            self.reply(200, content, {'Content-Security-Policy': document_csp(content)})
            return
        if path == '/favicon.ico':
            self.reply(204)
            return
        is_tenant_api = path == '/api/tenants'
        is_project_api = bool(re.fullmatch(r'/api/projects(?:/tcp-[a-z0-9]{4})?', path))
        is_chat_api = bool(re.fullmatch(r'/api/projects/tcp-[a-z0-9]{4}/chat', path))
        is_materials_api = bool(re.fullmatch(r'/api/projects/tcp-[a-z2-7]{4}/materials(?:/(?:sources|imports|M-[A-Z]{24}))?',path))
        is_creation_api = bool(re.fullmatch(r'/api/(?:customer|project)-operations/[a-f0-9]{32}', path)) or path in {'/api/customers', '/api/project-options'} or (path == '/api/projects' and self.command == 'POST')
        if path not in BACKEND_ROUTES and not is_project_api and not is_tenant_api and not is_chat_api and not is_creation_api and not is_materials_api:
            self.reply(404, b'Not found.\n')
            return
        port = self.server.projects_port if is_project_api else self.server.tenants_port if is_tenant_api else self.server.ask_port if path == '/ask' or is_chat_api else self.server.portal_port
        target = '/' if path == '/portal' else self.path
        forwarded = {'Host': self.server.domain, 'X-Forwarded-Proto': 'https'}
        if self.command == 'POST':
            forwarded.update({'Origin': 'https://' + self.server.domain, 'Content-Type': 'application/json' if is_chat_api or is_creation_api or is_materials_api else 'application/x-www-form-urlencoded'})
        # Never forward Authorization, cookies or user-supplied forwarding headers.
        conn = (UnixConnection(self.server.materials_socket) if is_materials_api else UnixConnection(self.server.customer_socket if path == '/api/customers' or path.startswith('/api/customer-operations/') else self.server.project_socket)
                if is_creation_api else http.client.HTTPConnection('127.0.0.1', port, timeout=150))
        try:
            conn.request('GET' if self.command == 'HEAD' else self.command, target, body=body, headers=forwarded)
            response = conn.getresponse()
            data = response.read(MAX_RESPONSE+1)
            if len(data) > MAX_RESPONSE:
                self.reply(502, b'The response was too large.\n')
                return
            permitted = {'content-type', 'content-security-policy', 'allow'}
            headers = {k: v for k, v in response.getheaders() if k.lower() in permitted}
            self.reply(response.status, data, headers)
        except (OSError, http.client.HTTPException):
            self.reply(502, b'The AWB service is temporarily unavailable.\n')
        finally:
            conn.close()

    do_GET = do_HEAD = do_POST = do_PUT = do_PATCH = do_DELETE = do_OPTIONS = dispatch

    def log_message(self, fmt, *args):
        # No question text, credentials, query strings or backend data in access logs.
        status = str(args[1]) if len(args) > 1 else '-'
        print(self.command, status, flush=True)


def make_server(port, auth, index, portal_port, ask_port, projects_port=8182, tenants_port=8183, *, domain):
    server = ThreadingHTTPServer(('127.0.0.1', port), Gateway)
    server.domain = domain.lower()
    server.daemon_threads = True
    server.auth = auth
    server.index = Path(index)
    server.portal_port = portal_port
    server.ask_port = ask_port
    server.projects_port = projects_port
    server.tenants_port = tenants_port
    server.customer_socket = '/run/awb-customers.sock'
    server.project_socket = '/run/awb-project-create.sock'
    server.materials_socket = '/run/awb-materials.sock'
    return server


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--auth', required=True)
    ap.add_argument('--domain', required=True, help='the host name of the site, without scheme')
    ap.add_argument('--index', required=True)
    ap.add_argument('--ports', nargs='+', type=int, default=[8080, 8081])
    ap.add_argument('--portal-port', type=int, default=8180)
    ap.add_argument('--ask-port', type=int, default=8181)
    ap.add_argument('--projects-port', type=int, default=8182)
    ap.add_argument('--tenants-port', type=int, default=8183)
    args = ap.parse_args()
    auth = Auth(args.auth)
    servers = [make_server(port, auth, args.index, args.portal_port, args.ask_port, args.projects_port, args.tenants_port, domain=args.domain) for port in args.ports]
    for server in servers[1:]:
        threading.Thread(target=server.serve_forever, daemon=True).start()
    print('AWB authenticated gateway ready', flush=True)
    servers[0].serve_forever()


if __name__ == '__main__':
    main()
