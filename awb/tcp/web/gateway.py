"""Authenticated front door for AWB. No application imports or model credentials."""
# Moved from the web adapters of 2026-10-02 into the repository on 2026-10-04, behaviour unchanged (tests/test_web_*.py).
# The host name of the site is a setting (--domain), so no host of the owner is written into the repository.

import argparse
import base64
import calendar
import hashlib
import hmac
import http.client
import html
import secrets
from http.cookies import SimpleCookie, CookieError
import json
import os
import re
import socket
import sys
import threading
import time
from collections import OrderedDict, deque
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


LOGIN_RE = re.compile(r'[a-z][a-z0-9._-]{1,31}')
NOT_A_LOGIN = '(not a login name)'
SERIES = 5                  # failures of one login within SERIES_WINDOW start a wait
SERIES_WINDOW = 15*60
FIRST_WAIT = 15*60          # doubles with each further series, up to MAX_WAIT; a success resets it
MAX_WAIT = 24*3600
SITE_CAP = 30               # failures of the whole site within SITE_WINDOW close every sign-in
SITE_WINDOW = 3600
COOLDOWN_TEXT = 'Too many failed sign-ins. Try again later.'
LOG_MAX = 2*1024*1024
DEFAULT_ROUNDS = 600_000


class Limiter:
    """The per-login wait and the site-wide cap (F4). Pure: every call gets the time, the log replays into it."""

    def __init__(self):
        self.logins = {}        # key -> {'fails': [t], 'until': t, 'level': n, 'last': t}
        self.site = deque()

    def _prune(self, now):
        while self.site and now-self.site[0] >= SITE_WINDOW:
            self.site.popleft()
        for key in [k for k, s in self.logins.items() if now-max(s['until'], s['last']) > MAX_WAIT]:
            del self.logins[key]

    def site_closed_until(self, now):
        self._prune(now)
        return self.site[0]+SITE_WINDOW if len(self.site) >= SITE_CAP else 0

    def waits_until(self, key, now):
        state = self.logins.get(key)
        return state['until'] if state and state['until'] > now else 0

    def refused(self, key, now):
        """'site', 'login' or '' (free to try)."""
        if self.site_closed_until(now):
            return 'site'
        return 'login' if key is not None and self.waits_until(key, now) else ''

    def failure(self, key, now):
        self.site.append(now)
        if key is None:
            return
        state = self.logins.setdefault(key, {'fails': [], 'until': 0, 'level': 0, 'last': now})
        state['fails'] = [t for t in state['fails'] if now-t < SERIES_WINDOW and t >= state['until']] + [now]
        state['last'] = now
        if len(state['fails']) >= SERIES:
            state['until'] = now + min(FIRST_WAIT * 2**state['level'], MAX_WAIT)
            state['level'] += 1
            state['fails'] = []

    def success(self, key):
        self.logins.pop(key, None)

    def cooldowns(self, now):
        self._prune(now)
        return {k: s['until'] for k, s in self.logins.items() if s['until'] > now}


def log_time(t):
    return time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime(t))


def read_log(path, since):
    """The entries (time, login, result) of the sign-in log and its rotated copy at or after `since`, oldest first."""
    out = []
    for name in (str(path)+'.1', str(path)):
        try:
            lines = Path(name).read_text(encoding='utf-8', errors='replace').splitlines()
        except OSError:
            continue
        for line in lines:
            try:
                entry = json.loads(line)
                t = calendar.timegm(time.strptime(entry['time'], '%Y-%m-%dT%H:%M:%SZ'))
                login, result = str(entry['login']), str(entry['result'])
            except (ValueError, KeyError, TypeError):
                continue
            if t >= since:
                out.append((t, login, result))
    out.sort(key=lambda e: e[0])
    return out


def replay(entries):
    """A Limiter in the state the logged attempts leave it in."""
    limiter = Limiter()
    for t, login, result in entries:
        key = None if login == NOT_A_LOGIN else login
        if result == 'ok':
            limiter.success(key)
        elif result == 'fail':
            limiter.failure(key, t)
    return limiter


class Auth:
    """The sign-in. The users file (one salt and PBKDF2 digest per login, `awb web user`) is read on every change;
    while it does not exist the single login of `path` works as before, with the per-address limit only."""

    def __init__(self, path, users=None, log=None, clock=time.time):
        self.path = Path(path)
        self.users_path = Path(users) if users else self.path.parent / 'users.json'
        self.log_path = Path(log) if log else None
        self.clock = clock
        self.lock = threading.Lock()
        self.log_lock = threading.Lock()
        self.failures = OrderedDict()
        self.cache_key = secrets.token_bytes(32)
        self.slots = threading.BoundedSemaphore(4)
        self.sessions = OrderedDict()
        self.owners = {}
        self.stamp = None
        self.users = None
        self.legacy = None
        self.username = None
        if self.path.exists() or not self.users_path.exists():
            data = json.loads(self.path.read_text())
            self.username = data['username']
            self.legacy = {data['username']: (bytes.fromhex(data['salt']), bytes.fromhex(data['hash']), data['rounds'])}
        now = clock()
        self.limiter = replay(read_log(self.log_path, now-2*MAX_WAIT)) if self.log_path else Limiter()

    def accounts(self):
        """(per-user mode, {login: (salt, digest, rounds)}), the users file read again when it changed. A users
        file that cannot be read lets nobody in."""
        try:
            st = os.stat(self.users_path)
        except FileNotFoundError:
            return False, self.legacy or {}
        except OSError:
            return True, {}
        stamp = (st.st_ino, st.st_mtime_ns, st.st_size)
        with self.lock:
            if stamp == self.stamp:
                return True, self.users
        try:
            data = json.loads(self.users_path.read_text())
            users = {login: (bytes.fromhex(u['salt']), bytes.fromhex(u['hash']), int(u['rounds']))
                     for login, u in data['users'].items() if LOGIN_RE.fullmatch(login)}
        except (OSError, ValueError, KeyError, TypeError, AttributeError):
            print('AWB gateway: the users file cannot be read; nobody signs in', flush=True)
            users = {}
        with self.lock:
            self.stamp, self.users = stamp, users
        return True, users

    def log(self, now, login, result):
        if not self.log_path:
            return
        line = json.dumps({'time': log_time(now), 'login': login if LOGIN_RE.fullmatch(login) else NOT_A_LOGIN,
                           'result': result}) + '\n'
        with self.log_lock:
            try:
                if self.log_path.exists() and self.log_path.stat().st_size > LOG_MAX:
                    os.replace(self.log_path, str(self.log_path)+'.1')
                fd = os.open(self.log_path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
                with os.fdopen(fd, 'a', encoding='utf-8') as fh:
                    fh.write(line)
            except OSError:
                print('AWB gateway: the sign-in log cannot be written', flush=True)

    def sign_in(self, login, password, client):
        """'ok', 'fail' (wrong password or unknown login, the same answer), 'cooldown' (the login waits or the site
        is closed), 'limited' (this address failed 8 times within a minute) or 'busy' (no free slot)."""
        login = login[:100]
        now = self.clock()
        per_user, accounts = self.accounts()
        key = login if LOGIN_RE.fullmatch(login) else 'h:'+hashlib.sha256(login.encode()).hexdigest()[:24]
        with self.lock:
            recent = [t for t in self.failures.get(client, []) if now-t < 60]
            if len(recent) >= 8:
                result = 'limited'
            elif per_user and self.limiter.refused(key, now):
                result = 'cooldown'
                self.failures[client] = recent + [now]
            else:
                result = ''
        if result:
            self.log(now, login, result if result != 'cooldown' else
                     ('site-cap' if self.limiter.site_closed_until(now) else 'cooldown'))
            return result
        if not self.slots.acquire(blocking=False):
            self.log(now, login, 'busy')
            return 'busy'
        try:
            account = accounts.get(login)
            salt, digest, rounds = account or (b'\0'*16, b'\0'*32, next(iter(accounts.values()), (0, 0, DEFAULT_ROUNDS))[2])
            computed = hashlib.pbkdf2_hmac('sha256', password.encode('utf-8', 'replace'), salt, rounds)
            valid = account is not None and hmac.compare_digest(computed, digest)
        finally:
            self.slots.release()
        with self.lock:
            if valid:
                self.failures.pop(client, None)
                if per_user:
                    self.limiter.success(key)
            else:
                self.failures[client] = recent + [now]
                self.failures.move_to_end(client)
                while len(self.failures) > 2048:
                    self.failures.popitem(last=False)
                if per_user:
                    self.limiter.failure(key, now)
        self.log(now, login, 'ok' if valid else 'fail')
        return 'ok' if valid else 'fail'

    def check(self, header, client):
        """The sign-in from a Basic header: 200, 401 or 429."""
        if not header or len(header) > 1024:
            return 401
        try:
            scheme, encoded = header.split(' ', 1)
            if scheme.lower() != 'basic':
                return 401
            login, password = base64.b64decode(encoded, validate=True).decode('utf-8').split(':', 1)
        except (ValueError, UnicodeError):
            return 401
        return {'ok': 200, 'fail': 401}.get(self.sign_in(login, password, client), 429)

    def status(self):
        """For awb web status: the sign-ins of the last day by result and the waits that hold now."""
        now = self.clock()
        return now, read_log(self.log_path, now-24*3600), self.limiter

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

    def new_session(self, login=None):
        """A session; one bound to a login ends when that login is removed or gets a new password."""
        token = secrets.token_urlsafe(32)
        key = hashlib.sha256(token.encode()).digest()
        bound = None
        if login is not None:
            account = self.accounts()[1].get(login)
            bound = (login, account[1] if account else b'')
        with self.lock:
            now = time.monotonic()
            self.sessions = OrderedDict((k,v) for k,v in self.sessions.items() if v > now)
            self.sessions[key] = now + 8*3600
            if bound:
                self.owners[key] = bound
            while len(self.sessions) > 128:
                self.sessions.popitem(last=False)
            self.owners = {k: v for k, v in self.owners.items() if k in self.sessions}
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
            bound = self.owners.get(key)
        per_user, accounts = self.accounts()
        if bound is None:
            return not per_user
        account = accounts.get(bound[0])
        if account and hmac.compare_digest(account[1], bound[1]):
            return True
        self.revoke_session(token)
        return False

    def revoke_session(self, token):
        key = hashlib.sha256(token.encode()).digest()
        with self.lock:
            self.sessions.pop(key, None)
            self.owners.pop(key, None)


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
            result = self.server.auth.sign_in(username, password, peer)
            if result != 'ok':
                text = {'fail': 'The username or password is incorrect.', 'cooldown': COOLDOWN_TEXT}.get(result, 'Too many attempts. Try again in one minute.')
                self.login_page(next_path, text, 200 if result == 'fail' else 429)
                return
            token = self.server.auth.new_session(username)
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
        is_project_api = bool(re.fullmatch(r'/api/projects(?:/tcp-[a-z0-9]{4})?', path)) or path == '/api/board'
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
    ap.add_argument('--auth', required=True, help='the single login used until the users file exists')
    ap.add_argument('--users', help='the users file of awb web user (default: users.json next to --auth)')
    ap.add_argument('--signin-log', help='where every sign-in attempt is logged (time, login, result)')
    ap.add_argument('--domain', required=True, help='the host name of the site, without scheme')
    ap.add_argument('--index', required=True)
    ap.add_argument('--ports', nargs='+', type=int, default=[8080, 8081])
    ap.add_argument('--portal-port', type=int, default=8180)
    ap.add_argument('--ask-port', type=int, default=8181)
    ap.add_argument('--projects-port', type=int, default=8182)
    ap.add_argument('--tenants-port', type=int, default=8183)
    args = ap.parse_args()
    auth = Auth(args.auth, args.users, args.signin_log)
    servers = [make_server(port, auth, args.index, args.portal_port, args.ask_port, args.projects_port, args.tenants_port, domain=args.domain) for port in args.ports]
    for server in servers[1:]:
        threading.Thread(target=server.serve_forever, daemon=True).start()
    print('AWB authenticated gateway ready', flush=True)
    servers[0].serve_forever()


if __name__ == '__main__':
    main()
