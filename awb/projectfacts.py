"""What the reports read of a project folder and how they check its texts, in one place for `awb report --period`
(awb/period.py) and `awb report --codes` (awb/reportcodes.py).

- `git_dates`: the committer dates of the project's own repository, optionally of some paths;
- `mtime_date`: the day a file was last written;
- `status_of`: the text of the Status line of STATE.md;
- `export_hits`: the stronger check of a text that leaves the host (the release screen of a solutions package): the
  name check, the secret and pattern detectors of the commit gate and the candidate rule of short texts.

Codes only: a message carries counts, never a text.
"""
from __future__ import annotations

import subprocess
from datetime import date, datetime
from pathlib import Path

from awb import ledger


def git_dates(folder: Path, *paths: str) -> list[date]:
    """Committer dates of the project's own repository (never a repository above it), optionally of some paths.
    A folder this user cannot look into (a project folder of the work user, mode 700, read by the owner) gives no
    date, never an error."""
    try:
        if not (folder / ".git").exists():
            return []
    except OSError:
        return []
    try:
        out = subprocess.run(["git", "-c", "safe.directory=%s" % folder, "-C", str(folder), "log", "--format=%cs",
                              "--", *paths], capture_output=True, text=True, timeout=30,
                             stdin=subprocess.DEVNULL).stdout
    except (OSError, subprocess.SubprocessError):
        return []
    days = []
    for line in out.split():
        try:
            days.append(date.fromisoformat(line))
        except ValueError:
            pass
    return days


def mtime_date(path: Path) -> date | None:
    try:
        return datetime.fromtimestamp(path.stat().st_mtime).date()
    except OSError:
        return None


def status_of(folder: Path) -> str:
    """The text of the Status line of STATE.md, empty when there is none."""
    try:
        text = (folder / "STATE.md").read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return ""
    for line in text.splitlines():
        if line.startswith("Status:"):
            return " ".join(line[len("Status:"):].split())
    return ""


def export_hits(texts: list[str], reg: Path) -> set[int]:
    """Indexes of the texts that must not leave the host: a hit of the name check (one call over all texts first),
    of the gate detectors (secrets, private keys, tokens, home paths, the blocklist) or of the candidate rule of short
    texts (a name that is not registered). An identifier, an address, a host name and a URL are no finding (B-089).
    Only the exit of the solutions packages uses it. Raises LedgerError when the name check cannot run."""
    from awb import intake

    idx = [i for i, t in enumerate(texts) if t]
    if not idx:
        return set()
    ledger.ensure_checkable(reg)
    hits = set(ledger.hit_indexes(texts, reg))
    detectors = ledger._detectors()
    for i in idx:
        if i in hits:
            continue
        for fn in detectors.values():
            try:
                found = fn(texts[i])
            except Exception:
                found = [1]                      # a detector that fails withholds: never a text it could not check
            if found:
                hits.add(i)
                break
        if i not in hits and intake.short_text_candidates(texts[i]):
            hits.add(i)
    return hits
