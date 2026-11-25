"""tcp-bytg 2026-11-22, changed by task B-080 (the owner's word of 2026-11-25): `awb winlab get` of a disk image
runs no secret check after the transfer, so it says nothing before it about a check or --public-image: the image is
published after the SHA-256 compare, in every window; stderr carries no note and no check progress line."""
from __future__ import annotations

import hashlib

from awb import cli, winlab, winlabsession
from tests.test_hooks import project  # noqa: F401 (fixture)
from tests.test_wl_session import CODE, MACHINE, WINDIR, lab, logs  # noqa: F401 (fixture)

DISK = b"vhdxfile" + bytes(64 * 1024 - 8) + b"plain sectors of an invented lab disk\r\n" * 64


def _no_note(err: str) -> None:
    assert "is a disk image" not in err and "--public-image" not in err and "note: " not in err
    assert "check" not in err and "secret" not in err


def test_bytg_disk_note_a_disk_image_of_a_plain_window_says_it_before_the_transfer(lab, capsys):
    lab.files[(WINDIR + "disk.vhdx").lower()] = bytearray(DISK)
    assert cli.main(["winlab", "get", WINDIR + "disk.vhdx", "disk.vhdx"]) == 0
    _no_note(capsys.readouterr().err)
    assert (lab.root / "disk.vhdx").read_bytes() == DISK
    assert hashlib.sha256(DISK).hexdigest() in logs(lab.root)
    assert not hasattr(winlabsession, "DISK_NOTE")


def test_bytg_disk_note_by_the_target_suffix_too(lab, capsys):
    lab.files[(WINDIR + "export.bin").lower()] = bytearray(DISK)
    assert cli.main(["winlab", "get", WINDIR + "export.bin", "disk.VHDX"]) == 0
    _no_note(capsys.readouterr().err)
    assert (lab.root / "disk.VHDX").read_bytes() == DISK


def test_bytg_disk_note_none_for_a_public_window_or_a_plain_file(lab, capsys):
    lab.files[(WINDIR + "report.txt").lower()] = bytearray(b"report of the run\n")
    assert cli.main(["winlab", "get", WINDIR + "report.txt", "report.txt"]) == 0
    _no_note(capsys.readouterr().err)
    winlab.close(MACHINE)
    winlab.open_(MACHINE, 60, CODE)
    lab.files[(WINDIR + "disk.vhdx").lower()] = bytearray(DISK)
    assert cli.main(["winlab", "get", WINDIR + "disk.vhdx", "disk.vhdx"]) == 0
    _no_note(capsys.readouterr().err)
    assert not hasattr(winlabsession, "_disk_suffix")
