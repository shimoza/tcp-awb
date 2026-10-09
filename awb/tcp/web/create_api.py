"""Narrow customer and project creation adapters. No model or cloud calls.

Run as two different users behind permissioned, socket-activated Unix sockets.
Only the owner process sees names. Journals contain codes and keyed digests only.
"""
# Moved from the web adapters of 2026-10-02 into the repository on 2026-10-04, behaviour unchanged (tests/test_web_*.py).

import argparse
import datetime
import hashlib
import hmac
import http.client
import json
import os
from pathlib import Path
import pwd
import re
import socket
import sqlite3
import struct
import stat
import threading
import unicodedata
from http.server import BaseHTTPRequestHandler, HTTPServer
from socketserver import ThreadingMixIn

from awb import codes, config, intake, ledger, projects, register, vault

PROJECT_SOCKET = '/run/awb-project-create.sock'
REQUEST = re.compile(r'[a-f0-9]{32}')
CUSTOMER = re.compile(r'CUST-[A-Z2-7]{4}')
WEB_REGISTER_OFF = "New customers are registered on the owner's terminal, not in the console."


class Problem(Exception):
    def __init__(self, status, message, **data):
        self.status, self.data = status, {'error': message, **data}


class UnixConnection(http.client.HTTPConnection):
    def __init__(self, path, timeout=30):
        super().__init__('localhost', timeout=timeout)
        self.path = path

    def connect(self):
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(self.timeout)
        self.sock.connect(self.path)


def customer_issued(paths, code):
    """F11: the project side no longer asks the owner's customer socket. A customer code counts when the owner
    issued it, by its folder in the outbox (register add opens it), the rule awb spawn keeps on the work side."""
    path = paths.outbox / code
    return bool(CUSTOMER.fullmatch(code or '')) and path.is_dir() and not path.is_symlink()


class Journal:
    def __init__(self, root):
        root = Path(root)
        root.mkdir(mode=0o700, parents=True, exist_ok=True)
        key = root / 'digest.key'
        if not key.exists():
            fd = os.open(key, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, 'wb') as f:
                f.write(os.urandom(32)); f.flush(); os.fsync(f.fileno())
        self.key = key.read_bytes()
        self.path = root / 'operations.sqlite3'
        with self.db() as db:
            db.execute('CREATE TABLE IF NOT EXISTS operations (id TEXT PRIMARY KEY, digest TEXT NOT NULL, code TEXT UNIQUE NOT NULL, result TEXT)')
        os.chmod(self.path, 0o600)

    def db(self):
        return sqlite3.connect(self.path)

    def digest(self, payload):
        return hmac.new(self.key, json.dumps(payload, sort_keys=True, ensure_ascii=False).encode(), hashlib.sha256).hexdigest()

    def get(self, key, payload):
        with self.db() as db:
            row = db.execute('SELECT digest,code,result FROM operations WHERE id=?', (key,)).fetchone()
        if row and not hmac.compare_digest(row[0], self.digest(payload)):
            raise Problem(409, 'This request already belongs to another submission. Reopen the form.', terminal=True)
        return row

    def start(self, key, payload, code):
        with self.db() as db:
            db.execute('INSERT INTO operations VALUES (?,?,?,NULL)', (key, self.digest(payload), code))

    def finish(self, key, result):
        with self.db() as db:
            db.execute('UPDATE operations SET result=? WHERE id=?', (json.dumps(result), key))
        return result

    def discard(self, key):
        with self.db() as db:
            db.execute('DELETE FROM operations WHERE id=? AND result IS NULL', (key,))

    def codes(self):
        with self.db() as db:
            return {r[0] for r in db.execute('SELECT code FROM operations')}

    def status(self, key):
        with self.db() as db:
            row = db.execute('SELECT code,result FROM operations WHERE id=?', (key,)).fetchone()
        if not row:
            return {'status': 'unknown'}
        return {'status': 'complete', **json.loads(row[1])} if row[1] else {'status': 'pending', 'code': row[0]}


def request_id(data, allowed):
    if not isinstance(data, dict) or set(data) - set(allowed) - {'request_id'}:
        raise Problem(400, 'Unsupported form fields.')
    key = data.get('request_id')
    if not isinstance(key, str) or not REQUEST.fullmatch(key):
        raise Problem(400, 'A valid request ID is required.')
    return key


def normalize_name(value):
    return ' '.join(unicodedata.normalize('NFKC', value).casefold().split())


class CustomerStore:
    def __init__(self, paths, state):
        self.p, self.journal, self.lock = paths, Journal(state), threading.Lock()

    def list(self):
        # This response is only for the authenticated owner UI, never project or chat context.
        entries = register.load(self.p.register)
        grouped = {}
        for e in entries:
            if e.kind == 'CUST' and CUSTOMER.fullmatch(e.code):
                grouped.setdefault(e.code, []).append(e)
        return {'customers': [{'code': code, 'name': rows[0].form,
                              'aliases': [e.form for e in rows[1:] if e.status == 'active'],
                              'active': any(e.status == 'active' for e in rows)}
                             for code, rows in sorted(grouped.items())]}

    def codes(self):
        # D-NOW and D-F3 = no (2026-10-08): what the console gets, on the owner host only (T9 step 2): the code, its
        # state and the count of its projects. The forms of the register never leave the owner side; `name`
        # carries the code so that a page of before keeps working.
        try:
            counts = {}
            for p in projects.load(self.p):
                if p.state != 'deleted':
                    counts[p.customer] = counts.get(p.customer, 0) + 1
        except (OSError, ValueError, projects.ProjectError):
            counts = None
        return {'customers': [dict(c, name=c['code'], aliases=[], state='active' if c['active'] else 'retired',
                                   projects=counts.get(c['code'], 0) if counts is not None else None)
                              for c in self.list()['customers']]}

    def active(self, code):
        return any(e.code == code and e.status == 'active' for e in register.load(self.p.register))

    def marker(self, code):
        path = self.p.outbox / code
        if path.is_symlink():
            raise Problem(503, 'The customer was saved but is not ready for projects. Retry this submission.')
        config.make_dir(path, 0o750, shared=True)

    def create(self, data):
        key = request_id(data, {'name', 'aliases'})
        name, aliases = data.get('name'), data.get('aliases', [])
        if not isinstance(name, str) or not isinstance(aliases, list) or len(aliases) > 10:
            raise Problem(400, 'Enter a customer name and at most 10 alternative names.')
        forms = [name] + aliases
        if any(not isinstance(f, str) or not 2 <= len(f.strip()) <= 200 or
               any(unicodedata.category(c).startswith('C') for c in f) for f in forms):
            raise Problem(400, 'Names must contain 2 to 200 characters on one line.')
        forms = list(dict.fromkeys(f.strip() for f in forms))
        try:
            for f in forms:
                register.check_entry(register.Entry('CUST-AAAA', 'CUST', f, datetime.date.today().isoformat(), 'active'), 'new customer')
        except register.RegisterError:
            raise Problem(400, 'Use a name without brackets, pipes, hashes or notes.') from None
        payload = {'forms': forms}
        with self.lock, vault.vault_lock(self.p, wait=3):
            op = self.journal.get(key, payload)
            if op and op[2]:
                return json.loads(op[2])
            entries = register.load(self.p.register)
            if op:
                code = op[1]
                actual = {e.form for e in entries if e.code == code and e.status == 'active'}
                if any(e.code == code and e.status == 'retired' for e in entries):
                    raise Problem(409, 'This customer was retired. Open the customer register.', terminal=True, code=code)
                if actual:
                    if not set(forms) <= actual:
                        raise Problem(409, 'The saved customer needs inspection before retrying.', terminal=True, code=code)
                    self.marker(code)
                    return self.journal.finish(key, {'code': code, 'created': True})
            else:
                # A collision is a choice for the owner, never an automatic merge.
                wanted = {normalize_name(f) for f in forms}
                matches = sorted({e.code for e in entries if e.kind == 'CUST' and CUSTOMER.fullmatch(e.code)
                                  and normalize_name(e.form) in wanted})
                if matches:
                    raise Problem(409, 'A customer with this name already exists. Select the existing customer.',
                                  matches=matches)
                used = intake.issued_codes(self.p) | self.journal.codes()
                code = intake.safe_new_code(entries, 'CUST', used)
                self.journal.start(key, payload, code)
            # Entire alias set is validated and committed together by the existing register implementation.
            new = [register.Entry(code, 'CUST', f, datetime.date.today().isoformat(), 'active') for f in forms]
            for e in new:
                register.check_entry(e, 'new customer')
            register.save(self.p.register, entries + new)
            self.marker(code)
            return self.journal.finish(key, {'code': code, 'created': True})


class ProjectStore:
    def __init__(self, paths, state, active=None):
        self.p, self.journal = paths, Journal(state)
        self.active = active or (lambda code: customer_issued(paths, code))
        self.lock = threading.Lock()

    def options(self):
        return {'kinds': list(projects.PROJECT_KINDS), 'tags': projects.known_tags()}

    def publish_material(self, code, data):
        from awb import check, gate
        if not isinstance(data, dict) or set(data) != {'id', 'customer', 'text', 'sha256'}:
            raise Problem(400, 'Invalid working copy.')
        ident, content = data['id'], data['text']
        if not isinstance(ident, str) or not re.fullmatch(r'M-[A-Z]{24}', ident) or not isinstance(content, str):
            raise Problem(400, 'Invalid working copy.')
        raw = content.encode()
        if not raw or len(raw) > 256 * 1024 or hashlib.sha256(raw).hexdigest() != data['sha256']:
            raise Problem(400, 'Invalid working copy digest or size.')
        if check.check_text(content, self.p.register) or any(gate.DETECTORS[k](content) for k in ('secret', 'private-key', 'token', 'homepath')):
            raise Problem(422, 'The working copy failed its final data check.')
        with self.lock, projects.locked(self.p):
            pr = next((p for p in projects.load(self.p) if p.code == code), None)
            if not pr or pr.state != 'active' or pr.customer != data['customer']:
                raise Problem(409, 'The project is unavailable or its customer changed.')
            if Path(pr.path) != self.p.projects_root / code:
                raise Problem(409, 'The registered project folder is unsupported.')
            parent = os.open(self.p.projects_root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            try:
                root = os.open(code, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent)
                try:
                    folder = os.open('input', os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=root)
                finally:
                    os.close(root)
            finally:
                os.close(parent)
            name = ident + '.md'
            temp = '.' + ident + '.tmp-' + os.urandom(8).hex()
            try:
                try:
                    existing = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=folder)
                except FileNotFoundError:
                    existing = None
                if existing is not None:
                    with os.fdopen(existing, 'rb') as f:
                        if not stat.S_ISREG(os.fstat(f.fileno()).st_mode) or f.read(len(raw) + 1) != raw:
                            raise Problem(409, 'An input with this identifier already exists with different content.')
                    return {'filename': name}
                fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o640, dir_fd=folder)
                with os.fdopen(fd, 'wb') as f:
                    f.write(raw); f.flush(); os.fsync(f.fileno())
                os.link(temp, name, src_dir_fd=folder, dst_dir_fd=folder, follow_symlinks=False)
                os.fsync(folder)
                return {'filename': name}
            finally:
                try: os.unlink(temp, dir_fd=folder)
                except FileNotFoundError: pass
                os.close(folder)

    def create(self, data):
        key = request_id(data, {'kind', 'goal', 'customer', 'tags'})
        kind, goal, customer, tags = data.get('kind'), data.get('goal'), data.get('customer', 'none'), data.get('tags', [])
        if kind not in projects.PROJECT_KINDS or not isinstance(goal, str) or not goal.strip() or len(goal) > 500:
            raise Problem(400, 'Choose a project type and enter a goal of up to 500 characters.')
        if not isinstance(customer, str) or (customer != 'none' and not CUSTOMER.fullmatch(customer)):
            raise Problem(400, 'Select a valid customer or Internal project.')
        if not isinstance(tags, list) or len(tags) > 20 or any(not isinstance(t, str) for t in tags):
            raise Problem(400, 'Select tags from the list.')
        if kind == 'engagement' and customer == 'none':
            raise Problem(400, 'Select a customer for customer work.', field='customer')
        payload = {'kind': kind, 'goal': goal.strip(), 'customer': customer, 'tags': sorted(set(tags))}
        with self.lock:
            op = self.journal.get(key, payload)
            if op and op[2]:
                return json.loads(op[2])
            if customer != 'none' and not self.active(customer):
                raise Problem(400, 'This customer is unavailable or retired. Select an active customer.', field='customer')
            rows = projects.load(self.p)
            if op:
                code = op[1]
                existing = next((p for p in rows if p.code == code), None)
                if existing:
                    if existing.kind != kind or existing.customer != customer or projects.goal_of(existing.path) != goal.strip():
                        raise Problem(409, 'The reserved project needs inspection.', terminal=True, code=code)
                    return self.journal.finish(key, {'code': code, 'created': True,
                        'warning': 'Project recovered after an interrupted request. Its first activity entry may be missing.'})
                if (self.p.projects_root / code).exists():
                    raise Problem(409, 'Creation was interrupted. The reserved folder needs inspection before retrying.',
                                  terminal=True, code=code)
            else:
                used = {p.code for p in rows} | self.journal.codes()
                code = codes.new_project_code('tcp', used)
                self.journal.start(key, payload, code)
            # Pin allocation to the durable operation. The core still holds its cross-process project lock,
            # validates all data, checks collisions, installs hooks and commits the standard skeleton.
            try:
                pr = projects.spawn(self.p, kind, goal, customer, self.p.register, payload['tags'], from_outbox=False,
                                    code=code)
            except projects.ProjectError as e:
                # Core errors are designed to contain no names or submitted values.
                self.journal.discard(key)
                raise Problem(400, str(e)) from None
            result = {'code': pr.code, 'created': True}
            try:
                projects.record(self.p, pr, 'Started %s (%s project): %s' % (pr.code, kind, goal.strip()), tags=payload['tags'])
            except (ledger.LedgerError, OSError):
                result['warning'] = 'Project created. The first activity entry could not be written.'
            return self.journal.finish(key, result)


class Handler(BaseHTTPRequestHandler):
    def setup(self):
        super().setup()
        self.connection.settimeout(20)

    def reply(self, status, result):
        body = json.dumps(result, ensure_ascii=False).encode()
        self.send_response(status)
        for k, v in {'Content-Type': 'application/json; charset=utf-8', 'Content-Length': str(len(body)),
                     'Cache-Control': 'no-store, private', 'Connection': 'close'}.items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        self.dispatch()

    def do_POST(self):
        self.dispatch()

    def dispatch(self):
        try:
            uid = struct.unpack('3i', self.connection.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12))[1]
            publish = re.fullmatch(r'/internal/projects/(tcp-[a-z2-7]{4})/materials', self.path)
            if self.server.mode == 'projects' and publish and self.command == 'POST' and uid == self.server.owner_uid:
                lengths = self.headers.get_all('Content-Length', [])
                if len(lengths) != 1 or self.headers.get('Transfer-Encoding') or self.headers.get('Content-Type', '').split(';')[0] != 'application/json':
                    raise Problem(400, 'Invalid working copy request.')
                n = int(lengths[0])
                if not 0 < n <= 2 * 1024 * 1024:
                    raise Problem(413, 'Working copy is too large.')
                raw = self.rfile.read(n)
                if len(raw) != n: raise Problem(400, 'Incomplete working copy.')
                return self.reply(200, self.server.store.publish_material(publish[1], json.loads(raw)))
            # F11: the web user alone; the work user's call of the customer socket is gone
            if uid != self.server.web_uid:
                raise Problem(403, 'Not allowed.')
            store = self.server.store
            if self.command == 'GET':
                status_route = re.fullmatch(r'/api/(customer|project)-operations/([a-f0-9]{32})', self.path)
                if status_route and status_route[1] + 's' == self.server.mode:
                    return self.reply(200, store.journal.status(status_route[2]))
                if self.server.mode == 'customers' and self.path == '/api/customers':
                    return self.reply(200, store.codes())
                if self.server.mode == 'projects' and self.path == '/api/project-options':
                    return self.reply(200, store.options())
                raise Problem(404, 'Not found.')
            route = '/api/customers' if self.server.mode == 'customers' else '/api/projects'
            if self.path != route:
                raise Problem(404, 'Not found.')
            if self.server.mode == 'customers':
                raise Problem(403, WEB_REGISTER_OFF)  # D-F3 = no: names never come in through the web
            lengths = self.headers.get_all('Content-Length', [])
            if len(lengths) != 1 or self.headers.get('Transfer-Encoding'):
                raise Problem(400, 'Invalid request length.')
            if self.headers.get('Content-Type', '').split(';')[0] != 'application/json':
                raise Problem(415, 'Use JSON.')
            try:
                length = int(lengths[0])
                if not 0 < length <= 16384:
                    raise ValueError()
                raw = self.rfile.read(length)
                if len(raw) != length:
                    raise ValueError()
                data = json.loads(raw)
            except (ValueError, UnicodeError):
                raise Problem(400, 'Invalid request.') from None
            return self.reply(201, store.create(data))
        except Problem as e:
            self.reply(e.status, e.data)
        except (register.RegisterBusy, vault.VaultBusy):
            self.reply(503, {'error': 'The vault daemon is reloading, try again. Retry this submission in a moment.'})
        except (register.RegisterError, vault.VaultError):
            self.reply(503, {'error': 'The customer register is locked or unavailable. Retry this submission when it is unlocked.'})
        except Exception:
            # Do not log exceptions: third-party messages could carry names or paths.
            self.reply(503, {'error': 'The operation could not finish. Retry the same submission to check its result.'})

    def log_message(self, *args):
        pass


class Server(ThreadingMixIn, HTTPServer):
    address_family = socket.AF_UNIX
    daemon_threads = True

    def get_request(self):
        conn, _ = self.socket.accept()
        return conn, ('local', 0)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('mode', choices=['customers', 'projects'])
    ap.add_argument('--state', required=True)
    ap.add_argument('--owner', required=True, help='the owner user, who alone may publish working copies')
    args = ap.parse_args()
    if os.environ.get('LISTEN_PID') != str(os.getpid()) or os.environ.get('LISTEN_FDS') != '1':
        raise SystemExit('Socket activation is required.')
    server = Server('', Handler, bind_and_activate=False)
    server.socket.close()
    server.socket = socket.socket(fileno=3)
    server.web_uid = pwd.getpwnam('awb-web').pw_uid
    server.owner_uid = pwd.getpwnam(args.owner).pw_uid
    server.mode = args.mode
    server.store = (CustomerStore if args.mode == 'customers' else ProjectStore)(config.paths(), args.state)
    server.serve_forever()


if __name__ == '__main__':
    main()
