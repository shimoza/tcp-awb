"""The bucket rows of a move plan carried out (task BK3; the design of the buckets accepted 2026-11-06, D-B3).

    awb files tidy --go PLAN [--only owner-bucket|lab-bucket] [--project CODE] [--dry-run]

PLAN is the PLAN.tsv of `awb files plan` (awb/filesplan.py) or its folder `<shared>/files/plan-<stamp>/`. Owner side
only: refused as the work user and inside an assistant session; a run without a terminal refuses (the word `yes`
is typed there). A plan whose stamp is more than FRESH_HOURS (24) hours old is refused with one line: make a new
plan. The owner strikes a row by deleting its line from PLAN.tsv before the run (D-B3); the command reads the
columns by their names and never adds a row.

Only the rows of the buckets (place owner-bucket or lab-bucket) are read, those of one bucket with `--only`, of one
project with `--project`. Each row is checked against the bucket before it is acted on: a row whose path is
`[withheld]`, whose key or target does not have the shape the plan gives its action or whose object is gone or has
another size than the row says is skipped with its reason.

    keep, withheld           nothing
    move, unsorted, archive  copy to the target key, head both, compare the size and the md5 (the etag of a
                             single-part object; with a multipart etag the size alone and the log says so), then
                             delete the source; a target that exists already is skipped, never overwritten; a
                             copy that differs leaves the source in place
    delete                   only when the keeper the row names exists with the size of its row and the md5 of the
                             object (the size, when an etag is multipart) or for an object of in/ whose key and md5
                             the vault's record of a take or an import still holds (reason stale-in); a delete
                             without a keeper that is not stale-in is refused

The moves come first, then the deletes, so a keeper the run moved is looked for at its target. Before anything
changes the run prints what it will carry out: the rows and bytes per bucket and action and each delete with its key
and size, then asks for `yes`. `--dry-run` prints the same and stops: no question, no call to a bucket, no change.

Each row acted on is a line of `<shared>/files/tidy-<stamp>/LOG.tsv` (id, action, source, target, result ok,
skipped or failed, reason), written as it goes. At the end the log goes up to `reports/tidy/<stamp>/LOG.tsv` of the
owner bucket (`bucket_layout.tidy_key`) and one summary line follows. A failed row never stops the others. Exit 0
done, 1 when a row failed or the log did not go up, 2 refused before anything changed.

The key. The key service answers the owner's bucket calls with one pair, the lab key of the tenant `bucket_tenant`
of keys.conf; it offers no copy and no delete (awb/bucket.py). The tidy reads that same pair from the password store
(`awb/tenant/<alias>/lab/ak` and `sk`, `obs.keys_from_reference`) in the owner's own terminal at call time, for the
buckets owner_bucket and lab_bucket of keys.conf in its region; nothing keeps it. Keys in the log and on the terminal
are the plan's coded keys. After an archive of a lab put nothing else is needed: INDEX.md drops the rows whose object
is gone on the next put (task BK1). Standard library only.
"""
from __future__ import annotations

import datetime as _dt
import os
import re
import sys
from pathlib import Path

from awb import bucket_layout, codes, config, filesplan, inventory, obs

PROG = "awb files tidy"
FRESH_HOURS = 24
NEEDED = ("id", "place", "action", "target", "project", "path", "keeper", "reason", "size")
LOG_COLUMNS = ("id", "action", "source", "target", "result", "reason")
MOVES = ("move", "unsorted", "archive")
ACTS = MOVES + ("delete",)
OK, SKIPPED, FAILED = "ok", "skipped", "failed"
STALE_IN = "stale-in"
YES = "yes"
EXIT_OK, EXIT_FAILED, EXIT_ERROR = 0, 1, 2

_PLAN_DIR_RE = re.compile(r"^plan-([0-9]{8}-[0-9]{6})$")
_CODE_RE = re.compile("^%s$" % codes.TCP_PROJECT_WIDE)
_ALIAS_RE = re.compile(r"^[a-z][a-z0-9-]{1,30}$")
_FOLDER = r"(%s)/(%s)/" % (bucket_layout.MONTH, codes.TCP_PROJECT_WIDE)
_OWNER_LAYOUT_RE = re.compile("^%s(?:in|out|%s)/" % (_FOLDER, filesplan.ARCHIVE))
_OWNER_MOVE_RE = re.compile(r"^%s(?:in|out)/([^/]+)$" % _FOLDER)
_OWNER_OUT_RE = re.compile(r"^%sout/([^/]+)$" % _FOLDER)
_OWNER_DELETE_RE = re.compile(r"^%s(in|out)/." % _FOLDER)
_LAB_PUT_RE = re.compile(r"^(%s)/from-session/." % codes.TCP_PROJECT_WIDE)


class TidyError(Exception):
    """The run is refused before anything changed. The message carries codes, counts and reasons, never a name."""


# --------------------------------------------------------------------------- the plan


def read_plan(spec: Path) -> tuple[Path, str, list[dict]]:
    """(PLAN.tsv, the stamp of its folder, its bucket rows by the names of its header). The file is opened without
    following a link."""
    path = Path(spec)
    if path.is_dir():
        path = path / filesplan.PLAN_FILE
    m = _PLAN_DIR_RE.match(path.parent.name)
    if path.name != filesplan.PLAN_FILE or not m:
        raise TidyError("PLAN is the PLAN.tsv of awb files plan or its folder plan-<stamp>/")
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
        with os.fdopen(fd, encoding="utf-8") as fh:
            lines = fh.read().splitlines()
    except (OSError, UnicodeDecodeError) as err:
        raise TidyError("the plan cannot be read (%s)" % type(err).__name__) from None
    head = lines[0].split("\t") if lines else []
    missing = [c for c in NEEDED if c not in head]
    if missing:
        raise TidyError("the header of the plan lacks %s" % ", ".join(missing))
    rows = []
    for line in lines[1:]:
        cells = line.split("\t")
        row = {c: cells[i] if i < len(cells) else "" for i, c in enumerate(head)}
        if row["place"] in filesplan.BUCKETS:
            rows.append(row)
    return path, m.group(1), rows


def check_fresh(stamp: str, now: _dt.datetime | None = None) -> None:
    made = _dt.datetime.strptime(stamp, inventory.STAMP_FORMAT).replace(tzinfo=_dt.timezone.utc)
    if (now or _dt.datetime.now(_dt.timezone.utc)) - made > _dt.timedelta(hours=FRESH_HOURS):
        raise TidyError("the plan is older than %d hours: make a new plan (awb files plan)" % FRESH_HOURS)


def shape_ok(row: dict, stamp: str) -> bool:
    """The key and the target have the shape `awb files plan` gives the action (filesplan.bucket_part): a row
    edited by hand into anything else is never acted on."""
    path, target, action = row["path"], row["target"], row["action"]
    if bucket_layout.is_keep(path) or (target and not filesplan._key_ok(target)):
        return False
    name = path.rsplit("/", 1)[-1]
    if row["place"] == "owner-bucket":
        loose = not _OWNER_LAYOUT_RE.match(path) and path.split("/", 1)[0] not in filesplan.OWNER_TOP
        if action == "move":
            m = _OWNER_MOVE_RE.match(target)
            return loose and bool(m) and m.group(3) == name
        if action == "unsorted":
            return loose and not path.startswith(filesplan.UNSORTED + "/") and \
                target == "%s/%s/%s" % (filesplan.UNSORTED, stamp, path)
        if action == "archive":
            m = _OWNER_OUT_RE.match(path)
            return bool(m) and target == "%s/%s/%s/%s/%s" % (m.group(1), m.group(2), filesplan.ARCHIVE, stamp, name)
        if action == "delete":
            m = _OWNER_DELETE_RE.match(path)
            want = "in" if row["reason"].startswith(STALE_IN) else "out"
            return not target and bool(m) and m.group(3) == want
        return False
    m = _LAB_PUT_RE.match(path)
    if not m or path == "%s/from-session/INDEX.md" % m.group(1):
        return False
    parts = path.split("/")
    if action == "archive":
        return len(parts) > 3 and target == "%s/%s/%s/%s" % (parts[0], filesplan.ARCHIVE, parts[2][:7],
                                                              "/".join(parts[1:]))
    return action == "delete" and not target and not row["reason"].startswith(STALE_IN)


def picked(rows: list[dict], only: str | None, project: str | None) -> list[dict]:
    return [r for r in rows if (only is None or r["place"] == only) and (project is None or r["project"] == project)]


def summary(rows: list[dict], stamp: str) -> list[str]:
    """What the run will carry out: rows and bytes per bucket and action, what it leaves, each delete."""
    per: dict[tuple[str, str], list[int]] = {}
    left: dict[str, int] = {}
    for r in rows:
        if r["action"] in ACTS:
            n = per.setdefault((r["place"], r["action"]), [0, 0])
            n[0] += 1
            n[1] += int(r["size"]) if r["size"].isdigit() else 0
        else:
            left[r["action"] or "no action"] = left.get(r["action"] or "no action", 0) + 1
    lines = ["%s: plan %s, %d bucket row%s" % (PROG, stamp, len(rows), "" if len(rows) == 1 else "s")]
    for place in filesplan.BUCKETS:
        for action in ACTS:
            if (place, action) in per:
                n, size = per[(place, action)]
                lines.append("  %-12s %-9s %5d row%s %14d bytes" % (place, action, n, " " if n == 1 else "s", size))
    if left:
        lines.append("  nothing for %s" % ", ".join("%d %s" % (n, a) for a, n in sorted(left.items())))
    for r in rows:
        if r["action"] == "delete":
            why = STALE_IN if r["reason"].startswith(STALE_IN) else "keeper %s" % (r["keeper"] or "none: refused")
            lines.append("delete %s %s %s bytes (%s)" % (r["place"], r["path"], r["size"] or "?", why))
    return lines


# --------------------------------------------------------------------------- the bucket


def _header(h: dict, name: str) -> str:
    for k, v in (h or {}).items():
        if k.lower() == name.lower():
            return str(v)
    return ""


def _size(h: dict) -> int:
    value = _header(h, "Content-Length")
    return int(value) if value.isdigit() else -1


def _md5(h: dict) -> str:
    from awb.filesinv import md5_of_etag

    return md5_of_etag(_header(h, "ETag"))


def _http(err: Exception) -> str:
    return "HTTP %s" % err.status if isinstance(err, obs.OBSError) else type(err).__name__


def move_object(c, path: str, target: str) -> tuple[str, str]:
    """One object moved inside its bucket: copy to `target`, head both, compare the size and the md5 (the size alone
    with a multipart etag), then delete the source. A target that exists already is skipped, never overwritten; a
    copy that differs leaves the source in place. Also the archive moves of task BK4 (awb fetch, awb bucket put)."""
    if c.head(target) is not None:
        return SKIPPED, "the target exists already; never overwritten"
    try:
        c.copy(path, target)
    except Exception as err:
        return FAILED, "the copy failed (%s); the source stays" % _http(err)
    got, source = c.head(target), c.head(path)
    if got is None:
        return FAILED, "the copy is not there; the source stays"
    if source is None:
        return FAILED, "the source is gone after the copy"
    if _size(got) != _size(source):
        return FAILED, "the copy differs in size; the source stays"
    a, b = _md5(source), _md5(got)
    if a and b and a != b:
        return FAILED, "the copy differs in md5; the source stays"
    how = "size and md5 compared" if a and b else "size compared only (multipart etag)"
    try:
        c.delete(path)
    except Exception as err:
        return FAILED, "copied (%s), the source could not be deleted (%s)" % (how, _http(err))
    return OK, "copied, %s, source deleted" % how


class _Run:
    """One run over the picked rows; `log` takes each line as it is decided."""

    def __init__(self, rows: list[dict], every: list[dict], stamp: str, clients: dict, taken, log):
        self.rows, self.stamp, self.clients, self.taken, self.log = rows, stamp, clients, taken, log
        self.by_id = {}
        for r in every:
            self.by_id.setdefault(r["id"], r)
        self.moved: dict[str, str] = {}
        self.gone: set[str] = set()
        self.counts = {OK: 0, SKIPPED: 0, FAILED: 0}

    def go(self) -> dict:
        seen: set[str] = set()
        todo = [r for r in self.rows if r["action"] in MOVES] + [r for r in self.rows if r["action"] == "delete"]
        for r in todo:
            if r["id"] in seen:
                self._line(r, SKIPPED, "the id is in the plan twice; carried out once")
                continue
            seen.add(r["id"])
            try:
                result, reason = self._one(r)
            except Exception as err:            # one row never stops the others
                result, reason = FAILED, "a call failed (%s)" % _http(err)
            self._line(r, result, reason)
        return self.counts

    def _line(self, r: dict, result: str, reason: str) -> None:
        self.counts[result] += 1
        self.log({"id": r["id"], "action": r["action"], "source": r["path"], "target": r["target"],
                  "result": result, "reason": reason})

    def _one(self, r: dict) -> tuple[str, str]:
        path = r["path"]
        if path == inventory.WITHHELD or not filesplan._key_ok(path):
            return SKIPPED, "path withheld, never acted on"
        if not r["size"].isdigit():
            return SKIPPED, "no size in the plan row"
        if not shape_ok(r, self.stamp):
            return SKIPPED, "the key or the target does not have the shape the plan gives %s" % r["action"]
        if r["action"] == "delete" and not r["reason"].startswith(STALE_IN) and not r["keeper"]:
            return SKIPPED, "refused: a delete without a keeper that is not stale-in"
        c = self.clients[r["place"]]
        h = c.head(path)
        if h is None:
            return SKIPPED, "the object is gone"
        if _size(h) != int(r["size"]):
            return SKIPPED, "the object changed size since the plan (%d bytes, the plan %s)" % (_size(h), r["size"])
        return self._delete(c, r, h) if r["action"] == "delete" else self._move(c, r)

    def _move(self, c, r: dict) -> tuple[str, str]:
        result, reason = move_object(c, r["path"], r["target"])
        if result == OK:
            self.moved[r["id"]] = r["target"]
        return result, reason

    def _delete(self, c, r: dict, h: dict) -> tuple[str, str]:
        path = r["path"]
        if r["reason"].startswith(STALE_IN):
            md5 = _md5(h)
            if not md5 or (path, md5) not in self.taken:
                return SKIPPED, "stale-in, but the vault's record of the imports holds not this key and md5"
            c.delete(path)
            self.gone.add(r["id"])
            return OK, "stale-in: the import took it, deleted"
        k = self.by_id.get(r["keeper"])
        if k is None or k["place"] != r["place"]:
            return SKIPPED, "the keeper has no row of this bucket in the plan"
        key = self.moved.get(k["id"], k["path"])
        if r["keeper"] in self.gone:
            return SKIPPED, "the keeper is gone"
        if key == inventory.WITHHELD or not filesplan._key_ok(key):
            return SKIPPED, "the path of the keeper is withheld"
        if key == path:
            return SKIPPED, "the keeper is the object itself"
        kh = c.head(key)
        if kh is None:
            return SKIPPED, "the keeper is gone"
        if k["size"].isdigit() and _size(kh) != int(k["size"]):
            return SKIPPED, "the keeper changed size since the plan"
        a, b = _md5(h), _md5(kh)
        if a and b:
            if a != b:
                return SKIPPED, "the keeper is not identical (md5)"
            how = "the keeper %s has the same md5" % k["id"]
        elif _size(kh) != _size(h):
            return SKIPPED, "the keeper is not identical (size)"
        else:
            how = "the keeper %s has the same size (multipart etag, size compared only)" % k["id"]
        c.delete(path)
        self.gone.add(r["id"])
        return OK, "%s, deleted" % how


# --------------------------------------------------------------------------- keys, log and run


def owner_clients() -> dict:
    """The clients of the two buckets with the pair the key service uses for the owner's bucket calls: the lab key of
    `bucket_tenant` of keys.conf, read from the password store now; owner_bucket, lab_bucket and region of keys.conf."""
    xchg, keys = filesplan._platform("xchg"), filesplan._platform("keys")
    try:
        s = xchg.read_settings()
    except xchg.XchgError as err:
        raise TidyError("keys.conf: %s" % err) from None
    alias = str(s.get("bucket_tenant") or "")
    if not _ALIAS_RE.match(alias):
        raise TidyError("keys.conf names no bucket_tenant: its lab key reaches both buckets")
    try:
        pair = obs.keys_from_reference("pass:%s/%s/lab" % (keys.STORE_PREFIX, alias))
    except obs.OBSError as err:
        raise TidyError("the lab key of the bucket tenant: %s" % err) from None
    region = str(s.get("region") or bucket_layout.REGION)
    return {"owner-bucket": obs.Client(str(s["owner_bucket"]), pair, region),
            "lab-bucket": obs.Client(str(s["lab_bucket"]), pair, region)}


def _terminal_confirm():
    """The question on the owner's terminal; refused without one, before anything is read."""
    if not (sys.stdin.isatty() and sys.stdout.isatty()):
        raise TidyError("a run without a terminal refuses: run it in your own terminal and type yes")

    def ask(prompt: str) -> str:
        try:
            return input(prompt)
        except EOFError:
            return ""
    return ask


def _cell(value: str) -> str:
    return re.sub(r"[\t\r\n]", " ", str(value))


def _open_log(p: config.Paths, when: str) -> Path:
    from awb import filesinv

    folder = config.make_dir(p.shared / filesinv.FILES_DIR, 0o750, shared=True) / ("tidy-%s" % when)
    try:
        folder.mkdir(mode=0o750)
    except FileExistsError:
        raise TidyError("a tidy of this second exists already; run it again") from None
    return folder / bucket_layout.TIDY_FILE


def tidy(p: config.Paths, plan: Path, *, clients=None, only: str | None = None, project: str | None = None,
         dry_run: bool = False, confirm=None, now: _dt.datetime | None = None, taken=None, say=print) -> int:
    """Check, show, ask, carry out, log and put the log. `clients` is {place: client} or a function that makes it
    (default owner_clients, called after the yes), `confirm(prompt)` gives the typed word (default the terminal),
    `taken` the (key, md5) of what the imports took (default filesplan.taken_in)."""
    from awb import bucket

    if config.is_work_user():
        raise TidyError("the buckets are the owner's: run awb files tidy in your own shell")
    try:
        bucket.owner_terminal(PROG)
    except bucket.BucketError as err:
        raise TidyError(str(err)) from None
    if only is not None and only not in filesplan.BUCKETS:
        raise TidyError("--only names owner-bucket or lab-bucket")
    if project is not None and not _CODE_RE.match(project):
        raise TidyError("--project names a project code")
    if not dry_run and confirm is None:
        confirm = _terminal_confirm()
    path, stamp, every = read_plan(plan)
    check_fresh(stamp, now)
    rows = picked(every, only, project)
    for line in summary(rows, stamp):
        say(line)
    todo = [r for r in rows if r["action"] in ACTS]
    if not todo:
        say("%s: nothing to carry out" % PROG)
        return EXIT_OK
    if dry_run:
        say("%s: dry run, nothing changed" % PROG)
        return EXIT_OK
    if confirm("Type %s to carry out these %d row%s: " % (YES, len(todo), "" if len(todo) == 1 else "s")).strip() \
            != YES:
        say("%s: no yes, nothing changed" % PROG)
        return EXIT_ERROR
    made = (clients or owner_clients)() if callable(clients) or clients is None else clients
    taken = filesplan.taken_in(p) if taken is None else taken
    when = inventory.stamp(now)
    log = _open_log(p, when)
    fd = os.open(log, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o640)
    with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fh:
        fh.write("\t".join(LOG_COLUMNS) + "\n")

        def write(entry: dict) -> None:
            fh.write("\t".join(_cell(entry[c]) for c in LOG_COLUMNS) + "\n")
            fh.flush()
            if entry["result"] != OK:
                say("%s %s %s: %s" % (entry["result"], entry["action"], entry["source"], entry["reason"]))

        counts = _Run(todo, every, stamp, made, taken, write).go()
    key = bucket_layout.tidy_key(when)
    put = ""
    try:
        made["owner-bucket"].put_file(key, log, "text/tab-separated-values")
        put = "; put %s" % key
    except Exception as err:
        put = "; the log did not go up (%s)" % _http(err)
    say("%s: %d row%s: %d ok, %d skipped, %d failed; log %s%s" % (
        PROG, len(todo), "" if len(todo) == 1 else "s", counts[OK], counts[SKIPPED], counts[FAILED], log, put))
    return EXIT_FAILED if counts[FAILED] or not put.startswith("; put") else EXIT_OK


def add_parser(sub) -> None:
    s = sub.add_parser("tidy", help="carry out the bucket rows of a move plan: move, unsorted and archive copied, "
                                    "compared and the source deleted, delete only with its keeper or stale-in; asks "
                                    "for yes in your own terminal")
    s.add_argument("--go", dest="plan", type=Path, required=True, metavar="PLAN",
                   help="the PLAN.tsv of awb files plan or its folder plan-<stamp>/, made less than 24 hours ago; "
                        "strike a row by deleting its line first")
    s.add_argument("--only", choices=filesplan.BUCKETS, default=None, metavar="BUCKET",
                   help="the rows of one bucket: owner-bucket or lab-bucket")
    s.add_argument("--project", default=None, metavar="CODE", help="the rows of one project")
    s.add_argument("--dry-run", action="store_true", help="print what would be carried out and change nothing")


def run(args) -> int:
    try:
        return tidy(config.paths(), args.plan, only=args.only, project=args.project, dry_run=args.dry_run)
    except TidyError as err:
        print("%s: %s" % (PROG, err), file=sys.stderr)
        return EXIT_ERROR
    except OSError as err:
        print("%s: %s (operating system error)" % (PROG, type(err).__name__), file=sys.stderr)
        return EXIT_ERROR
