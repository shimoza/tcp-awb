"""UX20: the owner takes a server-read default through the owner host."""
import json
import io
import os
import struct
from concurrent.futures import ThreadPoolExecutor
from email.message import Message
from pathlib import Path
from types import SimpleNamespace

import pytest

from awb import config, devnote, ledger, ownerwait, projects
from awb.tcp.web import create_api, gateway, users
from tests import fixtures as fx

DOMAIN = "awb.example.test"
OWNER = "owner.example.test"
ROUTE = "/api/owner/items/take-default"
ITEMS = [
    "2026-11-11 owner: decide ecs-1. Default: keep ecs-1 stopped.",
    "2026-11-11 owner: **review** `ecs-2`. Default: keep ecs-2 stopped. " + "wait for capacity " * 8,
]


class Body(io.BytesIO):
    def restart(self, seconds):
        pass


class Site:
    """Run the real gateway dispatch and service handler with an in-process transport."""

    def __init__(self, root, home):
        users_path = root / "users.json"
        users.change(users_path, "add", "owner1", False, rounds=1000, level="owner")
        self.auth = gateway.Auth(root / "auth.json", users_path, None)
        self.projects = SimpleNamespace(mode="projects", web_uid=os.getuid(),
                                        store=create_api.ProjectStore(home, root / "project-state"))
        self.forwarded = []
        self.server = SimpleNamespace(auth=self.auth, domain=DOMAIN, owner_host=OWNER, v1_marker=None,
                                      owner_socket="owner-test.sock", project_socket="project-test.sock")

    def request(self, body, headers, host):
        response = []
        handler = object.__new__(gateway.Gateway)
        handler.server, handler.command, handler.path = self.server, "POST", ROUTE
        handler.headers = Message()
        for key, value in {"Host": host, "Content-Length": str(len(body)), **headers}.items():
            handler.headers[key] = value
        handler.rfile = Body(body)
        handler.connection = SimpleNamespace(settimeout=lambda seconds: None)
        handler.client_address = ("127.0.0.1", 12345)
        handler.reply = lambda status, body=b"", headers=None: response.append((status, headers, body))

        def forward(target, path, data, extra=None):
            self.forwarded.append(target)
            assert target == ("unix", self.server.project_socket)
            service = object.__new__(create_api.Handler)
            service.server, service.path, service.command = self.projects, path, "POST"
            service.connection = SimpleNamespace(getsockopt=lambda *args: struct.pack("3i", 0, os.getuid(), 0))
            service.headers = Message()
            for key, value in {"X-AWB-Level": handler.level, "Content-Type": "application/json",
                               "Content-Length": str(len(data))}.items():
                service.headers[key] = value
            service.rfile = Body(data)
            service.reply = lambda status, answer: response.append((status, {}, json.dumps(answer).encode()))
            service.do_POST()

        handler.forward = forward
        handler.dispatch()
        assert len(response) == 1
        return response[0]


@pytest.fixture
def action(home, tmp_path, monkeypatch):
    monkeypatch.setattr(config, "is_work_user", lambda: False)
    pr = projects.spawn(home, "project", "check the cluster", fx.CUSTOMER_CODE, home.register)
    root = Path(pr.path)
    (root / "OPEN.md").write_text("## Waiting on the owner\n\n" +
                                  "".join("- %s\n" % item for item in ITEMS))
    site = Site(tmp_path, home)
    token = site.auth.new_owner_session("owner1")
    headers = {"Cookie": gateway.OWNER_COOKIE + "=" + token, "Origin": "https://" + OWNER,
               "Sec-Fetch-Site": "same-origin", "Content-Type": "application/json",
               "X-AWB-CSRF": site.auth.owner_csrf(token)}

    def post(item=1, *, extra=None, request_headers=None, host=OWNER):
        return site.request(json.dumps({"code": pr.code, "item": item, **(extra or {})}).encode(),
                            headers if request_headers is None else request_headers, host)

    return home, pr, root, site, headers, post


def artifacts(action):
    home, _, root, *_ = action
    return sorted((root / devnote.FOLDER).glob("*.md")), ledger.load(home)


def test_valid_call_writes_one_fixed_note_one_ledger_line_and_prompt_notice(action):
    home, pr, root, _, _, post = action
    before = (root / "OPEN.md").read_text()
    status, _, raw = post()
    assert status == 200
    data = json.loads(raw)
    notes, entries = artifacts(action)
    assert len(notes) == len(entries) == 1
    printed = ownerwait.open_items(home)[0][1].text
    assert len(printed) == 110 and printed.endswith("...")
    assert notes[0].read_text().split("\n\n", 1)[1] == (
        "the owner takes the default of item: " + printed[:110] + "\n")
    assert data == {"code": pr.code, "item": 1, "note": devnote.FOLDER + "/" + notes[0].name, "taken": True}
    assert entries[0].project == pr.code and entries[0].deliverables == [data["note"]]
    notices = devnote.notices(home, "ux20-session", root)
    assert len(notices) == 1 and data["note"] in notices[0]
    assert devnote.notices(home, "ux20-session", root) == []
    assert (root / "OPEN.md").read_text() == before  # UX6 closes it after the session acts.
    fx.assert_no_fixture_name(raw.decode() + notes[0].read_text())


def test_out_of_range_index_writes_nothing(action):
    for index in (-1, len(ITEMS), 10**20):
        assert action[-1](index)[0] == 409
        assert artifacts(action) == ([], [])


def test_parked_project_writes_nothing(action):
    home, pr, *_ = action
    rows = projects.load(home)
    next(row for row in rows if row.code == pr.code).state = "parked"
    projects._save(home, rows)
    assert action[-1]()[0] == 409
    assert artifacts(action) == ([], [])


def test_another_origin_or_fetch_site_writes_nothing(action):
    headers, post = action[-2:]
    for change in ({"Origin": "https://" + DOMAIN}, {"Origin": "null"},
                   {"Sec-Fetch-Site": "same-site"}, {"Sec-Fetch-Site": "cross-site"}):
        assert post(request_headers={**headers, **change})[0] == 403
        assert artifacts(action) == ([], [])
    assert post()[0] == 200


def test_owner_session_and_its_csrf_are_required(action):
    _, _, _, server, headers, post = action
    assert post(request_headers={k: v for k, v in headers.items() if k != "Cookie"})[0] == 401
    assert post(request_headers={k: v for k, v in headers.items() if k != "X-AWB-CSRF"})[0] == 403
    assert post(request_headers={**headers, "X-AWB-CSRF": "invalid"})[0] == 403
    assert post(host=DOMAIN)[0] == 401
    reader = server.auth.new_session("owner1")
    assert post(host=DOMAIN, request_headers={**headers, "Cookie": "__Host-awb-session=" + reader})[0] == 404
    assert artifacts(action) == ([], [])
    assert post()[0] == 200


def test_repeated_call_uses_a_durable_receipt_and_writes_nothing(action):
    home, pr, _, _, _, post = action
    assert post()[0] == 200
    notes, entries = artifacts(action)
    before = {p: p.read_bytes() for p in home.shared.rglob("*") if p.is_file()}
    status, _, raw = post()
    assert status == 200
    assert json.loads(raw) == {"code": pr.code, "item": 1,
                               "note": devnote.FOLDER + "/" + notes[0].name, "taken": False}
    # A fresh service instance has no in-memory record of the first request.
    fresh = create_api.ProjectStore(home, action[3].projects.store.journal.path.parent)
    status, result, _ = fresh.take_default(pr.code, 1)
    assert status == 200 and result == json.loads(raw)
    assert artifacts(action) == (notes, entries)
    assert {p: p.read_bytes() for p in home.shared.rglob("*") if p.is_file()} == before
    assert len(devnote.notices(home, "ux20-retry", Path(pr.path))) == 1


def test_extra_request_text_never_enters_the_note_or_ledger(action):
    planted = fx.PERSON_FORMS[0]
    status, _, raw = action[-1](extra={"text": planted, "title": planted, "note": planted})
    assert status == 200
    notes, entries = artifacts(action)
    assert len(notes) == len(entries) == 1
    fx.assert_no_fixture_name(raw.decode() + notes[0].read_text() + json.dumps(entries[0].as_dict()))


def test_request_types_cannot_select_an_item_or_path(action):
    post = action[-1]
    for value in (True, False, "1", 1.0, None, [], {}):
        assert post(value)[0] == 400
    for value in ("../tcp-q7m4", fx.PERSON_FORMS[0], None, 1):
        status, _, raw = post(extra={"code": value})
        assert status == 400
        fx.assert_no_fixture_name(raw.decode())
    assert artifacts(action) == ([], [])


def test_unsafe_item_is_refused_without_repeating_its_text(action):
    _, _, root, _, _, post = action
    (root / "OPEN.md").write_text("## Waiting on the owner\n- owner: " + fx.PERSON_FORMS[0] + "\n")
    status, _, raw = post(0)
    assert status == 409
    fx.assert_no_fixture_name(raw.decode())
    assert artifacts(action) == ([], [])


def test_concurrent_calls_write_only_one_note_and_ledger_line(action):
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(lambda _: action[-1](), range(4)))
    assert [r[0] for r in results] == [200] * 4
    notes, entries = artifacts(action)
    assert len(notes) == len(entries) == 1


def test_items_with_the_same_printed_prefix_have_distinct_receipts(action):
    _, _, root, _, _, post = action
    prefix = "2026-11-11 owner: " + "wait for capacity " * 8
    (root / "OPEN.md").write_text("## Waiting on the owner\n" +
                                  "".join("- " + prefix + "Default: keep ecs-%d stopped.\n" % n
                                          for n in (1, 2)))
    assert post(0)[0] == post(1)[0] == 200
    notes, entries = artifacts(action)
    assert len(notes) == len(entries) == 2
    assert notes[0].read_text() == notes[1].read_text()


def test_gateway_sends_take_default_to_project_socket_not_owner_socket(action):
    site, post = action[3], action[-1]
    assert post()[0] == 200
    assert site.forwarded == [("unix", site.server.project_socket)]
    assert ("unix", site.server.owner_socket) not in site.forwarded
