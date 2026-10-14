"""Tests of the seal: seal/setup.sh and seal/verify.sh (syntax and dry runs only), the unit, the client files of the
work user and awb/seal.py (`awb seal check`).

The scripts only ever run with --dry-run here or where they must refuse before any step. Every command they could
change something with is shadowed by a fake on PATH that leaves a mark; no test may find a mark. `sudo` is never
run: the checks of awb/seal.py get a fake in its place.
"""
from __future__ import annotations

import getpass
import json
import os
import pwd
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

import pytest

from awb import config, gate, hooks, seal
from tests import fixtures
from tests.conftest import not_as_build_user

REPO = Path(__file__).resolve().parent.parent
SEAL = REPO / "seal"
SETUP = SEAL / "setup.sh"
VERIFY = SEAL / "verify.sh"
UNIT = SEAL / "awb-vaultd.service"
WORK_SETTINGS = SEAL / "work-claude" / "settings.json"
WORK_CLAUDE = SEAL / "work-claude" / "CLAUDE.md"

USER = getpass.getuser()
OWNER_HOME = pwd.getpwnam(USER).pw_dir.rstrip("/")
VAULT = OWNER_HOME + "/tcp-vault"
PSTORE = OWNER_HOME + "/.password-store"

# every command the scripts could change something with; the fakes only leave a mark
MUTATING = ("groupadd", "useradd", "usermod", "gpasswd", "chmod", "chown", "mv", "mkdir", "install", "systemctl",
            "ln", "rm", "runuser", "git", "tar", "python3", "pip", "mount", "find", "tee", "cp", "sudo", "mktemp",
            "awk", "passwd", "chgrp", "touch")


@pytest.fixture
def fakebin(tmp_path):
    folder = tmp_path / "fakebin"
    folder.mkdir()
    marks = tmp_path / "marks"
    for name in MUTATING:
        f = folder / name
        f.write_text("#!/bin/sh\necho %s >> '%s'\nexit 0\n" % (name, marks), encoding="utf-8")
        f.chmod(0o755)
    return folder, marks


def run_script(script: Path, args: list[str], fakebin, sudo_user: str | None = USER,
               build_host: str | None = None) -> subprocess.CompletedProcess:
    """A dry run plans against a fresh host by default (AWB_SETUP_BUILD_HOST): the build side's probes never look at
    the host the suite runs on, which may carry the build user and its venv (D-SB1-11)."""
    if build_host is None:
        build_host = "fresh" if "--dry-run" in args else ""
    folder, _ = fakebin
    env = {k: v for k, v in os.environ.items()
           if k not in ("SUDO_USER", "AWB_SETUP_BUILD_HOST", "AWB_SETUP_CONF", "AWB_VERIFY_CONF")}
    env["PATH"] = "%s:%s" % (folder, os.environ.get("PATH", "/usr/bin:/bin"))
    if "--dry-run" in args:
        # a dry run reads no host file of this host: /etc/awb/paths.conf stands in as a file that is not there
        env["AWB_SETUP_CONF"] = env["AWB_VERIFY_CONF"] = str(folder.parent / "host-paths.conf")
    if build_host:
        env["AWB_SETUP_BUILD_HOST"] = build_host
    if sudo_user is not None:
        env["SUDO_USER"] = sudo_user
    return subprocess.run(["bash", str(script), *args], capture_output=True, text=True, env=env, timeout=60,
                          cwd=folder.parent)


def no_marks(fakebin) -> None:
    _, marks = fakebin
    assert not marks.exists(), "a command ran: %s" % marks.read_text(encoding="utf-8").split()


def commands(out: str) -> list[str]:
    """Command lines of a dry run: '+ ' will run, '= ' is skipped because it is already in place."""
    return [line for line in out.splitlines() if line.startswith(("+ ", "= "))]


# --------------------------------------------------------------------------- the scripts


@pytest.mark.parametrize("script", [SETUP, VERIFY])
def test_scripts_parse_and_are_strict(script):
    res = subprocess.run(["bash", "-n", str(script)], capture_output=True, text=True, timeout=30)
    assert res.returncode == 0, res.stderr
    text = script.read_text(encoding="utf-8")
    assert text.startswith("#!/usr/bin/env bash\n")
    assert "set -euo pipefail" in text
    assert os.access(script, os.X_OK)


def test_scripts_read_homes_from_getent_and_carry_none(fakebin):
    for script in (SETUP, VERIFY):
        text = script.read_text(encoding="utf-8")
        assert "getent passwd" in text
        assert gate.DETECTORS["homepath"](text) == []
        assert OWNER_HOME not in text


def test_setup_dry_run_prints_every_step_and_keeps_the_work_users_keys_out_of_its_reach(fakebin):
    """Replaces test_setup_dry_run_prints_every_step_and_closes_the_host_file_to_other_users (F2 G3: the owner's
    keys go to the root owned key file of the work user in the ssh folder of the host, 644 root:root; it pinned a copy
    in the work home owned by the work user, 600), which replaced test_setup_dry_run_prints_every_step (SB1 of the
    self-build: the host file is written 640 root:awb in /etc/awb root:awb 751, it pinned 644 root:root; the one
    pip install of the seal is the one into the suite's venv on the build image, it pinned none). Everything else
    is the same."""
    res = run_script(SETUP, ["--dry-run"], fakebin)
    assert res.returncode == 0, res.stderr
    no_marks(fakebin)
    out = res.stdout
    cmds = commands(out)
    joined = "\n".join(cmds)

    def has(pattern: str) -> bool:
        return re.search(pattern, joined, re.M) is not None

    assert has(r"^[+=] groupadd awb(  # .*)?$")
    assert has(r"^[+=] useradd .*--gid awb .*awb(  # .*)?$")
    assert has(r"^[+=] usermod -a -G awb %s(  # .*)?$" % re.escape(USER))
    # the moves
    assert has(r"^\+ mv -- %s/tcp-shared \S+/tcp-shared  # if present$" % re.escape(OWNER_HOME))
    assert has(r"^\+ mv -- %s/tcp-kb \S+/tcp-kb  # if present$" % re.escape(OWNER_HOME))
    assert has(r"^\+ mv -- %s/tcp-<code> \S+/tcp-<code>" % re.escape(OWNER_HOME))
    assert has(r"^\+ chmod 2750 \S+/tcp-shared$")
    assert has(r"find \S+/tcp-shared/outbox \S+/tcp-shared/tenants -type d -exec chmod 3770")
    assert has(r"git -C \S+/tcp-kb init -q$")
    # the code (T1, build/DECISIONS.md row 11): a release folder per commit, one .pth line, the exchange
    assert has(r"git -C %s archive HEAD \| tar -x --no-same-owner --no-same-permissions -C "
               r"/opt/tcp-awb/releases/\.<HEAD>\.partial$" % re.escape(str(REPO)))
    assert has(r"^\+ python3 -m venv --system-site-packages /opt/tcp-awb/venv")
    assert has(r"^\+ write /opt/tcp-awb/venv/lib/python3[^/]*/site-packages/awb\.pth \(mode 644, root:root\)$")
    assert has(r"^\+ exchange /opt/tcp-awb/src\.next /opt/tcp-awb/src  # renameat2 RENAME_EXCHANGE, one call$")
    assert [c for c in cmds if "pip install" in c] == [
        "+ /srv/awb-build/venv/bin/python -I -m pip install --no-cache-dir --disable-pip-version-check pytest "
        "pytest-timeout python-docx python-pptx openpyxl reportlab"]
    assert not has(r"rm -rf -- /opt/tcp-awb/src$")
    # T3 (build/DECISIONS.md, 2026-10-07, D-T3a): a wrapper in place of the symlink, see the wrapper tests below
    assert has(r"^\+ write /usr/local/bin/awb \(mode 755, root:root\)$")
    # the host file and the unit
    assert has(r"^\+ chown root:awb /etc/awb$") and has(r"^\+ chmod 751 /etc/awb$")
    assert has(r"^\+ write /etc/awb/paths\.conf \(mode 640, root:awb\)$")
    assert has(r"^\+ chmod 640 /etc/awb/blocklist\.txt  # if present$")
    for key in ("owner = %s" % USER, "vault = %s" % VAULT, "check_socket = /run/awb/check.sock",
                "mirrors = /srv/tcp-mirrors"):
        assert "    | " + key in out.splitlines()
    assert has(r"^\+ write /etc/systemd/system/awb-vaultd\.service ")
    assert "    | User=%s" % USER in out.splitlines()
    assert "    | Group=awb" in out.splitlines()
    assert has(r"^\+ systemctl enable --now awb-vaultd\.service$")
    # the keys and the client files of the work user
    assert has(r"^\+ install -o root -g root -m 644 %s/\.ssh/authorized_keys /etc/ssh/authorized_keys/awb  # if present$"
               % re.escape(OWNER_HOME))
    assert not has(r"install -o awb .*authorized_keys")
    for name in ("settings.json", "CLAUDE.md"):
        assert has(r"^\+ install -o root -g awb -m 644 %s \S+/\.claude/%s$"
                   % (re.escape(str(SEAL / "work-claude" / name)), re.escape(name)))
    assert "nothing was changed" in out


def test_setup_dry_run_never_writes_into_the_vault_or_the_password_store(fakebin):
    res = run_script(SETUP, ["--dry-run", "--mirrors", "/srv/nothing/docs"], fakebin)
    assert res.returncode == 0, res.stderr
    no_marks(fakebin)
    lines = res.stdout.splitlines()
    # every line is a command, a skipped command, a note or the content of a written file: nothing hides
    assert all(line.startswith(("+ ", "= ", "# ", "    | ")) for line in lines)
    touching = [c for c in commands(res.stdout) if VAULT in c or PSTORE in c]
    assert sorted(touching) == sorted([
        "+ chmod 700 %s  # if present" % VAULT,
        "+ chmod 700 %s  # if present" % PSTORE,
    ])
    assert "+ append to /etc/fstab: /srv/nothing/docs /srv/tcp-mirrors/docs none bind,ro 0 0" in lines


@pytest.mark.parametrize("sudo_user, args, message", [
    (None, ["--dry-run"], "SUDO_USER is empty"),
    ("", ["--dry-run"], "SUDO_USER is empty"),
    ("root", ["--dry-run"], "not root"),
    ("awb", ["--dry-run"], "not be the work user"),
    (USER, [], "run this with sudo"),
    (USER, ["--mirrors"], "at least one folder"),
    (USER, ["--bogus"], "usage"),
])
def test_setup_refuses_before_any_step(fakebin, sudo_user, args, message):
    if not args and os.geteuid() == 0:
        pytest.skip("runs as root")
    res = run_script(SETUP, args, fakebin, sudo_user=sudo_user)
    assert res.returncode != 0
    assert message in res.stderr
    assert commands(res.stdout) == []
    no_marks(fakebin)


@pytest.mark.parametrize("mirror", [VAULT, VAULT + "/originals", OWNER_HOME, PSTORE, OWNER_HOME + "/.ssh"])
def test_setup_refuses_a_mirror_that_shows_the_vault_or_keys(fakebin, mirror):
    res = run_script(SETUP, ["--dry-run", "--mirrors", mirror], fakebin)
    assert res.returncode == 1
    assert "refused" in res.stderr
    assert commands(res.stdout) == []
    no_marks(fakebin)


def _guard(snippet: str) -> subprocess.CompletedProcess:
    """Run the guard of setup.sh on its own, with an invented owner home."""
    lines = SETUP.read_text(encoding="utf-8").splitlines(keepends=True)
    start = next(i for i, x in enumerate(lines) if x.startswith("# ----") and "printing and the guard" in x)
    end = next(i for i, x in enumerate(lines) if x.startswith("# ----") and x.rstrip().endswith("the steps"))
    body = "".join(lines[start:end])
    script = "set -euo pipefail\ndie() { echo \"setup: $*\" >&2; exit 1; }\ndry_run=1\n" \
             "protected=(/srv/o/tcp-vault /srv/o/.password-store)\n%s\n%s\n" % (body, snippet)
    return subprocess.run(["bash", "-c", script], capture_output=True, text=True, timeout=30)


@pytest.mark.parametrize("snippet, allowed", [
    ("run chmod 700 /srv/o/tcp-vault", True),
    ("run chmod 750 /srv/o", True),
    ("run mv -- /srv/o/tcp-shared /srv/w/tcp-shared", True),
    ("run chmod -R 700 /srv/o/tcp-vault", False),
    ("run chmod 644 /srv/o/tcp-vault/register.tsv", False),
    ("run mv -- /srv/o/tcp-vault /srv/w/tcp-vault", False),
    ("run cp /srv/o/.password-store/x.gpg /tmp/x", False),
    ("run chown -R awb:awb /srv/o", False),
    ("run rm -rf -- /srv/o/", False),
    ("run_if_present /srv/o/tcp-vault/log chown awb /srv/o/tcp-vault/log", False),
    ("echo x | write_file /srv/o/tcp-vault/register.tsv 600 root:root", False),
    ("append_line /srv/o/.password-store/.gpg-id x", False),
])
def test_setup_guard_refuses_every_step_into_the_vault_but_a_mode(snippet, allowed):
    res = _guard(snippet)
    assert (res.returncode == 0) is allowed, res.stderr
    if not allowed:
        assert "refused" in res.stderr
        assert res.stdout == ""


def test_verify_dry_run_lists_every_check(fakebin):
    for sudo_user in (None, USER):
        res = run_script(VERIFY, ["--dry-run"], fakebin, sudo_user=sudo_user)
        assert res.returncode == 0, res.stderr
        no_marks(fakebin)
        checks = [line[6:] for line in res.stdout.splitlines() if line.startswith("CHECK ")]
        assert not [line for line in res.stdout.splitlines() if line.startswith(("PASS", "FAIL"))]
        text = "\n".join(checks)
        for must in ("cannot read the register", "cannot read the encrypted register", "cannot list the vault",
                     "password store", "owner's .claude", "owner's .ssh", "folder in the owner's home",
                     "sudo -n true", "check socket answers ping", "can write tcp-shared",
                     "owner can write the outbox"):
            assert must in text, must
        for group in ("ubuntu", "docker", "adm", "sudo", "lxd"):
            assert "the work user is not in the group %s" % group in checks


def test_verify_refuses_without_root(fakebin):
    if os.geteuid() == 0:
        pytest.skip("runs as root")
    res = run_script(VERIFY, [], fakebin)
    assert res.returncode == 2
    assert "sudo" in res.stderr
    assert not re.search(r"^(PASS|FAIL|CHECK)", res.stdout, re.M)
    no_marks(fakebin)


# --------------------------------------------------------------------------- the unit and the client files


def test_unit_runs_the_daemon_as_the_owner_with_the_group_awb():
    keys: dict[str, str] = {}
    section = None
    for line in UNIT.read_text(encoding="utf-8").splitlines():
        if line.startswith("["):
            section = line
        elif "=" in line and not line.startswith("#") and section == "[Service]":
            k, v = line.split("=", 1)
            keys[k] = v
    assert keys["User"] == "@OWNER@"
    assert keys["Group"] == "awb"
    assert keys["RuntimeDirectory"] == "awb"
    assert keys["RuntimeDirectoryMode"] == "0750"
    assert keys["ExecStart"] == "/usr/local/bin/awb vault serve"
    assert UNIT.read_text(encoding="utf-8").count("@OWNER@") == 1


def _service_keys(path: Path) -> dict[str, list[str]]:
    keys: dict[str, list[str]] = {}
    section = None
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.startswith("["):
            section = line
        elif "=" in line and not line.startswith("#") and section == "[Service]":
            k, v = line.split("=", 1)
            keys.setdefault(k, []).append(v)
    return keys


@pytest.mark.parametrize("unit, command", [("awb-vaultd.service", "vault"), ("awb-keyd.service", "keys")])
def test_the_units_declare_notify_and_reload(unit, command):
    """T2, design 8.6. Planted: a unit without one of the four lines; ExecReload naming another command."""
    path = SEAL / unit
    keys = _service_keys(path)
    assert keys["Type"] == ["notify"]
    assert keys["NotifyAccess"] == ["main"]
    assert keys["ExecReload"] == ["/usr/local/bin/awb %s reload" % command]
    assert keys["TimeoutStartSec"] == ["120"]
    assert keys["ExecStart"] == ["/usr/local/bin/awb %s serve" % command]
    assert "Supervising process N which is not our child" in path.read_text(encoding="utf-8")


def test_work_settings_carry_the_hooks_and_deny_every_tool_the_guard_does_not_see():
    """Replaces test_work_settings_carry_the_five_hooks_and_deny_connectors (finding 9 of the review of 2026-10-10):
    the deny list carries the connectors and every tool of the client that reaches a file, a command or work
    elsewhere outside the guard."""
    data = json.loads(WORK_SETTINGS.read_text(encoding="utf-8"))
    assert set(data) == {"hooks", "permissions"}
    assert data["hooks"] == hooks.client_settings("/usr/local/bin/awb hook")["hooks"]
    assert set(data["hooks"]) == {"UserPromptSubmit", "PreToolUse", "PostToolUse", "Stop", "SessionStart"}
    assert data["permissions"] == {"deny": list(hooks.DENIED_TOOLS)}
    assert data["permissions"]["deny"][0] == "mcp__*" and "PowerShell" in data["permissions"]["deny"]


def test_work_claude_md_is_short_clean_and_complete(tmp_path):
    text = WORK_CLAUDE.read_text(encoding="utf-8")
    assert len(text.splitlines()) <= 60
    reg = tmp_path / "register.tsv"
    reg.write_text("\n".join(fixtures.register_lines()) + "\n", encoding="utf-8")
    assert gate.scan_files([WORK_CLAUDE], reg) == []
    fixtures.assert_no_fixture_name(text, "CLAUDE.md of the work user")
    assert "\u2014" not in text
    assert not re.search(r",\s+(and|or)\b", text)
    for must in ("awb intake", "awb ledger add", "awb career", "awb report", "English note", "never repeat it",
                 "~/tcp-shared", "~/tcp-kb", "/opt/tcp-awb"):
        assert must in text, must


def test_every_seal_file_passes_the_gate(tmp_path):
    reg = tmp_path / "register.tsv"
    reg.write_text("\n".join(fixtures.register_lines()) + "\n", encoding="utf-8")
    files = sorted(f for f in SEAL.rglob("*") if f.is_file())
    assert {f.name for f in files} >= {"setup.sh", "verify.sh", "awb-vaultd.service", "settings.json",
                                       "CLAUDE.md", "README.md"}
    assert gate.scan_files(files, reg) == []


# --------------------------------------------------------------------------- awb seal check


@pytest.fixture
def ping_server():
    """A small check socket under /tmp that answers each request with the given reply; stopped at test end."""
    folder = tempfile.mkdtemp(prefix="awb", dir="/tmp")
    path = Path(folder) / "check.sock"
    reply = {"answer": b'{"ok":true,"state":"unlocked"}\n'}
    srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    srv.bind(str(path))
    srv.listen(4)
    srv.settimeout(0.2)
    stop = threading.Event()

    def serve():
        while not stop.is_set():
            try:
                conn, _ = srv.accept()
            except (socket.timeout, OSError):
                continue
            with conn:
                conn.settimeout(2)
                try:
                    conn.recv(4096)
                    conn.sendall(reply["answer"])
                except OSError:
                    pass

    t = threading.Thread(target=serve, daemon=True)
    t.start()
    try:
        yield path, reply
    finally:
        stop.set()
        t.join(5)
        srv.close()
        shutil.rmtree(folder, ignore_errors=True)


@pytest.fixture
def sealed(tmp_path, monkeypatch, ping_server):
    """An owner home with a vault, a shared side, fakes for sudo and the groups. The home starts closed."""
    sock, reply = ping_server
    home = tmp_path / "owner"
    vault = home / "tcp-vault"
    vault.mkdir(parents=True)
    (vault / "register.tsv").write_text("\n".join(fixtures.register_lines()) + "\n", encoding="utf-8")
    for d in (".password-store", ".claude", ".ssh", "projects"):
        (home / d).mkdir()
    shared = tmp_path / "work" / "tcp-shared"
    (shared / "outbox").mkdir(parents=True)
    p = config.Paths(shared=shared, vault=vault, projects_root=tmp_path / "work", check_socket=sock)
    sudo_calls = []
    monkeypatch.setattr(seal, "_sudo_allowed", lambda: sudo_calls.append(1) or False)
    monkeypatch.setattr(seal, "_group_names", lambda: {"awb"})
    home.chmod(0)
    if os.access(home, os.R_OK):
        home.chmod(0o755)
        pytest.skip("file modes do not bind this user")
    try:
        yield p, home, reply, sudo_calls
    finally:
        home.chmod(0o755)


def by_what(results) -> dict[str, seal.Result]:
    return {r.what: r for r in results}


def test_a_sealed_user_passes_every_check(sealed):
    p, home, _, sudo_calls = sealed
    results = seal.run_checks(p, home)
    assert [r.what for r in results if not r.ok] == []
    assert len(results) >= 13
    assert sudo_calls == [1]


def test_an_open_home_fails_the_checks_of_the_vault_and_the_home(sealed):
    p, home, _, _ = sealed
    home.chmod(0o755)
    got = by_what(seal.run_checks(p, home))
    for what in ("cannot read the register (plain or encrypted)", "cannot list the vault",
                 "cannot list the owner's password store", "cannot list the owner's .claude",
                 "cannot list the owner's .ssh", "cannot list the owner's home",
                 "cannot enter the owner's home (its folders included)"):
        assert not got[what].ok, what
    assert got["the check socket answers ping"].ok


def test_an_absent_path_proves_nothing(sealed, tmp_path):
    p, _, _, _ = sealed
    nowhere = tmp_path / "nowhere"
    got = by_what(seal.run_checks(config.Paths(shared=p.shared, vault=nowhere / "tcp-vault",
                                               projects_root=p.projects_root, check_socket=p.check_socket),
                                  nowhere))
    assert not got["cannot list the vault"].ok
    assert "proves nothing" in got["cannot list the vault"].note
    assert not got["cannot read the register (plain or encrypted)"].ok


def test_sudo_groups_ping_and_writes_can_fail(sealed, monkeypatch):
    p, home, reply, _ = sealed
    monkeypatch.setattr(seal, "_sudo_allowed", lambda: True)
    monkeypatch.setattr(seal, "_group_names", lambda: {"awb", "docker"})
    reply["answer"] = b'{"ok":false,"error":"locked"}\n'
    unwritable = p.shared.parent / "readonly"
    unwritable.mkdir()
    unwritable.chmod(0o500)
    try:
        q = config.Paths(shared=unwritable, vault=p.vault, projects_root=p.projects_root, check_socket=p.check_socket)
        failed = {r.what for r in seal.run_checks(q, home) if not r.ok}
    finally:
        unwritable.chmod(0o700)
    assert failed == {"cannot run sudo -n true", "not in the group docker", "the check socket answers ping",
                      "can write tcp-shared", "can write the outbox"}


def test_ping_without_a_daemon_fails(tmp_path):
    folder = tempfile.mkdtemp(prefix="awb", dir="/tmp")
    try:
        assert seal._ping(Path(folder) / "none.sock") is False
    finally:
        shutil.rmtree(folder, ignore_errors=True)


def test_the_command_prints_pass_or_fail_per_line(sealed, monkeypatch, capsys):
    p, home, _, _ = sealed
    monkeypatch.setattr(seal.config, "paths", lambda *a, **k: p)
    monkeypatch.setattr(seal, "owner_home", lambda *a, **k: home)
    from awb import cli
    assert cli.main(["seal", "check"]) == 0
    out = capsys.readouterr().out
    lines = [line for line in out.splitlines() if not line.startswith("#")]
    assert lines and all(re.match(r"^(PASS|FAIL)  \S", line) for line in lines)
    assert str(home.parent) not in out
    monkeypatch.setattr(seal, "_group_names", lambda: {"awb", "sudo"})
    assert seal.main(["check"]) == 1
    out = capsys.readouterr().out
    assert "FAIL  not in the group sudo" in out.splitlines()
    assert seal.main([]) == 2


def test_sudo_probe_runs_the_sudo_on_path_without_a_prompt(tmp_path, monkeypatch):
    folder = tmp_path / "bin"
    folder.mkdir()
    log = tmp_path / "sudo.log"
    fake = folder / "sudo"
    monkeypatch.setenv("PATH", str(folder))
    for code, allowed in ((0, True), (1, False)):
        fake.write_text("#!/bin/sh\nprintf '%%s\\n' \"$*\" >> '%s'\nexit %d\n" % (log, code), encoding="utf-8")
        fake.chmod(0o755)
        assert seal._sudo_allowed() is allowed
    assert log.read_text(encoding="utf-8").splitlines() == ["-n true", "-n true"]
    fake.unlink()
    assert seal._sudo_allowed() is False


def test_owner_home_comes_from_the_host_file_or_the_vault(tmp_path):
    p = config.Paths(shared=tmp_path / "s", vault=tmp_path / "o" / "tcp-vault", projects_root=tmp_path)
    assert seal.owner_home(p, {}) == tmp_path / "o"
    assert seal.owner_home(p, {"owner": USER}) == Path(OWNER_HOME)
    assert seal.owner_home(p, {"owner": "no-such-user-" + "x7"}) == tmp_path / "o"


def test_setup_keeps_needrestart_away_from_the_daemons_that_hold_secrets(fakebin):
    """A package upgrade restarted the vault daemon on 2026-10-06 and the console lost projects and chat until the
    owner unlocked the vault again: needrestart now leaves the vault daemon and the key service running."""
    res = run_script(SETUP, ["--dry-run"], fakebin)
    assert res.returncode == 0, res.stderr
    out = res.stdout.splitlines()
    assert "+ write /etc/needrestart/conf.d/awb.conf (mode 644, root:root)" in out
    for unit in ("awb-vaultd", "awb-keyd"):
        assert "    | $nrconf{override_rc}{qr(^%s\\b)} = 0;" % unit in out


@pytest.mark.skipif(shutil.which("perl") is None, reason="perl is not installed")
def test_the_needrestart_rule_matches_the_two_daemons_and_nothing_else():
    script = ("our %%nrconf = (override_rc => {}); do '%s'; die $@ if $@;"
              "for my $rc (@ARGV) { my $r = 1; foreach my $re (keys %%{$nrconf{override_rc}}) "
              "{ next unless $rc =~ /$re/; $r = $nrconf{override_rc}->{$re}; last } print \"$rc=$r\\n\" }"
              % (SEAL / "needrestart-awb.conf"))
    units = ["awb-vaultd.service", "awb-keyd.service", "awb-web.service", "awb-ask.service", "awb-vaultdx.service"]
    res = subprocess.run(["perl", "-e", script] + units, capture_output=True, text=True, check=True)
    assert res.stdout.split() == ["awb-vaultd.service=0", "awb-keyd.service=0", "awb-web.service=1",
                                  "awb-ask.service=1", "awb-vaultdx.service=1"]


# --------------------------------------------------------------------------- T3: the installed command runs isolated

WRAPPER_EXEC = 'exec /opt/tcp-awb/venv/bin/python3 -I -m awb "$@"'
MARKER = "PLANTED-PACKAGE-REACHED"


def wrapper_text(fakebin) -> str:
    """The wrapper as the dry run of setup.sh prints it: the lines under `+ write /usr/local/bin/awb`."""
    res = run_script(SETUP, ["--dry-run"], fakebin)
    assert res.returncode == 0, res.stderr
    lines = res.stdout.splitlines()
    start = lines.index("+ write /usr/local/bin/awb (mode 755, root:root)") + 1
    body = []
    for line in lines[start:]:
        if not line.startswith("    | "):
            break
        body.append(line[len("    | "):])
    return "\n".join(body) + "\n"


def plant(tmp_path: Path) -> dict[str, str]:
    """A package named awb whose cli.main and __main__ print MARKER, on PYTHONPATH, in a user site and in the working
    folder. Returns the environment that carries the plant."""
    import sysconfig

    userbase = tmp_path / "userbase"
    usersite = Path(sysconfig.get_path("purelib", "posix_user", vars={"userbase": str(userbase)}))
    for folder in (tmp_path / "pythonpath", usersite, tmp_path / "cwd"):
        pkg = folder / "awb"
        pkg.mkdir(parents=True)
        (pkg / "__init__.py").write_text("", encoding="utf-8")
        (pkg / "cli.py").write_text("def main(argv=None):\n    print(%r)\n    return 0\n" % MARKER, encoding="utf-8")
        (pkg / "__main__.py").write_text("print(%r)\n" % MARKER, encoding="utf-8")
    env = {k: v for k, v in os.environ.items() if not k.startswith("PYTHON")}
    env.update(PYTHONPATH=str(tmp_path / "pythonpath"), PYTHONUSERBASE=str(userbase), HOME=str(tmp_path))
    return env


def wrapper_problems(text: str, tmp_path: Path) -> list[str]:
    """What is wrong with a wrapper text: its shape, and what it loads with a planted package (the interpreter of
    the tests stands in for the installed one)."""
    import sys

    tmp_path.mkdir(parents=True, exist_ok=True)
    problems = []
    lines = text.splitlines()
    if not lines or lines[0] != "#!/bin/sh":
        problems.append("first line")
    if WRAPPER_EXEC not in lines:
        problems.append("exec line")
    runnable = tmp_path / "wrapper"
    runnable.write_text(text.replace("/opt/tcp-awb/venv/bin/python3", sys.executable), encoding="utf-8")
    runnable.chmod(0o755)
    env = plant(tmp_path)
    res = subprocess.run([str(runnable), "vault", "--help"], capture_output=True, text=True, env=env, timeout=60,
                         cwd=tmp_path / "cwd", stdin=subprocess.DEVNULL)
    if MARKER in res.stdout + res.stderr:
        problems.append("loaded the planted package")
    if res.returncode != 0 or "usage: awb vault" not in res.stdout:
        problems.append("no usage of the real command")
    return problems


def test_setup_writes_the_wrapper_not_a_symlink(fakebin):
    """Planted failure: the symlink of today (`ln -sfn` of the bin link) or a wrapper of another shape."""
    res = run_script(SETUP, ["--dry-run"], fakebin)
    no_marks(fakebin)
    assert not re.search(r"^[+=] ln -sfn \S+ /usr/local/bin/awb", res.stdout, re.M)
    assert wrapper_text(fakebin) == ("#!/bin/sh\n# written by seal/setup.sh: the installed Workbench, run isolated "
                                     "from the invoking user's packages\n%s\n" % WRAPPER_EXEC)
    assert res.stdout.count("+ write /usr/local/bin/awb (mode 755, root:root)") == 1


def test_the_wrapper_ignores_a_planted_package(fakebin, tmp_path):
    """The wrapper lines of the dry run, run with a package planted on PYTHONPATH, in a user site and in the working
    folder: the real command answers. The control, the old symlink (the venv script), loads the plant, which proves
    the test can fail; a wrapper with -i, without exec or with "$*" fails it."""
    import sys

    text = wrapper_text(fakebin)
    assert wrapper_problems(text, tmp_path / "real") == []
    script = Path(sys.executable).parent / "awb"
    if not script.exists():
        pytest.skip("the virtual environment has no awb entry point")
    control = tmp_path / "control"
    control.mkdir()
    res = subprocess.run([str(script), "vault", "--help"], capture_output=True, text=True, env=plant(control),
                         timeout=60, cwd=control / "cwd", stdin=subprocess.DEVNULL)
    assert MARKER in res.stdout, "the control did not load the plant: this test cannot fail"
    for n, bad in enumerate((text.replace(" -I ", " -i "), text.replace("exec ", ""), text.replace('"$@"', '"$*"'))):
        assert bad != text
        assert wrapper_problems(bad, tmp_path / ("bad%d" % n)), bad


def isolation_problems(text: str) -> list[str]:
    """ExecStart lines of a unit text that run a python3 without -I."""
    out = []
    for line in text.splitlines():
        m = re.match(r"^ExecStart=(\S*python3)(\s.*)$", line)
        if m and not m.group(2).startswith(" -I "):
            out.append(line)
    return out


def test_the_web_units_run_isolated():
    """Planted failure: a template of seal/web whose python3 line lacks -I."""
    units = sorted(f for f in (SEAL / "web").rglob("*") if f.suffix in (".service", ".conf"))
    pythons = [f for f in units if re.search(r"(?m)^ExecStart=\S*python3", f.read_text(encoding="utf-8"))]
    assert len(pythons) >= 7
    for f in units:
        assert isolation_problems(f.read_text(encoding="utf-8")) == [], f.name
    planted = (SEAL / "web" / "awb-materials.service").read_text(encoding="utf-8").replace(" -I -m ", " -m ")
    assert isolation_problems(planted)
    gateway = (SEAL / "web" / "awb-web.service").read_text(encoding="utf-8")
    assert "ExecStart=/usr/bin/python3 -I /opt/tcp-awb/src/awb/tcp/web/gateway.py " in gateway


def test_command_prefix_recognises_the_wrapper(tmp_path, monkeypatch):
    """A wrapper file in a temporary bin gives `<wrapper> hook`; the symlink of a host sealed before T3 still works;
    the fallback of an isolated interpreter carries -I; on a sealed host a foreign prefix is refused."""
    import sys
    from types import SimpleNamespace

    from awb import projects

    wrapper = tmp_path / "bin" / "awb"
    wrapper.parent.mkdir()
    wrapper.write_text("#!/bin/sh\n# a comment\nexec %s -I -m awb \"$@\"\n" % sys.executable, encoding="utf-8")
    monkeypatch.setattr(hooks, "INSTALLED_AWB", wrapper)
    assert hooks.command_prefix() == "%s hook" % wrapper
    for bad in ("exec %s -m awb \"$@\"" % sys.executable, "exec /usr/bin/python3 -I -m awb \"$@\"",
                "%s -I -m awb \"$@\"" % sys.executable):
        wrapper.write_text("#!/bin/sh\n%s\n" % bad, encoding="utf-8")
        assert hooks.command_prefix() != "%s hook" % wrapper, bad
    script = Path(sys.executable).parent / "awb"
    if script.exists():
        link = tmp_path / "bin" / "link"
        link.symlink_to(script)
        monkeypatch.setattr(hooks, "INSTALLED_AWB", link)
        assert hooks.command_prefix() == "%s hook" % link
    monkeypatch.setattr(hooks, "INSTALLED_AWB", tmp_path / "none" / "awb")
    monkeypatch.setattr(hooks, "sys", SimpleNamespace(executable="/opt/x/bin/python3",
                                                      flags=SimpleNamespace(isolated=1)))
    assert hooks.command_prefix() == "/opt/x/bin/python3 -I -m awb hook"
    monkeypatch.setattr(hooks, "sys", SimpleNamespace(executable="/opt/x/bin/python3",
                                                      flags=SimpleNamespace(isolated=0)))
    assert hooks.command_prefix() == "/opt/x/bin/python3 -m awb hook"
    monkeypatch.setattr(hooks, "sys", sys)
    host = tmp_path / "paths.conf"
    host.write_text("owner = someone\nwork_user = awb\n", encoding="utf-8")
    monkeypatch.setattr(hooks, "HOST_FILE", host)
    monkeypatch.setattr(hooks, "INSTALLED_AWB", tmp_path / "none" / "awb")
    with pytest.raises(hooks.ForeignPrefix):
        hooks.command_prefix()
    with pytest.raises(hooks.ForeignPrefix):
        hooks.command_prefix("/opt/tcp-awb/venv/bin/python")
    with pytest.raises(projects.ProjectError) as err:
        projects._settings()
    assert "sealed" in str(err.value) and "nothing was created" in str(err.value)
    wrapper.write_text("#!/bin/sh\nexec %s -I -m awb \"$@\"\n" % sys.executable, encoding="utf-8")
    monkeypatch.setattr(hooks, "INSTALLED_AWB", wrapper)
    assert hooks.command_prefix() == "%s hook" % wrapper


# --------------------------------------------------------------------------- T1: the update mode and the releases

IMPORT_LOOP = r"""
import importlib, os, sys, time
src, stop, out = sys.argv[1:4]
sys.path.insert(0, src)
n = fails = 0
while not os.path.exists(stop):
    for m in [k for k in sys.modules if k == "awb" or k.startswith("awb.")]:
        del sys.modules[m]
    importlib.invalidate_caches()
    try:
        import awb.config
        n += 1
    except Exception:
        fails += 1
    time.sleep(0.002)
with open(out, "w") as fh:
    fh.write("%d %d" % (n, fails))
"""


def _git(repo, *args):
    env = dict(os.environ, GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@example.org", GIT_COMMITTER_NAME="t",
               GIT_COMMITTER_EMAIL="t@example.org")
    return subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, text=True,
                          env=env).stdout.strip()


def _layout(tmp: Path) -> tuple[Path, Path, dict]:
    """A repository with the seal and the package, an install root with the plain folder of before T1 (with a marker)
    and the files of an editable install, and the environment of the test-only override."""
    repo = tmp / "repo"
    shutil.copytree(SEAL, repo / "seal", ignore=shutil.ignore_patterns("__pycache__"))
    shutil.copytree(REPO / "awb", repo / "awb", ignore=shutil.ignore_patterns("__pycache__"))
    _git(repo, "init", "-q")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "--no-verify", "-m", "one")
    opt = tmp / "opt"
    shutil.copytree(REPO / "awb", opt / "src" / "awb", ignore=shutil.ignore_patterns("__pycache__"))
    (opt / "src" / "MARK").write_text("the plain folder\n")
    (opt / "venv" / "bin").mkdir(parents=True)
    (opt / "venv" / "bin" / "python").write_text("")
    site = opt / "venv" / "lib" / "python3.12" / "site-packages"
    (site / "tcp_awb-0.1.0.dist-info").mkdir(parents=True)
    (site / "__editable__.tcp_awb-0.1.0.pth").write_text("import __editable___tcp_awb_0_1_0_finder\n")
    (site / "__editable___tcp_awb_0_1_0_finder.py").write_text("")
    (tmp / "bin").mkdir()
    env = {k: v for k, v in os.environ.items() if k not in ("SUDO_USER",)}
    env.update(SUDO_USER=USER, AWB_SETUP_OPT=str(opt), AWB_SETUP_BIN=str(tmp / "bin" / "awb"),
               AWB_SETUP_CONF=str(tmp / "host-paths.conf"))
    return repo, opt, env


def _with_imports(opt: Path, tmp: Path, run) -> tuple[subprocess.CompletedProcess, int, int]:
    """Run `run()` while a second process imports awb through opt/src every few milliseconds."""
    stop, out = tmp / "stop", tmp / "imports"
    for f in (stop, out):
        if f.exists():
            f.unlink()
    loop = subprocess.Popen([sys.executable, "-I", "-B", "-c", IMPORT_LOOP, str(opt / "src"), str(stop), str(out)])
    try:
        time.sleep(0.3)
        res = run()
        time.sleep(0.2)
    finally:
        stop.write_text("")
        loop.wait(timeout=30)
    n, fails = map(int, out.read_text().split())
    return res, n, fails


def _first_case_problems(res, opt: Path) -> list[str]:
    """What is wrong after the first run over the plain folder."""
    out = []
    if res.returncode != 0:
        return ["exit %d: %s" % (res.returncode, res.stderr.strip()[-200:])]
    src = opt / "src"
    cmds = commands(res.stdout)
    if not src.is_symlink() or not os.readlink(src).startswith("releases/"):
        out.append("src is not a symlink into releases")
    if (opt / "releases" / "pre-deploy" / "MARK").read_text() != "the plain folder\n":
        out.append("the plain folder is not releases/pre-deploy")
    if not any(c.startswith("+ exchange %s/src.next %s/src" % (opt, opt)) for c in cmds):
        out.append("no exchange")
    if [c for c in cmds if re.search(r"\b(mv|rm)\b.* %s/src( |$)" % re.escape(str(opt)), c)]:
        out.append("src itself was moved or removed")
    return out


def test_setup_update_code_step_for_real(tmp_path):
    """Under the test-only override: the plain src with a marker becomes a symlink into releases/<commit> and
    releases/pre-deploy holds the marker; the .pth holds one line; a second run with a new commit keeps two releases
    and flips while a second process imports awb through the layout every few milliseconds; a fake git that exits 1
    leaves src untouched. Planted: the migration swapped with the flip, or mv in place of the exchange, fails the
    first case."""
    repo, opt, env = _layout(tmp_path)
    c1 = _git(repo, "rev-parse", "HEAD")
    run1 = lambda: subprocess.run(["bash", str(repo / "seal" / "setup.sh")], capture_output=True, text=True,  # noqa
                                  env=env, timeout=120)
    res, n, fails = _with_imports(opt, tmp_path, run1)
    assert _first_case_problems(res, opt) == []
    assert (n, fails) > (0, 0) and fails == 0
    assert os.readlink(opt / "src") == "releases/%s" % c1
    site = opt / "venv" / "lib" / "python3.12" / "site-packages"
    assert (site / "awb.pth").read_text() == "%s/src\n" % opt
    assert sorted(p.name for p in site.iterdir()) == ["awb.pth"]
    assert (tmp_path / "bin" / "awb").read_text().splitlines()[-1] == 'exec %s/venv/bin/python3 -I -m awb "$@"' % opt
    assert not [p for p in (opt / "releases").iterdir() if p.name.startswith(".")]
    assert not (opt / "src.next").exists() and not (opt / "src.next").is_symlink()
    # a second commit, extracted the way awb deploy does it, installed with --update from its release folder
    (repo / "awb" / "config.py").write_text((repo / "awb" / "config.py").read_text() + "\n# two\n")
    _git(repo, "commit", "-q", "-a", "--no-verify", "-m", "two")
    c2 = _git(repo, "rev-parse", "HEAD")
    rel2 = opt / "releases" / c2
    rel2.mkdir()
    archive = subprocess.run(["git", "-C", str(repo), "archive", c2], capture_output=True, check=True).stdout
    subprocess.run(["tar", "-x", "-C", str(rel2)], input=archive, check=True)
    run2 = lambda: subprocess.run(["bash", str(rel2 / "seal" / "setup.sh"), "--update"], capture_output=True,  # noqa
                                  text=True, env=env, timeout=120)
    res, n, fails = _with_imports(opt, tmp_path, run2)
    assert res.returncode == 0, res.stderr
    assert n > 0 and fails == 0
    assert os.readlink(opt / "src") == "releases/%s" % c2
    assert sorted(p.name for p in (opt / "releases").iterdir()) == sorted([c1, c2, "pre-deploy"])
    # a git that fails leaves src where it was
    fake = tmp_path / "fakegit"
    fake.mkdir()
    (fake / "git").write_text("#!/bin/sh\nexit 1\n")
    (fake / "git").chmod(0o755)
    (repo / "awb" / "config.py").write_text((repo / "awb" / "config.py").read_text() + "\n# three\n")
    _git(repo, "commit", "-q", "-a", "--no-verify", "-m", "three")
    res = subprocess.run(["bash", str(repo / "seal" / "setup.sh")], capture_output=True, text=True, timeout=120,
                         env=dict(env, PATH="%s:%s" % (fake, env["PATH"])))
    assert res.returncode != 0
    assert os.readlink(opt / "src") == "releases/%s" % c2
    # the planted mutations of the flip fail the first case
    text = (repo / "seal" / "setup.sh").read_text(encoding="utf-8")
    mutations = {
        "mv": text.replace('        exchange "$next" "$src"\n', '        run mv -T -- "$next" "$src"\n'),
        "swapped": text.replace('    run ln -sfn "releases/$release" "$next"\n',
                                '    migrate_plain "$src"\n    run ln -sfn "releases/$release" "$next"\n'),
    }
    for name, mutated in mutations.items():
        assert mutated != text, name
        r, o, e = _layout(tmp_path / name)
        (r / "seal" / "setup.sh").write_text(mutated, encoding="utf-8")
        res = subprocess.run(["bash", str(r / "seal" / "setup.sh")], capture_output=True, text=True, env=e,
                             timeout=120)
        assert _first_case_problems(res, o), name


def test_the_test_override_is_refused_as_root_and_alone(tmp_path):
    """Planted: the override accepted as root, or with one of the two variables only."""
    text = SETUP.read_text(encoding="utf-8")
    assert 'refused as root' in text and '[ "$(id -u)" -ne 0 ] || die "AWB_SETUP_OPT' in text
    env = dict(os.environ, SUDO_USER=USER, AWB_SETUP_OPT=str(tmp_path / "opt"))
    env.pop("AWB_SETUP_BIN", None)
    res = subprocess.run(["bash", str(SETUP)], capture_output=True, text=True, env=env, timeout=60)
    assert res.returncode == 1 and "AWB_SETUP_OPT and AWB_SETUP_BIN" in res.stderr
    assert not (tmp_path / "opt").exists()


def test_setup_full_mode_creates_the_ask_user_and_names_the_next_step(fakebin):
    """Planted: the Ask page's service user left to a hand step, or the old restart line at the end."""
    res = run_script(SETUP, ["--dry-run"], fakebin)
    cmds = commands(res.stdout)
    assert any(re.match(r"^[+=] useradd --system --gid awb --no-create-home -d /nonexistent --shell "
                        r"/usr/sbin/nologin awb-ask", c) for c in cmds)
    assert "restart it yourself" not in SETUP.read_text() and "systemctl restart" not in SETUP.read_text()
    assert 'info "next: sudo awb deploy"' in SETUP.read_text()


def _client_snippet(work_home: Path, sentinel_guard: Path) -> str:
    lines = SETUP.read_text(encoding="utf-8").splitlines(keepends=True)
    start = next(i for i, x in enumerate(lines) if x.startswith("# ----") and "printing and the guard" in x)
    end = next(i for i, x in enumerate(lines) if x.startswith("# ----") and x.rstrip().endswith("the steps"))
    cs = next(i for i, x in enumerate(lines) if x.startswith("clear_target() {"))
    ce = next(i for i, x in enumerate(lines) if x.startswith("step_managed() {"))
    return ("set -euo pipefail\ndie() { echo \"setup: $*\" >&2; exit 1; }\ndry_run=0\ntest_root=1\nROOT_UID=0\n"
            "WORK_USER=awb\nWORK_GROUP=awb\nCLIENT_FILES=\"settings.json CLAUDE.md skills/drafting/SKILL.md\"\n"
            "work_home=%s\nseal_dir=%s\nprotected=(%s)\n%s\n%s\nstep_client\n"
            % (work_home, SEAL, sentinel_guard, "".join(lines[start:end]), "".join(lines[cs:ce])))


def test_step_client_removes_a_foreign_entry(tmp_path):
    """Planted: a fake chattr that exits 1 on a symlink (as e2fsprogs does), a symlink at one client file target and
    a folder at another: exit 0, the sentinel untouched, the targets regular files, chattr +i recorded on them."""
    work = tmp_path / "work"
    claude = work / ".claude"
    claude.mkdir(parents=True)
    sentinel = tmp_path / "sentinel"
    sentinel.write_text("keep\n")
    (claude / "settings.json").symlink_to(sentinel)
    (claude / "CLAUDE.md").mkdir()
    (claude / "CLAUDE.md" / "inside").write_text("x")
    fake = tmp_path / "fake"
    fake.mkdir()
    log = tmp_path / "chattr.log"
    scripts = {
        "chattr": 'for a; do last="$a"; done; echo "$*" >> "%s"; [ -L "$last" ] && exit 1; exit 0' % log,
        "chown": "exit 0",
        "install": ('d=0; while [ $# -gt 0 ]; do case "$1" in -d) d=1; shift ;; -o|-g|-m) shift 2 ;; *) break ;; esac; '
                    'done; if [ "$d" -eq 1 ]; then mkdir -p "$@"; else cp "$1" "$2"; fi'),
    }
    for name, body in scripts.items():
        (fake / name).write_text("#!/bin/sh\n%s\n" % body)
        (fake / name).chmod(0o755)
    env = dict(os.environ, PATH="%s:%s" % (fake, os.environ["PATH"]))
    res = subprocess.run(["bash", "-c", _client_snippet(work, tmp_path / "vault")], capture_output=True, text=True,
                         env=env, timeout=60)
    assert res.returncode == 0, res.stderr + res.stdout
    assert sentinel.read_text() == "keep\n" and not sentinel.is_symlink()
    for name in ("settings.json", "CLAUDE.md", "skills/drafting/SKILL.md"):
        target = claude / name
        assert target.is_file() and not target.is_symlink(), name
        assert target.read_bytes() == (SEAL / "work-claude" / name).read_bytes(), name
        assert "+i %s" % target in log.read_text().splitlines(), name
    assert "a foreign entry at %s" % (claude / "settings.json") in res.stdout
    assert "a foreign entry at %s" % (claude / "CLAUDE.md") in res.stdout


# --------------------------------------------------------------------------- T11: the work user's git identity


def test_setup_dry_run_installs_the_git_identity_of_the_work_user(fakebin):
    for args in (["--dry-run"], ["--dry-run", "--update"]):
        res = run_script(SETUP, args, fakebin)
        assert res.returncode == 0, res.stderr
        joined = "\n".join(commands(res.stdout))
        assert re.search(r"^\+ install -o root -g awb -m 644 \S*/seal/work-gitconfig \S*/\.gitconfig$", joined, re.M), args
        assert re.search(r"^\+ chattr \+i \S*/\.gitconfig", joined, re.M), args


def test_verify_checks_the_git_identity_of_the_work_user(fakebin):
    res = run_script(VERIFY, ["--dry-run"], fakebin)
    assert res.returncode == 0, res.stderr
    checks = [line[6:] for line in res.stdout.splitlines() if line.startswith("CHECK ")]
    assert any("git identity of the work user is the one of the repository" in c for c in checks)
    assert any("git identity of the work user is immutable" in c for c in checks)


def test_the_work_gitconfig_names_the_workbench():
    text = (SEAL / "work-gitconfig").read_text(encoding="utf-8")
    from awb import projects
    assert text == "[user]\n\tname = %s\n\temail = %s\n" % (projects._GIT_NAME, projects._GIT_EMAIL)


# --------------------------------------------------------------------------- F2 G3: the persistence of the work user

WORK_JOBS = SEAL / "work-jobs"


def setup_value(name: str) -> str:
    """The value of a constant at the top of setup.sh, quotes taken off."""
    m = re.search(r'(?m)^%s=("[^"]*"|\S+)' % name, SETUP.read_text(encoding="utf-8"))
    assert m, name
    return m.group(1).strip('"')


def dropin_lines(out: str, path: str) -> list[str]:
    """The lines a dry run writes into PATH, as they land in the file."""
    lines = out.splitlines()
    i = lines.index("+ write %s (mode 644, root:root)" % path)
    body = []
    for line in lines[i + 1:]:
        if not line.startswith("    | "):
            break
        body.append(line[6:])
    return body


def test_setup_dry_run_puts_the_work_users_keys_in_a_root_file_and_checks_sshd_before_the_reload(fakebin):
    """F2 G3. The keys of the work user go to /etc/ssh/authorized_keys/awb (root, 644), a drop-in of sshd points the
    work user alone at that file and closes its block, sshd -t runs before the reload and the old copy in the work
    home goes after it. The owner's ssh is not touched and a deploy leaves all of it alone."""
    match_kw, keys_kw = setup_value("SSHD_MATCH"), setup_value("SSHD_KEYS_FILE")
    assert (match_kw.lower(), keys_kw.lower()) == ("match user", "authorizedkeysfile")
    res = run_script(SETUP, ["--dry-run"], fakebin)
    assert res.returncode == 0, res.stderr
    no_marks(fakebin)
    cmds = commands(res.stdout)
    dropin = "/etc/ssh/sshd_config.d/awb-work-user.conf"
    body = [line for line in dropin_lines(res.stdout, dropin) if not line.startswith("#")]
    assert body == ["%s awb" % match_kw, "    %s /etc/ssh/authorized_keys/%%u" % keys_kw, "Match all"]

    def at(line: str) -> int:
        return cmds.index(line)

    first = at("+ install -d -o root -g root -m 755 /etc/ssh/authorized_keys")
    copy = at("+ install -o root -g root -m 644 %s/.ssh/authorized_keys /etc/ssh/authorized_keys/awb  # if present"
              % OWNER_HOME)
    write = at("+ write %s (mode 644, root:root)" % dropin)
    check = at("+ sshd -t")
    reload = at("+ systemctl try-reload-or-restart ssh.service")
    old = next(i for i, c in enumerate(cmds) if re.match(r"^\+ rm -f -- \S+/\.ssh/authorized_keys$", c))
    assert first < copy < write < check < reload < old
    ssh_lines = [c for c in cmds if "ssh" in c and "awb-build" not in c]
    assert not [c for c in ssh_lines if OWNER_HOME + "/.ssh" in c and c != cmds[copy]]
    assert not [c for c in ssh_lines if re.search(r"(chown|chmod|install -o awb).*\.ssh", c)]
    update = run_script(SETUP, ["--dry-run", "--update"], fakebin)
    assert update.returncode == 0, update.stderr
    assert not [c for c in commands(update.stdout) if "ssh" in c and "awb-build" not in c]


def test_setup_dry_run_turns_the_crontab_of_the_work_user_into_root_timers_and_denies_it_cron(fakebin):
    """F2 G3. Two units and two timers rendered from seal/work-jobs into the unit folder of root, enabled, then
    cron.deny names the work user (appended, so other lines stay) and its crontab goes. The repository carries no kb
    commit script, so the plan says so and the timer runs the script where the crontab ran it."""
    res = run_script(SETUP, ["--dry-run"], fakebin)
    assert res.returncode == 0, res.stderr
    no_marks(fakebin)
    out = res.stdout
    cmds = commands(out)
    units = ["awb-work-update.service", "awb-work-update.timer", "awb-kb-commit.service", "awb-kb-commit.timer"]
    writes = [cmds.index("+ write /etc/systemd/system/%s (mode 644, root:root)" % u) for u in units]
    enables = [cmds.index("+ systemctl enable --now %s" % t) for t in units if t.endswith(".timer")]
    deny = cmds.index("+ append to /etc/cron.deny: awb")
    gone = cmds.index("+ crontab -u awb -r  # if present")
    assert max(writes) < min(enables) and max(enables) < deny < gone
    assert "+ chown root:root /etc/cron.deny" in cmds and "+ chmod 644 /etc/cron.deny" in cmds
    update = dropin_lines(out, "/etc/systemd/system/awb-work-update.service")
    kb = dropin_lines(out, "/etc/systemd/system/awb-kb-commit.service")
    assert not [line for line in update + kb if "@" in line]
    assert re.search(r"(?m)^ExecStart=/bin/sh -c 'mkdir -p (\S+)/\.local/state && exec \1/\.local/bin/claude update "
                     r">>\1/\.local/state/claude-update\.log 2>&1'$", "\n".join(update))
    assert not (WORK_JOBS / "nightly-kb-commit").exists()
    assert "the repository has no seal/work-jobs/nightly-kb-commit" in out
    assert re.search(r"(?m)^ExecStart=\S+/\.local/bin/nightly-kb-commit$", "\n".join(kb))
    for name, at in (("awb-work-update", "01:35:00"), ("awb-kb-commit", "01:40:00")):
        timer = dropin_lines(out, "/etc/systemd/system/%s.timer" % name)
        assert "OnCalendar=*-*-* %s" % at in timer and "Unit=%s.service" % name in timer
    plan = run_script(SETUP, ["--dry-run", "--update"], fakebin)
    assert plan.returncode == 0, plan.stderr
    assert not [c for c in commands(plan.stdout) if "cron" in c or "work-update" in c or "kb-commit" in c]


@pytest.mark.parametrize("name, families", [("awb-work-update", ["AF_UNIX AF_INET AF_INET6 AF_NETLINK"]),
                                            ("awb-kb-commit", ["AF_UNIX"])])
def test_the_nightly_jobs_run_as_the_work_user_in_a_hardened_oneshot(name, families):
    """F2 G3. Planted: a template without one of the hardening lines, with another user or with a log that systemd
    would open as root (StandardOutput=append:) fails."""
    keys = _service_keys(WORK_JOBS / ("%s.service" % name))
    assert keys["Type"] == ["oneshot"]
    assert keys["User"] == ["awb"] and keys["Group"] == ["awb"]
    for key in ("NoNewPrivileges", "PrivateTmp", "PrivateDevices", "ProtectKernelTunables", "ProtectKernelModules",
                "ProtectControlGroups", "RestrictSUIDSGID", "LockPersonality", "RestrictNamespaces"):
        assert keys[key] == ["yes"], key
    assert keys["ProtectSystem"] == ["strict"] and keys["ProtectHome"] == ["read-only"]
    assert keys["ReadWritePaths"] == ["@WORK_HOME@"]
    assert keys["CapabilityBoundingSet"] == [""]
    assert keys["RestrictAddressFamilies"] == families
    assert "StandardOutput" not in keys and "StandardError" not in keys
    timer = (WORK_JOBS / ("%s.timer" % name)).read_text(encoding="utf-8")
    assert "WantedBy=timers.target" in timer and "Persistent" not in timer


def _persist_snippet(tmp: Path) -> str:
    lines = SETUP.read_text(encoding="utf-8").splitlines(keepends=True)
    start = next(i for i, x in enumerate(lines) if x.startswith("# ----") and "printing and the guard" in x)
    end = next(i for i, x in enumerate(lines) if x.startswith("# ----") and x.rstrip().endswith("the steps"))
    ss = next(i for i, x in enumerate(lines) if x.startswith("step_ssh() {"))
    se = next(i for i, x in enumerate(lines) if x.startswith("clear_target() {"))
    consts = "".join(x for x in lines if re.match(r"^(SSHD_MATCH|SSHD_KEYS_FILE|WORK_JOBS|KB_COMMIT_OLD)=", x))
    return ("set -euo pipefail\ndie() { echo \"setup: $*\" >&2; exit 1; }\ndry_run=0\nreal=0\ntest_root=1\n"
            "WORK_USER=awb\nWORK_GROUP=awb\n%sSSH_KEYS_DIR=%s/keys\nSSHD_DROPIN=%s/sshd.d/awb-work-user.conf\n"
            "CRON_DENY=%s/cron.deny\nUNITS_DIR=%s/units\nKB_COMMIT=%s/libexec/nightly-kb-commit\n"
            "owner_home=%s/owner\nwork_home=%s/work\nseal_dir=%s\nprotected=(%s/vault)\n%s\n%s\n"
            % (consts, tmp, tmp, tmp, tmp, tmp, tmp, tmp, SEAL, tmp, "".join(lines[start:end]),
               "".join(lines[ss:se])))


def _persist_run(tmp: Path, step: str, sshd_ok: bool = True, crontab: str | None = None):
    fake = tmp / "fake"
    fake.mkdir(exist_ok=True)
    log = tmp / "calls.log"
    scripts = {
        "sshd": 'echo "sshd $*" >> "%s"; exit %d' % (log, 0 if sshd_ok else 1),
        "systemctl": 'echo "systemctl $*" >> "%s"' % log,
        "chown": "exit 0",
        "crontab": ('echo "crontab $*" >> "%s"; case "$*" in *-l*) [ -f "%s" ] && cat "%s" && exit 0; '
                    'echo "no crontab for awb" >&2; exit 1 ;; *-r*) rm -f "%s" ;; esac' % ((log,) + (tmp / "tab",) * 3)),
        "install": ('d=0; while [ $# -gt 0 ]; do case "$1" in -d) d=1; shift ;; -o|-g|-m) shift 2 ;; *) break ;; esac; '
                    'done; if [ "$d" -eq 1 ]; then mkdir -p "$@"; else cp "$1" "$2"; fi'),
    }
    for name, body in scripts.items():
        (fake / name).write_text("#!/bin/sh\n%s\n" % body)
        (fake / name).chmod(0o755)
    if crontab is not None:
        (tmp / "tab").write_text(crontab)
    (tmp / "units").mkdir(exist_ok=True)
    env = dict(os.environ, PATH="%s:%s" % (fake, os.environ["PATH"]))
    res = subprocess.run(["bash", "-c", _persist_snippet(tmp) + step], capture_output=True, text=True, env=env,
                         timeout=60)
    return res, (log.read_text().splitlines() if log.exists() else [])


def test_step_ssh_for_real_moves_the_keys_and_takes_the_drop_in_back_when_sshd_refuses_it(tmp_path):
    """F2 G3, with fakes of sshd and systemctl. sshd -t passes: the key file is the owner's, the drop-in in place,
    sshd reloaded, the old copy gone. sshd -t fails: the drop-in is removed again, no reload, the old copy stays."""
    (tmp_path / "owner" / ".ssh").mkdir(parents=True)
    (tmp_path / "owner" / ".ssh" / "authorized_keys").write_text("ssh-ed25519 AAAAplanted owner\n")
    old = tmp_path / "work" / ".ssh" / "authorized_keys"
    old.parent.mkdir(parents=True)
    old.write_text("ssh-ed25519 AAAAplanted session\n")
    res, calls = _persist_run(tmp_path, "step_ssh\n", sshd_ok=False)
    assert res.returncode == 1 and "sshd -t refused" in res.stderr
    assert not (tmp_path / "sshd.d" / "awb-work-user.conf").exists()
    assert calls == ["sshd -t"] and old.exists()
    res, calls = _persist_run(tmp_path, "step_ssh\n")
    assert res.returncode == 0, res.stderr + res.stdout
    assert (tmp_path / "keys" / "awb").read_text() == "ssh-ed25519 AAAAplanted owner\n"
    text = (tmp_path / "sshd.d" / "awb-work-user.conf").read_text()
    assert "%s awb\n" % setup_value("SSHD_MATCH") in text
    assert "%s %s/keys/%%u\n" % (setup_value("SSHD_KEYS_FILE"), tmp_path) in text
    assert calls[-2:] == ["sshd -t", "systemctl try-reload-or-restart ssh.service"]
    assert not old.exists()


def test_step_work_jobs_for_real_keeps_the_other_lines_of_cron_deny_and_drops_the_crontab(tmp_path):
    """F2 G3, with fakes of crontab and systemctl. A cron.deny whose last line has no newline keeps it as its own
    line; a crontab line other than the two jobs is named before the crontab goes; a second run changes nothing."""
    (tmp_path / "cron.deny").write_text("guest\nnobody")
    tab = "35 1 * * * $HOME/.local/bin/claude update >> $HOME/.local/state/claude-update.log 2>&1\n" \
          "40 1 * * * $HOME/.local/bin/nightly-kb-commit\n*/5 * * * * $HOME/.cache/planted\n"
    res, calls = _persist_run(tmp_path, "step_work_jobs\n", crontab=tab)
    assert res.returncode == 0, res.stderr + res.stdout
    assert (tmp_path / "cron.deny").read_text() == "guest\nnobody\nawb\n"
    assert "a line the timers do not run: */5 * * * * $HOME/.cache/planted" in res.stdout
    assert "claude update" not in "".join(x for x in res.stdout.splitlines() if "warning" in x)
    assert "crontab -u awb -r" in calls and not (tmp_path / "tab").exists()
    assert {"systemctl enable --now awb-work-update.timer", "systemctl enable --now awb-kb-commit.timer"} <= set(calls)
    for unit in ("awb-work-update.service", "awb-work-update.timer", "awb-kb-commit.service", "awb-kb-commit.timer"):
        text = (tmp_path / "units" / unit).read_text()
        assert "@" not in text, unit
        assert "%s/work" % tmp_path in text or unit.endswith(".timer"), unit
    res, calls = _persist_run(tmp_path, "step_work_jobs\n")
    assert res.returncode == 0, res.stderr + res.stdout
    assert (tmp_path / "cron.deny").read_text() == "guest\nnobody\nawb\n"
    assert "= crontab -u awb -r  # no crontab" in res.stdout


def test_verify_dry_run_lists_the_persistence_checks_of_the_work_user(fakebin):
    """F2 G3: the root owned key file out of the work user's reach, the file sshd -T names for it, no key file in the
    work home, cron.deny, crontab -l refused and no crontab, both timers enabled and their services as the work user."""
    res = run_script(VERIFY, ["--dry-run"], fakebin)
    assert res.returncode == 0, res.stderr
    no_marks(fakebin)
    checks = [line[6:] for line in res.stdout.splitlines() if line.startswith("CHECK ")]
    for must in ("the work user's ssh key file /etc/ssh/authorized_keys/awb is owned by root",
                 "the work user cannot write its ssh key file",
                 "the work user cannot write the folder of its ssh key file",
                 "sshd reads the work user's keys from /etc/ssh/authorized_keys alone (sshd -T -C user=awb)",
                 "the work home holds no .ssh/authorized_keys",
                 "/etc/cron.deny names the work user",
                 "crontab -l as the work user is refused",
                 "the work user has no crontab",
                 "the timer awb-work-update.timer is enabled", "the timer awb-kb-commit.timer is enabled",
                 "awb-work-update.service runs as the work user", "awb-kb-commit.service runs as the work user"):
        assert must in checks, must
    text = VERIFY.read_text(encoding="utf-8")
    assert "sshd -T -C \"user=$WORK_USER," in text
    assert re.search(r"(?m)^SSHD_KEYS_LINE='authorizedkeysfile /etc/ssh/authorized_keys/%u'$", text)
    assert 'grep -qi "not allowed"' in text


def test_the_host_file_names_the_front_socket_and_the_tunnel_user(fakebin):
    """T9 step 1: the host file carries front_socket and cloudflared_user (kept when set, else the defaults) and
    keeps an owner_host it had; install.sh stops without them."""
    res = run_script(SETUP, ["--dry-run"], fakebin)
    assert res.returncode == 0, res.stderr
    lines = res.stdout.splitlines()
    assert any(re.fullmatch(r"    \| front_socket = /run/[a-z0-9._-]+/[a-z0-9._-]+\.sock", x) for x in lines)
    assert any(re.fullmatch(r"    \| cloudflared_user = [a-z_][a-z0-9_-]*", x) for x in lines)
    text = SETUP.read_text(encoding="utf-8")
    assert 'owner_host=$(kept_conf owner_host "")' in text and "kept_conf front_socket /run/awb-web/front.sock" in text


def test_the_dry_run_holds_cloudflared_and_terraform_in_the_seal_and_in_a_deploy(fakebin):
    """T9 step 4 (RT-26): both packages are held with an info line, in the first seal and in every update."""
    for args in (["--dry-run"], ["--update", "--dry-run"]):
        res = run_script(SETUP, args, fakebin)
        assert res.returncode == 0, res.stderr
        lines = res.stdout.splitlines()
        assert "+ apt-mark hold cloudflared" in lines and "+ apt-mark hold terraform" in lines, args
        assert any(x.startswith("# cloudflared and terraform are held (RT-26)") for x in lines)
    res = run_script(SETUP, ["--update", "--units-only", "--dry-run"], fakebin)
    assert "apt-mark" not in res.stdout


def test_setup_update_mode_skips_the_seal_steps_holds_the_packages_hooks_the_kb_loads_the_egress_filter_adds_the_build_side_and_its_failure_oneshot(fakebin):
    """Replaces test_setup_update_mode_skips_the_seal_steps_holds_the_packages_hooks_the_kb_loads_the_egress_filter_and_adds_the_build_side
    (SB5 of the self-build: the line of the runner's unit no longer says the runner comes with SB3; the update
    writes the oneshot awb-task-failed@.service of a failed task unit and reloads systemd), which replaced
    test_setup_update_mode_skips_the_seal_steps_holds_the_packages_hooks_the_kb_and_loads_the_egress_filter
    (SB1 of the self-build: the update adds the build side, step_build after step_managed, with its own user, mount,
    pip and git lines on the build root, and installs awb-buildd.service without enabling it), which replaced
    test_setup_update_mode_skips_the_seal_steps_holds_the_packages_and_hooks_the_kb (the egress filter of
    the work user: the update installs seal/*.timer with the units and runs step_egress after step_units), which
    replaced test_setup_update_mode_skips_the_seal_steps_and_holds_the_packages (TM0 item 7: the update installs
    the commit gate of the knowledge base, step_kb_hooks after step_holds), which replaced
    test_setup_update_mode_skips_the_seal_steps (T9 step 4: the update holds cloudflared and terraform,
    step_holds after step_needrestart). Planted: the full mode prints the chown of the shared tree, the users and the moves (so the checks can fail);
    the update mode prints none of them, no mount, no rm -rf of src, and prints the .pth line, the exchange, every
    seal/*.service and the needrestart rule. A grep over the script finds no rm -rf of src."""
    full = commands(run_script(SETUP, ["--dry-run"], fakebin).stdout)

    def seal_steps(cmds):
        return [c for c in cmds if re.match(r"^[+=] (chown|chmod|find)\b.*tcp-shared", c)
                or re.match(r"^[+=] (useradd|groupadd|usermod|gpasswd)\b", c) or c.startswith("+ mv -- ")
                or re.match(r"^[+=] mount\b", c) or "authorized_keys" in c
                or re.search(r"rm -rf -- /opt/tcp-awb/src( |$)", c)]

    assert seal_steps([c for c in full if "awb-build" not in c])
    res = run_script(SETUP, ["--update", "--dry-run"], fakebin)
    assert res.returncode == 0, res.stderr
    no_marks(fakebin)
    cmds = commands(res.stdout)
    assert [c for c in seal_steps(cmds) if "awb-build" not in c] == []
    assert seal_steps(cmds) == ["+ groupadd --system awb-build",
                                "+ useradd --system --gid awb-build -M -d /srv/awb-build/home --shell /bin/bash "
                                "--comment Architect\\ Workbench\\ build awb-build",
                                "+ mount /srv/awb-build"]
    joined = "\n".join(cmds)
    assert re.search(r"^\+ write /opt/tcp-awb/venv/lib/python3[^/]*/site-packages/awb\.pth ", joined, re.M)
    assert "    | /opt/tcp-awb/src" in res.stdout.splitlines()
    assert "+ exchange /opt/tcp-awb/src.next /opt/tcp-awb/src  # renameat2 RENAME_EXCHANGE, one call" in cmds
    for unit in sorted(p.name for p in list(SEAL.glob("*.service")) + list(SEAL.glob("*.timer"))):
        assert "+ write /etc/systemd/system/%s (mode 644, root:root)" % unit in cmds
        assert ("+ systemctl enable --now %s" % unit in cmds) is (unit != "awb-buildd.service"), unit
    assert "# awb-buildd.service is installed, not enabled: the steering session starts it by hand for a trial task and enables it on his word" in res.stdout.splitlines()
    assert "+ write /etc/systemd/system/awb-task-failed@.service (mode 644, root:root)" in cmds
    assert "    | ExecStart=/usr/local/bin/awb build failed %i" in res.stdout.splitlines()
    assert "+ write /etc/needrestart/conf.d/awb.conf (mode 644, root:root)" in cmds
    assert [c for c in cmds if " mv -T " in c or c.startswith("+ mv -T")] == [
        "+ mv -T -- /opt/tcp-awb/src.next /opt/tcp-awb/releases/pre-deploy  # if /opt/tcp-awb/src.next is the plain "
        "folder of before T1, else rm -f"]
    assert [c for c in cmds if "pip" in c] == [c for c in cmds if c.startswith("+ /srv/awb-build/venv/bin/python -I -m pip ")]
    gits = [c for c in cmds if not c.startswith("+ write ") and re.search(r"(^|[ |;&])git ", c)]
    assert len(gits) == 2 and all("/srv/awb-build/main.git" in c for c in gits)
    text = SETUP.read_text(encoding="utf-8")
    assert not re.search(r'rm -rf[^\n]*\$OPT/src["\s]', text)
    assert re.search(r"^update_steps\(\) \{\n(    #.*\n)?    if \[ \"\$units_only\" -eq 1 \]; then\n        step_units\n"
                     r"        return 0\n    fi\n    step_code\n    step_conf\n    step_units\n    step_egress\n    step_needrestart\n"
                     r"    step_holds\n    step_kb_hooks\n    step_client\n    step_managed\n    step_build\n    step_mirrors\n\}", text,
                     re.M)
    # --units-only: the units and daemon-reload, nothing else
    res = run_script(SETUP, ["--update", "--units-only", "--dry-run"], fakebin)
    assert res.returncode == 0, res.stderr
    only = commands(res.stdout)
    assert only == ["+ write /etc/systemd/system/%s (mode 644, root:root)" % p.name
                    for p in sorted(SEAL.glob("*.service")) + sorted(SEAL.glob("*.timer"))] + ["+ systemctl daemon-reload"]
    assert "+ /usr/local/bin/awb egress load" in cmds and "+ systemctl enable --now awb-egress.timer" in cmds
    assert run_script(SETUP, ["--units-only", "--dry-run"], fakebin).returncode == 1


def test_the_seal_installs_the_commit_gate_of_the_knowledge_base_and_verify_checks_it(fakebin, tmp_path):
    """TM0 item 7: the first seal and every update run awb gate --install --kb as the work user on its tcp-kb;
    verify.sh checks the three hooks by their mark. Planted: a knowledge base without its commit-msg hook fails."""
    for args in (["--dry-run"], ["--update", "--dry-run"]):
        res = run_script(SETUP, args, fakebin)
        assert res.returncode == 0, res.stderr
        lines = [x for x in res.stdout.splitlines() if " gate --install --kb --repo " in x]
        assert len(lines) == 1 and lines[0].startswith("+ runuser -u awb -- env HOME="), args
        assert lines[0].endswith("/tcp-kb")
    res = run_script(SETUP, ["--update", "--units-only", "--dry-run"], fakebin)
    assert "gate --install" not in res.stdout
    res = run_script(VERIFY, ["--dry-run"], fakebin)
    checks = [line[6:] for line in res.stdout.splitlines() if line.startswith("CHECK ")]
    for h in ("pre-commit", "commit-msg", "pre-push"):
        assert "the knowledge base carries the %s hook of the commit gate" % h in checks
    from awb import gate

    kb = tmp_path / "tcp-kb"
    subprocess.run(["git", "init", "-q", str(kb)], check=True)
    gate.install_hook(kb, kb=True)
    text = VERIFY.read_text(encoding="utf-8")
    mark = re.search(r'grep -qF -- "([^"]+)" "\$kb/\.git/hooks/\$h"', text).group(1)
    assert mark == gate.HOOK_MARK

    def present(h):
        return subprocess.run(["grep", "-qF", "--", mark, str(kb / ".git" / "hooks" / h)]).returncode == 0

    assert all(present(h) for h in gate.HOOK_NAMES)
    (kb / ".git" / "hooks" / "commit-msg").unlink()
    assert not present("commit-msg")


# --------------------------------------------------------------------------- SB1: the build side of the self-build

BUILD_UNIT = SEAL / "awb-buildd.service"
BUILD_SETTINGS = SEAL / "build-claude" / "settings.json"
HOOKS_BUILD = REPO / "hooks-build"


def test_setup_plans_the_build_side_and_its_failure_oneshot_in_the_seal_and_in_every_update(fakebin, tmp_path):
    """Replaces test_setup_plans_the_build_side_in_the_seal_and_in_every_update (SB5: the runner's unit line no longer
    says it comes with SB3; the plan writes the oneshot awb-task-failed@.service as the owner with awb build failed).
    SB1 item 1: the dry run of the first seal and of an update names the build user, the image, the fstab line, the
    mount, the folders with their owners and modes, the venv, the home template, the pinned client, the host file's
    two keys and the runner's unit, installed and not enabled; in that order where it matters (the user before its
    folders, the mount before anything on the image). --units-only touches none of it. Planted: the plan of a
    first seal without the build step (cut from a copy of the script) fails every check."""
    def problems(out: str) -> list[str]:
        lines = out.splitlines()
        cmds = commands(out)
        want = [
            "+ groupadd --system awb-build",
            "+ fallocate -l 20G /var/lib/awb-build.img  # if missing, when /var/lib keeps 10 GB free after it",
            "+ mkfs.ext4 -q -F -L awb-build -m 0 /var/lib/awb-build.img  # if missing",
            "+ append to /etc/fstab: /var/lib/awb-build.img /srv/awb-build ext4 loop,nofail,nodev,nosuid,noatime 0 0",
            "+ mount /srv/awb-build",
            "+ install -d -o %s -g %d -m 755 /srv/awb-build/main.git" % (USER, os.getgid()),
            "+ install -d -o awb-build -g awb-build -m 755 /srv/awb-build/wt",
            "+ install -d -o %s -g %d -m 700 /srv/awb-build/runs" % (USER, os.getgid()),
            "+ install -d -o awb-build -g awb-build -m 700 /srv/awb-build/home",
            "+ python3 -m venv --system-site-packages /srv/awb-build/venv",
            "+ install -o root -g root -m 644 %s /srv/awb-build/home-template/.claude/settings.json" % BUILD_SETTINGS,
            "+ chattr +i /srv/awb-build/home-template  # if present",
            "+ install -d -o root -g root -m 755 /opt/claude-build /opt/claude-build/versions /opt/claude-build/bin",
            "+ write /opt/claude-build/bin/claude (mode 755, root:root)",
            "+ write /etc/systemd/system/awb-buildd.service (mode 644, root:root)",
            "+ write /etc/systemd/system/awb-task-failed@.service (mode 644, root:root)",
        ]
        out_ = ["missing: %s" % w for w in want if w not in cmds]
        if not any(c.startswith("+ useradd --system --gid awb-build -M -d /srv/awb-build/home --shell /bin/bash ")
                   for c in cmds):
            out_.append("missing: useradd")
        if not any(c.startswith("+ runuser -u %s -- env HOME=%s git init --bare --quiet -b main "
                                "/srv/awb-build/main.git" % (USER, OWNER_HOME)) for c in cmds):
            out_.append("missing: git init")
        if not any(".local/share/claude/versions/2.1.296 /opt/claude-build/versions/2.1.296.partial  # if missing; "
                   "kept only with sha256 " in c for c in cmds):
            out_.append("missing: the client copy")
        for key in ("build_user = awb-build", "build_root = /srv/awb-build",
                    "exec /opt/claude-build/versions/2.1.296 \"$@\"", "DISABLE_AUTOUPDATER=1", "Type=oneshot",
                    "User=%s" % USER, "ExecStart=/usr/local/bin/awb build failed %i"):
            if "    | " + key not in lines:
                out_.append("missing: %s" % key)
        if "+ systemctl enable --now awb-buildd.service" in cmds:
            out_.append("the runner is enabled")
        if "+ systemctl enable --now awb-buildd.service" in cmds or "# awb-buildd.service is installed, not enabled: the steering session starts it by hand for a trial task and enables it on his word" not in lines:
            out_.append("the runner's unit line")
        pos = {w: next((i for i, c in enumerate(cmds) if c.startswith(w)), None) for w in
               ("+ groupadd --system awb-build", "+ mount /srv/awb-build", "+ install -d -o awb-build")}
        if None not in pos.values() and not (pos["+ groupadd --system awb-build"] < pos["+ mount /srv/awb-build"]
                                             < pos["+ install -d -o awb-build"]):
            out_.append("the order")
        return out_

    for args in (["--dry-run"], ["--update", "--dry-run"]):
        res = run_script(SETUP, args, fakebin)
        assert res.returncode == 0, res.stderr
        no_marks(fakebin)
        assert problems(res.stdout) == [], args
    res = run_script(SETUP, ["--update", "--units-only", "--dry-run"], fakebin)
    assert "awb-build" not in "\n".join(c for c in commands(res.stdout) if "awb-buildd.service" not in c)
    # planted: the build step cut out of a copy of the seal
    copy = tmp_path / "repo"
    shutil.copytree(SEAL, copy / "seal")
    for name in ("pyproject.toml", "CLAUDE.md"):
        shutil.copy(REPO / name, copy / name)
    planted = copy / "seal" / "setup.sh"
    planted.write_text(SETUP.read_text(encoding="utf-8").replace("    step_build\n", ""), encoding="utf-8")
    res = run_script(planted, ["--dry-run"], fakebin)
    assert res.returncode == 0, res.stderr
    found = problems(res.stdout)
    assert "missing: + groupadd --system awb-build" in found and "missing: git init" in found


def _build_snippet(tmp: Path, tail: str) -> str:
    """The printing helpers and the build section of setup.sh, run for real against a throw-away image path, root and
    fstab: every command that would change the host is a fake on PATH."""
    lines = SETUP.read_text(encoding="utf-8").splitlines(keepends=True)
    start = next(i for i, x in enumerate(lines) if x.startswith("# ----") and "printing and the guard" in x)
    end = next(i for i, x in enumerate(lines) if x.startswith("# ----") and x.rstrip().endswith("the steps"))
    bs = next(i for i, x in enumerate(lines) if x.startswith("# ----") and "the build side (self-build SB1)" in x)
    be = next(i for i, x in enumerate(lines) if x.startswith("check_mirror() {"))
    return ("set -euo pipefail\ndie() { echo \"setup: $*\" >&2; exit 1; }\ndry_run=0\nreal=1\nupdate=1\ntest_root=1\n"
            "OPT=/opt/tcp-awb\n"
            "BUILD_IMAGE=%s\nBUILD_ROOT=%s\nBUILD_SIZE_GB=20\nBUILD_FLOOR_GB=10\nFSTAB=%s\n"
            "BUILD_FSTAB=\"$BUILD_IMAGE $BUILD_ROOT ext4 loop,nofail 0 0\"\nprotected=(%s)\n%s\n%s\n%s\n"
            % (tmp / "img", tmp / "root", tmp / "fstab", tmp / "vault", "".join(lines[start:end]),
               "".join(lines[bs:be]), tail))


def _run_build(tmp: Path, tail: str, df: int = 100, mounted: bool = True, mkfs_ok: bool = True):
    fake = tmp / "fake"
    fake.mkdir(exist_ok=True)
    log = tmp / "calls.log"
    if log.exists():
        log.unlink()
    scripts = {
        "df": 'echo Avail; echo " %d"' % df,
        "fallocate": 'for a; do last="$a"; done; : > "$last"; echo "fallocate $*" >> "%s"' % log,
        "mkfs.ext4": 'echo "mkfs $*" >> "%s"; exit %d' % (log, 0 if mkfs_ok else 1),
        "mount": 'echo "mount $*" >> "%s"' % log,
        "mountpoint": "exit %d" % (0 if mounted else 1),
        "chown": 'echo "chown $*" >> "%s"' % log,
    }
    for name, body in scripts.items():
        (fake / name).write_text("#!/bin/sh\n%s\n" % body)
        (fake / name).chmod(0o755)
    env = dict(os.environ, PATH="%s:%s" % (fake, os.environ["PATH"]))
    res = subprocess.run(["bash", "-c", _build_snippet(tmp, tail)], capture_output=True, text=True, env=env, timeout=60)
    return res, (log.read_text() if log.exists() else "")


def test_the_build_image_is_made_once_and_nothing_lands_on_the_root_filesystem_unmounted(tmp_path):
    """SB1 item 1, D-S17: the image is allocated and formatted only when it is missing and the filesystem keeps its
    floor; an existing image is never formatted again; the fstab line goes in once; when the mount does not hold, no
    build folder is made (it would land on the root filesystem). Planted: an existing image, too little space, a
    mount that does not hold, a failing mkfs, two runs."""
    stub = ("step_build_user() { :; }\nbuild_client() { :; }\nfind_build_source() { :; }\n"
            "build_folders() { echo FOLDERS; }\nbuild_venv() { :; }\nbuild_template() { :; }\nstep_build\n")
    (tmp_path / "img").write_text("an image\n")
    res, calls = _run_build(tmp_path, stub)
    assert res.returncode == 0, res.stderr
    assert "mkfs" not in calls and "fallocate" not in calls and "FOLDERS" in res.stdout
    assert "# already there, never formatted again" in res.stdout and "  # already mounted" in res.stdout
    res, _ = _run_build(tmp_path, stub)
    assert (tmp_path / "fstab").read_text().count(str(tmp_path / "img")) == 1
    (tmp_path / "img").unlink()
    res, calls = _run_build(tmp_path, stub, df=25)
    assert res.returncode == 0 and "fallocate" not in calls and "FOLDERS" not in res.stdout
    assert "waits for the next run" in res.stdout and not (tmp_path / "img").exists()
    res, calls = _run_build(tmp_path, stub, mounted=False)
    assert res.returncode == 0 and "fallocate -l 20G" in calls and "mkfs -q -F -L awb-build -m 0" in calls
    assert "FOLDERS" not in res.stdout and "is not mounted" in res.stdout and "chown root:root" not in calls
    (tmp_path / "img").unlink()
    res, calls = _run_build(tmp_path, stub, mkfs_ok=False)
    assert res.returncode == 0 and not (tmp_path / "img").exists() and "FOLDERS" not in res.stdout


@not_as_build_user
def test_setup_plans_the_build_side_against_a_host_that_has_it(fakebin):
    """D-SB1-11: with AWB_SETUP_BUILD_HOST=present the probes of the build side answer "present", so the dry run of
    the seal and of an update prints the group, the user and the suite's venv as "=" lines and no groupadd, useradd,
    usermod, gpasswd or venv to make; with fresh the same lines are "+". The variable goes with --dry-run only, and
    a value other than fresh or present is refused."""
    present = ["= groupadd --system awb-build  # the group exists",
               "= useradd --system --gid awb-build -M -d /srv/awb-build/home --shell /bin/bash awb-build  # the user exists",
               "= python3 -m venv --system-site-packages /srv/awb-build/venv  # already there"]
    for args in (["--dry-run"], ["--update", "--dry-run"]):
        res = run_script(SETUP, args, fakebin, build_host="present")
        assert res.returncode == 0, res.stderr
        no_marks(fakebin)
        cmds = commands(res.stdout)
        for line in present:
            assert line in cmds, (args, line)
        assert not [c for c in cmds if re.match(r"^\+ (groupadd|useradd|usermod|gpasswd)\b.*awb-build", c)], args
        assert "+ python3 -m venv --system-site-packages /srv/awb-build/venv" not in cmds, args
        fresh = commands(run_script(SETUP, args, fakebin).stdout)
        assert "+ groupadd --system awb-build" in fresh and not [c for c in fresh if c in present], args
        assert "+ python3 -m venv --system-site-packages /srv/awb-build/venv" in fresh, args
    res = run_script(SETUP, ["--dry-run"], fakebin, build_host="maybe")
    assert res.returncode == 1 and "AWB_SETUP_BUILD_HOST is fresh or present" in res.stderr
    res = run_script(SETUP, ["--update"], fakebin, sudo_user=None, build_host="fresh")
    assert res.returncode == 1 and "AWB_SETUP_BUILD_HOST goes with --dry-run only" in res.stderr
    no_marks(fakebin)


def _template_check(tmp_path: Path, gitconfig: str) -> int:
    """Runs the check of verify.sh that the home template carries the build settings, CLAUDE.md and the owner's
    identity, taken from the script as it is, against a template in tmp_path whose .gitconfig is GITCONFIG."""
    text = VERIFY.read_text(encoding="utf-8")
    m = re.search(r'allowed "the home template carries the build settings and CLAUDE\.md of the repository and the '
                  r"owner's identity\" \\\n\s+sh -c '([^']+)' \\\n\s+verify ", text)
    assert m, "the check is not found in verify.sh"
    seal, repo, t = tmp_path / "repo" / "seal", tmp_path / "repo", tmp_path / "template"
    (seal / "build-claude").mkdir(parents=True)
    (t / ".claude").mkdir(parents=True)
    for f in (seal / "build-claude" / "settings.json", t / ".claude" / "settings.json"):
        f.write_text('{"x": 1}\n', encoding="utf-8")
    for f in (repo / "CLAUDE.md", t / ".claude" / "CLAUDE.md"):
        f.write_text("# rules\n", encoding="utf-8")
    (t / ".gitconfig").write_text(gitconfig, encoding="utf-8")
    return subprocess.run(["sh", "-c", m.group(1), "verify", str(seal), str(repo), str(t)], timeout=30).returncode


@pytest.mark.parametrize("gitconfig, ok", [
    ("# written by seal/setup.sh: a build task commits as the owner (D-S16); no safe.directory\n"
     "[user]\n\tname = A Name\n\temail = someone@example.org\n", True),
    ("[user]\n\tname = A Name\n\temail = someone@example.org\n", True),
    ("[user]\n\tname = A Name\n\temail = someone@example.org\n[safe]\n\tdirectory = *\n", False),
    ("[user]\n\tname = A Name\n\temail = someone@example.org\n  [Safe]\n", False),
    ("[user]\n\tname = A Name\n\temail = someone@example.org\n\tdirectory = /srv\n", False),
    ("[user]\n\tname = A Name\n", False),
])
def test_verify_checks_the_template_identity_without_a_safe_directory(tmp_path, gitconfig, ok):
    """SB1: the template's .gitconfig carries the comment "no safe.directory" (seal/setup.sh build_identity); the check
    of verify.sh refused any line with the word safe and failed on the sealed host. It refuses a [safe] section or a
    directory = line instead. Planted: a [safe] directory = * line, a bare [Safe] and a missing email fail."""
    assert (_template_check(tmp_path, gitconfig) == 0) is ok


@pytest.mark.parametrize("value, plain", [("A Name", True), ("someone@example.org", True), ("", False),
                                          ('a "quoted" name', False), ("a\\name", False), ("x; y", False),
                                          ("x # y", False), ("[user]", False), ("line\nbreak", False)])
def test_the_owner_identity_goes_into_the_template_only_as_plain_values(tmp_path, value, plain):
    """D-S16: the template's .gitconfig takes the owner's name and mail as they are, so a value git config would read
    differently (a quote, a backslash, a comment sign, a bracket, a line break) is refused and the file stays."""
    script = _build_snippet(tmp_path, 'if plain_value "$X"; then echo plain; else echo refused; fi')
    res = subprocess.run(["bash", "-c", script], capture_output=True, text=True, timeout=30, env=dict(os.environ, X=value))
    assert res.stdout.strip() == ("plain" if plain else "refused"), res.stderr


def test_the_build_runner_unit_runs_as_the_owner_and_may_call_sudo():
    """SB1 item 2: User=@OWNER@, Group=awb, NoNewPrivileges=no (sudo -n systemd-run would be refused with it),
    UMask=0022, the installed command, Restart=on-failure, the build root mounted first."""
    keys = _service_keys(BUILD_UNIT)
    assert keys["User"] == ["@OWNER@"] and keys["Group"] == ["awb"]
    assert keys["NoNewPrivileges"] == ["no"] and keys["UMask"] == ["0022"]
    assert keys["ExecStart"] == ["/usr/local/bin/awb build serve"] and keys["Restart"] == ["on-failure"]
    assert "\nRequiresMountsFor=/srv/awb-build\n" in BUILD_UNIT.read_text(encoding="utf-8")
    assert "INSTALL_ONLY=\"awb-buildd.service\"" in SETUP.read_text(encoding="utf-8")


def test_the_build_settings_deny_the_network_and_the_connectors_and_keep_three_hooks():
    """SB1 item 1: the build settings deny mcp__*, WebFetch and WebSearch and every tool the work user's settings
    deny, switch auto memory and the commit attribution off, and run pre-tool, pre-write and post-write through the
    installed command with the matchers of the work user's settings (the managed drop-in runs the same commands,
    which the client runs once)."""
    data = json.loads(BUILD_SETTINGS.read_text(encoding="utf-8"))
    work = json.loads(WORK_SETTINGS.read_text(encoding="utf-8"))
    deny = data["permissions"]["deny"]
    assert {"mcp__*", "WebFetch", "WebSearch"} <= set(deny) and set(hooks.DENIED_TOOLS) <= set(deny)
    assert data["autoMemoryEnabled"] is False and data["attribution"] == {"commit": "", "pr": ""}
    assert data["env"]["DISABLE_AUTOUPDATER"] == "1"
    assert set(data["hooks"]) == {"PreToolUse", "PostToolUse"}
    assert data["hooks"]["PreToolUse"] == work["hooks"]["PreToolUse"]
    assert data["hooks"]["PostToolUse"] == work["hooks"]["PostToolUse"]
    commands_ = [h["command"] for entries in data["hooks"].values() for e in entries for h in e["hooks"]]
    assert sorted(commands_) == ["/usr/local/bin/awb hook %s" % n for n in sorted(hooks.BUILD_HOOKS)]


def _hook_copy(tmp: Path) -> Path:
    """hooks-build with the installed interpreter's path replaced by this suite's interpreter."""
    out = tmp / "hooks-build"
    out.mkdir()
    for name in ("pre-commit", "commit-msg"):
        text = (HOOKS_BUILD / name).read_text(encoding="utf-8")
        assert text.count("python=/opt/tcp-awb/venv/bin/python3\n") == 1
        (out / name).write_text(text.replace("python=/opt/tcp-awb/venv/bin/python3\n", "python=%s\n" % sys.executable))
        (out / name).chmod(0o755)
    return out


def test_the_build_hooks_run_the_installed_gate_without_the_register(tmp_path):
    """SB1 item 1: the commit hooks of a build clone run `awb gate --selftest --no-register`, then the staged files
    or the message, by the interpreter's absolute path, never a .venv of the clone. Planted: a staged secret and a
    co-author line are refused; a clean commit passes; a .venv/bin/python planted in the clone is never run."""
    for name in ("pre-commit", "commit-msg"):
        text = (HOOKS_BUILD / name).read_text(encoding="utf-8")
        assert os.access(HOOKS_BUILD / name, os.X_OK) and text.startswith("#!/bin/sh\n")
        code_lines = [x for x in text.splitlines() if not x.startswith("#")]
        assert "python=/opt/tcp-awb/venv/bin/python3" in code_lines and not [x for x in code_lines if ".venv" in x]
        assert "--no-register" in text and "--selftest --no-register" in text and '-I -B -m awb gate' in text
    hooks_dir = _hook_copy(tmp_path)
    repo = tmp_path / "clone"
    env = dict(os.environ, GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@example.org", GIT_COMMITTER_NAME="t",
               GIT_COMMITTER_EMAIL="t@example.org")
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    subprocess.run(["git", "-C", str(repo), "config", "core.hooksPath", str(hooks_dir)], check=True)
    trap = repo / ".venv" / "bin" / "python"
    trap.parent.mkdir(parents=True)
    trap.write_text("#!/bin/sh\ntouch %s\nexit 0\n" % (tmp_path / "trap-ran"))
    trap.chmod(0o755)
    (repo / ".gitignore").write_text(".venv\n")

    def commit_(name: str, content: bytes, message: str) -> subprocess.CompletedProcess:
        (repo / name).write_bytes(content)
        subprocess.run(["git", "-C", str(repo), "add", name, ".gitignore"], check=True)
        return subprocess.run(["git", "-C", str(repo), "commit", "-q", "-m", message], capture_output=True, text=True,
                              env=env, timeout=120)

    res = commit_("ok.py", b"X = 1\n", "a clean commit")
    assert res.returncode == 0, res.stderr
    res = commit_("leak.py", gate._selftest_cases()["secret"], "a secret")
    assert res.returncode != 0 and "secret" in res.stdout + res.stderr
    subprocess.run(["git", "-C", str(repo), "rm", "-q", "--cached", "leak.py"], check=True)
    res = commit_("ok2.py", b"Y = 2\n", "a change\n\nCo-Authored-By: Claude <noreply@" + "anthropic.com>")
    assert res.returncode != 0 and "attribution" in res.stdout + res.stderr
    assert not (tmp_path / "trap-ran").exists()


def test_verify_lists_the_build_checks_and_reads_the_pin_of_setup(fakebin):
    """SB1 item 4: the dry run of verify.sh names the build side and the probes of N1 to N5 and N8; verify.sh reads
    the client's version and digest from setup.sh with patterns that match it; setup.sh and awb/config.py name the
    same build root; a probe of the unit counts only after the unit was shown to run (a unit that never starts would
    make every denied probe pass)."""
    res = run_script(VERIFY, ["--dry-run"], fakebin)
    assert res.returncode == 0, res.stderr
    checks = [line[6:] for line in res.stdout.splitlines() if line.startswith("CHECK ")]
    for want in ("the build user is in no group but its own", "the build image is 20 GB",
                 "the build user cannot see the vault", "the build user cannot connect to the check socket",
                 "the build user cannot run sudo -n true", "the build user cannot push into main.git",
                 "the build user cannot list the work user's home",
                 "the probe unit runs as the build user with the properties of a task",
                 "the probe unit reaches the model endpoint (api.anthropic.com)",
                 "the probe unit cannot reach a TCP API host (iam.eu-de.otc.t-systems.com)",
                 "the probe unit cannot reach github.com", "the probe unit cannot write under /opt/tcp-awb",
                 "the probe unit cannot write under runs/", "the probe unit cannot list runs/",
                 "the tool guards refuse their planted cases, the build user's too"):
        assert want in checks, want
    setup = SETUP.read_text(encoding="utf-8")
    verify = VERIFY.read_text(encoding="utf-8")
    pin = re.search(r"^BUILD_CLIENT_SHA256=([0-9a-f]{64})\s", setup, re.M)
    version = re.search(r"^BUILD_CLIENT_VERSION=([0-9.]+)\s", setup, re.M)
    assert pin and version
    for pattern, text in (("s/^BUILD_CLIENT_SHA256=\\([0-9a-f]\\{64\\}\\).*/\\1/p", pin.group(1)),
                          ("s/^BUILD_CLIENT_VERSION=\\([0-9.]*\\).*/\\1/p", version.group(1))):
        assert pattern in verify
        out = subprocess.run(["sed", "-n", pattern, str(SETUP)], capture_output=True, text=True).stdout.strip()
        assert out == text
    assert re.search(r"^BUILD_ROOT=%s\s" % re.escape(config.BUILD_ROOT), setup, re.M)
    assert 'build_root="${build_root:-%s}"' % config.BUILD_ROOT in verify
    assert 'elif [ "$probe_ran" -eq 0 ]; then\n        report 1 "$what (the probe unit did not run)"' in verify
    for prop in ("ProtectSystem=strict", "ProtectHome=yes", "PrivateTmp=yes", "NoNewPrivileges=yes",
                 "InaccessiblePaths=/etc/awb", "IPAddressDeny=any", "IPAddressAllow=$probe_allow"):
        assert "--property=%s" % prop in verify or '--property="%s"' % prop in verify, prop


def test_the_probe_units_of_verify_bind_a_hosts_file_and_a_resolv_conf_without_a_nameserver(tmp_path):
    """Review of 2026-10-13, finding B4: the short probe units of verify.sh see what a task's session sees, a hosts
    file of loopback over /etc/hosts and a resolv.conf without a nameserver over /etc/resolv.conf, and allow no
    nameserver. The shell's own probe() and the lines that write the two files run against a systemd-run on PATH
    that prints each bind with the bound file."""
    text = VERIFY.read_text(encoding="utf-8")
    func = re.search(r"(?ms)^probe\(\) \{\n.*?^\}\n", text)
    names = re.search(r"(?ms)^    probe_names=\$\(mktemp -d\)\n.*?^    chmod 644 [^\n]*\n", text)
    assert func and names
    fake = tmp_path / "bin"
    fake.mkdir()
    (fake / "systemd-run").write_text(
        "#!/bin/bash\nfor a; do case \"$a\" in --property=BindReadOnlyPaths=*) b=\"${a#*=}\"; b=\"${b#*=}\"; "
        "echo \"bind ${b#*:}\"; cat \"${b%%:*}\" ;; --property=IPAddressAllow=*) echo \"allow ${a#*Allow=}\" ;; "
        "esac; done\n")
    (fake / "systemd-run").chmod(0o755)
    script = "set -euo pipefail\nBUILD_USER=nobody\nprobe_allow=192.0.2.10\n%s%sprobe id -un\n" % (
        names.group(0).replace("    ", ""), func.group(0))
    env = {"PATH": "%s:/usr/bin:/bin" % fake, "TMPDIR": str(tmp_path)}
    res = subprocess.run(["bash", "-c", script], capture_output=True, text=True, env=env, timeout=60)
    assert res.returncode == 0, res.stderr
    out = res.stdout
    assert "allow 192.0.2.10\n" in out
    assert "bind /etc/hosts\n127.0.0.1\tlocalhost\n::1\tlocalhost\n" in out
    assert "bind /etc/resolv.conf\n" in out
    assert not re.search(r"(?m)^\s*nameserver", out)


def test_setup_plans_his_login_by_default_the_key_only_with_build_model_key_and_verify_names_the_mode(fakebin,
                                                                                                    monkeypatch):
    """D-S7 changed (replaces the SB3 test that pinned the key as the default): the dry run of a first seal and of an
    update writes build_model = login, copies no key, removes a key copy left from the key mode, names the login
    command and installs the build settings as they are (no apiKeyHelper); with build_model = key it copies the key
    as in SB3 and writes the settings with apiKeyHelper. Both make the board's build folder and write the host file
    keys. verify.sh names the mode, checks the key copy, the folder and runs awb build selftest as the owner. A
    build model outside login and key, and the test's override outside a dry run, are refused."""
    login_cmd = "sudo -u awb-build -H /opt/claude-build/bin/claude login"
    for model in ("login", "key"):
        monkeypatch.setenv("AWB_SETUP_BUILD_MODEL", model)
        for args in (["--dry-run"], ["--update", "--dry-run"]):
            res = run_script(SETUP, args, fakebin)
            assert res.returncode == 0, res.stderr
            lines = res.stdout.splitlines()
            cmds = commands(res.stdout)
            install = "+ install -o %s -g %d -m 400 /etc/awb/anthropic.key /etc/awb/build-model.key.partial  # when " \
                      "missing or the source changed" % (USER, os.getgid())
            settings = "+ install -o root -g root -m 644 %s /srv/awb-build/home-template/.claude/settings.json" % \
                BUILD_SETTINGS
            helper = '    |   "apiKeyHelper": "cat \\"$HOME/.config/awb-build/model.key\\"",'
            if model == "login":
                assert install not in cmds and "+ rm -f -- /etc/awb/build-model.key  # if present" in cmds, args
                assert settings in cmds and helper not in lines
                assert res.stdout.count(login_cmd) == 1
            else:
                assert install in cmds and "+ rm -f -- /etc/awb/build-model.key  # if present" not in cmds, args
                assert settings not in cmds and helper in lines
                assert "+ write /srv/awb-build/home-template/.claude/settings.json (mode 644, root:root)" in cmds
                assert login_cmd not in res.stdout
            board = [c for c in cmds if c.startswith("+ install -d -o awb -g awb -m 3770 ")]
            assert len(board) == 1 and board[0].endswith("/tcp-shared/build")
            for key in ("build_model = %s" % model, "build_model_key = /etc/awb/build-model.key",
                        "build_model_key_source = /etc/awb/anthropic.key", "build_push = yes"):
                assert "    | " + key in lines, key
    monkeypatch.setenv("AWB_SETUP_BUILD_MODEL", "both")
    res = run_script(SETUP, ["--dry-run"], fakebin)
    assert res.returncode == 1 and "AWB_SETUP_BUILD_MODEL is login or key" in res.stderr
    monkeypatch.setenv("AWB_SETUP_BUILD_MODEL", "key")
    res = run_script(SETUP, ["--update"], fakebin, build_host="")
    assert res.returncode == 1 and "AWB_SETUP_BUILD_MODEL goes with --dry-run only" in res.stderr
    no_marks(fakebin)
    res = run_script(VERIFY, ["--dry-run"], fakebin)
    checks = [line[6:] for line in res.stdout.splitlines() if line.startswith("CHECK ")]
    for want in ("the build side runs on his login (build_model = login) and the login is there, or on the model key",
                 "the model key of a build task is the owner's (400) and matches its source",
                 "the board's build folder is the work user's, 3770",
                 "the build runner refuses its planted cases (awb build selftest)"):
        assert want in checks, want
    assert "runuser -u \"$owner\" -- \\\n        env HOME=\"$owner_home\" /usr/local/bin/awb build selftest" in \
        VERIFY.read_text(encoding="utf-8")
    text = SETUP.read_text(encoding="utf-8")
    assert re.search(r"(?m)^BUILD_MODEL_DEFAULT=login\s", text)
    assert 'build_model=$(build_model_mode)\n' in text


def _model_check(tmp_path: Path, conf: str, login: str | None, helper: bool, key: bool = False,
                 stray: bool = False) -> str:
    """build_model_check of verify.sh, taken from the script as it is, run for real against a throw-away host file,
    build root and template: the build user is this user, report prints PASS or FAIL as verify does. The runner's key
    copy (BUILD_MODEL_KEY) is a file of the test too, there only with `stray`: the host's own copy is never looked at."""
    text = VERIFY.read_text(encoding="utf-8")
    fn = re.search(r"(?ms)^build_model_check\(\) \{.*?^\}\n", text).group(0)
    root, t = tmp_path / "root", tmp_path / "template"
    (root / "home" / ".claude").mkdir(parents=True, exist_ok=True)
    (t / ".claude").mkdir(parents=True, exist_ok=True)
    body = '{\n  "autoMemoryEnabled": false,\n%s  "x": 1\n}\n' % (
        '  "apiKeyHelper": "cat \\"$HOME/.config/awb-build/model.key\\"",\n' if helper else "")
    (t / ".claude" / "settings.json").write_text(body)
    cred = root / "home" / ".claude" / ".credentials.json"
    if cred.exists():
        cred.unlink()
    if login is not None:
        cred.write_text(login)
    conf_file = tmp_path / "paths.conf"
    conf_file.write_text(conf)
    stray_file = tmp_path / "build-model.key"
    if stray_file.exists():
        stray_file.unlink()
    if stray:
        stray_file.write_text("k\n")
    keyfile = tmp_path / "copy.key"
    if key:
        if keyfile.exists():
            keyfile.unlink()
        keyfile.write_text("k\n")
        (tmp_path / "source.key").write_text("k\n")
        keyfile.chmod(0o400)
    script = ("set -u\nreport() { if [ \"$1\" -eq 0 ]; then echo \"PASS  $2\"; else echo \"FAIL  $2\"; fi; }\n"
              "allowed() { local w=\"$1\"; shift; if \"$@\" >/dev/null 2>&1; then report 0 \"$w\"; "
              "else report 1 \"$w\"; fi; }\n"
              "conf_value() { sed -n \"s/^[[:space:]]*$1[[:space:]]*=[[:space:]]*//p\" %s | tail -n 1; }\n"
              "BUILD_USER=%s\nBUILD_CLIENT=/opt/claude-build\nBUILD_MODEL_KEY=%s\nbuild_root=%s\nowner=%s\n%s"
              "build_model_check %s\n" % (conf_file, USER, stray_file, root, USER, fn, t))
    res = subprocess.run(["bash", "-c", script], capture_output=True, text=True, timeout=30)
    assert res.returncode == 0, res.stderr
    return res.stdout


def test_verify_names_the_build_model_and_the_login_by_its_presence_only(tmp_path):
    """D-S7 changed: verify's one line names the mode; with login it passes only when the build user's home holds
    the client's login (a regular file of the build user) and the template carries no apiKeyHelper, and a missing
    login names the one command he runs; the login's content is never printed. With key the line and the key's
    check of SB3 run."""
    secret = "fake-" + "login-token-verify"
    out = _model_check(tmp_path, "", None, helper=False)
    assert out.startswith("FAIL  the build side runs on his login (build_model = login): no login yet, as the owner "
                          "run sudo -u %s -H /opt/claude-build/bin/claude login" % USER)
    out = _model_check(tmp_path, "build_model = login\n", '{"claudeAiOauth": {"accessToken": "%s"}}' % secret,
                       helper=False)
    assert out == "PASS  the build side runs on his login (build_model = login): a login is there\n"
    out = _model_check(tmp_path, "build_model = login\n", '{"claudeAiOauth": {"accessToken": "%s"}}' % secret,
                       helper=False, stray=True)
    assert out == "FAIL  the build side runs on his login (build_model = login): a login is there\n"
    out = _model_check(tmp_path, "", '{"claudeAiOauth": {"accessToken": "%s"}}' % secret, helper=True)
    assert out.startswith("FAIL  the build side runs on his login") and secret not in out
    conf = "build_model = key\nbuild_model_key = %s\nbuild_model_key_source = %s\n" % (
        tmp_path / "copy.key", tmp_path / "source.key")
    out = _model_check(tmp_path, conf, None, helper=True, key=True)
    assert out == ("PASS  the build side runs on the model key (build_model = key)\n"
                   "PASS  the model key of a build task is the owner's (400) and matches its source\n")
    out = _model_check(tmp_path, conf, None, helper=False, key=True)
    assert out.startswith("FAIL  the build side runs on the model key")
    out = _model_check(tmp_path, "build_model = both\n", None, helper=False)
    assert out == "FAIL  the build model of the host file is login or key (build_model = both)\n"


def test_the_template_of_the_key_mode_is_the_settings_of_sb3_and_the_login_mode_has_no_key_helper():
    """The build settings in the repository carry no apiKeyHelper (the login mode installs them as they are); the
    key mode's template, as setup.sh writes it, is valid JSON whose apiKeyHelper reads the runner's key in the task
    home, and verify's template check takes both."""
    from awb import buildrun

    src = BUILD_SETTINGS.read_text(encoding="utf-8")
    assert "apiKeyHelper" not in src
    helper = re.search(r"(?m)^BUILD_KEY_HELPER='([^']*)'", SETUP.read_text(encoding="utf-8")).group(1)
    lines = src.splitlines(keepends=True)
    keyed = "".join(lines[:2]) + helper + "\n" + "".join(lines[2:])
    assert json.loads(keyed)["apiKeyHelper"] == 'cat "$HOME/%s"' % buildrun.KEY_IN_HOME
    assert json.loads(keyed) == dict(json.loads(src), apiKeyHelper='cat "$HOME/%s"' % buildrun.KEY_IN_HOME)
    assert "head -n 2 \"$seal_dir/build-claude/settings.json\"" in SETUP.read_text(encoding="utf-8")
    assert "tail -n +3 \"$seal_dir/build-claude/settings.json\"" in SETUP.read_text(encoding="utf-8")


def test_the_model_key_copy_is_the_owners_alone_and_follows_its_source_never_printed(tmp_path):
    """SB3, D-S7: build_model_key of setup.sh run for real against a throw-away source, copy and host file: a missing
    copy is made 400 with the source's bytes, a current one is left, a changed source is copied again, a source that
    is a link is refused with a warning and the copy stays; the key's bytes are never printed."""
    text = SETUP.read_text(encoding="utf-8")
    kept = re.search(r"(?ms)^kept_conf\(\) \{.*?^\}\n", text).group(0)
    src, dst, conf = tmp_path / "source.key", tmp_path / "copy.key", tmp_path / "paths.conf"
    first = "fake-" + "model-key-one"
    src.write_text(first + "\n")
    conf.write_text("build_model_key_source = %s\n" % src)
    tail = ("owner=%s\nowner_gid=%d\nCONF_FILE=%s\nBUILD_MODEL_KEY=%s\nBUILD_MODEL_KEY_SOURCE=/nonexistent\n%s"
            "build_model_key\n" % (USER, os.getgid(), conf, dst, kept))

    def run():
        res, _ = _run_build(tmp_path, tail)
        assert res.returncode == 0, res.stderr
        assert "model-key" not in res.stdout + res.stderr
        return res.stdout

    out = run()
    assert "+ install -o %s -g %d -m 400 %s %s.partial" % (USER, os.getgid(), src, dst) in out
    assert dst.read_text() == first + "\n" and oct(dst.stat().st_mode & 0o777) == "0o400"
    assert "= install " in run() and "the copy is current" in run()
    src.write_text("fake-" + "model-key-two\n")
    run()
    assert dst.read_text() == "fake-" + "model-key-two\n"
    src.unlink()
    src.symlink_to(conf)
    out = run()
    assert "warning: the source of the model key is not a regular file" in out
    assert dst.read_text() == "fake-" + "model-key-two\n"


def test_the_host_file_keeps_its_mode_and_group_when_a_command_rewrites_it(tmp_path, monkeypatch):
    """SB1: the host file is 640 root:awb; `sudo awb egress enforce|count` and `sudo awb web host HOST` rewrite it and
    must not open it again (they wrote 644). Planted: a host file of mode 640 stays 640 after both, one of 660 loses
    the group write bit."""
    from awb import egress
    from awb.tcp.web import publish

    conf = tmp_path / "paths.conf"
    conf.write_text("owner = x\nwork_user = awb\negress = count\n", encoding="utf-8")
    conf.chmod(0o640)
    egress.write_mode(egress.Host(conf=conf, chown_root=False), "enforce")
    assert oct(conf.stat().st_mode & 0o777) == "0o640" and "egress = enforce" in conf.read_text()
    monkeypatch.setattr(publish, "_domain", lambda unit: None)
    host = publish.Host(geteuid=lambda: 0, environ={"AWB_CONF": str(conf)}, chown=lambda *a: pytest.fail("chown"))
    assert publish.main(["host", "owner.example.test"], host=host) == 0
    assert oct(conf.stat().st_mode & 0o777) == "0o640" and "owner_host = owner.example.test" in conf.read_text()
    conf.chmod(0o660)
    egress.write_mode(egress.Host(conf=conf, chown_root=False), "count")
    assert oct(conf.stat().st_mode & 0o777) == "0o640"


# --------------------------------------------------------------------------- SB5: the selftest, the probe and the oneshot

def test_the_dry_runs_name_the_selftest_the_probe_of_a_task_and_the_failure_oneshot(fakebin):
    """SB5: verify.sh's dry run names the runner's selftest, the oneshot of a failed task unit and the lines of `awb
    build probe` in its order (the token host of his login among them); setup.sh's dry run writes the oneshot as
    the owner. The shell's lines for each build model are the runner's own (probe_lines), so a line the probe
    prints is the line verify counts. Planted: a verify.sh without the call of the probe lists none of its lines."""
    from awb import buildrun

    res = run_script(VERIFY, ["--dry-run"], fakebin)
    assert res.returncode == 0, res.stderr
    checks = [line[6:] for line in res.stdout.splitlines() if line.startswith("CHECK ")]
    assert "the build runner refuses its planted cases (awb build selftest)" in checks
    assert "the oneshot awb-task-failed@.service of a failed task unit is installed and writes the board line as the " \
           "owner" in checks
    net = [ln for _, ln in buildrun.probe_lines("login")]
    assert checks[-len(net):] == net
    assert "the probe unit reaches the token host of his login (platform.claude.com)" in net
    text = VERIFY.read_text(encoding="utf-8")
    assert "runuser -u \"$owner\" -- env HOME=\"$owner_home\" /usr/local/bin/awb build probe" in text
    funcs = re.search(r"^net_lines\(\) \{\n.*?^\}\n", text, re.M | re.S).group(0)
    for model in ("login", "key"):
        out = subprocess.run(["bash", "-c", "MODEL_HOST=api.anthropic.com; TOKEN_HOST=platform.claude.com; "
                              "TCP_API_HOST=iam.eu-de.otc.t-systems.com; GITHUB_HOST=github.com\n%s\nnet_lines %s"
                              % (funcs, model)], capture_output=True, text=True, check=True).stdout.splitlines()
        assert out == [ln for _, ln in buildrun.probe_lines(model)], model
    for name, value in (("MODEL_HOST", buildrun.MODEL_HOST), ("TOKEN_HOST", buildrun.TOKEN_HOST),
                        ("TCP_API_HOST", buildrun.TCP_API_HOST), ("GITHUB_HOST", buildrun.GITHUB_HOST)):
        assert re.search(r"^%s=%s(\s|$)" % (name, re.escape(value)), text, re.M), name
    res = run_script(SETUP, ["--update", "--dry-run"], fakebin)
    lines = res.stdout.splitlines()
    i = lines.index("+ write /etc/systemd/system/awb-task-failed@.service (mode 644, root:root)")
    unit = [x[6:] for x in lines[i + 1:] if x.startswith("    | ")][:20]
    for want in ("Type=oneshot", "User=%s" % USER, "Group=awb", "NoNewPrivileges=yes",
                 "ExecStart=/usr/local/bin/awb build failed %i"):
        assert want in unit, want
    assert buildrun.FAILED_UNIT % "%i" == "awb-task-failed@%i.service"
    # planted: the call of the probe cut out
    planted = fakebin[0].parent / "verify-planted.sh"
    planted.write_text(text.replace("    net_probe\n", ""), encoding="utf-8")
    res = run_script(planted, ["--dry-run"], fakebin)
    assert not [ln for ln in net if "CHECK " + ln in res.stdout.splitlines()]


def test_the_selftest_refuses_to_report_clean_when_a_planted_case_is_removed(monkeypatch, capsys):
    """SB5: the selftest holds a fixed list of its cases; a case taken out of SELFTEST_CASES (or doubled) is a failure,
    never a shorter clean report. Control: every case there, each a stand-in that refuses, passes."""
    from awb import buildrun

    class NoWorld:
        def __init__(self, *a, **k):
            pass

        def close(self):
            pass

    monkeypatch.setattr(buildrun, "World", NoWorld)
    stand_in = [(name, lambda w: None) for name, _ in buildrun.SELFTEST_CASES]
    assert sorted(n for n, _ in stand_in) == sorted(buildrun.SELFTEST_REQUIRED)
    monkeypatch.setattr(buildrun, "SELFTEST_CASES", stand_in)
    assert buildrun.selftest(python=sys.executable) == []
    for removed in ("n9-deleted", "push-nonff"):
        monkeypatch.setattr(buildrun, "SELFTEST_CASES", [c for c in stand_in if c[0] != removed])
        assert buildrun.selftest(python=sys.executable) == ["%s: the planted case is missing" % removed]
        monkeypatch.setattr(buildrun.config, "is_build_user", lambda: False)
        assert buildrun.selftest_main() == 1
        assert "selftest failed: %s: the planted case is missing" % removed in capsys.readouterr().out
    monkeypatch.setattr(buildrun, "SELFTEST_CASES", stand_in + stand_in[:1])
    assert buildrun.selftest(python=sys.executable) == ["%s: the planted case is there twice" % stand_in[0][0]]


def test_seal_check_on_a_build_host_passes_only_on_a_probe_record_with_the_host_checks(tmp_path, monkeypatch, capsys):
    """Replaces test_seal_check_runs_the_selftest_and_reads_the_probe_record_on_a_build_host (finding of 2026-10-12:
    a full record of the key mode has seven checks, the three of the host itself among them). `awb seal check` on a
    host with a build user runs the runner's selftest and reads the record of the last probe unit. Planted: no
    record, a record without the stub's check and a record with a failed line each fail the command; a host without
    a build user prints neither line."""
    from awb import buildrun

    monkeypatch.setattr(seal, "run_checks", lambda: [])
    monkeypatch.setattr(buildrun, "selftest", lambda: [])
    shared = tmp_path / "shared"
    (shared / "build").mkdir(parents=True)
    monkeypatch.setattr(config, "paths", lambda: config.Paths(shared=shared, vault=tmp_path / "v",
                                                             projects_root=tmp_path, check_socket=tmp_path / "s"))
    monkeypatch.setattr(config, "build_user", lambda: "awb-build")
    assert seal.main(["check"]) == 1
    out = capsys.readouterr().out
    assert "PASS  the build runner refuses its planted cases" in out
    assert "FAIL  the probe unit with a task's own properties passed its checks (no record yet: sudo seal/verify.sh " \
           "runs it)" in out
    rows = "probe\t2026-10-12T21:00:00\tkey\t7\t0\n" + "".join(
        "check\t%s\tpass\n" % k for k in ("unit", "model", "tcp", "github", "loopback", "stub", "host"))
    (shared / "build" / "probe.tsv").write_text(rows)
    assert seal.main(["check"]) == 0
    assert "PASS  the probe unit with a task's own properties passed its 7 checks (sudo seal/verify.sh, 2026-10-12 " \
           "21:00)" in capsys.readouterr().out
    (shared / "build" / "probe.tsv").write_text(rows.replace("check\tstub\tpass\n", ""))
    assert seal.main(["check"]) == 1
    assert "(failed: stub)" in capsys.readouterr().out
    (shared / "build" / "probe.tsv").write_text(rows.replace("host\tpass", "host\tfail"))
    assert seal.main(["check"]) == 1
    assert "(failed: host)" in capsys.readouterr().out
    monkeypatch.setattr(config, "build_user", lambda: None)
    assert seal.main(["check"]) == 0
    assert "probe unit" not in capsys.readouterr().out
