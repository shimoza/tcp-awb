"""Client hooks of working sessions: `awb hook prompt|pre-write|post-write|stop|session-start`.

Each hook reads the hook JSON of the client from standard input and answers the way the client expects:

    prompt          UserPromptSubmit   once per change: the notice that the rules the session loaded changed
                                       (awb/rulesync.py), as additional context of a clean prompt.
                                       Exit 2 when the prompt carries a registered name, structured data or a
                                       secret of the gate's classes (the client drops the prompt and shows the
                                       message to him). Exit 2 as well when the name check cannot run (a locked
                                       vault, no vault daemon, no register, no check module or any other failure
                                       of the check): the Workbench is locked, nothing reaches the model, and the
                                       message says since when and that the owner unlocks it (T3). The rate
                                       limit of the vault daemon is not a locked vault: the hook waits up to
                                       HOOK_RATE_WAIT seconds and then blocks with "send it again in a minute".
                                       The check reads what hides behind a base64 or hex block and under a bidi
                                       override as well. Structured data of SOFT_CLASSES (url, phone, ip, mac)
                                       inside a pasted block (`<pasted_content>`) passes when the typed text has
                                       none, and in a background agent's result or a CI event (the whole prompt
                                       is the client's `<task-notification>` or `<ci-monitor-event>`): the model
                                       gets a context line with the counts instead (D-T8). A registered name, a
                                       secret and the other classes still block; an event is refused with the
                                       agent id and the classes
    pre-write       PreToolUse         exit 2 when Write, Edit, MultiEdit or NotebookEdit targets a file inside
                                       the knowledge base, also through a link or a hard link to one of its
                                       files: facts go in through `awb kb add`, `amend` and `retire` only. A path
                                       the hook cannot read blocks too. What gets around it (a shell command) is
                                       caught by the commit hook of the knowledge base, `awb kb verify --staged`
    pre-tool        PreToolUse         exit 2 when Read, Edit, MultiEdit, NotebookEdit, Write, Glob, Grep or Bash
                                       reaches another project folder, the client's folder (~/.claude) or a key
                                       folder (~/.ssh, ~/.config), or a folder that holds one of them: the file
                                       argument, the path of a search, and for Bash every absolute or
                                       home-relative path of the command line and every relative one with `..`,
                                       each as written and through its links. The own project, <shared>, the
                                       knowledge base, /tmp and the installed code pass. One line names the class
                                       of the place, never the path, and one line goes into
                                       <shared>/sessions/guard.log. Fails closed: no project folder, an input it
                                       cannot read or an internal error refuse the call
    post-write      PostToolUse        exit 2 when the written file carries a hit of the name check, a secret, a
                                       bidi override or (under a deliverables folder at any depth) a blocking
                                       tell of the writing check: class and line only; also when a folder on its
                                       path carries a registered name. Also exit 2 when the name check cannot
                                       run (with the time of the lock and the unlock) or the file is over
                                       MAX_WRITE_BYTES (MAX_DELIVERABLE_BYTES under deliverables): the file is
                                       written (the tool ran) and the session is told that it is unchecked
    selftest        (none)             plants refused cases and a pass in a throw-away home; exit 1 when a
                                       planted refusal passes
    stop            Stop               exit 2 with "run the review for: ..." while a deliverable has no valid
                                       review record, and while STATE.md lags behind the project's commits or
                                       lacks its Status: and Next: lines (awb/status.py); exit 0 when
                                       `stop_hook_active` is set (no loop). Before
                                       that, silently: a ledger entry drafted from the new commits of the project
                                       (T-41, at most every DRAFT_HOURS) and the "English note:" of the last reply
                                       kept in the list of notes (T-48), the questions count and the refused
                                       tool calls of the project and the day (awb/questions.py)
    session-start   SessionStart       JSON with `additionalContext`: SCOPE.md, the first 60 lines of STATE.md,
                                       OPEN.md, the count of expired knowledge entries and the days since the last
                                       career update when that is over 90. A section is withheld when it carries
                                       any hit of the name check. When the name check cannot run at all the
                                       whole context is one line, that the Workbench is locked, and no file is
                                       loaded. It also claims the project for the session (T-62) and says when
                                       another live session holds it, when the project is idle (close it) and
                                       when STATE.md is over its size (move older layers to history/).
                                       On a sealed host it first takes the receipt: the hooks of the managed
                                       settings run the installed command, the project's repository carries
                                       the commit gate, the vault daemon answers a check, the key service a
                                       ping, the rules are the installed ones and the host mode is known. The
                                       context opens with "guards: N of N active (<release>)"; a missing guard
                                       makes the whole context one line naming it (never a path), and every
                                       prompt is refused with that line until the guards pass. An owner session
                                       gets the receipt line alone, without the project repository, and is not
                                       refused. Each start is a line in <shared>/sessions/receipts.tsv

The platform of a project (tcp or hcs, which decides whether vendor names block in a deliverable) comes from
the prefix of its project code, the name of its folder. A line in SCOPE.md cannot change it.

The name check is `check.check_text` (it goes to the vault daemon when the register cannot be read here). The
writing check, the review status, the knowledge base and the career log live in their own modules. Each is
imported only when a hook needs it; a module that is missing or fails makes its part of the hook step back with
a warning and never crashes a hook. The name check is the exception: without it the prompt and the written file
are refused and the start loads no file.

Exit codes follow the client: 0 go on, 2 block. A usage error or an unexpected error exits 1, which the client
shows without blocking. A hook that runs longer than HOOK_BUDGET seconds stops itself and fails closed (exit 2
for the blocking hooks), before the client's own limit would cancel it and let the input through unchecked.
Nothing here prints a matched value, a form or a code of the register: messages carry counts, classes, line
numbers and file names that passed the name check.
"""
from __future__ import annotations

import datetime
import subprocess

import importlib
import json
import os
import pwd
import re
import shlex
import signal
import sys
import threading
import time
from datetime import date
from pathlib import Path

from awb import config

HOOKS = ("prompt", "pre-write", "pre-tool", "post-write", "stop", "session-start", "selftest")
EVENTS = {
    "prompt": "UserPromptSubmit",
    "pre-write": "PreToolUse",
    "pre-tool": "PreToolUse",
    "post-write": "PostToolUse",
    "stop": "Stop",
    "session-start": "SessionStart",
}
WRITE_MATCHER = "Write|Edit|MultiEdit|NotebookEdit"
"""The tools whose target file the pre-write hook guards and whose written file the post-write hook checks."""

OK = 0
ERROR = 1
BLOCK = 2

STATE_LINES = 60
SECTION_CHARS = 5000
"""At most this many characters of one file go into the context of a starting session."""
START_CHARS = 12000
"""At most this many characters of files go into the context of a starting session; the checks always follow."""
CAREER_DUE_DAYS = 90
DRAFT_HOURS = 2
_SNIFF = 8192
_SCOPE_MAX_DEPTH = 40
HOOK_RATE_WAIT = 20.0
HOOK_BUDGET = 50.0
"""Seconds a hook may run before it stops itself and fails closed; under the client's default limit of 60."""
MAX_WRITE_BYTES = 4 * 1024 * 1024
MAX_DELIVERABLE_BYTES = 1024 * 1024
"""The largest written file the write-time check reads: the name check takes seconds per megabyte and the
writing check more, and a check the client cancels is no check."""
SECRET_CLASSES = ("secret", "private-key", "token")
"""The classes of the gate that a prompt or a written file must not carry either."""
"""Seconds a hook waits and retries while the vault daemon answers with its rate limit."""
INSTALLED_AWB = Path("/usr/local/bin/awb")
"""The command the seal installs for the work user (seal/setup.sh): a root owned wrapper that runs the installed
interpreter isolated (`python3 -I -m awb`); a symlink to the venv script on a host sealed before T3."""
SINCE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")
"""The one shape of a lock time that goes into a message (vault.SINCE_FORMAT); any other value is "unknown"."""
UNLOCK_HINT = "as the owner run awb vault unlock (and awb keys unlock)"
LOCKED_START = ("The Workbench is locked since %s. Every prompt is refused until the owner runs awb vault unlock. "
                "The project files were not loaded.")
"""The whole context of a session that starts while the name check cannot run (T3)."""
HOST_FILE = Path(config.HOST_CONF)
"""The host file of a sealed host; its `work_user` says whose sessions the hooks check."""


class Unavailable(Exception):
    """A check could not run. The message is a short reason chosen here, never text of the checked input.
    `rate` is true when the reason is the rate limit of the vault daemon (it passes within a minute). `since` is
    the time the vault daemon became locked when it said so in the fixed shape, else None."""

    def __init__(self, reason: str, rate: bool = False, since: str | None = None):
        super().__init__(reason)
        self.rate = rate
        self.since = since if isinstance(since, str) and SINCE_RE.fullmatch(since) else None

    def locked(self) -> str:
        """"the Workbench is locked since <time> (<reason>)", "since unknown" without a time."""
        return "the Workbench is locked since %s (%s)" % (self.since or "unknown", self)


# --------------------------------------------------------------------------- client settings


def client_settings(prefix: str) -> dict:
    """The `hooks` block of a client settings file: the six hooks, each command `<prefix> <name>`; PreToolUse
    carries two entries, the knowledge base guard of the write tools and the place guard of every file tool."""
    def entry(name: str, matcher: str | None = None) -> list[dict]:
        item: dict = {}
        if matcher:
            item["matcher"] = matcher
        item["hooks"] = [{"type": "command", "command": "%s %s" % (prefix, name)}]
        return [item]

    return {
        "hooks": {
            EVENTS["prompt"]: entry("prompt"),
            EVENTS["pre-write"]: entry("pre-write", WRITE_MATCHER) + entry("pre-tool", GUARD_MATCHER),
            EVENTS["post-write"]: entry("post-write", WRITE_MATCHER),
            EVENTS["stop"]: entry("stop"),
            EVENTS["session-start"]: entry("session-start"),
        }
    }


class ForeignPrefix(Exception):
    """On a sealed host the hook command of a project would not be the installed command."""


_EXEC_RE = re.compile(r"^[ \t]*exec[ \t]+(/\S+)[ \t]+-I[ \t]+-m[ \t]+awb[ \t]+\"\$@\"[ \t]*$")
"""The exec line of the wrapper seal/setup.sh writes: `exec <python> -I -m awb "$@"`."""


def _runs_this_python(installed: Path) -> bool:
    """True when INSTALLED_AWB runs the interpreter of this process: the wrapper whose exec line names an
    interpreter in the folder of `sys.executable`, or (a host sealed before T3) a symlink to the `awb` script of
    that folder."""
    here = Path(sys.executable).parent.resolve()
    try:
        if installed.is_symlink():
            target = installed.resolve(strict=True)
            return target.parent == here and target.name == "awb"
        if not installed.is_file() or installed.stat().st_size > 4096:
            return False
        lines = installed.read_text(encoding="utf-8").splitlines()
    except (OSError, RuntimeError, UnicodeDecodeError):
        return False
    if not lines or lines[0].strip() != "#!/bin/sh":
        return False
    for line in lines[1:]:
        m = _EXEC_RE.match(line)
        if m:
            exe = Path(m.group(1))
            try:
                return exe.parent.resolve() == here and exe.name.startswith("python")
            except (OSError, RuntimeError):
                return False
    return False


def _sealed() -> bool:
    """True when the root host file names a work user (the seal ran on this host)."""
    try:
        text = Path(HOST_FILE).read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return False
    return re.search(r"(?m)^[ \t]*work_user[ \t]*=[ \t]*\S+[ \t]*$", text) is not None


def command_prefix(executable: str | os.PathLike | None = None) -> str:
    """`<python> -m awb hook` for the settings of a project, `sys.executable` by default.

    An interpreter under the home folder is written as "$HOME/..." (the client runs hook commands through a
    shell), so that no project file carries a home path; the commit gate refuses one. Else the absolute path.
    When this process runs isolated (`python3 -I`) the prefix carries `-I` too, so that the hooks load no
    package of the invoking user's site folder or PYTHONPATH either.

    When no `executable` is given and INSTALLED_AWB runs the interpreter of this process (the wrapper of the
    seal, or the symlink of a host sealed before T3) the prefix is `/usr/local/bin/awb hook`, the very command of
    the work user's settings: the client runs identical hook commands once, so the hooks of a project do not run
    twice and do not spend the rate budget of the vault daemon twice.

    On a sealed host (the host file names a work user) any other prefix raises ForeignPrefix: a project there
    runs its hooks through the installed command or not at all (the owner's `awb spawn` from the repository
    environment is refused, T3).
    """
    if executable is None and _runs_this_python(INSTALLED_AWB):
        return "%s hook" % shlex.quote(str(INSTALLED_AWB))
    if _sealed():
        raise ForeignPrefix("this host is sealed: a project gets the hooks of the installed command only. Run "
                            "awb from %s, as the work user" % INSTALLED_AWB)
    exe = Path(executable or sys.executable)
    try:
        rel = exe.relative_to(Path.home())
    except (ValueError, RuntimeError, KeyError):
        rel = None
    if rel is not None and rel.parts and not re.search(r'["$`\\\s]', rel.as_posix()):
        shown = '"$HOME/%s"' % rel.as_posix()
    else:
        shown = shlex.quote(str(exe))
    isolated = " -I" if executable is None and sys.flags.isolated else ""
    return "%s%s -m awb hook" % (shown, isolated)


# --------------------------------------------------------------------------- small helpers


def _module(name: str):
    """awb.<name> or None when it is not there (or broken)."""
    try:
        return importlib.import_module("awb.%s" % name)
    except Exception:  # an unfinished module must not take a hook down
        return None


def _emit(event: str, context: str, notice: str | None = None) -> None:
    out: dict = {"hookSpecificOutput": {"hookEventName": event, "additionalContext": context}}
    if notice:
        out["systemMessage"] = notice
    print(json.dumps(out, ensure_ascii=False))


def _readable(path: Path) -> bool:
    try:
        return os.access(path, os.R_OK) and Path(path).is_file()
    except OSError:
        return False


def _is_rate(mod, exc: BaseException) -> bool:
    mod = mod if mod is not None else _module("check")
    cls = getattr(mod, "CheckRateLimited", None) if mod is not None else None
    if isinstance(cls, type) and isinstance(exc, cls):
        return True
    return "rate limit" in str(exc).lower()


def _reason(exc: BaseException, mod=None) -> str:
    """A short reason for an unavailable check, from a fixed list, never the text of the exception."""
    text = str(exc).lower()
    if _is_rate(mod, exc):
        return "rate limit of the vault daemon"
    if "locked" in text:
        return "vault locked"
    if "daemon" in text or "socket" in text or "unavailable" in text:
        return "no vault daemon"
    return "%s in the name check" % type(exc).__name__


def _with_rate_wait(mod, call):
    """`call()` of the check module, retried while the vault daemon answers with its rate limit, up to
    HOOK_RATE_WAIT seconds. Raises Unavailable (rate=True when the limit is still reached)."""
    deadline = time.monotonic() + HOOK_RATE_WAIT
    while True:
        try:
            return call()
        except Exception as exc:
            if _is_rate(mod, exc):
                if time.monotonic() + 1.0 <= deadline:
                    time.sleep(1.0)
                    continue
                raise Unavailable(_reason(exc, mod), rate=True) from None
            raise Unavailable(_reason(exc, mod), since=getattr(exc, "since", None)) from None


def _counts(hits: list[dict]) -> str:
    counts: dict[str, int] = {}
    for h in hits:
        cls = str(h.get("cls", "unknown")) if isinstance(h, dict) else "unknown"
        counts[cls] = counts.get(cls, 0) + 1
    return ", ".join("%s %d" % kv for kv in sorted(counts.items()))


def _field(obj, *names, default=None):
    for n in names:
        if isinstance(obj, dict) and n in obj:
            return obj[n]
        if not isinstance(obj, dict) and hasattr(obj, n):
            return getattr(obj, n)
    return default


# --------------------------------------------------------------------------- the name check


def _require_register(mod, reg: Path) -> None:
    """Raise Unavailable when the vault folder can be read and holds no register at all: `check` would then
    check against an empty register and call every name clean."""
    source = getattr(mod, "register_source", None)
    missing = getattr(mod, "MISSING", None)
    if source is None or missing is None:
        return
    try:
        kind, _ = source(reg)
    except Exception as exc:
        raise Unavailable(_reason(exc)) from None
    if kind == missing:
        raise Unavailable("no register found")


def name_hits(text: str, p: config.Paths | None = None) -> list[dict]:
    """`check.check_text` over `text` with the Workbench register. Raises Unavailable when it cannot run.

    A check module without the remote path would call a text clean when the register cannot be read here; that
    case counts as unavailable too, and so does a readable vault folder without any register.
    """
    mod = _module("check")
    if mod is None or not hasattr(mod, "check_text"):
        raise Unavailable("the name check is not installed")
    p = p or config.paths()
    reg = p.register
    if not _readable(reg) and not hasattr(mod, "CheckUnavailable"):
        raise Unavailable("no readable register and no vault daemon")
    _require_register(mod, reg)
    hits = _with_rate_wait(mod, lambda: mod.check_text(text, reg))
    return [h for h in hits if isinstance(h, dict)]


def _secret_hits(text: str) -> list[dict]:
    """Hits of the gate's secret classes in `text` as {start, length, cls}; nothing when the gate is absent."""
    mod = _module("gate")
    detectors = getattr(mod, "DETECTORS", None) if mod is not None else None
    if not isinstance(detectors, dict):
        return []
    out: list[dict] = []
    for cls in SECRET_CLASSES:
        detect = detectors.get(cls)
        if detect is None:
            continue
        try:
            out.extend({"start": off, "length": 0, "cls": cls} for off in detect(text))
        except Exception:
            continue
    return out


def _has_override(text: str) -> bool:
    """A bidi override (LRO, RLO): a viewer renders what follows reversed."""
    return "\u202e" in text or "\u202d" in text


def _plain_text(path: Path) -> str | None:
    try:
        data = path.read_bytes()
    except OSError:
        return None
    if b"\x00" in data[:_SNIFF]:
        return None
    try:
        return data.decode("utf-8-sig")
    except UnicodeDecodeError:
        return None


def file_hits(path: Path, p: config.Paths | None = None) -> list[dict]:
    """`check.check_file` over a file with the Workbench register (it goes to the vault daemon when the register
    cannot be read here). Raises Unavailable, also for a check module without the remote path and no readable
    register."""
    mod = _module("check")
    if mod is None or not hasattr(mod, "check_file"):
        raise Unavailable("the name check is not installed")
    p = p or config.paths()
    if not _readable(p.register) and not hasattr(mod, "CheckUnavailable"):
        raise Unavailable("no readable register and no vault daemon")
    _require_register(mod, p.register)
    hits = _with_rate_wait(mod, lambda: mod.check_file(path, p.register))
    return [h for h in hits if isinstance(h, dict)]


def _lines(path: Path, hits: list[dict]) -> list[tuple[str, int]]:
    """(class, line) per hit. Positions are in the normalised text; they are mapped back to the original text
    of a plain text file. Line 0 stands for the file as a whole or a position outside its own text (a decoded
    block, an extracted part)."""
    text = _plain_text(path)
    mapper = None
    if text is not None:
        try:
            from awb import normalize

            norm = normalize.normalize(text)

            def mapper(start: int, length: int) -> int:
                if length <= 0 or not 0 <= start < len(norm.text):
                    return 0
                a, _ = normalize.original_span(norm, start, min(start + length, len(norm.text)))
                return text.count("\n", 0, a) + 1
        except Exception:
            mapper = None
    out: list[tuple[str, int]] = []
    for h in hits:
        start, length = h.get("start", 0), h.get("length", 0)
        line = 0
        if mapper is not None and isinstance(start, int) and isinstance(length, int):
            try:
                line = mapper(start, length)
            except Exception:
                line = 0
        out.append((str(h.get("cls", "unknown")), line))
    return out


def _describe(found: list[tuple[str, int]]) -> str:
    """"name on line 3, 7; url on line 9; path (whole file)": classes and line numbers only."""
    by_cls: dict[str, set[int]] = {}
    for cls, line in found:
        by_cls.setdefault(cls, set()).add(line)
    parts = []
    for cls in sorted(by_cls):
        lines = sorted(n for n in by_cls[cls] if n > 0)
        text = "%s on line %s" % (cls, ", ".join(str(n) for n in lines)) if lines else "%s (whole file)" % cls
        if lines and 0 in by_cls[cls]:
            text += " and in the file as a whole"
        parts.append(text)
    return "; ".join(parts)


# --------------------------------------------------------------------------- the project


def project_root(start: str | os.PathLike | None) -> Path | None:
    """The nearest folder at or above `start` with a SCOPE.md. None when there is none."""
    if not start:
        return None
    here = Path(start)
    if not here.is_absolute():
        here = Path.cwd() / here
    if not here.is_dir():
        here = here.parent
    for n, folder in enumerate([here, *here.parents]):
        if n > _SCOPE_MAX_DEPTH:
            break
        try:
            if (folder / "SCOPE.md").is_file():
                return folder
        except OSError:
            return None
    return None


def _session_folder(data: dict) -> str | None:
    cwd = data.get("cwd")
    if isinstance(cwd, str) and cwd:
        return cwd
    return os.environ.get("CLAUDE_PROJECT_DIR") or os.getcwd()


def _platform(root: Path | None) -> str:
    """hcs only for a project whose folder is an hcs- project code; every other folder is tcp. SCOPE.md is
    written by the session and cannot switch the vendor-name rule off."""
    from awb import codes

    return "tcp" if root is None else codes.project_platform(root)


def _is_deliverable(path: Path, root: Path | None) -> bool:
    """Under a folder named deliverables at any depth, whatever its case, also through a link: a SCOPE.md
    written inside deliverables/ does not take the folder out of the rule (the red team of 2026-09-27)."""
    parts = list(path.parent.parts)
    try:
        parts += Path(os.path.realpath(path)).parent.parts
    except (OSError, ValueError):
        pass
    return any(part.lower() == "deliverables" for part in parts)


# --------------------------------------------------------------------------- the hooks


def _beat(data: dict) -> None:
    """Move the heartbeat of this session's claim on its project; never fails a hook."""
    try:
        root = project_root(_session_folder(data))
        sid = data.get("session_id")
        if root is not None and isinstance(sid, str) and sid:
            from awb import sessions
            sessions.beat(config.paths(), root.name, sid)
    except Exception:
        pass


def _rules_text(data: dict) -> str | None:
    """The notice that the rules a running session loaded at its start changed (awb/rulesync.py), once."""
    try:
        from awb import rulesync

        return rulesync.notice(config.paths(), data.get("session_id"), project_root(_session_folder(data))) or None
    except Exception:
        return None


def _rules_notice(data: dict, extra: str | None = None) -> None:
    """Tell a running session once that its rules changed; `extra` is a context line of its own (T8)."""
    said = "\n".join(t for t in (extra, _rules_text(data)) if t)
    if said:
        _emit(EVENTS["prompt"], said)


SOFT_CLASSES = ("url", "phone", "ip", "mac")
"""Structured data that a pasted block, a background agent's result or a CI event may carry (D-T8): terminal
output is full of addresses, resource names and times. A registered name, a mail, bank data, a register number,
a tax number and a secret still block; text he typed keeps the strict rule."""
_PASTE_RE = re.compile(r'<pasted_content(?P<attr>(?: id="[^"<>\n]{1,80}")?)>(?P<body>.*?)</pasted_content(?P=attr)>',
                       re.S)
"""A block the client marks as pasted: `<pasted_content id="X">...</pasted_content id="X">` or without the id."""
_EVENT_RE = re.compile(r"\A\s*(?:<system-reminder>[^<]{0,400})?<(?P<tag>task-notification|ci-monitor-event)>"
                       r".*</(?P=tag)>\s*(?:</system-reminder>\s*)?\Z", re.S)
"""A whole prompt that is the client's own event: a finished background agent or a CI event. A prompt that
merely carries the tag in the middle is his prompt."""
_TASK_ID_RE = re.compile(r"<task-id>\s*([A-Za-z0-9_-]*\d[A-Za-z0-9_-]*)\s*</task-id>")
"""The agent id of a task notification; named in a message only in this shape (letters, digits, at least one
digit, up to 64 characters), so no word of the result is repeated."""


def _prompt_kind(text: str) -> str:
    """"agent" or "ci" for the client's events, "paste" for a prompt with a pasted block, else "typed"."""
    m = _EVENT_RE.match(text)
    if m:
        return "agent" if m.group("tag") == "task-notification" else "ci"
    return "paste" if _PASTE_RE.search(text) else "typed"


def _typed_has_data(text: str) -> bool:
    """True when the text outside the pasted blocks carries structured data of its own (checked here, with the
    patterns alone: no name is in the text at this point). True as well when the patterns cannot run."""
    try:
        from awb import check, normalize, patterns

        typed = check.with_hidden_views(_PASTE_RE.sub("\n", text))
        return bool(patterns.find_structured(normalize.normalize(typed).text))
    except Exception:
        return True


def _event_source(text: str, kind: str) -> str:
    """"the result of background agent ID", "the CI event": the agent id only when it has the shape of one."""
    if kind == "ci":
        return "the CI event"
    m = _TASK_ID_RE.search(text)
    if m and len(m.group(1)) <= 64:
        return "the result of background agent %s" % m.group(1)
    return "the result of a background agent"


def hook_prompt(data: dict) -> int:
    _beat(data)
    text = data.get("prompt")
    if not isinstance(text, str) or not text.strip():
        return OK
    if _sealed():
        refused = _prompt_receipt(data)
        if refused:
            print(refused, file=sys.stderr)
            return BLOCK
    try:
        hits = name_hits(text)
    except Unavailable as exc:
        if exc.rate:
            # not a locked vault: the check is back within a minute, the prompt is not passed unchecked
            print("the name check is busy (rate limit of the vault daemon). Nothing was sent: send the prompt "
                  "again in a minute.", file=sys.stderr)
            return BLOCK
        # a guard that switches itself off is no guard (T3): whatever stops the check stops the prompt
        print("%s: %s, then send the prompt again" % (exc.locked(), UNLOCK_HINT), file=sys.stderr)
        return BLOCK
    secrets = _secret_hits(text)
    if not hits and not secrets:
        _rules_notice(data)
        return OK
    names = [h for h in hits if h.get("cls") == "name"]
    other = [h for h in hits if h.get("cls") != "name"]
    kind = _prompt_kind(text)
    soft = [h for h in other if h.get("cls") in SOFT_CLASSES]
    if (kind != "typed" and not names and not secrets and len(soft) == len(other)
            and (kind != "paste" or not _typed_has_data(text))):
        what = {"paste": "The paste", "agent": "The agent result", "ci": "The CI event"}[kind]
        _rules_notice(data, "%s carries %d data items (%s); never repeat them, refer to them by line."
                      % (what, len(soft), _counts(soft)))
        return OK
    if kind in ("agent", "ci"):
        blocked = ([{"cls": "registered name"} for _ in names]
                   + [h for h in other if h.get("cls") not in SOFT_CLASSES] + secrets)
        rerun = " Run the agent again with the rule \"codes only\"." if kind == "agent" else ""
        print("%s was not delivered: it carries %s.%s" % (_event_source(text, kind), _counts(blocked), rerun),
              file=sys.stderr)
        return BLOCK
    parts: list[str] = []
    if names:
        msg = ("the prompt carries a registered name (%d hits). Register new names with `awb register` in your "
               "own shell and write the code instead." % len(names))
        if other:
            msg += " It also carries structured data (%s)." % _counts(other)
        parts.append(msg)
    elif other:
        parts.append("the prompt carries structured data (%s). Leave it out or write a code instead."
                     % _counts(other))
    if secrets:
        parts.append("the prompt carries a secret (%s). A session never holds a live secret: keep it in the "
                     "vault or the password store and refer to it by name." % _counts(secrets))
    print(" ".join(parts), file=sys.stderr)
    return BLOCK


def _written_file(data: dict) -> Path | None:
    """The file a write tool targets. None when the input names no file. Raises ValueError for an input the
    hook cannot read (a tool input that is not an object, a path with a NUL byte): such a write is blocked."""
    tool_input = data.get("tool_input")
    if tool_input is None:
        return None
    if not isinstance(tool_input, dict):
        raise ValueError("tool input of an unexpected shape")
    raw = tool_input.get("file_path") or tool_input.get("notebook_path") or tool_input.get("path")
    if raw is None:
        return None
    if not isinstance(raw, str) or not raw or "\x00" in raw:
        raise ValueError("a path the hook cannot read")
    path = Path(os.path.expandvars(os.path.expanduser(raw)))
    if not path.is_absolute():
        base = data.get("cwd") if isinstance(data.get("cwd"), str) else os.getcwd()
        path = Path(base) / path
    return path


def _shares_inode(target: Path, kb_root: Path) -> bool:
    """True when `target` is a hard link to a file of the knowledge base: realpath resolves symbolic links only,
    a hard link shares the inode (the security review of 2026-09-27, sec-session-1)."""
    try:
        st = os.stat(target)
    except OSError:
        return False
    if st.st_nlink < 2:
        return False
    for folder, _, files in os.walk(kb_root):
        for name in files:
            try:
                other = os.stat(os.path.join(folder, name))
            except OSError:
                continue
            if (other.st_dev, other.st_ino) == (st.st_dev, st.st_ino):
                return True
    return False


def hook_pre_write(data: dict) -> int:
    """A Write, Edit, MultiEdit or NotebookEdit into the knowledge base is refused before it happens. Relative
    paths are taken from the session folder, ~ and variables are expanded, links and hard links are followed,
    so none leads around the check; an input the hook cannot read is refused as well."""
    try:
        path = _written_file(data)
        if path is None:
            return OK
        kb_root = Path(os.path.realpath(config.paths().kb))
        target = Path(os.path.realpath(path))
    except ValueError:
        print("the hook could not read the path of this write; nothing passes unchecked", file=sys.stderr)
        return BLOCK
    if target == kb_root or kb_root in target.parents or _shares_inode(target, kb_root):
        print("the knowledge base takes facts only through `awb kb add`, `awb kb amend` and `awb kb retire`, "
              "which run every check. Write the fact with one of them instead of editing the file.", file=sys.stderr)
        return BLOCK
    return OK


# --------------------------------------------------------------------------- pre-tool: a session stays in its project

GUARD_MATCHER = "Read|Edit|MultiEdit|NotebookEdit|Write|Glob|Grep|Bash"
"""The tools whose target paths the pre-tool hook guards."""
GUARD_TOOLS = tuple(GUARD_MATCHER.split("|"))
ANOTHER_PROJECT = "another project"
CLIENT_FOLDER = "the client's folder"
KEY_FOLDER = "a key folder"
UNKNOWN_PROJECT = "an unknown project"
"""The classes of a refused place; a refusal names the class, never the path."""
KEY_FOLDERS = (".ssh", ".config")
_PROJECT_DIR_RE = re.compile(r"^(?:tcp|hcs)-")
_GLOB_CHARS = "*?[{"
_SPLIT_RE = re.compile(r"[\s'\"`;|&<>(),=:]+")
_SID_RE = re.compile(r"[A-Za-z0-9_-]{1,64}")


class _Refused(Exception):
    """A tool call the guard refuses; the message is the class of the place."""


def _inside(path: Path, folder: Path) -> bool:
    return path == folder or folder in path.parents


def _glob_base(raw: str) -> str:
    """The part of a path before its first glob character, cut back to a whole folder: `/a/tcp-*/x` is `/a`."""
    cut = min((raw.find(c) for c in _GLOB_CHARS if c in raw), default=-1)
    if cut < 0:
        return raw
    head = raw[:cut]
    return head if head.endswith("/") else os.path.dirname(head) or "/"


def _expand(raw: str, home: Path) -> str:
    """~, ~/x, $HOME/x and ${HOME}/x as paths under `home`; ~user/x through the password database."""
    for var in ("${HOME}", "$HOME"):
        if raw == var or raw.startswith(var + "/"):
            return str(home) + raw[len(var):]
    if raw == "~" or raw.startswith("~/"):
        return str(home) + raw[1:]
    return os.path.expanduser(raw) if raw.startswith("~") else raw


def _absolute(raw: str, cwd: Path, home: Path) -> Path:
    if "\x00" in raw:
        raise _Refused(UNKNOWN_PROJECT)
    text = _glob_base(_expand(raw, home))
    path = Path(text)
    return path if path.is_absolute() else cwd / path


def _bash_paths(command: str, cwd: Path, home: Path) -> list[Path]:
    """Every absolute or home-relative path of a command line, and every relative one that climbs with `..`. The
    guard reads the command line, not what the shell computes: a path built at run time gets past it."""
    out = []
    for tok in _SPLIT_RE.split(command.replace("${HOME}", "$HOME")):
        tok = tok.strip("{}[]!")
        if not tok:
            continue
        if tok.startswith(("/", "~", "$HOME")):
            out.append(_absolute(tok, cwd, home))
        elif tok == ".." or tok.startswith("../") or "/../" in tok or tok.endswith("/.."):
            out.append(_absolute(tok, cwd, home))
    return out


def _tool_paths(tool: str, tool_input: dict, cwd: Path, home: Path) -> list[Path]:
    """The target paths of one tool call; a Glob or Grep without a path searches the session folder."""
    if tool == "Bash":
        command = tool_input.get("command")
        if not isinstance(command, str):
            raise _Refused(UNKNOWN_PROJECT)
        return [cwd] + _bash_paths(command, cwd, home)
    names = {"Glob": ("path", "pattern"), "Grep": ("path", "glob")}.get(tool, ("file_path", "notebook_path", "path"))
    out = []
    for key in names:
        raw = tool_input.get(key)
        if raw is None or raw == "":
            continue
        if not isinstance(raw, str):
            raise _Refused(UNKNOWN_PROJECT)
        if key in ("pattern", "glob") and not raw.startswith(("/", "~", "$HOME", "${HOME}")):
            continue                        # a relative pattern searches under the path or the session folder
        out.append(_absolute(raw, cwd, home))
    if tool in ("Glob", "Grep") and not tool_input.get("path"):
        out.append(cwd)
    if not out and tool not in ("Glob", "Grep"):
        raise _Refused(UNKNOWN_PROJECT)
    return out


def place_class(path: Path, own: Path, p: config.Paths, home: Path) -> str | None:
    """The class of a refused place for `path`, or None when a session may reach it. The path is taken as written
    and through its links; both must pass. The own project, the shared folder (outbox, datasets) and the knowledge
    base pass; another project, the client's folder and the key folders of `home` do not, and neither does a
    folder that holds one of them (a search from it would reach them)."""
    literal = Path(os.path.normpath(str(path)))
    try:
        real = Path(os.path.realpath(literal))
    except (OSError, ValueError):
        return UNKNOWN_PROJECT
    own_real = Path(os.path.realpath(own))
    allowed = [own_real, Path(os.path.realpath(p.shared)), Path(os.path.realpath(p.kb))]
    roots = {Path(os.path.realpath(p.projects_root)), Path(os.path.realpath(home))}
    client = Path(os.path.realpath(home / ".claude"))
    keys = [Path(os.path.realpath(home / k)) for k in KEY_FOLDERS]
    for t in dict.fromkeys((literal, real)):
        if any(_inside(t, a) for a in allowed):
            continue
        if any(_inside(t, k) for k in keys):
            return KEY_FOLDER
        if _inside(t, client):
            return CLIENT_FOLDER
        for r in roots:
            if t != r and _inside(t, r):
                first = t.relative_to(r).parts[0]
                if _PROJECT_DIR_RE.match(first):
                    return ANOTHER_PROJECT
        found = project_root(t)
        if found is not None and Path(os.path.realpath(found)) != own_real:
            return ANOTHER_PROJECT
        if any(_inside(r, t) for r in roots) or _inside(own_real, t):
            return ANOTHER_PROJECT
        if any(_inside(k, t) for k in keys):
            return KEY_FOLDER
        if _inside(client, t):
            return CLIENT_FOLDER
    return None


def _own_project(data: dict) -> Path | None:
    """The project of the session: the folder the client started in, else the session folder of the call."""
    started = os.environ.get("CLAUDE_PROJECT_DIR")
    root = project_root(started) if started else None
    return root if root is not None else project_root(data.get("cwd") if isinstance(data.get("cwd"), str) else None)


def guard_log(p: config.Paths) -> Path:
    return p.shared / "sessions" / "guard.log"


def _log_refusal(p: config.Paths, data: dict, tool: str, cls: str, own: Path | None) -> None:
    """One line in <shared>/sessions/guard.log: time, session id, tool, class, project code. Never a path."""
    sid = data.get("session_id")
    sid = sid if isinstance(sid, str) and _SID_RE.fullmatch(sid) else "unknown"
    from awb import questions
    code = questions.project_of(own)
    when = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    try:
        path = guard_log(p)
        path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o640)
        try:
            os.write(fd, ("%s\t%s\t%s\t%s\t%s\n" % (when, sid, tool, cls, code)).encode("utf-8"))
        finally:
            os.close(fd)
    except OSError:
        pass


def guard(data: dict, p: config.Paths | None = None, home: Path | None = None,
          sealed: bool | None = None) -> str | None:
    """The class of the place a tool call reaches outside its project, or None when it may run. Fails closed:
    a call whose project or target cannot be read is refused as UNKNOWN_PROJECT. Only a host without a seal (the
    plugin on an architect's own machine, `sealed` false) lets a session outside any project pass."""
    tool = data.get("tool_name")
    if tool not in GUARD_TOOLS:
        return None
    try:
        p = p or config.paths()
        home = home or Path.home()
        own = _own_project(data)
        if own is None:
            return UNKNOWN_PROJECT if (_sealed() if sealed is None else sealed) else None
        cwd_raw = data.get("cwd")
        cwd = Path(cwd_raw) if isinstance(cwd_raw, str) and cwd_raw else own
        tool_input = data.get("tool_input")
        if not isinstance(tool_input, dict):
            return UNKNOWN_PROJECT
        for path in _tool_paths(tool, tool_input, cwd, home):
            cls = place_class(path, own, p, home)
            if cls:
                return cls
    except _Refused as exc:
        return str(exc)
    except Exception:
        return UNKNOWN_PROJECT
    return None


def refusal(cls: str) -> str:
    if cls == UNKNOWN_PROJECT:
        return ("refused: the project folder of this session cannot be determined, so no tool call runs. Start the "
                "session in its project folder.")
    return "refused: this call reaches %s. A session stays inside its own project folder." % cls


def hook_pre_tool(data: dict) -> int:
    """Read, Edit, MultiEdit, NotebookEdit, Write, Glob, Grep and Bash stay inside the session's project: a target
    in another project, in the client's folder or in a key folder is refused with one line naming the class."""
    cls = guard(data)
    if cls is None:
        return OK
    try:
        p = config.paths()
    except Exception:
        p = None
    if p is not None:
        _log_refusal(p, data, str(data.get("tool_name")) if data.get("tool_name") in GUARD_TOOLS else "other", cls,
                     _own_project(data))
    print(refusal(cls), file=sys.stderr)
    return BLOCK


def refusals(p: config.Paths, code: str, day: date | None = None) -> int:
    """The refused tool calls of a project on a day, from guard.log."""
    want = (day or date.today()).isoformat()
    try:
        lines = guard_log(p).read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeDecodeError):
        return 0
    return sum(1 for line in lines
               if line.startswith(want) and line.split("\t")[-1:] == [code] and line.count("\t") == 4)


def selftest() -> list[str]:
    """Plant two projects, a key folder and the client's folder in a throw-away home and prove that the guard
    refuses what it must and passes the own project. The problems found; empty when the guard works."""
    import tempfile

    problems: list[str] = []
    with tempfile.TemporaryDirectory(prefix="awb-guard-") as tmp:
        home = Path(tmp)
        own, other = home / "tcp-aaaa", home / "tcp-bbbb"
        for root in (own, other):
            root.mkdir()
            (root / "SCOPE.md").write_text("# Scope\n", encoding="utf-8")
        (home / ".ssh").mkdir()
        p = config.Paths(shared=home / "tcp-shared", vault=home / "no-vault", projects_root=home, kb=home / "tcp-kb")
        saved = os.environ.pop("CLAUDE_PROJECT_DIR", None)
        try:
            cases = [
                ({"tool_name": "Read", "tool_input": {"file_path": str(other / "STATE.md")}}, ANOTHER_PROJECT),
                ({"tool_name": "Bash", "tool_input": {"command": "cat %s/STATE.md" % other}}, ANOTHER_PROJECT),
                ({"tool_name": "Read", "tool_input": {"file_path": str(home / ".ssh" / "id_ed25519")}}, KEY_FOLDER),
                ({"tool_name": "Read", "tool_input": {"file_path": str(own / "STATE.md")}}, None),
            ]
            for payload, want in cases:
                payload["cwd"] = str(own)
                got = guard(payload, p, home, sealed=True)
                if got != want:
                    problems.append("%s case %s: %s where %s was due"
                                    % (payload["tool_name"], len(problems) + 1, got or "passed", want or "a pass"))
            if guard({"tool_name": "Read", "cwd": tmp, "tool_input": {"file_path": str(own / "x")}}, p, home,
                     sealed=True) != UNKNOWN_PROJECT:
                problems.append("a call without a project folder passed")
        finally:
            if saved is not None:
                os.environ["CLAUDE_PROJECT_DIR"] = saved
    return problems


def _writing_tells(path: Path, scope: str) -> list[tuple[str, int, str]]:
    """Blocking tells of `writing.check_file` as (class, line, hint). Raises Unavailable."""
    mod = _module("writing")
    fn = getattr(mod, "check_file", None) if mod is not None else None
    if fn is None:
        raise Unavailable("the writing check is not installed")
    try:
        result = fn(path, mode="doc", scope=scope)
    except Exception as exc:
        raise Unavailable("%s in the writing check" % type(exc).__name__) from None
    tells = result[0] if isinstance(result, tuple) else result
    out = []
    for t in tells or []:
        if not _field(t, "blocking", default=False):
            continue
        line = _field(t, "line", default=0)
        hint = str(_field(t, "hint", default="") or "")
        out.append((str(_field(t, "cls", default="unknown")), line if isinstance(line, int) else 0, hint))
    return out


def hook_post_write(data: dict) -> int:
    try:
        path = _written_file(data)
    except ValueError:
        print("the hook could not read the path of this write; the written file is not checked and goes nowhere",
              file=sys.stderr)
        return BLOCK
    if path is None or not path.is_file():
        return OK
    root = project_root(path.parent)
    deliverable = _is_deliverable(path, root)
    try:
        size = path.stat().st_size
    except OSError:
        size = 0
    limit = MAX_DELIVERABLE_BYTES if deliverable else MAX_WRITE_BYTES
    if size > limit:
        print("the written file is %d MB, over the %d MB the write-time check reads: it is not checked and goes "
              "nowhere. Split it, or run `awb check` on it yourself before it leaves the project."
              % (size // (1024 * 1024), limit // (1024 * 1024)), file=sys.stderr)
        return BLOCK
    problems: list[str] = []
    warnings: list[str] = []
    text = _plain_text(path)
    if text is not None:
        secrets = _secret_hits(text)
        if secrets:
            problems.append("the written file carries a secret (%s). A session never holds a live secret: keep it "
                            "in the vault or the password store and refer to it by name." % _counts(secrets))
        if _has_override(text):
            problems.append("the written file carries a bidi override (U+202D or U+202E), which a viewer renders "
                            "reversed. Take it out.")
    folders = ""
    if root is not None:
        try:
            folders = "/".join(path.relative_to(root).parts[:-1])
        except ValueError:
            folders = ""
    try:
        hits = file_hits(path)
        if folders and name_hits(folders):
            problems.append("a folder on the path of the written file carries a registered name: rename it to a "
                            "code before anything else goes in.")
    except Unavailable as exc:
        # the file is written (the tool ran), but it must not count as checked: exit 2 puts this in front of the
        # session as an error it has to act on, not as a note
        if exc.rate:
            problems.append("the written file is not checked: the name check did not run (%s). Keep every name "
                            "out of it and run `awb check` on it when the check is back; until then it goes "
                            "nowhere." % exc)
        else:
            problems.append("the written file is not checked: %s: %s, then run `awb check` on it. Keep every "
                            "name out of it; until then it goes nowhere." % (exc.locked(), UNLOCK_HINT))
        hits = []
    if hits:
        problems.append("the written file carries %d hits of the name check: %s. Replace them with codes "
                        "(awb check FILE shows the positions)." % (len(hits), _describe(_lines(path, hits))))
    if deliverable:
        try:
            tells = _writing_tells(path, _platform(root))
        except Unavailable as exc:
            warnings.append("the writing check did not run (%s)" % exc)
            tells = []
        if tells:
            listed = "; ".join("%s line %d%s" % (cls, line, " (%s)" % hint if hint else "")
                               for cls, line, hint in tells)
            problems.append("the deliverable breaks %d writing rules: %s." % (len(tells), listed))
    if problems:
        print("\n".join(problems), file=sys.stderr)
        return BLOCK
    if warnings:
        text = "Warning from the Workbench: %s. Check the file again when the check is back." % "; ".join(warnings)
        _emit(EVENTS["post-write"], text, "awb: %s" % "; ".join(warnings))
    return OK


def _safe_names(files: list[str]) -> list[str]:
    """File names as they are when they pass the name check, else "file N"."""
    out = []
    for n, name in enumerate(files, start=1):
        try:
            clean = not name_hits(name)
        except Unavailable:
            clean = False
        out.append(name if clean else "file %d" % n)
    return out


def _git_lines(root: Path, *args: str) -> list[str] | None:
    try:
        r = subprocess.run(["git", "-C", str(root), *args], capture_output=True, text=True, timeout=20,
                           stdin=subprocess.DEVNULL)
    except (OSError, subprocess.SubprocessError):
        return None
    return r.stdout.splitlines() if r.returncode == 0 else None


def draft_ledger(p: config.Paths, root: Path, now: datetime.datetime | None = None) -> bool:
    """T-41: a ledger entry drafted from the commits of the project since the last draft, at most every
    DRAFT_HOURS. Commit subjects passed the commit gate; the ledger checks them again. True when written. The
    marker moves only after the entry is in: a draft the ledger refuses raises ledger.Refused, is noted in
    <code>.refused beside the marker and is tried again with the next commit."""
    from awb import ledger, projects
    now = now or datetime.datetime.now(datetime.timezone.utc)
    code = root.name
    head = _git_lines(root, "rev-parse", "HEAD")
    if not head:
        return False
    marker = p.ledger / ".drafts" / (code + ".json")
    try:
        state = json.loads(marker.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        state = {}
    if state.get("head") == head[0]:
        return False
    try:
        if now - datetime.datetime.fromisoformat(state["at"]) < datetime.timedelta(hours=DRAFT_HOURS):
            return False
    except (KeyError, TypeError, ValueError):
        pass
    # a bare date means "since this time of day" to git: the first draft starts at midnight UTC of the day
    span = (["%s..HEAD" % state["head"]] if state.get("head")
            else ["--since=%s 00:00:00 +0000" % now.date().isoformat()])
    subjects = [s for s in (_git_lines(root, "log", "--format=%s", *span) or []) if not s.startswith("Spawn tcp-")]
    refused = marker.with_suffix(".refused")
    try:
        if refused.read_text(encoding="utf-8").strip() == head[0]:
            return False                    # this draft was refused and said once; the next commit tries again
    except OSError:
        pass
    written = False
    if subjects:
        row = next((r for r in projects.load(p) if r.code == code), None)
        done = "%s: %d commit(s): %s" % (code, len(subjects), "; ".join(reversed(subjects)))
        try:
            ledger.add("other", done[:ledger.MAX_TEXT], project=code,
                       customer=None if row is None or row.customer == projects.NO_CUSTOMER else row.customer,
                       outcome="drafted at session end from the commits; correct it with awb ledger add when "
                               "needed", p=p)
        except ledger.Refused:
            marker.parent.mkdir(parents=True, exist_ok=True)
            refused.write_text(head[0] + "\n", encoding="utf-8")
            raise
        written = True
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text(json.dumps({"head": head[0], "at": now.isoformat()}) + "\n", encoding="utf-8")
    try:
        refused.unlink()
    except OSError:
        pass
    return written


def _quietly(fn, *args) -> None:
    try:
        fn(*args)
    except Exception:
        pass


def hook_stop(data: dict) -> int:
    if data.get("stop_hook_active"):
        return OK
    root = project_root(_session_folder(data))
    from awb import english, questions
    _quietly(english.capture, config.paths(), data.get("transcript_path"))
    _quietly(questions.capture, config.paths(), data.get("transcript_path"), root)
    _quietly(questions.record_refusals, config.paths(), root, refusals)
    if root is None:
        return OK
    draft_msg = None
    try:
        draft_ledger(config.paths(), root)
    except Exception as exc:
        from awb import ledger
        if isinstance(exc, ledger.Refused):
            # the refusal names the field and the counts, never a value
            draft_msg = ("the ledger refused the draft of this session's commits (%s): add the entry yourself with "
                         "awb ledger add, in plain words" % exc)
    harvest_msg = None
    try:
        from awb import harvest
        harvest_msg = harvest.due(config.paths(), data.get("transcript_path"), data.get("session_id"))
    except Exception:
        harvest_msg = None
    late_msg = None
    try:
        from awb import status as _status
        late_msg = _status.reminder(root)
    except Exception:
        late_msg = None
    mod = _module("review")
    status = getattr(mod, "status", None) if mod is not None else None
    rows = None
    if status is not None:
        try:
            rows = status(root)
        except Exception:
            rows = None
    pending = []
    unreadable = []
    for row in rows or []:
        state = str(_field(row, "state", "record", "status", default="") or "")
        if state == "valid":
            continue
        name = _field(row, "file", "deliverable", "name", "path", default="")
        (unreadable if state == "unreadable" else pending).append(Path(str(name)).name or "unnamed")
    if not pending and not unreadable:
        said = [m for m in (draft_msg, harvest_msg, late_msg) if m]
        if said:
            print("\n".join(said), file=sys.stderr)
            return BLOCK
        return OK
    shown = _safe_names(pending + unreadable)
    lines = [m for m in (draft_msg, harvest_msg, late_msg) if m]
    if pending:
        lines.append("run the review for: %s" % ", ".join(shown[:len(pending)]))
    if unreadable:
        # an image or another file without text can never pass the review: asking for one would never end
        lines.append("these files cannot be read as text and can never pass the review, move them out of "
                     "deliverables/ (a diagram as SVG or as a PDF with a text layer can stay): %s"
                     % ", ".join(shown[len(pending):]))
    print("\n".join(lines), file=sys.stderr)
    return BLOCK


def _kb_expired(p: config.Paths) -> int | None:
    """Count of expired knowledge entries, from awb.kb when it offers it, else from the entry headers."""
    mod = _module("kb")
    fn = getattr(mod, "expired", None) if mod is not None else None
    if callable(fn):
        for arg in (p, p.kb):
            try:
                found = fn(arg)
            except Exception:
                continue
            if isinstance(found, (list, tuple, set)):
                return len(found)
            if isinstance(found, int) and not isinstance(found, bool):
                return found
    folder = p.kb / "entries"
    if not folder.is_dir():
        return None
    today = date.today()
    count = 0
    for f in sorted(folder.glob("KB-*.md")):
        try:
            head = f.read_text(encoding="utf-8").split("\n---", 1)[0]
        except (OSError, UnicodeDecodeError):
            continue
        m = re.search(r"(?m)^expires:\s*([0-9]{4}-[0-9]{2}-[0-9]{2})\s*$", head)
        if not m or re.search(r"(?m)^retired:\s*\S", head):   # a withdrawn entry is never due again
            continue
        try:
            if date.fromisoformat(m.group(1)) < today:
                count += 1
        except ValueError:
            continue
    return count


def _career_days(p: config.Paths) -> int | None:
    """Days since the last career update, from awb.career when it offers it (`due`: before the first update the
    days since the oldest entry, as `awb career due` counts them), else from <shared>/career-updated."""
    mod = _module("career")
    fn = getattr(mod, "due", None) if mod is not None else None
    if callable(fn):
        try:
            answer = fn(p)
        except Exception:
            answer = None
        if isinstance(answer, tuple) and answer:
            days = answer[0]
            if days is None or (isinstance(days, int) and not isinstance(days, bool)):
                return days
    for name in ("days_since_update", "days_since"):
        fn = getattr(mod, name, None) if mod is not None else None
        if callable(fn):
            try:
                days = fn(p)
            except Exception:
                continue
            if isinstance(days, int) and not isinstance(days, bool):
                return days
    try:
        text = (p.shared / "career-updated").read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None
    m = re.search(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", text)
    if not m:
        return None
    try:
        return (date.today() - date.fromisoformat(m.group(0))).days
    except ValueError:
        return None


def _section(root: Path, name: str, max_lines: int | None, notes: list[str], down: list[str] | None = None) -> str:
    """One file as a section of the session context. Withheld when it carries any hit of the name check and when
    the check cannot run (`down` carries the reason once the check failed, so the next sections do not wait for
    it again)."""
    title = name if max_lines is None else "%s (first %d lines)" % (name, max_lines)
    try:
        text = (root / name).read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return "## %s\n\n(missing or unreadable)\n" % title
    if max_lines is not None:
        text = "\n".join(text.splitlines()[:max_lines])
    if _has_override(text):
        return ("## %s\n\n(withheld: it carries a bidi override (U+202D or U+202E), which a viewer renders "
                "reversed. Take it out of the file.)\n" % title)
    checked = text
    cut = len(text) > SECTION_CHARS
    if cut:
        # the cut falls at a line end when one is near, and the check reads on past it, so that a form that
        # straddles the cut is seen whole and not loaded in part
        end = text.find("\n", SECTION_CHARS, SECTION_CHARS + 500)
        end = SECTION_CHARS if end < 0 else end
        checked = text[:end + 200]
        text = text[:end]
        notes.append("%s is longer than %d characters: only its start was loaded, read the rest in the file"
                     % (name, SECTION_CHARS))
    try:
        if down:
            raise Unavailable(down[0])
        hits = name_hits(checked)
    except Unavailable as exc:
        # fail closed: a text that could not be checked never goes into the context of a session
        if down is not None and not down:
            down.append(str(exc))
        notes.append("%s was not name checked (%s), so it is withheld" % (name, exc))
        return "## %s\n\n(withheld: the name check did not run. Read the file yourself once the check is back.)\n" \
            % title
    names = [h for h in hits if h.get("cls") == "name"]
    if names:
        return ("## %s\n\n(withheld: it carries %d registered names. Run `awb check %s` and replace them with "
                "codes.)\n" % (title, len(names), name))
    if hits:
        notes.append("%s carries structured data (%s), replace it with codes" % (name, _counts(hits)))
        return ("## %s\n\n(withheld: it carries structured data (%s). Run `awb check %s` and replace it with "
                "codes.)\n" % (title, _counts(hits), name))
    return "## %s\n\n%s\n" % (title, text.strip())


def _project_notes(p: config.Paths, root: Path, sid, shown: str | None = None) -> list[str]:
    """The claim of the project for this session (T-62), an idle project (T-24) and a STATE.md over its size
    (T-22). Never fails the hook. `shown` is the name of the project as the context may show it."""
    from awb import projects, sessions
    shown = shown or root.name
    out: list[str] = []
    try:
        if isinstance(sid, str) and sid:
            other = sessions.claim(p, root.name, sid)
            if other:
                out.append("another session (%s, since %s) holds this project: two sessions in one project "
                           "overwrite each other's STATE.md, finish the other one first"
                           % (sessions.short(other["session"]), str(other.get("since", ""))[:16]))
    except Exception:
        pass
    try:
        state = root / "STATE.md"
        age = (datetime.date.today() - datetime.date.fromtimestamp(state.stat().st_mtime)).days
        if age > projects.IDLE_DAYS:
            out.append("STATE.md was last changed %d days ago: when the work is over, `awb close %s`"
                       % (age, shown))
        lines = state.read_text(encoding="utf-8").count("\n")
        if lines > projects.MAX_STATE_LINES:
            out.append("STATE.md has %d lines, over %d: move the older layers to history/"
                       % (lines, projects.MAX_STATE_LINES))
    except (OSError, UnicodeDecodeError):
        pass
    return out


# --------------------------------------------------------------------------- the receipt of a sealed start

MANAGED_FILE = Path("/etc/claude-code/managed-settings.d/awb-workbench.json")
"""The managed client settings the seal installs (seal/setup.sh): the hooks of every session of the host."""
MANAGED_SOURCE = Path(__file__).resolve().parent.parent / "seal" / "work-claude" / "managed-settings.json"
"""The managed settings of the running release, the hooks the installed file must carry."""
KEYS_SOCKET = Path("/run/awb-keys/cloud.sock")
KEYS_SOCKET_ENV = "AWB_KEYS_SOCKET"
"""The call socket of the key service and its variable, those of awb/tcp/keys.py (the core imports no awb.tcp)."""
RECEIPT_TIMEOUT = 5.0
"""Seconds the receipt waits for the vault daemon and for the key service."""
GUARDS = (
    ("hooks", "the client hooks", "the managed settings or the installed command are not the release's: sudo awb "
                                  "deploy"),
    ("gate", "the commit gate of the project", "awb gate --install in the project folder"),
    ("vault", "the vault daemon", "it does not answer a check: the owner starts awb-vaultd"),
    ("keys", "the key service", "it does not answer a ping: the owner starts awb-keyd"),
    ("rules", "the rules digest", "the rules are not the installed ones: awb projects sync"),
    ("mode", "the host mode", "the host file names no mode this release knows: sudo awb deploy"),
)
"""Every guard of the receipt in its order: key, the name a refusal shows (never a path) and the fix."""
RECEIPT_LINE = "guards: %d of %d active (%s)"
REFUSED_START = ("Start refused, %s missing: %s (%s). Every prompt is refused until it is fixed; the project files "
                 "were not loaded.")
RECEIPTS_LOG = "receipts.tsv"
"""<shared>/sessions/receipts.tsv: one line per start of a sealed session (UTC time, session id, project code or
-, "ok" or the keys of the missing guards)."""


def _release() -> str:
    """The short name of the running release: the first 7 characters of its commit, else the folder name."""
    try:
        from awb import vault

        name = os.path.basename(vault.release_path())
    except Exception:
        return "unknown release"
    return name[:7] if re.fullmatch(r"[0-9a-f]{40}", name) else (name or "unknown release")


def _hook_entries(data) -> set[tuple[str, str, str]] | None:
    """(event, matcher, command) of every command hook of a settings object; None when it has no such shape."""
    if not isinstance(data, dict) or not isinstance(data.get("hooks"), dict):
        return None
    out = set()
    for event, entries in data["hooks"].items():
        for entry in entries if isinstance(entries, list) else ():
            if not isinstance(entry, dict):
                continue
            for h in entry.get("hooks") or ():
                if isinstance(h, dict) and h.get("type") == "command" and isinstance(h.get("command"), str):
                    out.add((str(event), str(entry.get("matcher") or ""), h["command"]))
    return out


def _guard_hooks() -> bool:
    """Every hook of the release's managed settings is in the installed managed file, and each of them runs the
    installed command (INSTALLED_AWB, an executable file)."""
    try:
        want = _hook_entries(json.loads(Path(MANAGED_SOURCE).read_text(encoding="utf-8")))
        have = _hook_entries(json.loads(Path(MANAGED_FILE).read_text(encoding="utf-8")))
    except (OSError, ValueError, UnicodeDecodeError):
        return False
    if not want or have is None or not want <= have:
        return False
    prefix = "%s hook " % INSTALLED_AWB
    if not all(cmd.startswith(prefix) for _, _, cmd in want):
        return False
    try:
        return Path(INSTALLED_AWB).is_file() and os.access(INSTALLED_AWB, os.X_OK)
    except OSError:
        return False


def _guard_gate(root: Path | None) -> bool:
    """The project's repository carries the three hooks of the commit gate (awb gate --install) and runs its hooks
    from .git/hooks (no core.hooksPath)."""
    if root is None:
        return False
    from awb import gate

    git = Path(root) / ".git"
    try:
        if not git.is_dir() or git.is_symlink():
            return False
        conf = (git / "config").read_text(encoding="utf-8", errors="replace")
        if re.search(r"(?im)^\s*hookspath\s*=", conf):
            return False
        for name in gate.HOOK_NAMES:
            hook = git / "hooks" / name
            if not hook.is_file() or not os.access(hook, os.X_OK):
                return False
            if gate.HOOK_MARK not in hook.read_text(encoding="utf-8", errors="replace"):
                return False
    except OSError:
        return False
    return True


def _guard_vault(p: config.Paths) -> str:
    """"ok", "locked" or "down": the vault daemon answers a check, says it is locked, or does not answer."""
    from awb import vault

    try:
        state = vault.ping_state(p.check_socket, timeout=RECEIPT_TIMEOUT)
    except Exception:
        return "down"
    return "locked" if state["state"] == "locked" else "ok"


def _keys_socket() -> Path:
    """The call socket of the key service; for the work user of a sealed host never the one of the environment."""
    env = None if config.is_work_user() else os.environ.get(KEYS_SOCKET_ENV)
    return Path(env) if env else Path(KEYS_SOCKET)


def _guard_keys() -> bool:
    """The key service answers {"op":"ping"} with ok true on its call socket."""
    import socket

    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
            s.settimeout(RECEIPT_TIMEOUT)
            s.connect(str(_keys_socket()))
            s.sendall(b'{"op":"ping"}\n')
            buf = b""
            while not buf.endswith(b"\n") and len(buf) < 65536:
                part = s.recv(4096)
                if not part:
                    break
                buf += part
        answer = json.loads(buf.decode("utf-8"))
    except (OSError, ValueError):
        return False
    return isinstance(answer, dict) and answer.get("ok") is True


def _guard_rules(root: Path | None, owner: bool) -> bool:
    from awb import rulesync

    try:
        return rulesync.current(root, None if owner else Path.home() / ".claude" / "CLAUDE.md")
    except Exception:
        return False


def receipt(p: config.Paths, root: Path | None, owner: bool = False) -> tuple[list[str], list[str], str]:
    """The guards of a sealed start: (the keys checked, the keys missing, the line for the context). An owner
    session is checked without the project repository. A locked vault makes the line the locked start (T3)."""
    vault = _guard_vault(p)
    results = {
        "hooks": _guard_hooks,
        "gate": lambda: _guard_gate(root),
        "vault": lambda: vault == "ok",
        "keys": _guard_keys,
        "rules": lambda: _guard_rules(root, owner),
        "mode": lambda: config.mode() in config.KNOWN_MODES,
    }
    checked = [k for k, _, _ in GUARDS if not (owner and k == "gate")]
    missing = []
    for key in checked:
        try:
            ok = bool(results[key]())
        except Exception:
            ok = False
        if not ok:
            missing.append(key)
    if not missing:
        return checked, missing, RECEIPT_LINE % (len(checked), len(checked), _release())
    if missing == ["vault"] and vault == "locked":
        since = None
        try:
            from awb import vault as _vault

            since = _vault.ping_state(p.check_socket, timeout=RECEIPT_TIMEOUT)["since"]
        except Exception:
            pass
        return checked, missing, LOCKED_START % (since if since and SINCE_RE.match(since) else "unknown")
    names = [n for k, n, _ in GUARDS if k in missing]
    fixes = [f for k, _, f in GUARDS if k in missing]
    return checked, missing, REFUSED_START % ("guard" if len(names) == 1 else "guards", ", ".join(names),
                                              "; ".join(fixes))


def _receipt_mark(p: config.Paths, sid) -> Path | None:
    sid = re.sub(r"[^A-Za-z0-9_-]", "", sid or "")[:80] if isinstance(sid, str) else ""
    return p.shared / "sessions" / ("receipt-%s.ok" % sid) if sid else None


def _log_receipt(p: config.Paths, sid, root: Path | None, missing: list[str]) -> None:
    from awb import codes

    code = root.name if root is not None and codes.PROJECT_CODE_RE.fullmatch(root.name) else "-"
    sid = re.sub(r"[^A-Za-z0-9_-]", "", sid)[:80] if isinstance(sid, str) else ""
    line = "%s\t%s\t%s\t%s\n" % (datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                                 sid or "-", code, ",".join(missing) or "ok")
    try:
        folder = p.shared / "sessions"
        folder.mkdir(parents=True, exist_ok=True)
        with open(folder / RECEIPTS_LOG, "a", encoding="utf-8") as fh:
            fh.write(line)
    except OSError:
        pass


def refused_today(p: config.Paths, day: str | None = None) -> int | None:
    """How many sessions started today (UTC) with a refused receipt; None when the log cannot be read. For awb
    board show and /health."""
    day = day or datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d")
    try:
        text = (p.shared / "sessions" / RECEIPTS_LOG).read_text(encoding="utf-8", errors="replace")
    except FileNotFoundError:
        return 0
    except OSError:
        return None
    seen = set()
    for line in text.splitlines():
        f = line.split("\t")
        if len(f) == 4 and f[0].startswith(day) and f[3] != "ok":
            seen.add(f[1] if f[1] != "-" else line)
    return len(seen)


def refused_line(p: config.Paths) -> str:
    n = refused_today(p)
    return "sessions refused at start today: %s" % ("unknown" if n is None else n)


def _start_receipt(p: config.Paths, data: dict, root: Path | None, owner: bool) -> tuple[bool, str]:
    """The receipt of a start on a sealed host, logged: (all guards active, the line)."""
    _, missing, line = receipt(p, root, owner)
    sid = data.get("session_id")
    _log_receipt(p, sid, root, missing)
    mark = _receipt_mark(p, sid)
    if mark is not None and not owner:
        try:
            if missing:
                mark.unlink(missing_ok=True)
            else:
                mark.parent.mkdir(parents=True, exist_ok=True)
                mark.write_text(line + "\n", encoding="utf-8")
        except OSError:
            pass
    return not missing, line


def _prompt_receipt(data: dict) -> str | None:
    """The prompt hook of a sealed host: None when this session's receipt passed (at its start or at an earlier
    prompt), else the guards are checked again and the refusal line comes back while any is missing."""
    p = config.paths()
    mark = _receipt_mark(p, data.get("session_id"))
    if mark is not None and mark.is_file() and not mark.is_symlink():
        return None
    root = project_root(_session_folder(data))
    _, missing, line = receipt(p, root)
    if missing:
        return line
    if mark is not None:
        try:
            mark.parent.mkdir(parents=True, exist_ok=True)
            mark.write_text(line + "\n", encoding="utf-8")
        except OSError:
            pass
    return None


def owner_session_start(data: dict) -> int:
    """The session start of the owner on a sealed host: the receipt line alone, without the project repository.
    The owner's sessions are not Workbench sessions, so a missing guard is told and nothing is refused."""
    p = config.paths()
    root = project_root(_session_folder(data))
    ok, line = _start_receipt(p, data, root, owner=True)
    _emit(EVENTS["session-start"], line, None if ok else "awb: " + line)
    return OK


def hook_session_start(data: dict) -> int:
    p = config.paths()
    root = project_root(_session_folder(data))
    try:
        from awb import rulesync

        rulesync.remember(p, data.get("session_id"), root)
    except Exception:
        pass
    notes: list[str] = []
    parts: list[str] = []
    if _sealed():
        ok, line = _start_receipt(p, data, root, owner=False)
        if not ok:
            if root is not None:
                try:
                    _project_notes(p, root, data.get("session_id"), "this project")
                except Exception:
                    pass
            _emit(EVENTS["session-start"], line, "awb: " + line)
            return OK
        parts.append(line + "\n")
    if root is None:
        parts.append("No Workbench project here (no SCOPE.md in this folder or above it).\n")
    else:
        try:
            # the folder name is whatever a shell command chose (the red team of 2026-09-27): checked like a file
            shown = "this project" if name_hits(root.name) else root.name
            down: list[str] = []
        except Unavailable as exc:
            if not exc.rate:
                # locked: one line and no file (T3); the claim of the project still runs
                _project_notes(p, root, data.get("session_id"), "this project")
                _emit(EVENTS["session-start"], LOCKED_START % (exc.since or "unknown"))
                return OK
            shown, down = "this project", [str(exc)]
        parts.append("Workbench project %s. Codes only, never a name.\n" % shown)
        parts.append(_section(root, "SCOPE.md", None, notes, down))
        parts.append(_section(root, "STATE.md", STATE_LINES, notes, down))
        parts.append(_section(root, "OPEN.md", None, notes, down))
        notes += _project_notes(p, root, data.get("session_id"), shown)
    expired = _kb_expired(p)
    if expired is not None:
        notes.append("knowledge base: %d expired entries (awb kb expired)" % expired)
    days = _career_days(p)
    if days is not None and days > CAREER_DUE_DAYS:
        notes.append("career log: last update %d days ago (awb career update)" % days)
    body = "\n".join(parts)
    if len(body) > START_CHARS:
        body = body[:START_CHARS].rstrip() + "\n"
        notes.append("the files of this project were cut at %d characters for the start: read SCOPE.md, STATE.md "
                     "and OPEN.md in full when the work needs them" % START_CHARS)
    if notes:
        body += "\n## Checks\n\n%s\n" % "\n".join("- %s" % n for n in notes)
    _emit(EVENTS["session-start"], body)
    return OK


def for_this_user() -> bool:
    """False when the host file names a work user (`work_user`, written by seal/setup.sh) and this is another
    user. The seal installs the hooks as managed settings of the client, which run for every user of the host;
    the owner's own sessions are not Workbench sessions and are left alone. Only the root-owned host file counts
    (HOST_FILE), never AWB_CONF: a client started with its own AWB_CONF must not switch the hooks off."""
    try:
        text = Path(HOST_FILE).read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return True
    m = re.search(r"(?m)^[ \t]*work_user[ \t]*=[ \t]*(\S+)[ \t]*$", text)
    if not m:
        return True
    want = m.group(1)
    try:
        return pwd.getpwuid(os.geteuid()).pw_name == want
    except KeyError:
        return True


HANDLERS = {
    "prompt": hook_prompt,
    "pre-write": hook_pre_write,
    "pre-tool": hook_pre_tool,
    "post-write": hook_post_write,
    "stop": hook_stop,
    "session-start": hook_session_start,
}


class _OverBudget(Exception):
    """The hook ran longer than HOOK_BUDGET seconds."""


def _on_alarm(signum, frame) -> None:
    raise _OverBudget()


def _budget_possible() -> bool:
    return hasattr(signal, "SIGALRM") and threading.current_thread() is threading.main_thread()


def _over_budget(name: str) -> int:
    """What a hook answers when its budget is spent: the blocking hooks fail closed, the others say so."""
    if name in ("prompt", "pre-write", "pre-tool", "post-write"):
        print("awb hook %s: the check ran longer than %d seconds and was stopped; nothing passes unchecked. "
              "Make the input smaller, or run `awb check` on the file yourself." % (name, HOOK_BUDGET),
              file=sys.stderr)
        return BLOCK
    if name == "session-start":
        _emit(EVENTS["session-start"],
              "Warning from the Workbench: the start hook ran longer than %d seconds and was stopped. Run "
              "`awb check` on SCOPE.md, STATE.md and OPEN.md before you read them." % HOOK_BUDGET,
              "awb: the start hook was stopped after %d seconds" % HOOK_BUDGET)
        return OK
    print("awb hook %s: the hook ran longer than %d seconds and was stopped" % (name, HOOK_BUDGET), file=sys.stderr)
    return OK


def main(argv: list[str] | None = None) -> int:
    """`awb hook NAME` with the hook JSON on standard input. Exit 0 go on, 2 block, 1 usage or internal error."""
    from awb.cli import SafeParser

    ap = SafeParser(prog="awb hook", description="Entry points for the client hooks of working sessions.")
    ap.add_argument("name", choices=HOOKS)
    try:
        args = ap.parse_args(argv)
    except SystemExit as exc:
        # a usage error must not block the session: the client blocks on exit 2 only
        return OK if exc.code in (0, None) else ERROR
    if args.name == "selftest":
        problems = selftest()
        for line in problems:
            print("FAIL  the tool guard: %s" % line)
        print("PASS  the tool guard refuses its planted cases" if not problems else "# the tool guard failed")
        return ERROR if problems else OK
    if not for_this_user():
        if args.name != "session-start" or not _sealed():
            return OK
        try:
            raw = sys.stdin.read() if sys.stdin is not None else ""
            data = json.loads(raw) if raw.strip() else {}
            return owner_session_start(data if isinstance(data, dict) else {})
        except Exception as exc:
            print("awb hook session-start: the receipt failed (%s)" % type(exc).__name__, file=sys.stderr)
            return ERROR
    try:
        raw = sys.stdin.read() if sys.stdin is not None else ""
        data = json.loads(raw) if raw.strip() else {}
    except (ValueError, OSError):
        print("awb hook %s: the hook input is not JSON" % args.name, file=sys.stderr)
        return BLOCK if args.name == "pre-tool" else ERROR
    if not isinstance(data, dict):
        print("awb hook %s: the hook input is not a JSON object" % args.name, file=sys.stderr)
        return BLOCK if args.name == "pre-tool" else ERROR
    budget = _budget_possible()
    try:
        if budget:
            signal.signal(signal.SIGALRM, _on_alarm)
            signal.setitimer(signal.ITIMER_REAL, HOOK_BUDGET)
        return HANDLERS[args.name](data)
    except _OverBudget:
        return _over_budget(args.name)
    except Exception as exc:
        print("awb hook %s: internal error (%s)" % (args.name, type(exc).__name__), file=sys.stderr)
        return BLOCK if args.name == "pre-tool" else ERROR
    finally:
        if budget:
            signal.setitimer(signal.ITIMER_REAL, 0)
            signal.signal(signal.SIGALRM, signal.SIG_DFL)


if __name__ == "__main__":
    sys.exit(main())
