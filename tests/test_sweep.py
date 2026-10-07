"""Package 7 (T-61, T-97): the sweep of untagged, expired and idle resources, and neutral handles for live ids.

The listings are stand-ins: invented ids and names, no call leaves the test.
"""
from __future__ import annotations

import datetime
import json
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
