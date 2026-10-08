"""The owner level's status file (T9 step 2) and the intake counts behind it.

    awb owner status            the status as the owner-actions service would show it
    awb owner status --write    write it to /var/lib/awb-owner-status/status.json (the timer of the owner, every minute)

The owner-actions service runs as its own system user and reads this one file and nothing else, so everything the
owner page shows comes from here: the state of the vault and of the key service from their pings, the deployed
release, the page facts, the runs of the UI queue and the intake counts per customer code. Every value is a code, a
number, a fixed word or a time, checked field by field before the file is written, and the whole text passes the
name check when the check can run (a locked vault cannot check; the fixed shape still holds then).

The intake and `awb register review` keep `<vault>/intake-counts.json`: per customer code the candidates that wait
for a review and the time of the last report. No report is opened to count.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
import sys
import tempfile
from pathlib import Path

from awb import config
from awb.intake_counts import now_text, read_counts
from awb.status_shape import DAEMON_STATES, RUN_RE, RUN_STATES, TIME_RE, StatusError, validate

STATUS = Path("/var/lib/awb-owner-status/status.json")
MAX_RUNS = 20


# --------------------------------------------------------------------------- the parts of the status


def daemons() -> dict:
    from awb import deploy

    states = deploy.owner_daemon_states()
    out = {}
    for name in ("vault", "keys"):
        d = states.get(name) or {}
        state = d.get("state") if d.get("state") in DAEMON_STATES else "unknown"
        out[name] = {"state": state, "since": d.get("since") if isinstance(d.get("since"), str) else None}
    return out


def deployed(root: Path | None = None) -> dict:
    from awb import deploy

    root = root or deploy.OPT
    journal = deploy.read_journal(root / deploy.JOURNAL_NAME) or {}
    return {"release": deploy.live_release(root), "journal": journal.get("state") if isinstance(
        journal.get("state"), str) and re.match(r"^[a-z]{1,20}$", journal["state"]) else None}


def page(root: Path | None = None) -> dict:
    from awb.tcp.web import publish

    root = root or publish.SRV
    index = root / publish.INDEX
    out = {"size": None, "time": None, "backup_time": None, "backups": 0}
    try:
        st = index.stat()
        out.update(size=st.st_size, time=now_text(st.st_mtime))
    except OSError:
        pass
    try:
        found = publish.backups(root)
    except OSError:
        found = []
    out["backups"] = len(found)
    if found:
        out["backup_time"] = now_text(found[-1].stat().st_mtime)
    return out


def ui_runs(queue: Path | None = None) -> list[dict]:
    """The newest runs of the UI queue: id, state, finish time, PASS or FAIL, the size of the diff and whether it is
    the one run Publish would take (the newest PASS run that is completed or ready)."""
    from awb.tcp.web import publish

    queue = queue or publish.queue_dir()
    db = queue / "queue.sqlite3"
    if not db.is_file():
        return []
    con = sqlite3.connect("file:%s?mode=ro" % db, uri=True)
    try:
        rows = con.execute("SELECT id, state, finished, apply_changes FROM jobs ORDER BY created DESC LIMIT ?",
                           (MAX_RUNS,)).fetchall()
    except sqlite3.Error:
        return []
    finally:
        con.close()
    out = []
    for run_id, state, finished, applies in rows:
        if not isinstance(run_id, str) or not RUN_RE.match(run_id):
            continue
        folder = queue / "runs" / run_id
        try:
            first = (folder / "validation.txt").read_text(encoding="utf-8", errors="replace")[:4]
        except OSError:
            first = ""
        try:
            diff = (folder / "change.diff").stat().st_size
        except OSError:
            diff = None
        out.append({"id": run_id, "state": state if state in RUN_STATES else "unknown",
                    "finished": finished if isinstance(finished, str) and TIME_RE.match(finished) else None,
                    "validation": "PASS" if first == "PASS" else "FAIL" if first else None,
                    "diff_bytes": diff, "candidate_only": not applies, "publishable": False})
    for run in out:     # the newest first: the first PASS run whose state lets it be published
        if run["validation"] == "PASS" and run["state"] == ("ready" if run["candidate_only"] else "completed"):
            run["publishable"] = True
            break
    return out


def intake(p: config.Paths) -> list[dict]:
    return [{"customer": code, "waiting": v.get("waiting") if isinstance(v.get("waiting"), int) else None,
             "time": v.get("time") if isinstance(v.get("time"), str) and TIME_RE.match(v["time"]) else None}
            for code, v in sorted(read_counts(p).items())]


def collect(p: config.Paths | None = None) -> dict:
    p = p or config.paths()
    return {"version": 1, "written": now_text(), **daemons(), "deployed": deployed(), "page": page(),
            "ui_runs": ui_runs(), "intake": intake(p)}


# --------------------------------------------------------------------------- the checks before the write


def name_check(text: str) -> str:
    """'passed', 'unavailable' (the vault cannot check now) or StatusError when the text carries a name, a secret,
    a token or a private key."""
    from awb import check, gate
    from awb.tcp.web import publish

    try:
        matcher = publish.name_matcher()
        found = {f.cls for f in gate._scan_text("status.json", text, matcher) if f.cls in publish.CLASSES}
    except (publish.Refused, check.CheckUnavailable):
        return "unavailable"
    if found:
        raise StatusError("the status is withheld: the name check found %s" % ", ".join(sorted(found)))
    return "passed"


def write(status: dict, path: Path = STATUS, checker=name_check) -> str:
    """Check and write the status atomically (mode 640); returns the result of the name check."""
    validate(status)
    text = json.dumps(status, sort_keys=True, indent=1) + "\n"
    result = checker(text)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".status.", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
        os.chmod(tmp, 0o640)
        os.replace(tmp, path)
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise
    return result


def main(argv: list[str] | None = None) -> int:
    from awb.cli import SafeParser

    ap = SafeParser(prog="awb owner", description="The owner level's status file.")
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("status", help="the status the owner page shows; --write writes it for owner-actions")
    s.add_argument("--write", action="store_true")
    s.add_argument("--out", help=argparse.SUPPRESS)
    try:
        a = ap.parse_args(sys.argv[1:] if argv is None else argv)
    except SystemExit as exc:
        return 0 if exc.code in (0, None) else 2
    if config.is_work_user():
        print("awb owner: the work user has no owner status", file=sys.stderr)
        return 2
    status = collect()
    try:
        if a.write:
            result = write(status, Path(a.out) if a.out else STATUS)
            print("written: %d runs, %d customers, name check %s" % (len(status["ui_runs"]), len(status["intake"]),
                                                                       result))
        else:
            validate(status)
            print(json.dumps(status, sort_keys=True, indent=1))
    except StatusError as err:
        print("awb owner: %s" % err, file=sys.stderr)
        return 1
    except OSError as err:
        print("awb owner: %s (operating system error)" % type(err).__name__, file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
