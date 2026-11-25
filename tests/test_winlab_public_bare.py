"""Owner's word of 2026-11-25 (ask 2026-11-25-tcp-bytg-4): in a --public-image window a binary file (a Veeam backup,
an image, a program) gets no name and no secret check; a text file keeps both, and a plain window keeps
the checks of a binary file."""
from awb import cli, winlab
from tests import fixtures
from tests.test_hooks import CODE, project  # noqa: F401 - the fixture
from tests.test_wl_session import KEY_ID, KEY_SECRET, MACHINE, WINDIR, lab, logs  # noqa: F401 - the fixture


def _backup() -> bytearray:
    body = "\nak = %s\nsk = %s\nfor %s\n" % (KEY_ID, KEY_SECRET, fixtures.CUSTOMER_FORMS[0])
    return bytearray(b"VBK-STORAGE\x00\x01" + b"\x00" * 500 + body.encode() + b"\x00" * 512)


def _public():
    winlab.close(MACHINE)
    winlab.open_(MACHINE, 60, CODE, public_image=True)


def test_a_backup_file_passes_with_no_check_in_a_public_window(lab, capsys):
    _public()
    for name in ("agent2.vbk", "agent2.vib", "agent2.vbm"):
        lab.files[(WINDIR + name).lower()] = _backup()
        assert cli.main(["winlab", "get", WINDIR + name, name]) == 0, capsys.readouterr().err
        assert (lab.root / name).exists()


def test_a_text_file_keeps_its_checks_in_a_public_window(lab, capsys):
    _public()
    lab.files[(WINDIR + "keys.txt").lower()] = bytearray(("ak = %s\nsk = %s\n" % (KEY_ID, KEY_SECRET)).encode())
    assert cli.main(["winlab", "get", WINDIR + "keys.txt", "keys.txt"]) == 1
    assert "secret(s): the copy was deleted" in capsys.readouterr().err


def test_a_backup_file_keeps_its_checks_in_a_plain_window(lab, capsys):
    lab.files[(WINDIR + "agent2.vbk").lower()] = _backup()
    assert cli.main(["winlab", "get", WINDIR + "agent2.vbk", "agent2.vbk"]) == 1
    assert "the copy was deleted" in capsys.readouterr().err
    assert not (lab.root / "agent2.vbk").exists()
