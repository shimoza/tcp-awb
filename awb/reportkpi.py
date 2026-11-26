"""The KPI file of the owner's report workbook (presentations/reports-design, task 7, a follow-up of task 1).

`awb report --codes` writes it beside `projects.csv`, by the same route and in the same form (awb/reportcodes.py):
`--to-shared` on the work side, `--from-shared --put --if-changed` on the owner side, `awb clean report` on the
laptop. No option of its own: every run of `awb report --codes` that wrote or kept `projects.csv` also makes
`projects-kpi.csv` and `projects-kpi.meta.json` in the same folder.

One row per project of the project register whose start, last change, close or ledger entries fall on START or
later, closed and deleted ones too (the six-column file drops them after 30 days, this one keeps every row the
register keeps). Ten columns, codes only:

    project code, customer code, kind, goal, start date, close date, state, tags and technologies,
    ledger entries, main deliverables

- the kind is query, project or ask (an old kind is shown as the kind it became);
- the goal is the goal line of SCOPE.md;
- the start date is the created date of the register;
- the close date, for a closed or deleted project only, is the day its fingerprint was written on the shared side
  (`awb close` writes it), else the date of its last change;
- tags and technologies are the tags of SCOPE.md and of its ledger entries, sorted, comma separated;
- ledger entries is the count of its entries from START to today;
- main deliverables are the files of deliverables/ by type with counts, as `awb report --period` shows them
  ("pdf 3, md 2", "none" without one).

The goal, the tags and the deliverables are written as they stand, like the texts of projects.csv: no content check
(B-089). On the owner side the customer code, the kind, the start date and the state come from the register again and
every other cell is checked for its form. The sidecar carries the time written, the rows, the texts withheld, the sha256 and the release. The
state of its last put is `kpi-last.json` beside `last.json` of the six-column file.
"""
from __future__ import annotations

import csv
import datetime as _dt
import hashlib
import io
import json
import re
from dataclasses import dataclass
from pathlib import Path

from awb import bucket_layout, codes, config, ledger, period, projects, reportcodes
from awb.projectfacts import mtime_date

HEADERS = ("project code", "customer code", "kind", "goal", "start date", "close date", "state",
           "tags and technologies", "ledger entries", "main deliverables")
"""The ten column names, in this order and this spelling: a query of the workbook reads them."""
FILE_NAME = "projects-kpi.csv"
META_NAME = "projects-kpi.meta.json"
STATE_NAME = "kpi-last.json"
START = _dt.date(2026, 9, 1)
"""The first day of the file: the Workbench's first projects."""
KINDS = ("query", "project", projects.ASK_KIND)
MAX_ENTRIES = 10 ** 6
_TAGS_RE = re.compile(r"(?:%s)(?:, (?:%s))*" % ((projects._TAG_RE.pattern,) * 2))
_DELIVERABLES_RE = re.compile(r"none|[a-z0-9]{1,8} [0-9]{1,6}(?:, [a-z0-9]{1,8} [0-9]{1,6})*")


@dataclass
class Row:
    code: str
    customer: str
    kind: str
    goal: str
    start: str
    close: str
    state: str
    tags: str
    entries: int
    deliverables: str

    def cells(self) -> tuple[str, ...]:
        return (self.code, self.customer, self.kind, self.goal, self.start, self.close, self.state, self.tags,
                str(self.entries), self.deliverables)


# --------------------------------------------------------------------------- the rows


def kind_of(kind: str) -> str:
    return projects.LEGACY_KINDS.get(kind, kind)


def scope_tags(folder: Path) -> list[str]:
    """The tags of the `- tags:` line of SCOPE.md that have the form of a tag; none for "none" or no line."""
    try:
        text = projects._read_regular(folder / "SCOPE.md").decode("utf-8")
    except (OSError, UnicodeDecodeError):
        return []
    for line in text.splitlines():
        if line.startswith("- tags:"):
            words = [w.strip() for w in line[len("- tags:"):].split(",")]
            return [w for w in words if w and w != "none" and projects._TAG_RE.fullmatch(w)]
    return []


def _iso(value: str) -> str:
    try:
        day = _dt.date.fromisoformat(value)
    except (TypeError, ValueError):
        return ""
    return day.isoformat() if day.isoformat() == value else ""


def close_date(p: config.Paths, code: str, folder: Path | None, created: str) -> _dt.date | None:
    """The day the fingerprint of the close was written, else the last change of the project."""
    from awb import review

    written = mtime_date(review.fingerprint_path(p, code))
    return written if written is not None else reportcodes.last_change(folder, created)


def gather(p: config.Paths, today: _dt.date | None = None) -> list[Row]:
    """One row per project of the file, sorted by project code, before the check of the texts. Raises
    reportcodes.CodesError when a project folder that is still there cannot be read here."""
    today = today or ledger.today()
    per_project: dict[str, list[ledger.Entry]] = {}
    for e in ledger.load(p, START, today):
        if codes.is_project_code(e.project):
            per_project.setdefault(e.project, []).append(e)
    out: list[Row] = []
    unreadable = 0
    for r in projects.load(p):
        folder = Path(r.path)
        try:
            usable = reportcodes._usable(folder)
        except PermissionError:
            unreadable += 1
            continue
        entries = per_project.get(r.code, [])
        changed = reportcodes.last_change(folder if usable else None, r.created)
        closed = None
        if r.state in ("closed", "deleted"):
            closed = close_date(p, r.code, folder if usable else None, r.created)
        start = _iso(r.created)
        if not (entries or (start and start >= START.isoformat())
                or any(d is not None and d >= START for d in (changed, closed))):
            continue
        tags = set(scope_tags(folder) if usable else [])
        for e in entries:
            tags.update(t for t in e.tags if projects._TAG_RE.fullmatch(t))
        goal = projects._plain(projects.goal_of(folder)) if usable else ""
        made = period._counts(period.deliverable_types(folder)) if usable else "none"
        out.append(Row(r.code, r.customer or projects.NO_CUSTOMER, kind_of(r.kind), goal, start,
                       closed.isoformat() if closed else "", r.state, ", ".join(sorted(tags)), len(entries), made))
    if unreadable:
        raise reportcodes.CodesError(
            "%d project folder%s cannot be read by this user for %s; on a sealed host the work side reads them "
            "(awb report --codes --to-shared) and the owner runs awb report --codes --from-shared; nothing is "
            "written" % (unreadable, "" if unreadable == 1 else "s", FILE_NAME))
    return sorted(out, key=lambda row: row.code)


# --------------------------------------------------------------------------- the owner side of a sealed host


def read_shared(p: config.Paths, path: Path, now: float | None = None) -> list[Row]:
    """The rows of the KPI file the work side wrote, checked as `reportcodes.from_shared` checks its file: the ten
    headers, one registered project code per row (an unknown code is left out), the customer code, the kind, the
    start date and the state of the register, an ISO close date or nothing, a count, tags and deliverables of their
    form or withheld. Raises reportcodes.CodesError for anything else."""
    err = reportcodes.CodesError
    text = reportcodes._read_shared(path, now)
    try:
        table = list(csv.reader(io.StringIO(text, newline=""), strict=True))
    except csv.Error:
        raise err("the KPI file of the work side is no CSV") from None
    if not table or tuple(table[0]) != HEADERS:
        raise err("the KPI file of the work side has another header")
    known = {r.code: r for r in projects.load(p)}
    out: list[Row] = []
    seen: set[str] = set()
    for n, cells in enumerate(table[1:], start=2):
        if len(cells) != len(HEADERS):
            raise err("row %d of the KPI file of the work side has %d cells" % (n, len(cells)))
        code, _customer, _kind, goal, _start, close, _state, tags, entries, made = cells
        if not codes.is_project_code(code) or code in seen:
            raise err("row %d of the KPI file of the work side has no project code or a second one" % n)
        seen.add(code)
        if close and _iso(close) != close:
            raise err("row %d of the KPI file of the work side has no ISO close date" % n)
        if not re.fullmatch(r"[0-9]{1,7}", entries) or int(entries) >= MAX_ENTRIES:
            raise err("row %d of the KPI file of the work side has no count of ledger entries" % n)
        if tags and tags != ledger.WITHHELD and not _TAGS_RE.fullmatch(tags):
            raise err("row %d of the KPI file of the work side has tags of another form" % n)
        if made != ledger.WITHHELD and not _DELIVERABLES_RE.fullmatch(made):
            raise err("row %d of the KPI file of the work side has deliverables of another form" % n)
        if len(goal) > reportcodes.TEXT_MAX or len(tags) > reportcodes.TEXT_MAX or len(made) > reportcodes.TEXT_MAX:
            raise err("row %d of the KPI file of the work side has a text longer than %d" % (n, reportcodes.TEXT_MAX))
        r = known.get(code)
        if r is None:
            continue
        out.append(Row(code, r.customer or projects.NO_CUSTOMER, kind_of(r.kind), goal, _iso(r.created), close,
                       r.state, tags, int(entries), made))
    return sorted(out, key=lambda row: row.code)


# --------------------------------------------------------------------------- the check and the file


TEXTS = ("goal", "tags", "deliverables")


def check(rows: list[Row], reg: Path) -> int:
    """No content check (B-089: the file goes to the owner): `reg` is kept as an ignored argument. Returns the count
    of texts withheld, which is only one a work side of an older release withheld."""
    return sum(1 for row in rows for f in TEXTS if getattr(row, f) == ledger.WITHHELD)


def render(rows: list[Row]) -> bytes:
    """The CSV in the form of projects.csv: a byte order mark, the header, the rows, CRLF, every field quoted."""
    q = reportcodes._quote
    lines = [",".join(q(c) for c in HEADERS)] + [",".join(q(c) for c in row.cells()) for row in rows]
    return (reportcodes.BOM + "".join(line + "\r\n" for line in lines)).encode("utf-8")


def meta(data: bytes, rows: int, withheld: int, now: _dt.datetime | None = None) -> dict:
    return dict(reportcodes.meta(data, rows, withheld, now), file=FILE_NAME)


def state_path() -> Path:
    return reportcodes.state_dir() / STATE_NAME


def put(out: Path, side: Path, client=None) -> list[str]:
    """The KPI file and its sidecar under reports/ of the owner bucket, through the key service."""
    from awb import bucket

    c = client or bucket.client()
    keys = []
    for src, key, ctype in ((out, bucket_layout.REPORTS_PREFIX + FILE_NAME, "text/csv"),
                            (side, bucket_layout.REPORTS_PREFIX + META_NAME, "application/json")):
        c.put_file(key, src, ctype)
        keys.append(key)
    return keys


# --------------------------------------------------------------------------- the run beside projects.csv


def run(p: config.Paths, codes_out: Path, *, from_shared: bool = False,
        if_changed: bool = False, want_put: bool = False, prog: str = "awb report --codes",
        today: _dt.date | None = None) -> int:
    """Make, write and put the KPI file beside `codes_out` (the projects.csv of this run). Exit 0 written,
    unchanged, a put skipped while the key service is locked or no KPI file of the work side yet; 1 the put failed;
    2 refused or an error. Prints its own lines; a message carries codes and counts, never a text."""
    import sys

    from awb import bucket, obs

    out = codes_out.with_name(FILE_NAME)
    if codes_out.name == FILE_NAME:
        print("%s: --out names %s itself; give projects.csv or another name, %s goes beside it"
              % (prog, FILE_NAME, FILE_NAME), file=sys.stderr)
        return reportcodes.EXIT_ERROR
    side = out.with_name(META_NAME)
    handed = reportcodes.shared_dir(p) / FILE_NAME
    if from_shared and not handed.exists() and not handed.is_symlink():
        print("%s: %s skipped, the work side has written none yet (its timer %s)"
              % (prog, FILE_NAME, reportcodes.WORK_TIMER))
        return reportcodes.EXIT_OK
    try:
        rows = read_shared(p, handed) if from_shared else gather(p, today)
        withheld = check(rows, ledger.register_path(p))
        data = render(rows)
        sha = hashlib.sha256(data).hexdigest()
        word = "written"
        if if_changed and reportcodes._same(out, data) and side.is_file():
            last = reportcodes.read_state(state_path())
            word = "unchanged" if not want_put or (last.get("state") == "put" and last.get("sha256") == sha) \
                else "unput"
        if word == "written":
            reportcodes._write(out, data)
            reportcodes._write(side, (json.dumps(meta(data, len(rows), withheld), indent=1) + "\n").encode("utf-8"))
    except (reportcodes.CodesError, ledger.LedgerError, projects.ProjectError) as err:
        print("%s: %s: %s" % (prog, FILE_NAME, err), file=sys.stderr)
        return reportcodes.EXIT_ERROR
    except OSError as err:
        print("%s: %s: %s (operating system error)" % (prog, FILE_NAME, type(err).__name__), file=sys.stderr)
        return reportcodes.EXIT_ERROR
    if word == "unchanged":
        print("%s: %s unchanged, %d rows" % (prog, out, len(rows)))
        return reportcodes.EXIT_OK
    if word == "unput":
        print("%s: %s unchanged, %d rows; its last put did not succeed, put again" % (prog, out, len(rows)))
    else:
        print("%s: %s written, %d rows, %d text%s withheld; sidecar %s" % (
            prog, out, len(rows), withheld, "" if withheld == 1 else "s", META_NAME))
    if not want_put:
        return reportcodes.EXIT_OK
    try:
        keys = put(out, side)
    except bucket.BucketError as err:
        reportcodes.write_state({"state": "failed", "reason": str(err), "sha256": sha}, state_path())
        if str(err) == bucket.LOCKED_LINE:
            print("%s: put of %s skipped, %s" % (prog, FILE_NAME, err))
            return reportcodes.EXIT_OK
        print("%s: put of %s failed, %s" % (prog, FILE_NAME, err), file=sys.stderr)
        return reportcodes.EXIT_FAILED
    except obs.OBSError as err:
        reportcodes.write_state({"state": "failed", "reason": "the object storage refused the put (HTTP %s)"
                                 % err.status, "sha256": sha}, state_path())
        print("%s: put of %s failed, the object storage refused it (HTTP %s)" % (prog, FILE_NAME, err.status),
              file=sys.stderr)
        return reportcodes.EXIT_FAILED
    except OSError as err:
        reportcodes.write_state({"state": "failed", "reason": "%s (operating system error)" % type(err).__name__,
                                 "sha256": sha}, state_path())
        print("%s: put of %s failed, %s (operating system error)" % (prog, FILE_NAME, type(err).__name__),
              file=sys.stderr)
        return reportcodes.EXIT_FAILED
    reportcodes.write_state({"state": "put", "sha256": sha}, state_path())
    for key in keys:
        print("%s: put %s" % (prog, key))
    return reportcodes.EXIT_OK
