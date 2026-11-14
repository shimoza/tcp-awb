"""UX4: checked project requests to development and their answer notes."""
from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

from awb import codes, config

FOLDER = "notices/dev"
MAX_BYTES = 128 * 1024
FILE_RE = re.compile(r"\d{4}-\d{2}-\d{2}-" + codes.PROJECT + r"-[1-9][0-9]*\.md")


class AskError(Exception):
    """A safe refusal with no argument values."""


def screen(p: config.Paths, text: str) -> None:
    from awb import check, ledger

    if any((ord(c) < 32 and c not in "\n\r\t") or ord(c) == 127 for c in text):
        raise AskError("the text carries a control character")
    if check._secret_classes(text):
        raise AskError("a secret was found; nothing was written")
    try:
        ledger.ensure_checkable(p.register)
        hits = ledger.screen(text, p.register, candidates=False)
    except ledger.LedgerError:
        raise AskError("the name check is unavailable; nothing was written") from None
    if hits:
        raise AskError("the name check or secret check found a hit; nothing was written")


def folder(p: config.Paths) -> Path:
    for path in (p.shared, p.shared / "notices", p.shared / FOLDER):
        if path.is_symlink():
            raise AskError("the development notices folder is a link")
    return p.shared / FOLDER


def _bytes(path: Path) -> str:
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, "rb") as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_BYTES:
            raise AskError("the ask is not a bounded regular file")
        return stream.read(MAX_BYTES + 1).decode("utf-8")


def read(path: Path, *, text: str | None = None) -> dict:
    """Read the metadata of a request without trusting its content or path for display."""
    if not FILE_RE.fullmatch(path.name):
        raise AskError("give a development ask file")
    try:
        line = (_bytes(path) if text is None else text).splitlines()[0]
        if not line.startswith("<!-- ") or not line.endswith(" -->"):
            raise ValueError
        data = json.loads(line[5:-4])
        if not isinstance(data, dict) or not codes.is_project_code(data.get("project", "")):
            raise ValueError
        if not path.name[11:].startswith(data["project"] + "-"):
            raise ValueError
        if data.get("state") not in ("open", "closed") or not isinstance(data.get("blocks"), bool):
            raise ValueError
        if not isinstance(data.get("title"), str) or not data["title"] or len(data["title"]) > 120:
            raise ValueError
        if any(ord(c) < 32 or ord(c) == 127 for c in data["title"]):
            raise ValueError
        created = datetime.fromisoformat(data["created"])
        if created.utcoffset() is None:
            raise ValueError
    except (OSError, UnicodeError, ValueError, KeyError, TypeError, IndexError):
        raise AskError("the development ask cannot be read") from None
    return data


def write(p: config.Paths, text: str, blocks: bool = False, command: str = "not supplied",
          error: str = "not supplied") -> Path:
    from awb import hooks, projects

    root = hooks.project_root(Path.cwd())
    if root is None or not codes.is_project_code(root.name):
        raise AskError("run awb dev ask in a registered project")
    pr = next((pr for pr in projects.load(p) if pr.code == root.name and
               Path(pr.path).resolve() == root.resolve() and pr.state in projects.LIVE_STATES), None)
    if pr is None:
        raise AskError("run awb dev ask in an active or parked registered project")
    if not text.strip():
        raise AskError("give the ask some text")
    screen(p, "\n".join((pr.code, text, command, error)))
    title = " ".join(text.splitlines()[0].split())[:120] or "development request"
    now = datetime.now(timezone.utc)
    data = dict(project=pr.code, title=title, created=now.isoformat(), blocks=blocks, state="open")
    body = ("<!-- %s -->\n# %s\n\nProject: %s\nBlocks: %s\n\n## Text\n\n%s\n\n## Command\n\n%s\n\n"
            "## Error\n\n%s\n") % (json.dumps(data), title, pr.code, "yes" if blocks else "no", text, command, error)
    if len(body.encode("utf-8")) > MAX_BYTES:
        raise AskError("the ask is too large")
    target = folder(p)
    config.make_dir(target.parent, 0o750, shared=True)
    config.make_dir(target, 0o750, shared=True)
    for n in range(1, 10000):
        path = target / ("%s-%s-%d.md" % (now.date(), pr.code, n))
        try:
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o640)
        except FileExistsError:
            continue
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            os.fchmod(stream.fileno(), 0o640)
            stream.write(body)
        return path
    raise AskError("too many requests today")


def open_asks(p: config.Paths) -> list[tuple[Path, dict]]:
    out = []
    try:
        paths = sorted(folder(p).glob("*.md"))
    except (OSError, AskError):
        return out
    for path in paths:
        try:
            body = _bytes(path)
            data = read(path, text=body)
            if data["state"] != "open":
                continue
            screen(p, body + "\n" + data["title"])
        except (AskError, OSError, UnicodeError):
            continue
        out.append((path, data))
    return out


def answer(p: config.Paths, code: str, file: str, title: str, text: str) -> Path:
    """Deliver the checked note first; only a successfully delivered note closes the ask."""
    from awb import devnote

    if not codes.is_project_code(code):
        raise AskError("give a project code")
    target = folder(p)
    path = Path(file)
    if path.parent == Path("."):
        path = target / path.name
    elif path.parent == Path(FOLDER):
        path = p.shared / path
    if path.parent.resolve() != target.resolve() or path.is_symlink():
        raise AskError("the answer must name a development ask file")
    data = read(path)
    if data["project"] != code or data["state"] != "open":
        raise AskError("the ask is closed or belongs to another project")
    screen(p, title + "\n" + text)
    body = _bytes(path).split("\n", 1)[1]
    note = devnote.write(p, code, title, text)
    data.update(state="closed", answer=devnote.FOLDER + "/" + note.name,
                answered=datetime.now(timezone.utc).isoformat())
    fd, temp = tempfile.mkstemp(prefix=".answer-", dir=target)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            os.fchmod(stream.fileno(), 0o640)
            stream.write("<!-- %s -->\n%s" % (json.dumps(data), body))
        os.replace(temp, path)
    finally:
        Path(temp).unlink(missing_ok=True)
    return note


def notices(p: config.Paths, cwd: str, session_id: str | None) -> list[str]:
    """An owner session in a repository checkout sees each open ask once."""
    if config.is_work_user() or config.is_build_user() or not isinstance(session_id, str) or not session_id:
        return []
    root = Path(cwd).resolve()
    if not any((r / ".git").exists() and (r / "awb/cli.py").is_file() and
               (r / "seal/work-claude/CLAUDE.md").is_file() for r in (root, *root.parents)):
        return []
    sid = hashlib.sha256(session_id.encode()).hexdigest()
    # The owner's group may write notices, but not the work user's sessions directory.
    marks = folder(p) / (".seen-" + sid)
    if marks.parent.is_symlink() or marks.is_symlink():
        return []
    out = []
    for path, data in open_asks(p):
        config.make_dir(marks.parent, 0o750, shared=True)
        config.make_dir(marks, 0o700)
        try:
            fd = os.open(marks / path.name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        except FileExistsError:
            continue
        os.close(fd)
        out.append("request from %s: %s (%s/%s)" % (data["project"], data["title"], FOLDER, path.name))
    return out


def status_items(p: config.Paths, view: str) -> list:
    from awb.ownerwait import Item, WHERE_STEERING

    asks = open_asks(p)
    if view in ("dev", "all"):
        return [Item(data["project"], "%s%s (%s/%s)" %
                     (data["title"], " [blocks]" if data["blocks"] else "", FOLDER, path.name),
                     WHERE_STEERING, kind="dev") for path, data in asks]
    cutoff = datetime.now(timezone.utc) - timedelta(hours=24)
    overdue = sorted({data["project"] for _, data in asks if datetime.fromisoformat(data["created"]) < cutoff})
    return [Item(code, "development owes %s an answer" % code, "", kind="dev-overdue") for code in overdue]


def main(argv: list[str] | None = None) -> int:
    from awb import projects
    from awb.cli import SafeParser

    ap = SafeParser(prog="awb dev", description="Requests from a project to development.")
    sub = ap.add_subparsers(dest="cmd", required=True)
    ask = sub.add_parser("ask", help="write a checked request from the current project")
    ask.add_argument("text", metavar="TEXT")
    ask.add_argument("--blocks", action="store_true", help="mark the request as blocking the project task")
    ask.add_argument("--command", default="not supplied", help="the command that failed, recorded without running it")
    ask.add_argument("--error", default="not supplied", help="the error text of that command")
    args = ap.parse_args(argv)
    try:
        path = write(config.paths(), args.text, args.blocks, args.command, args.error)
    except (AskError, projects.ProjectError, OSError) as exc:
        reason = str(exc) if isinstance(exc, AskError) else type(exc).__name__
        print("awb dev ask: %s" % reason, file=sys.stderr)
        return 2
    print("%s/%s written; development will see it at its next prompt" % (FOLDER, path.name))
    return 0
