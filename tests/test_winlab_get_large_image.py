"""tcp-bytg 2026-11-19: awb winlab get of a 21.9 GB disk image transferred for 1 h 45 and passed SHA-256, then the
whole name check could not read it (hooks.Unavailable) and the get deleted the copy. A disk image over
CHECK_READ_MAX gets the name check per piece, and when the name check cannot run the part stays under its private
name: `awb winlab get WINPATH FILE --recheck` checks it again, from where it stopped, without a transfer. The size is
simulated with patched limits, never with gigabytes."""
from __future__ import annotations

import json

import pytest

from awb import cli, hooks, winlab, winlabsession
from tests import fixtures
from tests.test_hooks import project  # noqa: F401 (fixture)
from tests.test_wl_session import MACHINE, WINDIR, lab, logs  # noqa: F401 (fixture)

IMAGE = b"vhdxfile" + b"plain sectors of an invented lab disk\n" * 200


@pytest.fixture
def small(monkeypatch):
    monkeypatch.setattr(winlabsession, "CHECK_WHOLE_MAX", 16)
    monkeypatch.setattr(winlabsession, "CHECK_READ_MAX", 1024)
    monkeypatch.setattr(winlabsession, "PIECE", 512)


def _records(root):
    folder = root / winlabsession.LOG_FOLDER / "transfers"
    return [json.loads(p.read_text()) for p in sorted(folder.glob("get-*.json"))] if folder.is_dir() else []


def _pieces_fail_after(monkeypatch, n):
    """hooks.name_hits fails as unavailable from the (n+1)th piece of a file on; command texts pass."""
    real = hooks.name_hits
    calls = []

    def name_hits(text, *a, **k):
        if "\n" in text:
            calls.append(1)
            if len(calls) > n:
                raise hooks.Unavailable("rate limit of the vault daemon")
        return real(text, *a, **k)

    monkeypatch.setattr(hooks, "name_hits", name_hits)
    return calls


def test_lg_a_disk_image_over_the_read_limit_skips_the_whole_check_and_is_published(lab, small, monkeypatch, capsys):
    lab.files[(WINDIR + "disk.vhdx").lower()] = bytearray(IMAGE)

    def whole(*a, **k):
        raise hooks.Unavailable("MemoryError in the name check")

    monkeypatch.setattr(hooks, "file_hits", whole)
    calls = _pieces_fail_after(monkeypatch, 10 ** 6)
    assert cli.main(["winlab", "get", WINDIR + "disk.vhdx", "results/disk.vhdx", "--check"]) == 0, capsys.readouterr().err
    assert (lab.root / "results" / "disk.vhdx").read_bytes() == IMAGE
    assert len(calls) == -(-len(IMAGE) // 512)
    assert not (lab.root / "results" / "disk.vhdx.awb-part").exists() and _records(lab.root) == []


def test_lg_an_unavailable_name_check_keeps_the_part_and_recheck_resumes_without_a_transfer(lab, small, monkeypatch,
                                                                                            capsys):
    lab.files[(WINDIR + "disk.vhdx").lower()] = bytearray(IMAGE)
    _pieces_fail_after(monkeypatch, 3)
    assert cli.main(["winlab", "get", WINDIR + "disk.vhdx", "results/disk.vhdx", "--check"]) == 1
    err = capsys.readouterr().err
    assert "kept unpublished as results/disk.vhdx.awb-part" in err and "--recheck" in err
    part = lab.root / "results" / "disk.vhdx.awb-part"
    assert part.read_bytes() == IMAGE and not (lab.root / "results" / "disk.vhdx").exists()
    assert sorted(p.name for p in (lab.root / "results").iterdir()) == ["disk.vhdx.awb-part"]
    (rec,) = _records(lab.root)
    assert rec["checked"] == 3 * 512 and rec["machine"] == MACHINE and rec["size"] == len(IMAGE)
    transferred = list(lab.chunks)
    monkeypatch.setattr(winlabsession, "connect", lambda machine: pytest.fail("the recheck connected"))
    # the name check still cannot run: the recheck keeps the part again and loses none of the progress
    _pieces_fail_after(monkeypatch, 0)
    assert cli.main(["winlab", "get", WINDIR + "disk.vhdx", "results/disk.vhdx", "--recheck"]) == 1
    assert "kept unpublished" in capsys.readouterr().err
    assert part.read_bytes() == IMAGE and _records(lab.root)[0]["checked"] == 3 * 512
    monkeypatch.setattr(hooks, "name_hits", _real_name_hits)
    pieces = []
    monkeypatch.setattr(winlabsession, "_piece_names", lambda text: pieces.append(1) or 0)
    assert cli.main(["winlab", "get", WINDIR + "disk.vhdx", "results/disk.vhdx", "--recheck"]) == 0, \
        capsys.readouterr().err
    assert (lab.root / "results" / "disk.vhdx").read_bytes() == IMAGE and not part.exists()
    assert len(pieces) == -(-(len(IMAGE) - 3 * 512 + winlabsession.OVERLAP) // 512)
    assert lab.chunks == transferred and _records(lab.root) == []
    assert "checked again without a transfer" in logs(lab.root)


_real_name_hits = hooks.name_hits


def test_lg_a_small_image_whose_whole_check_cannot_run_is_kept_not_deleted(lab, monkeypatch, capsys):
    lab.files[(WINDIR + "disk.vhdx").lower()] = bytearray(IMAGE)

    def whole(*a, **k):
        raise hooks.Unavailable("vault locked")

    monkeypatch.setattr(hooks, "file_hits", whole)
    assert cli.main(["winlab", "get", WINDIR + "disk.vhdx", "disk.vhdx", "--check"]) == 1
    assert "kept unpublished as disk.vhdx.awb-part" in capsys.readouterr().err
    assert (lab.root / "disk.vhdx.awb-part").read_bytes() == IMAGE
    monkeypatch.setattr(hooks, "file_hits", _real_file_hits)
    assert cli.main(["winlab", "get", WINDIR + "disk.vhdx", "disk.vhdx", "--recheck"]) == 0, capsys.readouterr().err
    assert (lab.root / "disk.vhdx").read_bytes() == IMAGE and not (lab.root / "disk.vhdx.awb-part").exists()


_real_file_hits = hooks.file_hits


def test_lg_the_same_get_again_resumes_the_checks_without_a_transfer(lab, small, monkeypatch, capsys):
    lab.files[(WINDIR + "disk.vhdx").lower()] = bytearray(IMAGE)
    _pieces_fail_after(monkeypatch, 2)
    assert cli.main(["winlab", "get", WINDIR + "disk.vhdx", "disk.vhdx", "--check"]) == 1
    capsys.readouterr()
    gets = [c for c in lab.chunks if c[0] == "get"]
    monkeypatch.setattr(hooks, "name_hits", _real_name_hits)
    assert cli.main(["winlab", "get", WINDIR + "disk.vhdx", "disk.vhdx", "--check"]) == 0, capsys.readouterr().err
    assert [c for c in lab.chunks if c[0] == "get"] == gets
    assert (lab.root / "disk.vhdx").read_bytes() == IMAGE


def test_lg_a_registered_name_in_a_kept_part_still_deletes_it_on_recheck(lab, small, monkeypatch, capsys):
    data = IMAGE + ("owner %s\n" % fixtures.CUSTOMER_FORMS[0]).encode() + IMAGE[8:]
    lab.files[(WINDIR + "disk.vhdx").lower()] = bytearray(data)
    _pieces_fail_after(monkeypatch, 1)
    assert cli.main(["winlab", "get", WINDIR + "disk.vhdx", "disk.vhdx", "--check"]) == 1
    capsys.readouterr()
    assert (lab.root / "disk.vhdx.awb-part").exists()
    monkeypatch.setattr(hooks, "name_hits", _real_name_hits)
    assert cli.main(["winlab", "get", WINDIR + "disk.vhdx", "disk.vhdx", "--recheck"]) == 1
    err = capsys.readouterr().err
    assert "registered name(s): the copy was deleted" in err
    fixtures.assert_no_fixture_name(err, "recheck refusal")
    assert not (lab.root / "disk.vhdx.awb-part").exists() and not (lab.root / "disk.vhdx").exists()
    assert _records(lab.root) == []


def test_lg_a_kept_part_changed_since_the_get_is_deleted_on_recheck(lab, small, monkeypatch, capsys):
    lab.files[(WINDIR + "disk.vhdx").lower()] = bytearray(IMAGE)
    _pieces_fail_after(monkeypatch, 1)
    assert cli.main(["winlab", "get", WINDIR + "disk.vhdx", "disk.vhdx", "--check"]) == 1
    capsys.readouterr()
    part = lab.root / "disk.vhdx.awb-part"
    part.write_bytes(b"x" + IMAGE[1:])
    monkeypatch.setattr(hooks, "name_hits", _real_name_hits)
    assert cli.main(["winlab", "get", WINDIR + "disk.vhdx", "disk.vhdx", "--recheck"]) == 1
    assert "changed since the get" in capsys.readouterr().err
    assert not part.exists() and not (lab.root / "disk.vhdx").exists() and _records(lab.root) == []


def test_lg_recheck_without_a_kept_part_is_refused(lab, capsys):
    assert cli.main(["winlab", "get", WINDIR + "disk.vhdx", "disk.vhdx", "--recheck"]) == 1
    assert "nothing to check again" in capsys.readouterr().err
    assert not (lab.root / "disk.vhdx").exists()


def test_lg_recheck_needs_no_open_window(lab, small, monkeypatch, capsys):
    lab.files[(WINDIR + "disk.vhdx").lower()] = bytearray(IMAGE)
    _pieces_fail_after(monkeypatch, 1)
    assert cli.main(["winlab", "get", WINDIR + "disk.vhdx", "disk.vhdx", "--check"]) == 1
    capsys.readouterr()
    winlab.close(MACHINE)
    monkeypatch.setattr(hooks, "name_hits", _real_name_hits)
    assert cli.main(["winlab", "get", WINDIR + "disk.vhdx", "disk.vhdx", "--recheck"]) == 0, capsys.readouterr().err
    assert (lab.root / "disk.vhdx").read_bytes() == IMAGE
