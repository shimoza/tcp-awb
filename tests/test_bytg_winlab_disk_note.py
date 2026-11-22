"""tcp-bytg 2026-11-22: `awb winlab get` of a 10.3 GB disk image in a window opened without --public-image ran for
15 minutes and then deleted the copy, because the secret check after the transfer hit. The get now says before the
transfer that a disk image of such a window gets the secret check after it and names --public-image of awb winlab
open; a public image window and a plain file get no such line."""
from __future__ import annotations

from awb import cli, winlab, winlabsession
from tests.test_hooks import project  # noqa: F401 (fixture)
from tests.test_wl_session import CODE, MACHINE, WINDIR, lab  # noqa: F401 (fixture)

DISK = b"vhdxfile" + bytes(64 * 1024 - 8) + b"plain sectors of an invented lab disk\r\n" * 64


def test_bytg_disk_note_a_disk_image_of_a_plain_window_says_it_before_the_transfer(lab, capsys):
    lab.files[(WINDIR + "disk.vhdx").lower()] = bytearray(DISK)
    assert cli.main(["winlab", "get", WINDIR + "disk.vhdx", "disk.vhdx"]) == 0
    err = capsys.readouterr().err
    assert "is a disk image and the window of %s was opened without --public-image" % MACHINE in err
    assert "--public-image` in his own terminal" in err
    assert err.index("note: ") == err.find("note: ")                # once, before anything else of the get


def test_bytg_disk_note_by_the_target_suffix_too(lab, capsys):
    lab.files[(WINDIR + "export.bin").lower()] = bytearray(DISK)
    assert cli.main(["winlab", "get", WINDIR + "export.bin", "disk.VHDX"]) == 0
    assert "is a disk image" in capsys.readouterr().err


def test_bytg_disk_note_none_for_a_public_window_or_a_plain_file(lab, capsys):
    lab.files[(WINDIR + "report.txt").lower()] = bytearray(b"report of the run\n")
    assert cli.main(["winlab", "get", WINDIR + "report.txt", "report.txt"]) == 0
    assert "is a disk image" not in capsys.readouterr().err
    winlab.close(MACHINE)
    winlab.open_(MACHINE, 60, CODE, public_image=True)
    lab.files[(WINDIR + "disk.vhdx").lower()] = bytearray(DISK)
    assert cli.main(["winlab", "get", WINDIR + "disk.vhdx", "disk.vhdx"]) == 0
    assert "is a disk image" not in capsys.readouterr().err
    assert winlabsession._disk_suffix("C:\\a\\B.Qcow2", winlabsession.Path("x")) is True
