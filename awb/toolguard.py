"""The pre-tool hook: a working session stays in its project, a build session in its own clone.

A part of the client hooks (awb/hooks.py, F1 step 6); the docstring there says what `pre-tool` refuses for each of
the two users. `guard` and `build_guard` read every path of a tool call (for a shell command each path after the
shell's quote removal, against the folder each cd leaves it in) and answer with the class of a refused place, never
the path. The exit codes of the hooks, `project_root` and `_sealed` live here because the guard needs them and
imports nothing of the hooks. `selftest` plants refused cases for both guards (`awb hook selftest`, `awb seal check`).
"""
from __future__ import annotations

import datetime
import os
import re
import sys
from datetime import date
from pathlib import Path

from awb import config


# --------------------------------------------------------------------------- the exit codes, the host and the project

OK = 0
ERROR = 1
BLOCK = 2
_SCOPE_MAX_DEPTH = 40
HOST_FILE = Path(config.HOST_CONF)
"""The host file of a sealed host; its `work_user` says whose sessions the hooks check."""


def _sealed() -> bool:
    """True when the root host file names a work user (the seal ran on this host)."""
    try:
        text = Path(HOST_FILE).read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return False
    return re.search(r"(?m)^[ \t]*work_user[ \t]*=[ \t]*\S+[ \t]*$", text) is not None


def project_root(start: str | os.PathLike | None) -> Path | None:
    """The nearest folder at or above `start` with a SCOPE.md. None when there is none."""
    if not start:
        return None
    here = Path(start)
    if not here.is_absolute():
        here = Path.cwd() / here
    try:
        folder = here.is_dir()
    except OSError:
        # a place this user cannot read (a folder of another user, a name too long): no project of its own,
        # and the session, which runs as the same user, cannot reach it either (PD2)
        return None
    if not folder:
        here = here.parent
    return config.scope_root(here, depth=_SCOPE_MAX_DEPTH)


# --------------------------------------------------------------------------- pre-tool: a session stays in its project

GUARD_MATCHER = "Read|Edit|MultiEdit|NotebookEdit|Write|Glob|Grep|Bash|Monitor|Workflow"
"""The tools whose target paths the pre-tool hook guards: every file tool, every tool of the client that runs a
shell command (Bash, Monitor) and the Workflow tool (PD3)."""
GUARD_TOOLS = tuple(GUARD_MATCHER.split("|"))
SHELL_TOOLS = ("Bash", "Monitor")
"""The guarded tools whose `command` runs in a shell."""
WORKFLOW_TOOL = "Workflow"
WORKFLOW_DIR = Path(os.path.abspath(__file__)).parent.parent / "workflows"
WORKFLOW_FILES = ("review.js", "request-check.js", "final-check.js")
"""The workflows a session on a sealed host may run (PD3): the Workbench's own saved scripts in the installed code,
by their scriptPath. Their agents run in the session's client, so their tool calls meet the same managed settings,
deny rules and hooks as the session's own. An inline script or a workflow by name is refused: its script could
start another workflow from any file (`workflow({scriptPath})` reads a file inside the client, where no hook
sees it)."""
DENIED_TOOLS = ("mcp__*", "PowerShell", "LSP", "NotebookRead", "Artifact", "ArtifactComments", "ArtifactData",
                "DesignSync", "SendUserFile", "EnterWorktree", "ExitWorktree", "RemoteTrigger",
                "ListMcpResourcesTool", "ReadMcpResourceTool", "ReadMcpResourceDirTool")
"""The deny rules of the work user's settings: the connectors, and every tool of the client that reads or sends a
file, runs a command or starts work elsewhere and is not guarded. The rest of the client's tools (agents, tasks,
questions, skills, the web tools, plan mode, schedules of a prompt) reach no path; their prompts and their agents'
tool calls meet the hooks. Workflow left the list with PD3: the guard passes the Workbench's own workflows only."""
ANOTHER_PROJECT = "another project"
CLIENT_FOLDER = "the client's folder"
KEY_FOLDER = "a key folder"
SESSION_RECORDS = "the session records"
UNREADABLE = "a path the guard cannot read"
UNKNOWN_PROJECT = "an unknown project"
FOREIGN_WORKFLOW = "a workflow other than the Workbench's own"
"""The classes of a refused place; a refusal names the class, never the path."""
KEY_FOLDERS = (".ssh", ".config")
_PROJECT_DIR_RE = re.compile(r"^(?:tcp|hcs)-")
_GLOB_CHARS = "*?[{"
_SPLIT_RE = re.compile(r"[\s'\"`;|&<>(),=:]+")
_SID_RE = re.compile(r"[A-Za-z0-9_-]{1,64}")
_NAME_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_VAR_RE = re.compile(r"\$[A-Za-z_{(]")
_START_VAR_RE = re.compile(r"\$(?:\{(HOME|PWD)\}|(HOME|PWD)(?![A-Za-z0-9_]))")
_DIRSTACK_RE = re.compile(r"~[+-]?\d+")
_CD_WORDS = ("cd", "pushd")
_ARITH_RE = re.compile(r"[A-Za-z0-9_ \t+\-*/%<>=!&|^~?:,.$#()]*")
"""The text of a plain arithmetic expansion: no quote, no backquote, no $( and no brace."""


class _Refused(Exception):
    """A tool call the guard refuses; the message is the class of the place."""


class _Text:
    """A place read out of text rather than out of a path argument (PD2): a token of a word with a blank in it (a
    quoted sentence, a sed or awk program, a python -c line), a token of a here-document or here-string body, a
    sed or awk expression and a bare / glued to an option (-F/, -d/). Such a place is refused only when it lies
    inside a refused place (another project, a key folder, the client's folder, the session records), never
    because it holds one: the / of `s/a/ /` or of `print(a / b)` is no search of the root. A token of text the
    guard cannot read is left out."""

    __slots__ = ("path",)

    def __init__(self, path: Path):
        self.path = path

    def __eq__(self, other) -> bool:
        return isinstance(other, _Text) and other.path == self.path

    def __hash__(self) -> int:
        return hash(("text", self.path))


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


def _based(text: str) -> Path:
    """The folder a path reaches before its first glob character. A `..` after a glob character climbs from a
    folder the guard cannot know, so it is refused."""
    if "\x00" in text:
        raise _Refused(UNKNOWN_PROJECT)
    cut = min((text.find(c) for c in _GLOB_CHARS if c in text), default=-1)
    if cut >= 0 and ".." in text[cut:].split("/"):
        raise _Refused(UNREADABLE)
    return Path(_glob_base(text))


def _absolute(raw: str, cwd: Path, home: Path) -> Path:
    text = _expand(raw, home)
    return _based(text if text.startswith("/") else os.path.join(str(cwd), text))


def _one_path(raw: str, cwds: list[Path], home: Path, dirstack: bool) -> list[Path]:
    """The places one token of a command line reaches: absolute, under the home, or relative to every folder the
    command may stand in. $HOME, $PWD and ~+ are read; any other expansion inside a path is refused."""
    if _VAR_RE.search(raw):
        m = _START_VAR_RE.match(raw)
        if m is None or _VAR_RE.search(raw[m.end():]):
            if "/" in raw or ".." in raw:
                raise _Refused(UNREADABLE)
            return []
        raw = (str(home) if (m.group(1) or m.group(2)) == "HOME" else ".") + raw[m.end():]
    if raw.startswith("~"):
        head, sep, tail = raw.partition("/")
        if head == "~":
            raw = str(home) + sep + tail
        elif head == "~+":
            raw = "." + sep + tail
        elif head == "~-" or (dirstack and _DIRSTACK_RE.fullmatch(head)):
            raise _Refused(UNREADABLE)
        else:
            raw = os.path.expanduser(raw)
    if raw.startswith("/"):
        return [_based(raw)]
    return [_based(os.path.join(str(c), raw)) for c in cwds]


def _token_paths(tok: str, cwds: list[Path], home: Path, dirstack: bool, text: bool = False) -> list:
    """The places of one token. A token that starts with - or @ (an option, a curl data file) is read from every
    point where a path may begin: after the option letter, after @, at the first /, ~, $ or `..`. With `text` the
    token comes out of text (see _Text): its places are _Text and a token the guard cannot read is left out. A
    bare / glued to an option (-F/, -d/, -t/) is a separator, so it is text as well."""
    tok = tok.strip("{}[]!")
    if not tok:
        return []
    cands = [tok]
    if tok[0] in "-@":
        cands = []
        if tok[0] == "-" and tok[1:2] != "-":
            cands.append(tok[2:])
        if "@" in tok:
            cands.append(tok[tok.index("@") + 1:])
        for start in ("/", "~", "$", ".."):
            if start in tok:
                cands.append(tok[tok.index(start):])
    out: list = []
    for cand in dict.fromkeys(cands):
        if cand and cand[0] not in "-@":
            lenient = text or (cand == "/" and tok[0] == "-")
            try:
                found = _one_path(cand, cwds, home, dirstack)
            except _Refused:
                if lenient:
                    continue
                raise
            out += [_Text(f) for f in found] if lenient else found
    return out


class _Word:
    """One word of a command line after the shell's quote removal: its text, its units (a run of plain characters,
    one quoted string, one escaped character) and whether the shell computes part of it at run time."""

    def __init__(self) -> None:
        self.text: list[str] = []
        self.units: list[str] = []
        self.dynamic = False

    def add(self, s: str, unit: str) -> None:
        self.text.append(s)
        if unit != "plain" or not self.units or self.units[-1] != "plain":
            self.units.append(unit)

    @property
    def value(self) -> str:
        return "".join(self.text)

    @property
    def mixed(self) -> bool:
        """A quote or an escape inside the word, next to other text: `tc''p-x`, `tcp\\-x`, `--out="a/b"`."""
        return len(self.units) > 1 and any(u != "plain" for u in self.units)


class _ShellReader:
    """Reads a command line the way the shell splits it, far enough for the guard: words after quote removal,
    the commands inside $(...), backquotes and process substitution, here-documents as data, comments dropped.
    What it cannot read raises _Refused(UNREADABLE)."""

    EXACT = (";", "&&", "\n")
    """The separators after which the next command runs in the same shell, so a cd before them holds after them."""

    def __init__(self, s: str, home: Path):
        self.s, self.i, self.home = s, 0, home
        self.items: list = []
        self.nested: list[list] = []
        self.data: list[str] = []
        self.pending: list[tuple[str, bool]] = []

    def fail(self):
        raise _Refused(UNREADABLE)

    def read(self, stop: str | None = None) -> None:
        """Words and separators up to the end, or up to the `stop` character that closes a substitution."""
        s, cur, depth = self.s, None, 0

        def end():
            nonlocal cur
            if cur is not None:
                self.items.append(cur)
                cur = None

        while self.i < len(s):
            c = s[self.i]
            if (stop == "`" and c == "`") or (stop == ")" and c == ")" and depth == 0):
                end()
                self.i += 1
                return
            if c in " \t":
                end()
                self.i += 1
            elif c == "\n":
                end()
                self.items.append("\n")
                self.i += 1
                self.bodies()
            elif c == "#" and cur is None:
                nl = s.find("\n", self.i)
                self.i = len(s) if nl < 0 else nl
            elif s.startswith("&>", self.i):
                end()
                self.i += 1
            elif c in ";&|()":
                end()
                sep = s[self.i:self.i + 2] if s.startswith(("&&", "||"), self.i) else c
                self.items.append(sep)
                depth += 1 if c == "(" else -1 if c == ")" else 0
                self.i += len(sep)
            elif c in "<>":
                end()
                if s.startswith("<<<", self.i):
                    self.i += 3
                elif s.startswith("<<", self.i):
                    self.i += 2
                    strip = s.startswith("-", self.i)
                    self.i += 1 if strip else 0
                    while self.i < len(s) and s[self.i] in " \t":
                        self.i += 1
                    delim = self.word_at()
                    if delim is None:
                        self.fail()
                    quoted = any(u != "plain" for u in delim.units)
                    self.pending.append((delim.value, strip, quoted, self.feeds_a_shell()))
                elif s.startswith(("<(", ">("), self.i):
                    self.i += 2
                    self.sub(")")
                else:
                    self.i += 2 if s[self.i + 1:self.i + 2] in ("&", "|", ">") else 1
            else:
                if cur is None:
                    cur = _Word()
                self.unit(cur)
        if stop is not None:
            self.fail()
        end()

    def sub(self, stop: str) -> None:
        """The command inside a substitution, read into a list of its own: it runs in a subshell."""
        saved, self.items = self.items, []
        self.read(stop)
        self.nested.append(self.items)
        self.items = saved

    def word_at(self) -> _Word | None:
        """One word from the current position (a here-document delimiter)."""
        w = None
        while self.i < len(self.s) and self.s[self.i] not in " \t\n;&|()<>":
            w = w or _Word()
            self.unit(w)
        return w

    def feeds_a_shell(self) -> bool:
        """True when the command a here-document is opened on runs a shell (bash, sh, source, .): its body is a
        command line then, not data."""
        words = []
        for item in reversed(self.items):
            if not isinstance(item, _Word):
                break
            words.append(item)
        words.reverse()
        if words and not words[0].dynamic and words[0].value in ("source", "."):
            return True
        return any(not w.dynamic and os.path.basename(w.value) in _SHELLS for w in words)

    def bodies(self) -> None:
        """The here-documents opened on the line that just ended, their lines up to the delimiter. The body of a
        shell (`bash <<EOF`) is read as a command line. Any other body is data (text, see _Text); when its
        delimiter is unquoted the shell runs its $(...) and backquotes, which are read as commands."""
        for delim, strip, quoted, shell in self.pending:
            lines = []
            while self.i < len(self.s):
                nl = self.s.find("\n", self.i)
                line = self.s[self.i:] if nl < 0 else self.s[self.i:nl]
                self.i = len(self.s) if nl < 0 else nl + 1
                if (line.lstrip("\t") if strip else line) == delim:
                    break
                lines.append(line)
            body = "\n".join(lines)
            if shell:
                sub = _ShellReader(body, self.home)
                sub.read()
                self.nested += [sub.items] + sub.nested
                self.data += sub.data
                continue
            self.data.append(body)
            if not quoted:
                self.substitutions(body)
        self.pending = []

    def substitutions(self, body: str) -> None:
        """The $(...) and backquotes of an unquoted here-document body, read as commands."""
        sub = _ShellReader(body, self.home)
        while sub.i < len(body):
            c = body[sub.i]
            if c == "\\":
                sub.i += 2
            elif c == "`" or body.startswith("$(", sub.i):
                sub.expansion(_Word())
            else:
                sub.i += 1
        self.nested += sub.nested
        self.data += sub.data

    def unit(self, w: _Word) -> None:
        """One unit of a word at the current position."""
        s, c = self.s, self.s[self.i]
        if c == "\\":
            if s.startswith("\\\n", self.i):
                self.i += 2
                return
            if self.i + 1 < len(s):
                w.add(s[self.i + 1], "escaped")
            self.i += 2
        elif c == "'":
            close = s.find("'", self.i + 1)
            if close < 0:
                self.fail()
            w.add(s[self.i + 1:close], "quoted")
            self.i = close + 1
        elif c == '"' or s.startswith('$"', self.i):
            self.i += 1 if c == '"' else 2
            w.add(self.double(w), "quoted")
        elif s.startswith("$'", self.i):
            close, j = None, self.i + 2
            while j < len(s):
                if s[j] == "\\":
                    w.dynamic = True                   # $'\x2f' builds any character at run time
                    j += 2
                    continue
                if s[j] == "'":
                    close = j
                    break
                j += 1
            if close is None:
                self.fail()
            w.add(s[self.i + 2:close], "quoted")
            self.i = close + 1
        elif c == "$" or c == "`":
            w.add(self.expansion(w), "plain")
        else:
            w.add(c, "plain")
            self.i += 1

    def double(self, w: _Word) -> str:
        """The text of a double-quoted string, the opening quote read; expansions as in expansion()."""
        s, out = self.s, []
        while self.i < len(s):
            c = s[self.i]
            if c == '"':
                self.i += 1
                return "".join(out)
            if c == "\\" and self.i + 1 < len(s) and s[self.i + 1] in '$`"\\\n':
                out.append(s[self.i + 1] if s[self.i + 1] != "\n" else "")
                self.i += 2
            elif c in "$`":
                start = not w.text and not out
                out.append(self.expansion(w, start))
            else:
                out.append(c)
                self.i += 1
        self.fail()

    def arithmetic_end(self, j: int) -> int | None:
        """The index of the `))` that closes the arithmetic $(( opened before `j`, when what lies between is plain
        arithmetic (numbers, names, operators, $NAME): its / divides and names no path (PD2). None for anything
        else, which is then read as a command substitution."""
        s, depth = self.s, 0
        while j < len(s):
            c = s[j]
            if c == "(":
                depth += 1
            elif c == ")":
                if depth == 0:
                    inner = s[self.i + 3:j]
                    if s.startswith("))", j) and _ARITH_RE.fullmatch(inner) and "$(" not in inner:
                        return j
                    return None
                depth -= 1
            j += 1
        return None

    def expansion(self, w: _Word, start: bool | None = None) -> str:
        """A $ or a backquote at the current position. $HOME and $PWD at the start of a word are read (the home,
        the folder the command stands in); every other variable and every substitution makes the word one the
        shell computes; the commands inside a substitution are read as commands."""
        s = self.s
        start = not w.text if start is None else start
        if s[self.i] == "`":
            self.i += 1
            w.dynamic = True
            self.sub("`")
            return ""
        nxt = s[self.i + 1:self.i + 2]
        if s.startswith("$((", self.i):
            close = self.arithmetic_end(self.i + 3)
            if close is not None:
                self.i = close + 2
                w.dynamic = True
                return ""
        if nxt == "(":
            self.i += 2
            w.dynamic = True
            self.sub(")")
            return ""
        if nxt == "{":
            close = s.find("}", self.i + 2)
            if close < 0:
                self.fail()
            name = s[self.i + 2:close]
            self.i = close + 1
        else:
            m = _NAME_RE.match(s, self.i + 1)
            if m is None:
                if nxt and nxt in "0123456789@*#?$!-":
                    self.i += 2
                    w.dynamic = True
                    return ""
                self.i += 1
                return "$"
            name = m.group(0)
            self.i = m.end()
        if start and name in ("HOME", "PWD"):
            return str(self.home) if name == "HOME" else "."
        w.dynamic = True
        return ""


_CMD_PREFIXES = ("builtin", "command", "then", "do", "else", "elif", "if", "while", "until", "{", "!", "time")
_ASSIGN_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*\+?=")


def _commands(items: list) -> list:
    """The words of a list of items with their place: (word, the separator before its command, is it the command
    word)."""
    out, sep, at_cmd = [], "\n", True
    for item in items:
        if not isinstance(item, _Word):
            sep, at_cmd = item, True
            continue
        if at_cmd and not item.dynamic and (item.value in _CMD_PREFIXES or _ASSIGN_RE.match(item.value)):
            out.append((item, sep, False))
            continue
        out.append((item, sep, at_cmd))
        at_cmd = False
    return out


_SHELLS = ("sh", "bash", "dash", "zsh", "ksh")
_INNER_MAX = 20


def _inner_commands(words: list) -> list[str]:
    """The command strings a command line hands to another shell: `bash -c STRING` (also -lc, -ec) and `eval`."""
    out = []
    for n, (w, _, cmd) in enumerate(words):
        if not cmd or w.dynamic:
            continue
        args = []
        for t, _, c in words[n + 1:]:
            if c:
                break
            args.append(t)
        if w.value == "eval":
            out.append(" ".join(a.value for a in args))
        elif os.path.basename(w.value) in _SHELLS:
            for i, a in enumerate(args[:-1]):
                if a.value.startswith("-") and not a.value.startswith("--") and "c" in a.value:
                    out.append(args[i + 1].value)
                    break
    return out


def _cd_target(words: list, n: int) -> int | None:
    """The index of the target word of the cd or pushd at index n of `words`, None for a bare one."""
    for m in range(n + 1, len(words)):
        t, _, cmd = words[m]
        if cmd:
            break
        if t.value in ("-L", "-P", "-e", "-@") and not t.dynamic:
            continue
        return m
    return None


_SED = ("sed", "gsed")
_AWK = ("awk", "gawk", "mawk", "nawk")


def _programs(words: list) -> set[int]:
    """The indexes of the words that are a sed or awk program, not a path (PD2): the value of sed -e and
    --expression, of awk -F and -v, and the first operand when no -e, -f or --file names the program. A program
    file (-f FILE) and every input file stay paths."""
    out: set[int] = set()
    for n, (w, _, cmd) in enumerate(words):
        if not cmd or w.dynamic:
            continue
        name = os.path.basename(w.value)
        if name not in _SED + _AWK:
            continue
        sed, given, expect, m = name in _SED, False, None, n + 1
        while m < len(words) and not words[m][2]:
            t = words[m][0]
            v = "" if t.dynamic else t.value
            if expect is not None:
                if expect == "text":
                    out.add(m)
                expect = None
            elif v == "--":
                if not given and m + 1 < len(words) and not words[m + 1][2]:
                    out.add(m + 1)
                break
            elif v.startswith("--"):
                key = v[2:].split("=", 1)[0]
                if key in ("expression", "source", "field-separator", "assign"):
                    given = given or key in ("expression", "source")
                    if "=" in v:
                        out.add(m)
                    else:
                        expect = "text"
                elif key == "file":
                    given = True
                    expect = None if "=" in v else "file"
            elif v.startswith("-") and len(v) > 1:
                letters = v[1:]
                if sed and not letters.startswith("i"):
                    for k, ch in enumerate(letters):
                        if ch in "ef":
                            given = True
                            if k + 1 < len(letters):
                                if ch == "e":
                                    out.add(m)
                            else:
                                expect = "text" if ch == "e" else "file"
                            break
                elif not sed and letters[0] in "Fvf":
                    given = given or letters[0] == "f"
                    if len(letters) > 1:
                        if letters[0] != "f":
                            out.add(m)
                    else:
                        expect = "file" if letters[0] == "f" else "text"
            elif not given:
                out.add(m)
                given = True
            m += 1
    return out


def _shell_checks(command: str, cwd: Path, home: Path) -> list:
    """The checks of a shell command in their order: a Path to classify, or a class to refuse at once. A relative
    path is taken against the folder the command stands in at that point: the folder of the call, changed by each
    cd or pushd. When a folder change may not hold for what follows (a subshell, a pipe, a background job, ||, a
    substitution), every relative path is taken against every folder the command may stand in. Every one of those
    folders is checked last. The string of `bash -c` and of `eval` is read as a command line as well. Refused as
    unreadable: a quote or an escape inside a word that carries a path, a variable or a substitution inside a path
    other than $HOME and $PWD at its start, ~- and the directory stack, a cd or pushd the guard cannot follow,
    CDPATH, a quote that never closes."""
    reader = _ShellReader(command, home)
    reader.read()
    lists, data = [reader.items] + reader.nested, list(reader.data)
    inner, n = 0, 0
    while n < len(lists):
        for text in _inner_commands(_commands(lists[n])):
            inner += 1
            if inner > _INNER_MAX:
                raise _Refused(UNREADABLE)
            sub = _ShellReader(text, home)
            sub.read()
            lists += [sub.items] + sub.nested
            data += sub.data
        n += 1
    words = [w for items in lists for w in _commands(items)]
    if any("CDPATH" in w.value for w, _, _ in words):
        raise _Refused(UNREADABLE)
    dirstack = any(w.value == "pushd" for w, _, _ in words)
    cds = [n for n, (w, _, cmd) in enumerate(words) if cmd and not w.dynamic and not w.mixed and w.value in _CD_WORDS]
    exact = len(lists) == 1 and all(i in _ShellReader.EXACT for i in reader.items if isinstance(i, str))
    targets = {n: _cd_target(words, n) for n in cds}
    programs = _programs(words)
    visited, here = [cwd], [cwd]

    def move(n: int, base: list[Path]) -> list[Path]:
        target = None if targets[n] is None else words[targets[n]][0]
        if target is None:
            if words[n][0].value == "pushd":
                raise _Refused(UNREADABLE)
            return [home]
        if target.dynamic or target.mixed or target.value in ("", "-") or target.value.startswith("-"):
            raise _Refused(UNREADABLE)
        found = _one_path(target.value, base, home, dirstack)
        if not found:
            raise _Refused(UNREADABLE)
        return found

    if not exact:
        for n in cds:
            visited += [p for p in move(n, visited) if p not in visited]
        here = visited
    checks: list = []
    for n, (w, _, _) in enumerate(words):
        if exact and n in cds:
            here = move(n, here)
            visited += [p for p in here if p not in visited]
        if n in targets.values():
            continue                            # a folder the command moves to is checked with the others, last
        if w.dynamic:
            if "/" in w.value or ".." in w.value:
                checks.append(UNREADABLE)
            continue
        text = n in programs or any(c in w.value for c in " \t\n")
        if text and n not in programs and w.value.startswith(("/", "~", "./", "../")):
            try:                                # a path with a blank in it is still a path, as a whole
                checks += _one_path(w.value, here, home, dirstack)
            except _Refused:
                pass
        for tok in _SPLIT_RE.split(w.value):
            checks += _token_paths(tok, here, home, dirstack, text)
        if w.mixed and "/" in w.value:
            checks.append(UNREADABLE)
    for body in data:
        for tok in _SPLIT_RE.split(body):
            checks += _token_paths(tok, visited, home, dirstack, text=True)
    return checks + visited


def _tool_paths(tool: str, tool_input: dict, cwd: Path, home: Path) -> list:
    """The checks of one tool call (see _shell_checks); a Glob or Grep without a path searches the session folder,
    and its pattern is taken under that path."""
    if tool in SHELL_TOOLS:
        command = tool_input.get("command")
        if not isinstance(command, str):
            raise _Refused(UNKNOWN_PROJECT)
        return _shell_checks(command, cwd, home)
    names = {"Glob": ("path", "pattern"), "Grep": ("path", "glob")}.get(tool, ("file_path", "notebook_path", "path"))
    out = []
    base = cwd
    for key in names:
        raw = tool_input.get(key)
        if raw is None or raw == "":
            continue
        if not isinstance(raw, str):
            raise _Refused(UNKNOWN_PROJECT)
        if key in ("pattern", "glob"):
            out.append(_absolute(raw, base, home))
            continue
        out.append(_absolute(raw, cwd, home))
        if key == "path":
            base = cwd / _expand(raw, home)
    if tool in ("Glob", "Grep") and not tool_input.get("path"):
        out.append(cwd)
    if not out and tool not in ("Glob", "Grep"):
        raise _Refused(UNKNOWN_PROJECT)
    return out


def place_class(path: Path, own: Path, p: config.Paths, home: Path, text: bool = False) -> str | None:
    """The class of a refused place for `path`, or None when a session may reach it. The path is taken as written
    and through its links, each link resolved before a `..` after it climbs, as the kernel does; both must pass.
    The session records (<shared>/sessions: the receipt marks and guard.log) are refused to every call. The own
    project, the rest of the shared folder (outbox, datasets) and the knowledge base pass; another project, the
    client's folder and the key folders of `home` do not, and neither does a folder that holds one of them (a
    search from it would reach them). With `text` (a place read out of text, see _Text) only a place inside a
    refused one is refused, not a folder that holds one."""
    literal = Path(os.path.normpath(str(path)))
    try:
        real = Path(os.path.realpath(str(path)))
    except (OSError, ValueError):
        return UNKNOWN_PROJECT
    own_real = Path(os.path.realpath(own))
    records = Path(os.path.realpath(p.shared / "sessions"))
    allowed = [own_real, Path(os.path.realpath(p.shared)), Path(os.path.realpath(p.kb))]
    roots = {Path(os.path.realpath(p.projects_root)), Path(os.path.realpath(home))}
    client = Path(os.path.realpath(home / ".claude"))
    keys = [Path(os.path.realpath(home / k)) for k in KEY_FOLDERS]
    for t in dict.fromkeys((literal, real)):
        if _inside(t, records) or (not text and _inside(records, t) and not _inside(own_real, t)):
            return SESSION_RECORDS
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
        if text:
            continue
        if any(_inside(r, t) for r in roots) or _inside(own_real, t):
            return ANOTHER_PROJECT
        if any(_inside(k, t) for k in keys):
            return KEY_FOLDER
        if _inside(client, t):
            return CLIENT_FOLDER
    return None


_ENCODE_RE = re.compile(r"[^A-Za-z0-9]")
"""The client names the transcript folder of a session after its first folder, every other character a -."""


def _first_folder(data: dict, p: config.Paths | None) -> Path | None:
    """The project the session started in, read from the name of its transcript folder
    (<home>/.claude/projects/<first folder with - for / and .>/...): the one project folder under the projects root
    whose name it is, or whose name it starts with (a start in a subfolder). None when none or more than one fit."""
    raw = data.get("transcript_path")
    if not isinstance(raw, str) or not raw or p is None:
        return None
    parts = Path(raw).parts
    names = [parts[i + 1] for i in range(1, len(parts) - 1) if parts[i] == "projects" and parts[i - 1] == ".claude"]
    if not names:
        return None
    name = names[0]
    try:
        cands = [d for d in Path(p.projects_root).iterdir() if _PROJECT_DIR_RE.match(d.name)]
    except OSError:
        return None
    fits = []
    for d in cands:
        enc = _ENCODE_RE.sub("-", str(d))
        if name == enc or name.startswith(enc + "-"):
            try:
                if (d / "SCOPE.md").is_file():
                    fits.append(d)
            except OSError:
                continue
    return fits[0] if len(fits) == 1 else None


def _own_project(data: dict, p: config.Paths | None = None) -> Path | None:
    """The project of the session: the folder the client started in (CLAUDE_PROJECT_DIR), else the first folder
    of the session from its transcript, else the session folder of the call. The first two hold when a cd moved
    the session folder out of the project (PD2)."""
    started = os.environ.get("CLAUDE_PROJECT_DIR")
    root = project_root(started) if started else None
    if root is None:
        root = _first_folder(data, p)
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


def _workflow_class(tool_input: dict, home: Path, folder: Path | None = None) -> str | None:
    """None when a Workflow call runs one of WORKFLOW_FILES of `folder` (WORKFLOW_DIR) by its scriptPath, also to
    resume a run of it; FOREIGN_WORKFLOW for an inline script, a workflow by name or any other file."""
    folder = WORKFLOW_DIR if folder is None else folder
    if tool_input.get("script") or tool_input.get("name"):
        return FOREIGN_WORKFLOW
    raw = tool_input.get("scriptPath")
    if not isinstance(raw, str) or not raw or "\x00" in raw:
        return FOREIGN_WORKFLOW
    real = os.path.realpath(_expand(raw, home))
    allowed = {os.path.realpath(folder / name) for name in WORKFLOW_FILES}
    return None if real in allowed and os.path.isfile(real) else FOREIGN_WORKFLOW


def guard(data: dict, p: config.Paths | None = None, home: Path | None = None,
          sealed: bool | None = None) -> str | None:
    """The class of the place a tool call reaches outside its project, or None when it may run. Fails closed:
    a call whose project or input cannot be read is refused as UNKNOWN_PROJECT, a shell path the guard cannot read
    as UNREADABLE. Only a host without a seal (the
    plugin on an architect's own machine, `sealed` false) lets a session outside any project pass."""
    tool = data.get("tool_name")
    if tool not in GUARD_TOOLS:
        return None
    try:
        p = p or config.paths()
        home = home or Path.home()
        own = _own_project(data, p)
        if own is None:
            return UNKNOWN_PROJECT if (_sealed() if sealed is None else sealed) else None
        cwd_raw = data.get("cwd")
        cwd = Path(cwd_raw) if isinstance(cwd_raw, str) and cwd_raw else own
        tool_input = data.get("tool_input")
        if not isinstance(tool_input, dict):
            return UNKNOWN_PROJECT
        if tool == WORKFLOW_TOOL:
            if not (_sealed() if sealed is None else sealed):
                return None             # the plugin on an architect's own machine: Workflow was never denied there
            return _workflow_class(tool_input, home)
        for check in dict.fromkeys(_tool_paths(tool, tool_input, cwd, home)):
            if isinstance(check, str):
                cls = check
            elif isinstance(check, _Text):
                cls = place_class(check.path, own, p, home, True)
            else:
                cls = place_class(check, own, p, home)
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
    if cls == UNREADABLE:
        return ("refused: the guard cannot tell where this call goes (a variable, a substitution, a quote or an escape "
                "inside a path, or a change of folder it cannot follow). Write the path plainly, inside the project.")
    if cls == FOREIGN_WORKFLOW:
        return ("refused: a project session runs only the Workbench's own workflows (%s) by their scriptPath in %s, "
                "never an inline script or a workflow by name." % (", ".join(WORKFLOW_FILES), WORKFLOW_DIR))
    return "refused: this call reaches %s. A session stays inside its own project folder." % cls


def hook_pre_tool(data: dict) -> int:
    """Read, Edit, MultiEdit, NotebookEdit, Write, Glob, Grep, Bash and Monitor stay inside the session's project: a
    target in another project, in the client's folder, in a key folder or in the session records, and a shell path
    the guard cannot read, are refused with one line naming the class."""
    cls = guard(data)
    if cls is None:
        return OK
    try:
        p = config.paths()
    except Exception:
        p = None
    if p is not None:
        _log_refusal(p, data, str(data.get("tool_name")) if data.get("tool_name") in GUARD_TOOLS else "other", cls,
                     _own_project(data, p))
    print(refusal(cls), file=sys.stderr)
    return BLOCK


# --------------------------------------------------------------------------- pre-tool for the build user

NO_CLONE = "no build clone"
ANOTHER_CLONE = "another build clone"
BUILD_VENV = "the build venv, which is read only"
OUTSIDE_BUILD = "a place outside the build clone"
"""The classes of a place the build user's guard refuses (THREAT-MODEL.md of the self-build, N5)."""
BUILD_WRITE_TOOLS = ("Write", "Edit", "MultiEdit", "NotebookEdit")
BUILD_DEVICES = ("/dev/null", "/dev/zero", "/dev/random", "/dev/urandom", "/dev/stdin", "/dev/stdout", "/dev/stderr",
                 "/dev/fd")
"""Device paths a shell command of a task names (2>/dev/null and the like): they reach no file of anyone."""
BUILD_TMP = "/tmp"
"""The temporary folder a build task may use (its unit makes it private)."""
BUILD_HOOKS = ("pre-tool", "pre-write", "post-write")
"""The hooks that check the build user; the others exit 0 for it."""


def _build_clone(data: dict, wt: Path) -> Path | None:
    """The clone of a build session: the folder directly under `wt` that the client started in (CLAUDE_PROJECT_DIR,
    else the session folder of the call). None when that lies outside `wt`, is `wt` itself or is reached through a
    link."""
    started = os.environ.get("CLAUDE_PROJECT_DIR") or (data.get("cwd") if isinstance(data.get("cwd"), str) else None)
    if not started:
        return None
    real = Path(os.path.realpath(started))
    if real != Path(os.path.normpath(os.path.abspath(started))):
        return None     # a link on the way: a clone is entered by its own path only
    if real == wt or wt not in real.parents:
        return None
    own = wt / real.relative_to(wt).parts[0]
    if os.path.islink(own) or not own.is_dir():
        return None
    return own


_VENV_PYTHON_RE = re.compile(r"python(?:3(?:\.[0-9]{1,2})?)?")
"""The interpreter names of a venv's bin folder (python, python3, python3.12)."""


def _venv_base(venv: Path) -> set[Path]:
    """The real paths of the venv's base interpreter, from its root-owned pyvenv.cfg (`executable`, else `home` with
    a python3 name). Empty when the file cannot be read: no link out of the venv passes then."""
    try:
        text = (venv / "pyvenv.cfg").read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return set()
    conf = {}
    for line in text.splitlines():
        key, sep, value = line.partition("=")
        if sep:
            conf[key.strip()] = value.strip()
    out = set()
    if os.path.isabs(conf.get("executable", "")):
        out.add(Path(os.path.realpath(conf["executable"])))
    if os.path.isabs(conf.get("home", "")) and conf.get("version"):
        major_minor = ".".join(conf["version"].split(".")[:2])
        for name in ("python3", "python" + major_minor):
            out.add(Path(os.path.realpath(os.path.join(conf["home"], name))))
    return out


def _venv_interpreter(literal: Path, real: Path, venv: Path) -> bool:
    """A Read or a run of the venv's own interpreter: the path as written is python, python3 or python3.N in the
    venv's bin folder and its links end at the base interpreter pyvenv.cfg names. Any other link out of the venv
    stays refused (B-001: the venv's python is a link to /usr/bin/python3.12)."""
    return (literal.parent == venv / "bin" and bool(_VENV_PYTHON_RE.fullmatch(literal.name))
            and real in _venv_base(venv))


def build_place_class(path: Path, own: Path, root: Path, write: bool = False, tmp: Path | None = None) -> str | None:
    """The class of a refused place for the build user, or None when it may reach `path`: its own clone, /tmp, the
    device paths and (unless `write`) the suite's venv under `root`. The path as written and through its links must
    both pass; the one link that may leave the venv is its interpreter's to the base interpreter (`_venv_interpreter`),
    for a Read or a run, never a Write. A place under <root>/wt that is not the own clone, or a folder above the own
    clone, is another clone's; everything else is outside. `tmp` is BUILD_TMP unless given (the selftest plants its
    own)."""
    literal = Path(os.path.normpath(str(path)))
    if any(_inside(literal, Path(d)) for d in BUILD_DEVICES):
        return None     # /dev/stdin and /dev/fd/N resolve to the session's own pipes and terminal
    try:
        real = Path(os.path.realpath(str(path)))
    except (OSError, ValueError):
        return NO_CLONE
    wt, venv = root / "wt", root / "venv"
    tmp = Path(os.path.realpath(tmp if tmp is not None else BUILD_TMP))
    if _inside(literal, venv) and not write and _venv_interpreter(literal, real, venv):
        return None
    for t in dict.fromkeys((literal, real)):
        if _inside(t, own) or _inside(t, tmp):
            continue
        if _inside(t, venv):
            if write:
                return BUILD_VENV
            continue
        if _inside(t, wt) or _inside(own, t):
            return ANOTHER_CLONE
        return OUTSIDE_BUILD
    return None


def build_guard(data: dict, root: Path | None = None, home: Path | None = None, tmp: Path | None = None) -> str | None:
    """The class of the place a tool call of the build user reaches outside its clone, or None when it may run.
    Fails closed: no clone folder, an input it cannot read or an internal error refuse the call."""
    tool = data.get("tool_name")
    if tool not in GUARD_TOOLS:
        return None
    try:
        root = Path(os.path.realpath(root if root is not None else config.build_root()))
        own = _build_clone(data, root / "wt")
        if own is None:
            return NO_CLONE
        home = home or Path.home()
        cwd_raw = data.get("cwd")
        cwd = Path(cwd_raw) if isinstance(cwd_raw, str) and cwd_raw else own
        tool_input = data.get("tool_input")
        if not isinstance(tool_input, dict):
            return NO_CLONE
        if tool == WORKFLOW_TOOL:
            return OUTSIDE_BUILD        # the build settings deny Workflow; the guard refuses it as well
        for check in dict.fromkeys(_tool_paths(tool, tool_input, cwd, home)):
            if isinstance(check, _Text):
                check = check.path      # the build user's guard passes only its own places: text is no exception
            cls = check if isinstance(check, str) else build_place_class(check, own, root,
                                                                         tool in BUILD_WRITE_TOOLS, tmp)
            if cls:
                return NO_CLONE if cls == UNKNOWN_PROJECT else cls
    except _Refused as exc:
        return NO_CLONE if str(exc) == UNKNOWN_PROJECT else str(exc)
    except Exception:
        return NO_CLONE
    return None


def build_refusal(cls: str) -> str:
    if cls == NO_CLONE:
        return ("refused: this build session runs in no clone under %s/wt, so no tool call runs."
                % config.build_root())
    if cls == UNREADABLE:
        return refusal(UNREADABLE)
    return "refused: this call reaches %s. A build task stays inside its own clone." % cls


def hook_build_tool(data: dict) -> int:
    """pre-tool for the build user: one line naming the class, never the path; no log (the build user writes no
    shared file)."""
    cls = build_guard(data)
    if cls is None:
        return OK
    print(build_refusal(cls), file=sys.stderr)
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
                ({"tool_name": "Bash", "tool_input": {"command": "sed -i 's/a/ /' STATE.md"}}, None),
                ({"tool_name": "Workflow", "tool_input": {"script": "export const meta = {name: 'x'}"}},
                 FOREIGN_WORKFLOW),
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
    return problems + build_selftest()


def build_selftest() -> list[str]:
    """The build user's guard on a throw-away build root (THREAT-MODEL.md of the self-build, N5): a Read of a project
    path, of the owner's home and of another clone is refused, a Write into the venv is refused, a link out of the venv
    that is not its interpreter's is refused, a Read in the own clone and the venv's interpreter (a link to the base
    interpreter, as on the host) pass, a call from no clone is refused. The problems found; empty when the guard
    works."""
    import tempfile

    problems: list[str] = []
    with tempfile.TemporaryDirectory(prefix="awb-build-guard-") as tmp:
        base = Path(os.path.realpath(tmp))
        root, owner_home, project = base / "build", base / "owner", base / "work" / "tcp-aaaa"
        own, other = root / "wt" / "B-001-1", root / "wt" / "B-002-1"
        venv, usr = root / "venv", base / "usr" / "bin"
        for d in (own, other, venv / "bin", owner_home, project, usr):
            d.mkdir(parents=True)
        (usr / "python3.12").write_bytes(b"")
        (usr / "python3").symlink_to("python3.12")
        (venv / "pyvenv.cfg").write_text("home = %s\nversion = 3.12.3\nexecutable = %s\n"
                                         % (usr, usr / "python3.12"), encoding="utf-8")
        (venv / "bin" / "python3").symlink_to(usr / "python3")
        (venv / "bin" / "python").symlink_to("python3")
        (venv / "bin" / "notes").symlink_to(owner_home / "notes.md")
        saved = os.environ.pop("CLAUDE_PROJECT_DIR", None)
        try:
            cases = [
                ("Read", {"file_path": str(project / "STATE.md")}, OUTSIDE_BUILD),
                ("Read", {"file_path": str(owner_home / "notes.md")}, OUTSIDE_BUILD),
                ("Read", {"file_path": str(other / "CLAUDE.md")}, ANOTHER_CLONE),
                ("Write", {"file_path": str(venv / "bin" / "python"), "content": ""}, BUILD_VENV),
                ("Read", {"file_path": str(venv / "bin" / "notes")}, OUTSIDE_BUILD),
                ("Read", {"file_path": str(own / "CLAUDE.md")}, None),
                ("Bash", {"command": "%s/bin/python -m pytest -q tests 2>/dev/null" % venv}, None),
            ]
            for n, (tool, tool_input, want) in enumerate(cases, 1):
                got = build_guard({"tool_name": tool, "cwd": str(own), "tool_input": tool_input}, root, base,
                                  base / "tmp")
                if got != want:
                    problems.append("build case %d (%s): %s where %s was due" % (n, tool, got or "passed",
                                                                                want or "a pass"))
            if build_guard({"tool_name": "Read", "cwd": str(base), "tool_input": {"file_path": str(own / "x")}},
                           root, base, base / "tmp") != NO_CLONE:
                problems.append("a build call without a clone folder passed")
        finally:
            if saved is not None:
                os.environ["CLAUDE_PROJECT_DIR"] = saved
    return problems
