"""The activity ledger and the reports made from it.

    awb ledger add --kind K --done TEXT [--project C] [--customer C] [--partner C] [--tag T]... [--outcome TEXT]
                   [--deliverable P]... [--open TEXT]... [--date D]
    awb ledger list [--from D] [--to D]
    awb report --by customer|tech|kind|project [--from D] [--to D] [--format md|html]
    awb report --by questions                     the monthly count of replies that end with a question

One file per ISO week, `<shared>/ledger/YYYY-Www.jsonl` (the ISO year and week of the entry's date), one JSON
object per line with the fields of `FIELDS`. A line is appended under an exclusive `fcntl` lock on the week file
itself and written whole, so two sessions that append at the same time give two whole lines.

What goes in. The project is a project code or "none", the customer a top-level CUST code or "none", the partner
a top-level PART code or "none"; a value of another shape is refused and never repeated, because it may be a name
typed by mistake. Tags come from `rules/tags.txt`, the kind from `KINDS`. Every text (done, outcome, each
deliverable, each open item) goes through the name check (`check.check_text` with the register of
`config.paths()`, through the vault daemon when the register is not readable here) and through the pattern
detectors of the commit gate (secrets, tokens, home paths, the owner's blocklist, identifiers). A hit
refuses the entry (exit 1) and nothing is written. A check that cannot run refuses too (exit 2): the ledger never
takes a text it could not check.

What comes out. `awb ledger list` and `awb report` check the texts once more, because the register grows: a text
that carries a name registered after it was written is shown as "(withheld by the name check)". A report is
grouped by the key, newest first inside a group. It carries codes, dates, kinds and tags besides his texts. It is
written in plain style: no em-dash and no comma before "and" or "or" (`plain` rewrites both in his texts too).

Messages name fields, classes, counts, week files and line numbers. They never carry a text, a matched value or a
code that was refused.
"""
from __future__ import annotations

import dataclasses
import fcntl
import html
import json
import os
import re
import sys
import unicodedata
from collections import Counter
from dataclasses import dataclass, field
from datetime import date, timedelta
from pathlib import Path

from awb import codes, config

EXIT_OK = 0
EXIT_FINDINGS = 1
EXIT_ERROR = 2

KINDS = ("proof", "inquiry", "pricing", "tender", "training", "tooling", "code", "other")
FIELDS = ("date", "project", "customer", "partner", "tags", "kind", "done", "outcome", "deliverables", "open")
BY = ("customer", "tech", "kind", "project")
FORMATS = ("md", "html")
NONE = "none"
WITHHELD = "(withheld by the name check)"

MAX_TEXT = 2000
"""Characters of done and outcome."""
MAX_ITEM = 500
"""Characters of one deliverable or open item."""
MAX_ITEMS = 50
"""Deliverables or open items of one entry."""

RULES_DIR = Path(__file__).resolve().parent.parent / "rules"
TAGS_FILE = RULES_DIR / "tags.txt"

_WEEK_FILE_RE = re.compile(r"(?P<year>[0-9]{4})-W(?P<week>[0-9]{2})\.jsonl")
_DATE_RE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}")
_TAG_RE = re.compile(r"[a-z0-9][a-z0-9.+-]{0,47}")
_DASH_RE = re.compile(r"\s*\u2014\s*|\s+\u2013\s+")
_COMMA_AND_OR_RE = re.compile(r",\s*(?=(?:and|or)\b)", re.IGNORECASE)
_SPACES_RE = re.compile(r"[ \t]{2,}")
_CHECK_CHUNK = 500_000
"""Characters per request of the joined name check (the vault daemon takes requests up to 4 MB)."""


class LedgerError(Exception):
    """A usage or data problem. The message names fields, classes, counts, files and lines, never a value."""


class Refused(LedgerError):
    """A text carries a name, structured data, a secret or a blocklist word. Exit 1."""


@dataclass
class Entry:
    date: str
    project: str = NONE
    customer: str = NONE
    partner: str = NONE
    tags: list[str] = field(default_factory=list)
    kind: str = "other"
    done: str = ""
    outcome: str = ""
    deliverables: list[str] = field(default_factory=list)
    open: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {k: (list(getattr(self, k)) if isinstance(getattr(self, k), list) else getattr(self, k))
                for k in FIELDS}


def today() -> date:
    """The date of today. Tests replace it."""
    return date.today()


# --------------------------------------------------------------------------- small helpers


def plain(text: str) -> str:
    """`text` in plain style: an em-dash (or an en dash between spaces) becomes a comma, a comma directly before
    "and" or "or" goes. Nothing else changes."""
    text = _DASH_RE.sub(", ", text)
    text = _COMMA_AND_OR_RE.sub(" ", text)
    text = _SPACES_RE.sub(" ", text)
    text = text.replace(" ,", ",").replace(",,", ",")
    return text.strip().strip(",").strip()


def parse_date(value) -> date:
    """An ISO date (YYYY-MM-DD) as a date. Raises ValueError without the value."""
    if isinstance(value, date):
        return value
    if not isinstance(value, str) or not _DATE_RE.fullmatch(value):
        raise ValueError("not an ISO date (YYYY-MM-DD)")
    try:
        return date.fromisoformat(value)
    except ValueError:
        raise ValueError("not a valid date") from None


def iso_date(value: str) -> date:
    """argparse type: an ISO date. The error never repeats the value (SafeParser masks it)."""
    return parse_date(value)


def week_name(day) -> str:
    """The ISO week of a date as YYYY-Www, the ISO year included (2027-01-01 is 2026-W53)."""
    y, w, _ = parse_date(day).isocalendar()
    return "%04d-W%02d" % (y, w)


def week_file(p: config.Paths, day) -> Path:
    """The ledger file of the week of `day`."""
    return p.ledger / ("%s.jsonl" % week_name(day))


def _week_span(name: str) -> tuple[date, date] | None:
    m = _WEEK_FILE_RE.fullmatch(name)
    if not m:
        return None
    try:
        monday = date.fromisocalendar(int(m.group("year")), int(m.group("week")), 1)
    except ValueError:
        return None
    return monday, monday + timedelta(days=6)


def load_tags(path: Path | None = None) -> list[str]:
    """The controlled tag list: one tag per line, # starts a comment line."""
    f = Path(path) if path is not None else TAGS_FILE
    try:
        text = f.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        raise LedgerError("the tag list rules/tags.txt cannot be read") from None
    out: list[str] = []
    for line in text.splitlines():
        line = line.strip()
        if line and not line.startswith("#") and line not in out:
            out.append(line)
    return out


def clean_tags(tags, known: list[str] | None) -> list[str]:
    """Tags as a list without repeats. A comma list inside one value is split. Unknown tags are refused when
    `known` is given; the message counts them and names none."""
    if tags is None:
        return []
    if isinstance(tags, str):
        tags = [tags]
    out: list[str] = []
    for t in tags:
        if not isinstance(t, str):
            raise LedgerError("tags must be text")
        for part in t.split(","):
            part = part.strip().lower()
            if not part:
                continue
            if not _TAG_RE.fullmatch(part):
                raise LedgerError("a tag must be lower-case letters, digits, dots, plus signs or hyphens")
            if part not in out:
                out.append(part)
    if known is not None:
        unknown = [t for t in out if t not in known]
        if unknown:
            raise LedgerError("%d tag(s) are not in rules/tags.txt; add a tag there before using it" % len(unknown))
    return out


def clean_text(value, what: str, *, required: bool = False, limit: int = MAX_TEXT) -> str:
    """One line of text, stripped. `what` names the field in a message, the value is never shown."""
    if value is None:
        value = ""
    if not isinstance(value, str):
        raise LedgerError("%s must be text" % what)
    value = value.strip()
    if required and not value:
        raise LedgerError("%s is empty" % what)
    if len(value) > limit:
        raise LedgerError("%s is longer than %d characters" % (what, limit))
    if any(unicodedata.category(c) in ("Cc", "Cf", "Zl", "Zp") for c in value):
        raise LedgerError("%s must be one line without control or invisible characters" % what)
    return value


def clean_items(values, what: str) -> list[str]:
    if values is None:
        return []
    if isinstance(values, str):
        values = [values]
    out: list[str] = []
    for n, v in enumerate(values, start=1):
        v = clean_text(v, "%s %d" % (what, n), required=True, limit=MAX_ITEM)
        out.append(v)
    if len(out) > MAX_ITEMS:
        raise LedgerError("more than %d %ss" % (MAX_ITEMS, what))
    return out


def clean_code(value, what: str) -> str:
    """"none" or a code of the right shape for the field: project, customer (top-level CUST), partner (top-level
    PART). The value is never repeated: a wrong one may be a name typed by mistake."""
    if value is None or value == "" or value == NONE:
        return NONE
    if not isinstance(value, str):
        raise LedgerError("%s must be a code or none" % what)
    if what == "project":
        if codes.is_project_code(value):
            return value
        raise LedgerError("project must be a project code (tcp- and four characters) or none")
    kind = {"customer": "CUST", "partner": "PART"}[what]
    if codes.is_code(value) and value.count("-") == 1 and codes.kind_of(value) == kind:
        return value
    raise LedgerError("%s must be a top-level %s code or none" % (what, kind))


def _counts_text(counts: dict[str, int]) -> str:
    return ", ".join("%s %d" % (c, n) for c, n in sorted(counts.items()))


# --------------------------------------------------------------------------- the checks


def _detectors() -> dict:
    """The pattern detectors of the commit gate (secret, token, home path, old grammar, identifier)."""
    try:
        from awb import gate
    except ImportError:
        return {}
    return dict(getattr(gate, "DETECTORS", {}) or {})


def register_path(p: config.Paths | None = None) -> Path:
    return (p or config.paths()).register


def ensure_checkable(reg: Path) -> None:
    """Refuse to go on when a readable vault folder holds no register at all: the names could not be checked.
    Every other case (a readable register, the vault daemon) is left to `check.check_text`."""
    from awb import check

    try:
        kind, _ = check.register_source(reg)
    except Exception as err:  # RegisterError: a broken register
        raise LedgerError(_check_reason(err)) from None
    if kind == check.MISSING:
        raise LedgerError("no register found, names cannot be checked")


def _check_reason(err: Exception) -> str:
    from awb import check

    if isinstance(err, check.CheckUnavailable):
        return str(err) or "name check unavailable"
    return "the register cannot be read"


def screen(text: str, reg: Path | None, *, codes_too: bool = False) -> dict[str, int]:
    """Counts per class of what makes `text` unfit for the shared side: hits of the name check (a registered
    name or structured data), hits of the gate detectors and, with `codes_too`, register codes and project codes.
    Raises LedgerError when the name check cannot run. Never returns or raises with a value."""
    from awb import check

    counts: Counter = Counter()
    try:
        hits = check.check_text(text, reg)
    except Exception as err:  # CheckUnavailable and RegisterError
        raise LedgerError(_check_reason(err)) from None
    for h in hits:
        counts[h.get("cls", "unknown") if isinstance(h, dict) else "unknown"] += 1
    for cls, fn in _detectors().items():
        try:
            n = len(fn(text))
        except Exception:
            n = 0
        if n:
            counts[cls] += n
    if codes_too:
        # also a code written with a look-alike dash, fullwidth characters or invisible characters inside
        n_reg, n_proj = codes.count_codes(text)
        if n_reg:
            counts["code"] += n_reg
        if n_proj:
            counts["project-code"] += n_proj
    return dict(counts)


def screen_fields(fields: list[tuple[str, str]], reg: Path | None, *, codes_too: bool = False,
                  refused=Refused, hint: str = "") -> None:
    """Raise `refused` on the first field whose text does not pass `screen`. Field names and counts only."""
    for what, text in fields:
        if not text:
            continue
        found = screen(text, reg, codes_too=codes_too)
        if found:
            raise refused("%s refused (%s)%s" % (what, _counts_text(found), hint))


def hit_indexes(texts: list[str], reg: Path | None) -> set[int]:
    """Indexes of the texts that the name check finds anything in. One check over all texts joined first (the
    common case: nothing), then one per text only when the joined check found something. Raises LedgerError when
    the check cannot run."""
    from awb import check

    idx = [i for i, t in enumerate(texts) if t]
    if not idx:
        return set()
    try:
        suspects: list[int] = []
        chunk: list[int] = []
        size = 0
        for i in idx + [None]:
            if i is not None and size + len(texts[i]) < _CHECK_CHUNK:
                chunk.append(i)
                size += len(texts[i]) + 2
                continue
            if chunk and check.check_text("\n\n".join(texts[j] for j in chunk), reg):
                suspects.extend(chunk)
            chunk, size = ([i], len(texts[i]) + 2) if i is not None else ([], 0)
        return {i for i in suspects if check.check_text(texts[i], reg)}
    except Exception as err:  # CheckUnavailable and RegisterError
        raise LedgerError(_check_reason(err)) from None


def withhold(entries: list[Entry], reg: Path | None) -> tuple[list[Entry], int]:
    """The entries with every text that now carries a name hit replaced by WITHHELD. Also the count of texts
    replaced."""
    texts: list[str] = []
    where: list[tuple[int, str, int | None]] = []
    for n, e in enumerate(entries):
        for name in ("done", "outcome"):
            texts.append(getattr(e, name))
            where.append((n, name, None))
        for name in ("deliverables", "open"):
            for k, v in enumerate(getattr(e, name)):
                texts.append(v)
                where.append((n, name, k))
    hits = hit_indexes(texts, reg)
    if not hits:
        return list(entries), 0
    out = [dataclasses.replace(e, deliverables=list(e.deliverables), open=list(e.open)) for e in entries]
    for i in hits:
        n, name, k = where[i]
        if k is None:
            setattr(out[n], name, WITHHELD)
        else:
            getattr(out[n], name)[k] = WITHHELD
    return out, len(hits)


# --------------------------------------------------------------------------- entries


def make_entry(*, kind: str, done: str, project=None, customer=None, partner=None, tags=(), outcome="",
               deliverables=(), open_items=(), day=None, known_tags: list[str] | None = None) -> Entry:
    """A validated entry. Shapes only: the texts are checked by `check_entry`."""
    if kind not in KINDS:
        raise LedgerError("kind must be one of %s" % ", ".join(KINDS))
    if day is None:
        day = today()
    try:
        d = parse_date(day)
    except ValueError as err:
        raise LedgerError("date: %s" % err) from None
    return Entry(
        date=d.isoformat(),
        project=clean_code(project, "project"),
        customer=clean_code(customer, "customer"),
        partner=clean_code(partner, "partner"),
        tags=clean_tags(tags, known_tags),
        kind=kind,
        done=clean_text(done, "done", required=True),
        outcome=clean_text(outcome, "outcome"),
        deliverables=clean_items(deliverables, "deliverable"),
        open=clean_items(open_items, "open item"),
    )


def check_entry(e: Entry, reg: Path | None) -> None:
    """Refuse an entry whose texts carry a name, structured data, a secret, an identifier or the old grammar."""
    if reg is not None:
        ensure_checkable(reg)
    fields =[("done", e.done), ("outcome", e.outcome)]
    fields += [("deliverable %d" % n, v) for n, v in enumerate(e.deliverables, start=1)]
    fields += [("open item %d" % n, v) for n, v in enumerate(e.open, start=1)]
    screen_fields(fields, reg)


def append_line(path: Path, obj: dict, mode: int = 0o640) -> None:
    """Append one JSON line to `path` under an exclusive fcntl lock on the file, written whole. A last line left
    without its line break by a crashed writer gets one first, so this line stays whole."""
    data = (json.dumps(obj, ensure_ascii=False) + "\n").encode("utf-8")
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_RDWR | os.O_APPEND | os.O_CREAT, mode)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        try:
            size = os.fstat(fd).st_size
            if size and os.pread(fd, 1, size - 1) != b"\n":
                data = b"\n" + data
            view = memoryview(data)
            while view:
                written = os.write(fd, view)
                view = view[written:]
            os.fsync(fd)
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)


def add(kind: str, done: str, *, project=None, customer=None, partner=None, tags=(), outcome="",
        deliverables=(), open_items=(), day=None, p: config.Paths | None = None) -> Entry:
    """Validate, check and append one entry to the week file of its date. Returns the entry.

    Raises LedgerError on a wrong shape or when the check cannot run, Refused on a hit."""
    p = p or config.paths()
    e = make_entry(kind=kind, done=done, project=project, customer=customer, partner=partner, tags=tags,
                   outcome=outcome, deliverables=deliverables, open_items=open_items, day=day,
                   known_tags=load_tags())
    check_entry(e, register_path(p))
    append_line(week_file(p, e.date), e.as_dict())
    return e


def _entry_from(obj, known_tags=None) -> Entry:
    if not isinstance(obj, dict):
        raise LedgerError("not a JSON object")
    keys = set(obj)
    if keys != set(FIELDS):
        missing = len(set(FIELDS) - keys)
        extra = len(keys - set(FIELDS))
        raise LedgerError("fields do not match (%d missing, %d unknown)" % (missing, extra))
    for name in ("date", "project", "customer", "partner", "kind", "done", "outcome"):
        if not isinstance(obj[name], str):
            raise LedgerError("%s must be text" % name)
    for name in ("tags", "deliverables", "open"):
        if not isinstance(obj[name], list):
            raise LedgerError("%s must be a list" % name)
    return make_entry(kind=obj["kind"], done=obj["done"], project=obj["project"], customer=obj["customer"],
                      partner=obj["partner"], tags=obj["tags"], outcome=obj["outcome"],
                      deliverables=obj["deliverables"], open_items=obj["open"], day=obj["date"],
                      known_tags=known_tags)


def load(p: config.Paths | None = None, start=None, end=None) -> list[Entry]:
    """Every entry with start <= date <= end (both optional, inclusive), oldest first, in the order written.
    Strict: a malformed line raises LedgerError with the week file and the line number, never its content."""
    p = p or config.paths()
    start = parse_date(start) if start is not None else None
    end = parse_date(end) if end is not None else None
    try:
        names = sorted(n for n in os.listdir(p.ledger) if _WEEK_FILE_RE.fullmatch(n))
    except FileNotFoundError:
        return []
    except OSError as err:
        raise LedgerError("the ledger folder cannot be read (%s)" % type(err).__name__) from None
    out: list[Entry] = []
    for name in names:
        span = _week_span(name)
        if span is None:
            raise LedgerError("ledger %s: not a valid ISO week" % name)
        if (start and span[1] < start) or (end and span[0] > end):
            continue
        try:
            text = (p.ledger / name).read_text(encoding="utf-8")
        except UnicodeDecodeError:
            raise LedgerError("ledger %s is not valid UTF-8" % name) from None
        except OSError as err:
            raise LedgerError("ledger %s cannot be read (%s)" % (name, type(err).__name__)) from None
        lines = text.split("\n")
        if lines and lines[-1] == "":
            lines.pop()
        for n, line in enumerate(lines, start=1):
            where = "ledger %s line %d" % (name, n)
            if not line.strip():
                raise LedgerError("%s: blank line" % where)
            try:
                obj = json.loads(line)
            except ValueError:
                raise LedgerError("%s: not a JSON object" % where) from None
            try:
                e = _entry_from(obj)
            except LedgerError as err:
                raise LedgerError("%s: %s" % (where, err)) from None
            if week_name(e.date) != name[:-len(".jsonl")]:
                raise LedgerError("%s: the date is not in the week of the file" % where)
            d = date.fromisoformat(e.date)
            if (start and d < start) or (end and d > end):
                continue
            out.append(e)
    out.sort(key=lambda e: e.date)   # stable: the order written inside a date
    return out


# --------------------------------------------------------------------------- reports


def _keys(e: Entry, by: str) -> list[str]:
    if by == "customer":
        return [e.customer]
    if by == "project":
        return [e.project]
    if by == "kind":
        return [e.kind]
    if by == "tech":
        return list(e.tags) or [NONE]
    raise LedgerError("by must be one of %s" % ", ".join(BY))


def group(entries: list[Entry], by: str) -> dict[str, list[Entry]]:
    """Entries grouped by the key, groups in key order with "none" last, newest first inside a group (the one
    written later first on the same date). With by="tech" an entry is listed under each of its tags."""
    if by not in BY:
        raise LedgerError("by must be one of %s" % ", ".join(BY))
    seq = {id(e): n for n, e in enumerate(entries)}
    groups: dict[str, list[Entry]] = {}
    for e in entries:
        for k in _keys(e, by):
            groups.setdefault(k, []).append(e)
    ordered: dict[str, list[Entry]] = {}
    for k in sorted(groups, key=lambda k: (k == NONE, k)):
        ordered[k] = sorted(groups[k], key=lambda e: (e.date, seq[id(e)]), reverse=True)
    return ordered


COLUMNS = ("Date", "Kind", "Project", "Customer", "Partner", "Tech", "Done", "Outcome", "Deliverables", "Open")


def _cells(e: Entry) -> list[str]:
    return [plain(c) for c in (e.date, e.kind, e.project, e.customer, e.partner, ", ".join(e.tags), e.done,
                               e.outcome, ", ".join(e.deliverables), "; ".join(e.open))]


def _period(entries: list[Entry], start: date | None, end: date | None) -> str:
    if start and end:
        return "from %s to %s" % (start.isoformat(), end.isoformat())
    if start:
        return "from %s" % start.isoformat()
    if end:
        return "up to %s" % end.isoformat()
    if entries:
        return "all entries, %s to %s" % (entries[0].date, entries[-1].date)
    return "all entries"


def _plural(n: int, word: str) -> str:
    return "%d %s%s" % (n, word, "" if n == 1 else "s")


_BY_TITLE = {"customer": "customer", "tech": "technology", "kind": "kind of work", "project": "project"}


def _entries_word(n: int) -> str:
    return "%d %s" % (n, "entry" if n == 1 else "entries")


def render(entries: list[Entry], by: str, fmt: str = "md", start=None, end=None) -> str:
    """The report text: Markdown or a small HTML page with one table per group."""
    if fmt not in FORMATS:
        raise LedgerError("format must be md or html")
    start = parse_date(start) if start is not None else None
    end = parse_date(end) if end is not None else None
    groups = group(entries, by)
    title = "Ledger report by %s" % _BY_TITLE[by]
    summary = ["Period: %s." % _period(entries, start, end),
               "%s in %s." % (_entries_word(len(entries)), _plural(len(groups), "group"))]
    if by == "tech":
        summary.append("An entry with several tags is listed under each tag.")
    if fmt == "html":
        return _render_html(title, summary, groups)
    return _render_md(title, summary, groups)


def _md_cell(text: str) -> str:
    return text.replace("\\", "\\\\").replace("|", "\\|")


def _render_md(title: str, summary: list[str], groups: dict[str, list[Entry]]) -> str:
    out = ["# %s" % title, "", " ".join(summary), ""]
    if not groups:
        out.append("No entries in the period.")
        return "\n".join(plain(l) if l else l for l in out) + "\n"
    out += ["| Group | Entries | Newest |", "| --- | --- | --- |"]
    for k, rows in groups.items():
        out.append("| %s | %d | %s |" % (_md_cell(k), len(rows), rows[0].date))
    out.append("")
    for k, rows in groups.items():
        out += ["## %s (%s)" % (k, _entries_word(len(rows))), ""]
        out.append("| %s |" % " | ".join(COLUMNS))
        out.append("|%s" % (" --- |" * len(COLUMNS)))
        for e in rows:
            out.append("| %s |" % " | ".join(_md_cell(c) for c in _cells(e)))
        out.append("")
    return "\n".join(out).rstrip("\n") + "\n"


_STYLE = ("body{font-family:sans-serif;margin:1.5em;color:#222}"
          "table{border-collapse:collapse;margin:0 0 1.5em 0}"
          "th,td{border:1px solid #bbb;padding:4px 8px;text-align:left;vertical-align:top}"
          "th{background:#eee}")


def _render_html(title: str, summary: list[str], groups: dict[str, list[Entry]]) -> str:
    esc = html.escape
    out = ["<!DOCTYPE html>", '<html lang="en">', "<head>", '<meta charset="utf-8">',
           "<title>%s</title>" % esc(title), "<style>%s</style>" % _STYLE, "</head>", "<body>",
           "<h1>%s</h1>" % esc(title), "<p>%s</p>" % esc(" ".join(summary))]
    if not groups:
        out.append("<p>No entries in the period.</p>")
    else:
        out.append("<table>")
        out.append("<thead><tr><th>Group</th><th>Entries</th><th>Newest</th></tr></thead>")
        out.append("<tbody>")
        for k, rows in groups.items():
            out.append("<tr><td>%s</td><td>%d</td><td>%s</td></tr>" % (esc(k), len(rows), esc(rows[0].date)))
        out.append("</tbody>")
        out.append("</table>")
        for k, rows in groups.items():
            out.append("<h2>%s (%s)</h2>" % (esc(k), _entries_word(len(rows))))
            out.append("<table>")
            out.append("<thead><tr>%s</tr></thead>" % "".join("<th>%s</th>" % c for c in COLUMNS))
            out.append("<tbody>")
            for e in rows:
                out.append("<tr>%s</tr>" % "".join("<td>%s</td>" % esc(c) for c in _cells(e)))
            out.append("</tbody>")
            out.append("</table>")
    out += ["</body>", "</html>"]
    return "\n".join(out) + "\n"


def report(by: str, start=None, end=None, fmt: str = "md", p: config.Paths | None = None) -> tuple[str, int]:
    """Load, check once more and render. Returns the report text and the count of texts withheld."""
    p = p or config.paths()
    if start is not None and end is not None and parse_date(start) > parse_date(end):
        raise LedgerError("--from is after --to")
    entries = load(p, start, end)
    reg = register_path(p)
    if entries:
        ensure_checkable(reg)
    entries, withheld = withhold(entries, reg)
    return render(entries, by, fmt, start, end), withheld


# --------------------------------------------------------------------------- command line


def _exit_code(exc: SystemExit) -> int:
    if exc.code is None:
        return EXIT_OK
    return exc.code if isinstance(exc.code, int) else EXIT_ERROR


def _fail(prog: str, err: Exception) -> int:
    if isinstance(err, OSError):
        print("%s: %s (operating system error)" % (prog, type(err).__name__), file=sys.stderr)
        return EXIT_ERROR
    print("%s: %s" % (prog, err), file=sys.stderr)
    return EXIT_FINDINGS if isinstance(err, Refused) else EXIT_ERROR


def _range_args(ap) -> None:
    ap.add_argument("--from", dest="start", type=iso_date, default=None, metavar="D", help="first date (ISO)")
    ap.add_argument("--to", dest="end", type=iso_date, default=None, metavar="D", help="last date (ISO)")


def _cmd_add(args, p: config.Paths) -> int:
    e = add(args.kind, args.done, project=args.project, customer=args.customer, partner=args.partner,
            tags=args.tag, outcome=args.outcome, deliverables=args.deliverable, open_items=args.open,
            day=args.date, p=p)
    print("ledger: added one entry to %s (%s, project %s, customer %s, partner %s)"
          % (week_name(e.date), e.kind, e.project, e.customer, e.partner))
    return EXIT_OK


def _cmd_list(args, p: config.Paths) -> int:
    if args.start and args.end and args.start > args.end:
        raise LedgerError("--from is after --to")
    entries = load(p, args.start, args.end)
    if not entries:
        print("no entries")
        return EXIT_OK
    ensure_checkable(register_path(p))
    entries, withheld = withhold(entries, register_path(p))
    for e in entries:
        print("%s  %-8s  %-8s  %-9s  %-9s  %s" % (e.date, e.kind, e.project, e.customer, e.partner,
                                                  ",".join(e.tags) or NONE))
        print("    done: %s" % e.done)
        if e.outcome:
            print("    outcome: %s" % e.outcome)
        for d in e.deliverables:
            print("    deliverable: %s" % d)
        for o in e.open:
            print("    open: %s" % o)
    if withheld:
        print("ledger: %d texts withheld by the name check" % withheld, file=sys.stderr)
    return EXIT_OK


def _main_report(argv: list[str]) -> int:
    from awb.cli import SafeParser

    prog = "awb report"
    ap = SafeParser(prog=prog, description="A report from the ledger, grouped by a key. Codes only.")
    ap.add_argument("--by", required=True, choices=BY + ("questions",))
    _range_args(ap)
    ap.add_argument("--format", dest="fmt", choices=FORMATS, default="md")
    try:
        args = ap.parse_args(argv)
    except SystemExit as exc:
        return _exit_code(exc)
    if args.by == "questions":               # the monthly counts of the stop hook (T13), counts only
        from awb import questions
        sys.stdout.write(questions.render(config.paths()))
        return EXIT_OK
    try:
        text, withheld = report(args.by, args.start, args.end, args.fmt)
    except (LedgerError, OSError) as err:
        return _fail(prog, err)
    sys.stdout.write(text)
    if withheld:
        print("%s: %d texts withheld by the name check" % (prog, withheld), file=sys.stderr)
    return EXIT_OK


def main(argv: list[str] | None = None) -> int:
    """`awb ledger add|list ...` and `awb report ...` when argv[0] is "report"."""
    from awb.cli import SafeParser

    argv = list(sys.argv[1:] if argv is None else argv)
    if argv and argv[0] == "report":
        return _main_report(argv[1:])
    prog = "awb ledger"
    ap = SafeParser(prog=prog, description="The activity ledger. Codes only, every text is name-checked.")
    sub = ap.add_subparsers(dest="command", parser_class=SafeParser)
    s = sub.add_parser("add", help="add one entry")
    s.add_argument("--kind", required=True, choices=KINDS)
    s.add_argument("--done", required=True, help="what was done (one line)")
    s.add_argument("--project", default=None, help="a project code or none")
    s.add_argument("--customer", default=None, help="a CUST code or none")
    s.add_argument("--partner", default=None, help="a PART code or none")
    s.add_argument("--tag", action="append", default=[], help="a tag of rules/tags.txt (repeat for more)")
    s.add_argument("--outcome", default="", help="the outcome (one line)")
    s.add_argument("--deliverable", action="append", default=[], metavar="P", help="a deliverable path")
    s.add_argument("--open", action="append", default=[], metavar="TEXT", help="an open item")
    s.add_argument("--date", type=iso_date, default=None, metavar="D", help="the date (ISO, default today)")
    s.set_defaults(func=_cmd_add)
    s = sub.add_parser("list", help="list entries")
    _range_args(s)
    s.set_defaults(func=_cmd_list)
    try:
        args = ap.parse_args(argv)
    except SystemExit as exc:
        return _exit_code(exc)
    func = getattr(args, "func", None)
    if func is None:
        ap.print_usage(sys.stderr)
        return EXIT_ERROR
    try:
        return func(args, config.paths())
    except (LedgerError, OSError) as err:
        return _fail(prog, err)


if __name__ == "__main__":
    sys.exit(main())
