"""tcp-bytg ask 11 (2026-11-21): ten complete transfers of own lab disk images were lost because the check could not
finish in time (a 22 GB image takes 1 to 2.5 h). A complete transfer is never deleted for a check that did not run:
it stays unpublished as FILE.awb-part, a signal turns into one state line and the checks resume later, and a get
killed outright gives the file back under its part name. Sizes are simulated with patched limits."""
from __future__ import annotations

import json
import os
import signal

import pytest

from awb import cli, winlabsession
from tests.test_hooks import project  # noqa: F401 (fixture)
from tests.test_wl_session import WINDIR, lab, logs  # noqa: F401 (fixture)

IMAGE = b"vhdxfile" + b"plain sectors of an invented lab disk\n" * 200
SRC = WINDIR + "disk.vhdx"


@pytest.fixture
def small(monkeypatch):
    monkeypatch.setattr(winlabsession, "CHECK_WHOLE_MAX", 16)
    monkeypatch.setattr(winlabsession, "CHECK_READ_MAX", 1024)
    monkeypatch.setattr(winlabsession, "PIECE", 512)


def _records(root):
    folder = root / winlabsession.LOG_FOLDER / "transfers"
    return [json.loads(p.read_text()) for p in sorted(folder.glob("get-*.json"))] if folder.is_dir() else []


def _signal_after(monkeypatch, n):
    """The secret check of the image gets SIGTERM from the outside at its (n+1)th piece, as a tool call that ran
    out of time does."""
    real = winlabsession._image_secret_count
    calls = []

    def count(text):
        calls.append(1)
        if len(calls) == n + 1:
            os.kill(os.getpid(), signal.SIGTERM)
        return real(text)

    monkeypatch.setattr(winlabsession, "_image_secret_count", count)
    return calls


def test_bytg_a_signal_during_the_check_keeps_the_image_and_recheck_resumes_it(lab, small, monkeypatch, capsys):
    lab.files[SRC.lower()] = bytearray(IMAGE)
    _signal_after(monkeypatch, 3)
    assert cli.main(["winlab", "get", SRC, "results/disk.vhdx"]) == 1
    err = capsys.readouterr().err
    assert "kept unpublished as results/disk.vhdx.awb-part" in err and "stopped by a signal" in err
    assert "awb winlab get %s results/disk.vhdx --recheck" % SRC in err
    part = lab.root / "results" / "disk.vhdx.awb-part"
    assert part.read_bytes() == IMAGE and not (lab.root / "results" / "disk.vhdx").exists()
    assert sorted(p.name for p in part.parent.iterdir()) == ["disk.vhdx.awb-part"]   # no private copy left over
    (rec,) = _records(lab.root)
    assert rec["checked"] >= 3 * 512 and "staged" not in rec and rec["sha256"]
    assert signal.getsignal(signal.SIGTERM) is signal.SIG_DFL                       # the handler is given back
    transferred = list(lab.chunks)
    calls = _signal_after(monkeypatch, 10 ** 6)
    monkeypatch.setattr(winlabsession, "connect", lambda machine: pytest.fail("the recheck connected"))
    assert cli.main(["winlab", "get", SRC, "results/disk.vhdx", "--recheck"]) == 0, capsys.readouterr().err
    assert "checks of the received file: resumed at" in capsys.readouterr().err
    assert (lab.root / "results" / "disk.vhdx").read_bytes() == IMAGE and not part.exists()
    assert len(calls) < -(-len(IMAGE) // 512) and lab.chunks == transferred and _records(lab.root) == []


def _killed(lab, monkeypatch, capsys):
    """The disk as a get leaves it that SIGKILL ended in the check (no finally ran): the file under its private name,
    the record naming it. Taken at the moment the check starts, after the SHA-256 compare, and put back after the
    get ended."""
    lab.files[SRC.lower()] = bytearray(IMAGE)
    real = winlabsession._image_secret_count
    seen = {}

    def at_check(text):
        (path,) = (lab.root / winlabsession.LOG_FOLDER / "transfers").glob("get-*.json")
        seen["record"], seen["path"] = path.read_text(), path
        (staged,) = [p for p in lab.root.iterdir() if p.name.startswith(".winlab-")]
        assert not (lab.root / "disk.vhdx.awb-part").exists()
        os.link(staged, lab.root / "kept-copy")
        seen["staged"] = staged
        raise winlabsession.Stopped(signal.SIGKILL)

    monkeypatch.setattr(winlabsession, "_image_secret_count", at_check)
    assert cli.main(["winlab", "get", SRC, "disk.vhdx"]) == 1
    monkeypatch.setattr(winlabsession, "_image_secret_count", real)
    (lab.root / "disk.vhdx.awb-part").unlink()
    os.rename(lab.root / "kept-copy", seen["staged"])
    seen["path"].write_text(seen["record"])
    capsys.readouterr()
    assert json.loads(seen["record"])["staged"] == seen["staged"].name and json.loads(seen["record"])["sha256"]


def test_bytg_a_killed_get_gives_the_file_back_and_the_same_get_needs_no_transfer(lab, small, monkeypatch, capsys):
    _killed(lab, monkeypatch, capsys)
    gets = [c for c in lab.chunks if c[0] == "get"]
    assert cli.main(["winlab", "get", SRC, "disk.vhdx"]) == 0, capsys.readouterr().err
    assert "is back as disk.vhdx.awb-part" in capsys.readouterr().err
    assert (lab.root / "disk.vhdx").read_bytes() == IMAGE
    assert [c for c in lab.chunks if c[0] == "get"] == gets                          # nothing transferred again
    assert not [p for p in lab.root.iterdir() if p.name.startswith(".winlab-")] and _records(lab.root) == []


def test_bytg_a_killed_get_is_finished_by_recheck_without_the_machine(lab, small, monkeypatch, capsys):
    _killed(lab, monkeypatch, capsys)
    monkeypatch.setattr(winlabsession, "connect", lambda machine: pytest.fail("the recheck connected"))
    assert cli.main(["winlab", "get", SRC, "disk.vhdx", "--recheck"]) == 0, capsys.readouterr().err
    assert (lab.root / "disk.vhdx").read_bytes() == IMAGE
    assert not [p for p in lab.root.iterdir() if p.name.startswith(".winlab-") or p.name.endswith(".awb-part")]


def test_bytg_a_complete_transfer_without_the_machines_sha256_is_kept_not_deleted(lab, small, monkeypatch, capsys):
    lab.files[SRC.lower()] = bytearray(IMAGE)
    real = lab.stat

    def no_digest(path, digest=False):
        out = real(path, digest)
        if digest and out:
            out.pop("sha256")
        return out

    monkeypatch.setattr(lab, "stat", no_digest)
    assert cli.main(["winlab", "get", SRC, "disk.vhdx"]) == 1
    err = capsys.readouterr().err
    assert "kept unpublished as disk.vhdx.awb-part" in err and "gave no SHA-256" in err
    assert (lab.root / "disk.vhdx.awb-part").read_bytes() == IMAGE
    assert "deleted" not in logs(lab.root)
    gets = [c for c in lab.chunks if c[0] == "get"]
    monkeypatch.setattr(lab, "stat", real)
    assert cli.main(["winlab", "get", SRC, "disk.vhdx"]) == 0, capsys.readouterr().err
    assert (lab.root / "disk.vhdx").read_bytes() == IMAGE
    assert [c for c in lab.chunks if c[0] == "get"] == gets
