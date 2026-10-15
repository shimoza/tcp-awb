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
    assert first["model"] == ask.MODEL == "claude-opus-5-5" and first["max_tokens"] == 16000
    assert {t["name"] for t in first["tools"]} == {"kb_find", "price_find", "tenant_now"}
    assert api.bodies[1]["messages"][1]["role"] == "assistant"
    assert ask.budget_left() == (ask.DAILY_QUESTIONS - 1, ask.DAILY_TOKENS - 470)


def test_kb_find_withholds_a_form_registered_after_the_add(facts, monkeypatch):
    """TM0 item 6: the tool hands the model no entry whose form was registered after the add, and nothing at all
    when the check cannot run."""
    from awb import register

    kb.add("The invented node flavors also run in %s." % fixtures.CONTROL_UNREGISTERED, scope="tcp", tags=["cce"],
           grade="live", cls="stable", source="an invented live check", where=facts, register_path=facts.register)
    assert fixtures.CONTROL_UNREGISTERED in ask.run_tool(facts, "kb_find", {"query": "node flavors"})
    register.add(facts.register, fixtures.CUSTOMER_CODE + "-SITE-2", "SITE", fixtures.CONTROL_UNREGISTERED)
    out = ask.run_tool(facts, "kb_find", {"query": "node flavors"})
    assert fixtures.CONTROL_UNREGISTERED not in out and "three node flavors" in out

    def broken(*a, **k):
        raise kb.KBError("name check unavailable")

    monkeypatch.setattr(kb, "screen", broken)
    out = ask.run_tool(facts, "kb_find", {"query": "node flavors"})
    assert "flavors" not in out and "cannot run" in out


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


def _reply(stop, content):
    return lambda body, key: {"stop_reason": stop, "usage": {"input_tokens": 10, "output_tokens": 5}, "content": content}


def test_the_request_carries_the_effort_and_the_model(facts, monkeypatch):
    api = Api()
    ask.ask("Which node flavors does the cluster service offer?", facts, key="k", sender=api)
    assert api.bodies[0]["output_config"] == {"effort": "medium"} and api.bodies[0]["model"] == "claude-opus-5-5"
    monkeypatch.setenv("AWB_ASK_EFFORT", "high")
    api = Api()
    ask.ask("Which node flavors does the cluster service offer?", facts, key="k", sender=api)
    assert all(b["output_config"] == {"effort": "high"} for b in api.bodies)


def test_a_bad_effort_is_refused_at_start(facts, monkeypatch):
    monkeypatch.setenv("AWB_ASK_EFFORT", "ultra")
    with pytest.raises(ask.AskError, match="AWB_ASK_EFFORT"):
        ask.make_server(0)
    with pytest.raises(ask.AskError, match="AWB_ASK_EFFORT"):
        ask.ask("A question?", facts, key="k", sender=_reply("end_turn", []))
    assert ask.main(["serve"]) == 2


def test_a_max_tokens_stop_is_an_error_not_a_cut_answer(facts):
    cut = _reply("max_tokens", [{"type": "thinking", "thinking": "x", "signature": "s"},
                                {"type": "text", "text": "Three node fla"}])
    with pytest.raises(ask.AskError, match="token limit"):
        ask.ask("Which node flavors does the cluster service offer?", facts, key="k", sender=cut)


def test_thinking_blocks_are_kept_in_the_history_and_never_in_the_answer(facts):
    thinking = {"type": "thinking", "thinking": "SECRET-REASONING", "signature": "sig1"}
    bodies = []

    def api(body, key):
        bodies.append(body)
        if len(bodies) == 1:
            return {"stop_reason": "tool_use", "usage": {}, "content": [thinking, {
                "type": "tool_use", "id": "toolu_1", "name": "kb_find", "input": {"query": "node flavors"}}]}
        return {"stop_reason": "end_turn", "usage": {}, "content": [
            {"type": "thinking", "thinking": "MORE-REASONING", "signature": "sig2"}, {"type": "text", "text": "Done."}]}

    res = ask.ask("Which node flavors does the cluster service offer?", facts, key="k", sender=api)
    assert res["answer"] == "Done." and "REASONING" not in res["answer"]
    assert bodies[1]["messages"][1]["content"][0] == thinking


# --------------------------------------------------------------------------- one engine (B-022, F1 step 4)


def test_the_page_and_the_ask_api_send_the_model_the_same_request(facts, tmp_path, monkeypatch):
    """B-022: `awb ask`, the Ask page and POST /api/ask run the one engine (chat_service.engine): a general question
    sends the same model, effort, cap, system prompt, tools and messages through either door and gets the same
    answer and the same tools."""
    from awb import vault
    from awb.tcp.web import chat_service

    monkeypatch.setattr(vault, "ping", lambda *a, **k: "unlocked")
    monkeypatch.setattr(vault, "check_remote", lambda *a, **k: [])
    page, api = Api(), Api()
    res = ask.ask("Which node flavors does the cluster service offer?", facts, key="k", sender=page)
    status, body = chat_service.Conversations(tmp_path / "chat.sqlite3", facts, sender=api).ask_json(
        "Which node flavors does the cluster service offer?")
    assert status == 200 and body["answer"] == res["answer"] and body["tools"] == res["tools"] == ["kb_find"]
    assert len(page.bodies) == 2 and page.bodies == api.bodies


def test_the_cli_ask_fails_closed_on_an_unreadable_usage_record(facts):
    """B-022: the budget reader of the web Ask: an unreadable usage record refuses before anything is sent."""
    ask.USAGE_FILE.write_text("{broken", encoding="utf-8")
    api = Api()
    with pytest.raises(ask.AskError, match="usage record"):
        ask.ask("Which node flavors does the cluster service offer?", facts, key="k", sender=api)
    assert api.bodies == []


def test_every_round_reserves_its_tokens_and_a_reply_without_usage_keeps_the_reservation(facts):
    seen = []

    def api(body, key):
        seen.append(ask.budget_left()[1])
        return {"stop_reason": "end_turn", "usage": {"input_tokens": 10, "output_tokens": 5},
                "content": [{"type": "text", "text": "Done."}]}

    assert ask.ask("Which node flavors does the cluster service offer?", facts, key="k", sender=api)["tokens"] == 15
    assert seen[0] <= ask.DAILY_TOKENS - 16000 and ask.budget_left()[1] == ask.DAILY_TOKENS - 15
    res = ask.ask("Which node flavors does the cluster service offer?", facts, key="k",
                  sender=lambda body, key: {"stop_reason": "end_turn", "content": [{"type": "text", "text": "Done."}]})
    assert res["answer"] == "Done." and res["tokens"] == 0
    assert ask.budget_left()[1] <= ask.DAILY_TOKENS - 15 - 16000


def test_a_tool_answer_with_a_registered_name_never_reaches_the_model(facts, monkeypatch):
    """B-022: the tool answers pass the screen of the engine; a hit is withheld and the loop goes on."""
    import json

    name = fixtures.CUSTOMER_FORMS[0]
    monkeypatch.setattr(ask, "run_tool", lambda p, tool, args: json.dumps([{"id": "KB-AAAA", "fact": name + " runs it."}]))
    bodies = []

    def api(body, key):
        bodies.append(json.dumps(body))
        if len(bodies) == 1:
            return {"stop_reason": "tool_use", "usage": {"input_tokens": 10, "output_tokens": 5}, "content": [
                {"type": "tool_use", "id": "toolu_1", "name": "kb_find", "input": {"query": "node flavors"}}]}
        return {"stop_reason": "end_turn", "usage": {"input_tokens": 10, "output_tokens": 5},
                "content": [{"type": "text", "text": "No checked fact covers this question."}]}

    res = ask.ask("Which node flavors does the cluster service offer?", facts, key="k", sender=api)
    assert res["answer"] == "No checked fact covers this question." and res["tools"] == ["kb_find"]
    assert name not in bodies[1] and "withheld by the data check" in bodies[1]


def test_an_answer_or_a_tool_request_with_a_registered_name_stops(facts, monkeypatch):
    name = fixtures.CUSTOMER_FORMS[0]
    with pytest.raises(ask.AskError) as err:
        ask.ask("Which node flavors does the cluster service offer?", facts, key="k",
                sender=_reply("end_turn", [{"type": "text", "text": name + " runs three node flavors."}]))
    assert name not in str(err.value)
    ran = []
    monkeypatch.setattr(ask, "run_tool", lambda *a: ran.append(a) or "[]")
    with pytest.raises(ask.AskError):
        ask.ask("Which node flavors does the cluster service offer?", facts, key="k", sender=_reply(
            "tool_use", [{"type": "tool_use", "id": "t", "name": "kb_find", "input": {"query": name}}]))
    assert ran == []


def test_the_model_loop_is_written_once():
    from pathlib import Path

    from awb.tcp.web import chat_service

    assert "stop_reason" not in Path(ask.__file__).read_text(encoding="utf-8")
    assert Path(chat_service.__file__).read_text(encoding="utf-8").count("stop_reason") == 3
