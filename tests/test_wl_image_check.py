"""Task B-040, changed by task B-080 (the owner's word of 2026-11-25): a disk image built in the lab goes to a lab
machine and comes back at transfer speed, like every other file. WinLab runs no name check and no secret check of a
put or a get (awb/winlabsession.py): the file arrives with its SHA-256 and nothing is deleted for a name or a secret.

The disk image kinds (.vhdx, .vhd, .vmdk, .qcow2, .iso, .img with their signatures), a file named like one without
its signature and a raw .img of a size that is not whole sectors all pass alike, even when their bytes hold a secret
or a registered name of tests/fixtures.py. Every secret is made at run time; the noise is os.urandom. The speed bound
is 2 s for a put and a get of 4 MB on the build host; AWB_TEST_IMAGE_CHECK_SECONDS widens it on a slow box.
"""
from __future__ import annotations

import hashlib
import os
import random
import string
import time

import pytest

from awb import hooks, winlabsession
from tests import fixtures
from tests.test_hooks import project  # noqa: F401 - the fixture of lab
from tests.test_wl_session import WINDIR, lab, logs  # noqa: F401 - the fixture

_R = random.Random(20261106)
SPEED = float(os.environ.get("AWB_TEST_IMAGE_CHECK_SECONDS", "2"))
NAME = fixtures.CUSTOMER_FORMS[0]


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
HOLDS = ("password = %s\nfor %s\n" % (WORD, NAME)).encode()
"""A secret and a registered name: what the checks of before refused."""


def _image(folder, name: str, offset: int = 0, mark: bytes = b"", size: int = SIZE, body: bytes = b""):
    data = bytearray(size)
    data[offset:offset + len(mark)] = mark
    data[1024:1024 + len(body)] = body
    path = folder / name
    path.write_bytes(bytes(data[:size]))
    return path


@pytest.fixture
def name_check(monkeypatch):
    """A fake name check that finds nothing and records each call: it is never called."""
    calls: list[str] = []

    def whole(path, p=None):
        calls.append("whole")
        return []

    def pieces(text, p=None):
        calls.append("pieces")
        return []

    monkeypatch.setattr(hooks, "file_hits", whole)
    monkeypatch.setattr(hooks, "name_hits", pieces)
    return calls


@pytest.fixture
def no_name_check(monkeypatch):
    def refuse(*args, **kwargs):
        pytest.fail("a name check ran on a file of WinLab")

    monkeypatch.setattr(hooks, "file_hits", refuse)
    monkeypatch.setattr(hooks, "name_hits", refuse)


def _through(lab, path, winname: str | None = None) -> dict:
    """Put `path` (in winlab-out/) to the machine and get it back: both pass, the file arrives with its SHA-256 and
    nothing is deleted. Returns the result of the put."""
    data = path.read_bytes()
    sha = hashlib.sha256(data).hexdigest()
    winpath = WINDIR + (winname or path.name)
    put = winlabsession.put("winlab-out/" + path.name, winpath)
    assert "check" not in put and put["sha256"] == sha and put["size"] == len(data)
    assert bytes(lab.files[winpath.lower()]) == data
    back = winlabsession.get(winpath, "back-" + path.name)
    assert "check" not in back and back["sha256"] == sha and back["size"] == len(data)
    assert (lab.root / ("back-" + path.name)).read_bytes() == data
    assert not list(lab.root.glob("*.awb-part")) and not list(lab.root.glob(".winlab-*"))
    return put


@pytest.mark.parametrize("name, kind, offset, mark", SIGNED)
def test_wl_each_signature_makes_a_disk_image_that_gets_the_secret_check_only(lab, no_name_check, name, kind,
                                                                            offset, mark):
    path = _image(lab.root / "winlab-out", name, offset, mark, body=HOLDS)
    _through(lab, path)
    assert WORD not in logs(lab.root) and NAME not in logs(lab.root)


@pytest.mark.parametrize("name, kind, offset, mark", [s for s in SIGNED if s[1] != "img"])
def test_wl_the_name_of_a_disk_image_without_its_signature_gets_the_full_check(lab, name_check, name, kind,
                                                                             offset, mark):
    out = lab.root / "winlab-out"
    _through(lab, _image(out, name, body=HOLDS))
    # the signature in a file of another name passes alike
    _through(lab, _image(out, "disk.bin", offset, mark, body=HOLDS))
    assert name_check == []


def test_wl_an_img_whose_size_is_not_whole_sectors_gets_the_full_check(lab, name_check):
    _through(lab, _image(lab.root / "winlab-out", "raw.img", size=SIZE + 100, body=HOLDS))
    assert name_check == []


@pytest.mark.parametrize("body", ["password = %s\n" % WORD, KEY_BLOCK], ids=["password", "private-key"])
def test_wl_a_disk_image_with_a_secret_is_refused_without_the_name_check(lab, no_name_check, body):
    noise = random.Random(1).randbytes(2048)
    path = _image(lab.root / "winlab-out", "lab.vhdx", 0, b"vhdxfile",
                  body=noise + b"\n" + body.encode() + b"\n" + noise)
    _through(lab, path)
    assert WORD not in logs(lab.root) and "PRIVATE KEY" not in logs(lab.root)


@pytest.mark.parametrize("body, cut", [("password = %s\n" % WORD, 4), (KEY_BLOCK, 12)], ids=["password", "private-key"])
def test_wl_a_secret_across_the_border_of_two_pieces_of_a_disk_image_is_refused(lab, no_name_check, body, cut):
    start = 4096 - cut                                  # a border of 4096 bytes falls inside the secret
    data = bytearray(SIZE)
    data[:8] = b"vhdxfile"
    data[start - 1:start + len(body) + 1] = b"\n" + body.encode() + b"\n"
    path = lab.root / "winlab-out" / "lab.vhdx"
    path.write_bytes(bytes(data))
    _through(lab, path)


@pytest.mark.wallclock
def test_wl_the_secret_check_of_4_mb_of_random_bytes_keeps_up_with_the_transfer(lab, no_name_check):
    # random noise with a secret in it: no check runs and nothing delays the put or the get
    noise = os.urandom(winlabsession.CHUNK)
    data = b"vhdxfile" + noise[:1 << 20] + b"\npassword = %s\n" % WORD.encode() + noise[1 << 20:]
    path = lab.root / "winlab-out" / "noise.vhdx"
    path.write_bytes(data)
    sha = hashlib.sha256(data).hexdigest()
    t0 = time.perf_counter()
    put = winlabsession.put("winlab-out/noise.vhdx", WINDIR + "noise.vhdx")
    got = winlabsession.get(WINDIR + "noise.vhdx", "noise-back.vhdx")
    elapsed = time.perf_counter() - t0
    assert put["sha256"] == got["sha256"] == sha
    assert (lab.root / "noise-back.vhdx").read_bytes() == data
    assert elapsed <= SPEED, "a put and a get of 4 MB of random bytes took %.1f s" % elapsed


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
def test_wl_a_known_secret_in_random_noise_is_still_found(lab, no_name_check, secret):
    noise = os.urandom(256 * 1024)
    path = lab.root / "winlab-out" / "noise.bin"
    path.write_bytes(noise + b"\n" + secret.encode() + b"\n" + noise)
    _through(lab, path)


def test_wl_a_jwk_with_its_kty_far_from_its_private_part_is_still_found(lab, no_name_check):
    noise = os.urandom(256 * 1024)
    private = _run(string.ascii_letters + string.digits, 32)
    path = lab.root / "winlab-out" / "key.json"
    path.write_bytes(b'{"kty": "EC",\n' + noise + b'\n"d": "%s"}' % private.encode())
    _through(lab, path)


def test_wl_a_finding_of_the_lines_goes_to_the_whole_check_for_its_count(lab, no_name_check):
    noise = os.urandom(64 * 1024)
    path = lab.root / "winlab-out" / "many.bin"
    path.write_bytes(noise + b"\npassword = %s\n%s\n%s\n" % (WORD.encode(), KEY_ID.encode(), KEY_SECRET.encode())
                     + noise)
    # several secrets in one file: the put and the get pass, no count of them is made or reported
    put = _through(lab, path)
    assert set(put) == {"machine", "size", "sha256", "resumed", "log"}
    rows = lab.root / "winlab-out" / "rows.bin"
    rows.write_bytes(b"lab row\n" * 100)
    _through(lab, rows)


def test_wl_the_log_of_a_put_names_the_check_that_ran(lab, capsys):
    out = lab.root / "winlab-out"
    _image(out, "lab.vhdx", 0, b"vhdxfile", body=HOLDS)
    (out / "notes.txt").write_text("plain lab text\n", encoding="utf-8")
    first = winlabsession.put("winlab-out/lab.vhdx", WINDIR + "lab.vhdx")
    second = winlabsession.put("winlab-out/notes.txt", WINDIR + "notes.txt")
    assert "check" not in first and "check" not in second
    text = logs(lab.root)
    # the log names the size and the SHA-256 of each file and no check: none ran
    assert "sha256 %s, whole" % first["sha256"] in text and "sha256 %s, whole" % second["sha256"] in text
    assert "check" not in text and WORD not in text and NAME not in text
