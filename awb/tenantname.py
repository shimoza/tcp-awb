"""The two names of a tenant: tcp-10491 and its old name test-10491.

A tenant is named tcp- and the five or six digits of the tenant (codes.TENANT), never by a project code (tcp- and four
characters). The old name test-10491 stays a synonym for WINDOW_DAYS days from the date of the line `window-from DATE`
in rules/tenant-alias-window.txt: `canonical` prints one line "test-10491 is the old name of tcp-10491, until DATE" and
returns the new name; after the window it refuses the old one. Standard library only, so a client of the key service
needs nothing of the platform package."""
from __future__ import annotations

import datetime
import re
import sys
from pathlib import Path
from typing import Callable

WINDOW_FILE = Path(__file__).resolve().parents[1] / "rules" / "tenant-alias-window.txt"
WINDOW_DAYS = 30
_OLD_RE = re.compile(r"test-([0-9]{5,6})\Z")
_NEW_RE = re.compile(r"tcp-([0-9]{5,6})\Z")
_WINDOW_LINE_RE = re.compile(r"^window-from\s+(\d{4}-\d{2}-\d{2})$")


class OldNameRefused(ValueError):
    """The old name of a tenant after its window."""


def window_end(path: Path | None = None) -> datetime.date | None:
    """The last day the old name is taken: the date of the line `window-from DATE` of the rules file plus
    WINDOW_DAYS; None when the file or its line is missing (the old name is then refused). Read at each call."""
    try:
        text = Path(path or WINDOW_FILE).read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None
    for line in text.splitlines():
        m = _WINDOW_LINE_RE.match(line.split("#", 1)[0].strip())
        if m:
            try:
                return datetime.date.fromisoformat(m.group(1)) + datetime.timedelta(days=WINDOW_DAYS)
            except ValueError:
                return None
    return None


def twin(alias) -> str | None:
    """The other name of a tenant alias: test-10491 for tcp-10491 and the reverse; None for any other alias."""
    if not isinstance(alias, str):
        return None
    m = _NEW_RE.match(alias)
    if m:
        return "test-" + m.group(1)
    m = _OLD_RE.match(alias)
    return "tcp-" + m.group(1) if m else None


def canonical(alias, today: datetime.date | None = None, say: Callable[[str], None] | None = None,
              window: Path | None = None):
    """The name of an alias as the work rules write it. tcp-10491 and every other alias stay as they are;
    test-10491 becomes tcp-10491 with one warning line while the window lasts (`say` takes it, default standard
    error) and raises OldNameRefused after it."""
    m = _OLD_RE.match(alias) if isinstance(alias, str) else None
    if not m:
        return alias
    new = "tcp-" + m.group(1)
    end = window_end(window)
    if end is None or (today or datetime.date.today()) > end:
        raise OldNameRefused("%s is no tenant name any more: use %s" % (alias, new))
    line = "%s is the old name of %s, until %s" % (alias, new, end.isoformat())
    if say is None:
        print(line, file=sys.stderr)
    else:
        say(line)
    return new
