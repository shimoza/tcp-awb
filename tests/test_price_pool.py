"""The pooled pricing of Reserved (B-044, KB-NPRJ): awb/tcp/pricepool.py, `awb price pool`, `mode: pool` of awb
pricebook and the pool notes of `awb price check`.

The rates are the worked example of the task in a fixture and reach the code through a fake fetch: no test asks the
live price API or a stand-in server. g7v.2xlarge.8 Windows eu-de, PAYG 1.16, R24 746, 17 instances x 160 h.
"""
from __future__ import annotations

import ast
import datetime
import json
import sys
from decimal import Decimal
from pathlib import Path

import openpyxl
import pytest

import awb
from awb import calc, cli, deploy, jobs
from awb.tcp import price, pricebook, pricepool
from tests import fixtures as fx
from tests.tcp_fake import record

ROOT = Path(awb.__file__).resolve().parent.parent
FETCHED = "2026-11-07 10:00 UTC"
PAYG = Decimal("1.16")
R24 = Decimal("746")
POOLED = Decimal(17 * 160)                       # 2,720 h a month


def gpu_records() -> list[dict]:
    """The worked example and the Linux item of the same flavor; RU24 is invented, R36 and RU12 are not offered."""
    win = record("TEST_G7V_2XL_WIN", flavor="g7v.2xlarge.8", name="GPU-accelerated g7v.2xlarge.8", os="Windows",
                 payg="1.160000", r12="800.000000", r24="746.000000", r36="0.000000")
    win["RU24"] = "700.000000 EUR"
    lnx = record("TEST_G7V_2XL_LNX", flavor="g7v.2xlarge.8", name="GPU-accelerated g7v.2xlarge.8", os="Open Linux",
                 payg="0.900000", r12="600.000000", r24="550.000000", r36="0.000000")
    plain = record("TEST_S3_LARGE_2_LNX")
    return [win, lnx, plain]


def listing(raws=None) -> price.Fetch:
    recs = [price.Record.from_raw(r) for r in (gpu_records() if raws is None else raws)]
    return price.Fetch(jobs.Listing.of(recs), "fake", FETCHED)


@pytest.fixture
def fake(monkeypatch):
    """`price.fetch` answers from the fixture; any other way to the network fails the test."""
    calls = []

    def fetch(service=None, region="eu-de", **kw):
        calls.append((service, region))
        recs = [r for r in gpu_records() if service in (None, r["productIdParameter"]) and r["region"] == region]
        return listing(recs)

    monkeypatch.setattr(price, "fetch", fetch)
    monkeypatch.setattr(price, "_get_json", lambda *a, **k: pytest.fail("a test reached the network"))
    return calls


def run_pool(*args) -> int:
    return cli.main(["price", "pool", "ecs", "--flavor", "g7v.2xlarge.8"] + [str(a) for a in args])


# --------------------------------------------------------------------------- the arithmetic


def test_the_worked_example_to_the_cent_at_730_hours():
    p = pricepool.pool("R24", PAYG, R24, POOLED)
    assert p.average and len(p.months) == 24 and all(m.hours == 730 for m in p.months)
    assert p.best.units == 3
    month = p.best.months[0]
    assert (month.covered, month.rest) == (Decimal(2190), Decimal(530))
    assert (month.reserved, month.elastic, month.total) == (Decimal("2238.00"), Decimal("614.80"),
                                                           Decimal("2852.80"))
    assert month.elastic_only == Decimal("3155.20") and month.saving == Decimal("302.40")
    assert p.threshold.quantize(Decimal("0.1")) == Decimal("643.1")
    assert [c.units for c in p.candidates] == [0, 1, 2, 3, 4]             # up to ceil(2720 / 730)
    assert p.candidate(4).months[0].total == Decimal("2984.00")
    assert p.best.total == Decimal("2852.80") * 24 and p.best.saving == Decimal("302.40") * 24


def test_n_is_fixed_over_the_term_and_the_term_decides_not_one_month():
    p = pricepool.pool("R24", PAYG, R24, POOLED, datetime.date(2027, 2, 1))
    assert [m.label for m in p.months][:2] == ["2027-02", "2027-03"] and len(p.months) == 24
    assert p.best.units == 3
    feb, mar = p.best.months[0], p.best.months[1]
    assert feb.month.hours == 672 and (feb.total, feb.saving) == (Decimal("3054.64"), Decimal("100.56"))
    assert mar.month.hours == 744 and (mar.total, mar.saving) == (Decimal("2804.08"), Decimal("351.12"))
    # February alone would take a fourth unit, the term does not
    assert p.best_alone(0) == 4 and p.candidate(4).months[0].total == Decimal("3021.12")
    assert p.best_alone(1) == 3
    # hours in one month only: that month alone takes 3 units, the term as a whole none
    hours = [POOLED] + [Decimal(0)] * 11
    q = pricepool.pool("R24", PAYG, R24, hours + hours)
    assert q.best_alone(0) == 3 and q.best.units == 0


def test_a_start_after_the_first_gives_part_months_pro_rata():
    p = pricepool.pool("R12", PAYG, R24, POOLED, datetime.date(2027, 3, 16))
    assert len(p.months) == 13 and p.months[0].label == "2027-03" and p.months[-1].label == "2028-03"
    first, last = p.months[0], p.months[-1]
    assert first.hours == 16 * 24 and last.hours == 15 * 24          # 16 + 15 days: one month of 31 days
    assert first.share + last.share == 1
    assert p.best.months[0].reserved == calc.evaluate("round(3 * 746 * 16 / 31, 2)")
    with pytest.raises(pricepool.PoolError, match="one per month of the term \\(13\\)"):
        pricepool.pool("R12", PAYG, R24, [POOLED] * 12, datetime.date(2027, 3, 16))


def test_n_is_zero_below_the_threshold_and_a_rest_at_the_threshold_takes_no_unit():
    assert pricepool.pool("R24", PAYG, R24, Decimal(600)).best.units == 0
    # PAYG 1.25 and R 500: the threshold is 400 h exactly
    assert pricepool.pool("R12", Decimal("1.25"), Decimal(500), Decimal(400)).best.units == 0
    assert pricepool.pool("R12", Decimal("1.25"), Decimal(500), Decimal(401)).best.units == 1
    assert pricepool.pool("R12", Decimal("1.25"), Decimal(500), Decimal(1130)).best.units == 1
    assert pricepool.pool("R12", Decimal("1.25"), Decimal(500), Decimal(1131)).best.units == 2


def test_a_term_with_rate_zero_is_not_offered():
    with pytest.raises(pricepool.NotOffered, match="not offered"):
        pricepool.pool("R36", PAYG, Decimal("0.000000"), POOLED)


def test_an_upfront_term_uses_the_monthly_equivalent_and_is_invoiced_first():
    p = pricepool.pool("RU24", PAYG, Decimal(700), POOLED)
    assert p.upfront and p.best.units == 3
    assert p.best.months[0].reserved == Decimal("2100.00")
    assert p.upfront_total() == Decimal("50400.00")                   # 3 x 700 x 24 months
    assert pricepool.pool("R24", PAYG, R24, POOLED).upfront_total() is None


# --------------------------------------------------------------------------- awb price pool


def test_the_command_prints_the_figures_the_open_points_and_the_gpu_note(home, fake, tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    assert run_pool("--os", "Windows", "--hours", "2720", "--term", "R24") == 0
    out = capsys.readouterr().out
    assert "best n = 3 unit(s)" in out and "threshold R24 / PAYG = 643.1 h" in out
    assert "730 h for each month" in out
    assert "2238.00" in out and "614.80" in out and "2852.80" in out and "3155.20" in out and "302.40" in out
    assert "68467.20 against 75724.80 Open Elastic only, saving 7257.60" in out
    for line in out.splitlines():
        if "2852.80" in line and "each month" not in line:
            pytest.fail("a figure without its fetch time: %s" % line)
    assert out.count("price API, fetched %s" % FETCHED) >= 3
    assert "minimum order volume" in out and "agreed with telekom sales" in out.lower()
    assert "five working days" in out and "flavor plus the OS" in out and "unused hours expire" in out
    assert "KB-NAAF" in out and "disks only" in out
    assert "recorded" not in out                                      # no project around the current folder

    assert cli.main(["price", "pool", "ecs", "--flavor", "s3.large.2", "--os", "Open Linux", "--hours", "800",
                     "--term", "R12"]) == 0
    out = capsys.readouterr().out
    assert "minimum order volume" in out and "KB-NAAF" not in out


def test_the_command_takes_the_calendar_months_from_a_start(home, fake, capsys):
    assert run_pool("--os", "Windows", "--hours", "2720", "--term", "R24", "--start", "2027-02-01") == 0
    out = capsys.readouterr().out
    assert "the calendar months from 2027-02-01" in out
    feb = next(line for line in out.splitlines() if line.strip().startswith("2027-02"))
    assert "672" in feb and "3054.64" in feb and "100.56" in feb
    assert "2027-02 alone would take n = 4" in out


def test_a_term_with_rate_zero_is_refused_when_asked_and_listed_otherwise(home, fake, capsys):
    assert run_pool("--os", "Windows", "--hours", "2720", "--term", "R36") == 1
    assert "refused: R36 is not offered" in capsys.readouterr().out
    assert run_pool("--os", "Windows", "--hours", "2720") == 0
    out = capsys.readouterr().out
    assert "R36 is not offered" in out and "RU12 is not offered" in out and "refused" not in out
    assert "R24: 746" in out and "RU24: 700" in out


def test_an_upfront_term_says_the_whole_total_is_invoiced_at_the_start(home, fake, capsys):
    assert run_pool("--os", "Windows", "--hours", "2720", "--term", "RU24") == 0
    out = capsys.readouterr().out
    assert "upfront total of the term divided by its months" in out
    assert "50400.00 is invoiced at the start of the term" in out


def test_windows_and_linux_of_one_flavor_are_two_items_never_pooled(home, fake, capsys):
    f = listing()
    win = price.pool_record(f, "g7v.2xlarge.8", "Windows")
    lnx = price.pool_record(f, "g7v.2xlarge.8", "Linux")
    assert (win.id, lnx.id) == ("TEST_G7V_2XL_WIN", "TEST_G7V_2XL_LNX")
    with pytest.raises(price.PriceError, match="KB-SFB4"):
        price.pool_record(f, "g7v.2xlarge.8", "n")                     # fits Windows and Open Linux
    assert run_pool("--os", "n", "--hours", "2720") == 2
    assert "KB-SFB4" in capsys.readouterr().err
    assert run_pool("--os", "Linux", "--hours", "2720", "--term", "R24") == 0
    out = capsys.readouterr().out
    assert "record TEST_G7V_2XL_LNX" in out and "TEST_G7V_2XL_WIN" not in out and "R24: 550" in out


def test_the_totals_are_recorded_in_a_project(home, fake, tmp_path, capsys):
    project = tmp_path / "p"
    project.mkdir()
    (project / "SCOPE.md").write_text("# scope\n", encoding="utf-8")
    assert run_pool("--os", "Windows", "--hours", "2720", "--term", "R24", "--project", project) == 0
    assert "recorded K-1, K-2, K-3" in capsys.readouterr().out
    recs = calc.load(project)
    assert [r.result for r in recs] == ["2852.80", "68467.20", "7257.60"]
    assert recs[0].label == "pool r24 monthly total"
    assert calc.evaluate(recs[0].expression) == Decimal("2852.80")


# --------------------------------------------------------------------------- awb pricebook mode: pool


POOL_SPEC = """title: Render farm for %s
region: eu-de

[render farm]
service: ecs
flavor: g7v.2xlarge.8
os: Windows
quantity: 17
mode: pool
pooled_hours: 2720
term: r24
assumption: 17 machines of 160 h a month.
""" % fx.CUSTOMER_CODE


def test_pricebook_writes_a_pool_as_two_lines_with_the_same_totals(tmp_path, capsys):
    (tmp_path / "SCOPE.md").write_text("# scope\n", encoding="utf-8")
    spec = tmp_path / "farm.txt"
    spec.write_text(POOL_SPEC, encoding="utf-8")
    res = pricebook.build(spec, fetcher=lambda service, region: listing(), today="2026-11-07")
    assert res.lines == 2 and res.monthly == Decimal("2852.80") and res.yearly == Decimal("34233.60")
    ws = openpyxl.load_workbook(res.xlsx, data_only=True).worksheets[0]
    cell = lambda ref: ws[ref].value  # noqa: E731
    assert cell("B5") == "render farm (R24)" and cell("G5") == "Reserved 24 months"
    assert cell("I5") == 746 and cell("J5") == 3 and cell("K5") is None and cell("L5") == 2238
    assert cell("B6") == "render farm (Open Elastic)" and cell("G6") == "Pay as you go"
    assert cell("J6") == 1 and cell("K6") == 530 and float(cell("L6")) == pytest.approx(614.80)
    for ref in ("N5", "N6"):
        text = cell(ref)
        assert text.startswith("17 machines of 160 h a month.")
        assert "2720 h" in text and "730 h months" in text and "minimum order volume is an open point" in text
    assert calc.find(tmp_path, "K-1").result == "2852.80"
    assert any("KB-NAAF" in n for n in res.notes) and any("minimum order volume" in n for n in res.notes)
    assert pricepool.pool("R24", PAYG, R24, POOLED).best.months[0].total == res.monthly


def test_pricebook_pool_with_no_unit_or_no_rest_hours(tmp_path):
    spec = tmp_path / "farm.txt"
    for pooled, units, rest, monthly in (("600", 0, 600, "696.00"), ("730", 1, 0, "746.00")):
        spec.write_text(POOL_SPEC.replace("pooled_hours: 2720", "pooled_hours: " + pooled), encoding="utf-8")
        res = pricebook.build(spec, fetcher=lambda service, region: listing(), today="2026-11-07", record=False)
        assert res.lines == 2 and res.monthly == Decimal(monthly), pooled
        ws = openpyxl.load_workbook(res.xlsx, data_only=True).worksheets[0]
        assert ws["J5"].value == units and ws["K6"].value == rest, pooled


def test_pricebook_pool_with_a_start_and_the_refusals(tmp_path):
    spec = tmp_path / "farm.txt"
    spec.write_text(POOL_SPEC.replace("term: r24\n", "term: r24\nstart: 2027-02-01\n"), encoding="utf-8")
    res = pricebook.build(spec, fetcher=lambda service, region: listing(), today="2026-11-07", record=False)
    ws = openpyxl.load_workbook(res.xlsx, data_only=True).worksheets[0]
    p = pricepool.pool("R24", PAYG, R24, POOLED, datetime.date(2027, 2, 1))
    rest = calc.evaluate("round(r / 24, 2)", {"r": sum((m.rest for m in p.best.months), Decimal(0))})
    assert ws["J5"].value == 3 and Decimal(str(ws["K6"].value)) == rest
    assert "calendar months from 2027-02-01" in ws["N6"].value

    spec.write_text(POOL_SPEC.replace("term: r24", "term: r36"), encoding="utf-8")
    with pytest.raises(pricebook.LineRefused) as err:
        pricebook.build(spec, fetcher=lambda service, region: listing(), today="2026-11-07", record=False)
    assert "not offered" in err.value.problems["render farm"]
    for body, want in ((POOL_SPEC.replace("term: r24\n", ""), "needs term"),
                       (POOL_SPEC.replace("pooled_hours: 2720\n", ""), "needs pooled_hours"),
                       (POOL_SPEC.replace("mode: pool\n", "mode: payg\n"), "belongs to mode pool"),
                       (POOL_SPEC.replace("mode: pool\n", "mode: pool\nhours: 720\n"), "hours stay empty"),
                       (POOL_SPEC + "\n[render farm (R24)]\nservice: ecs\nflavor: s3.large.2\nquantity: 1\n",
                        "used twice")):
        with pytest.raises(pricebook.SpecError) as err:
            pricebook.parse_spec(body.encode())
        assert any(want in p for p in err.value.problems), want


# --------------------------------------------------------------------------- awb price check


CHECK_SHEET = """id,region,term,unit_price,quantity,hours,total,source,date
TEST_G7V_2XL_WIN,eu-de,PAYG,1.16,17,160,3155.20,price API,2026-11-07
TEST_G7V_2XL_WIN,eu-de,R24,746.00,%s,,%s,price API,2026-11-07
"""


def test_price_check_notes_the_pool_rule_and_warns_of_units_per_instance(tmp_path):
    sheet = tmp_path / "offer.csv"
    sheet.write_text(CHECK_SHEET % ("17", "12682.00"), encoding="utf-8")
    report = price.check_sheet(sheet, fetcher=lambda service, region: listing(), today=datetime.date(2026, 11, 7))
    assert [r.verdict for r in report.rows] == ["ok", "ok"] and report.exit_code() == 0
    assert price.POOL_SHEET_NOTE in report.notes
    warning = [n for n in report.notes if "warning" in n]
    assert len(warning) == 1 and warning[0].startswith("line 3: warning: the R24 units (17) equal the instance count")
    assert "per instance" in warning[0] and "line 2" in warning[0]
    text = "\n".join(price.format_report(report, sheet.name))
    assert "awb price pool" in text and "KB-NPRJ" in text

    sheet.write_text(CHECK_SHEET % ("3", "2238.00"), encoding="utf-8")
    report = price.check_sheet(sheet, fetcher=lambda service, region: listing(), today=datetime.date(2026, 11, 7))
    assert report.exit_code() == 0 and price.POOL_SHEET_NOTE in report.notes
    assert not any("warning" in n for n in report.notes)

    sheet.write_text(CHECK_SHEET.splitlines()[0] + "\n" + CHECK_SHEET.splitlines()[1] + "\n", encoding="utf-8")
    report = price.check_sheet(sheet, fetcher=lambda service, region: listing(), today=datetime.date(2026, 11, 7))
    assert price.POOL_SHEET_NOTE not in report.notes                   # no reserved line, no note


# --------------------------------------------------------------------------- the rules of the repository


def test_the_module_is_command_only_loaded_by_name_and_standard_library_only():
    assert "awb/tcp/pricepool.py" in deploy.COMMAND_ONLY
    assert deploy.DYNAMIC[("awb/tcp/price.py", "_pool_module")] == ()
    assert price._pool_module() is pricepool
    text = (ROOT / "awb" / "tcp" / "price.py").read_text(encoding="utf-8")
    assert "import pricepool" not in text and "awb.tcp.pricepool" in text
    stdlib = set(sys.stdlib_module_names)
    tree = ast.parse((ROOT / "awb" / "tcp" / "pricepool.py").read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        names = []
        if isinstance(node, ast.Import):
            names = [a.name.split(".")[0] for a in node.names]
        elif isinstance(node, ast.ImportFrom) and node.level == 0:
            names = [(node.module or "").split(".")[0]]
        for name in names:
            assert name in stdlib or name == "awb", name
        assert not (isinstance(node, ast.Call) and getattr(node.func, "id", "") == "open"), "no I/O"


def test_the_texts_hold_no_em_dash_and_no_comma_before_and_or():
    import re
    texts = list(price.POOL_NOTES) + [price.GPU_NOTE, price.POOL_SHEET_NOTE]
    texts += [t for lang in pricebook.POOL_TEXT.values() for t in lang.values()]
    for text in texts:
        assert chr(0x2014) not in text and chr(0x2013) not in text, text
        assert not re.search(r",\s+(?:and|or|und|oder)\b", text), text


def test_the_command_is_documented_and_in_the_test_list():
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    assert "awb price pool" in readme and "mode: pool" in readme
    assert "### `awb price pool" in (ROOT / "COMMANDS.md").read_text(encoding="utf-8")
    changelog = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
    assert "awb price pool" in changelog.split("\n## Week 44")[0]
    skill = (ROOT / "plugin" / "skills" / "tcp-price" / "SKILL.md").read_text(encoding="utf-8")
    assert "Reserved for many machines" in skill and "awb price pool" in skill
    rows = json.loads((ROOT / "build" / "tests.json").read_text(encoding="utf-8"))
    mine = [t for t in rows["tests"] if t["id"].startswith("b044-")]
    assert len(mine) >= 10 and all("tests/test_price_pool.py" in t["run"] for t in mine)
    assert rows["total_tests"] == len(rows["tests"])
