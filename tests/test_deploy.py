"""Tests of `awb deploy` (awb/deploy.py, task T1): the unit list, the map from changed files to units, the plan and
its validation, the entry as root, the drop to the owner, the journal, the archive, the waits, the reload decision,
the retention, the rollback and the sync.

Nothing here runs as root, touches systemd or reads the host's /opt/tcp-awb: the root path runs with `geteuid`
returning 0, a temporary install root, a temporary /etc and /run and a fake runner that records every call and
answers from a script. Every test names the planted failure it must catch.
"""
from __future__ import annotations

import ast
import getpass
import io
import json
import os
import pwd
import shutil
import signal
import subprocess
import sys
import tarfile
import time
import types
from pathlib import Path

import pytest

from awb import config, deploy

REPO = Path(__file__).resolve().parent.parent
USER = getpass.getuser()
WORK = "awbwork"
BIN = "/usr/local/bin/awb"
GIT_ENV = dict(GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@example.org", GIT_COMMITTER_NAME="t",
               GIT_COMMITTER_EMAIL="t@example.org")


def git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, text=True,
                          env=dict(os.environ, **GIT_ENV)).stdout.strip()


def commit(repo: Path, message: str = "c") -> str:
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "--no-verify", "-m", message)
    return git(repo, "rev-parse", "HEAD")


def units_of_repo() -> dict[str, dict]:
    return deploy.templates(deploy.DirTree(REPO))


# --------------------------------------------------------------------------- an invented package for the map

SYNTH = {
    "awb/__init__.py": "",
    "awb/__main__.py": "from awb.cli import main\n",
    "awb/cli.py": ("import importlib\nfrom awb import config\n"
                   "DELEGATED = {'vault': ('vault', 'v'), 'keys': ('tcp.keys', 'k'), 'portal': ('tcp.portal', 'p'),"
                   " 'ask': ('tcp.ask', 'a')}\n"
                   "def _delegate(name):\n    return importlib.import_module('awb.%s' % DELEGATED[name][0])\n"
                   "def main():\n    pass\n"),
    "awb/config.py": "import os\n",
    "awb/vault.py": "from awb import config\n",
    "awb/rulesync.py": "",
    "awb/tcp/__init__.py": "",
    "awb/tcp/keys.py": "from awb.cli import main\n",
    "awb/tcp/portal.py": "from awb.cli import main\n",
    "awb/tcp/ask.py": "from awb.cli import main\n",
    "awb/tcp/web/__init__.py": "",
    "awb/tcp/web/chat_service.py": "from awb import cli\nfrom awb.tcp.web import chat_only\n",
    "awb/tcp/web/chat_only.py": "",
    "awb/tcp/web/projects_api.py": "from awb import cli\n",
    "awb/tcp/web/tenant_api.py": "from awb import cli\n",
    "awb/tcp/web/create_api.py": "from awb import cli\n",
    "awb/tcp/web/materials_api.py": "from awb import cli\n",
    "awb/tcp/web/gateway.py": "import json\n",
}


def synth_tree(tmp: Path, extra: dict[str, str] | None = None) -> deploy.DirTree:
    """The seal of the repository with the invented package above (no daemon imports the vault but its own)."""
    root = tmp / "synth"
    if root.exists():
        shutil.rmtree(root)
    shutil.copytree(REPO / "seal", root / "seal", ignore=shutil.ignore_patterns("__pycache__"))
    for rel, text in dict(SYNTH, **(extra or {})).items():
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_text(text, encoding="utf-8")
    return deploy.DirTree(root)


def restarted(tree, changed, installed=None) -> set[str]:
    return set(deploy.unit_map(tree, installed).effects(changed).units)


def problems(tree) -> list[str]:
    """What the honesty rule refuses: a module in no closure and not in COMMAND_ONLY, an importlib call DYNAMIC does
    not name, a COMMAND_ONLY entry that a unit does import or that does not exist."""
    um = deploy.unit_map(tree)
    imap, reached = um.imap, um.reached
    out = ["no unit: %s" % f for f in sorted(imap.files - reached) if f not in deploy.COMMAND_ONLY]
    out += ["dynamic: %s" % s for s in imap.unlisted_dynamic()]
    out += ["listed but imported: %s" % f for f in deploy.COMMAND_ONLY if f in reached]
    out += ["listed but missing: %s" % f for f in deploy.COMMAND_ONLY if f not in imap.files]
    return out


# --------------------------------------------------------------------------- the unit list and the map


def install_sh_units(text: str) -> set[str]:
    """Every unit named in a restart line of the switch (the `restart VERB UNIT...` lines; the rollback has its own)."""
    out = set()
    for line in text.splitlines():
        if line.startswith("restart "):
            out |= {w for w in line.split() if deploy.UNIT_RE.match(w)}
    return out


def unit_list_problems(seal: Path) -> list[str]:
    tree = deploy.DirTree(seal.parent)
    units = deploy.templates(tree)
    named = install_sh_units((seal / "web" / "install.sh").read_text(encoding="utf-8"))
    out = ["no template: %s" % u for u in sorted(named - set(units))]
    out += ["not restarted by install.sh: %s" % u for u, i in units.items() if i["kind"] == "web" and u not in named]
    return out


def test_the_unit_list_is_the_template_files(tmp_path):
    """Planted: a copy of seal/ with one template added and one restart line of install.sh removed; the check fails
    both ways."""
    units = units_of_repo()
    files = {p.name for p in (REPO / "seal").glob("*.service")} | {
        p.name for p in (REPO / "seal" / "web").iterdir() if p.suffix in (".service", ".socket")} | {
        p.name[:-2] for p in (REPO / "seal" / "web").glob("*.d")}
    assert set(units) == files
    assert unit_list_problems(REPO / "seal") == []
    # the plan names every template, installed or not
    src = (REPO / "awb" / "deploy.py").read_text(encoding="utf-8")
    assert "awb-vaultd" not in src and "awb-web" not in src and "awb-ask" not in src, "a unit name in the module"
    copy = tmp_path / "repo" / "seal"
    shutil.copytree(REPO / "seal", copy)
    (copy / "web" / "awb-new.service").write_text("[Service]\nExecStart=/usr/local/bin/awb portal serve\n")
    text = (copy / "web" / "install.sh").read_text(encoding="utf-8")
    text = text.replace("restart restart awb-console-tenants.service awb-web.service\n", "")
    (copy / "web" / "install.sh").write_text(text, encoding="utf-8")
    found = unit_list_problems(copy)
    assert "not restarted by install.sh: awb-new.service" in found
    assert "not restarted by install.sh: awb-web.service" in found
    assert "awb-new.service" in deploy.templates(deploy.DirTree(copy.parent))


def test_every_module_has_a_unit_or_is_listed(tmp_path):
    """Planted: awb/newthing.py imported by nothing fails the test; imported by awb/vault.py it lands under awb-vaultd
    only; a dynamic import whose target is not in DYNAMIC fails the test."""
    assert problems(deploy.DirTree(REPO)) == []
    for rel, why in deploy.COMMAND_ONLY.items():
        assert why.strip(), rel
    # a new module nobody imports
    copy = tmp_path / "copy"
    shutil.copytree(REPO / "awb", copy / "awb", ignore=shutil.ignore_patterns("__pycache__"))
    shutil.copytree(REPO / "seal", copy / "seal")
    (copy / "awb" / "newthing.py").write_text("X = 1\n")
    assert problems(deploy.DirTree(copy)) == ["no unit: awb/newthing.py"]
    # imported by the vault module: the vault daemon and nothing else
    tree = synth_tree(tmp_path, {"awb/newthing.py": "X = 1\n", "awb/vault.py": "from awb import config, newthing\n"})
    assert restarted(tree, ["awb/newthing.py"]) == {"awb-vaultd.service"}
    # a dynamic import nobody listed
    (copy / "awb" / "newthing.py").write_text(
        "import importlib\ndef load(x):\n    return importlib.import_module('awb.' + x)\n")
    (copy / "awb" / "vault.py").write_text((REPO / "awb" / "vault.py").read_text() + "\nfrom awb import newthing\n")
    assert problems(deploy.DirTree(copy)) == ["dynamic: awb/newthing.py:load"]


def test_the_map_from_changed_files(tmp_path):
    """Planted: each row of the task's table. On the invented package (where only the vault entry imports the
    vault module): awb/vault.py restarts awb-vaultd only, the gateway the gateway only, rules/work.md the Ask page
    and the chat plus projects sync, awb/cli.py every unit that starts through the package (the gateway is a plain
    script that imports none of it), a changed unit file its unit and as a restart, never a reload."""
    tree = synth_tree(tmp_path)
    assert restarted(tree, ["awb/vault.py"]) == {"awb-vaultd.service"}
    assert restarted(tree, ["awb/tcp/web/gateway.py"]) == {"awb-web.service"}
    um = deploy.unit_map(tree)
    units = um.units
    eff = um.effects(["rules/work.md"])
    assert set(eff.units) == {"awb-ask.service"} and eff.sync
    ask_page = dict(deploy.template_entries(tree, units), **{"awb-ask.service": [BIN, "ask", "serve"]})
    assert restarted(tree, ["rules/work.md"], ask_page) == {"awb-ask.service"}
    assert restarted(tree, ["awb/cli.py"]) == set(units) - {"awb-web.service"}
    assert restarted(tree, ["tests/test_vault.py", "docs/x.md", "COMMANDS.md", "seal/setup.sh"]) == set()
    assert restarted(tree, ["pyproject.toml"]) == set(units)
    eff = um.effects(["seal/awb-vaultd.service"])
    assert set(eff.units) == {"awb-vaultd.service"} and eff.templates == {"awb-vaultd.service"}
    eff = um.effects(["seal/web/awb-ask.service.d/95-project-chat.conf"])
    assert set(eff.units) == {"awb-ask.service"}
    eff = um.effects(["somewhere/new.txt"])
    assert eff.unmapped == ["somewhere/new.txt"] and set(eff.units) == set(units)
    # a daemon whose unit file changed restarts even when the hand-over is there
    show = {"ActiveState": "active", "CanReload": "yes"}
    state = {"ops": ["reload"]}
    tk = synth_tree(tmp_path, {"awb/vault.py": "TAKEOVER = '--takeover'\n"})
    imap = deploy.import_map(tk)
    opts = deploy.Options()
    assert deploy._reload_or_restart("vault", "u", show, state, tk, imap, opts, False, "x")[0] == "reload"
    action, reason = deploy._reload_or_restart("vault", "u", show, state, tk, imap, opts, True, "x")
    assert action == "restart" and "the unit file changed" in reason


def test_the_entry_module_comes_from_the_installed_unit(tmp_path):
    """Planted: systemd answers `awb ask serve` for awb-ask while the template drop-in names the chat service: a
    change of a module only the chat service imports must not restart awb-ask from the installed map and must from
    the template map."""
    tree = synth_tree(tmp_path)
    units = deploy.templates(tree)
    show_text = "\n\n".join(
        "Id=%s\nLoadState=loaded\nExecStart={ path=/usr/local/bin/awb ; argv[]=%s ; ignore_errors=no ; "
        "start_time=[n/a] }" % (u, "/usr/local/bin/awb ask serve --port 8081" if u == "awb-ask.service" else
                                 " ".join(deploy.template_entries(tree, units)[u] or ["/bin/true"]))
        for u in units)
    shown = deploy.systemd_show(lambda cmd, **kw: subprocess.CompletedProcess(cmd, 0, show_text, ""), list(units))
    installed = {u: deploy.installed_argv(shown[u]) for u in units}
    assert installed["awb-ask.service"] == [BIN, "ask", "serve", "--port", "8081"]
    assert "awb-ask.service" not in restarted(tree, ["awb/tcp/web/chat_only.py"], installed)
    assert "awb-ask.service" in restarted(tree, ["awb/tcp/web/chat_only.py"])


# --------------------------------------------------------------------------- the world of a root run


def passwd(name: str, home: Path) -> pwd.struct_passwd:
    return pwd.struct_passwd((name, "x", os.getuid(), os.getgid(), "", str(home), "/bin/bash"))


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t

    def sleep(self, s):
        self.t += s


class World:
    """A temporary host: /opt/tcp-awb with one release, /etc with the host file, /run, the owner's home with a
    repository, and the fake runner of the root run."""

    def __init__(self, tmp: Path):
        self.tmp = tmp
        self.home = tmp / "home"
        self.repo = self.home / "repo"
        self.repo.mkdir(parents=True)
        (self.repo / ".git").mkdir()
        self.work_home = tmp / "work"
        self.work_home.mkdir()
        self.root = tmp / "opt"
        self.c0 = "a" * 40
        self.c1 = "b" * 40
        rel = self.root / "releases" / self.c0
        shutil.copytree(REPO / "seal", rel / "seal")
        (rel / "awb").mkdir()
        (self.root / "src").symlink_to("releases/%s" % self.c0)
        self.etc = tmp / "etc"
        (self.etc / "awb").mkdir(parents=True)
        self.vault = tmp / "vault"
        self.vault.mkdir()
        (self.etc / "awb" / "paths.conf").write_text("owner = %s\nwork_user = %s\nvault = %s\n"
                                                     % (USER, WORK, self.vault))
        self.run_dir = tmp / "run"
        self.run_dir.mkdir()
        self.owner = passwd(USER, self.home)
        self.work = passwd(WORK, self.work_home)
        self.clock = Clock()
        self.runner = FakeRunner(self)
        self.environ = {"SUDO_USER": USER, "HOME": "/root", "PATH": "/usr/bin:/bin", "LANG": "C.UTF-8",
                        "PYTHONPATH": "/planted", "AWB_CONF": "/planted/paths.conf"}

    def getpwnam(self, name):
        if name == USER:
            return self.owner
        if name == WORK:
            return self.work
        raise KeyError(name)

    def main(self, argv, **kw):
        args = dict(root=self.root, etc=self.etc, run_dir=self.run_dir, runner=self.runner, geteuid=lambda: 0,
                    environ=self.environ, module_file=self.root / "src" / "awb" / "deploy.py",
                    getpwnam=self.getpwnam, getgrouplist=lambda name, gid: [gid, 4242], clock=self.clock,
                    sleep=self.clock.sleep, cwd=self.repo)
        args.update(kw)
        return deploy.main(argv, **args)

    def plan(self, **kw) -> dict:
        units = []
        for u, info in units_of_repo().items():
            kind = {"awb-vaultd.service": "vault", "awb-keyd.service": "keys"}.get(u, info["kind"])
            units.append({"unit": u, "kind": kind, "action": "unchanged", "reason": "", "installed": True})
        plan = {"version": 1, "repo": str(self.repo), "target": self.c1, "target_time": "2026-10-07 12:00",
                "deployed": self.c0, "live": self.c0, "first": False, "interrupted": False, "rollback": False,
                "journal": "done", "notes": [], "changed": [], "unmapped": [], "sync": False, "sync_reason": "",
                "web": True, "units": units, "pending_last": [], "probes": {}, "keep": [],
                "daemons": {"vault": {"state": "unlocked", "since": None, "release": None, "codes": 3,
                                      "aliases": None},
                            "keys": {"state": "unlocked", "since": None, "release": None, "codes": None,
                                     "aliases": ["t1"]}}}
        acts = kw.pop("actions", {})
        for u in plan["units"]:
            if u["unit"] in acts:
                u["action"] = acts[u["unit"]]
                u["reason"] = "test"
        plan.update(kw)
        return plan


class FakeRunner:
    """Records every call; answers the owner children, systemctl, setup.sh, install.sh and tar from a script."""

    def __init__(self, world: World):
        self.w = world
        self.calls: list[tuple[list[str], dict]] = []
        self.plan: dict | None = None
        self.status = {"vault": {"state": "unlocked", "codes": 3}, "keys": {"state": "unlocked", "aliases": ["t1"]}}
        self.rc: dict[tuple[str, str], int] = {}
        self.units: dict[str, dict] = {}
        self.on = None           # a hook (cmd, kw) called first; may raise
        self.pid = 100

    def unit(self, u: str) -> dict:
        return self.units.setdefault(u, {"LoadState": "loaded", "ActiveState": "active", "MainPID": "50",
                                         "NRestarts": "0", "InactiveEnterTimestamp": ""})

    def __call__(self, cmd, **kw):
        cmd = [str(c) for c in cmd]
        self.calls.append((cmd, kw))
        if self.on:
            res = self.on(cmd, kw)
            if res is not None:
                return res
        ok = subprocess.CompletedProcess(cmd, 0, "", "")
        name = os.path.basename(cmd[0])
        if cmd[0] == BIN and cmd[1] == "deploy":
            sub = cmd[2]
            if sub == "plan":
                return subprocess.CompletedProcess(cmd, 0, json.dumps(self.plan), "")
            if sub == "archive":
                fh = kw["stdout"]
                with tarfile.open(fileobj=fh, mode="w") as tf:
                    tf.add(REPO / "seal", arcname="seal")
                    info = tarfile.TarInfo("awb/deploy.py")
                    data = b"# a release\n"
                    info.size = len(data)
                    info.mode = 0o664
                    tf.addfile(info, io.BytesIO(data))
                return ok
            if sub == "status":
                return subprocess.CompletedProcess(cmd, 0, json.dumps(self.status), "")
            return ok
        if cmd[0] == BIN:
            return ok
        if name == "systemctl":
            verb = cmd[1]
            if verb == "show":
                units = cmd[cmd.index("--") + 1:]
                blocks = []
                for u in units:
                    props = {k: (v(self.w.clock()) if callable(v) else v) for k, v in self.unit(u).items()}
                    blocks.append("\n".join(["Id=%s" % u] + ["%s=%s" % kv for kv in props.items()]))
                return subprocess.CompletedProcess(cmd, 0, "\n\n".join(blocks) + "\n", "")
            for u in cmd[2:]:
                rc = self.rc.get((verb, u), 0)
                if rc:
                    return subprocess.CompletedProcess(cmd, rc, "", "")
                if verb in ("restart",):
                    self.pid += 1
                    for key, value in (("MainPID", str(self.pid)), ("ActiveState", "active")):
                        if not callable(self.unit(u).get(key)):      # a scripted state stays scripted
                            self.unit(u)[key] = value
            return ok
        if name == "setup.sh":
            if "--units-only" not in cmd:
                release = Path(cmd[0]).parents[1].name
                deploy.flip(self.w.root, release)
            return ok
        if name == "install.sh":
            return ok
        if name in ("tar", "chmod"):
            return subprocess.run(cmd, **{k: v for k, v in kw.items() if k != "env"})
        raise AssertionError("an unexpected call: %s" % cmd)

    def named(self, *words) -> list[tuple[list[str], dict]]:
        """The calls that carry every word, as an argument or at the end of one (a script's path)."""
        return [(c, k) for c, k in self.calls if all(any(a == w or a.endswith("/" + w) for a in c) for w in words)]

    def systemctl(self) -> list[list[str]]:
        return [c for c, _ in self.calls if os.path.basename(c[0]) == "systemctl" and c[1] != "show"]


@pytest.fixture
def world(tmp_path):
    return World(tmp_path)


@pytest.fixture
def no_getpass(monkeypatch):
    """A stand-in getpass module that records every prompt."""
    asked = []
    fake = types.ModuleType("getpass")
    fake.getpass = lambda *a, **k: asked.append(a) or "x"
    fake.getuser = getpass.getuser
    monkeypatch.setitem(sys.modules, "getpass", fake)
    return asked


# --------------------------------------------------------------------------- dry run and validation


def test_dry_run_runs_nothing_and_asks_nothing(world, no_getpass, capsys):
    """Planted: the fake runner records any systemctl, setup.sh or tar call and the fake getpass any prompt."""
    world.runner.plan = world.plan(actions={"awb-vaultd.service": "restart", "awb-ask.service": "restart"},
                                   changed=["awb/vault.py", "rules/work.md"], sync=True, sync_reason="rules/work.md")
    assert world.main(["--dry-run"]) == 0
    out = capsys.readouterr().out
    assert [c for c, _ in world.runner.calls] == [[BIN, "deploy", "plan", "--json"]]
    assert no_getpass == []
    assert not (world.root / "DEPLOYED").exists()
    assert sorted(p.name for p in (world.root / "releases").iterdir()) == [world.c0]
    for line in ("# awb-vaultd.service           restart        test", "+ systemctl restart awb-vaultd.service",
                 "# the vault passphrase is asked once (restart); the key service keeps its keys",
                 "# dry run done, nothing was changed"):
        assert line in out.splitlines(), line
    assert "awb projects sync   # as %s, rules/work.md changed" % WORK in out


def test_the_plan_is_validated_against_allowlists(world, capsys):
    """Planted: a unit name outside the template set, an action outside the fixed set or a commit id of 39
    characters: exit 2 before any root step."""
    bad = []
    p = world.plan()
    p["units"][0]["unit"] = "sshd.service"
    bad.append(p)
    p = world.plan()
    p["units"][0]["unit"] = "awb-evil.service"
    p["units"][0]["action"] = "restart"
    bad.append(p)
    p = world.plan()
    p["units"][0]["action"] = "stop"
    bad.append(p)
    bad.append(world.plan(target="b" * 39))
    p = world.plan()
    p["first"] = "yes"
    bad.append(p)
    p = world.plan(changed=["awb/x.py\x1b[2J"])
    bad.append(p)
    for plan in bad:
        world.runner.calls.clear()
        world.runner.plan = plan
        assert world.main([]) == 2
        assert [c for c, _ in world.runner.calls] == [[BIN, "deploy", "plan", "--json"]]
        assert not (world.root / "DEPLOYED").exists()
        assert "does not validate" in capsys.readouterr().err
    # a unit the target adds may be listed as not installed
    p = world.plan()
    p["units"].append({"unit": "awb-new.service", "kind": "service", "action": "not installed", "reason": "",
                       "installed": False})
    world.runner.plan = p
    assert world.main(["--dry-run"]) == 0


# --------------------------------------------------------------------------- root never prompts, the drop


ROOT_FORBIDDEN = ("getpass", "socket", "awb.vault", "awb.tcp.keys")


def root_path_problems(source: str) -> list[str]:
    """getpass, socket, the vault or the key client imported outside an owner_* function, or an owner_* function
    called from a function that is neither owner_* nor _owner_main."""
    tree = ast.parse(source)
    out = []
    owner = {}

    def walk(node, fn):
        for child in ast.iter_child_nodes(node):
            f = child.name if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)) and not fn else fn
            owner[child] = f
            walk(child, f)

    walk(tree, "")
    for node in ast.walk(tree):
        names = []
        if isinstance(node, ast.Import):
            names = [a.name for a in node.names]
        elif isinstance(node, ast.ImportFrom):
            names = [node.module or ""] + ["%s.%s" % (node.module, a.name) for a in node.names]
        fn = owner.get(node, "")
        if any(n in ROOT_FORBIDDEN for n in names) and not fn.startswith("owner_"):
            out.append("import in %s" % (fn or "the module"))
        if deploy._is_dynamic_call(node) and not fn.startswith("owner_"):
            out.append("importlib in %s" % (fn or "the module"))
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id.startswith("owner_"):
            if not (fn.startswith("owner_") or fn == "_owner_main"):
                out.append("owner call in %s" % (fn or "the module"))
    return out


def test_the_root_path_never_prompts(world, no_getpass, capsys):
    """Planted: an `import getpass` or a module level `awb.vault` import in the root path fails the ast check; a
    fake getpass with geteuid 0 records zero calls over a full run; unlock runs only as a child that dropped root."""
    source = (REPO / "awb" / "deploy.py").read_text(encoding="utf-8")
    assert root_path_problems(source) == []
    assert root_path_problems("import getpass\n" + source)
    assert root_path_problems(source.replace("from awb import config\n", "from awb import config, vault\n", 1))
    assert root_path_problems(source.replace("def root_deploy(opts: Options, host: Host) -> int:\n",
                                             "def root_deploy(opts: Options, host: Host) -> int:\n"
                                             "    importlib.import_module('awb.' + 'vault')\n", 1))
    assert root_path_problems(source.replace("def root_deploy(opts: Options, host: Host) -> int:\n",
                                             "def root_deploy(opts: Options, host: Host) -> int:\n"
                                             "    owner_unlock()\n", 1))
    # the hidden subcommands refuse root
    host = deploy.Host(geteuid=lambda: 0, cwd=world.repo)
    assert deploy._owner_main(["unlock"], host) == 2
    assert "never as root" in capsys.readouterr().err
    # a full run that restarts the vault daemon
    world.runner.plan = world.plan(actions={"awb-vaultd.service": "restart"})
    assert world.main([]) == 0, capsys.readouterr()
    assert no_getpass == []
    unlock = world.runner.named(BIN, "unlock")
    assert len(unlock) == 1
    _cmd, kw = unlock[0]
    assert kw["user"] == world.owner.pw_uid and kw["group"] == world.owner.pw_gid
    assert kw["extra_groups"] == [world.owner.pw_gid, 4242]
    assert kw["env"]["HOME"] == str(world.home) and "PYTHONPATH" not in kw["env"]


DROP_KEYS = {"HOME", "USER", "LOGNAME", "SHELL", "PATH", "XDG_RUNTIME_DIR", "LANG", "TERM", "GPG_TTY"}


def test_the_drop_to_the_owner_is_complete(world, capsys):
    """Planted: a call without extra_groups, with a PYTHON* or AWB_* variable or with root's HOME fails."""
    world.runner.plan = world.plan(actions={"awb-vaultd.service": "restart", "awb-ask.service": "restart"},
                                   sync=True, sync_reason="rules/work.md")
    assert world.environ["HOME"] == "/root"
    assert world.main([]) == 0, capsys.readouterr()
    children = [(c, k) for c, k in world.runner.calls if c[0] == BIN]
    assert {c[2] if c[1] == "deploy" else c[1] for c, _ in children} == {"plan", "archive", "unlock", "status",
                                                                         "projects"}
    for cmd, kw in children:
        assert {"user", "group", "extra_groups", "env", "cwd"} <= set(kw), cmd
        env = kw["env"]
        assert set(env) <= DROP_KEYS, cmd
        assert not [k for k in env if k.startswith(("PYTHON", "AWB_"))]
        assert env["HOME"] != "/root" and env["PATH"] == deploy.SECURE_PATH
        who = world.work if cmd[1] == "projects" else world.owner
        assert env["HOME"] == who.pw_dir and kw["cwd"] == (who.pw_dir if cmd[1] == "projects" else str(world.repo))
        assert kw["extra_groups"] == [who.pw_gid, 4242]
    # the root children (systemctl, setup.sh, tar) never get the caller's environment either
    for cmd, kw in world.runner.calls:
        if cmd[0] != BIN and "env" in kw:
            assert "PYTHONPATH" not in kw["env"] and "AWB_CONF" not in kw["env"], cmd


def test_the_drop_for_real_with_the_own_uid(tmp_path):
    """Planted: a wrong HOME or an extra variable fails. The runner is the real run of subprocess with the test's
    own ids; the group drop needs root and stays proven by the recorded call above."""
    me = passwd(USER, tmp_path)
    code = "import json, os; print(json.dumps([os.getuid(), os.getgid(), os.environ.get('HOME'), sorted(os.environ)]))"
    environ = {"HOME": "/elsewhere", "PYTHONPATH": "/planted", "AWB_CONF": "/planted", "LANG": "C.UTF-8",
               "TERM": "xterm", "SSH_AUTH_SOCK": "/planted"}
    res = deploy.as_user(subprocess.run, [sys.executable, "-I", "-c", code], me, environ, tmp_path, groups=False,
                         capture_output=True, text=True)
    assert res.returncode == 0, res.stderr
    uid, gid, home, keys = json.loads(res.stdout)
    assert (uid, gid, home) == (os.getuid(), os.getgid(), str(tmp_path))
    assert set(keys) <= DROP_KEYS and {"HOME", "USER", "PATH", "LANG", "TERM"} <= set(keys), keys


# --------------------------------------------------------------------------- refusals before anything runs


def test_a_dirty_tree_is_refused_with_exit_2_and_nothing_runs(tmp_path, capsys):
    """Planted: a tree with one unstaged change; the plan must refuse before any other call."""
    repo = tmp_path / "repo"
    repo.mkdir()
    git(repo, "init", "-q")
    (repo / "a.txt").write_text("1\n")
    commit(repo)
    (repo / "a.txt").write_text("2\n")
    calls = []

    def runner(cmd, **kw):
        calls.append(cmd)
        return subprocess.run(cmd, **kw)

    host = deploy.Host(root=tmp_path / "opt", runner=runner, geteuid=os.geteuid, cwd=repo)
    with pytest.raises(deploy.Refused, match="commit first"):
        deploy.owner_plan(deploy.Options(), repo, host)
    assert calls == [["git", "-C", str(repo), "status", "--porcelain"]]
    assert deploy._owner_main(["dry-run"], host, deploy.Options(dry_run=True)) == 2
    assert "commit first" in capsys.readouterr().err


def test_the_work_user_is_refused(world, monkeypatch, capsys):
    """Planted: the is_work_user stand-in says yes: exit 2, no call."""
    monkeypatch.setattr(config, "is_work_user", lambda: True)
    for argv in ([], ["--dry-run"], ["status"], ["plan", "--json"]):
        assert world.main(argv) == 2
    assert world.runner.calls == []
    assert "the work user cannot deploy" in capsys.readouterr().err


def test_the_entry_checks_refuse_a_wrong_caller(world, tmp_path, capsys):
    """Planted: with geteuid returning 0 so the battery reaches each check: no SUDO_USER, root, the work user, a
    folder not owned by the owner, a real path outside the owner's home, a module outside /opt/tcp-awb, a held
    lock. Each exit 2 with its own message and no call."""
    outside = tmp_path / "outside"
    (outside / ".git").mkdir(parents=True)
    linked = world.home / "linked"
    linked.symlink_to(outside)
    stranger = pwd.struct_passwd((USER, "x", os.getuid() + 1, os.getgid(), "", str(world.home), "/bin/sh"))
    cases = [
        (dict(environ={}), "SUDO_USER is empty"),
        (dict(environ={"SUDO_USER": "root"}), "not root"),
        (dict(environ={"SUDO_USER": WORK}), "the work user cannot deploy"),
        (dict(environ={"SUDO_USER": "someoneelse"}), "only the owner"),
        (dict(geteuid=lambda: 1000), "run it with sudo"),
        (dict(getpwnam=lambda n: stranger if n == USER else world.work), "not owned by the owner"),
        (dict(cwd=linked), "must lie in the owner's home"),
        (dict(cwd=world.home), "must lie in the owner's home"),
        (dict(module_file=REPO / "awb" / "deploy.py"), "root runs the installed command only"),
    ]
    for kw, message in cases:
        world.runner.calls.clear()
        assert world.main([], **kw) == 2, message
        err = capsys.readouterr().err
        assert message in err, (message, err)
        assert world.runner.calls == []
    (world.repo / ".git").rmdir()
    assert world.main([]) == 2 and "not a git work tree" in capsys.readouterr().err
    (world.repo / ".git").mkdir()
    fd = deploy.take_lock(world.run_dir)
    try:
        assert world.main([]) == 2
        assert "another deploy runs since" in capsys.readouterr().err
        assert world.runner.calls == []
    finally:
        os.close(fd)


def test_the_lock_dies_with_its_holder(tmp_path):
    """Planted: an existence check of the lock file in place of flock fails both halves: a killed holder leaves
    the file, and the second deploy must name the pid of the first."""
    code = ("import sys, time; sys.path.insert(0, %r); from pathlib import Path; from awb import deploy; "
            "fd = deploy.take_lock(Path(%r)); print('held', flush=True); time.sleep(60)" % (str(REPO), str(tmp_path)))
    child = subprocess.Popen([sys.executable, "-I", "-c", code], stdout=subprocess.PIPE, text=True)
    try:
        assert child.stdout.readline().strip() == "held"
        with pytest.raises(deploy.Refused, match=r"another deploy runs since \d{4}-.*\(pid %d\)" % child.pid):
            deploy.take_lock(tmp_path)
        child.send_signal(signal.SIGKILL)
        child.wait(timeout=10)
    finally:
        if child.poll() is None:
            child.kill()
            child.wait()
    assert (tmp_path / deploy.LOCK_NAME).exists()
    fd = deploy.take_lock(tmp_path)
    try:
        assert oct((tmp_path / deploy.LOCK_NAME).stat().st_mode & 0o777) == "0o600"
        assert os.get_inheritable(fd) is False
    finally:
        os.close(fd)


# --------------------------------------------------------------------------- the run and its journal


def test_a_full_run_writes_the_journal_extracts_and_restarts(world, capsys):
    """Planted: a run that restarts a unit the plan left unchanged, or leaves the journal running, fails."""
    world.runner.plan = world.plan(actions={"awb-ask.service": "restart"})
    assert world.main([]) == 0, capsys.readouterr()
    j = json.loads((world.root / "DEPLOYED").read_text())
    assert j["state"] == "done" and j["target"] == world.c1 and j["previous"] == world.c0
    assert j["units"]["awb-ask.service"]["result"] == "restarted" and j["pending"] == []
    assert j["units"]["awb-vaultd.service"]["result"] == "unchanged"
    assert oct((world.root / "DEPLOYED").stat().st_mode & 0o777) == "0o644"
    assert os.readlink(world.root / "src") == "releases/%s" % world.c1
    rel = world.root / "releases" / world.c1
    assert (rel / "seal" / "setup.sh").exists() and not list((world.root / "releases").glob(".*"))
    assert all(not (p.stat().st_mode & 0o022) for p in rel.rglob("*") if not p.is_symlink())
    acting = [c for c in world.runner.systemctl() if c[1] not in ("daemon-reload",)]
    assert acting == []                     # the web side is switched: install.sh restarts awb-ask
    install = world.runner.named("install.sh")
    assert [c[1:] for c, _ in install] == [["--only", "awb-ask.service"]]
    assert install[0][0][0] == str(rel / "seal" / "web" / "install.sh")
    setup = world.runner.named("setup.sh")
    assert [c for c, _ in setup] == [[str(rel / "seal" / "setup.sh"), "--update"]]
    log = (world.root / "deploy.log").read_text()
    assert "target=%s" % world.c1 in log and "result=ok" in log


def test_an_interrupted_run_leaves_its_units_pending(world, tmp_path, capsys):
    """Planted: the runner flips the symlink and raises KeyboardInterrupt at step 7; the next plan must list the
    units as pending from an interrupted run (a plan built from the symlink and the old journal prints unchanged);
    a unit whose ActiveState turns activating (auto-restart) 4 s after the restart must be failed."""
    repo = tmp_path / "plan-repo"
    shutil.copytree(synth_tree(tmp_path).root, repo)
    git(repo, "init", "-q")
    c0 = commit(repo)
    (repo / "tests").mkdir()
    (repo / "tests" / "x.txt").write_text("1\n")
    c1 = commit(repo)
    world.c0, world.c1 = c0, c1
    os.rename(world.root / "releases" / ("a" * 40), world.root / "releases" / c0)
    (world.root / "src").unlink()
    (world.root / "src").symlink_to("releases/%s" % c0)
    world.runner.plan = world.plan(actions={"awb-vaultd.service": "restart"})

    def interrupt(cmd, kw):
        if cmd[:2] == ["systemctl", "restart"]:
            raise KeyboardInterrupt

    world.runner.on = interrupt
    assert world.main([]) == 1
    assert "interrupted" in capsys.readouterr().err
    j = json.loads((world.root / "DEPLOYED").read_text())
    assert j["state"] == "running" and os.readlink(world.root / "src") == "releases/%s" % c1
    assert j["pending"] == ["awb-vaultd.service"]
    # the next plan, as the owner, from the journal
    host = plan_host(world, repo)
    plan = deploy.owner_plan(deploy.Options(), repo, host)
    vault = next(u for u in plan["units"] if u["unit"] == "awb-vaultd.service")
    assert plan["interrupted"] and plan["deployed"] == c0
    assert vault["action"] == "restart" and "pending from the last run" in vault["reason"]
    assert "awb-vaultd.service" in plan["pending_last"]
    # a naive plan from the symlink would diff c1..c1: nothing to restart
    assert git(repo, "diff", "--name-only", c1, c1) == ""
    # a unit that dies 4 s after its restart
    world.runner.on = None
    restarted_at = {}

    def show_state(t):
        start = restarted_at.get("t")
        return "activating" if start is not None and t - start >= 4 else "active"

    def mark(cmd, kw):
        if cmd[:2] == ["systemctl", "restart"]:
            restarted_at["t"] = world.clock()

    world.runner.on = mark
    world.runner.unit("awb-vaultd.service")["ActiveState"] = show_state
    world.runner.plan = world.plan(actions={"awb-vaultd.service": "restart"}, live=c1, deployed=c0)
    assert world.main([]) == 1
    j = json.loads((world.root / "DEPLOYED").read_text())
    assert j["units"]["awb-vaultd.service"]["result"] == "failed" and j["pending"] == ["awb-vaultd.service"]


def plan_host(world: World, repo: Path, daemons=None, show=None) -> deploy.Host:
    """A host for owner_plan: real git in `repo`, systemctl show from the fake runner's units, stand-in daemons."""
    def runner(cmd, **kw):
        if cmd[0] == "systemctl":
            return world.runner(cmd, **kw)
        return subprocess.run(cmd, **kw)

    for u in units_of_repo():
        info = world.runner.unit(u)
        argv = (show or {}).get(u) or {
            "awb-vaultd.service": "/usr/local/bin/awb vault serve", "awb-keyd.service": "/usr/local/bin/awb keys serve",
            "awb-ask.service": "/opt/tcp-awb/venv/bin/python3 -I -m awb.tcp.web.chat_service",
            "awb-portal.service": "/usr/local/bin/awb portal serve",
            "awb-web.service": "/usr/bin/python3 -I %s/src/awb/tcp/web/gateway.py" % world.root,
        }.get(u, "/opt/tcp-awb/venv/bin/python3 -I -m awb.tcp.web.projects_api")
        info["ExecStart"] = "{ path=x ; argv[]=%s ; ignore_errors=no ; }" % argv
    return deploy.Host(root=world.root, runner=runner, geteuid=os.geteuid, cwd=repo,
                       daemons=daemons or (lambda: {"vault": {"state": "unlocked"}, "keys": {"state": "unlocked"}}))


def test_a_unit_that_does_not_come_back_is_reported_and_pending(world, capsys):
    """Planted: is-active answers failed, or the MainPID keeps changing: exit 1, the unit named, the journal lists
    it pending; the next run restarts it without a new commit."""
    world.runner.plan = world.plan(actions={"awb-keyd.service": "restart"})
    world.runner.unit("awb-keyd.service")["ActiveState"] = "failed"
    world.runner.on = lambda cmd, kw: None
    original = FakeRunner.__call__

    def keep_failed(cmd, **kw):
        res = original(world.runner, cmd, **kw)
        world.runner.unit("awb-keyd.service")["ActiveState"] = "failed"
        return res

    world.runner.__class__ = type("Failing", (FakeRunner,), {"__call__": lambda self, cmd, **kw: keep_failed(cmd, **kw)})
    assert world.main([]) == 1
    out = capsys.readouterr().out
    assert "awb-keyd.service did not come back: journalctl -u awb-keyd.service" in out
    j = json.loads((world.root / "DEPLOYED").read_text())
    assert j["pending"] == ["awb-keyd.service"] and j["state"] == "done"
    assert "keys: down since" in out and "pending" in out
    # a MainPID that keeps changing never holds for 3 s
    world.runner.__class__ = FakeRunner
    world.runner.unit("awb-keyd.service")["ActiveState"] = "active"
    world.runner.unit("awb-keyd.service")["MainPID"] = lambda t: str(int(t * 10))
    assert world.main([]) == 1
    assert "awb-keyd.service did not come back" in capsys.readouterr().out
    # the next plan (as the owner) has it pending from the last run; the next run restarts it with no new commit
    world.runner.unit("awb-keyd.service")["MainPID"] = "77"
    world.runner.plan = world.plan(actions={"awb-keyd.service": "restart"}, pending_last=["awb-keyd.service"])
    world.runner.calls.clear()
    assert world.main([]) == 0
    assert ["systemctl", "restart", "awb-keyd.service"] in world.runner.systemctl()
    assert json.loads((world.root / "DEPLOYED").read_text())["pending"] == []


def test_a_reload_that_failed_but_switched_is_not_flipped_back(world, capsys):
    """Planted: systemctl reload exits 1 while the MainPID changed: recorded reloaded, no flip back; unchanged: the
    flip back, the previous units reinstalled, exit 1."""
    world.runner.plan = world.plan(actions={"awb-vaultd.service": "reload"})
    world.runner.rc[("reload", "awb-vaultd.service")] = 1

    def switch(cmd, kw):
        if cmd[:2] == ["systemctl", "reload"]:
            world.runner.unit("awb-vaultd.service")["MainPID"] = "999"

    world.runner.on = switch
    assert world.main([]) == 0, capsys.readouterr()
    j = json.loads((world.root / "DEPLOYED").read_text())
    assert j["units"]["awb-vaultd.service"]["result"] == "reloaded"
    assert os.readlink(world.root / "src") == "releases/%s" % world.c1
    assert not world.runner.named("--units-only")
    # the old daemon still serves: the flip back
    w2 = World(world.tmp / "second")
    w2.runner.plan = w2.plan(actions={"awb-vaultd.service": "reload", "awb-keyd.service": "reload"})
    w2.runner.rc[("reload", "awb-vaultd.service")] = 1
    assert w2.main([]) == 1
    out = capsys.readouterr().out
    assert "the reload of awb-vaultd.service failed and the old daemon still serves" in out
    back = w2.runner.named("--units-only")
    assert [c for c, _ in back] == [[str(w2.root / "releases" / w2.c0 / "seal" / "setup.sh"), "--update",
                                     "--units-only"]]
    assert os.readlink(w2.root / "src") == "releases/%s" % w2.c0
    j = json.loads((w2.root / "DEPLOYED").read_text())
    assert j["units"]["awb-vaultd.service"]["result"] == "failed" and j["units_from"] == w2.c0
    assert ["systemctl", "reload", "awb-keyd.service"] not in w2.runner.systemctl()
    assert ["systemctl", "restart", "awb-vaultd.service"] not in w2.runner.systemctl()


def test_a_partial_hand_over_is_named(world, capsys):
    """Planted: the vault reload answers ok with a release, the keys reload fails: exit 1, the journal carries
    both, the status line names both releases and the next command."""
    world.runner.plan = world.plan(actions={"awb-vaultd.service": "reload", "awb-keyd.service": "reload"})
    world.runner.rc[("reload", "awb-keyd.service")] = 1
    world.runner.status = {"vault": {"state": "unlocked", "codes": 3, "release": world.c1},
                           "keys": {"state": "unlocked", "aliases": ["t1"], "release": world.c0}}
    assert world.main([]) == 1
    out = capsys.readouterr().out
    j = json.loads((world.root / "DEPLOYED").read_text())
    assert j["units"]["awb-vaultd.service"]["result"] == "reloaded"
    assert j["units"]["awb-keyd.service"]["result"] == "failed"
    assert "vault: unlocked, 3 codes, release %s" % world.c1[:7] in out
    # the flip back is of the code and the units only; the vault stays on the new release and is named
    keys_line = next(x for x in out.splitlines() if x.startswith("keys: "))
    assert "release %s" % world.c0[:7] in keys_line
    vault_line = next(x for x in out.splitlines() if x.startswith("vault: "))
    assert "code and units: release %s; next: sudo awb deploy --only awb-vaultd.service" % world.c0[:7] in vault_line


def test_the_archive_is_extracted_into_a_partial_folder(world, tmp_path):
    """Planted: a tar that fails leaves no releases/<commit>; a leftover .partial is removed; an entry with a setuid
    bit or a special type in a crafted archive fails the check."""
    caller = deploy.Caller(world.owner, world.work, world.repo, {})
    host = deploy.Host(root=world.root, runner=world.runner, environ=world.environ, getgrouplist=lambda n, g: [g])
    plan = world.plan()
    run = deploy.Run(host, caller, plan, deploy.Options())
    rel = world.root / "releases" / world.c1
    partial = world.root / "releases" / (".%s.partial" % world.c1)
    world.runner.on = lambda cmd, kw: subprocess.CompletedProcess(cmd, 1, "", "") if cmd[0] == "tar" else None
    with pytest.raises(deploy.StepFailed, match="tar failed"):
        run.extract()
    assert not rel.exists() and not partial.exists()
    world.runner.on = None
    partial.mkdir()
    (partial / "half").write_text("x")
    run.extract()
    assert rel.is_dir() and not partial.exists() and not (rel / "half").exists()
    assert not list((world.root / "releases").glob(".*"))
    # crafted archives
    for kind in ("setuid", "fifo", "under-link", "abs", "escape-link"):
        path = tmp_path / ("%s.tar" % kind)
        with tarfile.open(path, "w") as tf:
            def add(name, **attrs):
                info = tarfile.TarInfo(name)
                for k, v in attrs.items():
                    setattr(info, k, v)
                tf.addfile(info, io.BytesIO(b"") if info.isfile() else None)
            if kind == "setuid":
                add("bin/x", mode=0o4755)
            elif kind == "fifo":
                add("p", type=tarfile.FIFOTYPE)
            elif kind == "under-link":
                add("d", type=tarfile.SYMTYPE, linkname="sub")
                add("d/f")
            elif kind == "abs":
                add("/etc/x")
            else:
                add("l", type=tarfile.SYMTYPE, linkname="../../etc")
        with pytest.raises(deploy.StepFailed):
            deploy.check_archive(path)
    good = tmp_path / "good.tar"
    with tarfile.open(good, "w") as tf:
        tf.add(REPO / "seal" / "README.md", arcname="seal/README.md")
    deploy.check_archive(good)


def test_a_release_a_daemon_reports_is_kept(world, capsys):
    """Planted: the retention with a stand-in daemon that reports an old release; without the rule that folder is
    removed and the test fails."""
    old, pre = "c" * 40, deploy.PRE_DEPLOY
    for name in (old, pre, "d" * 40):
        (world.root / "releases" / name).mkdir()
    world.runner.plan = world.plan()
    world.runner.status = {"vault": {"state": "unlocked", "codes": 3, "release": old},
                           "keys": {"state": "unlocked", "aliases": ["t1"]}}
    assert world.main([]) == 0, capsys.readouterr()
    left = {p.name for p in (world.root / "releases").iterdir()}
    assert left == {world.c0, world.c1, old}
    # without the daemon's report it goes
    (world.root / "releases" / pre).mkdir()
    removed = deploy.retain(world.root / "releases", {world.c1, world.c0})
    assert sorted(removed) == sorted([old, pre])


def test_rollback_deploys_the_previous_release(world, tmp_path, capsys):
    """Planted: --rollback with two releases diffs backwards and the install names the older release; a target
    without --takeover restarts the daemons (with it, they reload); with one release: exit 2."""
    repo = tmp_path / "rb"
    shutil.copytree(synth_tree(tmp_path).root, repo)
    git(repo, "init", "-q")
    c1 = commit(repo)
    (repo / "awb" / "vault.py").write_text("TAKEOVER = '--takeover'\n")
    c2 = commit(repo)
    for c in (c1, c2):
        shutil.copytree(REPO / "seal", world.root / "releases" / c / "seal")
    (world.root / "src").unlink()
    (world.root / "src").symlink_to("releases/%s" % c2)
    deploy.write_journal(world.root / "DEPLOYED", {"state": "done", "target": c2, "previous": c1, "units": {},
                                                    "pending": [], "running": {}})
    for u in ("awb-vaultd.service", "awb-keyd.service"):
        world.runner.unit(u)["CanReload"] = "yes"
    daemons = lambda: {"vault": {"state": "unlocked", "ops": ["reload"]},       # noqa: E731
                       "keys": {"state": "unlocked", "ops": ["reload"]}}
    host = plan_host(world, repo, daemons)
    plan = deploy.owner_plan(deploy.Options(rollback=True), repo, host)
    assert plan["target"] == c1 and plan["deployed"] == c2 and plan["changed"] == ["awb/vault.py"]
    vault = next(u for u in plan["units"] if u["unit"] == "awb-vaultd.service")
    assert vault["action"] == "restart" and "the target has no hand-over" in vault["reason"]
    # forward to the release with the hand-over: a reload
    deploy.write_journal(world.root / "DEPLOYED", {"state": "done", "target": c1, "previous": c2, "units": {},
                                                    "pending": [], "running": {}})
    (world.root / "src").unlink()
    (world.root / "src").symlink_to("releases/%s" % c1)
    plan = deploy.owner_plan(deploy.Options(to=c2), repo, host)
    vault = next(u for u in plan["units"] if u["unit"] == "awb-vaultd.service")
    assert vault["action"] == "reload"
    # the root run of the rollback plan installs from the older release
    world.c1 = c1
    world.runner.plan = world.plan(target=c1, deployed=c2, live=c2, rollback=True)
    assert world.main(["--rollback"]) == 0, capsys.readouterr()
    assert [c for c, _ in world.runner.named("setup.sh")] == [
        [str(world.root / "releases" / c1 / "seal" / "setup.sh"), "--update"]]
    assert world.runner.named("plan", "--rollback")
    # one release only: nothing to go back to
    deploy.write_journal(world.root / "DEPLOYED", {"state": "done", "target": c1, "previous": None, "units": {},
                                                    "pending": [], "running": {}})
    with pytest.raises(deploy.Refused, match="no earlier release"):
        deploy.owner_plan(deploy.Options(rollback=True), repo, host)


def test_projects_sync_runs_as_the_work_user_only_when_rules_changed(world, tmp_path, capsys):
    """Planted: rules/work.md in the diff gives one call as the work user; awb/vault.py alone gives none."""
    repo = tmp_path / "sync"
    shutil.copytree(synth_tree(tmp_path).root, repo)
    (repo / "rules").mkdir()
    (repo / "rules" / "work.md").write_text("one\n")
    git(repo, "init", "-q")
    c0 = commit(repo)
    (repo / "rules" / "work.md").write_text("two\n")
    c1 = commit(repo)
    (repo / "awb" / "vault.py").write_text("from awb import config\nX = 2\n")
    c2 = commit(repo)
    (world.root / "src").unlink()
    (world.root / "src").symlink_to("releases/%s" % c0)
    deploy.write_journal(world.root / "DEPLOYED", {"state": "done", "target": c0, "previous": None, "units": {},
                                                    "pending": [], "running": {}})
    host = plan_host(world, repo)
    plan = deploy.owner_plan(deploy.Options(to=c1), repo, host)
    assert plan["sync"] and plan["sync_reason"] == "rules/work.md"
    assert {u["unit"] for u in plan["units"] if u["action"] == "restart"} == {"awb-ask.service"}
    deploy.write_journal(world.root / "DEPLOYED", {"state": "done", "target": c1, "previous": c0, "units": {},
                                                    "pending": [], "running": {}})
    (world.root / "src").unlink()
    (world.root / "src").symlink_to("releases/%s" % c1)
    plan2 = deploy.owner_plan(deploy.Options(to=c2), repo, host)
    assert not plan2["sync"]
    assert {u["unit"] for u in plan2["units"] if u["action"] == "restart"} == {"awb-vaultd.service"}
    # the root run: one call as the work user, or none
    (world.root / "src").unlink()
    (world.root / "src").symlink_to("releases/%s" % world.c0)
    (world.root / "DEPLOYED").unlink()
    world.runner.plan = world.plan(actions={"awb-ask.service": "restart"}, sync=True, sync_reason="rules/work.md")
    assert world.main([]) == 0, capsys.readouterr()
    sync = [(c, k) for c, k in world.runner.calls if c[:3] == [BIN, "projects", "sync"]]
    assert len(sync) == 1
    assert sync[0][1]["env"]["HOME"] == str(world.work_home) and sync[0][1]["cwd"] == str(world.work_home)
    world.runner.calls.clear()
    world.runner.plan = world.plan(actions={"awb-vaultd.service": "restart"})
    assert world.main([]) == 0
    assert not [c for c, _ in world.runner.calls if c[:2] == [BIN, "projects"]]


# --------------------------------------------------------------------------- the pin


PKG_SUB = {"pkg/sub/__init__.py": "", "pkg/sub/mod.py": "X = 1\n"}


def make_release(base: Path, name: str, init: str) -> None:
    for rel, text in dict(PKG_SUB, **{"pkg/__init__.py": init,
                                      "pkg/sub/lazy.py": "WHERE = %r\n" % name}).items():
        (base / name / rel).parent.mkdir(parents=True, exist_ok=True)
        (base / name / rel).write_text(text)


def lazy_after_flip(tmp: Path, init: str, entry: str = "") -> str:
    """A second interpreter imports pkg.sub.mod through the src symlink, the test flips the symlink to the other
    release, the interpreter imports pkg.sub.lazy: which release did it come from?"""
    releases = tmp / "releases"
    make_release(releases, "r1", init)
    make_release(releases, "r2", init)
    src = tmp / "src"
    src.symlink_to("releases/r1")
    code = ("import sys; sys.path.insert(0, %r); import pkg.sub.mod; %s print('ready', flush=True); "
            "sys.stdin.readline(); import pkg.sub.lazy; print(pkg.sub.lazy.WHERE, flush=True)" % (str(src), entry))
    child = subprocess.Popen([sys.executable, "-I", "-B", "-c", code], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                             text=True)
    try:
        assert child.stdout.readline().strip() == "ready"
        nxt = tmp / "src.next"
        nxt.symlink_to("releases/r2")
        os.replace(nxt, src)
        child.stdin.write("go\n")
        child.stdin.flush()
        return child.stdout.readline().strip()
    finally:
        child.kill()
        child.wait()


def test_the_pin_keeps_the_running_tree(tmp_path):
    """Planted: the pin moved out of __init__.py into an entry call must fail (the lazy import comes from the new
    release); awb.tcp.__path__ after an import through a symlink must not start with the symlink path."""
    init = (REPO / "awb" / "__init__.py").read_text(encoding="utf-8")
    assert init.split('"""', 2)[2].lstrip().startswith("import os\n\n__path__[0] = os.path.realpath(__path__[0])")
    assert lazy_after_flip(tmp_path / "a", init) == "r1"
    late = "import os\ndef pin():\n    __path__[0] = os.path.realpath(__path__[0])\n"
    assert lazy_after_flip(tmp_path / "b", late, "pkg.pin();") == "r2"
    link = tmp_path / "link"
    link.symlink_to(REPO)
    res = subprocess.run([sys.executable, "-I", "-B", "-c",
                          "import sys; sys.path.insert(0, %r); import awb.tcp; print(awb.tcp.__path__[0])" % str(link)],
                         capture_output=True, text=True, check=True)
    assert not res.stdout.strip().startswith(str(link)) and res.stdout.strip() == str(REPO / "awb" / "tcp")


# --------------------------------------------------------------------------- the status


def test_the_status_lines_name_three_states_and_the_split():
    """Planted: a down daemon printed as locked, or a split between a daemon and the code left unnamed."""
    j = {"target": "b" * 40, "units_from": "a" * 40, "pending": [], "running": {},
         "units": {"awb-vaultd.service": {"kind": "vault"}, "awb-keyd.service": {"kind": "keys"}}}
    status = {"vault": {"state": "locked", "since": "2026-10-07T10:00:00Z"}, "keys": {"state": "down"}}
    show = {"awb-keyd.service": {"ActiveState": "failed", "InactiveEnterTimestamp": "Wed 2026-10-07 10:01:00 UTC"}}
    lines = deploy.status_lines(j, status, show)
    assert lines[0] == "vault: locked since 2026-10-07T10:00:00Z"
    assert lines[1] == "keys: down since Wed 2026-10-07 10:01:00 UTC"
    assert "units from aaaaaaa, code from bbbbbbb" in lines
    status = {"vault": {"state": "unlocked", "codes": 2, "release": "a" * 40}, "keys": {"state": "unlocked",
                                                                                        "aliases": ["t1", "t2"]}}
    lines = deploy.status_lines(j, status, {})
    assert lines[0] == ("vault: unlocked, 2 codes, release aaaaaaa; code and units: release bbbbbbb; next: sudo awb "
                        "deploy --only awb-vaultd.service")
    assert lines[1] == "keys: t1, t2"
