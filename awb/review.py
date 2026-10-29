"""Review of a deliverable: the contract, the claim list, the level-0 check, the pass record and the status.

Per deliverable `<project>/deliverables/<name>` the review lives in `<project>/reviews/<name>/`:

    contract.md    what was asked, written by `init`: the request as given, the reader, the questions to answer
                   (a checklist he fills), what is out of scope, the word budget, the tier, the writing mode and,
                   when he lowered the tier of a text for a customer or partner, his words
    claims.tsv     written by `claims`: every sentence that carries a checkable claim, with evidence, grade and
                   verdict filled by hand or from the review workflow. A row added by hand stays on a re-run while
                   its sentence is in the text; one whose sentence is gone is dropped and named by its id
    kb.tsv         written by `claims` next to it: per claim the knowledge entries `kb find` returns (V-04) and
                   whether the base knew it before the work (column known_before, task KF: yes when an entry of
                   those shares KNOWN_SHARE of the claim and was checked before the project was created)
    lenses/*.json  written by the review workflow (workflows/review.js) for tier 2 and 3 and by the request check
                   (workflows/request-check.js, kind "request")
    record.json    written by `pass`: the sha256 of the deliverable, tier, date, the l0 result, the lens summary
                   and what the review found: the contract in numbers, the claims by verdict and grade, the counts
                   per lens, the agent runs and tokens and the open points

Commands (`awb review ...`):

    init DELIVERABLE --request FILE [--tier 0..3] --budget WORDS [--reader TEXT] [--mode mail|doc|chat]
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

At tier 3 a blind verification lens (verify.json) that checked 0 claims while the text has high-risk claims whose
verdict no blind check set refuses the pass (RV1): verdicts set before the review hide the claims from it. The final
text of a final check (deliverables/<stem>.final.md, named by reviews/<name>/final-check.json with its sha256) needs
neither the request check nor the blind verification while it is unchanged (RV2): its review runs workflows/review.js
with after_fx (the platform lenses and the partner lens) and the send gate takes the final check for the request
check. An edit after the final check or a record without the hash brings the full review back.

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
import stat
import sys
import tempfile
from collections import Counter
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field, replace
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
KNOWN_SHARE = 0.5
"""The share of a claim (the score of `kb find`) an entry must carry to count as known before the work (KF)."""
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


FINAL_SUFFIX = ".final.md"


def _checked_path(rel: str) -> str:
    """The path of deliverables/<rel> as the name check reads it. The name check takes x.final.md for a web address
    (the suffix .md is a country domain), so a final text of the final check (FX) is checked under the name of its
    draft, <stem>.md: the stem, where a name could hide, is checked all the same (RV2)."""
    if rel.endswith(FINAL_SUFFIX) and len(rel) > len(FINAL_SUFFIX):
        rel = rel[:-len(FINAL_SUFFIX)] + ".md"
    return "%s/%s" % (DELIVERABLES, rel)


def _label(rel: str, reg: Path) -> str:
    shown = "%s/%s" % (DELIVERABLES, rel)
    return shown if _is_clean(_checked_path(rel), reg) else "the deliverable"


def project_tier(project) -> int:
    """The review tier of a project: the `- review-tier:` line of its SCOPE.md (the owner sets it with awb start,
    awb spawn or awb projects tier), 3 when the line is missing or not 1, 2 or 3."""
    try:
        text = (Path(project) / "SCOPE.md").read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        return SEND_TIER
    m = re.search(r"(?m)^- review-tier: *([123]) *$", text)
    return int(m.group(1)) if m else SEND_TIER


_GERMAN_WORDS = frozenset("der die das und ist nicht mit für von ein eine den dem auf werden wird sind auch".split())
_ENGLISH_WORDS = frozenset("the and is of to in for with that this are be on as by".split())
_KIND_WORD_RE = re.compile(r"[a-z]{2,20}")
_LANG_RE = re.compile(r"[a-z]{2}")
DEFAULT_KIND_WORD = "doc"


def guess_language(path) -> str:
    """en, de or ru by the letters and the common words of the start of a text file; en when it cannot say."""
    try:
        raw = Path(path).read_bytes()[:20000].decode("utf-8")
    except (OSError, UnicodeError):
        return "en"
    letters = [c for c in raw if c.isalpha()]
    if letters and sum("\u0400" <= c <= "\u04ff" for c in letters) * 2 > len(letters):
        return "ru"
    words = re.findall(r"[a-zäöüß]+", raw.lower())
    de, en = sum(w in _GERMAN_WORDS for w in words), sum(w in _ENGLISH_WORDS for w in words)
    return "de" if de > en else "en"


def propose_name(project, name: str, path=None, kind_word: str | None = None, lang: str | None = None,
                 register_path=None) -> str | None:
    """A neutral file name for a deliverable whose name the name check refused: <project code>-<kind word>-
    <language>.<ext>, for example tcp-q7m4-mapping-de.md. The code is the name of the project folder, the kind word
    and the language come from the caller (doc and the guess of the text when absent), the extension from the
    name when it is plain. None when the project folder is no project code or the proposal does not pass the
    name check itself."""
    code = Path(project).name
    if not codes.is_project_code(code):
        return None
    kind_word = kind_word or DEFAULT_KIND_WORD
    lang = lang or (guess_language(path) if path is not None else "en")
    if not _KIND_WORD_RE.fullmatch(kind_word) or not _LANG_RE.fullmatch(lang):
        return None
    ext = Path(name).suffix.lower()
    if not re.fullmatch(r"\.[a-z0-9]{1,5}", ext):
        ext = ""
    proposal = "%s-%s-%s%s" % (code, kind_word, lang, ext)
    return proposal if _is_clean(proposal, _register(register_path)) else None


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
    if _check(_checked_path(rel), reg):
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


def init(deliverable, request, tier: int | None, budget: int, reader: str | None = None, mode: str = "doc",
         force: bool = False, register_path=None, today: str | None = None, lowered: str | None = None) -> Path:
    """Write `reviews/<name>/contract.md`. The request text, the reader and his words that lowered the tier must
    pass the name check; a hit raises Refused and nothing is written. Returns the path of the contract.

    `lowered` carries his words when he lowered the tier of a text that goes to a customer or partner (decision
    14: the tier of the project, 3 by default, he lowers it with one word). The send gate reads it; it needs a tier
    below the tier of the project. Without `tier` the deliverable takes the tier of its project."""
    project, rel = locate(deliverable)
    if tier is None:
        tier = project_tier(project)
    if tier not in TIERS:
        raise ReviewError("the tier must be 0, 1, 2 or 3")
    if not isinstance(budget, int) or isinstance(budget, bool) or budget <= 0:
        raise ReviewError("the budget must be a positive number of words")
    if mode not in MODES:
        raise ReviewError("the mode must be mail, doc or chat")
    reader = _clean_reader(reader)
    lowered = " ".join(str(lowered).split()) if lowered else ""
    if lowered and tier >= project_tier(project):
        raise ReviewError("--lowered goes with a tier below %d" % project_tier(project))
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
    hits = _check(body.replace("%s/%s" % (DELIVERABLES, rel), _checked_path(rel)), reg)
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
    data: bool = field(default=False, compare=False, repr=False)
    """A line of a code block or a raw JSON body (task T7): its numbers are data, never a computed claim."""

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


_JSON_LINE_RE = re.compile(r"""^\s*(?:[{}\]]|\[\s*(?:[{"\[0-9-]|$)|"[^"\n]+"\s*:)""")
"""A line of a raw JSON body quoted outside a code block: an opening or closing bracket or a "key": value."""
_JSON_BODY_RE = re.compile(r"""`[^`\n]+`|\{\s*"[^"\n]*"\s*:[^\n]*\}""")
"""Raw data inside a sentence: inline code or a JSON object."""


def _is_data(text: str) -> bool:
    """True for a raw JSON line (see _JSON_LINE_RE)."""
    return bool(_JSON_LINE_RE.match(text))


def _without_data(sentence: str) -> str:
    """The sentence without its inline code and JSON objects: a number quoted from an answer is no claim (T7)."""
    return _JSON_BODY_RE.sub(" ", sentence)


def _units(text: str) -> list[tuple[int, str]]:
    """(line, text) of every sentence, heading, table row and code line, in order of the text."""
    return [(line, unit) for line, unit, _ in _marked_units(text)]


def _marked_units(text: str) -> list[tuple[int, str, bool]]:
    """(line, text, data) of every unit of `_units`; data is true for a line of a code block or a raw JSON line."""
    out: list[tuple[int, str, bool]] = []
    block: list[tuple[int, str]] = []
    quoted = False   # the open block is a block quote

    def flush() -> None:
        if block:
            out.extend((line, unit, False) for line, unit in _sentences(block))
            block.clear()

    in_code = False
    for no, raw in enumerate(text.splitlines(), start=1):
        if _FENCE_RE.match(raw):
            flush()
            in_code = not in_code
            continue
        if in_code:
            if raw.strip():
                out.append((no, raw.strip(), True))
            continue
        if not raw.strip():
            flush()
            continue
        if _HEADING_RE.match(raw):
            flush()
            body = _NUMBERING_RE.sub("", _HEADING_RE.sub("", raw).strip()).rstrip("#").strip()
            if body:
                out.append((no, body, False))
            continue
        if _TABLE_RE.match(raw):
            flush()
            out.append((no, raw.strip(), False))
            continue
        if _is_data(raw):
            flush()
            out.append((no, raw.strip(), True))
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
    take(codes.PROJECT_RE)
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
    for line, sentence, data in _marked_units(text):
        kinds, total = _scan(sentence)
        if not kinds:
            continue
        if total and (data or not _scan(_without_data(sentence))[1]):
            total = False       # T7: "total" and its number inside a quoted raw answer are data, not a total
        out.append(Claim(id="C-%d" % (len(out) + 1), line=line, kind=",".join(kinds), risk=_risk(kinds, total),
                         sentence=_sentence_cell(sentence), data=data))
    return out


def _key(sentence: str) -> str:
    return " ".join(sentence.split()).casefold()


def _pair(fresh: list[Claim], old: list[Claim]) -> tuple[list[tuple[Claim, Claim | None]], int]:
    """Each fresh claim with the old row of the same sentence (in order for repeated sentences), and the count of
    old rows that no fresh claim took (see `_unpaired` for what becomes of them)."""
    pairs, left = _pair_rows(fresh, old)
    return pairs, len(left)


def _pair_rows(fresh: list[Claim], old: list[Claim]) -> tuple[list[tuple[Claim, Claim | None]], list[Claim]]:
    pool: dict[str, list[Claim]] = {}
    for row in old:
        pool.setdefault(_key(row.sentence), []).append(row)
    pairs: list[tuple[Claim, Claim | None]] = []
    for c in fresh:
        rows = pool.get(_key(c.sentence))
        pairs.append((c, rows.pop(0) if rows else None))
    left = [r for r in old if any(r is x for v in pool.values() for x in v)]
    return pairs, left


def is_manual(row: Claim) -> bool:
    """A row the session added by hand: marked `manual` in the kind column, or a sentence the generator would
    not list."""
    return "manual" in row.kinds() or not find_claims(row.sentence)


def _unpaired(left: list[Claim], text: str) -> tuple[list[Claim], list[Claim]]:
    """(kept, gone) of the old rows no fresh claim took. A row whose sentence is still in the text is kept as it
    is, with its line brought up to date; a row whose sentence is gone is dropped."""
    lines: dict[str, int] = {}
    for line, sentence in _units(text):
        lines.setdefault(_key(sentence), line)
    whole = _key(text)
    kept: list[Claim] = []
    gone: list[Claim] = []
    for r in left:
        k = _key(r.sentence)
        if k in lines:
            kept.append(replace(r, line=lines[k]))
        elif k and k in whole:
            kept.append(r)
        else:
            gone.append(r)
    return kept, gone


def _id_number(claim_id: str) -> int:
    m = re.fullmatch(r"C-([0-9]{1,6})", claim_id)
    return int(m.group(1)) if m else 0


def merge_claims(fresh: list[Claim], old: list[Claim], text: str | None = None
                 ) -> tuple[list[Claim], int, int]:
    """(rows, kept, dropped): fresh claims, where a sentence already listed keeps its id, evidence, grade and
    verdict; new sentences get new ids after the highest old one. With `text`, an old row no fresh claim took
    (a row added by hand) stays while its sentence is in the text, with its evidence and verdict; `dropped` then
    counts the rows whose sentence is gone. See `merge_claims_report` for the ids of the dropped manual rows."""
    rows, kept, gone = merge_claims_report(fresh, old, text)
    return rows, kept, len(gone)


def merge_claims_report(fresh: list[Claim], old: list[Claim], text: str | None = None
                        ) -> tuple[list[Claim], int, list[Claim]]:
    """(rows, kept, gone): as `merge_claims`, with the dropped old rows themselves."""
    pairs, left = _pair_rows(fresh, old)
    keep_manual, gone = _unpaired(left, text) if text is not None else ([], left)
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
    if keep_manual:
        kept += len(keep_manual)
        out = sorted(out + keep_manual, key=lambda r: r.line)
    return out, kept, gone


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
    dropped_manual: list[str] = field(default_factory=list)   # ids of hand-added rows whose sentence is gone
    kb_path: Path | None = None
    kb_rows: int = 0      # claims with at least one knowledge entry that may settle them (V-04)
    known_before: int = 0  # claims the base knew before the work (KF)


def kb_candidates(sentence: str, scope: str = "tcp", kb_where=None, today: date | None = None,
                  limit: int = KB_CANDIDATES) -> list[dict]:
    """The first entries `kb find` returns for a claim sentence (V-04): {"id", "grade", "checked", "expired"}.
    A knowledge base that cannot be read gives none: the list is a lead, the verdict stays with the check."""
    from awb import kb

    try:
        found = kb.find(sentence, where=kb_where, scope=scope)
    except (kb.KBError, OSError):
        return []
    return [{"id": e.id, "grade": e.grade, "checked": e.checked, "expired": e.is_expired(today), "score": score}
            for score, e in found[:limit]]


def _created(project: Path) -> str:
    """The created date of the project's SCOPE.md (ISO), empty when there is none."""
    try:
        text = (Path(project) / "SCOPE.md").read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        return ""
    m = re.search(r"(?m)^- created: (\d{4}-\d{2}-\d{2})", text)
    return m.group(1) if m else ""


def known_before(found: list[dict], created: str) -> bool:
    """True when one of the entries carries KNOWN_SHARE of the claim and was checked before the project was
    created (before today when the date is not known): the base knew it before the work (task KF)."""
    start = created or date.today().isoformat()
    return any(c.get("score", 0) >= KNOWN_SHARE and str(c.get("checked", "")) < start for c in found)


def render_kb(rows: list[Claim], candidates: dict[str, list[dict]], created: str = "") -> str:
    """kb.tsv: per claim the knowledge entries to read first. An entry that could settle a high-risk claim alone
    (grade live or contract, not expired) is marked "settles". The last column says yes or no: known from the base
    before the work (KF)."""
    lines = ["\t".join(("id", "risk", "entries", "known_before"))]
    for r in rows:
        found = candidates.get(r.id) or []
        cells = []
        for c in found:
            state = "expired" if c["expired"] else ("settles" if c["grade"] in SETTLING_GRADES else "lead")
            cells.append("%s %s %s %s" % (c["id"], c["grade"], c["checked"], state))
        lines.append("\t".join((r.id, r.risk, "; ".join(cells), "yes" if known_before(found, created) else "no")))
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
    rows, kept, gone = merge_claims_report(find_claims(text), old, text)
    dropped = len(gone)
    dropped_manual = [r.id for r in gone if is_manual(r)]
    _write_atomic(target, render_claims(rows))
    scope = _platform(project)
    candidates = {r.id: kb_candidates(r.sentence, scope, kb_where, today) for r in rows}
    kb_path = review_dir(project, rel) / KB_FILE
    created = _created(project)
    _write_atomic(kb_path, render_kb(rows, candidates, created))
    return ClaimsResult(path=target, rows=rows, kept=kept, dropped=dropped, dropped_manual=dropped_manual,
                        kb_path=kb_path,
                        kb_rows=sum(1 for v in candidates.values() if v),
                        known_before=sum(1 for v in candidates.values() if known_before(v, created)))


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


_KB_EVIDENCE_RE = re.compile(r"kb:(%s)" % codes.KB_WIDE)
_CALC_EVIDENCE_RE = re.compile(r"calc:(%s)" % codes.CALC)
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
        elif "number" in c.kinds() and not c.data and _ARITH_RE.search(_without_data(c.sentence)) \
                and not _CALC_EVIDENCE_RE.search(row.evidence):
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
        pairs, left = _pair_rows(fresh, load_claims(cpath))
        manual, gone = _unpaired(left, text)
        stale = len(gone)
        missing = [c for c, row in pairs if row is None]
        if missing:
            problems.append("claims.tsv misses %d claims of the current text (lines %s): run awb review claims "
                            "again" % (len(missing), ", ".join(str(c.line) for c in missing[:20])))
        for c, row in pairs:
            if row is not None:
                problems.extend(_claim_problems(c, row, project, path, contract.tier, kb_where))
        for row in manual:
            # a row added by hand is checked like a generated one: its verdict, grade and evidence count
            c = replace(row, risk=row.risk if row.risk in ("high", "medium", "low") else "medium")
            problems.extend(_claim_problems(c, c, project, path, contract.tier, kb_where))
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
    final_check: str = ""   # RV2: the record of the final check that stands in for the request check and the
                            # blind verification, "" when none does


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
    fx = fx_exemption(project, rel)
    if res.tier >= LENSES_REQUIRED_FROM and not fx.exempt:
        problems.extend(_blind_problems(path, project, folder, rel, res.high, fx.reason))
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
    if fx.exempt:
        record["final_check"] = fx.record
    _write_atomic(record_path, json.dumps(record, indent=2, sort_keys=True) + "\n")
    return PassResult(True, [], record, record_path, res, lenses, fx.record if fx.exempt else "")


VERIFY_KIND = "verify"
"""The kind (and the file name) of the blind verification lens of workflows/review.js."""
BLIND_EVIDENCE = "evidence/review"
"""Where workflows/review.js writes the blind answers: evidence/review/<slug>/<claim id>.md."""
BLIND_EMPTY = ("the blind verification checked 0 claims while %d high-risk claims carry a verdict it did not set: "
               "clear the verdicts set before the review in claims.tsv and run workflows/review.js again")
"""RV1 (2026-10-27): a session set the verdicts before the tier 3 review, so the blind verification had nothing to
check and the run looked complete."""


def _verify_checked(lens_dir: Path) -> int | None:
    """How many claims the blind verification lens files in `lens_dir` checked; None when there is none."""
    if not lens_dir.is_dir():
        return None
    checked: int | None = None
    for f in sorted(lens_dir.glob("*.json")):
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, ValueError):
            continue
        if not isinstance(data, dict) or (data.get("kind") != VERIFY_KIND and f.stem != VERIFY_KIND):
            continue
        # every claim the lens checked is in "claims"; a finding exists only for a checked claim
        claims, findings = data.get("claims"), data.get("findings")
        checked = (checked or 0) + max(len(claims) if isinstance(claims, list) else 0,
                                       len(findings) if isinstance(findings, list) else 0)
    return checked


def blind_slug(rel: str) -> str:
    """The folder name workflows/review.js gives the blind answers of deliverables/<rel> (its SLUG)."""
    return re.sub(r"[^A-Za-z0-9._-]+", "-", rel)


def _not_blind(path: Path, project: Path, folder: Path, rel: str) -> int:
    """The high-risk claims of the text (risk from the detector) whose row does not cite the answer of a blind
    check of an earlier round (evidence/review/<slug>/<claim id>.md, a file that is there)."""
    data = _read_bytes(path)
    if _read_plain(data) is not None:
        text, _ = _deliverable_text(path, data)
    else:
        with _snapshot(path, data) as snap:
            text, _ = _deliverable_text(snap, data)
    high = [c for c in find_claims(text) if c.risk == "high"]
    try:
        rows = load_claims(folder / CLAIMS)
    except ReviewError:
        return len(high)
    pairs, _ = _pair_rows(high, rows)
    gap = 0
    for _c, row in pairs:
        blind = "%s/%s/%s.md" % (BLIND_EVIDENCE, blind_slug(rel), row.id) if row is not None else ""
        items = [x.strip() for x in (row.evidence if row is not None else "").split(";")]
        if not blind or blind not in items or not (project / blind).is_file():
            gap += 1
    return gap


def _blind_problems(path: Path, project: Path, folder: Path, rel: str, high: int, fx_reason: str) -> list[str]:
    """RV1: at tier 3 a blind verification that checked 0 claims while the text has high-risk claims whose verdict
    no blind check set refuses the pass. A row whose verdict came from a blind check of an earlier round (V-14) counts
    as checked. A final text that lost its final check (`fx_reason`) needs the blind verification."""
    if high <= 0:
        return []
    checked = _verify_checked(folder / LENSES)
    if checked is None:
        if fx_reason:
            return ["%s: the full review applies again and the blind verification has not run (workflows/review.js "
                    "without after_fx)" % fx_reason]
        return []
    if checked > 0:
        return []
    try:
        gap = _not_blind(path, project, folder, rel)
    except (OSError, ReviewError):
        gap = high
    if not gap:
        return []
    return [("%s: " % fx_reason if fx_reason else "") + BLIND_EMPTY % gap]


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


FINAL_CHECK = "final-check.json"
"""The record of a final check (FX, workflows/final-check.js) beside record.json in reviews/<name>/. It binds
nothing of the draft. For the final text it names, with the sha256 of that text (RV2), it stands in for the request
check and the blind verification while the text is unchanged (`fx_exemption`)."""
FINAL_CHECK_KIND = "final-check"
_MODEL_RE = re.compile(r"[A-Za-z][A-Za-z0-9 .-]{0,39}")
_FC_DATE_RE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}")
_EFFORTS = ("low", "medium", "high", "xhigh", "max")


def final_name(name: str) -> str:
    """deliverables/<stem>.final.md, the final text of the deliverable deliverables/<name> (workflows/final-check.js):
    the stem is the name without the suffix of its last part."""
    head, _, base = name.rpartition("/")
    dot = base.rfind(".")
    stem = base[:dot] if dot > 0 else base
    return "%s/%s%s.final.md" % (DELIVERABLES, head + "/" if head else "", stem)


def final_check(project: Path, name: str) -> dict | None:
    """The final check of the deliverable deliverables/<name> as {"date", "model", "effort", "final", "table",
    "open_points"}; None when there is none or its file is not a final check of this deliverable."""
    path = review_dir(project, name) / FINAL_CHECK
    try:
        if path.is_symlink() or not path.is_file() or path.stat().st_size > 1_000_000:
            return None
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValueError):
        return None
    if not isinstance(data, dict) or data.get("kind") != FINAL_CHECK_KIND \
            or data.get("deliverable") != "%s/%s" % (DELIVERABLES, name):
        return None
    date_ = data.get("date") if isinstance(data.get("date"), str) and _FC_DATE_RE.fullmatch(data["date"]) else ""
    model = data.get("model") if isinstance(data.get("model"), str) and _MODEL_RE.fullmatch(data["model"]) else ""
    effort = data.get("effort") if data.get("effort") in _EFFORTS else ""
    final = data.get("final")
    if not isinstance(final, str) or not final.startswith(DELIVERABLES + "/") or not final.endswith(".final.md") \
            or ".." in final.split("/"):
        final = ""
    points = data.get("open_points")
    digest = data.get("final_sha256")
    return {"date": date_, "model": model, "effort": effort, "final": final,
            "table": "%s/%s/final-check.md" % (REVIEWS, name),
            "open_points": len(points) if isinstance(points, list) else 0,
            "final_sha256": digest if isinstance(digest, str) and _SHA_RE.fullmatch(digest) else ""}


_SHA_RE = re.compile(r"[0-9a-f]{64}")
FX_NO_HASH = "the record of the final check carries no hash of the final text (a run before RV2)"
FX_CHANGED = "the final text changed after the final check"


@dataclass
class FxState:
    """RV2 (his decision of 2026-10-27): whether the final check stands in for the request check and the blind
    verification of the final text deliverables/<rel>. `record` is the record that does; `reason` says why none does
    when the file is the final text of a recorded final check ("" when it is no such text)."""
    exempt: bool
    reason: str = ""
    record: str = ""


def fx_exemption(project: Path, rel: str) -> FxState:
    """The final check stands in when deliverables/<rel> is the final text <stem>.final.md of a recorded final check
    (reviews/<name>/final-check.json names it) and the record carries the sha256 of exactly the bytes on disk now.
    An edit after the check or an old record without the hash gives no exemption: the full review applies."""
    project = Path(project)
    target = "%s/%s" % (DELIVERABLES, rel)
    base = project / REVIEWS
    if not rel.endswith(".final.md") or not base.is_dir():
        return FxState(False)
    found: list[tuple[str, dict]] = []
    for f in sorted(base.rglob(FINAL_CHECK)):
        try:
            name = f.parent.relative_to(base).as_posix()
        except ValueError:
            continue
        if final_name(name) != target:
            continue
        fc = final_check(project, name)
        if fc is not None and fc["final"] == target:
            found.append((name, fc))
    if not found:
        return FxState(False)
    try:
        digest = sha256_file(project / DELIVERABLES / rel)
    except OSError:
        return FxState(False, FX_CHANGED)
    for name, fc in found:
        if fc["final_sha256"] == digest:
            return FxState(True, "", "%s/%s/%s" % (REVIEWS, name, FINAL_CHECK))
    return FxState(False, FX_CHANGED if any(fc["final_sha256"] for _, fc in found) else FX_NO_HASH)


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


# --------------------------------------------------------------------------- the fingerprint of a closed project

FINGERPRINTS = "fingerprints"
FINGERPRINT_VERSION = 1
_FP_MAX_SUBSTRINGS = 200_000
"""A deliverable whose single-word parts would need more hashes than this gets an incomplete fingerprint, which
the send gate does not use (it refuses)."""


def fingerprint_path(p, code: str) -> Path:
    """Where the fingerprint of project `code` lives: on the shared side, which the owner reads."""
    return Path(p.shared) / FINGERPRINTS / ("%s.json" % code)


def _readable_tree(project: Path) -> None:
    """Raise OSError when a folder under the project's deliverables/ or reviews/ or a deliverable cannot be
    read. A missing deliverables/ or reviews/ is no error. pathlib's glob skips a folder it cannot list, so the
    walk here is the one that notices."""
    def fail(err: OSError) -> None:
        raise err

    for sub in (DELIVERABLES, REVIEWS):
        top = project / sub
        try:
            if not stat.S_ISDIR(os.stat(top).st_mode):     # status() reads no deliverable there either
                continue
        except FileNotFoundError:
            continue
        for folder, _dirs, files in os.walk(top, onerror=fail):
            if sub != DELIVERABLES:
                continue
            for name in files:
                try:
                    st = os.stat(os.path.join(folder, name))
                except FileNotFoundError:          # a broken link: status() leaves it out too
                    continue
                if stat.S_ISREG(st.st_mode):
                    with open(os.path.join(folder, name), "rb"):
                        pass


def _fp_hash(salt: str, text: str) -> str:
    return hashlib.sha256((salt + "\0" + text).encode("utf-8", "surrogatepass")).hexdigest()[:16]


def _part_min(chars: int) -> int:
    """The fewest characters a file needs to be a part of a deliverable of `chars` characters (_same_text)."""
    n = int(_TEXT_PART_SHARE * chars)
    while n < _TEXT_PART_SHARE * chars:
        n += 1
    return n


def _fp_of_words(salt: str, words: list[str]) -> dict:
    """The hashes _same_text needs of a deliverable's words, no text: a word table (length, hash, hashes of its
    prefixes and suffixes), the words as indexes into it, the length of the joined text and the hashes of every
    single-word part long enough to be a part of the deliverable."""
    index: dict[str, int] = {}
    vocab: list[list] = []
    seq: list[int] = []
    for w in words:
        if w not in index:
            index[w] = len(vocab)
            vocab.append([len(w), _fp_hash(salt, w), [_fp_hash(salt, w[:k]) for k in range(1, len(w))],
                          [_fp_hash(salt, w[-k:]) for k in range(1, len(w))]])
        seq.append(index[w])
    chars = len(" ".join(words))
    least = max(_part_min(chars), 1)
    sub: set[str] | None = set()
    for w in index:
        if len(w) < least:
            continue
        if len(sub) + (len(w) - least + 1) * (len(w) - least + 2) // 2 > _FP_MAX_SUBSTRINGS:
            sub = None
            break
        for i in range(len(w) - least + 1):
            for j in range(i + least, len(w) + 1):
                sub.add(_fp_hash(salt, w[i:j]))
    return {"chars": chars, "words": seq, "vocab": vocab, "sub": sorted(sub) if sub is not None else None}


def fingerprint(project: Path, salt: str | None = None) -> dict:
    """The fingerprint of a project's deliverables, read strictly: the sha256 of each deliverable and the hashed
    words of its text, with a random salt. It carries no file name and no text. Raises OSError when anything under
    deliverables/ or reviews/ cannot be read."""
    project = Path(os.path.realpath(project))
    _readable_tree(project)
    salt = salt or os.urandom(16).hex()
    seen: set[str] = set()
    rows = []
    for _rel, real, _state in _deliverables_of(project):
        digest = sha256_file(real)
        if digest in seen:
            continue
        seen.add(digest)
        rows.append({"sha256": digest, **_fp_of_words(salt, _words(_text_of(real)))})
    return {"version": FINGERPRINT_VERSION, "salt": salt, "deliverables": rows}


def _old_salt(dest: Path) -> str | None:
    """The salt of the fingerprint at `dest`, so that closing a closed project again writes the same bytes and its
    pin on the owner side still holds (awb/fppin.py); None without a valid one."""
    from awb import fppin

    raw = fppin.read_bytes(Path(dest))
    data = _parse_fingerprint(raw) if raw is not None else None
    salt = data.get("salt") if data else None
    return salt if isinstance(salt, str) and re.fullmatch(r"[0-9a-f]{32}", salt) else None


def write_fingerprint(project: Path, dest: Path) -> int:
    """Write the fingerprint of `project` to `dest` (mode 640, replaced whole); the number of deliverables.
    Raises OSError when the project cannot be read or `dest` cannot be written."""
    dest = Path(dest)
    data = fingerprint(project, _old_salt(dest))
    config.make_dir(dest.parent, 0o750, shared=True)
    fd, tmp = tempfile.mkstemp(prefix=".fp-", dir=str(dest.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            os.fchmod(fh.fileno(), 0o640)
            json.dump(data, fh, separators=(",", ":"))
        os.replace(tmp, dest)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    return len(data["deliverables"])


def _load_fingerprint(path: Path) -> dict | None:
    """A fingerprint the gate can use, else None (missing, unreadable, malformed, another version, incomplete)."""
    try:
        raw = Path(path).read_bytes()
    except OSError:
        return None
    return _parse_fingerprint(raw)


def _parse_fingerprint(raw: bytes) -> dict | None:
    """_load_fingerprint of bytes already read (the gate hashes and parses the same bytes, K16)."""
    try:
        data = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError, RecursionError):
        return None
    if not isinstance(data, dict) or data.get("version") != FINGERPRINT_VERSION or not isinstance(data.get("salt"), str):
        return None
    rows = data.get("deliverables")
    if not isinstance(rows, list):
        return None
    for row in rows:
        try:
            ok = (isinstance(row["sha256"], str) and isinstance(row["chars"], int) and row["sub"] is not None
                  and all(isinstance(i, int) and 0 <= i < len(row["vocab"]) for i in row["words"])
                  and all(len(v) == 4 and len(v[2]) == len(v[3]) == max(v[0] - 1, 0) for v in row["vocab"]))
        except (KeyError, TypeError):
            ok = False
        if not ok:
            return None
    return data


def _fp_contains(outer: list, inner_len: int, inner_at, outer_word, edge) -> bool:
    """Whether the joined words `inner` lie in the joined words `outer`, as `d in f` on the joined texts: the
    first inner word a suffix of an outer word, the middle words equal, the last a prefix. `inner_at(k)` gives
    the hash of inner word k, `outer_word(i)` the outer word at i, `edge(i, k, kind)` the hash of the suffix or
    prefix of outer word i as long as inner word k (None when it is shorter)."""
    n = inner_len
    for i in range(len(outer) - n + 1):
        if edge(i, 0, "suf") != inner_at(0):
            continue
        if all(outer_word(i + k) == inner_at(k) for k in range(1, n - 1)) and edge(i + n - 1, n - 1, "pre") == inner_at(n - 1):
            return True
    return False


def _fp_same_text(file_words: list[str], salt: str, row: dict) -> bool:
    """_same_text(file_words, the deliverable's words) through the deliverable's fingerprint: the same answer."""
    seq, vocab, chars = row["words"], row["vocab"], row["chars"]
    if not file_words or not seq:
        return False
    fh = [_fp_hash(salt, w) for w in file_words]
    dh = [vocab[i][1] for i in seq]
    # the deliverable in the file
    if len(seq) == 1:
        size, want = vocab[seq[0]][0], dh[0]
        if any(_fp_hash(salt, w[i:i + size]) == want for w in set(file_words) for i in range(len(w) - size + 1)):
            return True
    else:
        def file_edge(i, k, kind):
            w, size = file_words[i], vocab[seq[k]][0]
            if len(w) < size:
                return None
            return _fp_hash(salt, w[-size:] if kind == "suf" else w[:size])

        if _fp_contains(fh, len(seq), lambda k: dh[k], lambda i: fh[i], file_edge):
            return True
    # the file in the deliverable, as a part worth _TEXT_PART_SHARE
    if len(" ".join(file_words)) >= _TEXT_PART_SHARE * chars:
        if len(file_words) == 1:
            if fh[0] in set(row["sub"]):
                return True
        else:
            def own_edge(i, k, kind):
                size, (n, full, pre, suf) = len(file_words[k]), vocab[seq[i]]
                if n < size:
                    return None
                return full if n == size else (suf if kind == "suf" else pre)[size - 1]

            if _fp_contains(dh, len(file_words), lambda k: fh[k], lambda i: dh[i], own_edge):
                return True
    if len(file_words) > _MAX_MATCH_WORDS or len(seq) > _MAX_MATCH_WORDS:
        return False
    import difflib

    known = {v[1]: i for i, v in enumerate(vocab)}
    other: dict[str, int] = {}
    tokens = [known[h] if h in known else -1 - other.setdefault(h, len(other)) for h in fh]
    return difflib.SequenceMatcher(None, tokens, seq, autojunk=False).ratio() >= _TEXT_SIMILARITY


def _fp_carries(fp: dict, digest: str, file_words) -> bool:
    """The file is a deliverable of the fingerprinted project: by bytes, then by text (`file_words()` gives the
    file's words)."""
    rows = fp["deliverables"]
    if any(row["sha256"] == digest for row in rows):
        return True
    words = file_words()
    return any(_fp_same_text(words, fp["salt"], row) for row in rows)


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
    the tier (decision 14) and one at tier 3 whose lenses hold no request check of this deliverable (V-16), unless it
    is the unchanged final text of a final check, which stands in for the request check (RV2, `fx_exemption`). The
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
    registered: dict[Path, tuple[str, str]] = {}
    if workbench is not None:
        # every registered project and every project folder under the projects root
        found: set[Path] = set()
        try:
            from awb import projects as _projects

            for pr in _projects.load(workbench):
                registered[Path(os.path.realpath(pr.path))] = (pr.code, pr.state)
            found.update(registered)
        except Exception:
            pass
        try:
            for d in Path(workbench.projects_root).glob("tcp-*"):
                try:
                    if (d / DELIVERABLES).is_dir():
                        found.add(Path(os.path.realpath(d)))
                except OSError:        # a folder the owner cannot look into: a project all the same
                    if codes.is_project_code(d.name):
                        found.add(Path(os.path.realpath(d)))
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
        words: list[list[str]] = []

        def file_words() -> list[str]:
            if not words:
                words.append(_words(_text_of(f)))
            return words[0]

        for root in others:
            try:
                _readable_tree(root)
                carried = _match_deliverable(f, digest, root)[0] is not None
            except Exception:
                # a folder the owner cannot read: a closed project answers through its fingerprint, anything
                # else refuses (never a pass, never a traceback)
                code, state = registered.get(root, (root.name, ""))
                fp, why = None, ""
                if state == "closed":
                    from awb import fppin

                    raw, why = fppin.pinned_bytes(workbench, code)
                    fp = _parse_fingerprint(raw) if raw is not None else None
                if why in ("conflict", "changed"):
                    return SEND_REFUSE, ("the fingerprint of project %s does not match the one pinned at close: "
                                         "nothing leaves unchecked" % code)
                if fp is None and state == "closed":
                    return SEND_REFUSE, ("project %s cannot be read here and has no usable fingerprint: run awb "
                                         "close %s as the work user, nothing leaves unchecked" % (code, code))
                if fp is None:
                    return SEND_REFUSE, ("project %s cannot be read here: let the owner read its deliverables and "
                                         "reviews, nothing leaves unchecked" % code)
                carried = _fp_carries(fp, digest, file_words)
            if carried:
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
    needed = project_tier(project)
    if tier < needed and not contract.lowered:
        return SEND_REFUSE, ("reviewed at tier %d, but a text for a customer or partner needs tier %d unless he "
                             "lowered it (awb review init --lowered)" % (tier, needed))
    if tier >= SEND_TIER and not _request_lens_for(folder, rel):
        fx = fx_exemption(project, rel)
        if fx.exempt:
            return SEND_OK, "reviewed at tier %d, the final check in place of the request check" % tier
        return SEND_REFUSE, ("the request check has not run for this deliverable (workflows/request-check.js)"
                             + ("; %s, so the full review applies again" % fx.reason if fx.reason else ""))
    return SEND_OK, "reviewed at tier %d%s" % (tier, ", lowered by him" if contract.lowered else "")


def find_project(start: Path | None = None) -> Path:
    """The nearest folder at or above `start` (default: the working folder) with a SCOPE.md, else `start`."""
    here = Path(os.path.abspath(start or Path.cwd()))
    root = config.scope_root(here, strict=True)
    return root if root is not None else here


# --------------------------------------------------------------------------- command line


def _print_problems(problems: list[str]) -> None:
    for p in problems:
        print("- %s" % p)


def _print_name_hint(project, rel: str, path, reg: Path, file=None) -> None:
    """When the name check refuses the file name of a deliverable, say which neutral name to give it."""
    if _is_clean("%s/%s" % (DELIVERABLES, rel), reg):
        return
    proposal = propose_name(project, rel, path if Path(path).is_file() else None, register_path=reg)
    if proposal:
        print("name: the file name was refused by the name check; rename the file to %s (kind word and language: "
              "<project code>-<kind word>-<language>.<ext>)" % proposal, file=file or sys.stdout)


def _hint_args(deliverable) -> tuple:
    project, rel = locate(deliverable)
    return project, rel, deliverable, _register(None)


def _cmd_init(args) -> int:
    target = init(args.deliverable, args.request, args.tier, args.budget, reader=args.reader, mode=args.mode,
                  force=args.force, lowered=args.lowered)
    project, rel = locate(args.deliverable)
    tier = args.tier if args.tier is not None else project_tier(project)
    reg = _register(None)
    print("review init: wrote the contract of %s (tier %d%s, budget %d words, mode %s)"
          % (_label(rel, reg), tier, ", lowered by him" if args.lowered else "", args.budget, args.mode))
    _print_name_hint(project, rel, args.deliverable, reg)
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
    if res.dropped_manual:
        print("review claims: %d rows added by hand were dropped because their sentence is gone: %s"
              % (len(res.dropped_manual), ", ".join(res.dropped_manual)))
    print("review claims: %d claims have knowledge entries to read first, listed in kb.tsv; cite one as "
          "kb:KB-XXXX" % res.kb_rows)
    print("review claims: %d of %d claims were known from the base before the work (kb.tsv, column known_before)"
          % (res.known_before, len(res.rows)))
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
        _print_name_hint(*_hint_args(args.deliverable))
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
        _print_name_hint(*_hint_args(args.deliverable))
        return EXIT_FINDINGS
    print("pass: %s (tier %d): %s; %d lens files, %d blocking findings closed; record.json written"
          % (label, res.l0.tier, _l0_line(res.l0), res.lenses["files"], res.lenses["blocking"]))
    if res.final_check:
        print("final check: it stands in for the request check and the blind verification while the final text is "
              "unchanged")
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


def _cmd_show(args) -> int:
    project, rel = locate(args.deliverable)
    reg = _register(None)
    path = project / DELIVERABLES / rel
    state, tier = _record_state(path, review_dir(project, rel) / RECORD) if path.is_file() else (MISSING, None)
    print("%s: record %s%s" % (_label(rel, reg), state, " (tier %d)" % tier if tier is not None else ""))
    fc = final_check(project, rel)
    if fc is None:
        print("final check: none")
    else:
        # The final text is named after the draft: <stem>.final.md. The name check reads x.final.md as a web
        # address, so the path is shown when it is the draft's own name with that suffix and the draft's is clean.
        final = fc["final"] if fc["final"] and (fc["final"] == final_name(rel) and _is_clean(
            "%s/%s" % (DELIVERABLES, rel), reg) or _is_clean(fc["final"], reg)) else "the final text"
        print("final check: %s, %s%s, %s; %d open points in %s"
              % (fc["date"] or "no date", fc["model"] or "no model", " effort " + fc["effort"] if fc["effort"] else "",
                 final, fc["open_points"], fc["table"] if _is_clean(fc["table"], reg) else "the table"))
    return EXIT_OK


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
    """`awb review init|claims|l0|pass|status|show|calibrate`. Exit 0 ok, 1 blocked or refused, 2 error."""
    from awb.cli import SafeParser

    argv = list(sys.argv[1:] if argv is None else argv)
    prog = "awb review"
    ap = SafeParser(prog=prog, description="Review a deliverable: contract, claims, level-0 check, pass record.")
    sub = ap.add_subparsers(dest="command", parser_class=SafeParser)
    s = sub.add_parser("init", help="write the contract of a deliverable")
    s.add_argument("deliverable", type=Path)
    s.add_argument("--request", required=True, type=Path, metavar="FILE", help="the request as a text file")
    s.add_argument("--tier", default=None, type=int, choices=TIERS,
                   help="the tier of this deliverable (default: the review tier of the project; raise it for a "
                        "file only when he says so)")
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
    s = sub.add_parser("show", help="the record state of one deliverable and its final check (FX) when there is one")
    s.add_argument("deliverable", type=Path)
    s.set_defaults(func=_cmd_show)
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
        if getattr(args, "deliverable", None) is not None:
            try:
                _print_name_hint(*_hint_args(args.deliverable), file=sys.stderr)
            except Exception:
                pass
        return EXIT_FINDINGS
    except ReviewError as err:
        print("%s: %s" % (prog, err), file=sys.stderr)
        return EXIT_ERROR
    except OSError as err:
        print("%s: %s (operating system error)" % (prog, type(err).__name__), file=sys.stderr)
        return EXIT_ERROR


if __name__ == "__main__":
    sys.exit(main())
