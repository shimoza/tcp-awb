"""The read-only portal (awb/tcp/portal.py): pages over the knowledge, the prices, the projects and the reviews, on
127.0.0.1 only, GET and HEAD only, every value escaped, no query in the log. Invented values only."""
from __future__ import annotations

import http.client
import threading
from decimal import Decimal
from types import SimpleNamespace

import pytest

from awb import kb, projects
from awb.tcp import portal
from awb.tcp import price
from tests import fixtures


@pytest.fixture
def served(home, register_path):
    kb.add("The invented cluster service offers three flavors in the test region.", scope="tcp", tags=["cce"],
           grade="live", cls="stable", source="an invented live check", where=home, register_path=register_path)
    projects.spawn(home, "lab", "try the invented cluster service", None, register_path)
    server = portal.make_server(0, home)
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    yield server
    server.shutdown()
    server.server_close()


def get(server, path: str, method: str = "GET") -> tuple[int, dict, str]:
    c = http.client.HTTPConnection(*server.server_address, timeout=20)
    c.request(method, path)
    r = c.getresponse()
    body = r.read().decode("utf-8")
    return r.status, dict(r.getheaders()), body


def test_the_portal_listens_on_the_loopback_only(served):
    assert served.server_address[0] == "127.0.0.1"


def test_the_knowledge_page_finds_a_fact_with_its_grade(served):
    status, headers, body = get(served, "/kb?q=cluster+flavors")
    assert status == 200 and "three flavors" in body and ">live<" in body
    assert "default-src 'none'" in headers["Content-Security-Policy"] and headers["X-Frame-Options"] == "DENY"
    assert get(served, "/kb?q=nothingmatcheszzz")[2].count("No checked fact") == 1


def test_every_value_is_escaped(served):
    body = get(served, "/kb?q=%3Cscript%3Ealert(1)%3C/script%3E")[2]
    assert "<script>" not in body and "&lt;script&gt;" in body


def test_the_projects_and_reviews_pages_show_codes(served, home):
    body = get(served, "/projects")[2]
    code = projects.load(home)[0].code
    assert code in body and ">lab<" in body
    fixtures.assert_no_fixture_name(body, "the projects page")
    assert get(served, "/reviews")[0] == 200


def test_prices_come_from_the_price_api(served, monkeypatch):
    rec = SimpleNamespace(flavor="s3.large.2", name="invented flavor", os="Linux", unit="h", currency="EUR",
                          prices={"PAYG": Decimal("0.0500"), "R12": None, "R36": Decimal("20.00")}, id="x")
    got = SimpleNamespace(listing=SimpleNamespace(state="list"), records=[rec],
                          source_line=lambda: "price API, fetched now")
    calls = []
    monkeypatch.setattr(price, "fetch", lambda service, region: calls.append((service, region)) or got)
    monkeypatch.setattr(price, "select", lambda records, flavor=None, grep=None: records)
    status, _, body = get(served, "/price?service=ecs&region=eu-nl")
    assert status == 200 and "0.0500 EUR" in body and calls == [("ecs", "eu-nl")]
    assert get(served, "/price?service=ECS%3Bdrop&region=moon")[2].count("reads like ecs") == 1
    assert calls == [("ecs", "eu-nl")]


def test_only_reading_is_allowed_and_errors_carry_no_detail(served, monkeypatch):
    assert get(served, "/kb", "POST")[0] == 405
    assert get(served, "/kb", "DELETE")[0] == 405
    assert get(served, "/nowhere")[0] == 404
    assert get(served, "/health")[2] == "ok\n"

    def broken(p, q):
        raise ValueError("a value nobody should see")

    monkeypatch.setitem(portal.ROUTES, "/kb", broken)
    status, _, body = get(served, "/kb?q=x")
    assert status == 500 and "ValueError" in body and "nobody should see" not in body


def test_the_log_keeps_no_query(served, capsys):
    get(served, "/kb?q=" + fixtures.CUSTOMER_FORMS[1])
    err = capsys.readouterr().err
    assert "GET /kb 200" in err and fixtures.CUSTOMER_FORMS[1] not in err


def test_the_tenant_page_shows_what_runs_and_what_ran(served, home):
    from awb.tcp import tenants

    assert "no tenant yet" in get(served, "/tenants")[2]
    tenants.add(home, "test-1", "file:/nowhere/keys", ["eu-de"])
    body = get(served, "/tenants?t=test-1")[2]
    assert "test-1: now" in body and "no snapshot yet" in body
    assert "<script>" not in get(served, "/tenants?t=%3Cscript%3E")[2]
