"""awb/harvest.py and the stop hook: a session that called the TCP API adds what it learned to the knowledge base
by itself. The stop hook asks once per batch of calls, never in a loop and never when nothing was called."""
from __future__ import annotations

import io
import json
import sys
from pathlib import Path

import pytest

from awb import harvest, hooks
from tests.test_lifecycle import spawn


def tool(cmd: str) -> str:
    return json.dumps({"type": "assistant", "message": {"content": [
        {"type": "tool_use", "name": "Bash", "input": {"command": cmd}}]}})


def say(text: str) -> str:
    return json.dumps({"type": "assistant", "message": {"content": [{"type": "text", "text": text}]}})


def transcript(path: Path, *lines: str) -> Path:
    with open(path, "a", encoding="utf-8") as f:
        for line in lines:
            f.write(line + "\n")
    return path


def test_no_live_call_no_question(home, tmp_path):
    t = transcript(tmp_path / "t.jsonl", tool("awb kb find ecs flavor"), tool("awb price find ecs"), say("done"))
    assert harvest.due(home, t, "s1") is None


def test_live_calls_without_a_knowledge_entry_ask_once(home, tmp_path):
    t = transcript(tmp_path / "t.jsonl", tool('awb cloud call GET vpc "/v1/{project_id}/vpcs" --tenant test-1'),
                   tool("awb cloud call POST ims /v2/cloudimages/action --tenant test-1 --role lab --body b.json"))
    msg = harvest.due(home, t, "s1")
    assert msg and "2 time(s)" in msg and "awb kb add --grade live" in msg
    assert harvest.due(home, t, "s1") is None                      # the same calls never ask twice
    transcript(t, say("nothing new"))
    assert harvest.due(home, t, "s1") is None
    transcript(t, tool("awb cloud call GET ecs /v1/{project_id}/cloudservers --tenant test-1"))
    assert "1 time(s)" in harvest.due(home, t, "s1")              # a new batch asks again


def test_a_knowledge_entry_after_the_last_call_counts_as_harvested(home, tmp_path):
    t = transcript(tmp_path / "t.jsonl", tool("awb cloud call GET vpc /v1/x --tenant test-1"),
                   tool('awb kb add --scope tcp --tag ims --grade live --class api --source "live call" "..."'))
    assert harvest.due(home, t, "s2") is None
    t2 = transcript(tmp_path / "t2.jsonl", tool('awb kb add --scope tcp --tag ims --grade live "..."'),
                    tool("awb cloud call GET vpc /v1/x --tenant test-1"))
    assert harvest.due(home, t2, "s3") is not None                # an entry before the call does not count


def test_sessions_are_kept_apart_and_odd_ids_are_safe(home, tmp_path):
    t = transcript(tmp_path / "t.jsonl", tool("awb cloud call GET vpc /v1/x --tenant test-1"))
    assert harvest.due(home, t, "../../etc/x") is not None
    assert harvest.due(home, t, "other") is not None
    assert all(f.parent == home.shared / "sessions" for f in (home.shared / "sessions").glob("harvest-*"))


def run_stop(payload, monkeypatch, capsys):
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(payload)))
    code = hooks.main(["stop"])
    return code, capsys.readouterr().err


def test_the_stop_hook_asks_the_session_to_harvest_in_a_project(home, register_path, tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(hooks, "for_this_user", lambda *a, **k: True, raising=False)
    pr = spawn(home, register_path)
    t = transcript(tmp_path / "t.jsonl", tool("awb cloud call GET vpc /v1/x --tenant test-1"))
    payload = {"cwd": pr.path, "transcript_path": str(t), "session_id": "s9"}
    code, err = run_stop(payload, monkeypatch, capsys)
    assert code == hooks.BLOCK and "Harvest before you stop" in err
    code, err = run_stop(payload, monkeypatch, capsys)
    assert code == hooks.OK and "Harvest" not in err
    # never while the client already continues because of a stop hook
    transcript(t, tool("awb cloud call GET vpc /v1/y --tenant test-1"))
    code, _ = run_stop(dict(payload, stop_hook_active=True), monkeypatch, capsys)
    assert code == hooks.OK


def test_outside_a_project_the_stop_hook_never_asks(home, tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(hooks, "for_this_user", lambda *a, **k: True, raising=False)
    t = transcript(tmp_path / "t.jsonl", tool("awb cloud call GET vpc /v1/x --tenant test-1"))
    code, err = run_stop({"cwd": str(tmp_path), "transcript_path": str(t), "session_id": "s8"}, monkeypatch, capsys)
    assert code == hooks.OK and "Harvest" not in err
