"""The first awb egress allow on a host with no grants file (ask 2026-11-23-tcp-ui5o-3).

The shared ledger belongs to the work user and the owner may only read it, so ledger.add raised PermissionError
and the grant failed with "the grants file could not be read or written safely". The entry now goes to the work
user (sudo -n -u WORK awb ledger add); its name check still stops the grant. Every path is a tmp path, sudo is a
recorder.
"""
from __future__ import annotations

import subprocess

import pytest

from awb import config, egressgrant, ledger
from tests.test_egress import EGRESS, ME, Clock
from tests.test_egress_grants import NOW


class Sudo:
    def __init__(self, code: int = 0):
        self.code = code
        self.calls: list[list[str]] = []

    def __call__(self, cmd, **kw):
        self.calls.append(cmd)
        return subprocess.CompletedProcess(cmd, self.code)


@pytest.fixture
def first(tmp_path, monkeypatch):
    folder = tmp_path / "home" / ".config" / "awb"

    def no_write(*a, **k):
        raise PermissionError(13, "denied")

    monkeypatch.setattr(ledger, "add", no_write)
    monkeypatch.setattr(config, "work_user", lambda: "awb-test-work")
    sudo = Sudo()
    o = egressgrant.Owner(folder=folder, allow_md=EGRESS / "ALLOW.md",
                          conf_text=lambda: "owner = %s\nwork_user = awb-test-work\n" % ME,
                          user=lambda: ME, geteuid=lambda: 1000, cwd=lambda: tmp_path, now=Clock(NOW), run=sudo)
    return o, sudo


def test_the_first_grant_on_a_host_without_a_grants_file_is_written(first):
    o, sudo = first
    assert not (o.dir() / egressgrant.GRANTS_FILE).exists()
    said = egressgrant.allow(o, "docs.example.org", "tcp-q7m4", "2026-11-27", "vendor docs for the PoC")
    assert "granted docs.example.org" in said
    assert (o.dir() / egressgrant.GRANTS_FILE).read_text().startswith("docs.example.org\ttcp-q7m4\t2026-11-27\t")
    cmd = sudo.calls[0]
    assert cmd[:5] == ["sudo", "-n", "-u", "awb-test-work", "-H"] and cmd[-10:-8] == ["ledger", "add"]
    assert "docs.example.org" not in " ".join(cmd)           # the ledger never carries the host


def test_a_refusal_of_the_work_user_name_check_writes_nothing(first):
    o, sudo = first
    sudo.code = 1
    with pytest.raises(egressgrant.GrantRefused):
        egressgrant.allow(o, "docs.example.org", "tcp-q7m4", "2026-11-27", "vendor docs for the PoC")
    assert not (o.dir() / egressgrant.GRANTS_FILE).exists()
