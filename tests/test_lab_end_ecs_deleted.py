"""Ask 2026-11-25-tcp-s2z5-6: after a delete the platform still answers a GET of an ECS server for a while, with
status DELETED. The run end counts that server as removed; a server in any other status, and any other service that
still answers, stays "still there"."""
from __future__ import annotations

import json

from awb.tcp import labadmin

SERVER = {"service": "ecs", "method": "DELETE", "path": "/v1/{project_id}/cloudservers/srv-1"}


def _answer(monkeypatch, status: int, body: dict):
    monkeypatch.setattr(labadmin, "_send", lambda *a, **k: (status, {}, json.dumps(body).encode(), "/x"))


def test_an_ecs_server_in_status_deleted_is_gone(monkeypatch):
    _answer(monkeypatch, 200, {"server": {"id": "srv-1", "status": "DELETED"}})
    assert labadmin._gone(None, None, "run-1", SERVER) is True


def test_an_ecs_server_in_another_status_is_still_there(monkeypatch):
    _answer(monkeypatch, 200, {"server": {"id": "srv-1", "status": "ACTIVE"}})
    assert labadmin._gone(None, None, "run-1", SERVER) is False


def test_a_deleted_status_counts_only_for_ecs(monkeypatch):
    _answer(monkeypatch, 200, {"server": {"status": "DELETED"}})
    assert labadmin._gone(None, None, "run-1", dict(SERVER, service="evs")) is False


def test_a_404_is_gone_as_before(monkeypatch):
    _answer(monkeypatch, 404, {})
    assert labadmin._gone(None, None, "run-1", SERVER) is True
