"""The project tag on every resource a project creates on a test tenant (PT, 2026-11-09).

The owner runs into quotas on the test tenants and has to see which project made what. Every create call that the key
service signs for a project (`awb cloud call --role lab`, the op call, and the calls of a lab run, the op lab_call)
gets the tag `awb-project` with the project code:

- `inline`: the create body of a service that takes tags gets the tag in its own field and form (INLINE). A tag
  `awb-project` the body already carries is set to the project of the call, so no project can tag for another one;
  every other tag of the body stays (the recipes add `awb-expiry`).
- `after`: a service whose create body takes no tags gets one tag call right after a create that answered 2xx, with
  the id of the new resource (AFTER).
- `untagged`: any other create that made a resource. The call passes and one line goes to
  `<shared>/tenants/untagged-YYYY-MM.tsv` (time, tenant, project, service, path, result), so the report names it.

Each field, form and tag call comes from the API reference of the docs mirror (`/srv/tcp-mirrors/docs`), read on
2026-11-09; ECS server_tags (kv) is the form the recipes and the reaper use live, the docs name it without its form.
An RDS create takes no tags in its body (the docs), so it gets the tag call. A security group of VPC v1, an SNAT rule
and an OBS bucket have no tag call a create can follow and stay untagged.
"""
from __future__ import annotations

import datetime
import re
from pathlib import Path

KEY = "awb-project"
UNTAGGED = "untagged-%s.tsv"

_SEG = r"[^/]+"

# (service, path pattern, the field as a dotted path in the body, the form: kv | dict | list)
INLINE: list[tuple[str, re.Pattern, str, str]] = [
    ("ecs", re.compile(r"^/v1(\.1)?/%s/cloudservers$" % _SEG), "server.server_tags", "kv"),     # live in the recipes
    ("evs", re.compile(r"^/v2(\.1)?/%s/cloudvolumes$" % _SEG), "volume.tags", "dict"),
    ("elb", re.compile(r"^/v3/%s/elb/loadbalancers$" % _SEG), "loadbalancer.tags", "kv"),
    ("vpc", re.compile(r"^/v3/%s/vpc/security-groups$" % _SEG), "security_group.tags", "kv"),
    ("nat", re.compile(r"^/v3/%s/private-nat/gateways$" % _SEG), "gateway.tags", "kv"),
    ("cce", re.compile(r"^/api/v3/projects/%s/clusters/%s/nodes$" % (_SEG, _SEG)), "spec.userTags", "kv"),
    ("cce", re.compile(r"^/api/v3/projects/%s/clusters/%s/nodepools$" % (_SEG, _SEG)), "spec.nodeTemplate.userTags",
     "kv"),
    ("dcs", re.compile(r"^/v2/%s/instances$" % _SEG), "tags", "kv"),
    ("dms", re.compile(r"^/v2/kafka/%s/instances$" % _SEG), "tags", "kv"),
    ("dds", re.compile(r"^/v3/%s/instances$" % _SEG), "tags", "kv"),
    ("sfs-turbo", re.compile(r"^/v1/%s/sfs-turbo/shares$" % _SEG), "share.tags", "kv"),
    ("cbr", re.compile(r"^/v3/%s/vaults$" % _SEG), "vault.tags", "kv"),
    ("dns", re.compile(r"^/v2/zones$"), "tags", "kv"),
    ("css", re.compile(r"^/v2\.0/%s/clusters$" % _SEG), "cluster.tags", "kv"),
    ("vpcep", re.compile(r"^/v1/%s/vpc-endpoints$" % _SEG), "tags", "kv"),
    ("vpn", re.compile(r"^/v5/%s/vpn-gateways$" % _SEG), "vpn_gateway.tags", "kv"),
    ("er", re.compile(r"^/v3/%s/enterprise-router/instances$" % _SEG), "instance.tags", "kv"),
]

# (service, path pattern (group 1 is the project id when the path has one), where the id is in the answer, the tag
# call path with {project} and {id}, the body: "action" = {"action": "create", "tags": [kv]}, "plain" = {"tags": [kv]})
AFTER: list[tuple[str, re.Pattern, str, str, str]] = [
    ("vpc", re.compile(r"^/v1/(%s)/vpcs$" % _SEG), "vpc.id", "/v2.0/{project}/vpcs/{id}/tags/action", "action"),
    ("vpc", re.compile(r"^/v1/(%s)/subnets$" % _SEG), "subnet.id", "/v2.0/{project}/subnets/{id}/tags/action",
     "action"),
    ("vpc", re.compile(r"^/v1/(%s)/publicips$" % _SEG), "publicip.id", "/v2.0/{project}/publicips/{id}/tags/action",
     "action"),
    ("eip", re.compile(r"^/v3/(%s)/eip/publicips$" % _SEG), "publicip.id",
     "/v2.0/{project}/publicips/{id}/tags/action", "action"),
    ("evs", re.compile(r"^/v2/(%s)/volumes$" % _SEG), "volume.id", "/v2/{project}/os-vendor-volumes/{id}/tags/action",
     "action"),
    ("elb", re.compile(r"^/v2\.0/lbaas/loadbalancers$"), "loadbalancer.id",
     "/v2.0/{project}/loadbalancers/{id}/tags/action", "action"),
    ("nat", re.compile(r"^/v2\.0/nat_gateways$"), "nat_gateway.id", "/v2.0/{project}/nat_gateways/{id}/tags/action",
     "action"),
    ("rds", re.compile(r"^/v3/(%s)/instances$" % _SEG), "instance.id", "/v3/{project}/instances/{id}/tags/action",
     "action"),
    ("cce", re.compile(r"^/api/v3/projects/(%s)/clusters$" % _SEG), "metadata.uid",
     "/api/v3/projects/{project}/clusters/{id}/tags/create", "plain"),
    ("kms", re.compile(r"^/v1\.0/(%s)/kms/create-key$" % _SEG), "key_info.key_id", "/v1.0/{project}/kms/{id}/tags/action",
     "action"),
    ("as", re.compile(r"^/autoscaling-api/v1/(%s)/scaling_group$" % _SEG), "scaling_group_id",
     "/autoscaling-api/v1/{project}/scaling_group_tag/{id}/tags/action", "action"),
]

_NOT_CREATE = frozenset({"tokens", "securitytokens", "action", "actions", "delete", "publish", "query", "search",
                         "detail", "batch", "count", "validate", "check", "logout", "tags"})
_ID_RE = re.compile(r"^[A-Za-z0-9_.:-]{1,128}$")


def tag(project: str) -> dict:
    return {"key": KEY, "value": project}


def is_create(method: str, service: str, path: str) -> bool:
    """A POST that makes a resource of its own (the rule of the lab run's record, labadmin.creates)."""
    if method.upper() != "POST" or service in ("obs", "iam"):
        return False
    segs = [s for s in path.strip("/").split("/") if s]
    if not segs:
        return False
    last = segs[-1].lower()
    return last not in _NOT_CREATE and not last.endswith("action") and "auth" not in [s.lower() for s in segs]


def _form(fields, project: str, form: str):
    """`fields` (the tags the body already carries, or None) with awb-project set to `project`."""
    if form == "dict":
        out = dict(fields) if isinstance(fields, dict) else {}
        out[KEY] = project
        return out
    if form == "list":
        out = [x for x in fields if isinstance(x, str) and x.partition("=")[0] != KEY] \
            if isinstance(fields, list) else []
        return out + ["%s=%s" % (KEY, project)]
    out = [x for x in fields if not (isinstance(x, dict) and x.get("key") == KEY)] if isinstance(fields, list) else []
    return out + [tag(project)]


def _place(body: dict, dotted: str, project: str, form: str) -> bool:
    parts = dotted.split(".")
    node = body
    for p in parts[:-1]:
        if not isinstance(node.get(p), dict):
            return False
        node = node[p]
    node[parts[-1]] = _form(node.get(parts[-1]), project, form)
    return True


def inject(method: str, service: str, path: str, body, project: str) -> tuple[object, str | None]:
    """(body, how) for one call: how is "inline" when the tag went into the body, "after" when a tag call follows a
    2xx answer, "untagged" for any other create, None for a call that creates nothing. The body given is never
    changed in place."""
    if not is_create(method, service, path) or not project:
        return body, None
    for svc, pattern, dotted, form in INLINE:
        if svc == service and pattern.match(path):
            if isinstance(body, dict):
                import copy

                new = copy.deepcopy(body)
                if _place(new, dotted, project, form):
                    return new, "inline"
            return body, "untagged"
    for svc, pattern, _where, _tpath, _form_ in AFTER:
        if svc == service and pattern.match(path):
            return body, "after"
    return body, "untagged"


def _dig(data, dotted: str):
    for p in dotted.split("."):
        if not isinstance(data, dict):
            return None
        data = data.get(p)
    return data


def after_call(service: str, path: str, data, project: str) -> tuple[str, str, str, dict] | None:
    """(method, service, path, body) of the tag call that follows a create of AFTER, or None when the answer names
    no id."""
    for svc, pattern, where, tpath, style in AFTER:
        m = pattern.match(path) if svc == service else None
        if m:
            rid = _dig(data, where)
            if not isinstance(rid, str) or not _ID_RE.match(rid):
                return None
            pid = m.group(1) if pattern.groups else "{project_id}"
            tags = _form(None, project, "kv")
            return "POST", service, tpath.format(project=pid, id=rid), \
                ({"action": "create", "tags": tags} if style == "action" else {"tags": tags})
    return None


def note_untagged(tenant: str, project: str, service: str, path: str, result: str, shared: Path | None = None,
                  now: datetime.datetime | None = None) -> None:
    """One line for a create that carries no project tag; a line that cannot be written never fails the call."""
    now = now or datetime.datetime.now(datetime.timezone.utc)
    try:
        if shared is None:
            from awb import config

            shared = config.paths().shared
        f = Path(shared) / "tenants" / (UNTAGGED % now.strftime("%Y-%m"))
        if not f.parent.is_dir():
            from awb import config

            config.make_dir(f.parent, 0o770, shared=True)
        clean = [re.sub(r"[\t\r\n]+", " ", str(x))[:200] for x in
                 (now.strftime("%Y-%m-%dT%H:%M:%SZ"), tenant, project, service, path, result)]
        with open(f, "a", encoding="utf-8") as fh:
            fh.write("\t".join(clean) + "\n")
    except OSError:
        pass


def untagged(shared: Path, month: str) -> list[dict]:
    f = Path(shared) / "tenants" / (UNTAGGED % month)
    out = []
    try:
        lines = f.read_text(encoding="utf-8").splitlines()
    except OSError:
        return out
    for line in lines:
        parts = line.split("\t")
        if len(parts) == 6:
            out.append(dict(zip(("at", "tenant", "project", "service", "path", "result"), parts)))
    return out
