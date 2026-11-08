"""The monthly cost estimate of the test tenants (B-048, awb/tcp/tenantcost.py): fake listings and fake price
records, never the network. Every id and rate is invented; the names come from tests/fixtures.py."""
from __future__ import annotations

import datetime
import json
from decimal import Decimal

import pytest

from awb import jobs
from awb.tcp import price, sweep, tenantcost, tenants
from awb.tcp.web import contract
from awb.tcp.web import tenant_api
from tests import fixtures
from tests.tcp_fake import record

NOW = datetime.datetime(2026, 11, 7, 6, 0, tzinfo=datetime.timezone.utc)
SID = {k: "%s-0000-4000-8000-%012d" % ("a1b2c3d4", n) for n, k in enumerate(("win", "lnx", "off", "gone"), 1)}
VID = {k: "%s-0000-4000-8000-%012d" % ("e5f6a7b8", n) for n, k in enumerate(("win", "off", "free", "kept"), 1)}
IP = "%s-0000-4000-8000-%012d" % ("c9d0e1f2", 1)


def records() -> list[price.Record]:
    return [price.Record.from_raw(r) for r in (
        record("TEST_S3_LARGE_2_LNX", payg="0.085000"),
        record("TEST_S3_LARGE_2_WIN", os="Windows", payg="0.131000"),
        record("TEST_S3_LARGE_2_SUSE", os="SUSE Linux", payg="0.200000"),
        record("TEST_EVS_SSD", service="evs", flavor="evs.ssd", name="EVS Ultra-high I/O SSD", os="Standard",
               payg="0.120000", unit="GB"),
        record("TEST_EVS_SAS", service="evs", flavor="evs.sas", name="EVS High I/O SAS", os="Standard",
               payg="0.060000", unit="GB"),
        record("TEST_EIP", service="vpc", flavor="eip", name="Elastic IP address", os="Standard", payg="0.010000"),
        record("TEST_BW", service="vpc", flavor="bandwidth", name="Bandwidth", os="Standard", payg="0.030000",
               unit="Mbit/h"),
    )]


class Prices:
    """A stand-in for the price API: one Fetch per region, counted."""

    def __init__(self, recs=None):
        self.recs = records() if recs is None else recs
        self.calls: list[str] = []

    def __call__(self, region):
        self.calls.append(region)
        return price.Fetch(jobs.Listing.of(self.recs), "fake", "2026-11-07 06:30 UTC", "2026-11-06 22:00:00")


def server(rid, status="ACTIVE", os_type="Linux", flavor="s3.large.2", project="tcp-q7m4"):
    return {"id": rid, "name": fixtures.CUSTOMER_FORMS[0] + " app", "created": "2026-10-01T10:00:00Z",
            "status": status, "flavor": {"id": flavor}, "metadata": {"os_type": os_type},
            "tags": ["awb-project=%s" % project] if project else []}


def disk(rid, size=40, vtype="SSD", on=None, project="", status=None):
    return {"id": rid, "name": "disk of " + fixtures.PERSON_FORMS[0], "created_at": "2026-10-01T10:05:00Z",
            "status": status or ("in-use" if on else "available"), "size": size, "volume_type": vtype,
            "attachments": [{"server_id": on, "device": "/dev/vdb"}] if on else [],
            "tags": {"awb-project": project} if project else {}}


class Cloud:
    def __init__(self):
        self.servers = [server(SID["win"], os_type="Windows"), server(SID["lnx"]),
                        server(SID["off"], status="SHUTOFF", flavor="g7.12xlarge.8", project="tcp-ab12")]
        self.volumes = [disk(VID["win"], on=SID["win"]), disk(VID["off"], size=100, vtype="SAS", on=SID["off"]),
                        disk(VID["free"], size=10)]
        self.eips = [{"id": IP, "create_time": "2026-10-02 08:00:00", "status": "DOWN", "bandwidth_size": 5}]
        self.unreadable: set[str] = set()

    def lister(self, t, region):
        def call(service, path, key, paging):
            if key in self.unreadable:
                return jobs.Listing.unknown("HTTP 403")
            rows = {"servers": self.servers, "volumes": self.volumes, "publicips": self.eips}[key]
            return jobs.Listing.of(rows)
        return call


@pytest.fixture
def tenant(home):
    return tenants.add(home, "test-1", "file:/nowhere/keys", ["eu-de"], today="2026-10-01")


def handle(home, rid, alias="test-1"):
    return sweep.Handles(tenants.folder(home, alias) / tenants.HANDLES).by_id[rid]


def snap(home, tenant, cloud=None):
    return tenants.snapshot(home, tenant, (cloud or Cloud()).lister, now=NOW)[0]


def item(cost, home, rid, alias="test-1"):
    h = handle(home, rid, alias)
    return next(r for r in cost["items"] if r["tenant"] == alias and r["handle"] == h)


def test_the_snapshot_keeps_the_os_family_and_the_server_of_a_disk(home, tenant):
    s = snap(home, tenant)
    by = {i["handle"]: i for i in s["items"]}
    assert by[handle(home, SID["win"])]["os"] == "windows" and by[handle(home, SID["lnx"])]["os"] == "linux"
    assert by[handle(home, VID["win"])]["attached"] == handle(home, SID["win"])
    assert by[handle(home, VID["free"])]["attached"] == ""
    odd = Cloud()
    odd.servers = [dict(server(SID["gone"]), metadata={"os_type": "Plan9"})]
    s = tenants.snapshot(home, tenant, odd.lister, now=NOW + datetime.timedelta(days=1))[0]
    assert [i["os"] for i in s["items"] if i["kind"] == "ecs"] == ["unknown"]


def test_running_servers_take_the_record_of_their_os_and_a_stopped_one_counts_only_its_disk(home, tenant):
    snap(home, tenant)
    prices = Prices()
    cost = tenantcost.estimate(home, fetcher=prices, now=NOW)
    assert prices.calls == ["eu-de"], "one price fetch per region"
    win, lnx, off = (item(cost, home, SID[k]) for k in ("win", "lnx", "off"))
    assert win["record"] == "TEST_S3_LARGE_2_WIN" and win["monthly"] == Decimal("0.131000") * 730
    assert lnx["record"] == "TEST_S3_LARGE_2_LNX" and lnx["monthly"] == Decimal("0.085000") * 730
    assert off["status"] == "stopped" and off["monthly"] == 0
    assert any("KB-VTTM" in r for r in off["remarks"]) and any("KB-NAAF" in r for r in off["remarks"])
    off_disk = item(cost, home, VID["off"])
    assert off_disk["status"] == "priced" and off_disk["monthly"] == Decimal("0.060000") * 100
    assert item(cost, home, VID["win"])["monthly"] == Decimal("0.120000") * 40
    eip = item(cost, home, IP)
    assert eip["record"] == "TEST_EIP" and eip["monthly"] == Decimal("0.010000") * 730
    assert cost["total"] == sum(r["monthly"] for r in cost["items"])
    assert cost["no_price"] == 0 and cost["complete"] is True
    assert {g["kind"]: g["amount"] for g in cost["by_kind"]}["ecs"] == win["monthly"] + lnx["monthly"]
    assert cost["top"][0]["handle"] == win["handle"]


def test_a_disk_takes_the_project_of_its_server_and_an_untagged_one_has_none(home, tenant):
    cloud = Cloud()
    cloud.volumes.append(disk(VID["kept"], on=SID["lnx"], project="tcp-zz99"))
    snap(home, tenant, cloud)
    cost = tenantcost.estimate(home, fetcher=Prices(), now=NOW)
    on_win, on_off = item(cost, home, VID["win"]), item(cost, home, VID["off"])
    assert (on_win["project"], on_win["project_source"]) == ("tcp-q7m4", "server")
    assert (on_off["project"], on_off["project_source"]) == ("tcp-ab12", "server")
    tagged = item(cost, home, VID["kept"])
    assert (tagged["project"], tagged["project_source"]) == ("tcp-zz99", "tag")
    free = item(cost, home, VID["free"])
    assert (free["project"], free["project_source"]) == ("no project", "none")
    assert item(cost, home, IP)["project"] == "no project"
    by_project = {g["project"]: g["amount"] for g in cost["by_project"]}
    assert by_project["tcp-ab12"] == Decimal("6.00") and by_project["no project"] == Decimal("1.20") + Decimal("7.30")


def test_an_item_without_a_price_record_is_listed_as_no_price_and_the_total_says_so(home, tenant):
    cloud = Cloud()
    cloud.servers.append(server(SID["gone"], flavor="x9.huge.1"))
    snap(home, tenant, cloud)
    cost = tenantcost.estimate(home, fetcher=Prices(), now=NOW)
    odd = item(cost, home, SID["gone"])
    assert odd["status"] == "no price" and odd["monthly"] is None and "x9.huge.1" in odd["reason"]
    assert cost["no_price"] == 1 and cost["complete"] is False
    assert cost["total"] == sum(r["monthly"] for r in cost["items"] if r["monthly"] is not None)
    assert cost["items"][-1]["handle"] == odd["handle"]
    text = "\n".join(tenantcost.lines(cost))
    assert "without 1 item(s) that have no price" in text and "no price (not counted)" in text
    # a region the price API gave nothing for: every item there has no price, none counts as 0
    cost = tenantcost.estimate(home, fetcher=lambda r: price.Fetch(jobs.Listing.unknown("HTTP 503"), "fake",
                                                                   "2026-11-07 06:30 UTC"), now=NOW)
    priced = [r for r in cost["items"] if r["status"] == "priced"]
    assert priced == [] and cost["total"] == 0 and cost["no_price"] == len(cost["items"]) - 1


def test_the_waste_groups_and_an_item_with_a_note_is_in_none_of_them(home, tenant, capsys):
    cloud = Cloud()
    cloud.volumes.append(disk(VID["kept"], size=20))
    snap(home, tenant, cloud)
    cost = tenantcost.estimate(home, fetcher=Prices(), now=NOW)
    groups = {w["group"]: [i["handle"] for i in w["items"]] for w in cost["waste"]}
    assert groups == {"stopped_server_disks": [handle(home, VID["off"])],
                      "unattached_disks": sorted([handle(home, VID["free"]), handle(home, VID["kept"])],
                                                 key=lambda h: [r["handle"] for r in cost["items"]].index(h)),
                      "unbound_eips": [handle(home, IP)]}
    kept = handle(home, VID["kept"])
    assert tenants.main(["cost", "note", kept, "kept by the owner: an old-world project to move"]) == 0
    assert tenants.main(["cost", "note", "test-1/" + handle(home, IP), "the address of a demo"]) == 0
    assert tenants.main(["cost", "note", "test-1/ecs-99", "nothing"]) == 1
    assert tenants.main(["cost", "note", kept, "ask " + fixtures.PERSON_CODE + " id " + "ab" * 16]) == 1
    notes = (tenantcost.notes_path(home)).read_text(encoding="utf-8").splitlines()
    assert notes[0] == "handle\tnote\tdate" and notes[1].startswith("test-1/%s\tkept by the owner" % kept)
    cost = tenantcost.estimate(home, fetcher=Prices(), now=NOW)
    groups = {w["group"]: [i["handle"] for i in w["items"]] for w in cost["waste"]}
    assert groups == {"stopped_server_disks": [handle(home, VID["off"])],
                      "unattached_disks": [handle(home, VID["free"])], "unbound_eips": []}
    assert item(cost, home, VID["kept"])["note"] == "kept by the owner: an old-world project to move"
    assert item(cost, home, VID["kept"])["monthly"] == Decimal("2.40"), "a noted item is still counted"
    # a note on the server keeps its disks out of the waste too
    off = handle(home, SID["off"])
    assert tenants.main(["cost", "note", off, "kept by the owner"]) == 0
    cost = tenantcost.estimate(home, fetcher=Prices(), now=NOW)
    assert [w["items"] for w in cost["waste"] if w["group"] == "stopped_server_disks"] == [[]]


def test_cost_json_holds_no_name_and_no_id_and_api_tenants_carries_it(home, tenant, monkeypatch, capsys, tmp_path):
    snap(home, tenant)
    monkeypatch.setattr(tenantcost, "_fetch_region", Prices())
    assert tenants.main(["cost", "note", handle(home, SID["win"]), "kept by the owner: an old-world project"]) == 0
    assert tenants.main(["cost"]) == 0
    out = capsys.readouterr().out
    assert "price API eu-de, fetched 2026-11-07 06:30 UTC" in out and "test-1" in out
    path = tenantcost.cost_path(home)
    text = path.read_text(encoding="utf-8")
    for rid in list(SID.values()) + list(VID.values()) + [IP]:
        assert rid not in text and rid not in out
    fixtures.assert_no_fixture_name(text, "cost.json")
    fixtures.assert_no_fixture_name(out, "awb tenant cost")
    cost = json.loads(text)
    assert cost["prices"][0]["fetched_at"] == "2026-11-07 06:30 UTC" and cost["prices"][0]["age_days"] == 1
    assert {r["handle"] for r in cost["items"]} == {i["handle"] for i in tenants.latest(home, "test-1")["items"]}
    assert cost["notes"][0]["note"] == "kept by the owner: an old-world project"
    assert tenants.main(["cost", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["total"] == cost["total"]

    doc = contract.build()
    assert contract.validate(doc, cost, contract.ref("TenantCost")) == []
    inventory = tenant_api.Inventory(tmp_path / "cache.json", lambda: [], lambda t: {})
    answer = inventory.get()
    assert answer["cost"] == cost, "read from the shared tenants folder"
    assert tenant_api.reader_view(answer)["cost"] == cost
    assert contract.validate(doc, answer, contract.ref("TenantInventory")) == []
    path.unlink()
    answer = inventory.get()
    assert answer["cost"] is None and contract.validate(doc, answer, contract.ref("TenantInventory")) == []


def test_an_old_snapshot_without_os_and_attached_still_prices(home, tenant):
    snap(home, tenant)
    path = tenants.snapshots(home, "test-1")[-1]
    old = json.loads(path.read_text(encoding="utf-8"))
    for i in old["items"]:
        i.pop("os", None)
        i.pop("attached", None)
    path.write_text(json.dumps(old), encoding="utf-8")
    cost = tenantcost.estimate(home, fetcher=Prices(), now=NOW)
    win = item(cost, home, SID["win"])
    assert win["os"] == "unknown" and win["record"] == "TEST_S3_LARGE_2_LNX"
    assert any("OS unknown" in r for r in win["remarks"])
    assert item(cost, home, VID["win"])["monthly"] == Decimal("4.80")
    groups = {w["group"]: [i["handle"] for i in w["items"]] for w in cost["waste"]}
    assert groups["unattached_disks"] == [handle(home, VID["free"])]
    assert cost["no_price"] == 0


def test_a_listing_the_read_key_cannot_read_leaves_the_tenant_unknown_and_the_snapshot_runs_the_estimate(
        home, tenant, monkeypatch, capsys):
    cloud = Cloud()
    cloud.unreadable = {"servers", "volumes"}
    monkeypatch.setattr(tenants, "_cloud_lister", cloud.lister)
    monkeypatch.setattr(tenantcost, "_fetch_region", Prices())
    assert tenants.main(["snapshot"]) == 0
    out = capsys.readouterr().out
    assert "2 listing(s) not readable" in out
    assert "cost estimate: 7.30 EUR a month; not complete for test-1" in out
    cost = json.loads(tenantcost.cost_path(home).read_text(encoding="utf-8"))
    assert cost["unknown_tenants"] == ["test-1"] and cost["complete"] is False
    assert cost["tenants"][0]["unreadable"] == [{"region": "eu-de", "kinds": ["ecs", "evs"]}]
    assert tenants.main(["cost"]) == 0
    assert "not readable in eu-de: ecs, evs" in capsys.readouterr().out


def test_the_elastic_ip_takes_the_record_with_the_flavor_eip_beside_the_address_record_of_zero():
    """eu-de lists OTC_ELASTIC_IP (flavor eip) and OTC_EIP_GRAY ("Elastic IP - Address", no flavor, 0): the address
    takes the one with the flavor eip; without it the two stay ambiguous (2026-11-07)."""
    gray = price.Record.from_raw(record("TEST_EIP_GRAY", service="eip", flavor="-", name="Elastic IP - Address",
                                        os="Standard", payg="0.000000"))
    eip = price.Record.from_raw(record("TEST_ELASTIC_IP", service="eip", flavor="eip", name="Elastic IP",
                                       os="Standard", payg="0.004400"))
    rec, why = tenantcost.eip_record([gray, eip])
    assert rec is not None and rec.id == "TEST_ELASTIC_IP"
    gray2 = price.Record.from_raw(record("TEST_EIP_GRAY2", service="eip", flavor="-", name="Elastic IP - Address 2",
                                         os="Standard", payg="0.001000"))
    rec, why = tenantcost.eip_record([gray, gray2])
    assert rec is None and "2 records" in why


def test_the_disk_type_ssd_takes_the_record_of_its_flavor_not_every_ssd_record():
    recs = [price.Record.from_raw(r) for r in (
        record("TEST_BLST_GPSSD", service="evs", flavor="vss.gpssd", name="EVS GP SSD", os="Standard",
               payg="0.104000", unit="GB"),
        record("TEST_BLST_SSD", service="evs", flavor="vss.ssd", name="EVS Ultra-High I/O", os="Standard",
               payg="0.110000", unit="GB"),
        record("TEST_BLST_ESSD", service="evs", flavor="vss.essd", name="EVS Extreme SSD", os="Standard",
               payg="0.308000", unit="GB"),
    )]
    assert tenantcost.disk_record(recs, "SSD")[0].id == "TEST_BLST_SSD"
    assert tenantcost.disk_record(recs, "GPSSD")[0].id == "TEST_BLST_GPSSD"
    assert tenantcost.disk_record(recs, "ESSD")[0].id == "TEST_BLST_ESSD"
    rec, why = tenantcost.disk_record(recs[:1] + recs[2:], "SSD")
    assert rec is None and "2 records of EVS SSD" in why
