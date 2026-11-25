"""tcp-bytg 2026-11-25 (ask 2026-11-25-tcp-bytg-2): in a public image window of the owner a vendor program (ELF or
PE) skips the secret check of a get, like a disk image; in a plain window it keeps it, and a plain text file keeps it
in both."""
from awb import cli, winlab
from tests.test_hooks import CODE, project  # noqa: F401 - the fixture
from tests.test_wl_session import KEY_ID, KEY_SECRET, MACHINE, WINDIR, lab, logs  # noqa: F401 - the fixture


def _program(magic: bytes) -> bytearray:
    return bytearray(magic + b"\x00" * 504 + ("\nak = %s\nsk = %s\n" % (KEY_ID, KEY_SECRET)).encode() + b"\x00" * 512)


def test_a_program_keeps_the_secret_check_in_a_plain_window(lab, capsys):
    lab.files[(WINDIR + "extract").lower()] = _program(b"\x7fELF")
    assert cli.main(["winlab", "get", WINDIR + "extract", "extract"]) == 1
    assert "secret(s): the copy was deleted" in capsys.readouterr().err
    assert not (lab.root / "extract").exists()


def test_a_program_skips_the_secret_check_in_a_public_image_window(lab, capsys):
    winlab.close(MACHINE)
    winlab.open_(MACHINE, 60, CODE, public_image=True)
    for name, magic in (("extract", b"\x7fELF"), ("tool.exe", b"MZ")):
        lab.files[(WINDIR + name).lower()] = _program(magic)
        assert cli.main(["winlab", "get", WINDIR + name, name]) == 0, capsys.readouterr().err
        assert (lab.root / name).exists()
    assert "secret check skipped: a public image window of the owner" in logs(lab.root)
