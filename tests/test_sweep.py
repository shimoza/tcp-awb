"""Package 7 (T-61, T-97): the sweep of untagged, expired and idle resources, and neutral handles for live ids.

The listings are stand-ins: invented ids and names, no call leaves the test.
"""
from __future__ import annotations

import datetime
import json
import os
import stat

import pytest

from awb import jobs
from awb.tcp import sweep

TODAY = datetime.date(2026, 9, 25)
NAMED = "zyxwo-prod-db"          # a resource name that carries a name: it must never reach the report


def listings(ecs=None, evs=None, eip=None):
    data = {"ecs": ecs, "evs": evs, "vpc": eip}

    def lister(service, path, key, paging):
        items = data[service]
        return jobs.Listing.unknown("HTTP 503") if items is None else jobs.Listing.of(items)
    return lister


SERVERS = [
    {"id": "srv-id-0001", "name": NAMED, "status": "ACTIVE", "created": "2026-09-01T10:00:00Z",
     "tags": ["awb-project=tcp-abcd", "awb-expiry=2026-09-20"]},
    {"id": "srv-id-0002", "name": "worker-a", "status": "SHUTOFF", "created": "2026-08-01T10:00:00Z",
     "tags": ["awb-project=tcp-abcd", "awb-expiry=2026-12-31"]},
    {"id": "srv-id-0003", "name": "worker-b", "status": "ACTIVE", "created": "2026-09-10T10:00:00Z",
     "tags": ["awb-project=tcp-abcd", "awb-expiry=2026-12-31"]},
    {"id": "srv-id-0004", "name": "legacy", "status": "ACTIVE", "created": "2025-01-01T10:00:00Z", "tags": []},
]
VOLUMES = [{"id": "vol-id-0001", "name": "scratch", "status": "available", "created_at": "2026-07-01T00:00:00",
            "tags": {"awb-project": "tcp-abcd"}}]
EIPS = [{"id": "eip-id-0001", "status": "DOWN", "create_time": "2026-06-01 00:00:00"}]


def test_the_sweep_names_untagged_expired_and_idle_oldest_first(tmp_path):
    h = sweep.Handles(tmp_path / "handles.json")
    rep = sweep.sweep(listings(SERVERS, VOLUMES, EIPS), TODAY, h)
    got = [(i.handle, i.reasons) for i in rep.items]
    assert got == [("ecs-4", ["untagged"]), ("eip-1", ["untagged", "idle"]), ("evs-1", ["no expiry", "idle"]),
                   ("ecs-2", ["idle"]), ("ecs-1", ["expired"])]
    assert rep.unknown == [] and rep.counts == {"expired": 1, "idle": 3, "no expiry": 1, "untagged": 2}
    assert "ecs-3" not in [i.handle for i in rep.items]                 # tagged, not expired, running


def test_an_unreadable_listing_is_unknown_and_never_empty(tmp_path):
    rep = sweep.sweep(listings(SERVERS, None, EIPS), TODAY, sweep.Handles(tmp_path / "h.json"))
    assert rep.unknown == ["evs"]
    assert any("could not read evs" in line for line in sweep.report_lines(rep))


def test_handles_are_stable_saved_private_and_mask_ids_and_names(tmp_path):
    path = tmp_path / "handles.json"
    h = sweep.Handles(path)
    rep = sweep.sweep(listings(SERVERS, VOLUMES, EIPS), TODAY, h)
    text = "\n".join(sweep.report_lines(rep))
    assert "srv-id-0001" not in text and NAMED not in text
    assert h.mask("the server %s (%s) is down" % (NAMED, "srv-id-0001")) == "the server ecs-1 (ecs-1) is down"
    h.save()
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    saved = json.loads(path.read_text(encoding="utf-8"))
    assert saved["handles"]["ecs-1"] == "srv-id-0001" and NAMED not in path.read_text(encoding="utf-8")
    again = sweep.Handles(path)
    assert again.handle("ecs", "srv-id-0001") == "ecs-1" and again.handle("ecs", "srv-id-9999") == "ecs-5"
    assert again.resolve("ecs-2") == "srv-id-0002"
    with pytest.raises(sweep.SweepError):
        again.resolve("ecs-77")


def test_the_sweep_marks_a_resource_listed_in_resources_as_known(tmp_path):
    root = tmp_path / "projects"
    folder = root / "tcp-abcd"
    folder.mkdir(parents=True)
    (folder / "SCOPE.md").write_text("# Scope\n", encoding="utf-8")
    (folder / "RESOURCES.md").write_text(
        "# Resources\n\n| handle | id | type | state |\n|---|---|---|---|\n| ecs-4 | srv-id-0004 | ecs | live |\n",
        encoding="utf-8")
    stray = root / "loose"                      # no SCOPE.md: not a project, its rows count for nothing
    stray.mkdir()
    (stray / "RESOURCES.md").write_text("| handle | id |\n|---|---|\n| eip-1 | eip-id-0001 |\n", encoding="utf-8")
    known = sweep.known_ids(root)
    assert known == {"srv-id-0004": "tcp-abcd"}
    rep = sweep.sweep(listings(SERVERS, VOLUMES, EIPS), TODAY, sweep.Handles(tmp_path / "h.json"), known)
    assert {i.handle: i.known for i in rep.items}["ecs-4"] == "tcp-abcd"
    assert {i.handle: i.known for i in rep.items}["eip-1"] == ""
    line = [x for x in sweep.report_lines(rep) if x.startswith("ecs-4")][0]
    assert "tcp-abcd" in line
    assert sweep.known_ids(tmp_path / "missing") == {}


# --------------------------------------------------------------------------- the network resources


NET_KINDS = ("vpc", "sub", "sg", "elb", "lsn", "pool", "nat")


def net_listings(answers: dict, asked: list | None = None):
    """A stand-in lister for ALL_SOURCES: the answer of each path (None: unreadable), every call kept in `asked`."""
    def lister(service, path, key, paging, query=None, project_header=False):
        if asked is not None:
            asked.append((service, path, key, project_header))
        items = answers.get(path, [])
        return jobs.Listing.unknown("HTTP 503") if items is None else jobs.Listing.of(items)
    return lister


def three_of(kind: str) -> list[dict]:
    """One untagged, one expired (tagged or named like a lab resource) and one tagged that no project lists."""
    expired = ({"tags": ["awb-project=tcp-abcd", "awb-expiry=2026-09-20"]} if kind in ("vpc", "elb") else
               {"name": "tcp-abcd-lab-20260920-wxyz"})
    return [
        {"id": "%s-id-0001" % kind, "name": NAMED, "status": "ACTIVE", "created_at": "2026-09-01T00:00:00"},
        dict({"id": "%s-id-0002" % kind, "status": "ACTIVE", "created_at": "2026-09-02T00:00:00"}, **expired),
        {"id": "%s-id-0003" % kind, "name": "tcp-abcd-lab-20261231-qrst", "status": "ACTIVE",
         "created_at": "2026-09-03T00:00:00"},
    ]


def test_the_sweep_lists_each_network_type_as_untagged_expired_or_not_in_resources(tmp_path):
    answers = {src[2]: three_of(src[0]) for src in sweep.NETWORK_SOURCES}
    answers.update({"/v1/{project_id}/cloudservers/detail": [], "/v2/{project_id}/cloudvolumes/detail": [],
                    "/v1/{project_id}/publicips": []})
    asked: list = []
    rep = sweep.sweep(net_listings(answers, asked), TODAY, sweep.Handles(tmp_path / "h.json"), {},
                      sweep.ALL_SOURCES)
    got = {(i.kind, i.handle.split("-")[1]): i.reasons for i in rep.items}
    for kind in NET_KINDS:
        assert got[(kind, "1")] == ["untagged"], kind
        assert got[(kind, "2")] == ["not in RESOURCES.md", "expired"], kind
        assert got[(kind, "3")] == ["not in RESOURCES.md"], kind
    assert rep.unknown == []
    assert {i.project for i in rep.items if i.handle.endswith("-3")} == {"tcp-abcd"}
    assert {i.expiry for i in rep.items if i.handle.endswith("-3")} == {"2026-12-31"}
    # ELB and NAT v2.0 carry no project in their path: their listing asks for the project header
    assert {s for s, _, _, header in asked if header} == {"elb", "nat"}
    assert ("vpc", "/v1/{project_id}/security-groups", "security_groups", False) in asked
    text = "\n".join(sweep.report_lines(rep))
    assert NAMED not in text and "sg-id-0001" not in text
    assert "sweep: not swept: dedicated ELB" in text and "to confirm live" in text


def test_a_network_resource_a_project_lists_is_known_and_not_named(tmp_path):
    answers = {src[2]: three_of(src[0]) for src in sweep.NETWORK_SOURCES}
    known = {"%s-id-0003" % kind: "tcp-abcd" for kind in NET_KINDS}
    rep = sweep.sweep(net_listings(answers), TODAY, sweep.Handles(tmp_path / "h.json"), known, sweep.ALL_SOURCES)
    assert not [i for i in rep.items if i.handle.endswith("-3")]          # tagged, listed, not expired
    assert all(i.reasons == ["untagged"] for i in rep.items if i.handle.endswith("-1"))


def test_an_unreadable_network_listing_is_unknown_and_never_empty(tmp_path):
    answers = {src[2]: three_of(src[0]) for src in sweep.NETWORK_SOURCES}
    answers["/v2.0/lbaas/listeners"] = None
    answers["/v2.0/nat_gateways"] = None
    rep = sweep.sweep(net_listings(answers), TODAY, sweep.Handles(tmp_path / "h.json"), {}, sweep.ALL_SOURCES)
    assert rep.unknown == ["lsn", "nat"]
    assert "sweep: could not read lsn, nat: the listing is unknown, not empty" in sweep.report_lines(rep)


def test_the_name_of_a_lab_resource_gives_its_tags():
    assert sweep.name_tags("tcp-abcd-lab-20261012-wxyz-a") == {"awb-project": "tcp-abcd",
                                                               "awb-expiry": "2026-10-12"}
    assert sweep.name_tags("tcp-abcd-lab-20261012-wxyz") != {}
    assert sweep.name_tags(NAMED) == {} and sweep.name_tags("tcp-abcd-web") == {}


def test_the_command_line_sweeps_the_network_types_with_the_project_header(home, tmp_path, monkeypatch, capsys):
    from awb import cli
    from awb.tcp import cloud
    from tests.tcp_fake import FakeGateway
    from tests.test_obs import fake_pass

    asked = []

    def listing(self, service, path, key, *, query=None, paging="marker", headers=None, **kw):
        asked.append((path, headers))
        return jobs.Listing.of(three_of(next(s[0] for s in sweep.ALL_SOURCES if s[2] == path)))
    with FakeGateway("AKFAKE", "sk-fake-secret") as gw:
        fake_pass(tmp_path, monkeypatch, {"cloud/owner/ak": "AKFAKE", "cloud/owner/sk": "sk-fake-secret"})
        monkeypatch.setenv("AWB_CLOUD_KEYS", "pass:cloud/owner")
        monkeypatch.setenv("AWB_CLOUD_ENDPOINT", gw.base)
        monkeypatch.setattr(cloud.Client, "list", listing)
        assert cli.main(["cloud", "sweep", "--today", "2026-09-25"]) == 0
    out = capsys.readouterr().out
    assert [p for p, _ in asked] == [s[2] for s in sweep.ALL_SOURCES]
    assert {p for p, h in asked if h} == {"/v2.0/lbaas/loadbalancers", "/v2.0/lbaas/listeners", "/v2.0/lbaas/pools",
                                          "/v2.0/nat_gateways"}
    assert all(h == {"X-Project-Id": FakeGateway.PROJECT} for _, h in asked if h)
    assert "not in RESOURCES.md" in out and "sweep: not swept: dedicated ELB" in out
    assert NAMED not in out


# --------------------------------------------------------------------------- the owner's sweep (live run 2026-10-12)


@pytest.mark.skipif(os.geteuid() == 0, reason="root reads and writes every file")
def test_the_owners_sweep_keeps_its_own_handles_when_the_shared_file_is_not_his(home, tmp_path, monkeypatch, capsys):
    from awb import cli
    from awb.tcp import cloud
    from tests.tcp_fake import FakeGateway
    from tests.test_obs import fake_pass

    shared = sweep.handles_path(home)
    shared.write_text(json.dumps({"handles": {"ecs-7": "srv-id-0007"}}), encoding="utf-8")
    os.chmod(shared, 0o000)                          # the work user's file, mode 600: this user cannot open it
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))

    def listing(self, service, path, key, *, query=None, paging="marker", headers=None, **kw):
        return jobs.Listing.of(SERVERS if path == "/v1/{project_id}/cloudservers/detail" else [])
    try:
        with FakeGateway("AKFAKE", "sk-fake-secret") as gw:
            fake_pass(tmp_path, monkeypatch, {"cloud/owner/ak": "AKFAKE", "cloud/owner/sk": "sk-fake-secret"})
            monkeypatch.setenv("AWB_CLOUD_KEYS", "pass:cloud/owner")
            monkeypatch.setenv("AWB_CLOUD_ENDPOINT", gw.base)
            monkeypatch.setattr(cloud.Client, "list", listing)
            assert cli.main(["cloud", "sweep", "--today", "2026-09-25"]) == 0
            out, err = capsys.readouterr()
            assert err.strip() == ("awb cloud: the shared handle file is not this user's: the handles of this sweep "
                                   "are this user's own (XDG_STATE_HOME/awb), their numbers differ from the shared "
                                   "ones")
            assert "Traceback" not in out + err and "ecs-1" in out
            own = tmp_path / "state" / "awb" / "handles.json"
            assert stat.S_IMODE(own.stat().st_mode) == 0o600
            assert json.loads(own.read_text(encoding="utf-8"))["handles"]["ecs-1"] == SERVERS[0]["id"]
            # neither file can be written: one line and exit 2, no traceback
            os.chmod(own, 0o400)
            assert cli.main(["cloud", "sweep", "--today", "2026-09-25"]) == 2
            out, err = capsys.readouterr()
            assert err.strip() == "awb cloud: neither the shared handle file nor this user's own can be written"
            assert out == ""
    finally:
        os.chmod(shared, 0o600)
    assert json.loads(shared.read_text(encoding="utf-8")) == {"handles": {"ecs-7": "srv-id-0007"}}


def test_a_handle_file_that_cannot_be_written_is_a_sweep_error(tmp_path):
    folder = tmp_path / "ro"
    folder.mkdir()
    h = sweep.Handles(folder / "handles.json")
    h.handle("ecs", "srv-id-0001")
    os.chmod(folder, 0o500)
    try:
        if os.geteuid() != 0:
            with pytest.raises(sweep.SweepError, match="cannot be written"):
                h.save()
    finally:
        os.chmod(folder, 0o700)


# --------------------------------------------------------------------------- OBS buckets (B-005)

BUCKETS = [
    {"id": "tcp-abcd-misc", "name": "tcp-abcd-misc", "created": "2026-09-01T08:00:00.000Z", "tags": []},
    {"id": "tcp-abcd-old", "name": "tcp-abcd-old", "created": "2026-09-02T08:00:00.000Z",
     "tags": [{"key": "awb-project", "value": "tcp-abcd"}, {"key": "awb-expiry", "value": "2026-09-20"}]},
    {"id": "tcp-abcd-lab-1", "name": "tcp-abcd-lab-1", "created": "2026-09-03T08:00:00.000Z",
     "tags": [{"key": "awb-project", "value": "tcp-abcd"}, {"key": "awb-expiry", "value": "2026-12-31"}]},
    {"id": "tcp-abcd-lab-2", "name": "tcp-abcd-lab-2", "created": "2026-09-04T08:00:00.000Z",
     "tags": [{"key": "awb-project", "value": "tcp-abcd"}, {"key": "awb-expiry", "value": "2026-12-31"}]},
]


def test_buckets_are_swept_by_the_rules_of_the_other_types(tmp_path):
    answers = {"/": BUCKETS}
    rep = sweep.sweep(net_listings(answers), TODAY, sweep.Handles(tmp_path / "h.json"),
                      {"tcp-abcd-old": "tcp-abcd", "tcp-abcd-lab-2": "tcp-abcd"}, sweep.OBS_SOURCES)
    assert [(i.handle, i.kind, i.reasons) for i in rep.items] == [
        ("obs-1", "obs", ["untagged"]), ("obs-2", "obs", ["expired"]), ("obs-3", "obs", ["not in RESOURCES.md"])]
    text = "\n".join(sweep.report_lines(rep))
    assert "tcp-abcd-misc" not in text and rep.unknown == []
    answers["/"] = None
    rep = sweep.sweep(net_listings(answers), TODAY, sweep.Handles(tmp_path / "h.json"), {}, sweep.OBS_SOURCES)
    assert rep.unknown == ["obs"] and rep.items == []


def test_the_report_says_when_the_buckets_were_not_swept(tmp_path):
    answers = {src[2]: [] for src in sweep.ALL_SOURCES}
    without = sweep.report_lines(sweep.sweep(net_listings(answers), TODAY, sweep.Handles(tmp_path / "h.json"), {},
                                             sweep.ALL_SOURCES))
    assert "sweep: not swept: %s" % sweep.NOT_SWEPT_OBS in without
    with_ = sweep.report_lines(sweep.sweep(net_listings(answers), TODAY, sweep.Handles(tmp_path / "h.json"), {},
                                           sweep.ALL_SOURCES + sweep.OBS_SOURCES))
    assert not any("OBS buckets" in line for line in with_)


def test_the_command_line_sweeps_the_buckets_with_the_flag_only(home, tmp_path, monkeypatch, capsys):
    from awb import cli
    from awb.tcp import cloud
    from tests.tcp_fake import FakeGateway
    from tests.test_obs import fake_pass

    asked = []

    def listing(self, service, path, key, *, query=None, paging="marker", headers=None, **kw):
        return jobs.Listing.of([])

    def buckets(self):
        asked.append(self.region)
        return jobs.Listing.of(BUCKETS)
    with FakeGateway("AKFAKE", "sk-fake-secret") as gw:
        fake_pass(tmp_path, monkeypatch, {"cloud/owner/ak": "AKFAKE", "cloud/owner/sk": "sk-fake-secret"})
        monkeypatch.setenv("AWB_CLOUD_KEYS", "pass:cloud/owner")
        monkeypatch.setenv("AWB_CLOUD_ENDPOINT", gw.base)
        monkeypatch.setattr(cloud.Client, "list", listing)
        monkeypatch.setattr(cloud.Client, "buckets", buckets)
        assert cli.main(["cloud", "sweep", "--today", "2026-09-25"]) == 0
        out = capsys.readouterr().out
        assert asked == [] and "obs-" not in out and "--buckets" in out
        assert cli.main(["cloud", "sweep", "--today", "2026-09-25", "--buckets"]) == 0
        out = capsys.readouterr().out
    assert asked == ["eu-de"]
    assert "untagged" in out and "expired" in out and "not in RESOURCES.md" in out
    assert "tcp-abcd-misc" not in out and "tcp-abcd-old" not in out


# --------------------------------------------------------------------------- named sources and digests (fact-check F3)

WAF_PATH, TOPIC_PATH, SUB_PATH, RDS_PATH = (s[2] for s in sweep.NAMED_SOURCES)


def named_answers() -> dict:
    return {
        WAF_PATH: [{"id": "waf-id-0001", "name": "tcp-q7m4-lab-20260920-wxyz"},
                   {"id": "waf-id-0002", "name": NAMED}],
        TOPIC_PATH: [{"topic_urn": "urn:smn:eu-de:0a1b:tcp-q7m4-lab-20261231-qrst",
                      "name": "tcp-q7m4-lab-20261231-qrst"},
                     {"topic_urn": "urn:smn:eu-de:0a1b:%s" % NAMED, "name": NAMED}],
        SUB_PATH: [{"subscription_urn": "urn:smn:eu-de:0a1b:tcp-q7m4-lab-20261231-qrst:sub-1",
                    "topic_urn": "urn:smn:eu-de:0a1b:tcp-q7m4-lab-20261231-qrst"}],
        RDS_PATH: [{"id": "rds-id-0001", "name": "tcp-q7m4-db", "status": "ACTIVE"}],
    }


def test_a_named_source_finds_the_resources_of_the_code_in_their_name(tmp_path):
    asked: list = []
    h = sweep.Handles(tmp_path / "h.json")
    rep = sweep.sweep(net_listings(named_answers(), asked), TODAY, h, known={"rds-id-0001": "tcp-q7m4"},
                      sources=sweep.NAMED_SOURCES)
    assert [a[1] for a in asked] == [WAF_PATH, TOPIC_PATH, SUB_PATH, RDS_PATH]
    by = {i.kind: i for i in rep.items}
    assert by["waf"].project == "tcp-q7m4" and by["waf"].expiry == "2026-09-20"
    assert by["waf"].reasons == ["not in RESOURCES.md", "expired"]
    assert by["smn"].reasons == ["not in RESOURCES.md"] and by["smns"].reasons == ["not in RESOURCES.md"]
    # the database a project lists, with the code but no lab name, has no expiry to tell
    assert by["rds"].known == "tcp-q7m4" and by["rds"].reasons == ["no expiry"]
    # a resource without a code is not the Workbench's: never reported, its name never in the report
    assert len(rep.items) == 4 and rep.unknown == []
    assert NAMED not in "\n".join(sweep.report_lines(rep))


def test_an_unreadable_named_listing_is_unknown_in_the_sweep_and_the_inventory(tmp_path):
    answers = named_answers()
    answers[WAF_PATH] = None
    rep = sweep.sweep(net_listings(answers), TODAY, sweep.Handles(tmp_path / "h.json"), sources=sweep.NAMED_SOURCES)
    assert rep.unknown == ["waf"] and "waf" not in {i.kind for i in rep.items}
    inv = sweep.inventory(net_listings(answers), sweep.NAMED_SOURCES)
    assert inv["waf"] is None and inv["rds"][0] == 1
    assert "waf unknown" in sweep.inventory_lines(inv)
    assert sweep.inventory_diff(inv, inv) == ["waf unknown"]


def test_equal_id_sets_give_equal_digests_and_one_id_more_does_not(tmp_path):
    answers = named_answers()
    before = sweep.inventory(net_listings(answers), sweep.NAMED_SOURCES)
    # every item counts, also one without a code: the inventory proves the tenant unchanged
    assert before["waf"][0] == 2 and before["smn"][0] == 2
    shuffled = dict(answers, **{WAF_PATH: list(reversed(answers[WAF_PATH]))})
    assert sweep.inventory(net_listings(shuffled), sweep.NAMED_SOURCES) == before
    assert sweep.inventory_diff(before, before) == []
    more = dict(answers, **{RDS_PATH: answers[RDS_PATH] + [{"id": "rds-id-0002", "name": "tcp-q7m4-db-2"}]})
    after = sweep.inventory(net_listings(more), sweep.NAMED_SOURCES)
    assert after["rds"][1] != before["rds"][1]
    assert sweep.inventory_diff(before, after) == ["rds +1/-0"]
    lines = sweep.inventory_lines(after)
    assert all("rds-id" not in line and NAMED not in line for line in lines)
    assert sweep.digest(["b", "a"]) == sweep.digest(["a", "b"]) != sweep.digest(["a", "b", "c"])
    # the network and server sources are in the inventory too
    inv = sweep.inventory(net_listings({}))
    assert set(inv) == {s[0] for s in sweep.ALL_SOURCES + sweep.NAMED_SOURCES} and inv["ecs"] == (0, sweep.digest([]))
