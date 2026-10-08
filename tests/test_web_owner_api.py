"""The owner level of the console (T9 step 2): the owner host in the gateway, owner-actions, the status file of the
owner's timer, the intake counts and the codes-only answers (D-F3 = no). Invented logins, codes and fixture names;
every backend is a stand-in on a temporary socket."""
import http.client
import json
import os
import re
import socket
import sqlite3
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlencode

import pytest

from tests import fixtures as fx
from awb import intake, intake_counts, status_shape
from awb.tcp import owner
from awb.tcp.web import contract, gateway, materials_api, owner_actions, tenant_api, users

DOMAIN = "awb.example.test"
OWNER = "owner.example.test"
ROUNDS = 1000


# --------------------------------------------------------------------------- stand-ins


class Recorder(BaseHTTPRequestHandler):
    """A backend that answers JSON and records what reached it."""

    def answer(self):
        n = int(self.headers.get("Content-Length") or 0)
        self.server.calls.append((self.command, self.path, dict(self.headers), self.rfile.read(n)))
        raw = json.dumps({"backend": self.server.mark, "path": self.path}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    do_GET = do_POST = answer

    def log_message(self, *args):
        pass


class UnixRecorder(gateway.UnixServer):
    pass


def unix_backend(path, mark):
    server = UnixRecorder(gateway.unix_listener(str(path)), Recorder)
    server.calls, server.mark = [], mark
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


def status_doc(**over):
    doc = {"version": 1, "written": owner.now_text(), "vault": {"state": "locked", "since": "2026-10-08T07:40:00Z"},
           "keys": {"state": "unlocked", "since": None},
           "deployed": {"release": "0123456789abcdef0123456789abcdef01234567", "journal": "done"},
           "page": {"size": 1200, "time": "2026-10-06T15:02:11Z", "backup_time": None, "backups": 0},
           "ui_runs": [{"id": "ui-20261008-001", "state": "completed", "finished": "2026-10-08T11:20:04+00:00",
                        "validation": "PASS", "diff_bytes": 10, "candidate_only": False, "publishable": True}],
           "intake": [{"customer": fx.CUSTOMER_CODE, "waiting": 3, "time": "2026-10-08T09:12:40Z"}]}
    doc.update(over)
    return doc


class Site:
    """A gateway with a main host and an owner host, owner-actions on a socket and stand-in backends."""

    def __init__(self, root: Path):
        self.root = root
        gateway_auth = root / "auth.json"
        gateway_auth.write_text(json.dumps({"username": "awb", "salt": "00", "rounds": ROUNDS, "hash": "00"}))
        self.users = root / "users.json"
        self.pw = {"reader1": users.change(self.users, "add", "reader1", False, rounds=ROUNDS)}
        self.pw["owner1"] = users.change(self.users, "add", "owner1", False, rounds=ROUNDS, level="owner")
        self.secret = json.loads(self.users.read_text())["users"]["owner1"]["totp"]
        self.log = root / "signin.log"
        self.auth = gateway.Auth(gateway_auth, self.users, self.log)
        self.status = root / "status.json"
        self.status.write_text(json.dumps(status_doc()))
        self.tcp = ThreadingHTTPServer(("127.0.0.1", 0), Recorder)
        self.tcp.calls, self.tcp.mark = [], "tcp"
        threading.Thread(target=self.tcp.serve_forever, daemon=True).start()
        port = self.tcp.server_port
        self.unix = {name: unix_backend(root / ("%s.sock" % name), name) for name in ("customers", "projects",
                                                                                      "materials")}
        self.actions = owner_actions.Server(gateway.unix_listener(str(root / "owner.sock")), os.getuid(), self.status,
                                            root / "actions-log")
        threading.Thread(target=self.actions.serve_forever, daemon=True).start()
        self.gw = gateway.make_server(0, self.auth, root / "index.html", port, port, port, port, domain=DOMAIN)
        (root / "index.html").write_text("<html>main page</html>")
        self.gw.owner_host = OWNER
        self.gw.customer_socket = str(root / "customers.sock")
        self.gw.project_socket = str(root / "projects.sock")
        self.gw.materials_socket = str(root / "materials.sock")
        self.gw.owner_socket = str(root / "owner.sock")
        threading.Thread(target=self.gw.serve_forever, daemon=True).start()

    def close(self):
        for s in [self.gw, self.tcp, self.actions, *self.unix.values()]:
            s.shutdown()
            s.server_close()

    def request(self, path, method="GET", host=DOMAIN, headers=None, body=None):
        c = http.client.HTTPConnection("127.0.0.1", self.gw.server_port, timeout=10)
        c.request(method, path, body=body, headers={"Host": host, **(headers or {})})
        r = c.getresponse()
        out = (r.status, dict(r.getheaders()), r.read())
        c.close()
        return out

    def code(self, offset=0):
        return gateway.totp_code(self.secret, int(time.time() // 30) + offset)

    def owner_form(self, **values):
        status, h, page = self.request("/login", host=OWNER)
        assert status == 200
        cookie = h["Set-Cookie"].split(";")[0]
        assert cookie.startswith(gateway.OWNER_LOGIN_COOKIE + "=")
        csrf = re.search(rb'name="csrf" value="([^"]+)"', page).group(1).decode()
        data = {"csrf": csrf, "username": "owner1", "password": self.pw["owner1"], "code": self.code()}
        data.update(values)
        return {"Cookie": cookie, "Origin": "https://" + OWNER, "Content-Type": "application/x-www-form-urlencoded",
                "CF-Connecting-IP": values.pop("address", "198.51.100.7")}, urlencode(data)

    def owner_session(self):
        token = self.auth.new_owner_session("owner1")
        assert token
        return token

    def reader_session(self):
        return self.auth.new_session("reader1")


@pytest.fixture
def site(tmp_path):
    s = Site(tmp_path)
    yield s
    s.close()


def owner_headers(site, token, post=False):
    h = {"Cookie": gateway.OWNER_COOKIE + "=" + token}
    if post:
        _, _, raw = site.request("/api/owner/state", host=OWNER, headers=h)
        h.update({"Origin": "https://" + OWNER, "Sec-Fetch-Site": "same-origin", "Content-Type": "application/json",
                  "X-AWB-CSRF": json.loads(raw)["csrf"]})
    return h


# --------------------------------------------------------------------------- the routes on the two hosts


MOVED = [("GET", "/api/customers"), ("POST", "/api/customers"), ("GET", "/api/customer-operations/" + "a" * 32),
         ("POST", "/api/projects"), ("GET", "/api/project-operations/" + "b" * 32),
         ("GET", "/api/projects/tcp-q7m4/materials"), ("GET", "/api/projects/tcp-q7m4/materials/sources?source=brief"),
         ("POST", "/api/projects/tcp-q7m4/materials/imports"),
         ("GET", "/api/projects/tcp-q7m4/materials/M-" + "A" * 24)]
OWNER_ROUTES = ["/api/owner/state", "/api/owner/ui-runs", "/api/owner/intake", "/api/owner/tenants"]


def test_owner_routes_and_moved_routes_answer_404_on_the_main_host_before_any_backend(site):
    reader = {"Cookie": "__Host-awb-session=" + site.reader_session(), "Origin": "https://" + DOMAIN,
              "Content-Type": "application/json"}
    for route in OWNER_ROUTES + ["/api/owner/publish"]:
        for method in ("GET", "POST"):
            assert site.request(route, method, headers=reader, body="{}")[0] == 404, route
    for method, route in MOVED:
        status, h, body = site.request(route, method, headers=reader, body="{}" if method == "POST" else None)
        assert (status, body) == (404, b"Not found.\n"), route
    assert site.tcp.calls == [] and all(b.calls == [] for b in site.unix.values())
    # an owner cookie means nothing on the main host
    status, h, _ = site.request("/api/projects", headers={"Cookie": gateway.OWNER_COOKIE + "=" + site.owner_session()})
    assert status == 401


def test_reader_routes_answer_on_both_hosts_and_owner_routes_on_the_owner_host(site):
    token = site.owner_session()
    reader = {"Cookie": "__Host-awb-session=" + site.reader_session()}
    for route in ("/api/projects", "/api/projects/tcp-q7m4", "/api/board", "/api/project-options", "/api/tenants"):
        assert site.request(route, headers=reader)[0] == 200, route
        assert site.request(route, host=OWNER, headers=owner_headers(site, token))[0] == 200, route
    for method, route in MOVED:
        h = owner_headers(site, token, post=method == "POST")
        status, _, raw = site.request(route, method, host=OWNER, headers=h, body="{}" if method == "POST" else None)
        assert status == 200, route
    sent = [c for c in site.unix["customers"].calls + site.unix["projects"].calls + site.unix["materials"].calls
            if c[1] != "/api/project-options"]
    assert len(sent) == len(MOVED)
    for _, _, headers, _ in sent:
        assert headers["X-AWB-Level"] == "owner" and re.fullmatch(r"[0-9a-f]{32}", headers["X-AWB-Request"])
        assert "Cookie" not in headers and "Authorization" not in headers and headers["Host"] == DOMAIN
    status, _, raw = site.request("/api/owner/tenants", host=OWNER, headers=owner_headers(site, token))
    assert status == 200 and json.loads(raw)["path"] == "/api/owner/tenants"
    # the old HTML pages, Ask and the chat stay on the main host
    for route in ("/kb", "/ask", "/portal", "/api/projects/tcp-q7m4/chat", "/api/owner/unknown"):
        assert site.request(route, host=OWNER, headers=owner_headers(site, token))[0] == 404, route
    # a reader session means nothing on the owner host
    status, h, _ = site.request("/api/owner/state", host=OWNER, headers=reader)
    assert status == 401
    assert site.request("/", host=OWNER, headers=reader)[1]["Location"] == "/login"


def test_the_owner_page_is_the_fixed_file_with_its_script_hash_and_coop(site):
    token = site.owner_session()
    status, h, page = site.request("/", host=OWNER, headers=owner_headers(site, token))
    assert status == 200 and page == (Path(gateway.__file__).with_name("owner.html")).read_bytes()
    assert h["Cross-Origin-Opener-Policy"] == "same-origin" and "frame-ancestors 'none'" in h["Content-Security-Policy"]
    assert "'sha256-" in h["Content-Security-Policy"] and "unsafe-inline' " not in h["Content-Security-Policy"].split(
        "style-src")[0]
    text = page.decode()
    for word in ("awb vault", "awb keys", "sudo", "awb intake", "awb register", "passphrase"):
        assert word not in text, word
    assert "innerHTML" not in text and "http://" not in text and "https://" not in text


# --------------------------------------------------------------------------- the owner sign-in


def test_the_owner_sign_in_takes_the_password_and_a_code_and_sets_the_strict_cookie(site):
    h, body = site.owner_form()
    status, out, _ = site.request("/login", "POST", OWNER, h, body)
    assert status == 303 and out["Location"] == "/"
    cookie = out["Set-Cookie"]
    assert cookie.startswith(gateway.OWNER_COOKIE + "=")
    for part in ("Path=/", "Secure", "HttpOnly", "SameSite=Strict"):
        assert part in cookie
    assert "Max-Age" not in cookie and "Domain=" not in cookie and "Expires" not in cookie
    token = cookie.split(";")[0].split("=", 1)[1]
    assert site.request("/api/owner/state", host=OWNER, headers={"Cookie": gateway.OWNER_COOKIE + "=" + token})[0] == 200
    # the main host never sets the owner cookie, whatever is posted to its /login
    status, out, page = site.request("/login")
    assert gateway.OWNER_COOKIE + "=" not in out.get("Set-Cookie", "") and b'name="code"' not in page


def test_wrong_reused_and_old_codes_answer_like_a_wrong_password_and_never_lock(site):
    clock = [time.time()]
    site.auth.clock = lambda: clock[0]

    def attempt(**values):
        h, body = site.owner_form(**values)
        status, out, page = site.request("/login", "POST", OWNER, h, body)
        return status, re.sub(rb'value="[^"]*"', b"", page), out.get("Set-Cookie", "")

    def wait():
        clock[0] = max(clock[0], max(site.auth.delays.until)) + 0.01

    wrong_pw = attempt(password="wrong-password-123")
    wait()
    wrong_code = attempt(code="000000" if site.code() != "000000" else "111111")
    wait()
    old_code = attempt(code=site.code(-2))
    wait()
    assert wrong_pw[:2] == wrong_code[:2] == old_code[:2]
    assert wrong_pw[0] == 200 and gateway.OWNER_FAIL_TEXT.encode() in wrong_pw[1]
    assert gateway.OWNER_COOKIE + "=" not in wrong_pw[2] + wrong_code[2] + old_code[2]
    # a used code is refused, also after a restart of the gateway
    code = site.code()
    assert attempt(code=code)[0] == 303
    wait()
    assert attempt(code=code)[0] == 200
    site.auth = site.gw.auth = gateway.Auth(site.root / "auth.json", site.users, site.log, clock=lambda: clock[0])
    assert attempt(code=code)[0] == 200
    # thirty wrong codes with the right password: a delay each time, never a lock
    for i in range(30):
        wait()
        attempt(code="%06d" % i if "%06d" % i != site.code() else "999999", address="203.0.113.%d" % i)
    wait()
    clock[0] += 31
    assert attempt(code=gateway.totp_code(site.secret, int(clock[0] // 30)), address="192.0.2.9")[0] == 303


def test_a_wrong_code_without_the_right_password_is_not_checked_and_a_reader_never_enters(site, monkeypatch):
    checked = []
    real = gateway.totp_step
    monkeypatch.setattr(gateway, "totp_step", lambda *a, **k: checked.append(a) or real(*a, **k))
    assert site.auth.sign_in_owner("owner1", "wrong-password-123", site.code(), "198.51.100.8") == "fail"
    assert checked == []
    site.auth.delays.success("owner:reader1")
    assert site.auth.sign_in_owner("reader1", site.pw["reader1"], site.code(), "198.51.100.9") == "fail"
    assert site.auth.new_owner_session("reader1") is None


def test_the_owner_session_ends_after_15_idle_minutes_and_after_one_hour(site, monkeypatch):
    now = [1000.0]
    monkeypatch.setattr(gateway.time, "monotonic", lambda: now[0])
    token = site.owner_session()
    now[0] += 14 * 60
    assert site.auth.owner_session(token) == "owner1"
    now[0] += 15 * 60 + 1
    assert site.auth.owner_session(token) is None
    token = site.owner_session()
    for _ in range(5):
        now[0] += 14 * 60
        site.auth.owner_session(token)
    assert site.auth.owner_session(token) is None
    token = site.owner_session()
    users.change(site.users, "reset", "owner1", False, rounds=ROUNDS)
    assert site.auth.owner_session(token) is None


def test_owner_posts_need_the_owner_origin_same_origin_fetch_and_the_session_csrf(site):
    token, other = site.owner_session(), site.owner_session()
    good = owner_headers(site, token, post=True)
    route = "/api/projects/tcp-q7m4/materials/imports"
    assert site.request(route, "POST", OWNER, good, "{}")[0] == 200
    bad = [dict(good, Origin="https://" + DOMAIN), dict(good, **{"Sec-Fetch-Site": "same-site"}),
           {k: v for k, v in good.items() if k != "Sec-Fetch-Site"}, dict(good, **{"X-AWB-CSRF": "1.forged"}),
           dict(good, Cookie=gateway.OWNER_COOKIE + "=" + other)]
    for h in bad:
        assert site.request(route, "POST", OWNER, h, "{}")[0] == 403
    assert site.request(route, "POST", OWNER, dict(good, **{"Content-Type": "text/plain"}), "{}")[0] == 415
    assert site.request("/api/owner/state", "POST", OWNER, good, "{}")[0] == 405
    assert len(site.unix["materials"].calls) == 1


def test_the_state_answer_carries_the_csrf_and_the_shared_password_flag(site):
    status, _, raw = site.request("/api/owner/state", host=OWNER, headers=owner_headers(site, site.owner_session()))
    data = json.loads(raw)
    assert status == 200 and data["shared_password_active"] is False and re.fullmatch(r"\d+\.[0-9a-f]{64}", data["csrf"])
    assert data["vault"] == {"state": "locked", "since": "2026-10-08T07:40:00Z"} and data["stale"] is False
    doc = contract.build()
    assert contract.validate(doc, data, contract.ref("OwnerState")) == []


# --------------------------------------------------------------------------- owner-actions


def test_owner_actions_answers_only_its_peer_and_only_from_its_status_file(tmp_path):
    """It refuses a peer other than the gateway's uid before a byte, answers the three routes from the status file
    alone (an opener records every file it opens) and never shows a word of a report next to it; without the status
    it answers 503. The vault folder does not exist."""
    status = tmp_path / "status.json"
    status.write_text(json.dumps(status_doc()))
    report = tmp_path / "private-report.md"
    report.write_text("candidate %s waits\n" % fx.CUSTOMER_FORMS[0])
    opened = []
    real_open = os.open

    def opener(path, flags, *a):
        opened.append(str(path))
        return real_open(path, flags, *a)

    def ask(server, path, level="owner"):
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.settimeout(5)
        s.connect(server.server_address)
        s.sendall(("GET %s HTTP/1.0\r\nX-AWB-Level: %s\r\n\r\n" % (path, level)).encode())
        data = b""
        try:
            while chunk := s.recv(65536):
                data += chunk
        except ConnectionResetError:
            pass
        s.close()
        return data

    other = owner_actions.Server(gateway.unix_listener(str(tmp_path / "a.sock")), os.getuid() + 1, status,
                                 tmp_path / "log")
    threading.Thread(target=other.serve_forever, daemon=True).start()
    assert ask(other, "/api/owner/state") == b""
    other.shutdown()
    other.server_close()
    assert (tmp_path / "log").glob("owner-*.tsv") and "\tpeer\t" in next((tmp_path / "log").glob("*.tsv")).read_text()

    mine = owner_actions.Server(gateway.unix_listener(str(tmp_path / "b.sock")), os.getuid(), status, tmp_path / "log")
    threading.Thread(target=mine.serve_forever, daemon=True).start()
    original = owner_actions.os.open
    owner_actions.os.open = opener
    try:
        answers = [ask(mine, route) for route in owner_actions.ROUTES]
        assert all(a.startswith(b"HTTP/1.0 200") for a in answers)
        assert {o for o in opened if not o.startswith(str(tmp_path / "log"))} == {str(status)}
        assert ask(mine, "/api/owner/state", level="reader").startswith(b"HTTP/1.0 403")
        assert ask(mine, "/api/owner/other").startswith(b"HTTP/1.0 404")
        status.unlink()
        assert ask(mine, "/api/owner/state").startswith(b"HTTP/1.0 503")
        status.symlink_to(report)
        assert ask(mine, "/api/owner/state").startswith(b"HTTP/1.0 503")
    finally:
        owner_actions.os.open = original
        mine.shutdown()
        mine.server_close()
    assert all(fx.CUSTOMER_FORMS[0] not in a.decode() for a in answers)
    body = json.loads(answers[1].split(b"\r\n\r\n", 1)[1])
    assert body["runs"][0]["publishable"] is True and "written" in body and "stale" in body
    log = "".join(p.read_text() for p in (tmp_path / "log").glob("*.tsv"))
    assert fx.CUSTOMER_FORMS[0] not in log and "/api/owner/state\tok\t" in log


def test_a_status_of_another_shape_is_never_served():
    for broken in (dict(status_doc(), extra=1), status_doc(intake=[{"customer": "Acme", "waiting": 1, "time": None}]),
                   status_doc(ui_runs=[{"id": "x y", "state": "completed", "finished": None, "validation": "PASS",
                                        "diff_bytes": 1, "candidate_only": False, "publishable": True}]),
                   status_doc(vault={"state": "open", "since": None})):
        with pytest.raises(status_shape.StatusError):
            status_shape.validate(broken)
    status_shape.validate(status_doc())


# --------------------------------------------------------------------------- the status file and the intake counts


def test_the_status_is_written_only_after_the_shape_and_the_name_check(tmp_path):
    out = tmp_path / "status" / "status.json"
    assert owner.write(status_doc(), out, checker=lambda text: "passed") == "passed"
    assert json.loads(out.read_text())["intake"][0]["customer"] == fx.CUSTOMER_CODE
    assert oct(out.stat().st_mode & 0o777) == "0o640"
    before = out.read_text()

    def finds(text):
        raise owner.StatusError("the status is withheld: the name check found name")

    with pytest.raises(owner.StatusError):
        owner.write(status_doc(written=owner.now_text(0)), out, checker=finds)
    with pytest.raises(owner.StatusError):
        owner.write(dict(status_doc(), extra=1), out, checker=lambda text: "passed")
    assert out.read_text() == before


def test_the_ui_runs_mark_only_the_newest_pass_run_publishable(tmp_path):
    db = sqlite3.connect(tmp_path / "queue.sqlite3")
    db.execute("CREATE TABLE jobs (id TEXT PRIMARY KEY, text TEXT, hash TEXT, state TEXT, created TEXT, started TEXT, "
               "finished TEXT, detail TEXT, apply_changes INTEGER)")
    rows = [("ui-1", "completed", "2026-10-06T10:00:00+00:00", 1, "PASS"),
            ("ui-2", "completed", "2026-10-07T10:00:00+00:00", 1, "PASS"),
            ("ui-3", "completed", "2026-10-08T10:00:00+00:00", 1, "FAIL"),
            ("ui-4", "running", None, 1, None)]
    for run_id, state, finished, applies, validation in rows:
        db.execute("INSERT INTO jobs VALUES (?,?,?,?,?,?,?,?,?)", (run_id, "", "", state, run_id, None, finished, "",
                                                                   applies))
        folder = tmp_path / "runs" / run_id
        folder.mkdir(parents=True)
        if validation:
            (folder / "validation.txt").write_text(validation + "\n")
        (folder / "change.diff").write_text("x" * 7)
    db.commit()
    db.close()
    runs = owner.ui_runs(tmp_path)
    assert [r["id"] for r in runs] == ["ui-4", "ui-3", "ui-2", "ui-1"]
    assert [r["publishable"] for r in runs] == [False, False, True, False]
    assert runs[1]["validation"] == "FAIL" and runs[0]["validation"] is None and runs[2]["diff_bytes"] == 7


def test_the_intake_and_the_review_write_the_counts_and_the_status_reads_them(home):
    f = home.inbox / "a.txt"
    f.write_text("Server 198.51.100.4 in eu-de\n", encoding="utf-8")
    res = intake.run([f], fx.CUSTOMER_CODE, home)
    counts = json.loads((home.vault / "intake-counts.json").read_text())
    assert counts[fx.CUSTOMER_CODE]["waiting"] == res.candidates
    assert re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ", counts[fx.CUSTOMER_CODE]["time"])
    intake_counts.record_intake(home, "CUST-NOPE1", 2)
    intake_counts.record_intake(home, "not a code", 2)
    intake_counts.record_intake(home, fx.CUSTOMER_CODE, 4, when=0)
    assert owner.intake(home) == [{"customer": fx.CUSTOMER_CODE, "waiting": 4, "time": "1970-01-01T00:00:00Z"}]
    assert oct((home.vault / "intake-counts.json").stat().st_mode & 0o777) == "0o600"


# --------------------------------------------------------------------------- D-F3 = no


def test_no_file_name_of_the_inbox_or_the_history_crosses(tmp_path):
    """A planted registered form in a bucket file name and in an import's name never appears in a web answer."""
    planted = fx.CUSTOMER_FORMS[0]
    browse = {"source": "customer", "limit_mb": 25, "files": [
        {"id": "a" * 64, "name": "%s Anforderungen.PDF" % planted, "etag": "e", "size": 10,
         "modified": "2026-10-08T07:58:12.000Z", "status": "new", "supported": True, "location": "Unassigned inbox"},
        {"id": "b" * 64, "name": "notes", "etag": "f", "size": 3, "modified": "2026-10-08T07:58:13.000Z",
         "status": "new", "supported": False, "location": "Project folder"}]}
    history = {"busy": False, "items": [
        {"id": "M-" + "A" * 24, "name": "%s.docx" % planted, "source": "customer", "state": "ready", "message": "m",
         "created": "2026-10-08T07:58:12+00:00", "version": 1, "filename": "%s.md" % planted, "size": 5}]}
    item = {"id": "M-" + "A" * 24, "version": 1, "filename": "%s.md" % planted, "imported": "x", "text": "t"}
    shown = [materials_api.without_names(browse, "browse"), materials_api.without_names(history, "history"),
             materials_api.without_names(item, "item")]
    text = json.dumps(shown, ensure_ascii=False)
    assert planted not in text and planted.casefold() not in text.casefold()
    assert [f["name"] for f in shown[0]["files"]] == ["file 1.pdf", "file 2"]
    assert shown[1]["items"][0]["name"] == "M-" + "A" * 24 + ".docx" and shown[2]["filename"] == "M-" + "A" * 24 + ".md"
    doc = contract.build()
    assert contract.validate(doc, shown[0], contract.ref("MaterialSources")) == []


def test_the_reader_view_of_the_tenants_carries_digits_never_the_domain_name():
    data = {"tenants": [{"alias": "test-1", "domain_name": "sample-domain-00000001", "status": "complete"}]}
    reader = tenant_api.reader_view(data)
    assert "domain_name" not in reader["tenants"][0] and reader["tenants"][0]["domain_digits"] == "00000001"
    assert data["tenants"][0]["domain_name"] == "sample-domain-00000001"
    assert tenant_api.domain_digits("no-digits") is None and tenant_api.domain_digits(None) is None


def test_the_contract_has_no_unlock_and_every_owner_operation_needs_the_owner_session():
    doc = contract.build()
    ops = list(contract.operations(doc))
    for method, path, op in ops:
        text = json.dumps(op).lower()
        assert not re.search(r"\b(unlock|lock|passphrase)\b", op["operationId"].lower()), op["operationId"]
        owner_side = path.startswith("/api/owner/") or path in (
            "/api/customers", "/api/customer-operations/{request_id}", "/api/project-operations/{request_id}") or \
            path.startswith("/api/projects/{code}/materials") or (path == "/api/projects" and method == "post")
        if owner_side:
            assert op["security"] == [{"owner": []}] and op["x-awb-state"] == "repository", (method, path)
            assert op["servers"][0]["url"] == "https://{owner_host}/"
        else:
            assert op.get("security") != [{"owner": []}], (method, path)
        assert "awb vault unlock" not in text and "awb keys unlock" not in text
    assert {p for _, p, _ in ops} >= set(OWNER_ROUTES)


# --------------------------------------------------------------------------- T9 step 3: Publish through the gateway


def test_publish_needs_the_csrf_of_this_session_and_a_fresh_code_before_owner_actions(site, tmp_path):
    secrets = tmp_path / "owner-publish.json"
    secrets.write_text(json.dumps({"owner1": site.secret}))
    site.actions.secrets = secrets
    unit_sock = gateway.unix_listener(str(tmp_path / "unit.sock"))
    got = []

    def unit():
        conn, _ = unit_sock.accept()
        got.append(conn.recv(1024))
        conn.sendall(b"published index.html.before-ui-20261008-009\n")
        conn.close()

    threading.Thread(target=unit, daemon=True).start()
    site.actions.publish_socket = str(tmp_path / "unit.sock")
    token, other = site.owner_session(), site.owner_session()
    good = owner_headers(site, token, post=True)
    body = lambda code: json.dumps({"run_id": "ui-20261008-009", "code": code})
    now = time.time()
    stale = gateway.totp_code(site.secret, int(now // 30) - 1)
    for headers, payload in ((dict(good, **{"X-AWB-CSRF": ""}), body(site.code())),
                             (dict(good, **{"X-AWB-CSRF": owner_headers(site, other, post=True)["X-AWB-CSRF"]}),
                              body(site.code())),
                             (good, json.dumps({"run_id": "ui-20261008-009"})), (good, body(stale)),
                             (good, body("12345"))):
        status, _, raw = site.request("/api/owner/publish", "POST", OWNER, headers, payload)
        assert status == 403, raw
    assert got == []
    status, _, raw = site.request("/api/owner/publish", "POST", OWNER, good, body(site.code()))
    assert status == 200 and json.loads(raw) == {"published": True, "backup": "index.html.before-ui-20261008-009"}
    assert got == [b"ui-20261008-009\n"]
    # the same code again is refused by the gateway itself
    status, _, raw = site.request("/api/owner/publish", "POST", OWNER, good, body(site.code()))
    assert status == 403 and json.loads(raw) == {"error": "The code is missing or not current."}
    # never on the main host
    reader = {"Cookie": "__Host-awb-session=" + site.reader_session(), "Origin": "https://" + DOMAIN,
              "Content-Type": "application/json"}
    assert site.request("/api/owner/publish", "POST", DOMAIN, reader, body(site.code()))[0] == 404
