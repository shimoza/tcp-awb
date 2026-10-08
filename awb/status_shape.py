"""The shape of the owner level's status file (T9 step 2): every field checked before `awb owner status --write`
writes it and again when owner-actions reads it. Standard library only, so owner-actions imports nothing else."""
from __future__ import annotations

import re

from awb.intake_counts import CUSTOMER_RE

RUN_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
COMMIT_RE = re.compile(r"^(?:[0-9a-f]{7,40}|pre-deploy|plain)$")
TIME_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:?\d{2})?$")
DAEMON_STATES = ("unlocked", "locked", "plain", "down", "unknown")
RUN_STATES = ("queued", "running", "ready", "completed", "failed", "cancelled", "unknown")


class StatusError(Exception):
    """The status cannot be written; the text names a field or a class, never a value."""


def _word(value, words) -> bool:
    return value in words


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
        if set(run) != {"id", "state", "finished", "validation", "diff_bytes", "candidate_only", "publishable"} or \
                not RUN_RE.match(run["id"]) or not _word(run["state"], RUN_STATES) or not time_or_none(
                run["finished"]) or run["validation"] not in ("PASS", "FAIL", None) or not count_or_none(
                run["diff_bytes"]) or not isinstance(run["candidate_only"], bool) or not isinstance(
                run["publishable"], bool):
            bad("ui_runs")
    for row in status["intake"]:
        if set(row) != {"customer", "waiting", "time"} or not CUSTOMER_RE.match(row["customer"]) or not \
                count_or_none(row["waiting"]) or not time_or_none(row["time"]):
            bad("intake")
