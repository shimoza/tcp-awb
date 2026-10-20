"""The project grants on the owner side (T12 part 3, P0; F2 closed).

`projects.tsv` belongs to the work user, so a session can write any kind into its own row. The key service
therefore never takes the kind from there for a session: a session reaches a test tenant, and writes there, only
for a project the owner granted with `awb projects kind CODE project` in his own terminal. The grant lives in
`<vault>/grants.json` (the owner's, mode 600): per project code its kind, the customer of its row at grant time
(a CUST code or none) and the time of the grant. The row of `projects.tsv` still has to be active: a session can
take access away by closing its project, never add it. The key service takes the customer of a granted project from
the grant alone (the residual of F5 and F6): a row that names another customer is refused, and a grant written
before the customer was recorded carries none until the owner runs `awb projects kind` for it once more.
"""
from __future__ import annotations

import json
import os
import re
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

from awb import codes, config

NAME = "grants.json"
CODE_RE = re.compile("^%s$" % codes.TCP_PROJECT_WIDE)
KINDS = ("query", "project")
CUST_RE = re.compile("^%s$" % codes.CUST)
NO_CUSTOMER = "none"
TOKENS_NAME = "key-tokens.json"
"""K4: the key token of each granted project's folder, recorded by the owner's grant (mode 600, next to the
grants); the key service takes a session's project from the token it presents."""
TOKEN_RE = re.compile(r"^[0-9a-f]{64}$")


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


def customer(p: config.Paths, code: str) -> str | None:
    """The customer the owner's grant recorded for `code`: a CUST code or none; None when there is no grant or the
    grant was written before the customer was recorded."""
    value = load(p).get(code, {}).get("customer") if isinstance(code, str) else None
    return value if isinstance(value, str) and (value == NO_CUSTOMER or CUST_RE.match(value)) else None


def readable(p: config.Paths) -> bool:
    """True when this user can read the owner's grants (the owner can, the work user cannot). False when the vault
    cannot even be looked into: the work user meets a vault folder he may not open (the first live lab run of
    2026-10-12 died there with a traceback)."""
    try:
        return os.access(path(p), os.R_OK) or (not path(p).exists() and os.access(p.vault, os.R_OK | os.X_OK))
    except OSError:
        return False


def grant(p: config.Paths, code: str, kind: str, when: float | None = None,
          customer: str | None = None) -> str | None:
    """Record the owner's kind for `code` and the customer of its row at grant time (a CUST code; none or None for
    a project without one); returns the kind before (None without one). Refused for the work user."""
    if config.is_work_user():
        raise GrantError("the grant is the owner's: he runs awb projects kind in his own terminal")
    if not CODE_RE.match(code or "") or kind not in KINDS:
        raise GrantError("a grant names a project code and query or project")
    customer = NO_CUSTOMER if customer in (None, "", NO_CUSTOMER) else customer
    if customer != NO_CUSTOMER and not CUST_RE.match(customer):
        raise GrantError("the customer of a grant reads CUST-XXXX or none")
    data = load(p)
    before = data.get(code, {}).get("kind")
    data[code] = {"kind": kind, "customer": customer,
                  "time": datetime.fromtimestamp(time.time() if when is None else when,
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


def _load_tokens(p: config.Paths) -> dict[str, str]:
    try:
        data = json.loads((p.vault / TOKENS_NAME).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(data, dict):
        return {}
    return {c: t for c, t in data.items() if CODE_RE.match(c) and isinstance(t, str) and TOKEN_RE.match(t)}


def tokens(p: config.Paths) -> dict[str, str]:
    """The key tokens of the projects the owner granted the kind project, by project code (K4)."""
    recorded = load(p)
    return {c: t for c, t in _load_tokens(p).items() if recorded.get(c, {}).get("kind") == "project"}


def token(p: config.Paths, code: str) -> str | None:
    """The key token recorded for a granted project `code`; None when there is none."""
    return tokens(p).get(code) if isinstance(code, str) else None


def record_token(p: config.Paths, code: str, value: str) -> None:
    """Record the key token of the folder of `code` (K4), the owner's alone like the grant."""
    if config.is_work_user():
        raise GrantError("the grant is the owner's: he runs awb projects kind in his own terminal")
    if not CODE_RE.match(code or "") or not TOKEN_RE.match(value or ""):
        raise GrantError("a key token is 64 hex digits for a project code")
    data = _load_tokens(p)
    data[code] = value
    p.vault.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".key-tokens.", dir=p.vault)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(data, fh, sort_keys=True, indent=1)
            fh.write("\n")
        os.chmod(tmp, 0o600)
        os.replace(tmp, p.vault / TOKENS_NAME)
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise
