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
import ipaddress
import secrets
import json
import os
import re
import pwd
import socket
import socketserver
import stat
import struct
import sys
import threading
import time
from collections import OrderedDict, deque, namedtuple
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit, parse_qs, urlencode

MAX_BODY = 16_384
MAX_UPLOAD = 25 * 1024 * 1024   # one file of the owner page into a project's in/: bucket_layout.MAX_UPLOAD
UPLOAD_PATH = re.compile(r'/api/projects/tcp-[a-z2-7]{4}/materials/upload')
MAX_RESPONSE = 12 * 1024 * 1024
BACKEND_ROUTES = {'/ask'}   # the server-rendered pages; the portal's pages went with it (F1 step 10)


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
BUSY_TEXT = 'The sign-in is busy. Try again in a moment.'
LOG_MAX = 2*1024*1024
REFUSED = ('limited', 'cooldown', 'site-cap', 'owner-limited', 'owner-delayed',   # cheap tries, logged counted
           'slots', 'slow')                                                        # a slot refused, a timeout
REFUSED_WINDOW = 60         # one line per address key and result within this many seconds, the rest counted
SEVERAL = '(several logins)'
WAITS_KEPT = 2048           # logins kept in the state file of the waits, the newest first
DEFAULT_ROUNDS = 600_000


class Limiter:
    """The per-login wait and the site-wide cap (F4). Pure: every call gets the time, the log replays into it.
    A sign-in reserves its try (`reserve`) before the pbkdf2 and gives it back (`release`) after it, so parallel
    guesses for one login never pass the series together (finding 11 of the review of 2026-10-10)."""

    def __init__(self):
        self.logins = {}        # key -> {'fails': [t], 'until': t, 'level': n, 'last': t}
        self.site = deque()
        self.pending = {}       # key -> tries of that key in flight (reserved before the pbkdf2)

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
        """'site', 'login' or '' (free to try). A try in flight counts as a failure until it is released."""
        if self.site_closed_until(now) or len(self.site) + sum(self.pending.values()) >= SITE_CAP:
            return 'site'
        if key is None:
            return ''
        if self.waits_until(key, now):
            return 'login'
        state = self.logins.get(key)
        recent = sum(1 for t in state['fails'] if now-t < SERIES_WINDOW and t >= state['until']) if state else 0
        return 'login' if recent + self.pending.get(key, 0) >= SERIES else ''

    def reserve(self, key):
        self.pending[key] = self.pending.get(key, 0) + 1

    def release(self, key):
        left = self.pending.get(key, 0) - 1
        if left > 0:
            self.pending[key] = left
        else:
            self.pending.pop(key, None)

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
        """Forget the login's failures; True when there were any."""
        return self.logins.pop(key, None) is not None

    def snapshot(self, now):
        """The state worth keeping across a restart (finding 13 of the review of 2026-10-10): the last SITE_CAP
        failures of the site and per login its running wait, its level and the failures of its current series, at
        most WAITS_KEPT logins, the running waits first, then the newest."""
        self._prune(now)
        kept = sorted(self.logins.items(), key=lambda kv: (kv[1]['until'] > now, kv[1]['last']), reverse=True)
        return {'version': 1, 'site': list(self.site)[-SITE_CAP:],
                'logins': {k: {'fails': v['fails'][-SERIES:], 'until': v['until'], 'level': v['level'],
                               'last': v['last']} for k, v in kept[:WAITS_KEPT]}}

    def merge(self, data):
        """Take the state a snapshot kept: its site failures, and per login the entry with the later wait."""
        def number(x):
            if isinstance(x, bool) or not isinstance(x, (int, float)) or not 0 <= x < 1e11:
                raise ValueError
            return float(x)
        site = [number(t) for t in data['site']][-SITE_CAP:]
        logins = {}
        for key, v in data['logins'].items():
            if not isinstance(key, str) or len(key) > 100:
                raise ValueError
            logins[key] = {'fails': [number(t) for t in v['fails']][-SERIES:], 'until': number(v['until']),
                           'level': int(number(v['level'])), 'last': number(v['last'])}
        if len(site) >= len(self.site):
            self.site = deque(sorted(site))
        for key, v in list(logins.items())[:WAITS_KEPT]:
            have = self.logins.get(key)
            if have is None or (v['until'], v['level'], v['last']) > (have['until'], have['level'], have['last']):
                self.logins[key] = v

    def cooldowns(self, now):
        self._prune(now)
        return {k: s['until'] for k, s in self.logins.items() if s['until'] > now}


TOTP_STEP = 30
TOTP_DIGITS = 6


def totp_code(secret, step):
    """The RFC 6238 code of a base32 secret for one time step (HMAC-SHA1, 6 digits)."""
    key = base64.b32decode(secret.upper() + '=' * (-len(secret) % 8))
    mac = hmac.new(key, struct.pack('>Q', step), 'sha1').digest()
    offset = mac[-1] & 15
    return '%0*d' % (TOTP_DIGITS, (struct.unpack('>I', mac[offset:offset+4])[0] & 0x7fffffff) % 10**TOTP_DIGITS)


def totp_step(secret, code, now, skew=1):
    """The time step `code` belongs to within one step of skew, or None."""
    if not secret or not isinstance(code, str) or not re.fullmatch(r'\d{%d}' % TOTP_DIGITS, code):
        return None
    current = int(now // TOTP_STEP)
    for step in range(current - skew, current + skew + 1):
        if hmac.compare_digest(totp_code(secret, step), code):
            return step
    return None


class Delays:
    """The delays of the owner host (design section 2 item 4): after each failure of a key its next allowed time
    moves 1, 2, 4 ... 60 seconds ahead; a try before it is answered like a wrong password without a pbkdf2, is not
    counted and does not move the time. Never a hard lock. sign_in_owner keys a delay on the name and the address
    key, so a stranger delays only his own address (finding 4 of the review of 2026-10-10), and keeps one more per
    name that only a wrong code after the right password starts. The times live in a fixed array indexed by a keyed
    hash, so a flood of junk keys cannot evict a real one (it can at worst share a cell and delay it, never longer
    than 60 seconds)."""
    CELLS = 65536
    MAX = 60

    def __init__(self):
        self.key = secrets.token_bytes(32)
        self.until = [0.0] * self.CELLS
        self.level = [0] * self.CELLS

    def cell(self, name):
        return int.from_bytes(hmac.new(self.key, name.encode('utf-8', 'replace'), 'sha256').digest()[:4], 'big') % self.CELLS

    def waiting(self, name, now):
        return self.until[self.cell(name)] > now

    def failure(self, name, now):
        i = self.cell(name)
        self.until[i] = now + min(2 ** self.level[i], self.MAX)
        self.level[i] = min(self.level[i] + 1, 6)

    def success(self, name):
        i = self.cell(name)
        self.until[i], self.level[i] = 0.0, 0


def log_time(t):
    return time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime(t))


def read_log(path, since, counted=False):
    """The entries (time, login, result) of the sign-in log and its rotated copy at or after `since`, oldest first;
    with `counted` (time, login, result, tries), where a counted line of refused tries stands for its count."""
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
                tries = int(entry.get('count', 1))
            except (ValueError, KeyError, TypeError, AttributeError):
                continue
            if t >= since:
                out.append((t, login, result, max(tries, 1)) if counted else (t, login, result))
    out.sort(key=lambda e: e[0])
    return out


def load_waits(path, limiter):
    """Merge the state file of the waits into `limiter`; a missing or broken file adds nothing."""
    try:
        data = json.loads(Path(path).read_text(encoding='utf-8'))
        if data.get('version') != 1:
            raise ValueError
        limiter.merge(data)
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        pass
    return limiter


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


# projects: the codes a reader may open (`awb web user projects`); empty means every project, as before the list
Account = namedtuple('Account', 'salt hash rounds level totp generation projects', defaults=((),))
LEVELS = ('reader', 'owner')
PROJECT_CODE = re.compile(r'tcp-[a-z0-9]{4}')
PROJECT_PATH = re.compile(r'/api/projects/(tcp-[a-z0-9]{4})(?:/|$)')     # every route of one project
PROJECT_LISTS = ('/api/projects', '/api/board')                           # the routes that list projects by code
SLOT_WAIT = 3.0             # a sign-in waits this long for a free pbkdf2 slot, then answers busy (never counted)
UNAUTHENTICATED = 64        # requests without a session at once
PER_ADDRESS = 4             # of them per address key; the next answers 429 before a byte of its body is read
UNAUTHENTICATED_TIMEOUT = 5  # seconds for the headers of any request and the body of one without a session
AUTHENTICATED_TIMEOUT = 20   # seconds for the body of a request with a session
SESSIONS_PER_USER = 5
SESSION_TABLE = 128
READER_TTL = 8*3600
OWNER_IDLE = 15*60
OWNER_MAX = 3600
OWNER_TABLE = 8


def address_key(client):
    """The key of the per-address limit: the /64 of an IPv6 address, an IPv4 address whole, anything else as given."""
    try:
        ip = ipaddress.ip_address(client.strip())
    except ValueError:
        return client[:100]
    if ip.version == 6:
        if ip.ipv4_mapped:
            return str(ip.ipv4_mapped)
        return str(ipaddress.ip_network(str(ip) + '/64', strict=False))
    return str(ip)


def parse_cookie(header, name):
    """The value of the first cookie called exactly `name`; a malformed neighbour never hides it (RT-20)."""
    for part in (header or '').split(';'):
        key, sep, value = part.strip().partition('=')
        if sep and key.strip() == name:
            value = value.strip()
            if len(value) >= 2 and value[0] == value[-1] == '"':
                value = value[1:-1]
            return value
    return ''


class Auth:
    """The sign-in. The users file (per login a salt and PBKDF2 digest, a level, a generation and for an owner entry
    a TOTP secret; `awb web user`) is read on every change; while it does not exist the single login of `path`
    works as before, with the per-address limit only."""

    def __init__(self, path, users=None, log=None, clock=time.time):
        self.path = Path(path)
        self.users_path = Path(users) if users else self.path.parent / 'users.json'
        self.log_path = Path(log) if log else None
        self.clock = clock
        self.lock = threading.Lock()
        self.log_lock = threading.Lock()
        self.failures = OrderedDict()
        self.inflight = set()
        self.cache_key = secrets.token_bytes(32)
        self.slots = threading.BoundedSemaphore(4)
        self.unauthenticated = threading.BoundedSemaphore(UNAUTHENTICATED)
        self.holds = {}                 # address key -> its requests without a session in flight (hold)
        self.holds_lock = threading.Lock()
        self.used_csrf = OrderedDict()
        self.sessions = OrderedDict()
        self.owners = {}
        self.stamp = None
        self.users = None
        self.legacy = None
        self.username = None
        if self.path.exists() or not self.users_path.exists():
            data = json.loads(self.path.read_text())
            self.username = data['username']
            self.legacy = {data['username']: Account(bytes.fromhex(data['salt']), bytes.fromhex(data['hash']),
                                                     data['rounds'], 'reader', None, 0)}
        now = clock()
        self.limiter = replay(read_log(self.log_path, now-2*MAX_WAIT)) if self.log_path else Limiter()
        # the waits also live in a small state file next to the log, written on every change, so a flood of the
        # log or its rotation never lets a restart forget one (finding 13 of the review of 2026-10-10)
        self.waits_path = self.log_path.parent / 'waits.json' if self.log_path else None
        self.waits_lock = threading.Lock()
        self.waits_seq = self.waits_written = 0
        if self.waits_path:
            load_waits(self.waits_path, self.limiter)
        self.refused_lines = OrderedDict()      # (address key, result) -> [window start, tries counted, login]
        self.delays = Delays()
        self.owner_slots = threading.BoundedSemaphore(2)
        self.owner_failures = {}
        self.owner_inflight = set()
        self.owner_sessions = OrderedDict()
        self.used_path = self.log_path.parent / 'totp-used.json' if self.log_path else None
        self.used = self.load_used(now)

    def accounts(self):
        """(per-user mode, {login: Account}), the users file read again when it changed. A users file that cannot
        be read lets nobody in."""
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
            users = {}
            for login, u in data['users'].items():
                level = u.get('level', 'reader')
                projects = u.get('projects') or []
                if not LOGIN_RE.fullmatch(login) or level not in LEVELS:
                    continue
                if not isinstance(projects, list) or not all(isinstance(c, str) and PROJECT_CODE.fullmatch(c)
                                                             for c in projects):
                    continue        # a project list that is not a list of codes: the login stays out, like a bad level
                users[login] = Account(bytes.fromhex(u['salt']), bytes.fromhex(u['hash']), int(u['rounds']), level,
                                       u.get('totp') if level == 'owner' else None, int(u.get('generation', 0)),
                                       tuple(projects))
        except (OSError, ValueError, KeyError, TypeError, AttributeError):
            print('AWB gateway: the users file cannot be read; nobody signs in', flush=True)
            users = {}
        with self.lock:
            self.stamp, self.users = stamp, users
        return True, users

    def log(self, now, login, result, client=None):
        """One line per attempt. A refused try (REFUSED, no pbkdf2) of an address key writes the first line of its
        window of REFUSED_WINDOW seconds and is counted after that; the count goes out as one line with `count`
        once the window is over, so cheap tries cannot flood the log (finding 13 of the review of 2026-10-10)."""
        if not self.log_path:
            return

        def line(t, name, word, count=None):
            shown = name if name == SEVERAL or LOGIN_RE.fullmatch(name) else NOT_A_LOGIN
            entry = {'time': log_time(t), 'login': shown, 'result': word}
            if count:
                entry['count'] = count
            return json.dumps(entry) + '\n'

        with self.log_lock:
            lines = []
            # every window that is over goes out first, as one line with its count
            for key in [k for k, w in self.refused_lines.items() if now - w[0] >= REFUSED_WINDOW]:
                begun, count, name = self.refused_lines.pop(key)
                if count:
                    lines.append(line(begun, name, key[1], count))
            key = (client, result) if result in REFUSED and client is not None else None
            if key is None:
                lines.append(line(now, login, result))
            elif key in self.refused_lines:
                window = self.refused_lines[key]
                window[1] += 1
                window[2] = login if window[2] in (None, login) else SEVERAL
            else:
                self.refused_lines[key] = [now, 0, None]
                lines.append(line(now, login, result))
                while len(self.refused_lines) > 2048:
                    (_, word), (begun, count, name) = self.refused_lines.popitem(last=False)
                    if count:
                        lines.append(line(begun, name, word, count))
            if not lines:
                return
            try:
                if self.log_path.exists() and self.log_path.stat().st_size > LOG_MAX:
                    os.replace(self.log_path, str(self.log_path)+'.1')
                fd = os.open(self.log_path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
                with os.fdopen(fd, 'a', encoding='utf-8') as fh:
                    fh.write(''.join(lines))
            except OSError:
                print('AWB gateway: the sign-in log cannot be written', flush=True)

    def save_waits(self, snapshot, seq):
        """Write a snapshot of the limiter to the state file, never an older one over a newer one."""
        if not self.waits_path:
            return
        with self.waits_lock:
            if seq <= self.waits_written:
                return
            tmp = self.waits_path.with_name('.waits.next')
            try:
                fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
                with os.fdopen(fd, 'w', encoding='utf-8') as fh:
                    json.dump(snapshot, fh)
                os.replace(tmp, self.waits_path)
                self.waits_written = seq
            except OSError:
                print('AWB gateway: the waits cannot be written', flush=True)

    def password(self, account, password, rounds=DEFAULT_ROUNDS):
        """The PBKDF2 of one attempt in a slot: True, False, or None when no slot came free within SLOT_WAIT. An
        unknown login runs the same PBKDF2 with `rounds` and is False."""
        if not self.slots.acquire(timeout=SLOT_WAIT):
            return None
        try:
            salt, digest, rounds = (account.salt, account.hash, account.rounds) if account else (
                b'\0'*16, b'\0'*32, rounds)
            computed = hashlib.pbkdf2_hmac('sha256', password.encode('utf-8', 'replace'), salt, rounds)
            return account is not None and hmac.compare_digest(computed, digest)
        finally:
            self.slots.release()

    def sign_in(self, login, password, client):
        """The main host's sign-in: 'ok', 'fail' (wrong password or unknown login, the same answer), 'cooldown'
        (the login waits or the site is closed), 'limited' (this address key failed 8 times within a minute or has
        a sign-in in flight) or 'busy' (no free slot within SLOT_WAIT; not counted). An owner entry is a login like
        any other here, cooldown and site cap included, so its name answers exactly like an unknown one (finding 10
        of the review of 2026-10-10); the owner host has its own sign-in and its own delay (sign_in_owner)."""
        login = login[:100]
        now = self.clock()
        per_user, accounts = self.accounts()
        key = login if LOGIN_RE.fullmatch(login) else 'h:'+hashlib.sha256(login.encode()).hexdigest()[:24]
        client = address_key(client)
        with self.lock:
            recent = [t for t in self.failures.get(client, []) if now-t < 60]
            if len(recent) >= 8 or client in self.inflight:
                result = 'limited'
            elif per_user and self.limiter.refused(key, now):
                result = 'cooldown'
                self.failures[client] = recent + [now]
            else:
                result = ''
                # the failure is counted before the pbkdf2 starts and taken back on a success or a busy slot: for
                # the address key here, for the login by the reservation of the limiter
                self.inflight.add(client)
                if per_user:
                    self.limiter.reserve(key)
                self.failures[client] = recent + [now]
                self.failures.move_to_end(client)
                while len(self.failures) > 2048:
                    self.failures.popitem(last=False)
        if result:
            self.log(now, login, result if result != 'cooldown' else
                     ('site-cap' if self.limiter.site_closed_until(now) else 'cooldown'), client)
            return result
        valid = None
        changed = None
        try:
            account = accounts.get(login)
            valid = self.password(account, password,
                                  next(iter(accounts.values())).rounds if accounts else DEFAULT_ROUNDS)
        finally:
            with self.lock:
                self.inflight.discard(client)
                if per_user:
                    self.limiter.release(key)
                    # the outcome is recorded under the same lock as the release, so no other try sees neither
                    if (valid and self.limiter.success(key)) or valid is False:
                        if valid is False:
                            self.limiter.failure(key, now)
                        self.waits_seq += 1
                        changed = (self.limiter.snapshot(now), self.waits_seq)
        if changed:
            self.save_waits(*changed)
        with self.lock:
            if valid is None or valid:
                kept = self.failures.get(client, [])
                if now in kept:
                    kept.remove(now)
            if valid:
                self.failures.pop(client, None)
        if valid is None:
            self.log(now, login, 'busy')
            return 'busy'
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

    def hold(self, client):
        """A slot for a request without a session, taken after its headers and before its body: '' when taken,
        'slots' when its address key (address_key) already holds PER_ADDRESS of them (logged counted, one line per
        key and minute, as the refused tries are) or 'busy' when all UNAUTHENTICATED are held. A slow body of one
        address key can thus hold four slots, never the site (n8). release_hold gives the slot back."""
        key = address_key(client)
        with self.holds_lock:
            if self.holds.get(key, 0) >= PER_ADDRESS:
                result = 'slots'
            elif not self.unauthenticated.acquire(blocking=False):
                result = 'busy'
            else:
                self.holds[key] = self.holds.get(key, 0) + 1
                return ''
        if result == 'slots':
            self.log(self.clock(), NOT_A_LOGIN, 'slots', key)
        return result

    def release_hold(self, client):
        key = address_key(client)
        with self.holds_lock:
            left = self.holds.get(key, 0) - 1
            if left > 0:
                self.holds[key] = left
            else:
                self.holds.pop(key, None)
        self.unauthenticated.release()

    def timed_out(self, client):
        """A request whose headers or body did not arrive within its timeout: logged counted by address key, as a
        refused try is, never with a byte of what arrived."""
        self.log(self.clock(), NOT_A_LOGIN, 'slow', address_key(client))

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

    def use_csrf(self, token):
        """A valid login token, taken once: a second use of the same token is refused (RT2-01)."""
        if not self.valid_csrf(token):
            return False
        now = time.time()
        with self.lock:
            while self.used_csrf and next(iter(self.used_csrf.values())) < now-600:
                self.used_csrf.popitem(last=False)
            if token in self.used_csrf:
                return False
            self.used_csrf[token] = now
            while len(self.used_csrf) > 20000:
                self.used_csrf.popitem(last=False)
        return True

    def new_session(self, login=None):
        """A session token, or None when the table is full. One bound to a login ends when that login is removed,
        gets a new password or a new generation; a login keeps at most SESSIONS_PER_USER, its oldest goes first;
        a full table refuses and never evicts another user's session (RT-19)."""
        token = secrets.token_urlsafe(32)
        key = hashlib.sha256(token.encode()).digest()
        bound = None
        if login is not None:
            account = self.accounts()[1].get(login)
            bound = (login, account.hash if account else b'', account.level if account else 'reader',
                     account.generation if account else 0)
        with self.lock:
            now = time.monotonic()
            self.sessions = OrderedDict((k, v) for k, v in self.sessions.items() if v > now)
            self.owners = {k: v for k, v in self.owners.items() if k in self.sessions}
            if bound:
                mine = [k for k in self.sessions if k in self.owners and self.owners[k][0] == login]
                for old in mine[:max(0, len(mine) - SESSIONS_PER_USER + 1)]:
                    self.sessions.pop(old, None)
                    self.owners.pop(old, None)
            if len(self.sessions) >= SESSION_TABLE:
                return None
            self.sessions[key] = now + READER_TTL
            if bound:
                self.owners[key] = bound
        return token

    def session_user(self, token):
        """(login, level) of a valid session, ('', 'reader') for an unbound one, None when not valid."""
        if not token or len(token) > 100:
            return None
        key = hashlib.sha256(token.encode()).digest()
        with self.lock:
            expires = self.sessions.get(key, 0)
            if expires <= time.monotonic():
                self.sessions.pop(key, None)
                return None
            bound = self.owners.get(key)
        per_user, accounts = self.accounts()
        if bound is None:
            return None if per_user else ('', 'reader')
        account = accounts.get(bound[0])
        if account and hmac.compare_digest(account.hash, bound[1]) and account.generation == bound[3]:
            return bound[0], account.level
        self.revoke_session(token)
        return None

    def session_valid(self, token):
        return self.session_user(token) is not None

    def projects_of(self, login):
        """The codes the login may open, read from the users file as it is now; () for every project (an empty or
        missing list, the single login)."""
        account = self.accounts()[1].get(login) if login else None
        return account.projects if account else ()

    # ------------------------------------------------------------------------------------- the owner level

    def load_used(self, now):
        """{login: [time steps]} of the TOTP codes used and not yet over, kept across a restart."""
        if not self.used_path:
            return {}
        try:
            data = json.loads(self.used_path.read_text())
            current = int(now // TOTP_STEP)
            return {k: [int(s) for s in v if int(s) >= current - 1] for k, v in data.items() if LOGIN_RE.fullmatch(k)}
        except (OSError, ValueError, TypeError, AttributeError):
            return {}

    def use_code(self, login, step, now):
        """Take a TOTP step for `login` once: False when it was used before (also before a restart)."""
        current = int(now // TOTP_STEP)
        with self.lock:
            steps = [s for s in self.used.get(login, []) if s >= current - 1]
            if step in steps:
                return False
            self.used[login] = steps + [step]
            snapshot = json.dumps(self.used)
        if self.used_path:
            try:
                tmp = self.used_path.with_name('.totp-used.next')
                fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
                with os.fdopen(fd, 'w') as fh:
                    fh.write(snapshot)
                os.replace(tmp, self.used_path)
            except OSError:
                print('AWB gateway: the used codes cannot be written', flush=True)
        return True

    def sign_in_owner(self, login, password, code, client):
        """The owner host's sign-in: an owner entry's password, then a current TOTP code that was not used before.
        'ok', 'fail' (a wrong password, a wrong or used code and a waiting try answer alike), 'limited' (this
        address key failed 8 times within a minute or has a sign-in in flight) or 'busy'. Every failure moves the
        next try of this name from this address key 1 ... 60 seconds ahead, so a stranger's wrong passwords delay
        his own address and never the owner's (finding 4 of the review of 2026-10-10); a wrong code after the right
        password also delays the name from every address, so whoever holds the password guesses at most 60 codes
        an hour (the owner's way out is `awb web user reset`). A try inside a delay costs no pbkdf2 and moves
        nothing; the right password and a current code always pass once the delays of the try are over. The code
        counts only after the password matched. The owner host has its own PBKDF2 slots."""
        login = login[:100]
        now = self.clock()
        per_user, accounts = self.accounts()
        account = accounts.get(login) if per_user else None
        if account is not None and account.level != 'owner':
            account = None
        client = address_key(client)
        name = 'owner:' + login + '\0' + client
        code_name = 'owner-code:' + login
        with self.lock:
            recent = [t for t in self.owner_failures.get(client, []) if now-t < 60]
            if len(recent) >= 8 or client in self.owner_inflight:
                result = 'limited'
            elif self.delays.waiting(name, now) or self.delays.waiting(code_name, now):
                result = 'delayed'
            else:
                result = ''
                self.owner_inflight.add(client)
                self.owner_failures[client] = recent + [now]
                while len(self.owner_failures) > 2048:
                    self.owner_failures.pop(next(iter(self.owner_failures)))
        if result:
            self.log(now, login, 'owner-' + result, client)
            return 'fail' if result == 'delayed' else result
        try:
            if not self.owner_slots.acquire(timeout=SLOT_WAIT):
                valid = None
            else:
                try:
                    salt, digest, rounds = (account.salt, account.hash, account.rounds) if account else (
                        b'\0'*16, b'\0'*32, next(iter(accounts.values())).rounds if accounts else DEFAULT_ROUNDS)
                    computed = hashlib.pbkdf2_hmac('sha256', password.encode('utf-8', 'replace'), salt, rounds)
                    valid = account is not None and hmac.compare_digest(computed, digest)
                finally:
                    self.owner_slots.release()
        finally:
            with self.lock:
                self.owner_inflight.discard(client)
        word = 'owner-ok'
        if valid:
            step = totp_step(account.totp, code, now)
            if step is None or not self.use_code(login, step, now):
                valid, word = False, 'owner-code'
        elif valid is False:
            word = 'owner-fail'
        with self.lock:
            if valid is None or valid:
                kept = self.owner_failures.get(client, [])
                if now in kept:
                    kept.remove(now)
            if valid:
                self.owner_failures.pop(client, None)
                self.delays.success(name)
                self.delays.success(code_name)
            elif valid is False:
                self.delays.failure(name, now)
                if word == 'owner-code':
                    self.delays.failure(code_name, now)
        if valid is None:
            self.log(now, login, 'owner-busy')
            return 'busy'
        self.log(now, login, word)
        return 'ok' if valid else 'fail'

    def new_owner_session(self, login):
        """An owner session: 15 minutes idle and one hour at most; None when the owner table is full."""
        account = self.accounts()[1].get(login)
        if account is None or account.level != 'owner':
            return None
        token = secrets.token_urlsafe(32)
        key = hashlib.sha256(token.encode()).digest()
        with self.lock:
            now = time.monotonic()
            self.owner_sessions = OrderedDict((k, v) for k, v in self.owner_sessions.items()
                                              if now - v['seen'] < OWNER_IDLE and now - v['created'] < OWNER_MAX)
            mine = [k for k, v in self.owner_sessions.items() if v['login'] == login]
            for old in mine[:max(0, len(mine) - SESSIONS_PER_USER + 1)]:
                self.owner_sessions.pop(old, None)
            if len(self.owner_sessions) >= OWNER_TABLE:
                return None
            self.owner_sessions[key] = {'login': login, 'hash': account.hash, 'generation': account.generation,
                                        'created': now, 'seen': now}
        return token

    def owner_session(self, token):
        """The login of a valid owner session (its idle time starts again), else None."""
        if not token or len(token) > 100:
            return None
        key = hashlib.sha256(token.encode()).digest()
        now = time.monotonic()
        with self.lock:
            s = self.owner_sessions.get(key)
            if s is None:
                return None
            if now - s['seen'] >= OWNER_IDLE or now - s['created'] >= OWNER_MAX:
                self.owner_sessions.pop(key, None)
                return None
        account = self.accounts()[1].get(s['login'])
        if account is None or account.level != 'owner' or not hmac.compare_digest(account.hash, s['hash']) \
                or account.generation != s['generation']:
            self.revoke_owner_session(token)
            return None
        with self.lock:
            s['seen'] = now
        return s['login']

    def revoke_owner_session(self, token):
        with self.lock:
            self.owner_sessions.pop(hashlib.sha256((token or '').encode()).digest(), None)

    def owner_csrf(self, token):
        """A CSRF token bound to one owner session, valid for 10 minutes."""
        stamp = str(int(time.time()))
        bound = hashlib.sha256((token or '').encode()).hexdigest()
        return stamp + '.' + hmac.new(self.cache_key, ('owner\0' + bound + '\0' + stamp).encode(), 'sha256').hexdigest()

    def valid_owner_csrf(self, token, value):
        try:
            stamp, sig = value.split('.')
            bound = hashlib.sha256((token or '').encode()).hexdigest()
            expected = hmac.new(self.cache_key, ('owner\0' + bound + '\0' + stamp).encode(), 'sha256').hexdigest()
            return 0 <= time.time() - int(stamp) < 600 and hmac.compare_digest(sig, expected)
        except (ValueError, TypeError, AttributeError):
            return False

    def revoke_session(self, token):
        key = hashlib.sha256(token.encode()).digest()
        with self.lock:
            self.sessions.pop(key, None)
            self.owners.pop(key, None)


OWNER_COOKIE = '__Host-awb-owner'
OWNER_LOGIN_COOKIE = '__Host-awb-owner-login'
OWNER_FAIL_TEXT = 'The username, password or code is incorrect.'
OWNER_ROUTES = (
    # the one table of the owner routes (F1 step 9): method, path, the socket of the server it goes to and whether
    # the query string goes along. owner_actions answers the rows of owner_socket and reads them from here.
    ('GET', '/api/owner/state', 'owner_socket', False),
    ('GET', '/api/owner/ui-runs', 'owner_socket', False),
    ('GET', '/api/owner/intake', 'owner_socket', False),
    ('GET', '/api/ui-runs', 'owner_socket', False),
    ('POST', '/api/owner/publish', 'owner_socket', False),
    # the IAM domain names: on the tenant service's owner socket, which answers this uid alone (finding 14)
    ('GET', '/api/owner/tenants', 'owner_tenants_socket', True),
)
OWNER_PATHS = frozenset(path for _, path, _, _ in OWNER_ROUTES)
OWNER_ACTIONS = tuple(path for method, path, sock, _ in OWNER_ROUTES if method == 'GET' and sock == 'owner_socket')
OWNER_ONLY_TEXT = b'{"error":"Owner only."}'
OWNER_POSTS = tuple(path for method, path, _, _ in OWNER_ROUTES if method == 'POST')


def restrict(answer, only):
    """The JSON answer as bytes, its `projects` rows cut to the codes in `only` when `only` is given. A row without
    a code of the list is dropped, so a reader with a list never sees another code."""
    if only:
        rows = answer.get('projects')
        if not isinstance(rows, list):
            raise ValueError('no project rows')
        answer = {**answer, 'projects': [r for r in rows if isinstance(r, dict) and r.get('code') in only]}
    return json.dumps(answer).encode()


def owner_route_of(method, path):
    """(socket attribute, query goes along) of an owner route; None for any other. A HEAD is the GET."""
    method = 'GET' if method == 'HEAD' else method
    for m, p, sock, query in OWNER_ROUTES:
        if (m, p) == (method, path):
            return sock, query
    return None


def is_moved(path, method):
    """The routes that show or take customer data or create something: on the owner host only (T9 step 2)."""
    return (path == '/api/customers' or (path == '/api/projects' and method == 'POST')
            or bool(re.fullmatch(r'/api/(?:customer|project)-operations/[a-f0-9]{32}', path))
            or bool(re.fullmatch(r'/api/projects/tcp-[a-z2-7]{4}/materials(?:/(?:sources|imports|upload|M-[A-Z]{24}))?', path)))


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
        # the session is known only after the headers, so every request reads them within the short timeout; a
        # request with a session gets AUTHENTICATED_TIMEOUT for its body in dispatch (n8)
        self.connection.settimeout(UNAUTHENTICATED_TIMEOUT)

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
        return parse_cookie(self.headers.get('Cookie', ''), name)

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

    def read_form(self, origin=None):
        origin = origin or 'https://' + self.server.domain
        if self.headers.get('Origin') != origin or self.headers.get('Sec-Fetch-Site') not in {None, 'same-origin', 'none'}:
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
        except socket.timeout:
            self.timed_out()
            return None
        except (OSError, ValueError, UnicodeError):
            self.reply(400, b'Invalid form.\n')
            return None

    def read_body(self, expected_type, limit=MAX_BODY):
        """The body of a POST after the length and type checks, or None when a refusal was sent."""
        lengths = self.headers.get_all('Content-Length', [])
        if self.headers.get('Transfer-Encoding') or len(lengths) != 1:
            self.reply(400, b'A single content length is required.\n')
            return None
        try:
            length = int(lengths[0])
        except ValueError:
            length = -1
        if not 0 <= length <= limit:
            self.reply(413, b'Request is too large.\n')
            return None
        if self.headers.get('Content-Type', '').split(';')[0].lower() != expected_type:
            self.reply(415, b'Use the Ask form.\n')
            return None
        try:
            body = self.rfile.read(length)
        except socket.timeout:
            self.timed_out()
            return None
        except OSError:
            self.reply(408, b'Request timed out.\n')
            return None
        if len(body) != length:
            self.reply(400, b'Incomplete request.\n')
            return None
        return body

    def timed_out(self, reply=True):
        """The body (`reply`: 408) or the headers of a request did not arrive within the timeout of the
        connection: one counted log line for the address key, never a byte of what arrived (n8)."""
        self.refusal = 'slow'
        self.server.auth.timed_out(self.client_key())
        if reply:
            self.reply(408, b'Request timed out.\n')

    def dispatch(self):
        self.refusal, self.level, self.host_label = '', '-', 'main'
        self.user = self.owner_login = self.only = None
        owner_side = bool(self.server.owner_host) and self.headers.get('Host', '').lower() == self.server.owner_host
        if owner_side:
            self.host_label = 'owner'
            session = self.cookie(OWNER_COOKIE)
            self.owner_login = self.server.auth.owner_session(session)
            signed_in = self.owner_login is not None
        else:
            session = self.cookie('__Host-awb-session')
            self.user = self.server.auth.session_user(session)
            signed_in = self.user is not None
        if signed_in:
            self.level = 'owner' if owner_side else self.user[1]
            self.connection.settimeout(AUTHENTICATED_TIMEOUT)
            return self.owner_route(session) if owner_side else self.route(session)
        peer = self.client_key()
        refused = self.server.auth.hold(peer)
        if refused:
            # before a byte of the body: the address key holds PER_ADDRESS slots already (429) or the site holds
            # all UNAUTHENTICATED (503); the owner host's own limiter is not touched
            self.refusal = refused
            self.reply(429 if refused == 'slots' else 503, b'The site is busy. Try again in a moment.\n',
                       {'Retry-After': '5'})
            return
        try:
            return self.owner_route(session) if owner_side else self.route(session)
        finally:
            self.server.auth.release_hold(peer)

    def client_key(self):
        """The client address for the limiter: CF-Connecting-IP only on a listener whose one peer is the tunnel's
        user, checked before a byte was read; anywhere else the TCP peer, whatever the header says (finding 12 of
        the review of 2026-10-10: a local client picked a new address key per try)."""
        if isinstance(self.client_address, tuple) and self.client_address:
            fallback = str(self.client_address[0])
        else:
            fallback = 'local'
        if getattr(self.server, 'peer', None) is None or getattr(self, 'headers', None) is None:
            # no headers yet: they did not arrive within the timeout and the peer is all there is
            return fallback[:100]
        return self.headers.get('CF-Connecting-IP', fallback)[:100]

    def first_checks(self):
        """The method and the request target; the path, or None when a refusal was sent."""
        v1 = self.server.v1_marker
        if v1 and self.headers.get('X-AWB-V1') == v1:
            print('v1: CF-Connecting-IP %s' % ('arrived as the client sent it' if self.headers.get('CF-Connecting-IP') == v1
                                               else 'was replaced by the edge'), flush=True)
        if self.command not in {'GET', 'HEAD', 'POST'}:
            self.reply(405, b'Method not allowed.\n', {'Allow': 'GET, HEAD, POST'})
            return None
        url = urlsplit(self.path)
        if url.scheme or url.netloc or self.path.startswith('//'):
            self.reply(400, b'Invalid request target.\n')
            return None
        if url.path == '/favicon.ico':
            self.reply(204)
            return None
        return url

    def route(self, session):
        if self.headers.get('Host', '').lower() != self.server.domain:
            self.reply(400, b'Invalid host.\n')
            return
        url = self.first_checks()
        if url is None:
            return
        path = url.path
        peer = self.client_key()
        authenticated = self.user is not None
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
            if not csrf or not hmac.compare_digest(csrf.encode(), self.cookie('__Host-awb-login').encode()) or not self.server.auth.use_csrf(csrf):
                self.login_page(error='The sign-in form expired. Please try again.', status=403)
                return
            username = (form.get('username') or [''])[0]
            password = (form.get('password') or [''])[0]
            next_path = self.safe_next((form.get('next') or ['/'])[0])
            result = self.server.auth.sign_in(username, password, peer)
            if result != 'ok':
                text = {'fail': 'The username or password is incorrect.', 'cooldown': COOLDOWN_TEXT,
                        'busy': BUSY_TEXT}.get(result, 'Too many attempts. Try again in one minute.')
                if result != 'fail':
                    self.refusal = 'busy' if result == 'busy' else 'limited'
                self.login_page(next_path, text, {'fail': 200, 'busy': 503}.get(result, 429))
                return
            token = self.server.auth.new_session(username)
            if token is None:
                self.refusal = 'busy'
                self.login_page(next_path, 'The workspace has too many open sessions. Try again later.', 503)
                return
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
        if path.startswith('/api/owner/') or is_moved(path, self.command):
            # T9 step 2: the owner routes and the routes that show or take customer data live on the owner host
            self.reply(404, b'Not found.\n')
            return
        if path in OWNER_PATHS:
            # T7 part 2: an owner route outside /api/owner/ (the UI runs) answers on the owner host only; a session
            # of the main host is a reader's
            self.refusal = 'level'
            self.reply(403, OWNER_ONLY_TEXT, {'Content-Type': 'application/json; charset=utf-8'})
            return
        # G10: a reader with a project list opens the listed codes alone; a route of another project is not there
        allowed = self.server.auth.projects_of(self.user[0])
        one = PROJECT_PATH.match(path)
        if allowed and one and one.group(1) not in allowed:
            self.refusal = 'project'
            self.reply(404, b'Not found.\n')
            return
        if self.command == 'POST':
            # Cookie sessions require the exact HTTPS origin for submitted questions.
            is_chat = bool(re.fullmatch(r'/api/projects/tcp-[a-z0-9]{4}/chat', path)) or path == '/api/ask'
            if path != '/ask' and not is_chat:
                self.reply(405, b'Method not allowed.\n')
                return
            if self.headers.get('Origin') != 'https://' + self.server.domain:
                self.reply(403, b'Open Ask on the AWB site to submit a question.\n')
                return
            if self.headers.get('Sec-Fetch-Site') not in {None, 'same-origin', 'none'}:
                self.reply(403, b'Cross-site requests are not allowed.\n')
                return
            body = self.read_body('application/json' if is_chat else 'application/x-www-form-urlencoded')
            if body is None:
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
        if path == '/api/ask' and self.command != 'POST':
            self.reply(405, b'Method not allowed.\n')
            return
        backend = self.backend(path, reader_pages=True)
        if backend is None:
            self.reply(404, b'Not found.\n')
            return
        self.only = allowed if allowed and path in PROJECT_LISTS else None
        self.forward(backend, self.path, body)

    def backend(self, path, reader_pages=False):
        """Where a route goes: ('tcp', port) or ('unix', socket path); None for a path no backend serves."""
        s = self.server
        if reader_pages and path in BACKEND_ROUTES:
            return ('tcp', s.ask_port)
        if path == '/api/tenants':
            return ('tcp', s.tenants_port)
        if re.fullmatch(r'/api/projects(?:/tcp-[a-z0-9]{4})?', path) or path in ('/api/board', '/api/kb', '/api/price', '/api/usage'):
            if path == '/api/projects' and self.command == 'POST':
                return ('unix', s.project_socket)
            return ('tcp', s.projects_port)
        if reader_pages and (path in ('/api/ask', '/api/ask/budget') or re.fullmatch(r'/api/projects/tcp-[a-z0-9]{4}/chat', path)):
            return ('tcp', s.ask_port)
        if re.fullmatch(r'/api/projects/tcp-[a-z2-7]{4}/materials(?:/(?:sources|imports|upload|M-[A-Z]{24}))?', path):
            return ('unix', s.materials_socket)
        if path == '/api/customers' or re.fullmatch(r'/api/customer-operations/[a-f0-9]{32}', path):
            return ('unix', s.customer_socket)
        if path == '/api/project-options' or re.fullmatch(r'/api/project-operations/[a-f0-9]{32}', path):
            return ('unix', s.project_socket)
        return None

    def forward(self, backend, target, body, extra=None):
        """Hand the request to its backend and its answer back. Never forwards Authorization, cookies or a forwarding
        header of the client; adds the level and a fresh request id. `self.only` (codes, set by a reader route)
        keeps of a JSON answer's `projects` rows those whose code is listed (G10)."""
        kind, where = backend
        only = getattr(self, 'only', None)
        forwarded = {'Host': self.server.domain, 'X-Forwarded-Proto': 'https', 'X-AWB-Level': self.level,
                     'X-AWB-Request': secrets.token_hex(16)}
        if self.command == 'POST':
            forwarded.update({'Origin': 'https://' + self.server.domain,
                              'Content-Type': self.headers.get('Content-Type', '').split(';')[0].lower()})
            if UPLOAD_PATH.fullmatch(urlsplit(target).path):
                # the upload names its extension alone, never a file name (D-F3)
                ext = self.headers.get('X-AWB-Extension', '').lower()
                if re.fullmatch(r'\.[a-z0-9]{1,8}', ext):
                    forwarded['X-AWB-Extension'] = ext
        conn = UnixConnection(where) if kind == 'unix' else http.client.HTTPConnection('127.0.0.1', where, timeout=150)
        try:
            conn.request('GET' if self.command == 'HEAD' else self.command, target, body=body, headers=forwarded)
            response = conn.getresponse()
            data = response.read(MAX_RESPONSE+1)
            if len(data) > MAX_RESPONSE:
                self.reply(502, b'The response was too large.\n')
                return
            if (extra or only) and response.status == 200:
                try:
                    data = restrict({**json.loads(data), **(extra or {})}, only)
                except (ValueError, TypeError, AttributeError):
                    self.reply(502, b'The AWB service is temporarily unavailable.\n')
                    return
            permitted = {'content-type', 'content-security-policy', 'allow'}
            headers = {k: v for k, v in response.getheaders() if k.lower() in permitted}
            self.reply(response.status, data, headers)
        except (OSError, http.client.HTTPException):
            self.reply(502, b'The AWB service is temporarily unavailable.\n')
        finally:
            conn.close()

    # ------------------------------------------------------------------------------------------- the owner host

    def fresh_code(self, body):
        """Publish needs a TOTP code of the signed-in owner of the current step (or the next), used once."""
        try:
            code = json.loads(body).get('code')
        except (ValueError, AttributeError, UnicodeError):
            return False
        account = self.server.auth.accounts()[1].get(self.owner_login)
        now = self.server.auth.clock()
        step = totp_step(account.totp if account else None, code, now)
        return step is not None and step >= int(now // TOTP_STEP) and self.server.auth.use_code(self.owner_login, step, now)

    def owner_origin(self):
        return 'https://' + self.server.owner_host

    def owner_login_page(self, error='', status=200):
        token = self.server.auth.csrf_token()
        message = '<p role="alert" class="error">'+html.escape(error)+'</p>' if error else ''
        content = ('<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Owner sign in · AWB</title><style>'
                   '*{box-sizing:border-box}body{margin:0;background:#1d2530;color:#121a24;font:15px/1.5 -apple-system,BlinkMacSystemFont,"Segoe UI",Arial,sans-serif;min-height:100vh;display:grid;place-items:center;padding:24px}.box{width:100%;max-width:400px;background:white;border-radius:12px;padding:34px}h1{font-size:23px;margin:0 0 6px}.muted{color:#636d78;font-size:13px;margin:0 0 20px}label{display:block;font-size:13px;margin:14px 0 6px}input{width:100%;padding:11px 12px;border:1px solid #d8dfe8;border-radius:7px;font:inherit}button{width:100%;padding:12px;border:0;border-radius:7px;background:#c4006a;color:white;font:600 14px inherit;margin-top:22px;cursor:pointer}input:focus-visible,button:focus-visible{outline:2px solid #c4006a;outline-offset:3px}.error{color:#b33a31;background:#f8e7e5;border-radius:7px;padding:12px;font-size:13px}</style></head>'
                   '<body><main class="box"><h1>Owner level</h1><p class="muted">Your password and the code of your authenticator app.</p>' + message +
                   '<form action="/login" method="post"><input type="hidden" name="csrf" value="'+html.escape(token)+'"><label for="username">Username</label><input id="username" name="username" autocomplete="username" required maxlength="100" autofocus><label for="password">Password</label><input id="password" name="password" type="password" autocomplete="current-password" required maxlength="200"><label for="code">Code</label><input id="code" name="code" inputmode="numeric" autocomplete="one-time-code" required maxlength="6" pattern="[0-9]{6}"><button type="submit">Sign in</button></form></main></body></html>').encode()
        self.reply(status, content, {'Content-Security-Policy': document_csp(content), 'Cross-Origin-Opener-Policy': 'same-origin',
                                     'Set-Cookie': OWNER_LOGIN_COOKIE+'='+token+'; Path=/; Secure; HttpOnly; SameSite=Strict; Max-Age=600'})

    def owner_route(self, session):
        if self.headers.get('Sec-Fetch-Site') not in {None, 'same-origin', 'none'}:
            # the main host is the same site, so SameSite=Strict still sends the owner cookie with a request of its
            # page: every request from another origin is refused here, GETs too (finding 15 of the review)
            self.refusal = 'owner-site'
            self.reply(403, b'Open the owner page directly.\n')
            return
        url = self.first_checks()
        if url is None:
            return
        path = url.path
        origin = self.owner_origin()
        if path == '/login':
            if self.command in {'GET', 'HEAD'}:
                if self.owner_login is not None:
                    self.reply(303, headers={'Location': '/'})
                else:
                    self.owner_login_page()
                return
            form = self.read_form(origin)
            if form is None:
                return
            csrf = (form.get('csrf') or [''])[0]
            if not csrf or not hmac.compare_digest(csrf.encode(), self.cookie(OWNER_LOGIN_COOKIE).encode()) or not self.server.auth.use_csrf(csrf):
                self.owner_login_page('The sign-in form expired. Please try again.', 403)
                return
            username = (form.get('username') or [''])[0]
            result = self.server.auth.sign_in_owner(username, (form.get('password') or [''])[0],
                                                    (form.get('code') or [''])[0], self.client_key())
            if result != 'ok':
                if result != 'fail':
                    self.refusal = 'busy' if result == 'busy' else 'limited'
                self.owner_login_page({'fail': OWNER_FAIL_TEXT, 'busy': BUSY_TEXT}.get(
                    result, 'Too many attempts. Try again in one minute.'), {'fail': 200, 'busy': 503}.get(result, 429))
                return
            token = self.server.auth.new_owner_session(username)
            if token is None:
                self.refusal = 'busy'
                self.owner_login_page('Too many owner sessions are open. Sign out elsewhere first.', 503)
                return
            self.reply(303, headers={'Location': '/', 'Set-Cookie': OWNER_COOKIE+'='+token+'; Path=/; Secure; HttpOnly; SameSite=Strict'})
            return
        if self.owner_login is None:
            if path.startswith('/api/'):
                self.reply(401, b'{"error":"Sign in to open the project data."}', {'Content-Type': 'application/json; charset=utf-8'})
            elif self.command in {'GET', 'HEAD'}:
                self.reply(303, headers={'Location': '/login'})
            else:
                self.reply(403, b'Sign in before submitting a question.\n')
            return
        if path == '/logout':
            if self.command in {'GET', 'HEAD'}:
                self.reply(200, b'<html><head><title>Sign out - AWB owner</title></head><body><form method="post" action="/logout"><button>Sign out</button></form></body></html>')
            elif self.read_form(origin) is not None:
                self.server.auth.revoke_owner_session(session)
                self.reply(303, headers={'Location': '/login', 'Set-Cookie': OWNER_COOKIE+'=; Path=/; Secure; HttpOnly; SameSite=Strict; Max-Age=0'})
            return
        if path == '/' and self.command in {'GET', 'HEAD'}:
            page = self.server.owner_page
            if page is None:
                self.reply(503, b'The workspace is temporarily unavailable.\n')
                return
            self.reply(200, page[0], {'Content-Security-Policy': page[1], 'Cross-Origin-Opener-Policy': 'same-origin'})
            return
        body = None
        if self.command == 'POST':
            if self.headers.get('Origin') != origin or self.headers.get('Sec-Fetch-Site') != 'same-origin':
                self.refusal = 'owner-origin'
                self.reply(403, b'Cross-site requests are not allowed.\n')
                return
            if not self.server.auth.valid_owner_csrf(session, self.headers.get('X-AWB-CSRF', '')):
                self.refusal = 'owner-csrf'
                self.reply(403, b'The owner page expired. Reload it.\n')
                return
            if not (path == '/api/projects' or is_moved(path, 'POST') or path in OWNER_POSTS):
                self.reply(405, b'Method not allowed.\n')
                return
            upload = bool(UPLOAD_PATH.fullmatch(path))
            body = self.read_body('application/octet-stream', MAX_UPLOAD) if upload else self.read_body('application/json')
            if body is None:
                return
            if path == '/api/owner/publish' and not self.fresh_code(body):
                self.refusal = 'owner-code'
                self.reply(403, b'{"error":"The code is missing or not current."}',
                           {'Content-Type': 'application/json; charset=utf-8'})
                return
        route = owner_route_of(self.command, path)
        if route is not None:
            sock, query = route
            extra = None
            if path == '/api/owner/state':
                extra = {'csrf': self.server.auth.owner_csrf(session),
                         'shared_password_active': not self.server.auth.accounts()[0]}
            self.forward(('unix', getattr(self.server, sock)), self.path if query else path, body, extra)
            return
        if path.startswith('/api/owner/'):
            self.reply(404, b'Not found.\n')
            return
        backend = self.backend(path)
        if backend is None or not path.startswith('/api/'):
            self.reply(404, b'Not found.\n')
            return
        self.forward(backend, self.path, body)

    do_GET = do_HEAD = do_POST = do_PUT = do_PATCH = do_DELETE = do_OPTIONS = dispatch

    def log_error(self, fmt, *args):
        # the request line or the headers did not arrive within the timeout (handle_one_request closes the
        # connection): counted in the sign-in log by address key, one access line with 408 and no answer
        if fmt.startswith('Request timed out'):
            self.timed_out(reply=False)
            args = ('-', 408)
        self.log_message(fmt, *args)

    def log_message(self, fmt, *args):
        # No question text, credentials, query strings, addresses, user names or backend data in access logs: the
        # method, the status, the host (main or owner), the level and one word on a refusal.
        status = str(args[1]) if len(args) > 1 else '-'
        print(getattr(self, 'command', None) or '-', status, getattr(self, 'host_label', 'main'),
              getattr(self, 'level', '-'), getattr(self, 'refusal', '') or '-', flush=True)


def _configure(server, auth, index, _portal_port, ask_port, projects_port, tenants_port, domain):
    # _portal_port: the place of the removed portal's port, kept so the callers' positional order stays; unused
    server.domain = domain.lower()
    server.daemon_threads = True
    server.auth = auth
    server.index = Path(index)
    server.ask_port = ask_port
    server.projects_port = projects_port
    server.tenants_port = tenants_port
    server.customer_socket = '/run/awb-customers.sock'
    server.project_socket = '/run/awb-project-create.sock'
    server.materials_socket = '/run/awb-materials.sock'
    server.v1_marker = None
    server.owner_host = None
    server.owner_socket = '/run/awb-owner.sock'
    server.owner_tenants_socket = '/run/awb-owner-tenants.sock'
    server.owner_page = owner_page()
    return server


def owner_page(path=None):
    """(bytes, CSP) of the owner page of this release, the script hashes computed once at start; None without it."""
    try:
        data = Path(path or Path(__file__).with_name('owner.html')).read_bytes()
    except OSError:
        return None
    return data, document_csp(data)


class TCPServer(ThreadingHTTPServer):
    """A TCP listener on 127.0.0.1. With `peer` set, a connection whose local process is not of that uid is closed
    before a byte is read, as on the front socket (finding 12 of the review of 2026-10-10); without it every local
    client is served, and none of them chooses its address key."""
    peer = None

    def verify_request(self, request, client_address):
        if self.peer is None or loopback_peer_uid(request) == self.peer:
            return True
        print('-', '-', 'main', '-', 'peer', flush=True)
        return False


def make_server(port, auth, index, _portal_port, ask_port, projects_port=8182, tenants_port=8183, *, domain,
                peer=None):
    """A TCP listener on 127.0.0.1, for the tests only: the installed unit takes the site on the front socket."""
    server = TCPServer(('127.0.0.1', port), Gateway)
    server.peer = peer
    return _configure(server, auth, index, _portal_port, ask_port, projects_port, tenants_port, domain)


def peer_uid(sock):
    """The uid of the process at the other end of a Unix socket (SO_PEERCRED)."""
    cred = sock.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize('3i'))
    return struct.unpack('3i', cred)[1]


def _proc_address(address):
    """An (ip, port) in the form of /proc/net/tcp and tcp6: each 32-bit word of the address in host order, hex."""
    ip, port = address[0], address[1]
    packed = socket.inet_pton(socket.AF_INET6 if ':' in ip else socket.AF_INET, ip)
    words = ''.join('%08X' % int.from_bytes(packed[i:i+4], sys.byteorder) for i in range(0, len(packed), 4))
    return '%s:%04X' % (words, port)


def loopback_peer_uid(sock, tables=('/proc/net/tcp', '/proc/net/tcp6')):
    """The uid of the local process at the other end of a loopback TCP connection: the owner of the socket whose
    local address is our peer and whose remote address is us, from the kernel's table; None when it is not found."""
    try:
        want = (_proc_address(sock.getpeername()), _proc_address(sock.getsockname()))
    except (OSError, ValueError, IndexError):
        return None
    for table in tables:
        try:
            with open(table, encoding='ascii', errors='replace') as fh:
                lines = fh.read().splitlines()[1:]
        except OSError:
            continue
        for line in lines:
            fields = line.split()
            if len(fields) > 7 and (fields[1], fields[2]) == want:
                try:
                    return int(fields[7])
                except ValueError:
                    return None
    return None


def unix_listener(path, mode=0o660):
    """A Unix stream socket bound at `path` (a stale socket file replaced), for a run without socket activation."""
    if os.path.exists(path) and stat.S_ISSOCK(os.lstat(path).st_mode):
        os.unlink(path)
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    old = os.umask(0o777 & ~mode)
    try:
        sock.bind(path)
    finally:
        os.umask(old)
    os.chmod(path, mode)
    sock.listen(64)
    return sock


class UnixServer(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
    """An HTTP server on a listening Unix socket. With `peer` set, a connection from any other uid is closed before
    a byte is read (D-FRONT: only cloudflared's own user reaches the gateway)."""
    daemon_threads = True
    allow_reuse_address = False

    def __init__(self, sock, handler, peer=None):
        socketserver.BaseServer.__init__(self, sock.getsockname(), handler)
        self.socket = sock
        self.peer = peer

    def verify_request(self, request, client_address):
        if self.peer is None:
            return True
        try:
            if peer_uid(request) == self.peer:
                return True
        except OSError:
            pass
        print('-', '-', 'main', '-', 'peer', flush=True)
        return False


def make_front_server(sock, auth, index, _portal_port, ask_port, projects_port=8182, tenants_port=8183, *, domain,
                      peer):
    """The front door: the site on a Unix socket that only the uid `peer` (cloudflared's user) may use."""
    if peer is None:
        raise TypeError('the front socket needs the uid of its one peer')
    server = UnixServer(sock, Gateway, peer)
    return _configure(server, auth, index, _portal_port, ask_port, projects_port, tenants_port, domain)


class StatusHandler(BaseHTTPRequestHandler):
    """The status socket: GET /health and nothing else, for awb web status, the publish code and the deploy."""
    server_version = 'AWB'
    sys_version = ''
    timeout = 3   # the socket is 0666: an idle client must not hold a thread of the gateway (K5)

    def answer(self):
        body = b'{"status":"ok"}\n' if self.path == '/health' and self.command in ('GET', 'HEAD') else b'Not found.\n'
        self.send_response(200 if body.startswith(b'{') else 404)
        self.send_header('Content-Type', 'application/json' if body.startswith(b'{') else 'text/plain')
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Connection', 'close')
        self.end_headers()
        if self.command != 'HEAD':
            self.wfile.write(body)

    do_GET = do_HEAD = do_POST = answer

    def log_message(self, fmt, *args):
        pass


def make_status_server(sock):
    return UnixServer(sock, StatusHandler)


def inherited_sockets(environ=os.environ):
    """{path: socket} of the listening Unix sockets systemd handed over (LISTEN_FDS), empty without activation."""
    if environ.get('LISTEN_PID') != str(os.getpid()):
        return {}
    out = {}
    for fd in range(3, 3 + int(environ.get('LISTEN_FDS', '0') or 0)):
        sock = socket.socket(fileno=fd)
        out[sock.getsockname()] = sock
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--auth', required=True, help='the single login used until the users file exists')
    ap.add_argument('--users', help='the users file of awb web user (default: users.json next to --auth)')
    ap.add_argument('--signin-log', help='where every sign-in attempt is logged (time, login, result)')
    ap.add_argument('--domain', required=True, help='the host name of the site, without scheme')
    ap.add_argument('--index', required=True)
    ap.add_argument('--owner-host', help='the host name of the owner level, one level under the zone of the site')
    ap.add_argument('--front-socket', help='the Unix socket of the site (from systemd, else bound here)')
    ap.add_argument('--front-peer', help='the one user whose processes may connect to the front socket')
    ap.add_argument('--status-socket', help='a Unix socket that answers GET /health only')
    ap.add_argument('--ports', nargs='*', type=int, default=[],
                    help='TCP listeners on 127.0.0.1 that answer the --front-peer user alone (for the tests; no '
                         'installed unit uses them, the site comes in on the front socket)')
    ap.add_argument('--v1-marker', help=argparse.SUPPRESS)
    ap.add_argument('--ask-port', type=int, default=8181)
    ap.add_argument('--projects-port', type=int, default=8182)
    ap.add_argument('--tenants-port', type=int, default=8183)
    args = ap.parse_args()
    if not args.front_socket and not args.ports:
        ap.error('give --front-socket (--ports is for the tests)')
    peer = None
    if (args.front_socket or args.ports) and not args.front_peer:
        ap.error('--front-socket and --ports need --front-peer, the one user that may connect')
    if args.front_peer:
        try:
            peer = pwd.getpwnam(args.front_peer).pw_uid
        except KeyError:
            print('AWB gateway: the user %s of --front-peer does not exist' % args.front_peer, flush=True)
            sys.exit(2)
    auth = Auth(args.auth, args.users, args.signin_log)
    rest = (None, args.ask_port, args.projects_port, args.tenants_port)
    handed = inherited_sockets()
    servers = []
    if args.front_socket:
        sock = handed.get(args.front_socket) or unix_listener(args.front_socket)
        servers.append(make_front_server(sock, auth, args.index, *rest, domain=args.domain, peer=peer))
    if args.status_socket:
        servers.append(make_status_server(handed.get(args.status_socket) or unix_listener(args.status_socket, 0o666)))
    servers += [make_server(port, auth, args.index, *rest, domain=args.domain, peer=peer) for port in args.ports]
    for server in servers:
        server.v1_marker = args.v1_marker
        server.owner_host = (args.owner_host or '').lower() or None
    for server in servers[1:]:
        threading.Thread(target=server.serve_forever, daemon=True).start()
    print('AWB authenticated gateway ready', flush=True)
    servers[0].serve_forever()


if __name__ == '__main__':
    main()
