"""The gateway against the contract (B-026, step 9 of the F1 plan): every operation of docs/api/openapi.yaml reaches
the gateway's route on its host and an owner operation never answers on the main host. The owner routes are one
table (`gateway.OWNER_ROUTES`) that owner-actions reads too. The gateway runs on a temporary Unix socket with its
forward recorded instead of sent, so no backend and no loopback is needed. Invented logins and codes."""
import json
import threading
import time
import urllib.parse

import pytest

from awb.tcp.web import contract, gateway, owner_actions, users

DOMAIN = "awb.example.test"
OWNER = "owner.example.test"
ROUNDS = 1000
SAMPLES = {"{code}": "tcp-q7m4", "{request_id}": "a" * 32, "{material_id}": "M-" + "A" * 24}
OWN = {"/login", "/logout", "/", "/architect-workbench.html"}
"""The paths the gateway answers itself."""
BODIES = {"application/json": b"{}", "application/x-www-form-urlencoded": b"a=b",
          "application/octet-stream": b"x"}


class Recording(gateway.Gateway):
    def forward(self, backend, target, body, extra=None):
        self.server.forwarded.append((self.command, backend, target))
        self.reply(200, b'{"recorded":true}', {'Content-Type': 'application/json; charset=utf-8'})


class Site:
    def __init__(self, root):
        auth_file = root / "auth.json"
        auth_file.write_text(json.dumps({"username": "awb", "salt": "00", "rounds": ROUNDS, "hash": "00"}))
        users_file = root / "users.json"
        users.change(users_file, "add", "reader1", False, rounds=ROUNDS)
        users.change(users_file, "add", "owner1", False, rounds=ROUNDS, level="owner")
        self.secret = json.loads(users_file.read_text())["users"]["owner1"]["totp"]
        self.auth = gateway.Auth(auth_file, users_file, root / "signin.log")
        (root / "index.html").write_text("<html>main page</html>")
        self.sock = str(root / "gw.sock")
        server = gateway.UnixServer(gateway.unix_listener(self.sock), Recording)
        self.gw = gateway._configure(server, self.auth, root / "index.html", 8180, 8181, 8182, 8183, DOMAIN)
        self.gw.owner_host = OWNER
        self.gw.forwarded = []
        threading.Thread(target=self.gw.serve_forever, daemon=True).start()

    def close(self):
        self.gw.shutdown()
        self.gw.server_close()

    def call(self, method, target, owner_host=False, media=None, body=None):
        """(status, the forward it caused or None), each call in a new session (POST /logout ends one)."""
        self.reader = self.auth.new_session("reader1")
        self.owner = self.auth.new_owner_session("owner1")
        before = len(self.gw.forwarded)
        if owner_host:
            headers = {"Host": OWNER, "Cookie": gateway.OWNER_COOKIE + "=" + self.owner}
            if method == "POST":
                headers.update({"Origin": "https://" + OWNER, "Sec-Fetch-Site": "same-origin",
                                "X-AWB-CSRF": self.auth.owner_csrf(self.owner)})
        else:
            headers = {"Host": DOMAIN, "Cookie": "__Host-awb-session=" + self.reader}
            if method == "POST":
                headers.update({"Origin": "https://" + DOMAIN, "Sec-Fetch-Site": "same-origin"})
        if method == "POST":
            media = media or "application/json"
            headers["Content-Type"] = media
            body = BODIES[media] if body is None else body
        c = gateway.UnixConnection(self.sock, timeout=10)
        try:
            c.request(method, target, body=body, headers=headers)
            r = c.getresponse()
            r.read()
            status = r.status
        finally:
            c.close()
        after = self.gw.forwarded[before:]
        assert len(after) <= 1
        return status, after[0] if after else None


@pytest.fixture
def site(tmp_path):
    s = Site(tmp_path)
    yield s
    s.close()


def concrete(path, op):
    for name, value in SAMPLES.items():
        path = path.replace(name, value)
    query = {}
    for p in op.get("parameters", []):
        if p.get("in") == "query" and p.get("required"):
            query[p["name"]] = (p.get("schema", {}).get("enum") or ["x"])[0]
    return path + ("?" + urllib.parse.urlencode(query) if query else "")


def owner_side(op):
    return op.get("security") == [{"owner": []}]


def media_of(op):
    return next(iter(op.get("requestBody", {}).get("content", {})), None)


def operations():
    return list(contract.operations(contract.build()))


def test_every_operation_of_the_contract_reaches_its_route_on_its_host(site):
    missed = []
    for method, path, op in operations():
        target = concrete(path, op)
        body = None
        if path == "/api/owner/publish":
            body = json.dumps({"code": gateway.totp_code(site.secret, int(time.time() // 30))}).encode()
        status, sent = site.call(method.upper(), target, owner_side(op), media_of(op), body)
        if path in OWN:
            if status in (404, 405) or sent is not None:
                missed.append((method, path, status))
        elif sent is None or sent[0] != method.upper() or sent[2] != target:
            missed.append((method, path, status))
    assert missed == [], "the gateway does not route these operations of the contract: %s" % missed


def test_no_owner_operation_answers_on_the_main_host(site):
    for method, path, op in operations():
        if not owner_side(op):
            continue
        status, sent = site.call(method.upper(), concrete(path, op), False, media_of(op))
        assert sent is None and status in (403, 404), (method, path, status)
    assert site.gw.forwarded == []


def test_the_owner_route_table_is_the_owner_operations_of_the_contract(site):
    owner_ops = {(m.upper(), p) for m, p, op in operations() if owner_side(op)}
    rows = {(m, p) for m, p, _, _ in gateway.OWNER_ROUTES}
    assert rows <= owner_ops
    # every owner operation outside the table is a route of a backend of the owner host (the moved routes)
    for method, path in owner_ops - rows:
        assert gateway.is_moved(concrete(path, {}), method) or (method, path) == ("POST", "/api/projects"), path


def test_each_owner_route_goes_to_the_socket_of_its_row(site):
    site.gw.owner_socket, site.gw.owner_tenants_socket = "/run/test-owner.sock", "/run/test-owner-tenants.sock"
    for method, path, sock, query in gateway.OWNER_ROUTES:
        body = None
        if method == "POST":
            body = json.dumps({"code": gateway.totp_code(site.secret, int(time.time() // 30))}).encode()
        status, sent = site.call(method, path + ("" if method == "POST" else "?refresh=1"), True, body=body)
        assert status == 200 and sent == (method, ("unix", getattr(site.gw, sock)),
                                          path + ("?refresh=1" if query else "")), path
        if method == "GET":
            assert site.call("HEAD", path, True)[1] == ("HEAD", ("unix", getattr(site.gw, sock)), path)
            assert site.call("POST", path, True)[0] == 405
    assert site.call("GET", "/api/owner/publish", True) == (404, None)
    assert site.call("GET", "/api/owner/other", True) == (404, None)


def test_owner_actions_answers_the_rows_of_its_socket():
    rows = [(m, p) for m, p, sock, _ in gateway.OWNER_ROUTES if sock == "owner_socket"]
    assert owner_actions.ROUTES == tuple(p for m, p in rows if m == "GET") == gateway.OWNER_ACTIONS
    assert owner_actions.ROUTES == ("/api/owner/state", "/api/owner/ui-runs", "/api/owner/intake", "/api/ui-runs")
    assert [p for m, p in rows if m == "POST"] == [owner_actions.PUBLISH] == ["/api/owner/publish"]
    assert gateway.OWNER_POSTS == ("/api/owner/publish",)
