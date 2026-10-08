"""The live check of a leased key (T12 part 3, P2 and P3 of DESIGN-terraform.md), run by the owner.

    awb cloud lease-check ALIAS [--region R] [--yes] [--no-terraform]

P3: the key service mints a temporary AK/SK and security token from the tenant's lab key with an AK/SK-signed call
(op lease, 15 minutes, the policy of the Terraform services with IAM denied). P2: with that key alone, per service of
the lease one read and, where a throw-away write exists that costs nothing or cents, one create and one delete
(VPC with a subnet, an ECS server group, an EVS disk, a private DNS zone, a shared load balancer, a NAT gateway, an
object in the lab bucket); IMS is read only. Then one Terraform plan, apply and destroy of a one-resource set (a VPC)
with the key in the provider's environment only. Every resource carries the name awb-p2-<4 letters>; whatever was
made is deleted in the reverse order and checked gone, also after a failure. Without --yes it prints the plan and
makes nothing. The result is one line per step: the HTTP status and what it means (2xx works; 400, 404 or 409 the
key was accepted and the request was refused for its content; 401 or 403 the key was not accepted). No value of a
key, a token or an id is printed.
"""
from __future__ import annotations

import json
import os
import secrets
import shutil
import string
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path

from awb import obs
from awb.tcp import keys as _keys
from awb.tcp.cloud import Client, CloudError

WAIT = {"subnet": 90, "evs": 120, "elb": 180, "nat": 300, "gone": 180}


@dataclass
class Step:
    service: str
    what: str
    status: int | None = None
    note: str = ""

    def meaning(self) -> str:
        s = self.status
        if s is None:
            return "not run"
        if 200 <= s < 300:
            return "works"
        if s in (400, 404, 409, 422):
            return "key accepted, request refused"
        if s in (401, 403):
            return "key NOT accepted"
        return "no clear answer"


@dataclass
class Check:
    alias: str
    region: str
    name: str = field(default_factory=lambda: "awb-p2-" + "".join(secrets.choice(string.ascii_lowercase)
                                                                   for _ in range(4)))
    steps: list[Step] = field(default_factory=list)
    made: list[tuple] = field(default_factory=list)      # (label, delete function, gone function), newest last

    def step(self, service, what, response=None, note="") -> Step:
        st = Step(service, what, response.status if response is not None else None, note)
        self.steps.append(st)
        return st


def lease(alias: str, region: str | None, minutes: int = 15, request=_keys.request) -> dict:
    answer = request(_keys.call_socket(), {"op": "lease", "tenant": alias, "minutes": minutes,
                                           **({"region": region} if region else {})}, timeout=90)
    if not answer.get("ok"):
        raise CloudError("P3: %s" % answer.get("error", "the lease was refused"))
    return answer


def project_id(alias: str, region: str, request=_keys.request) -> str:
    """The id of the region's project, read with the tenant's read key through the key service (the leased key
    may not read IAM)."""
    answer = request(_keys.call_socket(), {"op": "call", "tenant": alias, "role": "read", "method": "GET",
                                           "service": "iam", "path": "/v3/projects", "region": region,
                                           "query": {"name": region}}, timeout=90)
    found = [p.get("id") for p in ((answer.get("data") or {}).get("projects") or []) if isinstance(p, dict)
             and p.get("name") == region]
    if not answer.get("ok") or len(found) != 1:
        raise CloudError("the project of %s cannot be read with the read key" % region)
    return found[0]


def _until(fn, seconds: int, every: float = 3.0) -> bool:
    end = time.monotonic() + seconds
    while True:
        if fn():
            return True
        if time.monotonic() >= end:
            return False
        time.sleep(every)


def run_checks(chk: Check, c: Client, pid: str, token: str, lab_bucket: str | None, obs_keys: obs.Keys) -> None:
    h = {"X-Security-Token": token}
    hp = dict(h, **{"X-Project-Id": pid})
    n = chk.name

    def req(method, service, path, body=None, query=None, headers=h):
        return c.request(method, service, path, body=body, query=query, headers=headers)

    def remember(label, delete, gone):
        chk.made.append((label, delete, gone))

    # VPC and a subnet (the network of the other writes)
    chk.step("vpc", "read", req("GET", "vpc", "/v1/%s/vpcs" % pid, query={"limit": "1"}))
    r = req("POST", "vpc", "/v1/%s/vpcs" % pid, {"vpc": {"name": n, "cidr": "192.168.0.0/16"}})
    chk.step("vpc", "create", r)
    vpc = ((r.data or {}).get("vpc") or {}).get("id") if r.ok else None
    subnet = net = nsub = None
    if vpc:
        remember("vpc", lambda: req("DELETE", "vpc", "/v1/%s/vpcs/%s" % (pid, vpc)),
                 lambda: req("GET", "vpc", "/v1/%s/vpcs/%s" % (pid, vpc)).status == 404)
        r = req("POST", "vpc", "/v1/%s/subnets" % pid, {"subnet": {
            "name": n, "cidr": "192.168.10.0/24", "gateway_ip": "192.168.10.1", "vpc_id": vpc,
            "availability_zone": "%s-01" % chk.region}})
        chk.step("vpc", "create subnet", r)
        sub = (r.data or {}).get("subnet") or {} if r.ok else {}
        subnet = sub.get("id")
        if subnet:
            remember("subnet", lambda: req("DELETE", "vpc", "/v1/%s/vpcs/%s/subnets/%s" % (pid, vpc, subnet)),
                     lambda: req("GET", "vpc", "/v1/%s/subnets/%s" % (pid, subnet)).status == 404)
            ok = _until(lambda: ((req("GET", "vpc", "/v1/%s/subnets/%s" % (pid, subnet)).data or {})
                                 .get("subnet") or {}).get("status") == "ACTIVE", WAIT["subnet"])
            if ok:
                data = (req("GET", "vpc", "/v1/%s/subnets/%s" % (pid, subnet)).data or {}).get("subnet") or {}
                net, nsub = data.get("neutron_network_id"), data.get("neutron_subnet_id")
    # ECS: a server group
    chk.step("ecs", "read", req("GET", "ecs", "/v1/%s/cloudservers/detail" % pid, query={"limit": "1"}))
    r = req("POST", "ecs", "/v2.1/%s/os-server-groups" % pid, {"server_group": {"name": n,
                                                                              "policies": ["anti-affinity"]}})
    chk.step("ecs", "create server group", r)
    group = ((r.data or {}).get("server_group") or {}).get("id") if r.ok else None
    if group:
        remember("ecs server group", lambda: req("DELETE", "ecs", "/v2.1/%s/os-server-groups/%s" % (pid, group)),
                 lambda: req("GET", "ecs", "/v2.1/%s/os-server-groups/%s" % (pid, group)).status == 404)
    # EVS: a 10 GB disk
    chk.step("evs", "read", req("GET", "evs", "/v2/%s/cloudvolumes/detail" % pid, query={"limit": "1"}))
    r = req("POST", "evs", "/v2/%s/volumes" % pid, {"volume": {"name": n, "size": 10, "volume_type": "SATA",
                                                              "availability_zone": "%s-01" % chk.region}})
    chk.step("evs", "create disk", r)
    disk = ((r.data or {}).get("volume") or {}).get("id") if r.ok else None
    if disk:
        _until(lambda: ((req("GET", "evs", "/v2/%s/volumes/%s" % (pid, disk)).data or {}).get("volume") or {})
               .get("status") in ("available", "error"), WAIT["evs"])
        remember("evs disk", lambda: req("DELETE", "evs", "/v2/%s/volumes/%s" % (pid, disk)),
                 lambda: req("GET", "evs", "/v2/%s/volumes/%s" % (pid, disk)).status == 404)
    # IMS: read only (no throw-away write)
    chk.step("ims", "read", req("GET", "ims", "/v2/cloudimages", query={"__imagetype": "private", "limit": "1"}))
    chk.step("ims", "write", None, "not checked: no throw-away write")
    # DNS: a private zone on the check's VPC
    chk.step("dns", "read", req("GET", "dns", "/v2/zones", query={"type": "private", "limit": "1"}, headers=hp))
    if vpc:
        r = req("POST", "dns", "/v2/zones", {"name": "%s.example." % n, "zone_type": "private",
                                            "router": {"router_id": vpc, "router_region": chk.region}}, headers=hp)
        chk.step("dns", "create private zone", r)
        zone = (r.data or {}).get("id") if r.ok else None
        if zone:
            remember("dns zone", lambda: req("DELETE", "dns", "/v2/zones/%s" % zone, headers=hp),
                     lambda: req("GET", "dns", "/v2/zones/%s" % zone, headers=hp).status == 404)
    # ELB: a shared load balancer on the check's subnet
    chk.step("elb", "read", req("GET", "elb", "/v2.0/lbaas/loadbalancers", query={"limit": "1"}, headers=hp))
    if nsub:
        r = req("POST", "elb", "/v2.0/lbaas/loadbalancers", {"loadbalancer": {"name": n, "vip_subnet_id": nsub}},
                headers=hp)
        chk.step("elb", "create load balancer", r)
        lb = ((r.data or {}).get("loadbalancer") or {}).get("id") if r.ok else None
        if lb:
            _until(lambda: ((req("GET", "elb", "/v2.0/lbaas/loadbalancers/%s" % lb, headers=hp).data or {})
                            .get("loadbalancer") or {}).get("provisioning_status") in ("ACTIVE", "ERROR"),
                   WAIT["elb"])
            remember("elb load balancer", lambda: req("DELETE", "elb", "/v2.0/lbaas/loadbalancers/%s" % lb,
                                                      headers=hp),
                     lambda: req("GET", "elb", "/v2.0/lbaas/loadbalancers/%s" % lb, headers=hp).status == 404)
    # NAT: the smallest gateway on the check's VPC and network
    chk.step("nat", "read", req("GET", "nat", "/v2.0/nat_gateways", query={"limit": "1"}, headers=hp))
    if vpc and net:
        r = req("POST", "nat", "/v2.0/nat_gateways", {"nat_gateway": {"name": n, "router_id": vpc,
                                                                     "internal_network_id": net, "spec": "1"}},
                headers=hp)
        chk.step("nat", "create gateway", r)
        gw = ((r.data or {}).get("nat_gateway") or {}).get("id") if r.ok else None
        if gw:
            _until(lambda: ((req("GET", "nat", "/v2.0/nat_gateways/%s" % gw, headers=hp).data or {})
                            .get("nat_gateway") or {}).get("status") in ("ACTIVE", "ERROR"), WAIT["nat"])
            remember("nat gateway", lambda: req("DELETE", "nat", "/v2.0/nat_gateways/%s" % gw, headers=hp),
                     lambda: req("GET", "nat", "/v2.0/nat_gateways/%s" % gw, headers=hp).status == 404)
    # OBS: an object in the lab bucket
    if lab_bucket:
        b = obs.Client(lab_bucket, obs_keys, chk.region)
        th = {"x-obs-security-token": token}
        status, _, _ = b._send("GET", "", query="max-keys=1", headers=dict(th))
        chk.steps.append(Step("obs", "read", status))
        key = "p2-check/%s.txt" % n
        status, _, _ = b._send("PUT", key, data=b"awb p2 check\n", headers=dict(th))
        chk.steps.append(Step("obs", "create object", status))
        if 200 <= status < 300:
            remember("obs object", lambda: b._send("DELETE", key, headers=dict(th)),
                     lambda: b._send("HEAD", key, headers=dict(th))[0] == 404)


def cleanup(chk: Check, out=print) -> list[str]:
    """Delete what the check made, newest first, and check each gone; the labels still there."""
    left = []
    for label, delete, gone in reversed(chk.made):
        try:
            delete()
            if not _until(lambda: gone(), WAIT["gone"], 5.0):
                left.append(label)
        except Exception:
            left.append(label)
    chk.made = []
    out("cleanup: %s" % ("every resource deleted and checked gone" if not left else "STILL THERE: " + ", ".join(left)))
    return left


TF_SET = """terraform {
  required_providers {
    opentelekomcloud = {
      source  = "opentelekomcloud/opentelekomcloud"
      version = "1.37.6"
    }
  }
}

provider "opentelekomcloud" {
  auth_url    = "https://iam.%(region)s.otc.t-systems.com/v3"
  tenant_name = "%(region)s"
}

resource "opentelekomcloud_vpc_v1" "check" {
  name = "%(name)s-tf"
  cidr = "10.251.0.0/16"
}
"""


def terraform(chk: Check, lease_: dict, terraform_bin: str = "terraform", out=print) -> None:
    """One plan, apply and destroy of a one-resource set with the leased key in the environment only."""
    folder = Path(tempfile.mkdtemp(prefix="awb-p2-tf-"))
    try:
        (folder / "main.tf").write_text(TF_SET % {"region": chk.region, "name": chk.name}, encoding="utf-8")
        env = {k: v for k, v in os.environ.items() if not k.startswith(("OS_", "TF_VAR_"))}
        env.update(OS_ACCESS_KEY=lease_["ak"], OS_SECRET_KEY=lease_["sk"], OS_SECURITY_TOKEN=lease_["token"],
                   TF_IN_AUTOMATION="1")
        for what, args in (("init", ["init", "-input=false", "-no-color"]),
                           ("plan", ["plan", "-input=false", "-no-color", "-out=plan"]),
                           ("apply", ["apply", "-input=false", "-no-color", "plan"]),
                           ("destroy", ["destroy", "-input=false", "-no-color", "-auto-approve"])):
            try:
                r = subprocess.run([terraform_bin] + args, cwd=folder, env=env, capture_output=True, text=True,
                                   timeout=900)
            except (OSError, subprocess.SubprocessError) as err:
                chk.steps.append(Step("terraform", what, None, type(err).__name__))
                break
            text = r.stdout + r.stderr
            for value in (lease_["ak"], lease_["sk"], lease_["token"]):
                text = text.replace(value, "<key>")
            last = [x for x in text.splitlines() if "Error" in x][:2]
            chk.steps.append(Step("terraform", what, 200 if r.returncode == 0 else 0,
                                  "; ".join(x.strip()[:160] for x in last)))
            if r.returncode != 0 and what != "destroy":
                if what == "apply":
                    subprocess.run([terraform_bin, "destroy", "-input=false", "-no-color", "-auto-approve"],
                                   cwd=folder, env=env, capture_output=True, text=True, timeout=900)
                break
    finally:
        shutil.rmtree(folder, ignore_errors=True)


def report(chk: Check, out=print) -> None:
    for st in chk.steps:
        out("%-9s %-22s %-5s %s%s" % (st.service, st.what, st.status if st.status is not None else "-",
                                      st.meaning() if st.service != "terraform" else
                                      ("works" if st.status == 200 else "FAILED" if st.status == 0 else "not run"),
                                      ("  (" + st.note + ")") if st.note else ""))


def main(alias: str, region: str | None, yes: bool, with_terraform: bool, out=print) -> int:
    from awb.tcp import xchg

    if not yes:
        out("dry run: the check would lease a 15-minute key of the lab key of %s, then read and create-and-delete "
            "one throw-away resource per service (VPC with subnet, ECS server group, EVS disk, DNS private zone, "
            "shared ELB, NAT gateway, an object in the lab bucket), IMS read only%s; run with --yes"
            % (alias, ", then a Terraform plan, apply and destroy of one VPC" if with_terraform else ""))
        return 0
    lz = lease(alias, region)
    region = lz["region"]
    out("P3: the lease of an AK/SK-signed call works: a temporary key for %d minutes" % lz["minutes"])
    chk = Check(alias, region)
    pid = project_id(alias, region)
    c = Client(obs.Keys(lz["ak"], lz["sk"]), region, label=alias, timeout=60.0)
    try:
        lab = xchg.read_settings().get("lab_bucket")
    except Exception:
        lab = None
    try:
        run_checks(chk, c, pid, lz["token"], lab, obs.Keys(lz["ak"], lz["sk"]))
    except Exception as err:
        chk.steps.append(Step("check", "stopped", None, type(err).__name__))
    finally:
        left = cleanup(chk, out)
    if with_terraform:
        terraform(chk, lz, out=out)
        gone = req_gone_tf(c, pid, chk, lz["token"])
        out("terraform cleanup: %s" % ("the VPC of the set is gone" if gone else "STILL THERE: the VPC of the set"))
        if not gone:
            left.append("terraform vpc")
    report(chk, out)
    return 0 if not left else 1


def req_gone_tf(c: Client, pid: str, chk: Check, token: str) -> bool:
    r = c.request("GET", "vpc", "/v1/%s/vpcs" % pid, query={"limit": "100"}, headers={"X-Security-Token": token})
    return not any(v.get("name") == chk.name + "-tf" for v in ((r.data or {}).get("vpcs") or []))
