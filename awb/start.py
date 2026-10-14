"""`awb start`: a piece of work in one owner command (P1 of F3).

    awb start project|query --goal "..." [--customer CUST-XXXX|new] [--tag TAG]...

It runs, in his own terminal, the four steps that stood in three places of the docs, each through the command that
does it today. It ends with the one line that opens the session:

1. register: `--customer new` issues a customer code (`awb register new CUST`); a customer without an active form in
   the register (a new one always) is asked for its written forms on the terminal, as `awb import` asks. A name is
   typed at that prompt and never an argument, so it never lands in the shell history.
2. spawn: `awb spawn KIND --goal ... [--customer CUST-XXXX] [--tag TAG]` as the work user through `sudo -n`, as
   `awb projects open` starts a session; on a host without a work user in this process, as the owner runs it there.
3. grant: `awb projects kind CODE KIND`, the owner's grant the key service reads.
4. import: what lies in the project's folder in/ of the owner bucket, through `awb import CODE`. A new project's
   folder is mostly empty: the line says so and `awb import CODE` takes what he drops there later. The vault inbox is
   never taken here (it may hold files of another customer).

Then: `open: awb projects open CODE`. Owner side only: refused for the work user and inside an assistant session.
A step that fails stops the run with its own message; the steps before it stay done and the line says which.
"""
from __future__ import annotations

import argparse
import shlex
import subprocess
import sys

from awb import codes, config, importcmd, intake, projects, register

EXIT_OK, EXIT_HELD, EXIT_ERROR = 0, 1, 2


class StartError(Exception):
    """A step of the start cannot run. The message carries codes and counts, never a name."""


def _say(text: str) -> None:
    print(text)


def customer_step(p: config.Paths, customer: str | None, *, read=None, tty=None) -> str | None:
    """The customer code the spawn gets, its forms asked first where the register has none."""
    if customer is None:
        return None
    if customer == "new":
        customer = intake.safe_new_code(register.load(p.register), "CUST", intake.issued_codes(p))
        _say("new customer %s" % customer)
    elif not (codes.is_code(customer) and codes.kind_of(customer) == "CUST" and customer.count("-") == 1):
        raise StartError("a customer code reads CUST-XXXX or new")
    if importcmd._active_forms(p, customer) == 0:
        importcmd.ask_forms(p, customer, read=read, tty=tty)
    # the work side cannot read the register: its outbox folder is how awb spawn knows the code was issued, as
    # awb register add makes it
    config.make_dir(p.outbox / customer, 0o750, shared=True)
    return customer


def spawn_argv(kind: str, goal: str, customer: str | None, tags: list[str]) -> list[str]:
    argv = ["spawn", kind, "--goal", goal]
    if customer:
        argv += ["--customer", customer]
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


def grant_step(p: config.Paths, code: str, kind: str) -> None:
    from awb import cli

    if cli._cmd_projects_kind(argparse.Namespace(code=code, kind=kind), p) != 0:
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


def start(p: config.Paths, kind: str, goal: str, customer: str | None = None, tags: list[str] = (), *,
          run=subprocess.run, read=None, tty=None) -> int:
    from awb import vault

    if config.is_work_user() or vault.in_assistant_session():
        raise StartError("awb start runs on the owner side, in your own terminal")
    if kind not in projects.PROJECT_KINDS:
        raise StartError("a piece of work is a query or a project")
    cust = customer_step(p, customer, read=read, tty=tty)
    code = spawn_step(p, spawn_argv(kind, goal, cust, list(tags)), run=run)
    try:
        grant_step(p, code, kind)
        result = import_step(p, code, read=read, tty=tty)
    except (StartError, importcmd.ImportRefused, intake.IntakeError, register.RegisterError,
            projects.ProjectError) as err:
        raise StartError("%s is spawned; then: %s" % (code, err)) from None
    _say("open: awb projects open %s" % code)
    return result


def main(argv: list[str] | None = None) -> int:
    """`awb start project|query --goal "..." [--customer CUST-XXXX|new] [--tag TAG]...`. Exit 0 done, 1 the import
    withheld a file, 2 refused or an error."""
    from awb import vault
    from awb.cli import SafeParser

    ap = SafeParser(prog="awb start", description="A piece of work in one command: register, spawn, grant, import "
                                                  "(owner side).")
    ap.add_argument("kind", choices=projects.PROJECT_KINDS, help="query (no test tenant) or project (test tenants)")
    ap.add_argument("--goal", required=True)
    ap.add_argument("--customer", default=None, metavar="CUST-XXXX|new",
                    help="the customer's code or new to issue one; its written forms are asked on the terminal, "
                         "never given here")
    ap.add_argument("--tag", action="append", default=[], help="a tag of rules/tags.txt, as awb spawn takes it")
    try:
        args = ap.parse_args(argv)
    except SystemExit as exc:
        return exc.code if isinstance(exc.code, int) else EXIT_ERROR
    try:
        return start(config.paths(), args.kind, args.goal, args.customer, args.tag)
    except (StartError, importcmd.ImportRefused, intake.IntakeError, register.RegisterError, projects.ProjectError,
            vault.VaultError) as err:
        print("awb start: %s" % err, file=sys.stderr)
        return EXIT_ERROR
    except OSError as err:
        print("awb start: %s (operating system error)" % type(err).__name__, file=sys.stderr)
        return EXIT_ERROR


if __name__ == "__main__":
    sys.exit(main())
