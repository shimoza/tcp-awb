"""tcp-bytg 2026-11-20: `awb winlab get --recheck` of a kept 21.9 GB disk image checked 444 MB in 70 minutes, one
request of the vault daemon's check socket per part of each 4 MB piece under its byte budget (16 MB a minute per uid),
so the whole image would have taken two days. A large disk image now gets the name check over the unique text runs
of its bytes (ASCII and UTF-8, UTF-16 little and big endian), zero blocks and repeats dropped, in large paced requests,
with a resume mark and a progress line. The image is a few MB with patched limits; the check socket is a fake that
counts requests and answers from the fixture register."""
from __future__ import annotations

import json
import random

import pytest

from awb import check, cli, config, vault_client, winlabsession
from tests import fixtures
from tests.test_hooks import project  # noqa: F401 (fixture)
from tests.test_wl_session import WINDIR, lab, logs  # noqa: F401 (fixture)

KB = 1024
MB = 1024 * KB
BLOCK = b"".join(b"plain sectors of an invented lab disk, line %03d\r\n" % n for n in range(80))


def _image(name: bytes = b"", at: int = 0, early: bytes = b"", late: bytes = b"") -> bytes:
    """About 6 MB: a vhdx header, zero blocks, 1 MB of noise, the same text block 256 times between zero blocks and
    `name` (already encoded) at `at`; `early` and `late` are plain markers at 0.5 MB and 5 MB. A piece of 256 KB
    ends at 4.5 MB."""
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
def large(monkeypatch):
    monkeypatch.setattr(winlabsession, "CHECK_WHOLE_MAX", 16)
    monkeypatch.setattr(winlabsession, "CHECK_READ_MAX", 64 * KB)
    monkeypatch.setattr(winlabsession, "RUNS_MIN", MB)
    monkeypatch.setattr(winlabsession, "PIECE", 256 * KB)


@pytest.fixture
def socket_(monkeypatch):
    """The name check goes the remote way of the vault daemon; each request is counted and answered from the fixture
    register (never the live daemon)."""
    real = check.checker
    real_names = winlabsession._piece_names
    sent: list[str] = []
    state = {"fail_after": None, "calls": 0, "down": False}

    def remote(register_path, rate_wait=0.0):
        return check.RemoteCheck(config.paths().register.parent / "no-daemon.sock", rate_wait)

    def one(text, sock, timeout=None):
        if state["down"]:
            raise vault_client.VaultUnavailable("the vault daemon is not running")
        sent.append(text)
        return real(config.paths().register).scan(text)

    def names(text):
        # the daemon stops after `fail_after` name check calls of the image
        state["calls"] += 1
        state["down"] = state["fail_after"] is not None and state["calls"] > state["fail_after"]
        try:
            return real_names(text)
        finally:
            state["down"] = False

    monkeypatch.setattr(check, "checker", remote)
    monkeypatch.setattr(vault_client, "_check_one", one)
    monkeypatch.setattr(winlabsession, "_piece_names", names)
    return sent, state


def _records(root):
    folder = root / winlabsession.LOG_FOLDER / "transfers"
    return [json.loads(p.read_text()) for p in sorted(folder.glob("get-*.json"))] if folder.is_dir() else []


def _get(*extra):
    """A get of the image with the opt-in name check (--check), or `extra` as given (--recheck)."""
    return cli.main(["winlab", "get", WINDIR + "disk.vhdx", "disk.vhdx", *(extra or ("--check",))])


def test_fast_text_runs_find_ascii_utf8_and_utf16_at_both_alignments():
    word = fixtures.ORG_FORMS[0]
    for blob in (word.encode("utf-8"), word.encode("utf-16-le"), b"\0" + word.encode("utf-16-le"),
                 word.encode("utf-16-be"), b"\x07" + word.encode("utf-16-be")):
        runs = winlabsession._text_runs(bytes(300) + blob + bytes(301))
        assert word in runs
    assert [r for r in winlabsession._text_runs(bytes(MB)) if r] == []
    two = winlabsession._text_runs(b"\0first line\nsecond line\0")
    assert "first line\nsecond line" in two


def test_fast_a_clean_image_is_published_with_few_requests_and_each_repeat_checked_once(lab, large, socket_,
                                                                                         monkeypatch, capsys):
    sent, state = socket_
    monkeypatch.setattr(winlabsession, "PROGRESS_EVERY", 0.0)
    data = _image()
    lab.files[(WINDIR + "disk.vhdx").lower()] = bytearray(data)
    assert _get() == 0, capsys.readouterr().err
    err = capsys.readouterr().err
    assert (lab.root / "disk.vhdx").read_bytes() == data and _records(lab.root) == []
    pieces = len(data) // winlabsession.PIECE
    assert pieces == 24 and 1 <= state["calls"] <= 3 and len(sent) <= 6
    assert sum(t.count("line 042") for t in sent) == 1
    assert sum(len(t) for t in sent) < len(data) // 20
    assert "name check of the disk image: read 6.0 MB of 6.0 MB" in err


@pytest.mark.parametrize("encoding", ["ascii", "utf-8", "utf-16-le"])
def test_fast_a_registered_name_in_any_encoding_still_deletes_the_image(lab, large, socket_, encoding, capsys):
    form = fixtures.CUSTOMER_FORMS[0] if encoding == "ascii" else fixtures.ORG_FORMS[0]
    name = ("owner: %s\r\n" % form).encode(encoding)
    lab.files[(WINDIR + "disk.vhdx").lower()] = bytearray(_image(name, 4 * MB + MB // 2 + 12345))
    assert _get() == 1
    err = capsys.readouterr().err
    assert "registered name(s): the copy was deleted" in err
    fixtures.assert_no_fixture_name(err, "refusal")
    assert not (lab.root / "disk.vhdx").exists() and not (lab.root / "disk.vhdx.awb-part").exists()
    assert _records(lab.root) == []


def test_fast_a_name_across_two_pieces_is_found(lab, large, socket_, capsys):
    name = ("owner: %s\r\n" % fixtures.ORG_FORMS[0]).encode("utf-16-le")
    lab.files[(WINDIR + "disk.vhdx").lower()] = bytearray(_image(name, 4 * MB + MB // 2 - 9))
    assert _get() == 1
    assert "registered name(s): the copy was deleted" in capsys.readouterr().err


def test_fast_an_interrupted_check_resumes_at_its_mark_and_does_not_redo_finished_parts(lab, large, socket_,
                                                                                        monkeypatch, capsys):
    sent, state = socket_
    monkeypatch.setattr(winlabsession, "RUN_BATCH", 10 ** 7)
    monkeypatch.setattr(winlabsession, "RUN_MARK", MB)
    early, late = b"early marker of the invented disk", b"late marker of the invented disk"
    data = _image(early=early, late=late)
    lab.files[(WINDIR + "disk.vhdx").lower()] = bytearray(data)
    state["fail_after"] = 3
    assert _get() == 1
    err = capsys.readouterr().err
    assert "kept unpublished as disk.vhdx.awb-part" in err and "--recheck" in err
    (rec,) = _records(lab.root)
    assert rec["checked"] == 5 * MB - winlabsession.RUN_OVERLAP and state["calls"] == 4
    assert any(early.decode() in t for t in sent)
    first = len(sent)
    state["fail_after"] = None
    state["calls"] = 0
    monkeypatch.setattr(winlabsession, "connect", lambda machine: pytest.fail("the recheck connected"))
    assert _get("--recheck") == 0, capsys.readouterr().err
    assert "resumed at 5.0 MB of 6.0 MB" in capsys.readouterr().err
    again = sent[first:]
    assert again and not any(early.decode() in t for t in again)
    assert any(late.decode() in t for t in again)
    assert (lab.root / "disk.vhdx").read_bytes() == data and _records(lab.root) == []


def test_fast_requests_are_paced_under_the_byte_budget(monkeypatch):
    waits: list[float] = []
    monkeypatch.setattr(winlabsession, "_pause", waits.append)
    monkeypatch.setattr(winlabsession, "_piece_names", lambda text: 0)
    monkeypatch.setattr(winlabsession, "RUN_PACE_BYTES", 300_000)
    monkeypatch.setattr(winlabsession, "RUN_BATCH", 100_000)
    runs = winlabsession._TextRuns(10 * MB)
    runs.feed(b"\0".join(b"run %06d of an invented disk" % n for n in range(20_000)))
    runs.flush()
    assert runs.requests >= 6 and len(waits) >= runs.requests - 3
    assert all(0 < w <= winlabsession.RUN_PACE_WINDOW for w in waits)


def test_fast_a_small_image_keeps_the_check_per_piece(lab, socket_, monkeypatch, capsys):
    monkeypatch.setattr(winlabsession, "CHECK_WHOLE_MAX", 16)
    monkeypatch.setattr(winlabsession, "CHECK_READ_MAX", 1024)
    monkeypatch.setattr(winlabsession, "PIECE", 512)
    monkeypatch.setattr(winlabsession, "_TextRuns", lambda size: pytest.fail("runs for a small image"))
    data = b"vhdxfile" + b"plain sectors of an invented lab disk\n" * 100
    lab.files[(WINDIR + "disk.vhdx").lower()] = bytearray(data)
    assert _get() == 0, capsys.readouterr().err
    assert (lab.root / "disk.vhdx").read_bytes() == data


def test_fast_a_disk_image_without_check_is_published_after_the_sha256_as_own_lab_output(lab, large, socket_,
                                                                                          monkeypatch, capsys):
    """Owner decision 2026-11-20: a disk image from the lab machine is own lab output and gets no name check; the
    secret check of 2026-11-17 stays (a public image window skips it)."""
    sent, state = socket_
    added = []
    from awb import ledger
    monkeypatch.setattr(ledger, "add", lambda kind, done, **kw: added.append((kind, done, kw.get("project"))))
    secrets_ = []
    real = winlabsession._image_secret_count
    monkeypatch.setattr(winlabsession, "_image_secret_count", lambda text: secrets_.append(1) or real(text))
    name = ("owner: %s\r\n" % fixtures.CUSTOMER_FORMS[0]).encode()
    data = _image(name, 4 * MB + MB // 2 + 12345)
    lab.files[(WINDIR + "disk.vhdx").lower()] = bytearray(data)
    assert cli.main(["winlab", "get", WINDIR + "disk.vhdx", "disk.vhdx"]) == 0, capsys.readouterr().err
    assert (lab.root / "disk.vhdx").read_bytes() == data and _records(lab.root) == []
    assert state["calls"] == 0 and not any("owner:" in t for t in sent)
    assert len(secrets_) == len(data) // winlabsession.PIECE
    assert winlabsession.OWN_IMAGE in logs(lab.root)
    (entry,) = added
    assert entry[0] == "other" and winlabsession.OWN_IMAGE in entry[1] and "disk.vhdx" in entry[1]
    assert "6.0 MB" in entry[1] and entry[2]


def test_fast_a_plain_file_from_the_lab_keeps_its_name_check(lab, socket_, capsys):
    text = ("report of the run\nowner: %s\n" % fixtures.CUSTOMER_FORMS[0]).encode()
    lab.files[(WINDIR + "report.txt").lower()] = bytearray(text)
    assert cli.main(["winlab", "get", WINDIR + "report.txt", "report.txt"]) == 1
    assert "registered name(s)" in capsys.readouterr().err
    assert not (lab.root / "report.txt").exists()
