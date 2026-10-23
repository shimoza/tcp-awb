"""The name check learns from its false positives (task FP), on the owner's side and without his marking anything.

Collect. Every block of the prompt hook, the commit gate, `awb check` and `awb import` is recorded: the time, the
door (source), the rule that fired, the class and, for the classes a learned exception may relax, the blocked
string and the word before it. The string is written only on the owner's side, inside the vault folder:

    <vault>/log/fp/blocks-YYYY-MM.tsv    one line per blocked item, 0600 in a 0700 folder, read by the owner alone

A process that reads the register here (the owner) writes the line itself. Any other process (the work user) sends
the text to the vault daemon with its check request (`record`), and the daemon scans it again and writes what it
found: the work user never names the string of a line and never writes one. Work-user logs, ledgers, hook messages
and the daemon's check log carry the count and the class only, as before. A registered form and a secret class are
recorded without their string ("-").

Learn (`awb check learn`, the owner's nightly timer awb-check-learn.timer). A blocked string of a LEARNABLE class
becomes "learned harmless" when it stands in the public corpus (the mirrors under AWB_MIRRORS or ~/tcp-mirrors: the
docs, the service description and the release notes; the knowledge base; rules/tech-terms.txt and
rules/common-words.txt) or when the whole string matches a rule of LOG_RULES (host names and FQDNs, ids and hashes,
error codes, HTTP status lines, file paths, timestamps, service names of rules/services.tsv). It stays blocked when
a word of it is a first name of rules/first-names.txt, when it stands after a title or a salutation, when it carries
a legal form, when the register finds a form in it, or when it looks like a person's name by the person rules of
the intake. The learned strings go to

    <vault>/log/fp/allow.tsv             class, string, reason, date: the allow list the check reads

never to a file of rules/ in git. An entry relaxes one exact string of one class: a hit of the check whose whole
matched text equals the entry, nothing around it, never the name class, never a mail, a bank, register or tax
number, never a secret. Every entry is therefore narrower than the rule it relaxes.

Safety gate. Before the allow list changes, the planted names of awb/planted.py and every value of the red team
wipe pack (calibration/redteam/wipe, 405 cases) run through the check under the old list and under the new one,
alone and glued into host names. When one planted value that was blocked before passes under the new list, the
change is dropped and the report says so. The pack not found drops the change too: a gate that cannot run never
lets a change through. First names are never learned.

Report. Once a week one line goes to the owner by mail and into <vault>/log/fp/weekly.txt: blocks by door, learned,
kept blocked by class. `awb check allow WORD` (the owner's terminal only) releases a word by hand, through the same
safety gate; `awb check allow --list` shows him the list. Nothing here prints a blocked string except --list.
"""
from __future__ import annotations

import contextlib
import datetime
import os
import re
import secrets as _secrets
import sys
from collections import Counter
from pathlib import Path

from awb import config

FOLDER = "fp"
BLOCKS_HEAD = "time\tevent\tsource\trule\tclass\tbefore\tstring\n"
ALLOW_HEAD = ("# The learned allow list of the name check (awb check learn, awb check allow). One exact string of one "
              "class per line.\nclass\tstring\treason\tdate\n")
SOURCES = ("prompt", "gate", "check", "import", "kb")
LEARNABLE = ("url", "ip", "mac", "phone")
"""The classes a learned exception may relax. A registered form (name), a mail address, bank data, a register or
tax number and every secret class are never relaxed."""
ANY = "*"
"""The class of a word the owner released by hand: it applies to every LEARNABLE class, still one exact string."""
MAX_STRING = 300
MAX_ITEMS = 200
"""Lines of one recorded block at most: a flood of hits is counted, not written out."""
MAX_MONTH_BYTES = 32 * 1024 * 1024
"""A month file that reached this size takes no more lines: the work user cannot fill the owner's disk."""
LOOKBACK_DAYS = 35
KEEP_DAYS = 120
"""Block files older than this are removed by the nightly run."""
WEEK_DAYS = 7
CLASS_RE = re.compile(r"[a-z][a-z-]{0,39}")
_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f\u2028\u2029]")


class LearnError(Exception):
    """The learning cannot run. The message carries no blocked string."""


def folder(vault: Path) -> Path:
    """<vault>/log/fp: beside the check log, the one place of the vault where plain files are kept by design."""
    return Path(vault) / "log" / FOLDER


def _clean(s: str) -> str:
    return _CONTROL_RE.sub(" ", s)[:MAX_STRING]


def _now() -> datetime.datetime:
    return datetime.datetime.now(datetime.timezone.utc)


def _stamp(now: datetime.datetime) -> str:
    return now.strftime("%Y-%m-%dT%H:%M:%SZ")


# --------------------------------------------------------------------------- collect


def _before(text: str, start: int) -> str:
    """The word right before `start` (letters only, at most 40), the place a title or a salutation stands."""
    m = re.search(r"([^\W\d_]{1,40})[.,:]?[ \t]+$", text[max(0, start - 60):start])
    return m.group(1) if m else ""


def items_of(text: str | None, hits, extra=None) -> list[tuple[str, str, str, str]]:
    """(rule, class, before, string) per blocked item. `text` is the text the check scanned (positions of `hits`
    point into its normalised form with its bidi views appended, as check._scan reads it). The string is kept for a
    LEARNABLE class alone; `extra` maps further classes (a secret, a gate detector) to their counts, no string."""
    from awb import check, normalize

    out: list[tuple[str, str, str, str]] = []
    n = ""
    if text:
        views = check._bidi_views(text)
        n = normalize.normalize(text + "".join("\n\n" + v for v in views)).text
    for h in hits or ():
        if not isinstance(h, dict):
            continue
        cls = str(h.get("cls", "unknown"))
        if not CLASS_RE.fullmatch(cls):
            cls = "unknown"
        start, length = h.get("start"), h.get("length")
        s = before = ""
        if cls in LEARNABLE and isinstance(start, int) and isinstance(length, int) and length > 0 \
                and 0 <= start and start + length <= len(n):
            s = _clean(n[start:start + length])
            before = _before(n, start)
        out.append(("register" if cls == "name" else "pattern", cls, before, s or "-"))
    for cls, count in sorted((extra or {}).items()):
        if isinstance(cls, str) and CLASS_RE.fullmatch(cls) and isinstance(count, int) and count > 0:
            out.extend((cls, cls, "", "-") for _ in range(min(count, MAX_ITEMS)))
    return out[:MAX_ITEMS]


def write_items(vault: Path, source: str, items, now: datetime.datetime | None = None) -> int:
    """Append the items of one block to this month's file of the vault's fp folder. Returns the lines written. The
    folder is made only inside a vault folder that exists."""
    if source not in SOURCES or not items:
        return 0
    vault = Path(vault)
    if not vault.is_dir():
        return 0
    now = now or _now()
    d = folder(vault)
    config.make_dir(d.parent, 0o700)
    config.make_dir(d, 0o700)
    f = d / ("blocks-%s.tsv" % now.strftime("%Y-%m"))
    event = _secrets.token_hex(4)
    body = "".join("%s\t%s\t%s\t%s\t%s\t%s\t%s\n" % (_stamp(now), event, source, _clean(rule), _clean(cls),
                                                    _clean(before) or "-", _clean(s) or "-")
                   for rule, cls, before, s in items)
    fd = os.open(f, os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        size = os.fstat(fd).st_size
        if size >= MAX_MONTH_BYTES:
            return 0
        if size == 0:
            os.write(fd, BLOCKS_HEAD.encode("utf-8"))
        os.write(fd, body.encode("utf-8"))
    finally:
        os.close(fd)
    return len(items)


def record(source: str, text: str | None = None, hits=None, extra=None, p: config.Paths | None = None) -> None:
    """Record one block. Never raises and never prints: a record that fails costs the learning one line, never the
    block itself. Where the register is read here (the owner) the line is written here; else the text goes to the
    vault daemon, which scans it again and writes the line on the owner's side."""
    try:
        from awb import check

        p = p or config.paths()
        kind, _ = check.register_source(p.register)
        if kind in (check.LOCAL, check.MISSING):
            write_items(p.vault, source, items_of(text, hits, extra))
            return
        from awb import vault_client

        payload = text if isinstance(text, str) and len(text) <= vault_client.CHECK_CHUNK else ""
        vault_client.record_remote(payload, p.check_socket, source, _extra_counts(extra, hits, bool(payload)))
    except Exception:
        return


def _extra_counts(extra, hits, sent: bool) -> dict[str, int]:
    """What the daemon cannot find again on its own: the extra classes, and the classes of the hits when the text
    was too long to send."""
    out = Counter({k: v for k, v in (extra or {}).items() if isinstance(k, str) and isinstance(v, int)})
    if not sent:
        out.update(str(h.get("cls")) for h in hits or () if isinstance(h, dict))
    return dict(out)


def valid_extra(raw) -> dict[str, int]:
    """The extra counts of a request to the daemon, checked: class words of CLASS_RE, counts up to MAX_ITEMS."""
    if not isinstance(raw, dict):
        return {}
    return {k: min(v, MAX_ITEMS) for k, v in list(raw.items())[:32]
            if isinstance(k, str) and CLASS_RE.fullmatch(k) and isinstance(v, int) and not isinstance(v, bool) and v > 0}


# --------------------------------------------------------------------------- the allow list

_allow_cache: dict[Path, tuple[tuple, frozenset]] = {}


def read_allow(vault: Path) -> list[tuple[str, str, str, str]]:
    """(class, string, reason, date) per entry of the allow list; empty when there is none or it cannot be read."""
    try:
        lines = (folder(vault) / "allow.tsv").read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeDecodeError):
        return []
    out = []
    for line in lines:
        if not line or line.startswith("#") or line.startswith("class\t"):
            continue
        cells = line.split("\t")
        if len(cells) >= 2 and (cells[0] in LEARNABLE or cells[0] == ANY) and cells[1]:
            out.append((cells[0], cells[1], cells[2] if len(cells) > 2 else "", cells[3] if len(cells) > 3 else ""))
    return out


def load_allow(vault: Path | None) -> frozenset:
    """The allow list as a set of (class, string), read again when the file changes. Empty when there is none or it
    cannot be read: an unreadable list relaxes nothing."""
    if vault is None:
        return frozenset()
    f = folder(vault) / "allow.tsv"
    try:
        st = os.stat(f)
    except OSError:
        return frozenset()
    key = (st.st_dev, st.st_ino, st.st_mtime_ns, st.st_size)
    hit = _allow_cache.get(f)
    if hit is not None and hit[0] == key:
        return hit[1]
    entries = frozenset((c, s) for c, s, _, _ in read_allow(vault))
    _allow_cache[f] = (key, entries)
    return entries


def allowed(cls: str, s: str, allow) -> bool:
    """True when the hit of class `cls` whose whole text is `s` is relaxed by `allow`."""
    return bool(allow) and cls in LEARNABLE and ((cls, s) in allow or (ANY, s) in allow)


def write_allow(vault: Path, entries: list[tuple[str, str, str, str]]) -> None:
    d = folder(vault)
    config.make_dir(d.parent, 0o700)
    config.make_dir(d, 0o700)
    body = ALLOW_HEAD + "".join("%s\t%s\t%s\t%s\n" % (c, _clean(s), _clean(r), _clean(t)) for c, s, r, t in entries)
    tmp = d / (".allow.%d.tmp" % os.getpid())
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(body)
    os.replace(tmp, d / "allow.tsv")


# --------------------------------------------------------------------------- the rules of learning

def _lexicon():
    from awb.intake.wipe import lexicon

    return lexicon


def _services() -> frozenset[str]:
    """Service names and their other names of rules/services.tsv, case folded."""
    from awb import patterns

    out: set[str] = set()
    try:
        lines = (patterns.RULES_DIR / "services.tsv").read_text(encoding="utf-8").splitlines()
    except OSError:
        return frozenset()
    for line in lines:
        if line.startswith("#"):
            continue
        cells = line.split("\t")
        if len(cells) > 1 and cells[1].strip():
            out.add(cells[1].strip().casefold())
        if len(cells) > 4:
            out.update(a.strip().casefold() for a in cells[4].split("|") if a.strip())
    return frozenset(out)


_TOKEN_RE = re.compile(r"[^\W\d_]+|\d+")
_WORD_RE = re.compile(r"[^\W\d_]+")


def _first_name(word: str) -> bool:
    return word.casefold() in _lexicon()._FIRST_NAMES


def _known(word: str) -> bool:
    """A word the learning may take as harmless: known to the intake's word lists and no first name."""
    lx = _lexicon()
    low = word.casefold()
    return not _first_name(word) and (lx._known_word(word) or low in lx._TECH_TERMS or low in lx._COMMON_WORDS)


def keep_reason(s: str, before: str, names) -> str | None:
    """Why a blocked string stays blocked whatever the corpus says, or None. `names` finds registered forms (a
    LocalCheck or a RemoteCheck)."""
    lx = _lexicon()
    words = _WORD_RE.findall(s)
    if any(_first_name(w) for w in words):
        return "first-name"
    if before and before.casefold() in lx._TITLE_WORDS | lx._GENERIC_ADDRESSEES:
        return "title"
    if any(w.casefold() in lx._LEGAL_WORDS for w in words):
        return "legal-form"
    if names is not None and names.name_spans(s):
        return "registered"
    run = re.findall(r"(?<![\w-])[A-ZÄÖÜА-ЯЁ][a-zäöüßа-яё]+(?:[ \t]+[A-ZÄÖÜА-ЯЁ][a-zäöüßа-яё]+)+", s)
    if any(not all(lx._person_known(w) for w in r.split()) for r in run):
        return "person"
    if "@" in s:
        return "person"
    return None


_HOST_LABEL = r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?"
LOG_RULES: tuple[tuple[str, re.Pattern], ...] = (
    ("timestamp", re.compile(r"\d{4}-\d\d-\d\d(?:[T ]\d\d:\d\d(?::\d\d(?:[.,]\d{1,9})?)?(?:Z|[+-]\d\d:?\d\d)?)?"
                             r"|\d\d:\d\d:\d\d(?:[.,]\d{1,9})?|\d{1,2}\.\d{1,2}\.\d{4}(?:[ T]\d\d:\d\d(?::\d\d)?)?"
                             r"|1\d{9}(?:\d{3})?")),
    ("http-status", re.compile(r"HTTP/\d(?:\.\d)? [1-5]\d\d(?: [A-Za-z][A-Za-z -]{0,40})?")),
    ("error-code", re.compile(r"0x[0-9A-Fa-f]{4,16}|[A-Z]{1,6}[-_]?\d{2,8}|ERR_[A-Z0-9_]{2,40}|E[A-Z]{3,12}")),
    ("hash-id", re.compile(r"(?=[0-9a-f]*\d)[0-9a-f]{7,64}|[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}",
                           re.IGNORECASE)),
    ("file-path", re.compile(r"(?:/[\w.@+-]{1,80}){1,20}/?|[A-Za-z]:\\(?:[\w.@+-]{1,80}\\?){1,20}")),
    ("host", re.compile(r"(?:(?:https?|wss?)://)?%s(?:\.%s){1,10}(?::\d{1,5})?(?:/[\w./%%-]{0,200})?"
                        % (_HOST_LABEL, _HOST_LABEL))),
)
"""The log-shape rules for support logs. A rule matches the whole blocked string or not at all; the word rules of
`_log_words_ok` decide on top for the rules whose strings carry words."""


def _log_words_ok(rule: str, s: str) -> bool:
    """The words of a host name or a path: every word known or a short label (three letters at most) or a label
    with a digit, and the registrable domain (the label before the top level) of known words alone."""
    if rule in ("timestamp", "http-status", "error-code", "hash-id"):
        return True
    body = re.sub(r"^[a-z]+://", "", s)
    host, _, path = body.partition("/")
    host = host.split(":", 1)[0]
    parts = [t for t in re.split(r"[^\w]|_", path) if t] if rule == "host" else []
    labels = host.split(".") if rule == "host" else []
    if rule == "file-path":
        parts = [t for t in re.split(r"[^\w]|_", s) if t]
    for t in parts + [w for lab in labels[:-1] for w in lab.split("-")]:
        if any(c.isdigit() for c in t):
            if _WORD_RE.fullmatch(re.sub(r"\d", "", t) or "x") and len(re.sub(r"\d", "", t)) > 3 \
                    and not _known(re.sub(r"\d", "", t)):
                return False
            continue
        if len(t) <= 3:
            if _first_name(t):
                return False
            continue
        if not _known(t):
            return False
    if rule == "host":
        if len(labels) < 2 or not _WORD_RE.fullmatch(labels[-1]):
            return False   # an address of digits is no host name
        registrable = labels[-2]
        if not all(_known(w) for w in registrable.split("-") if w):
            return False
    return True


def log_rule(s: str) -> str | None:
    """The name of the log-shape rule the whole string matches, or None."""
    if s.casefold() in _services():
        return "service"
    for name, rx in LOG_RULES:
        if rx.fullmatch(s) and _log_words_ok(name, s):
            return name
    return None


def corpus_files(p: config.Paths) -> list[Path]:
    """The files of the public corpus: the text files of the mirrors, of the knowledge base, rules/tech-terms.txt and
    rules/common-words.txt."""
    from awb import words

    out: list[Path] = [words.TECH_FILE, words.COMMON_FILE]
    for root in (words.mirror_root(), p.kb):
        try:
            if not root.is_dir():
                continue
            out.extend(f for f in sorted(root.rglob("*"))
                       if f.suffix.lower() in (".rst", ".md", ".txt", ".html", ".htm", ".tsv", ".json", ".csv")
                       and f.is_file() and ".git" not in f.parts)
        except OSError:
            continue
    return out


CORPUS_FILE_LIMIT = 16 * 1024 * 1024


def in_corpus(strings: set[str], files: list[Path]) -> set[str]:
    """The strings that stand in one file of the corpus as a whole word, case folded."""
    pending = {s: re.compile(r"(?<![\w-])%s(?![\w-])" % re.escape(s.casefold())) for s in strings}
    found: set[str] = set()
    for f in files:
        if not pending:
            break
        try:
            if f.stat().st_size > CORPUS_FILE_LIMIT:
                continue
            text = f.read_text(encoding="utf-8", errors="replace").casefold()
        except OSError:
            continue
        for s, rx in list(pending.items()):
            if s.casefold() in text and rx.search(text):
                found.add(s)
                del pending[s]
    return found


def decide(cands: dict[tuple[str, str], str], names, files: list[Path]) -> dict[tuple[str, str], str]:
    """(class, string) -> the reason: "corpus" or "log-RULE" when learned, "keep-REASON" when it stays blocked."""
    out: dict[tuple[str, str], str] = {}
    open_: dict[tuple[str, str], None] = {}
    for key, before in cands.items():
        why = keep_reason(key[1], before, names)
        if why:
            out[key] = "keep-" + why
            continue
        rule = log_rule(key[1])
        if rule:
            out[key] = "log-" + rule
        else:
            open_[key] = None
    hits = in_corpus({s for _, s in open_}, files) if open_ else set()
    for key in open_:
        out[key] = "corpus" if key[1] in hits else "keep-unknown"
    return out


# --------------------------------------------------------------------------- the safety gate

def _repo_roots() -> list[Path]:
    env = os.environ.get("AWB_REPO")
    roots = [Path(env)] if env else []
    roots += [Path(__file__).resolve().parent.parent, Path("~/tcp-awb").expanduser()]
    return roots


def pack_values() -> list[str]:
    """Every value of the red team wipe pack (calibration/redteam/wipe), loaded from the repository (AWB_REPO, the
    tree of this package, ~/tcp-awb). Raises LearnError when the pack cannot be loaded."""
    import importlib

    for root in _repo_roots():
        harness = root / "calibration" / "redteam" / "wipe" / "harness.py"
        if not harness.is_file():
            continue
        added = str(root) not in sys.path
        if added:
            sys.path.insert(0, str(root))
        try:
            h = importlib.import_module("calibration.redteam.wipe.harness")
            values: list[str] = []
            for d in h.DIMENSIONS:
                values.extend(v for c in h.load_cases(h.module_path(d)) for v in c.get("values", ()) if v)
            return values
        except Exception as err:
            raise LearnError("the red team wipe pack cannot be loaded (%s)" % type(err).__name__) from None
        finally:
            if added:
                with contextlib.suppress(ValueError):
                    sys.path.remove(str(root))
    raise LearnError("the red team wipe pack is not found (calibration/redteam/wipe)")


def planted_values() -> list[str]:
    """The planted names of awb/planted.py: the self-test's forms and every word of its wipe cases."""
    from awb import planted

    out = [planted.FORM, planted.SHORT, planted.CUSTOMER_FORM, planted.CUSTOMER_SHORT]
    for _, _, words_, _ in planted.WIPE_CASES:
        out.extend(words_)
    return out


def _slug(v: str) -> str:
    return re.sub(r"[^\w]+", "-", v.casefold()).strip("-_")


def planted_texts(values: list[str]) -> list[tuple[str, str]]:
    """(value, text) per planted value: the value alone, and glued into a host name, an FQDN and a URL."""
    out: list[tuple[str, str]] = []
    for v in dict.fromkeys(values):
        out.append((v, "seen: %s today" % v))
        slug = _slug(v)
        if slug:
            out += [(v, "host %s.ops-lab.de down" % slug), (v, "host srv-%s-02.ops-lab.de down" % slug),
                    (v, "see https://%s.ops-lab.de/status now" % slug)]
    return out


def safety_gate(old, new, values: list[str]) -> list[str]:
    """The planted values a change of the allow list from `old` to `new` lets pass that were blocked before: a hit
    of a planted text under `old` that is gone under `new`, or a new entry that carries a planted value. Empty when
    the change is safe."""
    from awb import check
    from awb.matcher import Matcher

    old, new = frozenset(old), frozenset(new)
    lost: list[str] = []
    m = Matcher([])
    for v, text in planted_texts(values):
        before = {(h["start"], h["length"], h["cls"]) for h in check._scan(text, m, old)}
        after = {(h["start"], h["length"], h["cls"]) for h in check._scan(text, m, new)}
        if before - after:
            lost.append(v)
    folded = [(v, v.casefold()) for v in dict.fromkeys(values) if len(v) >= 3]
    for _, s in new - old:
        low = s.casefold()
        lost.extend(v for v, f in folded if f in low or _slug(v) in low)
    return sorted(set(lost))


# --------------------------------------------------------------------------- the nightly run

def read_blocks(vault: Path, since: datetime.datetime) -> list[dict]:
    """Every block line from `since` on, as dicts of the BLOCKS_HEAD columns."""
    out: list[dict] = []
    d = folder(vault)
    try:
        files = sorted(d.glob("blocks-*.tsv"))
    except OSError:
        return out
    cols = BLOCKS_HEAD.strip().split("\t")
    first = since.strftime("%Y-%m")
    for f in files:
        if f.stem[len("blocks-"):] < first:
            continue
        try:
            lines = f.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            continue
        for line in lines[1:]:
            cells = line.split("\t")
            if len(cells) != len(cols):
                continue
            row = dict(zip(cols, cells))
            if row["time"] >= _stamp(since):
                out.append(row)
    return out


def _prune(vault: Path, now: datetime.datetime) -> None:
    cut = (now - datetime.timedelta(days=KEEP_DAYS)).strftime("%Y-%m")
    for f in folder(vault).glob("blocks-*.tsv"):
        if f.stem[len("blocks-"):] < cut:
            try:
                f.unlink()
            except OSError:
                pass


class Outcome:
    """What one run of the learning did: counts only (`line`), the strings stay in the vault."""

    def __init__(self):
        self.blocks: Counter = Counter()
        self.learned = 0
        self.kept: Counter = Counter()
        self.dropped: list[str] = []
        self.note = ""

    def line(self, week: str) -> str:
        def counts(c: Counter) -> str:
            return ", ".join("%s %d" % kv for kv in sorted(c.items())) or "none"
        text = "awb check learning, week %s: blocks %d (%s); learned %d; kept blocked %d (%s)" % (
            week, sum(self.blocks.values()), counts(self.blocks), self.learned, sum(self.kept.values()),
            counts(self.kept))
        if self.dropped:
            text += "; the safety gate dropped the change: %d planted value(s) would pass" % len(self.dropped)
        if self.note:
            text += "; %s" % self.note
        return text


def _names(p: config.Paths):
    from awb import check

    try:
        return check.checker(p.register)
    except Exception as err:
        raise LearnError("the register cannot be read (%s)" % type(err).__name__) from None


def learn(p: config.Paths, now: datetime.datetime | None = None, values: list[str] | None = None,
          files: list[Path] | None = None) -> Outcome:
    """One nightly run: decide every blocked string of the last LOOKBACK_DAYS that is not on the list yet, pass the
    new list through the safety gate and write it. `values` and `files` replace the planted values and the corpus
    (tests)."""
    now = now or _now()
    out = Outcome()
    rows = read_blocks(p.vault, now - datetime.timedelta(days=LOOKBACK_DAYS))
    old = read_allow(p.vault)
    old_keys = {(c, s) for c, s, _, _ in old}
    cands: dict[tuple[str, str], str] = {}
    for r in rows:
        if r["class"] in LEARNABLE and r["string"] != "-" and (r["class"], r["string"]) not in old_keys \
                and (ANY, r["string"]) not in old_keys:
            cands.setdefault((r["class"], r["string"]), "" if r["before"] == "-" else r["before"])
    names = _names(p)
    try:
        decided = decide(cands, names, corpus_files(p) if files is None else files)
    except Exception as err:
        from awb import register

        if isinstance(err, register.RegisterError):
            raise LearnError("the name check is unavailable: %s" % type(err).__name__) from None
        raise
    added = [(c, s, why, now.strftime("%Y-%m-%d")) for (c, s), why in sorted(decided.items())
             if not why.startswith("keep-")]
    if added:
        try:
            vals = (planted_values() + pack_values()) if values is None else values
            lost = safety_gate(old_keys, old_keys | {(c, s) for c, s, _, _ in added}, vals)
        except LearnError as err:
            out.note = "%s: the list was not changed" % err
            added, lost = [], []
        if lost:
            out.dropped = lost
            added = []
        if added:
            write_allow(p.vault, old + added)
    _prune(p.vault, now)
    week = [r for r in rows if r["time"] >= _stamp(now - datetime.timedelta(days=WEEK_DAYS))]
    out.blocks = Counter(src for src, _ in {(r["source"], r["event"]) for r in week})
    allow_now = load_allow(p.vault)
    learned_week = {(c, s) for c, s, _, t in read_allow(p.vault)
                    if t >= (now - datetime.timedelta(days=WEEK_DAYS)).strftime("%Y-%m-%d")}
    out.learned = len(learned_week)
    out.kept = Counter(r["class"] for r in week if not allowed(r["class"], r["string"], allow_now))
    return out


def iso_week(now: datetime.datetime) -> str:
    y, w, _ = now.isocalendar()
    return "%d-W%02d" % (y, w)


def weekly(p: config.Paths, out: Outcome, now: datetime.datetime | None = None, send=None) -> str | None:
    """The weekly line, once a week: appended to <vault>/log/fp/weekly.txt and handed to `send(subject, line)` (the
    command awb/fplearn_cmd.py gives the owner's mail). None when this week's line went already."""
    now = now or _now()
    f = folder(p.vault) / "weekly.txt"
    try:
        last = f.read_text(encoding="utf-8").splitlines()[-1].split("\t", 1)[0]
    except (OSError, IndexError):
        last = ""
    if last and last > _stamp(now - datetime.timedelta(days=WEEK_DAYS) + datetime.timedelta(hours=2)):
        return None
    line = out.line(iso_week(now))
    config.make_dir(folder(p.vault).parent, 0o700)
    config.make_dir(folder(p.vault), 0o700)
    fd = os.open(f, os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        os.write(fd, ("%s\t%s\n" % (_stamp(now), line)).encode("utf-8"))
    finally:
        os.close(fd)
    if send is not None:
        try:
            send("awb check learning, week %s" % iso_week(now), line)
        except Exception:
            pass
    return line


# --------------------------------------------------------------------------- the owner's hand

def owner_terminal() -> str | None:
    """Why this process may not see or change the list, or None for the owner's own terminal."""
    from awb import vault

    if config.is_work_user() or config.is_build_user():
        return "the allow list is the owner's: run it in your own shell"
    if vault.in_assistant_session():
        return "the allow list never shows inside an assistant session; run it in your own terminal"
    return None


def allow_by_hand(p: config.Paths, word: str, values: list[str] | None = None,
                  now: datetime.datetime | None = None) -> str:
    """Release `word` for every LEARNABLE class, through the safety gate. Returns the line to print (no word in
    it). Raises LearnError when it is refused."""
    word = " ".join(word.split())
    if not word or len(word) > MAX_STRING or _CONTROL_RE.search(word):
        raise LearnError("give one word or string of at most %d characters" % MAX_STRING)
    why = keep_reason(word, "", _names(p))
    if why in ("first-name", "registered"):
        raise LearnError("refused: a %s is never released" % why.replace("-", " "))
    old = read_allow(p.vault)
    keys = {(c, s) for c, s, _, _ in old}
    if (ANY, word) in keys:
        return "awb check allow: already on the list"
    vals = (planted_values() + pack_values()) if values is None else values
    if safety_gate(keys, keys | {(ANY, word)}, vals):
        raise LearnError("refused: the safety gate found a planted value that would pass")
    write_allow(p.vault, old + [(ANY, word, "hand", (now or _now()).strftime("%Y-%m-%d"))])
    return "awb check allow: released by hand (1 entry, %d on the list)" % (len(old) + 1)
