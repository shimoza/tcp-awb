"""Ask 2026-11-25-tcp-bj3o-2: the run end of a lab run removes LTS log groups and checks what a delete did.

(1) An LTS group answers log_group_id and not id: the create is recorded and the end deletes it, streams first. A
create whose answer holds no known id key is listed as "not recorded" instead of staying silent.
(2) After a delete the end reads the resource again: only a 404 counts as removed. A resource that answers is "still
there after delete" with its id, and an IAM project is named as kept by the platform.

The API is a stand-in function in place of labadmin._http, so no socket is used. Every id here is invented.
"""
from __future__ import annotations

import json

import pytest

from awb.tcp import labadmin
from tests.test_keys import calls_logged_here, no_waiting  # noqa: F401
from tests.test_lr_lab_runs import P_DE, fake, lab_call, run_op, session, svc  # noqa: F401

GROUP = "-".join(("%08x" % 0x1a2b, "%04x" % 0x3c4d, "4%03x" % 0x5e6, "8%03x" % 0x7f8, "%012x" % 0x9a0b))
STREAM = "-".join(("%08x" % 0x2b3c, "%04x" % 0x4d5e, "4%03x" % 0x6f7, "8%03x" % 0x809, "%012x" % 0xa1b2))
PROJECT_ID = "b" * 32


class Api:
    """What the platform answers: `keeps` are paths that answer a read after their delete, `answers` the body of a
    POST by its last path segment (status, data)."""

    def __init__(self):
        self.live: set[str] = set()
        self.keeps: set[str] = set()
        self.answers: dict[str, tuple[int, dict]] = {}
        self.sent: list[tuple[str, str]] = []

    def __call__(self, method, url, headers, data, timeout=0):
        path = url.split("?")[0].partition("//")[2].partition("/")[2]
        path = "/" + path
        if path == "/v3/auth/tokens":
            body = {"token": {"expires_at": "2099-01-01T00:00:00.000000Z",
                              "project": {"id": P_DE, "name": "eu-de", "domain": {"id": "d" * 32}},
                              "domain": {"id": "d" * 32}}}
            return 201, {"X-Subject-Token": "MII" + "t" * 90}, json.dumps(body).encode()
        self.sent.append((method, path))
        if method == "POST":
            status, body = self.answers[path.rsplit("/", 1)[-1]]
            return status, {}, json.dumps(body).encode()
        if method == "GET":
            return (200, {}, b"{}") if path in self.live else (404, {}, b'{"error_msg": "not found"}')
        if method == "DELETE":
            if path not in self.live:
                return 404, {}, b"{}"
            if path not in self.keeps:
                self.live.discard(path)
            return 204, {}, b""
        return 405, {}, b"{}"


@pytest.fixture
def api(monkeypatch):
    a = Api()
    monkeypatch.setattr(labadmin, "_http", a)
    return a


def test_bj3o_lts_group_and_stream_are_recorded_and_removed_streams_first(session, api):
    svc, uid = session
    run = run_op(svc, uid, "start")["run"]
    base = "/v2/%s/groups" % P_DE
    api.answers["groups"] = (201, {"log_group_id": GROUP})
    api.answers["stream"] = (201, {"log_stream_id": STREAM})
    group = lab_call(svc, uid, "POST", "lts", "/v2/{project_id}/groups", run, body={"log_group_name": "lr-group"})
    stream = lab_call(svc, uid, "POST", "lts", "/v2/{project_id}/groups/%s/stream" % GROUP, run,
                      body={"log_stream_name": "lr-stream"})
    assert group["created"] == ["lts groups"] and stream["created"] == ["lts stream"], (group, stream)
    api.live |= {"%s/%s" % (base, GROUP), "%s/%s/stream/%s" % (base, GROUP, STREAM)}
    api.sent.clear()
    ended = run_op(svc, uid, "end")
    assert ended["ok"] and ended["failed"] == [] and len(ended["removed"]) == 2, ended
    deletes = [p for m, p in api.sent if m == "DELETE"]
    assert deletes == ["%s/%s/stream/%s" % (base, GROUP, STREAM), "%s/%s" % (base, GROUP)]
    assert api.live == set()


def test_bj3o_an_answer_without_a_known_id_key_is_listed_as_not_recorded(session, api):
    svc, uid = session
    run = run_op(svc, uid, "start")["run"]
    api.answers["groups"] = (201, {"something_else": GROUP})
    made = lab_call(svc, uid, "POST", "lts", "/v2/{project_id}/groups", run, body={"log_group_name": "lr-group"})
    assert made["ok"]
    left = [c for r in run_op(svc, uid, "status")["runs"] for c in r["left"]]
    assert [c["state"] for c in left] == ["unrecorded"]
    ended = run_op(svc, uid, "end")
    assert [(c["error"], c["name"]) for c in ended["failed"]] == [("not recorded", "/v2/%s/groups" % P_DE)], ended
    assert ended["removed"] == []


def test_bj3o_a_resource_that_still_answers_after_its_delete_is_still_there(session, api):
    svc, uid = session
    run = run_op(svc, uid, "start")["run"]
    api.answers["groups"] = (201, {"log_group_id": GROUP})
    lab_call(svc, uid, "POST", "lts", "/v2/{project_id}/groups", run, body={})
    path = "/v2/%s/groups/%s" % (P_DE, GROUP)
    api.live.add(path)
    api.keeps.add(path)
    ended = run_op(svc, uid, "end")
    assert ended["removed"] == [] and [c["error"] for c in ended["failed"]] == [
        "still there after delete: %s" % GROUP], ended
    api.keeps.clear()
    again = run_op(svc, uid, "end")
    assert again["ok"] and again["failed"] == [] and len(again["removed"]) == 1, again


def test_bj3o_an_iam_project_the_platform_keeps_is_named_as_left_behind(session, api):
    svc, uid = session
    run = run_op(svc, uid, "start")["run"]
    api.answers["projects"] = (201, {"project": {"id": PROJECT_ID, "name": "eu-de_lr"}})
    lab_call(svc, uid, "POST", "iam", "/v3/projects", run, body={"project": {"name": "eu-de_lr"}})
    path = "/v3/projects/%s" % PROJECT_ID
    api.live.add(path)
    api.keeps.add(path)
    ended = run_op(svc, uid, "end")
    assert ended["removed"] == []
    assert [c["error"] for c in ended["failed"]] == [
        "the platform keeps an IAM project after delete: %s is left behind" % PROJECT_ID], ended
    assert [m for m, p in api.sent if m == "DELETE"] == ["DELETE"]
