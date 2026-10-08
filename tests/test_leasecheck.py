"""The live check of a leased key (awb/tcp/leasecheck.py, T12 part 3 P2 and P3) against a fake cloud: every call carries
the security token, whatever was made is deleted newest first and checked gone, the key reaches Terraform through its
environment only, and the dry run makes nothing."""
import os
import stat
from pathlib import Path

import pytest

from awb import obs
from awb.tcp import leasecheck
from awb.tcp.cloud import Response

TOKEN = "tmp-token-value-x"
LEASE = {"ok": True, "region": "eu-de", "minutes": 15, "ak": "TMPAKQ1", "sk": "tmp-secret-q1", "token": TOKEN}


class FakeCloud:
    """Creates answer an id, a GET of a live id answers ACTIVE or available, a deleted id answers 404."""

    def __init__(self, refuse=()):
        self.live, self.calls, self.n, self.refuse = {}, [], 0, set(refuse)

    def request(self, method, service, path, *, query=None, body=None, headers=None):
        self.calls.append((method, service, path, dict(headers or {})))
        if service in self.refuse:
            return Response(403, {"error": "no"}, "")
        last = path.rstrip("/").rsplit("/", 1)[-1]
        if method == "POST":
            self.n += 1
            ident = "id-%d" % self.n
            self.live[ident] = service
            kind = next(iter(body))
            data = {kind: {"id": ident}} if kind != "name" else {"id": ident}
            if kind == "subnet":
                data[kind].update(neutron_network_id="net-1", neutron_subnet_id="nsub-1")
            return Response(201, data, "")
        if method == "DELETE":
            self.live.pop(last, None)
            return Response(204, None, "")
        if last in self.live:
            inner = {"status": "ACTIVE", "provisioning_status": "ACTIVE", "neutron_network_id": "net-1",
                     "neutron_subnet_id": "nsub-1"}
            return Response(200, {k: dict(inner, status="available" if k == "volume" else "ACTIVE")
                                  for k in ("subnet", "volume", "loadbalancer", "nat_gateway")}, "")
        if last.startswith("id-"):
            return Response(404, None, "")
        return Response(200, {"vpcs": []}, "")


@pytest.fixture(autouse=True)
def quick(monkeypatch):
    monkeypatch.setattr(leasecheck.time, "sleep", lambda s: None)


def test_every_call_carries_the_token_and_everything_made_is_deleted_newest_first():
    cloud = FakeCloud()
    chk = leasecheck.Check("test-1", "eu-de")
    leasecheck.run_checks(chk, cloud, "pid", TOKEN, None, obs.Keys("a", "b"))
    assert all(h.get("X-Security-Token") == TOKEN for _, _, _, h in cloud.calls)
    assert all(h.get("X-Project-Id") == "pid" for _, s, _, h in cloud.calls if s in ("dns", "elb", "nat"))
    assert [label for label, _, _ in chk.made] == ["vpc", "subnet", "ecs server group", "evs disk", "dns zone",
                                                   "elb load balancer", "nat gateway"]
    made = [p for m, _, p, _ in cloud.calls if m == "POST"]
    assert len(made) == 7
    lines = []
    assert leasecheck.cleanup(chk, lines.append) == [] and not cloud.live
    deletes = [p for m, _, p, _ in cloud.calls if m == "DELETE"]
    assert "/nat_gateways/" in deletes[0] and "/vpcs/" in deletes[-1] and "/subnets/" in deletes[-2]
    assert lines == ["cleanup: every resource deleted and checked gone"]
    out = []
    leasecheck.report(chk, out.append)
    text = "\n".join(out)
    assert "id-" not in text and TOKEN not in text and "ims       write" in text and "not checked" in text


def test_a_resource_that_stays_is_named_and_a_refused_key_reads_as_not_accepted():
    cloud = FakeCloud(refuse={"nat"})
    chk = leasecheck.Check("test-1", "eu-de")
    leasecheck.run_checks(chk, cloud, "pid", TOKEN, None, obs.Keys("a", "b"))
    nat = [s for s in chk.steps if s.service == "nat"]
    assert nat[0].status == 403 and nat[0].meaning() == "key NOT accepted"
    label, delete, gone = chk.made[0]
    chk.made[0] = (label, delete, lambda: False)
    monkey = leasecheck.WAIT
    leasecheck.WAIT = dict(monkey, gone=0)
    try:
        lines = []
        assert leasecheck.cleanup(chk, lines.append) == ["vpc"]
    finally:
        leasecheck.WAIT = monkey
    assert lines == ["cleanup: STILL THERE: vpc"]


def test_terraform_gets_the_key_in_its_environment_only(tmp_path):
    record = tmp_path / "record"
    fake = tmp_path / "terraform"
    fake.write_text("#!/bin/sh\necho \"$@\" >> %s\n[ -n \"$OS_ACCESS_KEY\" ] && [ -n \"$OS_SECURITY_TOKEN\" ] && "
                    "echo env-ok >> %s\ncat main.tf >> %s\nexit 0\n" % (record, record, record))
    fake.chmod(fake.stat().st_mode | stat.S_IXUSR)
    chk = leasecheck.Check("test-1", "eu-de")
    os.environ["TF_VAR_planted"] = "x"
    try:
        leasecheck.terraform(chk, LEASE, terraform_bin=str(fake))
    finally:
        del os.environ["TF_VAR_planted"]
    text = record.read_text()
    assert [s.what for s in chk.steps] == ["init", "plan", "apply", "destroy"] and all(s.status == 200 for s in chk.steps)
    assert text.count("env-ok") == 4
    for value in (LEASE["ak"], LEASE["sk"], LEASE["token"]):
        assert value not in text
    assert 'name = "%s-tf"' % chk.name in text and "security_token" not in text and "access_key" not in text
    assert not list(Path("/tmp").glob("awb-p2-tf-*%s" % chk.name))


def test_the_dry_run_makes_nothing(monkeypatch):
    called = []
    monkeypatch.setattr(leasecheck, "lease", lambda *a, **k: called.append(a))
    out = []
    assert leasecheck.main("test-1", None, False, True, out.append) == 0
    assert called == [] and out[0].startswith("dry run:") and "--yes" in out[0]
