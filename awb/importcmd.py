"""`awb import CODE [FILE...]`: customer material into a project in one command (owner side, T4).

    awb import CODE [FILE...] [--customer CUST-XXXX|new] [--review] [--redo]

The one door of wipe mode. It takes the FILE arguments, else every new file of the project's in/ folder in the
owner bucket, else every file of the vault inbox, and says how many from where. It runs the intake for the
project's customer: the copies carry the customer as its code and every other name the candidate rules recognise as
a token, nothing stops and no editor opens. It prints the files taken, the copies written, the values wiped per
class, the pictures held and the files withheld, and leaves a notice for the sessions of the project (rulesync).

- The customer is the project's. A project without one names it: `--customer CUST-XXXX`, or `--customer new`, which
  issues CUST- and the four characters of the project code (a random code when that one is taken or unsafe).
- A customer without an active form in the register is asked for its written forms on the terminal, one per line,
  an empty line ends; they are registered before the intake runs. Nothing is echoed.
- `--review` stops once on the candidates and opens the review of `awb register review` (the marks register the
  names, the rest goes to the keep list), then the run goes on in wipe mode.
- `--redo` takes the originals of the customer's last import out of the vault and runs them again under their file
  ids with the keep list of now; the copies replace the earlier ones in the outbox.

Refused for the work user and inside an assistant session: the forms prompt and the review carry names. Nothing here
prints a name, a form or a file name.
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


def project_customer_code(code: str) -> str:
    """CUST- and the four characters of the project code (tcp-q7m4 -> CUST-Q7M4)."""
    return "CUST-" + code.split("-", 1)[1].upper()


def new_customer(p: config.Paths, code: str) -> tuple[str, bool]:
    """The code of a new customer for project `code`: the project's four characters when that code is free and
    safe (no register form in it, no structured shape, never issued), else a random safe code. Returns (code, the
    project's own)."""
    entries = register.load(p.register)
    wanted = project_customer_code(code)
    issued = intake.issued_codes(p) | set(register.codes(entries))
    engine = intake._Engine(entries)
    if codes.is_code(wanted) and wanted not in issued and engine.safe(wanted):
        return wanted, True
    return intake.safe_new_code(entries, "CUST", issued), False


def _active_forms(p: config.Paths, customer: str) -> int:
    return sum(1 for e in register.load(p.register) if e.code == customer and e.status == "active")


def ask_forms(p: config.Paths, customer: str, read=None, tty=None) -> int:
    """The written forms of a customer the register does not know yet, typed on the terminal, one per line, an empty
    line ends. Registered one by one; returns how many. Refused without a terminal."""
    if not (tty if tty is not None else (sys.stdin.isatty() and sys.stdout.isatty())):
        raise ImportRefused("%s has no form in the register: run awb import in your own terminal, it asks for the "
                           "written forms" % customer)
    read = read or (lambda: sys.stdin.readline())
    _say("%s has no form in the register yet. Type its written forms, one per line (the full name, the short "
         "name, an acronym ...); an empty line ends." % customer)
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
    if not added:
        raise ImportRefused("no form was typed: nothing was imported")
    config.make_dir(p.outbox / customer, 0o750, shared=True)
    _say("registered %d form(s) of %s" % (added, customer))
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
        redo: bool = False, read=None, tty=None, edit=None, confirm=None) -> int:
    from awb import projects, rulesync, vault

    if config.is_work_user():
        raise ImportRefused("awb import is the owner's: run it in your own terminal")
    if vault.in_assistant_session():
        raise ImportRefused("awb import never runs inside an assistant session: run it in your own terminal")
    if not codes.is_project_code(code or ""):
        raise ImportRefused("a project code reads tcp-xxxx")
    project = next((pr for pr in projects.load(p) if pr.code == code), None)
    if project is None or project.state != "active":
        raise ImportRefused("%s is not an active project" % code)
    own = project.customer if project.customer != projects.NO_CUSTOMER else None
    if customer == "new":
        if own:
            raise ImportRefused("%s has the customer %s already" % (code, own))
        cust, from_project = new_customer(p, code)
        _say("new customer %s%s" % (cust, "" if from_project else " (the project's own code was taken, a random one "
                                                                  "was issued)"))
    elif customer:
        if not (codes.is_code(customer) and codes.kind_of(customer) == "CUST" and customer.count("-") == 1):
            raise ImportRefused("a customer code reads CUST-XXXX or new")
        if own and customer != own:
            raise ImportRefused("%s is not the customer of %s" % (customer, code))
        cust = customer
    elif own:
        cust = own
    else:
        raise ImportRefused("%s has no customer: name it with --customer CUST-XXXX or --customer new" % code)
    if _active_forms(p, cust) == 0:
        ask_forms(p, cust, read=read, tty=tty)
    if review and not (tty if tty is not None else (sys.stdin.isatty() and sys.stdout.isatty())) and edit is None:
        raise ImportRefused("--review opens an editor: run it in your own terminal")

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
        res = intake.run(files, cust, p, review=review, edit=edit, confirm=confirm, redo=mapping)
    finally:
        if tmp is not None:
            shutil.rmtree(tmp, ignore_errors=True)
    copies = [o for o in res.outputs if o.name != "intake-report.md"]
    _say("files taken: %d" % len(res.states))
    _say("copies written: %d%s" % (len(copies), " (replacing the earlier ones)" if redo else ""))
    _say("wiped: %s" % intake.wiped_text(res.wiped))
    _say("pictures held: %d" % res.pictures)
    _say("files withheld: %d" % len(res.held))
    if copies:
        rulesync.post_input(p, res.customer, code, [o.stem for o in copies], replaced=redo)
    return EXIT_HELD if res.held or res.unsealed else EXIT_OK


def main(argv: list[str] | None = None) -> int:
    from awb.cli import SafeParser

    ap = SafeParser(prog="awb import", description="Customer material into a project in one command (owner side).")
    ap.add_argument("code", metavar="CODE", help="the project code, tcp-xxxx")
    ap.add_argument("files", nargs="*", metavar="FILE", help="files to take in (default: the bucket folder in/, then "
                                                               "the vault inbox)")
    ap.add_argument("--customer", default=None, metavar="CUST-XXXX|new",
                    help="the customer of a project without one; new issues CUST- and the project's four characters")
    ap.add_argument("--review", action="store_true", help="stop once on the candidates and review them in an editor")
    ap.add_argument("--redo", action="store_true", help="run the originals of the last import again with the keep "
                                                         "list of now, the copies replace the earlier ones")
    try:
        args = ap.parse_args(argv)
    except SystemExit as exc:
        return 0 if exc.code in (0, None) else EXIT_ERROR
    from awb import projects, vault

    try:
        return run(config.paths(), args.code, [Path(f) for f in args.files], customer=args.customer,
                   review=args.review, redo=args.redo)
    except (ImportRefused, intake.IntakeError, register.RegisterError, projects.ProjectError, vault.VaultError) as err:
        print("awb import: %s" % err, file=sys.stderr)
        return EXIT_ERROR
    except OSError as err:
        print("awb import: %s (operating system error)" % type(err).__name__, file=sys.stderr)
        return EXIT_ERROR


if __name__ == "__main__":
    sys.exit(main())
