"""Local recovery packs for the owner, without a model or network call (LX-5)."""
from __future__ import annotations

from datetime import datetime, timezone, date
import fcntl
import hashlib
import os
from pathlib import Path
import pwd
import re
import shlex
import shutil
import subprocess
import sys
import tarfile
import tempfile

from awb import config, vault

ROOT = Path(__file__).resolve().parent.parent
LOCAL_PATHS = (
    "CLAUDE.local.md", "build/PROGRESS.md", "build/COVERAGE.md",
    "build/DECISIONS.md", "build/OPEN.md", "build/queue", "presentations",
)
SESSION_MARKERS = ("CODEX_THREAD_ID", "CODEX_CI")


class PackError(Exception):
    """A fixed message that cannot disclose a source file name or its content."""


def _private_file(path: Path):
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    os.fchmod(fd, 0o600)
    return os.fdopen(fd, "wb")


def _archive(path: Path, base: Path, sources: list[Path]) -> None:
    with _private_file(path) as out, tarfile.open(fileobj=out, mode="w:gz") as archive:
        for source in sources:
            archive.add(source, arcname=source.relative_to(base))


def _summary(path: Path) -> tuple[str, str]:
    # Count members as well as regular files: tar -tzf includes directory and link entries.
    with tarfile.open(path, "r:gz") as archive:
        entries = files = 0
        for member in archive:
            entries += 1
            files += member.isfile()
    with path.open("rb") as stream:
        digest = hashlib.file_digest(stream, "sha256").hexdigest()
    counts = f"{path.name}: files={files} entries={entries} bytes={path.stat().st_size}"
    return counts, f"{counts} sha256={digest}"


def _work_argv() -> list[str] | None:
    user = config.work_user()
    if not user:
        return None
    # Resolve the other uid's home through passwd, never by opening its sessions.
    home = Path(pwd.getpwnam(user).pw_dir)
    return ["sudo", "-n", "tar", "-czf", "-", "-C", str(home / ".claude"), "projects"]


def _work_archive(path: Path, argv: list[str]) -> bool:
    try:
        # stdout is opened by the owner, so sudo never creates a root-owned archive.
        with _private_file(path) as out:
            result = subprocess.run(argv, stdin=subprocess.DEVNULL, stdout=out,
                                    stderr=subprocess.DEVNULL)
        if result.returncode == 0:
            return True
    except OSError:
        pass
    path.unlink(missing_ok=True)
    return False


def _prune(base: Path, keep: int) -> None:
    days = []
    for path in base.iterdir():
        if path.is_symlink() or not path.is_dir() or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", path.name):
            continue
        try:
            date.fromisoformat(path.name)
        except ValueError:
            continue
        days.append(path)
    for path in sorted(days, reverse=True)[keep:]:
        shutil.rmtree(path)


def pack(base: Path, *, no_transcripts: bool = False, keep: int = 6) -> int:
    if (config.is_work_user() or config.is_build_user() or vault.in_assistant_session()
            or any(os.environ.get(marker) for marker in SESSION_MARKERS)):
        raise PackError("run in the owner's terminal outside a model session")
    if ROOT.stat().st_uid != os.geteuid():
        raise PackError("the repository owner must run this command")
    if keep < 1:
        raise PackError("--keep must be at least 1")
    project = Path.home() / ".claude" / "projects" / re.sub(r"[^a-zA-Z0-9]", "-", str(ROOT.resolve()))
    if project.is_symlink():
        raise PackError("the client project folder must not be a link")
    memory = project / "memory"
    base = base.expanduser().absolute()
    # Do not let a pack recursively include its own output.
    if any(base.resolve().is_relative_to(source.resolve())
           for source in [project] + [ROOT / name for name in LOCAL_PATHS]):
        raise PackError("the destination must be outside the source folders")
    if base.is_symlink():
        raise PackError("the destination must not be a link")
    base.mkdir(mode=0o700, parents=True, exist_ok=True)
    if base.stat().st_uid != os.geteuid():
        raise PackError("the destination must belong to the owner")
    base.chmod(0o700)
    # Serialize replacement and retention for manual runs overlapping the timer.
    lock_fd = os.open(base / ".exit-pack.lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    with os.fdopen(lock_fd, "w") as lock:
        os.fchmod(lock.fileno(), 0o600)
        fcntl.flock(lock, fcntl.LOCK_EX)
        return _pack_locked(base, project, memory, no_transcripts, keep)


def _pack_locked(base: Path, project: Path, memory: Path, no_transcripts: bool, keep: int) -> int:
    day = datetime.now(timezone.utc).date().isoformat()
    target = base / day
    if target.is_symlink() or (target.exists() and not target.is_dir()):
        raise PackError("the dated destination must be a directory, not a link")
    commit = subprocess.run(["git", "-C", str(ROOT), "rev-parse", "HEAD"],
                            capture_output=True, text=True, check=True).stdout.strip()
    manifest = [f"date: {day} UTC", f"repository commit: {commit}",
                "files counts regular files; entries counts all tar members; bytes is archive size"]
    output = []
    status = 0
    work_argv = None
    with tempfile.TemporaryDirectory(prefix=".exit-pack-", dir=base) as temp:
        stage = Path(temp) / day
        stage.mkdir(mode=0o700)
        stage.chmod(0o700)
        _archive(stage / "memory.tgz", project, [memory] if memory.exists() else [])
        if not memory.exists():
            manifest.append("memory: source missing")
        local = [ROOT / name for name in LOCAL_PATHS if (ROOT / name).exists() or (ROOT / name).is_symlink()]
        _archive(stage / "local-only.tgz", ROOT, local)
        manifest.append(f"local-only: {len(local)} of {len(LOCAL_PATHS)} source paths present")
        if no_transcripts:
            manifest += ["owner transcripts: omitted (--no-transcripts)",
                         "work transcripts: omitted (--no-transcripts)"]
        else:
            sessions = []

            def unreadable(error):
                if not isinstance(error, FileNotFoundError):
                    raise error

            # Include subagent sessions too, but never another client project or memory.
            for folder, dirs, files in os.walk(project, onerror=unreadable):
                dirs[:] = sorted(d for d in dirs if d != "memory" and not (Path(folder) / d).is_symlink())
                sessions.extend(Path(folder) / name for name in sorted(files) if name.endswith(".jsonl"))
            _archive(stage / "transcripts.tgz", project, sessions)
            work_argv = _work_argv()
            if work_argv is None:
                manifest.append("work transcripts: not packed (no work user configured)")
            elif not _work_archive(stage / "transcripts-work.tgz", work_argv):
                manifest.append("work transcripts: not packed (sudo)")
                status = 1
            else:
                manifest.append("work transcripts: packed")
        for archive in sorted(stage.glob("*.tgz")):
            counts, record = _summary(archive)
            output.append(counts)
            manifest.append(record)
        if work_argv:
            manifest.append("Work transcripts can be packed again in the owner's shell:")
            manifest.append("(umask 077; " + shlex.join(work_argv) + " > "
                            + shlex.quote(str(target / "transcripts-work.tgz")) + ")")
        with _private_file(stage / "MANIFEST.txt") as out:
            out.write(("\n".join(manifest) + "\n").encode("utf-8"))
        if target.exists():
            shutil.rmtree(target)
        stage.rename(target)
    _prune(base, keep)
    for line in output:
        print(line)
    return status


def main(argv: list[str] | None = None) -> int:
    from awb.cli import SafeParser

    parser = SafeParser(prog="awb exit", description="Local recovery packs, owner only.")
    sub = parser.add_subparsers(dest="command", required=True)
    command = sub.add_parser("pack", help="pack client memory, transcripts and local repository files")
    command.add_argument("--to", type=Path, default=Path("~/awb-exit"), metavar="DIR",
                         help="parent folder for UTC dated packs (default: ~/awb-exit)")
    command.add_argument("--no-transcripts", action="store_true", help="omit owner and work transcript archives")
    command.add_argument("--keep", type=int, default=6, metavar="N", help="keep the newest N dated folders (default: 6)")
    args = parser.parse_args(argv)
    try:
        return pack(args.to, no_transcripts=args.no_transcripts, keep=args.keep)
    except PackError as error:
        print(f"awb exit: {error}", file=sys.stderr)
    except (OSError, KeyError, subprocess.SubprocessError, tarfile.TarError):
        # Exceptions can contain transcript paths. Never echo them.
        print("awb exit: pack failed (local file or archive operation)", file=sys.stderr)
    return 2
