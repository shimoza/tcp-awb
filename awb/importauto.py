"""`awb import --auto`: the owner-side job that takes the new files of in/ of every active project (task
lc-laptop-clean, his yes of 2026-10-22). seal/setup.sh runs it as the owner every 2 minutes
(awb-import-auto.timer).

For each active project whose import is not running (a lock per project, which `awb import` takes as well), the new
files of `<YYYY-MM>/<code>/in/` come down into the vault inbox. The clean copies of `awb clean` (a name that ends in
`.clean.md`, `.clean.log` ...) are imported with `--trusted`: no name is guessed, the registered forms, the secret
check, mail addresses and phones still apply. Any other file is imported in plain wipe mode, as `awb import` does.
The original of each file the intake took moves into the vault (the intake does that) and is then deleted from
in/ through the key service (`bucket_drop_in`); a file the final check withheld stays in in/ and in the vault inbox.
Each import leaves the "new input" notice for the sessions of the project (rulesync). A project whose customer has no
form yet is left for `awb import CODE` in his own terminal (it asks the forms).

One line per run goes to standard output (the journal of the unit) and to `<vault>/import-auto.log`: projects, files,
drops, skips and errors as counts and codes, never a file name.
"""
from __future__ import annotations

import contextlib
import fcntl
import io
import os
import sys
from datetime import datetime
from pathlib import Path

from awb import config

LOG_NAME = "import-auto.log"
_HELD: set[str] = set()


@contextlib.contextmanager
def project_lock(p: config.Paths, code: str):
    """The import lock of one project (`<vault>/locks/import-<code>.lock`, flock): yields True when this process
    holds it, False when another import of the project runs. Re-entrant inside one process."""
    if code in _HELD:
        yield True
        return
    folder = Path(p.vault) / "locks"
    folder.mkdir(parents=True, exist_ok=True)
    os.chmod(folder, 0o700)
    fd = os.open(folder / ("import-%s.lock" % code), os.O_WRONLY | os.O_CREAT, 0o600)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            yield False
            return
        _HELD.add(code)
        try:
            yield True
        finally:
            _HELD.discard(code)
            fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)


def _log(p: config.Paths, line: str) -> None:
    print(line, flush=True)
    try:
        fd = os.open(Path(p.vault) / LOG_NAME, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        with os.fdopen(fd, "a", encoding="utf-8") as fh:
            fh.write(line + "\n")
    except OSError:
        pass


def run(p: config.Paths, client=None, now: datetime | None = None) -> int:
    """One run over every active project. Returns 0, or 2 when the bucket could not be read or a project failed."""
    from awb import bucket, clean, importcmd, intake, obs, projects, register, vault

    stamp = (now or datetime.now()).strftime("%Y-%m-%dT%H:%M:%S")
    if config.is_work_user() or vault.in_assistant_session():
        print("awb import --auto: the owner's job, run by its timer or in his own terminal", file=sys.stderr)
        return importcmd.EXIT_ERROR
    try:
        c = client or bucket.client()
        active = [pr for pr in projects.load(p) if pr.state in projects.LIVE_STATES]
    except (bucket.BucketError, projects.ProjectError) as err:
        _log(p, "%s import --auto: nothing done (%s)" % (stamp, err))
        return importcmd.EXIT_ERROR
    looked = taken = trusted = dropped = left = skipped = errors = 0
    into: list[str] = []
    for pr in active:
        with project_lock(p, pr.code) as got:
            if not got:
                skipped += 1
                continue
            looked += 1
            try:
                if bucket.find_folder(c, pr.code) is None:
                    continue
                got_files = bucket.fetch(p, c, pr.code)
            except (bucket.BucketError, obs.OBSError, OSError):
                errors += 1
                continue
            files, keys = got_files["files"], got_files.get("keys") or []
            if not files:
                continue
            groups = ([f for f in files if clean.is_clean_name(f.name)], True), \
                     ([f for f in files if not clean.is_clean_name(f.name)], False)
            for group, trust in groups:
                if not group:
                    continue
                quiet_out, quiet_err = io.StringIO(), io.StringIO()
                try:
                    with contextlib.redirect_stdout(quiet_out), contextlib.redirect_stderr(quiet_err):
                        importcmd.run(p, pr.code, group, tty=False, trusted=trust, auto=True)
                except (importcmd.ImportRefused, intake.IntakeError, register.RegisterError, projects.ProjectError,
                        vault.VaultError, OSError):
                    errors += 1
            done = 0
            for f, key in zip(files, keys):
                if f.exists():
                    left += 1
                    continue
                done += 1
                if clean.is_clean_name(f.name):
                    trusted += 1
                try:
                    c.drop_in(key)
                    dropped += 1
                except (bucket.BucketError, obs.OBSError, OSError):
                    errors += 1
            taken += done
            if done:
                into.append(pr.code)
    _log(p, "%s import --auto: %d project(s) looked at, %d file(s) imported (%d clean copies with --trusted)%s, "
            "%d dropped from in/, %d left in the vault inbox, %d project(s) skipped while their import runs, "
            "%d error(s)" % (stamp, looked, taken, trusted, " into " + ", ".join(into) if into else "", dropped, left,
                             skipped, errors))
    return importcmd.EXIT_ERROR if errors else importcmd.EXIT_OK
