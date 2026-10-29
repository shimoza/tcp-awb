"""The row format of the three inventories of presentations/reports-design/DESIGN.md §3.1, in one place for every
side: the laptop (`awb clean inventory`, awb/cleaninventory.py) and the host and the buckets (`awb files
inventory`). The move plan (`awb files plan`) reads what they write.

One inventory is one TSV file, UTF-8, LF, one header row with the eleven names of `COLUMNS` in this order and one
row per file:

    id       the row's id: the letter of the place, the stamp of the inventory and a number (L-20261029-101500-0001);
             the laptop finds the real path of a plan row by it in its full local list
    path     the path relative to the root, segments with /, coded; `[withheld]` when it still carries a name
    suffix   the suffix in lower case with its dot, empty without one
    size     bytes
    mtime    UTC, 2026-10-29T07:50:00Z
    sha256   hex; empty where only the md5 is known (a bucket object)
    md5      hex; empty for a multipart object of a bucket
    project  tcp-xxxx, `none` without a project
    group    (project, normalised stem, suffix) as project/stem.suffix (§3.2); `[withheld]` for a withheld path
    rank     1 for the newest of its group by mtime, then 2, 3 ...; empty for a withheld path
    place    laptop, host, owner-bucket or lab-bucket

The file name is `<place>-<stamp>.tsv` (with a label `<place>-<stamp>-<label>.tsv`), in the owner bucket under
`reports/inventory/`. Standard library only; nothing here opens a file but to hash it and to write an inventory.
"""
from __future__ import annotations

import hashlib
import re
from datetime import datetime, timezone
from pathlib import Path

from awb import bucket_layout

COLUMNS = ("id", "path", "suffix", "size", "mtime", "sha256", "md5", "project", "group", "rank", "place")
HEADER = "\t".join(COLUMNS)
WITHHELD = "[withheld]"
NO_PROJECT = "none"
PLACES = {"laptop": "L", "host": "H", "owner-bucket": "B", "lab-bucket": "S"}
"""Each place and the first letter of the ids of its rows."""
PREFIX = bucket_layout.INVENTORY_PREFIX
"""Where the coded inventories lie in the owner bucket."""
STAMP_FORMAT = "%Y%m%d-%H%M%S"
CHUNK = 1024 * 1024
LABEL_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,31}$")

_COPY_RE = re.compile(r"\s*\(\d+\)$")
_DATE_RES = (re.compile(r"(?<!\d)\d{4}-\d{2}-\d{2}(?!\d)"), re.compile(r"(?<!\d)\d{8}(?!\d)"),
             re.compile(r"(?<!\d)\d{2}\.\d{2}\.\d{4}(?!\d)"))
_LAST_WORDS = frozenset(("kopie", "copy", "final", "neu", "new", "alt", "old", "draft", "entwurf"))
_VERSION_RE = re.compile(r"^v(?:[1-9]|[1-9][0-9])$")
_WORD_SPLIT_RE = re.compile(r"[\s._-]+")


class InventoryError(Exception):
    """An inventory cannot be read or written. The message carries counts, never a path."""


def stamp(now: datetime | None = None) -> str:
    return (now or datetime.now(timezone.utc)).astimezone(timezone.utc).strftime(STAMP_FORMAT)


def file_name(place: str, when: str, label: str | None = None) -> str:
    """laptop-20261029-101500.tsv, with a label laptop-20261029-101500-kunden.tsv."""
    if place not in PLACES:
        raise InventoryError("a place is one of %s" % ", ".join(PLACES))
    if label is not None and not LABEL_RE.match(label):
        raise InventoryError("a label is one word of small letters, digits and dashes, at most 32")
    return "%s-%s%s.tsv" % (place, when, "-" + label if label else "")


def row_id(place: str, when: str, number: int) -> str:
    return "%s-%s-%04d" % (PLACES[place], when, number)


def mtime_text(seconds: float) -> str:
    return datetime.fromtimestamp(int(seconds), timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def hashes(path: Path) -> tuple[str, str]:
    """The sha256 and the md5 of a file, read in chunks; nothing of its content is kept."""
    sha, md5 = hashlib.sha256(), hashlib.md5(usedforsecurity=False)
    with open(path, "rb") as fh:
        while True:
            block = fh.read(CHUNK)
            if not block:
                break
            sha.update(block)
            md5.update(block)
    return sha.hexdigest(), md5.hexdigest()


def norm_stem(stem: str) -> str:
    """The stem of §3.2: lower case, without a copy mark ` (2)`, a date (YYYY-MM-DD, YYYYMMDD, DD.MM.YYYY) and the
    version and state words at its end (kopie, copy, v1 to v99, final, neu, new, alt, old, draft, entwurf)."""
    text = _COPY_RE.sub("", (stem or "").strip()).casefold()
    for date_re in _DATE_RES:
        text = date_re.sub(" ", text)
    words = [w for w in _WORD_SPLIT_RE.split(text) if w]
    while len(words) > 1 and (words[-1] in _LAST_WORDS or _VERSION_RE.match(words[-1])):
        words.pop()
    return "-".join(words)


def group_of(project: str, path: str, suffix: str) -> str:
    """project/stem.suffix of a coded path; `[withheld]` for a withheld one."""
    if path == WITHHELD:
        return WITHHELD
    name = path.rsplit("/", 1)[-1]
    stem = name[:-len(suffix)] if suffix and name.lower().endswith(suffix) else name
    return "%s/%s%s" % (project or NO_PROJECT, norm_stem(stem) or "-", suffix)


def rank(rows: list[dict]) -> None:
    """Sets `rank` in place: per group, newest mtime first (the path breaks a tie); withheld rows get none."""
    groups: dict[str, list[dict]] = {}
    for r in rows:
        if r["group"] == WITHHELD:
            r["rank"] = ""
            continue
        groups.setdefault(r["group"], []).append(r)
    for members in groups.values():
        members.sort(key=lambda r: r["path"])
        members.sort(key=lambda r: r["mtime"], reverse=True)
        for n, r in enumerate(members, start=1):
            r["rank"] = str(n)


def _cell(value) -> str:
    return " ".join(str(value).split()) if value is not None else ""


def line(row: dict, columns: tuple[str, ...] = COLUMNS) -> str:
    return "\t".join(_cell(row.get(c, "")) for c in columns)


def text(rows: list[dict], columns: tuple[str, ...] = COLUMNS) -> str:
    """The inventory as a file: the header and one line per row."""
    return "\n".join(["\t".join(columns)] + [line(r, columns) for r in rows]) + "\n"


def read(path: Path) -> list[dict]:
    """The rows of an inventory; a file whose header is not `HEADER` is refused."""
    try:
        lines = Path(path).read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeDecodeError) as err:
        raise InventoryError("the inventory cannot be read (%s)" % type(err).__name__) from None
    if not lines or lines[0].split("\t")[:len(COLUMNS)] != list(COLUMNS):
        raise InventoryError("the header of the inventory is not the eleven columns")
    head = lines[0].split("\t")
    return [dict(zip(head, ln.split("\t"))) for ln in lines[1:] if ln.strip()]
