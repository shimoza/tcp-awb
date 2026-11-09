"""RB: what a read block can reach (2026-11-09, after the first block run). A missing endpoint is proven by its
status when the block names it in `expect`; a block can send its calls to another region of the tenant; a selector
may hold spaces; a path into an answer such as `subnets.0.id` is not a web address for the text check of `kb how`.
Every id and value here is invented; no call leaves the process."""
from __future__ import annotations

import pytest

from awb import kb
from awb.tcp import recheck
from tests.test_recheck import FLAVORS, TODAY, Cloud, entry, go, project  # noqa: F401  (project is a fixture)

MISSING = "/v1/{project_id}/gadgets"


class Missing(Cloud):
    """The stand-in with one endpoint that answers 404 and the flavor list beside it."""

    def __init__(self, status=404):
        super().__init__()
        self.missing_status = status

    def __call__(self, req: dict) -> dict:
        if req.get("op") == "call" and req.get("path") == MISSING:
            self.calls.append(req)
            if self.missing_status == 200:
                return {"ok": True, "status": 200, "data": {"gadgets": []}, "text": ""}
            return {"ok": True, "status": self.missing_status, "data": {"error_code": "APIGW.0101"}, "text": ""}
        return super().__call__(req)


def missing_block(expect="expect: 404\n") -> str:
    return ("tier: read\n"
            "calls: GET ecs %s; GET ecs %s\n"
            "%s"
            "confirms: status is 404\n"
            "refutes: status is 200\n"
            "control: 2:flavors[name=s2.medium.1].name is s2.medium.1\n"
            "needs: none\n" % (MISSING, FLAVORS, expect))


def test_a_missing_endpoint_is_proven_by_the_status_the_block_expects(project):
    tried = ["GET of the gadgets path answered 404 on a test tenant", "the flavor list beside it answered 200"]
    e = entry("ECS offers no gadgets endpoint in eu-de", missing_block(), cls="api", tried=tried)
    code, lines, errors = go(Missing())
    assert code == 0, errors
    assert kb.get(e.id).checked == TODAY.isoformat(), lines
    e2 = entry("Widgets of the compute service cannot be listed by any public call", missing_block(), cls="api",
               tried=tried)
    go(Missing(status=200), ids=[e2.id])
    assert kb.get(e2.id).is_expired(), "the endpoint answered: the fact is refuted, never confirmed"


def test_a_status_the_block_does_not_expect_stays_undecided():
    block = {"expect": "404", "confirms": "status is 404", "refutes": "status is 200",
             "control": "2:flavors.0.name has s"}
    answers = [{"ok": True, "status": 403, "data": None}, {"ok": True, "status": 200, "data": {"flavors": [
        {"name": "s2"}]}}]
    assert recheck.read_verdict(block, answers)[0] == "undecided"
    answers[0]["status"] = 404
    assert recheck.read_verdict(block, answers) == ("confirmed", "confirms holds, refutes does not")
    assert recheck.read_verdict(dict(block, expect="2:404"), answers)[0] == "undecided", \
        "an expected status belongs to the call it names"


def test_a_status_test_stands_only_for_a_call_whose_status_is_expected():
    def problems(text):
        return kb.block_problems(kb.parse_block(text.splitlines()))
    assert not problems(missing_block())
    assert any("tests status alone" in p for p in problems(missing_block(expect="")))
    assert any("tests status alone" in p for p in problems(missing_block(expect="expect: 2:404\n")))
    assert any("expect is not" in p for p in problems(missing_block(expect="expect: 200\n")))
    assert any("expect is not" in p for p in problems(missing_block(expect="expect: 503\n")))


def test_a_block_sends_its_calls_to_the_region_it_names(project):
    block = ("tier: read\n"
             "region: eu-nl\n"
             "calls: GET ecs %s\n"
             "confirms: flavors[name=s9.medium.1].os-extra_specs.cond:operation:az has eu-de-01(normal)\n"
             "refutes: flavors[name=s9.medium.1].os-extra_specs.cond:operation:az lacks eu-de-01(normal)\n"
             "control: flavors[name=s2.medium.1].os-extra_specs.cond:operation:az has sellout\n"
             "needs: none\n" % FLAVORS)
    e = entry("ECS flavor s9.medium.1 is offered in eu-nl", block)
    assert kb.get(e.id).block["region"] == "eu-nl"
    cloud = Cloud()
    code, _, errors = go(cloud, ids=[e.id])
    assert code == 0, errors
    assert [c["region"] for c in cloud.calls] == ["eu-nl"]
    plain = entry("Small memory sizes of the compute family appear in the first zone of the main region",
                  block.replace("region: eu-nl\n", ""))
    cloud = Cloud()
    go(cloud, ids=[plain.id])
    assert [c["region"] for c in cloud.calls] == [None], "without a region the tenant's first one"


def test_a_region_is_a_region_and_belongs_to_a_read_tier():
    def problems(text):
        return kb.block_problems(kb.parse_block(text.splitlines()))
    base = missing_block()
    assert any("region is not" in p for p in problems(base.replace("tier: read\n", "tier: read\nregion: Holland\n")))
    write = ("tier: write\nregion: eu-nl\ncalls: recipe widget-check size=small\nconfirms: the widget is active\n"
             "refutes: the widget is not active\ncontrol: the forbidden path is refused\nneeds: none\n")
    assert any("only a read tier carries region" in p for p in problems(write))


def test_a_selector_may_hold_spaces():
    line = "roles[display_name=bucket list viewer].type is XA"
    assert recheck.parse_test(line) == (1, "roles[display_name=bucket list viewer].type", "is", "XA")
    assert kb._read_test(line) is not None
    answer = [{"ok": True, "status": 200, "data": {"roles": [{"display_name": "bucket list viewer", "type": "XA"}]}}]
    assert recheck.holds(recheck.parse_test(line), answer) is True
    assert recheck.parse_test("roles.0 type is XA") is None, "a space outside brackets still ends the path"


@pytest.fixture
def url_like(monkeypatch):
    """A name check that reads every dotted path as a web address and the word Planted as a name."""
    def hits(text, reg):
        out = [{"start": 0, "length": 4, "cls": "url"}] if ".0." in text else []
        if "Planted" in text:
            out.append({"start": 0, "length": 7, "cls": "name"})
        return out
    monkeypatch.setattr(kb, "_name_hits", hits)
    monkeypatch.setattr(kb, "_record_block", lambda text, hits: None)


def test_a_path_into_an_answer_is_no_web_address_for_the_block_check(url_like, tmp_path):
    reg = tmp_path / "register.tsv"
    block = kb.parse_block(["tier: read", "calls: GET vpc /v1/{project_id}/subnets",
                            "confirms: subnets.0.id has -", "refutes: subnets.0.id lacks -",
                            "control: subnets.0.name has a", "needs: none"])
    assert kb._block_text_problems(block, reg) == []
    assert kb.text_problems("the statement", "subnets.0.id is set", reg), "prose keeps the web address check"
    named = kb.parse_block(["tier: read", "calls: GET vpc /v1/{project_id}/subnets",
                            "confirms: subnets[name=Planted].id has -", "refutes: subnets.0.id lacks -",
                            "control: subnets.0.name has a", "needs: none"])
    assert any("name check (name 1)" in p for p in kb._block_text_problems(named, reg)), \
        "a name in a test line is still a name"
