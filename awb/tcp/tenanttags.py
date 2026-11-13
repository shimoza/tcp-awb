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


def preflight_hosts() -> list[str]:
    """The same exact lists as doc fetch and lab obs fetch, without fetching a page."""
    from awb import vendorhosts
    from awb.tcp import doc

    # The existing display readers tolerate missing files. Preflight must not mistake that for evidence.
    doc.HOSTS_FILE.read_text(encoding="utf-8")
    vendorhosts.VENDOR_FILE.read_text(encoding="utf-8")
    extra = doc.extra_file()
    if extra is not None:
        extra.read_text(encoding="utf-8")
    download, portals = vendorhosts.read()
    return sorted((set(doc.current_hosts()) | set(download)) - set(portals))


def preflight_access(p, row, grant, *, request=None, store=None):
    """GET probes using only read keys. Never acquire a token or open a lab run.

    The call socket exposes loaded timestamps, but no store stat or project grant RPC. On the work side
    inaccessible owner metadata stays unknown. A successful work-side probe proves a project grant.
    """
    import json
    from pathlib import Path

    from awb import preflight, projects
    from awb.tcp import keys, readonly

    Check = preflight.Check
    if not projects.tenant_access(row.kind):
        return []
    request = request or keys.request
    store = Path(store) if store is not None else keys.store_root()
    out = []
    try:
        answer = request(keys.call_socket(), {"op": "tenants"}, timeout=5)
        if not isinstance(answer, dict) or not answer.get("ok") or not isinstance(answer.get("tenants"), dict):
            raise ValueError()
        held = answer["tenants"]
        if any(not isinstance(a, str) or not keys._ALIAS_RE.fullmatch(a) or not isinstance(t, dict)
               for a, t in held.items()):
            raise ValueError()
    except (OSError, ValueError, keys.KeysError):
        held = {}
        out.append(Check("tenants", "unknown", "key-service"))
    try:
        registered = {t.alias for t in tenants.load(p)}
    except (OSError, ValueError):
        registered = set()
        out.append(Check("tenant-register", "unknown", "unreadable"))
    try:
        tenant_grants = json.loads(readonly.grants_path(p).read_text(encoding="utf-8"))
        if not isinstance(tenant_grants, dict) or any(
                not isinstance(a, str) or not keys._ALIAS_RE.fullmatch(a) or not isinstance(codes, list)
                or any(not isinstance(code, str) for code in codes) for a, codes in tenant_grants.items()):
            raise ValueError()
    except FileNotFoundError:
        tenant_grants = {}
    except (OSError, ValueError):
        tenant_grants = None
    aliases = registered | set(held)
    if tenant_grants is not None:
        aliases |= {a for a, codes in tenant_grants.items() if isinstance(a, str)
                    and keys._ALIAS_RE.fullmatch(a) and isinstance(codes, list) and row.code in codes}
    if not aliases and not out:
        out.append(Check("tenants", "unknown", "none-discovered"))
    token = projects.read_key_token(Path(row.path))
    for alias in sorted(aliases):
        tenant = held.get(alias, {})
        is_readonly = tenant.get("kind") == "readonly" or (
            tenant_grants is not None and alias in tenant_grants)
        if is_readonly and tenant_grants is not None and row.code not in tenant_grants.get(alias, []):
            continue
        if is_readonly and tenant_grants is None:
            out.append(Check(alias + "/grant", "unknown", "unreadable"))
        data, state = None, "unknown"
        # A read-only login can POST to IAM while handling a GET. Do not enter that path, including write mode.
        roles = tenant.get("roles")
        if tenant and tenant.get("auth") in (None, "key") and isinstance(roles, list) and "read" in roles \
                and not (is_readonly and tenant.get("mode") == "write"):
            req = {"op": "call", "tenant": alias, "project": row.code, "token": token,
                   "role": "read", "method": "GET", "service": "vpc",
                   "path": "/v1/{project_id}/quotas", "query": {}, "body": None}
            try:
                response = request(keys.call_socket(), req, timeout=5)
                if not isinstance(response, dict):
                    raise ValueError()
                status = response.get("status")
                if response.get("ok") and type(status) is int and 200 <= status < 300:
                    state, data = "ok", response.get("data")
                    if grant.status == "unknown" and config.is_work_user():
                        out.append(Check("grant", "ok", "kind=project"))
                elif status in (401, 403) or "has no grant of the owner" in str(response.get("error", "")):
                    state = "missing"
                    if "has no grant of the owner" in str(response.get("error", "")):
                        out.append(Check("grant", "missing", "kind=project"))
            except (OSError, ValueError, keys.KeysError):
                pass
        out.append(Check(alias + "/read", state))
        if is_readonly:
            continue
        resources = data.get("quotas", {}).get("resources", []) if isinstance(data, dict) \
            and isinstance(data.get("quotas"), dict) else []
        for label, types in (("eip", ("floatingip", "eip")), ("vpc", ("vpc",)),
                             ("route-table", ("routetable", "route_table"))):
            matches = [r for r in resources if isinstance(r, dict) and r.get("type") in types] \
                if isinstance(resources, list) else []
            quota = matches[0] if len(matches) == 1 else {}
            used, cap = quota.get("used"), quota.get("quota")
            valid = type(used) is int and type(cap) is int and used >= 0 and cap >= 0
            free = max(0, cap - used) if valid else None
            out.append(Check(alias + "/" + label, ("ok" if free else "missing") if valid else "unknown",
                             "free=%d" % free if valid else "unreadable"))
        for part in ("domain", "user", "password"):
            entry = "awb/admin/%s/%s" % (alias, part)
            stamps = tenant.get("store_times")
            loaded = stamps.get(entry) if isinstance(stamps, dict) else None
            state = "unknown"
            if tenant and tenant.get("admin") in (None, False):
                state = "missing"
            elif tenant.get("admin") is True and type(loaded) is int and loaded > 0:
                try:
                    current = (store / (entry + ".gpg")).stat().st_mtime_ns
                    state = "missing" if current > loaded else "ok"
                except OSError:
                    pass
            out.append(Check(alias + "/admin/" + part, state, "store-age"))
    return out
