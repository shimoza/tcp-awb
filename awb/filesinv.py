"""The file inventories of the host and the buckets (presentations/reports-design, section 3.1, task R4).

    sudo -n -u awb -H awb files inventory --host [--out FILE]
    awb files inventory --buckets [--out FILE]

`--host` walks the folder of every project of the register (not a deleted one) as the work user: every regular
file, its sha256 and md5 read as bytes and never parsed, its path relative to the project folder. Not walked: .git,
_archive, __pycache__, .terraform, .venv, node_modules and every link; not listed: Office lock files, .tmp,
Thumbs.db, desktop.ini.

`--buckets` lists, as the owner through the key service, the owner bucket `awb` (the folders `<YYYY-MM>/<code>/` of
the live projects and inbox/; reports/ and the dataset are not project files and are skipped) and the lab bucket
(`<code>/...` of the registered projects and inbox/, with the key service's owner listing `lab_list`). No byte of an
object is read: the etag of a single-part object is its md5, a multipart object has no hash.

Every path goes through the check of a text that leaves the host (`projectfacts.export_hits`: the name check, the
detectors of the commit gate and the candidate rule); a path with a hit is written as `[withheld]` with its id,
size, times and hashes, which is all the matching needs. A name of in/ or inbox/ is an original: it stays withheld
unless it passes. When the check cannot run nothing is written.

The rows have the format of the shared module `awb/inventory.py` (the laptop inventory of `awb clean inventory`
writes the same). By default the host goes to `<shared>/files/host-<stamp>.tsv` and the buckets to
`owner-bucket-<stamp>.tsv` and `lab-bucket-<stamp>.tsv` beside it, where `awb files plan` reads the newest of each
place; `--out FILE` writes all rows of the run into that one file (each row names its place). Output carries codes,
counts and the paths of the files, never a name of a file and never the name of a folder that matches no project.
"""
from __future__ import annotations

import datetime as _dt
import os
import re
import stat
import sys
from pathlib import Path

from awb import bucket_layout, codes, config, inventory, ledger, projects
from awb.projectfacts import export_hits

PROG = "awb files inventory"
SKIP_DIRS = frozenset((".git", "_archive", "__pycache__", ".terraform", ".venv", "node_modules"))
SKIP_NAMES = frozenset(("thumbs.db", "desktop.ini"))
SKIP_SUFFIXES = (".tmp",)
KNOWN_SUFFIXES = frozenset(bucket_layout.CONSOLE_SUFFIXES) | set(bucket_layout.TEXT_SUFFIXES) | \
    set(bucket_layout.IMAGE_SUFFIXES) | {".doc", ".xls", ".xlsm", ".ppt", ".rtf", ".zip", ".msg", ".eml", ".vsdx"}
"""The suffixes a withheld path keeps: a known one says nothing about a name; any other is dropped with the path."""
INBOX = "inbox/"
FILES_DIR = "files"
EXIT_OK, EXIT_ERROR = 0, 2
_CODE_RE = re.compile("^%s$" % codes.TCP_PROJECT_WIDE)
_MONTH_RE = re.compile("^%s/$" % bucket_layout.MONTH)
_SUFFIX_RE = re.compile(r"^\.[a-z0-9]{1,10}$")
_HEX32 = re.compile(r"^[0-9a-f]{32}$")
_CONTROL = re.compile(r"[\x00-\x1f\x7f]")


class InventoryError(Exception):
    """The inventory cannot be made. The message carries codes, counts and reasons, never a name."""


def _row(path: str, size: int, mtime: str, sha: str, md5: str, project: str, place: str) -> dict:
    return {"id": "", "path": path, "suffix": suffix_of(path), "size": str(size), "mtime": mtime, "sha256": sha,
            "md5": md5, "project": project or inventory.NO_PROJECT, "group": "", "rank": "", "place": place}


def suffix_of(path: str) -> str:
    ext = os.path.splitext(path.rsplit("/", 1)[-1])[1].lower()
    return ext if _SUFFIX_RE.match(ext) else ""


# --------------------------------------------------------------------------- the host


def skipped(name: str) -> bool:
    low = name.lower()
    return low in SKIP_NAMES or low.startswith("~$") or low.endswith(SKIP_SUFFIXES)


def _walk(folder: Path):
    """(relative path, absolute path) of every file under `folder`, links neither followed nor listed."""
    for top, dirs, files in os.walk(folder, followlinks=False):
        dirs[:] = sorted(d for d in dirs if d.lower() not in SKIP_DIRS and not os.path.islink(os.path.join(top, d)))
        for name in sorted(files):
            full = os.path.join(top, name)
            if skipped(name) or os.path.islink(full):
                continue
            yield Path(os.path.relpath(full, folder)).as_posix(), Path(full)


def _hash(full: Path) -> tuple[str, str, int, float]:
    """(sha256, md5, size, mtime) of a regular file. The file is opened without following a link and hashed by
    `inventory.hashes` through its open handle, so a link planted after the walk reaches nothing."""
    fd = os.open(full, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode):
            raise OSError("not a regular file")
        sha, md5 = inventory.hashes(Path("/proc/self/fd/%d" % fd))
        return sha, md5, st.st_size, st.st_mtime
    finally:
        os.close(fd)


def host_rows(p: config.Paths) -> tuple[list[dict], int]:
    """The rows of every file in the project folders of the register, before the check. Also the count of files
    that could not be read (gone, a link put in their place, no permission)."""
    rows: list[dict] = []
    unreadable = 0
    for r in sorted(projects.load(p), key=lambda x: x.code):
        if r.state == "deleted":
            continue
        folder = Path(r.path)
        if folder.is_symlink() or not folder.is_dir():
            continue
        for rel, full in _walk(folder):
            try:
                sha, md5, size, mtime = _hash(full)
            except OSError:
                unreadable += 1
                continue
            rows.append(_row(rel, size, inventory.mtime_text(mtime), sha, md5, r.code, "host"))
    return rows, unreadable


# --------------------------------------------------------------------------- the buckets


def md5_of_etag(etag: str) -> str:
    """The etag of a single-part object is its md5; a multipart etag (`<hex>-<parts>`) gives none."""
    tag = str(etag or "").strip().strip('"').lower()
    return tag if _HEX32.match(tag) else ""


def utc_text(value: str) -> str:
    """A time as the object storage writes it (2026-10-03T07:58:12.000Z) as 2026-10-03T07:58:12Z; empty when it
    cannot be read."""
    try:
        when = _dt.datetime.fromisoformat(str(value).strip().replace("Z", "+00:00"))
    except ValueError:
        return ""
    if when.tzinfo is None:
        when = when.replace(tzinfo=_dt.timezone.utc)
    return when.astimezone(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _object_rows(listing, place: str, project: str) -> list[dict]:
    return [_row(key, int(size), utc_text(listing.modified.get(key, "")), "", md5_of_etag(etag), project, place)
            for key, size, etag in listing.objects if not key.endswith("/") and not bucket_layout.is_keep(key)]


def bucket_rows(p: config.Paths, c) -> tuple[list[dict], int]:
    """The rows of the owner bucket and the lab bucket, before the check. Also the count of folders not listed: a
    folder that matches no live project in the owner bucket, no registered project in the lab bucket."""
    reg = {r.code: r for r in projects.load(p) if r.state != "deleted"}
    live = {code for code, r in reg.items() if r.state in projects.LIVE_STATES}
    rows: list[dict] = []
    left = 0
    for month in sorted(m for m in c.list("", "/").prefixes if _MONTH_RE.match(m)):
        for pref in sorted(c.list(month, "/").prefixes):
            code = pref[len(month):].rstrip("/")
            if code not in live:
                left += 1
                continue
            rows += _object_rows(c.list(pref), "owner-bucket", code)
    rows += _object_rows(c.list(INBOX), "owner-bucket", "")
    for pref in sorted(c.lab_list("", "/").prefixes):
        if pref == INBOX:
            rows += _object_rows(c.lab_list(INBOX), "lab-bucket", "")
            continue
        code = pref.rstrip("/")
        if not _CODE_RE.match(code) or code not in reg:
            left += 1
            continue
        rows += _object_rows(c.lab_list(pref), "lab-bucket", code)
    return rows, left


# --------------------------------------------------------------------------- the check and the files


def check(rows: list[dict], reg: Path) -> int:
    """Every path through `export_hits`; a path with a hit, or one a TSV cell cannot hold, becomes `[withheld]`
    and keeps its suffix only when it is a known one. Returns the count withheld. Raises InventoryError when the
    check cannot run."""
    bad = {i for i, row in enumerate(rows) if _CONTROL.search(row["path"])}
    texts = ["" if i in bad else row["path"] for i, row in enumerate(rows)]
    try:
        hits = export_hits(texts, reg) if any(texts) else set()
    except ledger.LedgerError as err:
        raise InventoryError("the paths cannot be checked (%s); nothing is written" % err) from None
    withheld = bad | set(hits)
    for i in withheld:
        rows[i]["path"] = inventory.WITHHELD
        if rows[i]["suffix"] not in KNOWN_SUFFIXES:
            rows[i]["suffix"] = ""
    return len(withheld)


def finish(rows: list[dict], when: str) -> list[dict]:
    """Ids (numbered per place), groups and ranks, in place, after the check."""
    count: dict[str, int] = {}
    for row in rows:
        count[row["place"]] = count.get(row["place"], 0) + 1
        row["id"] = inventory.row_id(row["place"], when, count[row["place"]])
        row["group"] = inventory.group_of(row["project"], row["path"], row["suffix"])
    inventory.rank(rows)
    return rows


def _write(path: Path, data: str) -> None:
    """`data` into `path` in one step: a new file beside it, then a rename over it (a link at `path` is replaced,
    never followed)."""
    tmp = path.with_name(".%s.part" % path.name)
    try:
        tmp.unlink()
    except FileNotFoundError:
        pass
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o640)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(data)
        os.replace(tmp, path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


def make(p: config.Paths, kind: str, out: Path | None, client=None,
         now: _dt.datetime | None = None) -> tuple[list[Path], int, int, int]:
    """Gather, check, number and write. Returns (files written, rows, withheld, not listed or unreadable)."""
    if kind == "host":
        rows, left = host_rows(p)
        places = ("host",)
    else:
        from awb import bucket

        rows, left = bucket_rows(p, client or bucket.client())
        places = ("owner-bucket", "lab-bucket")
    withheld = check(rows, ledger.register_path(p))
    when = inventory.stamp(now)
    finish(rows, when)
    if out is not None:
        _write(out, inventory.text(rows))
        return [out], len(rows), withheld, left
    folder = config.make_dir(p.shared / FILES_DIR, 0o750, shared=True)
    written = []
    for place in places:
        target = folder / inventory.file_name(place, when)
        _write(target, inventory.text([r for r in rows if r["place"] == place]))
        written.append(target)
    return written, len(rows), withheld, left


def main(argv: list[str]) -> int:
    """`awb files inventory --host|--buckets [--out FILE]` and `awb files plan` (awb/filesplan.py). Exit 0 written,
    2 refused or an error."""
    from awb import bucket, filesplan, obs
    from awb.cli import SafeParser

    ap = SafeParser(prog="awb files", description="The files of the projects in every place, without their content.")
    sub = ap.add_subparsers(dest="command", parser_class=SafeParser)
    s = sub.add_parser("inventory", help="list the files of the host or the buckets with sizes, times and hashes")
    which = s.add_mutually_exclusive_group(required=True)
    which.add_argument("--host", action="store_true",
                       help="the project folders of the register, as the work user (sudo -n -u awb -H)")
    which.add_argument("--buckets", action="store_true",
                       help="the owner bucket and the lab bucket through the key service (the owner)")
    s.add_argument("--out", type=Path, default=None, metavar="FILE",
                   help="one file for all rows of the run; default <shared>/files/host-<stamp>.tsv or "
                        "owner-bucket-<stamp>.tsv and lab-bucket-<stamp>.tsv")
    filesplan.add_parser(sub)
    try:
        args = ap.parse_args(argv)
    except SystemExit as exc:
        return ledger._exit_code(exc)
    if args.command == "plan":
        return filesplan.run(args)
    if args.command != "inventory":
        ap.print_usage(sys.stderr)
        return EXIT_ERROR
    kind = "host" if args.host else "buckets"
    try:
        if args.out is not None and args.out.is_dir():
            raise InventoryError("--out names a folder; give the path of the file")
        files, n, withheld, left = make(config.paths(), kind, args.out)
    except (InventoryError, inventory.InventoryError, ledger.LedgerError, projects.ProjectError,
            bucket.BucketError) as err:
        print("%s: %s" % (PROG, err), file=sys.stderr)
        return EXIT_ERROR
    except obs.OBSError as err:
        print("%s: the object storage refused a listing (HTTP %s)" % (PROG, err.status), file=sys.stderr)
        return EXIT_ERROR
    except OSError as err:
        print("%s: %s (operating system error)" % (PROG, type(err).__name__), file=sys.stderr)
        return EXIT_ERROR
    what = "file%s could not be read" if kind == "host" else "folder%s not listed (no project)"
    extra = ("; %d " + what) % (left, "" if left == 1 else "s") if left else ""
    print("%s: %s written, %d file%s of the %s, %d path%s withheld%s" % (
        PROG, ", ".join(str(f) for f in files), n, "" if n == 1 else "s", kind, withheld,
        "" if withheld == 1 else "s", extra))
    return EXIT_OK
