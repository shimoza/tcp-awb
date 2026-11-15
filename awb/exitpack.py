"""Local recovery packs for the owner, without a model or network call (LX-5)."""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone, date
import fcntl
import hashlib
import os
from pathlib import Path, PurePosixPath
import pwd
import re
import secrets
import shutil
import stat
import subprocess
import sys

import tarfile

from awb import config, vault

ROOT = Path(__file__).resolve().parent.parent
LOCAL_PATHS = (
    "CLAUDE.local.md", "build/PROGRESS.md", "build/COVERAGE.md",
    "build/DECISIONS.md", "build/OPEN.md", "build/queue", "presentations",
)
SESSION_MARKERS = ("CODEX_THREAD_ID", "CODEX_CI")


class PackError(Exception):
    """A fixed message that cannot disclose a source file name or its content."""


@contextmanager
def _directory(path: Path, *, dir_fd=None, create=False):
    """Open each component without following links, and keep the directory pinned."""
    path = Path(path)
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    fd = os.open("/" if path.is_absolute() else ".", flags, dir_fd=dir_fd)
    try:
        for part in path.parts:
            if part in ("/", "."):
                continue
            if part == "..":
                raise PackError("parent traversal is not allowed")
            if create:
                try:
                    os.mkdir(part, mode=0o700, dir_fd=fd)
                except FileExistsError:
                    pass
            child = os.open(part, flags, dir_fd=fd)
            os.close(fd)
            fd = child
        yield fd
    finally:
        os.close(fd)


def _regular(info) -> None:
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
        raise PackError("a file must be regular and have no links")


def _private_file(path: Path, *, dir_fd=None):
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=dir_fd)
    try:
        os.fchmod(fd, 0o600)
        return os.fdopen(fd, "wb")
    except BaseException:
        os.close(fd)
        raise


def _source_present(path: Path, *, directory: bool) -> bool:
    try:
        with _directory(path.parent) as parent:
            info = os.stat(path.name, dir_fd=parent, follow_symlinks=False)
    except FileNotFoundError:
        return False
    if directory:
        if not stat.S_ISDIR(info.st_mode):
            raise PackError("a source folder must be a directory, not a link")
    else:
        _regular(info)
    return True


def _archive(path: Path, base: Path, sources: list[Path], *, dir_fd=None,
             directories=()) -> None:
    def add(archive, parent, name, arcname, expected_directory=None):
        fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
        try:
            info = os.fstat(fd)
            is_dir = stat.S_ISDIR(info.st_mode)
            if expected_directory is not None and is_dir != expected_directory:
                raise PackError("a source has an unexpected file type")
            if not is_dir:
                _regular(info)
            member = tarfile.TarInfo(arcname)
            member.mode = stat.S_IMODE(info.st_mode)
            member.uid, member.gid, member.mtime = info.st_uid, info.st_gid, info.st_mtime
            if is_dir:
                member.type = tarfile.DIRTYPE
                archive.addfile(member)
                for child in sorted(os.listdir(fd)):
                    add(archive, fd, child, arcname + "/" + child)
            else:
                member.size = info.st_size
                with os.fdopen(os.dup(fd), "rb") as stream:
                    archive.addfile(member, stream)
        finally:
            os.close(fd)

    with _private_file(path, dir_fd=dir_fd) as out, tarfile.open(fileobj=out, mode="w:gz") as archive:
        if sources:
            with _directory(base) as root:
                for source in sources:
                    relative = source.relative_to(base)
                    with _directory(relative.parent, dir_fd=root) as parent:
                        add(archive, parent, relative.name, relative.as_posix(),
                            relative.as_posix() in directories)


def _summary(path: Path, *, dir_fd=None) -> tuple[str, str]:
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=dir_fd)
    with os.fdopen(fd, "rb") as stream:
        info = os.fstat(fd)
        _regular(info)
        # Work tar output is untrusted too. Never publish links, devices or escaping names.
        with tarfile.open(fileobj=stream, mode="r:gz") as archive:
            entries = files = 0
            for member in archive:
                name = PurePosixPath(member.name)
                if (name.is_absolute() or ".." in name.parts or not name.parts
                        or not (member.isfile() or member.isdir())):
                    raise PackError("an archive contains unsafe members")
                entries += 1
                files += member.isfile()
        stream.seek(0)
        digest = hashlib.file_digest(stream, "sha256").hexdigest()
    counts = f"{path.name}: files={files} entries={entries} bytes={info.st_size}"
    return counts, f"{counts} sha256={digest}"


def _work_argv() -> list[str] | None:
    user = config.work_user()
    if not user:
        return None
    # Resolve the other uid's home through passwd, never by opening its sessions.
    home = Path(pwd.getpwnam(user).pw_dir)
    return ["sudo", "-n", "tar", "-czf", "-", "-C", str(home / ".claude"), "projects"]


def _work_archive(path: Path, argv: list[str], *, dir_fd=None) -> bool:
    # Do not unlink a pre-existing file if exclusive creation fails.
    with _private_file(path, dir_fd=dir_fd) as out:
        try:
            result = subprocess.run(argv, stdin=subprocess.DEVNULL, stdout=out,
                                    stderr=subprocess.DEVNULL)
            if result.returncode == 0:
                return True
        except OSError:
            pass
    os.unlink(path, dir_fd=dir_fd)
    return False


def _prune(base_fd: int, keep: int) -> None:
    days = []
    for name in os.listdir(base_fd):
        info = os.stat(name, dir_fd=base_fd, follow_symlinks=False)
        if not stat.S_ISDIR(info.st_mode) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", name):
            continue
        try:
            date.fromisoformat(name)
        except ValueError:
            continue
        days.append(name)
    for name in sorted(days, reverse=True)[keep:]:
        shutil.rmtree(name, dir_fd=base_fd)


def _same_directory(path: Path, fd: int) -> None:
    with _directory(path) as current:
        if not os.path.samestat(os.fstat(current), os.fstat(fd)):
            raise PackError("the destination changed during packing")


def pack(base: Path, *, no_transcripts: bool = False, keep: int = 6) -> int:
    if (config.is_work_user() or config.is_build_user() or vault.in_assistant_session()
            or any(os.environ.get(marker) for marker in SESSION_MARKERS)):
        raise PackError("run in the owner's terminal outside a model session")
    if ROOT.stat().st_uid != os.geteuid():
        raise PackError("the repository owner must run this command")
    if keep < 1:
        raise PackError("--keep must be at least 1")
    project = Path.home() / ".claude" / "projects" / re.sub(r"[^a-zA-Z0-9]", "-", str(ROOT.resolve()))
    memory = project / "memory"
    # Check ancestors even when the project or its memory is absent.
    _source_present(project, directory=True)
    base = Path(os.path.abspath(base.expanduser()))
    resolved = base.resolve()
    sources = [project] + [ROOT / name for name in LOCAL_PATHS]
    if (ROOT.resolve().is_relative_to(resolved)
            or any(resolved.is_relative_to(source.resolve()) or source.resolve().is_relative_to(resolved)
                   for source in sources)):
        raise PackError("the destination must be outside the source folders")
    with _directory(base, create=True) as base_fd:
        if os.fstat(base_fd).st_uid != os.geteuid():
            raise PackError("the destination must belong to the owner")
        os.fchmod(base_fd, 0o700)
        lock_fd = os.open(".exit-pack.lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK,
                          0o600, dir_fd=base_fd)
        try:
            info = os.fstat(lock_fd)
            _regular(info)
            if info.st_uid != os.geteuid():
                raise PackError("the lock must belong to the owner")
            fcntl.flock(lock_fd, fcntl.LOCK_EX)
            current = os.stat(".exit-pack.lock", dir_fd=base_fd, follow_symlinks=False)
            if not os.path.samestat(info, current):
                raise PackError("the lock changed during packing")
            os.fchmod(lock_fd, 0o600)
            _same_directory(base, base_fd)
            return _pack_locked(base, project, memory, no_transcripts, keep, base_fd=base_fd)
        finally:
            os.close(lock_fd)


def _target_exists(base_fd: int, day: str) -> bool:
    try:
        info = os.stat(day, dir_fd=base_fd, follow_symlinks=False)
    except FileNotFoundError:
        return False
    if not stat.S_ISDIR(info.st_mode):
        raise PackError("the dated destination must be a directory, not a link")
    return True


def _sessions(project: Path) -> list[Path]:
    if not _source_present(project, directory=True):
        return []
    sessions = []

    def scan(fd, relative):
        with os.scandir(fd) as entries:
            names = sorted(entry.name for entry in entries)
        for name in names:
            if name == "memory":
                continue
            info = os.stat(name, dir_fd=fd, follow_symlinks=False)
            if stat.S_ISLNK(info.st_mode):
                raise PackError("a transcript source must not be a link")
            if name.endswith(".jsonl"):
                _regular(info)
                sessions.append(project / relative / name)
            elif stat.S_ISDIR(info.st_mode):
                with _directory(Path(name), dir_fd=fd) as child:
                    scan(child, relative / name)

    with _directory(project) as root:
        scan(root, Path())
    return sessions


def _pack_locked(base: Path, project: Path, memory: Path, no_transcripts: bool, keep: int,
                 *, base_fd: int) -> int:
    day = datetime.now(timezone.utc).date().isoformat()
    _target_exists(base_fd, day)
    # -C alone does not override an inherited GIT_DIR or injected Git configuration.
    env = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
    commit = subprocess.run(["git", "-C", str(ROOT), "rev-parse", "--verify", "HEAD"],
                            capture_output=True, text=True, check=True, env=env).stdout.strip()
    if not re.fullmatch(r"(?:[0-9a-f]{40}|[0-9a-f]{64})", commit):
        raise PackError("the repository commit is invalid")
    manifest = [f"date: {day} UTC", f"repository commit: {commit}",
                "files counts regular files; entries counts all tar members; bytes is archive size"]
    output = []
    status = 0
    work_argv = None
    stage = ".exit-pack-" + secrets.token_hex(12)
    backup = ".exit-pack-previous-" + secrets.token_hex(12)
    os.mkdir(stage, mode=0o700, dir_fd=base_fd)
    try:
        with _directory(Path(stage), dir_fd=base_fd) as stage_fd:
            os.fchmod(stage_fd, 0o700)
            memory_present = _source_present(memory, directory=True)
            _archive(Path("memory.tgz"), project, [memory] if memory_present else [],
                     dir_fd=stage_fd, directories=("memory",))
            if not memory_present:
                manifest.append("memory: source missing")
            directories = ("build/queue", "presentations")
            local = [ROOT / name for name in LOCAL_PATHS
                     if _source_present(ROOT / name, directory=name in directories)]
            _archive(Path("local-only.tgz"), ROOT, local, dir_fd=stage_fd, directories=directories)
            manifest.append(f"local-only: {len(local)} of {len(LOCAL_PATHS)} source paths present")
            if no_transcripts:
                manifest += ["owner transcripts: omitted (--no-transcripts)",
                             "work transcripts: omitted (--no-transcripts)"]
            else:
                _archive(Path("transcripts.tgz"), project, _sessions(project), dir_fd=stage_fd)
                work_argv = _work_argv()
                if work_argv is None:
                    manifest.append("work transcripts: not packed (no work user configured)")
                elif not _work_archive(Path("transcripts-work.tgz"), work_argv, dir_fd=stage_fd):
                    manifest.append("work transcripts: not packed (sudo)")
                    status = 1
                else:
                    manifest.append("work transcripts: packed")
            for name in sorted(os.listdir(stage_fd)):
                if name.endswith(".tgz"):
                    counts, record = _summary(Path(name), dir_fd=stage_fd)
                    output.append(counts)
                    manifest.append(record)
            if work_argv:
                manifest.append("Set WORK_HOME to the work user's home and PACK_DIR to this dated folder.")
                manifest.append("Work transcripts can be packed again in the owner's shell:")
                manifest.append('(umask 077; sudo -n tar -czf - -C "$WORK_HOME/.claude" projects '
                                '> "$PACK_DIR/transcripts-work.tgz")')
            with _private_file(Path("MANIFEST.txt"), dir_fd=stage_fd) as out:
                out.write(("\n".join(manifest) + "\n").encode("utf-8"))
            _same_directory(base, base_fd)
            previous = _target_exists(base_fd, day)
            if previous:
                os.rename(day, backup, src_dir_fd=base_fd, dst_dir_fd=base_fd)
            try:
                os.rename(stage, day, src_dir_fd=base_fd, dst_dir_fd=base_fd)
            except OSError:
                if previous:
                    # If even rollback fails, leave the backup for manual recovery.
                    os.rename(backup, day, src_dir_fd=base_fd, dst_dir_fd=base_fd)
                raise
            if previous:
                shutil.rmtree(backup, dir_fd=base_fd)
    finally:
        try:
            shutil.rmtree(stage, dir_fd=base_fd)
        except FileNotFoundError:
            pass
    _prune(base_fd, keep)
    for line in output:
        print(line)
    return status


def main(argv: list[str] | None = None) -> int:
    from awb.cli import SafeParser

    class Parser(SafeParser):
        def error(self, message):
            super().error("invalid exit pack arguments")

    parser = Parser(prog="awb exit", description="Local recovery packs, owner only.")
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
    except (OSError, ValueError, RuntimeError, EOFError, KeyError, subprocess.SubprocessError, tarfile.TarError):
        # Exceptions can contain transcript paths. Never echo them.
        print("awb exit: pack failed (local file or archive operation)", file=sys.stderr)
    return 2
