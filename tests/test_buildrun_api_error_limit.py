"""B-091: a run on the login that ends on api_error with 0 input and 0 output tokens is the subscription limit that
names no text (B-073 runs 1 and 2): the task is held for the limit and, with build_model = auto, runs again on the
key. A login run with tokens and a key run with the same result keep the old path (stopped).

The runner runs in the throw-away world of awb.buildrun.World with a stand-in client in front of the world's own:
the scenarios `apierr0` and `apierr120` answer every session with an api_error result and 0 or 120 tokens.
"""
from __future__ import annotations

import json
import sys

import pytest

from awb import build, buildbudget, buildrun

_WRAP = r'''
import json, sys, uuid
prompt = sys.stdin.read()
tokens = 0 if "SCENARIO: apierr0" in prompt else 120
print(json.dumps({"type": "result", "subtype": "success", "is_error": True, "num_turns": 2,
                  "terminal_reason": "api_error", "api_error_status": 529,
                  "usage": {"input_tokens": tokens, "output_tokens": 0},
                  "result": "API Error", "session_id": str(uuid.uuid4()), "total_cost_usd": 0}))
sys.exit(1)
'''


@pytest.fixture
def world(tmp_path):
    w = buildrun.World(tmp_path / "w")
    wrap = tmp_path / "wrap.py"
    wrap.write_text(_WRAP, encoding="utf-8")
    w.units.commands[buildrun.CLIENT] = [sys.executable, "-I", str(wrap)]
    d = w.root / "home" / ".claude"
    d.mkdir(mode=0o700, exist_ok=True)
    login = d / ".credentials.json"
    login.write_text(json.dumps({"claudeAiOauth": {"accessToken": "fake-login-a", "refreshToken": "fake-login-r",
                                                   "expiresAt": 1_900_000_000_000, "subscriptionType": "max"}}))
    login.chmod(0o600)
    key = tmp_path / "model.key"
    key.write_text("fake-model-key-for-tests\n")
    key.chmod(0o400)
    w.host.model, w.host.model_key = "login", key
    yield w
    w.close()


def polls(w, n: int = 8) -> None:
    r = w.runner()
    for _ in range(n):
        if not r.poll_once():
            break


def states(w, tid: str) -> list[str]:
    return [r.state for r in build.runs_of(tid, w.runs)]


def test_a_login_run_with_api_error_and_no_tokens_is_held_for_the_limit(world):
    tid = world.task("apierr0")
    polls(world)
    assert states(world, tid) == [buildbudget.HELD_LIMIT]
    assert buildbudget.load(world.runs)["until"] is not None


def test_with_auto_the_held_task_runs_again_on_the_key(world):
    world.host.mode = "auto"
    tid = world.task("apierr0")
    polls(world)
    runs = build.runs_of(tid, world.runs)
    assert [r.state for r in runs] == [buildbudget.HELD_LIMIT, "stopped: success"]
    assert [(r.folder / "model").read_text() for r in runs] == ["login\n", "key\n"]


def test_a_login_run_with_api_error_and_tokens_is_not_a_limit_hit(world):
    tid = world.task("apierr120")
    polls(world)
    assert states(world, tid) == ["stopped: success"]
    assert buildbudget.load(world.runs)["until"] is None


def test_a_key_run_with_api_error_and_no_tokens_is_not_a_limit_hit(world):
    world.host.model = "key"
    tid = world.task("apierr0")
    polls(world)
    assert states(world, tid) == ["stopped: success"]
    assert buildbudget.load(world.runs)["until"] is None


@pytest.mark.parametrize("obj, expected", [
    ({"terminal_reason": "api_error", "usage": {"input_tokens": 0, "output_tokens": 0}}, True),
    ({"subtype": "api_error", "usage": {"input_tokens": 0, "output_tokens": 0}}, True),
    ({"api_error_status": 429, "usage": {"input_tokens": 0, "output_tokens": 0}}, True),
    ({"terminal_reason": "api_error", "usage": {"input_tokens": 0, "output_tokens": 3}}, False),
    ({"terminal_reason": "api_error", "usage": {"input_tokens": 5, "output_tokens": 0}}, False),
    ({"terminal_reason": "api_error"}, False),
    ({"terminal_reason": "completed", "usage": {"input_tokens": 0, "output_tokens": 0}}, False),
    (None, False),
])
def test_empty_api_error_reads_the_reason_and_both_token_counts(obj, expected):
    assert buildbudget.empty_api_error(obj) is expected
