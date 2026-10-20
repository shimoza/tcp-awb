"""G10 of the threat model: a project list per reader in users.json, enforced by the gateway on every project
route and in the project lists; an empty or missing list keeps every project open, as before the list.
"""
from __future__ import annotations

import http.client
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from awb.tcp.web import gateway, publish, users
from tests.test_web_gateway import DOMAIN, PASSWORD, write_auth

MINE, OTHER = "tcp-q7m4", "tcp-kx2a"


class Projects(BaseHTTPRequestHandler):
    """A stand-in for the projects service and the ask service: every project for every caller."""
    calls: list[tuple[str, str]] = []

    def do_GET(self):
        self.answer()

    def do_POST(self):
        self.rfile.read(int(self.headers.get("Content-Length", 0)))
        self.answer()

    def answer(self):
        self.calls.append((self.command, self.path))
        rows = [{"code": MINE, "state": "active"}, {"code": OTHER, "state": "active"}]
        if self.path == "/api/projects":
            body = {"source": "register", "projects": rows}
        elif self.path == "/api/board":
            body = {"summary": {"projects": 2}, "projects": rows, "build": "none"}
        else:
            body = {"code": self.path.split("/")[3], "answer": "yes"}
        data = json.dumps(body).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *args):
        pass


@pytest.fixture
def site(tmp_path):
    """Two readers, the first with a list of one project, in front of the stand-in backend."""
    write_auth(tmp_path / "auth.json", "awb", PASSWORD)
    path = tmp_path / "users.json"
    pw = {login: users.change(path, "add", login, False, rounds=1000) for login in ("reader1", "reader2")}
    users.change(path, "projects", "reader1", False, projects=[MINE])
    auth = gateway.Auth(tmp_path / "auth.json", path)
    backend = ThreadingHTTPServer(("127.0.0.1", 0), Projects)
    (tmp_path / "index.html").write_text("<html>page</html>")
    port = backend.server_port
    server = gateway.make_server(0, auth, tmp_path / "index.html", port, port, domain=DOMAIN)
    for s in (backend, server):
        threading.Thread(target=s.serve_forever, daemon=True).start()
    Projects.calls.clear()
    yield auth, server, path, pw
    for s in (server, backend):
        s.shutdown()
        s.server_close()


def call(server, path, token, method="GET", body=None):
    h = {"Host": DOMAIN, "Cookie": "__Host-awb-session=" + token}
    if method == "POST":
        h.update({"Origin": "https://" + DOMAIN, "Content-Type": "application/json"})
    c = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=5)
    c.request(method, path, body=body, headers=h)
    r = c.getresponse()
    out = (r.status, r.read())
    c.close()
    return out


def codes_of(body: bytes) -> list[str]:
    return [r["code"] for r in json.loads(body)["projects"]]


def test_a_reader_with_a_list_opens_the_listed_project_alone(site):
    auth, server, path, pw = site
    token = auth.new_session("reader1")
    status, body = call(server, "/api/projects/" + MINE, token)
    assert status == 200 and json.loads(body)["code"] == MINE
    assert call(server, "/api/projects/" + OTHER, token) == (404, b"Not found.\n")
    assert call(server, "/api/projects/%s/chat" % OTHER, token, "POST", b'{"question":"x"}')[0] == 404
    assert call(server, "/api/projects/%s/chat" % MINE, token, "POST", b'{"question":"x"}')[0] == 200
    assert not any(OTHER in p for _, p in Projects.calls)              # the backend never heard of the other one
    status, body = call(server, "/api/projects", token)
    assert status == 200 and codes_of(body) == [MINE] and json.loads(body)["source"] == "register"
    status, body = call(server, "/api/board", token)
    assert status == 200 and codes_of(body) == [MINE] and json.loads(body)["summary"] == {"projects": 2}


def test_a_reader_without_a_list_sees_every_project_as_before(site):
    auth, server, path, pw = site
    token = auth.new_session("reader2")
    assert call(server, "/api/projects/" + OTHER, token)[0] == 200
    assert codes_of(call(server, "/api/projects", token)[1]) == [MINE, OTHER]
    assert codes_of(call(server, "/api/board", token)[1]) == [MINE, OTHER]
    assert auth.projects_of("reader2") == () and auth.projects_of("") == ()      # the single login has no list


def test_the_list_follows_the_file_a_reset_keeps_it_and_no_code_clears_it(site):
    auth, server, path, pw = site
    token = auth.new_session("reader2")
    users.change(path, "projects", "reader2", False, projects=[OTHER])
    assert call(server, "/api/projects/" + MINE, token)[0] == 404        # an open session, the file read again
    assert codes_of(call(server, "/api/projects", token)[1]) == [OTHER]
    users.change(path, "reset", "reader1", False, rounds=1000)
    assert users.projects_of(path, "reader1") == [MINE]
    users.change(path, "projects", "reader1", False, projects=[])
    assert users.projects_of(path, "reader1") == [] and "projects" not in json.loads(path.read_text())["users"]["reader1"]
    assert call(server, "/api/projects/" + OTHER, auth.new_session("reader1"))[0] == 200
    with pytest.raises(users.Refused, match="tcp-xxxx"):
        users.change(path, "projects", "reader2", False, projects=["TCP-1234"])
    with pytest.raises(users.Refused, match="go with projects"):
        users.change(path, "add", "reader3", False, rounds=1000, projects=[MINE])
    with pytest.raises(users.Refused, match="no such login"):
        users.change(path, "projects", "reader3", False, projects=[MINE])


def test_a_hand_edited_list_that_is_not_a_list_of_codes_keeps_the_login_out(site):
    auth, server, path, pw = site
    assert auth.sign_in("reader1", pw["reader1"], "198.51.100.1") == "ok"
    data = json.loads(path.read_text())
    data["users"]["reader1"]["projects"] = MINE                          # a string, not a list
    path.write_text(json.dumps(data))
    assert auth.sign_in("reader1", pw["reader1"], "198.51.100.2") == "fail"
    data["users"]["reader1"]["projects"] = [MINE, "tcp-q7m4-x"]
    path.write_text(json.dumps(data))
    assert auth.sign_in("reader1", pw["reader1"], "198.51.100.3") == "fail"
    assert auth.sign_in("reader2", pw["reader2"], "198.51.100.4") == "ok"


def test_the_user_command_sets_shows_and_clears_the_list(tmp_path, monkeypatch):
    path = tmp_path / "users.json"
    users.change(path, "add", "reader1", False, rounds=1000)
    out = []
    monkeypatch.setattr(publish, "_say", out.append)
    monkeypatch.delenv("CLAUDECODE", raising=False)
    monkeypatch.delenv("CLAUDE_CODE_ENTRYPOINT", raising=False)
    host = publish.Host(geteuid=lambda: 1000, environ={"AWB_WEB_USERS": str(path)})
    assert publish.main(["user", "projects", "reader1", OTHER, MINE], host=host) == 0
    assert out[-1] == "projects of reader1: %s, %s" % (OTHER, MINE)          # sorted
    assert publish.main(["user", "list"], host=host) == 0
    assert out[-1] == "logins: reader1 (reader; projects %s, %s)" % (OTHER, MINE)
    assert publish.main(["user", "projects", "reader1"], host=host) == 0
    assert out[-1] == "projects of reader1: every project (no list)"
    assert publish.main(["user", "add", "reader2", MINE], host=host) == 1
    assert publish.main(["user", "projects"], host=host) == 1
