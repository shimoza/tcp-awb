"""Rules that change reach every project and every running session.

Two gaps closed here (2026-10-06: a session started on 2026-09-30 still looked for a file in the vault inbox, because
its project's CLAUDE.md and the rules it had loaded at its start predated the bucket inboxes):

- `awb projects sync [CODE...]` brings the template lines of each project's own CLAUDE.md and STATE.md up to the
  current ones: a known old line is replaced by its new form and a line the template gained is added after its
  anchor. Every other line of the project, its own notes included, stays as it is. Nothing is committed: the
  session commits with its next step.
- The session-start hook remembers a digest of the rules a session loaded (the work rules of the seal and the
  project's CLAUDE.md); the prompt hook compares it with the files of now and, when they changed or the session
  started before this check existed, tells the session once to read them again.

A second kind of notice tells a running session about new input without his message: `awb import` leaves a notice
in <shared>/notices/ with the customer code, the project code and the file ids of its copies (codes only), and the
next prompt of a session of that project or of a project of that customer reads "new input: N copies in the
outbox of CUST-XXXX, report intake-report.md" once ("replaced" after `awb import --redo`). A notice whose copies
have all left the outbox is not shown any more.

A third kind is the note from development (awb/devnote.py, `awb projects note`): "note from development: TITLE
(notes/from-dev/FILE): read it first", once per session.

A fourth kind is the knowledge base block (awb/kbfirst.py, task KF): "Known from the knowledge base (N facts on this
topic ...)" with the closest facts, once per task.
"""
from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import secrets
import stat
from contextlib import contextmanager
from datetime import datetime, timezone
from functools import wraps
from pathlib import Path

from awb import config, devnote, safefile
from awb.projects import (FX_AFTER_OLD, FX_HEADING_OLD, FX_PHRASE_OLD, FX_SCRIPT_LINE, KB_FIRST, LAB_LOGS,
                          LAB_RUN_RULE, LAB_RUN_RULE_HEAD, LAB_RUN_RULE_OLD_HEAD, LEVEL_2, LEVEL_2_RUN,
                          OPEN_COMMAND_RULE, OWN_OUTPUT_RULE, PROJECT_LINE, PUT_KEY_RULE, REVIEW_L1, START_RULE)

CLAUDE_FIXES = (
    # T6 (2026-10-27): the FX line named /opt/tcp-awb/workflows, a path no release creates
    ("- First one line: it runs Fable 5.1 at effort max, costs about 200k tokens and 10-15 minutes. Wait for one "
     "word. Then run Workflow with scriptPath `/opt/tcp-awb/workflows/final-check.js` and args {project: this "
     "folder, deliverable: deliverables/<name>, date: today}.",
     "- First one line: it runs Fable 5.1 at effort max, costs about 200k tokens and 10-15 minutes. Wait for one "
     "word. Then run Workflow with scriptPath `/opt/tcp-awb/src/workflows/final-check.js` and args {project: this "
     "folder, deliverable: deliverables/<name>, date: today}."),
    # task GT (2026-10-27): the FX section becomes the review levels
    (FX_HEADING_OLD, "## Review levels"),
    (FX_PHRASE_OLD, REVIEW_L1 + "\n" + LEVEL_2),
    (FX_SCRIPT_LINE, LEVEL_2_RUN),
    ("- Start with SCOPE.md, STATE.md and OPEN.md. Update STATE.md and OPEN.md before the session ends.",
     "- Start with SCOPE.md, STATE.md and OPEN.md. Update STATE.md (with its Status: and Next: lines) and OPEN.md "
     "after every step that changes the status, in the same commit."),
    # task KF (2026-10-28): knowledge base first, on the start line
    ("- " + START_RULE, "- " + START_RULE + " " + KB_FIRST),
    ("- Customer material comes in only through `awb intake` and moves from the outbox into input/.",
     "- Files from him come through the bucket inboxes: when he says a file is in the inbox, run `awb inbox take "
     "<his words>`, never look in the vault. Copies of his own `awb import` wait in the outbox (the prompt says "
     "\"new input\" when they come): move them into input/."),
    ("- Files from him come through the bucket inboxes: when he says a file is in the inbox, run `awb inbox take "
     "<his words>`, never look in the vault. Copies of his own `awb intake` wait in the outbox: move them into "
     "input/.",
     "- Files from him come through the bucket inboxes: when he says a file is in the inbox, run `awb inbox take "
     "<his words>`, never look in the vault. Copies of his own `awb import` wait in the outbox (the prompt says "
     "\"new input\" when they come): move them into input/."),
    # ask 2026-11-27-tcp-j5sy-1: the outbox copies are taken with a command, not moved by hand
    ("- Files from him come through the bucket inboxes: when he says a file is in the inbox, run `awb inbox take "
     "<his words>`, never look in the vault. Copies of his own `awb import` wait in the outbox (the prompt says "
     "\"new input\" when they come): move them into input/.",
     "- Files from him come through the bucket inboxes: when he says a file is in the inbox, run `awb inbox take "
     "<his words>`, never look in the vault. Copies of his own `awb import` wait in the outbox (the prompt says "
     "\"new input\" when they come): `awb inbox take --outbox` takes them into input/ with the name check."),
    ("- Files from him come through the bucket inboxes: when he says a file is in the inbox, run `awb inbox take "
     "<his words>`, never look in the vault. Copies of his own `awb intake` wait in the outbox: `awb inbox take "
     "--outbox` takes them into input/ with the name check.",
     "- Files from him come through the bucket inboxes: when he says a file is in the inbox, run `awb inbox take "
     "<his words>`, never look in the vault. Copies of his own `awb import` wait in the outbox (the prompt says "
     "\"new input\" when they come): `awb inbox take --outbox` takes them into input/ with the name check."),
    # task LR (2026-10-28): the lab run rule joins the line of the kind project
    ("- " + PROJECT_LINE, "- " + PROJECT_LINE + " " + LAB_RUN_RULE),
)
CLAUDE_ADD = (
    ("- Proof goes to evidence/, results to deliverables/, review records to reviews/.",
     "- Files for him leave with `awb xchg put FILE` (the lab bucket, from-session/<date>/).",
     "awb xchg put"),
    ("- RESOURCES.md lists what the project uses, with platform ids and codes.",
     "- " + OWN_OUTPUT_RULE,
     "awb paste --own"),
    ("- Run `awb check FILE` on every deliverable and `awb gate` before every commit.", "",
     "workflows/final-check.js"),
    (LEVEL_2_RUN,
     "- The final text gets its own review and it costs less (RV2): `awb review init` and `awb review claims` on it, "
     "then Workflow with scriptPath `/opt/tcp-awb/src/workflows/review.js` and args {..., after_fx: true} (the "
     "platform lenses and the partner lens only), then `awb review pass`. The final check stands in for the request "
     "check and the blind verification while the final text is unchanged; after an edit the full review applies.",
     "after_fx"),
)
"""(anchor, new line, marker): the line goes after the anchor line unless a line carries the marker. An empty new
line stands for the FX section of the template (projects.FINAL_CHECK_SECTION, 2026-10-27): a file with the anchor
line gets it at its end, after the project's own lines."""
CLAUDE_ADD += (("- " + OWN_OUTPUT_RULE, devnote.FILE_RULE, "he cannot open it"),
               (devnote.FILE_RULE, devnote.NOTE_RULE, "notes/from-dev/"))
"""His rules of 2026-10-27 (task DN): no link to a file, deliverables as HTML and md, notes from development."""
CLAUDE_ADD += (("- " + OWN_OUTPUT_RULE, LAB_LOGS, "Logs of our own lab runs"),)
"""Task GT: the lab log line goes right after the line of P1, before the lines of DN."""
CLAUDE_APPEND = (("- Files for him leave with `awb xchg put FILE`", "--kind WORD", PUT_KEY_RULE),
                 ("- " + START_RULE, OPEN_COMMAND_RULE, OPEN_COMMAND_RULE))
"""(line start, marker, text): the text goes at the end of the line unless the line carries the marker. Task BK1
(2026-11-06): the kind word and the language of a put join the put line (the template holds 30 lines at most)."""
CLAUDE_CUT = (
    ("- Files for him leave with `awb xchg put FILE`", ", after the name check and, for a customer project, the review",
     ""),
    # the owner's decision of 2026-11-20: test-10503 is a lab tenant too
    ("- " + PROJECT_LINE + " " + LAB_RUN_RULE_OLD_HEAD, LAB_RUN_RULE_OLD_HEAD, LAB_RUN_RULE_HEAD),
)
"""(line start, old part, new part): task GT takes the review out of the put line of an older project, whatever
code its naming rule names; the lab run rule of an older project gets test-10503 (2026-11-20)."""
CLAUDE_DROP = (FX_AFTER_OLD,)
"""Lines task GT takes out: the last line of the FX section went into the run line of level 2."""
STATE_FIXES = (
    ("Where the work stands. Update it at the end of every session. Codes only, never a name.",
     "Where the work stands. Update it and OPEN.md after every step that changes the status, in the same commit. "
     "Codes only, never a name."),
)
RULES_READ = str(Path(__file__).resolve().parent.parent / "seal" / "work-claude" / "CLAUDE.md")
"""The work rules file the notice asks a session to read again: the installed one (projects.RULES_FILE, the file a
project's CLAUDE.md imports and the seal copies to the work user's ~/.claude/CLAUDE.md byte for byte). Never the
copy in ~/.claude: that is the client's folder, which the pre-tool guard refuses to every call (task T1)."""
NOTICE = ("The Workbench rules or the CLAUDE.md of this project changed since this session started (or the session "
          "is older than this check). Read %s (the Workbench rules) and the CLAUDE.md of this project again now and "
          "work by them: they replace what you loaded at the start. In short: \"the inbox\" is the bucket inboxes "
          "of `awb inbox take`, files for him leave with `awb xchg put`, and STATE.md keeps its Status: and Next: "
          "lines current after every step." % RULES_READ)


# --------------------------------------------------------------------------- the files of a project


def _fix_lines(text: str, fixes, adds=(), cuts=(), drops=(), appends=()) -> str:
    lines = text.split("\n")
    for old, new in fixes:
        lines = [new if line.rstrip() == old else line for line in lines]
    for start, old, new in cuts:
        lines = [line.replace(old, new) if line.startswith(start) else line for line in lines]
    lines = [line for line in lines if line.rstrip() not in drops]
    for anchor, new, marker in adds:
        if any(marker in line for line in lines):
            continue
        if not new:
            if not any(line.rstrip() == anchor for line in lines):
                continue
            from awb import projects
            block = [""] + projects.FINAL_CHECK_SECTION.rstrip("\n").split("\n")
            end = len(lines) - 1 if lines and lines[-1] == "" else len(lines)
            while end > 0 and lines[end - 1].strip() == "":
                end -= 1
            lines[end:end] = block
            continue
        at = next((i for i, line in enumerate(lines) if line.rstrip() == anchor), None)
        if at is not None:
            lines.insert(at + 1, new)
    for start, marker, text in appends:
        lines = [line.rstrip() + " " + text if line.startswith(start) and marker not in line else line
                 for line in lines]
    return "\n".join(lines)


# Bound reads even when a file grows after fstat. These are small rule and JSON files.
_MAX_BYTES = 1024 * 1024


@contextmanager
def _directory(path: Path, create: bool = False):
    """Walk through directory handles, refusing links in every component."""
    path = Path(os.path.abspath(path))
    fd = os.open(path.anchor, os.O_RDONLY | os.O_DIRECTORY)
    try:
        for part in path.parts[1:]:
            if create:
                try:
                    os.mkdir(part, 0o750, dir_fd=fd)
                except FileExistsError:
                    pass
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            os.close(fd)
            fd = child
        yield fd
    finally:
        os.close(fd)


@contextmanager
def _regular(path: Path, writable: bool = False):
    with _directory(path.parent) as parent:
        fd = os.open(path.name, (os.O_RDWR if writable else os.O_RDONLY) | os.O_NOFOLLOW | os.O_NONBLOCK,
                     dir_fd=parent)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise OSError("expected a single regular file")
        with os.fdopen(fd, "r+" if writable else "r", encoding="utf-8") as fh:
            fd = None
            yield fh
    finally:
        if fd is not None:
            os.close(fd)


def _read_text(fh) -> str:
    data = fh.buffer.read(_MAX_BYTES + 1)
    if len(data) > _MAX_BYTES:
        raise ValueError("file exceeds the rules size limit")
    return data.decode("utf-8").replace("\r\n", "\n").replace("\r", "\n")


def sync_project(root: Path) -> list[str]:
    """Bring the template lines of one project up to date. Returns the names of the files it changed."""
    changed = []
    for name, fixes, adds, cuts, drops, appends in (
            ("CLAUDE.md", CLAUDE_FIXES, CLAUDE_ADD, CLAUDE_CUT, CLAUDE_DROP, CLAUDE_APPEND),
            ("STATE.md", STATE_FIXES, (), (), (), ())):
        try:
            with _regular(Path(root) / name, writable=True) as fh:
                fcntl.flock(fh, fcntl.LOCK_EX)
                text = _read_text(fh)
                new = _fix_lines(text, fixes, adds, cuts, drops, appends)
                if new != text:
                    fh.seek(0)
                    fh.write(new)
                    fh.truncate()
                    fh.flush()
                    changed.append(name)
        except (OSError, ValueError):
            continue
    return changed


def current(root: Path | None, client: Path | None = None) -> bool:
    """True when the rules a session would load are the installed ones: the client file `client` (the work user's
    ~/.claude/CLAUDE.md, None to leave it out) is the installed work rules file byte for byte, and the template
    lines of the project's CLAUDE.md and STATE.md are the current ones (`sync_project` would change nothing).
    A file that cannot be read is not current. For the session-start receipt (awb/hooks.py)."""
    from awb import projects

    if client is not None:
        try:
            with _regular(Path(client)) as actual, _regular(Path(projects.RULES_FILE)) as installed:
                left, right = actual.buffer.read(_MAX_BYTES + 1), installed.buffer.read(_MAX_BYTES + 1)
                equal = left == right
                oversized = max(len(left), len(right)) > _MAX_BYTES
            if not equal or oversized:
                return False
        except OSError:
            return False
    if root is None:
        return True
    try:
        with _directory(Path(root)):
            pass
    except OSError:
        return False
    for name, fixes, adds, cuts, drops, appends in (
            ("CLAUDE.md", CLAUDE_FIXES, CLAUDE_ADD, CLAUDE_CUT, CLAUDE_DROP, CLAUDE_APPEND),
            ("STATE.md", STATE_FIXES, (), (), (), ())):
        path = Path(root) / name
        try:
            with _regular(path) as fh:
                text = _read_text(fh)
        except FileNotFoundError:
            if path.is_symlink():
                return False
            continue
        except (OSError, ValueError):
            return False
        if _fix_lines(text, fixes, adds, cuts, drops, appends) != text:
            return False
    return True


def sync(p: config.Paths, codes: list[str] | None = None) -> list[tuple[str, list[str]]]:
    """Every active project, or the ones named: (code, files changed)."""
    from awb import projects

    out = []
    for r in projects.load(p):
        if r.state not in projects.LIVE_STATES or (codes and r.code not in codes):
            continue
        folder = Path(r.path)
        if folder.parent != p.projects_root or folder.name != r.code:
            continue
        out.append((r.code, sync_project(folder)))
    return out


def sync_ignores(p: config.Paths, codes: list[str] | None = None) -> list[tuple[str, str]]:
    """The disk image lines (projects.ignore_disk_images) in every active project, or in the ones named (`awb
    projects sync`): (code, "added" | "current" | "cannot be written")."""
    from awb import projects

    out = []
    for r in projects.load(p):
        if r.state not in projects.LIVE_STATES or (codes and r.code not in codes):
            continue
        folder = Path(r.path)
        if folder.parent != p.projects_root or folder.name != r.code:
            continue
        try:
            out.append((r.code, "added" if projects.ignore_disk_images(folder) else "current"))
        except (OSError, UnicodeError):
            out.append((r.code, "cannot be written"))
    return out


def sync_settings(p: config.Paths, codes: list[str] | None = None) -> list[tuple[str, str]]:
    """The installed workflows folder in the client settings of every active project, or of the ones named (`awb
    projects sync`, B-118): (code, "added" | "current" | "cannot be written"). Nothing is committed: the session
    commits with its next step."""
    from awb import projects

    out = []
    for r in projects.load(p):
        if r.state not in projects.LIVE_STATES or (codes and r.code not in codes):
            continue
        folder = Path(r.path)
        if folder.parent != p.projects_root or folder.name != r.code:
            continue
        try:
            out.append((r.code, "added" if projects.add_workflows_dir(folder) else "current"))
        except (OSError, ValueError):
            out.append((r.code, "cannot be written"))
    return out


# --------------------------------------------------------------------------- what a running session loaded


def _rules_files(root: Path | None) -> list[Path]:
    from awb import projects

    files = [Path.home() / ".claude" / "CLAUDE.md", Path(projects.RULES_FILE)]
    if root is not None:
        files.append(Path(root) / "CLAUDE.md")
    return files


def digest(root: Path | None) -> str:
    h = hashlib.sha256()
    for f in _rules_files(root):
        try:
            with _regular(f) as fh:
                data = fh.buffer.read(_MAX_BYTES + 1)
            if len(data) > _MAX_BYTES:
                raise ValueError("file exceeds the rules size limit")
            data = b"read\0" + data
        except (OSError, ValueError):
            data = b"unreadable"
        h.update(os.fsencode(f) + b"\0" + data + b"\0")
    return h.hexdigest()


def _mark(p: config.Paths, session_id: str) -> Path:
    # Keep established short safe ids, but never discard characters or truncate ids.
    if isinstance(session_id, str) and re.fullmatch(r"[A-Za-z0-9_-]{1,80}", session_id):
        sid = session_id
    else:
        sid = "." + hashlib.sha256(str(session_id).encode("utf-8", "surrogatepass")).hexdigest()
    return p.shared / "sessions" / ("rules-%s.json" % sid)


def _unique_object(pairs):
    out = {}
    for key, value in pairs:
        if key in out:
            raise ValueError("duplicate JSON field")
        out[key] = value
    return out


def _read_mark(path: Path) -> dict:
    try:
        with _regular(path) as fh:
            data = json.loads(_read_text(fh), object_pairs_hook=_unique_object)
    except (OSError, ValueError, RecursionError):
        return {}
    pending = [(data, 0)]
    while pending:
        value, depth = pending.pop()
        if depth > 32:
            return {}
        if isinstance(value, dict):
            pending.extend((v, depth + 1) for v in value.values())
        elif isinstance(value, list):
            pending.extend((v, depth + 1) for v in value)
    if not isinstance(data, dict):
        return {}
    for key in ("inputs", "notes"):
        if key in data:
            data[key] = _strings(data[key])
    return data


def _publish(parent: int, name: str, data: dict, mode: int, exclusive: bool = False) -> None:
    safefile.write_atomic(name, json.dumps(data) + "\n", dir_fd=parent, mode=mode, fsync=False,
                          overwrite=not exclusive, temp=lambda _name: ".rules-" + secrets.token_hex(16) + ".tmp")


def _write_mark(path: Path, data: dict) -> None:
    try:
        with _directory(path.parent, create=True) as parent:
            try:
                info = os.stat(path.name, dir_fd=parent, follow_symlinks=False)
            except FileNotFoundError:
                pass
            else:
                if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                    return
            _publish(parent, path.name, data, 0o600)
    except (OSError, ValueError):
        # Hooks must not echo storage paths or exception text.
        return


def _strings(value) -> list[str]:
    return [x for x in value if isinstance(x, str)][-200:] if isinstance(value, list) else []


def _session_locked(func):
    @wraps(func)
    def locked(p, session_id, root):
        if not session_id:
            return func(p, session_id, root)
        try:
            with _directory(p.shared / "sessions", create=True) as parent:
                # The directory inode is stable across atomic mark replacement.
                fcntl.flock(parent, fcntl.LOCK_EX)
                return func(p, session_id, root)
        except OSError:
            return [] if func.__name__ == "input_notices" else None
    return locked


@_session_locked
def remember(p: config.Paths, session_id: str | None, root: Path | None) -> None:
    """The session-start hook: what this session loaded. The input notices it was told stay told."""
    if not session_id:
        return
    path = _mark(p, session_id)
    mark = _read_mark(path)
    data = {"digest": digest(root), "inputs": _strings(mark.get("inputs"))}
    if isinstance(mark.get("notes"), list):
        data["notes"] = [x for x in mark["notes"] if isinstance(x, str)][-200:]     # notes told stay told (DN)
    if isinstance(mark.get("kb"), dict):
        data["kb"] = mark["kb"]          # the facts told and the last prompt stay (task KF, awb/kbfirst.py)
    if isinstance(mark.get("winlab"), list):
        data["winlab"] = [x for x in mark["winlab"] if isinstance(x, str)][-50:]   # lab window warnings told (Q-0009)
    _write_mark(path, data)


def notice(p: config.Paths, session_id: str | None, root: Path | None) -> str | None:
    """The prompt hook: NOTICE once when the rules changed since the session loaded them, or when the session is
    older than this check, and once every input notice of its project (`input_notices`); None when there is
    nothing to say and for a prompt without a session id."""
    if not session_id:
        return None
    said = [t for t in (_rules_changed(p, session_id, root),) if t] + input_notices(p, session_id, root)
    from awb import devnote
    said += devnote.notices(p, session_id, root)      # notes from development (task DN), once each
    return "\n".join(said) or None


@_session_locked
def _rules_changed(p: config.Paths, session_id: str, root: Path | None) -> str | None:
    seen = _read_mark(_mark(p, session_id)).get("digest")
    if seen == digest(root):
        return None
    remember.__wrapped__(p, session_id, root)
    return NOTICE


# --------------------------------------------------------------------------- new input (awb import)

INPUT = "new input: %d copies in the outbox of %s, report intake-report.md; awb inbox take --outbox takes them"
REPLACED = ("replaced: %d copies in the outbox of %s under the file ids they had, report intake-report.md; awb inbox "
            "take --outbox takes them into input/ over the earlier ones")
_NOTICE_RE = re.compile(r"input-[0-9TZ-]+-[0-9a-f]{8}\.json")
_FILE_ID_RE = re.compile(r"F-[A-Z2-7]{4}|(?:mail|transcript|table|paste)-\d{4}-\d{2}-\d{2}-\d+\.(?:md|csv)", re.ASCII)
"""A file id of an imported copy (F-ABCD) or the name of the copy of a paste (mail-2026-10-12-1.md)."""


def notices_dir(p: config.Paths) -> Path:
    return p.shared / "notices"


AUTO_INPUT = "new input: %d files (%s)"
"""The line of an import of the timer (`awb import --auto`): the count and the file ids that wait, at most
AUTO_IDS_SHOWN of them."""
AUTO_IDS_SHOWN = 8


def post_input(p: config.Paths, customer: str, project: str, file_ids: list[str], replaced: bool = False,
               auto: bool = False) -> Path:
    """Leave the notice of one import for the sessions (owner side): codes and file ids only. `auto` marks the
    import of the timer: its notice reads "new input: N files (FILE IDS)"."""
    if not _notice_codes(customer, project) or not isinstance(file_ids, list):
        raise ValueError("a notice carries a customer code, a project code and a list of file ids")
    ids = list(dict.fromkeys(f for f in file_ids if isinstance(f, str) and _FILE_ID_RE.fullmatch(f)))
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    path = notices_dir(p) / ("input-%s-%s.json" % (stamp, secrets.token_hex(4)))
    data = {"customer": customer, "project": project, "ids": ids, "replaced": bool(replaced)}
    if auto:
        data["auto"] = True
    try:
        with _directory(path.parent, create=True) as parent:
            _publish(parent, path.name, data, 0o640, exclusive=True)
    except OSError:
        raise ValueError("the input notice could not be stored") from None
    return path


def _notice_codes(customer, project) -> bool:
    from awb import codes

    return (isinstance(customer, str) and isinstance(project, str) and codes.is_project_code(project)
            and (codes.is_code(customer) or customer == project))


def _waiting(path: Path) -> bool:
    try:
        with _regular(path):
            return True
    except (OSError, ValueError):
        return False


def project_copies(p: config.Paths, code: str) -> dict[tuple[str, str], bool]:
    """The copies the input notices of project `code` name (its own imports, never another project's): (outbox
    folder, file name) -> whether a notice of an `awb import --redo` replaced it. The outbox folder is the
    customer code or, for a project without a customer, the project code. Used by `awb inbox take --outbox`."""
    out: dict[tuple[str, str], bool] = {}
    try:
        with _directory(notices_dir(p)) as parent:
            names = sorted(n for n in os.listdir(parent) if _NOTICE_RE.fullmatch(n))
    except OSError:
        return out
    for name in names:
        f = notices_dir(p) / name
        if f.is_symlink():
            continue
        data = _read_mark(f)
        cust, proj, ids = data.get("customer"), data.get("project"), data.get("ids") or []
        if proj != code or not _notice_codes(cust, proj) or not isinstance(ids, list):
            continue
        replaced = data.get("replaced") is True
        for i in ids:
            if isinstance(i, str) and _FILE_ID_RE.fullmatch(i):
                key = (cust, i if "." in i else "%s.md" % i)
                out[key] = out.get(key, False) or replaced
    return out


def _project_customer(p: config.Paths, code: str) -> str | None:
    from awb import projects

    try:
        for pr in projects.load(p):
            if pr.code == code:
                return pr.customer
    except projects.ProjectError:
        pass
    return None


@_session_locked
def input_notices(p: config.Paths, session_id: str | None, root: Path | None) -> list[str]:
    """The lines of the input notices this session has not been told yet: a notice of its project, or of the
    customer of its project, whose copies still wait in the outbox. Each is told once per session."""
    if not session_id or root is None:
        return []
    code = Path(root).name
    customer = _project_customer(p, code)
    try:
        with _directory(notices_dir(p)) as parent:
            names = sorted(n for n in os.listdir(parent) if _NOTICE_RE.fullmatch(n))
    except OSError:
        return []
    path = _mark(p, session_id)
    mark = _read_mark(path)
    seen = _strings(mark.get("inputs"))
    out: list[str] = []
    for name in names:
        if name in seen:
            continue
        f = notices_dir(p) / name
        if f.is_symlink():
            continue
        data = _read_mark(f)
        cust, proj, ids = data.get("customer"), data.get("project"), data.get("ids") or []
        if (not _notice_codes(cust, proj) or not isinstance(ids, list)
                or not isinstance(data.get("replaced", False), bool)
                or not isinstance(data.get("auto", False), bool)
                or not (proj == code or (customer and cust == customer))):
            continue
        waiting = list(dict.fromkeys(i for i in ids if isinstance(i, str) and _FILE_ID_RE.fullmatch(i)
                                    and _waiting(p.outbox / cust / (i if "." in i else "%s.md" % i))))
        if not waiting:
            continue
        if data.get("auto") is True:
            shown = ", ".join(i.rsplit(".", 1)[0] for i in waiting[:AUTO_IDS_SHOWN])
            if len(waiting) > AUTO_IDS_SHOWN:
                shown += ", +%d more" % (len(waiting) - AUTO_IDS_SHOWN)
            out.append(AUTO_INPUT % (len(waiting), shown))
        else:
            out.append((REPLACED if data.get("replaced") else INPUT) % (len(waiting), cust))
        seen.append(name)
    if out:
        mark["inputs"] = seen[-200:]
        mark.setdefault("digest", digest(root))
        _write_mark(path, mark)
    return out
