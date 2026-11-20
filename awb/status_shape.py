"""The shape of the owner level's status file (T9 step 2): every field checked before `awb owner status --write`
writes it and again when owner-actions reads it. Standard library only, so owner-actions imports nothing else."""
from __future__ import annotations

import re

from awb import codes
from awb.intake_counts import CUSTOMER_RE

RUN_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
COMMIT_RE = re.compile(r"^(?:[0-9a-f]{7,40}|pre-deploy|plain)$")
TIME_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:?\d{2})?$")
DAEMON_STATES = ("unlocked", "locked", "plain", "down", "unknown")
RUN_STATES = ("queued", "running", "ready", "completed", "failed", "cancelled", "unknown")
WITHHELD_RE = re.compile(r"^(?:unavailable|(?:name|secret|token|private-key)(?:,(?:secret|token|private-key))*)$")
SUMMARY_MAX = 300
RUN_KEYS = {"id", "state", "created", "finished", "validation", "diff_bytes", "candidate_only", "publishable",
            "summary", "withheld"}


class StatusError(Exception):
    """The status cannot be written; the text names a field or a class, never a value."""


def _word(value, words) -> bool:
    return value in words


def summary_ok(summary, withheld) -> bool:
    """A run summary is one printable line of at most SUMMARY_MAX characters, or None; withheld is None or the
    classes that withheld it, never both a summary and a class."""
    if withheld is not None:
        return summary is None and isinstance(withheld, str) and bool(WITHHELD_RE.match(withheld))
    return summary is None or (isinstance(summary, str) and 0 < len(summary) <= SUMMARY_MAX and summary.isprintable())


def validate(status: dict) -> None:
    """Every field of the status in its fixed shape, or StatusError naming the field."""
    def bad(field):
        raise StatusError("the status field %s has an unexpected shape" % field)

    def time_or_none(v):
        return v is None or (isinstance(v, str) and TIME_RE.match(v))

    def count_or_none(v):
        return v is None or (isinstance(v, int) and not isinstance(v, bool) and v >= 0)

    if set(status) != {"version", "written", "vault", "keys", "deployed", "page", "ui_runs", "intake"}:
        bad("set")
    if status["version"] != 1 or not time_or_none(status["written"]):
        bad("written")
    for name in ("vault", "keys"):
        d = status[name]
        if set(d) != {"state", "since"} or not _word(d["state"], DAEMON_STATES) or not time_or_none(d["since"]):
            bad(name)
    d = status["deployed"]
    if set(d) != {"release", "journal"} or not (d["release"] is None or COMMIT_RE.match(d["release"])) or not (
            d["journal"] is None or re.match(r"^[a-z]{1,20}$", d["journal"])):
        bad("deployed")
    d = status["page"]
    if set(d) != {"size", "time", "backup_time", "backups"} or not count_or_none(d["size"]) or not time_or_none(
            d["time"]) or not time_or_none(d["backup_time"]) or not count_or_none(d["backups"]):
        bad("page")
    for run in status["ui_runs"]:
        if set(run) != RUN_KEYS or not RUN_RE.match(run["id"]) or not _word(run["state"], RUN_STATES) or not \
                time_or_none(run["created"]) or not time_or_none(run["finished"]) or run["validation"] not in (
                "PASS", "FAIL", None) or not count_or_none(run["diff_bytes"]) or not isinstance(
                run["candidate_only"], bool) or not isinstance(run["publishable"], bool) or not summary_ok(
                run["summary"], run["withheld"]):
            bad("ui_runs")
    for row in status["intake"]:
        if set(row) != {"customer", "waiting", "time"} or not CUSTOMER_RE.match(row["customer"]) or not \
                count_or_none(row["waiting"]) or not time_or_none(row["time"]):
            bad("intake")


# --------------------------------------------------------------------------- the dev asks and the queue (Q-0025)

ASK_FILE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}-%s-[1-9][0-9]{0,3}\.md$" % codes.PROJECT)   # awb.devask.FILE_RE
PROJECT_CODE_RE = re.compile(r"^(?:%s|%s)$" % (codes.PROJECT, codes.TCP_PROJECT_WIDE))
QUEUE_ID_RE = re.compile(r"^[A-Za-z][A-Za-z0-9-]{0,39}$")                                       # awb.workqueue.HANDLE
QUEUE_WHO_RE = re.compile(r"^(?:owner|desk|development|session:[A-Za-z][A-Za-z0-9-]{0,39})$")
TITLE_MAX = 120
LIST_MAX = 200
ASK_KEYS = {"code", "file", "title", "withheld", "created", "blocks"}
QUEUE_KEYS = {"id", "who", "project", "what", "next", "withheld", "created", "next_changed", "stale"}
LIST_FIELDS = ("dev_asks", "queue", "lists_built")
"""The optional fields of the status that the owner page shows as Dev asks and Queue (Q-0025). An older status file
has none of them and stays valid."""


def _text_ok(text, withheld, top: int) -> bool:
    if withheld is not None:
        return text is None and isinstance(withheld, str) and bool(WITHHELD_RE.match(withheld))
    return text is None or (isinstance(text, str) and 0 < len(text) <= top and text.isprintable())


def validate_lists(status: dict) -> None:
    """The dev asks and the queue rows of the status, each field in its fixed shape or StatusError. A free text is
    one printable line that passed the name check or None with the classes that withheld it."""
    def bad(field):
        raise StatusError("the status field %s has an unexpected shape" % field)

    def time_ok(v):
        return isinstance(v, str) and bool(TIME_RE.match(v))

    built = status.get("lists_built", 0)
    if type(built) is not int or built < 0:
        bad("lists_built")
    asks = status.get("dev_asks", [])
    if not isinstance(asks, list) or len(asks) > LIST_MAX:
        bad("dev_asks")
    for row in asks:
        if (not isinstance(row, dict) or set(row) != ASK_KEYS or not isinstance(row["code"], str)
                or not PROJECT_CODE_RE.match(row["code"]) or not isinstance(row["file"], str)
                or not ASK_FILE_RE.match(row["file"]) or not row["file"][11:].startswith(row["code"] + "-")
                or not _text_ok(row["title"], row["withheld"], TITLE_MAX) or not time_ok(row["created"])
                or not isinstance(row["blocks"], bool)):
            bad("dev_asks")
    queue = status.get("queue", {"readable": True, "rows": []})
    if (not isinstance(queue, dict) or set(queue) != {"readable", "rows"} or not isinstance(queue["readable"], bool)
            or not isinstance(queue["rows"], list) or len(queue["rows"]) > LIST_MAX):
        bad("queue")
    seen = set()
    for row in queue["rows"]:
        if (not isinstance(row, dict) or set(row) != QUEUE_KEYS or not isinstance(row["id"], str)
                or not QUEUE_ID_RE.match(row["id"]) or row["id"] in seen or not isinstance(row["who"], str)
                or not QUEUE_WHO_RE.match(row["who"])
                or not (row["project"] is None or (isinstance(row["project"], str)
                                                   and PROJECT_CODE_RE.match(row["project"])))
                or not _text_ok(row["what"], row["withheld"], SUMMARY_MAX)
                or not _text_ok(row["next"], row["withheld"], SUMMARY_MAX)
                or not time_ok(row["created"]) or not time_ok(row["next_changed"])
                or not isinstance(row["stale"], bool)):
            bad("queue")
        seen.add(row["id"])


# --------------------------------------------------------------------------- what runs now (Q-0014)

RUNNING_KINDS = ("task", "check", "deploy", "lab", "ask", "labrun", "job", "vault", "keys", "login")  # events.KINDS
RUNNING_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._@/-]{0,79}$")                               # awb.events.ID_RE
RUNNING_KEYS = {"kind", "id", "project", "since", "eta", "overdue"}
RUNNING_FIELDS = ("running", "ended")
"""The optional fields of the status with the open and the ended items of the events file (awb/events.py); an older
status file has none and stays valid."""
ENDED_KEYS = {"kind", "id", "project", "start", "end", "outcome"}
ENDED_OUTCOMES = ("ok", "red", "stopped", "needs-you")                                         # awb.events.OUTCOMES


def validate_running(status: dict) -> None:
    """The open items of the events file: kind, id, project code or None, since, ETA or None, overdue."""
    def bad():
        raise StatusError("the status field running has an unexpected shape")

    rows = status.get("running", [])
    if not isinstance(rows, list) or len(rows) > LIST_MAX:
        bad()
    for row in rows:
        if (not isinstance(row, dict) or set(row) != RUNNING_KEYS or row["kind"] not in RUNNING_KINDS
                or not isinstance(row["id"], str) or not RUNNING_ID_RE.fullmatch(row["id"])
                or not (row["project"] is None or (isinstance(row["project"], str)
                                                   and PROJECT_CODE_RE.match(row["project"])))
                or not isinstance(row["since"], str) or not TIME_RE.match(row["since"])
                or not (row["eta"] is None or (isinstance(row["eta"], str) and TIME_RE.match(row["eta"])))
                or not isinstance(row["overdue"], bool)):
            bad()
    validate_ended(status)


def validate_ended(status: dict) -> None:
    """The items of the events file that ended in the last 24 hours (Q-0014 phase 2): kind, id, project code or
    None, start or None, end, outcome."""
    def bad():
        raise StatusError("the status field ended has an unexpected shape")

    rows = status.get("ended", [])
    if not isinstance(rows, list) or len(rows) > LIST_MAX:
        bad()
    for row in rows:
        if (not isinstance(row, dict) or set(row) != ENDED_KEYS or row["kind"] not in RUNNING_KINDS
                or not isinstance(row["id"], str) or not RUNNING_ID_RE.fullmatch(row["id"])
                or not (row["project"] is None or (isinstance(row["project"], str)
                                                   and PROJECT_CODE_RE.match(row["project"])))
                or not (row["start"] is None or (isinstance(row["start"], str) and TIME_RE.match(row["start"])))
                or not isinstance(row["end"], str) or not TIME_RE.match(row["end"])
                or row["outcome"] not in ENDED_OUTCOMES):
            bad()
