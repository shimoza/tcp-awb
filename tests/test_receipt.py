"""The receipt of a sealed start: `awb hook session-start` proves every guard active before a session gets its files.

The guards in order: the hooks of the managed settings run the installed command, the project's repository carries
the commit gate, the vault daemon answers a check, the key service answers a ping, the rules of the project are the
installed ones and the host mode is known. All present: the context opens with "guards: N of N active (<release>)".
One missing: the whole context is one line naming it (never a path) and every prompt is refused with that line
until it is fixed. An owner session gets the receipt without the project repository.
"""
from __future__ import annotations

import io
import json
import os
import pwd
import re
import shutil
import sys
from contextlib import ExitStack
from pathlib import Path

import pytest

from awb import config, gate, hooks, projects, rulesync
from awb.tcp import keys
from tests.test_hooks import SINCE, locked_answers, stand_in

CODE = "tcp-q7m4"
ME = pwd.getpwuid(os.geteuid()).pw_name
UNLOCKED = {"ping": {"ok": True, "state": "unlocked"}, "check": {"ok": True, "hits": []}}
KEYS = {"ping": {"ok": True, "state": "unlocked"}}
RELEASE = re.escape(hooks._release())


def run(name: str, payload: dict, monkeypatch, capsys) -> tuple[int, str, str]:
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(payload)))
    code = hooks.main([name])
    cap = capsys.readouterr()
    return code, cap.out, cap.err


def context(out: str) -> str:
    return json.loads(out)["hookSpecificOutput"]["additionalContext"]


class Host:
    """A sealed host in a temporary folder: the host file names this user as the work user, the managed settings
    run an installed command, a project with the commit gate and the installed rules, and stand-ins for the vault
    daemon and the key service that a test starts or leaves out."""

    def __init__(self, home: config.Paths, tmp: Path, monkeypatch, stack: ExitStack):
        self.p, self.tmp, self.mp, self.stack = home, tmp, monkeypatch, stack
        self.host_file = tmp / "host" / "paths.conf"
        self.host_file.parent.mkdir()
        self.check_socket = tmp / "run" / "none.sock"
        self.keys_socket = tmp / "run" / "keys-none.sock"
        self.mode_line = ""
        self.work_user = ME
        installed = tmp / "bin" / "awb"
        installed.parent.mkdir()
        installed.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        installed.chmod(0o755)
        self.installed = installed
        source = json.loads(Path(hooks.MANAGED_SOURCE).read_text(encoding="utf-8"))
        text = json.dumps(source).replace("/usr/local/bin/awb", str(installed))
        self.source = tmp / "release-managed.json"
        self.source.write_text(text, encoding="utf-8")
        self.managed = tmp / "managed" / "awb-workbench.json"
        self.managed.parent.mkdir()
        self.managed.write_text(text, encoding="utf-8")
        user = tmp / "userhome"
        (user / ".claude").mkdir(parents=True)
        shutil.copyfile(projects.RULES_FILE, user / ".claude" / "CLAUDE.md")
        monkeypatch.setenv("HOME", str(user))
        monkeypatch.delenv("CLAUDE_PROJECT_DIR", raising=False)
        self.root = home.projects_root / CODE
        self.root.mkdir(parents=True)
        (self.root / "SCOPE.md").write_text("# Scope of %s\n" % CODE, encoding="utf-8")
        (self.root / "STATE.md").write_text("Status: working\nNext: size\n", encoding="utf-8")
        (self.root / "CLAUDE.md").write_text("# Project %s\n\n- Codes only.\n" % CODE, encoding="utf-8")
        git = self.root / ".git"
        (git / "hooks").mkdir(parents=True)
        (git / "config").write_text("[core]\n\tbare = false\n", encoding="utf-8")
        for name in gate.HOOK_NAMES:
            hook = git / "hooks" / name
            hook.write_text("#!/bin/sh\n%s\nexit 0\n" % gate.HOOK_MARK, encoding="utf-8")
            hook.chmod(0o755)
        monkeypatch.setattr(hooks, "MANAGED_FILE", self.managed)
        monkeypatch.setattr(hooks, "MANAGED_SOURCE", self.source)
        monkeypatch.setattr(hooks, "INSTALLED_AWB", installed)
        monkeypatch.setattr(hooks, "RECEIPT_TIMEOUT", 2.0)
        monkeypatch.setattr(config, "HOST_CONF", str(self.host_file))
        monkeypatch.setattr(hooks, "HOST_FILE", self.host_file)
        self.write()

    def write(self) -> None:
        self.host_file.write_text(
            "work_user = %s\nshared = %s\nvault = %s\nprojects = %s\nkb = %s\ncheck_socket = %s\n%s"
            % (self.work_user, self.p.shared, self.p.vault, self.p.projects_root, self.p.kb, self.check_socket,
               self.mode_line), encoding="utf-8")
        self.mp.setattr(hooks, "KEYS_SOCKET", self.keys_socket)
        # an owner process reads the environment first, the work user never
        self.mp.setenv("AWB_CHECK_SOCKET", str(self.check_socket))
        self.mp.setenv(keys.CALL_SOCKET_ENV, str(self.keys_socket))

    def vault(self, answers=UNLOCKED) -> None:
        sock, _ = self.stack.enter_context(stand_in(answers))
        self.check_socket = sock
        self.write()

    def keys(self) -> None:
        sock, _ = self.stack.enter_context(stand_in(KEYS))
        self.keys_socket = sock
        self.write()

    def start(self, capsys, sid: str = "s-1") -> tuple[int, str]:
        code, out, err = run("session-start", {"cwd": str(self.root), "session_id": sid}, self.mp, capsys)
        return code, context(out) if out.strip() else ""

    def prompt(self, capsys, sid: str = "s-1") -> tuple[int, str]:
        code, _, err = run("prompt", {"prompt": "Size the cluster of the test.", "cwd": str(self.root),
                                      "session_id": sid}, self.mp, capsys)
        return code, err.strip()

    def log(self) -> list[list[str]]:
        f = self.p.shared / "sessions" / hooks.RECEIPTS_LOG
        return [line.split("\t") for line in f.read_text(encoding="utf-8").splitlines()] if f.exists() else []


@pytest.fixture
def host(home, tmp_path, monkeypatch):
    with ExitStack() as stack:
        yield Host(home, tmp_path, monkeypatch, stack)


def assert_no_path(text: str, host: Host) -> None:
    for path in (host.tmp, host.root, host.managed, host.installed, host.p.shared):
        assert str(path) not in text


def test_every_guard_present_gives_the_receipt_line_first(host, capsys):
    host.vault()
    host.keys()
    code, ctx = host.start(capsys)
    assert code == hooks.OK
    first = ctx.splitlines()[0]
    assert re.fullmatch(r"guards: 6 of 6 active \(%s\)" % RELEASE, first), first
    assert "Workbench project %s" % CODE in ctx and "# Scope of %s" % CODE in ctx
    assert host.log()[-1][1:] == ["s-1", CODE, "ok"]
    assert host.prompt(capsys) == (hooks.OK, "")


BREAKS = {
    "hooks": lambda h: h.managed.write_text(h.managed.read_text().replace(str(h.installed), "/tmp/other"),
                                            encoding="utf-8"),
    "gate": lambda h: (h.root / ".git" / "hooks" / "pre-push").unlink(),
    "vault": lambda h: None,                       # planted: no daemon behind the check socket
    "keys": lambda h: None,                        # no key service behind its socket
    "rules": lambda h: (h.root / "CLAUDE.md").write_text("# Project\n\n%s\n" % rulesync.CLAUDE_FIXES[0][0],
                                                         encoding="utf-8"),
    "mode": lambda h: setattr(h, "mode_line", "mode = private\n"),
}


@pytest.mark.parametrize("guard", [k for k, _, _ in hooks.GUARDS])
def test_each_missing_guard_refuses_the_start_with_its_name(host, capsys, guard):
    """Planted failure: a start that loads the files, or a refusal that names no guard or a path."""
    if guard != "vault":
        host.vault()
    if guard != "keys":
        host.keys()
    BREAKS[guard](host)
    host.write()
    name = dict((k, n) for k, n, _ in hooks.GUARDS)[guard]
    code, ctx = host.start(capsys)
    assert code == hooks.OK
    assert len(ctx.splitlines()) == 1 and ctx.startswith("Start refused, guard missing: %s (" % name), ctx
    assert "# Scope of" not in ctx and "guards:" not in ctx
    assert_no_path(ctx, host)
    assert host.log()[-1][1:] == ["s-1", CODE, guard]
    code, err = host.prompt(capsys)
    assert (code, err) == (hooks.BLOCK, ctx)


def test_the_daemon_down_refuses_and_the_prompt_passes_once_it_answers(host, capsys):
    host.keys()
    code, ctx = host.start(capsys)
    assert ctx.startswith("Start refused, guard missing: the vault daemon (")
    for _ in range(2):
        assert host.prompt(capsys) == (hooks.BLOCK, ctx)
    host.vault()
    assert host.prompt(capsys) == (hooks.OK, "")
    assert (host.p.shared / "sessions" / "receipt-s-1.ok").is_file()


def test_a_stale_rules_digest_refuses(host, capsys):
    host.vault()
    host.keys()
    (Path(os.environ["HOME"]) / ".claude" / "CLAUDE.md").write_text("# older rules\n", encoding="utf-8")
    code, ctx = host.start(capsys)
    assert ctx.startswith("Start refused, guard missing: the rules digest ("), ctx
    assert "awb projects sync" in ctx


def test_two_missing_guards_are_both_named(host, capsys):
    host.vault()
    BREAKS["gate"](host)
    code, ctx = host.start(capsys)
    assert ctx.startswith("Start refused, guards missing: the commit gate of the project, the key service (")


def test_a_locked_vault_gives_the_locked_start(host, capsys):
    host.vault(locked_answers(SINCE))
    host.keys()
    code, ctx = host.start(capsys)
    assert ctx == hooks.LOCKED_START % SINCE
    assert host.log()[-1][3] == "vault"
    assert host.prompt(capsys) == (hooks.BLOCK, ctx)


def test_the_owner_gets_the_receipt_without_the_project_repository(host, capsys):
    host.work_user = "awb-someone-else"
    host.vault()
    host.keys()
    BREAKS["gate"](host)
    code, ctx = host.start(capsys)
    assert code == hooks.OK
    assert re.fullmatch(r"guards: 5 of 5 active \(%s\)" % RELEASE, ctx), ctx
    host.keys_socket = host.tmp / "run" / "gone.sock"
    host.write()
    code, ctx = host.start(capsys, sid="s-2")
    assert code == hooks.OK and ctx.startswith("Start refused, guard missing: the key service (")
    assert host.log()[-1][1:] == ["s-2", CODE, "keys"]


def test_the_key_socket_is_the_one_of_the_key_service():
    assert (hooks.KEYS_SOCKET, hooks.KEYS_SOCKET_ENV) == (keys.DEFAULT_CALL_SOCKET, keys.CALL_SOCKET_ENV)


def test_an_unsealed_host_takes_no_receipt(home, tmp_path, monkeypatch, capsys):
    root = home.projects_root / CODE
    root.mkdir(parents=True)
    (root / "SCOPE.md").write_text("# Scope\n", encoding="utf-8")
    code, out, _ = run("session-start", {"cwd": str(root), "session_id": "s-1"}, monkeypatch, capsys)
    assert "guards:" not in context(out) and "Start refused" not in context(out)


def test_board_and_health_carry_the_refused_starts_of_today(host, capsys):
    from awb.tcp import portal

    host.vault()
    host.start(capsys, sid="s-1")                       # keys missing
    host.start(capsys, sid="s-1")                       # the same session again counts once
    host.start(capsys, sid="s-2")
    host.keys()
    host.start(capsys, sid="s-3")                       # all present
    log = host.p.shared / "sessions" / hooks.RECEIPTS_LOG
    with open(log, "a", encoding="utf-8") as fh:
        fh.write("2020-01-01T00:00:00Z\ts-9\t%s\tkeys\n" % CODE)
    assert hooks.refused_today(host.p) == 2
    assert portal.health(host.p) == "sessions refused at start today: 2\nok\n"
    import copy
    from unittest.mock import patch

    from awb import cli
    from awb.tcp.web import contract, projects_api

    capsys.readouterr()
    with patch.object(projects_api, "board", return_value=copy.deepcopy(contract.P_BOARD)):
        assert cli.main(["board", "show"]) == 0
    assert capsys.readouterr().out.rstrip().endswith("sessions refused at start today: 2")
    log.write_text("", encoding="utf-8")
    assert portal.health(host.p) == "ok\n"
