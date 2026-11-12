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
