"""awb tenant tags [ALIAS]: who holds what on a test tenant, next to its quotas (PT, 2026-11-09).

From the latest snapshot: the servers, disks and elastic IPs per `awb-project` tag ("-" for none). From the month's
`<shared>/tenants/untagged-YYYY-MM.tsv`: the creates of the projects that carry no tag (awb/tcp/projecttags.py).
Live, with the read key through the key service: the ECS limits (servers, cores, RAM) and the VPC quotas (VPCs,
subnets, security groups, elastic IPs and the rest), each quota at 80 % or more marked "near" and at 100 % "full".
A quota that cannot be read is said so in one line; the rest of the report stands.
"""
from __future__ import annotations

import datetime
from typing import Callable

from awb import config
from awb.tcp import projecttags, tenants

ECS_QUOTAS = (("servers", "maxTotalInstances", "totalInstancesUsed"),
              ("cores", "maxTotalCores", "totalCoresUsed"),
              ("RAM MB", "maxTotalRAMSize", "totalRAMUsed"))
NEAR = 0.8

Reader = Callable[[str, str, str], dict]
"""(alias, service, path) -> the JSON answer of one GET with the read key; raises on any failure."""


def _key_reader(alias: str, service: str, path: str) -> dict:
    from awb.tcp import keys

    answer = keys.request(keys.call_socket(), {"op": "call", "tenant": alias, "role": "read", "method": "GET",
                                               "service": service, "path": path, "query": {}, "body": None})
    if not answer.get("ok") or not 200 <= int(answer.get("status") or 0) < 300 or not isinstance(answer.get("data"),
                                                                                                    dict):
        raise RuntimeError(str(answer.get("error") or "HTTP %s" % answer.get("status")))
    return answer["data"]


def quotas(alias: str, read: Reader) -> tuple[list[tuple[str, int, int]], list[str]]:
    """([(name, used, quota)], [what could not be read])."""
    rows, missing = [], []
    try:
        absolute = read(alias, "ecs", "/v1/{project_id}/cloudservers/limits").get("absolute") or {}
        for name, cap, used in ECS_QUOTAS:
            if isinstance(absolute.get(cap), int) and isinstance(absolute.get(used), int):
                rows.append((name, absolute[used], absolute[cap]))
    except Exception:                                   # noqa: BLE001 - one line in the report, never a crash
        missing.append("ECS limits")
    try:
        for r in (read(alias, "vpc", "/v1/{project_id}/quotas").get("quotas") or {}).get("resources") or []:
            if isinstance(r, dict) and isinstance(r.get("used"), int) and isinstance(r.get("quota"), int):
                rows.append((str(r.get("type")), r["used"], r["quota"]))
    except Exception:                                   # noqa: BLE001
        missing.append("VPC quotas")
    return rows, missing


def mark(used: int, quota: int) -> str:
    if quota <= 0:
        return ""
    if used >= quota:
        return "full"
    return "near" if used >= NEAR * quota else ""


def lines(p: config.Paths, alias: str, today: datetime.date, read: Reader = _key_reader) -> list[str]:
    snap = tenants.latest(p, alias)
    out = []
    if snap is None:
        out.append("%s: no snapshot yet (awb tenant snapshot %s)" % (alias, alias))
    else:
        by: dict[str, dict[str, int]] = {}
        for i in snap["items"]:
            k = by.setdefault(i.get("project") or "-", {})
            k[i["kind"]] = k.get(i["kind"], 0) + 1
        kinds = sorted({i["kind"] for i in snap["items"]})
        out.append("%s: %d resources by project tag in the snapshot of %s (- = no awb-project tag)"
                   % (alias, len(snap["items"]), snap["date"]))
        rows = [[proj] + [str(by[proj].get(k, 0)) for k in kinds] + [str(sum(by[proj].values()))]
                for proj in sorted(by, key=lambda x: (x == "-", x))]
        if rows:
            out += ["  " + x for x in tenants.table(rows, ["project"] + kinds + ["total"])]
    month = today.strftime("%Y-%m")
    notes = [n for n in projecttags.untagged(p.shared, month) if n["tenant"] == alias]
    if notes:
        count: dict[tuple[str, str], int] = {}
        for n in notes:
            count[(n["project"], n["service"])] = count.get((n["project"], n["service"]), 0) + 1
        out.append("%s: %d create(s) without the project tag in %s" % (alias, len(notes), month))
        out += ["  " + x for x in tenants.table([[pr, sv, str(c)] for (pr, sv), c in sorted(count.items())],
                                                ["project", "service", "creates"])]
    rows, missing = quotas(alias, read)
    if rows:
        out.append("%s: quotas now" % alias)
        out += ["  " + x for x in tenants.table([[n, str(u), str(q), mark(u, q)] for n, u, q in rows],
                                                ["quota", "used", "of", ""])]
    for m in missing:
        out.append("%s: %s not readable now" % (alias, m))
    return out


def command(p: config.Paths, alias: str | None, today: datetime.date) -> list[str]:
    aliases = [alias] if alias else [t.alias for t in tenants.load(p)]
    out: list[str] = []
    for a in aliases:
        tenants.get(p, a)
        out += lines(p, a, today)
    return out or ["no tenant yet"]
