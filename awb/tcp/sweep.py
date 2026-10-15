"""Package 7, the cloud side: the resource sweep (T-61) and neutral handles for live identifiers (T-97).

    awb cloud sweep [--region R] [--today YYYY-MM-DD] [--buckets]     read-only: GET list calls only

T-61. A resource the Workbench creates carries two tags: `awb-project` (the project code, tcp-xxxx) and
`awb-expiry` (an ISO date). A service that takes no tags carries both in the name the lab gives it
(`<code>-lab-<YYYYMMDD>-<4 letters>`, awb/tcp/lab.py), which the sweep reads in the place of the tags. The sweep
lists servers, disks and elastic IPs (SOURCES) and on the command line also VPCs, subnets, security groups, shared
ELB load balancers, listeners and pools and NAT gateways (NETWORK_SOURCES), with `--buckets` also the OBS buckets
of the region (OBS_SOURCES, their tags from GET /BUCKET?tagging). It names every one that is untagged (no
project tag), expired (its expiry date has passed), not in RESOURCES.md (it carries a project tag and no project
lists its id) or idle (a server that is stopped, a disk that is attached to nothing, an address bound to nothing),
oldest first. A listing that could not be read is reported as unknown and never as empty. A resource whose id a
project lists in its RESOURCES.md is marked known, with that project's code (`known_ids`). Dedicated ELB (v3) is
not swept yet: no shape of its API is in the repository (NOT_SWEPT, the report says so).

Named sources (fact-check design F3): WAF policies, SMN topics and subscriptions and RDS instances take no tags the
sweep can read, so it finds the Workbench's own by the project code in their name (NAMED_SOURCES: the id field and
the name field of each; a subscription has no name and is found by the name of its topic in its topic URN). A
resource of these types whose name carries no project code is not the Workbench's and is not reported. `inventory`
lists every source and gives per type the count and the sha256 digest of the sorted id set (unknown when the
listing cannot be read); `inventory_diff` compares two of them with counts only, never an id. A recheck run takes it
before and after a recipe.

T-97. The name or the id of a live resource can carry a name (an environment code, an account name, a key pair
file). What a session reads carries a neutral handle instead: `ecs-1`, `evs-3`, `eip-2`. The handles live in
`<shared>/handles.json` (mode 600) with the id they stand for; only the tool that makes a call resolves a handle
(`resolve`) and every text it prints goes through `mask` first. The resource names are never kept. On a sealed
host that file belongs to the work user; when the owner's `awb cloud sweep` cannot read or write it, his handles go
to `$XDG_STATE_HOME/awb/handles.json` (default `~/.local/state/awb/`) and the sweep says so in one line
(`open_handles`).
"""
from __future__ import annotations

import datetime
import hashlib
import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from awb import codes, config, jobs

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
# (kind, service, path, key of the list, paging, query, project header, id field, name field): the services without
# tags the sweep reads, found by the project code in the name. Paths, keys and paging are to confirm live
NAMED_SOURCES = (
    ("waf", "waf", "/v1/{project_id}/waf/policy", "items", "none", {"pagesize": "100"}, False, "id", "name"),
    ("smn", "smn", "/v2/{project_id}/notifications/topics", "topics", "offset", {}, False, "topic_urn", "name"),
    ("smns", "smn", "/v2/{project_id}/notifications/subscriptions", "subscriptions", "offset", {}, False,
     "subscription_urn", "topic_urn"),
    ("rds", "rds", "/v3/{project_id}/instances", "instances", "offset", {}, False, "id", "name"),
)
# the OBS buckets of the region (`awb cloud sweep --buckets`): the lister answers them with Client.buckets, the
# bucket's name as its id and the tags of GET /BUCKET?tagging, one call per bucket
OBS_SOURCES = (("obs", "obs", "/", "buckets", "none", {}, False),)
NOT_SWEPT = ("dedicated ELB load balancers, listeners and pools (ELB v3): no shape of its API is in the repository "
             "yet, to confirm live",)
NOT_SWEPT_OBS = "OBS buckets: awb cloud sweep --buckets sweeps them, with one call per bucket for its tags"
IDLE_STATES = {"ecs": ("SHUTOFF",), "evs": ("available",), "eip": ("DOWN",)}
_CODE_IN_NAME_RE = re.compile(r"(?<![a-z0-9])" + codes.PROJECT + r"(?![a-z0-9])")
_LAB_NAME_RE = re.compile(r"^(?P<code>" + codes.PROJECT + r")-lab-(?P<y>\d{4})(?P<m>\d{2})(?P<d>\d{2})-[a-z]{4}"
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
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_name("." + self.path.name + ".tmp")
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump({"handles": dict(sorted(self.ids.items()))}, fh, indent=1)
                fh.write("\n")
            os.replace(tmp, self.path)
        except OSError:
            raise SweepError("the handle file cannot be written") from None


def handles_path(p: config.Paths) -> Path:
    return p.shared / HANDLES_FILE


def owner_handles_path() -> Path:
    """The handle file of a user who may not write the shared one (the owner on a sealed host)."""
    base = os.environ.get("XDG_STATE_HOME") or str(Path.home() / ".local" / "state")
    return Path(base) / "awb" / HANDLES_FILE


def _writable(path: Path) -> bool:
    if path.exists():
        return os.access(path, os.R_OK | os.W_OK) and os.access(path.parent, os.W_OK | os.X_OK)
    parent = next((x for x in path.parents if x.exists()), None)
    return parent is not None and os.access(parent, os.W_OK | os.X_OK)


def open_handles(p: config.Paths) -> tuple[Handles, str]:
    """The shared handles when this user may read and write them, else this user's own (`owner_handles_path`).
    Returns the handles and a line that says where they are when they are not the shared ones ("" otherwise).
    Raises SweepError when neither can be used."""
    shared = handles_path(p)
    if _writable(shared):
        try:
            return Handles(shared), ""
        except SweepError:
            pass
    own = owner_handles_path()
    if not _writable(own):
        raise SweepError("neither the shared handle file nor this user's own can be written")
    return Handles(own), ("the shared handle file is not this user's: the handles of this sweep are this user's "
                          "own (XDG_STATE_HOME/awb), their numbers differ from the shared ones")


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
        kind = source[0]
        listing = _list(lister, source)
        if listing.state == jobs.UNKNOWN:
            unknown.append(kind)
            continue
        named = len(source) > 7
        for raw in listing.items:
            rid, name = _id_and_name(source, raw)
            if rid is None:
                continue
            if named:
                code = _CODE_IN_NAME_RE.search(name)
                if code is None:
                    continue           # not the Workbench's: these services carry no tag to tell
                tags = name_tags(name) or {PROJECT_TAG: code.group(0)}
            else:
                tags = _tags(raw.get("tags") if "tags" in raw else raw.get("metadata"))
                if PROJECT_TAG not in tags:
                    tags.update(name_tags(name))
            h = handles.handle(kind, rid, name)
            project = tags.get(PROJECT_TAG, "")
            expiry = tags.get(EXPIRY_TAG, "")
            reasons = []
            if not project:
                reasons.append("untagged")
            elif listed and rid not in known:
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
                items.append(Item(h, kind, _created(raw), state, project, expiry, reasons, known.get(rid, "")))
    items.sort(key=lambda i: (i.created or "9999", i.handle))
    counts: dict[str, int] = {}
    for i in items:
        for r in i.reasons:
            counts[r] = counts.get(r, 0) + 1
    not_swept = list(NOT_SWEPT) if any(src in NETWORK_SOURCES for src in sources) else []
    if not_swept and not any(src in OBS_SOURCES for src in sources):
        not_swept.append(NOT_SWEPT_OBS)
    return SweepReport(items, unknown, counts, not_swept)


def _list(lister, source) -> jobs.Listing:
    kind, service, path, key, paging = source[:5]
    query, project_header = (source[5], source[6]) if len(source) > 5 else ({}, False)
    if query or project_header:
        return lister(service, path, key, paging, query=dict(query), project_header=project_header)
    return lister(service, path, key, paging)


def _id_and_name(source, raw) -> tuple[str | None, str]:
    """The id and the name of a listed item; a named source names its fields, a URN gives its last part."""
    if not isinstance(raw, dict):
        return None, ""
    id_field, name_field = (source[7], source[8]) if len(source) > 7 else ("id", "name")
    rid = raw.get(id_field)
    name = raw.get(name_field) if isinstance(raw.get(name_field), str) else ""
    if name.startswith("urn:"):
        name = name.rsplit(":", 1)[-1]
    return (rid if isinstance(rid, str) and rid else None), name


def digest(ids) -> str:
    """The sha256 of the sorted id set: equal sets give equal digests, whatever the order of the listing."""
    return hashlib.sha256("\n".join(sorted(set(ids))).encode("utf-8")).hexdigest()


def inventory(lister: Callable[..., jobs.Listing], sources=ALL_SOURCES + NAMED_SOURCES
              ) -> dict[str, tuple[int, str] | None]:
    """{kind: (count, digest of the id set)} of every source, None for a listing that cannot be read (an unknown
    listing proves no equality). Every item counts, the Workbench's or not: the inventory proves that a run left
    the tenant as it found it."""
    out: dict[str, tuple[int, str] | None] = {}
    for source in sources:
        listing = _list(lister, source)
        if listing.state == jobs.UNKNOWN:
            out[source[0]] = None
            continue
        ids = {rid for rid, _ in (_id_and_name(source, raw) for raw in listing.items) if rid is not None}
        out[source[0]] = (len(ids), digest(ids))
    return out


def inventory_lines(inv: dict[str, tuple[int, str] | None]) -> list[str]:
    """One line per type for inventory-before.txt and inventory-after.txt: kind, count and digest or unknown."""
    return ["%s %s" % (kind, "unknown" if v is None else "%d %s" % v) for kind, v in sorted(inv.items())]


def inventory_diff(before: dict, after: dict) -> list[str]:
    """What differs between two inventories, per type and in counts only ("TYPE +n/-n", "TYPE unknown"); empty when
    every type is known in both and its digest is equal."""
    out = []
    for kind in sorted(set(before) | set(after)):
        a, b = before.get(kind), after.get(kind)
        if a is None or b is None:
            out.append("%s unknown" % kind)
        elif a[1] != b[1]:
            more, fewer = max(b[0] - a[0], 0), max(a[0] - b[0], 0)
            out.append("%s +%d/-%d" % (kind, more, fewer) if more or fewer else "%s other ids" % kind)
    return out


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
