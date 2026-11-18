"""awb/tcp/pricebook.py and awb/xlsxlite.py: the price workbook, against a stand-in price API with invented rates.

openpyxl reads what the standard library writer made. No test reaches the real price API: the fake answers on
127.0.0.1 and a guard refuses any other address.
"""
from __future__ import annotations

import ast
import datetime
import json
import re
import socket
import zipfile
from decimal import Decimal
from pathlib import Path

import openpyxl
import pytest

import awb
from awb import cli, jobs, offered, xlsxlite
from awb.tcp import price, pricebook, throttle
from tests import fixtures as fx
from tests.tcp_fake import FakePriceAPI, default_records, record

ROOT = Path(awb.__file__).resolve().parent.parent


def extra_records() -> list[dict]:
    return default_records() + [
        record("TEST_TRAP_GB", service="evs", flavor="trap.gb", os="Standard", payg="1.005000", unit="GB/month",
               r12="0.000000", r24="0.000000"),
        record("TEST_DIME_GB", service="evs", flavor="dime.gb", os="Standard", payg="0.100000", unit="GB/month",
               r12="0.000000", r24="0.000000"),
        record("TEST_TWO_DIMES_GB", service="evs", flavor="twodimes.gb", os="Standard", payg="0.200000",
               unit="GB/month", r12="0.000000", r24="0.000000"),
        record("TEST_FLEX_LNX", service="ecsflex", flavor="s3.flex.1", payg="0.040000", r12="20.000000"),
        record("TEST_TIERS_LNX", flavor="tiers.1", os="Open Linux", payg="0.050000"),
        record("TEST_TIERS_SUSE", flavor="tiers.1", os="SUSE Linux", payg="0.060000"),
    ]


@pytest.fixture
def api(monkeypatch):
    with FakePriceAPI(extra_records(), cached_at="2026-10-09 14:12:40") as f:
        monkeypatch.setenv("AWB_PRICE_API", f.url)
        yield f


@pytest.fixture(autouse=True)
def quiet(monkeypatch):
    monkeypatch.setattr(jobs.time, "sleep", lambda s: None)
    monkeypatch.setattr(throttle, "wait_turn", lambda host, *a, **k: 0.0)
    real = socket.socket.connect

    def only_here(self, address):
        host = address[0] if isinstance(address, tuple) else address
        assert host in ("127.0.0.1", "::1", "localhost"), "a test reached an address outside this host"
        return real(self, address)

    monkeypatch.setattr(socket.socket, "connect", only_here)


@pytest.fixture
def folder(tmp_path):
    (tmp_path / "SCOPE.md").write_text("# scope\n", encoding="utf-8")
    return tmp_path


HEAD = "title: Sizing for %s\nregion: eu-de\n" % fx.CUSTOMER_CODE

WEB = """
[web servers]
service: ecs
flavor: s3.large.2
os: Linux
quantity: %s
mode: payg
assumption: Two zones and all day.
assumption.de: Rund um die Uhr in zwei Zonen.
"""

DISKS = """
[data disks]
service: evs
flavor: evs.ssd
quantity: 2000
assumption: 500 GB per server.
"""


def spec_file(folder: Path, body: str, name: str = "est.txt") -> Path:
    path = folder / name
    path.write_text(body, encoding="utf-8")
    return path


def run(argv) -> int:
    return cli.main(["pricebook"] + [str(a) for a in argv])


def sheet_cells(path: Path, name: str | None = None, values: bool = False) -> dict:
    wb = openpyxl.load_workbook(path, data_only=values)
    ws = wb[name] if name else wb.worksheets[0]
    return {c.coordinate: c.value for row in ws.iter_rows() for c in row if c.value is not None}


def find_row(cells: dict, text: str) -> int:
    return next(int(k[1:]) for k, v in cells.items() if v == text and k.startswith("B"))


# --------------------------------------------------------------------------- the specification


def test_text_and_json_forms_read_the_same():
    text = HEAD + WEB % "4" + "\n# a comment\n" + DISKS
    spec = pricebook.parse_spec(text.encode(), "est.txt")
    data = {"title": "Sizing for %s" % fx.CUSTOMER_CODE, "region": "eu-de", "lines": [
        {"label": "web servers", "service": "ecs", "flavor": "s3.large.2", "os": "Linux", "quantity": 4,
         "mode": "payg", "assumption": "Two zones and all day.", "assumption_de": "Rund um die Uhr in zwei Zonen."},
        {"label": "data disks", "service": "evs", "flavor": "evs.ssd", "quantity": 2000,
         "assumption": "500 GB per server."}]}
    other = pricebook.parse_spec(json.dumps(data).encode(), "est.json")
    assert spec.lines == other.lines
    assert [l.label for l in spec.lines] == ["web servers", "data disks"]
    assert spec.lines[0].quantity == Decimal("4") and spec.lines[0].term == "PAYG"
    assert spec.lines[0].assumption_for("de") == "Rund um die Uhr in zwei Zonen."
    assert spec.lines[1].assumption_for("de") == "500 GB per server."


def test_a_value_continues_on_an_indented_line():
    spec = pricebook.parse_spec((HEAD + "[a]\nservice: ecs\nflavor: s3.large.2\nquantity: 1\n"
                                 "assumption: first part\n    second part\n").encode())
    assert spec.lines[0].assumption == "first part second part"


@pytest.mark.parametrize("text, term", [
    ("payg", "PAYG"), ("Pay as you go", "PAYG"), ("pay-as-you-go", "PAYG"), ("r12", "R12"), ("reserved 24", "R24"),
    ("Reserved 36 months", "R36"), ("ru12", "RU12"), ("up front 24", "RU24"), ("upfront-36", "RU36"),
    ("reserved 12 up front", "RU12"), ("reserved up front 36 months", "RU36"),
])
def test_modes_are_read(text, term):
    assert pricebook.mode_term(text) == term


@pytest.mark.parametrize("text", ["", "monthly", "r13", "reserved", "spot", "r12 r24"])
def test_unknown_modes_are_not_guessed(text):
    assert pricebook.mode_term(text) is None


def test_a_bad_specification_lists_every_problem():
    body = HEAD + """
[a]
service: ecs
flavor: s3.large.2
quantity: 1,5
mode: spot
colour: red

[a]
service: ecs
flavor: s3.large.2
quantity: 1

[b]
service: Elastic Cloud Server
flavor: s3.large.2
quantity: 0

[c]
service: ecs
quantity: 1
region: us-east

[d]
service: ecs
id: TEST_S3_LARGE_2_LNX
flavor: s3.large.2
quantity: 1
hours: 800
"""
    with pytest.raises(pricebook.SpecError) as err:
        pricebook.parse_spec(body.encode())
    text = "\n".join(err.value.problems)
    for want in ("colour", "[a] quantity", "[a] mode", "used twice", "[b] give api", "[b] quantity",
                 "[c] a line needs a flavor or an id", "[c] the region", "[d] id replaces", "[d] hours"):
        assert want in text, want


def test_the_text_form_refuses_what_is_no_setting_and_no_head():
    with pytest.raises(pricebook.SpecError) as err:
        pricebook.parse_spec((HEAD + "this is a sentence\n[a]\nservice: ecs\nflavor: x\nquantity: 1\n").encode())
    assert "line 3" in err.value.problems[0]


def test_a_specification_needs_a_line_and_stays_small():
    with pytest.raises(pricebook.SpecError):
        pricebook.parse_spec(HEAD.encode())
    many = HEAD + "".join("[l%d]\nservice: ecs\nflavor: s3.large.2\nquantity: 1\n" % i
                          for i in range(pricebook.MAX_LINES + 1))
    with pytest.raises(pricebook.SpecError) as err:
        pricebook.parse_spec(many.encode())
    assert "more than" in err.value.problems[0]
    with pytest.raises(pricebook.SpecError):
        pricebook.parse_spec(b"\xff\xfe\x00")


# --------------------------------------------------------------------------- the workbook


def test_build_writes_lines_formulas_and_sources(folder, api, capsys):
    spec = spec_file(folder, HEAD + WEB % "4" + DISKS)
    assert run(["build", spec]) == 0
    out = capsys.readouterr().out
    assert "2 line(s)" in out and "monthly total 484.80 EUR" in out
    xlsx = folder / "est.xlsx"
    assert xlsx.is_file() and (folder / "est.md").is_file()

    cells = sheet_cells(xlsx)
    wb = openpyxl.load_workbook(xlsx)
    assert wb.sheetnames == ["Lines", "Sources"]
    heads = [cells["%s4" % c] for c in "ABCDEFGHIJKLMNO"]
    assert heads == ["No.", "Item", "Service", "Flavor or product", "Operating system", "Region", "Pricing mode",
                     "Unit", "Unit price (EUR)", "Quantity", "Hours per month", "Monthly (EUR)", "Yearly (EUR)",
                     "Assumption", "Price record"]
    # the first line: every price is the record's, none is typed
    assert (cells["B5"], cells["C5"], cells["D5"], cells["E5"], cells["F5"], cells["G5"], cells["H5"]) == (
        "web servers", "ecs", "s3.large.2", "Open Linux", "eu-de", "Pay as you go", "h")
    assert cells["I5"] == 0.085 and cells["J5"] == 4 and cells["O5"] == "TEST_S3_LARGE_2_LNX"
    assert cells["N5"] == "Two zones and all day."
    assert cells["I6"] == 0.12 and cells["J6"] == 2000 and cells["N6"] == "500 GB per server."
    assert "K6" not in cells                                    # a monthly unit has no hours
    # the figures are formulas, the hours of line 1 come from the cell of the totals block
    assert cells["L5"] == '=ROUND(I5*J5*IF(K5="",1,K5),2)' and cells["M5"] == "=L5*$C$11"
    assert cells["K5"] == "=$C$12"
    assert cells["C9"] == "=SUM(L5:L6)" and cells["C10"] == "=SUM(M5:M6)"
    assert cells["B9"] == "Monthly total (EUR)" and cells["B10"] == "Yearly total (EUR)"
    assert (cells["B11"], cells["C11"], cells["C12"]) == ("Months per year", 12, 720)
    # the stored results are the exact ones
    values = sheet_cells(xlsx, values=True)
    assert (values["K5"], values["L5"], values["M5"]) == (720, 244.8, 2937.6)
    assert (values["L6"], values["M6"], values["C9"], values["C10"]) == (240, 2880, 484.8, 5817.6)

    src = sheet_cells(xlsx, "Sources")
    rows = {src["A%d" % r]: src.get("B%d" % r) for r in range(1, 40) if "A%d" % r in src}
    snapshot = next(v for k, v in rows.items() if k.startswith("Price API snapshot"))
    assert re.fullmatch(r"\d{4}-\d\d-\d\d \d\d:\d\d UTC", snapshot)
    assert rows["Region"] == "eu-de"
    catalog = offered.load()
    assert rows["Service description revision"].startswith(offered.revision_label(catalog))
    assert rows["Price API"] == api.url
    assert "Elastic Cloud Server" in " ".join(str(v) for v in src.values())
    assert any(k.startswith("Served from the cache") and v == "2026-10-09 14:12:40" for k, v in rows.items())


def test_the_price_comes_from_the_api_and_follows_it(folder, api):
    spec = spec_file(folder, HEAD + WEB % "4")
    assert run(["build", spec]) == 0
    assert sheet_cells(folder / "est.xlsx")["I5"] == 0.085
    for r in api.records:
        if r["id"] == "TEST_S3_LARGE_2_LNX":
            r["priceAmount"] = "0.100000 EUR"
    assert run(["build", spec]) == 0
    cells = sheet_cells(folder / "est.xlsx", values=True)
    assert cells["I5"] == 0.1 and cells["L5"] == 288
    assert len(api.calls) == 2                                              # asked at each build, not remembered


def test_a_service_the_description_does_not_offer_is_refused_before_the_api_is_asked(folder, api, capsys):
    body = HEAD + WEB % "1" + "\n[bare metal]\nservice: bms\nflavor: x\nquantity: 1\n"
    spec = spec_file(folder, body)
    assert run(["build", spec]) == 1
    err = capsys.readouterr().err
    assert "[bare metal]" in err and "not offered" in err and "Do not propose it" in err
    assert "web servers" not in err
    assert api.calls == []
    assert not (folder / "est.xlsx").exists() and not (folder / "est.md").exists()


def test_a_withdrawn_service_is_refused(folder, api, capsys):
    spec = spec_file(folder, HEAD + "\n[backup]\nservice: csbs\nflavor: x\nquantity: 1\n")
    assert run(["build", spec]) == 1
    assert "no longer" in capsys.readouterr().err


def test_the_api_name_cannot_hide_a_service_that_is_not_offered(folder, api, capsys):
    spec = spec_file(folder, HEAD + "\n[hidden]\nservice: ecs\napi: bms\nflavor: x\nquantity: 1\n")
    assert run(["build", spec]) == 1
    assert "does not offer" in capsys.readouterr().err
    assert api.calls == []


def test_a_name_the_description_does_not_list_gets_a_hint(folder, api, capsys):
    spec = spec_file(folder, HEAD + "\n[flex]\nservice: ecsflex\nflavor: s3.flex.1\nquantity: 1\n")
    assert run(["build", spec]) == 1
    assert "in `service`" in capsys.readouterr().err


def test_a_family_of_the_api_is_priced_under_the_service(folder, api):
    spec = spec_file(folder, HEAD + "\n[flex]\nservice: ecs\napi: ecsflex\nflavor: s3.flex.1\nquantity: 2\n")
    assert run(["build", spec]) == 0
    cells = sheet_cells(folder / "est.xlsx", values=True)
    assert cells["C5"] == "ecs" and cells["O5"] == "TEST_FLEX_LNX" and cells["L5"] == 57.6


def test_lines_the_api_cannot_price_are_refused_with_the_reason(folder, api, capsys):
    body = HEAD + """
[ambiguous]
service: ecs
flavor: s3.large.2
quantity: 1

[other family]
service: ecs
flavor: s3.flex.1
quantity: 1

[missing]
service: ecs
flavor: s9.nothing.1
quantity: 1

[no reserved rate]
service: ecs
flavor: s3.large.2
os: Linux
quantity: 1
mode: r36

[hours on reserved]
service: ecs
flavor: s3.large.2
os: Linux
quantity: 1
mode: r12
hours: 100

[hours on a monthly unit]
service: evs
flavor: evs.ssd
quantity: 1
hours: 100

[wrong id family]
service: ecs
id: TEST_EVS_SSD
quantity: 1

[no such os]
service: ecs
flavor: s3.large.2
os: Plan
quantity: 1
"""
    spec = spec_file(folder, body)
    assert run(["build", spec]) == 1
    err = capsys.readouterr().err
    for want in ("[ambiguous] 2 records match", "[other family] no record under sn=ecs", "sn=ecsflex",
                 "[missing] no record for service ecs and flavor s9.nothing.1", "[no reserved rate] the API carries "
                 "no R36 rate", "[hours on reserved] reserved rates are flat monthly values",
                 "[hours on a monthly unit] hours apply to an hourly rate", "[wrong id family] the record "
                 "TEST_EVS_SSD belongs to the price API service evs", "[no such os] no record of the flavor"):
        assert want in err, want
    assert "8 line(s) refused" in err
    assert not (folder / "est.xlsx").exists()


def test_a_part_of_the_os_unit_that_fits_several_records_is_not_guessed(folder, api, capsys):
    head = "\n[tiers]\nservice: ecs\nflavor: tiers.1\nquantity: 1\n"
    assert run(["build", spec_file(folder, HEAD + head + "os: Linux\n")]) == 1
    assert "2 records match (OS units: Open Linux, SUSE Linux" in capsys.readouterr().err
    assert run(["build", spec_file(folder, HEAD + head + "os: open linux\n")]) == 0
    assert sheet_cells(folder / "est.xlsx")["O5"] == "TEST_TIERS_LNX"
    assert run(["build", spec_file(folder, HEAD + head + "os: SUSE\n")]) == 0
    assert sheet_cells(folder / "est.xlsx")["O5"] == "TEST_TIERS_SUSE"


def test_a_reserved_line_is_a_flat_monthly_value(folder, api):
    spec = spec_file(folder, HEAD + "\n[node]\nservice: ecs\nflavor: s3.large.2\nos: Linux\nquantity: 3\nmode: r12\n"
                     "assumption: Booked for a year.\n")
    assert run(["build", spec]) == 0
    values = sheet_cells(folder / "est.xlsx", values=True)
    assert values["G5"] == "Reserved 12 months" and values["I5"] == 45 and "K5" not in values
    assert values["L5"] == 135 and values["M5"] == 1620
    md = (folder / "est.md").read_text(encoding="utf-8")
    assert "Reserved rates are monthly values" in md


def test_a_record_is_chosen_by_id_and_a_tiered_rate_is_noted(folder, api):
    spec = spec_file(folder, HEAD + "\n[uplink]\nservice: vpc\nid: TEST_BW_1\nquantity: 10\nhours: 720\n"
                     "\n[disk]\nservice: evs\nid: TEST_EVS_SSD\nquantity: 10\n")
    assert run(["build", spec]) == 0
    values = sheet_cells(folder / "est.xlsx", values=True)
    assert values["L5"] == 216 and values["K5"] == 720 and values["L6"] == 1.2
    src = sheet_cells(folder / "est.xlsx", "Sources")
    assert any("tiered rate" in str(v) for v in src.values())


def test_the_price_api_not_answering_writes_nothing(folder, api, capsys):
    api.mode = "html"
    spec = spec_file(folder, HEAD + WEB % "1")
    assert run(["build", spec]) == 2
    assert "could not be read" in capsys.readouterr().err
    assert not (folder / "est.xlsx").exists()


def test_an_empty_region_is_refused_for_every_line(folder, api, capsys):
    spec = spec_file(folder, "title: x\nregion: eu-ch2\n" + WEB % "1")
    assert run(["build", spec]) == 1
    assert "holds no records for eu-ch2" in capsys.readouterr().err


def test_a_line_in_another_region_uses_that_region(folder, api):
    body = HEAD + WEB % "1" + "\n[nl servers]\nservice: ecs\nflavor: s3.large.2\nregion: eu-nl\nquantity: 1\n"
    spec = spec_file(folder, body)
    assert run(["build", spec]) == 0
    values = sheet_cells(folder / "est.xlsx", values=True)
    assert values["F6"] == "eu-nl" and values["O6"] == "TEST_NL_S3_LARGE_2_LNX" and values["L6"] == 64.8
    src = sheet_cells(folder / "est.xlsx", "Sources")
    assert "eu-de, eu-nl" in src.values()
    assert sum(1 for v in src.values() if str(v).startswith("20") and v.endswith(" UTC")) == 2


# --------------------------------------------------------------------------- German


def test_german_headers_and_fixed_texts(folder, api):
    spec = spec_file(folder, HEAD + WEB % "4" + DISKS)
    assert run(["build", spec, "--lang", "de"]) == 0
    xlsx = folder / "est.xlsx"
    wb = openpyxl.load_workbook(xlsx)
    assert wb.sheetnames == ["Positionen", "Quellen"]
    cells = sheet_cells(xlsx)
    heads = [cells["%s4" % c] for c in "ABCDEFGHIJKLMNO"]
    assert heads == ["Pos.", "Position", "Dienst", "Flavor oder Produkt", "Betriebssystem", "Region", "Preismodell",
                     "Einheit", "Einzelpreis (EUR)", "Menge", "Stunden pro Monat", "Monatlich (EUR)",
                     "Jährlich (EUR)", "Annahme", "Preis-ID"]
    assert cells["N5"] == "Rund um die Uhr in zwei Zonen."          # the German variant of the assumption
    assert cells["N6"] == "500 GB per server."                       # no variant: the plain text
    assert (cells["B9"], cells["B10"], cells["B11"]) == ("Summe monatlich (EUR)", "Summe jährlich (EUR)",
                                                         "Monate pro Jahr")
    assert "Preis-API" in cells["A2"] and re.search(r"\d\d\.\d\d\.\d{4} \d\d:\d\d UTC", cells["A2"])
    src = sheet_cells(xlsx, "Quellen")
    assert src["A3"] == "Angabe" and "Stand der Leistungsbeschreibung" in src.values()
    assert any(str(v).startswith("Stand der Preis-API") for v in src.values())
    md = (folder / "est.md").read_text(encoding="utf-8")
    assert "Summen" in md and "Monatlich: 484,80 EUR" in md and "Jährlich: 5.817,60 EUR" in md
    assert "Annahmen" in md and "Rund um die Uhr in zwei Zonen." in md


def test_the_language_of_the_specification_is_the_default(folder, api):
    spec = spec_file(folder, HEAD + "language: de\n" + WEB % "1")
    assert run(["build", spec]) == 0
    assert openpyxl.load_workbook(folder / "est.xlsx").sheetnames == ["Positionen", "Quellen"]
    assert run(["build", spec, "--lang", "en"]) == 0
    assert openpyxl.load_workbook(folder / "est.xlsx").sheetnames == ["Lines", "Sources"]


# --------------------------------------------------------------------------- exact arithmetic


def test_a_sum_that_floats_get_wrong_is_exact(folder, api):
    body = HEAD + """
[a]
service: evs
flavor: dime.gb
quantity: 1

[b]
service: evs
flavor: twodimes.gb
quantity: 1

[c]
service: evs
flavor: trap.gb
quantity: 1
"""
    spec = spec_file(folder, body)
    assert run(["build", spec]) == 0
    assert 0.1 + 0.2 != 0.3 and round(1.005, 2) == 1.0                # what floats do
    with zipfile.ZipFile(folder / "est.xlsx") as zf:
        sheet = zf.read("xl/worksheets/sheet1.xml").decode()
    assert "<v>0.1</v>" in sheet and "<v>0.2</v>" in sheet
    assert "<v>1.01</v>" in sheet                                      # 1.005 rounds half up to the cent
    assert "<f>SUM(L5:L7)</f><v>1.31</v>" in sheet                     # 0.10 + 0.20 + 1.01, not 1.3100000000000001
    assert "<f>SUM(M5:M7)</f><v>15.72</v>" in sheet
    assert "0000000" not in sheet                                      # no float noise anywhere
    values = sheet_cells(folder / "est.xlsx", values=True)
    assert values["L7"] == 1.01 and values["C10"] == 1.31
    md = (folder / "est.md").read_text(encoding="utf-8")
    assert "Monthly: 1.31 EUR" in md and "Yearly: 15.72 EUR" in md


def test_the_hours_and_the_rounding_follow_the_calculation_rules(folder, api):
    body = HEAD + "\n[a]\nservice: ecs\nflavor: s3.xlarge.2\nquantity: 3\nhours: 730\n"
    spec = spec_file(folder, body)
    assert run(["build", spec]) == 0
    values = sheet_cells(folder / "est.xlsx", values=True)
    assert values["K5"] == 730 and values["L5"] == 372.3 and values["M5"] == 4467.6      # 0.17 * 3 * 730


# --------------------------------------------------------------------------- the summary for a mail


def test_the_summary_names_totals_three_biggest_lines_and_assumptions(folder, api):
    body = (HEAD + WEB % "4" + DISKS + "\n[small]\nservice: evs\nflavor: dime.gb\nquantity: 10\n"
            "assumption: Scratch space.\n\n[tiny]\nservice: evs\nflavor: dime.gb\nquantity: 1\n")
    spec = spec_file(folder, body)
    assert run(["build", spec]) == 0
    md = (folder / "est.md").read_text(encoding="utf-8")
    assert md.startswith("# Sizing for %s\n" % fx.CUSTOMER_CODE)
    assert "- Monthly: 485.90 EUR" in md and "- Yearly: 5,830.80 EUR" in md
    biggest = md.split("## Biggest lines")[1].split("##")[0]
    lines = [l for l in biggest.splitlines() if l.strip()]
    assert [l.split(":")[0] for l in lines] == ["1. web servers", "2. data disks", "3. small"]
    assert "244.80 EUR per month (50.4 % of the monthly total)" in lines[0]
    assumptions = md.split("## Assumptions")[1]
    assert "- web servers: Two zones and all day." in assumptions and "- small: Scratch space." in assumptions
    assert "tiny:" not in assumptions
    assert "720 hours a month" in assumptions
    assert chr(0x2014) not in md
    fx.assert_no_fixture_name(md, "the summary")


@pytest.mark.parametrize("lang", ["en", "de"])
def test_the_summary_passes_the_writing_check_for_a_mail(folder, api, lang, capsys):
    spec = spec_file(folder, HEAD + WEB % "4" + DISKS)
    assert run(["build", spec, "--lang", lang, "--no-record"]) == 0
    capsys.readouterr()
    assert cli.main(["write", "check", str(folder / "est.md"), "--mode", "mail"]) == 0
    assert "0 blocking" in capsys.readouterr().out


def test_the_texts_the_command_writes_have_no_em_dash_and_no_comma_before_and_or():
    for lang, table in pricebook.TEXT.items():
        for key, text in table.items():
            assert chr(0x2014) not in text and chr(0x2013) not in text, (lang, key)
            assert not re.search(r",\s+(?:and|or|und|oder)\b", text), (lang, key)


# --------------------------------------------------------------------------- the totals in the project


def test_the_totals_are_recorded_for_the_review(folder, api, capsys):
    spec = spec_file(folder, HEAD + WEB % "4" + DISKS)
    assert run(["build", spec]) == 0
    assert "recorded  K-1, K-2" in capsys.readouterr().out
    from awb import calc
    first, second = calc.find(folder, "K-1"), calc.find(folder, "K-2")
    assert first.result == "484.80" and first.expression == "sum(244.80, 240.00)"
    assert second.result == "5817.60" and second.expression == "sum(244.80, 240.00) * 12"
    assert calc.appears_in(first.value(), "The monthly total is 484.80 EUR.")
    src = sheet_cells(folder / "est.xlsx", "Sources")
    assert "K-1, K-2" in src.values()
    assert "Recorded calculations: K-1, K-2." in (folder / "est.md").read_text(encoding="utf-8")
    assert run(["build", spec, "--no-record"]) == 0
    assert [r.id for r in calc.load(folder)] == ["K-1", "K-2"]


def test_outside_a_project_nothing_is_recorded(tmp_path, api, capsys):
    spec = spec_file(tmp_path, HEAD + WEB % "1")
    assert run(["build", spec]) == 0
    out = capsys.readouterr().out
    assert "not recorded" in out and not (tmp_path / "calc").exists()


# --------------------------------------------------------------------------- the files


def test_out_names_the_workbook_and_the_summary_goes_next_to_it(folder, api):
    (folder / "deliverables").mkdir()
    spec = spec_file(folder, HEAD + WEB % "1")
    assert run(["build", spec, "--out", folder / "deliverables" / "offer.xlsx"]) == 0
    assert (folder / "deliverables" / "offer.xlsx").is_file() and (folder / "deliverables" / "offer.md").is_file()
    assert not (folder / "est.xlsx").exists()
    assert run(["build", spec, "--out", folder / "offer.csv"]) == 2
    assert run(["build", spec, "--out", folder / "missing" / "offer.xlsx"]) == 2
    assert run(["build", spec, "--out", spec]) == 2


def test_the_same_specification_and_snapshot_give_the_same_bytes(folder, api):
    spec = spec_file(folder, HEAD + WEB % "4" + DISKS)
    when = datetime.datetime(2026, 10, 11, 12, 0, tzinfo=datetime.timezone.utc)

    def fetcher(service, region):
        return price.fetch(service, region, now=when)

    for name in ("one", "two"):
        pricebook.build(spec, out=folder / (name + ".xlsx"), fetcher=fetcher, record=False)
    assert (folder / "one.xlsx").read_bytes() == (folder / "two.xlsx").read_bytes()
    assert sheet_cells(folder / "one.xlsx", "Sources")["B5"] == "2026-10-11 12:00 UTC"


def test_a_bad_specification_writes_nothing_and_exits_two(folder, api, capsys):
    spec = spec_file(folder, HEAD + "[a]\nservice: ecs\n")
    assert run(["build", spec]) == 2
    assert "problem(s)" in capsys.readouterr().err
    assert run(["build", folder / "absent.txt"]) == 2
    assert api.calls == []


# --------------------------------------------------------------------------- the diff


def test_diff_of_a_changed_quantity(folder, api, capsys):
    old = spec_file(folder, HEAD + WEB % "2" + DISKS, "v1.txt")
    new = spec_file(folder, HEAD + WEB % "4" + DISKS, "v2.txt")
    assert run(["diff", old, new]) == 1
    out = capsys.readouterr().out
    assert "changed  web servers  quantity  2 -> 4" in out
    assert "changed  web servers  monthly  122.40 -> 244.80  (+100.0 %)" in out
    assert "changed  web servers  yearly  1468.80 -> 2937.60" in out
    assert "totals   monthly  362.40 -> 484.80  (+122.40 EUR, +33.8 %)" in out
    assert "totals   yearly  4348.80 -> 5817.60  (+1468.80 EUR, +33.8 %)" in out
    assert "data disks" not in out
    assert len(api.calls) == 1                                          # one snapshot serves both versions


def test_diff_without_a_change_exits_zero(folder, api, capsys):
    one = spec_file(folder, HEAD + WEB % "2", "v1.txt")
    two = spec_file(folder, HEAD + WEB % "2", "v2.txt")
    assert run(["diff", one, two]) == 0
    assert "no difference" in capsys.readouterr().out


def test_diff_lists_added_and_removed_lines_and_other_fields(folder, api, capsys):
    old = spec_file(folder, HEAD + WEB % "2" + DISKS, "v1.txt")
    new = spec_file(folder, HEAD + (WEB % "2").replace("mode: payg", "mode: r12").replace(
        "assumption: Two zones and all day.", "assumption: Three zones.") + "\n[backup disks]\nservice: evs\n"
        "flavor: dime.gb\nquantity: 500\n", "v2.txt")
    assert run(["diff", old, new]) == 1
    out = capsys.readouterr().out
    assert "removed  data disks  (evs evs.ssd, quantity 2000, 240.00 EUR per month)" in out
    assert "added    backup disks  (evs dime.gb, quantity 500, 50.00 EUR per month)" in out
    assert "web servers  pricing mode  PAYG -> R12" in out
    assert "web servers  assumption  Two zones and all day. -> Three zones." in out
    assert "web servers  unit price  0.085 -> 45" in out
    assert "web servers  hours  720 -> -" in out


def test_diff_without_prices_compares_the_settings_and_asks_no_api(folder, api, capsys):
    old = spec_file(folder, HEAD + WEB % "2", "v1.txt")
    new = spec_file(folder, HEAD + WEB % "3", "v2.txt")
    assert run(["diff", old, new, "--no-prices"]) == 1
    out = capsys.readouterr().out
    assert "web servers  quantity  2 -> 3" in out and "monthly" not in out.split("totals")[0]
    assert "not compared" in out
    assert api.calls == []


def test_diff_of_two_workbooks_and_of_a_workbook_against_a_specification(folder, api, capsys):
    v1 = spec_file(folder, HEAD + WEB % "2" + DISKS, "v1.txt")
    v2 = spec_file(folder, HEAD + WEB % "4" + DISKS, "v2.txt")
    assert run(["build", v1, "--no-record"]) == 0 and run(["build", v2, "--no-record"]) == 0
    capsys.readouterr()
    assert run(["diff", folder / "v1.xlsx", folder / "v2.xlsx"]) == 1
    out = capsys.readouterr().out
    assert "changed  web servers  quantity  2 -> 4" in out and "totals   monthly  362.40 -> 484.80" in out
    assert run(["diff", folder / "v2.xlsx", v2]) == 0                   # the same estimate, built and specified
    assert run(["diff", folder / "v1.xlsx", v2]) == 1
    assert "quantity  2 -> 4" in capsys.readouterr().out


def test_diff_reads_a_german_workbook_and_an_english_one_alike(folder, api, capsys):
    v = spec_file(folder, HEAD + WEB % "2" + DISKS, "v1.txt")
    assert run(["build", v, "--lang", "de", "--out", folder / "de.xlsx", "--no-record"]) == 0
    assert run(["build", v, "--lang", "en", "--out", folder / "en.xlsx", "--no-record"]) == 0
    capsys.readouterr()
    assert run(["diff", folder / "de.xlsx", folder / "en.xlsx", "--lang", "en"]) == 1
    out = capsys.readouterr().out
    assert "assumption" in out and "quantity" not in out and "totals   monthly  362.40 -> 362.40" in out


def test_diff_reports_a_price_that_moved_between_two_workbooks(folder, api, capsys):
    spec = spec_file(folder, HEAD + WEB % "2", "v1.txt")
    assert run(["build", spec, "--out", folder / "old.xlsx", "--no-record"]) == 0
    for r in api.records:
        if r["id"] == "TEST_S3_LARGE_2_LNX":
            r["priceAmount"] = "0.100000 EUR"
    assert run(["build", spec, "--out", folder / "new.xlsx", "--no-record"]) == 0
    capsys.readouterr()
    assert run(["diff", folder / "old.xlsx", folder / "new.xlsx"]) == 1
    out = capsys.readouterr().out
    assert "unit price  0.085 -> 0.1" in out and "monthly  122.40 -> 144.00" in out


def test_diff_with_the_price_api_unknown_exits_two(folder, api, capsys):
    one = spec_file(folder, HEAD + WEB % "2", "v1.txt")
    two = spec_file(folder, HEAD + WEB % "3", "v2.txt")
    api.mode = "html"
    assert run(["diff", one, two]) == 2
    assert "could not be read" in capsys.readouterr().err


def test_diff_marks_a_line_that_cannot_be_priced_and_compares_no_totals(folder, api, capsys):
    one = spec_file(folder, HEAD + WEB % "2" + "\n[backup]\nservice: csbs\nflavor: x\nquantity: 1\n", "v1.txt")
    two = spec_file(folder, HEAD + WEB % "3", "v2.txt")
    assert run(["diff", one, two]) == 1
    out = capsys.readouterr().out
    assert "not priced" in out and "totals   not compared" in out


# --------------------------------------------------------------------------- build against an older version


def test_build_against_a_workbook_adds_the_sheet_of_changes(folder, api, capsys):
    v1 = spec_file(folder, HEAD + WEB % "2" + DISKS, "v1.txt")
    v2 = spec_file(folder, HEAD + WEB % "4" + DISKS, "v2.txt")
    assert run(["build", v1, "--no-record"]) == 0
    assert run(["build", v2, "--against", folder / "v1.xlsx", "--no-record"]) == 0
    assert "against   3 change(s) in the lines" in capsys.readouterr().out
    xlsx = folder / "v2.xlsx"
    assert openpyxl.load_workbook(xlsx).sheetnames == ["Lines", "Sources", "Changes"]
    cells = sheet_cells(xlsx, "Changes")
    assert [cells["%s3" % c] for c in "ABCDE"] == ["Change", "Item", "Field", "Old", "New"]
    rows = {(cells["B%d" % r], cells["C%d" % r]): (cells["D%d" % r], cells["E%d" % r]) for r in (4, 5, 6)}
    assert rows[("web servers", "Quantity")] == (2, 4)
    assert rows[("web servers", "Monthly (EUR)")] == (122.4, 244.8)
    assert cells["A4"] == "changed"
    top = find_row(cells, "Monthly total (EUR)")
    assert cells["C%d" % top] == 362.4
    assert cells["D%d" % top] == "='Lines'!$C$9" and cells["E%d" % top] == "=D%d-C%d" % (top, top)
    values = sheet_cells(xlsx, "Changes", values=True)
    assert values["D%d" % top] == 484.8 and values["E%d" % top] == 122.4
    yearly = find_row(cells, "Yearly total (EUR)")
    assert cells["D%d" % yearly] == "='Lines'!$C$10" and values["E%d" % yearly] == 1468.8
    md = (folder / "v2.md").read_text(encoding="utf-8")
    assert "## Changes against the old version" in md and "web servers, Quantity: 2 -> 4" in md
    assert "Monthly: from 362.40 to 484.80 EUR (difference +122.40)" in md


def test_build_against_a_specification_and_in_german(folder, api):
    v1 = spec_file(folder, HEAD + WEB % "2" + DISKS, "v1.txt")
    v2 = spec_file(folder, HEAD + (WEB % "2") + "\n[extra]\nservice: evs\nflavor: dime.gb\nquantity: 100\n", "v2.txt")
    assert run(["build", v2, "--against", v1, "--lang", "de", "--no-record"]) == 0
    xlsx = folder / "v2.xlsx"
    assert openpyxl.load_workbook(xlsx).sheetnames == ["Positionen", "Quellen", "Änderungen"]
    cells = sheet_cells(xlsx, "Änderungen")
    assert [cells["%s3" % c] for c in "ABCDE"] == ["Änderung", "Position", "Feld", "Alt", "Neu"]
    kinds = {cells["B%d" % r]: cells["A%d" % r] for r in range(4, 8) if "B%d" % r in cells}
    assert kinds == {"data disks": "entfallen", "extra": "neu"}
    assert "Summen gegenüber der alten Fassung" in cells.values()


def test_build_against_the_same_version_says_so(folder, api, capsys):
    v1 = spec_file(folder, HEAD + WEB % "2", "v1.txt")
    assert run(["build", v1, "--no-record"]) == 0
    assert run(["build", v1, "--against", folder / "v1.xlsx", "--no-record"]) == 0
    assert "against   no difference" in capsys.readouterr().out
    assert "Nothing changed." in (folder / "v1.md").read_text(encoding="utf-8")


def test_build_against_a_file_that_is_no_workbook_is_an_error(folder, api, capsys):
    v1 = spec_file(folder, HEAD + WEB % "2", "v1.txt")
    (folder / "old.xlsx").write_bytes(b"not a workbook")
    assert run(["build", v1, "--against", folder / "old.xlsx"]) == 2
    assert "nothing written" in capsys.readouterr().err
    assert not (folder / "v1.xlsx").exists()


# --------------------------------------------------------------------------- the writer and the reader


def test_the_writer_and_the_reader_agree_and_a_text_stays_a_text(tmp_path):
    a = xlsxlite.Sheet("One")
    a.put(1, 1, "=1+1", style="title")
    a.put(2, 1, Decimal("0.1"))
    a.put(2, 2, Decimal("0.2"))
    a.put(2, 3, Decimal("0.3"), formula="A2+B2", style="money")
    a.put(3, 1, "tab\there\x01 & <b>")
    a.put(4, 1, "")
    b = xlsxlite.Sheet("Two words")
    b.put(1, 1, "x")
    path = tmp_path / "t.xlsx"
    xlsxlite.write(path, [a, b], title="t", created="2026-10-11T12:00:00Z")
    wb = openpyxl.load_workbook(path)
    assert wb.sheetnames == ["One", "Two words"]
    ws = wb["One"]
    assert ws["A1"].data_type == "s" and ws["A1"].value == "=1+1"
    assert ws["C2"].value == "=A2+B2" and openpyxl.load_workbook(path, data_only=True)["One"]["C2"].value == 0.3
    read = xlsxlite.read(path)
    assert [s.name for s in read] == ["One", "Two words"]
    assert read[0].rows[2] == {1: Decimal("0.1"), 2: Decimal("0.2"), 3: Decimal("0.3")}
    assert read[0].rows[3][1] == "tab\there  & <b>" and read[0].rows[4][1] == ""
    with zipfile.ZipFile(path) as zf:
        assert 'fullCalcOnLoad="1"' in zf.read("xl/workbook.xml").decode()


def test_the_writer_refuses_what_a_workbook_cannot_hold():
    for bad in ("", "a/b", "x" * 32, "[x]", "'x"):
        with pytest.raises(xlsxlite.XlsxError):
            xlsxlite.build([xlsxlite.Sheet(bad)])
    with pytest.raises(xlsxlite.XlsxError):
        xlsxlite.build([xlsxlite.Sheet("A"), xlsxlite.Sheet("a")])
    with pytest.raises(xlsxlite.XlsxError):
        xlsxlite.build([])
    with pytest.raises(xlsxlite.XlsxError):
        xlsxlite.Sheet("A").put(0, 1, "x")
    with pytest.raises(xlsxlite.XlsxError):
        xlsxlite.Sheet("A").put(1, 1, "x", style="loud")
    with pytest.raises(xlsxlite.XlsxError):
        xlsxlite.build([xlsxlite.Sheet("A")], created="yesterday")


def test_the_reader_refuses_a_file_that_is_no_workbook_or_carries_a_declaration(tmp_path):
    (tmp_path / "a.xlsx").write_bytes(b"plain")
    with pytest.raises(xlsxlite.XlsxError):
        xlsxlite.read(tmp_path / "a.xlsx")
    with pytest.raises(xlsxlite.XlsxError):
        xlsxlite.read(tmp_path / "missing.xlsx")
    path = tmp_path / "b.xlsx"
    sheet = xlsxlite.Sheet("A")
    sheet.put(1, 1, "x")
    xlsxlite.write(path, [sheet])
    evil = tmp_path / "evil.xlsx"
    with zipfile.ZipFile(path) as src, zipfile.ZipFile(evil, "w") as out:
        for info in src.infolist():
            data = src.read(info.filename)
            if info.filename == "xl/sharedStrings.xml":
                data = b'<?xml version="1.0"?><!DOCTYPE x [<!ENTITY a "aaaa">]>' + data.split(b"?>", 1)[1]
            out.writestr(info, data)
    with pytest.raises(xlsxlite.XlsxError, match="declaration"):
        xlsxlite.read(evil)


def test_a_workbook_that_is_no_lines_sheet_is_refused(tmp_path):
    sheet = xlsxlite.Sheet("A")
    sheet.put(1, 1, "x")
    xlsxlite.write(tmp_path / "w.xlsx", [sheet])
    with pytest.raises(pricebook.PricebookError, match="not a lines sheet"):
        pricebook.book_of_workbook(tmp_path / "w.xlsx")


# --------------------------------------------------------------------------- the rules of the repository


def test_runtime_code_uses_the_standard_library_and_the_package_only():
    import sys
    stdlib = set(sys.stdlib_module_names)
    for rel in ("awb/tcp/pricebook.py", "awb/xlsxlite.py"):
        tree = ast.parse((ROOT / rel).read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            names = []
            if isinstance(node, ast.Import):
                names = [a.name.split(".")[0] for a in node.names]
            elif isinstance(node, ast.ImportFrom) and node.level == 0:
                names = [(node.module or "").split(".")[0]]
            for name in names:
                assert name in stdlib or name == "awb", (rel, name)


def test_the_command_is_wired_and_documented():
    assert "pricebook" in cli.DELEGATED
    assert cli.main(["pricebook"]) == 2
    for doc in ("COMMANDS.md", "README.md"):
        text = (ROOT / doc).read_text(encoding="utf-8")
        assert "awb pricebook build" in text and "awb pricebook diff" in text, doc
    changelog = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
    assert "awb pricebook build" in changelog.split("\n## Week 40")[0]


def test_the_rows_of_this_file_are_in_the_test_list():
    rows = json.loads((ROOT / "build" / "tests.json").read_text(encoding="utf-8"))
    mine = [t for t in rows["tests"] if t["id"].startswith("PB-")]
    assert len(mine) >= 8 and all("tests/test_pricebook.py" in t["run"] for t in mine)
    assert rows["total_tests"] == len(rows["tests"])


# Boundary regressions use only temporary files and the local fake API.

@pytest.fixture(autouse=True)
def pricebook_privacy_register(home):
    """Exercise the real name screen with the invented register for every pricebook test."""
    return home.register


def test_json_duplicate_keys_are_refused():
    for body in ('{"lines": [], "lines": [{"label":"a","service":"ecs","flavor":"x","quantity":1}]}',
                 '{"lines":[{"label":"a","service":"ecs","flavor":"x","quantity":1,"quantity":2}]}'):
        with pytest.raises(pricebook.SpecError):
            pricebook.parse_spec(body.encode(), "a.json")


def test_json_nesting_has_a_controlled_refusal():
    import sys
    # Python 3.12's C JSON decoder has a separate recursion limit.
    depth = max(10000, sys.getrecursionlimit() + 100)
    with pytest.raises(pricebook.SpecError, match="not valid JSON"):
        pricebook.parse_spec(('{"lines":' + '[' * depth + '0' + ']' * depth + '}').encode())


def test_json_numbers_are_bounded_before_fixed_point_expansion(monkeypatch):
    import builtins
    def bounded_format(value, spec):
        if isinstance(value, Decimal):
            assert value.is_finite() and abs(value.as_tuple().exponent) <= 128, "expanded an unbounded number"
        return builtins.format(value, spec)
    monkeypatch.setattr(pricebook, "format", bounded_format, raising=False)
    for value in ('1e10000', '1e-10000', 'NaN', 'Infinity'):
        data = '{"lines":[{"label":"a","service":"ecs","flavor":"x","quantity":1,"assumption":%s}]}' % value
        with pytest.raises(pricebook.SpecError):
            pricebook.parse_spec(data.encode())


def test_json_surrogates_are_refused():
    data = {"title": "\ud800", "lines": [{"label": "a", "service": "ecs", "flavor": "x", "quantity": 1}]}
    with pytest.raises(pricebook.SpecError):
        pricebook.parse_spec(json.dumps(data).encode())


def test_control_characters_are_rejected_before_whitespace_normalization():
    body = {"lines": [{"label": "a", "service": "ecs", "flavor": "x", "quantity": 1,
                        "assumption": "hidden\x0bvalue"}]}
    with pytest.raises(pricebook.SpecError, match="invalid character"):
        pricebook.parse_spec(json.dumps(body).encode())


def test_specification_links_are_refused(tmp_path):
    source = spec_file(tmp_path, HEAD + WEB % "1")
    link = tmp_path / "link.txt"
    link.symlink_to(source)
    with pytest.raises(pricebook.PricebookError):
        pricebook.load_spec(link)


def test_input_parent_links_are_refused(tmp_path):
    real = tmp_path / "real"
    real.mkdir()
    spec_file(real, HEAD + WEB % "1")
    link = tmp_path / "linked"
    link.symlink_to(real, target_is_directory=True)
    with pytest.raises(pricebook.PricebookError):
        pricebook.load_spec(link / "est.txt")


def test_special_inputs_are_refused_without_opening_a_stream(tmp_path, monkeypatch):
    import os
    path = tmp_path / "pipe.txt"
    os.mkfifo(path)
    original = Path.read_bytes

    def read(p):
        assert p != path, "a FIFO was handed to a blocking reader"
        return original(p)

    monkeypatch.setattr(Path, "read_bytes", read)
    original_open = os.open
    def open_file(name, flags, *args, **kwargs):
        if Path(name).name == path.name:
            assert flags & os.O_NONBLOCK, "a FIFO would block the reader"
        return original_open(name, flags, *args, **kwargs)
    monkeypatch.setattr(os, "open", open_file)
    with pytest.raises(pricebook.PricebookError, match="bounded regular file"):
        pricebook.load_spec(path)


def test_specification_size_is_bounded_during_read(tmp_path, monkeypatch):
    path = tmp_path / "large.txt"
    with path.open("wb") as stream:
        stream.truncate(pricebook.MAX_SPEC_BYTES + 1)
    original = Path.read_bytes

    def read(p):
        assert p != path, "an unbounded read was attempted"
        return original(p)

    monkeypatch.setattr(Path, "read_bytes", read)
    with pytest.raises(pricebook.PricebookError, match="bounded regular file"):
        pricebook.load_spec(path)


def test_missing_and_unreadable_inputs_have_value_free_errors(tmp_path, monkeypatch):
    import os
    path = tmp_path / "missing.txt"
    with pytest.raises(pricebook.PricebookError) as error:
        pricebook.load_spec(path)
    assert str(path) not in str(error.value)
    path.write_text(HEAD + WEB % "1")
    real_open = os.open

    def denied(name, *args, **kwargs):
        if Path(name).name == path.name:
            raise PermissionError("pass" + "word=InventedValue42")
        return real_open(name, *args, **kwargs)

    monkeypatch.setattr(os, "open", denied)
    with pytest.raises(pricebook.PricebookError) as error:
        pricebook.load_spec(path)
    assert "InventedValue42" not in str(error.value)


def test_private_spec_fields_never_reach_outputs(folder, api, home, capsys):
    for text in (fx.CUSTOMER_FORMS[0], "pass" + "word=InventedValue42"):
        spec = spec_file(folder, HEAD + WEB % "1" + "assumption.en: " + text + "\n")
        assert run(["build", spec]) == 2
        output = capsys.readouterr()
        assert text not in output.out + output.err
        assert not (folder / "est.xlsx").exists()
        assert not (folder / "est.md").exists()
        assert not (folder / "calc").exists()
    assert api.calls == []


def test_invalid_spec_errors_withhold_private_keys_and_labels(folder, home, capsys):
    private = fx.PERSON_FORMS[0]
    spec = spec_file(folder, json.dumps({"lines": [{"label": private, private: "bad"}]}), "bad.json")
    assert pricebook.main(["build", str(spec)]) == 2
    output = capsys.readouterr()
    assert private not in output.out + output.err


def test_private_filename_default_title_is_refused(folder, api, home, capsys):
    spec = spec_file(folder, WEB % "1", fx.CUSTOMER_FORMS[1] + ".txt")
    with pytest.raises(pricebook.PricebookError, match="withheld"):
        pricebook.load_spec(spec)
    assert pricebook.main(["build", str(spec), "--out", str(folder / "safe.xlsx")]) == 2
    output = capsys.readouterr()
    assert fx.CUSTOMER_FORMS[1] not in output.out + output.err
    assert not spec.with_suffix(".xlsx").exists()
    assert not (folder / "safe.xlsx").exists()


def test_api_metadata_is_screened_before_recording(folder, api, home):
    private = fx.CUSTOMER_FORMS[0]
    for row in api.records:
        if row["id"] == "TEST_S3_LARGE_2_LNX":
            row["osUnit"] = "Open Linux " + private
    spec = spec_file(folder, HEAD + WEB % "1")
    with pytest.raises(pricebook.PricebookError):
        pricebook.build(spec)
    assert not (folder / "est.xlsx").exists()
    assert not (folder / "calc").exists()


def test_privacy_check_failure_refuses_output(folder, api, monkeypatch):
    from awb import check
    def unavailable(*args, **kwargs):
        raise RuntimeError("pass" + "word=InventedValue42")
    monkeypatch.setattr(check, "check_text", unavailable)
    spec = spec_file(folder, HEAD + WEB % "1")
    with pytest.raises(pricebook.PricebookError) as error:
        pricebook.build(spec)
    assert "InventedValue42" not in str(error.value)
    assert not (folder / "est.xlsx").exists()


def test_dependency_errors_never_echo_values(folder, monkeypatch, capsys):
    spec = spec_file(folder, HEAD + WEB % "1")
    private = "pass" + "word=InventedValue42"
    def failed(*args, **kwargs):
        raise price.PriceError(private)
    monkeypatch.setattr(price, "fetch", failed)
    assert pricebook.main(["build", str(spec)]) == 2
    output = capsys.readouterr()
    assert private not in output.out + output.err
    assert pricebook.main(["diff", str(spec), str(spec)]) == 2
    output = capsys.readouterr()
    assert private not in output.out + output.err


def test_diff_filenames_and_notes_are_screened(folder, home, monkeypatch, capsys):
    private = fx.CUSTOMER_FORMS[1]
    old = spec_file(folder, HEAD + WEB % "1", private + ".txt")
    new = spec_file(folder, HEAD + WEB % "2", "new.txt")
    assert pricebook.main(["diff", str(old), str(new), "--no-prices"]) == 1
    output = capsys.readouterr()
    assert private not in output.out + output.err


def test_outputs_cannot_escape_a_project(folder, api):
    spec = spec_file(folder, HEAD + WEB % "1")
    out = folder.parent / (folder.name + ".xlsx")
    with pytest.raises(pricebook.PricebookError):
        pricebook.build(spec, out=out)
    assert not out.exists()
    assert not (folder / "calc").exists()


def test_explicit_project_must_contain_the_specification(tmp_path, api):
    project = tmp_path / "project"
    project.mkdir()
    (project / "SCOPE.md").write_text("# scope\n")
    spec = spec_file(tmp_path, HEAD + WEB % "1")
    with pytest.raises(pricebook.PricebookError):
        pricebook.build(spec, project=project)
    assert not (project / "calc").exists()


def test_output_links_and_special_files_are_preserved(folder, api):
    import os
    spec = spec_file(folder, HEAD + WEB % "1")
    victim = folder / "keep"
    victim.write_bytes(b"keep")
    for suffix in (".xlsx", ".md"):
        path = spec.with_suffix(suffix)
        path.symlink_to(victim)
        with pytest.raises(pricebook.PricebookError):
            pricebook.build(spec, record=False)
        assert path.is_symlink() and victim.read_bytes() == b"keep"
        path.unlink()
        os.mkfifo(path)
        with pytest.raises(pricebook.PricebookError):
            pricebook.build(spec, record=False)
        assert path.is_fifo()
        path.unlink()


def test_summary_cannot_overwrite_the_specification(folder, api):
    body = HEAD + WEB % "1"
    spec = spec_file(folder, body, "est.md")
    with pytest.raises(pricebook.PricebookError):
        pricebook.build(spec)
    assert spec.read_text() == body
    assert not spec.with_suffix(".xlsx").exists()


def test_output_parent_links_are_refused(folder, api):
    real = folder / "real"
    real.mkdir()
    link = folder / "linked"
    link.symlink_to(real, target_is_directory=True)
    spec = spec_file(folder, HEAD + WEB % "1")
    with pytest.raises(pricebook.PricebookError):
        pricebook.build(spec, out=link / "out.xlsx")
    assert list(real.iterdir()) == []


def test_failed_second_publication_restores_both_outputs(folder, api, monkeypatch):
    import os
    spec = spec_file(folder, HEAD + WEB % "1")
    xlsx, summary = spec.with_suffix(".xlsx"), spec.with_suffix(".md")
    xlsx.write_bytes(b"old workbook")
    summary.write_bytes(b"old summary")
    before = sorted(p.name for p in folder.iterdir())
    original = os.replace
    failed = False
    def replace(src, dst, *args, **kwargs):
        nonlocal failed
        if Path(dst).name == summary.name and not failed:
            failed = True
            raise OSError("pass" + "word=InventedValue42")
        return original(src, dst, *args, **kwargs)
    monkeypatch.setattr(os, "replace", replace)
    with pytest.raises(pricebook.PricebookError) as error:
        pricebook.build(spec, record=False)
    assert "InventedValue42" not in str(error.value)
    assert xlsx.read_bytes() == b"old workbook" and summary.read_bytes() == b"old summary"
    assert sorted(p.name for p in folder.iterdir()) == before


def test_concurrent_build_is_refused_before_fetch(folder, api):
    import fcntl
    import os
    spec = spec_file(folder, HEAD + WEB % "1")
    fd = os.open(folder, os.O_RDONLY | os.O_DIRECTORY)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with pytest.raises(pricebook.PricebookError):
            pricebook.build(spec, record=False)
    finally:
        os.close(fd)
    assert api.calls == []
    assert not spec.with_suffix(".xlsx").exists()


def test_directory_swap_during_build_cannot_redirect_writes(folder, api):
    destination = folder / "output"
    destination.mkdir()
    elsewhere = folder / "elsewhere"
    elsewhere.mkdir()
    spec = spec_file(folder, HEAD + WEB % "1")
    def fetcher(service, region):
        destination.rename(folder / "moved")
        destination.symlink_to(elsewhere, target_is_directory=True)
        return price.fetch(service, region)
    with pytest.raises(pricebook.PricebookError):
        pricebook.build(spec, out=destination / "book.xlsx", fetcher=fetcher, record=False)
    assert list(elsewhere.iterdir()) == []


def test_workbook_links_are_refused(folder, api):
    spec = spec_file(folder, HEAD + WEB % "1")
    result = pricebook.build(spec, record=False)
    link = folder / "link.xlsx"
    link.symlink_to(result.xlsx)
    with pytest.raises(pricebook.PricebookError):
        pricebook.book_of_workbook(link)


def _boundary_workbook(path, field, value):
    sheet = xlsxlite.Sheet("Lines")
    for column, name in enumerate(pricebook.COLUMNS, 1):
        sheet.put(4, column, pricebook.TEXT["en"]["h_" + name])
    for name, item in {"item": "a", "service": "ecs", "quantity": Decimal(1),
                       "monthly": Decimal(1), "yearly": Decimal(12), field: value}.items():
        sheet.put(5, pricebook.COL[name], item)
    xlsxlite.write(path, [sheet])


def test_workbook_field_types_are_validated(tmp_path):
    path = tmp_path / "bad.xlsx"
    for field, value in (("service", Decimal(2)), ("quantity", "many")):
        _boundary_workbook(path, field, value)
        with pytest.raises(pricebook.PricebookError):
            pricebook.book_of_workbook(path)


def test_workbook_nonfinite_and_extreme_numbers_are_refused(tmp_path):
    path = tmp_path / "bad.xlsx"
    _boundary_workbook(path, "quantity", Decimal(1))
    original = path.read_bytes()
    import io
    for value in ("NaN", "Infinity", "1e999999", "1e30", "1e-129"):
        with zipfile.ZipFile(io.BytesIO(original)) as source, zipfile.ZipFile(path, "w") as target:
            for info in source.infolist():
                data = source.read(info.filename)
                if info.filename == "xl/worksheets/sheet1.xml":
                    data = data.replace(b'<c r="J5"><v>1</v>', ('<c r="J5"><v>' + value + '</v>').encode())
                target.writestr(info, data)
        with pytest.raises(pricebook.PricebookError):
            pricebook.book_of_workbook(path)


def test_workbook_private_cells_are_refused(tmp_path, home):
    path = tmp_path / "bad.xlsx"
    _boundary_workbook(path, "assumption", fx.CUSTOMER_FORMS[0])
    with pytest.raises(pricebook.PricebookError):
        pricebook.book_of_workbook(path)


def test_pool_settings_are_compared_without_prices():
    body = HEAD + '[pool]\nservice: ecs\nflavor: s3.large.2\nmode: pool\nterm: r12\npooled_hours: %s\n'
    old = pricebook.book_of_spec(pricebook.parse_spec((body % "100").encode()), None, {}, "en")
    new = pricebook.book_of_spec(pricebook.parse_spec((body % "200").encode()), None, {}, "en")
    assert pricebook.compare(old, new).changed
    later = pricebook.book_of_spec(pricebook.parse_spec((body % "100" + "start: 2026-01-01\n").encode()),
                                   None, {}, "en")
    assert pricebook.compare(old, later).changed


def test_pool_start_must_fit_the_entire_term():
    body = HEAD + '[pool]\nservice: ecs\nflavor: s3.large.2\nmode: pool\nterm: r36\npooled_hours: 100\n'
    with pytest.raises(pricebook.SpecError):
        pricebook.parse_spec((body + 'start: 9999-12-01\n').encode())


def test_duplicate_price_record_ids_are_not_guessed(folder, api):
    api.records.append(dict(api.records[0]))
    spec = spec_file(folder, HEAD + '[a]\nservice: ecs\nid: ' + api.records[0]["id"] + '\nquantity: 1\n')
    with pytest.raises(pricebook.LineRefused):
        pricebook.build(spec, record=False)


def test_changed_outputs_are_preserved(folder, api):
    spec = spec_file(folder, HEAD + WEB % "1")
    out = spec.with_suffix(".xlsx")
    out.write_bytes(b"old")
    def fetcher(service, region):
        out.write_bytes(b"concurrent edit")
        return price.fetch(service, region)
    with pytest.raises(pricebook.PricebookError):
        pricebook.build(spec, fetcher=fetcher, record=False)
    assert out.read_bytes() == b"concurrent edit"
    assert not spec.with_suffix(".md").exists()


def test_project_marker_links_are_refused(tmp_path, api):
    marker = tmp_path / "marker"
    marker.write_text("# scope\n")
    (tmp_path / "SCOPE.md").symlink_to(marker)
    spec = spec_file(tmp_path, HEAD + WEB % "1")
    with pytest.raises(pricebook.PricebookError):
        pricebook.build(spec)
    assert not (tmp_path / "calc").exists()


def test_workbook_duplicate_zip_parts_are_refused(tmp_path):
    path = tmp_path / "bad.xlsx"
    _boundary_workbook(path, "quantity", Decimal(1))
    with zipfile.ZipFile(path, "a") as archive:
        with pytest.warns(UserWarning, match="Duplicate"):
            archive.writestr("xl/workbook.xml", archive.read("xl/workbook.xml"))
    with pytest.raises(pricebook.PricebookError):
        pricebook.book_of_workbook(path)


def test_workbook_total_expansion_is_bounded(tmp_path):
    path = tmp_path / "bad.xlsx"
    _boundary_workbook(path, "quantity", Decimal(1))
    with zipfile.ZipFile(path, "a", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("unused.xml", b"x" * (16 * 1024 * 1024))
    with pytest.raises(pricebook.PricebookError):
        pricebook.book_of_workbook(path)


def _record_boundary_totals(folder):
    from types import SimpleNamespace
    return pricebook.record_totals(folder, [SimpleNamespace(monthly=Decimal(1))], Decimal(1), Decimal(12))


def test_calc_special_files_are_refused_without_blocking(folder, monkeypatch):
    import os
    from awb import calc
    path = calc.path_of(folder)
    path.parent.mkdir()
    os.mkfifo(path)
    original = os.open
    def open_file(name, flags, *args, **kwargs):
        if Path(name).name == path.name:
            assert flags & os.O_NONBLOCK, "the record opened a special file in blocking mode"
        return original(name, flags, *args, **kwargs)
    monkeypatch.setattr(os, "open", open_file)
    with pytest.raises(calc.CalcError):
        _record_boundary_totals(folder)
    assert path.is_fifo()


def test_calc_hardlinks_are_refused(folder):
    import os
    from awb import calc
    path = calc.path_of(folder)
    path.parent.mkdir()
    victim = folder / "keep.tsv"
    victim.write_bytes(b"")
    os.link(victim, path)
    with pytest.raises(calc.CalcError):
        _record_boundary_totals(folder)
    assert victim.read_bytes() == b""


def test_calc_malformed_records_are_preserved(folder):
    from awb import calc
    path = calc.path_of(folder)
    path.parent.mkdir()
    body = "wrong\theader\nK-4\tbad\n"
    path.write_text(body)
    with pytest.raises(calc.CalcError):
        _record_boundary_totals(folder)
    assert path.read_text() == body


def test_calc_size_is_bounded_before_reading(folder):
    from awb import calc
    path = calc.path_of(folder)
    path.parent.mkdir()
    body = "\t".join(calc.HEADER) + "\n" + "\n" * pricebook.MAX_SPEC_BYTES
    path.write_text(body)
    with pytest.raises(calc.CalcError, match="bounded regular file"):
        _record_boundary_totals(folder)
    assert path.read_text() == body


def test_calc_directory_swap_preserves_the_old_record(folder, monkeypatch):
    import os
    from awb import calc
    path = calc.path_of(folder)
    path.parent.mkdir()
    path.write_bytes(b"")
    moved = folder / "moved"
    original = os.open
    def open_file(name, flags, *args, **kwargs):
        fd = original(name, flags, *args, **kwargs)
        if Path(name).name == path.name:
            path.parent.rename(moved)
            path.parent.mkdir()
        return fd
    monkeypatch.setattr(os, "open", open_file)
    with pytest.raises(calc.CalcError):
        _record_boundary_totals(folder)
    assert (moved / path.name).read_bytes() == b""


def test_calc_partial_append_is_rolled_back(folder, monkeypatch):
    import os
    from awb import calc
    path = calc.path_of(folder)
    path.parent.mkdir()
    path.write_bytes(b"")
    original = os.fsync
    def failed(fd):
        if os.path.samestat(os.fstat(fd), path.stat()):
            raise OSError("pass" + "word=InventedValue42")
        return original(fd)
    monkeypatch.setattr(os, "fsync", failed)
    with pytest.raises(calc.CalcError) as error:
        _record_boundary_totals(folder)
    assert "InventedValue42" not in str(error.value)
    assert path.read_bytes() == b""


def test_calc_lock_contention_has_a_bounded_refusal(folder, monkeypatch):
    import fcntl
    from awb import calc
    path = calc.path_of(folder)
    path.parent.mkdir()
    path.write_bytes(b"")
    original = fcntl.flock
    with path.open("rb") as held:
        original(held, fcntl.LOCK_EX)
        def lock(fd, flags):
            assert flags & fcntl.LOCK_NB, "a held record lock would wait without a bound"
            return original(fd, flags)
        monkeypatch.setattr(fcntl, "flock", lock)
        with pytest.raises(calc.CalcError):
            _record_boundary_totals(folder)
    assert path.read_bytes() == b""


def test_input_and_output_hardlinks_are_refused(folder, api):
    import os
    spec = spec_file(folder, HEAD + WEB % "1")
    alias = folder / "alias.txt"
    os.link(spec, alias)
    with pytest.raises(pricebook.PricebookError):
        pricebook.load_spec(alias)
    alias.unlink()
    keep = folder / "keep"
    keep.write_bytes(b"keep")
    os.link(keep, spec.with_suffix(".md"))
    with pytest.raises(pricebook.PricebookError):
        pricebook.build(spec)
    assert keep.read_bytes() == b"keep"


def test_generated_files_are_private(folder, api):
    import stat
    result = pricebook.build(spec_file(folder, HEAD + WEB % "1"), record=False)
    assert stat.S_IMODE(result.xlsx.stat().st_mode) == 0o600
    assert stat.S_IMODE(result.summary.stat().st_mode) == 0o600


def test_api_unknown_reason_is_screened_at_the_library_boundary(home):
    spec = pricebook.parse_spec((HEAD + WEB % "1").encode())
    private = "pass" + "word=InventedValue42"
    fetch = price.Fetch(jobs.Listing.unknown(private), "unused", "2026-11-17 12:00 UTC")
    with pytest.raises(pricebook.PricebookError) as error:
        pricebook.price_spec(spec, lambda region: fetch)
    assert private not in str(error.value)


def test_private_output_filenames_are_refused(folder, api, home):
    spec = spec_file(folder, HEAD + WEB % "1")
    out = folder / (fx.CUSTOMER_FORMS[1] + ".xlsx")
    with pytest.raises(pricebook.PricebookError):
        pricebook.build(spec, out=out)
    assert not out.exists() and not out.with_suffix(".md").exists()
    assert api.calls == []


def test_workbook_parser_errors_do_not_echo_part_targets(tmp_path):
    import io
    path = tmp_path / "bad.xlsx"
    _boundary_workbook(path, "quantity", Decimal(1))
    original = path.read_bytes()
    private = "pass" + "word=InventedValue42"
    with zipfile.ZipFile(io.BytesIO(original)) as source, zipfile.ZipFile(path, "w") as target:
        for info in source.infolist():
            data = source.read(info.filename)
            if info.filename == "xl/_rels/workbook.xml.rels":
                data = data.replace(b"worksheets/sheet1.xml", private.encode())
            target.writestr(info, data)
    with pytest.raises(pricebook.PricebookError) as error:
        pricebook.book_of_workbook(path)
    assert private not in str(error.value)


def test_argument_errors_do_not_echo_quoted_values(capsys):
    private = "pass" + "word=InventedValue42'quoted"
    assert pricebook.main(["diff", "old", "new", "--lang", private]) == 2
    output = capsys.readouterr()
    assert "InventedValue42" not in output.out + output.err


def test_json_depth_and_significand_limits_are_enforced():
    # These are below Python's recursion limit and do not need fixed-point expansion.
    nested = '{"lines": [], "extra":' + '[' * 65 + '0' + ']' * 65 + '}'
    with pytest.raises(pricebook.SpecError, match="too deeply nested"):
        pricebook.parse_spec(nested.encode())
    number = '1.' + '2' * 128
    body = '{"lines":[{"label":"a","service":"ecs","flavor":"x","assumption":%s}]}' % number
    with pytest.raises(pricebook.SpecError, match="not valid JSON"):
        pricebook.parse_spec(body.encode())


def test_json_decoded_private_values_are_refused(home):
    for private in (fx.CUSTOMER_FORMS[0], "pass" + "word=InventedValue42"):
        encoded = ''.join('\\u%04x' % ord(char) for char in private)
        body = '{"lines":[{"label":"a","service":"ecs","flavor":"x","assumption":"%s"}]}' % encoded
        with pytest.raises(pricebook.PricebookError, match="withheld"):
            pricebook.parse_spec(body.encode())


def test_text_controls_are_refused_and_crlf_is_supported():
    body = HEAD + WEB % "1"
    assert pricebook.parse_spec(body.replace('\n', '\r\n').encode()).lines
    with pytest.raises(pricebook.SpecError, match="invalid character"):
        pricebook.parse_spec((body + '# hidden\x0bvalue\n').encode())
    problems = []
    assert pricebook._clean_text("hidden\x0bvalue", "text", 80, problems, "") == ""
    assert problems == ["text has a control character"]


def test_read_growth_is_bounded_even_after_a_small_stat(tmp_path, monkeypatch):
    import os
    from types import SimpleNamespace
    path = spec_file(tmp_path, HEAD + WEB % "1")
    original = os.fstat
    def stale(fd):
        info = original(fd)
        return SimpleNamespace(st_mode=info.st_mode, st_nlink=info.st_nlink, st_size=0)
    monkeypatch.setattr(os, "fstat", stale)
    with pytest.raises(pricebook.PricebookError, match="too large"):
        pricebook._read_file(path, 10)


def test_pricing_errors_are_controlled_and_private_reasons_are_screened(monkeypatch):
    from awb import calc
    from decimal import InvalidOperation
    spec = pricebook.parse_spec((HEAD + WEB % "1").encode())
    fetch = price.Fetch(jobs.Listing.of([object()]), "unused", "2026-11-17 12:00 UTC")
    private = "pass" + "word=InventedValue42"
    for error in (calc.CalcError, InvalidOperation, ValueError, OverflowError, pricebook.LineProblem):
        def fail(*args):
            raise error(private)
        monkeypatch.setattr(pricebook, "price_line", fail)
        priced, problems = pricebook.price_spec(spec, lambda region: fetch)
        assert priced == [] and set(problems) == {"web servers"}
        assert private not in problems["web servers"]


def test_pool_dates_use_the_canonical_format():
    body = HEAD + '[pool]\nservice: ecs\nflavor: s3.large.2\nmode: pool\nterm: r12\npooled_hours: 100\n'
    for start in ('20260101', '2026-W01-1'):
        with pytest.raises(pricebook.SpecError, match="YYYY-MM-DD"):
            pricebook.parse_spec((body + 'start: ' + start + '\n').encode())


def test_optional_pool_fields_report_additions_and_removals():
    for field, value in (("pooled_hours", Decimal(100)), ("start", "2026-01-01")):
        old = pricebook.Book("spec", "old", {"a": ("a", {})}, None, None)
        new = pricebook.Book("spec", "new", {"a": ("a", {field: value})}, None, None)
        for before, after in ((old, new), (new, old)):
            changes = pricebook.compare(before, after)
            assert [c.name for c in changes.changes] == [field]
            assert pricebook.FIELD_ENGLISH[field] in '\n'.join(pricebook.comparison_lines(changes))


def test_calc_rejects_inconsistent_yearly_totals_before_writing(folder):
    from awb import calc
    from types import SimpleNamespace
    with pytest.raises(calc.CalcError, match="differs"):
        pricebook.record_totals(folder, [SimpleNamespace(monthly=Decimal(1))], Decimal(1), Decimal(13))
    assert not calc.path_of(folder).exists()


def test_calc_links_preserve_the_destination(folder, monkeypatch):
    import os
    from awb import calc
    path = calc.path_of(folder)
    elsewhere = folder / "elsewhere"
    elsewhere.mkdir()
    victim = elsewhere / calc.FILE
    victim.write_bytes(b"")
    original = os.fdopen
    def open_file(fd, *args, **kwargs):
        if os.path.samestat(os.fstat(fd), victim.stat()):
            os.close(fd)
            pytest.fail("a linked calculation record reached the stream reader")
        return original(fd, *args, **kwargs)
    monkeypatch.setattr(os, "fdopen", open_file)
    path.parent.symlink_to(elsewhere, target_is_directory=True)
    with pytest.raises(calc.CalcError):
        _record_boundary_totals(folder)
    assert victim.read_bytes() == b""
    path.parent.unlink()
    path.parent.mkdir()
    path.symlink_to(victim)
    with pytest.raises(calc.CalcError):
        _record_boundary_totals(folder)
    assert victim.read_bytes() == b"" and path.is_symlink()


def test_calc_invalid_rows_and_encoding_are_preserved(folder):
    from awb import calc
    path = calc.path_of(folder)
    path.parent.mkdir()
    header = '\t'.join(calc.HEADER) + '\n'
    row = 'K-1\t2026-11-17T12:00:00Z\tlabel\t1\t\t1\n'
    for body in (header + row + row, header + row.replace('K-1', 'bad'),
                 header + row.replace('K-1', 'K-' + '1' * 15), header + 'K-1\tbad\n',
                 (header + row).rstrip('\n'), header.encode() + b'\xff\n'):
        data = body.encode() if isinstance(body, str) else body
        path.write_bytes(data)
        with pytest.raises(calc.CalcError):
            _record_boundary_totals(folder)
        assert path.read_bytes() == data


def test_calc_replaced_file_is_preserved(folder, monkeypatch):
    import os
    from awb import calc
    path = calc.path_of(folder)
    path.parent.mkdir()
    path.write_bytes(b"")
    moved = folder / "old.tsv"
    original = pricebook._same_directory
    def swap(parent, directory):
        original(parent, directory)
        os.replace(path, moved)
        path.write_bytes(b"concurrent edit")
    monkeypatch.setattr(pricebook, "_same_directory", swap)
    with pytest.raises(calc.CalcError):
        _record_boundary_totals(folder)
    assert path.read_bytes() == b"concurrent edit" and moved.read_bytes() == b""


def test_calc_identifier_exhaustion_preserves_the_record(folder):
    from awb import calc
    path = calc.path_of(folder)
    path.parent.mkdir()
    for last in ('K-99998', 'K-99999'):
        body = '\t'.join(calc.HEADER) + '\n' + last + '\t2026-11-17T12:00:00Z\tlabel\t1\t\t1\n'
        path.write_text(body)
        with pytest.raises(calc.CalcError, match="identifiers"):
            _record_boundary_totals(folder)
        assert path.read_text() == body


def test_calc_short_writes_complete_both_records(folder, monkeypatch):
    import os
    import stat
    from awb import calc
    original = os.fdopen
    class ShortWriter:
        def __init__(self, stream):
            self.stream = stream
        def __enter__(self):
            self.stream.__enter__()
            return self
        def __exit__(self, *args):
            return self.stream.__exit__(*args)
        def __getattr__(self, name):
            return getattr(self.stream, name)
        def write(self, data):
            return self.stream.write(data[:7])
    def open_file(fd, mode, **kwargs):
        stream = original(fd, mode, **kwargs)
        return ShortWriter(stream) if mode == 'r+b' else stream
    monkeypatch.setattr(os, "fdopen", open_file)
    assert _record_boundary_totals(folder) == ['K-1', 'K-2']
    assert _record_boundary_totals(folder) == ['K-3', 'K-4']
    path = calc.path_of(folder)
    rows = path.read_text().splitlines()
    assert len(rows) == 5 and all(len(row.split('\t')) == len(calc.HEADER) for row in rows)
    assert [row.split('\t')[-1] for row in rows[1:]] == ['1.00', '12.00', '1.00', '12.00']
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_calculation_and_writer_errors_withhold_values(folder, api, monkeypatch):
    from awb import calc
    spec = spec_file(folder, HEAD + WEB % "1")
    private = "pass" + "word=InventedValue42"
    def failed_record(*args):
        raise calc.CalcError(private)
    monkeypatch.setattr(pricebook, "record_totals", failed_record)
    result = pricebook.build(spec)
    assert result.calc_ids == [] and result.notes
    assert private not in '\n'.join(result.notes)
    def failed_writer(*args, **kwargs):
        raise xlsxlite.XlsxError(private)
    monkeypatch.setattr(xlsxlite, "build", failed_writer)
    with pytest.raises(pricebook.PricebookError) as error:
        pricebook.build(spec, record=False)
    assert private not in str(error.value)


def test_summary_is_screened_before_recording(folder, api, monkeypatch):
    spec = spec_file(folder, HEAD + WEB % "1")
    monkeypatch.setattr(pricebook, "build_summary", lambda *args: fx.CUSTOMER_FORMS[0])
    with pytest.raises(pricebook.PricebookError, match="withheld"):
        pricebook.build(spec)
    assert not (folder / 'calc').exists()
    assert not spec.with_suffix('.xlsx').exists()
    assert not spec.with_suffix('.md').exists()


def test_publication_failure_removes_new_outputs_and_staged_files(folder, api, monkeypatch):
    import os
    spec = spec_file(folder, HEAD + WEB % "1")
    before = sorted(p.name for p in folder.iterdir())
    original = os.replace
    def fail_summary(src, dst, **kwargs):
        if dst == 'est.md':
            raise OSError('unavailable')
        return original(src, dst, **kwargs)
    monkeypatch.setattr(os, 'replace', fail_summary)
    with pytest.raises(pricebook.PricebookError):
        pricebook.build(spec, record=False)
    assert sorted(p.name for p in folder.iterdir()) == before


def test_staging_failure_preserves_outputs_and_removes_temporary_files(folder, api, monkeypatch):
    import os
    spec = spec_file(folder, HEAD + WEB % "1")
    spec.with_suffix('.xlsx').write_bytes(b'old workbook')
    spec.with_suffix('.md').write_bytes(b'old summary')
    before = {p.name: p.read_bytes() for p in folder.iterdir() if p.is_file()}
    def fail_sync(fd):
        raise OSError('unavailable')
    monkeypatch.setattr(os, 'fsync', fail_sync)
    with pytest.raises(pricebook.PricebookError):
        pricebook.build(spec, record=False)
    assert {p.name: p.read_bytes() for p in folder.iterdir() if p.is_file()} == before


def test_changed_output_during_staging_is_preserved(folder, api, monkeypatch):
    import os
    spec = spec_file(folder, HEAD + WEB % "1")
    out = spec.with_suffix('.xlsx')
    out.write_bytes(b'old')
    original = os.fsync
    def edit(fd):
        out.write_bytes(b'concurrent edit')
        return original(fd)
    monkeypatch.setattr(os, 'fsync', edit)
    with pytest.raises(pricebook.PricebookError):
        pricebook.build(spec, record=False)
    assert out.read_bytes() == b'concurrent edit'
    assert not spec.with_suffix('.md').exists()
    assert not list(folder.glob('.pricebook-*'))


def test_missing_explicit_project_and_external_comparison_are_refused(tmp_path, api):
    spec = spec_file(tmp_path, HEAD + WEB % "1")
    with pytest.raises(pricebook.PricebookError, match='no SCOPE'):
        pricebook.build(spec, project=tmp_path)
    project = tmp_path / 'project'
    project.mkdir()
    (project / 'SCOPE.md').write_text('# scope\n')
    inside = spec_file(project, HEAD + WEB % "1")
    with pytest.raises(pricebook.PricebookError, match='outside'):
        pricebook.build(inside, against=spec)
    assert api.calls == [] and not (project / 'calc').exists()


def test_oversized_existing_outputs_are_preserved(folder, api):
    spec = spec_file(folder, HEAD + WEB % "1")
    for suffix in ('.xlsx', '.md'):
        path = spec.with_suffix(suffix)
        with path.open('wb') as stream:
            stream.truncate(pricebook.MAX_BOOK_BYTES + 1)
        with pytest.raises(pricebook.PricebookError):
            pricebook.build(spec, record=False)
        assert path.stat().st_size == pricebook.MAX_BOOK_BYTES + 1
        path.unlink()
    assert api.calls == []


def test_cli_build_diagnostics_and_notes_are_screened(folder, monkeypatch, capsys):
    from types import SimpleNamespace
    private = fx.CUSTOMER_FORMS[0]
    spec = spec_file(folder, HEAD + WEB % "1")
    for error in (pricebook.SpecError([private]), pricebook.LineRefused({private: private}),
                  pricebook.PricebookError(private)):
        def fail(*args, **kwargs):
            raise error
        monkeypatch.setattr(pricebook, 'build', fail)
        assert pricebook.main(['build', str(spec)]) in (1, 2)
        output = capsys.readouterr()
        assert private not in output.out + output.err
    result = SimpleNamespace(lines=1, monthly=Decimal(1), yearly=Decimal(12), xlsx=Path(private),
                             summary=Path(private), calc_ids=[], comparison=None, notes=[private])
    monkeypatch.setattr(pricebook, 'build', lambda *args, **kwargs: result)
    assert pricebook.main(['build', str(spec)]) == 0
    output = capsys.readouterr()
    assert private not in output.out + output.err
