"""tcp-bytg 2026-11-19: awb winlab get of a 21.9 GB image ended in OSError, its copy for the content check filled the
host disk. No content check and no second copy exist any more: the received file is compared and published in place,
and an unexpected failure after the transfer gives the part back."""
from __future__ import annotations

import shutil

from awb import cli, winlabsession
from tests.test_hooks import project  # noqa: F401 (fixture)
from tests.test_wl_session import WINDIR, lab  # noqa: F401 (fixture)


def test_bytg_get_checks_the_part_in_place_without_a_second_copy(lab, monkeypatch, capsys):
    lab.files[(WINDIR + "disk.bin").lower()] = bytearray(b"plain sectors\n" * 300)

    def no_copy(*a, **kw):
        raise AssertionError("the get copied the received file")

    monkeypatch.setattr(shutil, "copyfileobj", no_copy)
    assert cli.main(["winlab", "get", WINDIR + "disk.bin", "results/disk.bin"]) == 0
    capsys.readouterr()
    assert (lab.root / "results" / "disk.bin").read_bytes() == b"plain sectors\n" * 300
    assert sorted(p.name for p in (lab.root / "results").iterdir()) == ["disk.bin"]


def test_bytg_an_unexpected_failure_in_the_checks_gives_the_part_back(lab, monkeypatch, capsys):
    lab.files[(WINDIR + "disk.bin").lower()] = bytearray(b"plain sectors\n" * 300)

    def broken(*a, **kw):
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(winlabsession, "_sha256", broken)
    assert cli.main(["winlab", "get", WINDIR + "disk.bin", "results/disk.bin"]) != 0
    capsys.readouterr()
    names = sorted(p.name for p in (lab.root / "results").iterdir())
    assert names == ["disk.bin.awb-part"]
    assert (lab.root / "results" / "disk.bin.awb-part").stat().st_size == len(b"plain sectors\n" * 300)
