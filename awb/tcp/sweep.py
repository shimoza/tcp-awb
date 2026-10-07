"""Package 7, the cloud side: the resource sweep (T-61) and neutral handles for live identifiers (T-97).

    awb cloud sweep [--region R] [--today YYYY-MM-DD]     read-only: GET list calls only

T-61. A resource the Workbench creates carries two tags: `awb-project` (the project code, tcp-xxxx) and
`awb-expiry` (an ISO date). The sweep lists servers, disks and elastic IPs and names every one that is
untagged (no project tag), expired (its expiry date has passed) or idle (a server that is stopped, a disk that is
attached to nothing, an address bound to nothing), oldest first. A listing that could not be read is reported as
unknown and never as empty. A resource whose id a project lists in its RESOURCES.md is marked known, with that
project's code (`known_ids`).

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
IDLE_STATES = {"ecs": ("SHUTOFF",), "evs": ("available",), "eip": ("DOWN",)}


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
    return str(item.get("status") or "")


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


def sweep(lister: Callable[[str, str, str, str], jobs.Listing], today: datetime.date, handles: Handles,
          known: dict[str, str] | None = None) -> SweepReport:
    """List every source and judge each resource. `lister(service, path, key, paging)` returns a Listing (the
    cloud client's list call, or a stand-in in the tests). `known` is `known_ids()`: a listed resource is marked
    with the project that knows it."""
    known = known or {}
    items: list[Item] = []
    unknown: list[str] = []
    for kind, service, path, key, paging in SOURCES:
        listing = lister(service, path, key, paging)
        if listing.state == jobs.UNKNOWN:
            unknown.append(kind)
            continue
        for raw in listing.items:
            if not isinstance(raw, dict) or not isinstance(raw.get("id"), str):
                continue
            tags = _tags(raw.get("tags") if "tags" in raw else raw.get("metadata"))
            name = raw.get("name") if isinstance(raw.get("name"), str) else ""
            h = handles.handle(kind, raw["id"], name)
            project = tags.get(PROJECT_TAG, "")
            expiry = tags.get(EXPIRY_TAG, "")
            reasons = []
            if not project:
                reasons.append("untagged")
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
    return SweepReport(items, unknown, counts)


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
    return out
