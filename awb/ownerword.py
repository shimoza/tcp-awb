"""UXR9: the owner's word stamp. A note the desk or development writes on his word carries one line

    owner's word: "<his words>", 2026-11-20T09:30Z

at its end. The project rules (seal/work-claude/CLAUDE.md) and the development rules (CLAUDE.md) take a note with
this line as his decision: the session acts on it and does not ask him again. The words run through the line
format check of the asks (devask.screen) before anything is written; no name, data or secret check inside.
"""
from __future__ import annotations

import re
from datetime import datetime, timezone

PREFIX = "owner's word: "
MAX_WORDS = 400
LINE_RE = re.compile(r"^owner's word: \"[^\"\n]{1,%d}\", \d{4}-\d{2}-\d{2}T\d{2}:\d{2}Z$" % MAX_WORDS, re.M)


class WordError(ValueError):
    """The words cannot be stamped. The message names the reason, never the words."""


def clean(words) -> str:
    """One line: whitespace folded, a double quote turned into a single one."""
    if not isinstance(words, str):
        raise WordError("give his words as text")
    if any((ord(c) < 32 and c not in "\n\r\t") or ord(c) == 127 for c in words):
        raise WordError("his words carry a control character")
    words = " ".join(words.split()).replace('"', "'")
    if not words:
        raise WordError("give his words, they are empty")
    if len(words) > MAX_WORDS:
        raise WordError("his words are longer than %d characters" % MAX_WORDS)
    return words


def stamp(words, now: datetime | None = None) -> str:
    """The stamp line of his words at `now` (UTC, to the minute)."""
    now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    return '%s"%s", %s' % (PREFIX, clean(words), now.strftime("%Y-%m-%dT%H:%MZ"))


def add(text: str, words, now: datetime | None = None) -> str:
    """The note text with the stamp as its last paragraph."""
    return "%s\n\n%s\n" % (text.rstrip("\n"), stamp(words, now))


def stamped(text: str) -> bool:
    """True when a note carries a well-formed stamp line."""
    return bool(LINE_RE.search(text or ""))


def screened(p, text: str, words, now: datetime | None = None) -> str:
    """`add`, after the line format check of the asks over his words; raises devask.AskError on a control character."""
    from awb import devask

    now = now or datetime.now(timezone.utc)
    devask.screen(p, stamp(words, now))
    return add(text, words, now)
