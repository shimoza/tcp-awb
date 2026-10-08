"""The intake counts of the owner level (T9 step 2): `<vault>/intake-counts.json`, per customer code the candidates
that wait for a review and the time of the last report. Written by the intake and `awb register review`, read by
`awb owner status`. Codes and numbers only; no report is opened to count."""
from __future__ import annotations

import json
import os
import re
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

from awb import config

COUNTS_NAME = "intake-counts.json"
CUSTOMER_RE = re.compile(r"^CUST-[A-Z2-7]{4}$")


def now_text(t: float | None = None) -> str:
    return datetime.fromtimestamp(time.time() if t is None else t, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def counts_path(p: config.Paths) -> Path:
    return p.vault / COUNTS_NAME


def read_counts(p: config.Paths) -> dict:
    try:
        data = json.loads(counts_path(p).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return {k: v for k, v in data.items() if CUSTOMER_RE.match(k) and isinstance(v, dict)} if isinstance(data, dict) else {}


def record_intake(p: config.Paths, customer: str, waiting: int, when: float | None = None) -> None:
    """Set the count of candidates waiting for `customer` and the time of its last report. Codes only; a failure
    to write never stops an intake or a review."""
    if not CUSTOMER_RE.match(customer or "") or not isinstance(waiting, int) or waiting < 0:
        return
    data = read_counts(p)
    data[customer] = {"waiting": waiting, "time": now_text(when)}
    try:
        fd, tmp = tempfile.mkstemp(prefix=".intake-counts.", dir=p.vault)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(data, fh, sort_keys=True)
            fh.write("\n")
        os.chmod(tmp, 0o600)
        os.replace(tmp, counts_path(p))
    except OSError:
        try:
            os.unlink(tmp)
        except (OSError, UnboundLocalError):
            pass
