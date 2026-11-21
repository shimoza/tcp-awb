"""Disk images stay out of git in every project (tcp-bytg ask 8, 2026-11-21): git add began hashing a 9.6 GB VHDX
under evidence/ and had to be stopped. Every code and file here is invented."""
from __future__ import annotations

import subprocess
from pathlib import Path

from awb import cli, projects, rulesync
from tests.test_lifecycle import spawn

IMAGES = ["evidence/a.vhdx", "evidence/B.VHDX", "evidence/c.vhd", "evidence/d.qcow2", "evidence/e.vmdk",
          "evidence/f.img", "evidence/g.iso", "evidence/h.raw", "evidence/i.Ova", "evidence/a.vhdx.awb-part"]


def _ignored(folder: Path, names: list[str]) -> set[str]:
    out = subprocess.run(["git", "-C", str(folder), "check-ignore", "--no-index", *names], capture_output=True,
                         text=True).stdout
    return set(out.split())


def _exclude(folder: Path) -> Path:
    return folder / projects.IMAGE_EXCLUDE


def test_bytg_a_new_project_ignores_disk_images_in_any_case_and_git_add_leaves_them(home, register_path):
    pr = spawn(home, register_path)
    folder = Path(pr.path)
    lines = _exclude(folder).read_text(encoding="utf-8").splitlines()
    assert lines.count(projects.IMAGE_IGNORE_NOTE) == 1 and all(x in lines for x in projects.IMAGE_IGNORE_LINES)
    assert _ignored(folder, IMAGES + ["evidence/notes.md", "evidence/poc.py"]) == set(IMAGES)
    (folder / "evidence" / "disk.VHDX").write_bytes(b"vhdxfile" + b"\0" * 1024)
    (folder / "evidence" / "notes.md").write_text("a note\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(folder), "add", "-A"], check=True, capture_output=True)
    staged = subprocess.run(["git", "-C", str(folder), "diff", "--cached", "--name-only"], capture_output=True,
                            text=True).stdout.split()
    assert staged == ["evidence/notes.md"]
    assert projects.ignore_disk_images(folder) is False                # a second write changes nothing


def test_bytg_the_sync_adds_the_lines_to_an_older_project_and_keeps_its_own(home, register_path, capsys):
    pr = spawn(home, register_path)
    folder = Path(pr.path)
    _exclude(folder).write_text("# own line\n*.vhdx\nscratch/", encoding="utf-8")
    assert cli.main(["projects", "sync"]) == 0
    assert "%s: .git/info/exclude keeps disk images out of git now" % pr.code in capsys.readouterr().out
    lines = _exclude(folder).read_text(encoding="utf-8").splitlines()
    assert lines[:3] == ["# own line", "*.vhdx", "scratch/"]
    assert lines.count(projects.IMAGE_IGNORE_NOTE) == 1
    assert all(line in lines for line in projects.IMAGE_IGNORE_LINES)
    assert _ignored(folder, IMAGES) == set(IMAGES)
    assert cli.main(["projects", "sync"]) == 0
    assert "exclude" not in capsys.readouterr().out                    # a second run changes nothing
    _exclude(folder).unlink()
    _exclude(folder).parent.rmdir()
    assert rulesync.sync_ignores(home, [pr.code]) == [(pr.code, "added")]
    assert _ignored(folder, IMAGES) == set(IMAGES)


def test_bytg_a_linked_exclude_file_is_left_alone(home, register_path, tmp_path):
    pr = spawn(home, register_path)
    folder = Path(pr.path)
    outside = tmp_path / "elsewhere.txt"
    outside.write_text("keep\n", encoding="utf-8")
    _exclude(folder).unlink()
    _exclude(folder).symlink_to(outside)
    assert rulesync.sync_ignores(home, [pr.code]) == [(pr.code, "cannot be written")]
    assert outside.read_text(encoding="utf-8") == "keep\n"
