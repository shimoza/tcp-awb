"""Package 7, the cloud side: the resource sweep (T-61) and neutral handles for live identifiers (T-97).

    awb cloud sweep [--region R] [--today YYYY-MM-DD]     read-only: GET list calls only

T-61. A resource the Workbench creates carries two tags: `awb-project` (the project code, tcp-xxxx) and
`awb-expiry` (an ISO date). A service that takes no tags carries both in the name the lab gives it
(`<code>-lab-<YYYYMMDD>-<4 letters>`, awb/tcp/lab.py), which the sweep reads in the place of the tags. The sweep
lists servers, disks and elastic IPs (SOURCES) and on the command line also VPCs, subnets, security groups, shared
ELB load balancers, listeners and pools and NAT gateways (NETWORK_SOURCES). It names every one that is untagged (no
project tag), expired (its expiry date has passed), not in RESOURCES.md (it carries a project tag and no project
lists its id) or idle (a server that is stopped, a disk that is attached to nothing, an address bound to nothing),
oldest first. A listing that could not be read is reported as unknown and never as empty. A resource whose id a
project lists in its RESOURCES.md is marked known, with that project's code (`known_ids`). Dedicated ELB (v3) is
not swept yet: no shape of its API is in the repository (NOT_SWEPT, the report says so).

T-97. The name or the id of a live resource can carry a name (an environment code, an account name, a key pair
file). What a session reads carries a neutral handle instead: `ecs-1`, `evs-3`, `eip-2`. The handles live in
`<shared>/handles.json` (mode 600) with the id they stand for; only the tool that makes a call resolves a handle
(`resolve`) and every text it prints goes through `mask` first. The resource names are never kept.
"""
from __future__ import annotations

import datetime
import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from awb import config, jobs

PROJECT_TAG = "awb-project"
EXPIRY_TAG = "awb-expiry"
HANDLES_FILE = "handles.json"
_HANDLE_RE = re.compile(r"^(?P<kind>[a-z]{2,5})-(?P<n>[1-9][0-9]{0,5})$")

# (kind, service, path, key of the list, paging)
SOURCES = (
    ("ecs", "ecs", "/v1/{project_id}/cloudservers/detail", "servers", "offset"),
    ("evs", "evs", "/v2/{project_id}/cloudvolumes/detail", "volumes", "offset"),
    ("eip", "vpc", "/v1/{project_id}/publicips", "publicips", "marker"),
)
# (kind, service, path, key of the list, paging, query, project header): the network resources. The paths are the
# ones the lab recipes and the lease check call (awb/tcp/leasecheck.py sends the ELB and NAT v2.0 calls with the
# X-Project-Id header); the list keys of subnets, security groups, listeners, pools and NAT gateways and their paging
# are to confirm live
NETWORK_SOURCES = (
    ("vpc", "vpc", "/v1/{project_id}/vpcs", "vpcs", "marker", {}, False),
    ("sub", "vpc", "/v1/{project_id}/subnets", "subnets", "marker", {}, False),
    ("sg", "vpc", "/v1/{project_id}/security-groups", "security_groups", "marker", {}, False),
    ("elb", "elb", "/v2.0/lbaas/loadbalancers", "loadbalancers", "marker", {}, True),
    ("lsn", "elb", "/v2.0/lbaas/listeners", "listeners", "marker", {}, True),
    ("pool", "elb", "/v2.0/lbaas/pools", "pools", "marker", {}, True),
    ("nat", "nat", "/v2.0/nat_gateways", "nat_gateways", "marker", {}, True),
)
ALL_SOURCES = tuple(s + ({}, False) for s in SOURCES) + NETWORK_SOURCES
NOT_SWEPT = ("dedicated ELB load balancers, listeners and pools (ELB v3): no shape of its API is in the repository "
             "yet, to confirm live",)
IDLE_STATES = {"ecs": ("SHUTOFF",), "evs": ("available",), "eip": ("DOWN",)}
_LAB_NAME_RE = re.compile(r"^(?P<code>(?:tcp|hcs)-[a-z2-7]{4})-lab-(?P<y>\d{4})(?P<m>\d{2})(?P<d>\d{2})-[a-z]{4}"
                          r"(?:-[a-z0-9]{1,8})?$")


class SweepError(Exception):
    """The handle file cannot be read or written. The message carries no value."""


# --------------------------------------------------------------------------- T-97, neutral handles


class Handles:
    """Handles of one Workbench: kind-n for every id met, the same handle for the same id every time."""

    def __init__(self, path: Path):
        self.path = Path(path)
        self.by_id: dict[str, str] = {}
        self.ids: dict[str, str] = {}
        self.names: dict[str, str] = {}      # name -> handle, in memory only, never written
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            data = {}
        except (OSError, ValueError):
            raise SweepError("the handle file cannot be read") from None
        for handle, rid in (data.get("handles") or {}).items() if isinstance(data, dict) else ():
            if _HANDLE_RE.match(handle) and isinstance(rid, str) and rid:
                self.ids[handle] = rid
                self.by_id[rid] = handle

    def handle(self, kind: str, rid: str, name: str = "") -> str:
        h = self.by_id.get(rid)
        if h is None:
            n = 1 + max([int(x.split("-", 1)[1]) for x in self.ids if x.startswith(kind + "-")] + [0])
            h = "%s-%d" % (kind, n)
            self.ids[h] = rid
            self.by_id[rid] = h
        if name and len(name) >= 3:
            self.names[name] = h
        return h

    def rebind(self, handle: str, rid: str) -> None:
        """The handle given before an id was known (a lab create) now stands for that id."""
        old = self.ids.get(handle)
        if old is not None and self.by_id.get(old) == handle:
            del self.by_id[old]
        self.ids[handle] = rid
        self.by_id[rid] = handle

    def drop(self, handle: str) -> None:
        """Forget a handle that never stood for a resource (a create the platform refused)."""
        rid = self.ids.pop(handle, None)
        if rid is not None and self.by_id.get(rid) == handle:
            del self.by_id[rid]
        for name in [n for n, h in self.names.items() if h == handle]:
            del self.names[name]

    def resolve(self, handle: str) -> str:
        """The id a handle stands for: only for the tool that makes the call."""
        rid = self.ids.get(handle)
        if rid is None:
            raise SweepError("no resource has that handle")
        return rid

    def mask(self, text: str) -> str:
        """`text` with every known id and every name met in this run replaced by its handle, longest first."""
        pairs = sorted(list(self.by_id.items()) + list(self.names.items()), key=lambda kv: -len(kv[0]))
        for value, handle in pairs:
            if value:
                text = text.replace(value, handle)
        return text

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_name("." + self.path.name + ".tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump({"handles": dict(sorted(self.ids.items()))}, fh, indent=1)
            fh.write("\n")
        os.replace(tmp, self.path)


def handles_path(p: config.Paths) -> Path:
    return p.shared / HANDLES_FILE


# --------------------------------------------------------------------------- T-61, the sweep


@dataclass
class Item:
    handle: str
    kind: str
    created: str
    state: str
    project: str
    expiry: str
    reasons: list[str] = field(default_factory=list)
    known: str = ""              # the project whose RESOURCES.md lists the id, empty when none does


@dataclass
class SweepReport:
    items: list[Item]
    unknown: list[str]           # kinds whose listing could not be read
    counts: dict[str, int]
    not_swept: list[str] = field(default_factory=list)      # kinds the sweep cannot list yet, with the reason


def tags(raw) -> dict[str, str]:
    """Tags as a dict from the shapes the services use: a dict, a list of "key=value" or of {key, value}."""
    out: dict[str, str] = {}
    if isinstance(raw, dict):
        return {str(k): str(v) for k, v in raw.items()}
    if isinstance(raw, list):
        for t in raw:
            if isinstance(t, str) and "=" in t:
                k, _, v = t.partition("=")
                out[k.strip()] = v.strip()
            elif isinstance(t, dict) and "key" in t:
                out[str(t["key"])] = str(t.get("value", ""))
    return out


_tags = tags    # the readers below have a local named tags; the web adapters deployed outside use it too


def _created(item: dict) -> str:
    for key in ("created", "created_at", "create_time"):
        v = item.get(key)
        if isinstance(v, str) and v:
            return v
    return ""


def _state(kind: str, item: dict) -> str:
    return str(item.get("status") or item.get("provisioning_status") or "")


def name_tags(name: str) -> dict[str, str]:
    """The project and expiry tags a lab resource carries in its name (`<code>-lab-<YYYYMMDD>-<rand>`), else none."""
    m = _LAB_NAME_RE.match(name or "")
    if not m:
        return {}
    return {PROJECT_TAG: m.group("code"), EXPIRY_TAG: "%s-%s-%s" % (m.group("y"), m.group("m"), m.group("d"))}


def known_ids(projects_root: Path) -> dict[str, str]:
    """{platform id: project code} of every id in the id column of the RESOURCES.md of a project (a folder with
    SCOPE.md) under `projects_root`. A file that cannot be read adds nothing."""
    out: dict[str, str] = {}
    try:
        folders = sorted(Path(projects_root).iterdir())
    except OSError:
        return out
    for folder in folders:
        try:
            if not (folder / "SCOPE.md").is_file():
                continue
            text = (folder / "RESOURCES.md").read_text(encoding="utf-8")
        except OSError:
            continue
        head: list[str] | None = None
        for line in text.splitlines():
            if not line.strip().startswith("|"):
                continue
            cells = [c.strip() for c in line.strip().strip("|").split("|")]
            if head is None:
                head = [c.lower() for c in cells]
                continue
            rid = dict(zip(head, cells)).get("id", "").strip("`")
            if rid and not set(rid) <= set("-: "):
                out.setdefault(rid, folder.name)
    return out


def sweep(lister: Callable[..., jobs.Listing], today: datetime.date, handles: Handles,
          known: dict[str, str] | None = None, sources=SOURCES) -> SweepReport:
    """List every source and judge each resource. `lister(service, path, key, paging)` returns a Listing (the
    cloud client's list call or a stand-in in the tests); a source with a query or the project header gets them as
    `query=` and `project_header=`. `known` is `known_ids()`: a listed resource is marked with the project that
    knows it; when `known` is given, a tagged resource that no project lists is named. `sources` is SOURCES or
    ALL_SOURCES (the command line)."""
    listed = known is not None
    known = known or {}
    items: list[Item] = []
    unknown: list[str] = []
    for source in sources:
        kind, service, path, key, paging = source[:5]
        query, project_header = (source[5], source[6]) if len(source) > 5 else ({}, False)
        if query or project_header:
            listing = lister(service, path, key, paging, query=dict(query), project_header=project_header)
        else:
            listing = lister(service, path, key, paging)
        if listing.state == jobs.UNKNOWN:
            unknown.append(kind)
            continue
        for raw in listing.items:
            if not isinstance(raw, dict) or not isinstance(raw.get("id"), str):
                continue
            tags = _tags(raw.get("tags") if "tags" in raw else raw.get("metadata"))
            name = raw.get("name") if isinstance(raw.get("name"), str) else ""
            if PROJECT_TAG not in tags:
                tags.update(name_tags(name))
            h = handles.handle(kind, raw["id"], name)
            project = tags.get(PROJECT_TAG, "")
            expiry = tags.get(EXPIRY_TAG, "")
            reasons = []
            if not project:
                reasons.append("untagged")
            elif listed and raw["id"] not in known:
                reasons.append("not in RESOURCES.md")
            if expiry:
                try:
                    if datetime.date.fromisoformat(expiry) < today:
                        reasons.append("expired")
                except ValueError:
                    reasons.append("expiry unreadable")
            elif project:
                reasons.append("no expiry")
            state = _state(kind, raw)
            if state in IDLE_STATES.get(kind, ()):
                reasons.append("idle")
            if reasons:
                items.append(Item(h, kind, _created(raw), state, project, expiry, reasons, known.get(raw["id"], "")))
    items.sort(key=lambda i: (i.created or "9999", i.handle))
    counts: dict[str, int] = {}
    for i in items:
        for r in i.reasons:
            counts[r] = counts.get(r, 0) + 1
    not_swept = list(NOT_SWEPT) if any(src in NETWORK_SOURCES for src in sources) else []
    return SweepReport(items, unknown, counts, not_swept)


def report_lines(rep: SweepReport) -> list[str]:
    out = ["%-8s  %-5s  %-20s  %-10s  %-10s  %-10s  %-10s  %s" % ("handle", "kind", "created", "state", "project",
                                                                  "expiry", "known", "why")]
    for i in rep.items:
        out.append("%-8s  %-5s  %-20s  %-10s  %-10s  %-10s  %-10s  %s" % (i.handle, i.kind, i.created[:19], i.state,
                                                                          i.project or "-", i.expiry or "-",
                                                                          i.known or "-", ", ".join(i.reasons)))
    summary = ", ".join("%s %d" % kv for kv in sorted(rep.counts.items())) or "nothing to report"
    out.append("sweep: %d resource(s): %s" % (len(rep.items), summary))
    if rep.unknown:
        out.append("sweep: could not read %s: the listing is unknown, not empty" % ", ".join(rep.unknown))
    for what in rep.not_swept:
        out.append("sweep: not swept: %s" % what)
    return out
