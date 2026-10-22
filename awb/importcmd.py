"""`awb import CODE [FILE...]`: customer material into a project in one command (owner side, T4).

    awb import CODE [FILE...] [--customer CUST-XXXX|new] [--review] [--redo]

The one door of wipe mode. It takes the FILE arguments, else every new file of the project's in/ folder in the
owner bucket, else every file of the vault inbox, and says how many from where. It runs the intake for the
project's customer: the copies carry the customer as its code and every other name the candidate rules recognise as
a token, nothing stops and no editor opens. It prints the files taken, the copies written, the values wiped per
class, the pictures held and the files withheld, and leaves a notice for the sessions of the project (rulesync).

- The customer is the project's. A project without one runs with no customer code: every candidate becomes a token,
  nothing becomes a code and the copies and the public report go to the outbox under the project code. When a
  customer is to be issued, `--customer new` issues CUST- and the four characters of the project code (a random code
  when that one is taken or unsafe); `--customer CUST-XXXX` names one the register knows.
- A customer without an active form in the register is asked for its written forms on the terminal, one per line,
  an empty line ends; they are registered before the intake runs. Nothing is echoed. For a project without a
  customer a first empty line means "no forms": the code is issued without a form.
- `--review` stops once on the candidates and opens the review of `awb register review` (the marks register the
  names, the rest goes to the keep list), then the run goes on in wipe mode.
- `--redo` takes the originals of the customer's last import out of the vault and runs them again under their file
  ids with the keep list of now; the copies replace the earlier ones in the outbox.

Refused for the work user and inside an assistant session with the one line of the work rules (`hold_line`): the
forms prompt and the review carry names. Nothing here prints a name, a form or a file name.

    awb import --auto

The owner's job of the timer awb-import-auto (seal/setup.sh, every 10 minutes, awb/importauto.py): the new files
of in/ of every active project whose import is not running, the clean copies of `awb clean` with --trusted and any
other file in wipe mode; the originals into the vault and out of in/, the "new input" notice, one log line per run.
An import holds the lock of its project (`<vault>/locks/import-<code>.lock`): a second import of the same project
at the same time is refused with one line.

    awb import CODE --paste [--kind mail|transcript|table]

The same door for text he copies from a mail client, a meeting tool or a spreadsheet (`awb/pastein.py`). The text is
read from standard input (a pipe from the clipboard: `pbpaste |`, `wl-paste |` or `xclip -o -selection clipboard |`
or pasted into the terminal and ended with Ctrl-D without an echo) and never printed. It runs through the intake of
the project's customer in wipe mode. A secret refuses the whole paste with its class and line. The copy goes to
the outbox as `mail-<date>-<n>.md`, `transcript-<date>-<n>.md`, `table-<date>-<n>.csv` or `paste-<date>-<n>.md`
(no kind). The paste takes no FILE, --customer, --review or --redo. Refused for the work user and inside an assistant
session with `codes.PASTE_HOLD_LINE`.
"""
from __future__ import annotations

import base64
import os
import shutil
import sys
import tempfile
from pathlib import Path

from awb import codes, config, intake, register

EXIT_OK, EXIT_HELD, EXIT_ERROR = 0, 1, 2


class ImportRefused(Exception):
    """The import cannot run. The message carries codes and counts, never a name."""


def _say(text: str) -> None:
    print(text)


def _progress(text: str) -> None:
    """One progress line on standard error while the import runs (a file id and a time, never a name)."""
    print(text, file=sys.stderr, flush=True)


project_customer_code = intake.project_customer_code


def new_customer(p: config.Paths, code: str) -> tuple[str, bool]:
    """The code of a new customer for project `code`: CUST- and the project's four characters. The project
    exists, so a taken pair is refused: there is no second code to draw. Returns (code, True)."""
    entries = register.load(p.register)
    wanted = project_customer_code(code)
    if not intake.pair_is_free(p, entries, intake._Engine(entries), wanted):
        raise ImportRefused("%s is taken or not safe as a customer code: %s cannot get a customer of its own code"
                            % (wanted, code))
    return wanted, True


def _active_forms(p: config.Paths, customer: str) -> int:
    return sum(1 for e in register.load(p.register) if e.code == customer and e.status == "active")



def hold_line(code: str) -> str:
    """The one line of the work rules for a take or an import a session cannot do: what the owner runs, in his own
    terminal (the work rules hand out nothing else)."""
    return codes.HOLD_LINE % (code if codes.is_project_code(code or "") else "tcp-xxxx")


def paste_hold_line(code: str) -> str:
    """The one line of the work rules for a paste a session cannot take: the paste door the owner runs."""
    return codes.PASTE_HOLD_LINE % (code if codes.is_project_code(code or "") else "tcp-xxxx")


def ask_forms(p: config.Paths, customer: str, read=None, tty=None, allow_none: bool = False) -> int:
    """The written forms of a customer the register does not know yet, typed on the terminal, one per line, an empty
    line ends. Registered one by one; returns how many. Refused without a terminal. With `allow_none` (a project
    without a customer) an empty first line issues the code without a form: its outbox folder counts it as issued."""
    if not (tty if tty is not None else (sys.stdin.isatty() and sys.stdout.isatty())):
        raise ImportRefused("%s has no form in the register: run awb import in your own terminal, it asks for the "
                           "written forms" % customer)
    read = read or (lambda: sys.stdin.readline())
    _say("%s has no form in the register yet. Type its written forms in German, in English and in Russian, one per "
         "line: the full name, the short name, an acronym, the Cyrillic spelling; an empty line ends.%s"
         % (customer, " An empty first line issues it without a form." if allow_none else ""))
    added = 0
    while True:
        line = read()
        if not line or not line.strip():
            break
        try:
            register.add(p.register, customer, "CUST", line.rstrip("\r\n"))
            added += 1
        except register.RegisterError:
            _say("that line cannot go into the register (brackets, pipes, hashes or a form it holds already); "
                 "type it again or end with an empty line")
    if not added and not allow_none:
        raise ImportRefused("no form was typed: nothing was imported")
    config.make_dir(p.outbox / customer, 0o750, shared=True)
    _say("registered %d form(s) of %s" % (added, customer) if added else "issued %s without a form" % customer)
    return added


def _from_bucket(p: config.Paths, code: str) -> list[Path] | None:
    """The new files of the project's in/ folder, downloaded into the vault inbox; None when the bucket cannot be
    read here (no key, no folder)."""
    from awb import bucket, obs

    try:
        c = bucket.client()
        return bucket.fetch(p, c, code)["files"]
    except (bucket.BucketError, obs.OBSError, OSError):
        return None


def _last_import(p: config.Paths, customer: str) -> list[tuple[str, Path]]:
    """(file id, original in the vault) of the newest private report of the customer that wrote copies."""
    from awb import candidates

    folder = p.private_reports / customer
    reports = sorted((f for f in folder.glob("*.md*") if f.is_file()), reverse=True) if folder.is_dir() else []
    for report in reports:
        text = candidates.read_report(report)
        if "- mode: stopped for the review" in text:
            continue
        rows, inside = [], False
        for line in text.splitlines():
            if line.startswith("## "):
                inside = line.strip() == "## Files"
                continue
            if not inside or not line.startswith("| F-"):
                continue
            cells = candidates._cells(line)
            if len(cells) >= 6 and intake.FILE_ID_RE.fullmatch(cells[0]) and cells[5] not in ("not moved", ""):
                rows.append((cells[0], Path(cells[5])))
        if rows:
            return rows
    return []


def _copy_out(p: config.Paths, original: Path, folder: Path) -> Path:
    """A plaintext copy of an original of the vault (a sealed one through the vault daemon) in `folder`, under the
    vault's own name (the file id and its suffix)."""
    name = original.name[:-4] if original.name.endswith(".gpg") else original.name
    dst = folder / name
    if original.suffix == ".gpg":
        from awb import vault

        answer = vault.admin_call("open_file", p.admin_sock, path=str(original))
        data = base64.b64decode(answer.get("data") or "", validate=True)
    else:
        data = original.read_bytes()
    fd = os.open(dst, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "wb") as fh:
        fh.write(data)
    return dst


def run(p: config.Paths, code: str, files: list[Path], *, customer: str | None = None, review: bool = False,
        redo: bool = False, read=None, tty=None, edit=None, confirm=None, trusted: bool = False) -> int:
    from awb import projects, vault

    if config.is_work_user() or vault.in_assistant_session():
        raise ImportRefused(hold_line(code))
    if not codes.is_project_code(code or ""):
        raise ImportRefused("a project code reads tcp-xxxx")
    project = next((pr for pr in projects.load(p) if pr.code == code), None)
    if project is None or project.state != "active":
        raise ImportRefused("%s is not an active project" % code)
    own = project.customer if project.customer != projects.NO_CUSTOMER else None
    if customer == "new":
        if own:
            raise ImportRefused("%s has the customer %s already" % (code, own))
        cust, _own = new_customer(p, code)
        _say("new customer %s" % cust)
    elif customer:
        if not (codes.is_code(customer) and codes.kind_of(customer) == "CUST" and customer.count("-") == 1):
            raise ImportRefused("a customer code reads CUST-XXXX or new")
        if own and customer != own:
            raise ImportRefused("%s is not the customer of %s" % (customer, code))
        cust = customer
    elif own:
        cust = own
    else:
        # no customer: wipe mode with no customer code, the copies go out under the project code
        cust = code
        if review:
            raise ImportRefused("%s has no customer: --review registers codes of a customer, run without it or with "
                                "--customer new" % code)
    if cust != code and _active_forms(p, cust) == 0:
        if (p.outbox / cust).is_dir():
            # issued without a form (awb start --customer new, an empty first line): nothing to ask again; the
            # other customers' forms and the secret checks still run
            _say("%s was issued without a form: the copies are checked against the forms of the register" % cust)
        else:
            ask_forms(p, cust, read=read, tty=tty, allow_none=own is None)
    if review and not (tty if tty is not None else (sys.stdin.isatty() and sys.stdout.isatty())) and edit is None:
        raise ImportRefused("--review opens an editor: run it in your own terminal")

    from awb import importauto

    with importauto.project_lock(p, code) as got:
        if not got:
            raise ImportRefused("an import of %s is running (awb import --auto or another terminal): try again when "
                                "it ends" % code)
        return _take(p, code, files, cust, review=review, redo=redo, edit=edit, confirm=confirm, trusted=trusted)


def _take(p: config.Paths, code: str, files: list[Path], cust: str, *, review: bool, redo: bool, edit, confirm,
          trusted: bool) -> int:
    """The files of the import (the arguments, the bucket, the vault inbox or the last import) through the intake,
    under the import lock of the project."""
    from awb import rulesync

    tmp: Path | None = None
    mapping = None
    try:
        if redo:
            if files:
                raise ImportRefused("--redo takes the files of the last import, give no FILE")
            last = _last_import(p, cust)
            if not last:
                raise ImportRefused("%s has no import to redo" % cust)
            config.make_dir(p.vault / "tmp", 0o700)
            tmp = Path(tempfile.mkdtemp(prefix="redo-", dir=p.vault / "tmp"))
            mapping = {}
            for fid, original in last:
                mapping[_copy_out(p, original, tmp)] = (fid, original)
            files = list(mapping)
            where = "the vault (the last import of %s)" % cust
        elif files:
            where = "the command line"
        else:
            pulled = _from_bucket(p, code)
            if pulled:
                files, where = pulled, "the bucket folder in/ of %s" % code
            else:
                files, where = intake.inbox_files(p), "the vault inbox"
        if not files:
            _say("%s: no new file in the bucket folder in/ and none in the vault inbox" % code)
            return EXIT_OK
        _say("%s: %d file(s) from %s" % (code, len(files), where))
        if trusted:
            _say("trusted: no name is guessed; registered forms, secrets, mail addresses and phones are still wiped")
        res = intake.run(files, cust, p, review=review, edit=edit, confirm=confirm, redo=mapping, trusted=trusted,
                         progress=_progress)
    finally:
        if tmp is not None:
            shutil.rmtree(tmp, ignore_errors=True)
    copies = [o for o in res.outputs if o.name != "intake-report.md"]
    _say("files taken: %d" % len(res.states))
    _say("copies written: %d%s" % (len(copies), " (replacing the earlier ones)" if redo else ""))
    _say("wiped: %s" % intake.wiped_text(res.wiped))
    _say("pictures held: %d" % res.pictures)
    _say("files withheld: %d" % len(res.held))
    if res.held:
        # the final check withheld files: the count goes to the learning (awb/fplearn.py), the strings stay in the
        # private report of the intake
        from awb import fplearn

        fplearn.record("import", extra={"withheld": len(res.held)}, p=p)
    if copies:
        rulesync.post_input(p, res.customer, code, [o.stem for o in copies], replaced=redo)
    return EXIT_HELD if res.held or res.unsealed else EXIT_OK


def read_stdin(stdin=None) -> bytes:
    """The bytes on standard input. From a terminal the text is read to Ctrl-D with the echo off, so that the
    terminal does not print it back; the hint goes to standard error."""
    stdin = stdin if stdin is not None else sys.stdin
    raw = getattr(stdin, "buffer", None)
    tty = getattr(stdin, "isatty", lambda: False)()
    saved = None
    if tty:
        print("paste the text now, then end with Ctrl-D (nothing is shown)", file=sys.stderr)
        try:
            import termios

            saved = termios.tcgetattr(stdin.fileno())
            quiet = termios.tcgetattr(stdin.fileno())
            quiet[3] &= ~termios.ECHO
            termios.tcsetattr(stdin.fileno(), termios.TCSADRAIN, quiet)
        except (ImportError, OSError, ValueError):
            saved = None
    try:
        data = (raw or stdin).read()
    finally:
        if saved is not None:
            import termios

            termios.tcsetattr(stdin.fileno(), termios.TCSADRAIN, saved)
    return data if isinstance(data, bytes) else data.encode("utf-8", "surrogatepass")


def run_paste(p: config.Paths, code: str, kind: str | None = None, *, stdin=None, today=None) -> int:
    """`awb import CODE --paste`: the text on standard input through the intake of the project's customer. The checks
    of who may run it and of the project come before the text is read."""
    from awb import pastein, projects, rulesync, vault

    if config.is_work_user() or vault.in_assistant_session():
        raise ImportRefused(paste_hold_line(code))
    if not codes.is_project_code(code or ""):
        raise ImportRefused("a project code reads tcp-xxxx")
    project = next((pr for pr in projects.load(p) if pr.code == code), None)
    if project is None or project.state != "active":
        raise ImportRefused("%s is not an active project" % code)
    own = project.customer if project.customer != projects.NO_CUSTOMER else None
    cust = own or code
    if own and _active_forms(p, own) == 0:
        raise ImportRefused("%s has no form in the register: run awb import %s once, it asks for the written forms, "
                            "before a paste" % (own, code))
    try:
        text = pastein.read_text(read_stdin(stdin))
        res = pastein.run(p, cust, text, kind, input_dir=Path(project.path) / "input", today=today)
    except pastein.PasteHeld as err:
        print("awb import: %s" % err, file=sys.stderr)
        return EXIT_HELD
    except pastein.PasteRefused as err:
        raise ImportRefused(str(err)) from None
    (copy,) = res.outputs
    _say("%s: paste of kind %s, %d lines" % (code, kind or "text", text.count("\n") + (not text.endswith("\n"))))
    _say("copies written: 1 (%s)" % copy.name)
    _say("wiped: %s" % intake.wiped_text(res.wiped))
    rulesync.post_input(p, res.customer, code, [copy.name])
    return EXIT_HELD if res.unsealed else EXIT_OK


def main(argv: list[str] | None = None) -> int:
    from awb import pastein
    from awb.cli import SafeParser

    ap = SafeParser(prog="awb import", description="Customer material into a project in one command (owner side).")
    ap.add_argument("code", metavar="CODE", nargs="?", default=None, help="the project code, tcp-xxxx (none with --auto)")
    ap.add_argument("files", nargs="*", metavar="FILE", help="files to take in (default: the bucket folder in/, then "
                                                               "the vault inbox)")
    ap.add_argument("--customer", default=None, metavar="CUST-XXXX|new",
                    help="only when a project without a customer gets one: new issues CUST- and the project's four "
                         "characters; without it such a project runs with no customer code")
    ap.add_argument("--review", action="store_true", help="stop once on the candidates and review them in an editor")
    ap.add_argument("--trusted", action="store_true", help="the owner vouches that the files hold no name: no name "
                    "is guessed; the registered forms, the secret check, mail addresses and phones still apply")
    ap.add_argument("--auto", action="store_true", help="the owner's job of the timer awb-import-auto: the new "
                    "files of in/ of every active project, the clean copies of awb clean with --trusted, the rest in "
                    "wipe mode; the originals into the vault and out of the bucket, one log line per run")
    ap.add_argument("--redo", action="store_true", help="run the originals of the last import again with the keep "
                                                         "list of now, the copies replace the earlier ones")
    ap.add_argument("--paste", action="store_true",
                    help="take pasted text from standard input instead of files: pbpaste | awb import CODE --paste "
                         "(wl-paste or xclip -o -selection clipboard on Linux) or paste into the terminal and end "
                         "with Ctrl-D; a secret refuses the whole paste")
    ap.add_argument("--kind", choices=pastein.KINDS, default=None,
                    help="with --paste: a mail (signature and quoted header wiped as a whole), a meeting transcript "
                         "(speakers as [person 1], timestamps kept, filler dropped) or a table copied from a "
                         "spreadsheet (tab-separated, people and mail columns wiped as a whole); without it plain text")
    try:
        # intermixed: a FILE after an option (`CODE --paste FILE`) is a FILE on Python 3.12 too, so the paste can
        # refuse it with its own line instead of the parser's "unrecognized arguments"
        args = ap.parse_intermixed_args(argv)
    except SystemExit as exc:
        return 0 if exc.code in (0, None) else EXIT_ERROR
    from awb import projects, vault

    if args.auto:
        if args.code or args.files or args.customer or args.review or args.redo or args.paste or args.trusted:
            print("awb import: --auto takes no CODE, FILE or other option", file=sys.stderr)
            return EXIT_ERROR
        from awb import importauto

        return importauto.run(config.paths())
    if not args.code:
        print("awb import: a project code reads tcp-xxxx", file=sys.stderr)
        return EXIT_ERROR
    if args.kind and not args.paste:
        print("awb import: --kind goes with --paste", file=sys.stderr)
        return EXIT_ERROR
    if args.paste and (args.files or args.customer or args.review or args.redo):
        print("awb import: --paste takes no FILE, --customer, --review or --redo", file=sys.stderr)
        return EXIT_ERROR
    try:
        if args.paste:
            return run_paste(config.paths(), args.code, args.kind)
        return run(config.paths(), args.code, [Path(f) for f in args.files], customer=args.customer,
                   review=args.review, redo=args.redo, trusted=args.trusted)
    except (ImportRefused, intake.IntakeError, register.RegisterError, projects.ProjectError, vault.VaultError) as err:
        text = str(err)
        print(text if text.startswith("held: ") else "awb import: %s" % text, file=sys.stderr)
        return EXIT_ERROR
    except OSError as err:
        print("awb import: %s (operating system error)" % type(err).__name__, file=sys.stderr)
        return EXIT_ERROR


if __name__ == "__main__":
    sys.exit(main())
