"""The knowledge base, the price lookup and the API usage of the reader service (awb/tcp/web/reader_api.py): the
answers of the old portal pages as JSON, codes and counts only, every answer shaped as the contract says, no network.
Temporary folders and invented values only."""
from __future__ import annotations

import datetime
import http.client
import json
import threading
from decimal import Decimal
from http.server import ThreadingHTTPServer
from types import SimpleNamespace

import pytest

from awb import kb
from awb.tcp import portal, price, throttle
from awb.tcp.web import contract, projects_api, reader_api
from tests import fixtures

DOC = contract.build()


@pytest.fixture
def served(home, register_path):
    kb.add("The invented cluster service offers three flavors in the test region.", scope="tcp", tags=["cce"],
           grade="live", cls="stable", source="an invented live check", where=home, register_path=register_path)
    server = ThreadingHTTPServer(("127.0.0.1", 0), projects_api.Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield server
    server.shutdown()
    server.server_close()


def get(server, target: str, method: str = "GET"):
    c = http.client.HTTPConnection(*server.server_address, timeout=20)
    c.request(method, target)
    r = c.getresponse()
    raw = r.read().decode("utf-8")
    return r.status, (json.loads(raw) if raw else None), raw


def fits(schema: str, value) -> None:
    assert contract.validate(DOC, value, contract.ref(schema)) == []


def test_kb_finds_a_fact_with_its_grade(served):
    status, data, _ = get(served, "/api/kb?q=cluster+flavors")
    assert status == 200 and data["withheld"] == 0
    assert [f["grade"] for f in data["facts"]] == ["live"] and "three flavors" in data["facts"][0]["statement"]
    assert data["facts"][0]["expired"] is False
    fits("KbAnswer", data)
    assert get(served, "/api/kb?q=nothingmatcheszzz")[1]["facts"] == []


def test_kb_gives_what_the_portal_page_gives(served, home):
    data = get(served, "/api/kb?q=cluster+flavors")[1]
    page = portal.knowledge(home, {"q": "cluster flavors"})
    assert all(f["statement"] in page and f["id"] in page for f in data["facts"]) and len(data["facts"]) == 1


def test_kb_needs_words_and_cuts_them(served):
    assert get(served, "/api/kb")[0] == 400 and get(served, "/api/kb?q=+")[0] == 400
    status, data, _ = get(served, "/api/kb?q=" + "a" * 500)
    assert status == 200 and len(data["query"]) == reader_api.MAX_QUERY


def test_kb_withholds_a_form_registered_after_the_add_and_without_the_check_shows_nothing(served, home, monkeypatch):
    from awb import register

    kb.add("The invented cluster service runs a second site in %s." % fixtures.CONTROL_UNREGISTERED, scope="tcp",
           tags=["cce"], grade="live", cls="stable", source="an invented live check", where=home,
           register_path=home.register)
    assert fixtures.CONTROL_UNREGISTERED in get(served, "/api/kb?q=cluster+site")[2]
    register.add(home.register, fixtures.CUSTOMER_CODE + "-SITE-2", "SITE", fixtures.CONTROL_UNREGISTERED)
    status, data, raw = get(served, "/api/kb?q=cluster+site")
    assert status == 200 and fixtures.CONTROL_UNREGISTERED not in raw and data["withheld"] == 1

    def broken(*a, **k):
        raise kb.KBError("name check unavailable")

    monkeypatch.setattr(kb, "screen", broken)
    status, data, raw = get(served, "/api/kb?q=cluster+flavors")
    assert status == 503 and "three flavors" not in raw and "cannot run" in data["error"]


def test_kb_leaves_out_a_retired_entry(served, home):
    entry = kb.find("cluster flavors", where=home)[0][1]
    kb.retire(entry.id, "an invented reason", where=home)
    assert get(served, "/api/kb?q=cluster+flavors")[1]["facts"] == []


def _record(i: int):
    return SimpleNamespace(flavor="s3.large.%d" % i, name="invented flavor", os="Linux", unit="h", currency="EUR",
                           prices={"PAYG": Decimal("0.0500"), "R12": None, "R36": Decimal("20.00")}, id="x%d" % i)


def _fetch(monkeypatch, records, state="list"):
    got = SimpleNamespace(listing=SimpleNamespace(state=state), records=records,
                          source_line=lambda: "price API, fetched now")
    calls = []
    monkeypatch.setattr(price, "fetch", lambda service, region: calls.append((service, region)) or got)
    monkeypatch.setattr(price, "select", lambda recs, flavor=None, grep=None: [r for r in recs if not flavor
                                                                               or r.flavor == flavor])
    return calls


def test_price_gives_the_records_of_the_price_api(served, monkeypatch):
    calls = _fetch(monkeypatch, [_record(1)])
    status, data, raw = get(served, "/api/price?service=ECS&flavor=s3.large.1&region=eu-nl")
    assert status == 200 and calls == [("ecs", "eu-nl")]
    assert data["records"] == [{"flavor": "s3.large.1", "name": "invented flavor", "os": "Linux", "unit": "h",
                                "currency": "EUR", "prices": {"PAYG": "0.0500", "R12": None, "R36": "20.00"}}]
    assert data["total"] == 1 and data["truncated"] is False and data["source"] == "price API, fetched now"
    fits("PriceAnswer", data)
    fixtures.assert_no_fixture_name(raw, "the price answer")


def test_price_cuts_at_sixty_and_an_unknown_region_reads_as_the_first(served, monkeypatch):
    calls = _fetch(monkeypatch, [_record(i) for i in range(75)])
    data = get(served, "/api/price?service=ecs&region=mars")[1]
    assert calls == [("ecs", "eu-de")] and data["region"] == "eu-de"
    assert len(data["records"]) == reader_api.MAX_PRICE_ROWS and data["total"] == 75 and data["truncated"] is True


def test_price_refuses_a_bad_service_and_says_so_when_the_api_gives_nothing(served, monkeypatch):
    calls = _fetch(monkeypatch, [], state="failed")
    assert get(served, "/api/price")[0] == 400 and get(served, "/api/price?service=a%20b")[0] == 400
    assert calls == []
    status, data, _ = get(served, "/api/price?service=ecs")
    assert status == 503 and "no usable answer" in data["error"]

    def broken(service, region):
        raise OSError("down")

    monkeypatch.setattr(price, "fetch", broken)
    assert get(served, "/api/price?service=ecs")[0] == 503


def test_usage_counts_the_calls_of_the_month_by_day(served, home):
    now = datetime.datetime.now(datetime.timezone.utc)
    for status, service in ((200, "ecs"), (200, "evs"), (429, "ecs"), (503, "ecs")):
        throttle.log_call("test-1", service, "eu-de", "GET", status, 0.5, home.shared, now)
    status, data, raw = get(served, "/api/usage")
    assert status == 200 and data["month"] == now.strftime("%Y-%m")
    assert data["days"] == [{"day": now.strftime("%Y-%m-%d"), "calls": 4, "throttled": 1, "errors": 1,
                             "waited_seconds": 2.0,
                             "services": [{"service": "ecs", "calls": 3}, {"service": "evs", "calls": 1}]}]
    assert data["total"] == {"calls": 4, "throttled": 1, "errors": 1}
    fits("UsageAnswer", data)
    assert "test-1" not in raw       # the tenant alias stays out of the answer
    fixtures.assert_no_fixture_name(raw, "the usage answer")


def test_usage_without_a_record_is_empty_not_an_error(served):
    status, data, _ = get(served, "/api/usage")
    assert status == 200 and data["days"] == [] and data["total"] == {"calls": 0, "throttled": 0, "errors": 0}
    fits("UsageAnswer", data)


def test_usage_gives_the_counts_of_the_portal_lines(home):
    now = datetime.datetime.now(datetime.timezone.utc)
    for status in (200, 429):
        throttle.log_call("test-1", "ecs", "eu-de", "GET", status, 0, home.shared, now)
    month = now.strftime("%Y-%m")
    lines = "\n".join(throttle.usage_lines(home.shared, month))
    data = reader_api.usage_answer(home, {})[1]
    assert "throttled (429) this month: %d" % data["total"]["throttled"] in lines
    assert "ecs %d" % data["days"][0]["services"][0]["calls"] in lines


def test_the_routes_are_read_only_and_the_old_paths_stay_as_they_were(served):
    for path in reader_api.ROUTES:
        assert get(served, path + "?q=x&service=ecs", "HEAD")[0] in (200, 400, 503)
        c = http.client.HTTPConnection(*served.server_address, timeout=20)
        c.request("POST", path)
        assert c.getresponse().status == 405
    assert get(served, "/api/other")[0] == 404


def test_the_contract_lists_the_three_routes():
    ops = {p: op for m, p, op in contract.operations(DOC)}
    for path in reader_api.ROUTES:
        assert ops[path]["x-awb-state"] == "repository" and ops[path]["tags"] == ["reference"]
    assert contract.check(DOC) == []
