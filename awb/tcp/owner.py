"""The owner level's status file (T9 step 2) and the intake counts behind it.

    awb owner status            first what runs now (item, since, ETA) and what ended in the last 24 hours, red ones
                                marked (the events file, awb/events.py, Q-0014); then one list of everything that
                                waits on the owner, each item with where it is done and a ready command block
                                (OPEN.md of the live projects, QUEUE.md, the build list)
    awb owner status --mail     the same list as one mail (the owner's timer, 08:00 Europe/Berlin)
    awb owner status --json     the status as the owner-actions service would show it
    awb owner status --write    write it to /var/lib/awb-owner-status/status.json (the timer of the owner, every minute)
                                and first writes the lock of the vault and the key service, the lapsed login of the
                                build user and the runs of the timer jobs to the events file and mails one push per
                                new red end, needs-you end, lock, lapsed login and answered blocking dev ask
                                (push_tick, Q-0014 phase 2)

The owner-actions service runs as its own system user and reads this one file and nothing else, so everything the
owner page shows comes from here: the state of the vault and of the key service from their pings, the deployed
release, the page facts, the runs of the UI queue, the intake counts per customer code and the project decisions.
Decision text and app lines pass the owner-status name check before export. Values are checked field by field
before the file is written, and the whole text passes the
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

from awb import config, events, ownerwait
from awb.intake_counts import now_text, read_counts
from awb.status_shape import DAEMON_STATES, RUN_RE, RUN_STATES, SUMMARY_MAX, TIME_RE, StatusError
from awb.ownerwait import validate_status as validate

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


def ui_runs(queue: Path | None = None, screen=None) -> list[dict]:
    """The newest runs of the UI queue: id, state, creation and finish time, PASS or FAIL, the size of the diff and
    whether it is the one run Publish would take (the newest PASS run that is completed or ready). With `screen`
    (see summary_screen) each run also carries the summary of its result.json as the screen lets it pass, else
    summary and withheld are None. The queue is opened read only."""
    from awb.tcp.web import publish

    queue = queue or publish.queue_dir()
    db = queue / "queue.sqlite3"
    try:
        # SQLite opens its own descriptor. Keep a checked descriptor and verify its identity after opening.
        with ownerwait._regular_file(db) as stream:
            before = os.fstat(stream.fileno())
            con = sqlite3.connect(db.absolute().as_uri() + "?mode=ro", uri=True)
            try:
                after = db.stat(follow_symlinks=False)
                if (before.st_dev, before.st_ino) != (after.st_dev, after.st_ino):
                    con.close()
                    return []
            except OSError:
                con.close()
                return []
    except (OSError, sqlite3.Error):
        return []
    try:
        rows = con.execute("SELECT id, state, created, finished, apply_changes FROM jobs ORDER BY created DESC, id "
                           "DESC LIMIT ?", (MAX_RUNS,)).fetchall()
    except sqlite3.Error:
        return []
    finally:
        con.close()
    out = []
    for run_id, state, created, finished, applies in rows:
        if not isinstance(run_id, str) or not RUN_RE.fullmatch(run_id) or run_id in (".", ".."):
            continue
        folder = queue / "runs" / run_id
        try:
            with ownerwait._regular_file(folder / "validation.txt") as stream:
                first = stream.read(4).decode("utf-8", errors="replace")
        except OSError:
            first = ""
        try:
            with ownerwait._regular_file(folder / "change.diff") as stream:
                diff = os.fstat(stream.fileno()).st_size
        except OSError:
            diff = None
        summary, withheld = screen(_summary_text(folder)) if screen else (None, None)
        summary = _format_summary(summary) if summary is not None else None
        out.append({"id": run_id, "state": state if state in RUN_STATES else "unknown",
                    "created": created if isinstance(created, str) and TIME_RE.match(created) else None,
                    "finished": finished if isinstance(finished, str) and TIME_RE.match(finished) else None,
                    "validation": "PASS" if first == "PASS" else "FAIL" if first else None,
                    "diff_bytes": diff, "candidate_only": not applies, "publishable": False,
                    "summary": summary, "withheld": withheld})
    for run in out:     # the newest first: the first PASS run whose state lets it be published
        if run["validation"] == "PASS" and run["state"] == ("ready" if run["candidate_only"] else "completed"):
            run["publishable"] = True
            break
    return out


FILE_RE = re.compile(r"(?:[\w.~-]*/)+[\w.-]*|\b[\w-]+(?:\.[\w-]+)*\.[A-Za-z][A-Za-z0-9]{0,7}\b")


def _summary_text(folder: Path) -> str | None:
    """Read the complete bounded summary before any display transformation or screening."""
    try:
        data = json.loads(ownerwait._read_text(folder / "result.json", 65536))
    except (OSError, ValueError, RecursionError):
        return None
    text = data.get("summary") if isinstance(data, dict) else None
    return text if isinstance(text, str) and text else None


def _format_summary(text: str) -> str | None:
    text = FILE_RE.sub("[file]", " ".join("".join(c if c.isprintable() else " " for c in text).split()))
    if len(text) > SUMMARY_MAX:
        text = text[:SUMMARY_MAX - 3].rstrip() + "..."
    return text or None


def run_summary(folder: Path) -> str | None:
    """The bounded one-line display summary, with file names masked."""
    text = _summary_text(folder)
    return _format_summary(text) if text is not None else None


def summary_screen():
    """A screen for run summaries: text -> (summary, None) when the name check passes it, (None, classes) when it
    finds a name, a secret, a token or a private key, (None, "unavailable") when the check cannot run."""
    from awb import check, register
    from awb.tcp.web import publish

    state = {}

    def screen(text: str | None) -> tuple[str | None, str | None]:
        if text is None:
            return None, None
        if "matcher" not in state:
            try:
                state["matcher"] = publish.name_matcher()
            except (publish.Refused, check.CheckUnavailable, register.RegisterError, OSError):
                state["matcher"] = None
        if state["matcher"] is None:
            return None, "unavailable"
        try:
            found = set(check.screen(text, state["matcher"]).found) & set(publish.CLASSES)
        except check.CheckUnavailable:
            return None, "unavailable"
        if found:
            return None, ",".join(c for c in publish.CLASSES if c in found)
        return text, None

    return screen


def intake(p: config.Paths) -> list[dict]:
    return [{"customer": code, "waiting": v.get("waiting") if isinstance(v.get("waiting"), int) else None,
             "time": v.get("time") if isinstance(v.get("time"), str) and TIME_RE.match(v["time"]) else None}
            for code, v in sorted(read_counts(p).items())]


DECISIONS_EVERY = 15 * 60
"""Seconds between two rebuilds of the decisions: the list runs the name check on every owner item of every project,
and a rebuild each minute (the timer) used up the owner's name check budget (2026-11-14, a write took 92 s)."""


def _decisions(p: config.Paths, path: Path | None = None) -> tuple[list, int]:
    """The decisions of the status file PATH while they are younger than DECISIONS_EVERY, else a new list. Without
    PATH (every caller but `--write`) the list is always new."""
    import time

    if path is None:
        return ownerwait.page_items(p), int(time.time())
    now = time.time()
    try:
        with ownerwait._regular_file(path) as stream:
            if not 0 <= now - os.fstat(stream.fileno()).st_mtime < DECISIONS_EVERY:
                raise ValueError("stale status")
            raw = stream.read(1024 * 1024 + 1)
        if len(raw) > 1024 * 1024:
            raise ValueError("status too large")
        old = json.loads(raw)
        ownerwait._validate_decisions(old)
        built = old.get("decisions_built")
        if type(built) is int and 0 <= now - built < DECISIONS_EVERY and "decisions" in old:
            return old["decisions"], built
    except (OSError, ValueError, AttributeError, TypeError, RecursionError, StatusError):
        pass
    return ownerwait.page_items(p), int(time.time())


def _lists(p: config.Paths, path: Path | None = None) -> tuple[list, dict, int]:
    """The dev asks and the queue rows (Q-0025) of the status file PATH while they are younger than
    nowhealth.LISTS_EVERY, else new ones, their texts through the screen of the UI run summaries. Without PATH the
    lists are always new."""
    import time

    from awb.status_shape import validate_lists
    from awb.tcp.web import nowhealth

    now = time.time()
    if path is not None:
        try:
            with ownerwait._regular_file(path) as stream:
                if not 0 <= now - os.fstat(stream.fileno()).st_mtime < nowhealth.LISTS_EVERY:
                    raise ValueError("stale status")
                raw = stream.read(1024 * 1024 + 1)
            if len(raw) > 1024 * 1024:
                raise ValueError("status too large")
            old = json.loads(raw)
            validate_lists(old)
            built = old.get("lists_built")
            if type(built) is int and 0 <= now - built < nowhealth.LISTS_EVERY and "dev_asks" in old \
                    and "queue" in old:
                return old["dev_asks"], old["queue"], built
        except (OSError, ValueError, AttributeError, TypeError, RecursionError, StatusError):
            pass
    screen = summary_screen()
    return nowhealth.ask_rows(p, screen), nowhealth.queue_rows(screen), int(now)


REUSE: Path | None = None
"""The file `--write` writes, set by main for the run: its decisions are reused while fresh. None for every other
caller, which always builds a new list (and never reads the host's status file)."""


def collect(p: config.Paths | None = None) -> dict:
    p = p or config.paths()
    decisions, built = _decisions(p, REUSE)
    asks, queue, lists_built = _lists(p, REUSE)
    return {"version": 1, "written": now_text(), **daemons(), "deployed": deployed(), "page": page(),
            "ui_runs": ui_runs(screen=summary_screen()), "intake": intake(p), "decisions": decisions,
            "decisions_built": built, "dev_asks": asks, "queue": queue, "lists_built": lists_built,
            "running": events.running_rows(p), "ended": events.ended_rows(p)}


# --------------------------------------------------------------------------- the push of the minute timer (Q-0014)

PUSH_STATE = "events-push.json"
"""Beside the status file: the keys of the events already pushed and where each timer job stands (mode 600)."""


def login_lapsed(runs: Path | None = None) -> bool | None:
    """True when the newest run of a build task is held for a missing login of the build user (the runner's
    `held: login missing`, awb/buildrun.py LOGIN_HELD), False when none is, None when the runs cannot be read. The run
    folders are read directly, as ownerwait.build_items does."""
    import os

    runs = Path(runs) if runs else Path(os.environ.get("AWB_BUILD_RUNS") or config.build_root() / "runs")
    try:
        ids = [n for n in os.listdir(runs) if re.fullmatch(r"B-[0-9]{3,4}", n)]
    except OSError:
        return None
    for tid in ids:
        try:
            nums = sorted(int(n) for n in os.listdir(runs / tid) if re.fullmatch(r"[1-9][0-9]*", n))
            raw = ownerwait._read_text(runs / tid / str(nums[-1]) / "state", 512)
        except (OSError, ValueError, IndexError):
            continue
        if raw.split("\t", 1)[0].strip() == "held: login missing":
            return True
    return False


def ask_blocks(p: config.Paths):
    """`blocking(handle)` of events.push: True when the dev ask HANDLE of <shared>/notices/dev blocked its
    project."""
    from awb import devask

    def blocking(handle: str) -> bool:
        try:
            return devask.read(devask.folder(p) / (handle + ".md"))["blocks"] is True
        except Exception:
            return False
    return blocking


def push_check(text: str) -> str:
    """The name check of a push: "passed", "unavailable" or "found"."""
    try:
        return name_check(text)
    except StatusError:
        return "found"


def push_tick(status: dict, p: config.Paths | None = None, state_path: Path | None = None, send=None,
              units=None, now=None) -> dict:
    """What the owner's minute timer adds to `awb owner status --write`: the lock of the vault and the key
    service from STATUS, the login of the build user, the runs of the timer jobs, then one mail per new event that
    needs him (events.push). STATUS gets `running` and `ended` again after the writes. Returns the counts of the
    push; never raises."""
    try:
        p = p or config.paths()
        state_path = state_path or STATUS.parent / PUSH_STATE
        if send is None:
            from awb.tcp import keys

            send = lambda subject, body: keys.send_mail(subject, body) == "sent"   # noqa: E731
        board = events.folder(p)
        state = events.read_state(state_path)
        events.observe_locks({"vault": status.get("vault"), "keys": status.get("keys")}, board=board, now=now)
        events.observe_login(login_lapsed(), board=board, now=now)
        jobs = events.observe_jobs(events.job_units() if units is None else units,
                                   None if state is None else state.get("jobs"), board=board, now=now)
        sent, counts = events.push(state, send, check=push_check, blocking=ask_blocks(p), board=board, now=now)
        events.write_state(state_path, {"sent": sent["sent"], "jobs": jobs})
        status["running"] = events.running_rows(p, now)
        status["ended"] = events.ended_rows(p, now)
        return counts
    except Exception:
        return {"new": 0, "sent": 0, "held": 0, "withheld": 0}


# --------------------------------------------------------------------------- the checks before the write


def name_check(text: str) -> str:
    """'passed', 'unavailable' (the vault cannot check now) or StatusError when the text carries a name, a secret,
    a token or a private key (check.screen, the classes of publish.CLASSES refuse)."""
    from awb import check, register
    from awb.tcp.web import publish

    try:
        matcher = publish.name_matcher()
        found = set(check.screen(text, matcher).found) & set(publish.CLASSES)
    except (publish.Refused, check.CheckUnavailable, register.RegisterError, OSError):
        return "unavailable"
    if found:
        raise StatusError("the status is withheld: the name check found %s" % ", ".join(sorted(found)))
    return "passed"


def _status_text(status: dict, checker) -> tuple[str, str]:
    validate(status)
    text = json.dumps(status, sort_keys=True, indent=1) + "\n"
    result = checker(text)
    if result == "unavailable":
        status = dict(status)
        if "decisions" in status:
            status["decisions"] = []
            status["decisions_built"] = 0
        status["ui_runs"] = [dict(run, summary=None, withheld="unavailable") if run.get("summary") else run
                             for run in status["ui_runs"]]
        from awb.tcp.web import nowhealth

        status = nowhealth.withhold_lists(status)
        text = json.dumps(status, sort_keys=True, indent=1) + "\n"
    return text, result


def write(status: dict, path: Path = STATUS, checker=name_check) -> str:
    """Check and write the status atomically (mode 640); returns the result of the name check."""
    text, result = _status_text(status, checker)
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
    s = sub.add_parser("status", help="what runs now with its ETA and what ended in the last 24 hours, then "
                                      "everything that waits on the owner, one line each with a ready command; "
                                      "--mail sends it, --json and --write give the status of the owner page")
    s.add_argument("--write", action="store_true", help="write the status file for owner-actions")
    s.add_argument("--json", action="store_true", help="print the status of the owner page as JSON")
    s.add_argument("--mail", action="store_true", help="send the list as one mail (the owner's timer)")
    s.add_argument("--dev", action="store_true", help="print the tooling and steering items the list leaves out")
    s.add_argument("--all", nargs="?", const="", metavar="CODE",
                   help="print every item as it is, or every item of project CODE")
    s.add_argument("--history", metavar="CODE",
                   help="the closed and open waits of project CODE from the wait history: start, end, hours, where")
    s.add_argument("--record", action="store_true",
                   help="record the items in the wait history and print one line (the owner's hourly timer)")
    s.add_argument("--out", help=argparse.SUPPRESS)
    try:
        a = ap.parse_args(sys.argv[1:] if argv is None else argv)
    except SystemExit as exc:
        return 0 if exc.code in (0, None) else 2
    if config.is_work_user():
        print("awb owner: the work user has no owner status", file=sys.stderr)
        return 2
    try:
        if a.history is not None:
            from awb import ownerwaits

            print("\n".join(ownerwaits.history_lines(a.history, ownerwaits.load())))
            return 0
        if a.record:
            from awb import ownerwait

            ownerwait.collect(view="all", record=True)
            print("awb owner: waits %s" % ownerwait.LAST_RECORD)
            return 0 if ownerwait.LAST_RECORD.startswith(tuple("0123456789")) else 1
        if not a.write and not a.json:
            from awb import ownerwait
            from awb.tcp import keys

            if a.mail:
                word = ownerwait.mail(send=keys.send_mail)
                print("awb owner: mail %s" % word)
                return 0 if word in ("sent", "empty") else 1
            view = "all" if a.all is not None else "dev" if a.dev else "decisions"
            items, parked = ownerwait.collect(view=view, code=a.all or None, record=True)
            from awb import buildbudget

            print("\n".join(events.status_lines(events.folder()) + [buildbudget.host_line(), ""]
                            + ownerwait.render(items, parked, ownerwait.notes())))
            return 0
        global REUSE
        REUSE = (Path(a.out) if a.out else STATUS) if a.write else None
        status = collect()
        if a.write and not a.out:
            push_tick(status)
        if a.write:
            result = write(status, Path(a.out) if a.out else STATUS)
            print("written: %d runs, %d customers, name check %s" % (len(status["ui_runs"]), len(status["intake"]),
                                                                       result))
        else:
            text, _ = _status_text(status, name_check)
            print(text, end="")
    except StatusError as err:
        print("awb owner: %s" % err, file=sys.stderr)
        return 1
    except OSError as err:
        print("awb owner: %s (operating system error)" % type(err).__name__, file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
