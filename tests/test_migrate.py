"""Tests of awb/tcp/migrate.py: the Azure inventory, the mapping to TCP flavors, the estimate and the phase gates.

The price API is replaced by invented records in the shape of the real API (vCpu, ram, productCategory). The machine
names are invented and neutral.
"""
from __future__ import annotations

import json
from decimal import Decimal

import pytest

from awb import calc, cli
from awb.tcp import migrate, price
from tests.test_review import project, run  # noqa: F401 (fixtures)


def raw(flavor, service, vcpu, ram, os_="Open Linux", payg="0.100000", r12="50.000000", category="cat"):
    return {"id": "T_%s_%s" % (flavor.replace(".", "_"), os_[:2].upper()), "productIdParameter": service,
            "opiFlavour": flavor, "productName": "%s %s" % (flavor, os_), "osUnit": os_, "unit": "h",
            "region": "eu-de", "currency": "EUR", "priceAmount": "%s EUR" % payg, "R12": "%s EUR" % r12,
            "R24": "0.000000 EUR", "R36": "0.000000 EUR", "RU12": "0.000000 EUR", "RU24": "0.000000 EUR",
            "RU36": "0.000000 EUR", "vCpu": str(vcpu), "ram": "%s GiB" % ram, "productCategory": category}


RECORDS = [price.Record.from_raw(r) for r in (
    raw("c7n.large.2", "ecsnoc", 2, 4, payg="0.080000"),
    raw("c9.large.2", "ecsnoc", 2, 4, payg="0.070000"),
    raw("c7n.xlarge.2", "ecsnoc", 4, 8, payg="0.160000"),
    raw("c7n.xlarge.4", "ecsnoc", 4, 16, payg="0.200000"),
    raw("c7n.xlarge.4", "ecsnoc", 4, 16, os_="Windows", payg="0.300000", r12="120.000000"),
    raw("c7n.2xlarge.4", "ecsnoc", 8, 32, payg="0.400000"),
    raw("s3.xlarge.4", "ecs", 4, 16, payg="0.150000"),
    raw("s3.large.2", "ecs", 2, 4, payg="0.050000"),
    raw("m7n.xlarge.8", "memo", 4, 32, payg="0.250000"),
    raw("m7n.2xlarge.8", "memo", 8, 64, payg="0.500000"),
    raw("gpu.big", "gpu", 8, 64),
    raw("ebs.not.compute", "evs", 0, 0),
)]


class FakeFetch:
    def __init__(self, records):
        self.records = records

    def source_line(self):
        return "price API, fetched 2026-09-30 06:00 UTC"


CSV = "\n".join([
    "VM name,Size,vCPU,Memory (MB),OS,Count",
    "web-01,Standard_D4s_v5,,16384,Ubuntu 22.04,2",
    "web-02,Standard_D2s_v5,,4096,Windows Server 2022,",
    "db-01,Standard_E4-2ds_v5,,32768,Red Hat Enterprise Linux 9,",
    "gpu-01,Standard_NC4as_T4_v3,,28672,Ubuntu,",
    "old-01,Standard_DS3_v2,,14336,Windows Server 2016,",
    "odd-01,custom-box,6,12288,Debian,",
    "big-01,Standard_D64s_v5,,262144,Ubuntu,",
    "win-01,Standard_D4s_v5,,16384,Windows Server 2019,",
]) + "\n"

MARKDOWN = "\n".join([
    "# Sheet: machines",
    "",
    "| VM name | Size | Memory GB | Operating system |",
    "| --- | --- | --- | --- |",
    "| app-01 | Standard_E8s_v5 | 64 | SUSE Linux Enterprise 15 |",
    "| app-02 | Standard_F4s_v2 | 8 | Ubuntu |",
    "",
    "Some text between tables.",
    "",
    "| Owner | Cost centre |",
    "| --- | --- |",
    "| team-a | 1234 |",
]) + "\n"


@pytest.fixture
def mig(project):
    migrate.init(project, "azure")
    (project / "input").mkdir(exist_ok=True)
    (project / "input" / "inventory.csv").write_text(CSV, encoding="utf-8")
    return project


def classes():
    return migrate.flavors_from_records(RECORDS)


def by_row(rows):
    return {r["row"]: r for r in rows}


# --------------------------------------------------------------------------- the Azure side


@pytest.mark.parametrize("size, family, vcpu, constrained, arm", [
    ("Standard_D4s_v5", "D", 4, None, False),
    ("Standard_E4-2ds_v5", "E", 4, 2, False),
    ("Standard_DC8ads_v5", "DC", 8, None, False),
    ("Standard_NC4as_T4_v3", "NC", 4, None, False),
    ("Standard_M48ds_1_v3", "M", 48, None, False),
    ("Standard_B2ms", "B", 2, None, False),
    ("Standard_D2ps_v5", "D", 2, None, True),
    ("Standard_HB120rs_v3", "HB", 120, None, False),
    ("Standard_FX12mds", "FX", 12, None, False),
    ("Standard_D4_v3", "D", 4, None, False),
])
def test_azure_size_names_follow_the_naming_convention(size, family, vcpu, constrained, arm):
    p = migrate.parse_azure_size(size)
    assert (p["family"], p["vcpu"], p["constrained"], p["arm"]) == (family, vcpu, constrained, arm)


@pytest.mark.parametrize("size", ["Standard_DS3_v2", "Standard_D2", "Standard_D13_v2"])
def test_d_sizes_before_v3_give_no_vcpu_count(size):
    assert migrate.parse_azure_size(size)["vcpu"] is None


@pytest.mark.parametrize("size", ["custom-box", "", "Standard_", "Standard_Q4s_v5"])
def test_other_names_do_not_parse(size):
    assert migrate.parse_azure_size(size) is None


# --------------------------------------------------------------------------- the inventory


def test_the_inventory_from_csv(mig):
    doc = migrate.inventory(mig, mig / "input" / "inventory.csv")
    rows = by_row(doc["rows"])
    assert len(rows) == 8
    assert rows["R-1"]["vcpu"] == "4" and rows["R-1"]["vcpu_source"] == "size name"
    assert rows["R-1"]["memory_gib"] == "16" and rows["R-1"]["count"] == "2"
    assert rows["R-3"]["vcpu"] == "2" and "constrained" in rows["R-3"]["notes"][0]
    assert rows["R-5"]["vcpu"] == "" and "numbered by size" in rows["R-5"]["notes"][0]
    assert rows["R-6"]["family"] == "" and rows["R-6"]["vcpu"] == "6" and rows["R-6"]["vcpu_source"] == "inventory"
    assert json.loads((mig / "migration" / "inventory.json").read_text())["rows"][0]["row"] == "R-1"


def test_the_inventory_from_a_sanitised_copy_takes_the_first_table_that_fits(mig):
    (mig / "input" / "copy.md").write_text(MARKDOWN, encoding="utf-8")
    doc = migrate.inventory(mig, mig / "input" / "copy.md")
    rows = by_row(doc["rows"])
    assert doc["table"] == 1 and len(rows) == 2
    assert (rows["R-1"]["family"], rows["R-1"]["vcpu"], rows["R-1"]["memory_gib"]) == ("E", "8", "64")
    assert rows["R-2"]["os"] == "Ubuntu"


def test_an_inventory_without_a_fitting_table_is_refused_without_its_values(mig):
    (mig / "input" / "other.md").write_text("| Owner | Cost centre |\n| --- | --- |\n| team-secret | 1234 |\n",
                                            encoding="utf-8")
    with pytest.raises(migrate.MigrateError) as err:
        migrate.inventory(mig, mig / "input" / "other.md")
    assert "team-secret" not in str(err.value)


# --------------------------------------------------------------------------- the mapping


def test_the_target_classes_come_from_the_price_records():
    c = classes()
    assert [f.name for f in c["general-dedicated"]][:2] == ["c7n.large.2", "c9.large.2"]
    assert all(f.service == "memo" for f in c["memory"])
    x = next(f for f in c["general-dedicated"] if f.name == "c7n.xlarge.4")
    assert x.linux_payg == Decimal("0.200000")           # the open Linux record, not the Windows one
    assert not any(f.name == "ebs.not.compute" for fs in c.values() for f in fs)


def test_the_nearest_flavor_that_is_not_smaller(mig):
    migrate.inventory(mig, mig / "input" / "inventory.csv")
    rows = by_row(migrate.map_rows(migrate.load_inventory(mig), classes()))
    assert (rows["R-1"]["flavor"], rows["R-1"]["status"]) == ("c7n.xlarge.4", "ok")
    assert rows["R-1"]["alternative"] == "s3.xlarge.4"
    assert rows["R-2"]["flavor"] == "c9.large.2"          # same shape, the lower open Linux price wins
    assert (rows["R-3"]["tcp_class"], rows["R-3"]["flavor"], rows["R-3"]["status"]) == ("memory", "m7n.xlarge.8",
                                                                                         "flag")
    assert rows["R-4"]["status"] == "manual" and rows["R-4"]["flavor"] == ""
    assert rows["R-5"]["status"] == "needs-input"
    assert rows["R-6"]["status"] == "needs-input"
    assert rows["R-7"]["status"] == "no-fit"


def test_no_mapped_flavor_is_ever_smaller_than_its_source():
    inv = {"rows": []}
    n = 0
    for fam in ("D", "E", "F", "A", "B"):
        for v in (1, 2, 3, 4, 5, 8):
            for mem in ("1", "3.5", "4", "8", "15.9", "16", "31", "32", "64"):
                n += 1
                inv["rows"].append({"row": "R-%d" % n, "family": fam, "vcpu": str(v), "memory_gib": mem,
                                    "count": "1", "notes": []})
    for r in migrate.map_rows(inv, classes()):
        if r["flavor"]:
            src = next(x for x in inv["rows"] if x["row"] == r["row"])
            assert Decimal(r["flavor_vcpu"]) >= Decimal(src["vcpu"])
            assert Decimal(r["flavor_ram_gib"]) >= Decimal(src["memory_gib"])


# --------------------------------------------------------------------------- the estimate


def _mapped(mig):
    migrate.inventory(mig, mig / "input" / "inventory.csv")
    rows = migrate.map_rows(migrate.load_inventory(mig), classes())
    migrate.write_mapping(mig, rows, "eu-de", "price API, fetched 2026-09-30 06:00 UTC")


def test_the_estimate_prices_by_os_and_records_its_total(mig):
    _mapped(mig)
    res = migrate.estimate(mig, lambda region: FakeFetch(RECORDS), "PAYG")
    lines = by_row(res["rows"])
    assert lines["R-1"]["line_total"] == "288.00"        # 0.20 x 720 h x 2
    assert lines["R-8"]["os_tier"] == "windows" and lines["R-8"]["line_total"] == "216.00"
    assert lines["R-3"]["status"] == "no price for this OS and term"     # no Red Hat record in the fake API
    assert set(res["left_out"]) >= {"R-3", "R-4", "R-5", "R-6", "R-7"}
    rec = calc.find(mig, res["calc"])
    assert rec is not None and rec.result == res["total"]
    assert calc.evaluate(rec.expression) == Decimal(res["total"])
    assert "machines only" in (mig / "migration" / "estimate-PAYG.tsv").read_text().splitlines()[0]


def test_a_reserved_term_is_a_monthly_rate(mig):
    _mapped(mig)
    res = migrate.estimate(mig, lambda region: FakeFetch(RECORDS), "R12")
    assert by_row(res["rows"])["R-8"]["line_total"] == "120.00"


@pytest.mark.parametrize("text, tier", [("Windows Server 2019", "windows"), ("RHEL 9", "red hat"),
                                        ("SLES 15 for SAP", "suse for sap"), ("SUSE Linux", "suse linux"),
                                        ("Oracle Linux 8", "oracle linux"), ("Ubuntu 22.04", "open linux"),
                                        ("", "open linux")])
def test_the_os_tier(text, tier):
    assert migrate.os_tier(text) == tier


# --------------------------------------------------------------------------- the phases and their gates


def test_the_phases_run_in_order_and_the_gates_hold(mig, capsys):
    code, out, _ = run(["migrate", "--project", str(mig), "next"], capsys)
    assert code == 0 and "phase: discover" in out and "awb:migration-worker-files" in out
    migrate.inventory(mig, mig / "input" / "inventory.csv")
    assert migrate.done(mig, "map") == ["the phase map comes after discover, which is not done"]
    assert "migration/discover.md is missing or empty" in migrate.done(mig, "discover")
    (mig / "migration" / "discover.md").write_text("R-3 is constrained. R-5 needs its vCPU.\n", encoding="utf-8")
    problems = migrate.done(mig, "discover")
    assert "discover.md does not say what happens with R-6" in problems
    assert not any("R-1" in p for p in problems)
    (mig / "migration" / "discover.md").write_text("R-3, R-4, R-5, R-6 and R-7 need a word.\n", encoding="utf-8")
    assert migrate.done(mig, "discover") == []

    _mapped(mig)
    (mig / "migration" / "map-notes.md").write_text("R-3 R-4 R-5 R-6\n", encoding="utf-8")
    assert migrate.done(mig, "map") == ["map-notes.md does not decide R-7 (no-fit)"]
    (mig / "migration" / "map-notes.md").write_text("R-3 R-4 R-5 R-6 R-7\n", encoding="utf-8")
    assert migrate.done(mig, "map") == []
    code, out, _ = run(["migrate", "--project", str(mig), "next"], capsys)
    assert "phase: estimate" in out and "awb:migration-worker-shell" in out

    res = migrate.estimate(mig, lambda region: FakeFetch(RECORDS), "PAYG")
    (mig / "migration" / "estimate-notes.md").write_text("Total %s EUR (calc:K-99).\n" % res["total"],
                                                         encoding="utf-8")
    assert migrate.done(mig, "estimate") == ["estimate-notes.md names K-99, which is not recorded"]
    (mig / "migration" / "estimate-notes.md").write_text("Total %s EUR (calc:%s).\n" % (res["total"], res["calc"]),
                                                         encoding="utf-8")
    assert migrate.done(mig, "estimate") == []
    assert migrate.load_state(mig)["phases"][2]["state"] == "done"


def test_a_changed_artifact_turns_its_phase_and_the_later_ones_stale(mig, capsys):
    migrate.inventory(mig, mig / "input" / "inventory.csv")
    (mig / "migration" / "discover.md").write_text("R-3 R-4 R-5 R-6 R-7\n", encoding="utf-8")
    assert migrate.done(mig, "discover") == []
    _mapped(mig)
    (mig / "migration" / "map-notes.md").write_text("R-3 R-4 R-5 R-6 R-7\n", encoding="utf-8")
    assert migrate.done(mig, "map") == []
    (mig / "migration" / "discover.md").write_text("R-3 R-4 R-5 R-6 R-7, and one more line\n", encoding="utf-8")
    phases = migrate.load_state(mig)["phases"]
    assert [p["state"] for p in phases[:3]] == ["stale", "stale", "pending"]
    code, out, _ = run(["migrate", "--project", str(mig), "status"], capsys)
    assert "next: discover" in out


def test_the_map_gate_catches_a_flavor_edited_by_hand(mig):
    migrate.inventory(mig, mig / "input" / "inventory.csv")
    (mig / "migration" / "discover.md").write_text("R-3 R-4 R-5 R-6 R-7\n", encoding="utf-8")
    assert migrate.done(mig, "discover") == []
    _mapped(mig)
    path = mig / "migration" / "mapping.tsv"
    path.write_text(path.read_text().replace("c7n.xlarge.4\t4\t16", "c7n.large.2\t2\t4", 1), encoding="utf-8")
    (mig / "migration" / "map-notes.md").write_text("R-3 R-4 R-5 R-6 R-7\n", encoding="utf-8")
    assert "R-1: the flavor is smaller than the source" in migrate.done(mig, "map")


def test_init_twice_and_outside_a_project_are_refused(mig, tmp_path, capsys):
    code, _, err = run(["migrate", "--project", str(mig), "init", "--from", "azure"], capsys)
    assert code == 2 and "already started" in err
    code, _, err = run(["migrate", "--project", str(tmp_path), "init", "--from", "azure"], capsys)
    assert code == 2 and "no SCOPE.md" in err


def test_the_command_is_delegated():
    assert cli.DELEGATED["migrate"][0] == "tcp.migrate"


@pytest.mark.parametrize("tier, record_os, fits", [("red hat", "RedHat Linux", True), ("suse linux", "SUSE for SAP", False),
                                                   ("suse for sap", "SUSE for SAP", True), ("windows", "Windows", True),
                                                   ("open linux", "Oracle Linux", False)])
def test_the_os_tier_meets_the_records_as_the_api_writes_them(tier, record_os, fits):
    assert migrate._os_matches(tier, record_os) is fits
