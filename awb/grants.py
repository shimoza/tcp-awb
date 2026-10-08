"""The project grants on the owner side (T12 part 3, P0; F2 closed).

`projects.tsv` belongs to the work user, so a session can write any kind into its own row. The key service
therefore never takes the kind from there for a session: a session reaches a test tenant, and writes there, only
for a project the owner granted with `awb projects kind CODE project` in his own terminal. The grant lives in
`<vault>/grants.json` (the owner's, mode 600): per project code its kind and the time of the grant. The row of
`projects.tsv` still has to be active: a session can take access away by closing its project, never add it.
"""
from __future__ import annotations

import json
import os
import re
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

from awb import config

NAME = "grants.json"
CODE_RE = re.compile(r"^tcp-[a-z0-9]{4}$")
KINDS = ("query", "project")


class GrantError(Exception):
    """The grant cannot be written; the text names the reason, never a value of the vault."""


def path(p: config.Paths) -> Path:
    return p.vault / NAME


def load(p: config.Paths) -> dict[str, dict]:
    try:
        data = json.loads(path(p).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(data, dict):
        return {}
    return {code: g for code, g in data.items() if CODE_RE.match(code) and isinstance(g, dict)
            and g.get("kind") in KINDS}


def granted(p: config.Paths, code: str) -> bool:
    """True when the owner granted `code` the kind project (tenant access)."""
    return isinstance(code, str) and load(p).get(code, {}).get("kind") == "project"


def grant(p: config.Paths, code: str, kind: str, when: float | None = None) -> str | None:
    """Record the owner's kind for `code`; returns the kind before (None without one). Refused for the work user."""
    if config.is_work_user():
        raise GrantError("the grant is the owner's: he runs awb projects kind in his own terminal")
    if not CODE_RE.match(code or "") or kind not in KINDS:
        raise GrantError("a grant names a project code and query or project")
    data = load(p)
    before = data.get(code, {}).get("kind")
    data[code] = {"kind": kind, "time": datetime.fromtimestamp(time.time() if when is None else when,
                                                               timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")}
    p.vault.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".grants.", dir=p.vault)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(data, fh, sort_keys=True, indent=1)
            fh.write("\n")
        os.chmod(tmp, 0o600)
        os.replace(tmp, path(p))
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise
    return before
