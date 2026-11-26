"""Sweep and close the dated owner decisions in a project's OPEN.md.

A receipt is a complete `Default taken: X` line in a commit message or a cell in a
RESOURCES.md row starting with a handle. X must equal the item's default, ignoring
case, whitespace and its final period. Proposals and keyword overlaps are no proof.
Only the commit hash or resource handle and row number leave the evidence reader.
"""
from __future__ import annotations

from collections import Counter
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date
import fcntl
import os
import re
import stat
import subprocess
import sys
import unicodedata
from pathlib import Path

from awb import codes, config, ledger


class OpenError(Exception):
    """A refusal carrying no item text or filesystem path."""


_MAX_BYTES = 4 * 1024 * 1024


@contextmanager
def _directory(path: Path, *, create: bool = False):
    """Walk from the filesystem root without following any directory links."""
    fd = os.open("/", os.O_RDONLY | os.O_DIRECTORY)
    try:
        for part in Path(os.path.abspath(path)).parts[1:]:
            if create:
                try:
                    os.mkdir(part, mode=0o700, dir_fd=fd)
                except FileExistsError:
                    pass
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            os.close(fd)
            fd = child
        yield fd
    finally:
        os.close(fd)


@contextmanager
def _file(path: Path, *, write: bool = False, create: bool = False, lock: bool = True):
    """Open bounded regular data with exactly one link, anchored to its directory."""
    with _directory(path.parent, create=create) as parent:
        flags = os.O_RDWR if write else os.O_RDONLY
        fd = os.open(path.name, flags | os.O_NOFOLLOW | os.O_NONBLOCK |
                     (os.O_CREAT if create else 0), 0o600, dir_fd=parent)
        with os.fdopen(fd, "r+" if write else "r", encoding="utf-8", newline="") as stream:
            before = os.fstat(fd)
            if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1 or before.st_size > _MAX_BYTES:
                raise OpenError("project data must be a bounded regular file without links")
            if lock:
                fcntl.flock(stream, fcntl.LOCK_EX if write else fcntl.LOCK_SH)
            current = os.stat(path.name, dir_fd=parent, follow_symlinks=False)
            after = os.fstat(fd)
            if ((current.st_dev, current.st_ino) != (before.st_dev, before.st_ino) or
                    after.st_nlink != 1 or (write and not create and
                    (before.st_mtime_ns, before.st_ctime_ns, before.st_size) !=
                    (after.st_mtime_ns, after.st_ctime_ns, after.st_size))):
                raise OpenError("project data changed; run awb open sweep again")
            yield stream


def _read(stream) -> str:
    text = stream.read(_MAX_BYTES + 1)
    if len(text.encode("utf-8")) > _MAX_BYTES:
        raise OpenError("project data is too large")
    return text


def _read_file(path: Path) -> str:
    with _file(path) as stream:
        return _read(stream)


@dataclass(frozen=True)
class Item:
    line: int
    raw: str
    day: date | None
    text: str
    default: str


def today() -> date:
    return date.today()


def parse(text: str) -> list[Item]:
    """Open dash items naming the owner, or under an owner heading, in file order."""
    from awb.projects import CLOSED_HEADING

    result = []
    headings: list[tuple[int, str]] = []
    fence = ""
    for number, line in enumerate(text.splitlines(), 1):
        marker = re.match(r"^\s*(`{3,}|~{3,})(.*)$", line)
        if fence:
            if (marker and marker[1][0] == fence[0] and len(marker[1]) >= len(fence)
                    and not marker[2].strip()):
                fence = ""
            continue
        if marker:
            fence = marker[1]
            continue
        heading = re.match(r"^(#{1,6})\s+(.*)", line)
        if heading:
            level = len(heading[1])
            headings = [(n, h) for n, h in headings if n < level]
            headings.append((level, heading[2]))
            continue
        if not line.startswith("- ") or any(CLOSED_HEADING.search(h) for _, h in headings):
            continue
        body = line[2:].strip()
        if re.match(r"\[[xX]\]\s*", body):
            continue
        body = re.sub(r"^\[ \]\s*", "", body)
        if not (re.search(r"\bowner\b", body, re.I) or
                any(re.search(r"\bowner\b", h, re.I) for _, h in headings)):
            continue
        stamp = re.match(r"^(\d{4}-\d{2}-\d{2})\s+", body)
        day = None
        if stamp:
            try:
                day = date.fromisoformat(stamp[1])
            except ValueError:
                pass
            body = body[stamp.end():]
        body = re.sub(r"^owner:\s*", "", body, flags=re.I)
        parts = re.split(r"\bDefault:\s*", body, maxsplit=1, flags=re.I)
        result.append(Item(number, line, day, parts[0].strip(), parts[1].strip() if len(parts) > 1 else ""))
    return result


def undated(new: str, old: str) -> list[int]:
    """New undated owner lines; each unchanged legacy occurrence is exempt once."""
    legacy = Counter(item.raw for item in parse(old) if item.day is None)
    bad = []
    for item in parse(new):
        if item.day is not None:
            continue
        if legacy[item.raw]:
            legacy[item.raw] -= 1
        else:
            bad.append(item.line)
    return bad


def _key(text: str) -> str:
    return " ".join(text.split()).removesuffix(".").casefold()


def _receipt(text: str) -> str | None:
    match = re.fullmatch(r"Default taken:\s*(.+)", text.strip(), re.I)
    return _key(match[1]) if match else None


def evidence(root: Path) -> dict[str, str]:
    """Exact receipts mapped to safe locations, never raw commit or resource text."""
    found = {}
    try:
        if (root / ".git").is_symlink():
            raise OSError("linked repository metadata")
        env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
        env["GIT_CEILING_DIRECTORIES"] = str(root.parent.absolute())
        result = subprocess.run(["git", "--no-pager", "--no-replace-objects", "-C", str(root),
                                 "log", "--format=%H%x00%B%x00"],
                                capture_output=True, text=True, timeout=10, env=env,
                                stdin=subprocess.DEVNULL)
        if result.returncode == 0:
            fields = result.stdout.split("\0")
            for pos in range(0, len(fields) - 1, 2):
                commit, message = fields[pos].strip(), fields[pos + 1]
                if re.fullmatch(r"[0-9a-f]{40,64}", commit):
                    for line in message.splitlines():
                        key = _receipt(line)
                        if key:
                            found.setdefault(key, "commit " + commit[:12])
    except (OSError, UnicodeError, subprocess.SubprocessError):
        pass
    try:
        text = _read_file(root / "RESOURCES.md")
    except FileNotFoundError:
        text = ""
    for number, row in enumerate(text.splitlines(), 1):
        cells = [c.strip() for c in row.strip().strip("|").split("|")]
        if row.lstrip().startswith("|") and re.fullmatch(r"[a-z][a-z0-9]*-[0-9]+", cells[0]):
            for cell in cells[1:]:
                key = _receipt(cell)
                if key:
                    found.setdefault(key, "RESOURCES.md:%d %s" % (number, cells[0]))
    return found


def _code(root: Path) -> str:
    if not codes.is_project_code(root.name):
        raise OpenError("run inside a project with a project code and SCOPE.md")
    with _file(root / "SCOPE.md", lock=False):
        pass
    return root.name


def _cell(text: str) -> str:
    return " ".join(text.split()).replace("|", "\\|")


def sweep(root: Path) -> str:
    code = _code(root)
    items = parse(_read_file(root / "OPEN.md"))
    proof = evidence(root)
    rows = ["%s: %d open owner items" % (code, len(items)),
            "| N | age | text | default | evidence |", "|---|---|---|---|---|"]
    day = today()
    for n, item in enumerate(items, 1):
        age = "%dd" % (day - item.day).days if item.day else "unknown"
        text, default = item.text, item.default
        receipt = proof.get(_key(default), "")
        # shown as written (DMZ 4): only a control character is withheld, where a table line needs it
        if any(unicodedata.category(c) in ("Cc", "Cf") and c != "\t" for c in text + default):
            text, default, receipt = "[withheld]", "[withheld]", ""
        shown = "taken (%s)" % receipt if receipt else ""
        rows.append("| %d | %s | %s | %s | %s |" % (n, age, _cell(text), _cell(default), shown))
    return "\n".join(rows)


def close(root: Path, numbers: list[int]) -> int:
    code = _code(root)
    # Serialize closes against the same numbering, keeping the file's mode and owner.
    with _file(root / "OPEN.md", write=True) as stream:
        original = _read(stream)
        items = parse(original)
        chosen = sorted(set(numbers))
        if not chosen or any(n < 1 or n > len(items) for n in chosen):
            raise OpenError("item number out of range; run awb open sweep again")
        if any(not items[n - 1].default for n in chosen):
            raise OpenError("every selected item needs a Default: value")
        lines = original.splitlines(keepends=True)
        settled = []
        for n in chosen:
            pos = items[n - 1].line - 1
            settled.append(re.sub(r"^- (?:\[ \][ \t]*)?", "- [x] ", lines[pos], count=1).rstrip("\r\n"))
            lines[pos] = ""
        # Existing board and project-close readers exclude closed sections. Keep the
        # ticks there so every reader agrees that these items are settled.
        updated = "".join(lines).rstrip("\r\n") + "\n\n## Closed\n\n" + "\n".join(settled) + "\n"
        ledger.add("other", "closed owner items %s with their defaults" % ", ".join(map(str, chosen)),
                   project=code)
        stream.seek(0)
        stream.write(updated)
        stream.truncate()
    return len(chosen)


def reminder(root: Path, p: config.Paths | None = None) -> str | None:
    """Claim today's reminder under a lock shared by this project's sessions."""
    code = _code(root)
    try:
        count = len(parse(_read_file(root / "OPEN.md")))
    except FileNotFoundError:
        return None
    if count <= 20:
        return None
    marker = (p or config.paths()).ledger / ".open-sweep" / (code + ".day")
    stamp = today().isoformat()
    with _file(marker, write=True, create=True) as stream:
        if _read(stream).strip() == stamp:
            return None
        stream.seek(0)
        stream.truncate()
        stream.write(stamp)
    return ("%s: OPEN.md holds %d owner items; run awb open sweep and show the table once in the reply."
            % (code, count))


def main(argv: list[str] | None = None) -> int:
    from awb.cli import SafeParser
    from awb.toolguard import project_root

    class OpenParser(SafeParser):
        def error(self, message):
            # argparse's quoting is not a privacy boundary for hostile values.
            super().error("invalid arguments; see --help")

    parser = OpenParser(prog="awb open", description="Sweep and close owner decisions in this project.")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("sweep", help="list open owner items with ages, defaults and proven taken receipts")
    closer = sub.add_parser("close", help="tick numbered owner items and write one ledger entry")
    closer.add_argument("numbers", metavar="N", type=int, nargs="+", help="numbers from the current sweep")
    closer.add_argument("--default", action="store_true", required=True, help="close with the recorded defaults")
    try:
        args = parser.parse_args(argv)
        root = project_root(Path.cwd())
        if root is None:
            raise OpenError("run inside a project with SCOPE.md")
        if args.command == "sweep":
            print(sweep(root))
        else:
            count = close(root, args.numbers)
            print("%s: closed %d owner items with their defaults; one ledger entry" % (_code(root), count))
        return 0
    except SystemExit as exc:
        return int(exc.code)
    except OpenError as exc:
        print("awb open: %s" % exc, file=sys.stderr)
    except ledger.LedgerError:
        print("awb open: the ledger entry could not be written", file=sys.stderr)
    except (OSError, UnicodeError):
        print("awb open: project data could not be read or written", file=sys.stderr)
    return 2
