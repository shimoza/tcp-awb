"""K11: awb voice learn never follows a link planted in a project folder or in the voice store."""
from __future__ import annotations

import os
import stat
from pathlib import Path

import pytest

from awb import writing
from tests.test_voice_learn_owner import (DRAFT, SENT, draft, owner_terminal, project, run)  # noqa: F401


def test_a_linked_draft_in_the_project_is_refused_with_and_without_project(home, project, tmp_path, monkeypatch,
                                                                          capsys):
    secret = tmp_path / "owner-file.md"
    secret.write_text("An owner paragraph that must not be copied anywhere.\n", encoding="utf-8")
    link = Path(project.path) / "linked.md"
    link.symlink_to(secret)
    for extra in ([], ["--project", project.code]):
        code, shown = run(monkeypatch, capsys, ["voice", "learn", str(link), "-"] + extra, SENT)
        assert code == 2, shown
    assert not (home.shared / "voice").exists()


def test_a_draft_behind_a_linked_folder_of_the_project_is_refused(home, project, tmp_path, monkeypatch, capsys):
    real = tmp_path / "elsewhere"
    real.mkdir()
    (real / "d.md").write_text(DRAFT, encoding="utf-8")
    (Path(project.path) / "dir").symlink_to(real)
    code, shown = run(monkeypatch, capsys, ["voice", "learn", str(Path(project.path) / "dir" / "d.md"), "-",
                                            "--project", project.code], SENT)
    assert code == 2 and "link" in shown
    assert not (home.shared / "voice").exists()


def test_a_planted_link_for_the_voice_folder_is_not_followed_or_chmodded(home, tmp_path):
    target = tmp_path / "owner-folder"
    target.mkdir()
    os.chmod(target, 0o700)
    home.shared.mkdir(parents=True, exist_ok=True)
    (home.shared / "voice").symlink_to(target)
    with pytest.raises(writing.WritingError):
        writing._mkdir(home.shared / "voice" / "pairs")
    assert stat.S_IMODE(target.stat().st_mode) == 0o700
    assert list(target.iterdir()) == []
