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
    # the code (T1, build/DECISIONS.md row 11): a release folder per commit, one .pth line, the exchange
    assert has(r"git -C %s archive HEAD \| tar -x --no-same-owner --no-same-permissions -C "
               r"/opt/tcp-awb/releases/\.<HEAD>\.partial$" % re.escape(str(REPO)))
    assert has(r"^\+ python3 -m venv --system-site-packages /opt/tcp-awb/venv")
    assert has(r"^\+ write /opt/tcp-awb/venv/lib/python3[^/]*/site-packages/awb\.pth \(mode 644, root:root\)$")
    assert has(r"^\+ exchange /opt/tcp-awb/src\.next /opt/tcp-awb/src  # renameat2 RENAME_EXCHANGE, one call$")
    assert not has(r"pip install") and not has(r"rm -rf -- /opt/tcp-awb/src$")
    # T3 (build/DECISIONS.md, 2026-10-07, D-T3a): a wrapper in place of the symlink, see the wrapper tests below
    assert has(r"^\+ write /usr/local/bin/awb \(mode 755, root:root\)$")
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
    env.update(SUDO_USER=USER, AWB_SETUP_OPT=str(opt), AWB_SETUP_BIN=str(tmp / "bin" / "awb"))
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


def test_setup_update_mode_skips_the_seal_steps(fakebin):
    """Planted: the full mode prints the chown of the shared tree, the users and the moves (so the checks can fail);
    the update mode prints none of them, no mount, no rm -rf of src, and prints the .pth line, the exchange, every
    seal/*.service and the needrestart rule. A grep over the script finds no rm -rf of src."""
    full = commands(run_script(SETUP, ["--dry-run"], fakebin).stdout)

    def seal_steps(cmds):
        return [c for c in cmds if re.match(r"^[+=] (chown|chmod|find)\b.*tcp-shared", c)
                or re.match(r"^[+=] (useradd|groupadd|usermod|gpasswd)\b", c) or c.startswith("+ mv -- ")
                or re.match(r"^[+=] mount\b", c) or "authorized_keys" in c
                or re.search(r"rm -rf -- /opt/tcp-awb/src( |$)", c)]

    assert seal_steps(full)
    res = run_script(SETUP, ["--update", "--dry-run"], fakebin)
    assert res.returncode == 0, res.stderr
    no_marks(fakebin)
    cmds = commands(res.stdout)
    assert seal_steps(cmds) == []
    joined = "\n".join(cmds)
    assert re.search(r"^\+ write /opt/tcp-awb/venv/lib/python3[^/]*/site-packages/awb\.pth ", joined, re.M)
    assert "    | /opt/tcp-awb/src" in res.stdout.splitlines()
    assert "+ exchange /opt/tcp-awb/src.next /opt/tcp-awb/src  # renameat2 RENAME_EXCHANGE, one call" in cmds
    for unit in sorted(p.name for p in SEAL.glob("*.service")):
        assert "+ write /etc/systemd/system/%s (mode 644, root:root)" % unit in cmds
        assert "+ systemctl enable --now %s" % unit in cmds
    assert "+ write /etc/needrestart/conf.d/awb.conf (mode 644, root:root)" in cmds
    assert [c for c in cmds if " mv -T " in c or c.startswith("+ mv -T")] == [
        "+ mv -T -- /opt/tcp-awb/src.next /opt/tcp-awb/releases/pre-deploy  # if /opt/tcp-awb/src.next is the plain "
        "folder of before T1, else rm -f"]
    assert "pip" not in joined and not re.search(r"(^|[ |;&])git ", joined, re.M)
    text = SETUP.read_text(encoding="utf-8")
    assert not re.search(r'rm -rf[^\n]*\$OPT/src["\s]', text)
    assert re.search(r"^update_steps\(\) \{\n(    #.*\n)?    if \[ \"\$units_only\" -eq 1 \]; then\n        step_units\n"
                     r"        return 0\n    fi\n    step_code\n    step_conf\n    step_units\n    step_needrestart\n"
                     r"    step_client\n    step_managed\n    step_mirrors\n\}", text, re.M)
    # --units-only: the units and daemon-reload, nothing else
    res = run_script(SETUP, ["--update", "--units-only", "--dry-run"], fakebin)
    assert res.returncode == 0, res.stderr
    only = commands(res.stdout)
    assert only == ["+ write /etc/systemd/system/%s (mode 644, root:root)" % p.name
                    for p in sorted(SEAL.glob("*.service"))] + ["+ systemctl daemon-reload"]
    assert run_script(SETUP, ["--units-only", "--dry-run"], fakebin).returncode == 1


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
