"""Resources on the test tenants over time (awb/tcp/tenants.py): snapshots, events and the questions. The cloud is a
stand-in; every id is invented."""
from __future__ import annotations

import datetime
import json
import os

import pytest

from awb import jobs
from awb.tcp import tenants


def server(rid, created, status="ACTIVE", project="tcp-q7m4", expiry="2026-12-31", flavor="s3.large.2"):
    tags = ["awb-project=%s" % project] if project else []
    if expiry:
        tags.append("awb-expiry=%s" % expiry)
    return {"id": rid, "created": created, "status": status, "flavor": {"id": flavor}, "tags": tags}


class Cloud:
    """A stand-in tenant: what each list call answers, per kind."""

    def __init__(self):
        self.servers = [server("id-a", "2026-08-01T10:00:00Z"), server("id-b", "2026-09-20T09:00:00Z", project="")]
        self.volumes = [{"id": "vol-1", "created_at": "2026-08-01T10:05:00Z", "status": "in-use", "size": 40,
                         "volume_type": "SSD", "metadata": {}}]
        self.eip = jobs.Listing(jobs.LIST, ({"id": "ip-1", "create_time": "2026-08-02 08:00:00", "status": "ACTIVE",
                                              "bandwidth_size": 5},))
        self.broken_volumes = False

    def lister(self, t, region):
        def call(service, path, key, paging):
            if key == "servers":
                return jobs.Listing(jobs.LIST, tuple(self.servers)) if self.servers else jobs.Listing(jobs.EMPTY)
            if key == "volumes":
                if self.broken_volumes:
                    return jobs.Listing(jobs.UNKNOWN, reason="HTTP 503")
                return jobs.Listing(jobs.LIST, tuple(self.volumes)) if self.volumes else jobs.Listing(jobs.EMPTY)
            return self.eip
        return call


def at(day: str) -> datetime.datetime:
    return datetime.datetime.fromisoformat(day + "T06:00:00+00:00")


@pytest.fixture
def tenant(home):
    return tenants.add(home, "test-1", "file:/nowhere/keys", ["eu-de"], today="2026-09-01")


def test_add_checks_the_alias_and_the_key_reference(home, tenant):
    assert [t.alias for t in tenants.load(home)] == ["test-1"]
    for alias, keys in (("Test 1", "file:/x"), ("test-2", "plain-key"), ("test-1", "file:/x")):
        with pytest.raises(tenants.TenantError):
            tenants.add(home, alias, keys, ["eu-de"])


def test_the_first_snapshot_sees_everything_with_its_own_creation_time(home, tenant):
    cloud = Cloud()
    snap, events = tenants.snapshot(home, tenant, cloud.lister, now=at("2026-09-01"))
    assert len(snap["items"]) == 4 and {e["event"] for e in events} == {"seen"}
    assert snap["items"][0]["created"].startswith("2026-08-01")
    stored = (tenants.folder(home, "test-1") / "snapshots" / "2026-09-01.json").read_text(encoding="utf-8")
    assert "id-a" not in stored and "vol-1" not in stored, "ids stay in the handle file"
    assert oct(os.stat(tenants.folder(home, "test-1") / "handles.json").st_mode)[-3:] == "600"


def test_the_next_snapshots_record_what_appeared_changed_and_went(home, tenant):
    cloud = Cloud()
    tenants.snapshot(home, tenant, cloud.lister, now=at("2026-09-01"))
    cloud.servers[0]["status"] = "SHUTOFF"
    cloud.servers.append(server("id-c", "2026-09-02T12:00:00Z", project="tcp-ab12"))
    del cloud.servers[1]
    _, events = tenants.snapshot(home, tenant, cloud.lister, now=at("2026-09-02"))
    got = sorted((e["event"], e["handle"]) for e in events)
    assert [g[0] for g in got] == ["appeared", "changed", "gone"]
    changed = [e for e in events if e["event"] == "changed"][0]
    assert changed["fields"] == ["state"] and changed["was"] == {"state": "ACTIVE"}
    lines = tenants.history_lines(home, "test-1", project="tcp-ab12")
    assert len(lines) == 2 and "appeared" in lines[1]


def test_an_unreadable_listing_is_never_read_as_gone(home, tenant):
    cloud = Cloud()
    tenants.snapshot(home, tenant, cloud.lister, now=at("2026-09-01"))
    cloud.broken_volumes = True
    snap, events = tenants.snapshot(home, tenant, cloud.lister, now=at("2026-09-02"))
    assert events == [] and snap["unknown"] == {"eu-de": ["evs"]}
    assert "not readable in eu-de: evs" in "\n".join(tenants.now_lines(home, "test-1", datetime.date(2026, 9, 2)))


def test_now_at_list_and_project_answer_from_the_snapshots(home, tenant):
    cloud = Cloud()
    tenants.snapshot(home, tenant, cloud.lister, now=at("2026-09-01"))
    cloud.servers.append(server("id-c", "2026-09-10T12:00:00Z", project="tcp-ab12", expiry="2026-09-15"))
    tenants.snapshot(home, tenant, cloud.lister, now=at("2026-09-10"))
    today = datetime.date(2026, 9, 20)
    now = "\n".join(tenants.now_lines(home, "test-1", today))
    assert "5 resources in the snapshot of 2026-09-10" in now and "no project" in now and "expired" in now
    assert "id-c" not in now
    assert tenants.latest(home, "test-1", on="2026-09-05")["date"] == "2026-09-01"
    assert tenants.latest(home, "test-1", on="2026-08-01") is None
    listed = "\n".join(tenants.list_lines(home, today))
    assert "test-1" in listed and "2026-09-10" in listed
    proj = "\n".join(tenants.project_lines(home, "tcp-ab12", today))
    assert "1 running now" in proj and "appeared" in proj
    assert tenants.project_lines(home, "tcp-zz99", today) == ["tcp-zz99: nothing on any tenant"]


def test_a_second_snapshot_on_the_same_day_replaces_it_and_adds_only_new_events(home, tenant):
    cloud = Cloud()
    tenants.snapshot(home, tenant, cloud.lister, now=at("2026-09-01"))
    _, events = tenants.snapshot(home, tenant, cloud.lister, now=at("2026-09-01"))
    assert events == []
    assert len(tenants.snapshots(home, "test-1")) == 1
    assert len(tenants.events(home, "test-1")) == 4


def test_a_key_file_must_be_closed_and_complete(home, tmp_path):
    f = tmp_path / "keys"
    f.write_text("ak=AKINVENTED\n" + "s" + "k=" + "invented" + "-value\n", encoding="utf-8")
    os.chmod(f, 0o644)
    t = tenants.Tenant("test-9", ("eu-de",), "file:%s" % f, "2026-09-01")
    with pytest.raises(tenants.TenantError, match="open to others"):
        tenants.keys_of(t)
    os.chmod(f, 0o600)
    assert tenants.keys_of(t).ak == "AKINVENTED"
    f.write_text("ak=only\n", encoding="utf-8")
    with pytest.raises(tenants.TenantError, match="needs an ak= and an sk="):
        tenants.keys_of(t)


def test_the_command_line_refuses_a_bad_date_and_an_unknown_tenant(home, tenant, capsys):
    assert tenants.main(["now", "test-7"]) == 1 and "no tenant with the alias" in capsys.readouterr().err
    assert tenants.main(["at", "test-1", "yesterday"]) == 1
    assert tenants.main(["list"]) == 0 and "test-1" in capsys.readouterr().out
