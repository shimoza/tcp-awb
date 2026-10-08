"""The UI queue from the owner's side: submit a task, list the runs and get a result note when a run ends (T7).

    awb ui submit TASKFILE [--candidate-only] [--mail] [--no-watch]
    awb ui status [--ended] [--limit N]
    awb ui watch ID [--mail] [--interval S] [--timeout S]

`submit` checks the task file (the form of presentations/ui-tasks/: the file is named by its ID, the first line
is "UI task <ID>: ...", the scope is architect-workbench.html only; no registered name, secret, token, private key,
home path or blocklisted name), hands it to the bridge of the queue and starts `watch` in the background. `watch`
waits until the run leaves queued and running and writes <tasks>/<ID>.result.md: the state, validation.txt, the
summary and the limitations of result.json, the size of change.diff and the publish command (`awb web publish`,
T6). With --mail it also sends a mail through the SMN topic of keys.conf, by the key service, as `awb xchg put`
does. A text of the run that the checks find something in is withheld from the note with its class only.

`status` prints one line per run: id, state and finish time. `status --ended` prints one line for every run that
ended since the last `--ended` and nothing else: the owner session runs it at the start of every reply while a run
is open.

The queue's state (queue.sqlite3) is only read; the bridge is the only writer. The settings live in
`~/.config/awb/ui.conf` (AWB_UI_CONF gives another file):

    bridge = <path of bridge.py>
    tasks = <folder of the task files and the result notes>     (default ~/tcp-awb/presentations/ui-tasks)

AWB_UI_QUEUE gives another queue folder (as for `awb web`), AWB_UI_STATE another folder for the watch logs and the
list of reported runs (default ~/.local/state/awb-ui).
"""
from __future__ import annotations

import argparse
import fcntl
import json
import os
import re
import sqlite3
import subprocess
import sys
import time
from collections import Counter
from datetime import datetime
from pathlib import Path

from awb import check, config, gate, register
from awb.tcp.web import publish

CONF = Path(".config/awb/ui.conf")              # under the owner's home
STATE = Path(".local/state/awb-ui")             # under the owner's home
TASKS = Path("tcp-awb/presentations/ui-tasks")  # under the owner's home
ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]{2,79}$")   # the ids the bridge accepts
SCOPE = "Scope: architect-workbench.html only"
MAX_TASK = 32768
OPEN = ("queued", "running")
TASK_CLASSES = ("name", "secret", "token", "private-key", "homepath", "blocklist")
NOTE_CLASSES = ("name", "secret", "token", "private-key")
PUBLISHABLE = {True: "completed", False: "ready"}      # apply_changes -> the state a publishable run is in
INTERVAL = 20.0
TIMEOUT = 2 * 24 * 3600.0


class UiError(Exception):
    """A refusal; the text names a class, a state or a setting, never a value of the task or the run."""


# --------------------------------------------------------------------------- settings and places


def settings() -> dict[str, str]:
    path = Path(os.environ.get("AWB_UI_CONF") or Path.home() / CONF)
    out: dict[str, str] = {}
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return out
    for line in text.splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, value = line.split("=", 1)
            out[key.strip()] = value.strip()
    return out


def bridge_path() -> Path:
    value = settings().get("bridge")
    if not value:
        raise UiError("no bridge in ui.conf: add the line bridge = <path of bridge.py>")
    path = Path(value).expanduser()
    if not path.is_file():
        raise UiError("the bridge of ui.conf is missing")
    return path


def tasks_dir() -> Path:
    value = settings().get("tasks")
    return Path(value).expanduser() if value else Path.home() / TASKS


def state_dir() -> Path:
    path = Path(os.environ.get("AWB_UI_STATE") or Path.home() / STATE)
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    return path


def note_path(job_id: str) -> Path:
    return tasks_dir() / ("%s.result.md" % job_id)


# --------------------------------------------------------------------------- the queue, read only


def runs(job_id: str | None = None, limit: int | None = None) -> list[dict]:
    """The rows of the queue, newest first, read without writing to the database."""
    db = publish.queue_dir() / "queue.sqlite3"
    if not db.is_file():
        raise UiError("the queue has no database")
    con = sqlite3.connect("file:%s?mode=ro" % db, uri=True)
    con.row_factory = sqlite3.Row
    try:
        sql = "SELECT id, state, created, started, finished, detail, apply_changes FROM jobs"
        args: tuple = ()
        if job_id is not None:
            sql, args = sql + " WHERE id=?", (job_id,)
        sql += " ORDER BY created DESC, id DESC"
        if limit:
            sql += " LIMIT %d" % int(limit)
        return [dict(r) for r in con.execute(sql, args)]
    except sqlite3.Error:
        raise UiError("the queue database cannot be read") from None
    finally:
        con.close()


def _time(value: str | None) -> str:
    if not value:
        return "-"
    try:
        return datetime.fromisoformat(value).strftime("%Y-%m-%d %H:%M UTC")
    except ValueError:
        return "-"


# --------------------------------------------------------------------------- the checks


def _matcher():
    """The name check of this user, or None with the reason when it cannot run."""
    try:
        return publish.name_matcher(), None
    except publish.Refused as err:
        return None, str(err)
    except (check.CheckUnavailable, register.RegisterError):
        return None, "the name check cannot run: is the vault unlocked?"


def findings(text: str, matcher, classes: tuple[str, ...]) -> list[str]:
    """The classes found in `text`, each with its count of lines."""
    counts = Counter(f.cls for f in gate._scan_text("task", text, matcher) if f.cls in classes)
    return ["%s: %d line%s" % (c, counts[c], "" if counts[c] == 1 else "s") for c in classes if counts[c]]


def check_task(path: Path, matcher) -> tuple[str, str]:
    """(id, text) of a task file that may go to the queue; UiError names what is wrong."""
    job_id = path.stem
    if path.suffix != ".txt" or not ID_RE.match(job_id):
        raise UiError("the task file must be named <ID>.txt with an id of 3-80 lowercase letters, digits or hyphens")
    if not path.is_file():
        raise UiError("the task file is missing")
    data = path.read_bytes()
    if not data.strip() or len(data) > MAX_TASK:
        raise UiError("the task file must be nonempty and at most %d bytes" % MAX_TASK)
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        raise UiError("the task file is not UTF-8") from None
    if not text.startswith("UI task %s: " % job_id):
        raise UiError("the first line must be: UI task <ID>: <what changes>, with the id of the file name")
    if SCOPE not in text:
        raise UiError("the task must say: %s" % SCOPE)
    try:
        found = findings(text, matcher, TASK_CLASSES)
    except check.CheckUnavailable:
        raise UiError("the name check cannot run: is the vault unlocked?") from None
    if found:
        raise UiError("the task file is refused: " + "; ".join(found))
    return job_id, text


# --------------------------------------------------------------------------- the result note


def _read_text(path: Path, limit: int = 65536) -> str | None:
    try:
        with open(path, "rb") as fh:
            return fh.read(limit).decode("utf-8", errors="replace")
    except OSError:
        return None


def _result(run: Path) -> dict:
    try:
        value = json.loads(_read_text(run / "result.json") or "")
    except ValueError:
        return {}
    return value if isinstance(value, dict) else {}


def _diff_size(path: Path) -> str:
    try:
        size = path.stat().st_size
    except OSError:
        return "no change.diff"
    added = removed = lines = 0
    with open(path, "rb") as fh:
        for line in fh:
            lines += 1
            if line.startswith(b"+") and not line.startswith(b"+++"):
                added += 1
            elif line.startswith(b"-") and not line.startswith(b"---"):
                removed += 1
    return "change.diff: %d bytes, %d lines (+%d -%d)" % (size, lines, added, removed)


def _publish_line(row: dict, validation: str | None) -> str:
    applies = bool(row["apply_changes"])
    if row["state"] != PUBLISHABLE[applies]:
        return "Not publishable: the run is in state %s." % row["state"]
    if not (validation or "").startswith("PASS"):
        return "Not publishable: the validation is not PASS."
    return "sudo awb web publish --from-queue %s   (as the owner; --dry-run checks only)" % row["id"]


def _validation_word(validation: str | None) -> str:
    if validation is None:
        return "none"
    return "PASS" if validation.startswith("PASS") else "not PASS"


def note_text(row: dict, matcher, why_unchecked: str | None = None) -> str:
    """The result note of an ended run. Every text the run produced is checked; one with a finding is withheld."""
    run = publish.queue_dir() / "runs" / row["id"]
    result = _result(run)

    def checked(text: str) -> str:
        try:
            found = findings(text, matcher, NOTE_CLASSES)
        except check.CheckUnavailable:
            found = []
        return "(withheld: %s; read it in the run folder)" % "; ".join(found) if found else text

    validation = _read_text(run / "validation.txt")
    summary = result.get("summary") if isinstance(result.get("summary"), str) else None
    limits = [x for x in result.get("limitations") or [] if isinstance(x, str)]
    out = ["# UI run %s: %s" % (row["id"], row["state"]), ""]
    out.append("- Submitted %s, started %s, finished %s." % (_time(row["created"]), _time(row["started"]),
                                                               _time(row["finished"])))
    out.append("- %s." % ("Applies to the local source" if row["apply_changes"] else "Candidate only"))
    out.append("- Run folder: runs/%s in the queue folder." % row["id"])
    if row.get("detail"):
        out.append("- Detail: %s" % checked(row["detail"].strip()))
    if why_unchecked:
        out.append("- The name check did not run (%s); secrets and keys were checked." % why_unchecked)
    out += ["", "## Validation", "", checked(validation.strip()) if validation else "None: the run wrote no "
            "validation.txt.", "", "## Summary", "", checked(summary) if summary else "None in result.json.", "",
            "## Limitations", ""]
    out += ["- %s" % checked(x) for x in limits] or ["None in result.json."]
    out += ["", "## Change", "", _diff_size(run / "change.diff"), "", "## Publish", "", _publish_line(row, validation),
            ""]
    return "\n".join(out)


def write_note(row: dict) -> Path:
    matcher, why = _matcher()
    path = note_path(row["id"])
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(".%s.tmp" % path.name)
    tmp.write_text(note_text(row, matcher, why), encoding="utf-8")
    tmp.replace(path)
    return path


# --------------------------------------------------------------------------- the mail


def send_mail(subject: str, message: str) -> str:
    """A mail through the SMN topic of keys.conf, sent by the key service. Returns a word for the log."""
    from awb.tcp import keys

    try:
        answer = keys.request(keys.admin_socket(), {"op": "notify", "subject": subject, "message": message},
                              timeout=60)
    except OSError:
        return "not sent: the key service is not reachable"
    if not answer.get("ok"):
        return "not sent: the key service refused (%s)" % ("deploy the notify operation" if answer.get("error")
                                                          == "unknown operation" else "refused")
    return "sent" if answer.get("sent") else "not sent: no notify_topic or no lab key of bucket_tenant"


def mail_for(row: dict) -> tuple[str, str]:
    run = publish.queue_dir() / "runs" / row["id"]
    validation = _read_text(run / "validation.txt")
    subject = "Workbench: UI run %s %s" % (row["id"], row["state"])
    message = "\n".join(["UI run %s ended: %s at %s." % (row["id"], row["state"], _time(row["finished"])),
                         "Validation: %s." % _validation_word(validation),
                         "Note: presentations/ui-tasks/%s.result.md" % row["id"],
                         _publish_line(row, validation)])
    return subject, message


# --------------------------------------------------------------------------- the commands


def _say(text: str) -> None:
    print(text, flush=True)


def spawn_watch(job_id: str, mail: bool) -> Path:
    """Start `awb ui watch ID` detached from this process; returns its log file."""
    log = state_dir() / ("%s.watch.log" % job_id)
    argv = [sys.executable, "-m", "awb", "ui", "watch", job_id] + (["--mail"] if mail else [])
    with open(log, "ab") as out:
        subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=out, stderr=subprocess.STDOUT,
                         start_new_session=True, close_fds=True, cwd=state_dir())   # -m reads the cwd first
    return log


def _submit(a) -> int:
    path = Path(a.taskfile).absolute()
    matcher, why = _matcher()
    if matcher is None:
        raise UiError(why)
    job_id, _ = check_task(path, matcher)
    argv = [sys.executable, str(bridge_path()), "submit", "--id", job_id, "--file", str(path)]
    if a.candidate_only:
        argv.append("--candidate-only")
    done = subprocess.run(argv, capture_output=True, text=True, timeout=60)
    if done.returncode != 0:
        raise UiError("the bridge refused the task (exit %d); run bridge.py status" % done.returncode)
    try:
        state = json.loads(done.stdout).get("state", "?")
    except ValueError:
        state = "?"
    _say("UI run %s submitted: %s." % (job_id, state))
    if a.no_watch:
        _say("Not watched: awb ui watch %s writes the note." % job_id)
        return 0
    log = spawn_watch(job_id, a.mail)
    _say("Watched in the background; the note will be %s (log %s)." % (note_path(job_id), log.name))
    return 0


def _watch(a) -> int:
    if not ID_RE.match(a.id):
        raise UiError("the run id has a form the queue never gives")
    lock = open(state_dir() / ("%s.watch.lock" % a.id), "a")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        _say("A watch for %s already runs." % a.id)
        return 0
    deadline = time.monotonic() + a.timeout
    while True:
        rows = runs(a.id)
        if not rows:
            raise UiError("the queue has no run with this id")
        row = rows[0]
        if row["state"] not in OPEN:
            break
        if time.monotonic() >= deadline:
            _say("UI run %s is still %s; the watch gave up." % (a.id, row["state"]))
            return 1
        time.sleep(a.interval)
    path = write_note(row)
    _say("UI run %s ended: %s. Note %s." % (a.id, row["state"], path))
    if a.mail:
        _say("Mail %s." % send_mail(*mail_for(row)))
    return 0


def _seen_file() -> Path:
    return state_dir() / "reported.txt"


def _status(a) -> int:
    rows = runs(limit=None if a.ended else a.limit)
    if not a.ended:
        for r in rows:
            _say("%-24s %-12s finished %s" % (r["id"], r["state"], _time(r["finished"])))
        if not rows:
            _say("No runs in the queue.")
        return 0
    seen_file = _seen_file()
    ended = [r for r in rows if r["state"] not in OPEN and r["finished"]]
    if not seen_file.exists():
        # first use: what ended before the last day counts as reported
        cutoff = datetime.now().astimezone().timestamp() - 86400
        seen = {r["id"] for r in ended if _stamp(r["finished"]) < cutoff}
    else:
        seen = set(seen_file.read_text(encoding="utf-8").split())
    new = [r for r in reversed(ended) if r["id"] not in seen]
    for r in new:
        note = note_path(r["id"])
        _say("UI run %s ended: %s at %s; %s." % (r["id"], r["state"], _time(r["finished"]),
                                                 "note %s" % note if note.exists() else "no note yet"))
    seen |= {r["id"] for r in new}
    tmp = seen_file.with_name(".reported.tmp")
    tmp.write_text("".join("%s\n" % x for x in sorted(seen)), encoding="utf-8")
    tmp.replace(seen_file)
    return 0


def _stamp(value: str) -> float:
    try:
        return datetime.fromisoformat(value).timestamp()
    except ValueError:
        return 0.0


def _parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="awb ui", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("submit", help="check a task file, submit it and watch the run")
    s.add_argument("taskfile")
    s.add_argument("--candidate-only", action="store_true", help="a checked candidate, the source stays as it is")
    s.add_argument("--mail", action="store_true", help="a mail through SMN when the run ends")
    s.add_argument("--no-watch", action="store_true", help="submit only")
    st = sub.add_parser("status", help="one line per run: id, state, finish time")
    st.add_argument("--ended", action="store_true", help="only the runs that ended since the last --ended")
    st.add_argument("--limit", type=int, default=20)
    w = sub.add_parser("watch", help="wait for a run to end and write its result note")
    w.add_argument("id")
    w.add_argument("--mail", action="store_true")
    w.add_argument("--interval", type=float, default=INTERVAL)
    w.add_argument("--timeout", type=float, default=TIMEOUT)
    return ap


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if config.is_work_user():
        print("awb ui: the UI queue is the owner's", file=sys.stderr)
        return 2
    try:
        a = _parser().parse_args(argv)
    except SystemExit as exc:
        return 0 if exc.code in (0, None) else 2
    try:
        return {"submit": _submit, "status": _status, "watch": _watch}[a.cmd](a)
    except UiError as err:
        print("awb ui: %s" % err, file=sys.stderr)
        return 1
    except subprocess.TimeoutExpired:
        print("awb ui: the bridge did not answer", file=sys.stderr)
        return 2
    except OSError as err:
        print("awb ui: %s (operating system error)" % type(err).__name__, file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
