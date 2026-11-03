"""The write mode of a customer tenant (task TW, B-031): the tenant_write line of keys.conf.

A customer tenant (a read-only tenant of task RT, awb/tcp/readonly.py) is read-only until the customer gives the owner
a written OK to work in it directly. Then the owner runs `awb tenant mode ALIAS write --note TEXT`, which writes

    tenant_write = ALIAS:YYYY-MM-DD:NOTE[, ALIAS:YYYY-MM-DD:NOTE]...

into keys.conf: the day of the switch and a short note on the OK. The line travels to the key service with `awb keys
unlock` like every other setting. A tenant the line does not name is read-only. `awb tenant mode ALIAS readonly`
takes it out again. There is no expiry: `awb owner status` (awb/ownerwait.py) lists every tenant of the line until
the owner switches it back. Standard library only; core code, so the owner status needs no module of the platform
package.
"""
from __future__ import annotations

import os
import re
from pathlib import Path

SETTING = "tenant_write"
NOTE_MAX = 120
_ALIAS_RE = re.compile(r"^[a-z][a-z0-9-]{1,30}$")
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
NOTE_RE = re.compile(r"^[A-Za-z0-9 ._/()#+-]{0,%d}$" % NOTE_MAX)
"""A note: plain words, digits and . _ / ( ) # + -, no comma, colon or equals sign (the separators of the line)."""


def parse(value: str) -> dict[str, dict] | None:
    """{alias: {"since", "note"}} of the tenant_write line, or None when it is not a list of valid entries. An empty
    line is no tenant."""
    out: dict[str, dict] = {}
    if not value.strip():
        return out
    for item in value.split(","):
        alias, sep, rest = item.strip().partition(":")
        since, sep2, note = rest.partition(":")
        note = note.strip()
        if not sep or not sep2 or not _ALIAS_RE.match(alias) or not _DATE_RE.match(since) or \
                not NOTE_RE.match(note) or alias in out:
            return None
        out[alias] = {"since": since, "note": note}
    return out


def render(entries: dict[str, dict]) -> str:
    return ", ".join("%s:%s:%s" % (a, e["since"], e.get("note") or "") for a, e in sorted(entries.items()))


def of_settings(settings: dict) -> dict[str, dict]:
    """The tenants in write mode of the settings; nothing when the line is missing or broken."""
    value = settings.get(SETTING) if isinstance(settings, dict) else None
    return (parse(str(value)) or {}) if value is not None else {}


def settings_file() -> Path:
    """keys.conf of the owner, as awb/tcp/xchg.py finds it."""
    return Path(os.environ.get("AWB_KEYS_CONF") or "~/.config/awb/keys.conf").expanduser()


def read(path: Path | None = None) -> dict[str, dict]:
    """The tenants in write mode of keys.conf; nothing when the file or the line cannot be read."""
    try:
        text = (path or settings_file()).read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return {}
    for line in text.splitlines():
        key, sep, value = line.strip().partition("=")
        if sep and key.strip() == SETTING:
            return parse(value.strip()) or {}
    return {}
