"""Task WL part 2 (B-036): the setup and removal scripts for the owner's Windows lab laptop (awb/winlab/setup.ps1 and
awb/winlab/remove.ps1, printed by awb winlab bootstrap NAME and awb winlab remove NAME).

The scripts are checked as text: nothing here runs PowerShell. The keys are built in the test (a valid key blob of
zero bytes), the registry is winlab.json beside a keys.conf in tmp_path. Every machine name, account, host and key is
invented.
"""
from __future__ import annotations

import base64
import re

import pytest

from awb import cli, config, deploy, winlab

MACHINE = "lab-1"
OTHER_MACHINE = "lab-2"
FINGERPRINT = "SHA256:" + ("invented" * 6)[:43]
TUNNEL = "awbtunnel@awb-host.example"

SETUP = (winlab.SCRIPTS / "setup.ps1").read_text(encoding="ascii")
REMOVE = (winlab.SCRIPTS / "remove.ps1").read_text(encoding="ascii")
SCRIPTS = {"setup.ps1": SETUP, "remove.ps1": REMOVE}

# what a step may change on the laptop: none of these runs outside a Step block or a function
CHANGING = re.compile(r"\b(New-|Set-|Add-|Remove-|Register-|Unregister-|Start-|Stop-|Copy-Item|Move-Item|Invoke-Exe|"
                      r"Write-Ascii|Disable-|Enable-)")


def key_line(key_type: str = "ssh-ed25519", comment: str = "invented-key") -> str:
    """An OpenSSH public key line whose blob names its type and holds zero bytes."""
    kind = key_type.encode("ascii")
    blob = len(kind).to_bytes(4, "big") + kind + (32).to_bytes(4, "big") + bytes(32)
    return "%s %s %s\n" % (key_type, base64.b64encode(blob).decode("ascii"), comment)


@pytest.fixture
def owner(tmp_path, monkeypatch):
    """The owner's own terminal: keys.conf in tmp_path, no assistant session, not the work user."""
    conf = tmp_path / "conf" / "keys.conf"
    conf.parent.mkdir()
    conf.write_text("region = eu-de\n", encoding="utf-8")
    monkeypatch.setenv("AWB_KEYS_CONF", str(conf))
    monkeypatch.delenv("CLAUDECODE", raising=False)
    monkeypatch.delenv("CLAUDE_CODE_ENTRYPOINT", raising=False)
    monkeypatch.setattr(config, "is_work_user", lambda: False)
    return conf.parent / winlab.FILE_NAME


@pytest.fixture
def keys(tmp_path):
    """(host key, server key): two .pub files."""
    host = tmp_path / "id_ed25519.pub"
    host.write_text(key_line(), encoding="ascii")
    server = tmp_path / "ssh_host_ed25519_key.pub"
    server.write_text(key_line("ssh-ed25519", "root-at-host"), encoding="ascii")
    return str(host), str(server)


def bootstrap_argv(keys, *extra: str) -> list[str]:
    return ["winlab", "bootstrap", MACHINE, "--host-key", keys[0], "--server-key", keys[1], "--tunnel", TUNNEL,
            *extra]


def steps(text: str) -> list[tuple[str, str, str]]:
    """(name, do, check) of every `Step 'name' { do } { check }` at the top of a script."""
    out = []
    starts = [m for m in re.finditer(r"^Step '([a-z-]+)' \{\n", text, re.M)]
    for i, m in enumerate(starts):
        end = starts[i + 1].start() if i + 1 < len(starts) else len(text)
        body = text[m.end():end]
        do, sep, rest = body.partition("\n} {\n")
        assert sep, "step %s has no result check block" % m.group(1)
        check = rest[:rest.index("\n}\n")] if "\n}\n" in rest else ""
        out.append((m.group(1), do, check))
    return out


def top_level(text: str) -> list[str]:
    """The lines of a script outside every brace block, so outside the functions and the steps."""
    out, depth = [], 0
    in_here = False
    for line in text.splitlines():
        if in_here:
            in_here = not line.startswith("'@")
            continue
        if depth == 0:
            out.append(line)
        if re.search(r"= @'$", line):
            in_here = True
        depth += line.count("{") - line.count("}")
        assert depth >= 0, line
    assert depth == 0
    return out


def definitions(text: str) -> dict[str, str]:
    """{variable: right side} of the `$Name = ...` lines at the top of a script."""
    return dict(re.findall(r"^\$([A-Za-z]+) = (.+)$", "\n".join(top_level(text)), re.M))


# --------------------------------------------------------------------------- every step checks its own result


@pytest.mark.parametrize("name", sorted(SCRIPTS))
def test_wl_every_step_has_its_own_result_check_and_a_named_stop(name):
    text = SCRIPTS[name]
    found = steps(text)
    assert len(found) >= 8
    names = [n for n, _, _ in found]
    assert len(set(names)) == len(names), "two steps with one name"
    for step, do, check in found:
        assert do.strip(), step
        assert check.strip() and not check.strip().startswith("#"), "step %s checks nothing" % step
    # Step stops with one line naming the step: on a failed change, a failed check and a check that is not $true
    stop = "Stop-Setup" if name == "setup.ps1" else "Stop-Removal"
    helper = text[text.index("function Step("):text.index("\n}\n", text.index("function Step("))]
    assert helper.count("%s $Name" % stop) == 3
    assert "$result[-1] -ne $true" in helper
    stopper = text[text.index("function %s(" % stop):text.index("\n}\n", text.index("function %s(" % stop))]
    assert "stopped at step {0}: {1}" in stopper and "exit 1" in stopper and "-replace '\\s+', ' '" in stopper
    assert "$ErrorActionPreference = 'Stop'" in text
    # nothing changes the laptop outside a step
    for line in top_level(text):
        assert line.startswith("function ") or not CHANGING.search(line), line
    # a program's exit code is checked: every ssh-keygen and sshd goes through Invoke-Exe
    runs = list(re.finditer(r"(\S+) \(Join-Path \$OpenSsh '[a-z-]+\.exe'\) \(", text))
    assert len(runs) == (4 if name == "setup.ps1" else 0)
    assert all(m.group(1) == "Invoke-Exe" for m in runs)
    assert "Start-Process" not in text and "cmd /c" not in text
    assert names[0] == "administrator"


def test_wl_setup_has_the_steps_of_the_task_in_their_order():
    names = [n for n, _, _ in steps(SETUP)]
    order = ["administrator", "state-folder", "openssh-server", "sshd-stop", "host-key", "account", "groups",
             "authorized-keys", "sshd-config", "sshd-start", "tunnel-key", "known-host", "tunnel-script",
             "tunnel-task", "tunnel-start", "report"]
    assert names == order
    invoke = SETUP[SETUP.index("function Invoke-Exe("):SETUP.index("function Write-Ascii(")]
    assert "$p.ExitCode -ne 0" in invoke and "throw" in invoke


# --------------------------------------------------------------------------- sshd on 127.0.0.1 only


def test_wl_sshd_listens_on_127_0_0_1_only():
    for name, text in SCRIPTS.items():
        assert "0.0.0.0" not in text, name
        assert "::" not in text.replace("]::", "").replace("::Zero", ""), name
    config_step = dict((n, d) for n, d, _ in steps(SETUP))["sshd-config"]
    listen = re.findall(r"'ListenAddress [^']*'", config_step)
    assert listen == ["'ListenAddress 127.0.0.1'"]
    assert "'AddressFamily inet'" in config_step and "'Port 22'" in config_step
    # the config is checked by sshd -t before the service starts with it
    assert "'-t -f" in config_step
    # a fresh sshd listens on every address: it is stopped before the first start with the new config
    names = [n for n, _, _ in steps(SETUP)]
    assert names.index("openssh-server") < names.index("sshd-stop") < names.index("sshd-config") < \
        names.index("sshd-start")
    stop_do = dict((n, d) for n, d, _ in steps(SETUP))["sshd-stop"]
    assert "Stop-Service -Name sshd" in stop_do and "Start-Service" not in SETUP[:SETUP.index("Step 'sshd-start'")]
    # the listen addresses are read back after the start: every listener of the sshd process on 127.0.0.1
    start_check = dict((n, c) for n, _, c in steps(SETUP))["sshd-start"]
    assert "Test-SshdLoopbackOnly" in start_check
    probe = SETUP[SETUP.index("function Test-SshdLoopbackOnly"):SETUP.index("# ----", SETUP.index(
        "function Test-SshdLoopbackOnly"))]
    assert "Get-NetTCPConnection -State Listen -OwningProcess" in probe
    assert "$_.LocalAddress -ne '127.0.0.1'" in probe and "$other.Count -eq 0" in probe


def test_wl_key_logon_only_for_the_local_account():
    config_step = dict((n, d) for n, d, _ in steps(SETUP))["sshd-config"]
    for line in ("'PasswordAuthentication no'", "'AuthenticationMethods publickey'", "'PubkeyAuthentication yes'",
                 "'PermitEmptyPasswords no'", "('AllowUsers ' + $Account)",
                 "'AuthorizedKeysFile __PROGRAMDATA__/ssh/administrators_authorized_keys'"):
        assert line in config_step, line
    assert "PasswordAuthentication yes" not in SETUP
    # the local account in Administrators and Hyper-V Administrators, by SID
    assert "$AdminsSid = 'S-1-5-32-544'" in SETUP and "$HyperVAdminsSid = 'S-1-5-32-578'" in SETUP
    groups_check = dict((n, c) for n, _, c in steps(SETUP))["groups"]
    assert "Test-GroupMember $AdminsSid" in groups_check and "Test-GroupMember $HyperVAdminsSid" in groups_check
    account_do = dict((n, d) for n, d, _ in steps(SETUP))["account"]
    assert "New-LocalUser -Name $Account" in account_do
    # the host key file: inheritance off, Administrators and SYSTEM full, read back by the check
    acl = SETUP[SETUP.index("function Set-AdminOnlyAcl"):SETUP.index("function Test-AdminOnlyAcl")]
    assert "SetAccessRuleProtection($true, $false)" in acl and "'FullControl'" in acl
    assert "@($AdminsSid, $SystemSid)" in acl and "$SystemSid = 'S-1-5-18'" in SETUP
    keys_step = [(d, c) for n, d, c in steps(SETUP) if n == "authorized-keys"][0]
    assert "Set-AdminOnlyAcl $AuthorizedKeys" in keys_step[0] and "Test-AdminOnlyAcl $AuthorizedKeys" in keys_step[1]
    assert "$AuthorizedKeys = Join-Path $SshDir 'administrators_authorized_keys'" in SETUP


@pytest.mark.parametrize("name", sorted(SCRIPTS))
def test_wl_no_password_is_written_in_either_script(name):
    text = SCRIPTS[name]
    for bad in ("ConvertTo-SecureString", "AsPlainText", "net user", "-Password '", '-Password "', "Out-File",
                "Set-Content", "Export-Clixml", "Read-Host", "SecureStringToBSTR", "PtrToString", "Get-Credential"):
        assert bad not in text, bad
    assert not re.search(r"(?i)password\s*=", text)
    # the random password lives in a SecureString only: built char by char, handed to New-LocalUser, disposed
    uses = [line.strip() for line in text.splitlines() if "$secret" in line]
    if name == "remove.ps1":
        assert uses == []
        return
    assert len(uses) == 7, uses
    for line in uses:
        assert (line == "$secret = New-Object System.Security.SecureString" or "$secret.AppendChar(" in line
                or line.startswith("New-LocalUser -Name $Account -Password $secret ")
                or line == "$secret.Dispose()"), line
    assert "RandomNumberGenerator]::Create()" in text


# --------------------------------------------------------------------------- the tunnel


def test_wl_the_tunnel_is_a_visible_task_at_boot_that_restarts_ssh():
    script = SETUP[SETUP.index("$script = @'"):SETUP.index("\n'@\n")]
    for option in ("'ServerAliveInterval=30'", "'ServerAliveCountMax=3'", "'ExitOnForwardFailure=yes'",
                   "'StrictHostKeyChecking=yes'", "'BatchMode=yes'", "'-N'",
                   "'-R', '127.0.0.1:@@PORT@@:127.0.0.1:22', '@@TUNNEL@@'"):
        assert option in script, option
    assert "while ($true)" in script and "Start-Sleep -Seconds 30" in script
    task = dict((n, d) for n, d, _ in steps(SETUP))["tunnel-task"]
    assert "New-ScheduledTaskTrigger -AtStartup" in task and "-Hidden" not in SETUP
    assert "$TaskName = 'awb-winlab-tunnel'" in SETUP and "$TaskName = 'awb-winlab-tunnel'" in REMOVE
    task_check = dict((n, c) for n, _, c in steps(SETUP))["tunnel-task"]
    assert "-not $task.Settings.Hidden" in task_check and "MSFT_TaskBootTrigger" in task_check
    # the laptop's own tunnel key; the end prints the host key fingerprint and the tunnel public key
    assert "-C awb-winlab-tunnel-{0}" in SETUP
    tail = SETUP[SETUP.index("Step 'report'"):]
    assert "host key fingerprint of this laptop:" in tail and "tunnel public key:" in tail
    assert "awb winlab add {0} --port {1} --fingerprint {2} --no-customer-data" in tail
    assert 'restrict,port-forwarding,permitlisten="127.0.0.1:{0}"' in tail


# --------------------------------------------------------------------------- the removal script


def test_wl_the_removal_script_removes_each_thing_the_setup_creates():
    setup_defs, remove_defs = definitions(SETUP), definitions(REMOVE)
    # one name for one thing: what both scripts name, they name alike (remove.ps1 reads the account setup made from
    # state.txt)
    assert remove_defs["Account"] == "Get-State 'account'"
    for var in set(setup_defs) & set(remove_defs):
        if var not in ("Machine", "Account"):
            assert setup_defs[var] == remove_defs[var], var
    remove_steps = dict((n, (d, c)) for n, d, c in steps(REMOVE))
    names = list(remove_steps)
    assert names[-1] == "state-folder" and "Remove-IfThere $Dir" in remove_steps["state-folder"][0]
    assert "-not (Test-Path -LiteralPath $Dir)" in remove_steps["state-folder"][1]
    # every file setup writes lives in %ProgramData%\awb-winlab or is removed (or put back) by name
    written = set(re.findall(r"Write-Ascii \$([A-Za-z]+)", SETUP)) | set(re.findall(
        r"ssh-keygen\.exe'\) \('[^']*-f \"\{\d\}\"' -f [^)]*?\$([A-Za-z]+)\)", SETUP))
    assert written >= {"StateFile", "AuthorizedKeys", "SshdConfig", "KnownHosts", "TunnelScript", "HostKey",
                       "TunnelKey"}
    for var in written:
        if setup_defs[var].startswith("Join-Path $Dir "):
            continue
        assert remove_defs.get(var) == setup_defs[var], var
        assert re.search(r"(Remove-IfThere|-Destination) \$%s\b" % var, REMOVE), var
    # the private keys go by name too, with their .pub files
    for var in ("HostKey", "TunnelKey"):
        assert "Remove-IfThere $%s\n" % var in REMOVE and "Remove-IfThere ($%s + '.pub')" % var in REMOVE
    # the task, the service, the account and its profile folder
    pairs = [("Register-ScheduledTask -TaskName $TaskName", "Unregister-ScheduledTask -TaskName $TaskName"),
             ("Add-WindowsCapability -Online", "Remove-WindowsCapability -Online"),
             ("New-LocalUser -Name $Account", "Remove-LocalUser -SID $AccountSid"),
             ("Set-Service -Name sshd -StartupType Automatic", "Set-Service -Name sshd -StartupType $StartBefore")]
    for made, removed in pairs:
        assert made in SETUP and removed in REMOVE, removed
    assert "Win32_UserProfile" in remove_steps["profile"][0] and "Remove-CimInstance" in remove_steps["profile"][0]
    assert "Remove-IfThere $AccountFolder" in remove_steps["profile"][0]
    # the profile folder is only ever a well formed account's, never the Users folder itself
    assert "if ($Account -notmatch '^[a-z][a-z0-9-]{1,19}$') { $Account = $null }" in REMOVE
    assert "if ($Account) { $AccountFolder = Join-Path $env:SystemDrive ('Users\\' + $Account) }" in REMOVE
    assert REMOVE.index("$AccountFolder = $null") < REMOVE.index("Step 'profile'")
    assert winlab.ACCOUNT_RE.pattern == "^[a-z][a-z0-9-]{1,19}$"
    assert "Get-ScheduledTask" in remove_steps["tunnel-task"][1] and "Get-TunnelProcess" in remove_steps[
        "tunnel-task"][1]
    assert "Get-LocalUser -SID $AccountSid" in remove_steps["account"][1]
    # the state setup records is what remove reads
    for key in re.findall(r"Set-State '([a-z_]+)'", SETUP):
        assert "Get-State '%s'" % key in REMOVE, key
    # the copies of before are put back from the state folder before it goes
    assert "sshd_config.before" in SETUP and "sshd_config.before" in REMOVE
    assert "administrators_authorized_keys.before" in SETUP and "administrators_authorized_keys.before" in REMOVE
    assert names.index("sshd-service") < names.index("keys") < names.index("profile") < names.index("account")


def test_wl_setup_refuses_an_account_it_did_not_make():
    state = dict((n, d) for n, d, _ in steps(SETUP))["state-folder"]
    assert "a local account {0} exists already and this script did not make it" in state
    assert "this laptop is set up as lab machine {0}" in state


# --------------------------------------------------------------------------- awb winlab bootstrap


def test_wl_bootstrap_prints_the_filled_setup_script(owner, keys, capsys):
    winlab.add(MACHINE, 2201, FINGERPRINT, True)
    assert cli.main(bootstrap_argv(keys)) == 0
    out, err = capsys.readouterr()
    assert out.startswith("# awb-winlab setup for lab machine %s" % MACHINE)
    assert "@@" not in out
    assert "$Machine = '%s'" % MACHINE in out and "$Port = '2201'" in out and "$Account = 'awblab'" in out
    assert "$Tunnel = '%s'" % TUNNEL in out and "$TunnelUser = 'awbtunnel'" in out
    host_type, host_blob = key_line().split()[:2]
    assert "$HostKeyLine = '%s %s awb-winlab-host'" % (host_type, host_blob) in out
    assert "$KnownHostLine = 'awb-host.example %s %s'" % (host_type, host_blob) in out
    assert "'-p', '22', '-R', '127.0.0.1:2201:127.0.0.1:22', '%s'" % TUNNEL in out
    assert "invented-key" not in out and "root-at-host" not in out       # the comments of the key files stay out
    assert "setup.ps1 for %s printed" % MACHINE in err and len(err.strip().splitlines()) == 1
    assert out.isascii() and "0.0.0.0" not in out
    assert winlab.load()[MACHINE].get("window") is None                 # bootstrap writes nothing


def test_wl_bootstrap_takes_the_port_and_the_options_of_an_unregistered_machine(owner, keys, capsys):
    assert cli.main(bootstrap_argv(keys)) == 1
    assert "not registered yet: give its loopback port with --port" in capsys.readouterr().err
    assert cli.main(bootstrap_argv(keys, "--port", "2205", "--account", "lab-admin", "--ssh-port", "2222")) == 0
    out = capsys.readouterr().out
    assert "$Port = '2205'" in out and "$Account = 'lab-admin'" in out
    assert "$KnownHostLine = '[awb-host.example]:2222 ssh-ed25519 " in out and "'-p', '2222'" in out
    assert not owner.exists()


def test_wl_bootstrap_refuses_a_port_that_is_not_the_machines(owner, keys, capsys):
    winlab.add(MACHINE, 2201, FINGERPRINT, True)
    winlab.add(OTHER_MACHINE, 2202, FINGERPRINT, True)
    assert cli.main(bootstrap_argv(keys, "--port", "2203")) == 1
    assert "registered with port 2201, not 2203" in capsys.readouterr().err
    with pytest.raises(winlab.WinlabError, match="port 2202 is the port of %s" % OTHER_MACHINE):
        winlab.bootstrap("lab-3", keys[0], TUNNEL, port=2202, server_key=keys[1])
    for port in (22, 1023, 65536):
        with pytest.raises(winlab.WinlabError, match="a loopback port from 1024 to 65535"):
            winlab.bootstrap("lab-3", keys[0], TUNNEL, port=port, server_key=keys[1])


@pytest.mark.parametrize("tunnel", ["", "awb-host.example", "awbtunnel@", "@awb-host.example",
                                    "awbtunnel@awb host", "awbtunnel@awb-host.example'; Remove-Item C:\\",
                                    "Awb'x@awb-host.example", "awbtunnel@awb-host.example:22", "a@b@c",
                                    "awbtunnel@-awb-host.example", "awbtunnel@awb$host"])
def test_wl_bootstrap_refuses_a_tunnel_that_is_not_user_at_host(owner, keys, capsys, tunnel):
    winlab.add(MACHINE, 2201, FINGERPRINT, True)
    argv = bootstrap_argv(keys)
    argv[argv.index(TUNNEL)] = tunnel
    assert cli.main(argv) == 1
    out, err = capsys.readouterr()
    assert out == "" and "--tunnel reads USER@HOST" in err


@pytest.mark.parametrize("account", ["Awblab", "a", "awb lab", "awb'lab", "1lab", "a" * 21, "awb_lab"])
def test_wl_bootstrap_refuses_a_malformed_account(owner, keys, account):
    winlab.add(MACHINE, 2201, FINGERPRINT, True)
    with pytest.raises(winlab.WinlabError, match="the local account is 2 to 20"):
        winlab.bootstrap(MACHINE, keys[0], TUNNEL, account=account, server_key=keys[1])


def test_wl_bootstrap_refuses_a_key_file_that_is_not_one_public_key(owner, keys, tmp_path, capsys):
    winlab.add(MACHINE, 2201, FINGERPRINT, True)
    marker = "-----BEGIN OPENSSH " + "PRI" + "VATE KEY-----"
    not_one = "the file is not one OpenSSH public key (ssh-ed25519 AAAA...)"
    blob = key_line().split()[1]
    cases = {
        "private": (marker + "\nb3BlbnNzaC1rZXktdjEAAAAA\n", "the file is a private key, give its .pub file"),
        "two": (key_line() + key_line(), not_one),
        "empty": ("", not_one),
        "type": ("ssh-dss %s invented-key\n" % blob, not_one),
        "damaged": ("ssh-ed25519 %s invented-key\n" % blob[:-8], "the key in the file is damaged"),
        "other type": (key_line("ecdsa-sha2-nistp256").replace("ecdsa-sha2-nistp256", "ssh-ed25519", 1),
                       "the key in the file is damaged"),
        "options": ('command="x" ' + key_line(), not_one),
    }
    for case, (text, why) in cases.items():
        bad = tmp_path / ("bad-%s.pub" % case.replace(" ", "-"))
        bad.write_text(text, encoding="ascii")
        for option, argv in (("--host-key", ["--host-key", str(bad), "--server-key", keys[1]]),
                             ("--server-key", ["--host-key", keys[0], "--server-key", str(bad)])):
            assert cli.main(["winlab", "bootstrap", MACHINE, "--tunnel", TUNNEL] + argv) == 1, case
            out, err = capsys.readouterr()
            assert out == "" and err.strip() == "awb winlab: %s: %s" % (option, why), case
            assert "b3Blbn" not in err and blob[:20] not in err
    assert cli.main(["winlab", "bootstrap", MACHINE, "--tunnel", TUNNEL, "--host-key", str(tmp_path / "none.pub"),
                     "--server-key", keys[1]]) == 1
    assert "--host-key: there is no file at the path given" in capsys.readouterr().err
    # the key types an OpenSSH host offers are taken
    for key_type in winlab.KEY_TYPES:
        good = tmp_path / ("good-%s.pub" % key_type)
        good.write_text("# a comment line\n" + key_line(key_type), encoding="ascii")
        assert "'%s " % key_type in winlab.bootstrap(MACHINE, str(good), TUNNEL, server_key=keys[1])


@pytest.mark.parametrize("who", ["work user", "session", "entrypoint"])
def test_wl_bootstrap_and_the_removal_script_are_refused_for_the_work_user_and_in_a_session(owner, keys, monkeypatch,
                                                                                           capsys, who):
    winlab.add(MACHINE, 2201, FINGERPRINT, True)
    before = owner.read_text(encoding="utf-8")
    if who == "work user":
        monkeypatch.setattr(config, "is_work_user", lambda: True)
    elif who == "session":
        monkeypatch.setenv("CLAUDECODE", "1")
    else:
        monkeypatch.setenv("CLAUDE_CODE_ENTRYPOINT", "cli")
    for argv in (bootstrap_argv(keys), ["winlab", "remove", MACHINE, "--script-only"],
                 ["winlab", "remove", MACHINE]):
        assert cli.main(argv) == 1, argv
        out, err = capsys.readouterr()
        assert out == "" and winlab.OWNER_LINE in err
    for call in (lambda: winlab.bootstrap(MACHINE, keys[0], TUNNEL, server_key=keys[1]),
                 lambda: winlab.removal_script(MACHINE)):
        with pytest.raises(winlab.WinlabError) as err:
            call()
        assert str(err.value) == winlab.OWNER_LINE
    assert owner.read_text(encoding="utf-8") == before


# --------------------------------------------------------------------------- awb winlab remove


def test_wl_remove_prints_the_removal_script_and_forgets_the_machine(owner, capsys):
    winlab.add(MACHINE, 2201, FINGERPRINT, True)
    assert cli.main(["winlab", "remove", MACHINE]) == 0
    out, err = capsys.readouterr()
    assert out.startswith("# awb-winlab removal for lab machine %s" % MACHINE)
    assert "$Machine = '%s'" % MACHINE in out and "@@" not in out and out.isascii()
    assert err.strip() == "lab machine %s removed" % MACHINE
    assert winlab.load() == {}
    # a machine that is gone: refused, nothing printed; --script-only prints the script for any machine name
    assert cli.main(["winlab", "remove", MACHINE]) == 1
    assert capsys.readouterr().out == ""
    assert cli.main(["winlab", "remove", MACHINE, "--script-only"]) == 0
    out, err = capsys.readouterr()
    assert out.startswith("# awb-winlab removal") and "stays registered" in err


def test_wl_remove_script_only_keeps_the_machine(owner, capsys):
    winlab.add(MACHINE, 2201, FINGERPRINT, True)
    winlab.open_(MACHINE, 30, "tcp-ab2c")
    assert cli.main(["winlab", "remove", MACHINE, "--script-only"]) == 0
    assert "$Machine = '%s'" % MACHINE in capsys.readouterr().out
    assert winlab.load()[MACHINE]["window"]["project"] == "tcp-ab2c"
    with pytest.raises(winlab.WinlabError, match="a machine name"):
        winlab.removal_script("Lab'1")


# --------------------------------------------------------------------------- the templates and the package


def test_wl_the_templates_are_ascii_and_every_placeholder_is_filled():
    for name, text in SCRIPTS.items():
        assert text.isascii(), name
        assert "\u2014" not in text
    assert set(re.findall(r"@@([A-Z_]+)@@", SETUP)) == {"MACHINE", "PORT", "ACCOUNT", "HOST_KEY", "KNOWN_HOST",
                                                         "TUNNEL", "TUNNEL_USER", "SSH_PORT"}
    assert set(re.findall(r"@@([A-Z_]+)@@", REMOVE)) == {"MACHINE"}
    # every filled value lands in a single-quoted PowerShell string, which the checked values cannot end
    for text in SCRIPTS.values():
        for line in text.splitlines():
            for m in re.finditer(r"@@[A-Z_]+@@", line):
                if not line.lstrip().startswith("#"):
                    assert line[:m.start()].count("'") % 2 == 1, line


def test_wl_the_scripts_ship_with_the_package_and_restart_nothing():
    assert winlab.SCRIPTS.name == "winlab" and winlab.SCRIPTS.parent.name == "awb"
    assert not (winlab.SCRIPTS / "__init__.py").exists()             # data, not a package: awb.winlab is the module
    assert winlab.__file__.endswith("winlab.py")
    assert "awb/winlab/" in deploy.PACKAGE_DATA
    changed = ["awb/winlab/setup.ps1", "awb/winlab/remove.ps1"]
    eff = deploy.map_changes(changed, {}, {"awb-x.service": None}, {}, reached=set())
    assert eff.units == {} and eff.unmapped == [] and eff.every == []
    # a module put under the folder is still a module
    eff = deploy.map_changes(["awb/winlab/new.py"], {}, {"awb-x.service": set()}, {}, reached=set())
    assert eff.unmapped == ["awb/winlab/new.py"]
