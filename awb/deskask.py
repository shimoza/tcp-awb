"""UXR9: checked project requests to the desk and their answer notes, with the mechanics of `awb dev ask`.

    awb desk ask "TEXT" [--blocks]                        (a project session)
    awb desk list                                          (the desk)
    awb desk close HANDLE --note TEXT [--owner-word TEXT]  (the desk, as the work user on a sealed host)

A project session asks the desk for desk work (DNS records, Cloudflare, EIPs between projects, tenant steps) instead
of him copying text by hand. The ask is a file <shared>/notices/desk/<date>-<code>-<n>.md with the same header as a
dev ask (awb/devask.py): project, title, created, blocks, state. The prompt hook shows a new open ask once to each
desk session (an owner session outside a project and outside a checkout of this repository). `close` writes the note
into the asking project (notes/from-dev/, the writer of `awb projects note`, its title "desk answer: TITLE") and marks
the ask closed with it. `--owner-word` stamps the note with his words (awb/ownerword.py).
"""
from __future__ import annotations

import hashlib
import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

from awb import codes, config, devask

SUB = "desk"
FOLDER = "notices/desk"
TITLE = "desk answer: %s"
NEW_HOURS = devask.NEW_HOURS


def folder(p: config.Paths) -> Path:
    for path in (p.shared, p.shared / "notices", p.shared / FOLDER):
        if path.is_symlink():
            raise devask.AskError("the desk notices folder is a link")
    return p.shared / FOLDER


def write(p: config.Paths, text: str, blocks: bool = False) -> Path:
    from awb import hooks, projects

    root = hooks.project_root(Path.cwd())
    if root is None or not codes.is_project_code(root.name):
        raise devask.AskError("run awb desk ask in a registered project")
    pr = next((pr for pr in projects.load(p) if pr.code == root.name and
               Path(pr.path).resolve() == root.resolve() and pr.state in projects.LIVE_STATES), None)
    if pr is None:
        raise devask.AskError("run awb desk ask in an active or parked registered project")
    if not text.strip():
        raise devask.AskError("give the ask some text")
    devask.screen(p, "\n".join((pr.code, text)))
    title = " ".join(text.splitlines()[0].split())[:120] or "desk request"
    now = datetime.now(timezone.utc)
    data = dict(project=pr.code, title=title, created=now.isoformat(), blocks=blocks, state="open")
    body = "<!-- %s -->\n# %s\n\nProject: %s\nBlocks: %s\n\n## Text\n\n%s\n" % (
        json.dumps(data), title, pr.code, "yes" if blocks else "no", text)
    if len(body.encode("utf-8")) > devask.MAX_BYTES:
        raise devask.AskError("the ask is too large")
    target = folder(p)
    with devask._notices_directory(p, create=True, sub=SUB) as directory:
        with devask._staged(directory, body) as temp:
            for n in range(1, 10000):
                path = target / ("%s-%s-%d.md" % (now.date(), pr.code, n))
                try:
                    os.link(temp, path.name, src_dir_fd=directory, dst_dir_fd=directory, follow_symlinks=False)
                except FileExistsError:
                    continue
                return path
    raise devask.AskError("too many requests today")


def open_asks(p: config.Paths) -> list[tuple[Path, dict]]:
    out = []
    try:
        with devask._notices_directory(p, sub=SUB) as directory:
            for name in sorted(os.listdir(directory)):
                if not devask.FILE_RE.fullmatch(name):
                    continue
                path = p.shared / FOLDER / name
                try:
                    body = devask._bytes(Path(name), dir_fd=directory)
                    data = devask.read(path, text=body)
                    if data["state"] != "open":
                        continue
                    devask.screen(p, body + "\n" + json.dumps(data, ensure_ascii=False))
                except (devask.AskError, OSError, UnicodeError):
                    continue
                out.append((path, data))
    except (OSError, devask.AskError):
        return out
    return out


def close(p: config.Paths, handle: str, note: str, owner_word: str | None = None,
          now: datetime | None = None) -> tuple[Path, dict]:
    """Deliver the checked note into the asking project, then mark the ask closed. Returns the note and the header."""
    from awb import ownerword

    if not isinstance(handle, str) or not devask.HANDLE_RE.fullmatch(handle.removesuffix(".md")):
        raise devask.AskError("give the handle of a desk ask (its file name without .md)")
    if not isinstance(note, str) or not note.strip():
        raise devask.AskError("give the answer a --note")
    if owner_word is not None:
        try:
            note = ownerword.screened(p, note, owner_word, now)
        except ownerword.WordError as err:
            raise devask.AskError(str(err)) from None
    path = folder(p) / (handle.removesuffix(".md") + ".md")
    return devask._close(p, path, lambda d: TITLE % d["title"][:120 - len(TITLE % "")], note,
                         sub=SUB)


def notices(p: config.Paths, cwd: str, session_id: str | None) -> list[str]:
    """A desk session sees each open ask once, while it is new (NEW_HOURS)."""
    from awb import ux

    if config.is_work_user() or config.is_build_user() or not isinstance(session_id, str) or not session_id:
        return []
    try:
        if ux.session_kind(cwd)[0] != "desk":
            return []
        sid = hashlib.sha256(session_id.encode()).hexdigest()
    except (UnicodeError, OSError, ValueError):
        return []
    cutoff = datetime.now(timezone.utc) - timedelta(hours=NEW_HOURS)
    asks = [(path, data) for path, data in open_asks(p) if datetime.fromisoformat(data["created"]) >= cutoff]
    out: list[str] = []
    if not asks:
        return out
    try:
        with devask._notices_directory(p, sub=SUB) as directory:
            with devask._child_directory(directory, ".seen-" + sid, create=True, private=True) as marks:
                for path, data in asks:
                    try:
                        fd = os.open(path.name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                                     0o600, dir_fd=marks)
                    except FileExistsError:
                        continue
                    os.close(fd)
                    out.append("desk request from %s: %s%s (%s, awb desk close %s --note TEXT)" % (
                        data["project"], data["title"], " [blocks]" if data["blocks"] else "", FOLDER + "/" +
                        path.name, path.name[:-3]))
    except OSError:
        return out
    return out


def main(argv: list[str] | None = None) -> int:
    from awb import devnote, projects
    from awb.cli import SafeParser

    class AskParser(SafeParser):
        def error(self, message):
            super().error("invalid arguments")

    ap = AskParser(prog="awb desk", description="Requests from a project to the desk.")
    sub = ap.add_subparsers(dest="cmd", required=True)
    ask = sub.add_parser("ask", help="write a checked request for desk work from the current project")
    ask.add_argument("text", metavar="TEXT")
    ask.add_argument("--blocks", action="store_true", help="mark the request as blocking the project task")
    sub.add_parser("list", help="the open desk asks: handle, project, blocks, title")
    close_p = sub.add_parser("close", help="answer a desk ask with a note into its project and close it (run it as "
                                           "the work user)")
    close_p.add_argument("handle", metavar="HANDLE", help="the ask file name without .md")
    close_p.add_argument("--note", required=True, metavar="TEXT", help="the answer, - for standard input")
    close_p.add_argument("--owner-word", metavar="TEXT", help="his words: the note carries the owner's word stamp")
    args = ap.parse_args(argv)
    p = config.paths()
    if args.cmd == "list":
        asks = open_asks(p)
        if not asks:
            print("no open desk ask")
        for path, data in asks:
            print("%s  %s  %s%s" % (path.name[:-3], data["project"], data["title"],
                                     "  [blocks]" if data["blocks"] else ""))
        return 0
    if args.cmd == "close":
        text = sys.stdin.read() if args.note == "-" else args.note
        try:
            note, data = close(p, args.handle, text, args.owner_word)
        except (devask.AskError, devnote.NoteError, projects.ProjectError, OSError) as exc:
            reason = str(exc) if isinstance(exc, (devask.AskError, devnote.NoteError, projects.ProjectError)) \
                else type(exc).__name__
            print("awb desk close: %s" % reason, file=sys.stderr)
            return 2
        print("%s: desk ask closed, note %s/%s; its next session prompt shows it" % (
            data["project"], devnote.FOLDER, note.name))
        return 0
    try:
        path = write(p, args.text, args.blocks)
    except (devask.AskError, projects.ProjectError, OSError) as exc:
        reason = str(exc) if isinstance(exc, devask.AskError) else type(exc).__name__
        print("awb desk ask: %s" % reason, file=sys.stderr)
        return 2
    print("%s/%s written; the desk will see it at its next prompt" % (FOLDER, path.name))
    return 0
