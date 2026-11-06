"""Task B-040: a disk image built in the lab goes to a lab machine with the secret check only, at transfer speed
(awb/winlabsession.py disk_image, check_file, _image_secret_count; the owner's decision of 2026-11-06).

A disk image is a file whose name ends in .vhdx, .vhd, .vmdk, .qcow2, .iso or .img and whose bytes carry the
format's signature (a raw .img: a size of whole sectors). It never reaches the name check and still gets the secret
check over all of its bytes. Every secret is made at run time; the noise is os.urandom. The speed bound is 2 s per
piece of 4 MB on the build host; AWB_TEST_IMAGE_CHECK_SECONDS widens it on a slow box.
"""
from __future__ import annotations

import os
import random
import string
import time

import pytest

from awb import hooks, winlabsession
from awb.winlab import WinlabError
from tests.test_hooks import project  # noqa: F401 - the fixture of lab
from tests.test_wl_session import WINDIR, lab, logs  # noqa: F401 - the fixture

_R = random.Random(20261106)
SPEED = float(os.environ.get("AWB_TEST_IMAGE_CHECK_SECONDS", "2"))


def _run(chars: str, n: int) -> str:
    return "".join(_R.choice(chars) for _ in range(n))


WORD = "Xq7!" + _run(string.ascii_letters + string.digits, 10)
KEY_ID = _run(string.ascii_uppercase, 10) + _run(string.digits, 10)
KEY_SECRET = _run(string.ascii_lowercase, 14) + _run(string.ascii_uppercase, 13) + _run(string.digits, 13)
SPACED = " ".join(_run(string.ascii_letters, 2) + _run(string.digits, 1) for _ in range(6))
"""A quoted value with spaces: no run of 8 characters without one."""
ARMOUR = "-----BEGIN RSA " + "PRI" + "VATE KEY-----"
KEY_BLOCK = ARMOUR + "\n" + _run(string.ascii_lowercase, 40) + "\n" + ARMOUR.replace("BEGIN", "END") + "\n"
SIZE = 64 * 1024

SIGNED = [
    ("disk.vhdx", "vhdx", 0, b"vhdxfile"),
    ("disk.VHD", "vhd", 0, b"conectix"),
    ("disk.vhd", "vhd", SIZE - 512, b"conectix"),         # the footer of a fixed disk
    ("disk.vmdk", "vmdk", 0, b"KDMV"),
    ("disk.vmdk", "vmdk", 0, b"# Disk DescriptorFile\n"),
    ("disk.Qcow2", "qcow2", 0, b"QFI\xfb"),
    ("disk.iso", "iso", 0x8001, b"CD001"),
    ("disk.IMG", "img", 0, b""),
]


def _image(folder, name: str, offset: int = 0, mark: bytes = b"", size: int = SIZE, body: bytes = b""):
    data = bytearray(size)
    data[offset:offset + len(mark)] = mark
    data[1024:1024 + len(body)] = body
    path = folder / name
    path.write_bytes(bytes(data[:size]))
    return path


@pytest.fixture
def name_check(monkeypatch):
    """A fake name check that finds nothing and records each call: "whole" and "pieces"."""
    calls: list[str] = []

    def whole(path, p=None):
        calls.append("whole")
        return []

    def pieces(text):
        calls.append("pieces")
        return 0

    monkeypatch.setattr(hooks, "file_hits", whole)
    monkeypatch.setattr(winlabsession, "_name_count", pieces)
    return calls


@pytest.fixture
def no_name_check(monkeypatch):
    def refuse(*args, **kwargs):
        pytest.fail("the name check ran on a disk image")

    monkeypatch.setattr(hooks, "file_hits", refuse)
    monkeypatch.setattr(winlabsession, "_name_count", refuse)


@pytest.mark.parametrize("name, kind, offset, mark", SIGNED)
def test_wl_each_signature_makes_a_disk_image_that_gets_the_secret_check_only(tmp_path, no_name_check, name, kind,
                                                                            offset, mark):
    path = _image(tmp_path, name, offset, mark)
    assert winlabsession.disk_image(path, SIZE) == kind
    assert winlabsession.check_file(path, SIZE) == "secret check only, disk image %s" % kind


@pytest.mark.parametrize("name, kind, offset, mark", [s for s in SIGNED if s[1] != "img"])
def test_wl_the_name_of_a_disk_image_without_its_signature_gets_the_full_check(tmp_path, name_check, name, kind,
                                                                             offset, mark):
    path = _image(tmp_path, name)
    assert winlabsession.disk_image(path, SIZE) is None
    assert winlabsession.check_file(path, SIZE) == "name and secret check"
    assert name_check == ["whole", "pieces"]
    # the signature in a file of another name is no disk image either
    other = _image(tmp_path, "disk.bin", offset, mark)
    assert winlabsession.disk_image(other, SIZE) is None


def test_wl_an_img_whose_size_is_not_whole_sectors_gets_the_full_check(tmp_path, name_check):
    path = _image(tmp_path, "raw.img", size=SIZE + 100)
    assert winlabsession.disk_image(path, SIZE + 100) is None
    assert winlabsession.check_file(path, SIZE + 100) == "name and secret check"
    assert name_check == ["whole", "pieces"]


@pytest.mark.parametrize("body", ["password = %s\n" % WORD, KEY_BLOCK], ids=["password", "private-key"])
def test_wl_a_disk_image_with_a_secret_is_refused_without_the_name_check(tmp_path, no_name_check, body):
    noise = random.Random(1).randbytes(2048)
    path = _image(tmp_path, "lab.vhdx", 0, b"vhdxfile", body=noise + b"\n" + body.encode() + b"\n" + noise)
    with pytest.raises(WinlabError, match=r"carries \d+ secret\(s\)"):
        winlabsession.check_file(path, SIZE)


@pytest.mark.parametrize("body, cut", [("password = %s\n" % WORD, 4), (KEY_BLOCK, 12)], ids=["password", "private-key"])
def test_wl_a_secret_across_the_border_of_two_pieces_of_a_disk_image_is_refused(tmp_path, monkeypatch,
                                                                                no_name_check, body, cut):
    monkeypatch.setattr(winlabsession, "PIECE", 4096)
    start = 4096 - cut                                  # the border falls inside the key word or the armour line
    data = bytearray(SIZE)
    data[:8] = b"vhdxfile"
    data[start - 1:start + len(body) + 1] = b"\n" + body.encode() + b"\n"
    path = tmp_path / "lab.vhdx"
    path.write_bytes(bytes(data))
    with pytest.raises(WinlabError, match=r"carries \d+ secret\(s\)"):
        winlabsession.check_file(path, SIZE)


def test_wl_the_secret_check_of_4_mb_of_random_bytes_keeps_up_with_the_transfer():
    # today's check takes about one piece of noise in a hundred for a secret (Pwd= and three characters): such a
    # piece goes through the whole check again for its count, so the bound is taken on a piece without one
    for _ in range(5):
        text = winlabsession._piece_text(os.urandom(winlabsession.PIECE))
        if not winlabsession._secret_count(winlabsession._secret_lines(text)):
            break
    t0 = time.perf_counter()
    found = winlabsession._image_secret_count(text)
    elapsed = time.perf_counter() - t0
    assert found == 0
    assert elapsed <= SPEED, "the secret check of 4 MB of random bytes took %.1f s" % elapsed


SECRETS = [
    "password = %s" % WORD,
    "pwd\n:\n%s" % WORD,
    "db_password: |\n  %s" % _run(string.ascii_letters + string.digits, 24),
    'api_key = "%s"' % SPACED,
    "%s\n%s" % (KEY_ID, KEY_SECRET),
    "ghp_" + _run(string.ascii_letters, 20) + _run(string.digits, 16),
    "user | pw\n--- | ---\nadmin | %s" % WORD,
    "user | pw\n--- | ---\n" + "a | b\n" * 6 + "admin | %s" % WORD,      # the row far below its header
    "the pw is %s" % WORD,
    '-p "%s"' % SPACED,
    "https://admin:" + WORD + "@db.lab.invalid/",
    "ConvertTo-SecureString '%s' -AsPlainText -Force" % WORD,
    KEY_BLOCK,
]


@pytest.mark.parametrize("secret", SECRETS)
def test_wl_a_known_secret_in_random_noise_is_still_found(secret):
    assert winlabsession._secret_count(secret) > 0            # the check of today finds it
    noise = winlabsession._piece_text(os.urandom(256 * 1024))
    text = noise + "\n" + secret + "\n" + noise
    assert winlabsession._secret_count(winlabsession._secret_lines(text)) > 0


def test_wl_a_jwk_with_its_kty_far_from_its_private_part_is_still_found():
    noise = winlabsession._piece_text(os.urandom(256 * 1024))
    text = '{"kty": "EC",\n' + noise + '\n"d": "%s"}' % _run(string.ascii_letters + string.digits, 32)
    assert winlabsession._secret_count(winlabsession._secret_lines(text)) > 0


def test_wl_a_finding_of_the_lines_goes_to_the_whole_check_for_its_count(monkeypatch):
    noise = winlabsession._piece_text(os.urandom(64 * 1024))
    text = noise + "\npassword = %s\n%s\n%s\n" % (WORD, KEY_ID, KEY_SECRET) + noise
    whole = winlabsession._secret_count(text)
    assert winlabsession._image_secret_count(text) == whole > 1
    # lines that hold one of the secrets only or one the piece does not hold: the whole piece decides
    monkeypatch.setattr(winlabsession, "_secret_lines", lambda t: "password = %s" % WORD)
    assert winlabsession._image_secret_count(text) == whole
    assert winlabsession._image_secret_count("lab row\n" * 100) == 0


def test_wl_the_log_of_a_put_names_the_check_that_ran(lab, capsys):
    out = lab.root / "winlab-out"
    _image(out, "lab.vhdx", 0, b"vhdxfile")
    (out / "notes.txt").write_text("plain lab text\n", encoding="utf-8")
    assert winlabsession.put("winlab-out/lab.vhdx", WINDIR + "lab.vhdx")["check"] == \
        "secret check only, disk image vhdx"
    assert winlabsession.put("winlab-out/notes.txt", WINDIR + "notes.txt")["check"] == "name and secret check"
    text = logs(lab.root)
    assert "whole, secret check only, disk image vhdx" in text and "whole, name and secret check" in text
