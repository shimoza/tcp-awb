"""Q-0023: one file-safety module, awb/safefile.py (finding R4 of the UX review of 2026-11-19).

The pinned read, the atomic write and the private and shared publish, the mode table and its check in
seal/verify.sh, and the contracts the replaced copies kept (their modes, their temporary names and their errors).
A write as one user and a read as the other runs for real only as root (a child as another uid); as any other user
the same layout is checked by mode and group alone.
"""
from __future__ import annotations

import json
import os
import re
import stat
import subprocess
import sys
from pathlib import Path

import pytest

from awb import build, labclient, rulesync, safefile, ux, vault, winlab
from awb.tcp import recheck, tenants

ROOT = Path(__file__).resolve().parent.parent
VERIFY = ROOT / "seal" / "verify.sh"
NOBODY = 65534


def _mode(path: Path) -> int:
    return stat.S_IMODE(os.lstat(path).st_mode)


# --------------------------------------------------------------------------- the pinned read


def test_read_regular_refuses_a_link_a_pipe_and_a_folder_without_waiting(tmp_path):
    target = tmp_path / "target"
    target.write_bytes(b"data")
    (tmp_path / "link").symlink_to(target)
    os.mkfifo(tmp_path / "fifo")
    (tmp_path / "folder").mkdir()
    assert safefile.read_regular(target) == b"data"
    with pytest.raises(OSError):
        safefile.read_regular(tmp_path / "link")
    for name in ("fifo", "folder"):
        with pytest.raises(OSError, match="^%s$" % safefile.NOT_REGULAR):
            safefile.read_regular(tmp_path / name)
    assert safefile.read_regular(tmp_path / "link", follow=True) == b"data"


def test_read_regular_checks_links_and_size_and_names_the_cap(tmp_path):
    f = tmp_path / "f"
    f.write_bytes(b"x" * 10)
    os.link(f, tmp_path / "second")
    assert safefile.read_regular(f) == b"x" * 10
    with pytest.raises(OSError, match="^a file with more than one name$"):
        safefile.read_regular(f, single_link=True)
    os.unlink(tmp_path / "second")
    with pytest.raises(OSError, match="^larger than 9 bytes$"):
        safefile.read_regular(f, cap=9)
    with pytest.raises(OSError, match="^file too large$"):
        safefile.read_regular(f, cap=9, too_large="file too large")
    assert safefile.read_regular(f, cap=10) == b"x" * 10


def test_a_failed_check_closes_the_descriptor_and_raises_the_callers_error(tmp_path):
    os.mkfifo(tmp_path / "fifo")
    before = len(os.listdir("/proc/self/fd"))

    class Refused(Exception):
        pass

    for _ in range(20):
        with pytest.raises(Refused):
            safefile.open_regular(tmp_path / "fifo", refuse=Refused)
    assert len(os.listdir("/proc/self/fd")) == before


# --------------------------------------------------------------------------- the atomic write and the publish


def test_write_atomic_replaces_with_the_mode_and_leaves_no_temporary_file(tmp_path):
    target = tmp_path / "out"
    target.write_text("old")
    old = os.umask(0o077)
    try:
        safefile.write_atomic(target, "new text", mode=0o640)
        assert target.read_text() == "new text" and _mode(target) == 0o600     # the umask applies without chmod
        safefile.write_atomic(target, b"bytes", mode=0o640, chmod=True)
        assert target.read_bytes() == b"bytes" and _mode(target) == 0o640
    finally:
        os.umask(old)
    assert sorted(p.name for p in tmp_path.iterdir()) == ["out"]


def test_publish_private_and_shared_set_their_modes_from_the_table(tmp_path):
    old = os.umask(0o077)
    try:
        safefile.publish_private(tmp_path / "private", "p")
        safefile.publish_shared(tmp_path / "shared", "s")
    finally:
        os.umask(old)
    assert _mode(tmp_path / "private") == safefile.MODES["private"] == 0o600
    assert _mode(tmp_path / "shared") == safefile.MODES["shared"] == 0o664


def test_a_failed_rename_keeps_the_old_file_and_removes_the_temporary_file(tmp_path, monkeypatch):
    target = tmp_path / "out"
    target.write_text("before")

    def fail(*args, **kwargs):
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(os, "replace", fail)
    with pytest.raises(OSError):
        safefile.write_atomic(target, "after")
    with pytest.raises(OSError):
        safefile.write_atomic(target, "after", rename=fail)
    assert target.read_text() == "before" and [p.name for p in tmp_path.iterdir()] == ["out"]


def test_without_overwrite_an_existing_target_is_refused_and_a_new_one_written(tmp_path):
    folder = os.open(tmp_path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        safefile.write_atomic("new", "first", dir_fd=folder, overwrite=False)
        with pytest.raises(FileExistsError):
            safefile.write_atomic("new", "second", dir_fd=folder, overwrite=False)
    finally:
        os.close(folder)
    assert (tmp_path / "new").read_text() == "first" and [p.name for p in tmp_path.iterdir()] == ["new"]


def test_a_planted_link_at_the_temporary_name_is_never_followed(tmp_path):
    outside = tmp_path / "outside"
    outside.write_text("untouched")
    (tmp_path / ".out.part").symlink_to(outside)
    with pytest.raises(OSError):
        safefile.write_atomic(tmp_path / "out", "x", temp=lambda name: "." + name + ".part", exclusive=False)
    assert outside.read_text() == "untouched" and not (tmp_path / "out").exists()


def test_write_new_refuses_an_existing_file_and_a_link(tmp_path):
    safefile.write_new(tmp_path / "a", b"one")
    (tmp_path / "link").symlink_to(tmp_path / "missing")
    for name in ("a", "link"):
        with pytest.raises(FileExistsError):
            safefile.write_new(tmp_path / name, b"two")
    assert (tmp_path / "a").read_bytes() == b"one" and not (tmp_path / "missing").exists()


# --------------------------------------------------------------------------- the replaced copies keep their contracts


def test_the_copies_keep_their_errors(tmp_path):
    os.mkfifo(tmp_path / "fifo")
    with pytest.raises(OSError, match="^not a regular file$"):
        build._read_regular(tmp_path / "fifo", 10)
    with pytest.raises(tenants.TenantError, match="fifo is no regular file"):
        tenants._read_regular(tmp_path / "fifo")
    folder = os.open(tmp_path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        with pytest.raises(vault.VaultError):
            with vault._regular_at(folder, "fifo"):
                pass
        with pytest.raises(winlab.WinlabError):
            winlab._regular(folder, "fifo")
        with pytest.raises(labclient.LabClientError):
            labclient._file(folder, "fifo", os.O_RDONLY)
    finally:
        os.close(folder)


def test_the_copies_keep_their_modes_and_temporary_names(tmp_path, monkeypatch):
    names = []
    replace = os.replace

    def seen(src, dst, **kwargs):
        names.append(os.path.basename(src))
        return replace(src, dst, **kwargs)

    monkeypatch.setattr(os, "replace", seen)
    vault._write_atomic(tmp_path / "v", b"v", 0o600)
    winlab._atomic_json(tmp_path / "w.json", {"b": 1, "a": 2})
    build._write_atomic(tmp_path / "b", b"b", 0o640)
    folder = os.open(tmp_path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        rulesync._publish(folder, "r.json", {"k": 1}, 0o600)
        labclient._write(folder, "RESOURCES.md", "r\n")
    finally:
        os.close(folder)
    assert re.fullmatch(r"\.awb-[0-9a-f]{32}\.tmp", names[0])
    assert re.fullmatch(r"\.winlab-[0-9a-f]{32}", names[1])
    assert re.fullmatch(r"\.b\.[0-9a-f]{12}\.tmp", names[2])
    assert re.fullmatch(r"\.rules-[0-9a-f]{32}\.tmp", names[3])
    assert re.fullmatch(r"\.lab-[0-9a-f]{32}", names[4]) and len(names) == 5
    assert (tmp_path / "w.json").read_text() == json.dumps({"a": 2, "b": 1}, indent=1, sort_keys=True) + "\n"
    assert _mode(tmp_path / "v") == 0o600 and _mode(tmp_path / "RESOURCES.md") == 0o664
    assert json.loads((tmp_path / "r.json").read_text()) == {"k": 1}
    assert not [p for p in tmp_path.iterdir() if p.name.startswith(".")]


def test_the_shared_modes_of_the_modules_come_from_the_table():
    assert labclient.SHARED_MODES["RESOURCES.md"] == safefile.SHARED
    assert (ux.FOLDER_MODE, ux.FILE_MODE) == (recheck.FOLDER_MODE, recheck.FILE_MODE) == (0o2775, 0o664)
    tree = {rel: (safefile.MODES[d], safefile.MODES[f]) for rel, d, f in safefile.SHARED_TREE}
    assert tree[ux.FOLDER] == (ux.FOLDER_MODE, ux.FILE_MODE)
    assert tree["recheck"] == (recheck.FOLDER_MODE, recheck.FILE_MODE)


# --------------------------------------------------------------------------- the table and seal/verify.sh


def test_the_table_runs_as_a_script_with_the_standard_library_alone():
    res = subprocess.run([sys.executable, "-I", str(ROOT / "awb" / "safefile.py"), "table"], capture_output=True,
                         text=True, timeout=30)
    assert res.returncode == 0, res.stderr
    assert res.stdout.splitlines() == ["recheck 2775 664", "ux 2775 664"] == safefile.table_lines()
    src = (ROOT / "awb" / "safefile.py").read_text()
    assert not re.search(r"^\s*(from|import) awb", src, re.M)


def _shared_modes_ok(*args) -> int:
    text = VERIFY.read_text()
    func = re.search(r"^shared_modes_ok\(\) \{\n.*?^\}\n", text, re.M | re.S).group(0)
    return subprocess.run(["bash", "-c", func + 'shared_modes_ok "$@"', "verify", *map(str, args)],
                          timeout=30).returncode


def test_verify_checks_the_shared_folders_against_the_table(tmp_path):
    text = VERIFY.read_text()
    assert 'python3 -I -- "$seal_dir/../awb/safefile.py" table' in text
    d = tmp_path / "recheck"
    assert _shared_modes_ok(d, "2775", "664") == 0                      # not made yet
    d.mkdir()
    os.chmod(d, 0o2775)
    (d / "state.tsv").write_text("x")
    os.chmod(d / "state.tsv", 0o664)
    assert _shared_modes_ok(d, "2775", "664") == 0
    os.chmod(d / "state.tsv", 0o666)
    assert _shared_modes_ok(d, "2775", "664") == 0                      # wider bits stay
    os.chmod(d / "state.tsv", 0o644)
    assert _shared_modes_ok(d, "2775", "664") == 1
    os.chmod(d / "state.tsv", 0o664)
    os.chmod(d, 0o2755)
    assert _shared_modes_ok(d, "2775", "664") == 1                      # Q-0001: 2755 in place of 2775
    link = tmp_path / "ux"
    link.symlink_to(d)
    assert _shared_modes_ok(link, "2775", "664") == 1


# --------------------------------------------------------------------------- a write as one user, a read as the other


def test_shared_publish_in_a_shared_folder_gives_the_group_and_the_group_bits(tmp_path):
    """Simulated: the folder and the file carry what the other user of the group needs (chown skipped)."""
    d = tmp_path / "shared"
    d.mkdir()
    os.chmod(d, safefile.SHARED_DIR)
    old = os.umask(0o077)
    try:
        safefile.publish_shared(d / "events.tsv", "line\n")
        safefile.publish_private(d / "own", "secret\n")
    finally:
        os.umask(old)
    f = os.stat(d / "events.tsv")
    assert stat.S_IMODE(f.st_mode) & 0o060 == 0o060 and f.st_gid == os.stat(d).st_gid
    assert _mode(d / "own") & 0o077 == 0


@pytest.mark.skipif(os.geteuid() != 0, reason="two uids need root to fork a child as another user")
def test_across_two_uids_the_other_user_reads_the_shared_file_and_never_the_private_one(tmp_path):
    import tempfile

    root = Path(tempfile.mkdtemp(prefix="awbsf", dir="/tmp"))
    try:
        os.chmod(root, 0o755)
        d = root / "shared"
        d.mkdir()
        os.chown(d, 0, NOBODY)
        os.chmod(d, safefile.SHARED_DIR)
        safefile.publish_shared(d / "events.tsv", "line\n")
        safefile.publish_private(d / "own", "secret\n")
        r, w = os.pipe()
        pid = os.fork()
        if pid == 0:
            try:
                os.close(r)
                os.setgroups([])
                os.setgid(NOBODY)
                os.setuid(NOBODY)
                out = {"shared": safefile.read_regular(d / "events.tsv").decode()}
                try:
                    safefile.read_regular(d / "own")
                    out["private"] = "read"
                except PermissionError:
                    out["private"] = "denied"
                os.write(w, json.dumps(out).encode())
            finally:
                os._exit(0)
        os.close(w)
        data = b""
        while chunk := os.read(r, 1 << 16):
            data += chunk
        os.close(r)
        os.waitpid(pid, 0)
        assert json.loads(data) == {"shared": "line\n", "private": "denied"}
    finally:
        subprocess.run(["rm", "-rf", "--", str(root)], check=False)
