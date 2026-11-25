"""tcp-bytg 2026-11-25 (ask 2026-11-25-tcp-bytg-2), changed by task B-080 (the owner's word of 2026-11-25): a vendor
program (ELF or PE) comes with a get like every other file, in every window: only the SHA-256 compare runs, no secret
check. There is no public image window any more."""
import hashlib

import pytest

from awb import cli, winlab
from tests.test_hooks import CODE, project  # noqa: F401 - the fixture
from tests.test_wl_session import KEY_ID, KEY_SECRET, MACHINE, WINDIR, lab, logs  # noqa: F401 - the fixture


def _program(magic: bytes) -> bytearray:
    return bytearray(magic + b"\x00" * 504 + ("\nak = %s\nsk = %s\n" % (KEY_ID, KEY_SECRET)).encode() + b"\x00" * 512)


def test_a_program_keeps_the_secret_check_in_a_plain_window(lab, capsys):
    lab.files[(WINDIR + "extract").lower()] = _program(b"\x7fELF")
    assert cli.main(["winlab", "get", WINDIR + "extract", "extract"]) == 0, capsys.readouterr().err
    err = capsys.readouterr().err
    assert "secret(s)" not in err and "deleted" not in err
    assert (lab.root / "extract").read_bytes() == bytes(lab.files[(WINDIR + "extract").lower()])


def test_a_program_skips_the_secret_check_in_a_public_image_window(lab, capsys):
    winlab.close(MACHINE)
    winlab.open_(MACHINE, 60, CODE)
    with pytest.raises(TypeError):
        winlab.open_(MACHINE, 60, CODE, public_image=True)      # the option is gone
    for name, magic in (("extract", b"\x7fELF"), ("tool.exe", b"MZ")):
        body = _program(magic)
        lab.files[(WINDIR + name).lower()] = body
        assert cli.main(["winlab", "get", WINDIR + name, name]) == 0, capsys.readouterr().err
        assert (lab.root / name).read_bytes() == bytes(body)
        assert hashlib.sha256(bytes(body)).hexdigest() in logs(lab.root)
    assert "secret check" not in logs(lab.root) and "public image" not in logs(lab.root)
