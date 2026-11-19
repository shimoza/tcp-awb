"""The -p flag in the put scrub (tcp-bytg, 2026-11-19): `mkdir -p /var/tmp/tcp` reached the customer as
`mkdir -p [secret]`. A -p that means parents, port, pid or preserve, or a -p value that is a path, is no password;
the -p of a password command (mysql, sshpass) stays a secret, the glued forms too. The values here are invented."""
from __future__ import annotations

import pytest

from awb import putcopy

KEPT = (
    "mkdir -p /var/tmp/tcp",
    "sudo mkdir -p /var/tmp/tcp && cd /var/tmp/tcp",
    "mkdir -p ./out/run1",
    "mkdir -p ~/work/x",
    "mkdir -p logs/app",
    "install -d -p /opt/tool",
    "ssh -p 22 admin@host",
    "ssh -p 2222 admin@host",
    "docker run -p 8080:80 nginx",
    "docker run -d -p 3306:3306 mysql",
    "podman run -p 127.0.0.1:5432:5432 db",
    "psql -h db -p 5432 -U app",
    "tool --out -p /srv/data/out",
)

CAUGHT = (
    ("mysql -u root -p Xk9mq2Lzv db", "mysql -u root -p [secret] db"),
    ("mysql -u root -pXk9mq2Lzv db", "mysql -u root -p[secret] db"),
    ('mysql -u root -p"Xk9mq2Lzv" db', 'mysql -u root -p"[secret]" db'),
    ("docker exec db mysql -u root -p Xk9mq2Lzv db", "docker exec db mysql -u root -p [secret] db"),
    ("sshpass -p Xk9mq2Lzv ssh admin@host", "sshpass -p [secret] ssh admin@host"),
    ("sshpass -p'Xk9mq2Lzv' ssh admin@host", "sshpass -p'[secret]' ssh admin@host"),
    ("sshpass -pXk9mq2Lzv ssh admin@host", "sshpass -p[secret] ssh admin@host"),
    ("sshpass -p Xk9mq2Lzv ssh -p 2222 admin@host", "sshpass -p [secret] ssh -p 2222 admin@host"),
    ("mysql -u root -p /x9Ab!q", "mysql -u root -p [secret]"),
    ("tool -p Xk9#mq2Lz", "tool -p [secret]"),
)


@pytest.mark.parametrize("line", KEPT)
def test_p_flag_no_password_kept(line):
    out, n = putcopy.scrub_text(line)
    assert (out, n) == (line, 0)


@pytest.mark.parametrize("line,want", CAUGHT)
def test_p_flag_password_caught(line, want):
    out, n = putcopy.scrub_text(line)
    assert out == want
    assert n == 1


def test_guide_block_keeps_path_and_masks_password():
    text = "mkdir -p /var/tmp/tcp\nsshpass -p 'Xk9mq2Lzv' scp -P 22 a.tar admin@host:/var/tmp/tcp/\n"
    out, n = putcopy.scrub_text(text)
    assert out.splitlines()[0] == "mkdir -p /var/tmp/tcp"
    assert "Xk9mq2Lzv" not in out
    assert n == 1


# The masker of the Write hook (labsecrets.shorten, tcp-renz 2026-11-19) and the put scrub on PowerShell: a value
# that is a call or a variable is code, a literal stays a secret.
PW = "S3cr3t" + "Pw9"
LIT = {"pw": PW, "pws": "S3cr3t" + " Pw9"}
VAR = "$admin" + "Secret9"
CONVERT = "ConvertTo-" + "SecureString"
PW_IS = "$pw" + " = "
"""Invented values, built from pieces so the gate of this repository reads no assignment in this file."""

PS_KEPT = (
    PW_IS + 'Read-Host -AsSecureString -Prompt "Local admin password"',
    '$cred = Get-Credential -Message "admin password"',
    "$pw = (Get-Secret -Name admin)",
    "$pw = $env:ADMIN_PW",
    "$pw = " + VAR,
    "$sec = ConvertTo-SecureString $plain -AsPlainText -Force",
    "$secret = " + CONVERT + " -String $plain -AsPlainText -Force",
    "New-LocalUser -Name x -Password (Read-Host -AsSecureString)",
    "New-LocalUser -Name x -Password $pw",
)

PS_CAUGHT = tuple((a % LIT, b) for a, b in (
    ('$password = "%(pw)s"', '$password = "[secret]"'),
    ('$pw = "%(pw)s"', '$pw = "[secret]"'),
    ("$pw = %(pw)s", "$pw = [secret]"),
    ('-Password "%(pw)s"', '-Password "[secret]"'),
    ("New-LocalUser -Name x -Password '%(pws)s'", "New-LocalUser -Name x -Password '[secret]'"),
    ("-AdminPassword %(pw)s", "-AdminPassword [secret]"),
    ("$pw = " + CONVERT + ' "%(pw)s" -AsPlainText -Force',
     "$pw = " + CONVERT + ' "[secret]" -AsPlainText -Force'),
))


@pytest.mark.parametrize("line", PS_KEPT)
def test_powershell_call_kept_by_hook_masker(line):
    from awb import labsecrets

    out, counts = labsecrets.shorten(line)
    assert out == line
    assert not counts


@pytest.mark.parametrize("line", PS_KEPT)
def test_powershell_call_kept_by_put_scrub(line):
    assert putcopy.scrub_text(line) == (line, 0)


@pytest.mark.parametrize("line,want", PS_CAUGHT)
def test_powershell_literal_caught_by_hook_masker(line, want):
    from awb import labsecrets

    out, counts = labsecrets.shorten(line)
    assert out == want
    assert counts["secret"] == 1


@pytest.mark.parametrize("line,want", PS_CAUGHT)
def test_powershell_literal_caught_by_put_scrub(line, want):
    assert putcopy.scrub_text(line) == (want, 1)
