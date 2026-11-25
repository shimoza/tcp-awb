"""Owner's word of 2026-11-25 (ask 2026-11-25-tcp-bytg-4), changed by task B-080 (his word of the same day): a binary
file (a Veeam backup, an image, a program) and a text file with a key come with a get like every other file, in
every window: only the SHA-256 compare runs. There is no public image window and no switch in the host file."""
import hashlib

from awb import cli, winlab
from tests import fixtures
from tests.test_hooks import CODE, project  # noqa: F401 - the fixture
from tests.test_wl_session import KEY_ID, KEY_SECRET, MACHINE, WINDIR, lab, logs  # noqa: F401 - the fixture


def _backup() -> bytearray:
    body = "\nak = %s\nsk = %s\nfor %s\n" % (KEY_ID, KEY_SECRET, fixtures.CUSTOMER_FORMS[0])
    return bytearray(b"VBK-STORAGE\x00\x01" + b"\x00" * 500 + body.encode() + b"\x00" * 512)


def _window():
    """The window of the owner as it is opened now: one kind only."""
    winlab.close(MACHINE)
    winlab.open_(MACHINE, 60, CODE)


def test_a_backup_file_passes_with_no_check_in_a_public_window(lab, capsys):
    _window()
    for name in ("agent2.vbk", "agent2.vib", "agent2.vbm"):
        body = _backup()
        lab.files[(WINDIR + name).lower()] = body
        assert cli.main(["winlab", "get", WINDIR + name, name]) == 0, capsys.readouterr().err
        assert (lab.root / name).read_bytes() == bytes(body)
        assert hashlib.sha256(bytes(body)).hexdigest() in logs(lab.root)


def test_a_text_file_keeps_its_checks_in_a_public_window(lab, capsys):
    _window()
    body = ("ak = %s\nsk = %s\n" % (KEY_ID, KEY_SECRET)).encode()
    lab.files[(WINDIR + "keys.txt").lower()] = bytearray(body)
    assert cli.main(["winlab", "get", WINDIR + "keys.txt", "keys.txt"]) == 0, capsys.readouterr().err
    assert (lab.root / "keys.txt").read_bytes() == body
    assert "secret(s)" not in capsys.readouterr().err


def test_a_backup_file_keeps_its_checks_in_a_plain_window(lab, capsys):
    body = _backup()
    lab.files[(WINDIR + "agent2.vbk").lower()] = body
    assert cli.main(["winlab", "get", WINDIR + "agent2.vbk", "agent2.vbk"]) == 0, capsys.readouterr().err
    assert "deleted" not in capsys.readouterr().err
    assert (lab.root / "agent2.vbk").read_bytes() == bytes(body)


def test_with_the_checks_off_a_text_file_with_a_key_passes_get_and_put(lab, capsys, tmp_path, monkeypatch):
    """The owner's word of 2026-11-25: no name check and no secret check of WinLab exists, so a winlab_checks line
    in the host file changes nothing (on or off) and the module has no switch for it; the SHA-256 compare of a get
    stays."""
    from awb import winlabsession

    conf = tmp_path / "paths.conf"
    monkeypatch.setenv("AWB_CONF", str(conf))
    assert not hasattr(winlabsession, "content_checks") and not hasattr(winlabsession, "CHECKS_KEY")
    body = ("ak = %s\nsk = %s\nfor %s\n" % (KEY_ID, KEY_SECRET, fixtures.CUSTOMER_FORMS[0])).encode()
    for number, line in enumerate(("winlab_checks = off\n", "winlab_checks = on\n")):
        conf.write_text(line)
        name = "keys%d.txt" % number
        lab.files[(WINDIR + name).lower()] = bytearray(body)
        assert cli.main(["winlab", "get", WINDIR + name, name]) == 0, capsys.readouterr().err
        assert (lab.root / name).read_bytes() == body
        winlabsession.guard_text("echo %s" % KEY_SECRET)
        (lab.root / "winlab-out" / name).write_bytes(body)
        put = winlabsession.put("winlab-out/" + name, WINDIR + "put-" + name)
        assert put["sha256"] == hashlib.sha256(body).hexdigest() and "check" not in put
        assert bytes(lab.files[(WINDIR + "put-" + name).lower()]) == body
