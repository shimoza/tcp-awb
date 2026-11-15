"""LX-5: every source and destination is in a throw-away home."""
from datetime import datetime, timezone
import hashlib
import os
from pathlib import Path
import re
import stat
import subprocess
import tarfile
from types import SimpleNamespace

import pytest

from awb import cli, config, exitpack


@pytest.fixture
def home(tmp_path, monkeypatch):
    owner = tmp_path / "owner"
    repo = owner / "tcp-awb"
    repo.mkdir(parents=True)
    monkeypatch.setenv("HOME", str(owner))
    monkeypatch.setattr(exitpack, "ROOT", repo)
    monkeypatch.setattr(config, "is_build_user", lambda: False)
    monkeypatch.setattr(config, "is_work_user", lambda: False)
    monkeypatch.setattr(config, "work_user", lambda: None)
    for marker in ("CLAUDECODE", "CLAUDE_CODE_ENTRYPOINT", *exitpack.SESSION_MARKERS):
        monkeypatch.delenv(marker, raising=False)
    project = owner / ".claude/projects" / re.sub(r"[^a-zA-Z0-9]", "-", str(repo))
    sources = {
        project / "memory/MEMORY.md": "private memory\n",
        project / "memory/topics/topic.md": "another memory\n",
        project / "private-session.jsonl": "private transcript line\n",
        project / "session/subagents/private-agent.jsonl": "private subagent line\n",
        project / "sessions-index.json": "index, not a transcript\n",
        project.parent / "another-project/other.jsonl": "unrelated session\n",
    }
    for name in exitpack.LOCAL_PATHS:
        path = repo / name
        if name in ("build/queue", "presentations"):
            path /= "note.md"
        sources[path] = "private local note\n"
    for path, text in sources.items():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    subprocess.run(["git", "-C", str(repo), "-c", "user.name=fixture", "-c", "user.email=fixture@example.invalid",
                    "commit", "--allow-empty", "-qm", "fixture", "--no-verify"], check=True)
    return SimpleNamespace(owner=owner, repo=repo, project=project,
                           base=owner / "awb-exit", day=datetime.now(timezone.utc).date().isoformat())


@pytest.fixture
def sudo(home, monkeypatch):
    work = home.owner.parent / "work"
    session = work / ".claude/projects/fixture/private-work.jsonl"
    session.parent.mkdir(parents=True)
    session.write_text("private work transcript\n")
    monkeypatch.setattr(config, "work_user", lambda: "work-fixture")
    monkeypatch.setattr(exitpack.pwd, "getpwnam", lambda name: SimpleNamespace(pw_dir=str(work)))
    real_run = subprocess.run
    calls = []
    state = SimpleNamespace(refused=False, calls=calls, session=session)

    def run(argv, **kwargs):
        if argv[0] != "sudo":
            return real_run(argv, **kwargs)
        calls.append(argv)
        assert argv == ["sudo", "-n", "tar", "-czf", "-", "-C", str(work / ".claude"), "projects"]
        assert kwargs["stderr"] == subprocess.DEVNULL
        assert kwargs["stdin"] == subprocess.DEVNULL
        assert stat.S_IMODE(os.fstat(kwargs["stdout"].fileno()).st_mode) == 0o600
        if state.refused:
            kwargs["stdout"].write(b"partial archive")
            return subprocess.CompletedProcess(argv, 1)
        return real_run(argv[2:], **kwargs)

    monkeypatch.setattr(exitpack.subprocess, "run", run)
    return state


def test_archives_counts_digests_commit_and_private_modes(home, sudo, capsys):
    assert cli.main(["exit", "pack"]) == 0
    day = home.base / home.day
    assert {p.name for p in day.iterdir()} == {
        "memory.tgz", "transcripts.tgz", "transcripts-work.tgz", "local-only.tgz", "MANIFEST.txt"}
    manifest = (day / "MANIFEST.txt").read_text()
    commit = subprocess.check_output(["git", "-C", str(home.repo), "rev-parse", "HEAD"], text=True).strip()
    assert f"repository commit: {commit}" in manifest
    for directory in (home.base, day):
        assert stat.S_IMODE(directory.stat().st_mode) == 0o700
    expected_files = {"memory.tgz": 2, "transcripts.tgz": 2, "local-only.tgz": 7, "transcripts-work.tgz": 1}
    expected_names = {
        "memory.tgz": {"memory/MEMORY.md", "memory/topics/topic.md"},
        "transcripts.tgz": {"private-session.jsonl", "session/subagents/private-agent.jsonl"},
        "local-only.tgz": {"CLAUDE.local.md", "build/PROGRESS.md", "build/COVERAGE.md", "build/DECISIONS.md",
                           "build/OPEN.md", "build/queue/note.md", "presentations/note.md"},
        "transcripts-work.tgz": {"projects/fixture/private-work.jsonl"},
    }
    for path in day.iterdir():
        assert path.stat().st_uid == os.geteuid()
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
        if path.suffix != ".tgz":
            continue
        with tarfile.open(path) as archive:
            members = archive.getmembers()
            assert {m.name for m in members if m.isfile()} == expected_names[path.name]
            assert all(archive.extractfile(m).read() for m in members if m.isfile())
        entries = len(subprocess.check_output(["tar", "-tzf", str(path)]).splitlines())
        assert entries == len(members)
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        assert (f"{path.name}: files={expected_files[path.name]} entries={entries} "
                f"bytes={path.stat().st_size} sha256={digest}") in manifest
    assert len(sudo.calls) == 1
    assert "sudo -n tar" in manifest.splitlines()[-1]
    out, err = capsys.readouterr()
    assert err == ""
    assert len(out.splitlines()) == 4
    assert all(re.fullmatch(r"[a-z-]+\.tgz: files=\d+ entries=\d+ bytes=\d+", line) for line in out.splitlines())


@pytest.mark.parametrize("marker", ["CLAUDECODE", "CLAUDE_CODE_ENTRYPOINT", *exitpack.SESSION_MARKERS])
def test_model_session_refused_before_any_output_is_written(home, monkeypatch, marker, capsys):
    monkeypatch.setenv(marker, "1")
    assert cli.main(["exit", "pack"]) == 2
    assert not home.base.exists()
    assert "outside a model session" in capsys.readouterr().err


def test_work_user_refused(home, monkeypatch):
    monkeypatch.setattr(config, "is_work_user", lambda: True)
    assert cli.main(["exit", "pack"]) == 2
    assert not home.base.exists()


def test_repository_must_belong_to_caller(home, monkeypatch):
    monkeypatch.setattr(exitpack.os, "geteuid", lambda: home.repo.stat().st_uid + 1)
    assert cli.main(["exit", "pack"]) == 2
    assert not home.base.exists()


def test_no_transcripts_omits_both_archives_and_sudo(home, sudo):
    dest = home.owner / "custom"
    assert cli.main(["exit", "pack", "--to", str(dest), "--no-transcripts"]) == 0
    day = dest / home.day
    assert {p.name for p in day.iterdir()} == {"memory.tgz", "local-only.tgz", "MANIFEST.txt"}
    manifest = (day / "MANIFEST.txt").read_text()
    assert "owner transcripts: omitted (--no-transcripts)" in manifest
    assert "work transcripts: omitted (--no-transcripts)" in manifest
    assert not sudo.calls
    assert not home.base.exists()


def test_refused_sudo_keeps_three_archives_and_returns_one(home, sudo, capsys):
    sudo.refused = True
    assert cli.main(["exit", "pack"]) == 1
    day = home.base / home.day
    assert {p.name for p in day.iterdir()} == {"memory.tgz", "transcripts.tgz", "local-only.tgz", "MANIFEST.txt"}
    assert "work transcripts: not packed (sudo)" in (day / "MANIFEST.txt").read_text()
    out, err = capsys.readouterr()
    assert err == ""
    assert len(out.splitlines()) == 3
    assert "private" not in out


def test_same_day_replaced_and_older_day_preserved(home):
    older = home.base / "2001-01-01"
    older.mkdir(parents=True)
    (older / "keep").write_text("keep")
    assert cli.main(["exit", "pack"]) == 0
    day = home.base / home.day
    (day / "stale").write_text("stale")
    (home.project / "memory/new.md").write_text("new memory")
    assert cli.main(["exit", "pack", "--no-transcripts"]) == 0
    assert not (day / "stale").exists()
    assert not (day / "transcripts.tgz").exists()
    assert (older / "keep").read_text() == "keep"
    with tarfile.open(day / "memory.tgz") as archive:
        assert "memory/new.md" in archive.getnames()


@pytest.mark.parametrize("keep", [None, 2])
def test_keep_prunes_only_oldest_real_dated_folders(home, keep):
    home.base.mkdir()
    dates = [f"2001-01-{i:02}" for i in range(1, 9)]
    for name in dates + ["notes", "2001-99-99", "2001-1-01", "2001-01-01-extra"]:
        (home.base / name).mkdir()
    elsewhere = home.owner / "elsewhere"
    elsewhere.mkdir()
    (elsewhere / "keep").write_text("keep")
    (home.base / "1999-01-01").symlink_to(elsewhere, target_is_directory=True)
    (home.base / "1998-01-01").write_text("a file")
    args = [] if keep is None else ["--keep", str(keep)]
    assert cli.main(["exit", "pack", *args]) == 0
    n = 6 if keep is None else keep
    assert [name for name in dates if (home.base / name).exists()] == dates[-(n - 1):]
    for name in ("notes", "2001-99-99", "2001-1-01", "2001-01-01-extra", "1999-01-01", "1998-01-01"):
        assert (home.base / name).exists()
    assert (elsewhere / "keep").read_text() == "keep"


@pytest.mark.parametrize("keep", ["0", "-1"])
def test_invalid_keep_refused_without_writes(home, keep):
    assert cli.main(["exit", "pack", "--keep", keep]) == 2
    assert not home.base.exists()


def test_archive_failure_does_not_print_private_path_or_replace_previous_pack(home, monkeypatch, capsys):
    assert cli.main(["exit", "pack"]) == 0
    previous = (home.base / home.day / "MANIFEST.txt").read_bytes()
    capsys.readouterr()

    def fail(*args, **kwargs):
        raise OSError("private-session.jsonl: private transcript line")

    monkeypatch.setattr(exitpack, "_archive", fail)
    assert cli.main(["exit", "pack"]) == 2
    out, err = capsys.readouterr()
    assert not out and "private" not in err
    assert (home.base / home.day / "MANIFEST.txt").read_bytes() == previous


def test_usage_errors_never_echo_transcript_names(home, capsys):
    assert cli.main(["exit", "pack", "private-session.jsonl"]) == 2
    out, err = capsys.readouterr()
    assert not out and "private-session" not in err
    assert not home.base.exists()


def test_timer_is_an_owner_user_unit_weekly_utc_and_persistent():
    root = Path(__file__).resolve().parents[1]
    service = (root / "tools/awb-exit-pack.service").read_text()
    timer = (root / "tools/awb-exit-pack.timer").read_text()
    assert "ExecStart=%h/tcp-awb/.venv/bin/awb exit pack --keep 6" in service
    assert "WorkingDirectory=%h/tcp-awb" in service
    assert "UMask=0077" in service
    assert "User=" not in service and "Environment=" not in service
    assert "OnCalendar=Sun *-*-* 03:00:00 UTC" in timer
    assert "Persistent=true" in timer
    assert "WantedBy=timers.target" in timer


@pytest.mark.parametrize("arguments", [
    lambda value: ["pack", "--keep", value],
    lambda value: [value],
    lambda value: ["pack", "--to=" + value, "--to-other=" + value],
])
def test_hostile_usage_values_are_never_echoed(home, capsys, arguments):
    value = "'" + "pass" + "word" + '\n"customer-name'
    assert cli.main(["exit", *arguments(value)]) == 2
    out, err = capsys.readouterr()
    assert not out and "customer-name" not in err and "pass" + "word" not in err
    assert not home.base.exists()


def test_nul_destination_is_a_private_error(home, capsys):
    value = "pass" + "word" + "\0"
    assert cli.main(["exit", "pack", "--to", value]) == 2
    out, err = capsys.readouterr()
    assert not out and value[:-1] not in err


@pytest.mark.parametrize("location", ["memory", "memory/topics", "private-session.jsonl",
                                     "session", "local", "build", "project-parent"])
def test_source_links_never_pack_outside_data(home, location, capsys):
    import shutil

    outside = home.owner / "outside"
    outside.mkdir()
    secret = "pass" + "word"
    (outside / "stolen.jsonl").write_text(secret)
    (outside / "MEMORY.md").write_text(secret)
    (outside / "PROGRESS.md").write_text(secret)
    source = {"local": home.repo / "CLAUDE.local.md", "build": home.repo / "build",
              "project-parent": home.project.parent}.get(location, home.project / location)
    if source.is_dir():
        shutil.rmtree(source)
        source.symlink_to(outside, target_is_directory=True)
    else:
        source.unlink()
        source.symlink_to(outside / "stolen.jsonl")
    assert cli.main(["exit", "pack"]) == 2
    assert not (home.base / home.day).exists()
    out, err = capsys.readouterr()
    assert not out and secret not in err and "stolen" not in err


@pytest.mark.parametrize("kind", ["fifo", "directory", "hardlink"])
def test_transcripts_must_be_single_regular_files(home, kind):
    source = home.project / "private-session.jsonl"
    source.unlink()
    if kind == "fifo":
        os.mkfifo(source)
    elif kind == "directory":
        source.mkdir()
    else:
        other = home.owner / "outside"
        other.write_text("pass" + "word")
        os.link(other, source)
    assert cli.main(["exit", "pack"]) == 2
    assert not (home.base / home.day).exists()


@pytest.mark.parametrize("location", ["memory", "CLAUDE.local.md", "build/queue"])
def test_source_root_types_are_checked(home, location):
    import shutil

    source = home.project / location if location == "memory" else home.repo / location
    if source.is_dir():
        shutil.rmtree(source)
        source.write_text("wrong type")
    else:
        source.unlink()
        source.mkdir()
    assert cli.main(["exit", "pack"]) == 2
    assert not (home.base / home.day).exists()


def test_file_replaced_with_link_at_open_is_refused(home, monkeypatch):
    source = home.project / "memory/MEMORY.md"
    outside = home.owner / "outside"
    outside.write_text("pass" + "word")
    real_open = os.open
    swapped = []

    def race(path, flags, *args, **kwargs):
        if Path(path).name == source.name and not swapped:
            swapped.append(True)
            source.unlink()
            source.symlink_to(outside)
        return real_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(exitpack.os, "open", race)
    assert cli.main(["exit", "pack"]) == 2
    assert swapped
    assert not (home.base / home.day).exists()


@pytest.mark.parametrize("kind", ["ancestor", "source", "repo", "home"])
def test_unsafe_destination_does_not_change_outside_directories(home, kind):
    outside = home.owner / "outside"
    outside.mkdir(mode=0o755)
    link = home.owner / "link"
    link.symlink_to(outside, target_is_directory=True)
    dest = {"ancestor": link / "packs", "source": home.project / "memory/packs",
            "repo": home.repo, "home": home.owner}[kind]
    before = stat.S_IMODE(dest.stat().st_mode) if dest.exists() else None
    assert cli.main(["exit", "pack", "--to", str(dest)]) == 2
    assert not (outside / "packs").exists()
    if before is not None:
        assert stat.S_IMODE(dest.stat().st_mode) == before


@pytest.mark.parametrize("kind", ["symlink", "hardlink", "fifo"])
def test_lock_rejects_links_and_special_files_without_chmod(home, kind):
    home.base.mkdir()
    outside = home.owner / "outside"
    outside.write_text("pass" + "word")
    outside.chmod(0o644)
    lock = home.base / ".exit-pack.lock"
    if kind == "symlink":
        lock.symlink_to(outside)
    elif kind == "hardlink":
        os.link(outside, lock)
    else:
        os.mkfifo(lock)
    assert cli.main(["exit", "pack"]) == 2
    assert stat.S_IMODE(outside.stat().st_mode) == 0o644
    assert outside.read_text() == "pass" + "word"
    assert not (home.base / home.day).exists()


def test_replaced_lock_is_detected_before_packing(home, monkeypatch):
    real_flock = exitpack.fcntl.flock

    def replace(fd, operation):
        real_flock(fd, operation)
        lock = home.base / ".exit-pack.lock"
        lock.unlink()
        lock.touch(mode=0o600)

    monkeypatch.setattr(exitpack.fcntl, "flock", replace)
    assert cli.main(["exit", "pack"]) == 2
    assert not (home.base / home.day).exists()


def test_destination_swap_does_not_redirect_output(home, monkeypatch):
    outside = home.owner / "outside"
    outside.mkdir()
    moved = home.owner / "moved"
    real_flock = exitpack.fcntl.flock

    def replace(fd, operation):
        real_flock(fd, operation)
        home.base.rename(moved)
        home.base.symlink_to(outside, target_is_directory=True)

    monkeypatch.setattr(exitpack.fcntl, "flock", replace)
    assert cli.main(["exit", "pack"]) == 2
    assert list(outside.iterdir()) == []
    assert not (moved / home.day).exists()


def test_publication_failure_preserves_previous_pack(home, monkeypatch, capsys):
    assert cli.main(["exit", "pack"]) == 0
    target = home.base / home.day
    previous = {p.name: p.read_bytes() for p in target.iterdir()}
    capsys.readouterr()
    real_rename = os.rename

    def fail(source, destination, *args, **kwargs):
        if Path(destination).name == home.day and ".exit-pack-previous-" not in str(source):
            raise OSError("pass" + "word")
        return real_rename(source, destination, *args, **kwargs)

    monkeypatch.setattr(exitpack.os, "rename", fail)
    assert cli.main(["exit", "pack"]) == 2
    assert {p.name: p.read_bytes() for p in target.iterdir()} == previous
    out, err = capsys.readouterr()
    assert not out and "pass" + "word" not in err


def test_git_environment_cannot_select_another_repository(home, monkeypatch):
    other = home.owner / "other-repo"
    subprocess.run(["git", "init", "-q", str(other)], check=True)
    subprocess.run(["git", "-C", str(other), "-c", "user.name=fixture", "-c", "user.email=fixture@example.invalid",
                    "commit", "--allow-empty", "-qm", "other", "--no-verify"], check=True)
    expected = subprocess.check_output(["git", "-C", str(home.repo), "rev-parse", "HEAD"], text=True).strip()
    monkeypatch.setenv("GIT_DIR", str(other / ".git"))
    assert cli.main(["exit", "pack"]) == 0
    assert "repository commit: " + expected in (home.base / home.day / "MANIFEST.txt").read_text()


@pytest.mark.parametrize("value", ["pass" + "word" + "\ninjected: yes", "", "z" * 40])
def test_malformed_git_commit_is_not_published(home, monkeypatch, capsys, value):
    monkeypatch.setattr(exitpack.subprocess, "run", lambda *a, **k: SimpleNamespace(stdout=value))
    assert cli.main(["exit", "pack"]) == 2
    assert not (home.base / home.day).exists()
    out, err = capsys.readouterr()
    assert not out and "pass" + "word" not in err


def test_recovery_hint_does_not_disclose_paths(home, sudo):
    dest = home.owner / ("pass" + "word")
    assert cli.main(["exit", "pack", "--to", str(dest)]) == 0
    manifest = (dest / home.day / "MANIFEST.txt").read_text()
    assert "pass" + "word" not in manifest
    assert str(home.owner) not in manifest and str(sudo.session.parent) not in manifest
    assert "sudo -n tar" in manifest


@pytest.mark.parametrize("member_name, member_type", [("../outside", tarfile.REGTYPE),
    ("/outside", tarfile.REGTYPE), ("projects/link", tarfile.SYMTYPE),
    ("projects/device", tarfile.CHRTYPE)])
def test_unsafe_work_archive_is_not_published(home, sudo, monkeypatch, member_name, member_type):
    real_run = exitpack.subprocess.run

    def run(argv, **kwargs):
        if argv[0] != "sudo":
            return real_run(argv, **kwargs)
        with tarfile.open(fileobj=kwargs["stdout"], mode="w:gz") as archive:
            member = tarfile.TarInfo(member_name)
            member.type = member_type
            member.linkname = "../../outside" if member_type == tarfile.SYMTYPE else ""
            archive.addfile(member)
        return subprocess.CompletedProcess(argv, 0)

    monkeypatch.setattr(exitpack.subprocess, "run", run)
    assert cli.main(["exit", "pack"]) == 2
    assert not (home.base / home.day).exists()


def test_missing_optional_sources_produce_explicit_empty_archives(home):
    import shutil

    shutil.rmtree(home.project)
    for name in exitpack.LOCAL_PATHS:
        path = home.repo / name
        if path.is_dir():
            shutil.rmtree(path)
        else:
            path.unlink()
    assert cli.main(["exit", "pack"]) == 0
    target = home.base / home.day
    for name in ("memory", "transcripts", "local-only"):
        with tarfile.open(target / (name + ".tgz")) as archive:
            assert archive.getnames() == []
    manifest = (target / "MANIFEST.txt").read_text()
    assert "memory: source missing" in manifest and "local-only: 0 of 7" in manifest


def test_unreadable_transcript_scan_preserves_previous_pack(home, monkeypatch, capsys):
    assert cli.main(["exit", "pack"]) == 0
    target = home.base / home.day / "MANIFEST.txt"
    previous = target.read_bytes()
    capsys.readouterr()
    real_scandir = os.scandir

    def fail(path):
        if path == str(home.project) or path == home.project:
            raise PermissionError("pass" + "word")
        if isinstance(path, int) and os.fstat(path).st_ino == home.project.stat().st_ino:
            raise PermissionError("pass" + "word")
        return real_scandir(path)

    monkeypatch.setattr(exitpack.os, "scandir", fail)
    assert cli.main(["exit", "pack"]) == 2
    assert target.read_bytes() == previous
    out, err = capsys.readouterr()
    assert not out and "pass" + "word" not in err


def test_build_user_is_refused_without_writes(home, monkeypatch):
    monkeypatch.setattr(config, "is_build_user", lambda: True)
    assert cli.main(["exit", "pack"]) == 2
    assert not home.base.exists()


def test_concurrent_runs_wait_for_lock_and_publish_complete_pack(home, monkeypatch):
    import threading

    real_pack = exitpack._pack_locked
    first_entered = threading.Event()
    release_first = threading.Event()
    second_lock = threading.Event()
    second_entered = threading.Event()
    entered = []
    results = []
    errors = []
    real_flock = exitpack.fcntl.flock

    def flock(fd, operation):
        if threading.current_thread().name == "second":
            second_lock.set()
        return real_flock(fd, operation)

    def locked(*args, **kwargs):
        entered.append(threading.current_thread().name)
        if threading.current_thread().name == "second":
            second_entered.set()
        if len(entered) == 1:
            first_entered.set()
            assert release_first.wait(5)
        return real_pack(*args, **kwargs)

    def run():
        try:
            results.append(exitpack.pack(home.base))
        except BaseException as error:
            errors.append(error)

    monkeypatch.setattr(exitpack.fcntl, "flock", flock)
    monkeypatch.setattr(exitpack, "_pack_locked", locked)
    first = threading.Thread(target=run, name="first")
    second = threading.Thread(target=run, name="second")
    first.start()
    try:
        assert first_entered.wait(5)
        second.start()
        assert second_lock.wait(5)
        assert not second_entered.wait(0.2)
        assert entered == ["first"]
    finally:
        release_first.set()
        first.join(10)
        if second.ident is not None:
            second.join(10)
    assert not first.is_alive() and not second.is_alive()
    assert not errors and results == [0, 0] and entered == ["first", "second"]
    target = home.base / home.day
    manifest = (target / "MANIFEST.txt").read_text()
    for path in target.glob("*.tgz"):
        assert hashlib.sha256(path.read_bytes()).hexdigest() in manifest
    assert not list(home.base.glob(".exit-pack-*"))


def test_work_archive_exclusive_create_does_not_remove_existing_file(home):
    path = home.owner / "existing.tgz"
    path.write_bytes(b"previous")
    with pytest.raises(FileExistsError):
        exitpack._work_archive(path, ["unused"])
    assert path.read_bytes() == b"previous"


@pytest.mark.parametrize("failure", ["missing", "unreadable"])
def test_source_read_failure_preserves_pack_and_hides_values(home, monkeypatch, capsys, failure):
    assert cli.main(["exit", "pack"]) == 0
    target = home.base / home.day
    previous = {p.name: p.read_bytes() for p in target.iterdir()}
    capsys.readouterr()
    source = home.project / "memory/MEMORY.md"
    real_open = os.open
    real_builtin_open = __import__("builtins").open
    hit = []

    def check(path):
        if not isinstance(path, int) and Path(path).name == source.name:
            hit.append(True)
            error = FileNotFoundError if failure == "missing" else PermissionError
            raise error("pass" + "word")

    def fail_open(path, *args, **kwargs):
        check(path)
        return real_open(path, *args, **kwargs)

    def fail_builtin(path, *args, **kwargs):
        check(path)
        return real_builtin_open(path, *args, **kwargs)

    monkeypatch.setattr(exitpack.os, "open", fail_open)
    monkeypatch.setattr("builtins.open", fail_builtin)
    assert cli.main(["exit", "pack"]) == 2
    assert hit
    assert {p.name: p.read_bytes() for p in target.iterdir()} == previous
    out, err = capsys.readouterr()
    assert not out and "pass" + "word" not in err


def test_source_directory_swap_cannot_redirect_reads(home, monkeypatch):
    memory = home.project / "memory"
    outside = home.owner / "outside"
    outside.mkdir()
    (outside / "MEMORY.md").write_text("pass" + "word")
    moved = home.project / "saved-memory"
    real_open = os.open
    swapped = []

    def race(path, flags, *args, **kwargs):
        if Path(path).name == "MEMORY.md" and not swapped:
            swapped.append(True)
            memory.rename(moved)
            memory.symlink_to(outside, target_is_directory=True)
        return real_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(exitpack.os, "open", race)
    assert cli.main(["exit", "pack", "--no-transcripts"]) == 0
    assert swapped
    with tarfile.open(home.base / home.day / "memory.tgz") as archive:
        assert archive.extractfile("memory/MEMORY.md").read() == b"private memory\n"


def test_private_source_names_and_contents_stay_in_archives(home, capsys):
    secret = "pass" + "word"
    name = "invented-customer-" + secret + ".jsonl"
    (home.project / name).write_text(secret)
    assert cli.main(["exit", "pack"]) == 0
    out, err = capsys.readouterr()
    manifest = (home.base / home.day / "MANIFEST.txt").read_text()
    assert secret not in out + err + manifest and "invented-customer" not in out + err + manifest
    with tarfile.open(home.base / home.day / "transcripts.tgz") as archive:
        assert archive.extractfile(name).read() == secret.encode()


@pytest.mark.parametrize("payload", [b"bad archive", b"\x1f\x8b\x08\x00"])
def test_corrupt_successful_work_output_is_a_private_failure(home, sudo, monkeypatch, capsys, payload):
    assert cli.main(["exit", "pack"]) == 0
    target = home.base / home.day
    previous = {p.name: p.read_bytes() for p in target.iterdir()}
    capsys.readouterr()
    real_run = exitpack.subprocess.run

    def run(argv, **kwargs):
        if argv[0] != "sudo":
            return real_run(argv, **kwargs)
        kwargs["stdout"].write(payload)
        return subprocess.CompletedProcess(argv, 0)

    monkeypatch.setattr(exitpack.subprocess, "run", run)
    assert cli.main(["exit", "pack"]) == 2
    assert {p.name: p.read_bytes() for p in target.iterdir()} == previous
    out, err = capsys.readouterr()
    assert not out and "bad archive" not in err
