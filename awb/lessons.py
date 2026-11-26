"""Lesson producers L2, L3 and L4 (DESIGN 8): what a project session learned becomes lesson cards
(`awb kb pattern add --kind lesson`, awb/kbpattern.py), asked for at the right moments and distilled at the end.

L2, the stop hook (`due`): after a batch of work with a shell step that failed and then worked (the same command,
its program and subcommand), a reverted step (`git revert`, `git reset --hard`, `git restore`, `git checkout --`)
or a new line under `## Decisions` of STATE.md, the stop hook asks once with MESSAGE: write what you learned as
lesson cards or say that there is nothing. It is the harvest pattern (awb/harvest.py): the position up to which a
transcript was checked is kept per session in `<shared>/sessions/lessons-<session id>.json`, so the same steps
never ask twice and a session is asked once per batch, never in a loop. A lesson card written after the last such
step counts as the answer. The decision lines of STATE.md are kept per project as short hashes in
`<shared>/sessions/lessons-decisions-<code>.json`; the first check of a project only takes them in.

L3, the compaction: the PreCompact hook of a project session leaves the mark `<shared>/sessions/lessons-compact-<session
id>` (`mark_compaction`) and the first prompt after the compaction takes it away and asks the same question once
(`after_compaction`, COMPACT_MESSAGE).

Neither asks in a side session (.side/NAME) or an ask project and neither runs for a turn that answers a
notification.

L4, `awb lessons distill CODE`, run as the work user in the folder of the project: reads the transcripts of the
work user's sessions of that project only (P6): the folders under ~/.claude/projects that the client names after
the project folder, a subfolder of it or one of its side sessions, never another project, never the owner's or the
desk's sessions. From the lines it has not read before (`<shared>/sessions/lessons-distill-<code>.json`) it takes
the episodes: a step that failed and then worked with its first error line, a reverted step and the new decision
lines of STATE.md, each with what the session said next. The digest keeps a word only when it is plain English or a
TCP term (the rules lists of awb/kbpattern.py, without the first names of rules/first-names.txt) and writes [x] for
every other: no name, no code of a project, no identifier, home path, secret or price is left and every line
passes the text screens of `kb add`. The project's own code reads "this project". It writes notes/lessons-digest.md
and starts `awb side start lessons` with it as the brief on a cheap model (SIDE_MODEL), which turns it into lesson
cards. A side session lessons that still runs is left alone (the digest waits for the next run); a finished one is
dropped first, its cards are in the knowledge base already. Nothing new: no digest and no side session.
`awb close` runs the distill once more before it closes a live project (`before_close`); it never stops a close.

Only commands, error lines, decision lines and the session's own words are read and only the screened words
leave into the digest.

    awb lessons distill CODE

Exit codes: 0 ok, 2 usage or error.
"""
from __future__ import annotations

import functools
import hashlib
import json
import os
import re
import stat
import sys
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

from awb import codes, config

MAX_TRANSCRIPT = 64 * 1024 * 1024
MAX_PIECE = 300
MAX_EPISODES = 40
SIDE_NAME = "lessons"
SIDE_MODEL = "haiku"
SIDE_EFFORT = "low"
DIGEST = "notes/lessons-digest.md"
STATE = "STATE.md"
_SID_RE = re.compile(r"[^A-Za-z0-9_-]")
_ENCODE_RE = re.compile(r"[^A-Za-z0-9]")
"""The client names the transcript folder of a session after its first folder, every other character a -."""

REVERT_RE = re.compile(r"\bgit\s+(?:revert\b|reset\s+--(?:hard|keep|merge)\b|restore\b|checkout\s+(?:\S+\s+)?--\s)")
"""A step taken back: a revert, a hard reset, a restore of files, a checkout of files."""
LESSON_RE = re.compile(r"\bawb\s+kb\s+pattern\s+(?:add\b[^\n]*--kind[= ]lesson\b|support\b)")
"""A lesson card written or this project counted for one: the answer to the question."""
_PATTERN_RE = re.compile(r"\bawb\s+kb\s+pattern\b")
_DECISIONS_RE = re.compile(r"^##\s+Decisions\s*$")
_ITEM_RE = re.compile(r"^\s*(?:[-*]|\d+[.)])\s+(\S.*)$")
_ASSIGN_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*=\S*")
_SUB_RE = re.compile(r"[a-z][a-z0-9-]*")

ASK = ("Write what you learned as lesson cards, one lesson each, without asking him: awb kb pattern add --kind "
       "lesson --sub dead-end|working-path|gotcha|decision (a decision needs --why) --package SLUG --tag T "
       "STATEMENT, a rule another project can use, codes only, no name and no price. When there is nothing to "
       "learn, end with one line that says so.")
MESSAGE = "Lessons before you stop: this session %s since the last check. " + ASK
COMPACT_MESSAGE = "Lessons after the compaction: before you go on with this prompt, look back at the work so far. " + ASK


class LessonsError(Exception):
    """A fixed message; never the content of a transcript."""


# --------------------------------------------------------------------------- reading a transcript


def _open_transcript(path: Path):
    """The transcript as a text file, never through a link; None when it is no regular file or too large."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except OSError:
        return None
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode) or st.st_size > MAX_TRANSCRIPT:
            os.close(fd)
            return None
        return os.fdopen(fd, "r", encoding="utf-8", errors="replace")
    except OSError:
        os.close(fd)
        return None


def _result_text(block: dict) -> str:
    content = block.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(c.get("text", "") for c in content
                         if isinstance(c, dict) and isinstance(c.get("text"), str))
    return ""


def events(transcript: Path, start: int = 0) -> tuple[list[tuple], int]:
    """The events of a transcript from line `start` on and the number of lines read:
    ("cmd", command) for a shell command the session ran, ("result", command, failed, first error line) when its
    result came back, ("text", text) for what the session said and ("write", file name) for a file it wrote with
    a tool. A file that cannot be read gives no event."""
    out: list[tuple] = []
    pending: dict[str, str] = {}
    n = start
    fh = _open_transcript(Path(transcript))
    if fh is None:
        return [], start
    try:
        with fh:
            for n, line in enumerate(fh, start=1):
                if n <= start:
                    continue
                try:
                    obj = json.loads(line)
                except ValueError:
                    continue
                if not isinstance(obj, dict):
                    continue
                content = (obj.get("message") or {}).get("content") if isinstance(obj.get("message"), dict) else None
                if not isinstance(content, list):
                    continue
                for c in content:
                    if not isinstance(c, dict):
                        continue
                    if obj.get("type") == "assistant" and c.get("type") == "tool_use":
                        args = c.get("input") if isinstance(c.get("input"), dict) else {}
                        cmd = args.get("command")
                        if isinstance(cmd, str):
                            out.append(("cmd", cmd))
                            if isinstance(c.get("id"), str):
                                pending[c["id"]] = cmd
                        target = args.get("file_path")
                        if isinstance(target, str) and c.get("name") in ("Write", "Edit", "MultiEdit"):
                            out.append(("write", os.path.basename(target)))
                    elif obj.get("type") == "assistant" and c.get("type") == "text" and isinstance(c.get("text"), str):
                        out.append(("text", c["text"]))
                    elif obj.get("type") == "user" and c.get("type") == "tool_result":
                        cmd = pending.pop(c.get("tool_use_id"), None) if isinstance(c.get("tool_use_id"), str) else None
                        if cmd is None:
                            continue
                        failed = c.get("is_error") is True
                        first = ""
                        if failed:
                            lines = [x.strip() for x in _result_text(c).splitlines() if x.strip()]
                            lines = [x for x in lines if not re.fullmatch(r"(?i)exit code \d+", x)]
                            first = lines[0] if lines else ""
                        out.append(("result", cmd, failed, first))
    except OSError:
        return [], start
    return out, max(n, start)


def command_key(cmd: str) -> str:
    """The program and its subcommand of the first simple command that is no cd: `terraform apply`, `awb lab`,
    `pytest`. Leading assignments, sudo, env and timeout are skipped."""
    for part in re.split(r"&&|\|\||[;|\n]", cmd):
        words = part.split()
        while words and (_ASSIGN_RE.fullmatch(words[0]) or words[0] in ("sudo", "env", "exec", "command")):
            words = words[1:]
        if words and words[0] == "timeout":
            words = words[2:] if len(words) > 1 and words[1][:1].isdigit() else words[1:]
        if not words or words[0] == "cd":
            continue
        prog = os.path.basename(words[0])
        sub = words[1] if len(words) > 1 and _SUB_RE.fullmatch(words[1]) else ""
        return ("%s %s" % (prog, sub)).strip()
    return ""


# --------------------------------------------------------------------------- the marks


def _sid(session_id) -> str:
    return _SID_RE.sub("", session_id if isinstance(session_id, str) else "")[:80] or "unknown"


def _sessions(p: config.Paths) -> Path:
    return p.shared / "sessions"


def _load(path: Path) -> dict:
    try:
        if path.is_symlink():
            return {}
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError, UnicodeDecodeError):
        return {}


def _save(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.is_symlink():
        return
    tmp = path.with_name(".%s.tmp" % path.name)
    tmp.write_text(json.dumps(data, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def _hash(line: str) -> str:
    return hashlib.sha256(" ".join(line.split()).encode("utf-8")).hexdigest()[:16]


def decisions(root: Path) -> list[str]:
    """The item lines under `## Decisions` of the project's STATE.md, as written; empty when it cannot be read."""
    try:
        path = Path(root) / STATE
        if path.is_symlink():
            return []
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return []
    out, inside = [], False
    for line in text.splitlines():
        if line.startswith("## "):
            inside = bool(_DECISIONS_RE.match(line.rstrip()))
            continue
        m = _ITEM_RE.match(line) if inside else None
        if m:
            out.append(m.group(1).strip())
    return out


def _new_decisions(p: config.Paths, root: Path) -> int:
    """How many decision lines of STATE.md the last check of the project had not seen; the first check takes them
    in and counts none."""
    path = _sessions(p) / ("lessons-decisions-%s.json" % root.name)
    old = _load(path).get("seen")
    now = sorted({_hash(d) for d in decisions(root)})
    try:
        _save(path, {"seen": now})
    except OSError:
        pass
    if not isinstance(old, list):
        return 0
    return len(set(now) - set(old))


# --------------------------------------------------------------------------- L2 and L3


def due(p: config.Paths, transcript, session_id, root: Path | None) -> str | None:
    """MESSAGE when the session, since the last check, had a shell step fail and then work, reverted a step or a
    new decision line stands in STATE.md and wrote no lesson card after it; None otherwise. Moves the mark either
    way, so the same steps are never asked about twice."""
    if not transcript or root is None:
        return None
    path = _sessions(p) / ("lessons-%s.json" % _sid(session_id))
    try:
        mark = int(_load(path).get("line", 0))
    except (TypeError, ValueError):
        mark = 0
    evs, end = events(Path(transcript), mark)
    if end < mark:            # a new transcript under the same session id: start again
        evs, end = events(Path(transcript), 0)
    failed: set[str] = set()
    triggers: list[tuple[int, str]] = []
    last_lesson = -1
    for i, ev in enumerate(evs):
        if ev[0] == "cmd":
            if LESSON_RE.search(ev[1]):
                last_lesson = i
            elif REVERT_RE.search(ev[1]):
                triggers.append((i, "reverted a step"))
        elif ev[0] == "result" and not _PATTERN_RE.search(ev[1]):
            key = command_key(ev[1])
            if not key:
                continue
            if ev[2]:
                failed.add(key)
            elif key in failed:
                failed.discard(key)
                triggers.append((i, "had a step fail and then work"))
    new = _new_decisions(p, root)
    try:
        _save(path, {"line": end})
    except OSError:
        pass
    reasons = [why for i, why in triggers if i > last_lesson]
    if new and last_lesson < 0:
        reasons.append("wrote %d new decision(s) into STATE.md" % new)
    if not reasons:
        return None
    return MESSAGE % " and ".join(dict.fromkeys(reasons))


def _compact_mark(p: config.Paths, session_id) -> Path:
    return _sessions(p) / ("lessons-compact-%s" % _sid(session_id))


def mark_compaction(p: config.Paths, session_id) -> None:
    """The PreCompact hook of a project session: the next prompt of the session asks for lessons (L3)."""
    path = _compact_mark(p, session_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.is_symlink():
        path.write_text(date.today().isoformat() + "\n", encoding="utf-8")


def after_compaction(p: config.Paths, session_id) -> str | None:
    """COMPACT_MESSAGE once, for the first prompt after a compaction of the session; the mark goes away."""
    path = _compact_mark(p, session_id)
    try:
        path.unlink()
    except FileNotFoundError:
        return None
    return COMPACT_MESSAGE


# --------------------------------------------------------------------------- L4: the digest


def encoded(folder: Path) -> str:
    return _ENCODE_RE.sub("-", str(folder))


def transcripts(root: Path, home: Path, p: config.Paths | None = None) -> list[Path]:
    """The transcripts of this project's sessions of the user whose home is `home` (P6): the top-level .jsonl files
    of the folders under home/.claude/projects named after the project folder, a folder inside it (a subfolder,
    a side session) and nothing else. A folder whose name fits another project folder better is left out."""
    base = Path(home) / ".claude" / "projects"
    own = {encoded(root), encoded(Path(os.path.realpath(root)))}
    others: set[str] = set()
    for parent in {Path(root).parent, Path(os.path.realpath(root)).parent} | (
            {Path(p.projects_root)} if p is not None else set()):
        try:
            for d in parent.iterdir():
                if codes.is_project_code(d.name) and d.name != Path(root).name:
                    others.add(encoded(d))
        except OSError:
            continue
    try:
        folders = sorted(base.iterdir())
    except OSError:
        return []
    out: list[Path] = []
    for d in folders:
        name = d.name
        fits = [e for e in own if name == e or name.startswith(e + "-")]
        if not fits:
            continue
        if any(len(o) > max(map(len, fits)) and (name == o or name.startswith(o + "-")) for o in others):
            continue
        try:
            if not stat.S_ISDIR(os.lstat(d).st_mode):
                continue
            files = sorted(d.iterdir())
        except OSError:
            continue
        for f in files:
            try:
                if f.suffix == ".jsonl" and stat.S_ISREG(os.lstat(f).st_mode):
                    out.append(f)
            except OSError:
                continue
    return out


SHORT_WORDS = frozenset("a an as at be by do go he if in is it me my no of on or so to up us we ok id vm ip os".split())
"""English words under three letters: the common-words list starts at three."""


@functools.lru_cache(maxsize=2)
def _vocabulary(stamp: tuple) -> frozenset[str]:
    from awb import kbpattern, patterns, words

    first = words.load_plain(patterns.RULES_DIR / "first-names.txt")
    return frozenset((kbpattern._rules_vocabulary(stamp) | SHORT_WORDS | {"awb", "kb"}) - first)


def vocabulary() -> frozenset[str]:
    """Words that identify no one and no project: the rules lists of awb/kbpattern.py without the first names."""
    from awb import kbpattern

    return _vocabulary(kbpattern._rules_stamp())


_WORD_RE = re.compile(r"[A-Za-z]+(?:['-][A-Za-z]+)*")
_NUM_RE = re.compile(r"\d{1,3}")
_LEAD = "([{\"'`<"
_TRAIL = ".,:;!?)]}\"'`>"


def screen(text: str, code: str | None = None) -> str:
    """`text` with every word that is not plain English or a TCP term written [x], on one line and at most
    MAX_PIECE characters; the project's own code reads "this project". A number of up to three digits stays."""
    if code:
        text = re.sub(r"(?i)(?<![\w-])%s(?![\w-])" % re.escape(code), "this project", text)
    vocab = vocabulary()
    out: list[str] = []
    for tok in text.split():
        core = tok.lstrip(_LEAD).rstrip(_TRAIL)
        tail = tok[len(tok.rstrip(_TRAIL)):]
        tail = tail[:1] if tail[:1] in ".,:;!?" else ""
        ok = False
        if _WORD_RE.fullmatch(core) and not codes.is_project_code(core.lower()) and not sum(codes.count_codes(core)):
            ok = all(part.casefold() in vocab for part in re.split(r"['-]", core) if part)
        elif _NUM_RE.fullmatch(core):
            ok = True
        word = (core + tail) if ok else "[x]" + tail
        if word.startswith("[x]") and out and out[-1].startswith("[x]") and not out[-1][3:]:
            out[-1] = word
            continue
        out.append(word)
    line = " ".join(out)
    if len(line) > MAX_PIECE:
        line = line[:MAX_PIECE].rsplit(" ", 1)[0] + " ..."
    return line


def _passes(text: str, p: config.Paths) -> str | None:
    """`text` when the screens of `kb add` take it, else the text without its numbers when they take that, else
    None."""
    from awb import kb

    if not kb.text_problems("the digest", text, p.register):
        return text
    bare = re.sub(r"\d+", "[x]", text)
    return bare if not kb.text_problems("the digest", bare, p.register) else None


@dataclass
class Digest:
    episodes: list[str] = field(default_factory=list)
    read: list[Path] = field(default_factory=list)
    marks: dict[str, int] = field(default_factory=dict)
    seen: list[str] = field(default_factory=list)


def _episodes(evs: list[tuple], code: str) -> list[str]:
    out: list[str] = []
    failed: dict[str, tuple[str, str]] = {}
    waiting: list[int] = []
    for ev in evs:
        if ev[0] == "text":
            said = " ".join(ev[1].split())
            if said and waiting:
                for i in waiting:
                    out[i] += " Said next: %s" % screen(said, code)
                waiting = []
        elif ev[0] == "cmd" and REVERT_RE.search(ev[1]):
            out.append("A step was taken back: %s." % screen(ev[1], code))
            waiting.append(len(out) - 1)
        elif ev[0] == "result" and not _PATTERN_RE.search(ev[1]):
            key = command_key(ev[1])
            if not key:
                continue
            if ev[2]:
                failed[key] = (ev[1], ev[3])
            elif key in failed:
                cmd, err = failed.pop(key)
                out.append("A step failed, then worked. Failed: %s. Error: %s. Worked: %s."
                           % (screen(cmd, code), screen(err, code) or "none shown", screen(ev[1], code)))
                waiting.append(len(out) - 1)
    return out


def collect(p: config.Paths, root: Path, home: Path) -> Digest:
    """The episodes of the project's transcripts since the last distill and the new decision lines of STATE.md,
    screened, with the marks to keep once they are handed on."""
    code = root.name
    state = _load(_sessions(p) / ("lessons-distill-%s.json" % code))
    marks = state.get("files") if isinstance(state.get("files"), dict) else {}
    seen = set(state.get("seen")) if isinstance(state.get("seen"), list) else set()
    d = Digest()
    for f in transcripts(root, home, p):
        key = "%s/%s" % (f.parent.name, f.name)
        try:
            start = int(marks.get(key, 0))
        except (TypeError, ValueError):
            start = 0
        evs, end = events(f, start)
        if end < start:
            evs, end = events(f, 0)
        d.read.append(f)
        d.marks[key] = end
        d.episodes += _episodes(evs, code)
    lines = decisions(root)
    d.seen = sorted({_hash(x) for x in lines})
    d.episodes += ["A decision written into STATE.md: %s" % screen(x, code) for x in lines if _hash(x) not in seen]
    d.episodes = [e for e in (_passes(e, p) for e in d.episodes) if e][-MAX_EPISODES:]
    return d


BRIEF = """# Lessons digest

Made by awb lessons distill on {day} from the transcripts of this project's own sessions. Every word that is not
plain English or a TCP term reads [x]: no name, code, identifier, path, secret or price is in it.

Your task: turn what another project could use into lesson cards, one lesson each, in plain words:

    awb kb pattern add --kind lesson --sub dead-end|working-path|gotcha|decision --package lessons --tag TAG STATEMENT

A dead end is a way that led nowhere, a working path a way that worked, a gotcha a trap and a decision a choice
with its reason (--why TEXT). TAG is a tag of rules/tags.txt. Write each statement as a rule that holds without this
project: no code, no name and no number of this project. When kb pattern add says a card is too similar, count this
project for it with awb kb pattern support PAT-XXXX instead. Skip an episode that teaches nothing. List the card
ids in SIDE-RESULT.md or write there that there was nothing to learn.

## Episodes

"""


def render(d: Digest, today: date | None = None) -> str:
    body = "".join("%d. %s\n" % (i, e) for i, e in enumerate(d.episodes, start=1))
    return BRIEF.format(day=(today or date.today()).isoformat()) + body


def _write_digest(root: Path, text: str) -> None:
    from awb import side

    notes = root / "notes"
    if os.path.lexists(notes) and not side._plain(root, notes, directory=True):
        raise LessonsError("notes/ of the project is no plain folder")
    notes.mkdir(exist_ok=True)
    side._write(root, root / DIGEST, text)


def _start_side(root: Path) -> tuple[bool, str]:
    """Start the side session lessons with the digest; (started, the line to show)."""
    from awb import side

    dropped = ""
    if os.path.lexists(root / side.SIDE_DIR / (SIDE_NAME + ".state")):
        st, _ = side._state(side._paths(root, SIDE_NAME))
        if st == "running":
            return False, ("the side session %s still runs: the digest waits, run awb lessons distill %s again when "
                           "it is done" % (SIDE_NAME, root.name))
        side.drop(SIDE_NAME, cwd=root)
        dropped = "the earlier side session %s (%s) was removed; " % (SIDE_NAME, st)
    return True, dropped + side.start(SIDE_NAME, DIGEST, SIDE_MODEL, SIDE_EFFORT, cwd=root)


def distill(p: config.Paths, code: str, cwd: Path | None = None, home: Path | None = None) -> list[str]:
    """L4: write the digest of the project's own transcripts and start the side session that makes lesson cards
    of it. Returns the lines to show. Raises LessonsError."""
    from awb import side

    if not isinstance(code, str) or not codes.is_project_code(code):
        raise LessonsError("not a project code")
    if not config.is_work_user():
        raise LessonsError("run awb lessons distill as the work user: it reads the transcripts of the work user's "
                           "sessions of the project and no one else's")
    try:
        root = side.project_root(Path(cwd) if cwd is not None else None)
    except side.SideError as err:
        raise LessonsError(str(err)) from None
    if root.name != code:
        raise LessonsError("run awb lessons distill %s in the folder of that project" % code)
    d = collect(p, root, Path(home) if home is not None else Path.home())
    state_path = _sessions(p) / ("lessons-distill-%s.json" % code)
    if not d.episodes:
        _save(state_path, {"files": d.marks, "seen": d.seen})
        return ["nothing new to distill since the last run (%d transcript(s) read): no digest, no side session"
                % len(d.read)]
    _write_digest(root, render(d))
    lines = ["digest of %d episode(s) from %d transcript(s) written to %s" % (len(d.episodes), len(d.read), DIGEST)]
    try:
        started, line = _start_side(root)
    except side.SideError as err:
        raise LessonsError("the side session %s did not start (%s); the digest stays in %s"
                           % (SIDE_NAME, err, DIGEST)) from None
    lines.append(line)
    if started:
        _save(state_path, {"files": d.marks, "seen": d.seen})
    return lines


def before_close(p: config.Paths, code: str, folder: Path) -> list[str]:
    """`awb close` runs the distill once more before it closes a live project. Never raises: a close never waits
    for lessons."""
    try:
        return distill(p, code, cwd=folder)
    except LessonsError as err:
        return ["lessons not distilled: %s" % err]
    except Exception as exc:
        return ["lessons not distilled (%s)" % type(exc).__name__]


def main(argv: list[str] | None = None) -> int:
    from awb.cli import SafeParser

    ap = SafeParser(prog="awb lessons", description="Lesson cards from the project's own transcripts.")
    sub = ap.add_subparsers(dest="command", required=True, parser_class=SafeParser)
    s = sub.add_parser("distill", help="digest this project's transcripts without names and start the side session "
                                       "lessons that turns it into lesson cards (as the work user, in the project)")
    s.add_argument("code", metavar="CODE")
    args = ap.parse_args(argv)
    try:
        for line in distill(config.paths(), args.code):
            print(line)
    except LessonsError as err:
        print("awb lessons: %s" % err, file=sys.stderr)
        return 2
    except OSError as err:
        print("awb lessons: %s (operating system error)" % type(err).__name__, file=sys.stderr)
        return 2
    return 0
