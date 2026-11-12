"""UX18: waiting locations and app destinations, including the reader boundary."""
import copy
import json
from types import SimpleNamespace
from unittest.mock import Mock
from urllib.parse import urlsplit

import pytest

from awb import cli, config, projects, vault
from awb.tcp.web import contract, gateway, projects_api as api
from tests import fixtures
from tests.test_web_gateway import DOMAIN


@pytest.fixture
def world(tmp_path, monkeypatch):
    conf = tmp_path / "paths.conf"
    conf.write_text(f"work_host = awb-test\nprojects = {tmp_path}\n")
    monkeypatch.setenv("AWB_CONF", str(conf))
    monkeypatch.setattr(config, "is_work_user", lambda: False)
    paths = config.Paths(shared=tmp_path / "shared", vault=tmp_path / "vault",
                         projects_root=tmp_path, kb=tmp_path / "kb")
    folder = tmp_path / "tcp-q7m4"
    folder.mkdir()
    (folder / "SCOPE.md").write_text("- goal: Check the deployment\n")
    (folder / "OPEN.md").write_text("# Open\n")
    row = projects.Project(folder.name, "lab", "none", "tcp", str(folder),
                           projects.memory_key(folder), "active", "2026-11-11")
    monkeypatch.setattr(projects, "load", lambda p: [row])
    monkeypatch.setattr(vault, "check_remote", lambda *args: [])
    monkeypatch.setattr(vault, "ping_state", lambda *args: {"state": "ready"})
    monkeypatch.setattr(config, "paths", lambda: paths)
    return paths, folder, conf


def answers(world, text):
    paths, folder, _ = world
    (folder / "STATE.md").write_text("## Waiting on\n" + text)
    return api.board(paths)["projects"][0], api.project_detail(folder.name, paths)


@pytest.mark.parametrize("text,where", [
    ("Owner: run: `awb keys status`", "his terminal"),
    ("Owner: tooling: fix the command", "development"),
    ("Owner: discuss the result", "project session"),
    ("Owner: here: confirm the result", "here"),
    ("Owner: the docs mention `awb keys status`", "project session"),
])
def test_waiting_items_have_locations(world, text, where):
    for result in answers(world, "- " + text + "\n"):
        assert result["waiting"] == {"items": [{"text": text, "where": where}], "total": 1}


def test_command_location_uses_the_owner_status_rule(world, monkeypatch):
    monkeypatch.setattr(projects, "item_command", lambda text: "awb keys status")
    for result in answers(world, "- Owner: discuss the result\n"):
        assert result["waiting"]["items"][0]["where"] == "his terminal"


def test_app_comes_from_paths_conf(world):
    paths, folder, conf = world
    for host in ("awb-test", "awb-test-2"):
        conf.write_text(f"work_host = {host}\nprojects = {paths.projects_root}\n")
        for result in answers(world, ""):
            assert result["app"] == {"host": host, "folder": str(folder)}
    conf.write_text("work_host = awb-test\nprojects = /different\n")
    for result in answers(world, ""):
        assert "app" not in result
    conf.write_text(f"projects = {paths.projects_root}\n")
    for result in answers(world, ""):
        assert "app" not in result


def test_waiting_text_is_checked_and_capped(world, monkeypatch):
    def check(text, sock):
        name = fixtures.PERSON_FORMS[0]
        return [{"start": text.index(name), "length": len(name), "cls": "name"}] if name in text else []

    monkeypatch.setattr(vault, "check_remote", check)
    text = "- Owner: " + fixtures.PERSON_FORMS[0] + "\n"
    for result in answers(world, text * 7):
        waiting = result["waiting"]
        assert waiting["total"] == 7 and len(waiting["items"]) == 5
        fixtures.assert_no_fixture_name(json.dumps(result))


def test_api_check_and_answers_include_the_new_contract(world):
    board, detail = answers(world, "- Owner: run: `awb keys status`\n")
    doc = contract.build()
    for value, name in ((board, "BoardProject"), (detail, "ProjectDetail")):
        assert "waiting" in value and "app" in value
        assert contract.validate(doc, value, contract.ref(name)) == []
        broken = copy.deepcopy(value)
        broken["waiting"]["items"][0]["where"] = "unknown"
        assert contract.validate(doc, broken, contract.ref(name))
    assert cli.main(["api", "check"]) == 0


def test_a_forbidden_project_returns_no_app(world, monkeypatch):
    paths, folder, _ = world
    _, detail = answers(world, "- Owner: discuss the result\n")
    allowed = [folder.name]
    handler = gateway.Gateway.__new__(gateway.Gateway)
    handler.server = SimpleNamespace(domain=DOMAIN, projects_port=8182,
                                     auth=SimpleNamespace(projects_of=lambda login: allowed))
    handler.user = ("reader1", "reader")
    handler.level = "reader"
    handler.command = "GET"
    handler.headers = {"Host": DOMAIN}
    handler.client_key = lambda: "local"
    handler.first_checks = lambda: urlsplit(handler.path)
    conn = Mock()
    monkeypatch.setattr(gateway.http.client, "HTTPConnection", lambda *a, **k: conn)

    def request(route):
        data = api.board(paths) if route == "/api/board" else detail
        response = Mock(status=200)
        response.read.return_value = json.dumps(data).encode()
        response.getheaders.return_value = [("Content-Type", "application/json")]
        conn.getresponse.return_value = response
        handler.path = route
        handler.reply = Mock()
        handler.route("test-session")
        return handler.reply.call_args.args[:2]

    for route in ("/api/board", "/api/projects/" + folder.name):
        status, body = request(route)
        assert status == 200 and b'"app"' in body
    allowed[:] = ["tcp-kx2a"]
    status, body = request("/api/board")
    assert status == 200 and json.loads(body)["projects"] == []
    assert b'"app"' not in body and str(folder).encode() not in body
    conn.reset_mock()
    status, body = request("/api/projects/" + folder.name)
    assert status == 404 and b'"app"' not in body and str(folder).encode() not in body
    conn.request.assert_not_called()
