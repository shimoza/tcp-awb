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
    import copy
    from unittest.mock import patch

    from awb import cli
    from awb.tcp.web import contract, projects_api

    capsys.readouterr()
    with patch.object(projects_api, "board", return_value=copy.deepcopy(contract.P_BOARD)):
        assert cli.main(["board", "show"]) == 0
    assert capsys.readouterr().out.rstrip().endswith("sessions refused at start today: 2")


# Receipt review: every filesystem fixture belongs to this test's temporary host.
from awb import receipt as receipts


@pytest.fixture
def active_receipt(monkeypatch):
    monkeypatch.setattr(receipts, "_guard_hooks", lambda: True)
    monkeypatch.setattr(receipts, "_guard_gate", lambda root: True)
    monkeypatch.setattr(receipts, "_guard_keys", lambda: True)
    monkeypatch.setattr(receipts, "_guard_rules", lambda root, owner: True)
    monkeypatch.setattr(config, "mode", lambda: config.KNOWN_MODES[0])


def test_vault_malformed_state_fails_closed(home, monkeypatch, active_receipt):
    from awb import vault

    for answer in (None, [], {}, {"state": "unexpected"}, {"state": []}):
        monkeypatch.setattr(vault, "ping_state", lambda *a, **kw: answer)
        _, missing, line = receipts.receipt(home, None)
        assert missing == ["vault"]
        assert line.startswith("Start refused, guard missing: the vault daemon")


def test_locked_time_is_typed_ascii_and_one_line(home, monkeypatch, active_receipt):
    from awb import vault

    for since in ([], {"x": 1}, 12, SINCE + "\n", SINCE.replace("2", "٢"), "pass" + "word"):
        monkeypatch.setattr(vault, "ping_state", lambda *a, **kw: {"state": "locked", "since": since})
        assert receipts.receipt(home, None)[2] == receipts.LOCKED_START % "unknown"


def test_managed_settings_cannot_disable_all_hooks(host):
    data = json.loads(host.managed.read_text())
    data["disableAllHooks"] = True
    host.managed.write_text(json.dumps(data))
    assert not receipts._guard_hooks()


def test_managed_settings_reject_linked_or_special_files(host):
    original = host.managed.read_bytes()
    outside = host.tmp / "outside-settings"
    outside.write_bytes(original)
    for kind in ("symlink", "hardlink", "fifo", "missing", "invalid", "oversized"):
        host.managed.unlink(missing_ok=True)
        if kind == "symlink":
            host.managed.symlink_to(outside)
        elif kind == "hardlink":
            os.link(outside, host.managed)
        elif kind == "fifo":
            os.mkfifo(host.managed)
        elif kind == "invalid":
            host.managed.write_bytes(b"\xff")
        elif kind == "oversized":
            host.managed.write_bytes(original + b" " * (1024 * 1024 + 1))
        assert not receipts._guard_hooks(), kind
    assert outside.read_bytes() == original


def test_hook_entries_reject_malformed_shapes():
    command = {"type": "command", "command": "/usr/local/bin/awb hook prompt"}
    for entries in ("wrong", [None], [{"matcher": 5, "hooks": [command]}],
                    [{"hooks": [None]}], [{"hooks": [{"type": "command", "command": []}]}]):
        assert receipts._hook_entries({"hooks": {"SessionStart": entries}}) is None


def test_gate_rejects_config_includes_and_boolean_hookspath(host):
    conf = host.root / ".git" / "config"
    for text in ('[include]\npath = /outside/config\n', '[includeIf "gitdir:*"]\npath = /outside/config\n',
                 '[core]\nhooksPath\n', '[core]\nhooksPath # comment\n', '[core] hooksPath = /outside/hooks\n'):
        conf.write_text(text)
        assert not receipts._guard_gate(host.root), text


def test_gate_checks_worktree_config(host):
    (host.root / ".git" / "config").write_text('[extensions]\nworktreeConfig = true\n')
    (host.root / ".git" / "config.worktree").write_text('[core]\nhooksPath = /outside/hooks\n')
    assert not receipts._guard_gate(host.root)


def test_gate_rejects_linked_project_root(host):
    link = host.tmp / "alias"
    link.symlink_to(host.root, target_is_directory=True)
    assert not receipts._guard_gate(link)


@pytest.fixture
def side_tree(host):
    import subprocess

    # Git creates the registration, independently of awb and of the checkout under test.
    shutil.rmtree(host.root / ".git")
    def git(*args):
        subprocess.run(["git", "-C", str(host.root), *args], check=True, capture_output=True,
                       env={**os.environ, "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull})
    git("init", "-q")
    git("-c", "user.name=Test", "-c", "user.email=test@example.invalid", "commit", "--allow-empty", "-qm", "init")
    root = host.root / ".side" / "audit"
    git("worktree", "add", "-qb", "audit", str(root))
    admin = Path((root / ".git").read_text().strip().removeprefix("gitdir: "))
    assert receipts._side_git(root, root / ".git") == host.root / ".git"
    return host.root, root, admin


def test_side_git_rejects_link_aliases_in_registration(side_tree):
    project, root, admin = side_tree
    alias = project / "alias"
    alias.symlink_to(admin, target_is_directory=True)
    (root / ".git").write_text("gitdir: " + str(alias) + "\n")
    assert receipts._side_git(root, root / ".git") is None


def test_side_git_rejects_link_aliases_in_backlink(side_tree):
    project, root, admin = side_tree
    alias = project / "alias"
    alias.symlink_to(root, target_is_directory=True)
    (admin / "gitdir").write_text(str(alias / ".git") + "\n")
    assert receipts._side_git(root, root / ".git") is None


def test_side_git_rejects_link_aliases_in_common_directory(side_tree):
    project, root, admin = side_tree
    alias = project / "alias"
    alias.symlink_to(project / ".git", target_is_directory=True)
    (admin / "commondir").write_text(str(alias) + "\n")
    assert receipts._side_git(root, root / ".git") is None


def test_side_git_rejects_outside_missing_and_unreadable_metadata(side_tree, tmp_path, monkeypatch):
    project, root, admin = side_tree
    gitfile = root / ".git"
    original = gitfile.read_text()
    gitfile.write_text("gitdir: " + str(tmp_path) + "\n")
    assert receipts._side_git(root, gitfile) is None
    gitfile.write_text(original)
    (admin / "commondir").unlink()
    assert receipts._side_git(root, gitfile) is None
    (admin / "commondir").write_text("../..\n")
    from awb import side
    def denied(*args, **kwargs):
        raise PermissionError("pass" + "word")
    monkeypatch.setattr(side, "_read", denied)
    assert receipts._side_git(root, gitfile) is None


@pytest.fixture
def key_reply(monkeypatch):
    import socket

    def reply(raw, error=None):
        class ReplySocket:
            def __enter__(self):
                self.raw = raw
                return self
            def __exit__(self, *args):
                pass
            def settimeout(self, value):
                assert value > 0
            def connect(self, path):
                if error is not None:
                    raise error
            def sendall(self, data):
                assert json.loads(data) == {"op": "ping"}
            def recv(self, size):
                part, self.raw = self.raw[:size], self.raw[size:]
                return part
        monkeypatch.setattr(socket, "socket", lambda *args: ReplySocket())
    return reply


def test_keys_call_rejects_unframed_oversized_and_deep_answers(key_reply):
    key_reply(b'{"ok":true}\n')
    assert receipts.keys_call({"op": "ping"}, Path("unused")) == {"ok": True}
    for raw in (b'{"ok":true}', b'{"ok":true,"pad":"' + b'x' * 65536 + b'"}\n',
                b'[' * 2000 + b']' * 2000 + b'\n', b'{"ok":false,"ok":true}\n'):
        key_reply(raw)
        with pytest.raises(ValueError):
            receipts.keys_call({"op": "ping"}, Path("unused"))


def test_keys_call_errors_do_not_echo_socket_or_parser_values(tmp_path, key_reply):
    secret = "pass" + "word"
    key_reply(b"", OSError(secret))
    with pytest.raises(OSError) as err:
        receipts.keys_call({"op": "ping"}, tmp_path / secret)
    assert secret not in str(err.value)
    key_reply(b'{"ok": "' + secret.encode() + b'\xff"}\n')
    with pytest.raises(ValueError) as err:
        receipts.keys_call({"op": "ping"}, Path("unused"))
    assert secret not in str(err.value)
    assert not isinstance(err.value, (UnicodeDecodeError, json.JSONDecodeError))


def test_keys_call_uses_a_total_deadline(monkeypatch):
    import socket
    import time

    class SlowSocket:
        def __enter__(self):
            self.sent = 0
            return self
        def __exit__(self, *args):
            pass
        def settimeout(self, value):
            self.timeout = value
        def connect(self, path):
            pass
        def sendall(self, data):
            pass
        def recv(self, size):
            if self.timeout < .02:
                raise TimeoutError()
            time.sleep(.02)
            self.sent += 1
            return b' ' if self.sent < 8 else b'{"ok":true}\n'
    monkeypatch.setattr(socket, "socket", lambda *args: SlowSocket())
    with pytest.raises(OSError):
        receipts.keys_call({"op": "ping"}, Path("unused"), timeout=.06)


def test_session_identifiers_do_not_alias_after_cleaning_or_truncation(home):
    ids = ["ab", "a/b", "a.b", "a" * 80, "a" * 80 + "b"]
    marks = [receipts._receipt_mark(home, sid) for sid in ids]
    assert len(set(marks)) == len(ids)


def test_receipt_log_and_mark_withhold_names_secrets_and_unknown_guards(home):
    from tests import fixtures

    secret = "pass" + "word" + "=hidden"
    name = fixtures.PERSON_FORMS[1]
    for sid in (secret, name):
        receipts._log_receipt(home, sid, Path(name), [secret])
        mark = receipts._receipt_mark(home, sid)
        assert mark is not None
        assert "hidden" not in str(mark)
        # DMZ 4 (B-088): an ordinary session id is kept as written, one with other characters is hashed
        assert (name in str(mark)) == (sid == name)
    text = (home.shared / "sessions" / receipts.RECEIPTS_LOG).read_text()
    assert secret not in text and "hidden" not in text


def test_receipt_log_does_not_follow_links_or_special_files(home, tmp_path):
    folder = home.shared / "sessions"
    folder.mkdir(exist_ok=True)
    log = folder / receipts.RECEIPTS_LOG
    outside = tmp_path / "outside"
    outside.write_text("untouched\n")
    for kind in ("symlink", "hardlink", "fifo"):
        log.unlink(missing_ok=True)
        if kind == "symlink":
            log.symlink_to(outside)
        elif kind == "hardlink":
            os.link(outside, log)
        else:
            os.mkfifo(log)
        receipts._log_receipt(home, "s-1", None, [])
        assert outside.read_text() == "untouched\n"
        assert receipts.refused_today(home) is None


def test_receipt_sessions_directory_cannot_redirect_writes(home, tmp_path, monkeypatch):
    outside = tmp_path / "outside"
    outside.mkdir()
    folder = home.shared / "sessions"
    folder.symlink_to(outside, target_is_directory=True)
    monkeypatch.setattr(receipts, "receipt", lambda *args: ([], [], "guards: active"))
    receipts._start_receipt(home, {"session_id": "s-1"}, None, False)
    assert list(outside.iterdir()) == []
    assert receipts._receipt_mark(home, "s-1") is None
    assert receipts.refused_today(home) is None


def test_receipt_mark_does_not_truncate_link_targets(home, tmp_path, monkeypatch):
    monkeypatch.setattr(receipts, "receipt", lambda *args: ([], [], "guards: active"))
    folder = home.shared / "sessions"
    folder.mkdir(exist_ok=True)
    outside = tmp_path / "outside"
    outside.write_text("untouched\n")
    mark = folder / "receipt-s-1.ok"
    for kind in ("symlink", "hardlink"):
        mark.unlink(missing_ok=True)
        if kind == "symlink":
            mark.symlink_to(outside)
        else:
            os.link(outside, mark)
        receipts._start_receipt(home, {"session_id": "s-1"}, None, False)
        assert outside.read_text() == "untouched\n"


def test_concurrent_starts_cannot_restore_a_stale_success_mark(home, monkeypatch):
    import threading
    from concurrent.futures import ThreadPoolExecutor

    first_check = threading.Event()
    second_started = threading.Event()
    release_first = threading.Event()
    calls = []
    def check(*args):
        calls.append(1)
        if len(calls) == 1:
            first_check.set()
            assert release_first.wait(5)
            return [], [], "guards: active"
        return [], ["gate"], "Start refused"
    monkeypatch.setattr(receipts, "receipt", check)
    def second():
        second_started.set()
        return receipts._start_receipt(home, {"session_id": "s-1"}, None, False)
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(receipts._start_receipt, home, {"session_id": "s-1"}, None, False)
        assert first_check.wait(5)
        last = pool.submit(second)
        assert second_started.wait(5)
        # The old implementation lets the refusal finish while the first check is pending.
        import time
        time.sleep(.1)
        release_first.set()
        assert first.result(timeout=5)[0]
        assert not last.result(timeout=5)[0]
    assert not (home.shared / "sessions" / "receipt-s-1.ok").exists()


def test_refused_today_ignores_malformed_rows_and_matches_the_whole_day(home):
    folder = home.shared / "sessions"
    folder.mkdir(exist_ok=True)
    log = folder / receipts.RECEIPTS_LOG
    log.write_text("2026-11-15T01:02:03Z\ts-1\t-\tkeys\n"
                   "2026-11-15garbage\ts-2\t-\tkeys\n"
                   "2026-11-15T01:02:03Z\ts-3\t-\tunknown\n"
                   "2026-11-15T01:02:03Z\ts-4\tprivate name\tkeys\n"
                   "2026-11-15T01:02:03Z\t\t-\tkeys\n"
                   "2026-11-15T01:02:03Z\ts-5\t-\tkeys")
    assert receipts.refused_today(home, "2026-11-15") == 1
    assert receipts.refused_today(home, "2026-11") is None


def test_refused_today_missing_and_unreadable_log(home, monkeypatch):
    assert receipts.refused_today(home) == 0
    def denied(*args, **kwargs):
        raise PermissionError("pass" + "word")
    monkeypatch.setattr(os, "open", denied)
    monkeypatch.setattr(Path, "read_text", denied)
    assert receipts.refused_today(home) is None
    assert receipts.refused_line(home).endswith("unknown")


def test_managed_command_hooks_cannot_run_asynchronously(host):
    data = json.loads(host.managed.read_text())
    data["hooks"]["UserPromptSubmit"][0]["hooks"][0]["async"] = True
    host.managed.write_text(json.dumps(data))
    assert not receipts._guard_hooks()


def test_receipt_log_directory_swap_does_not_escape_opened_directory(home, tmp_path, monkeypatch):
    folder = home.shared / "sessions"
    folder.mkdir(exist_ok=True)
    moved = home.shared / "old-sessions"
    outside = tmp_path / "outside"
    outside.mkdir()
    target = outside / receipts.RECEIPTS_LOG
    target.write_text("untouched\n")
    real_open = os.open
    real_builtin_open = open
    swapped = False
    def swap():
        nonlocal swapped
        if not swapped:
            folder.rename(moved)
            folder.symlink_to(outside, target_is_directory=True)
            swapped = True
    def opened(path, flags, *args, **kwargs):
        if str(path).endswith(receipts.RECEIPTS_LOG):
            swap()
        return real_open(path, flags, *args, **kwargs)
    def builtin_opened(path, *args, **kwargs):
        if str(path).endswith(receipts.RECEIPTS_LOG):
            swap()
        return real_builtin_open(path, *args, **kwargs)
    monkeypatch.setattr(os, "open", opened)
    monkeypatch.setattr("builtins.open", builtin_opened)
    receipts._log_receipt(home, "s-1", None, [])
    assert swapped
    assert target.read_text() == "untouched\n"
    assert (moved / receipts.RECEIPTS_LOG).read_text().endswith("\ts-1\t-\tok\n")


def test_receipt_mark_is_published_only_after_complete_write(home, monkeypatch):
    monkeypatch.setattr(receipts, "receipt", lambda *args: ([], [], "guards: active"))
    folder = home.shared / "sessions"
    folder.mkdir(exist_ok=True)
    mark = folder / "receipt-s-1.ok"
    mark.write_text("previous complete mark\n")
    real_fdopen = os.fdopen
    real_write_text = Path.write_text
    observed = []
    class InterruptedWriter:
        def __init__(self, stream):
            self.stream = stream
        def __enter__(self):
            return self
        def __exit__(self, *args):
            self.stream.close()
        def write(self, text):
            self.stream.write(text[:3])
            self.stream.flush()
            observed.append(mark.read_text())
            raise OSError("pass" + "word")
    def fdopened(fd, mode="r", *args, **kwargs):
        stream = real_fdopen(fd, mode, *args, **kwargs)
        return InterruptedWriter(stream) if mode == "w" else stream
    def path_write(path, text, *args, **kwargs):
        if path == mark:
            real_write_text(path, text[:3])
            observed.append(mark.read_text())
            raise OSError("pass" + "word")
        return real_write_text(path, text, *args, **kwargs)
    monkeypatch.setattr(os, "fdopen", fdopened)
    monkeypatch.setattr(Path, "write_text", path_write)
    receipts._start_receipt(home, {"session_id": "s-1"}, None, False)
    assert observed == ["previous complete mark\n"]
    assert mark.read_text() == "previous complete mark\n"
    assert sorted(path.name for path in folder.iterdir()) == [".receipt.lock", "receipt-s-1.ok", "receipts.tsv"]


def test_side_git_rejects_parent_traversal_through_a_regular_file(side_tree):
    project, root, admin = side_tree
    leaf = project / "leaf"
    leaf.write_text("not a directory\n")
    (admin / "commondir").write_text(str(leaf / ".." / ".git") + "\n")
    assert receipts._side_git(root, root / ".git") is None


def test_release_and_guard_exceptions_never_echo_private_values(home, monkeypatch, active_receipt):
    from awb import vault
    from tests import fixtures

    secret = "pass" + "word"
    for value in (secret, fixtures.PERSON_FORMS[0], "a" * 40 + "\n"):
        monkeypatch.setattr(vault, "release_path", lambda: "/outside/" + value)
        assert receipts._release() == "unknown release"
    def failed():
        raise ValueError(secret)
    monkeypatch.setattr(receipts, "_guard_keys", failed)
    monkeypatch.setattr(receipts, "_guard_vault", lambda p: "ok")
    _, missing, line = receipts.receipt(home, None)
    assert missing == ["keys"]
    assert secret not in line and "/outside" not in line


def test_side_gate_checks_its_own_worktree_configuration(side_tree):
    project, root, admin = side_tree
    for name in gate.HOOK_NAMES:
        hook = project / ".git" / "hooks" / name
        hook.write_text("#!/bin/sh\n" + gate.HOOK_MARK + "\n")
        hook.chmod(0o755)
    assert receipts._guard_gate(root)
    with (project / ".git" / "config").open("a") as stream:
        stream.write("[extensions]\nworktreeConfig = true\n")
    (admin / "config.worktree").write_text("[core]\nhooksPath = /outside/hooks\n")
    assert not receipts._guard_gate(root)


def test_receipt_lock_rejects_links_and_special_files(home, tmp_path, monkeypatch):
    monkeypatch.setattr(receipts, "receipt", lambda *args: ([], [], "guards: active"))
    folder = home.shared / "sessions"
    folder.mkdir(exist_ok=True)
    lock = folder / ".receipt.lock"
    outside = tmp_path / "outside"
    outside.write_text("untouched\n")
    for kind in ("symlink", "hardlink", "fifo"):
        lock.unlink(missing_ok=True)
        if kind == "symlink":
            lock.symlink_to(outside)
        elif kind == "hardlink":
            os.link(outside, lock)
        else:
            os.mkfifo(lock)
        assert receipts._start_receipt(home, {"session_id": "s-1"}, None, False)[0]
        assert not (folder / "receipt-s-1.ok").exists()
        assert not (folder / receipts.RECEIPTS_LOG).exists()
        assert outside.read_text() == "untouched\n"


def test_refused_today_bounds_reads_and_rejects_invalid_utf8(home):
    folder = home.shared / "sessions"
    folder.mkdir(exist_ok=True)
    log = folder / receipts.RECEIPTS_LOG
    for raw in (b"\xff", b"x" * (16 * 1024 * 1024 + 1)):
        log.write_bytes(raw)
        assert receipts.refused_today(home) is None


def test_screening_outage_cannot_leave_stale_marks_or_double_count_sessions(home, monkeypatch):
    from awb import side

    monkeypatch.setattr(side, "_safe", lambda text: True)
    monkeypatch.setattr(receipts, "receipt", lambda *args: ([], [], "guards: active"))
    receipts._start_receipt(home, {"session_id": "s-1"}, None, False)
    mark = receipts._receipt_mark(home, "s-1")
    assert mark.is_file()
    monkeypatch.setattr(side, "_safe", lambda text: False)
    monkeypatch.setattr(receipts, "receipt", lambda *args: ([], ["keys"], "Start refused"))
    receipts._start_receipt(home, {"session_id": "s-1"}, None, False)
    assert not mark.exists()
    monkeypatch.setattr(side, "_safe", lambda text: True)
    receipts._start_receipt(home, {"session_id": "s-1"}, None, False)
    assert receipts.refused_today(home) == 1
