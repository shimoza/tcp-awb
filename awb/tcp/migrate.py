"""A migration to T Cloud Public (TCP) in phases, with its state on disk (the migration skill, first source Azure).

The phases run in order. Each has a phase file (`plugin/skills/azure-to-tcp/phases/<n>-<name>.md`) that tells the
worker what to do, a worker tier and a gate. The orchestrator (the session that runs the skill) runs the
deterministic steps with the commands below and hands the judgement to a worker in a fresh context: the `files`
worker reads and writes files only, the `shell` worker may also run commands. The gate is code: `awb migrate done
PHASE` checks the phase's artifacts and records their hashes. When an artifact of a finished phase changes later,
that phase and every phase after it turn stale and must be done again, so a run resumes where it is still valid.

    <project>/migration/state.json       the phases, their state and the hashes of their artifacts
    <project>/migration/inventory.json   `awb migrate inventory`: the source machines, one row each (R-1, R-2 ...)
    <project>/migration/mapping.tsv      `awb migrate map`: per row the TCP flavor, the rule and a status
    <project>/migration/estimate-T.tsv   `awb migrate estimate --term T`: per row the price record and line total

Commands (`awb migrate ...`, inside a project):

    init --from azure          start a migration in the project
    status                     every phase, its state and the next step
    next                       the next phase: its phase file, its worker, what it reads and what it must write
    inventory FILE [--table N] read the machines from a CSV or TSV file or a table of a sanitised copy (intake
                               turns a spreadsheet into Markdown tables)
    map [--region R]           each machine to the nearest TCP flavor that is not smaller, from the live price API
    estimate [--term T] [--hours H]
                               the monthly price per row and the totals, computed by awb calc (R-005)
    done PHASE                 run the gate of a phase and record it

The mapping is code, never a guess. The Azure side of a row comes from the inventory (vCPU and memory) and from
Microsoft's naming convention of VM sizes (family, vCPU count, constrained vCPUs, see AZURE_SOURCES). The TCP side
comes from the live price API, which lists vCPU, RAM and the category of every flavor. A price record does not
prove that a flavor can still be ordered (KB-XELM): every mapping says so until the plan phase checks it.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import io
import json
import os
import re
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, DecimalException
from pathlib import Path
from typing import Callable

from awb import calc

FOLDER = "migration"
STATE = "state.json"
INVENTORY = "inventory.json"
MAPPING = "mapping.tsv"
ESTIMATE = "estimate-%s.tsv"      # one per term: estimate-PAYG.tsv, estimate-R12.tsv
SOURCES = ("azure",)
HOURS_PER_MONTH = 720      # the public price calculator counts 720 hours a month (KB-YNDP)
MAX_ROWS = 5000
PHASE_DIR = Path(__file__).resolve().parents[2] / "plugin" / "skills" / "azure-to-tcp" / "phases"


class MigrateError(Exception):
    """A step that cannot run. The message names a reason, a row id or a column, never a cell value."""


# --------------------------------------------------------------------------- the phases


@dataclass(frozen=True)
class Phase:
    name: str
    worker: str               # files | shell | none (the orchestrator alone)
    reads: tuple[str, ...]
    makes: tuple[str, ...]


PHASES = (
    Phase("discover", "files", ("input/", "migration/inventory.json"), ("migration/inventory.json",
                                                                        "migration/discover.md")),
    Phase("map", "files", ("migration/inventory.json", "migration/mapping.tsv", "migration/discover.md"),
          ("migration/mapping.tsv", "migration/map-notes.md")),
    Phase("estimate", "shell", ("migration/mapping.tsv", "migration/map-notes.md"),
          ("migration/estimate-notes.md",)),
    Phase("plan", "files", ("migration/discover.md", "migration/map-notes.md", "migration/estimate-*.tsv",
                            "migration/estimate-notes.md"), ("deliverables/migration-plan.md",)),
    Phase("review", "none", ("deliverables/migration-plan.md",), ("deliverables/migration-plan.md",)),
)
PHASE_NAMES = tuple(p.name for p in PHASES)
WORKERS = {"files": "awb:migration-worker-files", "shell": "awb:migration-worker-shell", "none": None}
PENDING, DONE, STALE = "pending", "done", "stale"


def phase_file(name: str) -> Path:
    n = PHASE_NAMES.index(name) + 1
    return PHASE_DIR / ("%d-%s.md" % (n, name))


def _folder(project: Path) -> Path:
    return Path(project) / FOLDER


def _sha(path: Path) -> str | None:
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return None


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.is_symlink():
        raise MigrateError("%s is a link" % path.name)
    tmp = path.with_name(".%s.tmp" % path.name)
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


def init(project: Path, source: str) -> dict:
    if source not in SOURCES:
        raise MigrateError("the source must be one of: %s" % ", ".join(SOURCES))
    if not (Path(project) / "SCOPE.md").is_file():
        raise MigrateError("not inside a project (no SCOPE.md)")
    path = _folder(project) / STATE
    if path.exists():
        raise MigrateError("a migration is already started in this project")
    state = {"source": source, "created": _now(), "phases": [
        {"name": p.name, "state": PENDING, "done_at": None, "artifacts": {}} for p in PHASES]}
    _write(path, json.dumps(state, indent=1) + "\n")
    return state


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def load_state(project: Path) -> dict:
    path = _folder(project) / STATE
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise MigrateError("no migration in this project (awb migrate init --from azure)") from None
    except (OSError, ValueError):
        raise MigrateError("the migration state cannot be read") from None
    if [p.get("name") for p in state.get("phases", [])] != list(PHASE_NAMES):
        raise MigrateError("the migration state does not list the phases in order")
    return _freshen(project, state)


def _freshen(project: Path, state: dict) -> dict:
    """A done phase whose artifacts changed turns stale, and so does every phase after it."""
    stale = False
    for p in state["phases"]:
        if stale and p["state"] == DONE:
            p["state"] = STALE
        elif p["state"] == DONE:
            for rel, sha in p["artifacts"].items():
                if _sha(Path(project) / rel) != sha:
                    p["state"] = STALE
                    break
        if p["state"] == STALE:
            stale = True
    return state


def next_phase(state: dict) -> dict | None:
    for p in state["phases"]:
        if p["state"] != DONE:
            return p
    return None


def done(project: Path, name: str) -> list[str]:
    """Run the gate of phase `name`. Returns the problems; when there are none the phase is recorded as done."""
    if name not in PHASE_NAMES:
        raise MigrateError("the phase must be one of: %s" % ", ".join(PHASE_NAMES))
    state = load_state(project)
    nxt = next_phase(state)
    if nxt is None or nxt["name"] != name:
        if state["phases"][PHASE_NAMES.index(name)]["state"] == DONE:
            return ["the phase %s is done already" % name]
        return ["the phase %s comes after %s, which is not done" % (name, nxt["name"] if nxt else "-")]
    problems = GATES[name](Path(project))
    if problems:
        return problems
    phase = PHASES[PHASE_NAMES.index(name)]
    entry = state["phases"][PHASE_NAMES.index(name)]
    entry.update(state=DONE, done_at=_now(),
                 artifacts={rel: _sha(Path(project) / rel) for rel in phase.makes})
    _write(_folder(project) / STATE, json.dumps(state, indent=1) + "\n")
    return []


# --------------------------------------------------------------------------- the inventory

_HEADERS = {
    "name": ("name", "vm name", "vm", "server", "server name", "hostname", "host", "machine", "machine name",
             "computer name", "display name"),
    "size": ("size", "vm size", "sku", "azure size", "instance type", "azure vm size", "vm sku",
             "recommended size", "target size"),
    "vcpu": ("vcpu", "vcpus", "cores", "cpu", "cpus", "number of cores", "cpu cores", "vcpu count", "processors"),
    "memory": ("memory", "ram", "memory gb", "memory gib", "ram gb", "ram gib", "memory mb", "memory mib",
               "ram mb", "ram mib", "memory in gb", "memory in mb", "allocated memory"),
    "os": ("os", "operating system", "os type", "os name", "os version", "guest os"),
    "disk": ("disk", "disk gb", "disks gb", "storage", "storage gb", "total disk", "total disk gb",
             "disk size", "disk size gb", "provisioned storage", "storage in gb"),
    "count": ("count", "quantity", "qty", "number", "instances"),
}
_MB_HINT = re.compile(r"\b(?:mb|mib)\b")


def _norm_header(h: str) -> str:
    return " ".join(re.sub(r"[^a-z0-9]+", " ", h.lower()).split())


def _columns(header: list[str]) -> tuple[dict[str, int], bool]:
    """Column index per field, and whether the memory column is in MB."""
    cols: dict[str, int] = {}
    mem_mb = False
    for i, raw in enumerate(header):
        h = _norm_header(raw)
        for field_, names in _HEADERS.items():
            if field_ not in cols and h in names:
                cols[field_] = i
                if field_ == "memory" and _MB_HINT.search(h):
                    mem_mb = True
    return cols, mem_mb


def _tables_of_markdown(text: str) -> list[list[list[str]]]:
    tables: list[list[list[str]]] = []
    current: list[list[str]] = []
    for line in text.splitlines():
        s = line.strip()
        if s.startswith("|") and s.endswith("|") and len(s) > 1:
            cells = [c.strip().replace("\\|", "|") for c in re.split(r"(?<!\\)\|", s[1:-1])]
            if all(re.fullmatch(r":?-{3,}:?", c) for c in cells if c) and any(cells):
                continue
            current.append(cells)
        else:
            if current:
                tables.append(current)
            current = []
    if current:
        tables.append(current)
    return tables


def read_tables(path: Path) -> list[list[list[str]]]:
    """The tables of a file: a CSV or TSV file is one table, a Markdown file (a sanitised copy) has one per block."""
    try:
        text = Path(path).read_text(encoding="utf-8-sig")
    except (OSError, UnicodeDecodeError):
        raise MigrateError("the inventory file cannot be read as text") from None
    if Path(path).suffix.lower() in (".csv", ".tsv", ".txt"):
        sample = text[:4096]
        delim = "\t" if "\t" in sample else (";" if sample.count(";") > sample.count(",") else ",")
        return [[row for row in csv.reader(io.StringIO(text), delimiter=delim) if any(c.strip() for c in row)]]
    return _tables_of_markdown(text)


def _decimal(text: str) -> Decimal | None:
    s = (text or "").strip().lower()
    s = re.sub(r"\s*(?:gib|gb|mib|mb|vcpus?|cores?|x)\s*$", "", s)
    if not s:
        return None
    if re.fullmatch(r"[0-9]+,[0-9]+", s):
        s = s.replace(",", ".")
    s = s.replace(",", "")
    try:
        v = Decimal(s)
    except DecimalException:
        return None
    return v if v.is_finite() and v >= 0 else None


# Microsoft's naming convention of VM sizes: [Family] + [Sub-family]* + [# of vCPUs] + [Constrained vCPUs]* +
# [Additive Features] + [Accelerator Type]* + [Memory Capacity]* + [Version]
AZURE_SOURCES = {
    "naming": "https://learn.microsoft.com/en-us/azure/virtual-machines/vm-naming-conventions",
    "families": "https://learn.microsoft.com/en-us/azure/virtual-machines/sizes/overview",
    "checked": "2026-09-30",
}
_AZURE_SIZE_RE = re.compile(
    r"^(?:standard_|basic_)?(?P<family>[A-Z]{1,3}?)(?P<vcpu>[0-9]{1,3})(?:-(?P<constrained>[0-9]{1,3}))?"
    r"(?P<features>[a-z]*)(?:_(?P<rest>[A-Za-z0-9_]*))?$", re.I)

# Azure family -> (the class Microsoft gives it, the TCP class it maps to, a flag or "")
# TCP classes by the service names of the price API: ecsnoc is labelled "Dedicated General Purpose" (c*), ecs
# "General Purpose" (s*), memo "Memory Optimized" (m*), lms and sap "Large Memory", dins "Disk Intensive", gpu "GPU
# accelerated".
AZURE_FAMILIES = {
    "A": ("general purpose, entry-level economical", "general", ""),
    "B": ("general purpose, burstable with CPU credits", "general",
          "B is the only Azure family with CPU credits: size by the CPU the machine really uses"),
    "D": ("general purpose, fixed CPU performance", "general-dedicated", ""),
    "DS": ("general purpose, fixed CPU performance (older naming)", "general-dedicated", ""),
    "DC": ("general purpose with confidential computing", "manual",
           "confidential computing: check what the workload needs from it before a flavor is chosen"),
    "F": ("compute optimized", "general-dedicated", ""),
    "FX": ("compute optimized with large memory", "memory", "FX is compute optimized with a high memory ratio"),
    "E": ("memory optimized", "memory", ""),
    "EC": ("memory optimized with confidential computing", "manual",
           "confidential computing: check what the workload needs from it before a flavor is chosen"),
    "M": ("memory optimized, extremely large databases", "large-memory",
          "very large memory: check that the flavor can be ordered in the region"),
    "L": ("storage optimized, local disks", "disk-intensive",
          "the local disks are why an L size is chosen: keep them on the TCP side"),
    "NC": ("GPU accelerated", "manual", "GPU: match the GPU model and its count, not only vCPU and memory"),
    "ND": ("GPU accelerated", "manual", "GPU: match the GPU model and its count, not only vCPU and memory"),
    "NG": ("GPU accelerated", "manual", "GPU: match the GPU model and its count, not only vCPU and memory"),
    "NV": ("GPU accelerated", "manual", "GPU: match the GPU model and its count, not only vCPU and memory"),
    "NP": ("FPGA accelerated", "manual", "FPGA: no like-for-like mapping by vCPU and memory"),
    "HB": ("high performance compute", "manual", "HPC: decide with the workload owner"),
    "HC": ("high performance compute", "manual", "HPC: decide with the workload owner"),
    "HX": ("high performance compute", "manual", "HPC: decide with the workload owner"),
}
# GPU sizes are decided by hand (the GPU model matters more than vCPU and memory), so gpu has no Azure family yet.
TCP_CLASSES = {
    "general-dedicated": ("ecsnoc",),
    "general": ("ecs",),
    "memory": ("memo",),
    "large-memory": ("lms", "sap"),
    "disk-intensive": ("dins",),
    "gpu": ("gpu",),
}
ALTERNATIVE = {"general-dedicated": "general"}


def parse_azure_size(size: str) -> dict | None:
    """Family, vCPU, constrained vCPUs and features of an Azure size name, or None when it does not parse."""
    m = _AZURE_SIZE_RE.match((size or "").strip())
    if not m:
        return None
    letters = m.group("family").upper()
    family = next((f for f in sorted(AZURE_FAMILIES, key=len, reverse=True) if letters.startswith(f)
                   and len(letters) - len(f) <= 1), None)
    if family is None:
        return None
    features = m.group("features") or ""
    version = re.search(r"(?:^|_)v([0-9]+)$", m.group("rest") or "", re.I)
    if family in ("D", "DS") and (version is None or int(version.group(1)) < 3):
        # D and DS before v3 are numbered by size, not by vCPU (a D3_v2 has 4 vCPUs): the name gives no count
        return {"family": family, "vcpu": None, "constrained": None, "arm": False, "features": features}
    return {"family": family, "vcpu": int(m.group("vcpu")),
            "constrained": int(m.group("constrained")) if m.group("constrained") else None,
            "arm": "p" in features, "features": features}


def inventory(project: Path, source_file: Path, table: int | None = None) -> dict:
    """Read the machines of `source_file` into migration/inventory.json. Returns the inventory."""
    tables = read_tables(source_file)
    picked = None
    for i, t in enumerate(tables, start=1):
        if table is not None and i != table:
            continue
        if not t:
            continue
        cols, _ = _columns(t[0])
        if "name" in cols and ("size" in cols or "vcpu" in cols):
            picked = (i, t)
            break
    if picked is None:
        raise MigrateError("no table with a name column and a size or vCPU column (headers such as VM name, "
                           "Size, vCPU, Memory GB)")
    index, rows = picked
    cols, mem_mb = _columns(rows[0])
    if len(rows) - 1 > MAX_ROWS:
        raise MigrateError("more than %d machines in one table" % MAX_ROWS)
    out = []
    for n, cells in enumerate(rows[1:], start=1):
        def cell(key: str) -> str:
            i = cols.get(key)
            return cells[i].strip() if i is not None and i < len(cells) else ""

        if not any(c.strip() for c in cells):
            continue
        rid = "R-%d" % n
        size = cell("size")
        parsed = parse_azure_size(size) if size else None
        vcpu = _decimal(cell("vcpu"))
        vcpu_source = "inventory" if vcpu is not None else ""
        if parsed and parsed["vcpu"] is not None:
            active = parsed["constrained"] or parsed["vcpu"]
            if vcpu is None:
                vcpu, vcpu_source = Decimal(active), "size name"
        mem = _decimal(cell("memory"))
        if mem is not None and mem_mb:
            mem = mem / 1024
        count = _decimal(cell("count"))
        notes = []
        if size and not parsed:
            notes.append("the size name does not follow the Azure naming convention")
        if parsed and parsed["vcpu"] is None and vcpu is None:
            notes.append("a D or DS size before v3 is numbered by size, not by vCPU: give the vCPU count")
        if parsed and parsed["constrained"]:
            notes.append("a constrained size: %d of %d vCPUs active, the memory of the full size"
                         % (parsed["constrained"], parsed["vcpu"]))
        if parsed and parsed["arm"]:
            notes.append("an Arm size on Azure: check that the software runs on x86")
        out.append({
            "row": rid, "name": cell("name"), "size": size,
            "family": parsed["family"] if parsed else "",
            "vcpu": calc.text(vcpu) if vcpu is not None else "", "vcpu_source": vcpu_source,
            "memory_gib": calc.text(mem) if mem is not None else "",
            "memory_source": "inventory" if mem is not None else "",
            "os": cell("os"), "disk_gb": calc.text(_decimal(cell("disk")) or Decimal(0)) if cell("disk") else "",
            "count": calc.text(count) if count and count == count.to_integral_value() and count > 0 else "1",
            "notes": notes,
        })
    if not out:
        raise MigrateError("the table has no machines")
    doc = {"source": "azure", "file": Path(source_file).name, "table": index, "read": _now(),
           "columns": sorted(cols), "rows": out}
    _write(_folder(project) / INVENTORY, json.dumps(doc, indent=1, ensure_ascii=False) + "\n")
    return doc


def load_inventory(project: Path) -> dict:
    try:
        return json.loads((_folder(project) / INVENTORY).read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise MigrateError("no inventory yet (awb migrate inventory FILE)") from None
    except (OSError, ValueError):
        raise MigrateError("the inventory cannot be read") from None


# --------------------------------------------------------------------------- the mapping


@dataclass(frozen=True)
class Flavor:
    name: str
    service: str
    vcpu: Decimal
    ram: Decimal
    category: str
    linux_payg: Decimal | None
    record_id: str


_RAM_RE = re.compile(r"^\s*([0-9]+(?:\.[0-9]+)?)\s*(GiB|GB|MiB|MB)?\s*$", re.I)


def flavors_from_records(records) -> dict[str, list[Flavor]]:
    """TCP flavors per class from price records (awb.tcp.price.Record), one per flavor name, with the price of its
    open Linux record when there is one."""
    by_class: dict[str, dict[str, Flavor]] = {c: {} for c in TCP_CLASSES}
    service_class = {s: c for c, services in TCP_CLASSES.items() for s in services}
    for r in records:
        cls = service_class.get(r.service)
        raw = getattr(r, "raw", {}) or {}
        if cls is None or not r.flavor:
            continue
        vcpu = _decimal(str(raw.get("vCpu", "")))
        m = _RAM_RE.match(str(raw.get("ram", "")))
        if vcpu is None or vcpu == 0 or not m:
            continue
        ram = Decimal(m.group(1))
        if (m.group(2) or "").lower() in ("mib", "mb"):
            ram = ram / 1024
        linux = "open linux" in (r.os or "").lower()
        payg = r.price("PAYG") if linux else None
        old = by_class[cls].get(r.flavor)
        if old is None or (payg is not None and old.linux_payg is None):
            by_class[cls][r.flavor] = Flavor(r.flavor, r.service, vcpu, ram, str(raw.get("productCategory", "")),
                                             payg, r.id)
    return {c: sorted(f.values(), key=lambda x: (x.vcpu, x.ram, x.name)) for c, f in by_class.items()}


def pick(flavors: list[Flavor], vcpu: Decimal, ram: Decimal) -> Flavor | None:
    """The nearest flavor that is not smaller on vCPU and on memory: the least vCPU, then the least memory, then the
    lowest open Linux price, then the name."""
    fits = [f for f in flavors if f.vcpu >= vcpu and f.ram >= ram]
    if not fits:
        return None
    big = Decimal("1e9")
    return min(fits, key=lambda f: (f.vcpu, f.ram, f.linux_payg if f.linux_payg is not None else big, f.name))


MAPPING_FIELDS = ("row", "size", "family", "azure_class", "vcpu", "memory_gib", "count", "tcp_class", "flavor",
                  "flavor_vcpu", "flavor_ram_gib", "alternative", "status", "note")
OK, FLAG, NEEDS_INPUT, MANUAL, NO_FIT = "ok", "flag", "needs-input", "manual", "no-fit"


def map_rows(inv: dict, classes: dict[str, list[Flavor]]) -> list[dict]:
    out = []
    for r in inv["rows"]:
        fam = r.get("family") or ""
        info = AZURE_FAMILIES.get(fam)
        row = {k: "" for k in MAPPING_FIELDS}
        row.update(row=r["row"], size=r.get("size", ""), family=fam, vcpu=r.get("vcpu", ""),
                   memory_gib=r.get("memory_gib", ""), count=r.get("count", "1"))
        notes = list(r.get("notes", []))
        if info is None:
            row.update(status=NEEDS_INPUT, tcp_class="",
                       note="; ".join(notes + ["no Azure family: give the size or the kind of workload"]))
            out.append(row)
            continue
        azure_class, tcp_class, flag = info
        row.update(azure_class=azure_class, tcp_class=tcp_class)
        if flag:
            notes.append(flag)
        if tcp_class == "manual":
            row.update(status=MANUAL, note="; ".join(notes))
            out.append(row)
            continue
        if not r.get("vcpu") or not r.get("memory_gib"):
            missing = [w for w, v in (("vCPU", r.get("vcpu")), ("memory", r.get("memory_gib"))) if not v]
            row.update(status=NEEDS_INPUT, note="; ".join(notes + ["%s missing in the inventory" % " and ".join(missing)]))
            out.append(row)
            continue
        vcpu, mem = Decimal(r["vcpu"]), Decimal(r["memory_gib"])
        best = pick(classes.get(tcp_class, []), vcpu, mem)
        if best is None:
            row.update(status=NO_FIT, note="; ".join(notes + ["no flavor of the class is as large"]))
            out.append(row)
            continue
        row.update(flavor=best.name, flavor_vcpu=calc.text(best.vcpu), flavor_ram_gib=calc.text(best.ram))
        alt_class = ALTERNATIVE.get(tcp_class)
        if alt_class:
            alt = pick(classes.get(alt_class, []), vcpu, mem)
            if alt is not None:
                row["alternative"] = alt.name
        row.update(status=FLAG if flag or r.get("notes") else OK, note="; ".join(notes))
        out.append(row)
    return out


def write_mapping(project: Path, rows: list[dict], region: str, source_line: str) -> Path:
    lines = ["# mapping of the inventory to TCP %s; %s; nearest flavor not smaller on vCPU and memory; a price record "
             "does not prove that a flavor can be ordered (KB-XELM)" % (region, source_line.replace("\t", " ")),
             "\t".join(MAPPING_FIELDS)]
    for r in rows:
        lines.append("\t".join(" ".join(str(r[k]).replace("\t", " ").split()) for k in MAPPING_FIELDS))
    path = _folder(project) / MAPPING
    _write(path, "\n".join(lines) + "\n")
    return path


def load_mapping(project: Path) -> tuple[str, list[dict]]:
    try:
        lines = (_folder(project) / MAPPING).read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        raise MigrateError("no mapping yet (awb migrate map)") from None
    if len(lines) < 2 or tuple(lines[1].split("\t")) != MAPPING_FIELDS:
        raise MigrateError("the mapping has no valid header")
    rows = []
    for n, line in enumerate(lines[2:], start=3):
        parts = line.split("\t")
        if len(parts) != len(MAPPING_FIELDS):
            raise MigrateError("line %d of the mapping is malformed" % n)
        rows.append(dict(zip(MAPPING_FIELDS, parts)))
    region = re.search(r"to TCP (eu-[a-z]{2}[0-9]?)", lines[0])
    return (region.group(1) if region else ""), rows


# --------------------------------------------------------------------------- the estimate

ESTIMATE_FIELDS = ("row", "flavor", "os_tier", "record", "term", "unit_price", "unit", "count", "hours",
                   "line_total", "status")


def os_tier(os_text: str) -> str:
    """The OS text of the price records a machine's OS falls under: Windows, Red Hat, SUSE, Oracle or open Linux."""
    s = (os_text or "").lower()
    if "windows" in s:
        return "windows"
    if "red hat" in s or "rhel" in s:
        return "red hat"
    if "sap" in s and ("suse" in s or "sles" in s):
        return "suse for sap"
    if "suse" in s or "sles" in s:
        return "suse linux"
    if "oracle" in s:
        return "oracle linux"
    return "open linux"


def _os_matches(tier: str, record_os: str) -> bool:
    """The price records write "RedHat Linux", "SUSE for SAP", "Open Linux": compare without spaces."""
    return tier.replace(" ", "") in (record_os or "").lower().replace(" ", "")


def estimate(project: Path, fetch_region: Callable, term: str = "PAYG", hours: int = HOURS_PER_MONTH) -> dict:
    """Price every mapped row and record the totals with awb calc. Returns {"rows", "total", "calc", "left_out"}."""
    region, rows = load_mapping(project)
    inv = {r["row"]: r for r in load_inventory(project)["rows"]}
    f = fetch_region(region or "eu-de")
    records = f.records
    by_flavor: dict[str, list] = {}
    for r in records:
        by_flavor.setdefault(r.flavor, []).append(r)
    out, left_out, totals = [], [], []
    for m in rows:
        if m["status"] not in (OK, FLAG) or not m["flavor"]:
            left_out.append(m["row"])
            continue
        tier = os_tier(inv.get(m["row"], {}).get("os", ""))
        cands = [r for r in by_flavor.get(m["flavor"], []) if _os_matches(tier, r.os)]
        if not cands and tier == "open linux":
            cands = [r for r in by_flavor.get(m["flavor"], []) if "linux" in (r.os or "").lower()]
        line = {k: "" for k in ESTIMATE_FIELDS}
        line.update(row=m["row"], flavor=m["flavor"], os_tier=tier, term=term, count=m["count"] or "1")
        price_ = cands[0].price(term) if cands else None
        if not cands or price_ is None or price_ == 0:
            line["status"] = "no price for this OS and term"
            left_out.append(m["row"])
            out.append(line)
            continue
        rec = cands[0]
        count = Decimal(line["count"])
        if term == "PAYG":
            total = calc.evaluate("p * h * n", {"p": price_, "h": Decimal(hours), "n": count})
            line.update(hours=str(hours))
        else:
            total = calc.evaluate("p * n", {"p": price_, "n": count})     # reserved rates are monthly (KB-T5E6)
        line.update(record=rec.id, unit_price=calc.text(price_), unit=rec.unit,
                    line_total=calc.text(total, 2), status="ok")
        totals.append(line["line_total"])
        out.append(line)
    head = ("# estimate per month, %s, TCP %s, %s; machines only (no disks, network or licences beyond the OS); "
            "line totals rounded half up to the cent" % (term, region or "eu-de", f.source_line()))
    lines = [head, "\t".join(ESTIMATE_FIELDS)]
    lines += ["\t".join(str(r[k]) for k in ESTIMATE_FIELDS) for r in out]
    _write(_folder(project) / (ESTIMATE % term), "\n".join(lines) + "\n")
    result = {"rows": out, "left_out": left_out, "total": None, "calc": None}
    if totals:
        expr = "sum(%s)" % ", ".join(totals)
        value = calc.text(calc.evaluate(expr), 2)
        rec = calc.record(project, expr, [], value, "migration estimate %s per month" % term.lower())
        result.update(total=value, calc=rec.id)
    return result


# --------------------------------------------------------------------------- the gates


def _mentions(text: str, row_id: str) -> bool:
    return re.search(r"(?<![\w-])%s(?![0-9])" % re.escape(row_id), text) is not None


def _read(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None


def _gate_discover(project: Path) -> list[str]:
    problems = []
    try:
        inv = load_inventory(project)
    except MigrateError as err:
        return [str(err)]
    text = _read(_folder(project) / "discover.md")
    if not text or not text.strip():
        return ["migration/discover.md is missing or empty"]
    for r in inv["rows"]:
        open_ = not r.get("vcpu") or not r.get("memory_gib") or not r.get("family") or r.get("notes")
        if open_ and not _mentions(text, r["row"]):
            problems.append("discover.md does not say what happens with %s" % r["row"])
    return problems


def _gate_map(project: Path) -> list[str]:
    problems = []
    try:
        _, rows = load_mapping(project)
        inv = {r["row"]: r for r in load_inventory(project)["rows"]}
    except MigrateError as err:
        return [str(err)]
    text = _read(_folder(project) / "map-notes.md")
    if not text or not text.strip():
        return ["migration/map-notes.md is missing or empty"]
    for m in rows:
        src = inv.get(m["row"])
        if src is None:
            problems.append("%s of the mapping is not in the inventory" % m["row"])
            continue
        if m["flavor"]:
            try:
                small = (Decimal(m["flavor_vcpu"]) < Decimal(src["vcpu"])
                         or Decimal(m["flavor_ram_gib"]) < Decimal(src["memory_gib"]))
            except (DecimalException, KeyError):
                small = True
            if small:
                problems.append("%s: the flavor is smaller than the source" % m["row"])
        if m["status"] != OK and not _mentions(text, m["row"]):
            problems.append("map-notes.md does not decide %s (%s)" % (m["row"], m["status"]))
    return problems


def _gate_estimate(project: Path) -> list[str]:
    if not any(_read(p) for p in _folder(project).glob("estimate-*.tsv")):
        return ["no estimate yet (awb migrate estimate)"]
    notes = _read(_folder(project) / "estimate-notes.md")
    if not notes or not notes.strip():
        return ["migration/estimate-notes.md is missing or empty"]
    ids = set(re.findall(r"calc:(K-[0-9]+)", notes))
    if not ids:
        return ["estimate-notes.md names no calculation (calc:K-N of the totals)"]
    try:
        known = {r.id for r in calc.load(project)}
    except calc.CalcError as err:
        return [str(err)]
    return ["estimate-notes.md names %s, which is not recorded" % i for i in sorted(ids - known)]


def _gate_plan(project: Path) -> list[str]:
    text = _read(Path(project) / "deliverables" / "migration-plan.md")
    if not text or not text.strip():
        return ["deliverables/migration-plan.md is missing or empty"]
    return []


def _gate_review(project: Path) -> list[str]:
    from awb import review

    for r in review.status(project):
        if r["file"] == "deliverables/migration-plan.md":
            return [] if r["state"] == review.VALID else ["the review of the plan is %s (awb review pass)" % r["state"]]
    return ["the plan has no review"]


GATES = {"discover": _gate_discover, "map": _gate_map, "estimate": _gate_estimate, "plan": _gate_plan,
         "review": _gate_review}


# --------------------------------------------------------------------------- command line


def _project(arg: str | None) -> Path:
    from awb import review

    return Path(arg) if arg else review.find_project()


def _print_status(project: Path) -> None:
    state = load_state(project)
    for p in state["phases"]:
        print("%-9s %s%s" % (p["name"], p["state"], "  (%s)" % p["done_at"] if p["done_at"] else ""))
    nxt = next_phase(state)
    print("next: %s" % (nxt["name"] if nxt else "none, the migration is done"))


def _print_next(project: Path) -> None:
    state = load_state(project)
    nxt = next_phase(state)
    if nxt is None:
        print("every phase is done")
        return
    phase = PHASES[PHASE_NAMES.index(nxt["name"])]
    print("phase: %s (%s)" % (phase.name, nxt["state"]))
    print("phase file: %s" % phase_file(phase.name))
    print("worker: %s" % (WORKERS[phase.worker] or "none, the orchestrator runs it"))
    print("reads: %s" % ", ".join(phase.reads))
    print("writes: %s" % ", ".join(phase.makes))
    print("then: awb migrate done %s" % phase.name)


def main(argv: list[str] | None = None) -> int:
    from awb.cli import SafeParser

    ap = SafeParser(prog="awb migrate", description="A migration to TCP in phases, with its state on disk.")
    ap.add_argument("--project", default=None)
    sub = ap.add_subparsers(dest="command")
    s = sub.add_parser("init", help="start a migration in the project")
    s.add_argument("--from", dest="source", required=True, choices=SOURCES)
    sub.add_parser("status", help="every phase and its state")
    sub.add_parser("next", help="the next phase, its worker and its files")
    s = sub.add_parser("inventory", help="read the machines from a CSV, TSV or a sanitised copy")
    s.add_argument("file")
    s.add_argument("--table", type=int, default=None)
    s = sub.add_parser("map", help="each machine to the nearest TCP flavor that is not smaller")
    s.add_argument("--region", default="eu-de")
    s = sub.add_parser("estimate", help="the monthly price per row and the totals")
    s.add_argument("--term", default="PAYG")
    s.add_argument("--hours", type=int, default=HOURS_PER_MONTH)
    s = sub.add_parser("done", help="run the gate of a phase and record it")
    s.add_argument("phase", choices=PHASE_NAMES)
    try:
        args = ap.parse_args(argv)
    except SystemExit as exc:
        return 0 if exc.code == 0 else 2
    if not args.command:
        ap.print_help()
        return 2
    project = _project(args.project)
    try:
        if args.command == "init":
            init(project, args.source)
            print("migration started: %s/%s" % (FOLDER, STATE))
            _print_next(project)
            return 0
        if args.command == "status":
            _print_status(project)
            return 0
        if args.command == "next":
            _print_next(project)
            return 0
        if args.command == "inventory":
            doc = inventory(project, Path(args.file), args.table)
            rows = doc["rows"]
            print("inventory: %d machines from table %d, columns %s" % (len(rows), doc["table"],
                                                                       ", ".join(doc["columns"])))
            open_ = [r["row"] for r in rows if not r["vcpu"] or not r["memory_gib"] or not r["family"] or r["notes"]]
            print("rows that need a word in discover.md: %s" % (", ".join(open_) or "none"))
            return 0
        if args.command == "map":
            from awb.tcp import price

            region = price.check_region(args.region)
            inv = load_inventory(project)
            f = price.fetch(None, region)
            if not f.records:
                print("awb migrate: the price API gave no records (%s), nothing mapped" % f.listing.state,
                      file=sys.stderr)
                return 1
            rows = map_rows(inv, flavors_from_records(f.records))
            write_mapping(project, rows, region, f.source_line())
            counts: dict[str, int] = {}
            for r in rows:
                counts[r["status"]] = counts.get(r["status"], 0) + 1
            print("mapping: %s" % ", ".join("%s %d" % kv for kv in sorted(counts.items())))
            print("written: %s/%s; decide every row that is not ok in migration/map-notes.md" % (FOLDER, MAPPING))
            return 0
        if args.command == "estimate":
            from awb.tcp import price

            if args.term not in price.TERMS:
                print("awb migrate: the term must be one of %s" % ", ".join(price.TERMS), file=sys.stderr)
                return 2
            res = estimate(project, lambda r: price.fetch(None, r), args.term, args.hours)
            print("estimate: %d rows priced, %d left out%s" % (sum(1 for r in res["rows"] if r["status"] == "ok"),
                                                              len(res["left_out"]),
                                                              " (%s)" % ", ".join(res["left_out"]) if res["left_out"] else ""))
            if res["total"] is not None:
                print("total per month: %s EUR, calc:%s" % (res["total"], res["calc"]))
            return 0
        if args.command == "done":
            problems = done(project, args.phase)
            if problems:
                print("the gate of %s did not pass:" % args.phase)
                for p in problems:
                    print("- %s" % p)
                return 1
            print("phase %s done" % args.phase)
            _print_next(project)
            return 0
    except (MigrateError, calc.CalcError) as err:
        print("awb migrate: %s" % err, file=sys.stderr)
        return 2
    except OSError as err:
        print("awb migrate: %s (operating system error)" % type(err).__name__, file=sys.stderr)
        return 2
    return 2


if __name__ == "__main__":
    sys.exit(main())
