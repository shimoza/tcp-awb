"""Projects: one folder per piece of work, listed in the project register on the shared side.

The project register is `<shared>/projects.tsv`, a TSV file with a fixed header and one line per project:

    code	kind	customer	platform	path	memory_key	state	created
    tcp-q7m4	engagement	CUST-Q7M4	tcp	/abs/root/tcp-q7m4	-abs-root-tcp-q7m4	active	2026-09-22

It carries codes, kinds, paths and dates only. The goal of a project lives in its SCOPE.md and nowhere else.
The format is strict like the name register: a malformed file is refused as a whole. The error names the line
number and the reason, never the content of the line.

`spawn` makes a sealed project: it refuses a goal or a tag that carries a registered name, structured data or
a word of the owner's blocklist. It also refuses a goal that looks like it carries an unregistered name (a word before a legal
form, a label such as "Kunde:", a person-like mail address, a name after a title, a spaced heading). Without
a register it refuses to run at all. A register that cannot be read here (an encrypted vault or the work user of
the seal) is fine while the vault daemon answers: the names are then checked over its socket. On the work side
a customer code counts when an intake issued it (its folder in the outbox). Then it writes the skeleton, checks
every generated file once more, initialises git with one commit and only then registers the project. Nothing in
this module writes a goal, a tag or a matched value into an exception or any other output.
"""
from __future__ import annotations

import fcntl
import os
import re
import shutil
import stat
import subprocess
import tempfile
import unicodedata
from contextlib import contextmanager
from dataclasses import dataclass, field, replace as _replace
from datetime import date
from pathlib import Path

from awb import codes as _codes
from awb import config as _config
from awb import devnote as _devnote
from awb import register as _register

PROJECT_KINDS = ("query", "project")
REVIEW_TIERS = (1, 2, 3)
DEFAULT_REVIEW_TIER = 3
"""The review tier of a project when SCOPE.md has no `- review-tier:` line (decision 14)."""
"""A query works with the knowledge base, the docs mirror, live prices and calculations; a project also reaches the
test tenants through the key service. A customer is an option of either (--customer), not a kind."""
LEGACY_KINDS = {"engagement": "query", "topic": "query", "code": "query", "lab": "project"}
"""Kinds of before 2026-10-03: still valid in the register, and taken by spawn as the kind they became."""
TENANT_KINDS = ("project", "lab")


def tenant_access(kind: str) -> bool:
    """True when a project of this kind may reach the test tenants."""
    return kind in TENANT_KINDS
HEADER = ("code", "kind", "customer", "platform", "path", "memory_key", "state", "created")
STATES = ("active", "parked", "closed", "deleted")
LIVE_STATES = ("active", "parked")
"""A parked project (PD4) waits on the customer or rests: it keeps its folder, grants, bucket folder and tenants, and
material may still come in. Only the board, the owner's waiting list and the mails leave it out. `awb close` works on
it, `awb projects unpark` brings it back. Its date and reason live in <shared>/parked.tsv (`park_info`)."""
PARK_FILE = "parked.tsv"
MAX_REASON = 80
_REASON_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9 .,;:()/+-]*")
PLATFORM = "tcp"
NO_CUSTOMER = "none"
SUBFOLDERS = ("input", "evidence", "deliverables", "reviews")
NOT_PROJECTS = ("tcp-awb", "tcp-kb", "tcp-shared", "tcp-vault", "tcp-mirrors", "tcp-datasources")
"""Folders of the Workbench itself under the projects root: never a project, never reported by `unregistered`."""
IDLE_DAYS = 30
MAX_STATE_LINES = 150
MAX_GOAL = 500
MAX_CLAUDE_LINES = 30

REPO = Path(__file__).resolve().parent.parent
RULES_FILE = REPO / "seal" / "work-claude" / "CLAUDE.md"
"""The work rules a project imports. The same file the seal installs for the work user; never the developer
instructions of the repository (`CLAUDE.md` at its top), which tell a session to build the Workbench."""

_GIT_NAME = "Architect Workbench"
_GIT_EMAIL = "awb@localhost"
"""The git identity of a project (seal/work-gitconfig names the same for the work user)."""

_DATE_RE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}")
_TAG_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._+-]{0,47}")
@dataclass
class Project:
    code: str
    kind: str
    customer: str
    platform: str
    path: str
    memory_key: str
    state: str
    created: str
    folder_note: str = field(default="", compare=False, repr=False)   # what spawn did in the owner bucket
    fingerprint_note: str = field(default="", compare=False, repr=False)   # what close did with the fingerprint


class ProjectError(Exception):
    """A project problem. The message carries codes, classes, counts and line numbers, never a goal or a form."""


def memory_key(path: str | os.PathLike) -> str:
    """The absolute path with every '/' turned into '-'."""
    return str(path).replace("/", "-")


GOAL_WIDTH = 72


def goal_of(path: str | os.PathLike) -> str:
    """The goal line of a project's SCOPE.md, empty when there is none or the file cannot be read."""
    try:
        text = (Path(path) / "SCOPE.md").read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return ""
    for line in text.splitlines():
        if line.startswith("- goal:"):
            return " ".join(line[len("- goal:"):].split())
    return ""


def short_goal(goal: str, width: int = GOAL_WIDTH) -> str:
    if not goal:
        return "-"
    return goal if len(goal) <= width else goal[: width - 3].rstrip(" ,.:;") + "..."


# --- the project register ---------------------------------------------------------------------------------

def _fail(where: str, reason: str) -> ProjectError:
    return ProjectError("projects.tsv %s: %s" % (where, reason))


def _is_customer(value: str) -> bool:
    return (value == NO_CUSTOMER
            or (_codes.is_code(value) and value.count("-") == 1 and _codes.kind_of(value) == "CUST"))


def _check(pr: Project, where: str) -> None:
    """Refuse a row that must not be in the project register. `where` is "line N" or "new row"."""
    for value in (pr.code, pr.kind, pr.customer, pr.platform, pr.path, pr.memory_key, pr.state, pr.created):
        if not isinstance(value, str):
            raise _fail(where, "every field must be a string")
        if any(unicodedata.category(c) == "Cc" for c in value):
            raise _fail(where, "a field contains a control character, a tab or a line break")
    if not _codes.is_project_code(pr.code):
        raise _fail(where, "code does not match the project code grammar")
    if pr.kind not in PROJECT_KINDS and pr.kind not in LEGACY_KINDS:
        raise _fail(where, "unknown project kind")
    if not _is_customer(pr.customer):
        raise _fail(where, "customer must be a top-level CUST code or none")
    if pr.platform not in ("tcp", "hcs") or not pr.code.startswith(pr.platform + "-"):
        raise _fail(where, "platform must be tcp or hcs and match the code prefix")
    path = Path(pr.path)
    if not path.is_absolute() or path.name != pr.code or str(path) != pr.path or ".." in path.parts:
        raise _fail(where, "path must be absolute, normalised and end in the project code")
    if pr.memory_key != memory_key(pr.path):
        raise _fail(where, "memory_key must be the path with every slash turned into a hyphen")
    if pr.state not in STATES:
        raise _fail(where, "state must be active, parked, closed or deleted")
    if not _DATE_RE.fullmatch(pr.created):
        raise _fail(where, "created is not an ISO date (YYYY-MM-DD)")
    try:
        date.fromisoformat(pr.created)
    except ValueError:
        raise _fail(where, "created is not a valid date") from None


def _check_unique(rows: list[Project]) -> None:
    codes_seen: dict[str, int] = {}
    paths_seen: dict[str, int] = {}
    for i, pr in enumerate(rows, start=2):
        if pr.code in codes_seen:
            raise _fail("line %d" % i, "the same code already appears on line %d" % codes_seen[pr.code])
        if pr.path in paths_seen:
            raise _fail("line %d" % i, "the same path already appears on line %d" % paths_seen[pr.path])
        codes_seen[pr.code] = i
        paths_seen[pr.path] = i


def load(p: _config.Paths) -> list[Project]:
    """Read the project register. A missing file is an empty list. Anything malformed is a ProjectError."""
    path = p.projects_register
    try:
        raw = path.read_bytes()
    except FileNotFoundError:
        return []
    except OSError as exc:
        raise ProjectError("projects.tsv cannot be read: %s" % exc.__class__.__name__) from None
    try:
        content = raw.decode("utf-8")
    except UnicodeDecodeError:
        raise ProjectError("projects.tsv is not valid UTF-8") from None
    if content.startswith("\ufeff"):
        raise _fail("line 1", "a byte order mark is not allowed")
    lines = content.split("\n")
    if lines and lines[-1] == "":
        lines.pop()
    if not lines:
        raise _fail("line 1", "missing header")
    if lines[0] != "\t".join(HEADER):
        raise _fail("line 1", "header must be exactly the %d column names, tab separated" % len(HEADER))
    rows: list[Project] = []
    for i, line in enumerate(lines[1:], start=2):
        where = "line %d" % i
        if line == "":
            raise _fail(where, "blank line")
        if line.startswith("#"):
            raise _fail(where, "comment lines are not allowed")
        if "\r" in line:
            raise _fail(where, "carriage return in line, the register uses LF line endings")
        fields = line.split("\t")
        if len(fields) != len(HEADER):
            raise _fail(where, "expected %d tab separated fields, got %d" % (len(HEADER), len(fields)))
        pr = Project(*fields)
        _check(pr, where)
        rows.append(pr)
    _check_unique(rows)
    return rows


def _save(p: _config.Paths, rows: list[Project]) -> None:
    """Write the project register atomically, file mode 640."""
    rows = list(rows)
    for i, pr in enumerate(rows, start=2):
        _check(pr, "line %d" % i)
    _check_unique(rows)
    target = p.projects_register
    lines = ["\t".join(HEADER)]
    lines.extend("\t".join((r.code, r.kind, r.customer, r.platform, r.path, r.memory_key, r.state, r.created))
                 for r in rows)
    data = ("\n".join(lines) + "\n").encode("utf-8")
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".%s." % target.name, suffix=".tmp", dir=target.parent)
    try:
        os.fchmod(fd, 0o640)
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, target)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


@contextmanager
def locked(p: _config.Paths):
    """One writer of the project register at a time, across processes."""
    p.shared.mkdir(parents=True, exist_ok=True)
    fd = os.open(p.shared / ".projects.lock", os.O_RDWR | os.O_CREAT, 0o640)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)


# The private name stays for the web adapters still deployed outside the repository until they are switched.
_locked = locked


# --- checks on what goes into a project -------------------------------------------------------------------

def _word_checks(text: str, what: str) -> None:
    """Refuse what the owner's blocklist matches (gate.BLOCKLIST_FILES). `what` names the field, never its value."""
    from awb import gate

    rx = gate.blocklist_re()
    if rx is not None and rx.search(text):
        raise ProjectError("%s refused: it matches the blocklist" % what)


def _name_check(check_text, text: str, register_path: Path, what: str) -> None:
    """Refuse any hit of the name check: a registered name or structured data. Classes and counts only."""
    try:
        hits = check_text(text, register_path)
    except _register.RegisterError:
        raise ProjectError("%s refused: the register cannot be read" % what) from None
    if hits:
        counts: dict[str, int] = {}
        for h in hits:
            cls = h.get("cls", "unknown") if isinstance(h, dict) else "unknown"
            counts[cls] = counts.get(cls, 0) + 1
        detail = ", ".join("%s %d" % (c, n) for c, n in sorted(counts.items()))
        raise ProjectError("%s refused by the name check (%s)" % (what, detail))


def _candidate_check(text: str, what: str) -> None:
    """Refuse a text that looks like it carries a name that is not in the register (the strong intake rules
    only: ordinary capitalised words in a goal are fine). Counts only, never the candidate."""
    from awb import intake as _intake
    from awb import normalize as _normalize

    found = _intake.unknown_candidates(_normalize.normalize(text).text, [], rules=_intake.STRONG_RULES)
    if found:
        raise ProjectError("%s refused: it looks like it carries %d name(s) that are not in the register; "
                           "register them in the vault or reword" % (what, len(found)))


def _clean_goal(goal) -> str:
    if not isinstance(goal, str):
        raise ProjectError("goal must be text")
    goal = goal.strip()
    if not goal:
        raise ProjectError("goal is empty")
    if len(goal) > MAX_GOAL:
        raise ProjectError("goal is longer than %d characters" % MAX_GOAL)
    if any(unicodedata.category(c) in ("Cc", "Cf", "Zl", "Zp") for c in goal):
        raise ProjectError("goal must be one line without control or invisible characters")
    return goal


def _clean_tags(tags) -> list[str]:
    if isinstance(tags, str):
        raise ProjectError("tags must be a list")
    out: list[str] = []
    for n, tag in enumerate(tags, start=1):
        if not isinstance(tag, str) or not _TAG_RE.fullmatch(tag):
            raise ProjectError("tag %d must be 1 to 48 letters, digits, dots, plus signs, hyphens or underscores"
                               % n)
        if tag not in out:
            out.append(tag)
    return out


def _readable_here(check_mod, register_path: Path) -> bool:
    """True when the register can be read here: as a file or (the owner's side of an encrypted vault) through
    the admin socket of the vault daemon. False on the work side of the seal, where only the check socket
    answers."""
    try:
        source, _ = check_mod.register_source(register_path)
    except _register.RegisterError:
        return True   # readable but broken: register.load says so below
    if source != check_mod.REMOTE:
        return True
    path = Path(register_path)
    try:
        return not path.exists() and _register.encrypted_path(path).is_file()
    except OSError:
        return False


def _require_name_check(check_mod, register_path: Path) -> None:
    """Refuse before anything is written when names cannot be checked: no register at all or a register that
    cannot be read here while the vault daemon does not answer or is locked."""
    try:
        source, _ = check_mod.register_source(register_path)
    except _register.RegisterError:
        raise ProjectError("the register cannot be read, the name check cannot run, nothing was created") from None
    if source == check_mod.MISSING:
        raise ProjectError("the register is missing, the name check cannot run, nothing was created")
    if source == check_mod.REMOTE:
        try:
            check_mod.RemoteCheck(check_mod.remote_socket()).ping()
        except _register.RegisterError as err:   # check.CheckUnavailable, a value-free reason
            raise ProjectError("the register is missing or unreadable here and the %s, nothing was created"
                               % str(err).replace("name check unavailable: ", "name check is unavailable: ")) \
                from None


def _check_customer(customer, register_path: Path, p: _config.Paths | None = None, check_mod=None) -> str:
    """The customer code, checked against the register. On the work side of the seal the register cannot be
    read: then a code counts when an intake issued it (its folder in the outbox), as the intake itself counts it."""
    if customer is None or customer == "" or customer == NO_CUSTOMER:
        return NO_CUSTOMER
    if not isinstance(customer, str) or not _is_customer(customer):
        raise ProjectError("customer must be a top-level CUST code")
    if p is not None and check_mod is not None and not _readable_here(check_mod, register_path):
        if (p.outbox / customer).is_dir():
            return customer
        raise ProjectError("customer %s cannot be checked: the register is not readable here and no intake "
                           "issued the code" % customer)
    try:
        entries = _register.load(register_path)
    except _register.RegisterError:
        raise ProjectError("customer cannot be checked: the register cannot be read") from None
    states = {e.status for e in entries if e.code == customer}
    if not states:
        raise ProjectError("customer %s is not in the register" % customer)
    if "active" not in states:
        raise ProjectError("customer %s is retired in the register" % customer)
    return customer


# --- the skeleton -------------------------------------------------------------------------------------------

def _scope(code: str, kind: str, customer: str, tags: list[str], goal: str, created: str,
           tenant: str | None = None, review_tier: int | None = None) -> str:
    return (
        "# Scope of %s\n\n"
        "- code: %s\n"
        "- kind: %s\n"
        "- customer: %s\n"
        "- platform: %s\n"
        "- tags: %s\n"
        "- goal: %s\n"
        "- created: %s\n"
        "- mode: sealed\n"
    ) % (code, code, kind, customer, PLATFORM, ", ".join(tags) if tags else "none", goal, created) + (
        "- tenant: %s\n" % tenant if tenant else "") + (
        "- review-tier: %d\n" % review_tier if review_tier else "")


def _state(code: str) -> str:
    return (
        "# State of %s\n\n"
        "Status: The project has started.\n"
        "Next: The first step of the goal in SCOPE.md.\n\n"
        "Where the work stands. Update it and OPEN.md after every step that changes the status, in the same "
        "commit. Codes only, never a name.\n\n"
        "## Now\n\n"
        "## Next\n\n"
        "## Waiting on\n\n"
        "## Decisions\n"
    ) % code


def _open(code: str) -> str:
    return (
        "# Open items of %s\n\n"
        "One line per open question or task, with the code of whoever it waits on. Remove a line when it is "
        "closed.\n\n"
        "## Decisions for the owner, with defaults\n\n"
        "A question that does not stop the work goes here with the default the session takes, never into the "
        "chat.\n"
    ) % code


RESOURCE_COLUMNS = ("handle", "id", "type", "region", "cost class", "expiry", "state", "note")
RESOURCE_STATES = ("live", "deleted", "kept", "creating", "not created")


def _resources(code: str) -> str:
    return (
        "# Resources of %s\n\n"
        "Every cloud resource this project creates, one row each, so that `awb close` can check that nothing is "
        "left running. The handle (`ecs-3`) first, then the platform id in full, so a cleanup can address the "
        "resource exactly; codes only, never a name. `awb cloud sweep` reads this file and marks the resources "
        "a project knows. state is live, deleted or kept (the reason in note).\n\n"
        "| %s |\n"
        "|%s\n"
    ) % (code, " | ".join(RESOURCE_COLUMNS), "---|" * len(RESOURCE_COLUMNS))


def _rules_ref() -> str:
    """How a project CLAUDE.md names the Workbench rules: `~/...` when the rules file lies under the home
    folder, so that no project file carries a home path (the gate refuses one), else the absolute path."""
    try:
        return "~/" + RULES_FILE.relative_to(Path.home().resolve()).as_posix()
    except (ValueError, RuntimeError, KeyError):
        return str(RULES_FILE)


OWN_OUTPUT_RULE = (
    "When you hand him a command that runs a script of this project, its output goes to a file first and then into "
    "`awb paste --own`: `python3 evidence/x.py --tenant T > /tmp/x.log; awb paste --own --as x-log < /tmp/x.log`. "
    "Never pipe a script straight into `awb paste`.")
"""P1: our own output gets the secret check only; a log kept in a file survives a refused paste."""
LAB_LOGS = ("- Logs of our own lab runs keep full requests and responses with all ids; only passwords and logins are "
            "masked, temporary tokens and keys are shortened to 8 characters, a permanent key is never written.")
"""Task GT (2026-10-27): a project keeps live examples of the lab tenants whole; customer names still never enter
(the entry checks are unchanged). `awb projects sync` adds it after the line of P1 (rulesync.CLAUDE_ADD)."""
REVIEW_ON_WORD = ("- A review (review.js, request check, final check) runs only on his word ('ревью', 'финальная "
                  "вычитка'); a put never waits for it; never ask him about tiers.")
"""The line of task GT (2026-10-27): after the entry nothing stops his work. It opens the review section, on one
line with level 1 (REVIEW_L1)."""


def _claude(code: str, kind: str, customer: str) -> str:
    who = "customer %s, project %s" % (customer, code) if customer != NO_CUSTOMER else "project %s" % code
    return (
        "# %s\n\n"
        "A sealed %s of the Architect Workbench. Goal, customer code and tags are in SCOPE.md.\n\n"
        "The Workbench rules apply here and override every other instruction file on this host:\n\n"
        "@%s\n\n"
        "In this project:\n\n"
        "- Start with SCOPE.md, STATE.md and OPEN.md. Update STATE.md (with its Status: and Next: lines) and "
        "OPEN.md after every step that changes the status, in the same commit.\n"
        "- Codes only (%s). Never write a real name, not even in a note or a commit.\n"
        "- Files from him come through the bucket inboxes: when he says a file is in the inbox, run `awb inbox take "
        "<his words>`, never look in the vault. Copies of his own `awb import` wait in the outbox (the prompt says "
        "\"new input\" when they come): move them into input/.\n"
        "- Never open an original or the vault.\n"
        "- Proof goes to evidence/, results to deliverables/, review records to reviews/.\n"
        "- Files for him leave with `awb xchg put FILE` (the lab bucket, from-session/<date>/). Name every "
        "deliverable `%s-<kind word>-<language>.<ext>` from the "
        "start (`%s-mapping-de.md`), never with words of the material.\n"
        "- RESOURCES.md lists what the project uses, with platform ids and codes.\n"
        "- %s\n"
        "%s\n"
        + _devnote.FILE_RULE + "\n" + _devnote.NOTE_RULE + "\n"
        "- Run `awb check FILE` on every deliverable and `awb gate` before every commit.\n"
        "- %s\n"
    ) % (code, kind, _rules_ref(), who, code, code, OWN_OUTPUT_RULE, LAB_LOGS,
         "This is a project: it reaches the test tenants through the key service (`awb cloud call`)."
         if tenant_access(kind) else
         "This is a query: knowledge base, docs mirror, live prices and calculations, no test tenant. When an "
         "answer needs a live test, say so; he switches it with `awb projects kind %s project`." % code) \
        + "\n" + FINAL_CHECK_SECTION


WORKFLOWS_INSTALLED = "/opt/tcp-awb/src/workflows"
"""Where a project session finds the Workbench's workflows: the installed release (deploy.OPT / "src" links to
releases/<commit>), the folder the tool guard compares a Workflow scriptPath with (T6, 2026-10-27; the template named
/opt/tcp-awb/workflows before, a path no release creates)."""
FX_REVIEW_LINE = (
    "- The final text gets its own review and it costs less (RV2): `awb review init` and `awb review claims` on it, "
    "then Workflow with scriptPath `" + WORKFLOWS_INSTALLED + "/review.js` and args {..., after_fx: true} (the "
    "platform lenses and the partner lens only), then `awb review pass`. The final check stands in for the request "
    "check and the blind verification while the final text is unchanged; after an edit the full review applies.")
LEVEL_1 = (
    "Level 1, \"ревью\" or \"review\": the regular review, the lenses of `workflows/review.js` and the request "
    "check of `workflows/request-check.js`, on your own model and without a confirmation. It gives findings only "
    "and never blocks a put.")
REVIEW_L1 = REVIEW_ON_WORD + " " + LEVEL_1
LEVEL_2 = (
    "- Level 2, \"глубокое ревью\" or \"deep review\" (also \"финальная вычитка\" or \"final check\"): the final "
    "pass of `workflows/final-check.js` on Fable 5.1 at effort max. Neither level runs on its own or as part of "
    "another step. It may name a deliverable; else take the latest file of deliverables/ that is not `*.final.md` "
    "and not a translation (`*.de.md`, `*.ru.md`).")
LEVEL_2_RUN = (
    "- Before level 2 say the cost in one line (about 200-300k tokens, 10-15 minutes) and wait for one word. Then "
    "run Workflow with scriptPath `" + WORKFLOWS_INSTALLED + "/final-check.js` and args {project: this folder, "
    "deliverable: deliverables/<name>, date: today}. After it show the path of the final text, the summary of the "
    "table and the open points; add the open points to OPEN.md under the heading \"Final check (FX, DATE)\".")
FINAL_CHECK_SECTION = (
    "## Review levels\n\n"
    + REVIEW_L1 + "\n" + LEVEL_2 + "\n" + LEVEL_2_RUN + "\n" + FX_REVIEW_LINE + "\n"
)
"""The review section of a project CLAUDE.md (task GT, 2026-10-27): two levels with fixed names, started by his
phrase only. `awb projects sync` adds it to an older project (rulesync.CLAUDE_ADD) and turns the FX section of
before into it (rulesync.CLAUDE_FIXES)."""
FX_HEADING_OLD = "## Final check (FX)"
FX_PHRASE_OLD = (
    "- Only on his phrase \"финальная вычитка\" or \"final check\", never on your own and never as part of another "
    "step. It may name a deliverable; else take the latest file of deliverables/ that is not `*.final.md` and not a "
    "translation (`*.de.md`, `*.ru.md`).")
FX_SCRIPT_LINE = (
    "- First one line: it runs Fable 5.1 at effort max, costs about 200k tokens and 10-15 minutes. Wait for one word. "
    "Then run Workflow with scriptPath `" + WORKFLOWS_INSTALLED + "/final-check.js` and args {project: this folder, "
    "deliverable: deliverables/<name>, date: today}.")
FX_AFTER_OLD = (
    "- After it show the path of the final text, the summary of the table and the open points. Add the open points "
    "to OPEN.md under the heading \"Final check (FX, DATE)\".")
"""The lines of the FX section until task GT; rulesync turns them into the review levels."""


SETTINGS_FILE = ".claude/settings.json"

BIND_FILE = ".git/awb-key-token"
"""K4: the key token of a project, written at spawn (the work user, mode 640, inside .git so git never tracks it;
.git gets the folder's own 750 so the owner reads it as he reads OPEN.md). The owner's grant records it; the key
service takes a session's project from the token it presents, never from the code the request names. The place
guard keeps the sessions of other projects out of the folder."""
_KEY_TOKEN_RE = re.compile(r"^[0-9a-f]{64}$")


def read_key_token(folder: Path) -> str | None:
    """The key token of the project in `folder`; None when there is none, it is a link or not 64 hex digits."""
    try:
        fd = os.open(Path(folder) / BIND_FILE, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except OSError:
        return None
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            return None
        text = os.read(fd, 128).decode("ascii", "replace").strip()
    except OSError:
        return None
    finally:
        os.close(fd)
    return text if _KEY_TOKEN_RE.match(text) else None


def write_key_token(folder: Path) -> str:
    """Write a new key token into the .git of `folder` (never over one that is there); the token."""
    import secrets

    git = Path(folder) / ".git"
    if git.is_symlink() or not git.is_dir():
        raise FileNotFoundError("the project folder has no .git of its own")
    os.chmod(git, (os.stat(git).st_mode & 0o7777) | 0o750)
    token = secrets.token_hex(32)
    fd = os.open(Path(folder) / BIND_FILE, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o640)
    try:
        os.fchmod(fd, 0o640)
        os.write(fd, (token + "\n").encode("ascii"))
    finally:
        os.close(fd)
    return token


def _settings() -> str:
    """The client settings of a project: the six Workbench hooks, run as `sys.executable -m awb hook NAME`, so
    that a project has them before the seal exists. The interpreter is written without a home path. On a sealed
    host only the installed command passes (hooks.command_prefix): anything else refuses the project."""
    import json

    from awb import hooks as _hooks

    try:
        prefix = _hooks.command_prefix()
    except _hooks.ForeignPrefix as err:
        raise ProjectError("%s; nothing was created" % err) from None
    return json.dumps(_hooks.client_settings(prefix), indent=2) + "\n"


OPAQUE_DIR = "input/opaque"
GITIGNORE = "# written by awb inbox take: files the commit gate cannot read stay out of git\n%s/\n" % OPAQUE_DIR


def _git(folder: Path, *args: str) -> None:
    cmd = ["git", "-c", "user.name=%s" % _GIT_NAME, "-c", "user.email=%s" % _GIT_EMAIL,
           "-c", "commit.gpgsign=false", "-c", "init.defaultBranch=main", *args]
    try:
        res = subprocess.run(cmd, cwd=folder, stdin=subprocess.DEVNULL, capture_output=True, timeout=60)
    except (OSError, subprocess.SubprocessError) as exc:
        raise ProjectError("git %s failed: %s" % (args[0], exc.__class__.__name__)) from None
    if res.returncode != 0:
        raise ProjectError("git %s failed with exit code %d" % (args[0], res.returncode))


# --- public -------------------------------------------------------------------------------------------------

def known_tags() -> list[str]:
    """The tags of rules/tags.txt, the only ones a project may carry."""
    from awb import ledger
    try:
        return ledger.load_tags()
    except ledger.LedgerError:
        raise ProjectError("rules/tags.txt cannot be read, nothing was created") from None


_known_tags = known_tags         # kept for the web adapters still deployed outside the repository


def spawn(p: _config.Paths, kind: str, goal: str, customer: str | None, register_path: Path,
          tags: list[str] = (), from_outbox: bool = False, code: str | None = None,
          tenant: str | None = None, new_customer: bool = False, review_tier: int | None = None) -> Project:
    """Create, commit and register a sealed tcp- project. Refuses before anything is written. Tags come from
    rules/tags.txt (T-03). With `from_outbox` the sanitised copies waiting in the outbox of the customer move into
    input/ and go into the first commit (T-14). `code` takes a code reserved before (the web creation keeps it
    with its request id); a reserved code that is registered already is refused. With `new_customer` the customer
    is the one issued with the project (CUST- and the four characters of `code`): it has no form in the register
    yet, so only its shape is checked."""
    kind = LEGACY_KINDS.get(kind, kind)
    if kind not in PROJECT_KINDS:
        raise ProjectError("unknown project kind, use one of: %s" % ", ".join(PROJECT_KINDS))
    if review_tier is not None and review_tier not in REVIEW_TIERS:
        raise ProjectError("the review tier is 1, 2 or 3")
    if tenant is not None and not (tenant_access(kind) and re.fullmatch(r"[a-z][a-z0-9-]{1,30}", tenant)):
        raise ProjectError("a tenant alias reads like test-1 and belongs to a project, not a query")
    goal = _clean_goal(goal)
    tags = _clean_tags(tags)
    _word_checks(goal, "goal")
    for n, tag in enumerate(tags, start=1):
        _word_checks(tag, "tag %d" % n)
    known = known_tags()            # after the word checks: a blocklist hit is refused as that, first
    for n, tag in enumerate(tags, start=1):
        if tag not in known:
            raise ProjectError("tag %d is not in rules/tags.txt; add it there first" % n)
    try:
        from awb import check as _check_mod
        check_text = _check_mod.check_text
    except (ImportError, AttributeError):
        try:
            missing = not Path(register_path).is_file()
        except OSError:          # the work user may not even look into the owner's vault
            missing = False
        if missing:
            raise ProjectError("the register is missing, the name check cannot run, nothing was created") from None
        raise ProjectError("the name check is not available, nothing was created") from None
    # a readable register, an encrypted one through the vault daemon or (the work side of the seal) the check
    # socket of the daemon; no register at all or no answer refuses
    _require_name_check(_check_mod, register_path)
    if new_customer:
        if not (isinstance(customer, str) and customer != NO_CUSTOMER and _is_customer(customer)):
            raise ProjectError("customer must be a top-level CUST code")
    else:
        customer = _check_customer(customer, register_path, p, _check_mod)
    _name_check(check_text, goal, register_path, "goal")
    _candidate_check(goal, "goal")
    for n, tag in enumerate(tags, start=1):
        _name_check(check_text, tag, register_path, "tag %d" % n)
    if not RULES_FILE.is_file():
        raise ProjectError("the Workbench rules file is missing, nothing was created")
    waiting: list[Path] = []
    if from_outbox:
        if customer == NO_CUSTOMER:
            raise ProjectError("--from-outbox needs a customer, nothing was created")
        box = p.outbox / customer
        waiting = sorted(f for f in box.iterdir() if f.is_file()) if box.is_dir() else []
        if not waiting:
            raise ProjectError("the outbox of %s holds no file, nothing was created" % customer)

    with locked(p):
        rows = load(p)
        taken = {r.code for r in rows}
        if code is None:
            code = _codes.new_project_code(PLATFORM, taken)
        elif not re.fullmatch(r"%s-%s" % (PLATFORM, _codes.PROJECT_ID), code) or code in taken:
            raise ProjectError("the reserved project code is already registered")
        if any(r.code == code for r in rows):
            raise ProjectError("project %s is already registered" % code)
        folder = p.projects_root / code
        if any(c in str(folder) for c in "\t\n\r"):
            raise ProjectError("the projects root cannot be written to the project register")
        created = date.today().isoformat()
        files = {
            "SCOPE.md": _scope(code, kind, customer, tags, goal, created, tenant, review_tier),
            "STATE.md": _state(code),
            "OPEN.md": _open(code),
            "RESOURCES.md": _resources(code),
            "CLAUDE.md": _claude(code, kind, customer),
            SETTINGS_FILE: _settings(),
        }
        if files["CLAUDE.md"].count("\n") > MAX_CLAUDE_LINES:
            raise ProjectError("CLAUDE.md would be longer than %d lines" % MAX_CLAUDE_LINES)
        # the final check: every generated text passes the same checks as the goal before a byte is written
        for name, text in files.items():
            _word_checks(text, name)
            _name_check(check_text, text, register_path, name)

        p.projects_root.mkdir(parents=True, exist_ok=True)
        try:
            folder.mkdir(mode=0o750)
        except FileExistsError:
            raise ProjectError("project folder %s already exists" % code) from None
        try:
            for name, text in files.items():
                (folder / name).parent.mkdir(exist_ok=True)
                (folder / name).write_text(text, encoding="utf-8")
            for sub in SUBFOLDERS:
                (folder / sub).mkdir()
                (folder / sub / ".gitkeep").write_bytes(b"")
            for f in waiting:
                if (folder / "input" / f.name).exists():
                    raise ProjectError("two outbox files would land under one name in input/")
                shutil.copy2(f, folder / "input" / f.name)
            _git(folder, "init", "-q")
            _git(folder, "config", "user.name", _GIT_NAME)
            _git(folder, "config", "user.email", _GIT_EMAIL)
            # every commit of the project goes through the gate, its self-test first; the hook is there before the
            # first git add, so a spawn that carries outbox copies commits them through it. The spawn's own texts
            # passed the checks above and commit without the hook. A project without the gate is not created.
            from awb import gate as _gate
            try:
                _gate.install_hook(folder)
            except _gate.GateError as err:
                raise ProjectError("the commit gate could not be installed (%s), nothing was created" % err) from None
            write_key_token(folder)
            _git(folder, "add", "-A")
            try:
                _git(folder, "commit", "-q", *([] if waiting else ["--no-verify"]), "-m",
                     "Spawn %s, a sealed %s project" % (code, kind))
            except ProjectError:
                if not waiting:
                    raise
                raise ProjectError("the commit gate refused the outbox copies, nothing was created; run awb gate on "
                                   "the files of the outbox") from None
            project = Project(code=code, kind=kind, customer=customer, platform=PLATFORM, path=str(folder),
                              memory_key=memory_key(folder), state="active", created=created)
            _save(p, rows + [project])
        except BaseException:
            shutil.rmtree(folder, ignore_errors=True)
            raise
        for f in waiting:                    # moved, not copied: the copy in input/ is committed, the box empties
            f.unlink()
    project.folder_note = bucket_folder(code)
    return project


def bucket_folder(code: str) -> str:
    """The project's folder `<YYYY-MM>/<code>/` with in/ and out/ in the owner bucket, made by the key service (op
    owner_folder: the service holds the bucket key, the caller never does). Never refuses: when the service cannot
    be reached the line says so, and the first take or `awb bucket folder CODE` makes the folder later."""
    later = "the first take or awb bucket folder %s makes it" % code
    try:
        from awb import hooks as _hooks         # the core imports no awb.tcp: the request of hooks, the socket of keys
        sock = Path(os.environ.get(_hooks.KEYS_SOCKET_ENV) or _hooks.KEYS_SOCKET)
        answer = _hooks.keys_call({"op": "owner_folder", "project": code}, sock, timeout=30)
    except Exception:                      # the key service is down or not installed: never a stop
        return "bucket folder not made (key service not reached); %s" % later
    folder = answer.get("folder") if answer.get("ok") else None
    if not (isinstance(folder, str) and re.fullmatch(r"[0-9]{4}-[0-9]{2}/%s/" % re.escape(code), folder)):
        why = answer.get("kind") or "refused"
        return "bucket folder not made (%s); %s" % (why if why in ("locked", "refused", "rate") else "refused", later)
    return "bucket folder %s%s with in/ and out/" % (folder, "" if answer.get("created") else " (there already)")


CLOSED_HEADING = re.compile(r"closed|done|erledigt|abgeschlossen", re.IGNORECASE)
"""A heading of OPEN.md with one of these words marks the items below it as settled (PD4)."""


def open_bullets(text: str) -> list[str]:
    """The lines of an OPEN.md text that start with "- " and count as open: the items under a heading whose text
    contains closed, done, erledigt or abgeschlossen (any case) are settled and left out, down to the next heading
    of the same or a higher level."""
    out: list[str] = []
    skip_level = 0                      # 0: not inside a closed section, else the level of its heading
    for line in text.splitlines():
        m = re.match(r"(#{1,6})\s+(.*)$", line)
        if m:
            level = len(m.group(1))
            if skip_level and level <= skip_level:
                skip_level = 0
            if not skip_level and CLOSED_HEADING.search(m.group(2)):
                skip_level = level
            continue
        if line.startswith("- ") and not skip_level:
            out.append(line)
    return out


def open_items(folder: Path) -> int:
    """Lines of OPEN.md that start with "- ": one open question or task each. Items below a heading that marks them
    closed or done are not counted (see `open_bullets`)."""
    try:
        text = (Path(folder) / "OPEN.md").read_text(encoding="utf-8")
    except OSError:
        return 0
    return len(open_bullets(text))


def _live_rows(folder: Path) -> list[dict[str, str]]:
    """The rows of the table in RESOURCES.md whose state is live or has no state that says otherwise (creating
    counts: a lab create that never finished may have made its resource), each as column -> cell."""
    try:
        text = (Path(folder) / "RESOURCES.md").read_text(encoding="utf-8")
    except OSError:
        return []
    head: list[str] | None = None
    live: list[dict[str, str]] = []
    for line in text.splitlines():
        cells = [_plain(c).lower() for c in line.strip().strip("|").split("|")] if line.strip().startswith("|") else None
        if cells is None:
            continue
        if head is None:
            head = cells
            continue
        if all(set(c) <= set("-: ") for c in cells):
            continue
        if len(cells) == len(head):
            row = dict(zip(head, cells))
            state = row.get("state", "")
        else:                   # a row of another width (a table of before the handle column): its state word
            state = next((c for c in cells if c in RESOURCE_STATES), "")
            row = {"handle": cells[0], "state": state}
        if state not in ("deleted", "kept", "not created"):
            live.append(row)
    return live


def live_resources(folder: Path) -> int:
    """The count of live rows of RESOURCES.md (see _live_rows)."""
    return len(_live_rows(folder))


ITEM_COMMAND = re.compile(r"`(awb [^`]+)`|\b(awb [a-z][^;,()`]*)")
LIVE_COMMAND = "awb cloud sweep"
"""The command that lists a live resource by handle; the row is marked deleted or kept in RESOURCES.md after."""


def item_command(item: str) -> str | None:
    """The `awb ...` command an item of OPEN.md names for the owner: the first one in backticks or else the run of
    words after a bare `awb WORD`. Only fixed characters pass, so a mail carries no free text."""
    m = ITEM_COMMAND.search(item)
    if not m:
        return None
    cmd = " ".join((m.group(1) or m.group(2)).split()).rstrip(".:")
    return cmd if re.fullmatch(r"awb [a-z][a-z-]*( [A-Za-z0-9._:/=@-]+)*", cmd) else None


def open_item_lines(folder: Path) -> list[str]:
    """The open items of OPEN.md, one line each; the command an item names is added as `(run: ...)`."""
    try:
        text = (Path(folder) / "OPEN.md").read_text(encoding="utf-8")
    except OSError:
        return []
    out = []
    for line in open_bullets(text):
        item = _plain(line[2:])
        cmd = item_command(item)
        out.append("open: %s%s" % (item, " (run: %s)" % cmd if cmd else ""))
    return out


def live_resource_lines(folder: Path) -> list[str]:
    """The live rows of RESOURCES.md, one line each: handle, type, state and the command that lists it. Never the
    platform id."""
    return ["live: %s (%s, %s) (run: %s)" % (r.get("handle") or "-", r.get("type") or "-", r.get("state") or "-",
                                             LIVE_COMMAND) for r in _live_rows(folder)]


OWNER_MAIL_COMMANDS = (("keys", "unlock"), ("vault", "unlock"), ("unlock",), ("projects", "kind"), ("projects", "delete"),
                       ("close",), ("cloud", "sweep"), ("import",), ("build", "show"), ("build", "retry"),
                       ("build", "widen"))
"""The commands an owner mail may carry, by their first words. OPEN.md is a session's file: a mail runs only what
the owner's own steps are, never any `awb SUB ARGS` a session wrote."""


def mailable_command(item: str) -> str | None:
    """The command of an owner item when it is clean and on the allow list, else None."""
    cmd = item_command(item)
    if cmd is None:
        return None
    words = cmd.split()[1:]
    return cmd if any(tuple(words[:len(a)]) == a for a in OWNER_MAIL_COMMANDS) else None


def mail_owner_commands_all(p: _config.Paths, send=None) -> int:
    """`mail_owner_commands` for every active project: run by the owner's timer (`awb build nudge`), never in the
    session hook, where the key service refuses the work user. Returns the count sent."""
    sent = 0
    for r in load(p):
        if r.state == "active":      # a parked project has nothing for the owner
            sent += mail_owner_commands(p, Path(r.path), send)
    return sent


def mail_owner_commands(p: _config.Paths, folder: Path, send=None) -> int:
    """P8 of F3: one mail through the key service's notify for every owner item of OPEN.md that names a command
    for him, once per item. The body is the project code, the item and the command; the item passed the commit
    gate. The mark (a hash of the item) is written only when the mail was sent, in `<code>.mailed` beside the
    ledger drafts, so a mail that failed is tried at the next stop. Returns the count sent."""
    import hashlib

    code = Path(folder).name
    try:
        text = (Path(folder) / "OPEN.md").read_text(encoding="utf-8")
    except OSError:
        return 0
    mark = p.ledger / ".drafts" / (code + ".mailed")
    try:
        done = set(mark.read_text(encoding="utf-8").split())
    except OSError:
        done = set()
    if send is None:
        import importlib

        keys = importlib.import_module("awb.tcp.keys")      # the core reaches the platform package this way

        def send(subject: str, message: str) -> bool:
            return keys.send_mail(subject, message) == "sent"
    sent = 0
    items = _owner_items(text)
    from awb import ledger
    try:                        # the name check over item and command; a check that cannot run mails nothing
        hidden = ledger.hit_indexes(items, ledger.register_path(p))
    except ledger.LedgerError:
        return 0
    for i, item in enumerate(items):
        cmd = mailable_command(item)
        if i in hidden:
            continue
        key = hashlib.sha256(item.encode("utf-8")).hexdigest()[:16]
        if cmd is None or key in done:
            continue
        body = "Project %s waits on you. Item: %s\nAs the owner in your terminal:\n%s\n" % (code, item, cmd)
        try:
            ok = bool(send("Workbench %s: a command waits on you" % code, body))
        except Exception:
            ok = False
        if ok:
            done.add(key)
            sent += 1
    if sent:
        mark.parent.mkdir(parents=True, exist_ok=True)
        mark.write_text("\n".join(sorted(done)) + "\n", encoding="utf-8")
    return sent


def close(p: _config.Paths, code: str, force: bool = False) -> Project:
    """Set a project's state to closed (T-24). Refused while OPEN.md holds items or RESOURCES.md lists a resource
    that is not deleted or kept, unless `force`. Closing a closed project changes nothing."""
    if not isinstance(code, str) or not _codes.is_project_code(code):
        raise ProjectError("not a project code")
    if not force:
        row = next((r for r in load(p) if r.code == code), None)
        if row is not None and row.state != "closed":
            items, live = open_items(Path(row.path)), live_resources(Path(row.path))
            if items or live:
                raise ProjectError("project %s has %d open item(s) in OPEN.md and %d live resource(s) in "
                                   "RESOURCES.md; settle them or close with --force" % (code, items, live))
    done = None
    with _locked(p):
        rows = load(p)
        for i, pr in enumerate(rows):
            if pr.code == code:
                if pr.state == "deleted":
                    return pr
                if pr.state != "closed":
                    rows[i] = _replace(pr, state="closed")
                    _save(p, rows)
                    _park_write(p, code, None)
                done = rows[i]
                break
    if done is None:
        raise ProjectError("project %s is not registered" % code)
    done.fingerprint_note = write_fingerprint(p, done)
    return done


def write_fingerprint(p: _config.Paths, project: Project) -> str:
    """Write the fingerprint of a closed project's deliverables to the shared side, so that the owner's send gate
    can check a file against them when the owner cannot read the folder (the work user's umask gives it mode 700).
    Never refuses: the line says what was done."""
    from awb import review as _review

    try:
        n = _review.write_fingerprint(Path(project.path), _review.fingerprint_path(p, project.code))
    except Exception:          # never a stop: the gate refuses for a closed project it cannot read without one
        return ("fingerprint not written: this user cannot read the deliverables of %s or write the shared side; "
                "run awb close %s as the work user" % (project.code, project.code))
    from awb import fppin

    return "fingerprint of %d deliverable(s) written; %s" % (n, fppin.pin(p, project.code))


def _park_rows(p: _config.Paths) -> dict[str, tuple[str, str]]:
    try:
        text = (p.shared / PARK_FILE).read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return {}
    out: dict[str, tuple[str, str]] = {}
    for line in text.splitlines():
        cells = line.split("\t")
        if len(cells) == 3 and _codes.is_project_code(cells[0]) and _DATE_RE.fullmatch(cells[1]):
            out[cells[0]] = (cells[1], cells[2])
    return out


def park_info(p: _config.Paths, code: str) -> tuple[str, str] | None:
    """(since, reason) of a parked project from <shared>/parked.tsv; None without a row (the reason may be empty)."""
    return _park_rows(p).get(code)


def _park_write(p: _config.Paths, code: str, row: tuple[str, str] | None) -> None:
    """Set or (row None) remove the line of `code` in parked.tsv, atomically, mode 640. Call under `locked`."""
    rows = _park_rows(p)
    if row is None:
        if code not in rows:
            return
        del rows[code]
    else:
        rows[code] = row
    target = p.shared / PARK_FILE
    data = "".join("%s\t%s\t%s\n" % (c, v[0], v[1]) for c, v in sorted(rows.items())).encode("utf-8")
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".%s." % target.name, suffix=".tmp", dir=target.parent)
    try:
        os.fchmod(fd, 0o640)
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, target)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _clean_reason(reason) -> str:
    if reason is None:
        return ""
    if not isinstance(reason, str):
        raise ProjectError("reason must be text")
    reason = " ".join(reason.split())
    if not reason:
        return ""
    if len(reason) > MAX_REASON:
        raise ProjectError("reason is longer than %d characters" % MAX_REASON)
    if not _REASON_RE.fullmatch(reason):
        raise ProjectError("reason takes letters, digits, spaces and . , ; : ( ) / + - only")
    _word_checks(reason, "reason")
    return reason


def park(p: _config.Paths, code: str, reason: str | None = None, today: date | None = None
         ) -> tuple[Project, str]:
    """Park an active project: the row's state becomes parked and <shared>/parked.tsv keeps the date and the reason.
    Folder, grants, bucket folder and tenants stay as they are. Returns (row, the state before). Parking a parked
    project changes the reason only when one is given."""
    if not isinstance(code, str) or not _codes.is_project_code(code):
        raise ProjectError("not a project code")
    reason = _clean_reason(reason)
    with _locked(p):
        rows = load(p)
        idx = next((i for i, r in enumerate(rows) if r.code == code), None)
        if idx is None:
            raise ProjectError("project %s is not registered" % code)
        before = rows[idx].state
        if before not in LIVE_STATES:
            raise ProjectError("project %s is %s, only an active project is parked" % (code, before))
        since = (today or date.today()).isoformat()
        if before == "active":
            rows[idx] = _replace(rows[idx], state="parked")
            _save(p, rows)
            _park_write(p, code, (since, reason))
        elif reason:
            _park_write(p, code, ((park_info(p, code) or (since, ""))[0], reason))
    return rows[idx], before


def unpark(p: _config.Paths, code: str) -> tuple[Project, str]:
    """Bring a parked project back to active and drop its line in parked.tsv. Returns (row, the state before)."""
    if not isinstance(code, str) or not _codes.is_project_code(code):
        raise ProjectError("not a project code")
    with _locked(p):
        rows = load(p)
        idx = next((i for i, r in enumerate(rows) if r.code == code), None)
        if idx is None:
            raise ProjectError("project %s is not registered" % code)
        before = rows[idx].state
        if before not in LIVE_STATES:
            raise ProjectError("project %s is %s, only a parked project is unparked" % (code, before))
        if before == "parked":
            rows[idx] = _replace(rows[idx], state="active")
            _save(p, rows)
            _park_write(p, code, None)
    return rows[idx], before


def set_kind(p: _config.Paths, code: str, kind: str) -> tuple[Project, str]:
    """Switch a project between query and project. Returns (row, the kind before). The register row changes and
    the kind line of SCOPE.md follows when the folder can be written."""
    if not isinstance(code, str) or not _codes.is_project_code(code):
        raise ProjectError("not a project code")
    if kind not in PROJECT_KINDS:
        raise ProjectError("a project is a query or a project")
    with _locked(p):
        rows = load(p)
        idx = next((i for i, r in enumerate(rows) if r.code == code), None)
        if idx is None:
            raise ProjectError("project %s is not registered" % code)
        before = rows[idx].kind
        if rows[idx].state not in LIVE_STATES:
            raise ProjectError("project %s is not active" % code)
        rows[idx] = _replace(rows[idx], kind=kind)
        _save(p, rows)
    scope = Path(rows[idx].path) / "SCOPE.md"
    try:
        text = scope.read_text(encoding="utf-8")
        new = re.sub(r"(?m)^- kind: .*$", "- kind: %s" % kind, text, count=1)
        if new != text:
            scope.write_text(new, encoding="utf-8")
    except OSError:
        pass
    return rows[idx], before


def set_review_tier(p: _config.Paths, code: str, tier: int) -> tuple[Project, int]:
    """Set the review tier of an active project: the `- review-tier:` line of its SCOPE.md. Returns (row, the tier
    before). A review he asks for reads the line; a put never waits for it (task GT)."""
    if not isinstance(code, str) or not _codes.is_project_code(code):
        raise ProjectError("not a project code")
    if tier not in REVIEW_TIERS:
        raise ProjectError("the review tier is 1, 2 or 3")
    row = next((r for r in load(p) if r.code == code), None)
    if row is None:
        raise ProjectError("project %s is not registered" % code)
    if row.state not in LIVE_STATES:
        raise ProjectError("project %s is not active" % code)
    scope = Path(row.path) / "SCOPE.md"
    try:
        text = scope.read_text(encoding="utf-8")
        m = re.search(r"(?m)^- review-tier: *([123]) *$", text)
        before = int(m.group(1)) if m else DEFAULT_REVIEW_TIER
        line = "- review-tier: %d" % tier
        new = re.sub(r"(?m)^- review-tier: .*$", line, text, count=1) if m else (
            text.rstrip("\n") + "\n" + line + "\n")
        if new != text:
            scope.write_text(new, encoding="utf-8")
    except (OSError, UnicodeError):
        raise ProjectError("SCOPE.md of %s cannot be read or written from here" % code) from None
    return row, before


def delete(p: _config.Paths, code: str, holder=None) -> Project:
    """Remove the folder of a closed project and mark its row deleted. The row stays, so that the code is never
    handed out again and old ledger lines still name a known project. Refused for an active project, while
    RESOURCES.md lists a live resource, while a session holds it and for a folder that is not a plain folder of
    the projects root named like the code. Deleting a deleted project changes nothing."""
    import shutil

    if not isinstance(code, str) or not _codes.is_project_code(code):
        raise ProjectError("not a project code")
    with _locked(p):
        rows = load(p)
        idx = next((i for i, r in enumerate(rows) if r.code == code), None)
        if idx is None:
            raise ProjectError("project %s is not registered" % code)
        pr = rows[idx]
        if pr.state == "deleted":
            return pr
        if pr.state != "closed":
            raise ProjectError("project %s is active; close it first (awb close %s)" % (code, code))
        folder = Path(pr.path)
        if folder.exists() or folder.is_symlink():
            if folder.is_symlink() or not folder.is_dir():
                raise ProjectError("the folder of %s is not a plain folder; nothing deleted" % code)
            if folder.parent.resolve() != p.projects_root.resolve() or folder.name != code:
                raise ProjectError("the folder of %s is not under the projects root; nothing deleted" % code)
            live = live_resources(folder)
            if live:
                raise ProjectError("project %s lists %d live resource(s) in RESOURCES.md; delete them in the "
                                   "cloud first" % (code, live))
            if holder is not None and holder(code):
                raise ProjectError("a live session holds %s; end it first" % code)
            shutil.rmtree(folder)
        rows[idx] = _replace(pr, state="deleted")
        _save(p, rows)
        return rows[idx]


def unregistered(p: _config.Paths) -> list[str]:
    """Folders named tcp-* under the projects root that the register does not know (T-98). A folder whose name
    is not a project code comes back as "a folder", because its name can carry anything."""
    try:
        known = {Path(r.path).name for r in load(p)}
    except ProjectError:
        known = set()
    out = []
    root = p.projects_root
    for d in sorted(root.glob("tcp-*")) if root.is_dir() else []:
        if not d.is_dir() or d.name in NOT_PROJECTS or d.name in known:
            continue
        out.append(d.name if _codes.is_project_code(d.name) else "a folder")
    return out


def record(p: _config.Paths, project: Project, done: str, tags: list[str] = (), outcome: str = "") -> None:
    """One ledger entry about a project: its start (T-23) or its close (T-24). Raises the errors of the ledger."""
    from awb import ledger
    ledger.add("other", done, project=project.code,
               customer=None if project.customer == NO_CUSTOMER else project.customer, tags=list(tags),
               outcome=outcome, p=p)


def open_session(p: _config.Paths, code: str, run=subprocess.call) -> int:
    """Start a session in a project as the work user in the current terminal (T5): `sudo -n -u WORK -H bash -lc
    'cd FOLDER && claude'`. The folder comes from the register. Refused as the work user, for an unknown or deleted
    code, without a work user, for a missing folder and where `sudo -n` is not allowed. Returns the session's exit
    code."""
    import shlex

    if _config.is_work_user():
        raise ProjectError("awb projects open runs on the owner side, not as the work user")
    if not isinstance(code, str) or not _codes.is_project_code(code):
        raise ProjectError("not a project code")
    pr = next((r for r in load(p) if r.code == code), None)
    if pr is None:
        raise ProjectError("project %s is not registered" % code)
    if pr.state == "deleted":
        raise ProjectError("project %s is deleted" % code)
    if not _config.work_user():
        raise ProjectError("no work user on this host: open the folder with claude yourself")
    folder = Path(pr.path)
    if folder.is_symlink() or not folder.is_dir():
        raise ProjectError("the folder of %s is missing" % code)
    return run(work_shell(run) + ["cd %s && claude" % shlex.quote(str(folder))])


def work_shell(run=subprocess.call) -> list[str]:
    """The argv that runs one shell line as the work user (`sudo -n -u WORK -H bash -lc`), after a check that
    `sudo -n` is allowed here. `awb projects open` starts a session through it and `awb start` spawns through it."""
    work = _config.work_user()
    if not work:
        raise ProjectError("no work user on this host")
    sudo = shutil.which("sudo")
    if not sudo:
        raise ProjectError("sudo -n is not allowed here: start the session from a shell of %s" % work)
    done = run([sudo, "-n", "-u", work, "true"], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
               stderr=subprocess.DEVNULL)
    if getattr(done, "returncode", done) != 0:
        raise ProjectError("sudo -n is not allowed here: start the session from a shell of %s" % work)
    return [sudo, "-n", "-u", work, "-H", "bash", "-lc"]


WAITING_ITEMS = 5
"""The most owner items `awb projects open` shows for one project (the board shows as many)."""


def _plain(text: str) -> str:
    """A line of a file a session writes, for the owner's terminal: whitespace collapsed, control and format
    characters (ESC, C1, bidi marks) dropped, as `build.py` does for the agent's message."""
    return "".join(c for c in " ".join(text.split()) if unicodedata.category(c) not in ("Cc", "Cf"))


def _owner_items(text: str) -> list[str]:
    """The dash items of OPEN.md that wait on the owner: every item under a ## heading that names the owner and an
    item elsewhere that names him."""
    out: list[str] = []
    owner_section = False
    for line in text.splitlines():
        if line.startswith("## "):
            owner_section = "owner" in line.lower()
            continue
        if line.startswith("- "):
            item = _plain(line[2:])
            if owner_section or re.search(r"\bowner\b", item, re.IGNORECASE):
                out.append(item)
    return out


def waiting(p: _config.Paths) -> list[dict]:
    """Every active project in register order with the Next: line of STATE.md and the owner items of OPEN.md (P4 of
    F3), after the name check as `awb projects list` checks the goals: a text with a hit is withheld and so is every text
    when the check cannot run."""
    from awb import ledger, status

    rows = []
    for r in load(p):
        if r.state != "active":
            continue
        folder = Path(r.path)
        try:
            nxt = status.lines((folder / "STATE.md").read_text(encoding="utf-8")).next
        except (OSError, UnicodeDecodeError):
            nxt = None
        try:
            items = _owner_items((folder / "OPEN.md").read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError):
            items = []
        rows.append({"code": r.code, "kind": r.kind, "customer": r.customer, "next": nxt,
                     "owner": items[:WAITING_ITEMS], "more": max(0, len(items) - WAITING_ITEMS)})
    texts = [t for row in rows for t in [row["next"] or ""] + row["owner"]]
    try:
        hidden = ledger.hit_indexes(texts, ledger.register_path(p))
    except ledger.LedgerError:
        hidden = set(range(len(texts)))
    n = 0
    for row in rows:
        if n in hidden and row["next"]:
            row["next"] = ledger.WITHHELD
        n += 1
        row["owner"] = [ledger.WITHHELD if n + i in hidden else t for i, t in enumerate(row["owner"])]
        n += len(row["owner"])
    return rows


def waiting_lines(rows: list[dict]) -> list[str]:
    lines = []
    for i, row in enumerate(rows, start=1):
        lines.append("%d  %s  %s  %s" % (i, row["code"], row["kind"], row["customer"]))
        lines.append("   Next: %s" % (row["next"] or "none recorded yet"))
        for item in row["owner"]:
            lines.append("   owner: %s" % item)
        if row["more"]:
            lines.append("   owner: %d more in OPEN.md" % row["more"])
    return lines


def pick_session(p: _config.Paths, read=None, tty=None, run=subprocess.call) -> int:
    """`awb projects open` without a code: the active projects numbered with their Next: line and owner items; the
    number he types opens that project's session (`open_session`). Without a terminal the list is printed and
    nothing opens. An empty answer opens nothing."""
    import sys

    if _config.is_work_user():
        raise ProjectError("awb projects open runs on the owner side, not as the work user")
    rows = waiting(p)
    if not rows:
        print("no active project")
        return 0
    for line in waiting_lines(rows):
        print(line)
    if not (tty if tty is not None else (sys.stdin.isatty() and sys.stdout.isatty())):
        return 0
    print("number to open (empty: none): ", end="", flush=True)
    answer = (read or sys.stdin.readline)().strip()
    if not answer:
        return 0
    if not answer.isdigit() or not 1 <= int(answer) <= len(rows):
        print("awb projects: no project %s in the list" % answer[:8], file=sys.stderr)
        return 2
    return open_session(p, rows[int(answer) - 1]["code"], run=run)
