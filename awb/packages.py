"""The register of solution packages and the question of the stop hook (DESIGN.md 4.2, 5.2; Q-0019 S3 and S7).

    <shared>/packages.tsv   slug, kind, tags, state, version, released, project, counts, checked

One row per package of the solutions project, written by `awb solutions build|status|release|sweep` (awb/solutions.py),
codes, slugs, tags, dates and counts only. The board reads it for its packages line and the refresh writes it again
with the live status. It is never a list of customers: the project column is the solutions project.

The stop hook asks a source session once per batch: when the session wrote or changed a file under deliverables/
since the last check, the project's tags (SCOPE.md) meet the tags of a standing package and the session ran no
`awb kb pattern add` or `support` after its last change, it is stopped once with `MESSAGE`. The position in the
transcript is kept in `<shared>/sessions/contrib-<session id>.json`, as harvest keeps its own, so the same changes
never ask twice and the question never loops. The solutions project itself is never asked.
"""
from __future__ import annotations

import fcntl
import json
import os
import re
from pathlib import Path

from awb import codes, config

REGISTER = "packages.tsv"
COLUMNS = ("slug", "kind", "tags", "state", "version", "released", "project", "counts", "checked")
_SLUG_RE = re.compile(r"[a-z0-9][a-z0-9-]{1,47}")
_CELL_RE = re.compile(r"[a-z0-9 ,.=:;_-]{0,200}")
_SID_RE = re.compile(r"[^A-Za-z0-9_-]")
_WRITE_TOOLS = ("Write", "Edit", "MultiEdit", "NotebookEdit")
_PATTERN_RE = re.compile(r"\bawb\s+kb\s+pattern\s+(?:add|support)\b")
_TAGS_RE = re.compile(r"(?m)^\s*-\s*tags\s*:\s*(.*)$")
MAX_TRANSCRIPT = 64 * 1024 * 1024
MESSAGE = (
    "Pattern cards before you stop: this session changed %d deliverable(s) of a project whose tags meet the "
    "solution package(s) %s. Add the reusable pieces yourself as pattern cards, in this project: awb kb pattern add "
    "--package SLUG --kind KIND --tag TAG [--fact KB-XXXX] \"statement\" (a rule, a range, a step or a placeholder; "
    "no code, no total, no date, no name, no inventory line), or count a card that says it already with awb kb "
    "pattern support PAT-XXXX. When there is nothing reusable, end with one line that says so."
)


def register_file(p: config.Paths | None = None) -> Path:
    return (p or config.paths()).shared / REGISTER


def _clean(row: dict) -> dict | None:
    if not _SLUG_RE.fullmatch(row.get("slug", "")):
        return None
    if row.get("project") and not codes.is_project_code(row["project"]):
        return None
    out = {}
    for c in COLUMNS:
        v = str(row.get(c, "") or "")
        if c not in ("slug", "project") and not _CELL_RE.fullmatch(v):
            return None
        out[c] = v
    return out


def rows(p: config.Paths | None = None) -> list[dict]:
    """The rows of the register; a malformed row is left out, never shown."""
    try:
        text = register_file(p).read_text(encoding="utf-8")
    except (FileNotFoundError, UnicodeDecodeError):
        return []
    out = []
    for line in text.splitlines()[1:]:
        cells = line.split("\t")
        if len(cells) == len(COLUMNS):
            row = _clean(dict(zip(COLUMNS, cells)))
            if row:
                out.append(row)
    return out


def record(row: dict, p: config.Paths | None = None) -> None:
    """Write or replace the row of one package, under a lock, in one step."""
    clean = _clean(row)
    if clean is None:
        raise ValueError("a package row carries slugs, codes, tags, dates and counts only")
    path = register_file(p)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        keep = [r for r in rows(p) if r["slug"] != clean["slug"]] + [clean]
        keep.sort(key=lambda r: r["slug"])
        tmp = path.with_name(".%s.%d.tmp" % (path.name, os.getpid()))
        with open(tmp, "w", encoding="utf-8") as fh:
            fh.write("\t".join(COLUMNS) + "\n")
            fh.writelines("\t".join(r[c] for c in COLUMNS) + "\n" for r in keep)
        os.chmod(tmp, 0o640)
        os.replace(tmp, path)
    finally:
        os.close(fd)


def line(p: config.Paths | None = None) -> str:
    """The board's packages line: slug, version, state and the reasons that count, or that none is registered."""
    found = rows(p)
    if not found:
        return "packages: none registered"
    parts = []
    for r in found:
        counts = [c.replace("=", " ") for c in r["counts"].split(";") if c and not c.endswith("=0")]
        parts.append("%s %s %s%s" % (r["slug"], r["version"] or "-", r["state"] or "draft",
                                     " (%s)" % ", ".join(counts) if counts else ""))
    return "packages: " + ", ".join(parts)


# --------------------------------------------------------------------------- the stop hook question (5.2)


def _tags_of(root: Path) -> set[str]:
    try:
        text = (root / "SCOPE.md").read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return set()
    m = _TAGS_RE.search(text)
    return {t.strip().lower() for t in m.group(1).split(",") if t.strip()} if m else set()


def _events(transcript: Path, start: int) -> tuple[list[tuple[str, str]], int]:
    """(kind, value) of the session's tool calls from line `start`: ("write", path) and ("cmd", command)."""
    out: list[tuple[str, str]] = []
    n = 0
    try:
        if os.path.getsize(transcript) > MAX_TRANSCRIPT:
            return [], start
        with open(transcript, "r", encoding="utf-8", errors="replace") as fh:
            for n, raw in enumerate(fh, start=1):
                if n <= start:
                    continue
                try:
                    obj = json.loads(raw)
                except ValueError:
                    continue
                if not isinstance(obj, dict) or obj.get("type") != "assistant":
                    continue
                content = (obj.get("message") or {}).get("content")
                for c in content if isinstance(content, list) else []:
                    if not isinstance(c, dict) or c.get("type") != "tool_use":
                        continue
                    data = c.get("input") or {}
                    if c.get("name") in _WRITE_TOOLS and isinstance(data.get("file_path"), str):
                        out.append(("write", data["file_path"]))
                    elif isinstance(data.get("command"), str):
                        out.append(("cmd", data["command"]))
    except OSError:
        return [], start
    return out, max(n, start)


def _mark_path(p: config.Paths, session_id: str) -> Path:
    sid = _SID_RE.sub("", session_id or "")[:80] or "unknown"
    return p.shared / "sessions" / ("contrib-%s.json" % sid)


def _read_mark(path: Path) -> int:
    try:
        return int(json.loads(path.read_text(encoding="utf-8")).get("line", 0))
    except (OSError, ValueError, AttributeError, TypeError):
        return 0


def _write_mark(path: Path, n: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.is_symlink():
        return
    tmp = path.with_name(".%s.tmp" % path.name)
    tmp.write_text(json.dumps({"line": n}) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def due(p: config.Paths, root: Path | None, transcript, session_id: str | None) -> str | None:
    """The question of the stop hook, or None. Moves the mark either way."""
    if root is None or not transcript:
        return None
    code = root.name
    standing = [r for r in rows(p) if r["state"] != "retired" and r["project"] != code]
    tags = _tags_of(root)
    hit = sorted({r["slug"] for r in standing if tags & {t.strip() for t in r["tags"].split(",") if t.strip()}})
    path = _mark_path(p, session_id or "")
    mark = _read_mark(path)
    events, end = _events(Path(transcript), mark)
    if end < mark:
        events, end = _events(Path(transcript), 0)
    try:
        _write_mark(path, end)
    except OSError:
        pass
    if not hit:
        return None
    deliverables = str(root / "deliverables") + os.sep
    writes = [i for i, (k, v) in enumerate(events) if k == "write" and os.path.abspath(v).startswith(deliverables)]
    if not writes:
        return None
    if any(k == "cmd" and _PATTERN_RE.search(v) for k, v in events[writes[-1] + 1:]):
        return None
    changed = len({events[i][1] for i in writes})
    return MESSAGE % (changed, ", ".join(hit))
