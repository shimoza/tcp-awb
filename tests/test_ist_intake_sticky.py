"""Task IST (2026-11-11): an intake never stops half way on a report it cannot replace.

The outbox folder of a project carries the sticky bit; an older intake-report.md of another user could not be
replaced, the run failed after the originals had moved and no private report was written. The other user is
played by a different effective uid (os.geteuid patched), since a test cannot chown."""
from __future__ import annotations

import os

import pytest

from awb import intake
from awb import report as _report
from tests import fixtures as fx


def _note(home):
    f = home.inbox / "notiz.txt"
    f.write_text("termin am Standort, Server 198.51.100.4\n", encoding="utf-8")
    return f


def test_ist_a_report_of_another_user_in_a_sticky_folder_refuses_before_anything_moves(home, monkeypatch):
    outdir = home.outbox / fx.CUSTOMER_CODE
    outdir.mkdir(parents=True, exist_ok=True)
    old = outdir / _report.PUBLIC_NAME
    old.write_text("older report\n", encoding="utf-8")
    os.chmod(outdir, 0o3770)
    f = _note(home)
    me = os.geteuid()
    monkeypatch.setattr(intake.os, "geteuid", lambda: me + 1)
    with pytest.raises(intake.IntakeError, match="sticky bit.*nothing of this run was written or moved"):
        intake.run([f], fx.CUSTOMER_CODE, home)
    assert f.exists() and old.read_text(encoding="utf-8") == "older report\n"
    assert sorted(x.name for x in outdir.iterdir()) == [_report.PUBLIC_NAME]


def test_ist_a_report_that_fails_to_write_takes_the_run_back(home, monkeypatch):
    f = _note(home)
    before = f.read_bytes()

    def fail(path, text):
        raise PermissionError(13, "denied")

    monkeypatch.setattr(intake._report, "write_public", fail)
    with pytest.raises(intake.IntakeError, match="could not be written \\(PermissionError\\); the run was taken back"):
        intake.run([f], fx.CUSTOMER_CODE, home)
    assert f.read_bytes() == before
    outdir = home.outbox / fx.CUSTOMER_CODE
    assert not outdir.exists() or not [x for x in outdir.iterdir() if x.name.startswith("F-")]


def test_ist_our_own_report_in_a_sticky_folder_is_replaced(home):
    outdir = home.outbox / fx.CUSTOMER_CODE
    outdir.mkdir(parents=True, exist_ok=True)
    (outdir / _report.PUBLIC_NAME).write_text("older report\n", encoding="utf-8")
    os.chmod(outdir, 0o3770)
    res = intake.run([_note(home)], fx.CUSTOMER_CODE, home)
    assert "older report" not in res.public_report.read_text(encoding="utf-8")
