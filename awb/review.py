"""Review of a deliverable: the contract, the claim list, the level-0 check, the pass record and the status.

Per deliverable `<project>/deliverables/<name>` the review lives in `<project>/reviews/<name>/`:

    contract.md    what was asked, written by `init`: the request as given, the reader, the questions to answer
                   (a checklist he fills), what is out of scope, the word budget, the tier, the writing mode and,
                   when he lowered the tier of a text for a customer or partner, his words
    claims.tsv     written by `claims`: every sentence that carries a checkable claim, with evidence, grade and
                   verdict filled by hand or from the review workflow
    kb.tsv         written by `claims` next to it: per claim the knowledge entries `kb find` returns (V-04)
    lenses/*.json  written by the review workflow (workflows/review.js) for tier 2 and 3 and by the request check
                   (workflows/request-check.js, kind "request")
    record.json    written by `pass`: the sha256 of the deliverable, tier, date, the l0 result, the lens summary
                   and what the review found: the contract in numbers, the claims by verdict and grade, the counts
                   per lens, the agent runs and tokens and the open points

Commands (`awb review ...`):

    init DELIVERABLE --request FILE --tier 0..3 --budget WORDS [--reader TEXT] [--mode mail|doc|chat]
         [--lowered TEXT] [--force]
    claims DELIVERABLE
    l0 DELIVERABLE
    pass DELIVERABLE
    status [PROJECT]
    calibrate [--json]      the calibration set through the script steps (awb/calibrate.py)

The claim list. Columns id, line, kind, risk, sentence (up to 200 characters), evidence, grade, verdict. A
sentence is listed when it carries a negation, a price, an identifier shaped like a flavor (s3.large.2), an API
path (/v3/{project_id}/vaults), a number, a version or a URL. Kind is the comma list of what was found. Risk is
high for negatives, prices, identifiers, API paths and numbers that enter a total (a sizing unit such as vCPU,
GB or VMs next to the number, or a word such as total, per month, a multiplication); medium for other numbers
and versions; low for a URL alone. When the list is written again, a row whose sentence is still in the text
keeps its id, evidence, grade and verdict. Evidence is one or more paths relative to the project, separated by
`;`. An evidence item can also be a knowledge entry, `kb:KB-XXXX`: it must exist, not be retired and not have
expired, and for a high-risk claim its grade must be live or contract (V-04). An evidence item can also be a
calculation, `calc:K-N` of `awb calc`: it must be recorded in the project and its result must appear in the sentence.
A high-risk sentence whose number was computed (a total, a sum, a saving, a difference, an average or a product
such as 20 x 8) needs such an item (R-005). Grade is live, contract, docs, said
or assumed. Verdict is supported, contradicted or unknown.

The level-0 check reads the deliverable once; every check sees those bytes (a file that is not plain text is
read from a private snapshot) and the result carries their hash. It blocks on: a blocking tell of the writing
check (mode from the contract; scope hcs only when the project is an hcs- project and the contract says hcs, so
that no line written in the project turns the vendor-name rule off; a missing writing check blocks too), any hit
of the name check, more words than the budget, a claim of the
current text that is not in claims.tsv, a high-risk claim without an existing evidence path or without a
verdict, a contradicted claim, an unknown claim from tier 2, a malformed grade, verdict or evidence path. The
risk of a row always comes from the detector, never from the file, so a row cannot be talked down by hand.

A lens file is a JSON object with a list `findings`. A finding is blocking when `blocking` is true or
`severity` is "blocking"; it is closed when `outcome` is "confirmed-fixed", "refuted" or "softened" (a plausible
finding whose sentence he softened or tagged). "plausible" alone stays open. A pass needs at least one lens file at
tier 3. Lens files that are present are checked at every tier: an open blocking finding
blocks the pass. A pass that fails removes an older record.json. The record carries the hash of the checked
bytes and the hashes of contract.md, claims.tsv and every lens file; it is valid only while all of them are
unchanged and it says that l0 passed with no open blocking lens finding. A record written by hand without
those hashes is stale. (A session under the same user could still compute them: the backstops are the commit
gate and his own review.)

The send gate (`send_check`, decision 15) runs before `awb bucket put` uploads a file. For a project with a
customer or partner it refuses a deliverable (or a copy of one) without a valid record, a record below tier 3
unless the contract carries his words for the lowered tier (decision 14) and a tier 3 record whose lenses hold no
request check (V-16). Everything else without a valid record leaves with a warning as an internal file.

`status` lists every file under deliverables/, hidden files too (only an empty .gitkeep is left out). A file
that no review can read as text (an image, an unknown binary) has the state unreadable: it can never pass and
belongs somewhere else.

Name checks go through `check.check_text` with the register of `config.paths()`; a readable vault folder without
a register refuses to run. Nothing here prints, logs or raises with a matched value, a register form or a code
of the register: output carries claim ids, line numbers, classes, counts and paths that passed the name check.
"""
from __future__ import annotations

import hashlib
import importlib
import json
import os
import re
import shutil
import sys
import tempfile
from collections import Counter
from contextlib import contextmanager
from dataclasses import asdict, dataclass, replace
from datetime import date
from pathlib import Path

from awb import codes, config

EXIT_OK = 0
EXIT_FINDINGS = 1
EXIT_ERROR = 2

DELIVERABLES = "deliverables"
REVIEWS = "reviews"
CONTRACT = "contract.md"
CLAIMS = "claims.tsv"
RECORD = "record.json"
LENSES = "lenses"

TIERS = (0, 1, 2, 3)
MODES = ("mail", "doc", "chat")
SCOPES = ("tcp", "hcs")
VERDICTS = ("supported", "contradicted", "unknown")
GRADES = ("live", "contract", "docs", "said", "assumed")
RISKS = ("high", "medium", "low")
KINDS = ("negative", "price", "identifier", "api-path", "number", "version", "url")
HIGH_KINDS = ("negative", "price", "identifier", "api-path")
CLAIM_FIELDS = ("id", "line", "kind", "risk", "sentence", "evidence", "grade", "verdict")
CLOSED_OUTCOMES = ("confirmed-fixed", "refuted", "softened")
UNKNOWN_BLOCKS_FROM = 2
LENSES_REQUIRED_FROM = 3
KB_FILE = "kb.tsv"
KB_CANDIDATES = 3
SETTLING_GRADES = ("live", "contract")
"""A knowledge entry settles a high-risk claim only with one of these grades (V-04)."""
SEND_TIER = 3
"""What leaves the machine for a customer or partner is reviewed at this tier unless he lowered it (decision 14)."""
REQUEST_KIND = "request"
"""The kind of the lens file that the request check (workflows/request-check.js) writes (V-16)."""
MAX_LOWERED = 200
MAX_SENTENCE = 200
MAX_READER = 200
MAX_QUESTIONS = 20


class ReviewError(Exception):
    """The review cannot run. The message carries no value: classes, counts, claim ids, line numbers."""


class Refused(ReviewError):
    """An input did not pass the name check. Exit 1."""


# --------------------------------------------------------------------------- places


def locate(deliverable) -> tuple[Path, str]:
    """(project folder, name) of a deliverable: the project is the parent of the nearest `deliverables` folder
    above the file, the name is the path below that folder. The file need not exist yet."""
    path = Path(os.path.abspath(Path(deliverable)))
    for parent in path.parents:
        if parent.name == DELIVERABLES:
            rel = path.relative_to(parent).as_posix()
            if rel in ("", "."):
                break
            return parent.parent, rel
    raise ReviewError("the deliverable must lie in the deliverables folder of a project")


def review_dir(project: Path, name: str) -> Path:
    return Path(project) / REVIEWS / name


def _platform(project: Path) -> str:
    """hcs only for a project whose folder is an hcs- project code. SCOPE.md and contract.md are written in the
    project and cannot switch the vendor-name rule off."""
    return codes.project_platform(Path(project))


def _scope(project: Path, contract: "Contract") -> str:
    """The scope of the writing check: hcs only when the project is an hcs project and the contract says hcs."""
    return "hcs" if _platform(project) == "hcs" and contract.scope == "hcs" else "tcp"


def _write_atomic(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".tmp-", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(text)
        os.chmod(tmp, 0o640)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for block in iter(lambda: fh.read(1 << 16), b""):
            h.update(block)
    return h.hexdigest()


# --------------------------------------------------------------------------- the name check


def _register(register_path) -> Path:
    return Path(register_path) if register_path is not None else config.paths().register


def _ensure_checkable(reg: Path) -> None:
    """Refuse when a readable vault folder holds no register at all: the names could not be checked."""
    from awb import check, register

    try:
        kind, _ = check.register_source(reg)
    except register.RegisterError:
        raise ReviewError("the register cannot be read, names cannot be checked") from None
    if kind == check.MISSING:
        raise ReviewError("no register found, names cannot be checked")


def _check(text: str, reg: Path) -> list[dict]:
    from awb import check, register

    try:
        return list(check.check_text(text, reg))
    except check.CheckUnavailable as err:
        raise ReviewError(str(err) or "name check unavailable") from None
    except register.RegisterError:
        raise ReviewError("the register cannot be read, names cannot be checked") from None


def _is_clean(text: str, reg: Path) -> bool:
    """True only when the name check ran and found nothing. Used before a path is printed."""
    try:
        _ensure_checkable(reg)
        return not _check(text, reg)
    except ReviewError:
        return False


def _counts(hits: list[dict]) -> str:
    c = Counter(h.get("cls", "unknown") if isinstance(h, dict) else "unknown" for h in hits)
    return ", ".join("%s %d" % kv for kv in sorted(c.items()))


def _label(rel: str, reg: Path) -> str:
    shown = "%s/%s" % (DELIVERABLES, rel)
    return shown if _is_clean(shown, reg) else "the deliverable"


# --------------------------------------------------------------------------- reading the deliverable


_BINARY_MAGIC = (b"%PDF", b"PK\x03\x04", b"PK\x05\x06", b"\xd0\xcf\x11\xe0", b"\x89PNG", b"\xff\xd8\xff", b"\x1f\x8b")


def _read_plain(data: bytes) -> str | None:
    head = data[:8192]
    if b"\x00" in head or head.startswith(_BINARY_MAGIC):
        return None
    try:
        return data.decode("utf-8-sig")
    except UnicodeDecodeError:
        return None


def _read_bytes(path: Path) -> bytes:
    try:
        return Path(path).read_bytes()
    except OSError as err:
        raise ReviewError("the deliverable cannot be read (%s)" % type(err).__name__) from None


@contextmanager
def _snapshot(path: Path, data: bytes):
    """A private copy of `data` under the name of the deliverable (mode 600 in a folder of mode 700, removed
    afterwards), for the readers that need a file: what they read are exactly the bytes that are hashed."""
    folder = tempfile.mkdtemp(prefix="awb-review-")
    try:
        os.chmod(folder, 0o700)
        snap = Path(folder) / Path(path).name
        fd = os.open(snap, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
        yield snap
    finally:
        shutil.rmtree(folder, ignore_errors=True)


def _deliverable_text(path: Path, data: bytes | None = None) -> tuple[str, bool]:
    """(text, plain). A plain UTF-8 file is read as it is, any other kind through awb.extract. With `data` the
    text is taken from those bytes; `path` must then hold the same bytes (a snapshot) for the extraction."""
    if data is None:
        data = _read_bytes(path)
    text = _read_plain(data)
    if text is not None:
        return text, True
    try:
        from awb import extract
    except ImportError:
        raise ReviewError("the deliverable is not a text file and no reader is installed") from None
    ex = extract.extract(path)
    if ex.state != "ok" or not (ex.text or "").strip():
        raise ReviewError("the deliverable cannot be read as text (state %s)" % ex.state)
    return ex.text, False


def _deliverable_hits(path: Path, rel: str, text: str, plain: bool, reg: Path) -> list[dict]:
    """Every hit of the name check in the deliverable, plus one `path` record when its name carries one."""
    if plain:
        hits = _check(text, reg)
    else:
        from awb import check, register

        try:
            hits = list(check.check_file(path, reg))
        except check.CheckUnavailable as err:
            raise ReviewError(str(err) or "name check unavailable") from None
        except register.RegisterError:
            raise ReviewError("the register cannot be read, names cannot be checked") from None
        except OSError as err:
            raise ReviewError("the deliverable cannot be read (%s)" % type(err).__name__) from None
        hits = [h for h in hits if h.get("cls") != "path"]
    if _check("%s/%s" % (DELIVERABLES, rel), reg):
        hits.insert(0, {"start": 0, "length": 0, "cls": "path"})
    return hits


def _existing_file(deliverable) -> tuple[Path, Path, str]:
    project, rel = locate(deliverable)
    path = project / DELIVERABLES / rel
    if not path.is_file():
        raise ReviewError("the deliverable is not a readable file")
    return path, project, rel


# --------------------------------------------------------------------------- the contract


@dataclass(frozen=True)
class Contract:
    tier: int
    budget: int
    mode: str
    scope: str
    reader: str
    lowered: str = ""   # his words when he lowered the tier of a text for a customer or partner (decision 14)


_CONTRACT_FIELD_RE = re.compile(r"(?m)^- (deliverable|tier|budget|mode|scope|reader|created|lowered): *(.*?) *$")
_QUESTION_SPLIT_RE = re.compile(r"(?<=[.!?])\s+")


def _questions(request: str) -> list[str]:
    """Every sentence of the request that ends with a question mark, as one line each."""
    out: list[str] = []
    for piece in _QUESTION_SPLIT_RE.split(" ".join(request.split())):
        piece = piece.strip().lstrip("-*> ").strip()
        if piece.endswith("?") and len(piece) > 1 and piece not in out:
            out.append(piece)
    return out[:MAX_QUESTIONS]


def render_contract(rel: str, tier: int, budget: int, mode: str, scope: str, reader: str, request: str,
                    created: str, lowered: str = "") -> str:
    lines = [
        "# Review contract",
        "",
        "- deliverable: %s/%s" % (DELIVERABLES, rel),
        "- tier: %d" % tier,
    ]
    if lowered:
        lines.append("- lowered: %s" % lowered)
    lines += [
        "- budget: %d words" % budget,
        "- mode: %s" % mode,
        "- scope: %s" % scope,
        "- reader: %s" % (reader or "not given"),
        "- created: %s" % created,
        "",
        "## Request as given",
        "",
    ]
    lines.extend(("> " + line.rstrip()) if line.strip() else ">" for line in request.strip("\n").splitlines())
    lines += [
        "",
        "## Questions to answer",
        "",
        "Tick a question when the deliverable answers it. Add the questions the request leaves implicit.",
        "",
    ]
    lines.extend("- [ ] %s" % q for q in _questions(request))
    lines += ["- [ ] (fill in)", "", "## Out of scope", "", "- (fill in)", ""]
    return "\n".join(lines)


def load_contract(path: Path, default_scope: str = "tcp") -> Contract:
    try:
        text = Path(path).read_text(encoding="utf-8")
    except FileNotFoundError:
        raise ReviewError("there is no contract.md: run awb review init first") from None
    except (OSError, UnicodeDecodeError):
        raise ReviewError("contract.md cannot be read") from None
    head = text.split("\n## ", 1)[0]
    fields = {k: v for k, v in _CONTRACT_FIELD_RE.findall(head)}
    tier = fields.get("tier", "")
    if not re.fullmatch(r"[0-3]", tier):
        raise ReviewError("contract.md: the tier must be 0, 1, 2 or 3")
    m = re.match(r"([0-9]{1,7})(?: words?)?$", fields.get("budget", ""))
    if not m or int(m.group(1)) <= 0:
        raise ReviewError("contract.md: the budget must be a positive number of words")
    mode = fields.get("mode", "doc") or "doc"
    if mode not in MODES:
        raise ReviewError("contract.md: the mode must be mail, doc or chat")
    scope = fields.get("scope", default_scope) or default_scope
    if scope not in SCOPES:
        raise ReviewError("contract.md: the scope must be tcp or hcs")
    return Contract(tier=int(tier), budget=int(m.group(1)), mode=mode, scope=scope, reader=fields.get("reader", ""),
                    lowered=fields.get("lowered", ""))


def _clean_reader(reader) -> str:
    if reader is None:
        return ""
    if not isinstance(reader, str):
        raise ReviewError("the reader must be text")
    reader = " ".join(reader.split())
    if len(reader) > MAX_READER:
        raise ReviewError("the reader is longer than %d characters" % MAX_READER)
    return reader


def init(deliverable, request, tier: int, budget: int, reader: str | None = None, mode: str = "doc",
         force: bool = False, register_path=None, today: str | None = None, lowered: str | None = None) -> Path:
    """Write `reviews/<name>/contract.md`. The request text, the reader and his words that lowered the tier must
    pass the name check; a hit raises Refused and nothing is written. Returns the path of the contract.

    `lowered` carries his words when he lowered the tier of a text that goes to a customer or partner (decision
    14: tier 3 by default, he lowers it with one word). The send gate reads it; it needs a tier below 3."""
    project, rel = locate(deliverable)
    if tier not in TIERS:
        raise ReviewError("the tier must be 0, 1, 2 or 3")
    if not isinstance(budget, int) or isinstance(budget, bool) or budget <= 0:
        raise ReviewError("the budget must be a positive number of words")
    if mode not in MODES:
        raise ReviewError("the mode must be mail, doc or chat")
    reader = _clean_reader(reader)
    lowered = " ".join(str(lowered).split()) if lowered else ""
    if lowered and tier >= SEND_TIER:
        raise ReviewError("--lowered goes with a tier below %d" % SEND_TIER)
    if len(lowered) > MAX_LOWERED:
        raise ReviewError("his words for the lowered tier are longer than %d characters" % MAX_LOWERED)
    try:
        data = Path(request).read_bytes()
    except OSError as err:
        raise ReviewError("the request file cannot be read (%s)" % type(err).__name__) from None
    text = _read_plain(data)
    if text is None:
        raise ReviewError("the request must be a UTF-8 text file")
    if not text.strip():
        raise ReviewError("the request is empty")
    reg = _register(register_path)
    _ensure_checkable(reg)
    hits = _check(text, reg)
    if hits:
        raise Refused("the request was refused by the name check (%s); nothing was written" % _counts(hits))
    if reader:
        hits = _check(reader, reg)
        if hits:
            raise Refused("the reader was refused by the name check (%s); nothing was written" % _counts(hits))
    if lowered:
        hits = _check(lowered, reg)
        if hits:
            raise Refused("the words for the lowered tier were refused by the name check (%s); nothing was written"
                          % _counts(hits))
    target = review_dir(project, rel) / CONTRACT
    if target.exists() and not force:
        raise ReviewError("contract.md exists already; give --force to write it again")
    body = render_contract(rel, tier, budget, mode, _platform(project), reader, text,
                           today or date.today().isoformat(), lowered)
    hits = _check(body, reg)
    if hits:
        raise Refused("the contract was refused by the name check (%s); nothing was written" % _counts(hits))
    _write_atomic(target, body)
    return target


# --------------------------------------------------------------------------- claims


@dataclass(frozen=True)
class Claim:
    id: str
    line: int
    kind: str
    risk: str
    sentence: str
    evidence: str = ""
    grade: str = ""
    verdict: str = ""

    def kinds(self) -> list[str]:
        return [k for k in self.kind.split(",") if k]


_FENCE_RE = re.compile(r"^\s*(?:```|~~~)")
_HEADING_RE = re.compile(r"^\s{0,3}#{1,6}\s+")
_NUMBERING_RE = re.compile(r"^(?:[0-9]{1,3}(?:\.[0-9]{1,3})*\.|[0-9]{1,3}(?:\.[0-9]{1,3})+)\s+")
_LIST_RE = re.compile(r"^\s*(?:[-*+]|[0-9]{1,3}[.)])\s+(?:\[[ xX]\]\s+)?")
_QUOTE_RE = re.compile(r"^\s*>\s?")
_TABLE_RE = re.compile(r"^\s*\|")
_SPLIT_RE = re.compile(r"[.!?][\"'\u201d\u2019)\]]*\s+(?=[\"'\u201c\u2018(\[*_`]*[A-Z\u00c4\u00d6\u00dc0-9])")

_URL_RE = re.compile(r"(?i)(?<![\w@])(?:https?://|www\.)[^\s<>()\[\]\"'`]+")
_API_RE = re.compile(r"(?<![\w/.:-])/(?:api/)?v[0-9]+(?:\.[0-9]+)?(?:/[A-Za-z0-9_{}.\-]+)+")
_IP4_RE = re.compile(r"(?<![\w.])[0-9]{1,3}(?:\.[0-9]{1,3}){3}(?:/[0-9]{1,2})?(?![0-9]|\.[0-9])")
_IP6_RE = re.compile(r"(?i)(?<![\w:])(?:[0-9a-f]{1,4}:){2,7}[0-9a-f:]*(?:/[0-9]{1,3})?")
_DATE_RE = re.compile(r"(?<![\w.])(?:[0-9]{4}-[0-9]{2}-[0-9]{2}|[0-9]{1,2}\.[0-9]{1,2}\.(?:19|20)[0-9]{2})(?![0-9])")
_TIME_RE = re.compile(r"(?<![\w:])[0-9]{1,2}:[0-9]{2}(?::[0-9]{2})?(?![0-9])")
_FLAVOR_RE = re.compile(
    r"(?<![\w./-])[a-z][a-z0-9]{0,7}(?:\.[a-z0-9]{1,12}){0,3}"
    r"\.(?:nano|micro|tiny|small|medium|large|xlarge|[1-9][0-9]?xlarge)(?:\.[0-9]{1,3}[a-z]?)?(?![\w-]|\.[\w])"
)
_FLAVOR_ANY_RE = re.compile(
    r"(?<![\w./-])(?!v[0-9])[a-z]{1,4}[0-9][a-z0-9]{0,3}\.[a-z0-9]{2,12}\.[0-9]{1,3}(?:\.[a-z]{2,12})?(?![\w-]|\.[\w])"
)
"""Any name shaped like a flavor (family with a digit, size, ratio, an optional suffix such as physical), also one
the size list of _FLAVOR_RE does not know: an invented flavor must reach the claim list to be checked."""
_MASK_RE = re.compile(r"(?<![\w.])/(?:[0-9]|[12][0-9]|3[0-2])(?![\w.])")
"""A network mask written alone, such as /29."""
_CURRENCY = r"(?:\u20ac|EUR|USD|CHF|GBP|\u00a3|\$)"
_PRICE_RE = re.compile(
    r"(?<![\w.])%s\s?[0-9]+(?:[.,][0-9]+)*"
    r"|(?<![\w.,])[0-9]+(?:[.,][0-9]+)*\s?(?:%s|(?i:euros?|cents?))(?![A-Za-z])" % (_CURRENCY, _CURRENCY)
)
_PRICE_WORD_RE = re.compile(r"(?i)\b(?:prices?|priced|pricing|costs?|fees?|charged?|billed|hourly rate)\b")
_VERSION_RE = re.compile(
    r"(?<![\w.])v[0-9]+(?:\.[0-9]+){1,3}(?![\w.]*[0-9])"
    r"|(?<![\w.])[0-9]+\.[0-9]+\.[0-9]+(?:\.[0-9]+)?(?![\w.]*[0-9])"
    r"|(?i:\bversions?\s+v?[0-9]+(?:\.[0-9]+)*)"
    r"|\b(?:Kubernetes|Terraform|PostgreSQL|MySQL|MariaDB|Python|Ubuntu|Debian|Windows Server|SQL Server|RHEL|SLES"
    r"|SUSE|Oracle|Java|OpenSSL|Redis|CentOS|OpenStack)\s+v?[0-9]+(?:\.[0-9]+)*"
)
_NUMBER_RE = re.compile(r"(?<![\w.,:/-])[0-9]+(?:[.,][0-9]+)*")
_UNIT_AFTER_RE = re.compile(
    r"\s?(?:x\s?)?(?i:vcpus?|cpus?|cores?|gb|gib|tb|tib|mb|mib|pb|gbit/s|mbit/s|gbps|mbps|vms?|servers?|nodes?"
    r"|instances?|hosts?|users?|seats?|licen[cs]es?|disks?|volumes?|eips?|ips?|hours?|h|months?|years?|%)(?![\w])"
)
_TOTAL_RE = re.compile(
    r"(?i)\b(?:total|totals|sum|subtotal|overall|altogether|in all|per month|monthly|per year|yearly|annual"
    r"|per hour|hourly)\b|\u00d7|[0-9]\s?[x*]\s?[0-9]"
)
_NEG_RE = re.compile(
    r"(?i)(?<![\w-])(?:not|no|never|none|nothing|neither|nor|cannot|unavailable|unsupported|impossible)(?![\w-])"
    r"|(?i:\b[a-z]+n['\u2019]t\b)"
    r"|(?i:\bonly\s+(?:in|on)\s+(?:the\s+)?(?:eu-[a-z]{2}[0-9]?|region|one\s+region)\b)"
)
"""A negative, also the categorical one of availability: "only in eu-nl" rules out every other region."""
_TRAIL = ".,;:!?)]}'\"`"


def _units(text: str) -> list[tuple[int, str]]:
    """(line, text) of every sentence, heading, table row and code line, in order of the text."""
    out: list[tuple[int, str]] = []
    block: list[tuple[int, str]] = []
    quoted = False   # the open block is a block quote

    def flush() -> None:
        if block:
            out.extend(_sentences(block))
            block.clear()

    in_code = False
    for no, raw in enumerate(text.splitlines(), start=1):
        if _FENCE_RE.match(raw):
            flush()
            in_code = not in_code
            continue
        if in_code:
            if raw.strip():
                out.append((no, raw.strip()))
            continue
        if not raw.strip():
            flush()
            continue
        if _HEADING_RE.match(raw):
            flush()
            body = _NUMBERING_RE.sub("", _HEADING_RE.sub("", raw).strip()).rstrip("#").strip()
            if body:
                out.append((no, body))
            continue
        if _TABLE_RE.match(raw):
            flush()
            out.append((no, raw.strip()))
            continue
        m = _LIST_RE.match(raw)
        if m:
            flush()
            quoted = False
            block.append((no, raw[m.end():].strip()))
            continue
        q = _QUOTE_RE.match(raw)
        if q and not quoted:
            # a block quote starts its own block; a plain line after it continues it (lazy continuation)
            flush()
            quoted = True
        elif not block:
            quoted = False
        block.append((no, (raw[q.end():] if q else raw).strip()))
    flush()
    return out


def _sentences(block: list[tuple[int, str]]) -> list[tuple[int, str]]:
    joined = ""
    starts: list[tuple[int, int]] = []
    for no, piece in block:
        if not piece:
            continue
        if joined:
            joined += " "
        starts.append((len(joined), no))
        joined += piece
    out: list[tuple[int, str]] = []
    pos = 0
    cuts = [m.end() for m in _SPLIT_RE.finditer(joined)] + [len(joined)]
    for end in cuts:
        piece = joined[pos:end]
        lead = len(piece) - len(piece.lstrip())
        at = pos + lead
        pos = end
        piece = piece.strip()
        if not piece:
            continue
        line = starts[0][1]
        for off, no in starts:
            if off <= at:
                line = no
        out.append((line, piece))
    return out


def _scan(sentence: str) -> tuple[list[str], bool]:
    """Kinds found in one sentence and whether a number in it enters a total."""
    taken: list[tuple[int, int]] = []
    kinds: set[str] = set()

    def free(a: int, b: int) -> bool:
        return not any(a < y and x < b for x, y in taken)

    def take(rx: re.Pattern, kind: str | None = None, trim: bool = False) -> None:
        for m in rx.finditer(sentence):
            a, b = m.span()
            if trim:
                while b > a and sentence[b - 1] in _TRAIL:
                    b -= 1
            if b <= a or not free(a, b):
                continue
            taken.append((a, b))
            if kind:
                kinds.add(kind)

    take(_URL_RE, "url", trim=True)
    take(_API_RE, "api-path", trim=True)
    take(codes.PLACEHOLDER_RE)
    take(codes.PROJECT_CODE_RE)
    take(_IP4_RE)
    take(_IP6_RE)
    take(_DATE_RE)
    take(_TIME_RE)
    take(_FLAVOR_RE, "identifier")
    take(_FLAVOR_ANY_RE, "identifier")
    take(_MASK_RE, "number")
    take(_PRICE_RE, "price")
    take(_VERSION_RE, "version")
    total = False
    numbers = 0
    for m in _NUMBER_RE.finditer(sentence):
        if not free(*m.span()):
            continue
        numbers += 1
        taken.append(m.span())
        if _UNIT_AFTER_RE.match(sentence, m.end()):
            total = True
    if numbers:
        kinds.add("number")
        if _PRICE_WORD_RE.search(sentence):
            kinds.add("price")
        if _TOTAL_RE.search(sentence):
            total = True
    if _NEG_RE.search(sentence):
        kinds.add("negative")
    return [k for k in KINDS if k in kinds], total and numbers > 0


def _risk(kinds: list[str], total: bool) -> str:
    if any(k in HIGH_KINDS for k in kinds) or ("number" in kinds and total):
        return "high"
    if "number" in kinds or "version" in kinds:
        return "medium"
    return "low"


def _cell(value) -> str:
    return " ".join(str(value).replace("\t", " ").split())


def _sentence_cell(text: str) -> str:
    return _cell(text)[:MAX_SENTENCE].rstrip()


def find_claims(text: str) -> list[Claim]:
    """Every sentence of `text` that carries a claim, with ids C-1, C-2 ... in order of the text."""
    out: list[Claim] = []
    for line, sentence in _units(text):
        kinds, total = _scan(sentence)
        if not kinds:
            continue
        out.append(Claim(id="C-%d" % (len(out) + 1), line=line, kind=",".join(kinds), risk=_risk(kinds, total),
                         sentence=_sentence_cell(sentence)))
    return out


def _key(sentence: str) -> str:
    return " ".join(sentence.split()).casefold()


def _pair(fresh: list[Claim], old: list[Claim]) -> tuple[list[tuple[Claim, Claim | None]], int]:
    """Each fresh claim with the old row of the same sentence (in order for repeated sentences), and the count of
    old rows whose sentence is gone."""
    pool: dict[str, list[Claim]] = {}
    for row in old:
        pool.setdefault(_key(row.sentence), []).append(row)
    pairs: list[tuple[Claim, Claim | None]] = []
    for c in fresh:
        rows = pool.get(_key(c.sentence))
        pairs.append((c, rows.pop(0) if rows else None))
    return pairs, sum(len(v) for v in pool.values())


def _id_number(claim_id: str) -> int:
    m = re.fullmatch(r"C-([0-9]{1,6})", claim_id)
    return int(m.group(1)) if m else 0


def merge_claims(fresh: list[Claim], old: list[Claim]) -> tuple[list[Claim], int, int]:
    """(rows, kept, dropped): fresh claims, where a sentence already listed keeps its id, evidence, grade and
    verdict; new sentences get new ids after the highest old one."""
    pairs, dropped = _pair(fresh, old)
    nxt = max([_id_number(r.id) for r in old] + [0]) + 1
    out: list[Claim] = []
    kept = 0
    for c, row in pairs:
        if row is not None:
            kept += 1
            out.append(replace(c, id=row.id, evidence=row.evidence, grade=row.grade, verdict=row.verdict))
        else:
            out.append(replace(c, id="C-%d" % nxt, evidence="", grade="", verdict=""))
            nxt += 1
    return out, kept, dropped


def render_claims(rows: list[Claim]) -> str:
    lines = ["\t".join(CLAIM_FIELDS)]
    for r in rows:
        lines.append("\t".join(_cell(v) for v in (r.id, r.line, r.kind, r.risk, r.sentence, r.evidence, r.grade,
                                                   r.verdict)))
    return "\n".join(lines) + "\n"


def load_claims(path: Path) -> list[Claim]:
    """The rows of a claims.tsv. Strict header; a row may lack trailing empty fields. Errors name the line only."""
    try:
        text = Path(path).read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        raise ReviewError("claims.tsv cannot be read") from None
    lines = text.splitlines()
    if not lines or tuple(lines[0].rstrip("\r").split("\t")) != CLAIM_FIELDS:
        raise ReviewError("claims.tsv: the header must be %s" % " ".join(CLAIM_FIELDS))
    out: list[Claim] = []
    seen: set[str] = set()
    for no, raw in enumerate(lines[1:], start=2):
        raw = raw.rstrip("\r")
        if not raw.strip():
            continue
        parts = raw.split("\t")
        if len(parts) > len(CLAIM_FIELDS):
            raise ReviewError("claims.tsv line %d: more than %d fields" % (no, len(CLAIM_FIELDS)))
        if len(parts) < 5:
            raise ReviewError("claims.tsv line %d: fewer than 5 fields" % no)
        parts += [""] * (len(CLAIM_FIELDS) - len(parts))
        cid, line, kind, risk, sentence, evidence, grade, verdict = (p.strip() for p in parts)
        if not re.fullmatch(r"C-[0-9]{1,6}", cid):
            raise ReviewError("claims.tsv line %d: the id must look like C-1" % no)
        if cid in seen:
            raise ReviewError("claims.tsv line %d: the id is used twice" % no)
        seen.add(cid)
        if not line.isdigit():
            raise ReviewError("claims.tsv line %d: the line must be a number" % no)
        out.append(Claim(id=cid, line=int(line), kind=kind, risk=risk, sentence=sentence, evidence=evidence,
                         grade=grade.lower(), verdict=verdict.lower()))
    return out


@dataclass
class ClaimsResult:
    path: Path
    rows: list[Claim]
    kept: int
    dropped: int
    kb_path: Path | None = None
    kb_rows: int = 0      # claims with at least one knowledge entry that may settle them (V-04)


def kb_candidates(sentence: str, scope: str = "tcp", kb_where=None, today: date | None = None,
                  limit: int = KB_CANDIDATES) -> list[dict]:
    """The first entries `kb find` returns for a claim sentence (V-04): {"id", "grade", "checked", "expired"}.
    A knowledge base that cannot be read gives none: the list is a lead, the verdict stays with the check."""
    from awb import kb

    try:
        found = kb.find(sentence, where=kb_where, scope=scope)
    except (kb.KBError, OSError):
        return []
    return [{"id": e.id, "grade": e.grade, "checked": e.checked, "expired": e.is_expired(today)}
            for _, e in found[:limit]]


def render_kb(rows: list[Claim], candidates: dict[str, list[dict]]) -> str:
    """kb.tsv: per claim the knowledge entries to read first. An entry that could settle a high-risk claim alone
    (grade live or contract, not expired) is marked "settles"."""
    lines = ["\t".join(("id", "risk", "entries"))]
    for r in rows:
        found = candidates.get(r.id) or []
        cells = []
        for c in found:
            state = "expired" if c["expired"] else ("settles" if c["grade"] in SETTLING_GRADES else "lead")
            cells.append("%s %s %s %s" % (c["id"], c["grade"], c["checked"], state))
        lines.append("\t".join((r.id, r.risk, "; ".join(cells))))
    return "\n".join(lines) + "\n"


def write_claims(deliverable, register_path=None, kb_where=None, today: date | None = None) -> ClaimsResult:
    """`awb review claims`: write reviews/<name>/claims.tsv and next to it kb.tsv, the knowledge entries that
    `kb find` returns for each claim (V-04: the knowledge is consulted before any agent runs). Refused when the
    deliverable carries a name hit, so that no sentence with a name is copied into the review."""
    path, project, rel = _existing_file(deliverable)
    reg = _register(register_path)
    _ensure_checkable(reg)
    text, plain = _deliverable_text(path)
    names = [h for h in _deliverable_hits(path, rel, text, plain, reg) if h.get("cls") in ("name", "path")]
    if names:
        raise Refused("the deliverable carries %d name hits; replace them with codes before the claims are "
                      "listed" % len(names))
    target = review_dir(project, rel) / CLAIMS
    old = load_claims(target) if target.exists() else []
    rows, kept, dropped = merge_claims(find_claims(text), old)
    _write_atomic(target, render_claims(rows))
    scope = _platform(project)
    candidates = {r.id: kb_candidates(r.sentence, scope, kb_where, today) for r in rows}
    kb_path = review_dir(project, rel) / KB_FILE
    _write_atomic(kb_path, render_kb(rows, candidates))
    return ClaimsResult(path=target, rows=rows, kept=kept, dropped=dropped, kb_path=kb_path,
                        kb_rows=sum(1 for v in candidates.values() if v))


# --------------------------------------------------------------------------- level 0


@dataclass
class L0Result:
    blocked: bool
    problems: list[str]
    tier: int
    mode: str
    words: int
    budget: int
    tells: int
    name_hits: int
    claims: int
    high: int
    stale_rows: int
    sha256: str = ""   # the hash of exactly the bytes every check of this run read

    def summary(self) -> dict:
        return asdict(self)


_WORD_RE = re.compile(r"[^\W_]+(?:['\u2019.\-/][^\W_]+)*")


def count_words(text: str) -> int:
    return len(_WORD_RE.findall(text))


class _WritingUnavailable(Exception):
    pass


def _field(obj, name: str, default=None):
    if isinstance(obj, dict):
        return obj.get(name, default)
    return getattr(obj, name, default)


def _writing_tells(path: Path, mode: str, scope: str, text: str | None = None) -> tuple[list[tuple[str, int]], int]:
    """(blocking tells as (class, line), count of all tells) of the writing check, imported when needed. With
    `text` and a module that offers check_text and check_name, the check runs on that text (the bytes that were
    hashed) and on the name of `path`; else writing.check_file reads `path`."""
    try:
        mod = importlib.import_module("awb.writing")
    except ImportError:
        mod = None
    fn = getattr(mod, "check_file", None) if mod is not None else None
    by_text = getattr(mod, "check_text", None) if mod is not None else None
    by_name = getattr(mod, "check_name", None) if mod is not None else None
    if fn is None and not (text is not None and by_text and by_name):
        raise _WritingUnavailable("the writing check is not installed")
    try:
        if text is not None and callable(by_text) and callable(by_name):
            tells, _ = by_text(text, mode=mode, scope=scope)
            result = (list(by_name(Path(path).name, mode=mode, scope=scope)) + list(tells), {})
        else:
            result = fn(path, mode=mode, scope=scope)
    except Exception as exc:
        raise _WritingUnavailable("the writing check failed (%s)" % type(exc).__name__) from None
    tells = result[0] if isinstance(result, tuple) else result
    tells = list(tells or [])
    out: list[tuple[str, int]] = []
    for t in tells:
        if not _field(t, "blocking", False):
            continue
        line = _field(t, "line", 0)
        out.append((str(_field(t, "cls", "unknown")), line if isinstance(line, int) else 0))
    return out, len(tells)


_KB_EVIDENCE_RE = re.compile(r"kb:(KB-[A-Z0-9]{4})")
_CALC_EVIDENCE_RE = re.compile(r"calc:(K-[1-9][0-9]{0,4})")
_ARITH_RE = re.compile(
    r"(?i)\b(?:total|totals|totalling|totaling|sum|subtotal|altogether|in all|saves?|saving|savings|difference"
    r"|average)\b|\u00d7|(?<![\w.])[0-9]+(?:[.,][0-9]+)?\s?[x*\u00d7]\s?[0-9]"
)
"""A sentence whose number was computed: a total, a sum, a saving, a difference, an average or a product (R-005)."""


def _kb_evidence_problem(kb_id: str, high: bool, kb_where=None, today: date | None = None) -> str | None:
    """A knowledge entry given as evidence (`kb:KB-XXXX`, V-04): it must exist, not be retired and not have
    expired; for a high-risk claim its grade must be live or contract."""
    from awb import kb

    try:
        e = kb.get(kb_id, where=kb_where)
    except (kb.KBError, OSError):
        return "a knowledge entry given as evidence does not exist"
    if e.is_retired:
        return "a knowledge entry given as evidence is retired"
    if e.is_expired(today):
        return "a knowledge entry given as evidence has expired"
    if high and e.grade not in SETTLING_GRADES:
        return "a knowledge entry settles a high-risk claim only with the grade live or contract"
    return None


def _calc_evidence_problem(project: Path, calc_id: str, sentence: str | None) -> str | None:
    """A calculation given as evidence (`calc:K-N`, R-005): it must be recorded in the project and its result must
    appear in the sentence."""
    from awb import calc

    try:
        rec = calc.find(project, calc_id)
    except calc.CalcError:
        return "the calculation record of the project cannot be read"
    if rec is None:
        return "a calculation given as evidence is not recorded in the project"
    try:
        value = rec.value()
    except ArithmeticError:
        return "a calculation given as evidence has no readable result"
    if sentence is not None and not calc.appears_in(value, sentence):
        return "the result of the calculation given as evidence is not in the sentence"
    return None


def _evidence_problem(project: Path, evidence: str, deliverable: Path, high: bool = False, kb_where=None,
                      today: date | None = None, sentence: str | None = None) -> str | None:
    parts = [p.strip() for p in evidence.split(";") if p.strip()]
    if not parts:
        return "no evidence"
    root = Path(os.path.abspath(project))
    for part in parts:
        m = _KB_EVIDENCE_RE.fullmatch(part)
        if m:
            problem = _kb_evidence_problem(m.group(1), high, kb_where, today)
            if problem:
                return problem
            continue
        m = _CALC_EVIDENCE_RE.fullmatch(part)
        if m:
            problem = _calc_evidence_problem(project, m.group(1), sentence)
            if problem:
                return problem
            continue
        if "://" in part:
            return "evidence must be a path in the project, not a link"
        p = Path(part)
        if p.is_absolute():
            return "an evidence path must be relative to the project"
        full = Path(os.path.abspath(root / p))
        try:
            full.relative_to(root)
        except ValueError:
            return "an evidence path leaves the project"
        if not full.exists():
            return "an evidence path does not exist"
        if full == Path(os.path.abspath(deliverable)):
            return "the deliverable cannot be its own evidence"
    return None


def _claim_problems(c: Claim, row: Claim, project: Path, deliverable: Path, tier: int, kb_where=None) -> list[str]:
    where = "claim %s (line %d, %s)" % (row.id, c.line, c.risk)
    out: list[str] = []
    if row.verdict and row.verdict not in VERDICTS:
        out.append("%s: the verdict must be supported, contradicted or unknown" % where)
    if row.grade and row.grade not in GRADES:
        out.append("%s: the grade must be live, contract, docs, said or assumed" % where)
    if c.risk == "high":
        problem = _evidence_problem(project, row.evidence, deliverable, True, kb_where, sentence=c.sentence)
        if problem:
            out.append("%s: %s" % (where, problem))
        elif "number" in c.kinds() and _ARITH_RE.search(c.sentence) and not _CALC_EVIDENCE_RE.search(row.evidence):
            out.append("%s: a computed number needs its calculation as evidence (awb calc, then calc:K-N)" % where)
        if not row.verdict:
            out.append("%s: no verdict" % where)
    elif row.evidence:
        problem = _evidence_problem(project, row.evidence, deliverable, False, kb_where, sentence=c.sentence)
        if problem:
            out.append("%s: %s" % (where, problem))
    if row.verdict == "contradicted":
        out.append("%s: contradicted" % where)
    if row.verdict == "unknown" and tier >= UNKNOWN_BLOCKS_FROM:
        out.append("%s: unknown, which blocks from tier %d" % (where, UNKNOWN_BLOCKS_FROM))
    return out


def l0(deliverable, register_path=None, kb_where=None) -> L0Result:
    """The level-0 check. Raises ReviewError when it cannot run (no contract, no register, the name check
    unavailable); every finding is a problem in the result, and any problem blocks."""
    path, project, rel = _existing_file(deliverable)
    folder = review_dir(project, rel)
    contract = load_contract(folder / CONTRACT, _platform(project))
    reg = _register(register_path)
    _ensure_checkable(reg)
    # read once: every check below sees these bytes and the result carries their hash, so a file that changes
    # while the checks run can never get a record for bytes that were not checked
    data = _read_bytes(path)
    if _read_plain(data) is not None:
        return _l0_of(path, path, rel, project, folder, contract, reg, data, kb_where)
    with _snapshot(path, data) as snap:
        return _l0_of(path, snap, rel, project, folder, contract, reg, data, kb_where)


def _l0_of(path: Path, source: Path, rel: str, project: Path, folder: Path, contract: Contract, reg: Path,
           data: bytes, kb_where=None) -> L0Result:
    """The checks of l0 over `data`. `source` is a file with exactly these bytes and the name of the
    deliverable: the deliverable itself for a plain text file, a private snapshot for any other kind."""
    digest = hashlib.sha256(data).hexdigest()
    text, plain = _deliverable_text(source, data)
    problems: list[str] = []

    tell_count = 0
    scope = _scope(project, contract)
    try:
        blocking, tell_count = _writing_tells(source, contract.mode, scope, text if plain else None)
    except _WritingUnavailable as exc:
        problems.append("writing: %s" % exc)
        blocking = []
    for cls, line in blocking:
        problems.append("writing: %s line %d" % (cls, line))

    hits = _deliverable_hits(source, rel, text, plain, reg)
    if hits:
        problems.append("name check: %d hits (%s)" % (len(hits), _counts(hits)))

    if _platform(project) != "hcs":
        # offered is what the latest service description lists: a deliverable never proposes anything else
        from awb import offered
        catalog = offered.load()
        for name in offered.mentions(text, catalog):
            problems.append("service: %s is not offered on TCP (the service description of %s)"
                            % (name, offered.revision_label(catalog)))

    words = count_words(text)
    if words > contract.budget:
        problems.append("words: %d, over the budget of %d" % (words, contract.budget))

    fresh = find_claims(text)
    high = sum(1 for c in fresh if c.risk == "high")
    stale = 0
    cpath = folder / CLAIMS
    if not cpath.exists():
        if fresh:
            problems.append("claims.tsv is missing: run awb review claims")
    else:
        pairs, stale = _pair(fresh, load_claims(cpath))
        missing = [c for c, row in pairs if row is None]
        if missing:
            problems.append("claims.tsv misses %d claims of the current text (lines %s): run awb review claims "
                            "again" % (len(missing), ", ".join(str(c.line) for c in missing[:20])))
        for c, row in pairs:
            if row is not None:
                problems.extend(_claim_problems(c, row, project, path, contract.tier, kb_where))
    return L0Result(blocked=bool(problems), problems=problems, tier=contract.tier, mode=contract.mode, words=words,
                    budget=contract.budget, tells=tell_count, name_hits=len(hits), claims=len(fresh), high=high,
                    stale_rows=stale, sha256=digest)


# --------------------------------------------------------------------------- lenses and the pass


def _is_blocking(finding: dict) -> bool:
    return finding.get("blocking") is True or str(finding.get("severity") or "").strip().lower() == "blocking"


def _lens_label(n: int, stem: str, reg: Path | None) -> str:
    if reg is not None and re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,40}", stem) and _is_clean(stem, reg):
        return "lens %d (%s)" % (n, stem)
    return "lens %d" % n


def lens_summary(folder: Path, tier: int, register_path=None) -> tuple[dict, list[str]]:
    """(summary, problems) of the lens files in `folder`. At tier 3 at least one file is required. In any file,
    a blocking finding without the outcome confirmed-fixed or refuted is a problem."""
    folder = Path(folder)
    files = sorted(p for p in folder.glob("*.json") if p.is_file()) if folder.is_dir() else []
    summary = {"required": tier >= LENSES_REQUIRED_FROM, "files": len(files), "findings": 0, "blocking": 0,
               "open_blocking": 0, "refuted": 0, "confirmed_fixed": 0}
    problems: list[str] = []
    if tier >= LENSES_REQUIRED_FROM and not files:
        problems.append("tier %d needs the lens results in lenses/: run the review workflow" % tier)
    reg = _register(register_path) if files else None
    for n, f in enumerate(files, start=1):
        label = _lens_label(n, f.stem, reg)
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, ValueError):
            problems.append("%s cannot be read as JSON" % label)
            continue
        findings = data.get("findings") if isinstance(data, dict) else None
        if not isinstance(findings, list) or not all(isinstance(x, dict) for x in findings):
            problems.append("%s has no list of findings" % label)
            continue
        open_blocking = 0
        for x in findings:
            summary["findings"] += 1
            outcome = str(x.get("outcome") or "").strip().lower()
            if outcome == "refuted":
                summary["refuted"] += 1
            elif outcome == "confirmed-fixed":
                summary["confirmed_fixed"] += 1
            if _is_blocking(x):
                summary["blocking"] += 1
                if outcome not in CLOSED_OUTCOMES:
                    open_blocking += 1
        if open_blocking:
            summary["open_blocking"] += open_blocking
            problems.append("%s: %d blocking findings without the outcome confirmed-fixed or refuted"
                            % (label, open_blocking))
    return summary, problems


def lens_counts(folder: Path, register_path=None) -> list[dict]:
    """Per lens file (V-12, V-18): the lens (its file name when it passes the name check), its kind, the findings,
    the blocking ones, the open ones (blocking and not) and the outcomes refuted, confirmed-fixed, softened and
    plausible,
    plus the findings the workflow dropped for lack of a quote or evidence (V-09). A file that cannot be read is
    counted with the state unreadable."""
    folder = Path(folder)
    files = sorted(p for p in folder.glob("*.json") if p.is_file()) if folder.is_dir() else []
    reg = _register(register_path) if files else None
    out: list[dict] = []
    for n, f in enumerate(files, start=1):
        label = _lens_label(n, f.stem, reg)
        row = {"lens": label, "kind": "", "findings": 0, "blocking": 0, "open_blocking": 0, "open_other": 0,
               "refuted": 0, "confirmed_fixed": 0, "softened": 0, "plausible": 0, "dropped": 0, "state": "read"}
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, ValueError):
            data = None
        findings = data.get("findings") if isinstance(data, dict) else None
        if not isinstance(findings, list) or not all(isinstance(x, dict) for x in findings):
            row["state"] = "unreadable"
            out.append(row)
            continue
        kind = data.get("kind")
        row["kind"] = kind if isinstance(kind, str) and re.fullmatch(r"[a-z-]{1,20}", kind) else ""
        dropped = data.get("dropped")
        row["dropped"] = dropped if isinstance(dropped, int) and not isinstance(dropped, bool) and dropped > 0 else 0
        for x in findings:
            row["findings"] += 1
            outcome = str(x.get("outcome") or "").strip().lower()
            key = outcome.replace("-", "_")
            if key in ("refuted", "confirmed_fixed", "softened", "plausible"):
                row[key] += 1
            if _is_blocking(x):
                row["blocking"] += 1
                if outcome not in CLOSED_OUTCOMES:
                    row["open_blocking"] += 1
            elif outcome not in CLOSED_OUTCOMES:
                row["open_other"] += 1
        out.append(row)
    return out


def claim_counts(path: Path) -> dict:
    """The claims of a claims.tsv by risk, verdict and grade, and how many cite a knowledge entry (V-18)."""
    try:
        rows = load_claims(path)
    except ReviewError:
        return {"rows": 0, "readable": False}
    return {
        "rows": len(rows),
        "readable": True,
        "high": sum(1 for r in rows if r.risk == "high"),
        "by_verdict": dict(sorted(Counter(r.verdict or "none" for r in rows).items())),
        "by_grade": dict(sorted(Counter(r.grade or "none" for r in rows).items())),
        "kb_evidence": sum(1 for r in rows if _KB_EVIDENCE_RE.search(r.evidence or "")),
    }


_QUESTION_BOX_RE = re.compile(r"(?m)^- \[( |x|X)\] (?!\(fill in\))\S")


def contract_counts(path: Path, contract: Contract) -> dict:
    """What the record keeps of the contract (V-18): tier, budget, mode, scope, whether he lowered the tier and
    the questions to answer with how many are ticked."""
    try:
        text = Path(path).read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        text = ""
    boxes = _QUESTION_BOX_RE.findall(text)
    return {"tier": contract.tier, "budget": contract.budget, "mode": contract.mode, "scope": contract.scope,
            "lowered": bool(contract.lowered), "questions": len(boxes),
            "ticked": sum(1 for b in boxes if b in ("x", "X"))}


def run_counts(folder: Path) -> dict:
    """Agent runs and tokens that the workflow wrote into its lens files (a key "run" with "agents" and
    "tokens"), summed. Zero when the files carry none."""
    folder = Path(folder)
    agents = tokens = 0
    for f in sorted(folder.glob("*.json")) if folder.is_dir() else []:
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, ValueError):
            continue
        run = data.get("run") if isinstance(data, dict) else None
        if isinstance(run, dict):
            for key in ("agents", "tokens"):
                v = run.get(key)
                if isinstance(v, int) and not isinstance(v, bool) and v > 0:
                    if key == "agents":
                        agents += v
                    else:
                        tokens += v
    return {"agents": agents, "tokens": tokens}


@dataclass
class PassResult:
    passed: bool
    problems: list[str]
    record: dict | None
    record_path: Path
    l0: L0Result
    lenses: dict


def _hash_or_none(path: Path) -> str | None:
    try:
        return sha256_file(path)
    except FileNotFoundError:
        return None
    except OSError:
        return "unreadable"


def bindings(folder: Path) -> dict:
    """What a record is bound to besides the deliverable: the hashes of contract.md, claims.tsv and every lens
    file of the review folder `folder` (None for a file that is not there)."""
    folder = Path(folder)
    lens_dir = folder / LENSES
    lenses: dict[str, str | None] = {}
    if lens_dir.is_dir():
        for f in sorted(lens_dir.glob("*.json")):
            if f.is_file():
                lenses[f.name] = _hash_or_none(f)
    return {"contract": _hash_or_none(folder / CONTRACT), "claims": _hash_or_none(folder / CLAIMS),
            "lenses": lenses}


def run_pass(deliverable, register_path=None, today: str | None = None, kb_where=None) -> PassResult:
    """`awb review pass`: l0, the lens check, then record.json. A failed pass removes an older record.

    The record carries the hash of exactly the bytes l0 checked and the hashes of the contract, the claim list
    and every lens file. `status` calls it stale as soon as any of them differs. It also keeps what the review
    found (V-18): the contract in numbers, the claims by risk, verdict and grade, the counts per lens (V-12), the
    agent runs and tokens the workflow reported and the open points (unknown claims, open findings that do not
    block)."""
    path, project, rel = _existing_file(deliverable)
    folder = review_dir(project, rel)
    bound = bindings(folder)
    res = l0(path, register_path, kb_where)
    lenses, lens_problems = lens_summary(folder / LENSES, res.tier, register_path)
    problems = list(res.problems) + lens_problems
    try:
        changed = sha256_file(path) != res.sha256
    except OSError:
        changed = True
    if changed:
        problems.append("the deliverable changed while the pass ran")
    if bindings(folder) != bound:
        problems.append("the contract, the claim list or a lens file changed while the pass ran")
    record_path = folder / RECORD
    if problems:
        try:
            record_path.unlink()
        except FileNotFoundError:
            pass
        return PassResult(False, problems, None, record_path, res, lenses)
    per_lens = lens_counts(folder / LENSES, register_path)
    claims = claim_counts(folder / CLAIMS)
    record = {
        "deliverable": "%s/%s" % (DELIVERABLES, rel),
        "sha256": res.sha256,
        "tier": res.tier,
        "date": today or date.today().isoformat(),
        "l0": res.summary(),
        "lenses": lenses,
        "bound": bound,
        "contract": contract_counts(folder / CONTRACT, load_contract(folder / CONTRACT, _platform(project))),
        "claims": claims,
        "per_lens": per_lens,
        "run": run_counts(folder / LENSES),
        "open_points": {"unknown_claims": (claims.get("by_verdict") or {}).get("unknown", 0),
                        "open_findings": sum(r["open_other"] for r in per_lens)},
    }
    _write_atomic(record_path, json.dumps(record, indent=2, sort_keys=True) + "\n")
    return PassResult(True, [], record, record_path, res, lenses)


# --------------------------------------------------------------------------- status


VALID = "valid"
MISSING = "missing"
STALE = "stale"
UNREADABLE = "unreadable"
UNREADABLE_KINDS = ("image", "unknown", "msg")
"""Kinds of awb.extract.sniff that no review can read as text: such a deliverable can never get a record."""


def _record_state(deliverable: Path, record: Path) -> tuple[str, int | None]:
    """valid only when the record's hash is the deliverable's, its bindings (contract, claims, lens files) are
    those of the review folder now, and it says that l0 passed with no open blocking lens finding."""
    try:
        data = json.loads(record.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, ValueError):
        return MISSING, None
    if not isinstance(data, dict) or not isinstance(data.get("sha256"), str):
        return MISSING, None
    tier = data.get("tier") if isinstance(data.get("tier"), int) else None
    try:
        digest = sha256_file(deliverable)
    except OSError:
        return STALE, tier
    if digest != data["sha256"]:
        return STALE, tier
    l0_summary, lenses = data.get("l0"), data.get("lenses")
    if not isinstance(l0_summary, dict) or l0_summary.get("blocked") is not False \
            or l0_summary.get("sha256") != digest:
        return STALE, tier
    if not isinstance(lenses, dict) or lenses.get("open_blocking") != 0:
        return STALE, tier
    if tier not in TIERS or data.get("bound") != bindings(record.parent):
        return STALE, tier
    return VALID, tier


def _unreadable(path: Path) -> bool:
    """True for a deliverable that no review can read as text (an image, an unknown binary, an Outlook file)."""
    try:
        from awb import extract
        return extract.sniff(path) in UNREADABLE_KINDS
    except Exception:
        return False


def _placeholder(path: Path, rel_parts: tuple[str, ...]) -> bool:
    """The empty .gitkeep that spawn puts into deliverables/ (at any depth). A .gitkeep with content counts."""
    try:
        return rel_parts[-1] == ".gitkeep" and path.stat().st_size == 0
    except OSError:
        return False


def status(project: Path) -> list[dict]:
    """Every file under deliverables/ as {"file": "deliverables/<name>", "state": valid | missing | stale |
    unreadable, "tier": the tier of the record or None}. Hidden files count too (a deliverable named .plan.md is
    a deliverable); only an empty .gitkeep is left out. Stale: the record does not match the deliverable or its
    review files any more. Unreadable: a file without a valid record that no review can read as text."""
    project = Path(project)
    base = project / DELIVERABLES
    rows: list[dict] = []
    if not base.is_dir():
        return rows
    for f in sorted(base.rglob("*")):
        rel_parts = f.relative_to(base).parts
        if not f.is_file() or _placeholder(f, rel_parts):
            continue
        rel = "/".join(rel_parts)
        state, tier = _record_state(f, review_dir(project, rel) / RECORD)
        if state != VALID and _unreadable(f):
            state = UNREADABLE
        rows.append({"file": "%s/%s" % (DELIVERABLES, rel), "state": state, "tier": tier})
    return rows


SEND_OK = "ok"
SEND_WARN = "warn"
SEND_REFUSE = "refuse"


_TEXT_PART_SHARE = 0.3
"""A file whose text is this share of a deliverable's text (or more) is a part of that deliverable."""
_TEXT_SIMILARITY = 0.6
"""A file whose words match a deliverable's words to this ratio is a changed copy of it."""
_MAX_MATCH_CHARS = 400_000
_MAX_MATCH_WORDS = 20_000


def _text_of(path: Path) -> str:
    """The text a reader gets from the file, its children included (an archive, a mail, an office file), through
    awb.extract when it is there; else the bytes as UTF-8."""
    try:
        from awb import extract

        ex = extract.extract(Path(path))
        parts: list[str] = []

        def walk(e) -> None:
            parts.append(e.text or "")
            for child in e.children or []:
                walk(child)

        walk(ex)
        text = "\n".join(parts)
        if text.strip():
            return text[:_MAX_MATCH_CHARS]
    except Exception:
        pass
    try:
        return Path(path).read_text(encoding="utf-8", errors="replace")[:_MAX_MATCH_CHARS]
    except OSError:
        return ""


def _words(text: str) -> list[str]:
    return " ".join(text.split()).casefold().split()


def _same_text(file_words: list[str], deliverable_words: list[str]) -> bool:
    """The file carries the deliverable: its whole text (and more), a part of it worth _TEXT_PART_SHARE, or a
    changed copy whose words still match to _TEXT_SIMILARITY."""
    if not file_words or not deliverable_words:
        return False
    f, d = " ".join(file_words), " ".join(deliverable_words)
    if d in f:
        return True
    if f in d and len(f) >= _TEXT_PART_SHARE * len(d):
        return True
    if len(file_words) > _MAX_MATCH_WORDS or len(deliverable_words) > _MAX_MATCH_WORDS:
        return False
    import difflib

    return difflib.SequenceMatcher(None, file_words, deliverable_words, autojunk=False).ratio() >= _TEXT_SIMILARITY


def _under(path: Path, folder: Path) -> bool:
    try:
        path.relative_to(folder)
        return True
    except ValueError:
        return False


def _deliverables_of(project: Path) -> list[tuple[str, Path, str]]:
    """(relative name, real path, record state) of every deliverable of a project."""
    return [(row["file"][len(DELIVERABLES) + 1:], Path(os.path.realpath(project / row["file"])), row["state"])
            for row in status(project)]


def _pick(matches: list[tuple[str, str]]) -> str | None:
    """Of several deliverables a file matches (the same text reviewed under two names), the one with a valid
    record, else the first."""
    for rel, state in matches:
        if state == VALID:
            return rel
    return matches[0][0] if matches else None


def _match_deliverable(f: Path, digest: str, project: Path) -> tuple[str | None, bool]:
    """(relative name of the deliverable the file is, whether the file is a changed copy of it). By path, then
    by bytes, then by text."""
    base = Path(os.path.realpath(project / DELIVERABLES))
    if _under(f, base):
        return f.relative_to(base).as_posix(), False
    rows = _deliverables_of(project)
    same_bytes: list[tuple[str, str]] = []
    for rel, real, state in rows:
        try:
            if sha256_file(real) == digest:
                same_bytes.append((rel, state))
        except OSError:
            continue
    if same_bytes:
        return _pick(same_bytes), False
    file_words = _words(_text_of(f))
    same_text = [(rel, state) for rel, real, state in rows if _same_text(file_words, _words(_text_of(real)))]
    if same_text:
        return _pick(same_text), True
    return None, False


def _request_lens_for(folder: Path, rel: str) -> bool:
    """A request check ran for this deliverable: a lens file of kind request whose deliverable field, when it has
    one, names this deliverable and not another."""
    lens_dir = folder / LENSES
    if not lens_dir.is_dir():
        return False
    for f in sorted(lens_dir.glob("*.json")):
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, ValueError):
            continue
        if not isinstance(data, dict) or data.get("kind") != REQUEST_KIND or not isinstance(data.get("findings"), list):
            continue
        named = data.get("deliverable")
        if isinstance(named, str) and named not in ("%s/%s" % (DELIVERABLES, rel), rel):
            continue
        return True
    return False


def _content_hits(f: Path, register_path) -> tuple[int, Counter]:
    """(registered names in the file, the structured classes and their counts) through the name check."""
    from awb import check

    reg = register_path if register_path is not None else config.paths().register
    hits = check.check_file(f, reg)
    names = sum(1 for h in hits if h.get("cls") in ("name", "path"))
    other = Counter(str(h.get("cls")) for h in hits if h.get("cls") not in ("name", "path"))
    return names, other


def send_check(project: Path, file: Path, for_customer: bool, register_path=None, workbench=None,
               digest_out: list | None = None, name: str | None = None) -> tuple[str, str]:
    """The send gate (decision 15), run before a file leaves the machine through the bucket: (verdict, message),
    the verdict ok, warn or refuse.

    Both paths are taken as the kernel opens them (real paths, links followed), so the file that is judged is
    the file that leaves; `digest_out` receives the hash of the bytes judged, so that the caller can prove it
    uploads the same bytes; `name`, the name the file leaves under when it is not its own (`xchg put --as`), is
    checked like the file's own name. A deliverable is the file itself under deliverables/, a file with the same bytes as
    one, or a file that carries its text: the whole text and more, a part of it, a changed copy, also inside a
    container (an office file, a pdf, an archive, a mail). Matched, the deliverable's record decides: for a
    project with a customer or partner (`for_customer`) the gate refuses one without a valid record, a changed
    copy, one whose record does not carry the tier of its contract, one reviewed below tier 3 unless he lowered
    the tier (decision 14) and one at tier 3 whose lenses hold no request check of this deliverable (V-16). The
    review material of a project (its reviews/ folder) and a deliverable of another project of the Workbench
    (`workbench`, the Paths) never leave. Anything else is an internal file: it leaves with a warning, unless it
    or its name carries a registered name, which refuses whatever the project (a name in a text a session wrote
    is a leak; the owner puts names back with --reveal after the gate). The message carries states, tiers and
    counts, never a file name or a value."""
    project = Path(os.path.realpath(project))
    f = Path(os.path.realpath(file))
    try:
        digest = sha256_file(f)
    except OSError:
        return SEND_REFUSE, "the file cannot be read"
    if digest_out is not None:
        digest_out.append(digest)
    others: list[Path] = []
    if workbench is not None:
        # every registered project and every project folder under the projects root
        found: set[Path] = set()
        try:
            from awb import projects as _projects

            found.update(Path(os.path.realpath(pr.path)) for pr in _projects.load(workbench))
        except Exception:
            pass
        try:
            found.update(Path(os.path.realpath(d)) for d in Path(workbench.projects_root).glob("tcp-*")
                         if (d / DELIVERABLES).is_dir())
        except OSError:
            pass
        others = sorted(d for d in found if d != project)
    for root in [project] + others:
        if _under(f, root / REVIEWS):
            return SEND_REFUSE, "the review material of a project (its contract, claims and lenses) never leaves"
    try:
        from awb import check

        reg = register_path if register_path is not None else config.paths().register
        if any(h.get("cls") == "name" for n in {f.name, name or f.name} for h in check.check_text(n, reg)):
            return SEND_REFUSE, "the file name carries a registered name"
    except Exception:
        return SEND_REFUSE, "the name check did not run, nothing leaves unchecked"
    rel, edited = _match_deliverable(f, digest, project)
    if rel is None:
        for root in others:
            other_rel, _ = _match_deliverable(f, digest, root)
            if other_rel is not None:
                return SEND_REFUSE, "the file carries the text of a deliverable of another project"
        try:
            names, other = _content_hits(f, register_path)
        except Exception:
            return SEND_REFUSE, "the name check did not run, nothing leaves unchecked"
        if names:
            return SEND_REFUSE, ("the file carries %d registered name(s): nothing with a name leaves through the "
                                 "bucket, put names back with --reveal after the gate" % names)
        if other:
            counts = ", ".join("%s %d" % kv for kv in sorted(other.items()))
            return SEND_WARN, ("not a reviewed deliverable of the project: it leaves as an internal file, and it "
                               "carries structured data (%s)" % counts)
        return SEND_WARN, "not a reviewed deliverable of the project: it leaves as an internal file"
    folder = review_dir(project, rel)
    state, tier = _record_state(project / DELIVERABLES / rel, folder / RECORD)
    if edited:
        return (SEND_REFUSE if for_customer else SEND_WARN,
                "a changed copy of a deliverable: the review does not cover it (%s), review the copy or send the "
                "reviewed file" % state)
    if state != VALID:
        return (SEND_REFUSE if for_customer else SEND_WARN,
                "the deliverable has no valid review record (%s): run awb review pass" % state)
    try:
        contract = load_contract(folder / CONTRACT, _platform(project))
    except ReviewError:
        return SEND_REFUSE, "the contract of the review cannot be read"
    if tier != contract.tier:
        return SEND_REFUSE, "the record does not carry the tier of its contract: run awb review pass again"
    if not for_customer:
        return SEND_OK, "reviewed at tier %d" % tier
    if tier < SEND_TIER and not contract.lowered:
        return SEND_REFUSE, ("reviewed at tier %d, but a text for a customer or partner needs tier %d unless he "
                             "lowered it (awb review init --lowered)" % (tier, SEND_TIER))
    if tier >= SEND_TIER and not _request_lens_for(folder, rel):
        return SEND_REFUSE, "the request check has not run for this deliverable (workflows/request-check.js)"
    return SEND_OK, "reviewed at tier %d%s" % (tier, ", lowered by him" if contract.lowered else "")


def find_project(start: Path | None = None) -> Path:
    """The nearest folder at or above `start` (default: the working folder) with a SCOPE.md, else `start`."""
    here = Path(os.path.abspath(start or Path.cwd()))
    for folder in (here, *here.parents):
        if (folder / "SCOPE.md").is_file():
            return folder
    return here


# --------------------------------------------------------------------------- command line


def _print_problems(problems: list[str]) -> None:
    for p in problems:
        print("- %s" % p)


def _cmd_init(args) -> int:
    target = init(args.deliverable, args.request, args.tier, args.budget, reader=args.reader, mode=args.mode,
                  force=args.force, lowered=args.lowered)
    project, rel = locate(args.deliverable)
    reg = _register(None)
    print("review init: wrote the contract of %s (tier %d%s, budget %d words, mode %s)"
          % (_label(rel, reg), args.tier, ", lowered by him" if args.lowered else "", args.budget, args.mode))
    print("next: fill the questions and the out of scope list in %s" % (
        target.relative_to(project).as_posix() if _is_clean(rel, reg) else "contract.md"))
    return EXIT_OK


def _cmd_claims(args) -> int:
    res = write_claims(args.deliverable)
    risks = Counter(r.risk for r in res.rows)
    project, rel = locate(args.deliverable)
    print("review claims: %d claims of %s (high %d, medium %d, low %d); %d rows kept their evidence, %d rows of "
          "sentences that are gone were dropped" % (len(res.rows), _label(rel, _register(None)), risks["high"],
                                                   risks["medium"], risks["low"], res.kept, res.dropped))
    print("review claims: %d claims have knowledge entries to read first, listed in kb.tsv; cite one as "
          "kb:KB-XXXX" % res.kb_rows)
    return EXIT_OK


def _l0_line(res: L0Result) -> str:
    return ("%d of %d words, %d claims (%d high), %d writing tells, %d name hits"
            % (res.words, res.budget, res.claims, res.high, res.tells, res.name_hits))


def _cmd_l0(args) -> int:
    res = l0(args.deliverable)
    _, rel = locate(args.deliverable)
    label = _label(rel, _register(None))
    if res.blocked:
        print("l0 blocked: %s (tier %d): %s" % (label, res.tier, _l0_line(res)))
        _print_problems(res.problems)
        return EXIT_FINDINGS
    print("l0 passed: %s (tier %d): %s" % (label, res.tier, _l0_line(res)))
    return EXIT_OK


def _cmd_pass(args) -> int:
    res = run_pass(args.deliverable)
    _, rel = locate(args.deliverable)
    label = _label(rel, _register(None))
    if not res.passed:
        print("pass blocked: %s (tier %d)" % (label, res.l0.tier))
        _print_problems(res.problems)
        return EXIT_FINDINGS
    print("pass: %s (tier %d): %s; %d lens files, %d blocking findings closed; record.json written"
          % (label, res.l0.tier, _l0_line(res.l0), res.lenses["files"], res.lenses["blocking"]))
    return EXIT_OK


def _cmd_status(args) -> int:
    project = Path(args.project) if args.project else find_project()
    rows = status(project)
    if not rows:
        print("no deliverables")
        return EXIT_OK
    reg = _register(None)
    for n, row in enumerate(rows, start=1):
        shown = row["file"] if _is_clean(row["file"], reg) else "file %d" % n
        print("%-7s  %s" % (row["state"], shown))
    return EXIT_OK if all(r["state"] == VALID for r in rows) else EXIT_FINDINGS


def _cmd_calibrate(args) -> int:
    from awb import calibrate

    if args.lenses is not None:
        return calibrate.main(["--lenses", str(args.lenses)])
    return calibrate.main(["--json"] if args.json else [])


def _exit_code(exc: SystemExit) -> int:
    if exc.code is None:
        return EXIT_OK
    return exc.code if isinstance(exc.code, int) else EXIT_ERROR


def _positive(value: str) -> int:
    """argparse type: a positive whole number. The error never repeats the value (SafeParser masks it)."""
    if not re.fullmatch(r"[0-9]{1,7}", value or "") or int(value) <= 0:
        raise ValueError("not a positive number")
    return int(value)


def main(argv: list[str] | None = None) -> int:
    """`awb review init|claims|l0|pass|status|calibrate`. Exit 0 ok, 1 blocked or refused, 2 error."""
    from awb.cli import SafeParser

    argv = list(sys.argv[1:] if argv is None else argv)
    prog = "awb review"
    ap = SafeParser(prog=prog, description="Review a deliverable: contract, claims, level-0 check, pass record.")
    sub = ap.add_subparsers(dest="command", parser_class=SafeParser)
    s = sub.add_parser("init", help="write the contract of a deliverable")
    s.add_argument("deliverable", type=Path)
    s.add_argument("--request", required=True, type=Path, metavar="FILE", help="the request as a text file")
    s.add_argument("--tier", required=True, type=int, choices=TIERS)
    s.add_argument("--budget", required=True, type=_positive, metavar="WORDS")
    s.add_argument("--reader", default=None, metavar="TEXT", help="who reads it (codes and roles only)")
    s.add_argument("--mode", default="doc", choices=MODES, help="the mode of the writing check (default doc)")
    s.add_argument("--force", action="store_true", help="write the contract again when it exists")
    s.add_argument("--lowered", default=None, metavar="TEXT",
                   help="his words when he lowered the tier of a text for a customer or partner below 3")
    s.set_defaults(func=_cmd_init)
    s = sub.add_parser("claims", help="list the claims of a deliverable in claims.tsv")
    s.add_argument("deliverable", type=Path)
    s.set_defaults(func=_cmd_claims)
    s = sub.add_parser("l0", help="the level-0 check")
    s.add_argument("deliverable", type=Path)
    s.set_defaults(func=_cmd_l0)
    s = sub.add_parser("pass", help="l0 and the lenses, then record.json")
    s.add_argument("deliverable", type=Path)
    s.set_defaults(func=_cmd_pass)
    s = sub.add_parser("status", help="the record state of every deliverable of a project")
    s.add_argument("project", nargs="?", default=None, type=Path)
    s.set_defaults(func=_cmd_status)
    s = sub.add_parser("calibrate", help="run the calibration set through the script steps of the review")
    s.add_argument("--json", action="store_true", help="print the result as JSON")
    s.add_argument("--lenses", type=Path, default=None, metavar="PROJECT",
                   help="score the lens files the workflows wrote for the workflow cases in PROJECT")
    s.set_defaults(func=_cmd_calibrate)
    try:
        args = ap.parse_args(argv)
    except SystemExit as exc:
        return _exit_code(exc)
    func = getattr(args, "func", None)
    if func is None:
        ap.print_usage(sys.stderr)
        return EXIT_ERROR
    try:
        return func(args)
    except Refused as err:
        print("%s: %s" % (prog, err), file=sys.stderr)
        return EXIT_FINDINGS
    except ReviewError as err:
        print("%s: %s" % (prog, err), file=sys.stderr)
        return EXIT_ERROR
    except OSError as err:
        print("%s: %s (operating system error)" % (prog, type(err).__name__), file=sys.stderr)
        return EXIT_ERROR


if __name__ == "__main__":
    sys.exit(main())
