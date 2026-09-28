"""The career log: outcomes worth a CV line and the update drafted from them every few months.

    awb career add --title T --role R --stack S --outcome O --cv-line L [--tag T]... [--date D]
    awb career update [--since D]
    awb career due

`<shared>/career.jsonl` holds one JSON object per line with the fields of `FIELDS`, appended under an exclusive
lock like the ledger. A career entry names no customer at all: every text is refused when it carries a register
code (CUST-Q7M4 and every other kind), a project code (tcp-q7m4), a registered name or structured data (the name
check, `check.check_text` with the register of `config.paths()`), a secret, an identifier or a word of the owner's
blocklist. Tags come from `rules/tags.txt`; without --tag they are taken from the words of the stack that are tags.

`awb career update` prints a Markdown draft: CV bullets grouped by theme (each entry goes to the first theme of
`THEMES` that one of its tags belongs to, else to "Other"), newest first inside a theme, then one LinkedIn
paragraph. The draft is written in plain style (no em-dash, no comma before "and" or "or") and then run through
`writing.check_text`; the tells go to standard error and a blocking tell gives exit 1. When `awb.writing` is not
there the step is skipped with a note. The date of the run goes to `<shared>/career-updated`.

`awb career due` prints the days since the last update and exits 1 when that is over 90. Before the first update
it counts from the oldest entry; with no update and no entry nothing is due.

Messages name fields, classes and counts, never a text or a refused value.
"""
from __future__ import annotations

import importlib
import json
import os
import re
import sys
import tempfile
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

from awb import config, ledger

EXIT_OK = 0
EXIT_FINDINGS = 1
EXIT_ERROR = 2

FIELDS = ("date", "title", "role", "stack", "outcome", "cv_line", "tags")
TEXT_FIELDS = ("title", "role", "stack", "outcome", "cv_line")
DUE_DAYS = 90
MAX_TEXT = 600

THEMES: tuple[tuple[str, frozenset[str]], ...] = (
    ("Cloud migration", frozenset(("migration", "dr", "backup", "cbr", "csbs", "ims", "obs", "sfs", "evs",
                                   "storage"))),
    ("Automation", frozenset(("terraform", "ansible", "api"))),
    ("Security", frozenset(("security", "iam", "kms", "waf", "cfw", "identity"))),
    ("AI and data", frozenset(("ai", "modelarts", "llm-gateway", "gpu", "database", "rds", "rds-mssql",
                               "rds-mysql", "rds-pg", "gaussdb", "dds", "dcs"))),
    ("Platform and network", frozenset(("ecs", "bms", "deh", "cce", "cci", "swr", "container", "as", "vpc", "elb",
                                        "nat", "dns", "eip", "vpn", "dc", "er", "network", "windows", "linux",
                                        "vdi", "sap"))),
    ("Operations", frozenset(("monitoring", "logging", "cts", "ces", "lts", "aom"))),
    ("Customer-facing", frozenset(("pricing", "tender", "ms-licensing"))),
)
OTHER = "Other"

_HINT = "; a career entry names no customer, project or person"
_STACK_SPLIT_RE = re.compile(r"[,;/]|\band\b|\bor\b", re.IGNORECASE)


class CareerError(Exception):
    """A usage or data problem. The message never carries a text or a refused value."""


class Refused(CareerError):
    """A text carries a code, a name, structured data, a secret or the old grammar. Exit 1."""


@dataclass
class Entry:
    date: str
    title: str
    role: str
    stack: str
    outcome: str
    cv_line: str
    tags: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {k: (list(getattr(self, k)) if k == "tags" else getattr(self, k)) for k in FIELDS}


@dataclass
class Update:
    draft: str
    entries: int
    tells: list = field(default_factory=list)
    metrics: dict = field(default_factory=dict)
    note: str | None = None
    withheld: int = 0
    candidates: str = ""           # ledger work that may be worth a career line, codes masked (T-46)

    @property
    def blocking(self) -> int:
        return sum(1 for t in self.tells if _tell(t)[2])


def today() -> date:
    """The date of today. Tests replace it."""
    return date.today()


def log_file(p: config.Paths) -> Path:
    return p.shared / "career.jsonl"


def stamp_file(p: config.Paths) -> Path:
    return p.shared / "career-updated"


def theme_of(tags) -> str:
    for name, members in THEMES:
        if any(t in members for t in tags):
            return name
    return OTHER


def tags_from_stack(stack: str, known: list[str]) -> list[str]:
    """The words of the stack that are tags, in their order."""
    out: list[str] = []
    word = []
    for c in stack.lower() + " ":
        if c.isalnum() or c in "-+.":
            word.append(c)
            continue
        w = "".join(word).strip(".")
        word = []
        if w in known and w not in out:
            out.append(w)
    return out


# --------------------------------------------------------------------------- entries


def _wrap(fn, *args, **kw):
    try:
        return fn(*args, **kw)
    except ledger.Refused as err:
        raise Refused(str(err)) from None
    except ledger.LedgerError as err:
        raise CareerError(str(err)) from None


def make_entry(*, title: str, role: str, stack: str, outcome: str, cv_line: str, tags=(), day=None,
               known_tags: list[str] | None = None) -> Entry:
    """A validated entry. Shapes only: the texts are checked by `check_entry`."""
    values = {}
    for name, value in (("title", title), ("role", role), ("stack", stack), ("outcome", outcome),
                        ("cv_line", cv_line)):
        values[name] = _wrap(ledger.clean_text, value, name.replace("_", "-"), required=True, limit=MAX_TEXT)
    tags = _wrap(ledger.clean_tags, tags, known_tags)
    if not tags and known_tags is not None:
        tags = tags_from_stack(values["stack"], known_tags)
    if day is None:
        day = today()
    try:
        d = ledger.parse_date(day)
    except ValueError as err:
        raise CareerError("date: %s" % err) from None
    return Entry(date=d.isoformat(), tags=tags, **values)


def check_entry(e: Entry, reg: Path | None) -> None:
    """Refuse a register code, a project code, a name, structured data, a secret, an identifier or a
    blocklist word in any text of the entry."""
    if reg is not None:
        _wrap(ledger.ensure_checkable, reg)
    fields = [(name.replace("_", "-"), getattr(e, name)) for name in TEXT_FIELDS]
    _wrap(ledger.screen_fields, fields, reg, codes_too=True, refused=ledger.Refused, hint=_HINT)


def add(title: str, role: str, stack: str, outcome: str, cv_line: str, *, tags=(), day=None,
        p: config.Paths | None = None) -> Entry:
    """Validate, check and append one career entry. Raises CareerError. Raises Refused on a hit."""
    p = p or config.paths()
    e = make_entry(title=title, role=role, stack=stack, outcome=outcome, cv_line=cv_line, tags=tags, day=day,
                   known_tags=_wrap(ledger.load_tags))
    check_entry(e, ledger.register_path(p))
    ledger.append_line(log_file(p), e.as_dict())
    return e


def load(p: config.Paths | None = None, since=None) -> list[Entry]:
    """The entries with date >= since (optional), oldest first. Strict: a malformed line raises CareerError with
    its line number, never its content."""
    p = p or config.paths()
    since = ledger.parse_date(since) if since is not None else None
    try:
        text = log_file(p).read_text(encoding="utf-8")
    except FileNotFoundError:
        return []
    except UnicodeDecodeError:
        raise CareerError("career.jsonl is not valid UTF-8") from None
    except OSError as err:
        raise CareerError("career.jsonl cannot be read (%s)" % type(err).__name__) from None
    lines = text.split("\n")
    if lines and lines[-1] == "":
        lines.pop()
    out: list[Entry] = []
    for n, line in enumerate(lines, start=1):
        where = "career.jsonl line %d" % n
        if not line.strip():
            raise CareerError("%s: blank line" % where)
        try:
            obj = json.loads(line)
        except ValueError:
            raise CareerError("%s: not a JSON object" % where) from None
        if not isinstance(obj, dict) or set(obj) != set(FIELDS):
            raise CareerError("%s: fields do not match" % where)
        if not isinstance(obj["tags"], list) or not all(isinstance(obj[k], str) for k in FIELDS if k != "tags"):
            raise CareerError("%s: wrong field types" % where)
        try:
            e = make_entry(title=obj["title"], role=obj["role"], stack=obj["stack"], outcome=obj["outcome"],
                           cv_line=obj["cv_line"], tags=obj["tags"], day=obj["date"])
        except CareerError as err:
            raise CareerError("%s: %s" % (where, err)) from None
        if since is None or date.fromisoformat(e.date) >= since:
            out.append(e)
    out.sort(key=lambda e: e.date)
    return out


# --------------------------------------------------------------------------- the update


def _sentence(text: str) -> str:
    text = text.strip().rstrip(".!;:,").strip()
    return text + "." if text else ""


def _join(items: list[str]) -> str:
    """a, b and c (no comma before the last "and")."""
    items = [i for i in items if i]
    if len(items) <= 1:
        return "".join(items)
    return "%s and %s" % (", ".join(items[:-1]), items[-1])


def _stack_words(entries: list[Entry], limit: int = 8) -> list[str]:
    """The distinct parts of the stacks, newest entry first."""
    out: list[str] = []
    for e in reversed(entries):
        for part in _STACK_SPLIT_RE.split(e.stack):
            part = part.strip().rstrip(".").strip()
            if part and part.lower() not in (o.lower() for o in out):
                out.append(part)
    return out[:limit]


def _pieces(n: int) -> str:
    return "%d %s of work" % (n, "piece" if n == 1 else "pieces")


def _lower_theme(t: str) -> str:
    """"Cloud migration" -> "cloud migration", "AI and data" stays."""
    return t if t[:2].isupper() else t[:1].lower() + t[1:]


def draft(entries: list[Entry], day: date, since: date | None) -> str:
    """The Markdown draft: CV bullets grouped by theme, then one LinkedIn paragraph. Plain style."""
    lines = ["# Career update %s" % day.isoformat(), ""]
    if not entries:
        lines.append("No career entries%s." % (" since %s" % since.isoformat() if since else ""))
        return "\n".join(lines) + "\n"
    first, last = entries[0].date, entries[-1].date
    lines += ["Entries from %s to %s: %d." % (first, last, len(entries)), "", "## CV bullets", ""]
    by_theme: dict[str, list[Entry]] = {}
    for e in entries:
        by_theme.setdefault(theme_of(e.tags), []).append(e)
    order = [name for name, _ in THEMES] + [OTHER]
    themes = [t for t in order if t in by_theme]
    for t in themes:
        lines += ["### %s" % t, ""]
        for e in sorted(by_theme[t], key=lambda e: e.date, reverse=True):
            lines.append("- %s" % _sentence(ledger.plain(e.cv_line)))
        lines.append("")
    newest = sorted(entries, key=lambda e: e.date, reverse=True)[:5]
    named = [_lower_theme(t) for t in themes if t != OTHER]
    para = ["From %s to %s I finished %s in %s." % (first, last, _pieces(len(entries)),
                                                   _join(named) if named else "several areas")]
    for e in newest:
        para.append("%s: %s" % (_sentence(ledger.plain(e.title))[:-1], _sentence(ledger.plain(e.outcome))))
    stack = _stack_words(entries)
    if stack:
        para.append("Main tools: %s." % _join(stack))
    lines += ["## LinkedIn", "", ledger.plain(" ".join(para)), ""]
    return "\n".join(lines).rstrip("\n") + "\n"


def _writing_module():
    try:
        mod = importlib.import_module("awb.writing")
    except Exception:  # missing, or unfinished and broken on import
        return None
    return mod if callable(getattr(mod, "check_text", None)) else None


def _tell(t) -> tuple[str, int, bool, str]:
    """(cls, line, blocking, hint) of a writing tell, an object or a dict."""
    get = (lambda k, d=None: t.get(k, d)) if isinstance(t, dict) else (lambda k, d=None: getattr(t, k, d))
    try:
        line = int(get("line", 0) or 0)
    except (TypeError, ValueError):
        line = 0
    return str(get("cls", "unknown")), line, bool(get("blocking", False)), str(get("hint", "") or "")


def _withhold(entries: list[Entry], reg: Path | None) -> tuple[list[Entry], int]:
    texts = [getattr(e, name) for e in entries for name in TEXT_FIELDS]
    hits = _wrap(ledger.hit_indexes, texts, reg)
    if not hits:
        return entries, 0
    out: list[Entry] = []
    k = 0
    for e in entries:
        values = {}
        for name in TEXT_FIELDS:
            values[name] = ledger.WITHHELD if k in hits else getattr(e, name)
            k += 1
        out.append(Entry(date=e.date, tags=list(e.tags), **values))
    return out, len(hits)


def _write_stamp(p: config.Paths, day: date) -> None:
    target = stamp_file(p)
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".career-updated.", suffix=".tmp", dir=target.parent)
    try:
        os.fchmod(fd, 0o640)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(day.isoformat() + "\n")
        os.replace(tmp, target)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def update(p: config.Paths | None = None, since=None, day=None) -> Update:
    """Draft the update from the entries since `since` (all when None), run the writing check over it and store
    the date of the run."""
    p = p or config.paths()
    run = ledger.parse_date(day) if day is not None else today()
    since_d = ledger.parse_date(since) if since is not None else None
    entries = load(p, since_d)
    withheld = 0
    if entries:
        reg = ledger.register_path(p)
        _wrap(ledger.ensure_checkable, reg)
        entries, withheld = _withhold(entries, reg)
    text = draft(entries, run, since_d)
    result = Update(draft=text, entries=len(entries), withheld=withheld)
    mod = _writing_module()
    if mod is None:
        result.note = "writing check skipped: awb.writing is not built yet"
    else:
        try:
            tells, metrics = mod.check_text(text, mode="doc", scope="tcp")
            result.tells = list(tells or [])
            result.metrics = dict(metrics or {})
        except Exception as err:  # an unfinished writing module must not lose the draft
            result.note = "writing check failed (%s), the draft is not checked" % type(err).__name__
    result.candidates = ledger_candidates(p, since_d or last_update(p))
    _write_stamp(p, run)
    return result


CANDIDATE_KINDS = ("code", "tooling", "proof", "tender", "training")


def masked(text: str) -> str:
    """A ledger text for the career side: every register code becomes "a customer", every project code "a
    project", because a career entry names no customer at all."""
    from awb import codes
    return codes.PROJECT_CODE_RE.sub("a project", codes.PLACEHOLDER_RE.sub("a customer", text))


def ledger_candidates(p: config.Paths, since: date | None) -> str:
    """T-46: the ledger entries since `since` that may be worth a career line, codes masked. Input for him, not
    a part of the checked draft."""
    try:
        entries = [e for e in ledger.load(p, start=since) if e.kind in CANDIDATE_KINDS]
    except ledger.LedgerError:
        return "## From the ledger\n\nThe ledger could not be read.\n"
    head = "## From the ledger%s\n\n" % (" since %s" % since.isoformat() if since else "")
    if not entries:
        return head + "No ledger entry of kind %s.\n" % ", ".join(CANDIDATE_KINDS)
    lines = ["Work that may be worth a career line (awb career add):", ""]
    for e in entries:
        text = masked(ledger.plain(e.done))
        if e.outcome:
            text += " Outcome: " + masked(ledger.plain(e.outcome))
        lines.append("- %s (%s): %s" % (e.date, e.kind, text))
    return head + "\n".join(lines) + "\n"


# --------------------------------------------------------------------------- due


def last_update(p: config.Paths | None = None) -> date | None:
    p = p or config.paths()
    try:
        text = stamp_file(p).read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None
    try:
        return ledger.parse_date(text.strip()[:10])
    except ValueError:
        return None


def days_since_update(p: config.Paths | None = None, day=None) -> int | None:
    """Days since the last `awb career update`, None when there was none (the session-start hook reads this)."""
    last = last_update(p)
    if last is None:
        return None
    run = ledger.parse_date(day) if day is not None else today()
    return (run - last).days


def due(p: config.Paths | None = None, day=None) -> tuple[int | None, bool]:
    """(days, due). Days since the last update (before the first update: since the oldest entry); None and not due
    when there is neither. Due when over DUE_DAYS."""
    p = p or config.paths()
    run = ledger.parse_date(day) if day is not None else today()
    days = days_since_update(p, run)
    if days is None:
        entries = load(p)
        if not entries:
            return None, False
        days = (run - date.fromisoformat(entries[0].date)).days
    return days, days > DUE_DAYS


# --------------------------------------------------------------------------- command line


def _fail(err: Exception) -> int:
    if isinstance(err, OSError):
        print("awb career: %s (operating system error)" % type(err).__name__, file=sys.stderr)
        return EXIT_ERROR
    print("awb career: %s" % err, file=sys.stderr)
    return EXIT_FINDINGS if isinstance(err, (Refused, ledger.Refused)) else EXIT_ERROR


def _cmd_add(args, p: config.Paths) -> int:
    e = add(args.title, args.role, args.stack, args.outcome, args.cv_line, tags=args.tag, day=args.date, p=p)
    print("career: added one entry of %s (theme %s)" % (e.date, theme_of(e.tags)))
    return EXIT_OK


def _cmd_update(args, p: config.Paths) -> int:
    res = update(p, since=args.since)
    sys.stdout.write(res.draft)
    if res.candidates:
        sys.stderr.write("\n" + res.candidates)
    if res.withheld:
        print("awb career: %d texts withheld by the name check" % res.withheld, file=sys.stderr)
    if res.note:
        print("awb career: %s" % res.note, file=sys.stderr)
    for t in res.tells:
        cls, line, blocking, hint = _tell(t)
        print("%s  line %d  %s%s" % (cls, line, hint, "" if blocking else "  (not blocking)"), file=sys.stderr)
    if res.metrics:
        shown = ", ".join("%s %s" % (k, v) for k, v in sorted(res.metrics.items())
                          if isinstance(v, (int, float, str)))
        if shown:
            print("awb career: metrics: %s" % shown, file=sys.stderr)
    print("awb career: update stored for %s, %s, %d blocking tells"
          % (last_update(p), "%d %s" % (res.entries, "entry" if res.entries == 1 else "entries"), res.blocking), file=sys.stderr)
    return EXIT_FINDINGS if res.blocking else EXIT_OK


def _cmd_due(args, p: config.Paths) -> int:
    days, is_due = due(p)
    if days is None:
        print("career: no update and no entries yet, nothing is due")
        return EXIT_OK
    last = last_update(p)
    what = "last update" if last is not None else "oldest entry, no update yet"
    print("career: %d days since the %s%s" % (days, what, ", run awb career update" if is_due else ""))
    return EXIT_FINDINGS if is_due else EXIT_OK


def main(argv: list[str] | None = None) -> int:
    """`awb career add|update|due`."""
    from awb.cli import SafeParser

    argv = list(sys.argv[1:] if argv is None else argv)
    ap = SafeParser(prog="awb career", description="The career log. No customer, project or person in it.")
    sub = ap.add_subparsers(dest="command", parser_class=SafeParser)
    s = sub.add_parser("add", help="add one entry")
    s.add_argument("--title", required=True)
    s.add_argument("--role", required=True, help="what you did, in verbs")
    s.add_argument("--stack", required=True, help="technologies, products, APIs")
    s.add_argument("--outcome", required=True, help="the measurable outcome")
    s.add_argument("--cv-line", dest="cv_line", required=True, help="one sentence in CV voice")
    s.add_argument("--tag", action="append", default=[], help="a tag of rules/tags.txt (default: from the stack)")
    s.add_argument("--date", type=ledger.iso_date, default=None, metavar="D", help="the date (ISO, default today)")
    s.set_defaults(func=_cmd_add)
    s = sub.add_parser("update", help="draft CV bullets and a LinkedIn paragraph")
    s.add_argument("--since", type=ledger.iso_date, default=None, metavar="D", help="first date (ISO)")
    s.set_defaults(func=_cmd_update)
    s = sub.add_parser("due", help="days since the last update, exit 1 when over %d" % DUE_DAYS)
    s.set_defaults(func=_cmd_due)
    try:
        args = ap.parse_args(argv)
    except SystemExit as exc:
        if exc.code is None:
            return EXIT_OK
        return exc.code if isinstance(exc.code, int) else EXIT_ERROR
    func = getattr(args, "func", None)
    if func is None:
        ap.print_usage(sys.stderr)
        return EXIT_ERROR
    try:
        return func(args, config.paths())
    except (CareerError, ledger.LedgerError, OSError) as err:
        return _fail(err)


if __name__ == "__main__":
    sys.exit(main())
