"""tcp-bytg 2026-11-19: awb winlab get of a 21.9 GB disk image transferred for 1 h 45 and passed SHA-256, then the
whole name check could not read it (hooks.Unavailable) and the get deleted the copy. On the owner's word of 2026-11-25
WinLab runs no content check: an image of any size is published right after its SHA-256 compare; no name check
can fail it. A get cut off after the transfer (SIGTERM) keeps the part under its private name:
`awb winlab get WINPATH FILE --recheck` compares its SHA-256 again and publishes it, without a transfer and without
the machine. The size is simulated: the name check entry points count their calls and a get must make none."""
from __future__ import annotations

import json
import os
import signal

import pytest

from awb import cli, hooks, winlab, winlabsession
from tests import fixtures
from tests.test_hooks import project  # noqa: F401 (fixture)
from tests.test_wl_session import MACHINE, WINDIR, lab, logs  # noqa: F401 (fixture)

IMAGE = b"vhdxfile" + b"plain sectors of an invented lab disk\n" * 200


@pytest.fixture
def small(monkeypatch):
    """The name check entry points of the hooks count their calls (they return no hit); the list stays empty."""
    calls = []

    def count(*a, **k):
        calls.append(1)
        return []

    monkeypatch.setattr(hooks, "name_hits", count)
    monkeypatch.setattr(hooks, "file_hits", count)
    return calls


def _records(root):
    folder = root / winlabsession.LOG_FOLDER / "transfers"
    return [json.loads(p.read_text()) for p in sorted(folder.glob("get-*.json"))] if folder.is_dir() else []


def _cut_off(monkeypatch):
    """The get or recheck gets SIGTERM from the outside right after its SHA-256 compare, as a tool call that ran out
    of time does: the part stays under its name and the record keeps its SHA-256."""
    real = winlabsession._write_json
    state = {"done": False}

    def write(path, data):
        if "sha256" in data and not state["done"]:
            state["done"] = True
            os.kill(os.getpid(), signal.SIGTERM)
        return real(path, data)

    monkeypatch.setattr(winlabsession, "_write_json", write)


def test_lg_a_disk_image_over_the_read_limit_skips_the_whole_check_and_is_published(lab, small, monkeypatch, capsys):
    lab.files[(WINDIR + "disk.vhdx").lower()] = bytearray(IMAGE)

    def whole(*a, **k):
        raise hooks.Unavailable("MemoryError in the name check")

    monkeypatch.setattr(hooks, "file_hits", whole)
    assert cli.main(["winlab", "get", WINDIR + "disk.vhdx", "results/disk.vhdx"]) == 0, capsys.readouterr().err
    assert (lab.root / "results" / "disk.vhdx").read_bytes() == IMAGE
    assert small == []
    assert not (lab.root / "results" / "disk.vhdx.awb-part").exists() and _records(lab.root) == []


def test_lg_an_unavailable_name_check_keeps_the_part_and_recheck_resumes_without_a_transfer(lab, small, monkeypatch,
                                                                                            capsys):
    lab.files[(WINDIR + "disk.vhdx").lower()] = bytearray(IMAGE)
    _cut_off(monkeypatch)
    assert cli.main(["winlab", "get", WINDIR + "disk.vhdx", "results/disk.vhdx"]) == 1
    err = capsys.readouterr().err
    assert "stopped by a signal" in err
    part = lab.root / "results" / "disk.vhdx.awb-part"
    assert part.read_bytes() == IMAGE and not (lab.root / "results" / "disk.vhdx").exists()
    assert sorted(p.name for p in (lab.root / "results").iterdir()) == ["disk.vhdx.awb-part"]
    (rec,) = _records(lab.root)
    assert rec["machine"] == MACHINE and rec["size"] == len(IMAGE) and rec["sha256"] and "staged" not in rec
    transferred = list(lab.chunks)
    monkeypatch.setattr(winlabsession, "connect", lambda machine: pytest.fail("the recheck connected"))
    # cut off again during the recheck: the part stays and the record keeps its SHA-256
    _cut_off(monkeypatch)
    assert cli.main(["winlab", "get", WINDIR + "disk.vhdx", "results/disk.vhdx", "--recheck"]) == 1
    assert "stopped by a signal" in capsys.readouterr().err
    assert part.read_bytes() == IMAGE and _records(lab.root)[0]["sha256"] == rec["sha256"]
    assert cli.main(["winlab", "get", WINDIR + "disk.vhdx", "results/disk.vhdx", "--recheck"]) == 0, \
        capsys.readouterr().err
    assert (lab.root / "results" / "disk.vhdx").read_bytes() == IMAGE and not part.exists()
    assert lab.chunks == transferred and _records(lab.root) == [] and small == []
    assert "compared again without a transfer" in logs(lab.root)


def test_lg_a_small_image_whose_whole_check_cannot_run_is_kept_not_deleted(lab, monkeypatch, capsys):
    """No whole check runs any more: an unavailable name check cannot keep or delete anything, the image arrives."""
    lab.files[(WINDIR + "disk.vhdx").lower()] = bytearray(IMAGE)

    def whole(*a, **k):
        raise hooks.Unavailable("vault locked")

    monkeypatch.setattr(hooks, "file_hits", whole)
    assert cli.main(["winlab", "get", WINDIR + "disk.vhdx", "disk.vhdx"]) == 0, capsys.readouterr().err
    assert (lab.root / "disk.vhdx").read_bytes() == IMAGE and not (lab.root / "disk.vhdx.awb-part").exists()
    assert cli.main(["winlab", "get", WINDIR + "disk.vhdx", "disk.vhdx", "--recheck"]) == 1
    assert "nothing to compare again" in capsys.readouterr().err
    assert (lab.root / "disk.vhdx").read_bytes() == IMAGE


def test_lg_the_same_get_again_resumes_the_checks_without_a_transfer(lab, small, monkeypatch, capsys):
    lab.files[(WINDIR + "disk.vhdx").lower()] = bytearray(IMAGE)
    _cut_off(monkeypatch)
    assert cli.main(["winlab", "get", WINDIR + "disk.vhdx", "disk.vhdx"]) == 1
    capsys.readouterr()
    gets = [c for c in lab.chunks if c[0] == "get"]
    assert cli.main(["winlab", "get", WINDIR + "disk.vhdx", "disk.vhdx"]) == 0, capsys.readouterr().err
    assert [c for c in lab.chunks if c[0] == "get"] == gets
    assert (lab.root / "disk.vhdx").read_bytes() == IMAGE and small == []


def test_lg_a_registered_name_in_a_kept_part_still_deletes_it_on_recheck(lab, small, monkeypatch, capsys):
    """A registered name in a kept part no longer matters: the recheck compares the SHA-256 and publishes it."""
    data = IMAGE + ("owner %s\n" % fixtures.CUSTOMER_FORMS[0]).encode() + IMAGE[8:]
    lab.files[(WINDIR + "disk.vhdx").lower()] = bytearray(data)
    _cut_off(monkeypatch)
    assert cli.main(["winlab", "get", WINDIR + "disk.vhdx", "disk.vhdx"]) == 1
    capsys.readouterr()
    assert (lab.root / "disk.vhdx.awb-part").exists()
    assert cli.main(["winlab", "get", WINDIR + "disk.vhdx", "disk.vhdx", "--recheck"]) == 0, capsys.readouterr().err
    assert "registered name" not in capsys.readouterr().err
    assert not (lab.root / "disk.vhdx.awb-part").exists() and (lab.root / "disk.vhdx").read_bytes() == data
    assert _records(lab.root) == [] and small == []


def test_lg_a_kept_part_changed_since_the_get_is_deleted_on_recheck(lab, small, monkeypatch, capsys):
    lab.files[(WINDIR + "disk.vhdx").lower()] = bytearray(IMAGE)
    _cut_off(monkeypatch)
    assert cli.main(["winlab", "get", WINDIR + "disk.vhdx", "disk.vhdx"]) == 1
    capsys.readouterr()
    part = lab.root / "disk.vhdx.awb-part"
    part.write_bytes(b"x" + IMAGE[1:])
    assert cli.main(["winlab", "get", WINDIR + "disk.vhdx", "disk.vhdx", "--recheck"]) == 1
    assert "changed since the get" in capsys.readouterr().err
    assert not part.exists() and not (lab.root / "disk.vhdx").exists() and _records(lab.root) == []


def test_lg_recheck_without_a_kept_part_is_refused(lab, capsys):
    assert cli.main(["winlab", "get", WINDIR + "disk.vhdx", "disk.vhdx", "--recheck"]) == 1
    assert "nothing to compare again" in capsys.readouterr().err
    assert not (lab.root / "disk.vhdx").exists()


def test_lg_recheck_needs_no_open_window(lab, small, monkeypatch, capsys):
    lab.files[(WINDIR + "disk.vhdx").lower()] = bytearray(IMAGE)
    _cut_off(monkeypatch)
    assert cli.main(["winlab", "get", WINDIR + "disk.vhdx", "disk.vhdx"]) == 1
    capsys.readouterr()
    winlab.close(MACHINE)
    assert cli.main(["winlab", "get", WINDIR + "disk.vhdx", "disk.vhdx", "--recheck"]) == 0, capsys.readouterr().err
    assert (lab.root / "disk.vhdx").read_bytes() == IMAGE
