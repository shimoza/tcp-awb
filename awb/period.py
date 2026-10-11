"""The period report for the owner: every project with activity in a period, in three blocks.

    awb report --period [--from D] [--to D] [--format md|html] [--reveal [--out PATH] [--replace]]

What counts as activity of a project: its ledger entries, the commits of its repository and the changes of its
STATE.md (the commits that touch it and the time the file was last written), each on a date inside the period.
A project with none of them in the period is left out. A project code the ledger names but the project register
does not know is listed with the kind and state "-".

Each project goes into one block by the rules of `BLOCKS` and `block_of`, the one place they live; the report
prints them at its end. Per project: code, customer code, kind and state, first and last activity, the goal line
of SCOPE.md, the ledger kinds and technologies with counts, the files of deliverables/ by type, whether a test
tenant was used (rows of RESOURCES.md and calls of the key service's log, counts only) and the Status line of
STATE.md. Above the blocks a summary: projects per block and per kind, ledger entries per kind and per technology.

The goal and the status are his texts: they go through the name check once more, as the ledger report does, and a
text that now carries a registered name is shown as "(withheld by the name check)".

Codes only by default. `--reveal` replaces each code by its registered name (`awb.reveal`). It is refused for the
work user and inside an assistant session, like `awb reveal`, and its output goes either to his own terminal or to
`--out PATH` (mode 600), never to a pipe and never to a place a session or the commit gate cannot see as such: the
shared side, the knowledge base, a registered project or any folder of a git working copy such as the repository.
"""
from __future__ import annotations

import html
import os
import re
import subprocess
import sys
from collections import Counter
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path

from awb import codes, config, ledger, projects

RECENT_DAYS = 30
"""The last days of the period that decide between active and finished customer work."""

BLOCKS = (
    (1, "Active customer work",
     "a customer code, state active and activity in the last %d days of the period" % RECENT_DAYS),
    (2, "Finished customer work",
     "a customer code, closed (or not active) or no activity in the last %d days of the period" % RECENT_DAYS),
    (3, "Technical questions", "kind query (or a kind that became query), or no customer"),
)
"""The three blocks: number, title and rule. `block_of` applies the rules in the order 3, 1, 2."""

UNKNOWN = "-"
OUT_HELP = ("where the named report goes, mode 600: a file of your own outside every git working copy (not under "
            "~/tcp-awb), outside the shared side, the knowledge base and the projects, for example "
            "~/reports/period-2026-H1.md; without --out it prints to your terminal only, never to a pipe")


def block_of(kind: str, customer: str, state: str, last: date, end: date) -> int:
    """The block of one project: the rules of BLOCKS, in one place."""
    if projects.LEGACY_KINDS.get(kind, kind) == "query" or customer in ("", projects.NO_CUSTOMER, UNKNOWN):
        return 3
    if state == "active" and last >= end - timedelta(days=RECENT_DAYS - 1):
        return 1
    return 2


class PeriodError(Exception):
    """The report cannot be made. The message carries codes, counts and reasons, never a text or a name."""


@dataclass
class Item:
    code: str
    customer: str = projects.NO_CUSTOMER
    kind: str = UNKNOWN
    state: str = UNKNOWN
    first: date | None = None
    last: date | None = None
    goal: str = ""
    status: str = ""
    sources: Counter = field(default_factory=Counter)        # ledger, commits, STATE.md -> dates in the period
    ledger_kinds: Counter = field(default_factory=Counter)
    tech: Counter = field(default_factory=Counter)
    deliverables: Counter = field(default_factory=Counter)
    resources: int = 0
    calls: int | None = 0                                    # None: the call log cannot be read here
    block: int = 3


# --------------------------------------------------------------------------- what a project folder holds


def _git_dates(folder: Path, *paths: str) -> list[date]:
    """Committer dates of the project's own repository (never a repository above it), optionally of some paths."""
    if not (folder / ".git").exists():
        return []
    try:
        out = subprocess.run(["git", "-c", "safe.directory=%s" % folder, "-C", str(folder), "log", "--format=%cs",
                              "--", *paths], capture_output=True, text=True, timeout=30,
                             stdin=subprocess.DEVNULL).stdout
    except (OSError, subprocess.SubprocessError):
        return []
    days = []
    for line in out.split():
        try:
            days.append(date.fromisoformat(line))
        except ValueError:
            pass
    return days


def _mtime_date(path: Path) -> date | None:
    try:
        return datetime.fromtimestamp(path.stat().st_mtime).date()
    except OSError:
        return None


def status_of(folder: Path) -> str:
    """The text of the Status line of STATE.md, empty when there is none."""
    try:
        text = (folder / "STATE.md").read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return ""
    for line in text.splitlines():
        if line.startswith("Status:"):
            return " ".join(line[len("Status:"):].split())
    return ""


def deliverable_types(folder: Path) -> Counter:
    """Files under deliverables/ by type (the suffix, "none" without one); hidden files and links are skipped."""
    out: Counter = Counter()
    root = folder / "deliverables"
    if root.is_symlink() or not root.is_dir():
        return out
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if not d.startswith(".") and not os.path.islink(os.path.join(dirpath, d))]
        for name in filenames:
            if name.startswith(".") or os.path.islink(os.path.join(dirpath, name)):
                continue
            suffix = Path(name).suffix.lower().lstrip(".")
            out[suffix if re.fullmatch(r"[a-z0-9]{1,8}", suffix) else "none"] += 1
    return out


def resource_rows(folder: Path) -> int:
    """Data rows of the table in RESOURCES.md, whatever their state."""
    try:
        text = (folder / "RESOURCES.md").read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return 0
    rows, head = 0, False
    for line in text.splitlines():
        if not line.strip().startswith("|"):
            continue
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if not head:
            head = True
            continue
        if all(set(c) <= set("-: ") for c in cells):
            continue
        rows += 1
    return rows


def call_counts(p: config.Paths, start: date | None, end: date) -> dict[str, int] | None:
    """Calls of the key service per project code in the period, from `<vault>/log/cloud-YYYY-MM.tsv`. None when the
    log folder cannot be read here (the work user of a sealed host); an empty dict when there is no log."""
    folder = p.vault / "log"
    try:
        names = sorted(n for n in os.listdir(folder) if re.fullmatch(r"cloud-[0-9]{4}-[0-9]{2}\.tsv", n))
    except FileNotFoundError:
        return {}
    except OSError:
        return None
    out: Counter = Counter()
    for name in names:
        month = name[len("cloud-"):-len(".tsv")]
        if (start and month < start.isoformat()[:7]) or month > end.isoformat()[:7]:
            continue
        try:
            text = (folder / name).read_text(encoding="utf-8", errors="replace")
        except OSError:
            return None
        for line in text.splitlines():
            cells = line.split("\t")
            if len(cells) < 9 or not codes.is_project_code(cells[8]):
                continue
            try:
                day = date.fromisoformat(cells[0][:10])
            except ValueError:
                continue
            if (start is None or day >= start) and day <= end:
                out[cells[8]] += 1
    return dict(out)


# --------------------------------------------------------------------------- gathering


def _inside(day: date | None, start: date | None, end: date) -> bool:
    return day is not None and (start is None or day >= start) and day <= end


def gather(p: config.Paths, start: date | None, end: date) -> tuple[list[Item], list[ledger.Entry]]:
    """Every project with activity in the period, with its block, and the ledger entries of the period."""
    entries = ledger.load(p, start, end)
    rows = {r.code: r for r in projects.load(p)}
    calls = call_counts(p, start, end)
    items: dict[str, Item] = {}
    dates: dict[str, list[date]] = {}

    def item(code: str) -> Item:
        if code not in items:
            items[code] = Item(code)
            dates[code] = []
        return items[code]

    for e in entries:
        if not codes.is_project_code(e.project):
            continue
        it = item(e.project)
        d = date.fromisoformat(e.date)
        dates[e.project].append(d)
        it.sources["ledger"] += 1
        it.ledger_kinds[e.kind] += 1
        it.tech.update(e.tags)
        if e.customer != ledger.NONE and e.project not in rows:
            it.customer = e.customer
    for code, row in rows.items():
        folder = Path(row.path)
        usable = folder.is_dir() and not folder.is_symlink()
        found = []
        if usable:
            commits = [d for d in _git_dates(folder) if _inside(d, start, end)]
            state = [d for d in _git_dates(folder, "STATE.md") if _inside(d, start, end)]
            written = _mtime_date(folder / "STATE.md")
            if _inside(written, start, end) and written not in state:
                state.append(written)
            found = commits + state
            if commits or state or code in items:
                it = item(code)
                it.sources["commits"] += len(commits)
                it.sources["STATE.md"] += len(state)
        if code not in items:
            continue
        it = items[code]
        dates[code] += found
        it.customer, it.kind, it.state = row.customer, row.kind, row.state
        if usable:
            it.goal = projects.goal_of(folder)
            it.status = status_of(folder)
            it.deliverables = deliverable_types(folder)
            it.resources = resource_rows(folder)
    for code, it in items.items():
        it.first, it.last = min(dates[code]), max(dates[code])
        it.calls = None if calls is None else calls.get(code, 0)
        it.block = block_of(it.kind, it.customer, it.state, it.last, end)
    ordered = sorted(items.values(), key=lambda it: (it.block, -it.last.toordinal(), it.code))
    return ordered, entries


def withhold(items: list[Item], reg: Path) -> int:
    """Goal and status of every item through the name check; a text that carries a name becomes WITHHELD."""
    texts, where = [], []
    for it in items:
        for name in ("goal", "status"):
            if getattr(it, name):
                texts.append(getattr(it, name))
                where.append((it, name))
    if not texts:
        return 0
    ledger.ensure_checkable(reg)
    hits = ledger.hit_indexes(texts, reg)
    for i in hits:
        setattr(where[i][0], where[i][1], ledger.WITHHELD)
    return len(hits)


# --------------------------------------------------------------------------- rendering


def _counts(c: Counter) -> str:
    items = sorted(((k, n) for k, n in c.items() if n), key=lambda kv: (-kv[1], kv[0]))
    return ", ".join("%s %d" % kv for kv in items) if items else "none"


def _tenant(it: Item) -> str:
    calls = "call log not readable here" if it.calls is None else "%d key service calls" % it.calls
    used = it.resources > 0 or bool(it.calls)
    return "%s (RESOURCES.md %d rows, %s)" % ("yes" if used else "no", it.resources, calls)


def _period_text(start: date | None, end: date, items: list[Item]) -> str:
    if start:
        return "%s to %s" % (start.isoformat(), end.isoformat())
    first = min((it.first for it in items), default=None)
    return "up to %s%s" % (end.isoformat(), " (first activity %s)" % first.isoformat() if first else "")


def _fields(it: Item) -> list[tuple[str, str]]:
    return [
        ("customer", it.customer),
        ("kind", "%s, state %s" % (it.kind, it.state)),
        ("activity", "%s to %s (%s)" % (it.first.isoformat(), it.last.isoformat(), _counts(it.sources))),
        ("goal", it.goal or "none"),
        ("ledger kinds", _counts(it.ledger_kinds)),
        ("technologies", _counts(it.tech)),
        ("deliverables", _counts(it.deliverables)),
        ("test tenant", _tenant(it)),
        ("status", it.status or "none"),
    ]


def _summary(items: list[Item], entries: list[ledger.Entry]) -> list[tuple[str, str]]:
    per_block = Counter({n: 0 for n, _, _ in BLOCKS})
    per_block.update(it.block for it in items)
    tech: Counter = Counter()
    for e in entries:
        tech.update(e.tags)
    return [
        ("projects", "%d (%s)" % (len(items), ", ".join("block %d: %d" % (n, per_block[n]) for n, _, _ in BLOCKS))),
        ("projects by kind", _counts(Counter(it.kind for it in items))),
        ("ledger entries", "%d, by kind: %s" % (len(entries), _counts(Counter(e.kind for e in entries)))),
        ("ledger entries by technology", _counts(tech)),
    ]


def render(items: list[Item], entries: list[ledger.Entry], start: date | None, end: date, fmt: str = "md",
           written: date | None = None) -> str:
    """The report as Markdown or as a small HTML page. Codes only."""
    if fmt not in ledger.FORMATS:
        raise PeriodError("format must be md or html")
    title = "Period report %s" % _period_text(start, end, items)
    intro = "Written %s from the ledger, the project register and the project folders. Codes only." % (
        (written or ledger.today()).isoformat())
    summary = _summary(items, entries)
    blocks = [(n, name, [it for it in items if it.block == n]) for n, name, _ in BLOCKS]
    rules = ["%d %s: %s." % (n, name, rule) for n, name, rule in BLOCKS]
    rules.append("Activity: ledger entries, commits of the project's repository and changes of its STATE.md in the "
                 "period. The rules are applied in the order 3, 1, 2.")
    if fmt == "html":
        return _render_html(title, intro, summary, blocks, rules)
    out = ["# %s" % title, "", intro, "", "## Summary", ""]
    out += ["- %s: %s" % kv for kv in summary]
    out.append("")
    for n, name, its in blocks:
        out += ["## %d %s (%d)" % (n, name, len(its)), ""]
        if not its:
            out += ["none", ""]
        for it in its:
            out += ["### %s" % it.code, ""]
            out += ["- %s: %s" % kv for kv in _fields(it)]
            out.append("")
    out += ["## Block rules", ""]
    out += ["- %s" % r for r in rules]
    return "\n".join(ledger.plain(line) if line else line for line in out) + "\n"


def _render_html(title, intro, summary, blocks, rules) -> str:
    esc = lambda s: html.escape(ledger.plain(s))  # noqa: E731
    out = ["<!DOCTYPE html>", '<html lang="en">', "<head>", '<meta charset="utf-8">',
           "<title>%s</title>" % esc(title), "<style>%s</style>" % ledger._STYLE, "</head>", "<body>",
           "<h1>%s</h1>" % esc(title), "<p>%s</p>" % esc(intro), "<h2>Summary</h2>", "<table><tbody>"]
    out += ["<tr><th>%s</th><td>%s</td></tr>" % (esc(k), esc(v)) for k, v in summary]
    out.append("</tbody></table>")
    for n, name, its in blocks:
        out.append("<h2>%d %s (%d)</h2>" % (n, esc(name), len(its)))
        if not its:
            out.append("<p>none</p>")
        for it in its:
            out += ["<h3>%s</h3>" % esc(it.code), "<table><tbody>"]
            out += ["<tr><th>%s</th><td>%s</td></tr>" % (esc(k), esc(v)) for k, v in _fields(it)]
            out.append("</tbody></table>")
    out += ["<h2>Block rules</h2>", "<ul>"] + ["<li>%s</li>" % esc(r) for r in rules] + ["</ul>", "</body>", "</html>"]
    return "\n".join(out) + "\n"


def report(start=None, end=None, fmt: str = "md", p: config.Paths | None = None) -> tuple[str, int]:
    """Gather, check the texts once more and render. Returns the text and the count of texts withheld."""
    p = p or config.paths()
    start = ledger.parse_date(start) if start is not None else None
    end = ledger.parse_date(end) if end is not None else ledger.today()
    if start is not None and start > end:
        raise PeriodError("--from is after --to")
    items, entries = gather(p, start, end)
    withheld = withhold(items, ledger.register_path(p))
    return render(items, entries, start, end, fmt), withheld


# --------------------------------------------------------------------------- names back, owner side only


def reveal_report(text: str, p: config.Paths, fmt: str) -> tuple[str, int]:
    """Every registered code of `text` replaced by its display form (HTML-escaped in an HTML page)."""
    from awb import register, reveal

    forms, _ = reveal.display_forms(register.load(p.register))
    n = 0

    def one(m) -> str:
        nonlocal n
        form = forms.get(m.group(0))
        if form is None:
            return m.group(0)
        n += 1
        return html.escape(form) if fmt == "html" else form

    return codes.PLACEHOLDER_RE.sub(one, text), n


def owner_only() -> None:
    from awb import vault

    if config.is_work_user():
        raise PeriodError("--reveal is for the owner, in your own shell")
    if vault.in_assistant_session():
        raise PeriodError("--reveal never runs inside an assistant session; run it in your own terminal")


def _in_git_tree(path: Path) -> bool:
    return any((d / ".git").exists() for d in path.parents)


def check_out(out: Path, p: config.Paths) -> Path:
    """The real path of `out`, refused where a session reads or inside a git working copy."""
    from awb import reveal

    real = Path(os.path.realpath(out))
    places = reveal.session_places(p) + [Path(os.path.realpath(projects.REPO))]
    if any(real == place or reveal._inside(real, place) for place in places) or _in_git_tree(real):
        raise PeriodError("the named report must not go into the shared side, the knowledge base, a project or a "
                          "git working copy such as the repository")
    return real


def write_out(text: str, out: Path, replace: bool) -> None:
    flags = os.O_WRONLY | os.O_CREAT | os.O_NOFOLLOW | (os.O_TRUNC if replace else os.O_EXCL)
    try:
        fd = os.open(out, flags, 0o600)
    except FileExistsError:
        raise PeriodError("the output file exists; give --replace to overwrite it") from None
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(text)
    os.chmod(out, 0o600)


# --------------------------------------------------------------------------- command line


def main(argv: list[str]) -> int:
    """`awb report --period ...`. Exit 0 written, 1 a text refused, 2 refused or an error."""
    from awb import register
    from awb.cli import SafeParser

    prog = "awb report --period"
    ap = SafeParser(prog="awb report", description="The period report: every project with activity in the period "
                    "in three blocks (active customer work, finished customer work, technical questions). Codes "
                    "only unless --reveal.")
    ap.add_argument("--period", action="store_true", required=True)
    ap.add_argument("--from", dest="start", type=ledger.iso_date, default=None, metavar="D",
                    help="first date (ISO); default the first activity")
    ap.add_argument("--to", dest="end", type=ledger.iso_date, default=None, metavar="D",
                    help="last date (ISO); default today")
    ap.add_argument("--format", dest="fmt", choices=ledger.FORMATS, default="md")
    ap.add_argument("--reveal", action="store_true",
                    help="put the registered names back: the owner only, in his own terminal, never inside an "
                         "assistant session; to the terminal or to --out")
    ap.add_argument("--out", type=Path, default=None, metavar="PATH", help=OUT_HELP)
    ap.add_argument("--replace", action="store_true", help="overwrite an existing --out file")
    try:
        args = ap.parse_args(argv)
    except SystemExit as exc:
        return ledger._exit_code(exc)
    try:
        if (args.out or args.replace) and not args.reveal:
            raise PeriodError("--out and --replace go with --reveal; the codes-only report prints to standard output")
        p = config.paths()
        out = None
        if args.reveal:
            owner_only()
            if args.out is not None:
                out = check_out(args.out, p)
            elif not sys.stdout.isatty():
                raise PeriodError("--reveal prints to a terminal only, never to a pipe or a file; give --out PATH")
        text, withheld = report(args.start, args.end, args.fmt, p)
        if args.reveal:
            text, n = reveal_report(text, p, args.fmt)
            if out is not None:
                write_out(text, out, args.replace)
                print("%s: %d codes replaced; written to %s (mode 600)" % (prog, n, out))
            else:
                sys.stdout.write(text)
        else:
            sys.stdout.write(text)
    except (PeriodError, ledger.LedgerError, projects.ProjectError, register.RegisterError, OSError) as err:
        return ledger._fail(prog, err)
    if withheld:
        print("%s: %d texts withheld by the name check" % (prog, withheld), file=sys.stderr)
    return ledger.EXIT_OK
