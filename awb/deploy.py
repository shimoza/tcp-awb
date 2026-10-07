"""`sudo awb deploy`: one command that brings a commit of the repository live (T1, INTERFACES.md "The deploy").

    sudo awb deploy [--dry-run] [--all] [--only UNIT...] [--restart] [--to COMMIT] [--rollback] [--mirrors DIR...]
    awb deploy --dry-run            the plan without root, as the owner
    awb deploy status               the journal, the release each daemon runs, locked or down since when

Three users take part and each does only its own part:

- root (this module under sudo) checks the caller, takes the lock, validates what the owner's children hand it,
  writes the journal, extracts the release, runs the scripts of the release it extracted
  (`releases/<commit>/seal/setup.sh --update`, `.../seal/web/install.sh --only`) and talks to systemd. It runs
  installed code only, never a script of the working tree, never reads the vault, never sees a passphrase and
  never opens a socket. The root path imports neither `getpass` nor `awb.vault` nor `socket`: every function that
  does is named `owner_*` and runs only in a child that dropped root for good (`_owner_main` refuses euid 0).
- the owner (SUDO_USER) plans the deploy from the repository (`awb deploy plan --json`), writes the archive of
  the target commit (`awb deploy archive COMMIT`), types the passphrase (`awb deploy unlock`) and reads the
  states (`awb deploy status --json`). These four are hidden subcommands of the installed command.
- the work user runs `awb projects sync` when the rules or the client files changed.

The unit list is the set of unit templates of the target release (seal/*.service, seal/web/*.service,
seal/web/*.socket, seal/web/*.d/*.conf); this module keeps no list of unit names. The entry module of each
installed unit comes from systemd (`systemctl show -p ExecStart,DropInPaths`), its closure from the imports of the
target tree (`ast`, module and function level, DYNAMIC for importlib). A changed file restarts the units whose
closure holds it; the fixed rules of RULES cover the files outside the package; a file under awb/ in no closure
and not in COMMAND_ONLY restarts every unit and prints as unmapped.

The journal /opt/tcp-awb/DEPLOYED (root, 644) is written `running` before anything changes, updated after every
step and marked `done` at the end; a run that dies leaves its units pending for the next one.

Exit codes: 0 done and nothing pending, 1 a step failed or a unit is pending, 2 refused before anything changed.
"""
from __future__ import annotations

import ast
import fcntl
import json
import os
import pwd
import re
import shlex
import shutil
import stat
import subprocess
import sys
import tarfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from awb import config

OPT = Path("/opt/tcp-awb")
BIN = "/usr/local/bin/awb"
# the secure_path of sudo on this host: the children of root and of the drop get it, never the caller's PATH
SECURE_PATH = "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin:/snap/bin"
LOCK_NAME = "awb-deploy.lock"
JOURNAL_NAME = "DEPLOYED"
LOG_NAME = "deploy.log"
PRE_DEPLOY = "pre-deploy"
COMMIT_RE = re.compile(r"^[0-9a-f]{40}$")
UNIT_RE = re.compile(r"^awb-[a-z0-9-]+\.(service|socket)$")
SINCE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")
ACTIONS = ("restart", "reload", "unchanged", "not installed")
KINDS = ("vault", "keys", "service", "web")
STATES = ("unlocked", "locked", "plain", "down", "unknown")
RESULTS = ("pending", "restarted", "reloaded", "unchanged", "failed", "not installed")
WAIT_TOTAL = 20.0       # a restarted unit must be active within this many seconds
WAIT_HOLD = 3.0         # and keep its MainPID this long
FINAL_AFTER = 5.0       # the last read of NRestarts and ActiveState comes at least this long after the restarts
UNLOCK_TRIES = 3
PROBE_PATH = "/.awb-deploy-probe"   # a path no API of the web side acts on

# The two daemons that hold secrets are found by their command, not by a unit name: (command, subcommand) of the
# ExecStart of a seal/*.service template. Each runs one module of cli.DELEGATED; the hand-over (T2) is read from it.
DAEMON_COMMANDS = {("vault", "serve"): "vault", ("keys", "serve"): "keys"}
DAEMON_ORDER = ("vault", "keys")

# The entry modules that read the rules at their start: the units that start them restart when rules/** changed.
RULES_ENTRIES = frozenset({"awb/tcp/ask.py", "awb/tcp/web/chat_service.py"})

# importlib calls the import map cannot follow, keyed by file and the function that holds the call. SUBCOMMAND:
# the dispatcher of cli.py runs the module of the command it was given, which is resolved per unit from its
# ExecStart (`awb vault serve` runs awb/vault.py), not added to every unit.
SUBCOMMAND = "subcommand"
DYNAMIC: dict[tuple[str, str], object] = {
    ("awb/cli.py", "_delegate"): SUBCOMMAND,
    ("awb/cli.py", "_exchange_leftovers"): ("awb.tcp.xchg",),
    ("awb/career.py", "_writing_module"): ("awb.writing",),
    ("awb/review.py", "_writing_tells"): ("awb.writing",),
    ("awb/extract/__init__.py", "_call_reader"): ("awb.extract.archive", "awb.extract.mail", "awb.extract.office",
                                                  "awb.extract.pdf", "awb.extract.text"),
    ("awb/hooks.py", "_module"): ("awb.check", "awb.gate", "awb.writing", "awb.review", "awb.kb", "awb.career"),
    ("awb/deploy.py", "owner_keys"): ("awb.tcp.keys",),
}

# Modules no unit imports: they run as a command and in no daemon, so a change of one restarts nothing.
COMMAND_ONLY: dict[str, str] = {
    "awb/deploy.py": "this command; root runs the installed copy, so a change is live from the following deploy",
    "awb/seal.py": "awb seal check, run by the work user against the host",
    "awb/tcp/inbox.py": "awb inbox take, run in a working session",
    "awb/tcp/migrate.py": "awb migrate, run in a working session",
    "awb/tcp/refresh.py": "awb refresh, run by the owner or a timer of his",
    "awb/tcp/web/contract.py": "awb api write|check, writes and checks docs/api/openapi.yaml",
}

# Files outside the package, first match wins. A value is a tuple of effects: "all" (every unit), "rules" (the
# units of RULES_ENTRIES), "sync" (awb projects sync as the work user) or nothing at all.
RULES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("rules/", ("rules", "sync")),
    ("seal/work-claude/", ("sync",)),
    ("seal/setup.sh", ()),
    ("seal/verify.sh", ()),
    ("seal/needrestart-awb.conf", ()),
    ("seal/web/install.sh", ()),
    ("seal/README.md", ()),
    ("seal/web/README.md", ()),
    ("pyproject.toml", ("all",)),
    ("tests/", ()),
    ("docs/", ()),
    ("build/", ()),
    ("plugin/", ()),
    (".claude-plugin/", ()),
    ("presentations/", ()),
    ("calibration/", ()),
    ("workflows/", ()),
    ("hooks/", ()),
    ("LICENSE", ()),
    ("NOTICE", ()),
    (".gitignore", ()),
)
SYNC_FILES = frozenset({"awb/rulesync.py"})


class Refused(Exception):
    """Exit 2: refused before anything changed. The message carries no value of the caller."""


class StepFailed(Exception):
    """Exit 1: a step failed after the journal was written."""


# --------------------------------------------------------------------------- the host, injectable for the tests


@dataclass
class Host:
    root: Path = OPT
    etc: Path = Path("/etc")
    run_dir: Path = Path("/run")
    runner: Callable = subprocess.run
    geteuid: Callable = os.geteuid
    environ: dict = field(default_factory=lambda: dict(os.environ))
    module_file: Path = Path(__file__)
    getpwnam: Callable = pwd.getpwnam
    getgrouplist: Callable = os.getgrouplist
    clock: Callable = time.monotonic
    sleep: Callable = time.sleep
    cwd: Path | None = None
    bin: str = BIN
    daemons: Callable | None = None      # owner side: the states of the two daemons (tests give a stand-in)

    @property
    def units_dir(self) -> Path:
        return self.etc / "systemd" / "system"

    @property
    def journal(self) -> Path:
        return self.root / JOURNAL_NAME

    @property
    def releases(self) -> Path:
        return self.root / "releases"


def say(line: str) -> None:
    print(line, flush=True)


def cmdline(cmd) -> str:
    return " ".join(shlex.quote(str(a)) for a in cmd)


def short(commit: str | None) -> str:
    if not commit:
        return "none"
    return commit[:7] if COMMIT_RE.match(commit) else commit


# --------------------------------------------------------------------------- trees: the files of one commit


class DirTree:
    """The files of a folder (a release, a copy of the repository in a test)."""

    def __init__(self, root: Path):
        self.root = Path(root)
        self._files = sorted(p.relative_to(self.root).as_posix() for p in self.root.rglob("*")
                             if p.is_file() and "__pycache__" not in p.parts and ".git" not in p.parts)

    def files(self) -> list[str]:
        return list(self._files)

    def read(self, rel: str) -> str:
        return (self.root / rel).read_text(encoding="utf-8")


class GitTree:
    """The files of one commit, read with git as the user who runs it (the owner)."""

    def __init__(self, repo: Path, commit: str, runner: Callable):
        self.repo, self.commit, self.runner = Path(repo), commit, runner
        out = git(runner, repo, "ls-tree", "-r", "-z", "--name-only", commit)
        self._files = sorted(x for x in out.split("\0") if x)
        self._cache: dict[str, str] = {}

    def files(self) -> list[str]:
        return list(self._files)

    def preload(self, rels: list[str]) -> None:
        want = [r for r in rels if r not in self._cache]
        if not want:
            return
        res = self.runner(["git", "-C", str(self.repo), "cat-file", "--batch"], capture_output=True,
                          input="".join("%s:%s\n" % (self.commit, r) for r in want).encode("utf-8"))
        if res.returncode != 0:
            raise Refused("git cat-file failed in the repository")
        data, pos = res.stdout, 0
        for rel in want:
            nl = data.index(b"\n", pos)
            head = data[pos:nl].decode("ascii", "replace").split()
            pos = nl + 1
            if len(head) < 3 or head[1] != "blob":
                self._cache[rel] = ""
                continue
            size = int(head[2])
            self._cache[rel] = data[pos:pos + size].decode("utf-8", "replace")
            pos += size + 1

    def read(self, rel: str) -> str:
        if rel not in self._cache:
            self.preload([rel])
        return self._cache[rel]


def git(runner: Callable, repo: Path, *args: str) -> str:
    res = runner(["git", "-C", str(repo), *args], capture_output=True, text=True)
    if res.returncode != 0:
        raise Refused("git %s failed in the repository" % args[0])
    return res.stdout


# --------------------------------------------------------------------------- the unit templates


def templates(tree) -> dict[str, dict]:
    """{unit: {"kind": "service" or "web", "files": [template paths], "dropins": [drop-in paths]}} from the
    template files of a tree. The single source of the unit list."""
    out: dict[str, dict] = {}

    def entry(unit: str, kind: str) -> dict:
        return out.setdefault(unit, {"kind": kind, "files": [], "dropins": []})

    for rel in tree.files():
        parts = rel.split("/")
        if len(parts) == 2 and parts[0] == "seal" and parts[1].endswith(".service"):
            entry(parts[1], "service")["files"].append(rel)
        elif len(parts) == 3 and parts[:2] == ["seal", "web"] and parts[2].endswith((".service", ".socket")):
            entry(parts[2], "web")["files"].append(rel)
    for rel in tree.files():
        parts = rel.split("/")
        if len(parts) == 4 and parts[:2] == ["seal", "web"] and parts[2].endswith(".d") and parts[3].endswith(".conf"):
            unit = parts[2][:-2]
            entry(unit, "web" if unit not in out else out[unit]["kind"])["dropins"].append(rel)
    for info in out.values():
        info["files"].sort()
        info["dropins"].sort()
    return dict(sorted(out.items()))


def template_of(rel: str, units: dict[str, dict]) -> str | None:
    """The unit whose template or drop-in `rel` is."""
    for unit, info in units.items():
        if rel in info["files"] or rel in info["dropins"]:
            return unit
    return None


def exec_start(text: str, base: list[str] | None = None) -> list[str] | None:
    """The last ExecStart of a unit file or drop-in; an empty `ExecStart=` clears the one of `base`."""
    argv = base
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("ExecStart="):
            value = line[len("ExecStart="):].strip()
            argv = shlex.split(value) if value else None
    return argv


def template_entries(tree, units: dict[str, dict]) -> dict[str, list[str] | None]:
    """{unit: argv} from the templates and their drop-ins (the map a host gets once install.sh ran)."""
    out = {}
    for unit, info in units.items():
        argv = None
        for rel in info["files"] + info["dropins"]:
            argv = exec_start(tree.read(rel), argv)
        out[unit] = argv
    return out


# --------------------------------------------------------------------------- systemd, read only


SHOW_PROPS = ("Id", "LoadState", "ActiveState", "SubState", "MainPID", "ExecMainStartTimestampMonotonic", "CanReload",
              "ExecStart", "DropInPaths", "InactiveEnterTimestamp", "NRestarts", "Listen", "FragmentPath")


def systemd_show(runner: Callable, units: list[str], props=SHOW_PROPS) -> dict[str, dict]:
    """{unit: {property: value}} from one `systemctl show`. A unit systemd does not know has LoadState not-found."""
    if not units:
        return {}
    res = runner(["systemctl", "show", "-p", ",".join(props), "--", *units], capture_output=True, text=True)
    out: dict[str, dict] = {}
    if res.returncode != 0:
        return {u: {"LoadState": "unknown"} for u in units}
    blocks = [b for b in res.stdout.split("\n\n") if b.strip()]
    for i, block in enumerate(blocks):
        info: dict[str, str] = {}
        for line in block.splitlines():
            key, _, value = line.partition("=")
            info.setdefault(key, value)
        unit = info.get("Id") or (units[i] if i < len(units) else None)
        if unit:
            out[unit] = info
    for u in units:
        out.setdefault(u, {"LoadState": "unknown"})
    return out


def installed_argv(info: dict) -> list[str] | None:
    """The argv of the last ExecStart systemd shows for an installed unit."""
    found = re.findall(r"argv\[\]=(.*?) ; ignore_errors=", info.get("ExecStart", ""))
    return found[-1].split() if found else None


def is_installed(info: dict) -> bool:
    return info.get("LoadState") == "loaded"


# --------------------------------------------------------------------------- the import map


def _module_file(name: str, files: set[str]) -> str | None:
    p = name.replace(".", "/")
    if p + ".py" in files:
        return p + ".py"
    if p + "/__init__.py" in files:
        return p + "/__init__.py"
    return None


def _with_parents(name: str, files: set[str]) -> set[str]:
    """The file of a module and of every package above it (importing awb.tcp.keys runs awb/tcp/__init__.py)."""
    out = set()
    parts = name.split(".")
    for i in range(1, len(parts) + 1):
        f = _module_file(".".join(parts[:i]), files)
        if f:
            out.add(f)
    return out


def _package(rel: str) -> str:
    mod = rel[:-3].replace("/", ".")
    return mod[:-len(".__init__")] if mod.endswith(".__init__") else mod.rsplit(".", 1)[0]


def _enclosing(tree: ast.AST) -> dict[ast.AST, str]:
    """Each node to the name of the outermost function that holds it ("" at module level)."""
    owner: dict[ast.AST, str] = {}

    def walk(node, name):
        for child in ast.iter_child_nodes(node):
            n = name
            if not name and isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                n = child.name
            owner[child] = n
            walk(child, n)

    walk(tree, "")
    return owner


def _is_dynamic_call(node: ast.AST) -> bool:
    if not isinstance(node, ast.Call):
        return False
    f = node.func
    name = f.attr if isinstance(f, ast.Attribute) else f.id if isinstance(f, ast.Name) else ""
    if name not in ("import_module", "__import__"):
        return False
    arg = node.args[0] if node.args else None
    if isinstance(arg, ast.Constant) and isinstance(arg.value, str) and not arg.value.startswith("awb"):
        return False        # the standard library by a constant name
    return True


def parse_imports(rel: str, text: str, files: set[str]) -> tuple[set[str], list[str]]:
    """(the files of the package `rel` imports at any level, the functions that call importlib with a name the
    map cannot read)."""
    try:
        tree = ast.parse(text)
    except SyntaxError:
        return set(), ["<syntax error>"]
    pkg = _package(rel)
    out: set[str] = set()
    sites: list[str] = []
    owner = _enclosing(tree)
    for node in ast.walk(tree):
        names: list[str] = []
        if isinstance(node, ast.Import):
            names = [a.name for a in node.names]
        elif isinstance(node, ast.ImportFrom):
            base = node.module or ""
            if node.level:
                parts = pkg.split(".")
                parts = parts[:len(parts) - (node.level - 1)] if node.level > 1 else parts
                base = ".".join(parts + ([node.module] if node.module else []))
            names = [base] + ["%s.%s" % (base, a.name) for a in node.names]
        elif _is_dynamic_call(node):
            sites.append(owner.get(node, ""))
        for name in names:
            if name == "awb" or name.startswith("awb."):
                out |= _with_parents(name, files)
    out.discard(rel)
    return out, sites


@dataclass
class ImportMap:
    files: set[str]
    edges: dict[str, set[str]]
    sites: dict[str, list[str]]       # file -> functions with an importlib call
    delegated: dict[str, str]         # command -> module of cli.DELEGATED

    def closure(self, entries: set[str]) -> set[str]:
        seen: set[str] = set()
        todo = list(entries)
        while todo:
            f = todo.pop()
            if f in seen or f not in self.files:
                continue
            seen.add(f)
            todo.extend(self.edges.get(f, ()))
        return seen

    def unlisted_dynamic(self) -> list[str]:
        """'file:function' of every importlib call that DYNAMIC does not name."""
        return sorted("%s:%s" % (f, fn) for f, fns in self.sites.items() for fn in fns if (f, fn) not in DYNAMIC)


def delegated_of(text: str) -> dict[str, str]:
    """cli.DELEGATED of a tree, read with ast: {command: module}."""
    try:
        tree = ast.parse(text)
    except SyntaxError:
        return {}
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id == "DELEGATED" for t in node.targets):
            try:
                value = ast.literal_eval(node.value)
            except ValueError:
                return {}
            return {k: v[0] for k, v in value.items() if isinstance(v, tuple) and v}
    return {}


def import_map(tree) -> ImportMap:
    rels = [r for r in tree.files() if r.startswith("awb/") and r.endswith(".py")]
    if hasattr(tree, "preload"):
        tree.preload(rels)
    files = set(rels)
    edges, sites = {}, {}
    for rel in rels:
        edges[rel], found = parse_imports(rel, tree.read(rel), files)
        if found:
            sites[rel] = found
    for (rel, _fn), targets in DYNAMIC.items():
        if rel in files and targets != SUBCOMMAND:
            for name in targets:
                edges[rel] |= _with_parents(name, files)
    delegated = delegated_of(tree.read("awb/cli.py")) if "awb/cli.py" in files else {}
    return ImportMap(files, edges, sites, delegated)


def entry_files(argv: list[str] | None, imap: ImportMap, root: Path = OPT) -> set[str] | None:
    """The files a unit starts from, from its ExecStart: `awb SUB ...` (the installed command or `-m awb`), `-m
    awb.x.y` or a script under <root>/src. None when the entry cannot be read (every change restarts the unit)."""
    if not argv:
        return None
    files = imap.files
    module, rest = None, []
    if "-m" in argv:
        i = argv.index("-m")
        if i + 1 < len(argv):
            module, rest = argv[i + 1], argv[i + 2:]
    elif os.path.basename(argv[0]) == "awb":
        module, rest = "awb", argv[1:]
    else:
        for a in argv[1:]:
            for prefix in ("%s/src/" % root, "%s/releases/" % root):
                if a.startswith(prefix) and a.endswith(".py"):
                    rel = a[len(prefix):]
                    if prefix.endswith("releases/"):
                        rel = rel.split("/", 1)[1] if "/" in rel else rel
                    if rel in files:
                        return {rel}
        return None
    if module == "awb":
        out = {f for f in ("awb/__init__.py", "awb/__main__.py", "awb/cli.py") if f in files}
        sub = next((a for a in rest if not a.startswith("-")), None)
        if sub in imap.delegated:
            out |= _with_parents("awb." + imap.delegated[sub], files)
        return out
    found = _with_parents(module, files)
    main = module.replace(".", "/") + "/__main__.py"
    if main in files:
        found.add(main)
    return found or None


def daemon_kind(argv: list[str] | None) -> str | None:
    """vault or keys when the argv starts one of the two daemons."""
    if not argv:
        return None
    if "-m" in argv:
        i = argv.index("-m")
        if i + 1 >= len(argv) or argv[i + 1] != "awb":
            return None
        rest = argv[i + 2:]
    elif os.path.basename(argv[0]) == "awb":
        rest = argv[1:]
    else:
        return None
    words = [a for a in rest if not a.startswith("-")][:2]
    return DAEMON_COMMANDS.get(tuple(words)) if len(words) == 2 else None


def has_takeover(tree, rel: str) -> bool:
    """The module of a daemon in the target tree knows `--takeover` (the hand-over of T2)."""
    if rel not in tree.files():
        return False
    try:
        mod = ast.parse(tree.read(rel))
    except SyntaxError:
        return False
    return any(isinstance(n, ast.Constant) and n.value == "--takeover" for n in ast.walk(mod))


# --------------------------------------------------------------------------- from changed files to units


@dataclass
class UnitMap:
    units: dict[str, dict]
    imap: ImportMap
    argvs: dict[str, list[str] | None]
    entries: dict[str, set[str] | None]
    closures: dict[str, set[str] | None]
    reached: set[str]                 # every file some unit imports, on the installed or on the template map

    def effects(self, changed: list[str]) -> "Effects":
        return map_changes(changed, self.units, self.closures, self.entries, self.reached)


def unit_map(tree, installed: dict[str, list[str] | None] | None = None, root: Path = OPT) -> UnitMap:
    """The units of a tree and the closure of each, from the installed ExecStart lines (`installed`, None for the
    template map). A socket unit starts the service of its name."""
    units = templates(tree)
    imap = import_map(tree)

    def with_sockets(argv: dict) -> dict:
        argv = dict(argv)
        for u in units:
            if u.endswith(".socket"):
                argv[u] = argv.get(u[:-len(".socket")] + ".service")
        return argv

    t_argv = with_sockets(template_entries(tree, units))
    argvs = with_sockets(installed) if installed is not None else t_argv
    entries = {u: entry_files(argvs.get(u), imap, root) for u in units}
    closures = {u: imap.closure(entries[u]) if entries[u] else None for u in units}
    reached = set().union(*[c for c in closures.values() if c])
    for u in units:
        e = entry_files(t_argv.get(u), imap, root)
        reached |= imap.closure(e) if e else set()
    return UnitMap(units, imap, argvs, entries, closures, reached)


@dataclass
class Effects:
    units: dict[str, list[str]]       # unit -> the changed files that restart it
    templates: set[str]               # units whose template or drop-in changed
    sync: bool
    unmapped: list[str]
    every: list[str]                  # files that restart every unit


def map_changes(changed: list[str], units: dict[str, dict], closures: dict[str, set[str] | None],
                entries: dict[str, set[str] | None], reached: set[str] | None = None) -> Effects:
    """Which units a list of changed files restarts. `closures[unit]` is None when its entry could not be read:
    every change of the package restarts it. `reached` is every file some unit imports on the installed map or on
    the template map: a module only a template entry reaches (a unit this host runs differently) is mapped and
    restarts nothing here; a module neither reaches is unmapped and restarts every unit."""
    eff = Effects({}, set(), False, [], [])
    if reached is None:
        reached = set().union(*[c for c in closures.values() if c])

    def add(unit: str, rel: str) -> None:
        eff.units.setdefault(unit, []).append(rel)

    for rel in changed:
        unit = template_of(rel, units)
        if unit:
            add(unit, rel)
            eff.templates.add(unit)
            continue
        if rel.startswith("awb/"):
            hit = [u for u, c in closures.items() if c is None or rel in c]
            for u in hit:
                add(u, rel)
            if rel in SYNC_FILES:
                eff.sync = True
            if rel not in reached and rel not in COMMAND_ONLY:
                eff.unmapped.append(rel)
                eff.every.append(rel)
            continue
        rule = next((effects for prefix, effects in RULES
                     if rel == prefix or (prefix.endswith("/") and rel.startswith(prefix))), None)
        if rule is None and "/" not in rel and rel.endswith(".md"):
            rule = ()
        if rule is None:
            eff.unmapped.append(rel)
            eff.every.append(rel)
            continue
        if "all" in rule:
            eff.every.append(rel)
        if "sync" in rule:
            eff.sync = True
        if "rules" in rule:
            for u, e in entries.items():
                if e and e & RULES_ENTRIES:
                    add(u, rel)
    if eff.every:
        for u in units:
            for rel in eff.every:
                if rel not in eff.units.get(u, []):
                    add(u, rel)
    return eff


# --------------------------------------------------------------------------- the journal


def read_journal(path: Path) -> dict | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def write_journal(path: Path, data: dict) -> None:
    tmp = path.with_name(".%s.next" % path.name)
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o644)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=1, sort_keys=True)
        fh.write("\n")
    os.chmod(tmp, 0o644)
    os.replace(tmp, path)


def live_release(root: Path) -> str | None:
    """The release `src` points at, "plain" for the folder of before T1, None without one."""
    src = root / "src"
    if src.is_symlink():
        return os.path.basename(os.readlink(src).rstrip("/"))
    if src.is_dir():
        return "plain"
    return None


def boot_id() -> str:
    try:
        return Path("/proc/sys/kernel/random/boot_id").read_text().strip()
    except OSError:
        return ""


def monotonic_us() -> int:
    return time.clock_gettime_ns(time.CLOCK_MONOTONIC) // 1000


# --------------------------------------------------------------------------- the plan (as the owner)


@dataclass
class Options:
    dry_run: bool = False
    all: bool = False
    only: list[str] = field(default_factory=list)
    restart: bool = False
    to: str | None = None
    rollback: bool = False
    mirrors: list[str] = field(default_factory=list)


def owner_plan(opts: Options, repo: Path, host: Host, tree=None) -> dict:
    """The plan of a deploy, computed as the owner from the repository, the journal, systemd and the daemons. One
    JSON object; root validates it before use."""
    run = host.runner
    if git(run, repo, "status", "--porcelain").strip():
        raise Refused("the working tree has changes: commit first")
    journal = read_journal(host.journal)
    live = live_release(host.root)
    notes: list[str] = []
    if opts.rollback:
        prev = (journal or {}).get("previous")
        if not (isinstance(prev, str) and COMMIT_RE.match(prev)):
            raise Refused("there is no earlier release to roll back to")
        want = prev
    else:
        want = opts.to or "HEAD"
    res = run(["git", "-C", str(repo), "rev-parse", "--verify", "--quiet", "%s^{commit}" % want],
              capture_output=True, text=True)
    target = res.stdout.strip()
    if res.returncode != 0 or not COMMIT_RE.match(target):
        raise Refused("--to names no commit of the repository" if opts.to else "the target names no commit")
    target_time = git(run, repo, "log", "-1", "--format=%cd", "--date=format:%Y-%m-%d %H:%M", target).strip()

    def known(commit) -> bool:
        return isinstance(commit, str) and COMMIT_RE.match(commit) is not None and run(
            ["git", "-C", str(repo), "cat-file", "-e", "%s^{commit}" % commit], capture_output=True).returncode == 0

    first = live in (None, "plain")
    interrupted = False
    pending_last: list[str] = []
    base = None
    if first:
        notes.append("first deploy: the plain folder becomes releases/%s, every unit restarts" % PRE_DEPLOY
                     if live == "plain" else "first deploy: no release is live, every unit restarts")
    elif journal is None:
        notes.append("no journal: every unit restarts")
    else:
        interrupted = journal.get("state") != "done" or live != journal.get("target")
        if interrupted:
            notes.append("the last run was interrupted: every unit it planned is pending again")
            base = journal.get("previous")
            pending_last = sorted({u for u, e in (journal.get("units") or {}).items()
                                   if isinstance(e, dict) and e.get("action") in ("restart", "reload")}
                                  | set(journal.get("pending") or []))
        else:
            base = journal.get("target")
            pending_last = sorted(set(journal.get("pending") or []))
        if not known(base):
            notes.append("the deployed release is not a commit of this repository: every unit restarts")
            base = None
    if tree is None:
        tree = GitTree(repo, target, run)
    units = templates(tree)
    show = systemd_show(run, list(units))
    web = any(is_installed(show[u]) and str(host.root) in show[u].get("ExecStart", "")
              for u, info in units.items() if info["kind"] == "web")
    um = unit_map(tree, {u: installed_argv(show[u]) if is_installed(show[u]) else None for u in units}, host.root)
    imap, argvs = um.imap, um.argvs
    changed: list[str] = []
    if base is not None:
        changed = [x for x in git(run, repo, "diff", "--name-only", base, target).splitlines() if x]
    eff = um.effects(changed)

    stale: set[str] = set()
    if journal and journal.get("boot") == boot_id() and isinstance(journal.get("flip"), int):
        for u, e in (journal.get("units") or {}).items():
            if u in show and isinstance(e, dict) and e.get("action") in ("restart", "reload"):
                try:
                    started = int(show[u].get("ExecMainStartTimestampMonotonic") or 0)
                except ValueError:
                    started = 0
                if is_installed(show[u]) and not u.endswith(".socket") and 0 < started < journal["flip"] \
                        and u not in eff.units:
                    stale.add(u)
    daemons = (host.daemons or owner_daemon_states)()
    plan_units = []
    for u, info in units.items():
        kind = daemon_kind(argvs[u]) if info["kind"] == "service" else None
        kind = kind or info["kind"]
        installed = is_installed(show[u])
        reason, action = "", "unchanged"
        if not installed or (info["kind"] == "web" and not web):
            action = "not installed"
            reason = "no installed unit" if not installed else "the web side runs outside %s" % host.root
        elif first or base is None or opts.all:
            action, reason = "restart", "--all" if opts.all else "every unit"
        elif u in eff.units:
            action, reason = "restart", " ".join(sorted(set(eff.units[u]))[:4]) + (
                " and more" if len(set(eff.units[u])) > 4 else "")
        elif u in pending_last:
            action, reason = "restart", "pending from the last run"
        elif u in stale:
            action, reason = "restart", "started before the last flip"
        if opts.only:
            if u in opts.only and installed and action != "not installed":
                if action == "unchanged":
                    action, reason = "restart", "named with --only"
            elif action != "not installed":
                action, reason = "unchanged", "not named with --only"
        if action == "restart" and kind in DAEMON_ORDER:
            action, reason = _reload_or_restart(kind, u, show[u], daemons.get(kind) or {}, tree, imap, opts,
                                                u in eff.templates, reason)
        plan_units.append({"unit": u, "kind": kind, "action": action, "reason": reason, "installed": installed})
    for name in opts.only:
        if name not in units:
            raise Refused("--only names a unit that has no template")
        if not is_installed(show[name]):
            raise Refused("--only names a unit that is not installed")
    probes = {}
    for u, info in units.items():
        if u.endswith(".socket") and is_installed(show[u]):
            m = re.match(r"(/run/[A-Za-z0-9._/-]+)", show[u].get("Listen", ""))
            if m:
                probes[u] = m.group(1)
    keep = sorted({r for r in (journal or {}).get("running", {}).values() if isinstance(r, str)}
                  | {d["release"] for d in daemons.values() if d.get("release")})
    return {
        "version": 1,
        "repo": str(repo),
        "target": target,
        "target_time": target_time,
        "deployed": base,
        "live": live if live != "plain" else None,
        "first": bool(first),
        "interrupted": bool(interrupted),
        "rollback": bool(opts.rollback),
        "journal": (journal or {}).get("state") or "none",
        "notes": notes,
        "changed": changed,
        "unmapped": eff.unmapped,
        "sync": bool(eff.sync and not opts.only),
        "sync_reason": next((c for c in changed if c.startswith(("rules/", "seal/work-claude/")) or c in SYNC_FILES),
                            ""),
        "web": bool(web),
        "units": plan_units,
        "pending_last": pending_last,
        "daemons": {k: {"state": v.get("state", "unknown"), "since": v.get("since"), "release": v.get("release"),
                        "codes": v.get("codes"), "aliases": v.get("aliases")} for k, v in daemons.items()},
        "probes": probes,
        "keep": keep,
    }


def _reload_or_restart(kind, unit, show, state, tree, imap, opts, template_changed, reason) -> tuple[str, str]:
    """reload only when every condition of the hand-over holds (design 3.2), else restart with the reason."""
    module = None
    sub = {"vault": "vault", "keys": "keys"}[kind]
    if sub in imap.delegated:
        module = "awb/%s.py" % imap.delegated[sub].replace(".", "/")
    why = None
    if opts.restart:
        why = "--restart"
    elif show.get("ActiveState") != "active":
        why = "inactive"
    elif template_changed:
        why = "the unit file changed"
    elif show.get("CanReload") != "yes":
        why = "the installed unit has no reload"
    elif "reload" not in (state.get("ops") or []):
        why = "the running daemon predates the hand-over"
    elif not module or not has_takeover(tree, module):
        why = "the target has no hand-over"
    if why:
        return "restart", "%s (%s)" % (reason, why) if reason else why
    return "reload", reason


def owner_keys():
    """The client of the key service. The core reaches the platform package the way cli.py does, through the module
    of the delegated command."""
    import importlib

    from awb.cli import DELEGATED

    return importlib.import_module("awb.%s" % DELEGATED["keys"][0])


def owner_daemon_states() -> dict[str, dict]:
    """The state of the two daemons over their admin sockets, as the owner. No answer means down."""
    from awb import vault

    keys = owner_keys()

    out: dict[str, dict] = {}
    try:
        ans = vault.admin_call("status")
        out["vault"] = {"state": ans.get("state") if ans.get("state") in STATES else "unknown",
                        "since": vault.since_or_none(ans.get("since")), "codes": ans.get("codes"),
                        "release": _release_word(ans.get("release")), "ops": ans.get("ops") or []}
    except vault.VaultError:
        out["vault"] = {"state": "down"}
    try:
        ans = keys.request(keys.admin_socket(), {"op": "status"}, timeout=10)
        if ans.get("ok"):
            tenants = ans.get("tenants") or {}
            out["keys"] = {"state": "unlocked" if tenants else "locked", "aliases": sorted(tenants),
                           "release": _release_word(ans.get("release")), "ops": ans.get("ops") or []}
        else:
            out["keys"] = {"state": "unknown"}
    except keys.KeysError:
        out["keys"] = {"state": "down"}
    return out


def _release_word(value) -> str | None:
    """The release a daemon reports: a commit id, the name of its release folder or a real path to one."""
    if not isinstance(value, str):
        return None
    word = os.path.basename(value.rstrip("/"))
    return word if COMMIT_RE.match(word) or word == PRE_DEPLOY else None


# --------------------------------------------------------------------------- validation (as root)


def _plain(value, limit: int = 300) -> bool:
    return isinstance(value, str) and len(value) <= limit and all(c.isprintable() for c in value)


def validate_plan(plan, allowed_units: set[str]) -> dict:
    """The plan as root may use it, or Refused. Commit ids are 40 hex characters, unit names come from the
    template set of the installed release (a unit the target adds may only be `not installed`), actions and kinds
    from fixed sets, flags are booleans and every text is printable."""
    def bad(what):
        raise Refused("the plan does not validate (%s): a bug, nothing was changed" % what)

    if not isinstance(plan, dict):
        bad("not an object")
    if not isinstance(plan.get("target"), str) or not COMMIT_RE.match(plan["target"]):
        bad("target")
    for key in ("deployed", "live"):
        v = plan.get(key)
        if v is not None and not (isinstance(v, str) and (COMMIT_RE.match(v) or v == PRE_DEPLOY)):
            bad(key)
    for key in ("first", "interrupted", "rollback", "sync", "web"):
        if not isinstance(plan.get(key), bool):
            bad(key)
    for key in ("changed", "unmapped", "notes", "pending_last", "keep"):
        if not isinstance(plan.get(key), list) or not all(_plain(x) for x in plan[key]):
            bad(key)
    if not all(COMMIT_RE.match(x) or x == PRE_DEPLOY for x in plan["keep"]):
        bad("keep")
    for key in ("target_time", "journal", "sync_reason", "repo"):
        if not _plain(plan.get(key, "")):
            bad(key)
    units = plan.get("units")
    if not isinstance(units, list) or not units:
        bad("units")
    seen = set()
    for u in units:
        if not isinstance(u, dict):
            bad("unit")
        name = u.get("unit")
        if not isinstance(name, str) or not UNIT_RE.match(name) or name in seen:
            bad("unit name")
        seen.add(name)
        if u.get("action") not in ACTIONS or u.get("kind") not in KINDS or not isinstance(u.get("installed"), bool):
            bad("unit %s" % name)
        if name not in allowed_units and u["action"] != "not installed":
            bad("unit name")
        if not _plain(u.get("reason", "")):
            bad("reason")
    if not all(isinstance(x, str) and UNIT_RE.match(x) for x in plan["pending_last"]):
        bad("pending_last")
    daemons = plan.get("daemons")
    if not isinstance(daemons, dict) or not set(daemons) <= set(DAEMON_ORDER):
        bad("daemons")
    for d in daemons.values():
        if not isinstance(d, dict) or d.get("state") not in STATES:
            bad("daemon state")
        if d.get("since") is not None and not (isinstance(d["since"], str) and SINCE_RE.match(d["since"])):
            bad("since")
        r = d.get("release")
        if r is not None and not (isinstance(r, str) and (COMMIT_RE.match(r) or r == PRE_DEPLOY)):
            bad("release")
        if d.get("codes") is not None and not isinstance(d["codes"], int):
            bad("codes")
        al = d.get("aliases")
        if al is not None and not (isinstance(al, list) and all(isinstance(a, str) and re.match(r"^[\w.-]{1,40}$", a)
                                                                for a in al)):
            bad("aliases")
    probes = plan.get("probes")
    if not isinstance(probes, dict) or not all(
            k in seen and isinstance(v, str) and re.match(r"^/run/[A-Za-z0-9._/-]+$", v) and ".." not in v
            for k, v in probes.items()):
        bad("probes")
    return plan


class _SealTree(DirTree):
    """The seal folder of an installed release only (root reads nothing else of it)."""

    def __init__(self, release: Path):
        self.root = Path(release)
        seal = self.root / "seal"
        self._files = sorted(p.relative_to(self.root).as_posix() for p in seal.rglob("*") if p.is_file()) \
            if seal.is_dir() else []


# --------------------------------------------------------------------------- the drop to another user


def child_env(pw, environ: dict) -> dict:
    """The environment of a child that dropped root: built, never inherited."""
    env = {"HOME": pw.pw_dir, "USER": pw.pw_name, "LOGNAME": pw.pw_name, "SHELL": pw.pw_shell or "/bin/sh",
           "PATH": SECURE_PATH, "XDG_RUNTIME_DIR": "/run/user/%d" % pw.pw_uid}
    for key in ("LANG", "TERM"):
        if environ.get(key) and _plain(environ[key], 100):
            env[key] = environ[key]
    tty = _tty()
    if tty:
        env["GPG_TTY"] = tty
    return env


def _tty() -> str | None:
    for fd in (0, 1, 2):
        try:
            return os.ttyname(fd)
        except OSError:
            continue
    return None


def drop(pw, getgrouplist: Callable, groups: bool = True) -> dict:
    """The arguments of subprocess.run that drop root for good to the user `pw`."""
    out = {"user": pw.pw_uid, "group": pw.pw_gid}
    if groups:
        out["extra_groups"] = getgrouplist(pw.pw_name, pw.pw_gid)
    return out


def as_user(runner: Callable, cmd: list[str], pw, environ: dict, cwd, getgrouplist: Callable = os.getgrouplist,
            groups: bool = True, **kw):
    return runner(cmd, env=child_env(pw, environ), cwd=str(cwd), **drop(pw, getgrouplist, groups), **kw)


# --------------------------------------------------------------------------- the entry as root (step 0)


def read_conf(path: Path) -> dict[str, str]:
    out = {}
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return out
    for line in text.splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, _, v = line.partition("=")
            out[k.strip()] = v.strip()
    return out


def _under(path: str, base: str) -> bool:
    return path == base or path.startswith(base.rstrip("/") + "/")


@dataclass
class Caller:
    owner: object         # the passwd entry
    work: object | None   # the passwd entry of the work user, None when absent
    repo: Path
    conf: dict


def entry_checks(host: Host) -> Caller:
    """Step 0: each check with its own message; any failure is Refused and nothing ran."""
    if host.geteuid() != 0:
        raise Refused("run it with sudo: sudo awb deploy (awb deploy --dry-run works without)")
    name = host.environ.get("SUDO_USER") or ""
    if not name:
        raise Refused("run it with sudo as the owner (SUDO_USER is empty)")
    if name == "root":
        raise Refused("the owner must be a user, not root")
    conf = read_conf(host.etc / "awb" / "paths.conf")
    work_name = conf.get("work_user") or ""
    if name == work_name:
        raise Refused("the work user cannot deploy")
    if not conf.get("owner"):
        raise Refused("%s names no owner: run seal/setup.sh first" % (host.etc / "awb" / "paths.conf"))
    if name != conf["owner"]:
        raise Refused("only the owner of /etc/awb/paths.conf deploys")
    try:
        owner = host.getpwnam(name)
    except KeyError:
        raise Refused("the owner is not a user of this host") from None
    try:
        work = host.getpwnam(work_name) if work_name else None
    except KeyError:
        work = None
    cwd = Path(os.path.realpath(host.cwd or os.getcwd()))
    home = os.path.realpath(owner.pw_dir)
    if not _under(str(cwd), home) or str(cwd) == home:
        raise Refused("the working folder must lie in the owner's home")
    repo = cwd
    while not (repo / ".git").exists():
        if str(repo) == home or repo.parent == repo:
            raise Refused("the working folder is not a git work tree")
        repo = repo.parent
    if not _under(str(repo), home) or str(repo) == home:
        raise Refused("the working folder is not a git work tree in the owner's home")
    if os.stat(repo).st_uid != owner.pw_uid or os.stat(repo / ".git").st_uid != owner.pw_uid:
        raise Refused("the repository is not owned by the owner")
    module = os.path.realpath(host.module_file)
    if not _under(module, os.path.realpath(host.root)):
        raise Refused("root runs the installed command only: run sudo awb deploy, not a copy of the repository")
    return Caller(owner, work, repo, conf)


def take_lock(run_dir: Path) -> int:
    """The deploy lock: a root file opened O_CREAT 0600, flock on the descriptor, never unlinked, closed on exec.
    The kernel releases it with its holder. Refused with the pid and the start of the holder."""
    path = run_dir / LOCK_NAME
    fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_CLOEXEC | os.O_NOFOLLOW, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        try:
            text = os.pread(fd, 200, 0).decode("ascii", "replace").split()
        except OSError:
            text = []
        os.close(fd)
        pid = text[0] if text and text[0].isdigit() else "unknown"
        since = text[1] if len(text) > 1 and SINCE_RE.match(text[1]) else "unknown"
        raise Refused("another deploy runs since %s (pid %s)" % (since, pid)) from None
    os.ftruncate(fd, 0)
    os.pwrite(fd, ("%d %s\n" % (os.getpid(), time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()))).encode(), 0)
    return fd


def vault_busy(vault: str | None) -> bool:
    """True when an intake or an encryption holds the vault lock (probed with a zero wait, released at once)."""
    if not vault:
        return False
    try:
        fd = os.open(vault, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    except OSError:
        return False
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return False
    except BlockingIOError:
        return True
    finally:
        os.close(fd)


# --------------------------------------------------------------------------- printing the plan


def plan_lines(plan: dict, dry: bool) -> list[str]:
    head = "# awb deploy: dry run for the owner, every step is printed, none is run" if dry else \
        "# awb deploy for the owner"
    lines = [head, "# repository %s, target %s (%s), deployed %s" % (plan["repo"], short(plan["target"]),
                                                                   plan["target_time"], short(plan["deployed"]))]
    for note in plan["notes"]:
        lines.append("# " + note)
    d = plan["daemons"]
    lines.append("# journal: %s; %s" % (plan["journal"], "; ".join(
        "%s %s" % (k, _state_word(d[k])) for k in DAEMON_ORDER if k in d)))
    if plan["changed"]:
        shown = plan["changed"][:12]
        lines.append("# %d files changed: %s%s" % (len(plan["changed"]), " ".join(shown),
                                                    " ..." if len(plan["changed"]) > 12 else ""))
    for rel in plan["unmapped"]:
        lines.append("# unmapped: %s, every unit restarts" % rel)
    for u in plan["units"]:
        lines.append(("# %-28s %-14s %s" % (u["unit"], u["action"], u["reason"])).rstrip())
    lines.append("# pending from the last run: %s" % (" ".join(plan["pending_last"]) or "none"))
    return lines


def _state_word(d: dict) -> str:
    s = d.get("state")
    if s == "locked":
        return "locked since %s" % (d.get("since") or "unknown")
    if d.get("release"):
        return "%s, release %s" % (s, short(d["release"]))
    return s or "unknown"


def program_lines(plan: dict, opts: Options, host: Host, owner_name: str, work_name: str) -> list[str]:
    """The steps a real run takes, printed by the dry run."""
    t = plan["target"]
    acting = [u for u in plan["units"] if u["action"] in ("restart", "reload")]
    daemons = [u for kind in DAEMON_ORDER for u in acting if u["kind"] == kind]
    services = [u for u in acting if u["kind"] == "service"]
    web = [u for u in acting if u["kind"] == "web"]
    rel = host.releases / t
    out = ["+ write %s running" % host.journal,
           "+ %s > %s/.%s.tar   # as %s" % (cmdline([host.bin, "deploy", "archive", t]), host.releases, t,
                                            owner_name),
           "+ tar -x --no-same-permissions --no-same-owner -f %s/.%s.tar -C %s/.%s.partial" % (
               host.releases, t, host.releases, t),
           "+ chmod -R go-w %s/.%s.partial; mv -T -- %s/.%s.partial %s" % (host.releases, t, host.releases, t, rel),
           "+ " + cmdline([str(rel / "seal" / "setup.sh"), "--update"] + (
               ["--mirrors", *opts.mirrors] if opts.mirrors else []))]
    for u in daemons:
        out.append("+ systemctl %s %s" % (u["action"], u["unit"]))
    if plan["web"] and (services or web):
        out.append("+ " + cmdline([str(rel / "seal" / "web" / "install.sh"), "--only"] +
                                  [u["unit"] for u in services + web]))
    elif services:
        out.append("+ systemctl restart %s" % " ".join(u["unit"] for u in services))
    socket_services = {u["unit"][:-len(".socket")] + ".service" for u in acting if u["unit"].endswith(".socket")}
    watch = [u["unit"] for u in acting if u["unit"] not in socket_services]
    if watch:
        out.append("+ wait until active: %s" % " ".join(watch))
    if any(u["unit"] in plan["probes"] for u in acting):
        out.append("# a socket activated service is proven by one GET on its socket, as %s" % owner_name)
    restarted = {u["kind"] for u in daemons if u["action"] == "restart"}
    if "vault" in restarted:
        out.append("# the vault passphrase is asked once (restart)%s" % (
            "" if "keys" in restarted else "; the key service keeps its keys"))
    elif any(u["kind"] == "vault" and u["action"] == "reload" for u in daemons):
        out.append("# the vault passphrase is not asked: the reload keeps it")
    if "keys" in restarted:
        out.append("# the keys are loaded again from the password store (restart)")
    if plan["sync"]:
        out.append("+ awb projects sync   # as %s, %s changed" % (work_name, plan["sync_reason"]))
    out.append("+ awb vault status; awb keys status   # as %s" % owner_name)
    return out


# --------------------------------------------------------------------------- the run (as root)


class Run:
    """One real deploy, after the plan validated. Every step prints its command before it runs."""

    def __init__(self, host: Host, caller: Caller, plan: dict, opts: Options):
        self.host, self.caller, self.plan, self.opts = host, caller, plan, opts
        self.target = plan["target"]
        self.journal: dict = {}
        self.restarted_at: float | None = None
        self.failed: list[str] = []
        self.status: dict = {}
        self.root_env = {"PATH": SECURE_PATH, "HOME": "/root", "SUDO_USER": caller.owner.pw_name,
                         "LANG": host.environ.get("LANG") or "C.UTF-8"}
        if host.environ.get("TERM"):
            self.root_env["TERM"] = host.environ["TERM"]

    # -- helpers
    def sh(self, cmd: list[str], **kw):
        say("+ " + cmdline(cmd))
        return self.host.runner(cmd, env=self.root_env, **kw)

    def owner(self, args: list[str], **kw):
        return as_user(self.host.runner, [self.host.bin, "deploy", *args], self.caller.owner, self.host.environ,
                       self.caller.repo, self.host.getgrouplist, **kw)

    def save(self) -> None:
        write_journal(self.host.journal, self.journal)

    def mark(self, unit: str, result: str) -> None:
        self.journal["units"][unit]["result"] = result
        pend = set(self.journal["pending"])
        if result in ("restarted", "reloaded", "unchanged", "not installed"):
            pend.discard(unit)
        else:
            pend.add(unit)
        self.journal["pending"] = sorted(pend)
        if result in ("restarted", "reloaded"):
            self.journal["running"][unit] = self.target
        self.save()

    def show(self, units: list[str]) -> dict[str, dict]:
        return systemd_show(self.host.runner, units, ("Id", "LoadState", "ActiveState", "SubState", "MainPID",
                                                      "NRestarts", "InactiveEnterTimestamp"))

    # -- steps
    def start_journal(self) -> None:
        old = read_journal(self.host.journal) or {}
        running = {k: v for k, v in (old.get("running") or {}).items() if isinstance(v, str)}
        live = live_release(self.host.root)
        previous = PRE_DEPLOY if live == "plain" else live
        if previous == self.target and old.get("target") == self.target:
            previous = old.get("previous")
        if live == "plain":
            running = {u["unit"]: PRE_DEPLOY for u in self.plan["units"] if u["installed"]}
        self.journal = {
            "state": "running", "target": self.target, "previous": previous,
            "started": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "boot": boot_id(), "flip": None,
            "units": {u["unit"]: {"action": u["action"], "kind": u["kind"],
                                  "result": "pending" if u["action"] in ("restart", "reload") else u["action"]}
                      for u in self.plan["units"]},
            "units_from": old.get("units_from") or previous,
            "pending": sorted(u["unit"] for u in self.plan["units"] if u["action"] in ("restart", "reload")),
            "running": running,
        }
        say("+ write %s running" % self.host.journal)
        self.save()

    def extract(self) -> None:
        rel = self.host.releases / self.target
        partial = self.host.releases / (".%s.partial" % self.target)
        tar_file = self.host.releases / (".%s.tar" % self.target)
        os.makedirs(self.host.releases, mode=0o755, exist_ok=True)
        for leftover in (partial, tar_file):
            if leftover.is_symlink() or leftover.is_file():
                leftover.unlink()
            elif leftover.exists():
                say("+ rm -rf -- %s   # a leftover of an earlier run" % leftover)
                shutil.rmtree(leftover)
        if rel.is_dir() and not rel.is_symlink():
            say("= %s   # complete from an earlier run, reused" % rel)
            return
        say("+ %s > %s   # as %s" % (cmdline([self.host.bin, "deploy", "archive", self.target]), tar_file,
                                     self.caller.owner.pw_name))
        fd = os.open(tar_file, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC, 0o600)
        try:
            with os.fdopen(fd, "wb") as fh:
                res = self.owner(["archive", self.target], stdout=fh)
            if res.returncode != 0:
                raise StepFailed("the archive of %s failed" % short(self.target))
            check_archive(tar_file)
            os.mkdir(partial, 0o755)
            res = self.sh(["tar", "-x", "--no-same-permissions", "--no-same-owner", "-f", str(tar_file), "-C",
                           str(partial)])
            if res.returncode != 0:
                raise StepFailed("tar failed, %s was not created" % rel)
            check_tree(partial)
            res = self.sh(["chmod", "-R", "go-w", str(partial)])
            if res.returncode != 0:
                raise StepFailed("chmod failed on the new release")
            say("+ mv -T -- %s %s" % (partial, rel))
            os.rename(partial, rel)
        except BaseException:
            if partial.exists():
                shutil.rmtree(partial, ignore_errors=True)
            raise
        finally:
            if tar_file.exists():
                tar_file.unlink()

    def install(self) -> None:
        script = self.host.releases / self.target / "seal" / "setup.sh"
        cmd = [str(script), "--update"] + (["--mirrors", *self.opts.mirrors] if self.opts.mirrors else [])
        self.journal["flip"] = monotonic_us()
        self.save()
        res = self.sh(cmd)
        if res.returncode != 0:
            raise StepFailed("setup.sh --update failed: the old release stays live")
        self.journal["units_from"] = self.target
        self.save()

    def main_pid(self, unit: str) -> str:
        return self.show([unit])[unit].get("MainPID", "0")

    def daemons(self) -> None:
        for kind in DAEMON_ORDER:
            for u in self.plan["units"]:
                if u["kind"] != kind or u["action"] not in ("restart", "reload"):
                    continue
                if u["action"] == "reload":
                    before = self.main_pid(u["unit"])
                    res = self.sh(["systemctl", "reload", u["unit"]])
                    if res.returncode == 0:
                        self.mark(u["unit"], "reloaded")
                        continue
                    if self.main_pid(u["unit"]) != before:
                        say("# %s answered a failure but its main process changed: the hand-over happened"
                            % u["unit"])
                        self.mark(u["unit"], "reloaded")
                        continue
                    self.mark(u["unit"], "failed")
                    self.flip_back()
                    raise StepFailed("the reload of %s failed and the old daemon still serves: the code and the "
                                     "units of %s are back" % (u["unit"], short(self.journal["previous"])))
                res = self.sh(["systemctl", "restart", u["unit"]])
                self.restarted_at = self.host.clock()
                self.mark(u["unit"], "restarted" if res.returncode == 0 else "failed")
                if res.returncode != 0:
                    self.failed.append(u["unit"])

    def flip_back(self) -> None:
        prev = self.journal.get("previous")
        if not prev:
            say("# no earlier release to go back to")
            return
        script = self.host.releases / prev / "seal" / "setup.sh"
        self.sh([str(script), "--update", "--units-only"])
        flip(self.host.root, prev)
        self.journal["units_from"] = prev
        self.journal["target"] = prev
        self.journal["previous"] = self.target
        self.save()

    def services(self) -> None:
        acting = [u for u in self.plan["units"] if u["action"] in ("restart", "reload")]
        services = [u for u in acting if u["kind"] == "service"]
        web = [u for u in acting if u["kind"] == "web"]
        if self.plan["web"] and (services or web):
            script = self.host.releases / self.target / "seal" / "web" / "install.sh"
            res = self.sh([str(script), "--only"] + [u["unit"] for u in services + web])
            self.restarted_at = self.host.clock()
            for u in services + web:
                self.mark(u["unit"], "restarted" if res.returncode == 0 else "failed")
        elif services:
            res = self.sh(["systemctl", "restart", *[u["unit"] for u in services]])
            self.restarted_at = self.host.clock()
            for u in services:
                self.mark(u["unit"], "restarted" if res.returncode == 0 else "failed")

    def wait(self) -> None:
        acting = [u["unit"] for u in self.plan["units"]
                  if self.journal["units"][u["unit"]]["result"] in ("restarted", "reloaded")]
        socket_services = {u[:-len(".socket")] + ".service" for u in acting if u.endswith(".socket")}
        watch = [u for u in acting if u not in socket_services]
        if not watch:
            return
        say("+ wait until active: %s" % " ".join(watch))
        start = self.host.clock()
        seen: dict[str, tuple[str, float]] = {}
        done: set[str] = set()
        self.nrestarts: dict[str, str] = {}
        while True:
            left = [u for u in watch if u not in done]
            if not left:
                break
            info = self.show(left)
            now = self.host.clock()
            for u in left:
                i = info.get(u, {})
                self.nrestarts.setdefault(u, i.get("NRestarts", "0"))
                pid = i.get("MainPID", "0")
                if i.get("ActiveState") != "active":
                    seen.pop(u, None)
                    continue
                if u.endswith(".socket"):
                    done.add(u)
                    continue
                if u not in seen or seen[u][0] != pid:
                    seen[u] = (pid, now)
                elif now - seen[u][1] >= WAIT_HOLD:
                    done.add(u)
            if now - start >= WAIT_TOTAL:
                break
            if len(done) < len(watch):
                self.host.sleep(0.5)
        for u in watch:
            if u not in done:
                say("# %s did not come back: journalctl -u %s" % (u, u))
                self.mark(u, "failed")
                self.failed.append(u)

    def unlock(self) -> None:
        units = self.journal["units"]
        need = any(units[u["unit"]]["result"] == "restarted" and u["kind"] in DAEMON_ORDER
                   for u in self.plan["units"]) or any(
            self.plan["daemons"].get(k, {}).get("state") == "locked" for k in DAEMON_ORDER)
        if need:
            say("+ %s   # as %s" % (cmdline([self.host.bin, "deploy", "unlock"]), self.caller.owner.pw_name))
            res = self.owner(["unlock"])
            if res.returncode != 0:
                self.failed.append("unlock")

    def read_status(self) -> None:
        units = self.journal["units"]
        probes = []
        for unit, path in self.plan["probes"].items():
            if units.get(unit, {}).get("result") == "restarted":
                probes += ["--probe", path]
        say("+ %s   # as %s" % (cmdline([self.host.bin, "deploy", "status", "--json"] + probes),
                                self.caller.owner.pw_name))
        res = self.owner(["status", "--json"] + probes, capture_output=True, text=True)
        try:
            self.status = validate_status(json.loads(res.stdout or "{}"))
        except (ValueError, Refused):
            self.status = {}
            say("# the states of the daemons could not be read")
        for path, ok in (self.status.get("probes") or {}).items():
            if not ok:
                unit = next((u for u, p in self.plan["probes"].items() if p == path), None)
                if unit:
                    say("# %s gave no answer on %s: journalctl -u %s" % (unit, path, unit[:-7] + ".service"))
                    self.mark(unit, "failed")
                    self.failed.append(unit)

    def sync(self) -> None:
        if not self.plan["sync"]:
            return
        work = self.caller.work
        if work is None:
            say("# no work user on this host: awb projects sync skipped")
            return
        say("+ %s   # as %s, %s changed" % (cmdline([self.host.bin, "projects", "sync"]), work.pw_name,
                                             self.plan["sync_reason"]))
        res = as_user(self.host.runner, [self.host.bin, "projects", "sync"], work, self.host.environ, work.pw_dir,
                      self.host.getgrouplist)
        if res.returncode != 0:
            self.failed.append("projects sync")

    def final_check(self) -> None:
        if self.restarted_at is None:
            return
        rest = FINAL_AFTER - (self.host.clock() - self.restarted_at)
        if rest > 0:
            self.host.sleep(rest)
        watch = [u for u, e in self.journal["units"].items() if e["result"] in ("restarted", "reloaded")
                 and not (u.endswith(".service") and u[:-8] + ".socket" in self.journal["units"])]
        info = self.show(watch)
        for u in watch:
            i = info.get(u, {})
            before = getattr(self, "nrestarts", {}).get(u)
            if i.get("ActiveState") != "active" or (before is not None and i.get("NRestarts") != before):
                say("# %s is %s after the restart: journalctl -u %s" % (u, i.get("ActiveState") or "gone", u))
                self.mark(u, "failed")
                self.failed.append(u)

    def finish(self) -> int:
        self.journal["state"] = "done"
        self.journal["finished"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        for kind in DAEMON_ORDER:
            r = (self.status.get(kind) or {}).get("release")
            if r:
                self.journal.setdefault("daemons", {})[kind] = r
        self.save()
        line = "%s target=%s previous=%s result=%s pending=%s\n" % (
            self.journal["finished"], self.target, self.journal.get("previous") or "none",
            "ok" if not self.journal["pending"] and not self.failed else "failed",
            ",".join(self.journal["pending"]) or "none")
        log = self.host.root / LOG_NAME
        fd = os.open(log, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o644)
        with os.fdopen(fd, "a", encoding="utf-8") as fh:
            fh.write(line)
        if not self.journal["pending"] and not self.failed:
            keep = {self.target, self.journal.get("previous")} | set(self.plan["keep"]) | set(
                self.journal["running"].values()) | {(self.status.get(k) or {}).get("release") for k in DAEMON_ORDER}
            retain(self.host.releases, keep)
            return 0
        return 1


def flip(root: Path, release: str) -> None:
    """src -> releases/<release> in one exchange (renameat2 RENAME_EXCHANGE), the way setup.sh does it."""
    import ctypes

    nxt, src = root / "src.next", root / "src"
    if nxt.is_symlink() or nxt.is_file():
        nxt.unlink()
    say("+ ln -sfn releases/%s %s" % (release, nxt))
    os.symlink("releases/%s" % release, nxt)
    say("+ exchange %s %s   # renameat2 RENAME_EXCHANGE, one call" % (nxt, src))
    if src.exists() or src.is_symlink():
        libc = ctypes.CDLL(None, use_errno=True)
        if libc.renameat2(-100, os.fsencode(str(nxt)), -100, os.fsencode(str(src)), 2) != 0:
            raise StepFailed("the exchange failed (%s)" % os.strerror(ctypes.get_errno()))
        if nxt.is_symlink():
            nxt.unlink()
    else:
        os.rename(nxt, src)


def retain(releases: Path, keep: set) -> list[str]:
    """Remove every release folder but those in `keep`; returns what was removed."""
    removed = []
    if not releases.is_dir():
        return removed
    for p in sorted(releases.iterdir()):
        if p.name.startswith(".") or p.name in keep or p.is_symlink() or not p.is_dir():
            continue
        if not (COMMIT_RE.match(p.name) or p.name == PRE_DEPLOY):
            continue
        say("+ rm -rf -- %s   # an old release no daemon reports" % p)
        shutil.rmtree(p)
        removed.append(p.name)
    return removed


def check_archive(path: Path) -> None:
    """The archive of the owner child may hold folders, regular files and links only, no mode bit above 0o777, no
    absolute or climbing name and no entry under a link."""
    links: set[str] = set()
    with tarfile.open(path, "r:") as tf:
        for m in tf:
            name = m.name.rstrip("/")
            parts = name.split("/")
            if name.startswith("/") or ".." in parts or not name:
                raise StepFailed("the archive holds a name outside the release")
            if not (m.isfile() or m.isdir() or m.issym()):
                raise StepFailed("the archive holds an entry that is not a file, a folder or a link")
            if m.mode & 0o7000:
                raise StepFailed("the archive holds an entry with a setuid, setgid or sticky bit")
            if any("/".join(parts[:i]) in links for i in range(1, len(parts))):
                raise StepFailed("the archive holds an entry under a link")
            if m.issym():
                target = os.path.normpath(os.path.join(os.path.dirname(name), m.linkname))
                if m.linkname.startswith("/") or target.startswith(".."):
                    raise StepFailed("the archive holds a link out of the release")
                links.add(name)


def check_tree(folder: Path) -> None:
    for dirpath, dirnames, filenames in os.walk(folder):
        for n in dirnames + filenames:
            st = os.lstat(os.path.join(dirpath, n))
            if not (stat.S_ISREG(st.st_mode) or stat.S_ISDIR(st.st_mode) or stat.S_ISLNK(st.st_mode)):
                raise StepFailed("the extracted release holds a special file")
            if st.st_mode & 0o7000:
                raise StepFailed("the extracted release holds a setuid, setgid or sticky bit")


def validate_status(data) -> dict:
    """What the status child hands root, reduced to fixed words, commit ids, counts and alias names."""
    if not isinstance(data, dict):
        raise Refused("status")
    out: dict = {}
    for kind in DAEMON_ORDER:
        d = data.get(kind)
        if not isinstance(d, dict):
            continue
        state = d.get("state") if d.get("state") in STATES else "unknown"
        since = d.get("since") if isinstance(d.get("since"), str) and SINCE_RE.match(d["since"]) else None
        release = _release_word(d.get("release"))
        codes = d.get("codes") if isinstance(d.get("codes"), int) else None
        aliases = [a for a in d.get("aliases") or [] if isinstance(a, str) and re.match(r"^[\w.-]{1,40}$", a)]
        out[kind] = {"state": state, "since": since, "release": release, "codes": codes, "aliases": aliases}
    probes = data.get("probes")
    if isinstance(probes, dict):
        out["probes"] = {k: bool(v) for k, v in probes.items() if isinstance(k, str) and k.startswith("/run/")}
    return out


def status_lines(journal: dict, status: dict, show: dict[str, dict]) -> list[str]:
    """The status lines: three states per daemon and the split between a daemon and the code named."""
    lines = []
    target = journal.get("target")
    units = journal.get("units") or {}
    for kind in DAEMON_ORDER:
        unit = next((u for u, e in units.items() if e.get("kind") == kind), None)
        d = status.get(kind) or {}
        state = d.get("state", "unknown")
        if state == "down" or (unit and show.get(unit, {}).get("ActiveState") not in (None, "active")):
            since = (show.get(unit) or {}).get("InactiveEnterTimestamp") if unit else None
            text = "down since %s" % (since or "unknown")
        elif state == "locked":
            text = "locked since %s" % (d.get("since") or "unknown")
        elif kind == "vault":
            text = state + (", %d codes" % d["codes"] if isinstance(d.get("codes"), int) else "")
        else:
            text = (", ".join(d.get("aliases") or []) or "no keys") if state == "unlocked" else state
        release = d.get("release") or ((journal.get("running") or {}).get(unit) if unit else None)
        if release:
            text += ", release %s" % short(release)
        if release and target and release != target and unit:
            text += "; code and units: release %s; next: sudo awb deploy --only %s" % (short(target), unit)
        elif unit and unit in (journal.get("pending") or []):
            text += "; pending, next: sudo awb deploy"
        lines.append("%s: %s" % (kind, text))
    if journal.get("units_from") and journal.get("units_from") != target:
        lines.append("units from %s, code from %s" % (short(journal["units_from"]), short(target)))
    pend = journal.get("pending") or []
    lines.append("pending: %s" % (" ".join(pend) if pend else "none"))
    return lines


# --------------------------------------------------------------------------- the owner side


def owner_archive(commit: str, repo: Path, runner: Callable = subprocess.run) -> int:
    """`git archive COMMIT` to standard output, as the owner."""
    if not COMMIT_RE.match(commit):
        print("awb deploy archive: a commit id of 40 hex characters", file=sys.stderr)
        return 2
    sys.stdout.flush()
    return runner(["git", "-C", str(repo), "archive", "--format=tar", commit], stdout=sys.stdout.buffer).returncode


def owner_unlock(tries: int = UNLOCK_TRIES) -> int:
    """After a restart: the vault passphrase (getpass on /dev/tty, three tries) and the keys from the password
    store, as the owner. Each only when its daemon answers locked."""
    import getpass

    from awb import vault

    keys = owner_keys()
    rc = 0
    try:
        state = vault.admin_call("status").get("state")
    except vault.VaultError:
        state = "down"
    if state == "locked":
        for n in range(tries):
            try:
                pw = getpass.getpass("vault passphrase: ")
            except (EOFError, KeyboardInterrupt):
                print("awb deploy: no passphrase, the vault stays locked", file=sys.stderr)
                rc = 1
                break
            try:
                vault.admin_call("unlock", passphrase=pw)
                print("vault unlocked")
                break
            except vault.VaultError as err:
                print("awb deploy: %s" % err, file=sys.stderr)
            finally:
                pw = None
        else:
            print("awb deploy: three wrong tries, the vault stays locked: awb vault unlock", file=sys.stderr)
            rc = 1
    try:
        answer = keys.request(keys.admin_socket(), {"op": "status"}, timeout=10)
        if answer.get("ok") and not answer.get("tenants"):
            summary = keys.unlock()
            print("keys: %s" % (", ".join(sorted(summary)) or "none"))
    except keys.KeysError as err:
        print("awb deploy: keys: %s" % err, file=sys.stderr)
        rc = 1
    return rc


def owner_probe(path: str, timeout: float = 10.0) -> bool:
    """One GET on a socket of the web side: any HTTP answer proves the new code imported and bound."""
    import socket

    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.settimeout(timeout)
    try:
        s.connect(path)
        s.sendall(("GET %s HTTP/1.0\r\nHost: localhost\r\n\r\n" % PROBE_PATH).encode("ascii"))
        return s.recv(16).startswith(b"HTTP/")
    except OSError:
        return False
    finally:
        s.close()


def owner_status_json(probes: list[str]) -> dict:
    out = owner_daemon_states()
    out["probes"] = {p: owner_probe(p) for p in probes}
    return out


def owner_status(host: Host) -> int:
    """`awb deploy status`: the journal, the release each daemon reports and the split."""
    j = read_journal(host.journal)
    live = live_release(host.root)
    if j is None:
        print("no journal: %s" % ("the plain folder of before T1 is live" if live == "plain" else
                                  "nothing was deployed with awb deploy yet"))
        j = {"units": {}}
    else:
        print("journal: %s, target %s, previous %s, started %s%s" % (
            j.get("state"), short(j.get("target")), short(j.get("previous")), j.get("started"),
            ", finished %s" % j["finished"] if j.get("finished") else ""))
        if live != j.get("target"):
            print("live code: release %s, the journal names %s: run sudo awb deploy" % (short(live),
                                                                                     short(j.get("target"))))
        for u, e in sorted((j.get("units") or {}).items()):
            print("  %-28s %-14s %s" % (u, e.get("action"), e.get("result")))
    units = [u for u, e in (j.get("units") or {}).items() if e.get("kind") in DAEMON_ORDER]
    for line in status_lines(j, validate_status(owner_daemon_states()), systemd_show(host.runner, units)):
        print(line)
    return 0


def _owner_main(argv: list[str], host: Host, opts: Options | None = None) -> int:
    """The hidden subcommands and the dry run without root. They never run as root."""
    if host.geteuid() == 0:
        print("awb deploy: %s runs as the owner, never as root" % (argv[0] if argv else "this"), file=sys.stderr)
        return 2
    repo = Path(os.path.realpath(host.cwd or os.getcwd()))
    cmd = argv[0]
    try:
        if cmd == "plan":
            o = opts or _parse_plan_args(argv[1:])
            print(json.dumps(owner_plan(o, _repo_root(repo), host), sort_keys=True))
            return 0
        if cmd == "dry-run":
            o = opts or Options(dry_run=True)
            plan = owner_plan(o, _repo_root(repo), host)
            allowed = set(templates(_SealTree(Path(os.path.realpath(host.module_file)).parents[1])))
            plan = validate_plan(plan, allowed or {u["unit"] for u in plan["units"]})
            for line in plan_lines(plan, True):
                say(line)
            me = pwd.getpwuid(os.getuid()).pw_name
            for line in program_lines(plan, o, host, me, config.work_user() or "the work user"):
                say(line)
            say("# dry run done, nothing was changed")
            return 0
        if cmd == "archive":
            if len(argv) != 2:
                return 2
            return owner_archive(argv[1], _repo_root(repo))
        if cmd == "unlock":
            return owner_unlock()
        if cmd == "status":
            if "--json" in argv:
                probes = [argv[i + 1] for i, a in enumerate(argv) if a == "--probe" and i + 1 < len(argv)]
                print(json.dumps(owner_status_json([p for p in probes if p.startswith("/run/")])))
                return 0
            return owner_status(host)
    except Refused as err:
        print("awb deploy: %s" % err, file=sys.stderr)
        return 2
    return 2


def _repo_root(path: Path) -> Path:
    p = path
    while not (p / ".git").exists():
        if p.parent == p:
            raise Refused("the working folder is not a git work tree")
        p = p.parent
    return p


# --------------------------------------------------------------------------- command line


def _parser():
    from awb.cli import SafeParser

    ap = SafeParser(prog="awb deploy", description="Bring a commit of the repository live: sudo awb deploy.")
    ap.add_argument("--dry-run", action="store_true", help="print the plan and every step, run none")
    ap.add_argument("--all", action="store_true", help="restart every installed unit")
    ap.add_argument("--only", nargs="+", metavar="UNIT", default=[], help="these units only, also when unchanged")
    ap.add_argument("--restart", action="store_true", help="restart the two daemons instead of a reload")
    ap.add_argument("--to", metavar="COMMIT", help="deploy this commit instead of HEAD")
    ap.add_argument("--rollback", action="store_true", help="deploy the release that was live before this one")
    ap.add_argument("--mirrors", nargs="+", metavar="DIR", default=[], help="passed to setup.sh --update")
    return ap


def _parse_plan_args(argv: list[str]) -> Options:
    ap = _parser()
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args(argv)
    return Options(a.dry_run, a.all, a.only, a.restart, a.to, a.rollback, a.mirrors)


def _plan_argv(opts: Options) -> list[str]:
    out = ["plan", "--json"]
    if opts.all:
        out.append("--all")
    if opts.only:
        out += ["--only", *opts.only]
    if opts.restart:
        out.append("--restart")
    if opts.to:
        out += ["--to", opts.to]
    if opts.rollback:
        out.append("--rollback")
    return out


def root_deploy(opts: Options, host: Host) -> int:
    caller = entry_checks(host)
    lock = take_lock(host.run_dir)
    try:
        res = as_user(host.runner, [host.bin, "deploy", *_plan_argv(opts)], caller.owner, host.environ, caller.repo,
                      host.getgrouplist, capture_output=True, text=True)
        if res.returncode != 0:
            if res.stderr:
                sys.stderr.write(res.stderr[-2000:])
            raise Refused("the plan was refused, nothing was changed")
        try:
            plan = json.loads(res.stdout)
        except ValueError:
            raise Refused("the plan does not validate (not JSON): a bug, nothing was changed") from None
        allowed = set(templates(_SealTree(Path(os.path.realpath(host.module_file)).parents[1])))
        plan = validate_plan(plan, allowed)
        for line in plan_lines(plan, opts.dry_run):
            say(line)
        work_name = caller.work.pw_name if caller.work else "the work user"
        if opts.dry_run:
            for line in program_lines(plan, opts, host, caller.owner.pw_name, work_name):
                say(line)
            say("# dry run done, nothing was changed")
            return 0
        if any(u["action"] == "reload" for u in plan["units"]) and vault_busy(caller.conf.get("vault")):
            raise Refused("an intake holds the vault, deploy later")
        run = Run(host, caller, plan, opts)
        code = 1
        try:
            run.start_journal()
            run.extract()
            run.install()
            run.daemons()
            run.services()
            run.wait()
            run.unlock()
            run.read_status()
            run.sync()
            run.final_check()
            code = run.finish()
        except StepFailed as err:
            say("# awb deploy: %s" % err)
            try:
                run.save()
            except OSError:
                pass
            if run.journal and not run.status:
                run.read_status()
            code = 1
        finally:
            if run.journal:
                for line in status_lines(run.journal, run.status, run.show(
                        [u for u, e in run.journal.get("units", {}).items() if e.get("kind") in DAEMON_ORDER])):
                    say(line)
        if run.failed:
            say("# failed: %s" % " ".join(sorted(set(run.failed))))
        return code
    finally:
        os.close(lock)


def main(argv: list[str] | None = None, *, root: Path = OPT, etc: Path = Path("/etc"), run_dir: Path = Path("/run"),
         runner: Callable = subprocess.run, geteuid: Callable = os.geteuid, **more) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    host = Host(root=Path(root), etc=Path(etc), run_dir=Path(run_dir), runner=runner, geteuid=geteuid, **more)
    if config.is_work_user():
        print("awb deploy: the work user cannot deploy", file=sys.stderr)
        return 2
    if argv and argv[0] in ("plan", "archive", "unlock", "status"):
        return _owner_main(argv, host)
    try:
        a = _parser().parse_args(argv)
    except SystemExit as exc:
        return 0 if exc.code in (0, None) else 2
    opts = Options(a.dry_run, a.all, a.only, a.restart, a.to, a.rollback, a.mirrors)
    if opts.rollback and opts.to:
        print("awb deploy: --rollback and --to exclude each other", file=sys.stderr)
        return 2
    try:
        if host.geteuid() != 0:
            if opts.dry_run:
                return _owner_main(["dry-run"], host, opts)
            raise Refused("run it with sudo: sudo awb deploy (awb deploy --dry-run works without)")
        return root_deploy(opts, host)
    except Refused as err:
        print("awb deploy: %s" % err, file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("awb deploy: interrupted; the journal keeps what is pending, run sudo awb deploy again",
              file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
