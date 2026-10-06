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
import tempfile
import threading
from pathlib import Path

import pytest

from awb import config, gate, hooks, seal
from tests import fixtures

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


def run_script(script: Path, args: list[str], fakebin, sudo_user: str | None = USER) -> subprocess.CompletedProcess:
    folder, _ = fakebin
    env = {k: v for k, v in os.environ.items() if k != "SUDO_USER"}
    env["PATH"] = "%s:%s" % (folder, os.environ.get("PATH", "/usr/bin:/bin"))
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


def test_setup_dry_run_prints_every_step(fakebin):
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
    # the code
    assert has(r"git -C %s archive HEAD \| tar -x -C /opt/tcp-awb/src$" % re.escape(str(REPO)))
    assert has(r"^\+ python3 -m venv --system-site-packages /opt/tcp-awb/venv")
    assert has(r"^\+ /opt/tcp-awb/venv/bin/pip install .*/opt/tcp-awb/src$")
    assert has(r"^\+ chown -R root:root /opt/tcp-awb$")
    assert has(r"^\+ ln -sfn /opt/tcp-awb/venv/bin/awb /usr/local/bin/awb$")
    # the host file and the unit
    assert has(r"^\+ write /etc/awb/paths\.conf \(mode 644, root:root\)$")
    for key in ("owner = %s" % USER, "vault = %s" % VAULT, "check_socket = /run/awb/check.sock",
                "mirrors = /srv/tcp-mirrors"):
        assert "    | " + key in out.splitlines()
    assert has(r"^\+ write /etc/systemd/system/awb-vaultd\.service ")
    assert "    | User=%s" % USER in out.splitlines()
    assert "    | Group=awb" in out.splitlines()
    assert has(r"^\+ systemctl enable --now awb-vaultd\.service$")
    # the keys and the client files of the work user
    assert has(r"^\+ install -o awb -g awb -m 600 %s/\.ssh/authorized_keys \S+/\.ssh/authorized_keys"
               % re.escape(OWNER_HOME))
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


def test_work_settings_carry_the_five_hooks_and_deny_connectors():
    data = json.loads(WORK_SETTINGS.read_text(encoding="utf-8"))
    assert set(data) == {"hooks", "permissions"}
    assert data["hooks"] == hooks.client_settings("/usr/local/bin/awb hook")["hooks"]
    assert set(data["hooks"]) == {"UserPromptSubmit", "PreToolUse", "PostToolUse", "Stop", "SessionStart"}
    assert data["permissions"] == {"deny": ["mcp__*"]}


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
