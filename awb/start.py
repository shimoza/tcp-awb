"""`awb start`: a piece of work in one owner command (P1 of F3).

    awb start project|query --goal "..." [--customer new] [--tag TAG]...

It runs, in his own terminal, the four steps that stood in three places of the docs, each through the command that
does it today. It ends with the one line that opens the session:

1. register: `--customer new` issues the project code and the customer code from the same four characters
   (tcp-xxxx and CUST-XXXX); an existing customer is refused, every project gets its own code. A customer without an active form in
   the register (a new one always) is asked for its written forms on the terminal, as `awb import` asks. A name is
   typed at that prompt and never an argument, so it never lands in the shell history.
2. spawn: `awb spawn KIND --goal ... [--customer new] [--tag TAG]` as the work user through `sudo -n`, as
   `awb projects open` starts a session; on a host without a work user in this process, as the owner runs it there.
3. grant: `awb projects kind CODE KIND`, the owner's grant the key service reads.
4. import: what lies in the project's folder in/ of the owner bucket, through `awb import CODE`. A new project's
   folder is mostly empty: the line says so and `awb import CODE` takes what he drops there later. The vault inbox is
   never taken here (it may hold files of another customer).

With `--from DIR` (and `--trusted` for an export that carries no name) the import of step 4 takes every regular file
of DIR and its subfolders (a link or a special file is skipped and counted) instead of the bucket folder, and the run
ends with the one line `app: SSH host awb-10491, folder WORKHOME/tcp-xxxx`, WORKHOME being the work user's home of the register folder.

Then: `open: awb projects open CODE`. Owner side only: refused for the work user and inside an assistant session.
A step that fails stops the run with its own message; the steps before it stay done and the line says which.
"""
from __future__ import annotations

import argparse
import os
import shlex
import subprocess
import sys
from pathlib import Path

from awb import codes, config, importcmd, intake, projects, register

EXIT_OK, EXIT_HELD, EXIT_ERROR = 0, 1, 2


APP_SSH_HOST = "awb-10491"
"""The SSH host of the app: the owner opens a session there, in the folder of the project."""


class StartError(Exception):
    """A step of the start cannot run. The message carries codes and counts, never a name."""


def _say(text: str) -> None:
    print(text)


def customer_step(p: config.Paths, customer: str | None, *, read=None, tty=None) -> tuple[str | None, str | None]:
    """The (customer code, project code) the spawn gets: a new pair of the same four characters, its customer's
    forms asked first. An existing customer is refused: every project gets its own code."""
    if customer is None:
        return None, None
    if customer != "new":
        if not (codes.is_code(customer) and codes.kind_of(customer) == "CUST" and customer.count("-") == 1):
            raise StartError("a customer code reads CUST-XXXX or new")
        raise StartError("every project gets its own code: give --customer new or no customer")
    code, customer = intake.new_pair(p)
    _say("new customer %s for project %s" % (customer, code))
    if importcmd._active_forms(p, customer) == 0:
        importcmd.ask_forms(p, customer, read=read, tty=tty, allow_none=True)
    # the work side cannot read the register: its outbox folder is how awb spawn knows the code was issued, as
    # awb register add makes it
    config.make_dir(p.outbox / customer, 0o750, shared=True)
    return customer, code


def spawn_argv(kind: str, goal: str, customer: str | None, tags: list[str], code: str | None = None,
               review_tier: int | None = None) -> list[str]:
    argv = ["spawn", kind, "--goal", goal]
    if review_tier:
        argv += ["--review-tier", str(review_tier)]
    if customer:
        argv += ["--customer", "new", "--code", code]
    for tag in tags:
        argv += ["--tag", tag]
    return argv


def spawn_step(p: config.Paths, argv: list[str], *, run=subprocess.run) -> str:
    """Spawn as the work user (sudo -n) or, without one, in this process; the new project's code."""
    from awb import cli

    before = {r.code for r in projects.load(p)}
    if config.work_user():
        prefix = projects.work_shell(run)
        done = run(prefix + ["awb " + shlex.join(argv)])
        code = getattr(done, "returncode", done)
    else:
        code = cli.main(argv)
    if code != 0:
        raise StartError("awb spawn refused (exit %s): nothing was spawned" % code)
    new = [r.code for r in projects.load(p) if r.code not in before]
    if len(new) != 1:
        raise StartError("the spawn ran but no one new project is in the register")
    return new[0]


def grant_step(p: config.Paths, code: str, kind: str, customer: str | None = None) -> None:
    from awb import cli

    if cli._cmd_projects_kind(argparse.Namespace(code=code, kind=kind, customer=customer), p) != 0:
        raise StartError("the grant was not written: run awb projects kind %s %s in your own terminal" % (code, kind))


def import_step(p: config.Paths, code: str, *, read=None, tty=None) -> int:
    files = importcmd._from_bucket(p, code)
    if files is None:
        _say("%s: the bucket folder in/ cannot be read here; awb import %s takes it later" % (code, code))
        return EXIT_OK
    if not files:
        _say("%s: nothing in the bucket folder in/ yet; awb import %s takes what you drop there" % (code, code))
        return EXIT_OK
    return importcmd.run(p, code, files, read=read, tty=tty)


def walk_from(folder: str) -> tuple[list[Path], int]:
    """The regular files of `folder` and its subfolders (sorted) and the count of the entries skipped because they
    are links or special files. A link to a folder is not followed."""
    root = Path(folder)
    if root.is_symlink() or not root.is_dir():
        raise StartError("--from takes a folder")
    files: list[Path] = []
    skipped = 0
    for here, dirs, names in os.walk(root, followlinks=False):
        dirs.sort()
        for d in list(dirs):
            if (Path(here) / d).is_symlink():
                dirs.remove(d)
                skipped += 1
        for name in sorted(names):
            f = Path(here) / name
            if f.is_symlink() or not f.is_file():
                skipped += 1
            else:
                files.append(f)
    if not files:
        raise StartError("--from: no regular file in the folder")
    return files, skipped


def app_line(p: config.Paths, code: str) -> str:
    """The one line that opens the session in the app: the SSH host and the project's folder from the register."""
    pr = next((r for r in projects.load(p) if r.code == code), None)
    folder = pr.path if pr is not None and pr.path else str(Path(p.projects_root) / code)
    return "app: SSH host %s, folder %s" % (APP_SSH_HOST, folder)


def start(p: config.Paths, kind: str, goal: str, customer: str | None = None, tags: list[str] = (), *,
          run=subprocess.run, read=None, tty=None, review_tier: int | None = None, from_dir: str | None = None,
          trusted: bool = False) -> int:
    from awb import vault

    if config.is_work_user() or vault.in_assistant_session():
        raise StartError("awb start runs on the owner side, in your own terminal")
    if kind not in projects.PROJECT_KINDS:
        raise StartError("a piece of work is a query or a project")
    if trusted and not from_dir:
        raise StartError("--trusted goes with --from DIR")
    taken: list[Path] = []
    if from_dir:
        taken, skipped = walk_from(from_dir)
        if skipped:
            _say("--from: %d link(s) or special file(s) skipped" % skipped)
    cust, reserved = customer_step(p, customer, read=read, tty=tty)
    code = spawn_step(p, spawn_argv(kind, goal, cust, list(tags), reserved, review_tier), run=run)
    if reserved and code != reserved:
        raise StartError("%s is spawned but not under the reserved code %s" % (code, reserved))
    try:
        grant_step(p, code, kind, cust)
        if from_dir:
            _say("%s: %d file(s) from the folder given with --from" % (code, len(taken)))
            result = importcmd.run(p, code, taken, read=read, tty=tty, trusted=trusted)
        else:
            result = import_step(p, code, read=read, tty=tty)
    except (StartError, importcmd.ImportRefused, intake.IntakeError, register.RegisterError,
            projects.ProjectError) as err:
        raise StartError("%s is spawned; then: %s" % (code, err)) from None
    _say("open: awb projects open %s" % code)
    if from_dir:
        _say(app_line(p, code))
    return result


def main(argv: list[str] | None = None) -> int:
    """`awb start project|query --goal "..." [--customer new] [--tag TAG]...`. Exit 0 done, 1 the import
    withheld a file, 2 refused or an error."""
    from awb import vault
    from awb.cli import SafeParser

    ap = SafeParser(prog="awb start", description="A piece of work in one command: register, spawn, grant, import "
                                                  "(owner side).")
    ap.add_argument("kind", choices=projects.PROJECT_KINDS, help="query (no test tenant) or project (test tenants)")
    ap.add_argument("--goal", required=True)
    ap.add_argument("--customer", default=None, metavar="new",
                    help="new issues CUST-XXXX with the four characters of the project code; its written forms are "
                         "asked on the terminal, never given here; an existing customer is refused")
    ap.add_argument("--tag", action="append", default=[], help="a tag of rules/tags.txt, as awb spawn takes it")
    ap.add_argument("--review-tier", type=int, choices=projects.REVIEW_TIERS, default=None, metavar="1|2|3",
                    help="the review tier of the project (default 3): written to its SCOPE.md")
    ap.add_argument("--from", dest="from_dir", default=None, metavar="DIR",
                    help="import every regular file of DIR and its subfolders inside the start (instead of the "
                         "bucket folder in/) and end with the app line")
    ap.add_argument("--trusted", action="store_true",
                    help="with --from: no name is guessed in the files (an export that carries none); registered "
                         "forms, secrets, mail addresses and phones are still wiped")
    try:
        args = ap.parse_args(argv)
    except SystemExit as exc:
        return exc.code if isinstance(exc.code, int) else EXIT_ERROR
    try:
        return start(config.paths(), args.kind, args.goal, args.customer, args.tag, review_tier=args.review_tier,
                     from_dir=args.from_dir, trusted=args.trusted)
    except (StartError, importcmd.ImportRefused, intake.IntakeError, register.RegisterError, projects.ProjectError,
            vault.VaultError) as err:
        print("awb start: %s" % err, file=sys.stderr)
        return EXIT_ERROR
    except OSError as err:
        print("awb start: %s (operating system error)" % type(err).__name__, file=sys.stderr)
        return EXIT_ERROR


if __name__ == "__main__":
    sys.exit(main())
