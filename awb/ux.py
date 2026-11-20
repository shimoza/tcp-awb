"""Counted frictions of the sessions (Q-0024, section 3 of the UX review of 2026-11-19).

The prompt, stop and pre-compact hooks append one line per event to `<shared>/ux/events.tsv`:

    time (UTC)  user kind (owner|work)  session kind (project|development|desk)  project code or -  class  text

The class of a prompt comes from the phrase list `rules/ux-phrases.tsv`, matched on the prompt text in memory only. A
counted line never carries a word of the prompt (its text field is "-") and calls nothing: no vault, no name check. A
prompt that matches no phrase writes no line. The stop hook writes `held-draft` or `held-note` when it holds a reply,
the pre-compact hook `compaction`. `awb ux note TEXT` adds a hand-written line of class `note`; its text passes the
name and secret checks once, as a dev ask does. `awb ux report` counts the classes per ISO week and the owner status
shows five lines of the week before on a Monday.

The folder is written by both sides: group writable with the setgid bit (2775), the file 664, every line one append
under an exclusive lock.
"""
from __future__ import annotations

import datetime as dt
import fcntl
import os
import re
import stat
import sys
from pathlib import Path

from awb import codes, config

FOLDER = "ux"
EVENTS_FILE = "events.tsv"
FOLDER_MODE = 0o2775
FILE_MODE = 0o664
PHRASES = Path(__file__).resolve().parent.parent / "rules" / "ux-phrases.tsv"
WHOLE_CLASSES = ("bare-yes",)
"""Classes whose phrase must be the whole prompt (spaces and the marks of STRIP around it aside)."""
STRIP = " \t\r\n.!,?;:"
NOTE_CLASS = "note"
HOOK_CLASSES = ("held-draft", "held-note", "compaction")
CLASS_RE = re.compile(r"[a-z][a-z-]{0,39}")
NOTE_CHARS = 300
MAX_READ = 8 * 1024 * 1024
WEEKS = 4
"""The weeks `awb ux report` shows without --since."""
OWNER_LINES = 5


class UxError(Exception):
    """A refusal with one line and no argument value."""


def now() -> dt.datetime:
    """The clock of this module (the tests set it)."""
    return dt.datetime.now(dt.timezone.utc)


# --------------------------------------------------------------------------- the phrase list


def load_phrases(path: Path | None = None) -> list[tuple[str, str]]:
    """(class, phrase) in file order; comment and blank lines and a line of another shape are left out. A file that
    cannot be read gives an empty list (the hook then writes nothing)."""
    try:
        text = Path(path or PHRASES).read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return []
    out = []
    for line in text.splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        parts = line.split("\t")
        if len(parts) != 2:
            continue
        cls, phrase = parts[0].strip(), parts[1].strip().lower()
        if CLASS_RE.fullmatch(cls) and phrase and cls not in HOOK_CLASSES + (NOTE_CLASS,):
            out.append((cls, phrase))
    return out


def _phrase_re(phrase: str) -> re.Pattern:
    """The phrase as whole words: a word boundary only on a side where the phrase starts or ends with a word sign."""
    left = r"(?<!\w)" if re.match(r"\w", phrase) else ""
    right = r"(?!\w)" if re.search(r"\w$", phrase) else ""
    return re.compile(left + re.escape(phrase) + right)


def classify(text, phrases: list[tuple[str, str]] | None = None) -> str | None:
    """The class of a prompt, None for one that matches nothing or is a notification of the client."""
    if not isinstance(text, str) or not text.strip():
        return None
    from awb import hooks

    if hooks.is_notification(text):
        return None
    low = text.lower()
    whole = low.strip(STRIP)
    for cls, phrase in (load_phrases() if phrases is None else phrases):
        if cls in WHOLE_CLASSES:
            if whole == phrase:
                return cls
        elif _phrase_re(phrase).search(low):
            return cls
    return None


# --------------------------------------------------------------------------- where the session stands


def user_kind() -> str:
    return "work" if config.is_work_user() or config.is_build_user() else "owner"


def session_kind(folder) -> tuple[str, str]:
    """(kind, project code or -): a folder inside a project is "project" with its code, a checkout of this
    repository "development", any other folder "desk"."""
    if not folder:
        return "desk", "-"
    from awb import toolguard

    try:
        root = toolguard.project_root(folder)
    except Exception:
        root = None
    if root is not None:
        for d in (root, *root.parents):
            if codes.is_project_code(d.name):
                return "project", d.name
        return "project", "-"
    try:
        here = Path(os.path.realpath(folder))
    except (OSError, ValueError):
        return "desk", "-"
    if any((d / ".git").exists() and (d / "awb" / "cli.py").is_file() for d in (here, *here.parents)):
        return "development", "-"
    return "desk", "-"


# --------------------------------------------------------------------------- the shared file


def events_path(p: config.Paths | None = None) -> Path:
    return (p or config.paths()).shared / FOLDER / EVENTS_FILE


def _folder(path: Path) -> Path:
    """`<shared>/ux` with FOLDER_MODE: created or widened when this user owns it. A link is refused."""
    if os.path.islink(path):
        raise UxError("a link stands where the shared folder ux should be")
    try:
        path.mkdir(parents=True, exist_ok=True)
    except PermissionError:
        raise UxError("the shared folder ux is missing and this user may not create it") from None
    st = os.lstat(path)
    if not stat.S_ISDIR(st.st_mode):
        raise UxError("the shared folder ux is not a folder")
    mode = stat.S_IMODE(st.st_mode)
    if mode & FOLDER_MODE != FOLDER_MODE:
        if st.st_uid != os.geteuid():
            raise UxError("the shared folder ux has mode %o and another user owns it: its owner runs chmod %o on it"
                          % (mode, FOLDER_MODE))
        config.chmod_dir(path, mode | FOLDER_MODE)
    return path


def _clean(text: str) -> str:
    text = re.sub(r"\s+", " ", text).strip()
    return text[:NOTE_CHARS] or "-"


def line(cls: str, session: str, code: str, user: str, text: str = "-", at: dt.datetime | None = None) -> str:
    at = (at or now()).astimezone(dt.timezone.utc)
    return "\t".join((at.strftime("%Y-%m-%dT%H:%M:%SZ"), user, session, code or "-", cls, _clean(text))) + "\n"


def append(row: str, p: config.Paths | None = None) -> Path:
    """One line appended under an exclusive lock, the file FILE_MODE and no link followed."""
    path = events_path(p)
    _folder(path.parent)
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND | os.O_NOFOLLOW | os.O_CLOEXEC, FILE_MODE)
    except PermissionError:
        raise UxError("events.tsv in the shared folder ux cannot be written by this user: its owner runs chmod %o "
                      "on it" % FILE_MODE) from None
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode):
            raise UxError("events.tsv in the shared folder ux is not a regular file")
        mode = stat.S_IMODE(st.st_mode)
        if mode & FILE_MODE != FILE_MODE and st.st_uid == os.geteuid():
            os.fchmod(fd, mode | FILE_MODE)
        fcntl.flock(fd, fcntl.LOCK_EX)
        try:
            data = row.encode("utf-8")
            while data:
                data = data[os.write(fd, data):]
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)
    return path


def hook_event(hook: str, data: dict, cls: str | None = None) -> str | None:
    """The line of one hook event: the prompt by its phrase class, the stop by `cls` (held-draft or held-note), the
    pre-compact as compaction. Returns the class written, None when nothing was. Never a word of the prompt."""
    if not isinstance(data, dict):
        return None
    if hook == "prompt":
        cls = classify(data.get("prompt"))
    elif hook == "pre-compact":
        cls = "compaction"
    elif hook != "stop" or cls not in HOOK_CLASSES:
        return None
    if not cls:
        return None
    folder = data.get("cwd") if isinstance(data.get("cwd"), str) and data.get("cwd") else (
        os.environ.get("CLAUDE_PROJECT_DIR") or os.getcwd())
    session, code = session_kind(folder)
    append(line(cls, session, code, user_kind()))
    return cls


def note(text: str, folder=None, p: config.Paths | None = None) -> str:
    """`awb ux note TEXT`: the text checked once (names and secrets, as a dev ask), then one line of class note."""
    from awb import devask

    p = p or config.paths()
    text = _clean(text or "")
    if text == "-":
        raise UxError("the note is empty")
    try:
        devask.screen(p, text)
    except devask.AskError as exc:
        raise UxError(str(exc)) from None
    session, code = session_kind(folder or os.getcwd())
    append(line(NOTE_CLASS, session, code, user_kind(), text), p)
    return code


# --------------------------------------------------------------------------- reading and counting


def read(p: config.Paths | None = None) -> list[dict]:
    """The lines of events.tsv as dicts (at, user, session, project, cls, text); a line of another shape is left out."""
    path = events_path(p)
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
    except FileNotFoundError:
        return []
    except OSError:
        raise UxError("events.tsv in the shared folder ux cannot be read") from None
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise UxError("events.tsv in the shared folder ux is not a regular file")
        with os.fdopen(fd, "rb", closefd=False) as fh:
            raw = fh.read(MAX_READ)
    finally:
        os.close(fd)
    out = []
    for ln in raw.decode("utf-8", "replace").splitlines():
        parts = ln.split("\t")
        if len(parts) != 6 or not CLASS_RE.fullmatch(parts[4]):
            continue
        try:
            at = dt.datetime.strptime(parts[0], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=dt.timezone.utc)
        except ValueError:
            continue
        out.append({"at": at, "user": parts[1], "session": parts[2], "project": parts[3], "cls": parts[4],
                    "text": parts[5]})
    return out


def week_of(day: dt.date) -> str:
    y, w, _ = day.isocalendar()
    return "%d-W%02d" % (y, w)


def _monday(day: dt.date) -> dt.date:
    return day - dt.timedelta(days=day.weekday())


def counts(events: list[dict], weeks: list[str]) -> dict[str, dict[str, int]]:
    """{class: {week: count}} for the weeks named."""
    out: dict[str, dict[str, int]] = {}
    for e in events:
        wk = week_of(e["at"].date())
        if wk in weeks:
            out.setdefault(e["cls"], dict.fromkeys(weeks, 0))[wk] += 1
    return out


def _weeks(since: dt.date, until: dt.date) -> list[str]:
    out, day = [], _monday(since)
    while day <= until:
        out.append(week_of(day))
        day += dt.timedelta(days=7)
    return out


def _signed(n: int) -> str:
    return "+%d" % n if n > 0 else "%d" % n if n < 0 else "0"


def report(events: list[dict], since: dt.date | None = None, today: dt.date | None = None) -> list[str]:
    """The table: class, one column per ISO week from `since` to today, the change of the last week against the week
    before; then the notes of the period, newest last."""
    today = today or now().date()
    since = since or _monday(today) - dt.timedelta(days=7 * (WEEKS - 1))
    weeks = _weeks(since, today)
    table = counts([e for e in events if e["at"].date() >= since], weeks)
    if not table:
        return ["no ux events since %s" % since.isoformat()]
    width = max(len("class"), *(len(c) for c in table))
    short = [w.split("-")[1] for w in weeks]
    head = ["class".ljust(width)] + short + ["change"]
    lines = ["  ".join(head)]
    for cls in sorted(table, key=lambda c: (-sum(table[c].values()), c)):
        row = table[cls]
        change = row[weeks[-1]] - row[weeks[-2]] if len(weeks) > 1 else row[weeks[-1]]
        lines.append("  ".join([cls.ljust(width)] + [str(row[w]).rjust(len(k)) for w, k in zip(weeks, short)]
                               + [_signed(change).rjust(len("change"))]))
    if len(weeks) > 1:
        lines.append("change: %s against %s; %s runs until Sunday" % (weeks[-1], weeks[-2], weeks[-1]))
    notes = [e for e in events if e["cls"] == NOTE_CLASS and e["at"].date() >= since]
    for e in notes[-20:]:
        lines.append("note %s %s %s: %s" % (e["at"].strftime("%Y-%m-%d"), e["session"], e["project"], e["text"]))
    return lines


def owner_lines(at: dt.datetime | None = None, p: config.Paths | None = None) -> list[str]:
    """The Monday block of the owner status: at most OWNER_LINES lines on the week that ended against the week before
    it, classes with the most events first. Empty on another day, without events or when the file cannot be read."""
    at = at or now()
    day = at.astimezone(dt.timezone.utc).date()
    if day.weekday() != 0:
        return []
    try:
        events = read(p)
    except UxError:
        return []
    last = day - dt.timedelta(days=7)
    before = day - dt.timedelta(days=14)
    weeks = [week_of(before), week_of(last)]
    table = counts(events, weeks)
    if not table:
        return []
    total = [sum(r[w] for r in table.values()) for w in weeks]
    lines = ["UX %s: %d events (%s against %s), awb ux report for the table"
             % (weeks[1], total[1], _signed(total[1] - total[0]), weeks[0])]
    for cls in sorted(table, key=lambda c: (-table[c][weeks[1]], -table[c][weeks[0]], c))[:OWNER_LINES - 1]:
        r = table[cls]
        lines.append("  %s: %d (%s)" % (cls, r[weeks[1]], _signed(r[weeks[1]] - r[weeks[0]])))
    return lines


# --------------------------------------------------------------------------- the command


def _date(value: str) -> dt.date:
    try:
        return dt.date.fromisoformat(value)
    except ValueError:
        raise UxError("--since takes a date like 2026-11-16") from None


def main(argv: list[str] | None = None) -> int:
    from awb.cli import SafeParser

    ap = SafeParser(prog="awb ux", description="Counted frictions of the sessions: a note by hand and the weekly "
                                               "table of the counts in <shared>/ux/events.tsv.")
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("note", help="add a hand-written friction line (class note); the text passes the name and "
                                    "secret checks once")
    s.add_argument("text", metavar="TEXT")
    s = sub.add_parser("report", help="the count of each class per ISO week and the change against the week before")
    s.add_argument("--since", metavar="DATE", help="the first day, YYYY-MM-DD (default: %d weeks back)" % WEEKS)
    args = ap.parse_args(argv)
    try:
        if args.cmd == "note":
            code = note(args.text)
            print("ux note written to %s/%s (project %s)" % (FOLDER, EVENTS_FILE, code))
            return 0
        since = _date(args.since) if args.since else None
        print("\n".join(report(read(), since)))
        return 0
    except UxError as exc:
        print("awb ux: %s" % exc, file=sys.stderr)
        return 2
    except OSError as exc:
        print("awb ux: %s (operating system error)" % type(exc).__name__, file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
