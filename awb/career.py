"""The career log: outcomes worth a CV line and the update drafted from them every few months.

    awb career add --title T --role R --stack S --outcome O --cv-line L [--tag T]... [--date D]
    awb career update [--since D]
    awb career due
    awb career feed [--since DATE] [--out FILE]

`<shared>/career.jsonl` holds one JSON object per line with the fields of `FIELDS`, appended under an exclusive
lock like the ledger. A career entry names no customer at all; the texts are kept as written (no name, data,
code or secret check inside) and the candidates from the ledger show a register code as "a customer" and a project
code as "a project". Tags come from `rules/tags.txt`; without --tag they are taken from the words of the stack that are tags.

`awb career update` prints a Markdown draft: CV bullets grouped by theme (each entry goes to the first theme of
`THEMES` that one of its tags belongs to, else to "Other"), newest first inside a theme, then one LinkedIn
paragraph. The draft is written in plain style (no em-dash, no comma before "and" or "or") and then run through
`writing.check_text`; the tells go to standard error and a blocking tell gives exit 1. When `awb.writing` is not
there the step is skipped with a note. The date of the run goes to `<shared>/career-updated`.

`awb career due` prints the days since the last update and exits 1 when that is over 90. Before the first update
it counts from the oldest entry; with no update and no entry nothing is due.

`awb career feed` (B-115), run as the work user once a week, writes cv-evidence-DATE.md for the career project (default
`<shared>/career-feed/`): "Changes since the last feed" on top (new ids, changed statuses, changed counts, against
`<shared>/career-feed/state.json`), then one record per item: the entries of one project of the ledger, one ledger
entry without a project, the newest built release of the public dataset (its STATS.json), the repository (STATS.json
at its root) and the build queue. A record carries the fields of `RECORD_FIELDS`, its id stays the same from feed to
feed (`ev-NNN`, the item behind it kept in the state file only). Codes, tenants, addresses, host names and amounts are
written as words ("a customer", "a project", "a tenant", "an address", "an amount"); the file then passes the exit
check (`exit_check`: the name check, secrets, keys, tokens, home paths, the blocklist, a code left); a finding stops
it with the record and the field named (exit 1, nothing written, the state kept). The numbers of a record are never
summed; a count whose method changed is named as such, never shown as growth. There is no LinkedIn line.

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

_STACK_SPLIT_RE = re.compile(r"[,;/]|\band\b|\bor\b", re.IGNORECASE)


class CareerError(Exception):
    """A usage or data problem. The message never carries a text or a refused value."""


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
    except ledger.LedgerError as err:
        raise CareerError(str(err)) from None


def make_entry(*, title: str, role: str, stack: str, outcome: str, cv_line: str, tags=(), day=None,
               known_tags: list[str] | None = None) -> Entry:
    """A validated entry. Shapes only."""
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


def add(title: str, role: str, stack: str, outcome: str, cv_line: str, *, tags=(), day=None,
        p: config.Paths | None = None) -> Entry:
    """Validate and append one career entry. Raises CareerError."""
    p = p or config.paths()
    e = make_entry(title=title, role=role, stack=stack, outcome=outcome, cv_line=cv_line, tags=tags, day=day,
                   known_tags=_wrap(ledger.load_tags))
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


DATASET_STATS = "STATS.json"
"""The file of the numbers of a built dataset release (awb/tcp/dataset.py STATS_FILE); named here so this core
module, which the hooks load, imports nothing of the platform package."""
DATASET_REPO = "shimoza/tcp-facts"
"""The public repository of the dataset (awb/tcp/dataset.py GITHUB_REPO)."""


def _build_module():
    """awb.build for the queue record, loaded only when the feed runs: a static import would pull the build modules
    into every unit that loads the hooks (awb/deploy.py)."""
    try:
        return importlib.import_module("awb.build")
    except Exception:
        return None


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


def _write_stamp(p: config.Paths, day: date) -> None:
    target = stamp_file(p)
    target.parent.mkdir(parents=True, exist_ok=True)
    _write_text(target, day.isoformat() + "\n")


def _write_text(target: Path, text: str) -> None:
    """Write `text` to `target` whole (a temporary file beside it, then a rename), mode 640."""
    fd, tmp = tempfile.mkstemp(prefix=".%s." % target.name, suffix=".tmp", dir=target.parent)
    try:
        os.fchmod(fd, 0o640)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
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
    text = draft(entries, run, since_d)
    result = Update(draft=text, entries=len(entries))
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
    return codes.PROJECT_RE.sub("a project", codes.PLACEHOLDER_RE.sub("a customer", text))


def ledger_candidates(p: config.Paths, since: date | None) -> str:
    """T-46: the ledger entries since `since` that may be worth a career line, as written with the codes masked.
    Input for him, not a part of the checked draft."""
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


# --------------------------------------------------------------------------- the weekly feed (B-115)


FEED_DIR = "career-feed"
FEED_STATE = "state.json"
FEED_NAME = "cv-evidence-%s.md"
REPO_ROOT = Path(__file__).resolve().parent.parent
PUBLIC_REPO = "shimoza/tcp-awb"
"""The public repository of the Workbench; its STATS.json is the one at REPO_ROOT."""

STATUSES = ("production", "PoC", "research", "proposal", "planned", "cancelled", "ongoing")
ROLES = ("led", "built", "advised", "trained", "reviewed")
SCOPES = ("customer", "partner", "internal", "tool")
KIND_STATUS = {"proof": "PoC", "tender": "proposal", "pricing": "proposal", "inquiry": "research",
               "training": "production", "tooling": "production", "code": "ongoing", "other": "ongoing"}
"""The status of a ledger item from the kind of its newest entry that is not "other", unless the Status line of the
project's STATE.md starts with a status word."""
KIND_ROLE = {"proof": "built", "code": "built", "tooling": "built", "training": "trained", "pricing": "advised",
             "tender": "advised", "inquiry": "advised", "other": "advised"}
SCOPE_WORDS = {"customer": "customer work", "partner": "partner work", "internal": "internal", "tool": "own tool"}
RECORD_FIELDS = ("answers", "claim", "status", "period", "numbers", "evidence", "measured", "cv-line", "note", "id",
                 "verify", "scope", "sector", "role", "changed", "counted-by", "target")
TARGETS = {1: "trainings and workshops", 2: "colleagues delivering with AI and playbooks handed over",
           3: "pricing analyses verified against sources and tooling", 4: "security and compliance engagements",
           5: "tender analyses"}
_SECURITY_TAGS = frozenset(("security", "iam", "kms", "waf", "cfw", "identity", "hss"))
_PRICING_TAGS = frozenset(("pricing", "ms-licensing"))
_HANDOVER_RE = re.compile(r"(?i)\b(?:playbooks?|hand-?overs?|handed over)\b")
_NOT_NEUTRAL = frozenset((
    "successful", "successfully", "great", "excellent", "significant", "significantly", "robust", "seamless",
    "seamlessly", "innovative", "strong", "powerful", "comprehensive", "complex", "major", "key", "important",
    "unique", "best", "huge", "massive", "efficient", "efficiently", "modern", "advanced", "outstanding",
    "remarkable", "impressive", "amazing", "incredible", "awesome", "revolutionary", "cutting-edge", "world-class",
    "state-of-the-art", "effortless", "effortlessly", "perfect", "ideal", "unparalleled", "unmatched"))
"""Words of praise a cv-line leaves out: the draft states, it does not rate."""
_IPV4_RE = re.compile(r"(?<![\w.])\d{1,3}(?:\.\d{1,3}){3}(?![\w.])")
_HOST_RE = re.compile(r"(?i)(?<![\w.@/-])(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+([a-z]{2,24})(?![\w-])")
_FILE_SUFFIXES = frozenset(("md", "py", "json", "jsonl", "txt", "csv", "tsv", "pdf", "docx", "xlsx", "pptx", "html",
                            "sh", "ps1", "yaml", "yml", "tf", "zip", "png", "svg", "js", "ts", "sql", "xml", "log"))
"""A dotted word that ends in one of these is a file name, not a host name."""
_BREAK_RE = re.compile(r"[\r\n\t]+")


class FeedStopped(CareerError):
    """The exit check found something in a field: the file is not written. The message names the record, the field
    and the classes, never the text."""


@dataclass
class Number:
    name: str
    value: int
    counted_by: str


@dataclass
class Record:
    key: str                       # the item behind the record, kept in the state file only (it may carry a code)
    claim: str
    status: str
    period: str
    numbers: list[Number]
    evidence: str
    measured: str                  # the date of the numbers
    weekly: bool
    note: str
    verify: str
    scope: str
    sector: str
    role: str
    targets: list[int]
    changed_on: str                # the date a status change took place
    id: str = ""
    changed: str = "new"

    def fields(self) -> list[tuple[str, str]]:
        """(name, value) in the order of RECORD_FIELDS, each value one line."""
        values = {
            "answers": "feed",
            "claim": self.claim,
            "status": self.status,
            "period": self.period,
            "numbers": "; ".join("%s %d" % (n.name, n.value) for n in self.numbers) or "none",
            "evidence": self.evidence,
            "measured": self.measured_line(),
            "cv-line": self.cv_line(),
            "note": self.note or "none",
            "id": self.id,
            "verify": self.verify,
            "scope": self.scope,
            "sector": self.sector,
            "role": self.role,
            "changed": self.changed,
            "counted-by": "; ".join("%s: %s" % (n.name, n.counted_by) for n in self.numbers) or "none",
            "target": ", ".join(str(t) for t in self.targets) or "none",
        }
        return [(k, _one_line(values[k])) for k in RECORD_FIELDS]

    def measured_line(self) -> str:
        if not self.weekly:
            return "%s, changes weekly no" % self.measured
        form = ", ".join("%s %s" % (cv_number(n.value), n.name) for n in self.numbers)
        return "%s, changes weekly yes, CV form: %s (as of %s)" % (self.measured, form, self.measured)

    def cv_line(self) -> str:
        """A neutral draft: the role, the claim without words of praise, the scope, the status and the month."""
        claim = " ".join(w for w in self.claim.rstrip(".").split() if w.lower().strip(",;:") not in _NOT_NEUTRAL)
        if claim[:1].isupper() and not claim[1:2].isupper():
            claim = claim[:1].lower() + claim[1:]
        if claim.split(" ", 1)[0] != self.role:
            claim = "%s: %s" % (self.role, claim)
        return "%s (%s, %s, %s)." % (claim[:1].upper() + claim[1:], SCOPE_WORDS[self.scope], self.status,
                                     self.period[:7])

    def text(self) -> str:
        return "## %s\n" % self.id + "".join("%s: %s\n" % (k, v) for k, v in self.fields())


def _one_line(text: str) -> str:
    return " ".join(_BREAK_RE.sub(" ", text).split())


def cv_number(n: int) -> str:
    """The CV form of a number that changes every week: two significant digits, rounded down ("over 940")."""
    if n < 100:
        return str(n)
    unit = 10 ** (len(str(n)) - 2)
    floor = n // unit * unit
    return "over %d" % floor if floor < n else str(n)


def feed_dir(p: config.Paths) -> Path:
    return p.shared / FEED_DIR


def feed_state_file(p: config.Paths) -> Path:
    return feed_dir(p) / FEED_STATE


def scrub(text: str) -> str:
    """A ledger text for the feed: a register code becomes "a customer", a project code "a project", a tenant "a
    tenant", an IP address, a web address or a host name "an address", a mail address "a mail address", a price or an
    amount "an amount", another structured number "a number". Plain style, one line."""
    from awb import codes, kb, patterns

    text = codes.TENANT_RE.sub("a tenant", masked(ledger.plain(text)))
    for s in sorted(patterns.find_structured(text), key=lambda s: s.start, reverse=True):
        word = {"mail": "a mail address", "url": "an address", "ip": "an address"}.get(s.cls, "a number")
        text = text[:s.start] + word + text[s.end:]
    text = _IPV4_RE.sub("an address", text)
    text = _HOST_RE.sub(lambda m: m.group(0) if m.group(1).lower() in _FILE_SUFFIXES else "an address", text)
    text = kb._PRICE_RE.sub("an amount", text)
    return _one_line(text)


def _state_line(folder: Path, key: str) -> str:
    """The text after `key:` on the first such line of the project's STATE.md, empty when there is none."""
    try:
        text = (folder / "STATE.md").read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return ""
    for line in text.splitlines():
        if line.lower().startswith(key.lower() + ":"):
            return " ".join(line[len(key) + 1:].split())
    return ""


def _project_folders(p: config.Paths) -> dict[str, Path]:
    from awb import projects

    try:
        return {pr.code: Path(pr.path) for pr in projects.load(p)}
    except Exception:  # no project register or one that cannot be read: no sector and no status recorded
        return {}


def _item_key(e: ledger.Entry) -> str:
    """The item an entry belongs to: its project, else the entry itself."""
    import hashlib

    if e.project != ledger.NONE:
        return "project:" + e.project
    return "entry:%s:%s:%s" % (e.date, e.kind, hashlib.sha256(e.done.encode("utf-8")).hexdigest()[:12])


def _week(day: str) -> str:
    y, w, _ = date.fromisoformat(day).isocalendar()
    return "%d-W%02d" % (y, w)


def ledger_records(p: config.Paths, since: date | None, day: date) -> list[Record]:
    """One record per item of the ledger: the entries of one project or one entry without a project."""
    groups: dict[str, list[ledger.Entry]] = {}
    for e in ledger.load(p, start=since, end=day):
        groups.setdefault(_item_key(e), []).append(e)
    folders = _project_folders(p)
    out = []
    for key, entries in groups.items():
        entries.sort(key=lambda e: e.date)
        newest = entries[-1]
        kinds = [e.kind for e in entries]
        tags = {t for e in entries for t in e.tags}
        folder = folders.get(newest.project) if newest.project != ledger.NONE else None
        status = ""
        if folder is not None:
            words = _state_line(folder, "Status").split()
            first = words[0].strip(".,;:").lower() if words else ""
            status = next((s for s in STATUSES if s.lower() == first), "")
        if not status:
            told = [e for e in entries if e.kind != "other"]
            status = KIND_STATUS[told[-1].kind if told else "other"]
        sector = _state_line(folder, "Sector") if folder is not None else ""
        if any(e.customer != ledger.NONE for e in entries):
            scope = "customer"
        elif any(e.partner != ledger.NONE for e in entries):
            scope = "partner"
        elif newest.kind in ("tooling", "code"):
            scope = "tool"
        else:
            scope = "internal"
        verb = (newest.done.split() or [""])[0].lower()
        role = ("reviewed" if verb.startswith("review") else "led" if verb in ("led", "lead", "leads")
                else KIND_ROLE[newest.kind])
        texts = [t for e in entries for t in [e.done, e.outcome] + e.deliverables]
        targets = set()
        if "training" in kinds:
            targets.add(1)
        if any(_HANDOVER_RE.search(t) for t in texts):
            targets.add(2)
        if "pricing" in kinds or tags & _PRICING_TAGS:
            targets.add(3)
        if tags & _SECURITY_TAGS:
            targets.add(4)
        if "tender" in kinds or "tender" in tags:
            targets.add(5)
        first, last = entries[0].date, newest.date
        out.append(Record(
            key=key,
            claim=scrub(newest.done),
            status=status,
            period=first if first == last else "%s to %s" % (first, last),
            numbers=[Number("ledger entries", len(entries),
                            "the lines of this item in the week files of the ledger (awb ledger list)"),
                     Number("deliverables", sum(len(e.deliverables) for e in entries),
                            "the deliverables named on those lines")],
            evidence="ledger weeks %s, kinds %s" % (", ".join(sorted({_week(e.date) for e in entries})),
                                                    ", ".join(sorted(set(kinds)))),
            measured=day.isoformat(),
            weekly=False,
            note=scrub(newest.outcome),
            verify="ask",
            scope=scope,
            sector=scrub(sector) if sector else "not recorded",
            role=role,
            targets=sorted(targets),
            changed_on=newest.date,
        ))
    out.sort(key=lambda r: (r.period[:10], r.key))
    return out


def _read_json(path: Path) -> dict | None:
    try:
        obj = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, ValueError):
        return None
    return obj if isinstance(obj, dict) else None


def _stats_numbers(stats: dict, names: tuple[str, ...]) -> list[Number]:
    """The numbers of a STATS.json, each with its counted_by; a number without one is left out."""
    out = []
    for name in names:
        item = stats.get(name)
        if (isinstance(item, dict) and type(item.get("value")) is int and isinstance(item.get("counted_by"), str)
                and item["counted_by"].strip()):
            out.append(Number(name.replace("_", " "), item["value"], item["counted_by"]))
    return out


def _stats_date(stats: dict, name: str, day: date) -> str:
    item = stats.get(name)
    value = item.get("value") if isinstance(item, dict) else None
    return value if isinstance(value, str) and ledger._DATE_RE.fullmatch(value) else day.isoformat()


def _raw_check(repo: str, key: str, value: int) -> str:
    return ("gh api -H 'Accept: application/vnd.github.raw' repos/%s/contents/STATS.json --jq .%s.value, expected %d"
            % (repo, key, value))


def dataset_record(p: config.Paths, day: date) -> Record | None:
    """The public dataset: STATS.json of its newest built release."""
    from awb import kbreview

    folder = kbreview.latest(p)
    stats = _read_json(folder / DATASET_STATS) if folder is not None else None
    numbers = _stats_numbers(stats, ("facts", "facts_tested_live", "services_orderable")) if stats else []
    if not numbers:
        return None
    released = _stats_date(stats, "released", day)
    return Record(
        key="dataset", claim="Built the public dataset of checked facts on T Cloud Public, one release a week",
        status="production", period=released, numbers=numbers,
        evidence="STATS.json of the release of %s in %s" % (released, DATASET_REPO), measured=released,
        weekly=True, note="", verify=_raw_check(DATASET_REPO, "facts", numbers[0].value), scope="tool",
        sector="not recorded", role="built", targets=[], changed_on=day.isoformat())


def repository_record(repo: Path | None, day: date) -> Record | None:
    """The Workbench itself: STATS.json at the root of its repository."""
    stats = _read_json(Path(repo) / "STATS.json") if repo is not None else None
    numbers = _stats_numbers(stats, ("commits", "commands", "modules", "test_functions")) if stats else []
    if not numbers:
        return None
    measured = _stats_date(stats, "date", day)
    return Record(
        key="repository", claim="Built the Architect Workbench, a command line tool for cloud architecture work "
                                "with an entry check and an exit check of customer data",
        status="production", period=measured, numbers=numbers,
        evidence="STATS.json of %s of %s" % (PUBLIC_REPO, measured), measured=measured, weekly=True, note="",
        verify=_raw_check(PUBLIC_REPO, numbers[0].name.replace(" ", "_"), numbers[0].value), scope="tool",
        sector="not recorded", role="built", targets=[], changed_on=day.isoformat())


def queue_record(day: date, qdir: Path | None = None, runs: Path | None = None) -> Record | None:
    """The build queue: its tasks whose last run is done and the others. None without a queue this user can read."""
    build = _build_module()
    if build is None:
        return None
    try:
        tasks, _ = build.read_queue(qdir)
    except OSError:
        return None
    if not tasks:
        return None
    done = 0
    for t in tasks:
        r = build.latest(t.id, runs)
        done += r is not None and r.state == "done"
    how = "tasks of build/queue/QUEUE.md whose last run %s (awb build list)"
    return Record(
        key="build-queue", claim="Built the Workbench through a queue of tasks that an assistant builds and a "
                                 "runner checks with the whole test suite",
        status="ongoing", period=day.isoformat(),
        numbers=[Number("tasks done", done, how % "is done"),
                 Number("tasks not done", len(tasks) - done, how % "is not done or that have no run")],
        evidence="the build queue and the records of its runner", measured=day.isoformat(), weekly=True, note="",
        verify="ask", scope="tool", sector="not recorded", role="built", targets=[], changed_on=day.isoformat())


def _load_state(p: config.Paths) -> dict:
    state = _read_json(feed_state_file(p)) or {}
    return {"day": state["day"] if isinstance(state.get("day"), str) else "",
            "ids": dict(state["ids"]) if isinstance(state.get("ids"), dict) else {},
            "records": dict(state["records"]) if isinstance(state.get("records"), dict) else {},
            "next": max(state["next"], 1) if type(state.get("next")) is int else 1}


def compare(records: list[Record], state: dict) -> list[str]:
    """Give every record its stable id and its `changed` and return the lines of "Changes since the last feed": the
    new ids, the changed statuses and the changed counts. A count is compared only with the count of the same name
    and the same method; one counted by another method now is named so and never shown as growth."""
    new: list[str] = []
    lines: list[str] = []
    for r in records:
        rid = state["ids"].get(r.key)
        if not isinstance(rid, str):
            rid = "ev-%03d" % state["next"]
            state["next"] += 1
            state["ids"][r.key] = rid
        r.id = rid
        before = state["records"].get(rid)
        if not isinstance(before, dict):
            r.changed = "new"
            new.append(rid)
            continue
        old_numbers = before.get("numbers") if isinstance(before.get("numbers"), dict) else {}
        counts = []
        for n in r.numbers:
            old = old_numbers.get(n.name)
            if not (isinstance(old, list) and len(old) == 2):
                continue
            if old[1] != n.counted_by:
                counts.append("- %s: %s is counted by another method now, the two numbers are not compared"
                              % (rid, n.name))
            elif old[0] != n.value:
                counts.append("- %s: %s from %s to %d, counted the same way" % (rid, n.name, old[0], n.value))
        old_status = before.get("status")
        if isinstance(old_status, str) and old_status != r.status:
            r.changed = "%s, was %s" % (r.changed_on, old_status)
            lines.append("- %s: status from %s to %s on %s" % (rid, old_status, r.status, r.changed_on))
        elif counts:
            r.changed = "%s, numbers changed, status unchanged" % r.measured
        else:
            r.changed = "no"
        lines.extend(counts)
    return (["- new: %s" % ", ".join(new)] if new else []) + lines or ["- no change"]


def _feed_text(day: date, last: str, changes: list[str], records: list[Record]) -> str:
    out = ["# CV evidence %s" % day.isoformat(), "",
           "The weekly evidence of the Architect Workbench for the career project, from the ledger, the build queue, "
           "the public dataset release and the repository. One record per item, its id the same in every feed. The "
           "numbers of a record are counted each on its own and never summed.", "",
           "## Changes since the last feed", "",
           "Last feed: %s." % (last or "none, every id is new"), ""]
    out += changes + [""]
    out += [r.text() for r in records]
    return "\n".join(out).rstrip("\n") + "\n"


def exit_check(items: list[tuple[str, str, str]], reg: Path | None) -> None:
    """The exit check of the feed over (where, field, text): the name check (a registered name), a secret, a private
    key, a token, a home path, a word of the blocklist, a code left over. Raises FeedStopped at the first finding with
    the record, the field and the classes, CareerError when the check cannot run."""
    from awb import codes

    try:
        named = ledger.hit_indexes([t for _, _, t in items], reg)
        detectors = ledger._detectors()
    except ledger.LedgerError as err:
        raise CareerError("exit check: %s, the file is not written" % err) from None
    for i, (where, name, text) in enumerate(items):
        found = ["name"] if i in named else []
        for cls in sorted(detectors):
            try:
                hit = detectors[cls](text)
            except Exception:  # a detector that fails stops the file: never a text it could not check
                hit = [1]
            if hit:
                found.append(cls)
        if any(codes.count_codes(text)):
            found.append("code")
        if found:
            raise FeedStopped("the file is not written: %s, field %s: %s" % (where, name, ", ".join(found)))


@dataclass
class Feed:
    path: Path
    records: int
    changes: list[str]


def feed(p: config.Paths | None = None, since=None, out=None, day=None, repo: Path | None = REPO_ROOT,
         qdir: Path | None = None, runs: Path | None = None) -> Feed:
    """Write cv-evidence-DAY.md (default in <shared>/career-feed/, else `out`) and keep the state of this feed for
    the next one. Raises FeedStopped when the exit check finds something: nothing written, the state as it was."""
    p = p or config.paths()
    run = ledger.parse_date(day) if day is not None else today()
    since_d = ledger.parse_date(since) if since is not None else None
    records = ledger_records(p, since_d, run)
    records += [r for r in (dataset_record(p, run), repository_record(repo, run), queue_record(run, qdir, runs)) if r]
    state = _load_state(p)
    changes = compare(records, state)
    text = _feed_text(run, state["day"], changes, records)
    items = [("the changes", "text", line) for line in text.split("\n## ev-", 1)[0].splitlines() if line.strip()]
    items += [("record %s" % r.id, k, v) for r in records for k, v in r.fields()]
    exit_check(items, ledger.register_path(p))
    target = Path(out) if out is not None else feed_dir(p) / (FEED_NAME % run.isoformat())
    if out is None:
        target.parent.mkdir(parents=True, exist_ok=True)
    _write_text(target, text)
    state["day"] = run.isoformat()
    state["records"].update({r.id: {"status": r.status, "numbers": {n.name: [n.value, n.counted_by]
                                                                    for n in r.numbers}} for r in records})
    feed_dir(p).mkdir(parents=True, exist_ok=True)
    _write_text(feed_state_file(p), json.dumps(state, indent=1, sort_keys=True) + "\n")
    return Feed(target, len(records), changes)


# --------------------------------------------------------------------------- command line


def _fail(err: Exception) -> int:
    if isinstance(err, OSError):
        print("awb career: %s (operating system error)" % type(err).__name__, file=sys.stderr)
        return EXIT_ERROR
    print("awb career: %s" % err, file=sys.stderr)
    return EXIT_ERROR


def _cmd_add(args, p: config.Paths) -> int:
    e = add(args.title, args.role, args.stack, args.outcome, args.cv_line, tags=args.tag, day=args.date, p=p)
    print("career: added one entry of %s (theme %s)" % (e.date, theme_of(e.tags)))
    return EXIT_OK


def _cmd_update(args, p: config.Paths) -> int:
    res = update(p, since=args.since)
    sys.stdout.write(res.draft)
    if res.candidates:
        sys.stderr.write("\n" + res.candidates)
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


def _cmd_feed(args, p: config.Paths) -> int:
    try:
        res = feed(p, since=args.since, out=args.out, repo=REPO_ROOT)
    except FeedStopped as err:
        print("awb career feed: %s" % err, file=sys.stderr)
        return EXIT_FINDINGS
    print("career: feed written to %s, %d records, %d lines of changes" % (res.path, res.records, len(res.changes)))
    return EXIT_OK


def main(argv: list[str] | None = None) -> int:
    """`awb career add|update|due|feed`."""
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
    s = sub.add_parser("feed", help="the weekly evidence file cv-evidence-DATE.md for the career project, as the work "
                                    "user")
    s.add_argument("--since", type=ledger.iso_date, default=None, metavar="DATE",
                   help="first date of the ledger entries (ISO, default all)")
    s.add_argument("--out", default=None, metavar="FILE",
                   help="where to write the file (default <shared>/%s/%s)" % (FEED_DIR, FEED_NAME % "DATE"))
    s.set_defaults(func=_cmd_feed)
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
