"""DW (2026-11-02): the weekly dataset publish failed with `put: Error (operating system error)`. The build folders
under the shared side carry setgid and the owner's unit runs with RestrictSUIDSGID, so shutil.copytree, which copies
the mode of a folder, got EPERM when it copied topics/ into the clone. Every text is invented.
"""
from __future__ import annotations

import os
import stat

import pytest

from awb.tcp import dataset


def _refuse_setgid(monkeypatch):
    """chmod as under RestrictSUIDSGID: a mode with setuid or setgid is refused."""
    real = os.chmod

    def chmod(path, mode, *a, **kw):
        if mode & (stat.S_ISUID | stat.S_ISGID):
            raise PermissionError(1, "Operation not permitted", str(path))
        return real(path, mode, *a, **kw)

    monkeypatch.setattr(os, "chmod", chmod)


def _tree(tmp_path):
    src = tmp_path / "topics"
    (src / "sub").mkdir(parents=True)
    (src / "a.md").write_text("one\n", encoding="utf-8")
    (src / "sub" / "b.md").write_text("two\n", encoding="utf-8")
    os.chmod(src, 0o2755)
    return src


def test_dw_control_copytree_of_a_setgid_folder_fails_under_the_restriction(tmp_path, monkeypatch):
    src = _tree(tmp_path)
    _refuse_setgid(monkeypatch)
    with pytest.raises(OSError):
        dataset.shutil.copytree(src, tmp_path / "out")


def test_dw_copy_plain_tree_copies_every_file_without_the_setgid_bit(tmp_path, monkeypatch):
    src = _tree(tmp_path)
    _refuse_setgid(monkeypatch)
    dst = tmp_path / "clone" / "topics"
    dataset.copy_plain_tree(src, dst)
    assert (dst / "a.md").read_text(encoding="utf-8") == "one\n"
    assert (dst / "sub" / "b.md").read_text(encoding="utf-8") == "two\n"
    assert not dst.stat().st_mode & stat.S_ISGID
    assert stat.S_IMODE((dst / "a.md").stat().st_mode) == stat.S_IMODE((src / "a.md").stat().st_mode)


def test_dw_github_put_copies_the_folders_with_copy_plain_tree():
    import inspect

    body = inspect.getsource(dataset.github_put)
    assert "copy_plain_tree(" in body and "shutil.copytree(" not in body
