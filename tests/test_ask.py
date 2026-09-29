"""The page for questions (awb/tcp/ask.py): the tool loop against a stand-in API, the name check before anything leaves, the
daily budget and the page. No call reaches the real API. Invented values only."""
from __future__ import annotations

import http.client
import threading
import urllib.parse

import pytest

from awb import kb
from awb.tcp import ask
from tests import fixtures


@pytest.fixture(autouse=True)
def usage(tmp_path, monkeypatch):
    monkeypatch.setattr(ask, "USAGE_FILE", tmp_path / "usage.json")
    monkeypatch.setattr(ask, "KEY_FILE", tmp_path / "no-key")


@pytest.fixture
def facts(home, register_path):
    kb.add("The invented cluster service offers three node flavors in the test region.", scope="tcp", tags=["cce"],
           grade="live", cls="stable", source="an invented live check", where=home, register_path=register_path)
    return home


class Api:
    """A stand-in for the Messages API: first a tool call, then an answer that names the entry."""

    def __init__(self):
        self.bodies = []

    def __call__(self, body, key):
        self.bodies.append(body)
        if len(self.bodies) == 1:
            return {"stop_reason": "tool_use", "usage": {"input_tokens": 100, "output_tokens": 20},
                    "content": [{"type": "text", "text": "Let me check."},
                                {"type": "tool_use", "id": "toolu_1", "name": "kb_find", "input": {"query": "node flavors"}}]}
        result = body["messages"][-1]["content"][0]["content"]
        entry = result.split('"id": "')[1].split('"')[0]
        return {"stop_reason": "end_turn", "usage": {"input_tokens": 300, "output_tokens": 50},
                "content": [{"type": "text", "text": "Three node flavors (%s)." % entry}]}


def test_the_answer_comes_from_the_tools_and_names_the_entry(facts):
    api = Api()
    res = ask.ask("Which node flavors does the cluster service offer?", facts, key="k", sender=api)
    assert res["answer"].startswith("Three node flavors (KB-") and res["tools"] == ["kb_find"]
    assert res["tokens"] == 470
    first = api.bodies[0]
    assert first["model"] == ask.MODEL == "claude-haiku-4-5-20251001" and first["max_tokens"] == 1000
    assert {t["name"] for t in first["tools"]} == {"kb_find", "price_find", "tenant_now"}
    assert api.bodies[1]["messages"][1]["role"] == "assistant"
    assert ask.budget_left() == (ask.DAILY_QUESTIONS - 1, ask.DAILY_TOKENS - 470)


def test_a_registered_name_or_structured_data_never_leaves(facts):
    api = Api()
    for q in ("What does %s run on the cluster service?" % fixtures.CUSTOMER_FORMS[0],
              "Is the server 203.0.113.40 still running?"):
        with pytest.raises(ask.AskError):
            ask.ask(q, facts, key="k", sender=api)
    assert api.bodies == []


def test_the_daily_budget_stops_the_page(facts, monkeypatch):
    monkeypatch.setattr(ask, "DAILY_QUESTIONS", 1)
    ask.ask("Which node flavors are there?", facts, key="k", sender=Api())
    with pytest.raises(ask.AskError, match="budget"):
        ask.ask("And the prices?", facts, key="k", sender=Api())


def test_a_refusal_an_endless_loop_and_a_missing_key_are_errors(facts, monkeypatch):
    with pytest.raises(ask.AskError, match="declined"):
        ask.ask("A question", facts, key="k", sender=lambda b, k: {"stop_reason": "refusal", "content": []})
    loop = {"stop_reason": "tool_use", "content": [{"type": "tool_use", "id": "t", "name": "tenant_now", "input": {}}]}
    with pytest.raises(ask.AskError, match="tool rounds"):
        ask.ask("A question", facts, key="k", sender=lambda b, k: loop)
    with pytest.raises(ask.AskError, match="not readable"):
        ask.ask("A question", facts)


def test_the_page_takes_a_question_from_its_own_origin_only(facts):
    server = ask.make_server(0, facts, sender=Api())
    threading.Thread(target=server.serve_forever, daemon=True).start()
    host = "%s:%d" % server.server_address

    def post(question, origin=None):
        c = http.client.HTTPConnection(*server.server_address, timeout=20)
        headers = {"Content-Type": "application/x-www-form-urlencoded", "Host": host}
        if origin:
            headers["Origin"] = origin
        c.request("POST", "/ask", urllib.parse.urlencode({"q": question}), headers)
        r = c.getresponse()
        return r.status, r.read().decode("utf-8")

    try:
        status, body = post("Which node flavors are there? <script>x</script>", origin="http://" + host)
        assert status == 200 and "Three node flavors (KB-" in body and "<script>x" not in body
        assert post("Which node flavors?", origin="https://elsewhere.example")[0] == 403
        c = http.client.HTTPConnection(*server.server_address, timeout=20)
        c.request("GET", "/ask")
        assert "questions left today" in c.getresponse().read().decode("utf-8")
    finally:
        server.shutdown()
        server.server_close()
