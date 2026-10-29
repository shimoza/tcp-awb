"""`awb clean files plan|apply|undo` (task R6 of presentations/reports-design/DESIGN.md §3.3 to §3.5): the move plan
of the host carried out on the laptop, file by file, with an undo.

    awb clean files plan [STAMP|latest]
    awb clean files apply [STAMP] [--go] [--include-onedrive]
    awb clean files undo [STAMP]

`plan` fetches `reports/plan/<stamp>/PLAN.tsv` of the bucket awb (`latest`, the default, is the newest stamp there)
with his own key (AWB_OBS_AK and AWB_OBS_SK) through HTTPS_PROXY, the client path of `awb clean fetch`, keeps it as
`vault/plans/<stamp>.tsv` of the laptop folder (mode 600) and prints it with the real paths: every id of the plan is
looked up in the full local inventories of `awb clean inventory` (`vault/inventory/*.tsv`). Those lines are for the
local screen only; nothing is sent back.

`apply` takes the plan fetched last (or the one of STAMP) and, without `--go`, prints what it would do and changes
nothing. With `--go` it moves the files of the rows `move` and `archive` into the layout root of `awb clean layout`:
a `move` target is `YYYY-MM/tcp-xxxx/private|clean|out/` plus the real file name, an `archive` target
`YYYY-MM/tcp-xxxx/_archive/<stamp>/` or `_archive/<stamp>/` plus the real path relative to the root of its
inventory. A row `keep` is left alone. Each file is checked first and skipped with its reason when:

- its id is not in a local inventory; the target is not one of the two forms above (or names another project);
- it is missing, no regular file or a link; its size or sha256 differ from the inventory (changed since);
- it lies outside the root of its inventory; its new place lies outside the layout root;
- it or its new place lies in the report folder or the laptop folder; it is a workbook with the fingerprint of
  the report workbook (rules/report-workbook.txt);
- it or its new place lies under OneDrive and `--include-onedrive` is not given (a move would download it).

Nothing is ever deleted and nothing is overwritten: a name that is taken gets ` (2)`, ` (3)` ... before its suffix.
A move is a hard link and the removal of the old name; across drives it is a copy whose sha256 is checked before the old
name goes. Every move is written at once to the undo journal `vault/moves/<stamp>.tsv` (id, from, to, sha256; mode
600, laptop only). `undo` moves every file of the journal of STAMP (default the newest) back to its old path when it
still has its sha256 at the new one, again without overwriting; it keeps the journal as `<stamp>.undone-<time>.tsv`.

Standard library only. Nothing here opens a network connection but the one download of `plan`.
"""
from __future__ import annotations

import errno
import hashlib
import os
import re
import shutil
import stat
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from awb import bucket_layout, cleaninventory, cleanreport, codes, laptop, reportbook

PLAN_COLUMNS = ("id", "place", "action", "target", "project", "path", "keeper", "reason")
"""The columns of PLAN.tsv, as `awb files plan` writes them (filesplan.COLUMNS; a test pins both)."""
JOURNAL_COLUMNS = ("id", "from", "to", "sha256")
PLANS = "plans"
MOVES = "moves"
LATEST = "latest"
MAX_PLAN = 16 * 1024 * 1024
EXIT_OK, EXIT_HELD, EXIT_ERROR = 0, 1, 2
ACTIONS = ("keep", "move", "archive")
STAMP_RE = re.compile(r"^[0-9]{8}-[0-9]{6}$")
_KEY_RE = re.compile(r"^%s([0-9]{8}-[0-9]{6})/%s$" % (re.escape(bucket_layout.PLAN_PREFIX),
                                                      re.escape(bucket_layout.PLAN_FILE)))
_MONTH = bucket_layout.MONTH
_MOVE_RE = re.compile(r"^(%s)/([a-z0-9-]+)/(private|clean|out)/$" % _MONTH)
_ARCHIVE_RE = re.compile(r"^(?:(%s)/([a-z0-9-]+)/)?_archive/([0-9]{8}-[0-9]{6})/$" % _MONTH)


class FilesError(Exception):
    """A step of `awb clean files` cannot run. The message carries stamps, ids and counts, never a path."""


@dataclass
class Step:
    id: str
    action: str
    src: Path | None = None
    dst: Path | None = None
    root: Path | None = None
    sha256: str = ""
    size: int = -1
    problem: str = ""


# --------------------------------------------------------------------------- the files of the laptop folder


def _vault_dir(name: str) -> Path:
    laptop.init()
    folder = laptop.vault() / name
    folder.mkdir(parents=True, exist_ok=True)
    os.chmod(folder, 0o700)
    return folder


def plan_path(stamp: str) -> Path:
    return laptop.vault() / PLANS / ("%s.tsv" % stamp)


def journal_path(stamp: str) -> Path:
    return laptop.vault() / MOVES / ("%s.tsv" % stamp)


def _newest(folder: Path) -> str | None:
    try:
        stamps = sorted(p.stem for p in folder.iterdir() if p.suffix == ".tsv" and STAMP_RE.match(p.stem))
    except OSError:
        return None
    return stamps[-1] if stamps else None


def _check_stamp(stamp: str) -> str:
    if not STAMP_RE.match(stamp or ""):
        raise FilesError("a plan stamp reads YYYYMMDD-HHMMSS or latest")
    return stamp


# --------------------------------------------------------------------------- plan: fetch and read


def fetch_plan(spec: str | None = None, *, client=None) -> str:
    """PLAN.tsv of `spec` (a stamp, default the newest) from reports/plan/ of the bucket into vault/plans/. Returns
    the stamp."""
    from awb import clean, obs

    spec = spec or LATEST
    if spec != LATEST:
        _check_stamp(spec)
    c = client or clean._client("awb clean files plan")
    folder = _vault_dir(PLANS)
    try:
        stamps = sorted({m.group(1) for k, _, _ in c.list(bucket_layout.PLAN_PREFIX).objects
                         for m in [_KEY_RE.match(k)] if m})
        if not stamps:
            raise FilesError("no plan in %s yet: the host makes it with awb files plan --put"
                             % bucket_layout.PLAN_PREFIX)
        stamp = stamps[-1] if spec == LATEST else spec
        if stamp not in stamps:
            raise FilesError("no plan %s in %s (newest %s)" % (stamp, bucket_layout.PLAN_PREFIX, stamps[-1]))
        part = folder / (".%s.part" % stamp)
        try:
            c.get(bucket_layout.plan_key(stamp), part)
            os.chmod(part, 0o600)
            if part.stat().st_size > MAX_PLAN:
                raise FilesError("the plan %s is larger than %d MB" % (stamp, MAX_PLAN // (1024 * 1024)))
            read_plan(part)
            os.replace(part, plan_path(stamp))
        finally:
            part.unlink(missing_ok=True)
    except obs.OBSError as err:
        raise FilesError("the bucket: %s" % err) from None
    return stamp


def read_plan(path: Path) -> list[dict]:
    """The laptop rows of a PLAN.tsv; a file whose header is not the eight columns is refused."""
    try:
        lines = Path(path).read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeDecodeError) as err:
        raise FilesError("the plan cannot be read (%s)" % type(err).__name__) from None
    if not lines or tuple(lines[0].split("\t")) != PLAN_COLUMNS:
        raise FilesError("the header of the plan is not the eight columns of awb files plan")
    rows = []
    for ln in lines[1:]:
        if not ln.strip():
            continue
        r = dict(zip(PLAN_COLUMNS, ln.split("\t")))
        if r.get("place") == "laptop" and r.get("action") in ACTIONS:
            rows.append(r)
    return rows


def local_plan(stamp: str | None) -> tuple[str, list[dict]]:
    """The stamp and the rows of a plan fetched before: STAMP, else the newest in vault/plans/."""
    stamp = _check_stamp(stamp) if stamp else _newest(laptop.vault() / PLANS)
    if stamp is None:
        raise FilesError("no plan on this laptop yet: fetch it with awb clean files plan")
    path = plan_path(stamp)
    if not path.is_file():
        raise FilesError("the plan %s is not on this laptop: fetch it with awb clean files plan %s" % (stamp, stamp))
    return stamp, read_plan(path)


def local_inventory() -> dict[str, dict]:
    """Every row of the full local inventories (vault/inventory/*.tsv) by its id, with its root and real path."""
    rows: dict[str, dict] = {}
    folder = laptop.vault() / "inventory"
    try:
        files = sorted(p for p in folder.iterdir() if p.suffix == ".tsv")
    except OSError:
        return rows
    for f in files:
        try:
            lines = f.read_text(encoding="utf-8").splitlines()
        except (OSError, UnicodeDecodeError):
            continue
        if not lines or tuple(lines[0].split("\t")) != cleaninventory.LOCAL_COLUMNS:
            continue
        for ln in lines[1:]:
            r = dict(zip(cleaninventory.LOCAL_COLUMNS, ln.split("\t")))
            if r.get("id") and r.get("real path") and r.get("root"):
                rows[r["id"]] = r
    return rows


# --------------------------------------------------------------------------- resolve and check


def _target_ok(row: dict) -> bool:
    target = row.get("target", "")
    if row["action"] == "move":
        m = _MOVE_RE.match(target)
    else:
        m = _ARCHIVE_RE.match(target)
    if not m or ".." in target.split("/"):
        return False
    code = m.group(2)
    if code is None:
        return True
    return codes.is_project_code(code) and code.startswith("tcp-") and row.get("project") in (code, "none")


def resolve(rows: list[dict], inv: dict[str, dict], layout: Path | None) -> list[Step]:
    """One step per row move and archive, with the real old and new path (no check of the file yet)."""
    steps = []
    for r in rows:
        if r["action"] == "keep":
            continue
        step = Step(r["id"], r["action"])
        steps.append(step)
        found = inv.get(r["id"])
        if found is None:
            step.problem = "not in the local inventory"
            continue
        step.src, step.root, step.sha256 = Path(found["real path"]), Path(found["root"]), found.get("sha256", "")
        try:
            step.size = int(found.get("size", "-1"))
        except ValueError:
            step.size = -1
        if not _target_ok(r):
            step.problem = "the target is not a folder of the layout"
            continue
        if layout is None:
            step.problem = "no layout root: set it with awb clean layout ROOT"
            continue
        try:
            rel = Path(os.path.abspath(step.src)).relative_to(os.path.abspath(step.root))
        except ValueError:
            step.problem = "outside the root of its inventory"
            continue
        tail = Path(step.src.name) if r["action"] == "move" else rel
        step.dst = Path(os.path.abspath(layout / r["target"] / tail))
    return steps


def _under(path: Path, root: Path) -> bool:
    real, base = os.path.realpath(path), os.path.realpath(root)
    return real == base or real.startswith(base.rstrip(os.sep) + os.sep)


def _real_parent(path: Path) -> Path:
    """The path with its deepest existing folder resolved, so a link in the folders above is seen."""
    p = Path(path)
    tail: list[str] = []
    while not p.exists() and p != p.parent:
        tail.insert(0, p.name)
        p = p.parent
    return Path(os.path.realpath(p)).joinpath(*tail)


def _sha256(path: Path) -> str:
    sha = hashlib.sha256()
    with open(path, "rb") as fh:
        for block in iter(lambda: fh.read(1024 * 1024), b""):
            sha.update(block)
    return sha.hexdigest()


def check(step: Step, layout: Path, include_onedrive: bool) -> str:
    """Why the step must be skipped, empty when it may go."""
    if step.problem:
        return step.problem
    src, dst = step.src, step.dst
    home = laptop.home()
    if not _under(src, step.root):
        return "outside the root of its inventory"
    if not _under(_real_parent(dst), layout):
        return "its new place is outside the layout root"
    if cleanreport.in_report_folder(src) or cleanreport.in_report_folder(_real_parent(dst)):
        return "the report folder"
    if _under(src, home) or _under(_real_parent(dst), home):
        return "the laptop folder"
    if not include_onedrive and (cleaninventory._onedrive(src) or cleaninventory._onedrive(dst)):
        return "under OneDrive (add --include-onedrive)"
    try:
        st = os.lstat(src)
    except OSError:
        return "missing"
    if stat.S_ISLNK(st.st_mode) or not stat.S_ISREG(st.st_mode):
        return "not a regular file"
    if reportbook.is_report_workbook(src):
        return "a report workbook"
    try:
        if st.st_size != step.size or _sha256(src) != step.sha256:
            return "changed since the inventory"
    except OSError:
        return "unreadable"
    if os.path.abspath(src) == os.path.abspath(dst):
        return "already there"
    return ""


def free_name(path: Path, taken: set[str] | None = None) -> Path:
    """The path itself when it is free; else `name (2).suffix`, `name (3).suffix` and so on (taken on disk or in
    `taken`)."""
    taken = taken or set()
    candidate, n = Path(path), 1
    while os.path.lexists(candidate) or str(candidate) in taken:
        n += 1
        candidate = path.with_name("%s (%d)%s" % (path.stem, n, path.suffix))
    return candidate


# --------------------------------------------------------------------------- the move


def move_file(src: Path, dst: Path, sha256: str) -> Path:
    """`src` to `dst` without overwriting (a taken name gets a number) and without losing it: a hard link and the
    removal of the old name, else a copy checked by sha256 first. Returns the new path."""
    dst.parent.mkdir(parents=True, exist_ok=True)
    while True:
        dst = free_name(dst)
        try:
            os.link(src, dst, follow_symlinks=False)
        except FileExistsError:
            continue
        except OSError as err:
            if err.errno not in (errno.EXDEV, errno.EPERM, errno.ENOTSUP, errno.EOPNOTSUPP, errno.EMLINK,
                                 errno.EACCES):
                raise
            dst = _copy_checked(src, dst, sha256)
        os.unlink(src)
        return dst


def _copy_checked(src: Path, dst: Path, sha256: str) -> Path:
    while True:
        try:
            fd = os.open(dst, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            break
        except FileExistsError:
            dst = free_name(dst)
    try:
        with os.fdopen(fd, "wb") as out, open(src, "rb") as fh:
            shutil.copyfileobj(fh, out, 1024 * 1024)
        shutil.copystat(src, dst)
        if _sha256(dst) != sha256:
            raise OSError(errno.EIO, "the copy differs")
    except BaseException:
        os.unlink(dst)                                  # only the copy this call made
        raise
    return dst


def _journal_line(path: Path, values: tuple[str, ...]) -> None:
    new = not path.exists()
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    with os.fdopen(fd, "a", encoding="utf-8", newline="\n") as fh:
        if new:
            fh.write("\t".join(JOURNAL_COLUMNS) + "\n")
        fh.write("\t".join(values) + "\n")
        fh.flush()
        os.fsync(fh.fileno())


# --------------------------------------------------------------------------- the three commands


def _line(word: str, step: Step, dst: Path | None = None) -> str:
    src = str(step.src) if step.src else "(no local path)"
    return "%-10s %s  %s -> %s" % (word, step.id, src, dst or step.dst or "(no target)")


def show(stamp: str | None = None, *, client=None, say=print) -> int:
    """`awb clean files plan`: fetch the plan and print it with the real paths. Returns the rows without a local
    path or a valid target."""
    stamp = fetch_plan(stamp, client=client)
    rows = read_plan(plan_path(stamp))
    steps = resolve(rows, local_inventory(), laptop.layout_root())
    say("plan %s: %d move, %d archive, %d keep (real paths on this screen only, nothing is sent)" % (
        stamp, sum(1 for s in steps if s.action == "move"), sum(1 for s in steps if s.action == "archive"),
        sum(1 for r in rows if r["action"] == "keep")))
    bad = 0
    for s in steps:
        if s.problem:
            bad += 1
            say("%-10s %s  %s: %s" % ("skip", s.id, s.src or "(no local path)", s.problem))
        else:
            say(_line(s.action, s))
    say("nothing moved: awb clean files apply %s shows the checks, --go moves" % stamp)
    return bad


def apply(stamp: str | None = None, *, go: bool = False, include_onedrive: bool = False, say=print) -> dict:
    """`awb clean files apply`: the dry run; with `go` the moves. Returns the counts."""
    stamp, rows = local_plan(stamp)
    layout = laptop.layout_root()
    if layout is None:
        raise FilesError("no layout root: set it once with awb clean layout ROOT")
    if not layout.is_dir():
        raise FilesError("the layout root is not a folder on this laptop")
    steps = resolve(rows, local_inventory(), layout)
    journal = journal_path(stamp)
    if go:
        _vault_dir(MOVES)
    taken: set[str] = set()
    count = {"moved": 0, "skipped": 0}
    for s in steps:
        why = check(s, layout, include_onedrive)
        if why:
            count["skipped"] += 1
            say("%-10s %s  %s: %s" % ("skip", s.id, s.src or "(no local path)", why))
            continue
        if not go:
            dst = free_name(s.dst, taken)
            taken.add(str(dst))
            count["moved"] += 1
            say(_line("would " + s.action, s, dst))
            continue
        try:
            dst = move_file(s.src, s.dst, s.sha256)
        except OSError as err:
            count["skipped"] += 1
            say("%-10s %s  %s: %s (operating system error)" % ("skip", s.id, s.src, type(err).__name__))
            continue
        _journal_line(journal, (s.id, str(s.src), str(dst), s.sha256))
        count["moved"] += 1
        say(_line(s.action + "d" if s.action == "archive" else "moved", s, dst))
    if go:
        say("plan %s: %d file%s moved, %d skipped, nothing deleted; undo: awb clean files undo %s" % (
            stamp, count["moved"], "" if count["moved"] == 1 else "s", count["skipped"], stamp))
    else:
        say("plan %s, dry run: %d would move, %d skipped, nothing changed; --go moves" % (
            stamp, count["moved"], count["skipped"]))
    return count


def undo(stamp: str | None = None, *, say=print) -> dict:
    """`awb clean files undo`: every file of the journal back to its old path. Returns the counts."""
    stamp = _check_stamp(stamp) if stamp else _newest(laptop.vault() / MOVES)
    if stamp is None:
        raise FilesError("no move journal on this laptop: nothing was applied")
    journal = journal_path(stamp)
    try:
        lines = journal.read_text(encoding="utf-8").splitlines()
    except OSError:
        raise FilesError("no move journal of the plan %s on this laptop" % stamp) from None
    if not lines or tuple(lines[0].split("\t")) != JOURNAL_COLUMNS:
        raise FilesError("the header of the journal %s is not id, from, to, sha256" % stamp)
    entries = [dict(zip(JOURNAL_COLUMNS, ln.split("\t"))) for ln in lines[1:] if ln.strip()]
    count = {"back": 0, "skipped": 0}
    left: list[dict] = []
    for e in reversed(entries):
        old, new = Path(e["from"]), Path(e["to"])
        try:
            st = os.lstat(new)
            ok = stat.S_ISREG(st.st_mode) and _sha256(new) == e["sha256"]
        except OSError:
            ok = False
        if not ok:
            count["skipped"] += 1
            left.append(e)
            say("%-10s %s  %s: not at its new place with its content" % ("skip", e["id"], new))
            continue
        try:
            back = move_file(new, old, e["sha256"])
        except OSError as err:
            count["skipped"] += 1
            left.append(e)
            say("%-10s %s  %s: %s (operating system error)" % ("skip", e["id"], new, type(err).__name__))
            continue
        count["back"] += 1
        say("%-10s %s  %s -> %s%s" % ("back", e["id"], new, back,
                                      "" if back == old else " (the old name was taken)"))
    when = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    done = journal.with_name("%s.undone-%s.tsv" % (stamp, when))
    os.replace(journal, done)
    for e in reversed(left):
        _journal_line(journal, tuple(e[c] for c in JOURNAL_COLUMNS))
    say("plan %s: %d file%s moved back, %d skipped, nothing deleted" % (
        stamp, count["back"], "" if count["back"] == 1 else "s", count["skipped"]))
    return count


# --------------------------------------------------------------------------- the command


def main(argv: list[str]) -> int:
    from awb import clean
    from awb.cli import SafeParser

    sub = argv[:1][0] if argv else ""
    rest = argv[1:]
    try:
        if sub == "plan":
            ap = SafeParser(prog="awb clean files plan", description="The newest move plan of reports/plan/ of the "
                            "bucket with the key of AWB_OBS_AK and AWB_OBS_SK, printed with the real paths of the "
                            "local inventories; this screen only, nothing is sent.")
            ap.add_argument("stamp", nargs="?", default=LATEST, metavar="STAMP|latest",
                            help="the plan (default: the newest)")
            args = ap.parse_args(rest)
            return EXIT_HELD if show(args.stamp) else EXIT_OK
        if sub == "apply":
            ap = SafeParser(prog="awb clean files apply", description="The plan fetched last: without --go what it "
                            "would move, with --go the moves into the layout root; nothing deleted or overwritten, "
                            "a changed or missing file skipped, an undo journal in the laptop vault.")
            ap.add_argument("stamp", nargs="?", default=None, metavar="STAMP",
                            help="the plan (default: the one fetched last)")
            ap.add_argument("--go", action="store_true", help="move the files (without it nothing changes)")
            ap.add_argument("--include-onedrive", action="store_true", help="also move a file under OneDrive")
            args = ap.parse_args(rest)
            count = apply(args.stamp, go=args.go, include_onedrive=args.include_onedrive)
            return EXIT_HELD if count["skipped"] else EXIT_OK
        if sub == "undo":
            ap = SafeParser(prog="awb clean files undo", description="Every file the apply of a plan moved, back "
                            "to its old path, from the undo journal of the laptop vault.")
            ap.add_argument("stamp", nargs="?", default=None, metavar="STAMP",
                            help="the plan (default: the newest journal)")
            args = ap.parse_args(rest)
            count = undo(args.stamp)
            return EXIT_HELD if count["skipped"] else EXIT_OK
        ap = SafeParser(prog="awb clean files", description="The move plan on the laptop: plan [STAMP|latest], "
                        "apply [STAMP] [--go] [--include-onedrive] and undo [STAMP].")
        if sub in ("-h", "--help"):
            ap.print_help()
            return EXIT_OK
        print("awb clean files: say plan, apply or undo", file=sys.stderr)
        return EXIT_ERROR
    except SystemExit as exc:
        return 0 if exc.code in (0, None) else EXIT_ERROR
    except (FilesError, laptop.LaptopError, clean.CleanError) as err:
        print("awb clean files %s: %s" % (sub, err), file=sys.stderr)
        return EXIT_ERROR
    except OSError as err:
        print("awb clean files %s: %s (operating system error)" % (sub, type(err).__name__), file=sys.stderr)
        return EXIT_ERROR
