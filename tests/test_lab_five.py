"""The five lab recipes of the n13 task (eip-bandwidth-adjust, evs-snapshot-restore, nat-snat-egress,
obs-lifecycle-rule, dns-private-zone) run against fake answers: each creates what it names, tears every row down and
proves it gone. The stand-in below extends the key service of tests/lab_fake.py with the paths of these recipes.
Every id and address here is invented.
"""
from __future__ import annotations

import itertools
import json
import re

import pytest

from awb import projects
from awb.tcp import lab
from tests.lab_fake import FakeKeyService
from tests.test_lab import PROJECT, REPO_RECIPES, TENANT, evidence, project, rows, run, writes  # noqa: F401

FIVE = ("eip-bandwidth-adjust", "evs-snapshot-restore", "nat-snat-egress", "obs-lifecycle-rule", "dns-private-zone")
P = "/v1/{project_id}/"

# (method-free path pattern, kind, key of the item in an answer, key of the list)
ROUTES = (
    (re.compile(r"^/v1/\{project_id\}/vpcs$"), "vpc", "vpc", "vpcs"),
    (re.compile(r"^/v1/\{project_id\}/subnets$"), "subnet", "subnet", "subnets"),
    (re.compile(r"^/v1/\{project_id\}/publicips$"), "eip", "publicip", "publicips"),
    (re.compile(r"^/v2/\{project_id\}/volumes$"), "volume", "volume", "volumes"),
    (re.compile(r"^/v2/\{project_id\}/cloudvolumes/detail$"), "volume", "volume", "volumes"),
    (re.compile(r"^/v3/\{project_id\}/snapshots$"), "snapshot", "snapshot", "snapshots"),
    (re.compile(r"^/v2\.0/nat_gateways$"), "gateway", "nat_gateway", "nat_gateways"),
    (re.compile(r"^/v2\.0/snat_rules$"), "rule", "snat_rule", "snat_rules"),
    (re.compile(r"^/v2/zones$"), "zone", None, "zones"),
    (re.compile(r"^/v2/zones/(?P<zone>[a-z0-9-]+)/recordsets$"), "record", None, "recordsets"),
)
ITEM_PATHS = {
    "vpc": "/v1/{project_id}/vpcs/", "subnet": "/v1/{project_id}/subnets/", "eip": "/v1/{project_id}/publicips/",
    "volume": "/v2/{project_id}/volumes/", "snapshot": "/v3/{project_id}/snapshots/",
    "gateway": "/v2.0/nat_gateways/", "rule": "/v2.0/snat_rules/", "zone": "/v2/zones/",
}


class FiveCloud(FakeKeyService):
    """The invented cloud of the five recipes, kept in memory. `lost` (kind: True) makes the item and then drops the
    answer of its create; `fail_check` (a name) leaves the data of a read wrong on purpose."""

    def __init__(self):
        super().__init__()
        self.stuff: dict[str, dict] = {}
        self.lost: dict[str, bool] = {}
        self.buckets: dict[str, dict] = {}
        self.ids = itertools.count(1)
        self.order: list[str] = []
        self.keep_size = False

    def live_of(self, kind: str) -> list[str]:
        return [i for i, it in self.stuff.items() if it["kind"] == kind and not it["deleted"]]

    def all_live(self) -> list[str]:
        return [i for i, it in self.stuff.items() if not it["deleted"]] + [b for b, v in self.buckets.items() if v]

    @staticmethod
    def ok(status, data=None, text=""):
        return {"ok": True, "status": status, "data": data, "text": text}

    def new(self, kind, body, **more):
        rid = "%08x-2222-4333-8444-%012x" % (0x2b3c4d5e + next(self.ids), 0xfeed00 + len(self.stuff))
        self.stuff[rid] = dict({"kind": kind, "deleted": False, "body": body, "tags": None}, **more)
        self.order.append(kind)
        return rid

    def view(self, rid, it):
        k = it["kind"]
        b = it["body"]
        if k == "subnet":
            return {"id": rid, "status": "ACTIVE", "neutron_network_id": "net-" + rid[:8]}
        if k == "eip":
            return {"id": rid, "bandwidth_id": it["bw"], "public_ip_address": "203.0.113.7"}
        if k == "volume":
            return {"id": rid, "status": "available", "size": b["size"]}
        if k == "snapshot":
            return {"id": rid, "status": "available"}
        if k == "gateway":
            return {"id": rid, "status": "ACTIVE"}
        if k == "rule":
            return {"id": rid, "status": "ACTIVE", "floating_ip_id": b["floating_ip_id"], "network_id": b["network_id"]}
        if k == "zone":
            return {"id": rid, "status": "ACTIVE", "name": b["name"]}
        if k == "record":
            return {"id": rid, "status": "ACTIVE", "name": b["name"], "type": b["type"], "records": b["records"]}
        return {"id": rid}

    def __call__(self, req):
        if req.get("op") != "call":
            return super().__call__(req)
        m, path, body, query = req["method"], req["path"], req.get("body"), req.get("query") or {}
        if path == "/v2/{project_id}/types":
            return super().__call__(req)
        self.calls.append(req)
        if self.on_call is not None:
            self.on_call(req)
        if self.refuse:
            return {"ok": False, "error": self.refuse}
        if req["service"] == "obs":
            return self.obs(m, path, query, body)
        if m == "POST" and path.endswith("/tags/action"):
            rid = path.split("/")[-3]
            if rid in self.stuff:
                self.stuff[rid]["tags"] = body["tags"]
            return self.ok(204)
        for rx, kind, one, many in ROUTES:
            hit = rx.match(path)
            if hit and m == "POST":
                return self.create(kind, one, hit, body)
            if hit and m == "GET":
                return self.listing(kind, many, hit, query)
        if m == "GET" and path == "/v1/{project_id}/bandwidths":
            items = [{"id": it["bw"], "name": it["body"]["name"], "publicip_info": [{"publicip_id": i}]}
                     for i, it in self.stuff.items() if it["kind"] == "eip" and not it["deleted"]]
            return self.ok(200, {"bandwidths": items})
        bw = re.match(r"^/v1/\{project_id\}/bandwidths/(?P<id>[a-z0-9-]+)$", path)
        if bw:
            owner = next((it for it in self.stuff.values() if it["kind"] == "eip" and it["bw"] == bw["id"]
                          and not it["deleted"]), None)
            if owner is None:
                return self.ok(404, {"error_msg": "not found"})
            if m == "PUT" and not self.keep_size:
                owner["size"] = body["bandwidth"]["size"]
                self.order.append("raise")
            return self.ok(200, {"bandwidth": {"id": bw["id"], "size": owner["size"]}})
        sub = re.match(r"^/v1/\{project_id\}/vpcs/(?P<vpc>[a-z0-9-]+)/subnets/(?P<id>[a-z0-9-]+)$", path)
        if sub and m == "DELETE":
            return self.remove(sub["id"])
        rec = re.match(r"^/v2/zones/(?P<zone>[a-z0-9-]+)/recordsets/(?P<id>[a-z0-9-]+)$", path)
        if rec:
            return self.one(m, rec["id"], "record")
        for kind, prefix in ITEM_PATHS.items():
            if path.startswith(prefix) and "/" not in path[len(prefix):]:
                return self.one(m, path[len(prefix):], kind)
        return self.ok(404, {"error_msg": "no such path"})

    def create(self, kind, one, hit, body):
        if kind in self.lost and self.lost[kind] is True:
            made = True
        else:
            made = False
        data = (body or {}).get(one) if one else dict(body or {})
        data = dict(data or {})
        if kind == "eip":
            data["name"] = body["bandwidth"]["name"]
            rid = self.new(kind, data, bw="bw-%08x" % next(self.ids), size=body["bandwidth"]["size"])
        elif kind == "volume" and data.get("snapshot_id") and data["snapshot_id"] not in self.live_of("snapshot"):
            return self.ok(404, {"error_msg": "no such snapshot"})
        elif kind == "rule":
            if data["nat_gateway_id"] not in self.live_of("gateway"):
                return self.ok(404, {"error_msg": "no such gateway"})
            rid = self.new(kind, data)
        elif kind == "gateway" and data["router_id"] not in self.live_of("vpc"):
            return self.ok(404, {"error_msg": "no such vpc"})
        elif kind == "zone" and data["router"]["router_id"] not in self.live_of("vpc"):
            return self.ok(404, {"error_msg": "no such vpc"})
        elif kind == "record" and hit["zone"] not in self.live_of("zone"):
            return self.ok(404, {"error_msg": "no such zone"})
        else:
            rid = self.new(kind, data)
        if kind == "record":
            self.stuff[rid]["zone"] = hit["zone"]
        if kind == "volume" and data.get("tags") is not None:
            self.stuff[rid]["tags"] = data["tags"]
        if made:
            raise ConnectionResetError("no answer")
        if kind in ("zone", "record"):
            return self.ok(202, {"id": rid})
        extra = {"public_ip_address": "203.0.113.7"} if kind == "eip" else {}
        return self.ok(200, {one: dict({"id": rid, "status": "BUILD"}, **extra)})

    def listing(self, kind, many, hit, query):
        items = []
        for i, it in self.stuff.items():
            if it["kind"] != kind or it["deleted"] or (kind == "record" and it["zone"] != hit["zone"]):
                continue
            b = it["body"]
            items.append({"id": i, "name": b.get("name") if kind != "eip" else b["name"],
                          "description": b.get("description", "")})
        return self.ok(200, {many: items})

    def remove(self, rid):
        it = self.stuff.get(rid)
        if it is None or it["deleted"]:
            return self.ok(404, {"error_msg": "not found"})
        if it["kind"] == "gateway" and any(r["body"]["nat_gateway_id"] == rid for r in self.stuff.values()
                                            if r["kind"] == "rule" and not r["deleted"]):
            return self.ok(409, {"error_msg": "gateway still has rules"})
        if it["kind"] == "vpc" and any(s["kind"] in ("subnet", "zone") and not s["deleted"]
                                       for s in self.stuff.values()):
            return self.ok(409, {"error_code": "VPC.0104"})
        if it["kind"] == "zone":
            for r in self.stuff.values():
                if r["kind"] == "record" and r["zone"] == rid:
                    r["deleted"] = True
        it["deleted"] = True
        self.order.append("delete " + it["kind"])
        return self.ok(204)

    def one(self, m, rid, kind):
        it = self.stuff.get(rid)
        if it is None or it["deleted"] or it["kind"] != kind:
            return self.ok(404, {"error_msg": "not found"})
        if m == "DELETE":
            return self.remove(rid)
        if kind in ("zone", "record"):
            return self.ok(200, self.view(rid, it))
        return self.ok(200, {{"eip": "publicip", "volume": "volume", "snapshot": "snapshot", "gateway": "nat_gateway",
                              "rule": "snat_rule"}.get(kind, kind): self.view(rid, it)})

    def obs(self, m, path, query, body):
        name = path[1:]
        if "lifecycle" in query:
            b = self.buckets.get(name)
            if b is None:
                return self.ok(404, None, "<Error><Code>NoSuchBucket</Code></Error>")
            if m == "PUT":
                b["lifecycle"] = body
                self.order.append("lifecycle")
                return self.ok(200)
            if not b.get("lifecycle"):
                return self.ok(404, None, "<Error><Code>NoSuchLifecycleConfiguration</Code></Error>")
            return self.ok(200, None, "<LifecycleConfiguration><Rule><Expiration><Days>1</Days></Expiration>"
                                      "</Rule></LifecycleConfiguration>")
        if m == "PUT":
            self.buckets[name] = {}
            self.order.append("bucket")
            return self.ok(200)
        if name not in self.buckets:
            return self.ok(404, None, "<Error><Code>NoSuchBucket</Code></Error>")
        if m == "DELETE":
            del self.buckets[name]
            self.order.append("delete bucket")
            return self.ok(204)
        return self.ok(200)


@pytest.fixture
def five(project, monkeypatch):
    monkeypatch.setenv(lab.RECIPES_ENV, str(REPO_RECIPES))
    return project


def go(name, fake, params=()):
    code, lines, errors = run(fake, recipe=name, params=params)
    return code, lines, errors


def green(name, folder, fake, params=()):
    code, lines, errors = go(name, fake, params)
    assert code == lab.EXIT_OK, (lines, errors)
    assert fake.all_live() == []
    rs = rows(folder)
    assert rs and all(r["state"] == "deleted" for r in rs)
    assert projects.live_resources(folder) == 0
    text = evidence(folder)
    assert "- result: passed" in text and "every resource deleted and proven gone" in text
    return lines, text, rs


# --------------------------------------------------------------------------- the files themselves


def test_the_five_recipes_are_valid_fit_the_guard_and_the_readme_lists_them():
    tags = {line.strip() for line in (REPO_RECIPES.parent / "rules" / "tags.txt").read_text().splitlines()
            if line.strip() and not line.startswith("#")}
    readme = (REPO_RECIPES / "README.md").read_text(encoding="utf-8")
    for name in FIVE:
        r = lab.load(REPO_RECIPES / ("%s.json" % name))
        assert r["name"] == name and set(r["kb"]["tags"]) <= tags
        assert lab.confirm_notes(r), "a recipe of shapes not proven live says so"
        creates = [s for s in r["steps"] if s["do"] == "create"]
        assert len(creates) <= r["guard"]["resources"]
        assert set(r["teardown"]) == {s["id"] for s in creates}, name
        assert all(t["gone"] for t in r["teardown"].values())
        assert r["budget"]["minutes"] + r["budget"]["teardown_minutes"] <= r["guard"]["lifetime_minutes"]
        assert "`%s`" % name in readme
        for s in creates:
            assert "${name}" in s["name"] or "${project}" in s["name"]


def test_the_defaults_of_the_five_recipes_are_the_sizes_of_the_task():
    def body(name, step):
        r = lab.load(REPO_RECIPES / ("%s.json" % name))
        return lab.expand(next(s for s in r["steps"] if s["id"] == step)["call"]["body"],
                          lab.params_of(r, []) | dict(r.get("vars") or {}), loose=True)
    eip = body("eip-bandwidth-adjust", "eip")
    assert eip["bandwidth"]["size"] == 5 and body("eip-bandwidth-adjust", "raise")["bandwidth"]["size"] == 10
    disk = body("evs-snapshot-restore", "disk")["volume"]
    assert disk["size"] == 10 and disk["volume_type"] == "SAS"
    life = body("obs-lifecycle-rule", "rule")
    assert life["LifecycleConfiguration"]["Rule"][0]["Expiration"]["Days"] == 1


@pytest.mark.parametrize("name, given", [("eip-bandwidth-adjust", "start_mbit=6"),
                                         ("eip-bandwidth-adjust", "target_mbit=11"),
                                         ("obs-lifecycle-rule", "days=30"),
                                         ("dns-private-zone", "address=10.0.0.1"),
                                         ("nat-snat-egress", "region=us-east")])
def test_a_value_outside_the_pattern_is_refused_before_any_call(five, name, given):
    fake = FiveCloud()
    code, lines, errors = go(name, fake, [given])
    assert code != lab.EXIT_OK and fake.calls == [] and "cost guard" in " ".join(errors)


def test_the_dry_run_of_each_recipe_makes_no_call_and_shows_every_create():
    for name in FIVE:
        r = lab.load(REPO_RECIPES / ("%s.json" % name))
        lines = lab.dry_run_lines(r, lab.params_of(r, []))
        assert sum("before the call" in line for line in lines) == len([s for s in r["steps"] if s["do"] == "create"])


# --------------------------------------------------------------------------- eip-bandwidth-adjust


def test_the_eip_recipe_raises_the_bandwidth_reads_it_back_and_releases_the_eip(five):
    fake = FiveCloud()
    lines, text, rs = green("eip-bandwidth-adjust", five, fake)
    eip = next(it for it in fake.stuff.values() if it["kind"] == "eip")
    assert eip["body"]["name"].startswith(PROJECT + "-lab-") and eip["size"] == 10
    assert [t["key"] for t in eip["tags"]] == ["awb-project", "awb-expiry"]
    assert fake.order == ["eip", "raise", "delete eip"]
    assert [r["handle"].split("-")[0] for r in rs] == ["eip"]
    assert "| raised | the bandwidth reads the raised size | bandwidth.size = 10 | 10 | passed |" in text


def test_a_bandwidth_that_stays_at_five_fails_the_check_and_the_eip_is_still_released(five):
    fake = FiveCloud()

    fake.keep_size = True
    code, lines, errors = go("eip-bandwidth-adjust", fake)
    assert code == lab.EXIT_CHECK
    assert fake.all_live() == [] and [r["state"] for r in rows(five)] == ["deleted"]
    assert "| raised |" in evidence(five) and "failed |" in evidence(five)


# --------------------------------------------------------------------------- evs-snapshot-restore


def test_the_evs_recipe_makes_a_disk_a_snapshot_and_a_disk_from_it_and_tears_all_three_down(five):
    fake = FiveCloud()
    lines, text, rs = green("evs-snapshot-restore", five, fake)
    kinds = [fake.stuff[r["id"]]["kind"] for r in rs]
    assert kinds == ["volume", "snapshot", "volume"]
    first, snap, second = (fake.stuff[r["id"]] for r in rs)
    assert first["body"]["volume_type"] == "SAS" and first["body"]["size"] == 10
    assert first["tags"] == {"awb-project": PROJECT, "awb-expiry": first["tags"]["awb-expiry"]}
    assert second["body"]["snapshot_id"] == rs[1]["id"] and second["body"]["name"].endswith("-r")
    assert snap["body"]["volume_id"] == rs[0]["id"] and snap["body"]["name"].startswith(PROJECT + "-lab-")
    assert fake.order[-3:] == ["delete volume", "delete snapshot", "delete volume"]
    assert "| same-size |" in text and "passed |" in text
    # the disk type is read before the first create
    assert fake.calls[1]["path"] == "/v2/{project_id}/types"


def test_a_sold_out_disk_type_stops_the_evs_recipe_before_any_create(five):
    fake = FiveCloud()
    fake.volume_types[0]["extra_specs"]["os-vendor-extended:sold_out_availability_zones"] = "eu-de-01"
    code, lines, errors = go("evs-snapshot-restore", fake)
    assert code != lab.EXIT_OK and fake.stuff == {} and rows(five) == []


# --------------------------------------------------------------------------- nat-snat-egress


def test_the_nat_recipe_builds_the_rule_reads_it_and_tears_down_in_reverse(five):
    fake = FiveCloud()
    lines, text, rs = green("nat-snat-egress", five, fake)
    assert [fake.stuff[r["id"]]["kind"] for r in rs] == ["vpc", "subnet", "eip", "gateway", "rule"]
    vpc, subnet, eip, gw, rule = (fake.stuff[r["id"]] for r in rs)
    assert gw["body"]["router_id"] == rs[0]["id"] and gw["body"]["internal_network_id"].startswith("net-")
    assert rule["body"]["nat_gateway_id"] == rs[3]["id"] and rule["body"]["floating_ip_id"] == rs[2]["id"]
    assert rule["body"]["description"].startswith(PROJECT + "-lab-")
    assert eip["size"] == 1
    assert fake.order[-5:] == ["delete rule", "delete gateway", "delete eip", "delete subnet", "delete vpc"]
    assert "| rule-eip |" in text and "| rule-net |" in text


def test_a_lost_create_answer_on_the_nat_gateway_is_found_by_name_and_torn_down(five):
    fake = FiveCloud()
    fake.lost = {"gateway": True}
    seen = []

    def watch(req):
        if req["method"] == "POST" and req["path"] == "/v2.0/nat_gateways":
            seen.extend(r for r in rows(five) if r["handle"].startswith("nat"))
    fake.on_call = watch
    lines, text, rs = green("nat-snat-egress", five, fake)
    assert len(seen) == 1 and seen[0]["state"] == "creating" and seen[0]["id"] == "unknown"
    assert "named %s-lab-" % PROJECT in seen[0]["note"]
    assert len(fake.live_of("gateway")) == 0 and [it["kind"] for it in fake.stuff.values()].count("gateway") == 1
    finds = [r for r in fake.calls if r["method"] == "GET" and r["path"] == "/v2.0/nat_gateways"]
    assert finds and finds[0]["query"]["name"].startswith(PROJECT + "-lab-")
    gw_row = next(r for r in rs if r["handle"].startswith("nat"))
    assert gw_row["handle"] == seen[0]["handle"] and gw_row["id"] != "unknown"
    assert "| 9 | run | gw find | GET | nat | `/v2.0/nat_gateways` | 200 | 1 | named like the resource = yes |" in text


def test_a_lost_create_answer_that_made_nothing_is_not_created_and_the_rest_is_torn_down(five):
    fake = FiveCloud()
    real = fake.create

    def nothing(kind, one, hit, body):
        if kind == "gateway":
            raise ConnectionResetError("no answer")
        return real(kind, one, hit, body)
    fake.create = nothing
    code, lines, errors = go("nat-snat-egress", fake)
    assert code != lab.EXIT_OK and fake.all_live() == []
    states = {r["handle"].split("-")[0]: r["state"] for r in rows(five)}
    assert states["nat"] == "not created" and states["vpc"] == "deleted" and states["eip"] == "deleted"


# --------------------------------------------------------------------------- obs-lifecycle-rule


def test_the_obs_recipe_makes_a_bucket_a_rule_reads_it_and_removes_the_bucket(five):
    fake = FiveCloud()
    lines, text, rs = green("obs-lifecycle-rule", five, fake)
    assert fake.order == ["bucket", "lifecycle", "delete bucket"]
    assert [r["state"] for r in rs] == ["deleted"] and rs[0]["id"].startswith(PROJECT + "-lab-")
    obs_calls = [r for r in fake.calls if r["service"] == "obs"]
    assert all(r["path"].startswith("/" + PROJECT + "-lab-") and r["role"] == "lab" for r in obs_calls)
    put = next(r for r in obs_calls if r["method"] == "PUT" and r.get("query") == {"lifecycle": ""})
    rule = put["body"]["LifecycleConfiguration"]["Rule"][0]
    assert rule["Expiration"] == {"Days": 1} and rule["Status"] == "Enabled"
    assert "| rule-there |" in text


def test_a_refused_lifecycle_rule_stops_the_run_and_the_bucket_still_goes(five):
    fake = FiveCloud()
    real = fake.obs

    def refuse(m, path, query, body):
        if m == "PUT" and "lifecycle" in query:
            return fake.ok(400, None, "<Error><Code>MalformedXML</Code><Message>bad body</Message></Error>")
        return real(m, path, query, body)
    fake.obs = refuse
    code, lines, errors = go("obs-lifecycle-rule", fake)
    assert code != lab.EXIT_OK and fake.buckets == {} and [r["state"] for r in rows(five)] == ["deleted"]
    assert "MalformedXML" in evidence(five)


# --------------------------------------------------------------------------- dns-private-zone


def test_the_dns_recipe_binds_a_zone_adds_a_record_reads_it_and_tears_down(five):
    fake = FiveCloud()
    lines, text, rs = green("dns-private-zone", five, fake)
    assert [fake.stuff[r["id"]]["kind"] for r in rs] == ["vpc", "zone", "record"]
    vpc, zone, record = (fake.stuff[r["id"]] for r in rs)
    assert zone["body"]["zone_type"] == "private" and zone["body"]["router"]["router_id"] == rs[0]["id"]
    assert zone["body"]["name"].startswith(PROJECT + "-lab-") and zone["body"]["name"].endswith(".example.")
    assert record["body"]["name"] == "www." + zone["body"]["name"] and record["body"]["records"] == ["192.168.10.10"]
    assert fake.order[-3:] == ["delete record", "delete zone", "delete vpc"]
    assert "| record-address |" in text


def test_a_record_that_reads_another_address_fails_the_check_and_zone_and_vpc_still_go(five):
    fake = FiveCloud()
    real = fake.view

    def wrong(rid, it):
        v = real(rid, it)
        if it["kind"] == "record":
            v["records"] = ["192.168.99.99"]
        return v
    fake.view = wrong
    code, lines, errors = go("dns-private-zone", fake)
    assert code != lab.EXIT_OK and fake.all_live() == []
    assert [r["state"] for r in rows(five)] == ["deleted", "deleted", "deleted"]


# --------------------------------------------------------------------------- what every recipe writes


def test_every_create_of_the_five_carries_the_project_code_and_the_tags_the_lab_writes(five):
    for name in FIVE:
        fake = FiveCloud()
        for p in (five / "RESOURCES.md", *(five / "evidence").glob("lab-*.md")):
            if p.name == "RESOURCES.md":
                p.write_text(projects._resources(PROJECT), encoding="utf-8")
            else:
                p.unlink()
        code, lines, errors = go(name, fake)
        assert code == lab.EXIT_OK, (name, lines, errors)
        for it in fake.stuff.values():
            b = it["body"]
            named = " ".join(str(b.get(k, "")) for k in ("name", "description"))
            assert PROJECT + "-lab-" in named, (name, it["kind"])
        for b in fake.buckets:
            raise AssertionError("a bucket is left")
        assert all(r["role"] == "lab" and r["project"] == PROJECT for r in fake.calls[1:]
                   if r.get("op") == "call" and r["path"] != "/v2/{project_id}/types"), name
        assert json.dumps(fake.calls).count(PROJECT) > 0
