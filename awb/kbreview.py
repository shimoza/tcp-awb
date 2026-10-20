"""The review before a dataset release (gap G6 of the threat model F2) and the helpers of the built folders.

    awb kb review [--out DIR] [--date D]      the facts of the newest built dataset (or the one of --date) added,
                                              amended (statement, source or grade) or retired since the last release,
                                              each with id, grade, check date, source and statement; then the mark

`awb dataset put` (awb/tcp/dataset.py) refuses a folder without the mark of its review, with a mark of other facts
(a build after the review) or of another release (one went out since), in one line that names the command. A
successful put writes RELEASED.jsonl beside the folders: the facts.jsonl of that release, its first line the date,
the facts the next review compares with. Before the first release every fact is listed as added.

This module is the work user's side of it (awb kb runs in every session and under the kb units) and part of the
core, so it imports nothing of the dataset command or the platform package: dataset.py imports it. The lines are codes only: the facts of a built folder are
name checked by the export and tenant free by the build.
"""
from __future__ import annotations

import datetime
import hashlib
import json
import os
import stat
from pathlib import Path

from awb import config

SLUG = "tcp-facts"


class ReviewError(Exception):
    """The review or the mark check cannot be done. The message names counts, folders and the command, never a fact."""


def sha256_of(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def changes(previous: list[dict], records: list[dict], fields: tuple[str, ...] = ("statement", "source"),
            ) -> tuple[list[dict], list[tuple[dict, dict]], list[str]]:
    """(added, amended, retired) of `records` against `previous`, by id: the added rows, the amended ones as
    (before, now) where one of `fields` changed, the ids no longer there."""
    before = {r["id"]: r for r in previous if r.get("id")}
    now = {r["id"]: r for r in records}
    added = [now[i] for i in sorted(set(now) - set(before))]
    amended = [(before[i], now[i]) for i in sorted(set(now) & set(before))
               if any(now[i].get(f) != before[i].get(f) for f in fields)]
    return added, amended, sorted(set(before) - set(now))


def datasets_dir(p: config.Paths) -> Path:
    """Where the built datasets lie: <shared>/datasets."""
    return p.shared / "datasets"


def latest(p: config.Paths, out: Path | None = None, day: str | None = None) -> Path | None:
    """The newest built folder under `out` (default: the datasets folder), or the one of `day`; None without."""
    base = out or datasets_dir(p)
    if day:
        folder = base / ("%s-%s" % (SLUG, day))
        return folder if folder.is_dir() else None
    folders = sorted(base.glob("%s-????-??-??" % SLUG))
    return folders[-1] if folders else None


def read_facts(path_or_text) -> list[dict]:
    """The fact rows of a facts.jsonl (a path or its text), without its first line of metadata."""
    text = path_or_text.read_text(encoding="utf-8") if isinstance(path_or_text, Path) else path_or_text
    rows = [json.loads(line) for line in text.splitlines() if line.strip()]
    return [r for r in rows if isinstance(r, dict) and r.get("id")]


RELEASED = "RELEASED.jsonl"          # beside the folders: the facts.jsonl of the last release, its first line the date
REVIEW_SUFFIX = ".review.json"       # beside a folder: the mark of its review
REVIEW_FIELDS = ("statement", "source", "grade")
REVIEW_HINT = "run awb kb review first"


def release_assets(folder: Path) -> list[Path]:
    """The files a release uploads and the signed SHA256SUMS lists: the zip, HOW-TO.pdf, MANIFEST.json, the .mcpb."""
    return [folder.parent / (folder.name + ".zip"), folder / "HOW-TO.pdf", folder / "MANIFEST.json",
            folder.parent / (folder.name + ".mcpb")]


def asset_sums(folder: Path) -> dict[str, str]:
    """The sha256 of every release asset there is now, by file name: what the review pins (K3 of the review of
    2026-10-20)."""
    return {a.name: sha256_of(a) for a in release_assets(folder) if a.is_file()}


def released_path(base: Path) -> Path:
    return base / RELEASED


def review_path(folder: Path) -> Path:
    return folder.parent / (folder.name + REVIEW_SUFFIX)


def owned_alone(path: Path) -> bool:
    """True when `path` is no link, belongs to this user and is writable by nobody else. The folders lie in the work
    user's tree: the mark and RELEASED.jsonl count only when the owner wrote them (K2 of the review of 2026-10-20)."""
    try:
        st = os.lstat(path)
    except OSError:
        return False
    return not stat.S_ISLNK(st.st_mode) and st.st_uid == os.geteuid() and not st.st_mode & 0o022


def released(base: Path) -> tuple[str, list[dict]]:
    """(date, facts) of the last release recorded beside the built folders; ("", []) before the first. ReviewError
    when the file is there but not written by this user alone: a session could hide what changed since."""
    path = released_path(base)
    if not path.is_file() and not path.is_symlink():
        return "", []
    if not owned_alone(path):
        raise ReviewError("%s beside the folders is not yours alone (a link, another owner or writable by others); "
                          "check it, then remove it or make it yours" % RELEASED)
    text = path.read_text(encoding="utf-8")
    first = text.splitlines()[0] if text.strip() else "{}"
    try:
        meta = json.loads(first)
    except ValueError:
        meta = {}
    day = str(meta.get("date") or "") if isinstance(meta, dict) else ""
    return day, read_facts(text)


def _write_text(path: Path, text: str) -> None:
    """`text` into `path` through a new file of mode 0644 that is never a link a session planted (O_EXCL|O_NOFOLLOW)."""
    tmp = path.with_name(".%s.new" % path.name)
    tmp.unlink(missing_ok=True)
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o644)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(text)
    os.chmod(tmp, 0o644)                                            # whatever the umask: writable by the owner alone
    os.replace(tmp, path)


def record_release(folder: Path) -> Path:
    """RELEASED.jsonl beside the folders: a copy of the facts.jsonl just released."""
    path = released_path(folder.parent)
    _write_text(path, (folder / "facts.jsonl").read_text(encoding="utf-8"))
    return path


def _fact_lines(word: str, r: dict) -> list[str]:
    return ["%s %s (%s, checked %s): %s" % (word, r["id"], r.get("grade", "-"), r.get("checked", "-"),
                                            r.get("statement", "")),
            "  source: %s" % r.get("source", "")]


def review_lines(folder: Path, since: str, added: list[dict], amended: list[tuple[dict, dict]],
                 retired: list[str]) -> list[str]:
    """What the owner reads: every fact added or amended since the release, with its source and grade, then the
    retired ids. Codes only: the facts are name checked by the export and tenant free by the build."""
    against = "the release of %s" % since if since else "no release yet (every fact is new)"
    lines = ["%s against %s: %d added, %d amended, %d retired" % (folder.name, against, len(added), len(amended),
                                                                   len(retired))]
    for r in added:
        lines += _fact_lines("added", r)
    for before, now in amended:
        what = ", ".join(f for f in REVIEW_FIELDS if now.get(f) != before.get(f))
        lines += _fact_lines("amended (%s)" % what, now)
        if now.get("statement") != before.get("statement"):
            lines.append("  statement before: %s" % before.get("statement", ""))
        if now.get("source") != before.get("source") or now.get("grade") != before.get("grade"):
            lines.append("  before: %s, source: %s" % (before.get("grade", "-"), before.get("source", "")))
    if retired:
        lines.append("retired: %s" % ", ".join(retired))
    return lines


def review(p: config.Paths, *, out: Path | None = None, day: str | None = None) -> list[str]:
    """The review of the newest built folder (or the one of `day`) against the last release, as lines to print,
    and its mark beside the folder: the sha256 of the facts reviewed and the release they were compared with.
    Refused for the work user: a session must not mark its own facts as read (K2 of the review of 2026-10-20)."""
    if config.is_work_user():
        raise ReviewError("awb kb review is the owner's; the work user may not write the mark of a review")
    folder = latest(p, out, day)
    if folder is None:
        raise ReviewError("no built dataset%s; run awb dataset build" % (" of that date" if day else ""))
    facts = folder / "facts.jsonl"
    if not facts.is_file():
        raise ReviewError("the folder of %s is not a complete build; run awb dataset build" % folder.name[len(SLUG) + 1:])
    since, previous = released(folder.parent)
    added, amended, retired = changes(previous, read_facts(facts), REVIEW_FIELDS)
    lines = review_lines(folder, since, added, amended, retired)
    mark = {"dataset": folder.name, "since": since, "facts_sha256": sha256_of(facts), "assets": asset_sums(folder),
            "added": len(added),
            "amended": len(amended), "retired": len(retired),
            "reviewed_at": datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d %H:%M UTC")}
    _write_text(review_path(folder), json.dumps(mark, indent=1, sort_keys=True) + "\n")
    lines.append("reviewed: awb dataset put may release %s while its facts.jsonl stays as it is" % folder.name)
    return lines


def require_review(folder: Path) -> dict:
    """The mark of `awb kb review` for this folder. ReviewError in one line that names the command when there is no mark,
    a mark of other facts or assets (the folder was built again) or of another release (one went out since)."""
    since, _ = released(folder.parent)
    path = review_path(folder)
    if not path.is_file():
        raise ReviewError("the facts of %s were not reviewed%s; %s"
                           % (folder.name, " since the release of %s" % since if since else "", REVIEW_HINT))
    if not owned_alone(path):
        raise ReviewError("the mark of %s is not yours alone (a link, another owner or writable by others); %s"
                          % (folder.name, REVIEW_HINT.replace("first", "again")))
    try:
        mark = json.loads(path.read_text(encoding="utf-8"))
        fresh = (mark["facts_sha256"] == sha256_of(folder / "facts.jsonl") and mark["since"] == since
                 and mark.get("dataset", folder.name) == folder.name and mark["assets"] == asset_sums(folder))
    except (ValueError, KeyError, TypeError, OSError):
        fresh = False
    if not fresh:
        raise ReviewError("the review of %s is not the review of its facts now against the last release; %s"
                           % (folder.name, REVIEW_HINT.replace("first", "again")))
    return mark
