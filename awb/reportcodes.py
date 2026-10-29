"""The codes file of the owner's report workbook (presentations/reports-design, task 1).

    awb report --codes [--out FILE] [--put] [--if-changed]

One row per project of the project register, six columns, codes only:

    project code, customer code, task description, next step, whose move, date of last change

- the customer code is `none` for a project without a customer;
- the task description is the goal line of SCOPE.md, the next step the `Next:` line of STATE.md;
- whose move is `closed` for a closed project, `owner` while OPEN.md holds an item for the owner, `customer` for a
  parked project and `session` for the rest;
- the date of last change is the newer of the last commit of the project and the time of STATE.md (the created
  date of the register when the folder gives neither).

A closed project stays in the file while its last change is less than 30 days old; a deleted one is never in it.
The two texts go through the check of a text that leaves the host (`projectfacts.export_hits`: the name check, the
secret and pattern detectors of the commit gate and the candidate rule); a text with a hit is written as
"(withheld by the name check)". When the check cannot run nothing is written.

The file is CSV in the form Excel's Power Query reads without a question: UTF-8 with a byte order mark, CRLF, a
comma, every field quoted, one header row with exactly the six names of `HEADERS` (a test pins them: the workbook's
query breaks on purpose when one changes), ISO dates, rows sorted by project code. Beside it
`<stem>.meta.json` carries the time written (UTC), the row count, the count of texts withheld, the sha256 of the
file and the release.

The file goes to `--out FILE`, by default to `projects.csv` of the state folder `/var/lib/awb-report-codes` (the
owner's, made by the timer unit; AWB_REPORT_CODES names another one). `--if-changed` writes nothing when the file
holds the same rows already and its last put succeeded. `--put` puts the file and the sidecar under `reports/` of
the owner bucket through the key service (owner calls only, `awb/bucket_layout.py` REPORT_KEYS); the result goes to
`last.json` of the state folder, which `awb owner status` reads: a failed put is an item there until a put
succeeds. A put while the key service is locked is skipped with one line (exit 0); any other failure is exit 1.
"""
from __future__ import annotations

import datetime as _dt
import hashlib
import json
import os
import sys
from dataclasses import dataclass
from pathlib import Path

from awb import bucket_layout, config, ledger, ownerwait, projects, status
from awb.projectfacts import export_hits, git_dates, mtime_date

HEADERS = ("project code", "customer code", "task description", "next step", "whose move", "date of last change")
"""The six column names, in this order and this spelling: the workbook's query reads them."""
MOVES = ("owner", "session", "customer", "closed")
CLOSED_DAYS = 30
"""Days after the last change in which a closed project stays in the file."""
FILE_NAME = "projects.csv"
STATE_DIR = ownerwait.REPORT_DIR                 # StateDirectory= of awb-report-codes.service, the owner's (700)
BOM = "﻿"
EXIT_OK, EXIT_FAILED, EXIT_ERROR = 0, 1, 2


class CodesError(Exception):
    """The file cannot be made. The message carries codes, counts and reasons, never a text."""


@dataclass
class Row:
    code: str
    customer: str
    goal: str
    next: str
    move: str
    changed: str

    def cells(self) -> tuple[str, ...]:
        return (self.code, self.customer, self.goal, self.next, self.move, self.changed)


# --------------------------------------------------------------------------- the rows


def _read(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return ""


def last_change(folder: Path | None, created: str) -> _dt.date | None:
    """The newer of the last commit of the project and the time of STATE.md; the created date without either (and
    without a folder that can be read)."""
    found = git_dates(folder) if folder is not None else []
    written = mtime_date(folder / status.STATE) if folder is not None else None
    if written is not None:
        found.append(written)
    if found:
        return max(found)
    try:
        return _dt.date.fromisoformat(created)
    except ValueError:
        return None


def whose_move(state: str, open_text: str) -> str:
    """The fixed word of MOVES for one project."""
    if state == "closed":
        return "closed"
    if projects._owner_items(open_text):
        return "owner"
    if state == "parked":
        return "customer"
    return "session"


def gather(p: config.Paths, today: _dt.date | None = None) -> list[Row]:
    """One row per project to list, sorted by project code, before the check of the texts."""
    today = today or ledger.today()
    out: list[Row] = []
    for r in projects.load(p):
        if r.state == "deleted":
            continue
        folder = Path(r.path)
        usable = folder.is_dir() and not folder.is_symlink()
        changed = last_change(folder if usable else None, r.created)
        if r.state == "closed" and (changed is None or (today - changed).days >= CLOSED_DAYS):
            continue
        goal = projects._plain(projects.goal_of(folder)) if usable else ""
        nxt = status.lines(_read(folder / status.STATE)).next if usable else None
        out.append(Row(r.code, r.customer or projects.NO_CUSTOMER, goal, projects._plain(nxt or ""),
                       whose_move(r.state, _read(folder / "OPEN.md") if usable else ""),
                       changed.isoformat() if changed else ""))
    return sorted(out, key=lambda row: row.code)


def check(rows: list[Row], reg: Path) -> int:
    """The task description and the next step of every row through `export_hits`; a text with a hit becomes
    WITHHELD. Returns the count withheld. Raises CodesError when the check cannot run."""
    texts = [t for row in rows for t in (row.goal, row.next)]
    try:
        hits = export_hits(texts, reg)
    except ledger.LedgerError as err:
        raise CodesError("the texts cannot be checked (%s); nothing is written" % err) from None
    for i in hits:
        row = rows[i // 2]
        if i % 2:
            row.next = ledger.WITHHELD
        else:
            row.goal = ledger.WITHHELD
    return len(hits)


# --------------------------------------------------------------------------- the file


def _quote(value: str) -> str:
    return '"%s"' % " ".join(value.split()).replace('"', '""')


def render(rows: list[Row]) -> bytes:
    """The CSV: a byte order mark, the header, the rows, CRLF after every line, every field quoted."""
    lines = [",".join(_quote(c) for c in HEADERS)] + [",".join(_quote(c) for c in row.cells()) for row in rows]
    return (BOM + "".join(line + "\r\n" for line in lines)).encode("utf-8")


def release() -> str:
    """The name of the release the running code came from (a folder of /opt/tcp-awb/releases on a sealed host)."""
    import awb

    return os.path.basename(os.path.dirname(os.path.realpath(awb.__path__[0])))


def meta(data: bytes, rows: int, withheld: int, now: _dt.datetime | None = None) -> dict:
    now = now or _dt.datetime.now(_dt.timezone.utc)
    return {"file": FILE_NAME, "written": now.strftime("%Y-%m-%dT%H:%M:%SZ"), "rows": rows, "withheld": withheld,
            "sha256": hashlib.sha256(data).hexdigest(), "release": release()}


def meta_path(out: Path) -> Path:
    """The sidecar of `out`: projects.csv -> projects.meta.json."""
    return out.with_name(out.stem + ".meta.json")


def state_dir() -> Path:
    """/var/lib/awb-report-codes or the folder AWB_REPORT_CODES names."""
    return ownerwait.report_dir()


def default_out() -> Path:
    folder = state_dir()
    if not folder.is_dir():
        raise CodesError("the state folder %s is missing (the timer unit makes it); give --out FILE" % folder)
    return folder / FILE_NAME


def _write(path: Path, data: bytes) -> None:
    """`data` into `path` in one step: a new file beside it, then a rename over it (a link at `path` is replaced,
    never followed)."""
    tmp = path.with_name(".%s.part" % path.name)
    try:
        tmp.unlink()
    except FileNotFoundError:
        pass
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o640)
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
        os.replace(tmp, path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


def _same(path: Path, data: bytes) -> bool:
    try:
        return path.read_bytes() == data
    except OSError:
        return False


# --------------------------------------------------------------------------- the state of the last put


def state_path() -> Path:
    return ownerwait.report_state_path()


def read_state(path: Path | None = None) -> dict:
    try:
        state = json.loads((path or state_path()).read_text(encoding="utf-8")[:4096])
    except (OSError, UnicodeDecodeError, ValueError):
        return {}
    return state if isinstance(state, dict) else {}


def write_state(state: dict, path: Path | None = None, now: _dt.datetime | None = None) -> Path | None:
    """The result of one put into the state file, in one step; None when its folder is missing or not writable (a
    run by hand outside the unit). A success keeps its time as `last_put`, a failure keeps the `last_put` before."""
    path = path or state_path()
    at = (now or _dt.datetime.now(_dt.timezone.utc)).strftime("%Y-%m-%dT%H:%M:%SZ")
    old = read_state(path)
    state = dict(state, at=at)
    if state.get("state") == "put":
        state["last_put"] = at
    elif old.get("last_put"):
        state["last_put"] = old["last_put"]
    try:
        _write(path, (json.dumps(state, sort_keys=True) + "\n").encode("utf-8"))
    except OSError:
        return None
    return path


def put(out: Path, side: Path, client=None) -> list[str]:
    """The file and its sidecar under reports/ of the owner bucket, through the key service."""
    from awb import bucket

    c = client or bucket.client()
    keys = []
    for src, key, ctype in ((out, bucket_layout.REPORTS_PREFIX + FILE_NAME, "text/csv"),
                            (side, bucket_layout.REPORTS_PREFIX + meta_path(Path(FILE_NAME)).name,
                             "application/json")):
        c.put_file(key, src, ctype)
        keys.append(key)
    return keys


# --------------------------------------------------------------------------- the command


def make(p: config.Paths, out: Path, *, if_changed: bool = False, want_put: bool = False,
         today: _dt.date | None = None) -> tuple[str, list[Row], int, bytes]:
    """Gather, check and write unless nothing changed. Returns ("written" or "unchanged", rows, withheld, data)."""
    rows = gather(p, today)
    withheld = check(rows, ledger.register_path(p))
    data = render(rows)
    if if_changed and _same(out, data) and meta_path(out).is_file():
        last = read_state()
        if not want_put or (last.get("state") == "put" and last.get("sha256") == hashlib.sha256(data).hexdigest()):
            return "unchanged", rows, withheld, data
        return "unput", rows, withheld, data
    _write(out, data)
    _write(meta_path(out), (json.dumps(meta(data, len(rows), withheld), indent=1) + "\n").encode("utf-8"))
    return "written", rows, withheld, data


def main(argv: list[str]) -> int:
    """`awb report --codes ...`. Exit 0 written or unchanged (or a put skipped while the key service is locked),
    1 the put failed, 2 refused or an error."""
    from awb import bucket, obs
    from awb.cli import SafeParser

    prog = "awb report --codes"
    ap = SafeParser(prog="awb report", description="The codes file of the report workbook: one row per project, "
                    "six columns, codes only (projects.csv and projects.meta.json).")
    ap.add_argument("--codes", action="store_true", required=True)
    ap.add_argument("--out", type=Path, default=None, metavar="FILE",
                    help="where projects.csv goes, the sidecar beside it; default the state folder of the timer, "
                         "%s" % (STATE_DIR / FILE_NAME))
    ap.add_argument("--put", action="store_true",
                    help="put the file and the sidecar under reports/ of the owner bucket (the owner only)")
    ap.add_argument("--if-changed", dest="if_changed", action="store_true",
                    help="write and put nothing when the rows are the same as in the file and its last put succeeded")
    try:
        args = ap.parse_args(argv)
    except SystemExit as exc:
        return ledger._exit_code(exc)
    try:
        p = config.paths()
        out = args.out if args.out is not None else default_out()
        if out.is_dir():
            raise CodesError("--out names a folder; give the path of the file")
        word, rows, withheld, data = make(p, out, if_changed=args.if_changed, want_put=args.put)
    except (CodesError, ledger.LedgerError, projects.ProjectError) as err:
        print("%s: %s" % (prog, err), file=sys.stderr)
        return EXIT_ERROR
    except OSError as err:
        print("%s: %s (operating system error)" % (prog, type(err).__name__), file=sys.stderr)
        return EXIT_ERROR
    if word == "unchanged":
        print("%s: %s unchanged, %d rows" % (prog, out, len(rows)))
        return EXIT_OK
    if word == "unput":
        print("%s: %s unchanged, %d rows; its last put did not succeed, put again" % (prog, out, len(rows)))
    if word == "written":
        print("%s: %s written, %d rows, %d text%s withheld; sidecar %s" % (
            prog, out, len(rows), withheld, "" if withheld == 1 else "s", meta_path(out).name))
    if not args.put:
        return EXIT_OK
    sha = hashlib.sha256(data).hexdigest()
    try:
        keys = put(out, meta_path(out))
    except bucket.BucketError as err:
        write_state({"state": "failed", "reason": str(err), "sha256": sha})
        if str(err) == bucket.LOCKED_LINE:
            print("%s: put skipped, %s" % (prog, err))
            return EXIT_OK
        print("%s: put failed, %s" % (prog, err), file=sys.stderr)
        return EXIT_FAILED
    except obs.OBSError as err:
        write_state({"state": "failed", "reason": "the object storage refused the put (HTTP %s)" % err.status,
                     "sha256": sha})
        print("%s: put failed, the object storage refused it (HTTP %s)" % (prog, err.status), file=sys.stderr)
        return EXIT_FAILED
    except OSError as err:
        write_state({"state": "failed", "reason": "%s (operating system error)" % type(err).__name__, "sha256": sha})
        print("%s: put failed, %s (operating system error)" % (prog, type(err).__name__), file=sys.stderr)
        return EXIT_FAILED
    write_state({"state": "put", "sha256": sha})
    for key in keys:
        print("%s: put %s" % (prog, key))
    return EXIT_OK
