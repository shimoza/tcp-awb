"""The monthly cost estimate of the test tenants (B-048).

    awb tenant cost [--json]            owner side: the estimate, kept in <shared>/tenants/cost.json
    awb tenant cost note HANDLE TEXT    owner side: a note on one item (ALIAS/HANDLE; HANDLE when one tenant has it)

The estimate reads the latest snapshot of each tenant (awb/tcp/tenants.py) and fetches the live price API once per
region (awb/tcp/price.py, every service of the region: the ECS families sit under several service names, KB-3KHR).
No price comes from memory or from a file. Each item is priced for a month of 730 hours, PAYG:

- a running server: the hourly rate of its flavor plus OS times 730, the Windows record for os windows and the Open
  Linux record otherwise (an unknown OS says so on the item);
- a stopped server (SHUTOFF): 0, its disks are still counted as disks (KB-VTTM); a stopped GPU flavor carries the
  open conflict KB-NAAF;
- a disk: the rate per GB and month times its size, from the record of its disk type (the API unit says GB,
  KB-DXYK);
- an elastic IP: the hourly rate of the address times 730; bandwidth and traffic are not in the estimate.

An item without exactly one record is listed as no price with the reason and never counted as 0: the total says how
many items it leaves out. A listing a snapshot could not read (a read key that gets 403, for instance) leaves the
tenant unknown for that kind and region and the estimate names it; the lab role never reads for the estimate.

Attribution: the awb-project tag; a disk without one takes the project of its server; else "no project". The waste
groups are the disks of stopped servers, the disks attached to nothing and the elastic IPs bound to nothing. An item
with an owner note (`<shared>/tenants/cost-notes.tsv`: handle, note, date) carries the note and is never in a waste
group, nor is a disk of a server with a note. The note command writes ALIAS/HANDLE, since each tenant numbers its
own handles; a bare handle written by hand holds for that handle on every tenant. cost.json holds aliases, handles, project codes, price record ids, the times and the notes, never a resource
name or id.

How a disk and an elastic IP find their record (the disk type as a word of the flavor or name of an EVS record with a
unit in GB; an hourly record of the services eip or vpc that names an elastic or public IP and no bandwidth) is to
confirm live: a record it does not find shows as no price, never as 0.
"""
from __future__ import annotations

import datetime
import json
import os
import re
import sys
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path
from typing import Callable

from awb import calc, codes, config, jobs
from awb.tcp import price, tenants
from awb.tcp import sweep as _sweep

HOURS = Decimal(730)
CURRENCY = "EUR"
COST_FILE = "cost.json"
NOTES_FILE = "cost-notes.tsv"
NOTE_FIELDS = ("handle", "note", "date")
NO_PROJECT = "no project"
NO_PRICE = "no price"
TOP = 10
CENT = Decimal("0.01")
WASTE = (("stopped_server_disks", "disks of stopped servers"), ("unattached_disks", "disks not attached"),
         ("unbound_eips", "elastic IPs not bound"))
_CODE_RE = re.compile("^%s$" % codes.TCP_PROJECT_WIDE)
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_REF_RE = re.compile(r"^(?:(?P<alias>[a-z][a-z0-9-]{1,30})/)?(?P<handle>(?:ecs|evs|eip)-\d{1,6})$")
_NOTE_RE = re.compile(r"^[A-Za-z0-9 .,:;_/()#+'-]{1,200}$")
_ID_LIKE_RE = re.compile(r"[0-9a-fA-F]{16,}|[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-")
_DISK_RE = re.compile(r"^(\d+(?:\.\d+)?) GB ?(\S*)$")
_STOPPED = _sweep.IDLE_STATES["ecs"]
_UNBOUND = _sweep.IDLE_STATES["eip"]


class CostError(Exception):
    """The estimate or a note cannot be made. The message names aliases, handles and counts, never an id."""


def cost_path(p: config.Paths) -> Path:
    return tenants.root(p) / COST_FILE


def notes_path(p: config.Paths) -> Path:
    return tenants.root(p) / NOTES_FILE


def _write(path: Path, text: str) -> None:
    config.make_dir(path.parent, 0o770, shared=True)
    tmp = path.with_name("." + path.name + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


# --------------------------------------------------------------------------- the owner's notes


def load_notes(p: config.Paths) -> list[dict]:
    """The rows of cost-notes.tsv that read right; a later row of the same handle wins."""
    try:
        lines = notes_path(p).read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        return []
    out: dict[str, dict] = {}
    for line in lines[1:]:
        cols = line.split("\t")
        if len(cols) == len(NOTE_FIELDS) and _REF_RE.match(cols[0]) and _NOTE_RE.match(cols[1]) \
                and _DATE_RE.match(cols[2]):
            out[cols[0]] = dict(zip(NOTE_FIELDS, cols))
    return list(out.values())


def note_of(notes: list[dict], alias: str, handle: str) -> dict | None:
    """The note of ALIAS/HANDLE, else the one of the bare handle."""
    by = {n["handle"]: n for n in notes}
    return by.get("%s/%s" % (alias, handle)) or by.get(handle)


def check_note(text: str) -> str:
    text = " ".join((text or "").split())
    if not _NOTE_RE.match(text):
        raise CostError("a note is up to 200 letters, digits, blanks and . , : ; _ / ( ) # + ' -")
    if _ID_LIKE_RE.search(text):
        raise CostError("a note carries no id: name the item by its handle")
    from awb import gate

    try:
        rx = gate.blocklist_re()
    except gate.GateError as err:
        raise CostError(str(err)) from None
    if rx is not None and rx.search(text):
        raise CostError("the note matches the local blocklist: write codes, not names")
    return text


def write_note(p: config.Paths, ref: str, text: str, today: str | None = None) -> dict:
    """Keep a note on one item as ALIAS/HANDLE; a bare handle must be on the latest snapshot of exactly one tenant."""
    m = _REF_RE.match((ref or "").strip())
    if not m:
        raise CostError("a handle reads like ecs-3 or test-1/ecs-3")
    text = check_note(text)
    handle, alias = m.group("handle"), m.group("alias")
    holders = []
    for t in tenants.load(p):
        snap = tenants.latest(p, t.alias)
        if snap and any(i.get("handle") == handle for i in snap.get("items", [])):
            holders.append(t.alias)
    if alias and alias not in holders:
        raise CostError("the latest snapshot of %s has no %s" % (alias, handle))
    if not alias:
        if not holders:
            raise CostError("no latest snapshot has %s" % handle)
        if len(holders) > 1:
            raise CostError("%s is on %s: give ALIAS/%s" % (handle, ", ".join(holders), handle))
        alias = holders[0]
    row = {"handle": "%s/%s" % (alias, handle), "note": text,
           "date": today or datetime.date.today().isoformat()}
    rows = [n for n in load_notes(p) if n["handle"] != row["handle"]] + [row]
    _write(notes_path(p), "\n".join(["\t".join(NOTE_FIELDS)] + ["\t".join(n[f] for f in NOTE_FIELDS) for n in rows])
           + "\n")
    return row


# --------------------------------------------------------------------------- the records


def _norm(text: str) -> str:
    return re.sub(r"[^a-z0-9]", "", (text or "").lower())


def _words(*texts: str) -> set[str]:
    return set(re.findall(r"[a-z0-9]+", " ".join(texts).lower()))


def _one(cands: list[price.Record], what: str) -> tuple[price.Record | None, str]:
    if not cands:
        return None, "no record of %s in the price API (it covers part of the catalog, KB-AI2E)" % what
    if len(cands) > 1:
        return None, "%d records of %s in the price API (%s)" % (len(cands), what,
                                                                 ", ".join(sorted(r.id for r in cands)[:4]))
    if cands[0].currency and cands[0].currency != CURRENCY:
        return None, "the record of %s is in %s" % (what, cands[0].currency)
    return cands[0], ""


def server_record(records: list[price.Record], flavor: str, os_family: str) -> tuple[price.Record | None, str]:
    """The PAYG record of flavor plus OS: Windows for windows, Open Linux for linux and unknown."""
    if not flavor:
        return None, "the snapshot has no flavor"
    same = [r for r in records if r.flavor.lower() == flavor.lower() and price.is_hourly(r.unit)
            and r.price("PAYG") is not None]
    if os_family == "windows":
        cands = [r for r in same if _norm(r.os) == "windows"] or [r for r in same if "windows" in r.os.lower()]
        return _one(cands, "%s Windows" % flavor)
    return _one([r for r in same if _norm(r.os) == "openlinux"], "%s Open Linux" % flavor)


def disk_record(records: list[price.Record], disk_type: str) -> tuple[price.Record | None, str]:
    """The PAYG record per GB and month of an EVS disk type."""
    if not disk_type:
        return None, "the snapshot has no disk type"
    want = disk_type.lower()
    per_gb = [r for r in records if r.service == "evs" and "gb" in r.unit.lower() and not price.is_hourly(r.unit)
              and r.price("PAYG") is not None]
    # the flavor vss.<type> names the API disk type exactly; the word "ssd" also sits in the names of GP SSD and
    # Extreme SSD, so the words count only when no flavor matches (SSD = ultra-high I/O = vss.ssd, KB-GL4O)
    exact = [r for r in per_gb if (r.flavor or "").strip().lower() == "vss." + want]
    return _one(exact or [r for r in per_gb if want in _words(r.flavor, r.name)], "EVS %s" % disk_type)


def eip_record(records: list[price.Record]) -> tuple[price.Record | None, str]:
    """The hourly PAYG record of an elastic IP address, without its bandwidth."""
    def is_address(r: price.Record) -> bool:
        w = _words(r.flavor, r.name)
        return "bandwidth" not in w and ("eip" in w or ("ip" in w and bool(w & {"elastic", "public"})))

    found = [r for r in records if r.service in ("eip", "vpc") and r.unit.strip().lower() == "h"
             and r.price("PAYG") is not None and is_address(r)]
    # eu-de lists two: OTC_ELASTIC_IP (flavor eip, the hourly price) and OTC_EIP_GRAY ("Elastic IP - Address", no
    # flavor, 0): the one with the flavor eip is the price of the address (2026-11-07)
    flavored = [r for r in found if (r.flavor or "").strip().lower() == "eip"]
    return _one(flavored or found, "an elastic IP")


# --------------------------------------------------------------------------- the estimate


def _project(item: dict) -> str:
    code = item.get("project") or ""
    return code if _CODE_RE.match(code) else ""


def _attached(item: dict) -> list[str]:
    return [h for h in (item.get("attached") or "").split(",") if h]


def _not_attached(item: dict) -> bool:
    if "attached" in item:
        return not _attached(item) and item.get("state") != "in-use"
    return item.get("state") == "available"            # a snapshot from before B-048


def price_item(item: dict, fetch: price.Fetch | None) -> dict:
    """One item of a snapshot with its monthly estimate (a Decimal) or no price with the reason."""
    kind, size = item.get("kind", ""), item.get("size") or ""
    out = {"handle": item.get("handle", ""), "kind": kind, "region": item.get("region", ""),
           "state": item.get("state", ""), "size": size, "os": "", "attached": "", "status": NO_PRICE,
           "monthly": None, "unit_price": None, "unit": "", "record": "", "reason": "", "remarks": []}
    if kind == "ecs":
        out["os"] = item.get("os") or "unknown"
    if kind == "evs":
        out["attached"] = item.get("attached") or ""
    if kind == "ecs" and item.get("state") in _STOPPED:
        out.update(status="stopped", monthly=Decimal(0))
        out["remarks"].append("stopped: billed for its disks only (KB-VTTM)")
        if price.is_gpu_flavor(size):
            out["remarks"].append("a stopped GPU flavor: whether it is billed is an open conflict (KB-NAAF); "
                                  "counted for its disks only")
        return out
    if fetch is None or fetch.listing.state != jobs.LIST:
        out["reason"] = "no price listing of %s (%s)" % (out["region"], fetch.listing.describe() if fetch else
                                                         "not fetched")
        return out
    records = fetch.records
    if kind == "ecs":
        rec, why = server_record(records, size, out["os"])
        quantity = HOURS
        if out["os"] == "unknown":
            out["remarks"].append("OS unknown: priced as Open Linux")
        if item.get("state") != "ACTIVE":
            out["remarks"].append("state %s: priced as running" % (item.get("state") or "unknown"))
    elif kind == "evs":
        m = _DISK_RE.match(size)
        if not m:
            out["reason"] = "the snapshot has no size in GB"
            return out
        rec, why = disk_record(records, m.group(2))
        quantity = Decimal(m.group(1))
    elif kind == "eip":
        rec, why = eip_record(records)
        quantity = HOURS
        out["remarks"].append("bandwidth and traffic not counted")
    else:
        rec, why = None, "no rule for the kind %s" % kind
        quantity = Decimal(0)
    if rec is None:
        out["reason"] = why
        return out
    rate = rec.price("PAYG")
    out.update(status="priced", monthly=rate * quantity, unit_price=str(rate), unit=rec.unit, record=rec.id)
    return out


def _fetch_region(region: str) -> price.Fetch:
    return price.fetch(None, region)


def _age_days(f: price.Fetch) -> int | None:
    if not f.cached_at:
        return 0
    try:
        return (datetime.date.fromisoformat(f.fetched_at[:10]) - datetime.date.fromisoformat(f.cached_at[:10])).days
    except ValueError:
        return None


def _sum(rows: list[dict]) -> Decimal:
    return sum((r["monthly"] for r in rows if r["monthly"] is not None), Decimal(0))


def _brief(r: dict) -> dict:
    return {"tenant": r["tenant"], "handle": r["handle"], "kind": r["kind"], "monthly": r["monthly"]}


def estimate(p: config.Paths, *, fetcher: Callable[[str], price.Fetch] | None = None,
             now: datetime.datetime | None = None) -> dict:
    """The estimate of every registered tenant from its latest snapshot, one price fetch per region. Amounts are
    Decimals here; `as_json` rounds them to the cent."""
    now = now or datetime.datetime.now(datetime.timezone.utc)
    fetcher = fetcher or _fetch_region
    notes = load_notes(p)
    snaps = [(t.alias, tenants.latest(p, t.alias)) for t in tenants.load(p)]
    regions = sorted({i.get("region", "") for _, s in snaps if s for i in s.get("items", [])} - {""})
    fetches: dict[str, price.Fetch | None] = {}
    prices = []
    for region in regions:
        try:
            f = fetcher(region)
        except price.PriceError as err:
            f, reason = None, str(err)
        else:
            reason = f.listing.reason if f.listing.state == jobs.UNKNOWN else ""
        fetches[region] = f
        prices.append({"region": region, "state": f.listing.state if f else jobs.UNKNOWN,
                       "fetched_at": f.fetched_at if f else None, "cached_at": f.cached_at if f else None,
                       "age_days": _age_days(f) if f else None, "records": len(f.records) if f else 0,
                       "reason": reason})
    rows: list[dict] = []
    tenant_rows = []
    for alias, snap in snaps:
        if snap is None:
            tenant_rows.append({"alias": alias, "snapshot": None, "taken": None, "status": "no snapshot",
                                "unreadable": [], "items": 0, "no_price": 0, "total": Decimal(0)})
            continue
        items = snap.get("items", [])
        servers = {i["handle"]: i for i in items if i.get("kind") == "ecs"}
        mine = []
        for item in items:
            r = dict(price_item(item, fetches.get(item.get("region", ""))), tenant=alias)
            project, source = _project(item), "tag"
            if not project and item.get("kind") == "evs":
                project = next((_project(servers[h]) for h in _attached(item)
                                if h in servers and _project(servers[h])), "")
                source = "server"
            r.update(project=project or NO_PROJECT, project_source=source if project else "none")
            n = note_of(notes, alias, r["handle"])
            r["note"] = n["note"] if n else None
            r["_item"] = item
            r["_servers"] = servers
            mine.append(r)
        unreadable = [{"region": reg, "kinds": list(kinds)} for reg, kinds in sorted(snap.get("unknown", {}).items())
                      if kinds]
        tenant_rows.append({"alias": alias, "snapshot": snap.get("date"), "taken": snap.get("taken"),
                            "status": "partial" if unreadable else "complete", "unreadable": unreadable,
                            "items": len(mine), "no_price": sum(1 for r in mine if r["status"] == NO_PRICE),
                            "total": _sum(mine)})
        rows += mine

    rows.sort(key=lambda r: (r["monthly"] is None, -(r["monthly"] or 0), r["tenant"], r["handle"]))
    waste: dict[str, list[dict]] = {key: [] for key, _ in WASTE}
    for r in rows:
        item, servers = r.pop("_item"), r.pop("_servers")
        if r["note"]:
            continue
        if r["kind"] == "evs":
            attached = _attached(item)
            if any(note_of(notes, r["tenant"], h) for h in attached):
                continue                                   # the disk of a server the owner keeps on purpose
            if attached and all(servers.get(h, {}).get("state") in _STOPPED for h in attached):
                waste["stopped_server_disks"].append(r)
            elif _not_attached(item):
                waste["unattached_disks"].append(r)
        elif r["kind"] == "eip" and item.get("state") in _UNBOUND:
            waste["unbound_eips"].append(r)

    def grouped(field: str) -> list[dict]:
        out: dict[str, list[dict]] = {}
        for r in rows:
            out.setdefault(r[field], []).append(r)
        return sorted(({field: k, "amount": _sum(v), "items": len(v), "no_price": sum(1 for r in v
                        if r["status"] == NO_PRICE)} for k, v in out.items()), key=lambda g: (-g["amount"], g[field]))

    no_price = sum(1 for r in rows if r["status"] == NO_PRICE)
    unknown = [t["alias"] for t in tenant_rows if t["status"] != "complete"]
    return {
        "computed": now.strftime("%Y-%m-%dT%H:%M:%SZ"), "currency": CURRENCY, "hours_per_month": int(HOURS),
        "total": _sum(rows), "no_price": no_price, "complete": not no_price and not unknown,
        "unknown_tenants": unknown, "prices": prices, "tenants": tenant_rows,
        "by_kind": grouped("kind"), "by_project": grouped("project"),
        "top": [_brief(r) for r in rows if r["monthly"]][:TOP],
        "waste": [{"group": key, "label": label, "amount": _sum(waste[key]), "items": [_brief(r) for r in waste[key]]}
                  for key, label in WASTE],
        "items": rows, "notes": notes,
    }


def as_json(value):
    """The estimate with every Decimal rounded to the cent as a number."""
    if isinstance(value, Decimal):
        return float(value.quantize(CENT, ROUND_HALF_UP))
    if isinstance(value, dict):
        return {k: as_json(v) for k, v in value.items()}
    if isinstance(value, list):
        return [as_json(v) for v in value]
    return value


def write(p: config.Paths, cost: dict) -> Path:
    path = cost_path(p)
    _write(path, json.dumps(as_json(cost), indent=1, sort_keys=True) + "\n")
    return path


# --------------------------------------------------------------------------- the text


def _eur(value) -> str:
    return "-" if value is None else calc.text(Decimal(value), 2)


def summary_line(cost: dict) -> str:
    line = "cost estimate: %s %s a month" % (_eur(cost["total"]), cost["currency"])
    if cost["no_price"]:
        line += ", without %d item(s) that have no price" % cost["no_price"]
    if cost["unknown_tenants"]:
        line += "; not complete for %s (listing not readable or no snapshot)" % ", ".join(cost["unknown_tenants"])
    return line


def lines(cost: dict) -> list[str]:
    out = [summary_line(cost),
           "  PAYG, %d h a month; disks per GB and month; elastic IPs without bandwidth and traffic"
           % cost["hours_per_month"]]
    for pr in cost["prices"]:
        if pr["state"] == jobs.LIST:
            text = "price API %s, fetched %s" % (pr["region"], pr["fetched_at"])
            if pr["cached_at"]:
                text += ", served from its cache of %s" % pr["cached_at"]
            if pr["age_days"]:
                text += ", %d day(s) old (KB-5LLN)" % pr["age_days"]
        else:
            text = "price API %s: no listing (%s); its items have no price" % (pr["region"], pr["reason"] or
                                                                              pr["state"])
        out.append("  " + text)
    if not cost["prices"]:
        out.append("  no region to price: no snapshot holds an item")
    for t in cost["tenants"]:
        if t["status"] == "no snapshot":
            out.append("  %s: no snapshot yet (awb tenant snapshot %s)" % (t["alias"], t["alias"]))
            continue
        text = "  %s: %s %s, %d items of the snapshot of %s" % (t["alias"], _eur(t["total"]), cost["currency"],
                                                                t["items"], t["snapshot"])
        if t["no_price"]:
            text += ", %d without a price" % t["no_price"]
        out.append(text)
        for u in t["unreadable"]:
            out.append("    not readable in %s: %s (unknown, not none: the read key cannot list it)"
                       % (u["region"], ", ".join(u["kinds"])))
    out.append("by kind: " + ", ".join("%s %s" % (g["kind"], _eur(g["amount"])) for g in cost["by_kind"]))
    out.append("by project: " + ", ".join("%s %s" % (g["project"], _eur(g["amount"])) for g in cost["by_project"]))
    if cost["top"]:
        out.append("top items:")
        by_key = {(r["tenant"], r["handle"]): r for r in cost["items"]}
        rows = []
        for b in cost["top"]:
            r = by_key[(b["tenant"], b["handle"])]
            rows.append([r["tenant"], r["handle"], r["size"] or "-", r["os"] or "-", r["state"] or "-", r["project"],
                         _eur(r["monthly"]), r["note"] or ""])
        out += ["  " + x for x in tenants.table(rows, ["tenant", "handle", "size", "os", "state", "project",
                                                       "EUR/month", "note"])]
    for w in cost["waste"]:
        if w["items"]:
            out.append("%s: %d, %s %s: %s" % (w["label"], len(w["items"]), _eur(w["amount"]), cost["currency"],
                                              ", ".join("%s/%s" % (i["tenant"], i["handle"]) for i in w["items"])))
        else:
            out.append("%s: none" % w["label"])
    unpriced = [r for r in cost["items"] if r["status"] == NO_PRICE]
    if unpriced:
        out.append("no price (not counted):")
        out += ["  %s/%s %s %s: %s" % (r["tenant"], r["handle"], r["kind"], r["size"] or "-", r["reason"])
                for r in unpriced]
    remarks = [r for r in cost["items"] if r["remarks"] and r["kind"] == "ecs"]
    for r in remarks:
        out.append("  %s/%s: %s" % (r["tenant"], r["handle"], "; ".join(r["remarks"])))
    noted = [r for r in cost["items"] if r["note"]]
    if noted:
        out.append("owner notes:")
        out += ["  %s/%s %s: %s" % (r["tenant"], r["handle"], _eur(r["monthly"]), r["note"]) for r in noted]
    return out


# --------------------------------------------------------------------------- command


def command(p: config.Paths, args, *, fetcher: Callable[[str], price.Fetch] | None = None) -> int:
    """awb tenant cost [--json] and awb tenant cost note HANDLE TEXT (owner side)."""
    try:
        if getattr(args, "cost_command", None) == "note":
            row = write_note(p, args.handle, args.text)
            print("note kept on %s (%s)" % (row["handle"], row["date"]))
            return 0
        cost = estimate(p, fetcher=fetcher)
        write(p, cost)
        if args.json:
            print(json.dumps(as_json(cost), indent=1, sort_keys=True))
        else:
            print("\n".join(lines(cost) + ["kept in tenants/%s of the shared folder" % COST_FILE]))
    except (CostError, tenants.TenantError) as err:
        print("awb tenant cost: %s" % err, file=sys.stderr)
        return 1
    return 0


def after_snapshot(p: config.Paths, *, fetcher: Callable[[str], price.Fetch] | None = None) -> list[str]:
    """The estimate after `awb tenant snapshot`: its summary line or why it was not made. Never raises for the
    price API or a file, so a snapshot never fails because of it."""
    try:
        cost = estimate(p, fetcher=fetcher)
        write(p, cost)
    except (CostError, tenants.TenantError, OSError, ValueError) as err:
        return ["cost estimate not made (%s)" % (err if isinstance(err, (CostError, tenants.TenantError))
                                                 else type(err).__name__)]
    return [summary_line(cost) + " (awb tenant cost)"]
