"""Ask 2026-11-27-tcp-bj3o-4: pictures the owner releases into input/<file id>-img/ of a project get the group of the
project folder and group read, so the work user (the group of the project folder) reads them."""
import grp
import os
import stat

import pytest

from awb import images


def _other_group() -> int:
    mine = [g for g in os.getgroups() if g != os.getegid()]
    if not mine:
        pytest.skip("this user is in no second group")
    return mine[0]


def test_released_pictures_take_the_group_of_the_project_folder(tmp_path):
    gid = _other_group()
    project = tmp_path / "tcp-q7m4"
    project.mkdir()
    os.chown(project, -1, gid)
    out = images._land_in_project(project, "F-AB12", [(1, "img-001.png", b"\x89PNG-invented")])
    folder = project / "input" / "F-AB12-img"
    st = folder.stat()
    assert st.st_gid == gid and stat.S_IMODE(st.st_mode) & 0o050 == 0o050 and not stat.S_IMODE(st.st_mode) & 0o007
    for path in out:
        st = path.stat()
        assert st.st_gid == gid, grp.getgrgid(st.st_gid).gr_name
        assert stat.S_IMODE(st.st_mode) == 0o640
    assert out[0].read_bytes() == b"\x89PNG-invented"


def test_a_second_release_into_a_folder_of_the_wrong_group_fixes_the_group(tmp_path):
    gid = _other_group()
    project = tmp_path / "tcp-q7m4"
    (project / "input" / "F-AB12-img").mkdir(parents=True)
    os.chown(project, -1, gid)
    os.chmod(project / "input" / "F-AB12-img", 0o700)
    images._land_in_project(project, "F-AB12", [(2, "img-002.png", b"\x89PNG-second")])
    st = (project / "input" / "F-AB12-img").stat()
    assert st.st_gid == gid and stat.S_IMODE(st.st_mode) == 0o750
    assert (project / "input" / "F-AB12-img" / "img-002.png").stat().st_gid == gid
