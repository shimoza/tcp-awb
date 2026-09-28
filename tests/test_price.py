"""awb/tcp/price.py against a stand-in price API with invented rates (tests/tcp_fake.py)."""
from __future__ import annotations

import ast
import datetime
import socket
from decimal import Decimal
from pathlib import Path

import pytest

import awb
from awb import cli, config, jobs
from awb.tcp import price
from tests.tcp_fake import FakePriceAPI, default_records, record

ROOT = Path(awb.__file__).resolve().parent
TODAY = datetime.date(2026, 9, 24)


@pytest.fixture
def api(monkeypatch):
    with FakePriceAPI(cached_at="2026-09-17 12:22:31") as f:
        monkeypatch.setenv("AWB_PRICE_API", f.url)
        yield f


@pytest.fixture(autouse=True)
def no_waiting(monkeypatch):
    monkeypatch.setattr(jobs.time, "sleep", lambda s: None)


def records(region: str = "eu-de") -> list[price.Record]:
    return [price.Record.from_raw(r) for r in default_records() if r["region"] == region]


def listing_of(recs) -> price.Fetch:
    return price.Fetch(jobs.Listing.of(recs), "test", "2026-09-24 12:00 UTC")


def row(**kw) -> dict:
    base = {"id": "TEST_S3_LARGE_2_LNX", "term": "PAYG", "unit_price": "0.085", "quantity": "2", "hours": "720",
            "total": "122.40", "source": "price API", "date": "2026-09-24", "line": 2}
    base.update(kw)
    return base


def check(r: dict, recs=None) -> price.RowResult:
    f = listing_of(records() if recs is None else recs)
    return price.check_row(r, lambda region: f, today=TODAY)


# --------------------------------------------------------------------------- parsing


def test_amounts_are_read_strictly():
    assert price.parse_amount("0.117000 EUR") == (Decimal("0.117000"), "EUR")
    assert price.parse_amount("1,234.560000 EUR") == (Decimal("1234.560000"), "EUR")
    assert price.parse_amount("0,117000 EUR") is None          # the de variant: a decimal comma
    assert price.parse_amount("1.234,56 EUR") is None
    assert price.parse_amount("0.117000") is None
    assert price.parse_amount("0,117 EUR") is None             # three digits after a comma: still no amount
    assert price.parse_amount("12 EUR") is None                # the API always writes a decimal point
    assert price.parse_amount(0.117) is None


def test_sheet_numbers_need_a_decimal_point():
    assert price.parse_number("0.085") == price.Number(Decimal("0.085"), 3)
    assert price.parse_number("1,234.56").value == Decimal("1234.56")
    assert price.parse_number("12 EUR").value == Decimal("12")
    assert price.parse_number("€ 12.5").places == 1
    assert price.parse_number("0,12") is None                  # 0.12 or 12? refused
    assert price.parse_number("0,085") is None                 # 0.085 or 85? refused
    assert price.parse_number("1,085") is None                 # 1085 or 1.085? refused
    assert price.parse_number("1.234,56") is None
    assert price.parse_number("") is None


def test_hourly_units():
    for unit in ("h", "Mbit/h", "rules/h", "LCU/h/zone", "Cores/Brokers/h"):
        assert price.is_hourly(unit), unit
    for unit in ("h/month", "GB hours", "GB/month", "Item/month", ""):
        assert not price.is_hourly(unit), unit


# --------------------------------------------------------------------------- fetch


def test_fetch_lists_the_records_of_one_service_and_region(api):
    f = price.fetch("ecs", "eu-de")
    assert f.listing.state == jobs.LIST and {r.id for r in f.records} == {
        "TEST_S3_LARGE_2_LNX", "TEST_S3_LARGE_2_WIN", "TEST_S3_XLARGE_2_LNX"}
    assert f.count == 3 and f.cached_at == "2026-09-17 12:22:31"
    assert "served from the API's cache of 2026-09-17 12:22:31 (server time)" in f.source_line()
    assert f.source_line().endswith("day(s) old (KB-5LLN)")
    fresh = price.Fetch(f.listing, f.url, "2026-09-17 10:00 UTC", "2026-09-17 12:22:31")
    assert "old" not in fresh.source_line()
    assert api.calls[-1]["sn"] == "ecs" and api.calls[-1]["rn"] == "eu-de"
    big = price.fetch("dws", "eu-de").records[0]
    assert big.price("PAYG") == Decimal("1234.500000")


def test_fetch_follows_the_pages(api):
    api.cap = 2
    f = price.fetch(None, "eu-de")
    assert f.listing.state == jobs.LIST and len(f.records) == 6
    assert [c.get("limitFrom") for c in api.calls] == [None, "2", "4"]


def test_empty_is_an_answer_and_an_unknown_service_is_not(api):
    empty = price.fetch("ecs", "eu-ch2")
    assert empty.listing.state == jobs.EMPTY and empty.count == 0
    unknown = price.fetch("nosuchservice", "eu-de")
    assert unknown.listing.state == jobs.UNKNOWN and "KB-BVUH" in unknown.listing.reason


def test_a_broken_answer_is_unknown_never_empty(api):
    api.mode = "html"
    assert "HTTP 200 with code none" in price.fetch("ecs", "eu-de").listing.reason
    api.mode = "short"
    short = price.fetch("ecs", "eu-de")
    assert short.listing.state == jobs.UNKNOWN and "sent 3 of 4" in short.listing.reason


def test_a_busy_api_is_asked_again(api):
    api.mode = "busy-once"
    assert price.fetch("ecs", "eu-de").listing.state == jobs.LIST
    assert len(api.calls) == 2


def test_no_answer_is_unknown(monkeypatch):
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    f = price.fetch("ecs", "eu-de", base="http://127.0.0.1:%d/en/open-telekom-price-api/" % port, timeout=2)
    assert f.listing.state == jobs.UNKNOWN and "did not answer" in f.listing.reason


def test_a_record_of_another_service_is_left_out(api):
    api.stray = record("TEST_STRAY", service="das", flavor="das.instance", name="another product")
    f = price.fetch("ecs", "eu-de")
    assert f.stray == 1 and "TEST_STRAY" not in {r.id for r in f.records}


def test_the_work_user_always_asks_the_public_api(api, monkeypatch):
    assert price.api_url() == api.url
    monkeypatch.setattr(config, "is_work_user", lambda: True)
    assert price.api_url() == price.API


def test_select_by_flavor_os_text_and_id():
    recs = records()
    assert {r.id for r in price.select(recs, flavor="S3.LARGE.2")} == {"TEST_S3_LARGE_2_LNX", "TEST_S3_LARGE_2_WIN"}
    assert [r.id for r in price.select(recs, flavor="s3.large.2", os_text="win")] == ["TEST_S3_LARGE_2_WIN"]
    assert [r.id for r in price.select(recs, grep="ultra")] == ["TEST_EVS_SSD"]
    assert [r.id for r in price.select(recs, ids={"TEST_BIG"})] == ["TEST_BIG"]


# --------------------------------------------------------------------------- snapshot and diff


def test_a_snapshot_keeps_the_listing_and_refuses_an_empty_one(home, api):
    f = price.fetch(None, "eu-de")
    path = price.write_snapshot(home, f, "eu-de", today=TODAY)
    assert path == home.shared / "prices" / "tcp" / "eu-de" / "2026-09-24.json"
    meta, recs = price.load_snapshot(path)
    assert meta["count"] == 6 and {r.id for r in recs} == {r.id for r in f.records}
    with pytest.raises(price.PriceError, match="--replace"):
        price.write_snapshot(home, f, "eu-de", today=TODAY)
    price.write_snapshot(home, f, "eu-de", today=TODAY, replace=True)
    for bad in (price.fetch("ecs", "eu-ch2"), price.fetch("nosuchservice", "eu-de")):
        with pytest.raises(price.PriceError, match="wrong baseline"):
            price.write_snapshot(home, bad, "eu-ch2", today=TODAY)
    assert not (home.shared / "prices" / "tcp" / "eu-ch2").exists() or \
        not any((home.shared / "prices" / "tcp" / "eu-ch2").iterdir())
    assert price.latest_snapshot(home, "eu-de") == path


def test_diff_compares_values_not_row_counts():
    old = records()
    raws = [dict(r.raw) for r in old]
    for r in raws:
        if r["id"] == "TEST_S3_LARGE_2_LNX":
            r["priceAmount"] = "0.130000 EUR"              # a reprice of more than half, same number of rows
    d = price.diff(old, [price.Record.from_raw(r) for r in raws])
    assert d.before == d.after and d.changed
    assert [(c.id, c.term) for c in d.changes] == [("TEST_S3_LARGE_2_LNX", "PAYG")]
    text = "\n".join(d.lines())
    assert "the number of records is the same, the values are not" in text and "(+52.9 %)" in text
    grown = price.diff(old[:2], old)
    assert grown.added == sorted(r.id for r in old[2:]) and not grown.changes
    assert not price.diff(old, old).changed


# --------------------------------------------------------------------------- the sheet check


def test_a_true_row_passes():
    r = check(row())
    assert r.verdict == "ok" and r.total == Decimal("122.40") and r.problems == []
    assert r.detail == "PAYG 0.085000 x 2 x 720 h = 122.40"


def test_the_unit_price_is_compared_at_the_precision_of_the_sheet_and_the_total_to_the_cent():
    rounded = check(row(unit_price="0.09", total="129.60"))      # 0.09 x 2 x 720, the rounded rate used
    assert rounded.verdict == "differs" and rounded.problems == [
        "total: sheet 129.60, recomputed 122.40 (difference 7.20)"]
    wrong = check(row(unit_price="0.086"))
    assert wrong.verdict == "differs" and "unit price: sheet 0.086, API 0.085000" in wrong.problems[0]
    one_cent = check(row(total="122.41"))
    assert one_cent.verdict == "differs" and "difference 0.01" in one_cent.problems[0]


def test_reserved_terms():
    no_rate = check(row(id="TEST_S3_LARGE_2_LNX", term="R36", unit_price="0.00", hours="", total=""))
    assert no_rate.verdict == "differs" and "no R36 rate" in no_rate.problems[0]
    with_hours = check(row(term="R12", unit_price="45.00", hours="720", total="90.00"))
    assert with_hours.verdict == "differs" and any("hours stay empty" in p for p in with_hours.problems)
    monthly = check(row(term="R12", unit_price="45.00", hours="", total="90.00"))
    assert monthly.verdict == "ok" and monthly.total == Decimal("90.00")


def test_an_hourly_rate_needs_hours_and_730_hours_is_noted():
    no_hours = check(row(hours="", total="0.17"))
    assert no_hours.verdict == "differs" and any("without hours" in p for p in no_hours.problems)
    h730 = check(row(hours="730", total="124.10"))
    assert h730.verdict == "ok" and any("720 hours" in n for n in h730.notes)
    gb = check(row(id="TEST_EVS_SSD", unit_price="0.12", quantity="500", hours="", total="60.00"))
    assert gb.verdict == "ok"


def test_the_os_decides_the_tier():
    r = check(row(os="Windows"))
    assert r.verdict == "differs" and "OS unit: sheet Windows, API Open Linux" in r.problems[0]
    by_flavor = check({"service": "ecs", "flavor": "s3.large.2", "unit_price": "0.131", "quantity": "1",
                       "hours": "720", "total": "94.32", "line": 3})
    assert by_flavor.verdict == "ambiguous" and "Open Linux, Windows" in by_flavor.problems[0]
    windows = check({"service": "ecs", "flavor": "s3.large.2", "os": "windows", "unit_price": "0.131",
                     "quantity": "1", "hours": "720", "total": "94.32", "line": 3})
    assert windows.verdict == "ok"


def test_a_flavor_under_another_service_name_is_found_there(home, api, capsys):
    api.records.append(record("TEST_C4_LARGE_2_LNX", service="ecsnoc", flavor="c4.large.2", payg="0.100000"))
    assert cli.main(["price", "find", "ecs", "--flavor", "c4.large.2"]) == 0
    out = capsys.readouterr().out
    assert "not under sn=ecs, but under sn=ecsnoc" in out and "TEST_C4_LARGE_2_LNX" in out
    recs = [price.Record.from_raw(r) for r in api.records if r["region"] == "eu-de"]
    r = check({"service": "ecs", "flavor": "c4.large.2", "unit_price": "0.10", "quantity": "1", "hours": "720",
               "line": 4}, recs)
    assert r.verdict == "missing" and "listed under sn=ecsnoc" in r.problems[0]
    assert cli.main(["price", "find", "ecs", "--flavor", "z9.none.1"]) == 1


def test_missing_invalid_unknown_and_notes():
    missing = check(row(id="TEST_NOT_THERE"))
    assert missing.verdict == "missing" and "KB-AI2E" in missing.problems[0]
    assert check(row(unit_price="0,085")).verdict == "invalid"
    assert check(row(quantity="0")).verdict == "invalid"
    assert check(row(term="R48")).verdict == "invalid"
    assert check(row(region="Frankfurt")).verdict == "invalid"
    unknown = price.check_row(row(), lambda region: price.Fetch(jobs.Listing.unknown("HTTP 503"), "t", "t"),
                              today=TODAY)
    assert unknown.verdict == "unknown" and "HTTP 503" in unknown.problems[0]
    old = check(row(date="2026-07-01", source=""))
    assert old.verdict == "ok" and any("85 days ago" in n for n in old.notes) and "no source" in old.notes
    tiered = check(row(id="TEST_BW_1", unit_price="0.03", quantity="100", hours="720", total="2160.00"))
    assert tiered.verdict == "ok" and any("tiered rate, group TEST_BW" in n for n in tiered.notes)


def test_the_self_test_catches_a_broken_check(monkeypatch):
    assert price.selftest(records(), today=TODAY) == []
    real = price.check_row

    def blind(r, fetch_region, **kw):
        res = real(r, fetch_region, **kw)
        res.verdict, res.problems = ("ok", []) if res.verdict == "differs" else (res.verdict, res.problems)
        return res

    monkeypatch.setattr(price, "check_row", blind)
    failed = price.selftest(records(), today=TODAY)
    assert any("one cent off" in f for f in failed) and any("without a rate" in f for f in failed)


SHEET = """id,region,term,unit_price,quantity,hours,total,source,date
TEST_S3_LARGE_2_LNX,eu-de,PAYG,0.085,2,720,122.40,price API,2026-09-24
TEST_S3_XLARGE_2_LNX,eu-de,R36,70.00,1,,70.00,price API,2026-09-24
TEST_NL_S3_LARGE_2_LNX,eu-nl,PAYG,0.09,1,720,64.80,price API,2026-09-24
# a comment row
TOTAL,,,,,,257.20,,
"""


def test_a_whole_sheet_one_fetch_per_region(home, api, tmp_path):
    sheet = tmp_path / "offer.csv"
    sheet.write_text(SHEET, encoding="utf-8")
    report = price.check_sheet(sheet, today=TODAY)
    assert [r.verdict for r in report.rows] == ["ok", "ok", "ok"]
    assert report.grand_total.verdict == "ok" and report.grand_total.total == Decimal("257.20")
    assert report.exit_code() == 0
    assert sorted(c["rn"] for c in api.calls) == ["eu-de", "eu-nl"]
    lines = price.format_report(report, sheet.name)
    assert lines[0].startswith("awb price check: offer.csv, 3 position(s); price API, fetched")
    assert lines[-1] == "result: 4 ok, 0 differs, 0 missing, 0 ambiguous, 0 unknown, 0 invalid"

    sheet.write_text(SHEET.replace("257.20", "257.21").replace(",", "\t"), encoding="utf-8")
    report = price.check_sheet(sheet, today=TODAY)
    assert report.grand_total.verdict == "differs" and report.exit_code() == 1

    # a row priced wrong while the TOTAL row still carries the right sum: the sheet's own arithmetic fails
    sheet.write_text(SHEET.replace("0.09,1,720,64.80", "0.08,1,720,57.60"), encoding="utf-8")
    report = price.check_sheet(sheet, today=TODAY)
    assert report.rows[2].verdict == "differs"
    assert report.grand_total.verdict == "differs" and report.grand_total.problems == [
        "grand total: sheet 257.20, but the totals of its own rows add up to 250.00"]


def test_a_sheet_with_semicolons_and_a_missing_column(tmp_path):
    good = tmp_path / "a.csv"
    good.write_text("id;unit_price;quantity\nX;1.00;1\n", encoding="utf-8")
    rows, notes = price.read_sheet(good)
    assert rows == [{"id": "X", "unit_price": "1.00", "quantity": "1", "line": 2}] and notes == []
    bad = tmp_path / "b.csv"
    bad.write_text("id,price,quantity\nX,1.00,1\n", encoding="utf-8")
    with pytest.raises(price.PriceError, match="no column unit_price"):
        price.read_sheet(bad)


def test_a_broken_check_refuses_the_whole_sheet(home, api, tmp_path, monkeypatch):
    sheet = tmp_path / "offer.csv"
    sheet.write_text(SHEET, encoding="utf-8")
    monkeypatch.setattr(price, "selftest", lambda recs, today: ["a total one cent off: ok instead of differs"])
    with pytest.raises(price.PriceError, match="failed its self-test"):
        price.check_sheet(sheet, today=TODAY)


# --------------------------------------------------------------------------- the command line


def test_the_command_line(home, api, tmp_path, capsys):
    assert cli.main(["price", "find", "ecs", "--flavor", "s3.large.2", "--os", "linux"]) == 0
    out = capsys.readouterr().out
    assert "TEST_S3_LARGE_2_LNX" in out and "TEST_S3_LARGE_2_WIN" not in out and "KB-XELM" in out
    assert cli.main(["price", "find", "ecs", "--flavor", "s9.huge.1"]) == 1
    assert "no proof" in capsys.readouterr().out
    assert cli.main(["price", "find", "nosuchservice"]) == 2
    capsys.readouterr()
    assert cli.main(["price", "find", "--id", "TEST_BIG", "--json"]) == 0
    assert '"PAYG": "1234.500000"' in capsys.readouterr().out

    assert cli.main(["price", "snapshot", "--region", "eu-de"]) == 0
    assert "eu-de, 6 records" in capsys.readouterr().out
    assert cli.main(["price", "diff", "--region", "eu-de"]) == 0
    capsys.readouterr()
    for r in api.records:
        if r["id"] == "TEST_EVS_SSD":
            r["priceAmount"] = "0.150000 EUR"
    assert cli.main(["price", "diff", "--region", "eu-de"]) == 1
    assert "changed  TEST_EVS_SSD  PAYG  0.120000 -> 0.150000  (+25.0 %)" in capsys.readouterr().out

    sheet = tmp_path / "offer.csv"
    sheet.write_text(SHEET.replace("TEST_S3_LARGE_2_LNX,eu-de,PAYG,0.085", "TEST_S3_LARGE_2_LNX,eu-de,PAYG,0.095"),
                     encoding="utf-8")
    assert cli.main(["price", "check", str(sheet)]) == 1
    assert "unit price: sheet 0.095, API 0.085000" in capsys.readouterr().out
    assert cli.main(["price", "find", "ecs", "--region", "Frankfurt"]) == 2


# --------------------------------------------------------------------------- T-88


def test_the_core_never_imports_the_platform_package():
    core = [p for p in ROOT.rglob("*.py") if "tcp" not in p.relative_to(ROOT).parts]
    assert len(core) > 20
    for path in core:
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            names = []
            if isinstance(node, ast.Import):
                names = [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom):
                names = [node.module or ""] + ["%s.%s" % (node.module, a.name) for a in node.names]
            assert not any(n == "awb.tcp" or n.startswith("awb.tcp.") for n in names), path.name
