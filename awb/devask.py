"""UX4: checked project requests to development and their answer notes."""
from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import secrets
import stat
import sys
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

from awb import codes, config, events

FOLDER = "notices/dev"
MAX_BYTES = 128 * 1024
FILE_RE = re.compile(r"\d{4}-\d{2}-\d{2}-" + codes.PROJECT + r"-[1-9][0-9]*\.md")
HANDLE_RE = re.compile(r"(?<![0-9A-Za-z])(\d{4}-\d{2}-\d{2}-" + codes.PROJECT + r"-[1-9][0-9]{0,3})(?![0-9A-Za-z])")
"""The handle of an ask: its file name without `.md` (2026-11-19-tcp-q7m4-5). A commit that names it closes the ask
when the deploy that makes the commit live ends well (Q-0007)."""
NEW_HOURS = 24
"""The prompt hook shows an ask to an owner session only while it is new, the first hours after it was written; an
older open ask stays in `awb owner status`, the hand-over and the console (Q-0007)."""


class AskError(Exception):
    """A safe refusal with no argument values."""


class AskClosed(AskError):
    """The ask is closed already or belongs to another project."""


def screen(p: config.Paths, text: str) -> None:
    from awb import check, ledger

    try:
        text.encode("utf-8")
    except UnicodeError:
        raise AskError("the text is not valid UTF-8") from None
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


@contextmanager
def _directory(path: Path):
    """Anchor every component before using it, including ancestors of the shared root."""
    path = Path(os.path.abspath(path))
    fd = os.open(path.anchor, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        for part in path.parts[1:]:
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            os.close(fd)
            fd = child
        yield fd
    finally:
        os.close(fd)


@contextmanager
def _child_directory(parent: int, name: str, *, create: bool = False, private: bool = False):
    created = False
    if create:
        try:
            os.mkdir(name, 0o700 if private else 0o750, dir_fd=parent)
            created = True
        except FileExistsError:
            pass
    fd = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent)
    try:
        if created or private:
            inherited = os.fstat(parent).st_mode
            mode = 0o700 if private else (stat.S_ISGID | (inherited & 0o770)
                                         if inherited & stat.S_ISGID else 0o750)
            os.fchmod(fd, mode)
        yield fd
    finally:
        os.close(fd)


@contextmanager
def _notices_directory(p: config.Paths, *, create: bool = False, sub: str = "dev"):
    """`sub` is the folder of the asks under <shared>/notices: dev here, desk for awb.deskask (UXR9)."""
    with _directory(p.shared) as shared:
        with _child_directory(shared, "notices", create=create) as notices_fd:
            with _child_directory(notices_fd, sub, create=create) as fd:
                yield fd


@contextmanager
def _staged(directory: int, body: str):
    """Only complete files become visible requests; failures leave no partial ask."""
    name = ".ask-" + secrets.token_hex(16)
    fd = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o640, dir_fd=directory)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            os.fchmod(stream.fileno(), 0o640)
            stream.write(body)
        yield name
    finally:
        try:
            os.unlink(name, dir_fd=directory)
        except FileNotFoundError:
            pass


def _bytes(path: Path, *, dir_fd: int | None = None) -> str:
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=dir_fd)
    with os.fdopen(fd, "rb") as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_BYTES:
            raise AskError("the ask is not a bounded regular file")
        body = stream.read(MAX_BYTES + 1)
        if len(body) > MAX_BYTES:
            raise AskError("the ask is not a bounded regular file")
        return body.decode("utf-8")


def _unique_keys(pairs: list) -> dict:
    data = {}
    for key, value in pairs:
        if key in data:
            raise ValueError
        data[key] = value
    return data


def read(path: Path, *, text: str | None = None) -> dict:
    """Read the metadata of a request without trusting its content or path for display."""
    if not FILE_RE.fullmatch(path.name):
        raise AskError("give a development ask file")
    try:
        content = _bytes(path) if text is None else text
        if len(content.encode("utf-8")) > MAX_BYTES:
            raise ValueError
        line = content.splitlines()[0]
        if not line.startswith("<!-- ") or not line.endswith(" -->"):
            raise ValueError
        data = json.loads(line[5:-4], object_pairs_hook=_unique_keys)
        # Escaped lone surrogates otherwise survive parsing and break later output.
        json.dumps(data, ensure_ascii=False).encode("utf-8")
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
    except (OSError, UnicodeError, ValueError, KeyError, TypeError, IndexError, RecursionError):
        raise AskError("the development ask cannot be read") from None
    return data


def _main_folder(root: Path) -> Path | None:
    """The project folder an ask is written for: `root` itself or for a side session's worktree
    (`<project>/.side/NAME`, awb/side.py) the main folder of its project only when the worktree's git link
    leads into that project's own .git (receipt._side_git); None for a .side folder that is no such worktree."""
    from awb import receipt, side

    if root.parent.name != side.SIDE_DIR or not codes.is_project_code(root.parent.parent.name):
        return root
    git = root / ".git"
    if git.is_symlink() or not git.is_file() or receipt._side_git(root, git) is None:
        return None
    return root.parent.parent


def _here(p: config.Paths, verb: str):
    """The registered project of the working folder (a side worktree counts as its project); AskError otherwise."""
    from awb import hooks, projects

    root = hooks.project_root(Path.cwd())
    if root is not None:
        root = _main_folder(root)
    if root is None or not codes.is_project_code(root.name):
        raise AskError("run awb dev %s in a registered project" % verb)
    pr = next((pr for pr in projects.load(p) if pr.code == root.name and
               Path(pr.path).resolve() == root.resolve() and pr.state in projects.LIVE_STATES), None)
    if pr is None:
        raise AskError("run awb dev %s in an active or parked registered project" % verb)
    return pr


def write(p: config.Paths, text: str, blocks: bool = False, command: str = "not supplied",
          error: str = "not supplied") -> Path:
    pr = _here(p, "ask")
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
    with _notices_directory(p, create=True) as directory:
        with _staged(directory, body) as temp:
            for n in range(1, 10000):
                path = target / ("%s-%s-%d.md" % (now.date(), pr.code, n))
                try:
                    os.link(temp, path.name, src_dir_fd=directory, dst_dir_fd=directory,
                            follow_symlinks=False)
                except FileExistsError:
                    continue
                events.start("ask", path.name[:-3], project=pr.code, board=p.shared / "build", now=now)
                return path
    raise AskError("too many requests today")


def open_asks(p: config.Paths) -> list[tuple[Path, dict]]:
    out = []
    try:
        with _notices_directory(p) as directory:
            for name in sorted(os.listdir(directory)):
                if not FILE_RE.fullmatch(name):
                    continue
                path = p.shared / FOLDER / name
                try:
                    body = _bytes(Path(name), dir_fd=directory)
                    data = read(path, text=body)
                    if data["state"] != "open":
                        continue
                    screen(p, body + "\n" + json.dumps(data, ensure_ascii=False))
                except (AskError, OSError, UnicodeError):
                    continue
                out.append((path, data))
    except (OSError, AskError):
        return out
    return out


def project_asks(p: config.Paths, code: str, closed: bool = False) -> list[tuple[str, dict]]:
    """(handle, header) of the asks of one project, oldest first; open ones only unless `closed` (tcp-bytg ask 9 of
    2026-11-21: a session saw an answer only as a new note and could not list its open asks). Only the header is
    read; an unreadable ask is left out."""
    out = []
    try:
        with _notices_directory(p) as directory:
            for name in sorted(os.listdir(directory)):
                if not FILE_RE.fullmatch(name) or not name[11:].startswith(code + "-"):
                    continue
                try:
                    data = read(p.shared / FOLDER / name, text=_bytes(Path(name), dir_fd=directory))
                except (AskError, OSError, UnicodeError):
                    continue
                if data["project"] == code and (closed or data["state"] == "open"):
                    out.append((name[:-3], data))
    except (OSError, AskError):
        return out
    return sorted(out, key=lambda hd: (hd[1]["created"], hd[0]))


def status_lines(asks: list[tuple[str, dict]]) -> list[str]:
    lines = []
    for handle, d in asks:
        state = d["state"] + (" blocks" if d.get("blocks") and d["state"] == "open" else "")
        line = "%-28s %-12s %s" % (handle, state, d["title"])
        if d["state"] == "closed":
            line += "  (answer %s%s)" % (d.get("answer", "-"),
                                         ", release %s" % str(d["release"])[:7] if d.get("release") else "")
        lines.append(line)
    return lines


def answer(p: config.Paths, code: str, file: str, title: str, text: str) -> Path:
    """Deliver the checked note first; only a successfully delivered note closes the ask."""
    if not codes.is_project_code(code):
        raise AskError("give a project code")
    target = folder(p)
    path = Path(file)
    if path.parent == Path("."):
        path = target / path.name
    elif path.parent == Path(FOLDER):
        path = p.shared / path
    if Path(os.path.abspath(path.parent)) != Path(os.path.abspath(target)) or not FILE_RE.fullmatch(path.name):
        raise AskError("the answer must name a development ask file")
    return _close(p, path, title, text, code=code)[0]


def _close(p: config.Paths, path: Path, title, text, *, code: str | None = None,
           extra: dict | None = None, sub: str = "dev") -> tuple[Path, dict]:
    """Write the note into the asking project, then mark the ask closed with the note (and `extra`). `title` and
    `text` may be functions of the ask's header. Returns the note and the header. `sub` dev or desk (UXR9): only a
    dev ask has a line in the events file."""
    from awb import devnote

    try:
        with _notices_directory(p, sub=sub) as directory:
            # Lock the directory, not the request inode which closing replaces.
            fcntl.flock(directory, fcntl.LOCK_EX)
            content = _bytes(Path(path.name), dir_fd=directory)
            data = read(path, text=content)
            if (code is not None and data["project"] != code) or data["state"] != "open":
                raise AskClosed("the ask is closed or belongs to another project")
            title = title(data) if callable(title) else title
            text = text(data) if callable(text) else text
            screen(p, content + "\n" + json.dumps(data, ensure_ascii=False))
            screen(p, title + "\n" + text)
            body = content.partition("\n")[2]
            note = devnote.write(p, data["project"], title, text)
            data.update(state="closed", answer=devnote.FOLDER + "/" + note.name,
                        answered=datetime.now(timezone.utc).isoformat(), **(extra or {}))
            with _staged(directory, "<!-- %s -->\n%s" % (json.dumps(data), body)) as temp:
                os.replace(temp, path.name, src_dir_fd=directory, dst_dir_fd=directory)
    except (OSError, UnicodeError, ValueError):
        raise AskError("the development answer cannot be stored") from None
    if sub == "dev":
        events.end("ask", path.name[:-3], "ok", project=data["project"], board=p.shared / "build")
    return note, data


def handles_in(text: str) -> set[str]:
    """The ask handles a commit message names."""
    return set(HANDLE_RE.findall(text or ""))


def open_handles(p: config.Paths) -> set[str]:
    """The handles of the open asks, from their headers alone: nothing of their text leaves (the deploy plan)."""
    out = set()
    try:
        with _notices_directory(p) as directory:
            for name in os.listdir(directory):
                if not FILE_RE.fullmatch(name):
                    continue
                try:
                    data = read(p.shared / FOLDER / name, text=_bytes(Path(name), dir_fd=directory))
                except (AskError, OSError, UnicodeError):
                    continue
                if data["state"] == "open":
                    out.add(name[:-3])
    except (OSError, AskError):
        return set()
    return out


def released(p: config.Paths, handles, release: str) -> tuple[list[str], bool]:
    """Close each open ask a released commit names: the note "live in release X" into the asking project, then the
    state `closed` with the release (Q-0007). An ask that is closed already is passed over. Returns the lines and
    whether every open ask named was closed."""
    from awb import devnote

    if not isinstance(release, str) or not re.fullmatch(r"[0-9a-f]{40}", release):
        raise AskError("give the full commit id of the release")
    short = release[:7]
    lines, ok = [], True
    for handle in sorted(set(handles)):
        if not HANDLE_RE.fullmatch(handle):
            lines.append("not a development ask handle: skipped")
            ok = False
            continue
        path = folder(p) / (handle + ".md")
        try:
            note, data = _close(
                p, path, "live in release %s" % short,
                lambda d: "Your development ask %s/%s (%s) is live in release %s. Check it in this project and "
                          "close the item." % (FOLDER, path.name, d["title"], short),
                extra=dict(release=release))
        except AskClosed:
            continue
        except (AskError, devnote.NoteError) as err:
            lines.append("%s: not closed (%s)" % (handle, err))
            ok = False
            continue
        lines.append("%s: development ask %s live in release %s, note %s/%s" % (
            data["project"], handle, short, devnote.FOLDER, note.name))
    return lines, ok


def notices(p: config.Paths, cwd: str, session_id: str | None) -> list[str]:
    """An owner session in a repository checkout sees each open ask once, while it is new (NEW_HOURS)."""
    if config.is_work_user() or config.is_build_user() or not isinstance(session_id, str) or not session_id:
        return []
    root = Path(cwd).resolve()
    if not any((r / ".git").exists() and (r / "awb/cli.py").is_file() and
               (r / "seal/work-claude/CLAUDE.md").is_file() for r in (root, *root.parents)):
        return []
    try:
        sid = hashlib.sha256(session_id.encode()).hexdigest()
    except UnicodeError:
        return []
    out = []
    cutoff = datetime.now(timezone.utc) - timedelta(hours=NEW_HOURS)
    asks = [(path, data) for path, data in open_asks(p) if datetime.fromisoformat(data["created"]) >= cutoff]
    if not asks:
        return out
    # The owner's group may write notices, but not the work user's sessions directory.
    try:
        with _notices_directory(p) as directory:
            with _child_directory(directory, ".seen-" + sid, create=True, private=True) as marks:
                for path, data in asks:
                    try:
                        fd = os.open(path.name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                                     0o600, dir_fd=marks)
                    except FileExistsError:
                        continue
                    os.close(fd)
                    out.append("request from %s: %s (%s/%s)" %
                               (data["project"], data["title"], FOLDER, path.name))
    except OSError:
        return out
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

    class AskParser(SafeParser):
        def error(self, message):
            # argparse's repr quoting can defeat value masking when an argument contains quotes.
            super().error("invalid arguments")

    ap = AskParser(prog="awb dev", description="Requests from a project to development.")
    sub = ap.add_subparsers(dest="cmd", required=True)
    ask = sub.add_parser("ask", help="write a checked request from the current project")
    ask.add_argument("text", metavar="TEXT")
    ask.add_argument("--blocks", action="store_true", help="mark the request as blocking the project task")
    ask.add_argument("--command", default="not supplied", help="the command that failed, recorded without running it")
    ask.add_argument("--error", default="not supplied", help="the error text of that command")
    st = sub.add_parser("status", help="the open asks of the current project (--all: the answered ones too)")
    st.add_argument("--all", action="store_true", help="also the closed asks with their answer note")
    close = sub.add_parser("close", help="close the open asks a released commit names (the deploy runs it as the "
                                         "work user)")
    close.add_argument("--release", required=True, metavar="COMMIT", help="the full commit id of the live release")
    close.add_argument("handles", nargs="+", metavar="HANDLE", help="an ask file name without .md")
    args = ap.parse_args(argv)
    if args.cmd == "status":
        p = config.paths()
        try:
            pr = _here(p, "status")
        except (AskError, projects.ProjectError, OSError) as exc:
            print("awb dev status: %s" % (exc if isinstance(exc, AskError) else type(exc).__name__), file=sys.stderr)
            return 2
        asks = project_asks(p, pr.code, closed=args.all)
        for line in status_lines(asks):
            print(line)
        n_open = sum(1 for _, d in asks if d["state"] == "open")
        print("%s: %d open ask(s)%s" % (pr.code, n_open, "" if args.all else "; --all lists the answered ones too"))
        return 0
    if args.cmd == "close":
        try:
            lines, ok = released(config.paths(), args.handles, args.release)
        except (AskError, OSError) as exc:
            print("awb dev close: %s" % (exc if isinstance(exc, AskError) else type(exc).__name__), file=sys.stderr)
            return 2
        for line in lines:
            print(line)
        return 0 if ok else 2
    try:
        path = write(config.paths(), args.text, args.blocks, args.command, args.error)
    except (AskError, projects.ProjectError, OSError) as exc:
        reason = str(exc) if isinstance(exc, AskError) else type(exc).__name__
        print("awb dev ask: %s" % reason, file=sys.stderr)
        return 2
    print("%s/%s written; development will see it at its next prompt" % (FOLDER, path.name))
    return 0
