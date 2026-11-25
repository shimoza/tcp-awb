"""tcp-bytg 2026-11-20: `awb winlab get --recheck` of a kept 21.9 GB disk image checked 444 MB in 70 minutes under the
byte budget of the vault daemon's check socket. On the owner's word of 2026-11-25 WinLab runs no content check at all
(WinLab only handles what he downloads from the internet): a get of a disk image of any size is published right after
its SHA-256 compare, with no request to the check socket, no check progress line and no pacing wait;
`--recheck` of a kept image compares the SHA-256 again and publishes it without the machine. The image is a few MB;
the check entry points are stand-ins that count their calls."""
from __future__ import annotations

import json
import os
import random
import signal

import pytest

from awb import check, cli, hooks, vault_client, winlabsession
from tests import fixtures
from tests.test_hooks import project  # noqa: F401 (fixture)
from tests.test_wl_session import WINDIR, lab, logs  # noqa: F401 (fixture)

KB = 1024
MB = 1024 * KB
BLOCK = b"".join(b"plain sectors of an invented lab disk, line %03d\r\n" % n for n in range(80))


def _image(name: bytes = b"", at: int = 0, early: bytes = b"", late: bytes = b"") -> bytes:
    """About 6 MB: a vhdx header, zero blocks, 1 MB of noise, the same text block 256 times between zero blocks and
    `name` (already encoded) at `at`; `early` and `late` are plain markers at 0.5 MB and 5 MB."""
    data = bytearray(b"vhdxfile" + bytes(64 * KB - 8))
    data += bytes(MB)
    data += random.Random(1).randbytes(MB) + bytes(64 * KB)
    for _ in range(256):
        data += BLOCK + bytes(8 * KB - len(BLOCK))
    data += bytes(6 * MB - len(data))
    for mark, pos in ((early, MB // 2), (late, 5 * MB), (name, at)):
        if mark:
            data[pos:pos + len(mark)] = mark
    return bytes(data)


@pytest.fixture
def socket_(monkeypatch):
    """Every way to a name check (the hooks, the check module, the check socket of the vault daemon) counts its call;
    a get must make none."""
    sent: list[str] = []

    def count(*a, **k):
        sent.append(str(a[0])[:60] if a else "")
        return []

    for owner, attr in ((hooks, "name_hits"), (hooks, "file_hits"), (check, "checker"),
                        (vault_client, "_check_one")):
        monkeypatch.setattr(owner, attr, count)
    return sent


def _records(root):
    folder = root / winlabsession.LOG_FOLDER / "transfers"
    return [json.loads(p.read_text()) for p in sorted(folder.glob("get-*.json"))] if folder.is_dir() else []


def _get(*extra):
    """A get of the image with `extra` after the paths (--recheck)."""
    return cli.main(["winlab", "get", WINDIR + "disk.vhdx", "disk.vhdx", *extra])


def _cut_off(monkeypatch):
    """The get gets SIGTERM from the outside right after its SHA-256 compare (as a tool call that ran out of time
    does): the file stays as FILE.awb-part and the record keeps its size and SHA-256."""
    real = winlabsession._write_json
    state = {"done": False}

    def write(path, data):
        if "sha256" in data and not state["done"]:
            state["done"] = True
            os.kill(os.getpid(), signal.SIGTERM)
        return real(path, data)

    monkeypatch.setattr(winlabsession, "_write_json", write)


def test_fast_text_runs_find_ascii_utf8_and_utf16_at_both_alignments(lab, socket_, capsys):
    """The text runs of an image are gone: a registered name in any encoding at any alignment arrives with the
    file."""
    assert not hasattr(winlabsession, "_text_runs") and not hasattr(winlabsession, "_TextRuns")
    word = fixtures.ORG_FORMS[0]
    blobs = (word.encode("utf-8"), word.encode("utf-16-le"), b"\0" + word.encode("utf-16-le"),
             word.encode("utf-16-be"), b"\x07" + word.encode("utf-16-be"))
    data = b"vhdxfile" + b"".join(bytes(300) + blob + bytes(301) for blob in blobs)
    lab.files[(WINDIR + "disk.vhdx").lower()] = bytearray(data)
    assert _get() == 0, capsys.readouterr().err
    assert (lab.root / "disk.vhdx").read_bytes() == data and _records(lab.root) == []
    assert socket_ == []


def test_fast_a_clean_image_is_published_with_few_requests_and_each_repeat_checked_once(lab, socket_, capsys):
    data = _image()
    lab.files[(WINDIR + "disk.vhdx").lower()] = bytearray(data)
    assert _get() == 0, capsys.readouterr().err
    err = capsys.readouterr().err
    assert (lab.root / "disk.vhdx").read_bytes() == data and _records(lab.root) == []
    assert socket_ == []                                         # no request at all, however often a block repeats
    assert "name check" not in err and "checks of the received file" not in err
    assert "sha256" in logs(lab.root)


@pytest.mark.parametrize("encoding", ["ascii", "utf-8", "utf-16-le"])
def test_fast_a_registered_name_in_any_encoding_still_deletes_the_image(lab, socket_, encoding, capsys):
    form = fixtures.CUSTOMER_FORMS[0] if encoding == "ascii" else fixtures.ORG_FORMS[0]
    name = ("owner: %s\r\n" % form).encode(encoding)
    data = _image(name, 4 * MB + MB // 2 + 12345)
    lab.files[(WINDIR + "disk.vhdx").lower()] = bytearray(data)
    assert _get() == 0, capsys.readouterr().err
    err = capsys.readouterr().err
    assert "registered name" not in err and "deleted" not in err
    assert (lab.root / "disk.vhdx").read_bytes() == data and not (lab.root / "disk.vhdx.awb-part").exists()
    assert _records(lab.root) == [] and socket_ == []


def test_fast_a_name_across_two_pieces_is_found(lab, socket_, capsys):
    name = ("owner: %s\r\n" % fixtures.ORG_FORMS[0]).encode("utf-16-le")
    data = _image(name, 4 * MB + MB // 2 - 9)
    lab.files[(WINDIR + "disk.vhdx").lower()] = bytearray(data)
    assert _get() == 0, capsys.readouterr().err
    assert "registered name" not in capsys.readouterr().err
    assert (lab.root / "disk.vhdx").read_bytes() == data and socket_ == []


def test_fast_an_interrupted_check_resumes_at_its_mark_and_does_not_redo_finished_parts(lab, socket_, monkeypatch,
                                                                                        capsys):
    early, late = b"early marker of the invented disk", b"late marker of the invented disk"
    data = _image(early=early, late=late)
    lab.files[(WINDIR + "disk.vhdx").lower()] = bytearray(data)
    _cut_off(monkeypatch)
    assert _get() == 1
    err = capsys.readouterr().err
    assert "stopped by a signal" in err
    part = lab.root / "disk.vhdx.awb-part"
    assert part.read_bytes() == data and not (lab.root / "disk.vhdx").exists()
    (rec,) = _records(lab.root)
    assert rec["size"] == len(data) and rec["sha256"] and "checked" not in rec and "staged" not in rec
    transferred = list(lab.chunks)
    monkeypatch.setattr(winlabsession, "connect", lambda machine: pytest.fail("the recheck connected"))
    assert _get("--recheck") == 0, capsys.readouterr().err
    err = capsys.readouterr().err
    assert "resumed at" not in err and "checks of the received file" not in err
    assert (lab.root / "disk.vhdx").read_bytes() == data and not part.exists() and _records(lab.root) == []
    assert lab.chunks == transferred and socket_ == []


def test_fast_requests_are_paced_under_the_byte_budget(lab, socket_, monkeypatch, capsys):
    """No request of a get goes to the check socket, so there is nothing to pace: the 20000 text runs of this image
    cost no request and no wait."""
    waits: list[float] = []
    monkeypatch.setattr(winlabsession, "_pause", waits.append)
    data = b"vhdxfile" + b"\0".join(b"run %06d of an invented disk" % n for n in range(20_000))
    lab.files[(WINDIR + "disk.vhdx").lower()] = bytearray(data)
    assert _get() == 0, capsys.readouterr().err
    assert (lab.root / "disk.vhdx").read_bytes() == data
    assert socket_ == [] and waits == []
    assert not hasattr(winlabsession, "RUN_PACE_BYTES") and not hasattr(winlabsession, "RUN_PACE_WINDOW")


def test_fast_a_small_image_keeps_the_check_per_piece(lab, socket_, capsys):
    data = b"vhdxfile" + b"plain sectors of an invented lab disk\n" * 100
    lab.files[(WINDIR + "disk.vhdx").lower()] = bytearray(data)
    assert _get() == 0, capsys.readouterr().err
    assert (lab.root / "disk.vhdx").read_bytes() == data
    assert socket_ == [] and "checks of the received file" not in capsys.readouterr().err


def test_fast_a_disk_image_without_check_is_published_after_the_sha256_as_own_lab_output(lab, socket_, monkeypatch,
                                                                                          capsys):
    """A disk image is published after the SHA-256 compare: no name check, no secret check, nothing ledgered."""
    added = []
    from awb import ledger
    monkeypatch.setattr(ledger, "add", lambda kind, done, **kw: added.append((kind, done, kw.get("project"))))
    name = ("owner: %s\r\n" % fixtures.CUSTOMER_FORMS[0]).encode()
    data = _image(name, 4 * MB + MB // 2 + 12345)
    lab.files[(WINDIR + "disk.vhdx").lower()] = bytearray(data)
    assert cli.main(["winlab", "get", WINDIR + "disk.vhdx", "disk.vhdx"]) == 0, capsys.readouterr().err
    assert (lab.root / "disk.vhdx").read_bytes() == data and _records(lab.root) == []
    assert socket_ == [] and added == []
    assert "6.0 MB, sha256 " in logs(lab.root)


def test_fast_a_plain_file_from_the_lab_keeps_its_name_check(lab, socket_, capsys):
    """A plain file arrives as it is, a registered name inside it included."""
    text = ("report of the run\nowner: %s\n" % fixtures.CUSTOMER_FORMS[0]).encode()
    lab.files[(WINDIR + "report.txt").lower()] = bytearray(text)
    assert cli.main(["winlab", "get", WINDIR + "report.txt", "report.txt"]) == 0, capsys.readouterr().err
    assert "registered name" not in capsys.readouterr().err
    assert (lab.root / "report.txt").read_bytes() == text and socket_ == []


def test_fast_a_plain_recheck_publishes_a_kept_image_without_a_check_request(lab, socket_, monkeypatch, capsys):
    """--recheck of a kept disk image verifies it (size, SHA-256 of the transfer) and publishes it: no request with a
    byte of the image, no machine."""
    data = _image()
    lab.files[(WINDIR + "disk.vhdx").lower()] = bytearray(data)
    _cut_off(monkeypatch)
    assert _get() == 1
    assert "stopped by a signal" in capsys.readouterr().err
    monkeypatch.setattr(winlabsession, "connect", lambda machine: pytest.fail("the recheck connected"))
    assert _get("--recheck") == 0, capsys.readouterr().err
    assert socket_ == []
    assert (lab.root / "disk.vhdx").read_bytes() == data and not (lab.root / "disk.vhdx.awb-part").exists()
    assert _records(lab.root) == [] and "compared again without a transfer" in logs(lab.root)
