"""The move plan of the project files (presentations/reports-design, sections 3.2 to 3.5, task R5).

    awb files plan [--laptop FILE|NAME|latest]... [--host FILE] [--buckets FILE...] [--put]

It reads the coded inventories of the three places (the rows of `awb/inventory.py`): the laptop's from
`reports/inventory/` of the owner bucket through the key service (`latest`, the default, is the newest
`laptop-<stamp>[-<label>].tsv` there; a NAME of that folder or a local FILE also works, and `--laptop` may be given
more than once), the host's and the buckets' from `<shared>/files/` (the newest `host-`, `owner-bucket-` and
`lab-bucket-<stamp>.tsv` of `awb files inventory`) or from the files named. A missing host or bucket inventory is
said in one line and the plan is made without it. The laptop rows are checked again at entry, as every text that
reaches the host: a path with a hit becomes `[withheld]`; a row that does not have the shape of an inventory row is
dropped and counted.

Matching (section 3.2). Identical: equal sha256 or equal md5 (a bucket object has the md5 only); an empty file
matches nothing. Probable: equal size and suffix, times within two minutes, no hash to compare (a multipart object);
listed only. Version: the same group (project, normalised stem, suffix) and kind, different content, the newest by
mtime first. A laptop file without a project takes the project of the host or bucket copies it is identical to when
they name one project; the rest stay without a project and are listed.

The laptop part (section 3.5). The keeper of identical laptop copies is the one already in its layout place, else
the one with the shallowest path, else the newest. Every other copy and every older version is `archive`. The
keeper and every other file of a project is `move` into the layout folder of its kind, or `keep` when it lies there
already. The kind: `out` when the file is identical to a deliverable (host `deliverables/`, bucket `out/` or
`from-session/`), `clean` when it is identical to a file that went up (host `input/`, bucket `in/`) or lies in a
folder `clean`, `out` when it lies in a folder `out`, else `private`. A file without a project, or of a project whose
month is not known, is `keep` with its reason. The month is that of the project's folder in the owner bucket, else
the month of `created` in the register.

The host and the buckets are listed, never moved: `stale-in` (an object of `in/` of the owner bucket, the import
should have taken and dropped it), `withheld` (an object of a project folder of the owner bucket whose path is
withheld, so in/ or out/ cannot be told), `duplicate` (an identical copy inside one host project, inside `out/` of
one project of the owner bucket or inside `from-session/` of one project of the lab bucket; `keeper` names the copy
kept). Nothing is deleted anywhere.

The plan goes to `<shared>/files/plan-<stamp>/`: `PLAN.md` (readable, per project and group) and `PLAN.tsv`, UTF-8,
LF, the header `COLUMNS` and one row per laptop file and per listed host or bucket file:

    id       the row id of the inventory (L-..., H-..., B-..., S-...); the laptop finds the real path by it
    place    laptop, host, owner-bucket or lab-bucket
    action   laptop: keep, move, archive; host and buckets: stale-in, duplicate, withheld
    target   laptop only, a folder relative to the layout root of the laptop, segments with /, ending with /:
             move     YYYY-MM/tcp-xxxx/private/ (or clean/, out/); the laptop appends the real file name
             archive  YYYY-MM/tcp-xxxx/_archive/<stamp>/, or _archive/<stamp>/ without a project or month; the laptop
                      appends the file's real path relative to the root of its inventory
             keep     empty
    project  tcp-xxxx or none, after the assignment by hash
    path     the coded path of the inventory or [withheld]; never a real path
    keeper   for archive and duplicate: the id of the copy or the newest version that stays; else empty
    reason   a short fixed text: identical, older version, in its layout place, no project, ...

`--put` sends the laptop rows alone, as `PLAN.tsv` with the same header, to `reports/plan/<stamp>/PLAN.tsv` of the
owner bucket through the key service (`bucket_layout.plan_key`), where `awb clean files plan` on the laptop fetches
the newest. Standard library only; nothing here opens a project file.
"""
from __future__ import annotations

import datetime as _dt
import re
import sys
import tempfile
from pathlib import Path

from awb import bucket_layout, codes, config, inventory, ledger, projects

PROG = "awb files plan"
COLUMNS = ("id", "place", "action", "target", "project", "path", "keeper", "reason")
HEADER = "\t".join(COLUMNS)
PLAN_FILE = bucket_layout.PLAN_FILE
MD_FILE = "PLAN.md"
KINDS = ("private", "clean", "out")
ARCHIVE = "_archive"
PROBABLE_SECONDS = 120
LATEST = "latest"
EXIT_OK, EXIT_ERROR = 0, 2
NOT_DEEP = 1 << 20
"""The depth of a withheld path: it is never the shallowest."""

_STAMP = r"[0-9]{8}-[0-9]{6}"
_NAME_RE = re.compile(r"^(laptop|host|owner-bucket|lab-bucket)-(%s)(?:-[a-z0-9][a-z0-9-]{0,31})?\.tsv$" % _STAMP)
_ID_RES = {place: re.compile(r"^%s-%s-[0-9]{4,}$" % (letter, _STAMP)) for place, letter in inventory.PLACES.items()}
_SHA_RE = re.compile(r"^[0-9a-f]{64}$")
_MD5_RE = re.compile(r"^[0-9a-f]{32}$")
_MTIME_RE = re.compile(r"^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z$")
_CODE = codes.TCP_PROJECT_WIDE
_CODE_RE = re.compile("^%s$" % _CODE)
_LAYOUT_RE = re.compile(r"(?:^|/)(%s)/(%s)/(private|clean|out)/" % (bucket_layout.MONTH, _CODE))
_OWNER_RE = re.compile(r"^(%s)/(%s)/(in|out)/" % (bucket_layout.MONTH, _CODE))
_LAB_RE = re.compile(r"^(%s)/(in|from-session)/" % _CODE)


class PlanError(Exception):
    """The plan cannot be made. The message carries codes, counts and reasons, never a name."""


# --------------------------------------------------------------------------- reading the inventories


def _valid(row: dict, places: tuple[str, ...]) -> bool:
    place = row.get("place", "")
    if place not in places or not _ID_RES[place].match(row.get("id", "")):
        return False
    if not row.get("size", "").isdigit() or not row.get("path"):
        return False
    if row.get("mtime") and not _MTIME_RE.match(row["mtime"]):
        return False
    if row.get("sha256") and not _SHA_RE.match(row["sha256"]):
        return False
    if row.get("md5") and not _MD5_RE.match(row["md5"]):
        return False
    project = row.get("project") or inventory.NO_PROJECT
    return project == inventory.NO_PROJECT or bool(_CODE_RE.match(project))


def clean_rows(rows: list[dict], places: tuple[str, ...]) -> tuple[list[dict], int]:
    """The rows that have the shape of an inventory row of one of `places`, and the count of the others."""
    good = []
    for row in rows:
        row = {c: str(row.get(c) or "") for c in inventory.COLUMNS}
        if _valid(row, places):
            row["project"] = row["project"] or inventory.NO_PROJECT
            good.append(row)
    return good, len(rows) - len(good)


def newest_name(names, place: str) -> str | None:
    """The newest inventory file name of a place among `names` (by its stamp, then by the name)."""
    found = []
    for name in names:
        m = _NAME_RE.match(name)
        if m and m.group(1) == place:
            found.append((m.group(2), name))
    return max(found)[1] if found else None


def newest_local(folder: Path, place: str) -> Path | None:
    try:
        names = [f.name for f in folder.iterdir() if f.is_file()]
    except OSError:
        return None
    name = newest_name(names, place)
    return folder / name if name else None


def laptop_rows(specs: list[str], client, tmp: Path) -> tuple[list[dict], list[str]]:
    """The rows of the laptop inventories of `specs` and the names they came from. `latest` is the newest under
    reports/inventory/, a NAME of that folder is fetched, anything else is a local file."""
    rows: list[dict] = []
    names: list[str] = []
    listing = None
    for spec in specs:
        if spec == LATEST or (_NAME_RE.match(spec) and not Path(spec).exists()):
            if client is None:
                from awb import bucket

                client = bucket.client()
            if spec == LATEST:
                if listing is None:
                    listing = [k[len(inventory.PREFIX):] for k, _, _ in client.list(inventory.PREFIX).objects]
                name = newest_name(listing, "laptop")
                if name is None:
                    raise PlanError("no laptop inventory under %s (awb clean inventory --put on the laptop)"
                                    % inventory.PREFIX)
            else:
                name = spec
                if _NAME_RE.match(name).group(1) != "laptop":
                    raise PlanError("--laptop names a laptop inventory")
            got = tmp / ("%d-%s" % (len(names), name))
            client.get(inventory.PREFIX + name, got)
            rows += inventory.read(got)
            names.append(name)
        else:
            path = Path(spec)
            rows += inventory.read(path)
            names.append(path.name)
    return rows, names


# --------------------------------------------------------------------------- matching


class _Sets:
    def __init__(self, n: int):
        self.up = list(range(n))

    def find(self, i: int) -> int:
        while self.up[i] != i:
            self.up[i] = self.up[self.up[i]]
            i = self.up[i]
        return i

    def join(self, a: int, b: int) -> None:
        self.up[self.find(a)] = self.find(b)


def identical(rows: list[dict]) -> list[int]:
    """The set of identical copies of each row, as the index of one member: equal sha256 or equal md5, empty files
    left alone."""
    sets = _Sets(len(rows))
    for col in ("sha256", "md5"):
        first: dict[str, int] = {}
        for i, r in enumerate(rows):
            if r[col] and r["size"] != "0":
                if r[col] in first:
                    sets.join(i, first[r[col]])
                else:
                    first[r[col]] = i
    return [sets.find(i) for i in range(len(rows))]


def _seconds(mtime: str) -> float | None:
    try:
        return _dt.datetime.strptime(mtime, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=_dt.timezone.utc).timestamp()
    except ValueError:
        return None


def _comparable(a: dict, b: dict) -> bool:
    return bool(a["sha256"] and b["sha256"]) or bool(a["md5"] and b["md5"])


def probable(rows: list[dict], root: list[int]) -> list[tuple[int, int]]:
    """Pairs of rows of equal size and suffix, times within two minutes, that no hash can compare."""
    by: dict[tuple[str, str], list[tuple[float, int]]] = {}
    for i, r in enumerate(rows):
        t = _seconds(r["mtime"])
        if t is not None and r["size"] != "0":
            by.setdefault((r["size"], r["suffix"]), []).append((t, i))
    pairs = []
    for members in by.values():
        members.sort()
        for n, (t, i) in enumerate(members):
            for t2, j in members[n + 1:]:
                if t2 - t > PROBABLE_SECONDS:
                    break
                if root[i] != root[j] and not _comparable(rows[i], rows[j]):
                    pairs.append((min(i, j), max(i, j)))
    return sorted(set(pairs), key=lambda p: (rows[p[0]]["id"], rows[p[1]]["id"]))


def _depth(r: dict) -> int:
    return NOT_DEEP if r["path"] == inventory.WITHHELD else r["path"].count("/")


def _evidence(r: dict) -> str | None:
    """The kind a copy in another place says: `out` for a deliverable, `clean` for what went up."""
    path = r["path"]
    if r["place"] == "host":
        return "out" if path.startswith("deliverables/") else "clean" if path.startswith("input/") else None
    m = _OWNER_RE.match(path) if r["place"] == "owner-bucket" else _LAB_RE.match(path)
    if not m:
        return None
    return "clean" if m.group(m.lastindex) == "in" else "out"


def layout_kind(r: dict, month: str | None) -> str | None:
    """The kind of the layout folder a laptop file lies in already (YYYY-MM/tcp-xxxx/private|clean|out/ of its own
    project and month), else None."""
    m = _LAYOUT_RE.search(r["path"])
    if not m or m.group(2) != r["project"] or (month and m.group(1) != month):
        return None
    return m.group(3)


def kind_of(r: dict, others: list[dict], month: str | None) -> str:
    here = layout_kind(r, month)
    if here:
        return here
    said = {_evidence(o) for o in others}
    if "out" in said:
        return "out"
    if "clean" in said:
        return "clean"
    segments = r["path"].lower().split("/")[:-1]
    if "clean" in segments or ".clean." in r["path"].lower():
        return "clean"
    return "out" if "out" in segments else "private"


def months(p: config.Paths, rows: list[dict]) -> dict[str, str]:
    """The month of each project: that of its folder in the owner bucket, else that of `created` in the register."""
    found: dict[str, str] = {}
    try:
        for r in projects.load(p):
            if r.state != "deleted" and r.created:
                found[r.code] = r.created[:7]
    except projects.ProjectError:
        pass
    for r in rows:
        m = _OWNER_RE.match(r["path"]) if r["place"] == "owner-bucket" else None
        if m and m.group(2) == r["project"]:
            found[r["project"]] = m.group(1)
    return found


# --------------------------------------------------------------------------- the plan


def _row(r: dict, action: str, target: str = "", keeper: str = "", reason: str = "") -> dict:
    return {"id": r["id"], "place": r["place"], "action": action, "target": target, "project": r["project"],
            "path": r["path"], "keeper": keeper, "reason": reason}


def _newest_first(members: list[dict]) -> list[dict]:
    members = sorted(members, key=lambda r: r["id"])
    members.sort(key=lambda r: r["mtime"], reverse=True)
    return members


def plan(p: config.Paths, rows: list[dict], when: str) -> tuple[list[dict], list[tuple[dict, dict]], dict]:
    """The plan rows, the probable pairs and the facts the readable plan shows (months, groups)."""
    root = identical(rows)
    members: dict[int, list[int]] = {}
    for i, k in enumerate(root):
        members.setdefault(k, []).append(i)
    month = months(p, rows)

    # a laptop file without a project takes the one project of its identical copies elsewhere
    ambiguous: set[str] = set()
    for i, r in enumerate(rows):
        if r["place"] == "laptop" and r["project"] == inventory.NO_PROJECT:
            found = {rows[j]["project"] for j in members[root[i]]
                     if rows[j]["place"] != "laptop" and rows[j]["project"] != inventory.NO_PROJECT}
            if len(found) == 1:
                r["project"] = found.pop()
            elif found:
                ambiguous.add(r["id"])
    for r in rows:
        if r["place"] == "laptop":
            r["group"] = inventory.group_of(r["project"], r["path"], r["suffix"])

    def archive_to(r: dict) -> str:
        m = month.get(r["project"])
        if r["project"] != inventory.NO_PROJECT and m:
            return "%s/%s/%s/%s/" % (m, r["project"], ARCHIVE, when)
        return "%s/%s/" % (ARCHIVE, when)

    out: dict[str, dict] = {}
    kind: dict[str, str] = {}
    survivors: list[dict] = []
    for k, idx in members.items():
        laptop = [rows[i] for i in idx if rows[i]["place"] == "laptop"]
        if not laptop:
            continue
        others = [rows[i] for i in idx if rows[i]["place"] != "laptop"]
        laptop.sort(key=lambda r: r["id"])
        laptop.sort(key=lambda r: r["mtime"], reverse=True)
        laptop.sort(key=_depth)
        laptop.sort(key=lambda r: layout_kind(r, month.get(r["project"])) is None)
        keeper = laptop[0]
        survivors.append(keeper)
        kind[keeper["id"]] = kind_of(keeper, others, month.get(keeper["project"]))
        for r in laptop[1:]:
            out[r["id"]] = _row(r, "archive", archive_to(r), keeper["id"], "identical")

    # versions: the newest of a group and kind stays, the older ones go to _archive
    groups: dict[tuple[str, str], list[dict]] = {}
    for r in survivors:
        if r["project"] != inventory.NO_PROJECT and r["group"] != inventory.WITHHELD:
            groups.setdefault((r["group"], kind[r["id"]]), []).append(r)
    for members_of in groups.values():
        ordered = _newest_first(members_of)
        for r in ordered[1:]:
            out[r["id"]] = _row(r, "archive", archive_to(r), ordered[0]["id"], "older version")

    for r in survivors:
        if r["id"] in out:
            continue
        m = month.get(r["project"])
        if r["id"] in ambiguous:
            out[r["id"]] = _row(r, "keep", reason="identical to copies of more than one project")
        elif r["project"] == inventory.NO_PROJECT:
            out[r["id"]] = _row(r, "keep", reason="no project")
        elif not m:
            out[r["id"]] = _row(r, "keep", reason="no month for the project")
        elif layout_kind(r, m) == kind[r["id"]]:
            out[r["id"]] = _row(r, "keep", reason="in its layout place")
        else:
            out[r["id"]] = _row(r, "move", "%s/%s/%s/" % (m, r["project"], kind[r["id"]]), reason=kind[r["id"]])

    # the host and the buckets: listed only
    for r in rows:
        if r["place"] == "owner-bucket" and r["project"] != inventory.NO_PROJECT:
            m = _OWNER_RE.match(r["path"])
            if m and m.group(3) == "in":
                out[r["id"]] = _row(r, "stale-in", reason="the import should have taken it")
            elif r["path"] == inventory.WITHHELD:
                out[r["id"]] = _row(r, "withheld", reason="in/ or out/ of the project folder, path withheld")
    for idx in members.values():
        scoped: dict[tuple[str, str], list[dict]] = {}
        for i in idx:
            r = rows[i]
            if r["place"] == "laptop" or r["project"] == inventory.NO_PROJECT:
                continue
            if r["place"] != "host":
                m = (_OWNER_RE if r["place"] == "owner-bucket" else _LAB_RE).match(r["path"])
                if not m or m.group(m.lastindex) not in ("out", "from-session"):
                    continue
            scoped.setdefault((r["place"], r["project"]), []).append(r)
        for copies in scoped.values():
            if len(copies) < 2:
                continue
            copies = _newest_first(copies)
            copies.sort(key=_depth)
            for r in copies[1:]:
                out.setdefault(r["id"], _row(r, "duplicate", keeper=copies[0]["id"], reason="identical"))

    order = {pl: n for n, pl in enumerate(inventory.PLACES)}
    plan_rows = sorted(out.values(), key=lambda x: (order[x["place"]], x["project"], x["id"]))
    pairs = [(rows[i], rows[j]) for i, j in probable(rows, root)]
    return plan_rows, pairs, {"months": month, "groups": {r["id"]: r["group"] for r in rows}}


# --------------------------------------------------------------------------- the files


def tsv(plan_rows: list[dict]) -> str:
    return inventory.text(plan_rows, COLUMNS)


def _md(value: str) -> str:
    return (value or "").replace("|", "\\|")


def markdown(plan_rows: list[dict], pairs: list[tuple[dict, dict]], facts: dict, when: str,
             sources: list[str], notes: list[str]) -> str:
    """PLAN.md: the counts, then the laptop part per project and group, the host and bucket listings and the
    probable copies. Codes, ids and coded paths only."""
    lines = ["# Move plan %s" % when, "",
             "Made by `awb files plan` from %s. Nothing moves until `awb clean files apply --go` on the laptop. "
             "Nothing is deleted anywhere: `_archive` is a folder, not a bin." % ", ".join(sources or ["no inventory"]),
             ""]
    lines += ["- %s" % n for n in notes] + ([""] if notes else [])
    counts: dict[tuple[str, str], int] = {}
    for r in plan_rows:
        counts[(r["place"], r["action"])] = counts.get((r["place"], r["action"]), 0) + 1
    lines += ["## Counts", "", "| place | action | rows |", "|---|---|---|"]
    lines += ["| %s | %s | %d |" % (pl, ac, n) for (pl, ac), n in sorted(counts.items())]
    lines.append("")
    laptop = [r for r in plan_rows if r["place"] == "laptop"]
    for project in sorted({r["project"] for r in laptop}, key=lambda c: (c == inventory.NO_PROJECT, c)):
        mine = [r for r in laptop if r["project"] == project]
        if project == inventory.NO_PROJECT:
            lines += ["## Laptop files without a project", ""]
        else:
            lines += ["## %s (month %s)" % (project, facts["months"].get(project, "not known")), ""]
        lines += ["| group | id | path | action | target | keeper | reason |", "|---|---|---|---|---|---|---|"]
        mine.sort(key=lambda r: (facts["groups"].get(r["id"], ""), r["action"] != "keep", r["action"], r["id"]))
        for r in mine:
            lines.append("| %s | %s | %s | %s | %s | %s | %s |" % (
                _md(facts["groups"].get(r["id"], "")), r["id"], _md(r["path"]), r["action"], _md(r["target"]),
                r["keeper"], _md(r["reason"])))
        lines.append("")
    listed = [r for r in plan_rows if r["place"] != "laptop"]
    lines += ["## Host and buckets (listed only, never moved by this step)", ""]
    if listed:
        lines += ["| place | project | id | path | action | keeper | reason |", "|---|---|---|---|---|---|---|"]
        lines += ["| %s | %s | %s | %s | %s | %s | %s |" % (r["place"], r["project"], r["id"], _md(r["path"]),
                                                          r["action"], r["keeper"], _md(r["reason"])) for r in listed]
    else:
        lines.append("Nothing to list.")
    lines += ["", "## Probable copies (equal size, suffix and time, no hash to compare)", ""]
    if pairs:
        lines += ["| id | path | id | path |", "|---|---|---|---|"]
        lines += ["| %s | %s | %s | %s |" % (a["id"], _md(a["path"]), b["id"], _md(b["path"])) for a, b in pairs]
    else:
        lines.append("None.")
    return "\n".join(lines) + "\n"


def _write(path: Path, data: str) -> None:
    from awb.filesinv import _write as write

    write(path, data)


def make(p: config.Paths, laptop: list[str], host: Path | None, buckets: list[Path] | None, *, put: bool = False,
         client=None, now: _dt.datetime | None = None) -> dict:
    """Read, check, match, write and (with `put`) put. Returns the facts of the run for the summary line."""
    from awb import filesinv

    when = inventory.stamp(now)
    files = p.shared / filesinv.FILES_DIR
    notes: list[str] = []
    sources: list[str] = []
    rows: list[dict] = []
    dropped = 0
    with tempfile.TemporaryDirectory() as tmp:
        if laptop:
            got, names = laptop_rows(laptop, client, Path(tmp))
            good, bad = clean_rows(got, ("laptop",))
            withheld = filesinv.check(good, ledger.register_path(p))
            if withheld:
                notes.append("%d laptop path%s withheld at entry" % (withheld, "" if withheld == 1 else "s"))
            rows += good
            dropped += bad
            sources += names
    if host is None:
        host = newest_local(files, "host")
        if host is None:
            notes.append("no host inventory (sudo -n -u awb -H awb files inventory --host)")
    if buckets is None:
        buckets = [f for f in (newest_local(files, "owner-bucket"), newest_local(files, "lab-bucket")) if f]
        if not buckets:
            notes.append("no bucket inventory (awb files inventory --buckets)")
    for f in ([host] if host else []) + list(buckets):
        good, bad = clean_rows(inventory.read(f), ("host", "owner-bucket", "lab-bucket"))
        rows += good
        dropped += bad
        sources.append(f.name)
    if not sources:
        raise PlanError("no inventory to plan from")
    seen: set[str] = set()
    unique = []
    for r in rows:
        if r["id"] in seen:
            dropped += 1
            continue
        seen.add(r["id"])
        unique.append(r)
    if dropped:
        notes.append("%d row%s dropped (not the shape of an inventory row, or an id twice)"
                     % (dropped, "" if dropped == 1 else "s"))
    plan_rows, pairs, facts = plan(p, unique, when)
    folder = config.make_dir(files, 0o750, shared=True) / ("plan-%s" % when)
    try:
        folder.mkdir(mode=0o750)
    except FileExistsError:
        raise PlanError("a plan of this second exists already; run it again") from None
    _write(folder / PLAN_FILE, tsv(plan_rows))
    _write(folder / MD_FILE, markdown(plan_rows, pairs, facts, when, sources, notes))
    laptop_part = [r for r in plan_rows if r["place"] == "laptop"]
    key = None
    if put:
        from awb import bucket

        c = client or bucket.client()
        part = folder / (".laptop-" + PLAN_FILE)
        _write(part, tsv(laptop_part))
        try:
            key = bucket_layout.plan_key(when)
            c.put_file(key, part, "text/tab-separated-values")
        finally:
            part.unlink(missing_ok=True)
    count = {a: sum(1 for r in laptop_part if r["action"] == a) for a in ("keep", "move", "archive")}
    return {"folder": folder, "laptop": count, "listed": len(plan_rows) - len(laptop_part), "pairs": len(pairs),
            "unassigned": sum(1 for r in laptop_part if r["project"] == inventory.NO_PROJECT), "notes": notes,
            "key": key, "stamp": when}


def add_parser(sub) -> None:
    s = sub.add_parser("plan", help="the move plan from the three inventories: laptop moves into the layout and "
                                    "_archive, host and bucket duplicates listed only")
    s.add_argument("--laptop", action="append", default=None, metavar="FILE|NAME|latest",
                   help="a laptop inventory: latest (the default, the newest under reports/inventory/ of the "
                        "owner bucket), a NAME there or a local FILE; may be given more than once")
    s.add_argument("--host", dest="host_file", type=Path, default=None, metavar="FILE",
                   help="the host inventory; default the newest host-<stamp>.tsv in <shared>/files/")
    s.add_argument("--buckets", dest="bucket_files", type=Path, nargs="+", default=None, metavar="FILE",
                   help="the bucket inventories; default the newest owner-bucket- and lab-bucket-<stamp>.tsv there")
    s.add_argument("--put", action="store_true",
                   help="also put the laptop rows to reports/plan/<stamp>/PLAN.tsv of the owner bucket")


def run(args) -> int:
    from awb import bucket, obs

    try:
        res = make(config.paths(), args.laptop or [LATEST], args.host_file, args.bucket_files, put=args.put)
    except (PlanError, inventory.InventoryError, ledger.LedgerError, bucket.BucketError) as err:
        print("%s: %s" % (PROG, err), file=sys.stderr)
        return EXIT_ERROR
    except obs.OBSError as err:
        print("%s: the object storage refused a call (HTTP %s)" % (PROG, err.status), file=sys.stderr)
        return EXIT_ERROR
    except OSError as err:
        print("%s: %s (operating system error)" % (PROG, type(err).__name__), file=sys.stderr)
        return EXIT_ERROR
    for note in res["notes"]:
        print("%s: %s" % (PROG, note))
    n = res["laptop"]
    print("%s: %s written; laptop %d keep, %d move, %d archive (%d without a project); %d host and bucket row%s "
          "listed; %d probable pair%s; nothing moved, nothing deleted%s" % (
              PROG, res["folder"], n["keep"], n["move"], n["archive"], res["unassigned"], res["listed"],
              "" if res["listed"] == 1 else "s", res["pairs"], "" if res["pairs"] == 1 else "s",
              "; put %s" % res["key"] if res["key"] else ""))
    return EXIT_OK
