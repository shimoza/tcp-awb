"""The checks of the build runner: checks 1 to 11 of a task run and the checks of a branch from outside.

A part of the build runner (awb/buildrun.py, F1 step 6); its docstring lists the checks and the states they set.
Checks 1 to 8 read data in main.git as the owner (`git`, never in a clone); 9 to 11 run code as the build user on
an export (`run_suite`). The first failure raises `Stop` with the run's state. `awb build check REF` runs the same
checks on a branch from outside (F2 G1).
"""
from __future__ import annotations

import contextlib
import json
import os
import re
import stat
import subprocess
from datetime import datetime
from pathlib import Path

from awb import build
from awb.build_unit_side import LOG_MAX, LOGIN_KEPT, LOGIN_KEPT_LINE, SHA_RE, failed_nodes, outcomes
from awb.build_units import SMALL_MAX, Ctx, Ended, Host, Spec, _hidden, _read, _take, _text, _write, wait


# --------------------------------------------------------------------------- the fixed values

HAND_SECONDS = 600
MAX_COMMITS = 5
FLAKY_DAYS = 7              # A7: a node flaky a second time in this many days gets a line in build/queue
FLAKY_LOG = ".flaky.tsv"    # runs/.flaky.tsv: time, id, run, a hash of the node, the node as shown
FLAKY_FILE = "FLAKY.md"     # build/queue/FLAKY.md: the lines for the owner, which the steering session reads
_NODE_SHOWN_RE = re.compile(r"tests/(?:[A-Za-z0-9_]+/)*[A-Za-z0-9_]+\.py(?:::[A-Za-z0-9_]+){1,2}"
                            r"(?:\[[A-Za-z0-9_.,:=+-]{1,60}\])?")
BUNDLE_MAX = 512 << 20
BLOB_MAX = 16 << 20
"""The largest changed blob and commit object the runner reads (B3); `git cat-file --batch-check` sizes them first."""
TAIL_LINES = 60
SUITE_ALLOW = ("localhost",)
"""The suite unit's one allowed range: loopback (127.0.0.0/8 and ::1) for the suite's own stand-in servers. The unit
has a network namespace of its own (PrivateNetwork=yes), so this is its own loopback, not the host's listeners; the
outside stays denied and has no route besides."""
SUBTYPE_RE = re.compile(r"[a-z0-9_]{1,40}")
CHECK_NAMES = (
    "the unit ended by itself",
    "the client ended well",
    "the last message is an answer",
    "a commit exists and nothing else moved",
    "the commits are his",
    "the commits pass the gate",
    "the commits stay inside the task",
    "the bookkeeping is there",
    "the whole suite is green in a fresh export",
    "no test was hollowed",
    "the task's own check",
)
_MARKER_RE = re.compile(r"pytest\.mark\.(?:skip|skipif|xfail)\b|pytest\.(?:skip|xfail)\(")
_TEST_DEF_RE = re.compile(r"^\s*(?:async\s+)?def test_")
_WEEK_RE = re.compile(r"## Week (\d{1,2}), (\d{4})-(\d{2})-(\d{2})\b")


# --------------------------------------------------------------------------- the owner's git, in main.git only

class GitFailed(Exception):
    pass


def git(host: Host, *args: str, stdin: bytes | None = None, check: bool = True, timeout: float = 300,
        net: bool = False) -> subprocess.CompletedProcess:
    """git in main.git as the owner, with fsmonitor, hooks and the in-tree attributes off. Never a clone."""
    cmd = ["git", "-C", str(host.main_git), "-c", "core.fsmonitor=false", "-c", "core.hooksPath=/dev/null", *args]
    env = dict(os.environ, GIT_TERMINAL_PROMPT="0", LANG="C")
    if not net:
        env["GIT_CONFIG_NOSYSTEM"] = "1"
    try:
        r = host.git_run(cmd, input=stdin, capture_output=True, timeout=timeout, check=False, env=env)
    except (OSError, subprocess.SubprocessError) as err:
        raise GitFailed("git %s: %s" % (args[0], type(err).__name__)) from None
    if check and r.returncode != 0:
        raise GitFailed("git %s exited %d" % (args[0], r.returncode))
    return r


def rev(host: Host, ref: str) -> str | None:
    r = git(host, "rev-parse", "--verify", "--quiet", ref + "^{commit}", check=False)
    out = r.stdout.decode("ascii", "replace").strip()
    return out if r.returncode == 0 and SHA_RE.fullmatch(out) else None


def is_ancestor(host: Host, a: str, b: str) -> bool:
    return git(host, "merge-base", "--is-ancestor", a, b, check=False).returncode == 0


def ensure_main(host: Host) -> None:
    """main.git is a bare repository of this uid with main; its info/attributes switch the in-tree export
    attributes off (a task cannot drop a file from the export) and every attribute that changes a file on its way out
    (a filter of the owner's git configuration, ident, eol, an encoding): the export holds the blobs the gate read.
    With the push on, origin is the public repository."""
    st = os.lstat(host.main_git)
    if not stat.S_ISDIR(st.st_mode) or st.st_uid != os.geteuid():
        raise GitFailed("main.git is not a folder of this user")
    if git(host, "rev-parse", "--is-bare-repository").stdout.strip() != b"true":
        raise GitFailed("main.git is not bare")
    info = host.main_git / "info"
    info.mkdir(exist_ok=True)
    want = b"* -export-ignore -export-subst -filter -ident -text -eol -working-tree-encoding\n"
    if _read(info / "attributes") != want:
        _write(info / "attributes", want, 0o644)
    if host.push:
        cur = git(host, "config", "--get", "remote.origin.url", check=False).stdout.decode("utf-8", "replace").strip()
        if cur != host.push_url:
            git(host, "config", "remote.origin.url", host.push_url)
            git(host, "config", "remote.origin.fetch", "+refs/heads/main:refs/remotes/origin/main")


def owner_identity(host: Host) -> tuple[str, str] | None:
    """The owner's name and mail from the root template's .gitconfig (D-S16), the identity every task commits as."""
    text = _text_any(host.template / ".gitconfig")
    if text is None:
        return None
    name = re.search(r"(?m)^\s*name\s*=\s*(.+?)\s*$", text)
    mail = re.search(r"(?m)^\s*email\s*=\s*(.+?)\s*$", text)
    return (name.group(1), mail.group(1)) if name and mail else None


def _text_any(path: Path, cap: int = SMALL_MAX) -> str | None:
    try:
        return build._read_regular(Path(path), cap, own=False).decode("utf-8")
    except (OSError, UnicodeDecodeError):
        return None


class Stop(Exception):
    """A check ended the round: `state` is the run's state."""

    def __init__(self, state: str):
        super().__init__(state)
        self.state = state


def _ok(ctx: Ctx, number: int, note: str = "") -> None:
    ctx.lines.append("%d ok    %s%s" % (number, CHECK_NAMES[number - 1], "; " + note if note else ""))


def _fail(ctx: Ctx, number: int, state: str) -> Stop:
    ctx.lines.append("%d FAIL  %s: %s" % (number, CHECK_NAMES[number - 1], state))
    return Stop(state)


# --------------------------------------------------------------------------- checks 1 to 3: the session

def parse_result(data: bytes | None) -> dict | None:
    """Exactly one JSON object with the fields the runner reads; None for none, two or a broken one."""
    if not data:
        return None
    try:
        text = data.decode("utf-8")
        obj, end = json.JSONDecoder().raw_decode(text.lstrip())
    except (UnicodeDecodeError, ValueError):
        return None
    if text.lstrip()[end:].strip() or not isinstance(obj, dict):
        return None
    return obj


def check_unit(ctx: Ctx, ended: Ended | None, deadline: float | None, now: float) -> None:
    if ended is None or ended.result not in ("success", "exit-code") or deadline is None or now > deadline:
        raise _fail(ctx, 1, "hung")
    _ok(ctx, 1)


def check_client(ctx: Ctx, obj: dict | None) -> None:
    if obj is None:
        raise _fail(ctx, 2, "stopped: no_result")
    subtype = obj.get("subtype")
    if subtype != "success" or obj.get("is_error") is True:
        word = subtype if isinstance(subtype, str) and SUBTYPE_RE.fullmatch(subtype) else "error"
        raise _fail(ctx, 2, "stopped: %s" % word)
    _ok(ctx, 2)


def check_answer(ctx: Ctx, obj: dict) -> None:
    from awb import questions

    text = obj.get("result") if isinstance(obj.get("result"), str) else ""
    last = text.rstrip().splitlines()[-1].strip() if text.strip() else ""
    if not last.startswith("DONE:") or questions.ends_with_question(text):
        raise _fail(ctx, 3, "asked")
    _ok(ctx, 3)


# --------------------------------------------------------------------------- checks 4 to 8: data in main.git

def quarantine(ctx: Ctx) -> str:
    return "refs/quarantine/%s/%d" % (ctx.tid, ctx.n)


def fetch_bundle(host: Host, ctx: Ctx) -> dict[str, str] | None:
    """The kept task.bundle into refs/quarantine/<ID>/<n>/ of main.git (the old ones dropped first); the heads."""
    q = quarantine(ctx)
    old = git(host, "for-each-ref", "--format=%(refname)", q + "/", check=False).stdout.decode("utf-8", "replace")
    for ref in old.split():
        if ref.startswith(q + "/"):
            git(host, "update-ref", "-d", ref, check=False)
    bundle = ctx.folder / "task.bundle"
    if not bundle.is_file():
        return None
    r = git(host, "-c", "transfer.fsckObjects=true", "-c", "fetch.fsckObjects=true", "fetch", "--quiet", "--no-tags",
            "--no-write-fetch-head", str(bundle), "+refs/heads/*:%s/*" % q, check=False)
    if r.returncode:
        return None
    out = git(host, "for-each-ref", "--format=%(refname)%00%(objectname)", q + "/").stdout
    heads: dict[str, str] = {}
    for line in out.split(b"\n"):
        if not line:
            continue
        name, _, sha = line.partition(b"\0")
        heads[name.decode("utf-8", "replace")[len(q) + 1:]] = sha.decode("ascii", "replace")
    return heads


def check_commits(host: Host, ctx: Ctx, heads: dict[str, str] | None, status: bytes | None) -> None:
    branch = "task/%s" % ctx.tid
    if not heads or branch not in heads:
        raise _fail(ctx, 4, "red: no commit")
    for name, sha in heads.items():
        if name == branch or (name == "main" and sha == ctx.start):
            continue
        raise _fail(ctx, 4, "red: second branch")
    ctx.tip = heads[branch]
    if not SHA_RE.fullmatch(ctx.tip) or not is_ancestor(host, ctx.start, ctx.tip):
        raise _fail(ctx, 4, "red: not on main")
    commits = git(host, "rev-list", "--reverse", "%s..%s" % (ctx.start, ctx.tip)).stdout.decode("ascii").split()
    if not commits:
        raise _fail(ctx, 4, "red: no commit")
    if len(commits) > MAX_COMMITS:
        raise _fail(ctx, 4, "red: too many commits")
    if git(host, "rev-list", "--min-parents=2", "%s..%s" % (ctx.start, ctx.tip)).stdout.strip():
        raise _fail(ctx, 4, "red: merge commit")
    if status is None or status.strip(b"\0\n "):
        raise _fail(ctx, 4, "red: dirty tree")
    big = oversized(host, commits)
    if big == "message":
        raise _fail(ctx, 4, "red: message too big")
    if big:
        raise _fail(ctx, 4, "red: blob too big")
    ctx.commits = commits
    _ok(ctx, 4, "%d commit(s)" % len(commits))


def oversized(host: Host, commits: list[str]) -> str | None:
    """`message` when a commit object of `commits` is above BLOB_MAX, `blob` when a blob one of them adds or changes
    is; None otherwise. Sizes come from `git cat-file --batch-check`, which reads no content: this runs before any
    read of a message or a blob, so a blob of many GB that compresses to a small bundle is never buffered."""
    from awb import gate

    objects = dict.fromkeys(commits)
    for commit in commits:
        raw = git(host, "diff-tree", "--root", "-r", "-m", "--no-commit-id", "--raw", "-z", "--no-abbrev",
                  "--no-renames", "--diff-filter=AMT", commit).stdout
        objects.update(dict.fromkeys(blob for _, mode, blob in gate._raw_entries(raw) if mode != "160000"))
    out = git(host, "cat-file", "--batch-check=%(objectname) %(objecttype) %(objectsize)",
              stdin=("\n".join(objects) + "\n").encode()).stdout.decode("ascii", "replace")
    for line in out.split("\n"):
        f = line.split()
        if len(f) == 3 and f[2].isdigit() and int(f[2]) > BLOB_MAX:
            return "message" if f[1] == "commit" else "blob"
    return None


def commit_fields(host: Host, commits: list[str]) -> dict[str, list[str]]:
    """Author name and mail, committer name and mail and the message of every commit, read in main.git."""
    if not commits:
        return {}
    out = git(host, "log", "--no-walk=unsorted", "--stdin", "-z",
              "--format=%H%x01%an%x01%ae%x01%cn%x01%ce%x01%B", stdin=("\n".join(commits) + "\n").encode()).stdout
    fields: dict[str, list[str]] = {}
    for entry in out.split(b"\0"):
        if not entry.strip():
            continue
        parts = entry.decode("utf-8", "replace").split("\x01", 5)
        if len(parts) == 6:
            fields[parts[0].strip()] = parts[1:]
    return fields


def check_identity(host: Host, ctx: Ctx, identity: tuple[str, str] | None) -> None:
    fields = commit_fields(host, ctx.commits)
    if identity is None or len(fields) != len(ctx.commits):
        raise _fail(ctx, 5, "red: identity")
    for f in fields.values():
        if (f[0], f[1]) != identity or (f[2], f[3]) != identity:
            raise _fail(ctx, 5, "red: identity")
    _ok(ctx, 5)


def _matcher(register: Path | None):
    """The gate's matcher with the register; register.RegisterError when the names cannot be checked."""
    from awb import check, gate, register as reg

    if register is None or check.register_source(Path(register))[0] == check.MISSING:
        raise reg.RegisterError("no register found, names cannot be checked")
    m = gate._load_matcher(register, rate_wait=gate.RATE_WAIT)
    if isinstance(m, check.RemoteCheck):
        m.ping()
    return m


def diff_entries(host: Host, a: str, b: str) -> list[tuple[str | None, str, str, str]]:
    """(path or None when it is not UTF-8, old mode, new mode, new blob) of every change from a to b, no renames."""
    out = git(host, "diff", "--raw", "-z", "--no-abbrev", "--no-renames", a, b).stdout
    parts = out.split(b"\0")
    entries = []
    i = 0
    while i + 1 < len(parts):
        meta = parts[i]
        if not meta.startswith(b":"):
            i += 1
            continue
        f = meta[1:].split()
        try:
            path = parts[i + 1].decode("utf-8")
        except UnicodeDecodeError:
            path = None
        if len(f) >= 4:
            entries.append((path, f[0].decode("ascii"), f[1].decode("ascii"), f[3].decode("ascii")))
        i += 2
    return entries


def scan_commits(host: Host, commits: list[str], matcher, identity: tuple[str, str] | None) -> list[tuple[str, str]]:
    """The gate over commits as `awb gate --pushed` does: every added or changed file of every commit (content and
    path), author, committer and message (the owner's identity passes, as his own repository's does). Returns (where,
    class) pairs; where is a commit's short hash or `<short>:<path>` only after the path passed the scan."""
    from awb import gate

    out: list[tuple[str, str]] = []
    own = set(identity or ())
    for commit, f in commit_fields(host, commits).items():
        lines = [x for x in f[:4] if x not in own]
        text = "\n".join(lines + f[4].split("\n"))
        found = {x.cls for x in gate._scan_text("commit", text, matcher)}
        found |= {x.cls for x in gate._attribution("commit", f[4])}
        out += [(commit[:10], c) for c in sorted(found)]
    seen: set[str] = set()
    for commit in commits:
        raw = git(host, "diff-tree", "--root", "-r", "-m", "--no-commit-id", "--raw", "-z", "--no-abbrev",
                  "--no-renames", "--diff-filter=AMT", commit).stdout
        for rel, mode, blob in gate._raw_entries(raw):
            if mode == "160000" or blob in seen:
                continue
            seen.add(blob)
            found = {x.cls for x in gate._scan_text(rel, rel, matcher)}
            data = git(host, "cat-file", "blob", blob, check=False)
            if data.returncode:
                found.add("opaque")
            else:
                found |= {x.cls for x in gate._scan_bytes(rel, data.stdout, matcher, host.main_git / rel)}
            out += [("%s:%s" % (commit[:10], "a path"), c) for c in sorted(found)]
    return out


def check_gate(host: Host, ctx: Ctx, identity) -> None:
    """The gate's self-test, then the gate with the register over every commit. A name check that cannot run is
    `held: checks pending` (rerun when the vault opens), never red."""
    from awb import gate, register as reg

    if gate.selftest(None):
        raise _fail(ctx, 6, "held: gate selftest")
    try:
        matcher = _matcher(host.register)
        found = scan_commits(host, ctx.commits, matcher, identity)
    except reg.RegisterError:
        raise _fail(ctx, 6, "held: checks pending") from None
    if found:
        raise _fail(ctx, 6, "red: gate %s" % found[0][1])
    _ok(ctx, 6)


def _plain_path(path: str | None) -> bool:
    return path is not None and build._plain_path(path) and "\n" not in path


def path_allowed(path: str, paths: tuple[str, ...]) -> bool:
    """Inside the task's paths (a file or under a folder written with a trailing slash) or in the always allowed
    set; CLAUDE.local.md, .claude/, hooks/, seal/, rules/ and plugin/ only when the paths name them."""
    if os.path.basename(path) == "CLAUDE.local.md":
        return path in paths
    for p in paths:
        if path == p or (p.endswith("/") and path.startswith(p)):
            return True
    return path in build.ALWAYS_ALLOWED


def check_paths(host: Host, ctx: Ctx) -> None:
    raw = diff_entries(host, ctx.start, ctx.tip)
    allowed = tuple(ctx.task.paths) + tuple(ctx.widened)
    for path, old, new, _ in raw:
        if not _plain_path(path):
            raise _fail(ctx, 7, "red: outside paths")
        if new in ("120000", "160000") or old in ("160000",):
            raise _fail(ctx, 7, "red: link or submodule")
        if not path_allowed(path, allowed):
            raise _fail(ctx, 7, "red: outside paths")
    ctx.entries = [(p, o, n) for p, o, n, _ in raw]
    _ok(ctx, 7, "%d file(s)" % len(raw))


def show(host: Host, commit: str, path: str) -> bytes | None:
    r = git(host, "cat-file", "blob", "%s:%s" % (commit, path), check=False)
    return r.stdout if r.returncode == 0 else None


def _hunks(host: Host, a: str, b: str, path: str) -> tuple[int, list[int]]:
    """(deleted line count, added line numbers in b) of one file."""
    out = git(host, "diff", "-U0", "--no-color", "--no-ext-diff", a, b, "--", path).stdout.decode("utf-8", "replace")
    deleted, added = 0, []
    for line in out.split("\n"):
        m = re.match(r"@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))? @@", line)
        if m:
            start, count = int(m.group(1)), int(m.group(2) if m.group(2) is not None else 1)
            added += list(range(start, start + count))
        elif line.startswith("-") and not line.startswith("---"):
            deleted += 1
    return deleted, added


def _changelog_ok(host: Host, ctx: Ctx, today: datetime) -> bool:
    deleted, added = _hunks(host, ctx.start, ctx.tip, "CHANGELOG.md")
    if deleted or not added:
        return not deleted and not added
    text = (show(host, ctx.tip, "CHANGELOG.md") or b"").decode("utf-8", "replace").split("\n")
    heads = [i + 1 for i, ln in enumerate(text) if ln.startswith("## ")]
    if not heads:
        return False
    first, second = heads[0], heads[1] if len(heads) > 1 else len(text) + 1
    m = _WEEK_RE.match(text[first - 1])
    if not m:
        return False
    try:
        monday = datetime(int(m.group(2)), int(m.group(3)), int(m.group(4)))
    except ValueError:
        return False
    if monday.isocalendar()[:2] != today.isocalendar()[:2]:
        return False
    return all(first <= x < second for x in added)


def _week_heading(day: datetime) -> str:
    """The CHANGELOG heading of the ISO week of `day`, as it reads while the week runs."""
    y, w, _ = day.isocalendar()
    monday = datetime.fromisocalendar(y, w, 1)
    sunday = datetime.fromisocalendar(y, w, 7)
    return "## Week %d, %s to %s (in progress)" % (w, monday.strftime("%Y-%m-%d"), sunday.strftime("%Y-%m-%d"))


def rolled_changelog(text: str, today: datetime) -> tuple[str, int, int] | None:
    """CHANGELOG.md with the week of `today` opened: its heading and an empty `### Added` above the newest entry,
    which loses `(in progress)`. Returns (text, new week, closed week). None when the newest entry is this week or a
    later one or its heading is not a week heading (nothing is guessed then)."""
    lines = text.split("\n")
    first = next((i for i, ln in enumerate(lines) if ln.startswith("## ")), None)
    if first is None:
        return None
    m = _WEEK_RE.match(lines[first])
    if not m:
        return None
    try:
        monday = datetime(int(m.group(2)), int(m.group(3)), int(m.group(4)))
    except ValueError:
        return None
    if monday.isocalendar()[:2] >= today.isocalendar()[:2]:
        return None
    closed = lines[first].rstrip()
    if closed.endswith(" (in progress)"):
        closed = closed[:-len(" (in progress)")]
    lines[first:first + 1] = [_week_heading(today), "", "### Added", "", closed]
    return "\n".join(lines), today.isocalendar()[1], int(m.group(1))


def roll_week(host: Host) -> str | None:
    """Before the first task of an ISO week: open the week's CHANGELOG section on main of main.git as its own commit
    with the owner's identity (as 4a8cad6 did by hand), so check 8 does not refuse the first task of a Monday. Plumbing
    only (hash-object, mktree, commit-tree, update-ref with the old value): no work tree, no hook. Returns the new
    commit; None when nothing was due. Raises GitFailed when it was due and could not be written."""
    main = rev(host, "refs/heads/main")
    if main is None:
        return None
    data = show(host, main, "CHANGELOG.md")
    if data is None:
        return None
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        return None
    rolled = rolled_changelog(text, host.now())
    if rolled is None:
        return None
    new, week, closed = rolled
    identity = owner_identity(host)
    if identity is None:
        raise GitFailed("no owner identity in the home template")
    blob = git(host, "hash-object", "-w", "--stdin", stdin=new.encode("utf-8")).stdout.decode("ascii").strip()
    entries = git(host, "ls-tree", "-z", main).stdout.split(b"\0")
    out, found = [], False
    for e in entries:
        if not e:
            continue
        meta, _, name = e.partition(b"\t")
        if name == b"CHANGELOG.md":
            mode, kind, _old = meta.split(b" ")
            if kind != b"blob":
                raise GitFailed("CHANGELOG.md is not a file")
            meta, found = b" ".join((mode, kind, blob.encode("ascii"))), True
        out.append(meta + b"\t" + name)
    if not found:
        raise GitFailed("CHANGELOG.md is not at the top of main")
    tree = git(host, "mktree", "-z", stdin=b"\0".join(out) + b"\0").stdout.decode("ascii").strip()
    message = "CHANGELOG.md opens week %d and closes week %d\n" % (week, closed)
    commit = git(host, "-c", "user.name=%s" % identity[0], "-c", "user.email=%s" % identity[1], "commit-tree", tree,
                 "-p", main, stdin=message.encode("utf-8")).stdout.decode("ascii").strip()
    if not SHA_RE.fullmatch(commit):
        raise GitFailed("commit-tree gave no commit")
    git(host, "update-ref", "-m", "awb build: week %d" % week, "refs/heads/main", commit, main)
    return commit


def _json(data: bytes | None):
    try:
        return json.loads(data.decode("utf-8")) if data is not None else None
    except (UnicodeDecodeError, ValueError):
        return None


def replaced_nodes(task: build.Task, tests_json) -> set[str]:
    """The node ids `tests_replaced` covers: node ids as written plus the nodes of the `run` field of a named entry
    of build/tests.json at the start commit (a whole file when the run names one)."""
    nodes = {t for t in task.tests_replaced if "::" in t}
    ids = {t for t in task.tests_replaced if "::" not in t}
    entries = tests_json.get("tests") if isinstance(tests_json, dict) else None
    for e in entries if isinstance(entries, list) else []:
        if isinstance(e, dict) and e.get("id") in ids and isinstance(e.get("run"), str):
            nodes |= {w for w in e["run"].split() if w.startswith("tests/")}
    return nodes


def _tests_json_ok(host: Host, ctx: Ctx) -> bool:
    before = _json(show(host, ctx.start, "build/tests.json"))
    after = _json(show(host, ctx.tip, "build/tests.json"))
    if not isinstance(after, dict) or not isinstance(after.get("tests"), list):
        return False
    if not all(isinstance(e, dict) and isinstance(e.get("id"), str) for e in after["tests"]):
        return False
    if not isinstance(before, dict) or not isinstance(before.get("tests"), list):
        return True
    now = {e["id"]: e for e in after["tests"]}
    nodes = replaced_nodes(ctx.task, before)
    for e in before["tests"]:
        if not isinstance(e, dict):
            continue
        if now.get(e.get("id")) == e:
            continue
        run_nodes = {w for w in str(e.get("run", "")).split() if w.startswith("tests/")}
        if e.get("id") in ctx.task.tests_replaced or (run_nodes and run_nodes <= nodes):
            continue
        return False
    return True


def lost_rule_lines(host: Host, ctx: Ctx) -> int:
    names = git(host, "ls-tree", "-z", "--name-only", ctx.start, "rules/").stdout.split(b"\0")
    lost = 0
    for raw in names:
        name = raw.decode("utf-8", "replace")
        if not re.fullmatch(r"rules/[A-Za-z0-9_.-]+\.txt", name):
            continue
        old = {ln.strip() for ln in (show(host, ctx.start, name) or b"").decode("utf-8", "replace").split("\n")}
        new = {ln.strip() for ln in (show(host, ctx.tip, name) or b"").decode("utf-8", "replace").split("\n")}
        lost += len({x for x in old if x} - new)
    return lost


def check_books(host: Host, ctx: Ctx, today: datetime) -> None:
    changed = {p for p, _, _ in ctx.entries}
    if any(p.startswith("awb/") for p in changed) and "CHANGELOG.md" not in changed:
        raise _fail(ctx, 8, "red: changelog")
    if "CHANGELOG.md" in changed and not _changelog_ok(host, ctx, today):
        raise _fail(ctx, 8, "red: changelog")
    if "build/tests.json" in changed and not _tests_json_ok(host, ctx):
        raise _fail(ctx, 8, "red: tests.json")
    if lost_rule_lines(host, ctx):
        raise _fail(ctx, 8, "red: rules list lost lines")
    _ok(ctx, 8)


# --------------------------------------------------------------------------- checks 9 to 11: code as the build user

def counts(results: dict[str, str] | None) -> dict[str, int]:
    c = {"passed": 0, "failed": 0, "skipped": 0}
    for o in (results or {}).values():
        c["failed" if o in ("failed", "error") else o] += 1
    return c


def export(host: Host, ctx: Ctx, commit: str, name: str) -> Path:
    """`git archive` of a commit from main.git into the run folder (the owner's): what the suite unit extracts."""
    path = ctx.folder / name
    git(host, "archive", "--format=tar", "-o", str(path), commit)
    return path


def run_suite(host: Host, ctx: Ctx, mode: str, tar: Path, tokens: tuple[str, ...] = ()) -> tuple[Ended, float]:
    """The suite unit: no network but its own loopback (SUITE_ALLOW in a namespace of its own), nothing writable but
    its out folder, the clock of D-S6."""
    out = host.out(ctx.tid, ctx.n)
    spec = Spec("awb-task-%s-suite" % ctx.tid,
                [host.awb, "build", "unit", "suite", str(host.root), ctx.tid, str(ctx.n), mode, host.suite_python,
                 *tokens], host.suite_seconds, "/tmp", "/tmp", rw=[str(out)], hidden=_hidden(host, ctx, clone=False),
                allow=list(SUITE_ALLOW), netns=True, stdin=tar, stderr=ctx.folder / ("%s.err" % mode))
    t0 = host.wall()
    ended = wait(host, host.units.start(spec), ctx, None)
    return ended, (host.wall() - t0) / 60.0


def baseline(host: Host, ctx: Ctx) -> dict[str, str] | None:
    """The outcome per test of the start commit: from the cache of an earlier run, else the suite on its export."""
    cache = host.runs / ".baseline"
    data = _read(cache / ("%s.json" % ctx.start), LOG_MAX)
    if data is not None:
        got = _json(data)
        if isinstance(got, dict):
            return got
    tar = export(host, ctx, ctx.start, "base.tar")
    try:
        ended, _ = run_suite(host, ctx, "base", tar)
    finally:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(tar)
    if ended.result == "timeout":
        return None
    got = outcomes(_take(host.out(ctx.tid, ctx.n) / "base.xml", host.uid(), LOG_MAX))
    if got is not None:
        build._mkdir(cache)
        _write(cache / ("%s.json" % ctx.start), json.dumps(got, sort_keys=True))
    return got


def _tail(text: str, matcher) -> list[str]:
    """The last lines of suite.log for result.md: control characters out, capped, a line with a gate finding
    withheld."""
    from awb import gate

    out = []
    for ln in text.split("\n")[-TAIL_LINES:]:
        ln = "".join(c for c in ln if c == "\t" or c.isprintable())[:200]
        try:
            hit = gate._scan_text("suite", ln, matcher) if ln.strip() else []
        except Exception:
            hit = [None]
        out.append("(line withheld)" if hit else ln)
    return out


def check_suite(host: Host, ctx: Ctx) -> dict[str, str]:
    tar = export(host, ctx, ctx.tip, "export.tar")
    try:
        ended, minutes = run_suite(host, ctx, "suite", tar)
    finally:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(tar)
    ctx.suite_minutes = minutes
    out = host.out(ctx.tid, ctx.n)
    log = _take(out / "suite.log", host.uid(), LOG_MAX) or b""
    _write(ctx.folder / "suite.log", log)
    got = outcomes(_take(out / "suite.xml", host.uid(), LOG_MAX))
    ctx.suite = counts(got)
    if ended.result == "timeout":
        raise _fail(ctx, 9, "hung: suite")
    flaky: list[str] = []
    if ended.code != 0 and got is not None:
        nodes = failed_nodes(got)
        again = outcomes(_take(out / "rerun.xml", host.uid(), LOG_MAX)) if nodes else None
        if again is not None and all(again.get(i) == "passed" for i in nodes):
            flaky = nodes
            got = dict(got, **{i: "passed" for i in nodes})
            ctx.suite = counts(got)
            rerun_log = _take(out / "rerun.log", host.uid(), LOG_MAX) or b""
            _write(ctx.folder / "suite.log", log + b"\n--- the rerun of the failed nodes (A7)\n" + rerun_log)
    if (ended.code != 0 and not flaky) or got is None:
        try:
            matcher = _matcher(host.register)
        except Exception:
            matcher = None
        ctx.tail = _tail(log.decode("utf-8", "replace"), matcher)
        raise _fail(ctx, 9, "red: suite")
    _ok(ctx, 9, "%(passed)d passed, %(failed)d failed, %(skipped)d skipped" % ctx.suite +
        ("; %d passed on the one rerun" % len(flaky) if flaky else ""))
    if flaky:
        note_flaky(host, ctx, flaky)
    build._mkdir(host.runs / ".baseline")
    _write(host.runs / ".baseline" / ("%s.json" % ctx.tip), json.dumps(got, sort_keys=True))
    return got


def _node_shown(node: str, matcher) -> str:
    """A node id of the build user's junit file as the owner's records and build/queue may carry it: test file,
    class and function names and a short parameter id, after the gate's classes with the register; else withheld."""
    from awb import gate

    if matcher is None or len(node) > 200 or not _NODE_SHOWN_RE.fullmatch(node):
        return "(node withheld)"
    try:
        return "(node withheld)" if gate._scan_text("node", node, matcher) else node
    except Exception:
        return "(node withheld)"


def note_flaky(host: Host, ctx: Ctx, nodes: list[str]) -> None:
    """A7: the nodes that failed and passed on the one rerun. A line `flaky: NODE...` in checks.log (and so in
    result.md), the nodes in flaky.txt of the run folder, a row each in runs/.flaky.tsv; a node flaky for the second
    time in FLAKY_DAYS days adds a line to build/queue/FLAKY.md for the owner."""
    import hashlib

    try:
        matcher = _matcher(host.register)
    except Exception:
        matcher = None
    shown = [_node_shown(i, matcher) for i in nodes]
    ctx.lines.append("flaky: %s" % " ".join(shown))
    _write(ctx.folder / "flaky.txt", "\n".join(shown) + "\n")
    now = int(host.wall())
    path = host.runs / FLAKY_LOG
    rows = [r.split("\t") for r in (_text(path, LOG_MAX) or "").split("\n") if r.count("\t") == 4]
    rows = [r for r in rows if r[0].isdigit() and now - int(r[0]) < 366 * 86400]
    told = []
    me = (ctx.tid, str(ctx.n))
    for node, word in zip(nodes, shown):
        sha = hashlib.sha256(node.encode("utf-8", "replace")).hexdigest()[:16]
        if any(r[3] == sha and (r[1], r[2]) == me for r in rows):
            continue                    # a second round of the same run (main moved) counts once
        seen = sorted({(r[1], r[2]) for r in rows if r[3] == sha and now - int(r[0]) < FLAKY_DAYS * 86400})
        rows.append([str(now), ctx.tid, str(ctx.n), sha, word])
        if len(seen) == 1:
            told.append("- %s %s is flaky a second time in %d days (%s run %s, %s run %d): it failed and passed on the "
                        "rerun; fix the test or file a task" % (host.now().strftime("%Y-%m-%d"), word, FLAKY_DAYS,
                                                               seen[0][0], seen[0][1], ctx.tid, ctx.n))
    _write(path, "".join("\t".join(r) + "\n" for r in rows))
    if told:
        try:
            build._make_queue_dir(host.queue)
            with build._queue_lock(host.queue):
                try:
                    old = build._read_regular(host.queue / FLAKY_FILE, build.QUEUE_MAX)
                except FileNotFoundError:
                    old = b"# flaky tests: written by awb build serve, one line per node flaky twice in %d days\n\n" \
                          % FLAKY_DAYS
                build._write_atomic(host.queue / FLAKY_FILE, old + "".join(t + "\n" for t in told).encode("utf-8"))
        except (OSError, build.Refused) as err:
            ctx.lines.append("- build/queue/%s could not be written (%s)" % (FLAKY_FILE, type(err).__name__))


def _covered(node: str, nodes: set[str]) -> bool:
    return any(node == n or node.startswith(n + "::") or node.startswith(n + "[") for n in nodes)


def check_hollow(host: Host, ctx: Ctx, after: dict[str, str]) -> None:
    before = baseline(host, ctx)
    if before is None:
        raise _fail(ctx, 10, "held: baseline")
    nodes = replaced_nodes(ctx.task, _json(show(host, ctx.start, "build/tests.json")))
    lost = [i for i, o in before.items() if o == "passed" and after.get(i) != "passed" and not _covered(i, nodes)]
    diff = git(host, "diff", "-U0", "--no-color", "--no-ext-diff", ctx.start, ctx.tip, "--", "tests/").stdout
    for line in diff.decode("utf-8", "replace").split("\n"):
        if line.startswith("+") and not line.startswith("+++") and _MARKER_RE.search(line):
            ctx.markers += 1
        if line.startswith("-") and not line.startswith("---") and _TEST_DEF_RE.match(line[1:]):
            ctx.removed_tests += 1
    ctx.lost = len(lost)
    if lost:
        raise _fail(ctx, 10, "red: tests lost")
    _ok(ctx, 10, "%d skip or xfail marker(s) added, %d test line(s) removed" % (ctx.markers, ctx.removed_tests))


def check_own(host: Host, ctx: Ctx) -> None:
    if not ctx.task.check:
        _ok(ctx, 11, "none")
        return
    tar = export(host, ctx, ctx.tip, "export.tar")
    try:
        ended, _ = run_suite(host, ctx, "check", tar, ctx.task.check)
    finally:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(tar)
    if ended.result == "timeout" or ended.code != 0:
        raise _fail(ctx, 11, "red: check")
    _ok(ctx, 11)


def unit_run(host: Host, ctx: Ctx, kind: str, args: list[str], stdin: Path | None = None,
             seconds: float = HAND_SECONDS, rw: list[str] | None = None) -> Ended:
    """A helper unit. The prepare and the clean unit make and remove the clone, the out folder and the home and copy
    the login: they keep wt/ and home/. The handover and the rebase unit run git in the task's clone: they write its
    clone and out folder alone, see no other clone or home and no login and have no network (review of 2026-10-13,
    B1)."""
    name = "awb-task-%s-%s" % (ctx.tid, kind)
    home = host.home(ctx.tid, ctx.n)
    if args[0] in ("prepare", "rekey", "clean"):
        hidden, netns = [str(host.runs)], False
        rw = rw if rw is not None else [str(host.wt), str(host.homes)]
    else:
        hidden, netns = _hidden(host, ctx), True
        rw = rw if rw is not None else [str(host.clone(ctx.tid, ctx.n)), str(host.out(ctx.tid, ctx.n))]
    spec = Spec(name, [host.awb, "build", "unit", *args], seconds, "/tmp", str(home) if kind == "prep" else "/tmp",
                rw=rw, hidden=hidden, netns=netns, stdin=stdin, stderr=ctx.folder / ("%s.err" % args[0]))
    return wait(host, host.units.start(spec), ctx, None)


def clean(host: Host, ctx: Ctx, what: str) -> None:
    """The clean unit. A login of the task that was not copied back for its account (exit LOGIN_KEPT, B7) is one
    fixed line of the run's result; the owner never reads the login itself."""
    e = unit_run(host, ctx, "hand", ["clean", str(host.root), ctx.tid, str(ctx.n), what] +
                 (["login"] if host.model == "login" else []))
    if host.model == "login" and e.code == LOGIN_KEPT and LOGIN_KEPT_LINE not in ctx.lines:
        ctx.lines.append(LOGIN_KEPT_LINE)


def handover(host: Host, ctx: Ctx) -> bool:
    """The hand-over unit, then the owner's copies of task.bundle and status.txt into the run folder."""
    for name in ("task.bundle", "status.txt"):
        with contextlib.suppress(FileNotFoundError):
            os.unlink(ctx.folder / name)
    e = unit_run(host, ctx, "hand", ["handover", str(host.root), ctx.tid, str(ctx.n)])
    out = host.out(ctx.tid, ctx.n)
    status = _take(out / "status.txt", host.uid(), SMALL_MAX)
    if status is not None:
        _write(ctx.folder / "status.txt", status)
    _take(out / "task.bundle", host.uid(), BUNDLE_MAX, dest=ctx.folder / "task.bundle")
    return e.code == 0


# --------------------------------------------------------------------------- a branch from outside (F2 G1)

REF_RE = re.compile(r"[A-Za-z0-9_][A-Za-z0-9._/-]{0,199}")
"""What `awb build check` takes as REF: a branch, a remote branch or a commit hash; never an option or a range."""
CHECK_MAX_COMMITS = 200
CHECK_WAIT = 5.0            # how often `awb build check` looks whether the runner took and ended its check


def check_task(cid: str) -> build.Task:
    """The stand-in block of a check of a branch: no paths, no replaced test, no own check. Check 10 reads
    `tests_replaced`, so a branch from outside may drop no test that passed on its merge base."""
    return build.Task(cid, "check of a branch", 0, build.DEFAULTS["model"], build.DEFAULTS["effort"], (), (), (), (),
                      int(build.DEFAULTS["budget"]), int(build.DEFAULTS["clock"]), "build/queue/%s.md" % cid)


def _working(host: Host) -> str:
    return os.path.abspath(host.working or Path.cwd())


def resolve_ref(host: Host, ref: str) -> str | None:
    """REF in the owner's working copy as a commit hash (fsmonitor and hooks off); None when it is none."""
    if not REF_RE.fullmatch(ref) or ".." in ref:
        return None
    cmd = ["git", "-C", _working(host), "-c", "core.fsmonitor=false", "-c", "core.hooksPath=/dev/null", "rev-parse",
           "--verify", "--quiet", "--end-of-options", ref + "^{commit}"]
    try:
        r = host.git_run(cmd, input=None, capture_output=True, timeout=60, check=False,
                         env=dict(os.environ, GIT_TERMINAL_PROMPT="0", LANG="C"))
    except (OSError, subprocess.SubprocessError):
        return None
    out = r.stdout.decode("ascii", "replace").strip()
    return out if r.returncode == 0 and SHA_RE.fullmatch(out) else None


def fetch_ref(host: Host, ctx: Ctx, commit: str) -> bool:
    """The commit from the owner's working copy into refs/quarantine/<C-ID>/1/head of main.git: the objects checked,
    no tag, no FETCH_HEAD, no gc. A bundle would need a ref in his copy for a bare commit, so main.git fetches it."""
    head = quarantine(ctx) + "/head"
    r = git(host, "-c", "transfer.fsckObjects=true", "-c", "fetch.fsckObjects=true", "-c", "gc.auto=0", "fetch",
            "--quiet", "--no-tags", "--no-write-fetch-head", _working(host), "+%s:%s" % (commit, head), check=False)
    return r.returncode == 0 and rev(host, head) == commit


def check_branch_commits(host: Host, ctx: Ctx) -> None:
    """Check 4 for a branch from outside: its commit is in quarantine, it meets main at a merge base and brings one
    to CHECK_MAX_COMMITS commits, none of them and no changed blob above BLOB_MAX (sized before any read, B3)."""
    main = rev(host, "refs/heads/main")
    if not SHA_RE.fullmatch(ctx.tip) or main is None or rev(host, quarantine(ctx) + "/head") != ctx.tip:
        raise _fail(ctx, 4, "red: no commit")
    base = git(host, "merge-base", main, ctx.tip, check=False).stdout.decode("ascii", "replace").strip()
    if not SHA_RE.fullmatch(base):
        raise _fail(ctx, 4, "red: not on main")
    commits = git(host, "rev-list", "--reverse", "%s..%s" % (base, ctx.tip)).stdout.decode("ascii").split()
    if not commits:
        raise _fail(ctx, 4, "red: no commit")
    if len(commits) > CHECK_MAX_COMMITS:
        raise _fail(ctx, 4, "red: too many commits")
    big = oversized(host, commits)
    if big == "message":
        raise _fail(ctx, 4, "red: message too big")
    if big:
        raise _fail(ctx, 4, "red: blob too big")
    ctx.start, ctx.commits = base, commits
    _write(ctx.folder / "start", base + "\n")
    _ok(ctx, 4, "%d commit(s) after %s" % (len(commits), base[:7]))


def check_onto_main(host: Host, ctx: Ctx) -> None:
    """A2 for a branch from outside: when main moved past its merge base, the branch's changes go onto the main of
    now before the suite runs, so check 9 and 10 judge it against today's baseline. main.git has no working tree:
    `git merge-tree --write-tree` puts the changes from the merge base to the branch onto main (data only, no hook,
    no filter) and one commit of that tree on top of main is what the suite exports. A conflict is `red: rebase
    conflict`. The gate still reads the branch's own commits."""
    main = rev(host, "refs/heads/main")
    if main is None or main == ctx.start:
        return
    r = git(host, "merge-tree", "--write-tree", "--no-messages", "--merge-base=%s" % ctx.start, main, ctx.tip,
            check=False)
    tree = r.stdout.decode("ascii", "replace").split("\n", 1)[0].strip()
    if r.returncode == 1:
        ctx.lines.append("- main moved to %s: the branch does not rebase onto it without a conflict" % main[:7])
        raise Stop("red: rebase conflict")
    if r.returncode or not SHA_RE.fullmatch(tree):
        ctx.lines.append("- main moved to %s: the branch could not be put onto it" % main[:7])
        raise Stop("held: rebase")
    c = git(host, "-c", "user.name=awb build", "-c", "user.email=awb-build@localhost", "commit-tree", tree, "-p",
            main, "-m", "awb build check %s: the branch onto main" % ctx.tid)
    onto = c.stdout.decode("ascii", "replace").strip()
    if not SHA_RE.fullmatch(onto):
        raise Stop("held: rebase")
    git(host, "update-ref", quarantine(ctx) + "/onto", onto)
    ctx.start, ctx.tip = main, onto
    _write(ctx.folder / "start", main + "\n")
    _write(ctx.folder / "onto", onto + "\n")
    ctx.lines.append("- main moved: the branch was rebased onto %s before the checks" % main[:7])


def check_outdir(host: Host, ctx: Ctx) -> bool:
    """The out folder of the check's suite unit, made by a unit of the build user (a check has no prepare unit)."""
    e = unit_run(host, ctx, "hand", ["outdir", str(host.root), ctx.tid, str(ctx.n)], rw=[str(host.wt)])
    return e.code == 0
