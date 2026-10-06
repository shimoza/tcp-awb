"""The `awb` command.

    awb init
    awb intake --customer CODE|new [--force] [FILE...]      (no FILE: every file in the vault inbox)
    awb register add CODE KIND FORM                          (FORM "-" reads the form from standard input)
    awb register new KIND [--parent CODE]
    awb register list [--forms]
    awb register retire CODE
    awb register keep PHRASE                                 (PHRASE "-" reads the phrase from standard input)
    awb register keep --list                                 (vault side: prints the kept phrases)
    awb check [--register PATH | --no-register] FILE...
    awb gate [--staged] [--pushed [--remote NAME]] [--message FILE] [--selftest] [--register PATH | --no-register]
             [--repo PATH] [PATH...]
    awb gate --install [--kb] [--force] [--repo PATH]
    awb spawn KIND --goal TEXT [--customer CODE] [--tag T]... [--from-outbox]
    awb close CODE [--force]
    awb projects list|check|sync

Commands that live in their own module (DELEGATED below; `awb NAME --help` shows the options of each):

    awb vault serve|unlock|lock|status|encrypt              awb/vault.py
    awb kb add|amend|retire|find|show|scope|expired|index|verify    awb/kb.py
    awb ledger add|list, awb report --by KEY                 awb/ledger.py
    awb career add|update|due                                awb/career.py
    awb write check FILE, awb voice learn DRAFT SENT         awb/writing.py
    awb review init|claims|l0|pass|status|calibrate          awb/review.py
    awb calc EXPRESSION, awb calc list|show                  awb/calc.py
    awb migrate init|status|next|inventory|map|estimate|done awb/tcp/migrate.py
    awb keys serve|unlock|lock|status                        awb/tcp/keys.py
    awb inbox take|list, awb xchg put|list                   awb/tcp/xchg.py
    awb images list|release                                  awb/images.py
    awb hook prompt|pre-write|post-write|stop|session-start  awb/hooks.py
    awb seal check                                           awb/seal.py
    awb bucket sync|folder|pull|put|move                     awb/bucket.py   (owner side)
    awb reveal FILE --out PATH                               awb/reveal.py   (owner side)
    awb price find|snapshot|diff|check                       awb/tcp/price.py
    awb cloud projects|get                                   awb/tcp/cloud.py   (owner side, read only)
    awb mirror docs|sd|status                                awb/tcp/mirror.py  (owner side updates)
    awb refresh [--part P]..., awb refresh apply RESULTS     awb/tcp/refresh.py
    awb english list [--month YYYY-MM]                       awb/english.py
    awb portal serve [--port N]                              awb/tcp/portal.py
    awb tenant add|snapshot|list|now|history|at|project      awb/tcp/tenants.py
    awb ask serve [--port N] | awb ask QUESTION              awb/tcp/ask.py
    awb api write|check                                      awb/tcp/web/contract.py
    awb service check|list|update                            awb/tcp/services.py
    awb board write [--out DIR] | show                       awb/tcp/board.py

Exit codes: 0 ok, 1 findings or blocked, 2 usage or error.

Only `awb register list --forms` prints a written form and only `awb register keep --list` prints a kept phrase:
both are the vault side. `awb register keep PHRASE` takes any phrase and prints counts only. Every other command
prints codes, classes, counts, states and paths made of codes. Error messages of the Workbench modules carry no value by
design. An operating system error is printed by its class only, because its text can carry a file name. A usage
error never repeats an argument. `awb register retire` names its argument only when it is a code. `awb check`
and `awb gate` print a path that carries a registered form as "file N" or with [name] in its place. They refuse to
run without a register unless --no-register is given.
"""
from __future__ import annotations

import argparse
import importlib
import re
import sys
from collections import Counter
from pathlib import Path

from awb import codes, config, register

EXIT_OK = 0
EXIT_FINDINGS = 1
EXIT_ERROR = 2


class SafeParser(argparse.ArgumentParser):
    """argparse with usage errors that never echo an argument value (awb check and awb gate use it too)."""

    def error(self, message: str):  # noqa: D401 - argparse hook
        self.print_usage(sys.stderr)
        if message.startswith("unrecognized arguments"):
            message = "unrecognized arguments"
        message = re.sub(r"'[^']*'", "'...'", message)
        print("%s: error: %s" % (self.prog, message), file=sys.stderr)
        raise SystemExit(EXIT_ERROR)


_Parser = SafeParser


def _err(msg: str) -> int:
    print("awb: %s" % msg, file=sys.stderr)
    return EXIT_ERROR


def _exit_code(exc: SystemExit) -> int:
    if exc.code is None:
        return EXIT_OK
    if isinstance(exc.code, int):
        return exc.code
    return EXIT_ERROR


# --------------------------------------------------------------------------- commands


def _cmd_init(args, p: config.Paths) -> int:
    config.ensure_layout(p)
    encrypted = p.register_encrypted.exists()
    if not p.register.exists() and not encrypted:
        register.save(p.register, [])
    print("shared: %s" % p.shared)
    print("vault: %s" % p.vault)
    print("projects: %s" % p.projects_root)
    try:
        entries = register.load(p.register)
    except register.RegisterError:
        if not encrypted:
            raise
        # an encrypted register is held by the vault daemon; init leaves it as it is
        print("register: encrypted, the vault daemon cannot read it now (locked or not running)")
        return EXIT_OK
    print("register:%s %d forms of %d codes" % (" encrypted," if encrypted else "", len(entries),
                                               len(register.codes(entries))))
    return EXIT_OK


def _cmd_intake(args, p: config.Paths) -> int:
    from awb import intake

    files = [Path(f) for f in args.files] if args.files else intake.inbox_files(p)
    if not files:
        return _err("intake: no files given and the inbox is empty")
    res = intake.run(files, args.customer, p, force=args.force)
    if res.unsealed:
        # the run is done; what could not be sealed stays inside the vault in plaintext
        print("awb: intake %s: %d files of this run are not sealed in the vault, see the private report"
              % (res.customer, res.unsealed), file=sys.stderr)
    if res.blocked:
        print("intake %s blocked: %d candidates, nothing was written to the outbox" % (res.customer, res.candidates))
        print("private report (vault side): %s" % res.private_report)
        print("sort them in one pass in your own terminal: awb register review %s (mark the real names, the rest "
              "passes), then run again with --customer %s" % (res.customer, res.customer))
        return EXIT_ERROR if res.unsealed else EXIT_FINDINGS
    states = Counter(res.states.values())
    print("intake %s: %d files, %d outputs, %d candidates reviewed"
          % (res.customer, len(res.states), len(res.outputs), res.candidates))
    print("states: %s" % ", ".join("%s %d" % kv for kv in sorted(states.items())))
    print("outbox: %s" % res.public_report.parent)
    print("public report: %s" % res.public_report)
    print("private report (vault side): %s" % res.private_report)
    if res.unsealed:
        return EXIT_ERROR
    return EXIT_FINDINGS if states.get("failed") else EXIT_OK


def _cmd_register_add(args, p: config.Paths) -> int:
    form = args.form
    if form == "-":
        form = sys.stdin.readline().rstrip("\r\n")
    register.add(p.register, args.code, args.kind, form)
    active = sum(1 for e in register.load(p.register) if e.code == args.code and e.status == "active")
    print("added one form to %s (%d active)" % (args.code, active))
    if args.kind == "CUST" and args.code.count("-") == 1:
        # the work side cannot read the register: its outbox folder is how awb spawn knows the code was issued
        try:
            config.make_dir(p.outbox / args.code, 0o750, shared=True)
        except OSError:
            print("register add: the outbox folder of %s could not be made; awb spawn as the work user will "
                  "refuse the code until an intake ran" % args.code, file=sys.stderr)
    return EXIT_OK


def _cmd_register_new(args, p: config.Paths) -> int:
    from awb import intake

    if args.kind not in codes.ENTITY_KINDS:
        return _err("register new: kind must be one of %s" % ", ".join(codes.ENTITY_KINDS))
    entries = register.load(p.register)
    if args.parent:
        if not codes.is_code(args.parent) or args.parent.count("-") != 1:
            return _err("register new: the parent must be a top-level code")
        if args.parent not in register.codes(entries):
            return _err("register new: %s is not in the register" % args.parent)
        print(register.next_sub_code(entries, args.parent, args.kind))
    else:
        print(intake.safe_new_code(entries, args.kind, intake.issued_codes(p)))
    return EXIT_OK


def _cmd_register_list(args, p: config.Paths) -> int:
    entries = register.load(p.register)
    by_code: dict[str, list[register.Entry]] = {}
    for e in entries:
        by_code.setdefault(e.code, []).append(e)
    if not by_code:
        print("the register is empty")
        return EXIT_OK
    width = max(len(c) for c in by_code)
    print("%-*s  %-4s  %6s  %7s" % (width, "code", "kind", "active", "retired"))
    for code in sorted(by_code):
        rows = by_code[code]
        active = sum(1 for e in rows if e.status == "active")
        print("%-*s  %-4s  %6d  %7d" % (width, code, rows[0].kind, active, len(rows) - active))
        if args.forms:
            for e in rows:
                print("%*s  %-7s  %s" % (width, "", e.status, e.form))
    return EXIT_OK


def _cmd_register_retire(args, p: config.Paths) -> int:
    if not codes.is_code(args.code):
        # the argument may be a form typed by mistake: it is never echoed
        return _err("register retire: the argument is not a code")
    entries = register.load(p.register)
    if args.code not in register.codes(entries):
        return _err("register retire: %s is not in the register" % args.code)
    n = register.retire(p.register, args.code)
    print("retired %d forms of %s" % (n, args.code))
    return EXIT_OK


def _cmd_register_keep(args, p: config.Paths) -> int:
    """Add a reviewed phrase to the keep list or print the list (vault side). The phrase is never echoed."""
    from awb import intake

    if args.list:
        if args.phrase is not None:
            return _err("register keep: give a phrase or --list, not both")
        try:
            phrases = intake.load_keep(p)
        except OSError:
            return _err("register keep --list: the keep list cannot be read here, "
                        "it is shown on the vault side only")
        for phrase in phrases:
            print(phrase)
        if not phrases:
            print("the keep list is empty")
        return EXIT_OK
    if args.phrase is None:
        return _err("register keep: give a phrase (or - to read it from standard input) or --list")
    phrase = args.phrase
    if phrase == "-":
        phrase = sys.stdin.readline().rstrip("\r\n")
    added, count = intake.keep_phrase(p, phrase)
    if added:
        print("kept one phrase, %d in the keep list" % count)
    else:
        print("the phrase was kept before, %d in the keep list" % count)
    return EXIT_OK


def _cmd_register_review(args, p: config.Paths) -> int:
    from awb import candidates

    return candidates.command(args, p)


def _cmd_spawn(args, p: config.Paths) -> int:
    from awb import ledger, projects

    pr = projects.spawn(p, args.kind, args.goal, args.customer, p.register, args.tag or [],
                        from_outbox=args.from_outbox)
    print("spawned %s (%s, customer %s): %s" % (pr.code, pr.kind, pr.customer, pr.path))
    try:
        projects.record(p, pr, "Started %s (a %s): %s" % (pr.code, pr.kind, " ".join(args.goal.split())),
                        tags=args.tag or [])
    except ledger.LedgerError as err:
        print("awb spawn: the first ledger entry was not written (%s); add it with awb ledger add" % err,
              file=sys.stderr)
    return EXIT_OK


def _exchange_leftovers(code: str, folder: Path | None) -> str | None:
    """Why a project cannot close because of files in its exchange folders of the lab bucket, or None. The check
    lives in the module of the delegated command xchg, so the core reaches it the way it reaches that command."""
    try:
        xchg = importlib.import_module("awb.%s" % DELEGATED["xchg"][0])
    except ImportError:
        return None
    n = xchg.leftovers(code)
    if n is None:
        print("awb close: the exchange folders could not be checked (key service); check them in the console",
              file=sys.stderr)
        return None
    if not n:
        return None
    try:
        res = (folder / "RESOURCES.md").read_text(encoding="utf-8") if folder else ""
    except OSError:
        res = ""
    if any("%s/" % code in line and "| kept |" in line for line in res.splitlines()):
        return None
    return ("%s still holds %d file(s) in its in/ and from-session/ folders of the lab bucket; fetch and delete "
            "them, mark them kept in RESOURCES.md or close with --force" % (code, n))


def _cmd_close(args, p: config.Paths) -> int:
    from awb import ledger, projects

    before = {r.code: r for r in projects.load(p)}.get(args.code)
    if not args.force and before is not None and before.state == "active":
        why = _exchange_leftovers(args.code, Path(before.path))
        if why:
            return _err(why)
    items = projects.open_items(before.path) if before else 0
    live = projects.live_resources(before.path) if before else 0
    pr = projects.close(p, args.code, force=args.force)
    if before is not None and before.state == "closed":
        print("%s was closed already" % pr.code)
        return EXIT_OK
    left = " with %d open item(s) and %d live resource(s) left" % (items, live) if (items or live) else ""
    print("closed %s%s" % (pr.code, left))
    try:
        projects.record(p, pr, "Closed %s%s" % (pr.code, left))
    except ledger.LedgerError as err:
        print("awb close: the ledger entry was not written (%s)" % err, file=sys.stderr)
    return EXIT_OK


def _cmd_projects_list(args, p: config.Paths) -> int:
    from awb import ledger, projects, sessions

    rows = [r for r in projects.load(p) if args.all or r.state != "deleted"]
    if not rows:
        print("no projects")
        return EXIT_OK
    goals = [projects.goal_of(r.path) for r in rows]
    # a goal passed the name check at spawn; the register grows, so it is checked again before it is shown
    try:
        hidden = ledger.hit_indexes(goals, ledger.register_path(p))
        unchecked = False
    except ledger.LedgerError:
        hidden, unchecked = set(), True
    last = "path" if args.paths else "goal"
    print("code      kind        customer   state   created     owner     %s" % last)
    for i, r in enumerate(rows):
        held = sessions.owner(p, r.code) if r.state == "active" else None
        if args.paths:
            tail = r.path
        elif unchecked:
            tail = "(goal not shown: the name check is unavailable)"
        elif i in hidden:
            tail = ledger.WITHHELD
        else:
            tail = projects.short_goal(goals[i])
        print("%-9s %-11s %-10s %-7s %-11s %-9s %s" % (r.code, r.kind, r.customer, r.state, r.created,
                                                     sessions.short(held["session"]) if held else "-", tail))
    return EXIT_OK


def _cmd_projects_kind(args, p: config.Paths) -> int:
    from awb import projects

    pr, before = projects.set_kind(p, args.code, args.kind)
    if before == pr.kind:
        print("%s is a %s already" % (pr.code, pr.kind))
        return EXIT_OK
    print("%s is now a %s (was %s)%s" % (pr.code, pr.kind, before,
                                       ": it reaches the test tenants" if projects.tenant_access(pr.kind)
                                       else ": no test tenant"))
    try:
        projects.record(p, pr, "Switched %s from %s to %s" % (pr.code, before, pr.kind))
    except Exception:
        print("awb projects: the ledger entry was not written", file=sys.stderr)
    return EXIT_OK


def _cmd_projects_delete(args, p: config.Paths) -> int:
    from awb import projects, sessions

    failed = 0
    for code in args.codes:
        try:
            before = {r.code: r for r in projects.load(p)}.get(code)
            pr = projects.delete(p, code, holder=lambda c: sessions.owner(p, c) is not None)
            if before is not None and before.state == "deleted":
                print("%s was deleted already" % pr.code)
                continue
            print("deleted %s: the folder is gone, the code stays registered" % pr.code)
            try:
                projects.record(p, pr, "Deleted %s" % pr.code)
            except Exception:
                print("awb projects: the ledger entry for %s was not written" % pr.code, file=sys.stderr)
        except projects.ProjectError as err:
            failed += 1
            print("awb projects: %s" % err, file=sys.stderr)
    return EXIT_FINDINGS if failed else EXIT_OK


def _cmd_projects_sync(args, p: config.Paths) -> int:
    from awb import rulesync

    done = rulesync.sync(p, args.codes or None)
    if not done:
        print("no active project%s" % (" of these codes" if args.codes else ""))
        return EXIT_FINDINGS if args.codes else EXIT_OK
    for code, files in done:
        print("%s: %s" % (code, ("brought up to date: " + ", ".join(files)) if files else "already current"))
    return EXIT_OK


def _cmd_projects_check(args, p: config.Paths) -> int:
    from awb import projects

    found = projects.unregistered(p)
    if not found:
        print("every tcp- folder under %s is a registered project" % p.projects_root)
        return EXIT_OK
    print("%d tcp- folder(s) that no project registered: %s" % (len(found), ", ".join(found)))
    return EXIT_FINDINGS


# --------------------------------------------------------------------------- parser


def _build() -> argparse.ArgumentParser:
    ap = _Parser(prog="awb", description="Architect Workbench")
    sub = ap.add_subparsers(dest="command", parser_class=_Parser)

    s = sub.add_parser("init", help="create the shared side, the vault side and an empty register")
    s.set_defaults(func=_cmd_init)

    s = sub.add_parser("intake", help="take customer material in: sanitised copies to the outbox")
    s.add_argument("--customer", required=True, metavar="CODE|new", help="a CUST code or new")
    s.add_argument("--force", action="store_true", help="write outputs even when candidates were found")
    s.add_argument("files", nargs="*", metavar="FILE", help="files to take in (default: the vault inbox)")
    s.set_defaults(func=_cmd_intake)

    reg = sub.add_parser("register", help="the register of written forms (vault side)")
    rsub = reg.add_subparsers(dest="register_command", parser_class=_Parser)
    s = rsub.add_parser("add", help="add one written form to a code")
    s.add_argument("code")
    s.add_argument("kind")
    s.add_argument("form", help="the written form or - to read it from standard input")
    s.set_defaults(func=_cmd_register_add)
    s = rsub.add_parser("new", help="print a fresh code of a kind")
    s.add_argument("kind")
    s.add_argument("--parent", default=None, help="a top-level code for a sub code such as CUST-Q7M4-PERS-2")
    s.set_defaults(func=_cmd_register_new)
    s = rsub.add_parser("list", help="codes, kinds and counts of forms")
    s.add_argument("--forms", action="store_true", help="also print the written forms (vault side only)")
    s.set_defaults(func=_cmd_register_list)
    s = rsub.add_parser("retire", help="retire every form of a code")
    s.add_argument("code")
    s.set_defaults(func=_cmd_register_retire)
    s = rsub.add_parser("keep", help="keep a reviewed phrase out of future candidates (vault side)")
    s.add_argument("phrase", nargs="?", default=None, help="the phrase or - to read it from standard input")
    s.add_argument("--list", action="store_true", help="print the kept phrases (vault side only)")
    s.set_defaults(func=_cmd_register_keep)
    s = rsub.add_parser("review", help="sort the candidates of a blocked intake in one pass in an editor: mark the "
                                       "real names, the rest is kept as not a name (owner's terminal)")
    s.add_argument("customer", help="the CUST code of the blocked intake")
    s.add_argument("--report", default=None, help="a private report other than the latest of the customer")
    s.set_defaults(func=_cmd_register_review)

    for name, (_, text) in DELEGATED.items():
        sub.add_parser(name, help=text, add_help=False)

    s = sub.add_parser("spawn", help="create a sealed tcp- project: a query or a project")
    s.add_argument("kind", help="query (knowledge base, prices, no tenant) or project (also the test tenants)")
    s.add_argument("--goal", required=True)
    s.add_argument("--customer", default=None)
    s.add_argument("--tag", action="append", default=[])
    s.add_argument("--from-outbox", action="store_true", help="move the customer's outbox copies into input/")
    s.set_defaults(func=_cmd_spawn)

    s = sub.add_parser("close", help="close a project: OPEN.md empty, no live resource, a ledger entry")
    s.add_argument("code")
    s.add_argument("--force", action="store_true", help="close although items or resources are left")
    s.set_defaults(func=_cmd_close)

    pr = sub.add_parser("projects", help="the project register")
    psub = pr.add_subparsers(dest="projects_command", parser_class=_Parser)
    s = psub.add_parser("list", help="list the projects with their goal")
    s.add_argument("--paths", action="store_true", help="the folder of each project instead of its goal")
    s.add_argument("--all", action="store_true", help="also the deleted projects")
    s.set_defaults(func=_cmd_projects_list)
    s = psub.add_parser("kind", help="switch a project between query (knowledge only) and project (test tenants)")
    s.add_argument("code")
    s.add_argument("kind", choices=("query", "project"))
    s.set_defaults(func=_cmd_projects_kind)
    s = psub.add_parser("delete", help="remove the folder of closed projects; the code stays registered as deleted")
    s.add_argument("codes", nargs="+", metavar="CODE")
    s.set_defaults(func=_cmd_projects_delete)
    s = psub.add_parser("check", help="find tcp- folders that no project registered")
    s.set_defaults(func=_cmd_projects_check)
    s = psub.add_parser("sync", help="bring the template lines of CLAUDE.md and STATE.md of every active project "
                                     "up to the current rules, every other line kept")
    s.add_argument("codes", nargs="*", metavar="CODE")
    s.set_defaults(func=_cmd_projects_sync)
    return ap


# Commands that live in their own module. Each module has main(argv) -> exit code and parses its own options
# with cli.SafeParser. The module name is the first value, the help line the second.
DELEGATED = {
    "check": ("check", "check files for names and structured data"),
    "gate": ("gate", "the commit gate"),
    "vault": ("vault", "the vault: serve, unlock, lock, status, encrypt"),
    "kb": ("kb", "the knowledge base: add, amend, retire, find, scope, expired, show"),
    "ledger": ("ledger", "the activity ledger: add, list"),
    "report": ("ledger", "reports from the ledger by customer, technology, kind or project"),
    "career": ("career", "the career log: add, update, due"),
    "write": ("writing", "check a text against the house style"),
    "voice": ("writing", "learn from a draft and the version that was sent"),
    "review": ("review", "review a deliverable: init, claims, l0, pass, status"),
    "calc": ("calc", "compute with exact decimals and record it in the project: every total an offer states"),
    "hook": ("hooks", "entry points for the client hooks of working sessions"),
    "images": ("images", "pictures of the intake, held on the vault side: list, release"),
    "seal": ("seal", "checks of the seal between the vault and working sessions"),
    "bucket": ("bucket", "the owner's bucket: sync, folder, pull, put, move"),
    "reveal": ("reveal", "put the real names back into a finished text (owner side)"),
    "price": ("tcp.price", "live prices of T Cloud Public (TCP): find, snapshot, diff, check a sheet"),
    "cloud": ("tcp.cloud", "read-only calls to the TCP API with the owner's key"),
    "mirror": ("tcp.mirror", "public mirrors of TCP: the documentation sources and the service description"),
    "refresh": ("tcp.refresh", "the refresh: mirrors, prices, expired knowledge, projects, defaults; apply results"),
    "english": ("english", "the English notes the sessions wrote: list"),
    "portal": ("tcp.portal", "a read-only web page over the knowledge, the prices, the projects and the reviews"),
    "ask": ("tcp.ask", "questions in plain words, answered only from the checked sources (the Ask page)"),
    "inbox": ("tcp.inbox", "take a file the owner dropped into an inbox into this project"),
    "xchg": ("tcp.xchg", "put a result of this project into the owner's from-session folder"),
    "keys": ("tcp.keys", "the key service: keys, logins and passwords of the test tenants, never shown to a session"),
    "migrate": ("tcp.migrate", "a migration to TCP in phases: inventory, mapping, estimate, plan, review"),
    "tenant": ("tcp.tenants", "resources on the test tenants over time: add, snapshot, list, now, history, at, project"),
    "api": ("tcp.web.contract", "the contract of the web API: write docs/api/openapi.yaml, check it"),
    "service": ("tcp.services", "offered services: the latest service description and nothing else (check, list, update)"),
    "board": ("tcp.board", "the status of every active project for management, as Markdown and HTML"),
}


def _delegate(name: str, argv: list[str]) -> int:
    module, _ = DELEGATED[name]
    try:
        mod = importlib.import_module("awb.%s" % module)
    except ImportError:
        return _err("the command %s is not built yet" % name)
    if name in ("report", "voice"):
        argv = [name] + list(argv)
    try:
        return int(mod.main(argv))
    except SystemExit as exc:
        return _exit_code(exc)


def main(argv: list[str] | None = None) -> int:
    """Run one `awb` command. Returns the exit code."""
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv and argv[0] in DELEGATED:
        return _delegate(argv[0], argv[1:])
    ap = _build()
    try:
        args = ap.parse_args(argv)
    except SystemExit as exc:
        return _exit_code(exc)
    func = getattr(args, "func", None)
    if func is None:
        ap.print_usage(sys.stderr)
        return EXIT_ERROR
    from awb import intake, projects

    try:
        return func(args, config.paths())
    except (register.RegisterError, intake.IntakeError, projects.ProjectError) as err:
        return _err(str(err))
    except OSError as err:
        return _err("%s (operating system error)" % type(err).__name__)


if __name__ == "__main__":
    sys.exit(main())
