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
