"""`awb clean report [--dir DIR] [--save]` (task R2 of presentations/reports-design/DESIGN.md §1.5): the codes file
of the report workbook reaches the owner's laptop.

On the host `awb report --codes --put` puts `reports/projects.csv` (six columns, codes only) and
`reports/projects.meta.json` (the time written in UTC, the row count, the count of withheld texts, the sha256 of the
csv and the release) into the owner bucket. On the laptop this command downloads both with his own key (AWB_OBS_AK
and AWB_OBS_SK), through HTTPS_PROXY, the path `awb clean fetch` uses; no new credential. It checks the sha256 of
the sidecar, keeps the file before as `projects.previous.csv` and replaces `projects.csv` in one step: the new file
is written as `projects.csv.part` and moved over the old one, three tries two seconds apart while Excel holds it,
else "Excel holds the file, run it again". The sidecar is read in memory and never written: projects.csv and
projects.previous.csv are the only files the Workbench writes into the report folder. It never uploads.

The folder is `--dir DIR`, else the one saved before with `--dir DIR --save` (the file `report-dir` of the laptop
folder), for example `C:\\Users\\<you>\\awb-report`, read as /mnt/c/Users/<you>/awb-report.

The same folder is the first of the two laptop refusals (`refused`): `awb clean FILE`, `--put`, `awb clean put`,
the page of `awb clean serve` and `awb clean fetch --to` refuse any path in the saved report folder or in a folder
named `awb-report`; `awb clean` and the puts refuse any workbook with both sheets Names and Projects
(awb/reportbook.py), with the one line `the report workbook never leaves the laptop` (rules/report-workbook.txt).
"""
from __future__ import annotations

import csv
import hashlib
import io
import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path

from awb import laptop, reportbook

DIR_FILE = "report-dir"
FOLDER_NAME = "awb-report"
PREFIX = "reports/"
CSV_NAME = "projects.csv"
META_NAME = "projects.meta.json"
PREVIOUS_NAME = "projects.previous.csv"
HEADER = ("project code", "customer code", "task description", "next step", "whose move", "date of last change")
WITHHELD = "(withheld by the name check)"
TRIES, PAUSE = 3, 2.0
MAX_BYTES = 16 * 1024 * 1024
HELD_BY_EXCEL = "Excel holds the file, run it again"


class ReportError(Exception):
    """The codes file cannot be brought. The message carries counts and the folder, never a cell."""


class Held(ReportError):
    """The new file could not replace the old one: Excel holds it."""


# --------------------------------------------------------------------------- the folder and the refusals


def saved_dir() -> Path | None:
    try:
        text = (laptop.home() / DIR_FILE).read_text(encoding="utf-8").strip()
    except OSError:
        return None
    return Path(text).expanduser() if text else None


def save_dir(folder: Path) -> None:
    laptop.init()
    fd = os.open(laptop.home() / DIR_FILE, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(str(folder) + "\n")


def _parts(path: Path) -> tuple[str, ...]:
    return tuple(p.casefold() for p in Path(os.path.realpath(os.path.abspath(Path(path).expanduser()))).parts)


def in_report_folder(path: Path) -> bool:
    """The path is the report folder or lies below it: the saved folder or any folder named awb-report. Case does
    not count (a Windows folder under /mnt/<drive>/ is the same in every case)."""
    parts = _parts(path)
    if FOLDER_NAME in parts:
        return True
    saved = saved_dir()
    if saved is None:
        return False
    root = _parts(saved)
    return len(root) > 1 and parts[:len(root)] == root


def refused(path: Path) -> bool:
    """A file the laptop never sends: one in the report folder or a workbook with the fingerprint."""
    return in_report_folder(path) or reportbook.is_report_workbook(Path(path))


# --------------------------------------------------------------------------- the download


def _age(written: datetime, now: datetime) -> str:
    minutes = max(0, int((now - written).total_seconds() // 60))
    if minutes < 60:
        return "%d min old" % minutes
    if minutes < 48 * 60:
        return "%d h old" % (minutes // 60)
    return "%d days old" % (minutes // (24 * 60))


def _written(meta: dict) -> datetime | None:
    value = meta.get("written")
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        when = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    return when if when.tzinfo else when.replace(tzinfo=timezone.utc)


def _rows(data: bytes) -> tuple[list[str], int, int]:
    """The header, the rows and the withheld texts of the codes file."""
    text = data.decode("utf-8-sig", errors="replace")
    table = list(csv.reader(io.StringIO(text, newline="")))
    header = table[0] if table else []
    body = [r for r in table[1:] if any(c.strip() for c in r)]
    withheld = sum(1 for r in body for c in r if c.strip() == WITHHELD)
    return header, len(body), withheld


def _get(c, key: str, to: Path) -> None:
    from awb import obs

    try:
        c.get(key, to)
    except obs.OBSError as err:
        to.unlink(missing_ok=True)
        if getattr(err, "status", None) == 404:
            raise ReportError("%s is not in the bucket yet: the host puts it with awb report --codes --put" % key) \
                from None
        raise ReportError("the bucket: %s" % err) from None
    if to.stat().st_size > MAX_BYTES:
        to.unlink(missing_ok=True)
        raise ReportError("%s is larger than %d MB" % (key, MAX_BYTES // (1024 * 1024)))


def _replace(part: Path, target: Path, tries: int, pause: float, sleep) -> None:
    for n in range(tries):
        try:
            os.replace(part, target)
            return
        except OSError:
            if n + 1 < tries:
                sleep(pause)
    part.unlink(missing_ok=True)
    raise Held(HELD_BY_EXCEL)


def fetch(folder: Path | None, *, client=None, save: bool = False, now: datetime | None = None, sleep=time.sleep,
          tries: int = TRIES, pause: float = PAUSE, say=print) -> int:
    """projects.csv into the report folder. Returns the number of rows. See the module text."""
    from awb import clean

    folder = folder if folder is not None else saved_dir()
    if folder is None:
        raise ReportError("give the report folder once with --dir DIR --save (for example "
                          "\"C:\\Users\\<you>\\awb-report\")")
    folder = Path(os.path.abspath(Path(folder).expanduser()))
    c = client or clean._client("awb clean report")
    folder.mkdir(parents=True, exist_ok=True)
    if save:
        save_dir(folder)
    target, part = folder / CSV_NAME, folder / (CSV_NAME + ".part")
    meta_tmp = laptop.tmp_dir() / (".%s.%d" % (META_NAME, os.getpid()))
    try:
        _get(c, PREFIX + META_NAME, meta_tmp)
        try:
            meta = json.loads(meta_tmp.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            meta = None
    finally:
        meta_tmp.unlink(missing_ok=True)
    want = meta.get("sha256") if isinstance(meta, dict) else None
    if not isinstance(want, str) or len(want.strip()) != 64:
        raise ReportError("%s carries no sha256: nothing was replaced" % META_NAME)
    _get(c, PREFIX + CSV_NAME, part)
    data = part.read_bytes()
    if hashlib.sha256(data).hexdigest() != want.strip().lower():
        part.unlink(missing_ok=True)
        raise ReportError("the sha256 of %s does not match %s (the host may be writing it: run it again in a "
                          "minute); nothing was replaced" % (CSV_NAME, META_NAME))
    try:
        os.chmod(part, 0o600)
    except OSError:
        pass                                             # a Windows folder keeps no mode
    header, rows, withheld = _rows(data)
    try:
        old = target.read_bytes() if target.is_file() else None
    except OSError:
        old = None
    if old == data:
        part.unlink(missing_ok=True)
        state = "unchanged"
    else:
        prev_part = folder / (PREVIOUS_NAME + ".part")
        if old is not None:
            prev_part.write_bytes(old)
        try:
            _replace(part, target, tries, pause, sleep)
        except Held:
            prev_part.unlink(missing_ok=True)
            raise
        if old is not None:
            try:
                _replace(prev_part, folder / PREVIOUS_NAME, tries, pause, sleep)
            except Held:
                say("%s is held open: the version before was not kept this time" % PREVIOUS_NAME)
        state = "replaced"
    when = _written(meta)
    now = now or datetime.now(timezone.utc)
    stamp = (", written %s UTC (%s)" % (when.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M"), _age(when, now))
             if when else "")
    say("%s: %d row%s, %d text%s withheld%s, %s. In Excel: Data > Refresh All or open report.xlsx."
        % (CSV_NAME, rows, "" if rows == 1 else "s", withheld, "" if withheld == 1 else "s", stamp, state))
    if tuple(h.strip() for h in header) != HEADER:
        say("the header of %s is not the six names of the design: the refresh in Excel stops until the query is "
            "edited" % CSV_NAME)
    return rows
