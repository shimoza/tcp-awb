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
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
from datetime import datetime, timezone
from pathlib import Path

from awb import config, devnote
from awb.projects import OWN_OUTPUT_RULE

CLAUDE_FIXES = (
    # T6 (2026-10-27): the FX line named /opt/tcp-awb/workflows, a path no release creates
    ("- First one line: it runs Fable 5.1 at effort max, costs about 200k tokens and 10-15 minutes. Wait for one "
     "word. Then run Workflow with scriptPath `/opt/tcp-awb/workflows/final-check.js` and args {project: this "
     "folder, deliverable: deliverables/<name>, date: today}.",
     "- First one line: it runs Fable 5.1 at effort max, costs about 200k tokens and 10-15 minutes. Wait for one "
     "word. Then run Workflow with scriptPath `/opt/tcp-awb/src/workflows/final-check.js` and args {project: this "
     "folder, deliverable: deliverables/<name>, date: today}."),
    ("- Start with SCOPE.md, STATE.md and OPEN.md. Update STATE.md and OPEN.md before the session ends.",
     "- Start with SCOPE.md, STATE.md and OPEN.md. Update STATE.md (with its Status: and Next: lines) and OPEN.md "
     "after every step that changes the status, in the same commit."),
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
)
CLAUDE_ADD = (
    ("- Proof goes to evidence/, results to deliverables/, review records to reviews/.",
     "- Files for him leave with `awb xchg put FILE` (the lab bucket, from-session/<date>/), after the name check "
     "and, for a customer project, the review.",
     "awb xchg put"),
    ("- RESOURCES.md lists what the project uses, with platform ids and codes.",
     "- " + OWN_OUTPUT_RULE,
     "awb paste --own"),
    ("- Run `awb check FILE` on every deliverable and `awb gate` before every commit.", "",
     "workflows/final-check.js"),
    ("- After it show the path of the final text, the summary of the table and the open points. Add the open points "
     "to OPEN.md under the heading \"Final check (FX, DATE)\".",
     "- The final text gets its own review and it costs less (RV2): `awb review init` and `awb review claims` on it, "
     "then Workflow with scriptPath `/opt/tcp-awb/src/workflows/review.js` and args {..., after_fx: true} (the "
     "platform lenses and the partner lens only), then `awb review pass`. The final check stands in for the request "
     "check and the blind verification while the final text is unchanged; after an edit the full review applies.",
     "after_fx"),
)
"""(anchor, new line, marker): the line goes after the anchor line unless a line carries the marker. An empty new
line stands for the FX section of the template (projects.FINAL_CHECK_SECTION, 2026-10-27): a file with the anchor
line gets it at its end, after the project's own lines."""
CLAUDE_ADD += devnote.CLAUDE_ADD
"""His rules of 2026-10-27 (task DN): no link to a file, deliverables as HTML and md, notes from development."""
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
_SID_RE = re.compile(r"[^A-Za-z0-9_-]")


# --------------------------------------------------------------------------- the files of a project


def _fix_lines(text: str, fixes, adds=()) -> str:
    lines = text.split("\n")
    for old, new in fixes:
        lines = [new if line.rstrip() == old else line for line in lines]
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
    return "\n".join(lines)


def sync_project(root: Path) -> list[str]:
    """Bring the template lines of one project up to date. Returns the names of the files it changed."""
    changed = []
    for name, fixes, adds in (("CLAUDE.md", CLAUDE_FIXES, CLAUDE_ADD), ("STATE.md", STATE_FIXES, ())):
        path = Path(root) / name
        try:
            if path.is_symlink() or not path.is_file():
                continue
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeError):
            continue
        new = _fix_lines(text, fixes, adds)
        if new != text:
            with open(path, "r+", encoding="utf-8") as fh:     # the same file: its owner and mode stay
                fh.write(new)
                fh.truncate()
            changed.append(name)
    return changed


def current(root: Path | None, client: Path | None = None) -> bool:
    """True when the rules a session would load are the installed ones: the client file `client` (the work user's
    ~/.claude/CLAUDE.md, None to leave it out) is the installed work rules file byte for byte, and the template
    lines of the project's CLAUDE.md and STATE.md are the current ones (`sync_project` would change nothing).
    A file that cannot be read is not current. For the session-start receipt (awb/hooks.py)."""
    from awb import projects

    if client is not None:
        try:
            if Path(client).read_bytes() != Path(projects.RULES_FILE).read_bytes():
                return False
        except OSError:
            return False
    if root is None:
        return True
    for name, fixes, adds in (("CLAUDE.md", CLAUDE_FIXES, CLAUDE_ADD), ("STATE.md", STATE_FIXES, ())):
        path = Path(root) / name
        if not path.exists() and not path.is_symlink():
            continue
        try:
            if path.is_symlink():
                return False
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeError):
            return False
        if _fix_lines(text, fixes, adds) != text:
            return False
    return True


def sync(p: config.Paths, codes: list[str] | None = None) -> list[tuple[str, list[str]]]:
    """Every active project, or the ones named: (code, files changed)."""
    from awb import projects

    out = []
    for r in projects.load(p):
        if r.state not in projects.LIVE_STATES or (codes and r.code not in codes):
            continue
        out.append((r.code, sync_project(Path(r.path))))
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
            data = f.read_bytes()
        except OSError:
            data = b""
        h.update(str(f).encode() + b"\0" + data + b"\0")
    return h.hexdigest()


def _mark(p: config.Paths, session_id: str) -> Path:
    sid = _SID_RE.sub("", session_id or "")[:80] or "unknown"
    return p.shared / "sessions" / ("rules-%s.json" % sid)


def _read_mark(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _write_mark(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.is_symlink():
        return
    tmp = path.with_name(".%s.tmp" % path.name)
    tmp.write_text(json.dumps(data) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def remember(p: config.Paths, session_id: str | None, root: Path | None) -> None:
    """The session-start hook: what this session loaded. The input notices it was told stay told."""
    if not session_id:
        return
    path = _mark(p, session_id)
    mark = _read_mark(path)
    seen = mark.get("inputs") or []
    data = {"digest": digest(root), "inputs": [x for x in seen if isinstance(x, str)][-200:]}
    if isinstance(mark.get("notes"), list):
        data["notes"] = [x for x in mark["notes"] if isinstance(x, str)][-200:]     # notes told stay told (DN)
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


def _rules_changed(p: config.Paths, session_id: str, root: Path | None) -> str | None:
    seen = _read_mark(_mark(p, session_id)).get("digest")
    if seen == digest(root):
        return None
    remember(p, session_id, root)
    return NOTICE


# --------------------------------------------------------------------------- new input (awb import)

INPUT = "new input: %d copies in the outbox of %s, report intake-report.md"
REPLACED = ("replaced: %d copies in the outbox of %s under the file ids they had, report intake-report.md; move them "
            "into input/ over the earlier ones")
_NOTICE_RE = re.compile(r"input-[0-9TZ-]+-[0-9a-f]{8}\.json")
_FILE_ID_RE = re.compile(r"F-[A-Z2-7]{4}|(?:mail|transcript|table|paste)-\d{4}-\d{2}-\d{2}-\d+\.(?:md|csv)")
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
    from awb import codes

    if not (codes.is_code(customer) or customer == project) or not codes.is_project_code(project):
        raise ValueError("a notice carries a customer code and a project code")
    ids = [f for f in file_ids if _FILE_ID_RE.fullmatch(f)]
    folder = config.make_dir(notices_dir(p), 0o750, shared=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    path = folder / ("input-%s-%s.json" % (stamp, secrets.token_hex(4)))
    data = {"customer": customer, "project": project, "ids": ids, "replaced": bool(replaced)}
    if auto:
        data["auto"] = True
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o640)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(json.dumps(data) + "\n")
    return path


def _project_customer(p: config.Paths, code: str) -> str | None:
    from awb import projects

    try:
        for pr in projects.load(p):
            if pr.code == code:
                return pr.customer
    except projects.ProjectError:
        pass
    return None


def input_notices(p: config.Paths, session_id: str | None, root: Path | None) -> list[str]:
    """The lines of the input notices this session has not been told yet: a notice of its project, or of the
    customer of its project, whose copies still wait in the outbox. Each is told once per session."""
    if not session_id or root is None:
        return []
    code = Path(root).name
    customer = _project_customer(p, code)
    try:
        names = sorted(f.name for f in notices_dir(p).iterdir() if _NOTICE_RE.fullmatch(f.name))
    except OSError:
        return []
    path = _mark(p, session_id)
    mark = _read_mark(path)
    seen = [x for x in (mark.get("inputs") or []) if isinstance(x, str)]
    out: list[str] = []
    for name in names:
        if name in seen:
            continue
        f = notices_dir(p) / name
        if f.is_symlink():
            continue
        data = _read_mark(f)
        cust, proj, ids = data.get("customer"), data.get("project"), data.get("ids") or []
        if not isinstance(cust, str) or not (proj == code or (customer and cust == customer)):
            continue
        waiting = [i for i in ids if isinstance(i, str) and _FILE_ID_RE.fullmatch(i)
                   and (p.outbox / cust / (i if "." in i else "%s.md" % i)).is_file()]
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
