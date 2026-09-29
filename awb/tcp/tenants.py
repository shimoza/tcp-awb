"""Resources on the test tenants, tracked over time (T-63).

    awb tenant add ALIAS --keys pass:ENTRY|file:PATH [--region R]...   owner side: a tenant and its read-only key
    awb tenant snapshot [ALIAS]...                                      owner side: list the tenant, GET only
    awb tenant list                                                     every tenant, what runs there now
    awb tenant now ALIAS                                                what runs there now, oldest first
    awb tenant history ALIAS [--project CODE] [--since YYYY-MM-DD]      what appeared, changed and went, when
    awb tenant at ALIAS YYYY-MM-DD                                      what ran there on a day
    awb tenant project CODE                                             what a project runs and ran, on every tenant

A snapshot lists the servers, disks and elastic IPs of every region of the tenant (the sources of the sweep,
awb/tcp/sweep.py) and keeps them in `<shared>/tenants/<alias>/snapshots/<date>.json`. Each resource is kept as a
neutral handle (`ecs-3`), its kind, region, creation time, state, size and its two tags: `awb-project` (the project
code) and `awb-expiry`. Names and ids are never kept in the snapshot; the ids stay in the tenant's handle file
(mode 600, owner only). A snapshot is compared with the one before it and the difference goes to
`<shared>/tenants/<alias>/events.jsonl`, one line per event, never rewritten: `seen` (in the first snapshot, with its
own creation time), `appeared`, `changed` (state, project, size) and `gone`. A listing that could not be read is
recorded as unknown for that kind and region and never read as empty: nothing is reported gone because of it.

The key of a tenant is a read-only key (list rights only) so that a daily snapshot runs without anyone: `file:PATH`
names a file of the owner (mode 600) with `ak=` and `sk=` lines, `pass:ENTRY` the entries `ENTRY/ak` and `ENTRY/sk`
of the password store. The work user never gets a key: it reads what the snapshots recorded.

The history is kept for good. The tenants are named by aliases (test-1), never by their ids: a tenant id is an
identifier the commit gate refuses.
"""
from __future__ import annotations

import datetime
import json
import os
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from awb import config, jobs, obs
from awb.tcp import sweep as _sweep

TENANTS_DIR = "tenants"
REGISTER = "tenants.tsv"
EVENTS = "events.jsonl"
SNAPSHOTS = "snapshots"
HANDLES = "handles.json"
_ALIAS_RE = re.compile(r"^[a-z][a-z0-9-]{1,30}$")
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_CODE_RE = re.compile(r"^tcp-[a-z0-9]{4}$")
FIELDS = ("alias", "regions", "keys", "added")
PASS_REF, FILE_REF = "pass" + ":", "file" + ":"
"""The two ways a tenant names its read-only key."""


class TenantError(Exception):
    """A tenant problem. The message names aliases, kinds and counts, never a key or an id."""


@dataclass
class Tenant:
    alias: str
    regions: tuple[str, ...]
    keys: str
    added: str


def root(p: config.Paths) -> Path:
    return p.shared / TENANTS_DIR


def folder(p: config.Paths, alias: str) -> Path:
    return root(p) / alias


# --------------------------------------------------------------------------- the register of tenants


def load(p: config.Paths) -> list[Tenant]:
    try:
        lines = (root(p) / REGISTER).read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        return []
    out = []
    for line in lines[1:]:
        cols = line.split("\t")
        if len(cols) == len(FIELDS) and _ALIAS_RE.match(cols[0]):
            out.append(Tenant(cols[0], tuple(r for r in cols[1].split(",") if r), cols[2], cols[3]))
    return out


def get(p: config.Paths, alias: str) -> Tenant:
    for t in load(p):
        if t.alias == alias:
            return t
    raise TenantError("no tenant with the alias %s (awb tenant list)" % alias)


def add(p: config.Paths, alias: str, keys: str, regions: list[str], today: str | None = None) -> Tenant:
    if not _ALIAS_RE.match(alias or ""):
        raise TenantError("an alias reads like test-1: small letters, digits and dashes")
    if not keys.startswith((PASS_REF, FILE_REF)) or len(keys) < 6:
        raise TenantError("the key reads pass:ENTRY or file:PATH")
    from awb.tcp.cloud import check_region

    regions = [check_region(r) for r in (regions or ["eu-de"])]
    if any(t.alias == alias for t in load(p)):
        raise TenantError("the alias %s is taken" % alias)
    t = Tenant(alias, tuple(regions), keys, today or datetime.date.today().isoformat())
    config.make_dir(root(p), 0o770, shared=True)
    reg = root(p) / REGISTER
    new = not reg.exists()
    with open(reg, "a", encoding="utf-8") as fh:
        if new:
            fh.write("\t".join(FIELDS) + "\n")
        fh.write("\t".join([t.alias, ",".join(t.regions), t.keys, t.added]) + "\n")
    return t


def keys_of(t: Tenant) -> obs.Keys:
    if t.keys.startswith(PASS_REF):
        try:
            return obs.keys_from_reference(t.keys)
        except obs.OBSError as err:
            raise TenantError("the key of %s: %s" % (t.alias, err)) from None
    path = Path(os.path.expanduser(t.keys[5:]))
    try:
        st = path.stat()
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        raise TenantError("the key file of %s cannot be read" % t.alias) from None
    if st.st_mode & 0o077:
        raise TenantError("the key file of %s is open to others: chmod 600 it" % t.alias)
    vals = dict(line.split("=", 1) for line in text.splitlines() if "=" in line)
    ak, sk = vals.get("ak", "").strip(), vals.get("sk", "").strip()
    if not ak or not sk:
        raise TenantError("the key file of %s needs an ak= and an sk= line" % t.alias)
    return obs.Keys(ak, sk)


# --------------------------------------------------------------------------- snapshots


def _size(kind: str, raw: dict) -> str:
    if kind == "ecs":
        fl = raw.get("flavor")
        return str(fl.get("id") or fl.get("name") or "") if isinstance(fl, dict) else ""
    if kind == "evs":
        size, vtype = raw.get("size"), raw.get("volume_type")
        return ("%s GB %s" % (size, vtype or "")).strip() if size is not None else ""
    if kind == "eip":
        bw = raw.get("bandwidth_size")
        return "%s Mbit/s" % bw if bw is not None else ""
    return ""


Lister = Callable[[str, str, str, str], jobs.Listing]


def take(t: Tenant, handles: _sweep.Handles, listers: dict[str, Lister], now: datetime.datetime) -> dict:
    """One snapshot of the tenant: every source in every region. `listers[region](service, path, key, paging)`."""
    items: list[dict] = []
    unknown: dict[str, list[str]] = {}
    for region in t.regions:
        lister = listers[region]
        for kind, service, path, key, paging in _sweep.SOURCES:
            listing = lister(service, path, key, paging)
            if listing.state == jobs.UNKNOWN:
                unknown.setdefault(region, []).append(kind)
                continue
            for raw in listing.items:
                if not isinstance(raw, dict) or not isinstance(raw.get("id"), str):
                    continue
                tags = _sweep._tags(raw.get("tags") or (raw.get("metadata") or {}).get("tags"))
                items.append({
                    "handle": handles.handle(kind, raw["id"]),
                    "kind": kind,
                    "region": region,
                    "created": _sweep._created(raw),
                    "state": _sweep._state(kind, raw),
                    "size": _size(kind, raw),
                    "project": tags.get(_sweep.PROJECT_TAG, ""),
                    "expiry": tags.get(_sweep.EXPIRY_TAG, ""),
                })
    items.sort(key=lambda i: (i["created"], i["handle"]))
    return {"alias": t.alias, "date": now.date().isoformat(), "taken": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "regions": list(t.regions), "unknown": unknown, "items": items}


def snapshots(p: config.Paths, alias: str) -> list[Path]:
    d = folder(p, alias) / SNAPSHOTS
    try:
        return sorted(f for f in d.iterdir() if _DATE_RE.match(f.stem) and f.suffix == ".json")
    except FileNotFoundError:
        return []


def read_snapshot(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def diff(before: dict | None, after: dict) -> list[dict]:
    """The events between two snapshots. A kind that was unknown in a region in either snapshot reports nothing
    gone there."""
    date = after["date"]
    if before is None:
        return [dict(item, event="seen", date=date) for item in after["items"]]
    old = {i["handle"]: i for i in before["items"]}
    new = {i["handle"]: i for i in after["items"]}
    blind = {(r, k) for snap in (before, after) for r, kinds in snap.get("unknown", {}).items() for k in kinds}
    out: list[dict] = []
    for h, item in new.items():
        if h not in old:
            out.append(dict(item, event="appeared", date=date))
            continue
        changed = [f for f in ("state", "project", "size", "expiry") if old[h].get(f) != item.get(f)]
        if changed:
            out.append(dict(item, event="changed", date=date, fields=changed,
                            was={f: old[h].get(f) for f in changed}))
    for h, item in old.items():
        if h not in new and (item["region"], item["kind"]) not in blind:
            out.append(dict(item, event="gone", date=date))
    return out


def record(p: config.Paths, snap: dict) -> list[dict]:
    """Keep the snapshot of the day (a second one on the same day replaces the first) and append its events."""
    d = folder(p, snap["alias"])
    config.make_dir(d / SNAPSHOTS, 0o770, shared=True)
    earlier = [s for s in snapshots(p, snap["alias"]) if s.stem < snap["date"]]
    today = [s for s in snapshots(p, snap["alias"]) if s.stem == snap["date"]]
    base = read_snapshot(today[0]) if today else (read_snapshot(earlier[-1]) if earlier else None)
    events = diff(base, snap)
    target = d / SNAPSHOTS / ("%s.json" % snap["date"])
    tmp = target.with_name("." + target.name + ".tmp")
    tmp.write_text(json.dumps(snap, indent=1) + "\n", encoding="utf-8")
    os.replace(tmp, target)
    with open(d / EVENTS, "a", encoding="utf-8") as fh:
        for e in events:
            fh.write(json.dumps(e, sort_keys=True) + "\n")
    return events


def snapshot(p: config.Paths, t: Tenant, make_lister: Callable[[Tenant, str], Lister] | None = None,
             now: datetime.datetime | None = None) -> tuple[dict, list[dict]]:
    now = now or datetime.datetime.now(datetime.timezone.utc)
    config.make_dir(folder(p, t.alias), 0o770, shared=True)
    handles = _sweep.Handles(folder(p, t.alias) / HANDLES)
    make_lister = make_lister or _cloud_lister
    snap = take(t, handles, {r: make_lister(t, r) for r in t.regions}, now)
    handles.save()
    try:
        os.chmod(folder(p, t.alias) / HANDLES, 0o600)
    except OSError:
        pass
    return snap, record(p, snap)


def _cloud_lister(t: Tenant, region: str) -> Lister:
    from awb.tcp.cloud import Client

    c = Client(keys_of(t), region)
    return lambda service, path, key, paging: c.list(service, path, key, paging=paging)


# --------------------------------------------------------------------------- questions


def events(p: config.Paths, alias: str) -> list[dict]:
    try:
        lines = (folder(p, alias) / EVENTS).read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        return []
    out = []
    for line in lines:
        try:
            out.append(json.loads(line))
        except ValueError:
            continue
    return out


def latest(p: config.Paths, alias: str, on: str | None = None) -> dict | None:
    snaps = [s for s in snapshots(p, alias) if on is None or s.stem <= on]
    return read_snapshot(snaps[-1]) if snaps else None


def age_days(created: str, today: datetime.date) -> str:
    try:
        return str((today - datetime.date.fromisoformat(created[:10])).days)
    except ValueError:
        return "?"


def flags(item: dict, today: datetime.date) -> str:
    out = []
    if not item.get("project"):
        out.append("no project")
    exp = item.get("expiry", "")
    if exp and _DATE_RE.match(exp) and exp < today.isoformat():
        out.append("expired")
    if item.get("state") in _sweep.IDLE_STATES.get(item.get("kind", ""), ()):
        out.append("idle")
    return ", ".join(out)


def _row(cols: list[str], widths: list[int]) -> str:
    return "  ".join(c.ljust(w) for c, w in zip(cols, widths)).rstrip()


def table(rows: list[list[str]], head: list[str]) -> list[str]:
    widths = [max(len(str(r[i])) for r in [head] + rows) for i in range(len(head))]
    return [_row(head, widths)] + [_row([str(c) for c in r], widths) for r in rows]


def now_lines(p: config.Paths, alias: str, today: datetime.date) -> list[str]:
    snap = latest(p, alias)
    if snap is None:
        return ["%s: no snapshot yet (awb tenant snapshot %s)" % (alias, alias)]
    rows = [[i["handle"], i["kind"], i["region"], i.get("size", ""), i.get("project") or "-", i["created"][:10],
             age_days(i["created"], today), i.get("expiry") or "-", i.get("state", ""), flags(i, today)]
            for i in snap["items"]]
    out = ["%s: %d resources in the snapshot of %s" % (alias, len(rows), snap["date"])]
    for region, kinds in sorted(snap.get("unknown", {}).items()):
        out.append("  not readable in %s: %s (counted as unknown, not as none)" % (region, ", ".join(kinds)))
    if rows:
        out += table(rows, ["handle", "kind", "region", "size", "project", "created", "days", "expiry", "state",
                            "flags"])
    return out


def history_lines(p: config.Paths, alias: str, project: str | None = None, since: str | None = None) -> list[str]:
    evs = [e for e in events(p, alias) if (not project or e.get("project") == project)
           and (not since or e.get("date", "") >= since)]
    if not evs:
        return ["%s: no event%s" % (alias, " for %s" % project if project else "")]
    rows = []
    for e in evs:
        what = e["event"]
        if what == "changed":
            what += " " + ", ".join("%s %s -> %s" % (f, (e.get("was") or {}).get(f) or "-", e.get(f) or "-")
                                    for f in e.get("fields", []))
        rows.append([e["date"], e["handle"], e["kind"], e.get("project") or "-", e.get("created", "")[:10], what])
    return table(rows, ["date", "handle", "kind", "project", "created", "event"])


def project_lines(p: config.Paths, code: str, today: datetime.date) -> list[str]:
    out = []
    for t in load(p):
        snap = latest(p, t.alias)
        running = [i for i in (snap or {}).get("items", []) if i.get("project") == code]
        past = [e for e in events(p, t.alias) if e.get("project") == code]
        if not running and not past:
            continue
        out.append("%s: %d running now, %d events" % (t.alias, len(running), len(past)))
        out += ["  " + line for line in history_lines(p, t.alias, project=code)]
    return out or ["%s: nothing on any tenant" % code]


def list_lines(p: config.Paths, today: datetime.date) -> list[str]:
    rows = []
    for t in load(p):
        snap = latest(p, t.alias)
        items = (snap or {}).get("items", [])
        kinds = {}
        for i in items:
            kinds[i["kind"]] = kinds.get(i["kind"], 0) + 1
        rows.append([t.alias, ",".join(t.regions), (snap or {}).get("date", "never"), str(len(items)),
                     " ".join("%s %d" % kv for kv in sorted(kinds.items())) or "-",
                     str(sum(1 for i in items if not i.get("project")))])
    if not rows:
        return ["no tenant yet (awb tenant add ALIAS --keys file:PATH)"]
    return table(rows, ["tenant", "regions", "snapshot", "resources", "by kind", "no project"])


# --------------------------------------------------------------------------- command line


def main(argv: list[str] | None = None) -> int:
    from awb.cli import SafeParser

    ap = SafeParser(prog="awb tenant", description="Resources on the test tenants, tracked over time.")
    sub = ap.add_subparsers(dest="command")
    a = sub.add_parser("add", help="a tenant and its read-only key (owner side)")
    a.add_argument("alias")
    a.add_argument("--keys", required=True)
    a.add_argument("--region", action="append", default=[])
    s = sub.add_parser("snapshot", help="list the tenants now, GET only (owner side)")
    s.add_argument("alias", nargs="*")
    sub.add_parser("list", help="every tenant and what runs there now")
    n = sub.add_parser("now", help="what runs on a tenant now")
    n.add_argument("alias")
    h = sub.add_parser("history", help="what appeared, changed and went")
    h.add_argument("alias")
    h.add_argument("--project")
    h.add_argument("--since")
    t_ = sub.add_parser("at", help="what ran on a tenant on a day")
    t_.add_argument("alias")
    t_.add_argument("date")
    pr = sub.add_parser("project", help="what a project runs and ran on every tenant")
    pr.add_argument("code")
    try:
        args = ap.parse_args(argv)
    except SystemExit as exc:
        return 0 if exc.code in (0, None) else 2
    if args.command is None:
        ap.print_usage(sys.stderr)
        return 2
    p = config.paths()
    today = datetime.date.today()
    try:
        if args.command == "add":
            if config.is_work_user():
                raise TenantError("tenants are added on the owner side")
            t = add(p, args.alias, args.keys, args.region)
            print("tenant %s added (%s); take the first snapshot: awb tenant snapshot %s"
                  % (t.alias, ",".join(t.regions), t.alias))
        elif args.command == "snapshot":
            if config.is_work_user():
                raise TenantError("snapshots are taken on the owner side, with the tenant's read-only key")
            chosen = [get(p, a) for a in args.alias] if args.alias else load(p)
            if not chosen:
                raise TenantError("no tenant yet (awb tenant add ALIAS --keys file:PATH)")
            for t in chosen:
                snap, evs = snapshot(p, t)
                counts = {}
                for e in evs:
                    counts[e["event"]] = counts.get(e["event"], 0) + 1
                unknown = sum(len(v) for v in snap["unknown"].values())
                print("%s: %d resources, events %s%s" % (t.alias, len(snap["items"]),
                      " ".join("%s %d" % kv for kv in sorted(counts.items())) or "none",
                      "; %d listing(s) not readable" % unknown if unknown else ""))
        elif args.command == "list":
            print("\n".join(list_lines(p, today)))
        elif args.command == "now":
            get(p, args.alias)
            print("\n".join(now_lines(p, args.alias, today)))
        elif args.command == "history":
            get(p, args.alias)
            if args.since and not _DATE_RE.match(args.since):
                raise TenantError("--since reads YYYY-MM-DD")
            print("\n".join(history_lines(p, args.alias, args.project, args.since)))
        elif args.command == "at":
            get(p, args.alias)
            if not _DATE_RE.match(args.date):
                raise TenantError("the date reads YYYY-MM-DD")
            snap = latest(p, args.alias, on=args.date)
            if snap is None:
                print("%s: no snapshot on or before %s" % (args.alias, args.date))
            else:
                rows = [[i["handle"], i["kind"], i["region"], i.get("size", ""), i.get("project") or "-",
                         i["created"][:10], i.get("state", "")] for i in snap["items"]]
                print("%s on %s (snapshot of %s): %d resources" % (args.alias, args.date, snap["date"], len(rows)))
                if rows:
                    print("\n".join(table(rows, ["handle", "kind", "region", "size", "project", "created", "state"])))
        elif args.command == "project":
            if not _CODE_RE.match(args.code):
                raise TenantError("a project code reads like tcp-q7m4")
            print("\n".join(project_lines(p, args.code, today)))
    except (TenantError, _sweep.SweepError) as err:
        print("awb tenant: %s" % err, file=sys.stderr)
        return 1
    except OSError as err:
        print("awb tenant: %s (operating system error)" % type(err).__name__, file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
