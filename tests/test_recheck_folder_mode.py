"""awb/tcp/recheck.py: the shared folder recheck is group writable with the setgid bit (2775) and its files 664, so
that the owner (another uid in the group of the work user) can write requested.tsv after the work side created the
folder (Q-0001: a folder of mode 2755 stopped the weekly refresh of 2026-11-14).

The tests run as one uid, so the two sides are simulated by modes: what the work side leaves behind must carry the
group write bits the owner needs, and a folder of another user is simulated by a different effective uid.
Every id here is invented.
"""
from __future__ import annotations

import datetime
import os
import stat

import pytest

from awb.tcp import recheck, refresh

TODAY = datetime.date(2026, 11, 14)


def mode(path) -> int:
    return stat.S_IMODE(os.lstat(path).st_mode)


@pytest.fixture
def narrow_umask():
    old = os.umask(0o022)
    try:
        yield
    finally:
        os.umask(old)


def test_the_work_side_creates_the_folder_2775_and_the_owner_can_append_requested_tsv(home, narrow_umask):
    # the work side: a run writes state.tsv and refuted.tsv first and so creates the folder
    recheck.record_refuted("KB-AAAA", TODAY)
    folder = home.shared / "recheck"
    assert mode(folder) == 0o2775
    assert mode(folder / "refuted.tsv") == 0o664
    # the owner writes through the group: the folder and a new file need the group write bit
    assert mode(folder) & stat.S_IWGRP and mode(folder) & stat.S_ISGID
    assert recheck.request([("KB-BBBB", TODAY, "release note 2026-11-14 ecs")]) == 1
    assert mode(folder / "requested.tsv") == 0o664
    assert recheck.requests() == [("KB-BBBB", "2026-11-14", "release note 2026-11-14 ecs")]


def test_a_folder_of_mode_2755_is_widened_to_2775_by_its_owner(home, narrow_umask):
    folder = home.shared / "recheck"
    folder.mkdir()
    os.chmod(folder, 0o2755)
    assert recheck.request([("KB-CCCC", TODAY, "docs mirror ecs/umn/a.md")]) == 1
    assert mode(folder) == 0o2775
    assert mode(folder / "requested.tsv") == 0o664


def test_a_wider_folder_keeps_its_wider_bits(home):
    folder = home.shared / "recheck"
    folder.mkdir()
    os.chmod(folder, 0o2777)
    recheck.shared_folder()
    assert mode(folder) == 0o2777


def test_a_requested_tsv_of_mode_644_is_widened_to_664_by_its_owner(home, narrow_umask):
    folder = recheck.shared_folder()
    path = folder / "requested.tsv"
    path.write_text("KB-DDDD\t2026-11-07\tservice description revision 18\n", encoding="utf-8")
    os.chmod(path, 0o644)
    assert recheck.request([("KB-EEEE", TODAY, "service description revision 19")]) == 1
    assert mode(path) == 0o664
    assert [r[0] for r in recheck.requests()] == ["KB-DDDD", "KB-EEEE"]


def test_the_state_line_of_a_run_lands_in_a_group_writable_file(home, narrow_umask):
    recheck.append_shared(home.shared.joinpath(*recheck.STATE_TSV), "line\n")
    assert mode(home.shared / "recheck") == 0o2775
    assert mode(home.shared.joinpath(*recheck.STATE_TSV)) == 0o664


def test_a_narrow_folder_of_another_user_is_one_clear_error_line(home, monkeypatch):
    folder = home.shared / "recheck"
    folder.mkdir()
    os.chmod(folder, 0o2755)
    monkeypatch.setattr(os, "geteuid", lambda: os.getuid() + 1)
    with pytest.raises(recheck.SharedModeError) as exc:
        recheck.request([("KB-FFFF", TODAY, "release note 2026-11-14 ecs")])
    line = str(exc.value)
    assert "\n" not in line
    assert "mode 2755" in line and "chmod 2775" in line
    assert mode(folder) == 0o2755
    assert not (folder / "requested.tsv").exists()


def test_a_link_where_the_folder_should_be_is_refused(home, tmp_path):
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    os.chmod(elsewhere, 0o755)
    (home.shared / "recheck").symlink_to(elsewhere)
    with pytest.raises(recheck.SharedModeError):
        recheck.request([("KB-GGGG", TODAY, "release note 2026-11-14 ecs")])
    assert mode(elsewhere) == 0o755
    assert not (elsewhere / "requested.tsv").exists()


def test_the_refresh_reports_a_narrow_folder_in_one_line_of_its_part(home, monkeypatch):
    folder = home.shared / "recheck"
    folder.mkdir()
    os.chmod(folder, 0o2755)
    monkeypatch.setattr(os, "geteuid", lambda: os.getuid() + 1)
    part = refresh.Part("mirrors")
    refresh._request(part, [("KB-HHHH", TODAY, "docs mirror ecs/umn/a.md")])
    part = refresh._finish(part)
    assert part.error.startswith("recheck requests not written: the shared folder recheck has mode 2755")
    assert "chmod 2775" in part.error and "\n" not in part.error
    assert part.requested == {}


def test_the_refresh_writes_its_requests_into_a_2775_folder(home, narrow_umask):
    part = refresh.Part("mirrors")
    refresh._request(part, [("KB-JJJJ", TODAY, "docs mirror ecs/umn/a.md")])
    assert not part.error
    assert part.requested == {"docs mirror": 1}
    assert mode(home.shared / "recheck") == 0o2775
    assert mode(home.shared.joinpath(*recheck.REQUESTED_TSV)) == 0o664
