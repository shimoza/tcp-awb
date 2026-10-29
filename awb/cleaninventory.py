"""`awb clean inventory ROOT... [--project tcp-xxxx] [--label LABEL] [--put] [--include-onedrive]` (task R3 of
presentations/reports-design/DESIGN.md §3.1): the files under the folders he names on the laptop, without their
content, for the move plan.

It walks only the roots he names (a Windows path D:\\Kunden is read as /mnt/d/Kunden) and opens a file only to hash
it (sha256 and md5 in one pass) and, for the fingerprint of the report workbook, to read `xl/workbook.xml` of a zip.
Two files come out:

- the full list with the real paths, `vault/inventory/<stamp>.tsv` of the laptop folder, mode 600, on this laptop
  only: the eleven columns of awb/inventory.py, then the root and the real path;
- the coded copy, `inventory/laptop-<stamp>.tsv` of the laptop folder (`laptop-<stamp>-<label>.tsv` with a label):
  the eleven columns only. Every segment of a path relative to its root goes through the name machinery of `awb
  clean` (the registered forms of the laptop register become the customer code, other persons and companies tokens
  like person-1); a path that still carries something, in one segment or across segments read together, is
  exported as `[withheld]` with its id, size and hashes. With several roots a path starts with root1/, root2/ ...;
  the roots themselves stay on the laptop.

Skipped and counted: the report folder of `awb clean report` and every workbook with both sheets Names and Projects
(rules/report-workbook.txt), the laptop folder itself, `_archive` and `.git` folders, `~$` files, `.tmp` files,
Thumbs.db, desktop.ini, links and whatever is no regular file. A root under OneDrive is refused without
`--include-onedrive`: hashing would download its cloud-only files.

`--put` sends the coded copy alone to `reports/inventory/` of the bucket awb with his own key (AWB_OBS_AK and
AWB_OBS_SK) through HTTPS_PROXY, the path of `awb clean put`; a file of that name in the bucket is not replaced.
Nothing printed carries a real path or a name: counts, codes and the two files of the laptop folder.
"""
from __future__ import annotations

import os
import re
import stat
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from awb import cleanreport, inventory, laptop, reportbook

PLACE = "laptop"
LOCAL_COLUMNS = inventory.COLUMNS + ("root", "real path")
"""The columns of the full list in the vault folder: the export's eleven, then the root and the real path."""
SKIP_DIRS = frozenset(("_archive", ".git"))
SKIP_FILES = frozenset(("thumbs.db", "desktop.ini"))
SKIP_SUFFIXES = frozenset((".tmp",))
ONEDRIVE_RE = re.compile(r"^onedrive\b", re.I)
_TOKEN_RE = re.compile(r"\[([a-z]+) (\d+)\]")
_SAFE_RE = re.compile(r"[^\w.-]+")
_SUFFIX_RE = re.compile(r"^\.[a-z0-9]{1,10}$")
_JOIN_RE = re.compile(r"[^\w]+")


class InventoryError(Exception):
    """The inventory cannot run. The message carries counts and codes, never a path or a name."""


@dataclass
class Result:
    rows: list[dict]
    local: Path
    export: Path
    skipped: Counter = field(default_factory=Counter)
    key: str | None = None

    @property
    def withheld(self) -> int:
        return sum(1 for r in self.rows if r["path"] == inventory.WITHHELD)


# --------------------------------------------------------------------------- coding a path


class Coder:
    """The name machinery of `awb clean` over path segments, one engine per inventory so a token means the same
    name in every row."""

    def __init__(self, entries=None):
        from awb.intake.engine import _Engine

        self.engine = _Engine(laptop.entries() if entries is None else entries)

    def segment(self, text: str) -> str:
        coded, _ = self.engine.sanitize(re.sub(r"_+", " ", text), "file name")
        coded = _TOKEN_RE.sub(lambda m: "%s-%s" % (m.group(1), m.group(2)), coded)
        return _SAFE_RE.sub("-", coded).strip("-") or "-"

    def leaks(self, text: str) -> bool:
        view = _JOIN_RE.sub(" ", text).strip()
        return bool(view) and bool(self.engine.leaks(view, candidates=True))

    def path(self, parts: list[str]) -> str:
        """The coded path, or `[withheld]` when a segment or the segments read together still carry something."""
        coded = [self.segment(p) for p in parts]
        if any(self.leaks(c) for c in coded) or self.leaks(" ".join(coded)):
            return inventory.WITHHELD
        return "/".join(coded)

    def suffix(self, name: str, coded: str) -> str:
        suffix = Path(name).suffix.lower()
        if not suffix or not _SUFFIX_RE.match(suffix):
            return ""
        if coded != inventory.WITHHELD:
            return suffix if coded.lower().endswith(suffix) else ""
        return suffix if self.segment(suffix[1:]) == suffix[1:] and not self.leaks(suffix[1:]) else ""

    def label(self, label: str) -> str:
        word = (label or "").strip().lower()
        if not inventory.LABEL_RE.match(word):
            raise InventoryError("--label is one word of letters, digits and dashes, at most 32")
        if self.segment(word) != word or self.leaks(word) or self.leaks(label):
            raise InventoryError("--label carries a name or a datum: choose another word")
        return word


# --------------------------------------------------------------------------- the walk


def _onedrive(path: Path) -> bool:
    return any(ONEDRIVE_RE.match(p) for p in Path(os.path.abspath(path)).parts)


def _check_roots(roots: list[Path], include_onedrive: bool) -> list[Path]:
    out = []
    for i, root in enumerate(roots, start=1):
        if root.is_symlink() or not root.is_dir():
            raise InventoryError("root %d of %d is not a folder" % (i, len(roots)))
        if cleanreport.in_report_folder(root):
            raise InventoryError("root %d of %d: %s" % (i, len(roots), reportbook.REFUSED))
        if _onedrive(root) and not include_onedrive:
            raise InventoryError("root %d of %d lies under OneDrive: hashing would download its cloud-only files; "
                                 "add --include-onedrive to take it anyway" % (i, len(roots)))
        out.append(Path(os.path.abspath(root)))
    return out


def _skip_file(path: Path, name: str, home: str) -> str | None:
    low = name.casefold()
    if low.startswith("~$"):
        return "office lock files"
    if low in SKIP_FILES:
        return "Thumbs.db and desktop.ini"
    if Path(low).suffix in SKIP_SUFFIXES:
        return ".tmp files"
    try:
        st = os.lstat(path)
    except OSError:
        return "unreadable"
    if stat.S_ISLNK(st.st_mode):
        return "links"
    if not stat.S_ISREG(st.st_mode):
        return "not a regular file"
    if os.path.realpath(path).startswith(home):
        return "the laptop folder"
    if cleanreport.in_report_folder(path):
        return "the report folder"
    if reportbook.is_report_workbook(path):
        return "report workbooks"
    return None


def _skip_dir(path: Path, name: str, home: str) -> str | None:
    if name.casefold() in SKIP_DIRS:
        return "_archive and .git folders"
    if path.is_symlink():
        return "links"
    real = os.path.realpath(path)
    if real == home.rstrip(os.sep) or real.startswith(home):
        return "the laptop folder"
    if cleanreport.in_report_folder(path):
        return "the report folder"
    return None


def walk(roots: list[Path], skipped: Counter):
    """(root number, root, the real path, its parts below the root) of every file to list, in a stable order."""
    home = os.path.realpath(laptop.home()) + os.sep
    for n, root in enumerate(roots, start=1):
        for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
            here = Path(dirpath)
            keep = []
            for d in sorted(dirnames):
                why = _skip_dir(here / d, d, home)
                if why:
                    skipped[why] += 1
                else:
                    keep.append(d)
            dirnames[:] = keep
            for f in sorted(filenames):
                path = here / f
                why = _skip_file(path, f, home)
                if why:
                    skipped[why] += 1
                    continue
                yield n, root, path, list(path.relative_to(root).parts)


# --------------------------------------------------------------------------- one run


def _write(path: Path, data: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    os.chmod(path.parent, 0o700)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(data)


def _stamp(now: datetime | None) -> str:
    base = inventory.stamp(now)
    when, n = base, 1
    while (laptop.vault() / "inventory" / ("%s.tsv" % when)).exists():
        n += 1
        when = "%s-%d" % (base, n)
    return when


def run(roots: list[Path], *, project: str | None = None, label: str | None = None, put: bool = False,
        include_onedrive: bool = False, client=None, now: datetime | None = None, say=print) -> Result:
    """The inventory of `roots`. See the module text."""
    if not roots:
        raise InventoryError("no folder given")
    if project:
        laptop.customer_of(project)
    laptop.init()
    roots = _check_roots([Path(r) for r in roots], include_onedrive)
    coder = Coder()
    word = coder.label(label) if label else None
    when = _stamp(now)
    name = inventory.file_name(PLACE, when, word)
    proj = project or inventory.NO_PROJECT
    skipped: Counter = Counter()
    rows: list[dict] = []
    many = len(roots) > 1
    for number, root, path, parts in walk(roots, skipped):
        try:
            st = os.stat(path)
            sha, md5 = inventory.hashes(path)
        except OSError:
            skipped["unreadable"] += 1
            continue
        coded = coder.path(parts)
        if many and coded != inventory.WITHHELD:
            coded = "root%d/%s" % (number, coded)
        suffix = coder.suffix(parts[-1], coded)
        rows.append({"id": inventory.row_id(PLACE, when, len(rows) + 1), "path": coded, "suffix": suffix,
                     "size": str(st.st_size), "mtime": inventory.mtime_text(st.st_mtime), "sha256": sha, "md5": md5,
                     "project": proj, "group": inventory.group_of(proj, coded, suffix), "rank": "", "place": PLACE,
                     "root": str(root), "real path": str(path)})
    inventory.rank(rows)
    local = laptop.vault() / "inventory" / ("%s.tsv" % when)
    _write(local, inventory.text(rows, LOCAL_COLUMNS))
    export = laptop.home() / "inventory" / name
    _write(export, inventory.text(rows))
    res = Result(rows=rows, local=local, export=export, skipped=skipped)
    if put:
        res.key = put_export(export, client=client, say=say)
    return res


def put_export(export: Path, *, client=None, say=print) -> str:
    """The coded copy into reports/inventory/ of the bucket; one of that name there is not replaced."""
    from awb import clean, obs

    if cleanreport.refused(export):
        raise InventoryError(reportbook.REFUSED)
    rows = inventory.read(export)
    if any(set(r) - set(inventory.COLUMNS) for r in rows):
        raise InventoryError("the coded copy carries more than the eleven columns: nothing was sent")
    key = inventory.PREFIX + export.name
    c = client or clean._client("--put")
    try:
        if c.head(key) is not None:
            raise InventoryError("%s is in the bucket already, not replaced" % key)
        c.put_file(key, export, "text/tab-separated-values")
    except obs.OBSError as err:
        raise InventoryError("the bucket: %s" % err) from None
    say("bucket: %s" % key)
    return key


def summary(res: Result) -> list[str]:
    skipped = ", ".join("%s %d" % kv for kv in sorted(res.skipped.items())) or "nothing"
    return ["inventory: %d file%s, %d path%s withheld; skipped: %s" % (
                len(res.rows), "" if len(res.rows) == 1 else "s", res.withheld, "" if res.withheld == 1 else "s",
                skipped),
            "full list with the real paths: %s (600, this laptop only)" % res.local,
            "coded copy: %s" % res.export]
